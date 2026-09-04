import csv
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ebc_cli.csvlog import StatusCsvLogger
from ebc_cli.device import DeviceError
from ebc_cli.profiles import make_bulk_to_float_profile
from ebc_cli.protocol import (
    FRAME_PREFIX,
    FRAME_SUFFIX,
    STATUS_CHECKSUM_INDEX,
    EbcStatus,
    checksum,
    parse_status,
)
from ebc_cli.session import SessionStopReason, SupervisedSession, run_bulk_to_float_session


class FakeTester:
    def __init__(self, statuses: list[EbcStatus]):
        self.statuses = list(statuses)
        self.bulk_calls: list[dict[str, object]] = []
        self.float_calls: list[dict[str, object]] = []
        self.stop_calls = 0

    def begin_lead_acid_charge(self, **kwargs: object) -> EbcStatus:
        self.bulk_calls.append(kwargs)
        return _status()

    def begin_float_charge(self, **kwargs: object) -> EbcStatus:
        self.float_calls.append(kwargs)
        return _status()

    def next_status(self) -> EbcStatus:
        if not self.statuses:
            raise DeviceError("fake status stream ended")
        return self.statuses.pop(0)

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
                baseline = profile.start_bulk(tester)
                logger.write(baseline, event=profile.bulk_baseline_event)
                with SupervisedSession(tester, logger) as supervised:
                    supervised.mark_started(profile.start_event)
                    result = run_bulk_to_float_session(
                        session=supervised,
                        tester=tester,
                        logger=logger,
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
        self.assertEqual(tester.bulk_calls, [{"current_amps": 0.8, "timeout_minutes": 2}])
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
                tester=tester,
                logger=None,
                printer=lambda _status: None,
                profile=profile,
                poll_interval=60,
                clock=clock,
                sleeper=clock.sleep,
            )

        self.assertEqual(result.stop_reason, SessionStopReason.BULK_TIMEOUT)
        self.assertEqual(tester.float_calls, [])
        self.assertEqual(tester.stop_calls, 1)


def _status(*, state: int = 0x68) -> EbcStatus:
    frame = bytearray(19)
    frame[0] = FRAME_PREFIX
    frame[1] = state
    frame[2:4] = bytes((0, 70))
    frame[4:6] = bytes((50, 0))
    frame[6:8] = bytes((0, 100))
    frame[16] = 0x06
    frame[STATUS_CHECKSUM_INDEX] = checksum(frame, STATUS_CHECKSUM_INDEX)
    frame[-1] = FRAME_SUFFIX
    return parse_status(bytes(frame))


if __name__ == "__main__":
    unittest.main()
