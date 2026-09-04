import math
import unittest
from unittest.mock import Mock, call, patch

from ebc_cli.device import DeviceError, EbcDevice
from ebc_cli.protocol import (
    CONNECT,
    FRAME_PREFIX,
    FRAME_SUFFIX,
    STATUS_CHECKSUM_INDEX,
    STOP,
    ProtocolError,
    checksum,
    decode_current,
    decode_voltage,
    encode_scaled,
    encode_timer,
    find_valid_status_frame,
    make_cccv_charge_plan,
    make_lead_acid_charge_plan,
    parse_status,
)


class ProtocolSafetyTests(unittest.TestCase):
    def test_status_requires_frame_suffix(self) -> None:
        frame = bytearray(status_frame())
        frame[-1] = 0x00
        with self.assertRaisesRegex(ProtocolError, "end with F8"):
            parse_status(bytes(frame))

    def test_frame_finder_skips_status_with_bad_suffix(self) -> None:
        bad = bytearray(status_frame())
        bad[-1] = 0x00
        self.assertIsNone(find_valid_status_frame(bytes(bad)))

    def test_scaled_encoding_rejects_non_finite_or_non_numeric_values(self) -> None:
        for value in (math.nan, math.inf, -math.inf, "not-a-number"):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                encode_scaled(value)
        for factor in (0, -1, math.nan, math.inf, "not-a-factor"):
            with self.subTest(factor=factor), self.assertRaises(ProtocolError):
                encode_scaled(1.0, factor)

    def test_decoding_rejects_non_finite_or_non_positive_factors(self) -> None:
        for decoder in (decode_current, decode_voltage):
            for factor in (0, -1, math.nan, math.inf, "not-a-factor"):
                with (
                    self.subTest(decoder=decoder.__name__, factor=factor),
                    self.assertRaises(ProtocolError),
                ):
                    decoder(bytes((0, 70)), factor)

    def test_timer_requires_integer_minutes(self) -> None:
        for minutes in (1.5, math.nan, "60", True):
            with self.subTest(minutes=minutes), self.assertRaises(ProtocolError):
                encode_timer(minutes)

    def test_status_can_inspect_an_unknown_model(self) -> None:
        status = parse_status(status_frame(model_id=0x99))
        self.assertEqual(status.model_id, 0x99)
        self.assertEqual(status.model_name, "unknown (0x99)")

    def test_device_lock_reports_busy_port(self) -> None:
        device = EbcDevice("/dev/test", timeout=0.1)
        with (
            patch("ebc_cli.device.fcntl.flock", side_effect=BlockingIOError),
            self.assertRaisesRegex(DeviceError, "already in use"),
        ):
            device._acquire_lock(42)

    def test_device_lock_can_be_disabled(self) -> None:
        device = EbcDevice("/dev/test", timeout=0.1, lock_device=False)
        with patch("ebc_cli.device.fcntl.flock") as flock:
            device._acquire_lock(42)
        flock.assert_not_called()

    def test_begin_rejects_unknown_model_before_stop_or_profile(self) -> None:
        device = EbcDevice(timeout=0.1)
        device._fd = 1
        device._write_all = Mock()
        device._read_status_frame = Mock(return_value=status_frame(model_id=0x99))

        with self.assertRaisesRegex(DeviceError, "validated control profile"):
            device.begin_float_charge(current_amps=0.5, voltage_volts=13.7, timeout_minutes=120)

        device._write_all.assert_called_once_with(CONNECT)
        self.assertFalse(device._control_active)

    def test_stop_sends_emergency_frame_without_status_query(self) -> None:
        device = EbcDevice(timeout=0.1)
        device._fd = 1
        device._write_all = Mock()
        device._read_status_frame = Mock(return_value=status_frame(model_id=0x99))

        device.stop()

        device._write_all.assert_called_once_with(STOP)
        device._read_status_frame.assert_not_called()

    def test_begin_verifies_model_before_control_frames(self) -> None:
        device = EbcDevice(timeout=0.1)
        device._fd = 1
        device._write_all = Mock()
        device._read_status_frame = Mock(side_effect=[status_frame(), status_frame()])

        baseline = device.begin_float_charge(
            current_amps=0.5, voltage_volts=13.7, timeout_minutes=120
        )

        self.assertEqual(baseline.model_id, 0x06)
        self.assertTrue(device._control_active)
        self.assertEqual(
            device._write_all.call_args_list,
            [
                call(CONNECT),
                call(STOP),
                call(CONNECT),
                call(
                    make_cccv_charge_plan(current_amps=0.5, voltage_volts=13.7, timeout_minutes=120)
                ),
            ],
        )

    def test_begin_lead_acid_charge_uses_the_dedicated_pb_ac_profile(self) -> None:
        device = EbcDevice(timeout=0.1)
        device._fd = 1
        device._write_all = Mock()
        device._read_status_frame = Mock(side_effect=[status_frame(), status_frame()])

        baseline = device.begin_lead_acid_charge(current_amps=0.8, timeout_minutes=1440)

        self.assertEqual(baseline.model_id, 0x06)
        self.assertEqual(
            device._write_all.call_args_list,
            [
                call(CONNECT),
                call(STOP),
                call(CONNECT),
                call(make_lead_acid_charge_plan(current_amps=0.8, cells=6, timeout_minutes=1440)),
            ],
        )

    def test_close_makes_best_effort_stop_for_active_profile(self) -> None:
        device = EbcDevice(timeout=0.1)
        device._fd = 42
        device._control_active = True
        device._write_all = Mock()

        with patch("ebc_cli.device.os.close") as close:
            device.close()

        device._write_all.assert_called_once_with(STOP)
        close.assert_called_once_with(42)
        self.assertIsNone(device._fd)
        self.assertFalse(device._control_active)

    def test_close_does_not_send_stop_for_idle_connection(self) -> None:
        device = EbcDevice(timeout=0.1)
        device._fd = 42
        device._write_all = Mock()

        with patch("ebc_cli.device.os.close") as close:
            device.close()

        device._write_all.assert_not_called()
        close.assert_called_once_with(42)


def status_frame(*, model_id: int = 0x06) -> bytes:
    frame = bytearray(19)
    frame[0] = FRAME_PREFIX
    frame[1] = 0x68
    frame[2:4] = bytes((0, 70))
    frame[4:6] = bytes((50, 0))
    frame[6:8] = bytes((0, 100))
    frame[14:16] = bytes((3, 0))
    frame[16] = model_id
    frame[STATUS_CHECKSUM_INDEX] = checksum(frame, STATUS_CHECKSUM_INDEX)
    frame[-1] = FRAME_SUFFIX
    return bytes(frame)


if __name__ == "__main__":
    unittest.main()
