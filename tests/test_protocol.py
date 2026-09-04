import csv
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from ebc_cli.cli import main
from ebc_cli.csvlog import CSV_HEADER, StatusCsvLogger
from ebc_cli.device import DeviceError, EbcDevice
from ebc_cli.protocol import (
    CONNECT,
    FRAME_PREFIX,
    FRAME_SUFFIX,
    STATUS_CHECKSUM_INDEX,
    EbcCompletionReason,
    ProtocolError,
    checksum,
    decode_capacity,
    decode_current,
    decode_timer,
    decode_voltage,
    encode_scaled,
    encode_timer,
    find_valid_status_frame,
    make_cccv_charge_plan,
    make_constant_current_discharge_plan,
    make_lead_acid_charge_plan,
    parse_status,
)


class ProtocolTests(unittest.TestCase):
    def test_csv_logger_writes_header_and_raw_frame(self) -> None:
        status = parse_status(_status_frame())
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "logs" / "capacity.csv"
            with StatusCsvLogger(path) as logger:
                logger.write(status, timestamp=datetime(2026, 9, 3, 20, 0, tzinfo=UTC))
            with path.open(newline="", encoding="utf-8") as csv_file:
                rows = list(csv.reader(csv_file))
        self.assertEqual(tuple(rows[0]), CSV_HEADER)
        self.assertEqual(rows[1][0], "2026-09-03T20:00:00+00:00")
        self.assertEqual(rows[1][1], "status")
        self.assertEqual(rows[1][-1], status.raw.hex().upper())

    def test_csv_logger_writes_event_row(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "event.csv"
            with StatusCsvLogger(path) as logger:
                logger.write_event("stop-sent", timestamp=datetime(2026, 9, 3, 20, 0, tzinfo=UTC))
            with path.open(newline="", encoding="utf-8") as csv_file:
                rows = list(csv.reader(csv_file))
        self.assertEqual(rows[1][1], "stop-sent")
        self.assertTrue(all(value == "" for value in rows[1][2:]))

    def test_known_connect_packet_is_already_valid(self) -> None:
        self.assertEqual(CONNECT.hex().upper(), "FA0500000000000005F8")

    def test_checksum_uses_the_ebc_framing_escape(self) -> None:
        frame = bytearray(19)
        frame[1] = 0xF3
        self.assertEqual(checksum(frame, STATUS_CHECKSUM_INDEX), 0x03)

    def test_scaled_conversion_matches_lazarus_formula(self) -> None:
        self.assertEqual(encode_scaled(1.0), bytes((0, 100)))
        self.assertEqual(encode_scaled(12.0), bytes((5, 0)))
        self.assertEqual(decode_current(bytes((0, 70))), 0.7)
        self.assertEqual(decode_voltage(bytes((50, 0))), 12.0)
        self.assertEqual(decode_capacity(bytes((0, 100))), 0.1)

    def test_timer_matches_lazarus_formula_and_reserved_value(self) -> None:
        self.assertEqual(encode_timer(720), bytes((3, 0)))
        self.assertEqual(decode_timer(bytes((3, 0))), 720)
        self.assertEqual(encode_timer(250), bytes((1, 9)))

    def test_pb_charge_plan_is_fully_framed(self) -> None:
        plan = make_lead_acid_charge_plan(current_amps=0.7, cells=6, timeout_minutes=720)
        self.assertEqual(plan.hex().upper(), "FA6100460006030022F8")

    def test_cccv_charge_plan_encodes_current_voltage_and_timeout(self) -> None:
        plan = make_cccv_charge_plan(current_amps=0.5, voltage_volts=13.7, timeout_minutes=120)
        self.assertEqual(plan.hex().upper(), "FA71003205AA007894F8")

    def test_cc_discharge_plan_encodes_current_cutoff_and_timeout(self) -> None:
        plan = make_constant_current_discharge_plan(
            current_amps=0.8, cutoff_volts=10.5, timeout_minutes=1440
        )
        self.assertEqual(plan.hex().upper(), "FA010050045A060009F8")

    def test_status_parser_decodes_a_valid_frame(self) -> None:
        status = parse_status(_status_frame())
        self.assertEqual(status.model_name, "EBC-A10H")
        self.assertEqual(status.current_amps, 0.7)
        self.assertEqual(status.voltage_volts, 12.0)
        self.assertEqual(status.capacity_ah, 0.1)
        self.assertEqual(status.device_timer_minutes, 720)
        self.assertIsNone(status.completion_reason)

    def test_status_exposes_capacity_cutoff_reason(self) -> None:
        status = parse_status(_status_frame(state=0x14))
        self.assertIs(status.completion_reason, EbcCompletionReason.CAPACITY_CUTOFF)

    def test_status_parser_rejects_bad_checksum(self) -> None:
        frame = bytearray(19)
        frame[0] = FRAME_PREFIX
        frame[STATUS_CHECKSUM_INDEX] = 0xFF
        with self.assertRaises(ProtocolError):
            parse_status(bytes(frame))

    def test_frame_finder_skips_non_status_reply(self) -> None:
        status = bytearray(19)
        status[0] = FRAME_PREFIX
        status[16] = 0x06
        status[STATUS_CHECKSUM_INDEX] = checksum(status, STATUS_CHECKSUM_INDEX)
        status[-1] = FRAME_SUFFIX
        self.assertEqual(find_valid_status_frame(b"\xfa\x01\x02" + bytes(status)), bytes(status))

    def test_device_rejects_non_positive_timeout_before_opening_port(self) -> None:
        with self.assertRaises(DeviceError):
            EbcDevice("/dev/ttyUSB0", timeout=0)

    def test_recovery_requires_two_explicit_acknowledgements(self) -> None:
        self.assertEqual(main(["recover-lead-acid"]), 2)

    def test_recovery_rejects_an_excessive_current_before_opening_port(self) -> None:
        self.assertEqual(
            main(
                [
                    "recover-lead-acid",
                    "--current",
                    "0.21",
                    "--execute",
                    "--i-am-present",
                ]
            ),
            2,
        )

    def test_float_charge_rejects_an_unsafe_voltage_before_opening_port(self) -> None:
        self.assertEqual(
            main(
                [
                    "float-charge",
                    "--voltage",
                    "14.4",
                    "--execute",
                    "--i-am-present",
                ]
            ),
            2,
        )

    def test_capacity_test_requires_two_explicit_acknowledgements(self) -> None:
        self.assertEqual(main(["capacity-test"]), 2)


def _status_frame(*, state: int = 0x68) -> bytes:
    frame = bytearray(19)
    frame[0] = FRAME_PREFIX
    frame[1] = state
    frame[2:4] = bytes((0, 70))  # 0.700 A
    frame[4:6] = bytes((50, 0))  # 12.000 V
    frame[6:8] = bytes((0, 100))  # 0.100 Ah
    frame[14:16] = bytes((3, 0))  # 720 minutes
    frame[16] = 0x06
    frame[STATUS_CHECKSUM_INDEX] = checksum(frame, STATUS_CHECKSUM_INDEX)
    frame[-1] = FRAME_SUFFIX
    return bytes(frame)


if __name__ == "__main__":
    unittest.main()
