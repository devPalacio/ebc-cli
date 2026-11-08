import os
import pty
import queue
import select
import threading
import time
import unittest

from ebc_cli.capabilities import A10H_CAPABILITIES
from ebc_cli.device import DeviceError, EbcDevice
from ebc_cli.modes import CCCV_CHARGE, EbcModeCommand, build_mode_packet
from ebc_cli.protocol import (
    CONNECT,
    FRAME_PREFIX,
    FRAME_SUFFIX,
    STOP,
    checksum,
)


class PtyDeviceTests(unittest.TestCase):
    def test_default_response_timeout_allows_a_delayed_stream_frame(self) -> None:
        status_frame = _status_frame()
        peer_errors: queue.Queue[BaseException] = queue.Queue()

        def peer() -> None:
            try:
                self.assertEqual(_read_exact(self.master_fd, len(CONNECT)), CONNECT)
                time.sleep(1.5)
                os.write(self.master_fd, status_frame)
            except (AssertionError, EOFError, OSError, TimeoutError) as exc:
                peer_errors.put(exc)

        thread = threading.Thread(target=peer)
        thread.start()
        try:
            with EbcDevice(self.slave_path) as device:
                status = device.status()
        finally:
            thread.join(timeout=2.0)
        self._assert_peer_finished(thread, peer_errors)
        self.assertEqual(status.voltage_volts, 12.0)

    def test_session_budget_bounds_a_silent_serial_read(self) -> None:
        with EbcDevice(self.slave_path, timeout=10) as device:
            started = time.monotonic()
            with self.assertRaises(DeviceError):
                device.next_status(timeout=0.02, latest=True)
            self.assertLess(time.monotonic() - started, 1.0)

    def test_next_status_latest_returns_newest_buffered_observation(self) -> None:
        first = _status_frame(voltage_raw=50)
        middle = _status_frame(voltage_raw=55)
        newest = _status_frame(voltage_raw=60)

        def peer() -> None:
            time.sleep(0.02)
            os.write(self.master_fd, first + middle + newest)

        thread = threading.Thread(target=peer)
        thread.start()
        try:
            with EbcDevice(self.slave_path, timeout=0.5) as device:
                status = device.next_status(latest=True)
        finally:
            thread.join(timeout=1.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(status.voltage_volts, 14.4)

    def test_profile_start_discards_pre_start_completion_frames(self) -> None:
        baseline = _status_frame(state=0x68)
        stale_completion = _status_frame(state=0x1A, current_raw=0)
        running = _status_frame(state=0x68, current_raw=70)
        expected_profile = build_mode_packet(
            EbcModeCommand(CCCV_CHARGE, current_amps=0.5, voltage_volts=13.7, timeout_minutes=120),
            capabilities=A10H_CAPABILITIES,
        )
        commands_seen: list[bytes] = []
        errors: queue.Queue[BaseException] = queue.Queue()

        def peer() -> None:
            try:
                while len(commands_seen) < 5:
                    command = _read_exact(self.master_fd, 10)
                    commands_seen.append(command)
                    if command == CONNECT:
                        if len(commands_seen) == 1:
                            os.write(self.master_fd, baseline)
                        else:
                            # The completion frame is already queued when the
                            # baseline response is read, so it must be removed
                            # at the profile start boundary.
                            os.write(self.master_fd, baseline + stale_completion)
                    elif command == expected_profile:
                        os.write(self.master_fd, running)
            except (AssertionError, EOFError, OSError, TimeoutError) as exc:
                errors.put(exc)

        thread = threading.Thread(target=peer)
        thread.start()
        try:
            with EbcDevice(self.slave_path, timeout=0.5) as device:
                device.begin_mode(
                    EbcModeCommand(
                        CCCV_CHARGE,
                        current_amps=0.5,
                        voltage_volts=13.7,
                        timeout_minutes=120,
                    )
                )
                status = device.next_status()
        finally:
            thread.join(timeout=1.2)
        self._assert_peer_finished(thread, errors)
        self.assertIsNone(status.completion_reason)
        self.assertEqual(commands_seen, [CONNECT, STOP, CONNECT, expected_profile, STOP])

    def test_fresh_status_skips_cached_first_frame(self) -> None:
        old = _status_frame()
        fresh = bytearray(old)
        fresh[4:6] = bytes((60, 0))
        fresh[17] = checksum(fresh, 17)
        latest = bytearray(old)
        latest[4:6] = bytes((65, 0))
        latest[17] = checksum(latest, 17)
        errors: queue.Queue[BaseException] = queue.Queue()

        def peer() -> None:
            try:
                self.assertEqual(_read_exact(self.master_fd, len(CONNECT)), CONNECT)
                os.write(self.master_fd, old)
                time.sleep(0.02)
                os.write(self.master_fd, bytes(fresh) + bytes(latest))
            except (AssertionError, EOFError, OSError, TimeoutError) as exc:
                errors.put(exc)

        thread = threading.Thread(target=peer)
        thread.start()
        try:
            with EbcDevice(self.slave_path, timeout=0.5) as device:
                status = device.fresh_status()
            self.assertEqual(status.voltage_volts, 15.6)
        finally:
            thread.join(timeout=1.2)
        self._assert_peer_finished(thread, errors)

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

    def test_fresh_status_does_not_fall_back_when_followup_is_missing(self) -> None:
        def peer() -> None:
            _read_exact(self.master_fd, len(CONNECT))
            os.write(self.master_fd, _status_frame())

        thread = threading.Thread(target=peer)
        thread.start()
        try:
            with (
                EbcDevice(self.slave_path, timeout=0.05) as device,
                self.assertRaisesRegex(DeviceError, "could not confirm a fresh status"),
            ):
                device.fresh_status()
        finally:
            thread.join(timeout=1.2)
        self.assertFalse(thread.is_alive())

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
        expected_profile = build_mode_packet(
            EbcModeCommand(CCCV_CHARGE, current_amps=0.5, voltage_volts=13.7, timeout_minutes=120),
            capabilities=A10H_CAPABILITIES,
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
            baseline = device.begin_mode(
                EbcModeCommand(
                    CCCV_CHARGE,
                    current_amps=0.5,
                    voltage_volts=13.7,
                    timeout_minutes=120,
                )
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


def _status_frame(*, state: int = 0x68, voltage_raw: int = 50, current_raw: int = 70) -> bytes:
    frame = bytearray(19)
    frame[0] = FRAME_PREFIX
    frame[1] = state
    frame[2:4] = bytes((0, current_raw))
    frame[4:6] = bytes((voltage_raw, 0))
    frame[6:8] = bytes((0, 100))
    frame[16] = 0x06
    frame[17] = checksum(frame, 17)
    frame[-1] = FRAME_SUFFIX
    return bytes(frame)


if __name__ == "__main__":
    unittest.main()
