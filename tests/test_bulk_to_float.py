import csv
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from support import FakeClock
from support import ProfileTester as FakeTester
from support import status as _status

from ebc_cli.csvlog import StatusCsvLogger
from ebc_cli.profiles import make_bulk_to_float_profile
from ebc_cli.session import SessionStopReason, SupervisedSession, run_bulk_to_float_session


class BulkToFloatSessionTests(unittest.TestCase):
    def test_auto_off_transitions_to_float_and_logs_each_phase(self) -> None:
        tester = FakeTester([_status(), _status(state=0x1A), _status()])
        clock = FakeClock()
        profile = make_bulk_to_float_profile(
            bulk_current_amps=0.8,
            bulk_timeout_minutes=2,
            float_current_amps=0.5,
            float_voltage_volts=13.7,
            float_timeout_minutes=1,
        )

        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "bulk-to-float.csv"
            with StatusCsvLogger(path) as logger:
                baseline = tester.begin_mode(profile.bulk_start_command)
                logger.write(baseline, event=profile.bulk_baseline_event)
                with SupervisedSession(tester, logger) as supervised:
                    supervised.mark_started(profile.start_event)
                    result = run_bulk_to_float_session(
                        session=supervised,
                        printer=lambda _status: None,
                        profile=profile,
                        poll_interval=60,
                        clock=clock,
                        sleeper=clock.sleep,
                    )
            with path.open(newline="", encoding="utf-8") as csv_file:
                rows = list(csv.reader(csv_file))

        self.assertEqual(result.stop_reason, SessionStopReason.FLOAT_TIMEOUT)
        self.assertEqual(result.samples, 3)
        self.assertEqual(
            tester.bulk_calls,
            [{"current_amps": 0.8, "cells": 6, "timeout_minutes": 2}],
        )
        self.assertEqual(
            tester.float_calls,
            [{"current_amps": 0.5, "voltage_volts": 13.7, "timeout_minutes": 1}],
        )
        self.assertEqual(tester.stop_calls, 1)
        self.assertEqual(
            [row[1] for row in rows[2:]],
            [
                "bulk-to-float-bulk-baseline",
                "bulk-to-float-started",
                "bulk-to-float-bulk-sample",
                "bulk-to-float-bulk-sample",
                "bulk-to-float-float-baseline",
                "bulk-to-float-float-started",
                "bulk-to-float-float-sample",
                "float-timeout-stop-sent",
            ],
        )

    def test_bulk_timeout_stops_without_starting_float(self) -> None:
        tester = FakeTester([_status()])
        clock = FakeClock()
        profile = make_bulk_to_float_profile(
            bulk_current_amps=0.8,
            bulk_timeout_minutes=1,
            float_current_amps=0.5,
            float_voltage_volts=13.7,
            float_timeout_minutes=1,
        )

        with SupervisedSession(tester, None) as supervised:
            supervised.mark_started(profile.start_event)
            result = run_bulk_to_float_session(
                session=supervised,
                printer=lambda _status: None,
                profile=profile,
                poll_interval=60,
                clock=clock,
                sleeper=clock.sleep,
            )

        self.assertEqual(result.stop_reason, SessionStopReason.BULK_TIMEOUT)
        self.assertEqual(tester.float_calls, [])
        self.assertEqual(tester.stop_calls, 1)


if __name__ == "__main__":
    unittest.main()
