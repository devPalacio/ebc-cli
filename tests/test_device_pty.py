import os
import pty
import queue
import select
import threading
import time
import unittest

from ebc_cli.device import DeviceError, EbcDevice
from ebc_cli.protocol import (
    CONNECT,
    FRAME_PREFIX,
    FRAME_SUFFIX,
    STOP,
    checksum,
    make_cccv_charge_plan,
)


class PtyDeviceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.master_fd, self.slave_fd = pty.openpty()
        self.slave_path = os.ttyname(self.slave_fd)

    def tearDown(self) -> None:
        for attribute in ("master_fd", "slave_fd"):
            fd = getattr(self, attribute, None)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, attribute, None)

    def test_status_reassembles_a_split_frame_from_the_serial_adapter(self) -> None:
        status_frame = _status_frame()
        peer_errors: queue.Queue[BaseException] = queue.Queue()

        def peer() -> None:
            try:
                self.assertEqual(_read_exact(self.master_fd, len(CONNECT)), CONNECT)
                os.write(self.master_fd, status_frame[:5])
                time.sleep(0.01)
                os.write(self.master_fd, status_frame[5:])
            except (AssertionError, EOFError, OSError, TimeoutError) as exc:
                peer_errors.put(exc)

        thread = threading.Thread(target=peer)
        thread.start()
        with EbcDevice(self.slave_path, timeout=0.5) as device:
            status = device.status()
        thread.join(timeout=1.0)
        self._assert_peer_finished(thread, peer_errors)

        self.assertEqual(status.voltage_volts, 12.0)
        self.assertEqual(status.current_amps, 0.7)

    def test_status_timeout_is_reported_when_the_serial_peer_is_silent(self) -> None:
        with (
            EbcDevice(self.slave_path, timeout=0.05) as device,
            self.assertRaisesRegex(DeviceError, "checksum-valid status frame"),
        ):
            device.status()

    def test_status_disconnect_is_reported_as_a_device_error(self) -> None:
        peer_errors: queue.Queue[BaseException] = queue.Queue()

        def peer() -> None:
            try:
                self.assertEqual(_read_exact(self.master_fd, len(CONNECT)), CONNECT)
                os.close(self.master_fd)
                self.master_fd = None
            except (AssertionError, EOFError, OSError, TimeoutError) as exc:
                peer_errors.put(exc)

        thread = threading.Thread(target=peer)
        thread.start()
        with (
            EbcDevice(self.slave_path, timeout=0.5) as device,
            self.assertRaises(DeviceError),
        ):
            device.status()
        thread.join(timeout=1.0)
        self._assert_peer_finished(thread, peer_errors)

    def test_active_profile_cleanup_sends_stop_through_the_serial_adapter(self) -> None:
        status_frame = _status_frame()
        expected_profile = make_cccv_charge_plan(
            current_amps=0.5,
            voltage_volts=13.7,
            timeout_minutes=120,
        )
        commands_seen: list[bytes] = []
        peer_errors: queue.Queue[BaseException] = queue.Queue()

        def peer() -> None:
            try:
                while len(commands_seen) < 5:
                    command = _read_exact(self.master_fd, 10)
                    commands_seen.append(command)
                    if command == CONNECT:
                        os.write(self.master_fd, status_frame)
            except (AssertionError, EOFError, OSError, TimeoutError) as exc:
                peer_errors.put(exc)

        thread = threading.Thread(target=peer)
        thread.start()
        with EbcDevice(self.slave_path, timeout=0.5) as device:
            baseline = device.begin_float_charge(
                current_amps=0.5,
                voltage_volts=13.7,
                timeout_minutes=120,
            )
        thread.join(timeout=1.0)
        self._assert_peer_finished(thread, peer_errors)

        self.assertEqual(baseline.voltage_volts, 12.0)
        self.assertEqual(
            commands_seen,
            [CONNECT, STOP, CONNECT, expected_profile, STOP],
        )

    def _assert_peer_finished(
        self, thread: threading.Thread, peer_errors: queue.Queue[BaseException]
    ) -> None:
        self.assertFalse(thread.is_alive(), "PTY peer did not finish")
        try:
            error = peer_errors.get_nowait()
        except queue.Empty:
            return
        raise error


def _read_exact(fd: int, size: int, timeout: float = 1.0) -> bytes:
    deadline = time.monotonic() + timeout
    received = bytearray()
    while len(received) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"received {len(received)} of {size} bytes")
        readable, _, _ = select.select([fd], [], [], remaining)
        if not readable:
            raise TimeoutError(f"received {len(received)} of {size} bytes")
        chunk = os.read(fd, size - len(received))
        if not chunk:
            raise EOFError("PTY peer closed before the expected frame")
        received.extend(chunk)
    return bytes(received)


def _status_frame() -> bytes:
    frame = bytearray(19)
    frame[0] = FRAME_PREFIX
    frame[2:4] = bytes((0, 70))
    frame[4:6] = bytes((50, 0))
    frame[6:8] = bytes((0, 100))
    frame[16] = 0x06
    frame[17] = checksum(frame, 17)
    frame[-1] = FRAME_SUFFIX
    return bytes(frame)


if __name__ == "__main__":
    unittest.main()
