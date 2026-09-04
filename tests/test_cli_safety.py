from __future__ import annotations

import csv
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ebc_cli import cli
from ebc_cli.csvlog import StatusCsvLogger
from ebc_cli.device import DeviceError
from ebc_cli.protocol import EbcStatus, FRAME_PREFIX, FRAME_SUFFIX, checksum


class FakeTester:
    def __init__(self, statuses: list[EbcStatus] | None = None):
        self.statuses = list(statuses or [])
        self.stop_calls = 0

    def __enter__(self) -> "FakeTester":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        pass

    def begin_float_charge(self, **_kwargs: object) -> EbcStatus:
        return _status()

    def begin_lead_acid_recovery(self, **_kwargs: object) -> EbcStatus:
        return _status()

    def begin_constant_current_discharge(self, **_kwargs: object) -> EbcStatus:
        return _status()

    def next_status(self) -> EbcStatus:
        if not self.statuses:
            raise DeviceError("fake status stream ended")
        status = self.statuses.pop(0)
        if isinstance(status, BaseException):
            raise status
        return status

    def status(self) -> EbcStatus:
        return self.next_status()

    def stop(self) -> None:
        self.stop_calls += 1


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class CliSafetyTests(unittest.TestCase):
    def test_active_session_stops_and_logs_at_timeout(self) -> None:
        tester = FakeTester([_status()])
        clock = FakeClock()
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "session.csv"
            with StatusCsvLogger(path) as logger:
                with cli._SupervisedSession(tester, logger) as session:
                    session.mark_started("test-started")
                    with patch.object(cli, "_print_status"):
                        cli._run_active_session(
                            session=session,
                            tester=tester,
                            logger=logger,
                            timeout_minutes=1,
                            sample_event="test-sample",
                            poll_interval=60,
                            clock=clock,
                            sleeper=clock.sleep,
                        )
            with path.open(newline="", encoding="utf-8") as csv_file:
                rows = list(csv.reader(csv_file))

        self.assertEqual(tester.stop_calls, 1)
        self.assertEqual([row[1] for row in rows[1:]], [
            "test-started",
            "test-sample",
            "timeout-stop-sent",
        ])
        self.assertEqual(rows[2][2], "0.000")

    def test_active_session_stops_after_serial_error(self) -> None:
        tester = FakeTester([DeviceError("serial read failed")])
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "session.csv"
            with StatusCsvLogger(path) as logger:
                with self.assertRaises(DeviceError):
                    with cli._SupervisedSession(tester, logger) as session:
                        session.mark_started("test-started")
                        cli._run_active_session(
                            session=session,
                            tester=tester,
                            logger=logger,
                            timeout_minutes=1,
                            sample_event="test-sample",
                            clock=lambda: 0.0,
                            sleeper=lambda _seconds: None,
                        )
            with path.open(newline="", encoding="utf-8") as csv_file:
                rows = list(csv.reader(csv_file))

        self.assertEqual(tester.stop_calls, 1)
        self.assertEqual(rows[-1][1], "error-stop-sent")

    def test_active_session_stops_after_termination_signal(self) -> None:
        tester = FakeTester([cli._SessionSignal(15)])
        with self.assertRaises(cli._SessionSignal):
            with cli._SupervisedSession(tester, None) as session:
                session.mark_started("test-started")
                cli._run_active_session(
                    session=session,
                    tester=tester,
                    logger=None,
                    timeout_minutes=1,
                    sample_event="test-sample",
                    clock=lambda: 0.0,
                    sleeper=lambda _seconds: None,
                )
        self.assertEqual(tester.stop_calls, 1)

    def test_active_session_stops_after_keyboard_interrupt(self) -> None:
        tester = FakeTester([KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            with cli._SupervisedSession(tester, None) as session:
                session.mark_started("test-started")
                cli._run_active_session(
                    session=session,
                    tester=tester,
                    logger=None,
                    timeout_minutes=1,
                    sample_event="test-sample",
                    clock=lambda: 0.0,
                    sleeper=lambda _seconds: None,
                )
        self.assertEqual(tester.stop_calls, 1)

    def test_recovery_main_uses_a_supervised_logged_session(self) -> None:
        tester = FakeTester()
        with TemporaryDirectory() as temporary_directory:
            with patch.object(cli, "EbcDevice", return_value=tester), patch.object(
                cli, "_run_active_session", autospec=True
            ) as run_session:
                result = cli.main(
                    [
                        "recover-lead-acid",
                        "--execute",
                        "--i-am-present",
                        "--timeout-minutes",
                        "1",
                        "--log-dir",
                        temporary_directory,
                    ]
                )
            paths = list(Path(temporary_directory).glob("*.csv"))
            with paths[0].open(newline="", encoding="utf-8") as csv_file:
                rows = list(csv.reader(csv_file))

        self.assertEqual(result, 0)
        self.assertEqual(run_session.call_count, 1)
        self.assertEqual(tester.stop_calls, 1)
        self.assertEqual(rows[1][1], "recovery-baseline")
        self.assertEqual(rows[2][1], "recovery-started")


def _status(*, state: int = 0x68) -> EbcStatus:
    frame = bytearray(19)
    frame[0] = FRAME_PREFIX
    frame[1] = state
    frame[2:4] = bytes((0, 70))
    frame[4:6] = bytes((50, 0))
    frame[6:8] = bytes((0, 100))
    frame[16] = 0x06
    frame[17] = checksum(frame, 17)
    frame[-1] = FRAME_SUFFIX
    return EbcStatus(
        voltage_volts=12.0,
        current_amps=0.7,
        capacity_ah=0.1,
        device_timer_minutes=0,
        model_id=0x06,
        raw=bytes(frame),
    )


if __name__ == "__main__":
    unittest.main()
