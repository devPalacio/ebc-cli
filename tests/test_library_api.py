# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Jay (Python CLI contributions)
# See NOTICE for upstream attribution and port details.

"""The documented library surface must import and drive hardware-free sessions."""

import contextlib
import os
import pty
import tempfile
import threading
import time
import unittest
from io import StringIO
from pathlib import Path

from support import ProfileTester, status

import ebc_cli
from ebc_cli import (
    DeviceError,
    SessionStopReason,
    StatusCsvLogger,
    build_mode_packet,
    make_bulk_to_float_profile,
    make_float_charge_profile,
    run_bulk_to_float,
    run_profile,
)
from ebc_cli.device import EbcDevice
from ebc_cli.protocol import CONNECT, FRAME_PREFIX, FRAME_SUFFIX, STATUS_CHECKSUM_INDEX, checksum

LIBRARY_EXAMPLE = (
    "EbcDevice",
    "SupervisedSession",
    "run_profile",
    "run_bulk_to_float",
    "make_float_charge_profile",
    "build_mode_packet",
    "StatusCsvLogger",
)


def frame(state: int = 0x68) -> bytes:
    raw = bytearray(19)
    raw[0] = FRAME_PREFIX
    raw[1] = state
    raw[2:4] = bytes((0, 70))
    raw[4:6] = bytes((50, 0))
    raw[6:8] = bytes((0, 100))
    raw[16] = 0x06
    raw[STATUS_CHECKSUM_INDEX] = checksum(bytes(raw), STATUS_CHECKSUM_INDEX)
    raw[-1] = FRAME_SUFFIX
    return bytes(raw)


def _read_exact(fd: int, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = os.read(fd, size - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


class LibrarySurfaceTests(unittest.TestCase):
    def test_every_advertised_name_imports_from_the_package(self) -> None:
        self.assertEqual(len(ebc_cli.__all__), len(set(ebc_cli.__all__)))
        for name in ebc_cli.__all__:
            with self.subTest(name=name):
                self.assertTrue(hasattr(ebc_cli, name))

    def test_documented_example_names_are_public(self) -> None:
        for name in LIBRARY_EXAMPLE:
            self.assertIn(name, ebc_cli.__all__)

    def test_package_ships_its_type_marker(self) -> None:
        self.assertTrue((Path(ebc_cli.__file__).parent / "py.typed").is_file())

    def test_run_profile_drives_a_supervised_session_without_the_cli(self) -> None:
        master, slave = pty.openpty()
        commands_seen: list[bytes] = []
        stop_stream = threading.Event()

        def peer() -> None:
            try:
                while len(commands_seen) < 4:
                    command = _read_exact(master, 10)
                    commands_seen.append(command)
                    if command == CONNECT:
                        os.write(master, frame())
                # A tester streams unsolicited status frames once started;
                # keep doing so until the caller has finished.
                while not stop_stream.is_set():
                    os.write(master, frame())
                    time.sleep(0.005)
            except EOFError, OSError:
                pass

        thread = threading.Thread(target=peer, daemon=True)
        thread.start()
        profile = make_float_charge_profile(current_amps=0.5, voltage_volts=13.7, timeout_minutes=1)
        printed: list[float] = []
        now = [0.0]
        stdout = StringIO()

        try:
            with (
                contextlib.redirect_stdout(stdout),
                EbcDevice(os.ttyname(slave), timeout=0.5, lock_device=False) as device,
            ):
                run = run_profile(
                    device,
                    profile,
                    on_sample=lambda sample: printed.append(sample.voltage_volts),
                    poll_interval=5.0,
                    clock=lambda: now[0],
                    sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
                )
        finally:
            stop_stream.set()
            thread.join(timeout=1.0)

        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(run.result.stop_reason, SessionStopReason.TIMEOUT)
        self.assertEqual(run.result.samples, len(printed))
        self.assertGreater(run.result.samples, 0)
        self.assertEqual(run.baseline.voltage_volts, 12.0)
        self.assertEqual(commands_seen[3], build_mode_packet(profile.start_command))
        self.assertEqual(len(commands_seen), 4)

    def test_run_profile_stops_the_tester_when_the_session_raises(self) -> None:
        tester = ProfileTester([status()])

        with self.assertRaises(DeviceError):
            run_profile(
                tester,
                make_float_charge_profile(current_amps=0.5, voltage_volts=13.7, timeout_minutes=1),
                poll_interval=5.0,
                clock=lambda: 0.0,
                sleeper=lambda _seconds: None,
            )

        self.assertEqual(tester.stop_calls, 1)
        self.assertEqual(len(tester.float_calls), 1)

    def test_run_bulk_to_float_starts_both_phases_and_stops(self) -> None:
        tester = ProfileTester([status(), status(state=0x1A), *[status()] * 30])
        profile = make_bulk_to_float_profile(
            bulk_current_amps=0.8,
            bulk_timeout_minutes=1,
            float_current_amps=0.5,
            float_voltage_volts=13.7,
            float_timeout_minutes=1,
        )
        now = [0.0]

        run = run_bulk_to_float(
            tester,
            profile,
            poll_interval=5.0,
            clock=lambda: now[0],
            sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )

        self.assertEqual(len(tester.bulk_calls), 1)
        self.assertEqual(len(tester.float_calls), 1)
        self.assertEqual(tester.bulk_calls[0]["current_amps"], 0.8)
        self.assertEqual(tester.float_calls[0]["voltage_volts"], 13.7)
        self.assertEqual(tester.stop_calls, 1)
        self.assertEqual(run.result.stop_reason, SessionStopReason.FLOAT_TIMEOUT)

    def test_library_run_writes_the_same_log_events_as_the_cli(self) -> None:
        tester = ProfileTester([status()] * 30)
        profile = make_float_charge_profile(current_amps=0.5, voltage_volts=13.7, timeout_minutes=1)
        now = [0.0]

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "library.csv"
            with StatusCsvLogger(path) as logger:
                run_profile(
                    tester,
                    profile,
                    logger=logger,
                    poll_interval=5.0,
                    clock=lambda: now[0],
                    sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
                )
            events = [line.split(",")[1] for line in path.read_text().splitlines()[2:]]

        self.assertEqual(events[:2], ["float-charge-started", "float-charge-baseline"])
        self.assertIn("float-charge-sample", events)
        self.assertEqual(events[-1], "timeout-stop-sent")


if __name__ == "__main__":
    unittest.main()
