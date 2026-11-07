import csv
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Self
from unittest.mock import patch

from support import FakeClock

from ebc_cli import cli, commands, resources, runner, session
from ebc_cli.csvlog import StatusCsvLogger
from ebc_cli.device import DeviceError
from ebc_cli.modes import EbcModeCommand
from ebc_cli.protocol import FRAME_PREFIX, FRAME_SUFFIX, EbcStatus, checksum


class FakeTester:
    def __init__(self, statuses: list[EbcStatus] | None = None):
        self.statuses = list(statuses or [])
        self.stop_calls = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        pass

    def begin_mode(self, _command: EbcModeCommand) -> EbcStatus:
        return _status()

    def next_status(self, *, timeout=None, latest=False) -> EbcStatus:
        if not self.statuses:
            raise DeviceError("fake status stream ended")
        status = self.statuses.pop(0)
        if isinstance(status, BaseException):
            raise status
        return status

    def status(self, *, timeout=None, latest=False) -> EbcStatus:
        return self.next_status()

    def stop(self) -> None:
        self.stop_calls += 1


class CliSafetyTests(unittest.TestCase):
    def test_active_session_stops_and_logs_at_timeout(self) -> None:
        tester = FakeTester([_status()])
        expected_final_status = tester.statuses[0]
        clock = FakeClock()
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "session.csv"
            with (
                StatusCsvLogger(path) as logger,
                session.SupervisedSession(tester, logger) as supervised,
            ):
                supervised.mark_started("test-started")
                result = session.run_active_session(
                    session=supervised,
                    printer=lambda _status: None,
                    timeout_minutes=1,
                    sample_event="test-sample",
                    poll_interval=60,
                    clock=clock,
                    sleeper=clock.sleep,
                )
            with path.open(newline="", encoding="utf-8") as csv_file:
                rows = list(csv.reader(csv_file))

        self.assertEqual(tester.stop_calls, 1)
        self.assertEqual(result.stop_reason, session.SessionStopReason.TIMEOUT)
        self.assertEqual(result.samples, 1)
        self.assertEqual(result.elapsed_seconds, 60.0)
        self.assertIs(result.final_status, expected_final_status)
        self.assertIsNotNone(result.measurement)
        self.assertEqual(result.measurement.sample_count, 1)
        self.assertEqual(result.measurement.amp_hours, 0.0)
        self.assertEqual(
            [row[1] for row in rows[2:]],
            [
                "test-started",
                "test-sample",
                "timeout-stop-sent",
            ],
        )
        self.assertEqual(rows[3][2], "0.000")

    def test_active_session_stops_after_serial_error(self) -> None:
        tester = FakeTester([DeviceError("serial read failed")])
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "session.csv"
            with (
                StatusCsvLogger(path) as logger,
                self.assertRaises(DeviceError),
                session.SupervisedSession(tester, logger) as supervised,
            ):
                supervised.mark_started("test-started")
                session.run_active_session(
                    session=supervised,
                    printer=lambda _status: None,
                    timeout_minutes=1,
                    sample_event="test-sample",
                    clock=lambda: 0.0,
                    sleeper=lambda _seconds: None,
                )
            with path.open(newline="", encoding="utf-8") as csv_file:
                rows = list(csv.reader(csv_file))

        self.assertEqual(tester.stop_calls, 1)
        self.assertEqual(rows[-1][1], "error-stop-sent")

    def test_active_session_stops_at_device_cutoff(self) -> None:
        expected_final_status = _status(state=0x14)
        tester = FakeTester([expected_final_status])
        clock = FakeClock()
        with session.SupervisedSession(tester, None) as supervised:
            supervised.mark_started("test-started")
            result = session.run_active_session(
                session=supervised,
                printer=lambda _status: None,
                timeout_minutes=1,
                sample_event="test-sample",
                clock=clock,
                sleeper=clock.sleep,
                stop_on_cutoff=True,
            )

        self.assertEqual(result.stop_reason, session.SessionStopReason.DEVICE_CUTOFF)
        self.assertEqual(result.samples, 1)
        self.assertIs(result.final_status, expected_final_status)
        self.assertIsNotNone(result.measurement)
        self.assertEqual(tester.stop_calls, 1)

    def test_active_session_accumulates_host_measurements(self) -> None:
        tester = FakeTester([_status(), _status(state=0x14)])
        clock = FakeClock()
        with session.SupervisedSession(tester, None) as supervised:
            supervised.mark_started("test-started")
            result = session.run_active_session(
                session=supervised,
                printer=lambda _status: None,
                timeout_minutes=1,
                sample_event="test-sample",
                clock=clock,
                sleeper=clock.sleep,
                stop_on_cutoff=True,
                poll_interval=1.0,
            )

        self.assertIsNotNone(result.measurement)
        self.assertEqual(result.measurement.sample_count, 2)
        self.assertEqual(result.measurement.integrated_intervals, 1)
        self.assertAlmostEqual(result.measurement.amp_hours, 0.7 / 3600)
        self.assertAlmostEqual(result.measurement.watt_hours, 8.4 / 3600)

    def test_active_session_without_samples_has_no_terminal_status(self) -> None:
        tester = FakeTester()
        with session.SupervisedSession(tester, None) as supervised:
            supervised.mark_started("test-started")
            result = session.run_active_session(
                session=supervised,
                printer=lambda _status: None,
                timeout_minutes=0,
                sample_event="test-sample",
                clock=lambda: 0.0,
                sleeper=lambda _seconds: None,
            )

        self.assertIsNone(result.final_status)
        self.assertIsNotNone(result.measurement)
        self.assertEqual(result.measurement.sample_count, 0)
        self.assertEqual(result.stop_reason, session.SessionStopReason.TIMEOUT)

    def test_session_result_reports_terminal_snapshot(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            commands._report_session_result(
                session.SessionResult(
                    stop_reason=session.SessionStopReason.DEVICE_CUTOFF,
                    samples=4,
                    elapsed_seconds=123.4,
                    final_status=_status(state=0x14),
                    measurement=session.MeasurementSummary(
                        sample_count=4,
                        integrated_intervals=3,
                        elapsed_seconds=123.4,
                        integrated_seconds=123.4,
                        amp_hours=1.25,
                        watt_hours=15.0,
                        skipped_intervals=0,
                        skipped_seconds=0.0,
                        ignored_samples=0,
                        first_timestamp=0.0,
                        last_timestamp=123.4,
                    ),
                )
            )

        rendered = output.getvalue()
        self.assertIn("Device cutoff reached; stop frame sent.", rendered)
        self.assertIn("Session: 123.4 s, 4 active sample(s)", rendered)
        self.assertIn("Terminal: EBC-A10H: 12.000 V", rendered)
        self.assertIn("Host integration: 1.250 Ah, 15.000 Wh", rendered)

    def test_monitor_session_stops_after_requested_count(self) -> None:
        tester = FakeTester([_status(), _status()])
        result = session.run_monitor_session(
            tester=tester,
            logger=None,
            printer=lambda _status: None,
            interval=1.0,
            count=2,
            sleeper=lambda _seconds: None,
        )

        self.assertEqual(result, session.MonitorResult(samples=2))

    def test_active_session_stops_after_termination_signal(self) -> None:
        tester = FakeTester([session._SessionSignal(15)])
        with (
            self.assertRaises(session._SessionSignal),
            session.SupervisedSession(tester, None) as supervised,
        ):
            supervised.mark_started("test-started")
            session.run_active_session(
                session=supervised,
                printer=lambda _status: None,
                timeout_minutes=1,
                sample_event="test-sample",
                clock=lambda: 0.0,
                sleeper=lambda _seconds: None,
            )
        self.assertEqual(tester.stop_calls, 1)

    def test_active_session_stops_after_keyboard_interrupt(self) -> None:
        tester = FakeTester([KeyboardInterrupt()])
        with (
            self.assertRaises(KeyboardInterrupt),
            session.SupervisedSession(tester, None) as supervised,
        ):
            supervised.mark_started("test-started")
            session.run_active_session(
                session=supervised,
                printer=lambda _status: None,
                timeout_minutes=1,
                sample_event="test-sample",
                clock=lambda: 0.0,
                sleeper=lambda _seconds: None,
            )
        self.assertEqual(tester.stop_calls, 1)

    def test_recovery_main_uses_a_supervised_logged_session(self) -> None:
        tester = FakeTester()
        with TemporaryDirectory() as temporary_directory:
            with (
                patch.object(resources, "EbcDevice", return_value=tester),
                patch.object(runner, "run_active_session", autospec=True) as run_session,
            ):
                run_session.return_value = session.SessionResult(
                    stop_reason=session.SessionStopReason.TIMEOUT,
                    samples=0,
                    elapsed_seconds=0.0,
                )
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
        self.assertEqual(rows[2][1], "recovery-started")
        self.assertEqual(rows[3][1], "recovery-baseline")

    def test_bulk_to_float_main_uses_a_supervised_logged_session(self) -> None:
        tester = FakeTester()
        with TemporaryDirectory() as temporary_directory:
            with (
                patch.object(resources, "EbcDevice", return_value=tester),
                patch.object(runner, "run_bulk_to_float_session", autospec=True) as run_session,
            ):
                run_session.return_value = session.SessionResult(
                    stop_reason=session.SessionStopReason.FLOAT_TIMEOUT,
                    samples=0,
                    elapsed_seconds=0.0,
                )
                result = cli.main(
                    [
                        "bulk-to-float",
                        "--execute",
                        "--i-am-present",
                        "--bulk-timeout-minutes",
                        "1",
                        "--float-timeout-minutes",
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
        self.assertEqual(rows[2][1], "bulk-to-float-started")
        self.assertEqual(rows[3][1], "bulk-to-float-bulk-baseline")


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
