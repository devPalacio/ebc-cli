import signal
import unittest
from unittest.mock import Mock

from support import FakeClock
from support import ProfileTester as FakeTester
from support import status as _status

from ebc_cli.device import DeviceError
from ebc_cli.profiles import make_bulk_to_float_profile
from ebc_cli.session import (
    SessionStopReason,
    SupervisedSession,
    read_live_status,
    run_active_session,
    run_bulk_to_float_session,
    run_monitor_session,
)


class SessionDeadlineTests(unittest.TestCase):
    def test_fallback_uses_only_remaining_read_budget(self):
        clock = FakeClock()
        tester = Mock()

        def no_stream(**kwargs):
            self.assertEqual(kwargs, {"timeout": 5, "latest": True})
            clock.now = 3
            raise DeviceError("no stream")

        tester.next_status.side_effect = no_stream
        tester.status.return_value = _status()
        read_live_status(tester, deadline=5, clock=clock)
        tester.status.assert_called_once_with(timeout=2, latest=True)

    def test_read_timeout_at_deadline_stops_without_fallback(self):
        clock = FakeClock()
        tester = Mock()

        def no_stream(**kwargs):
            clock.now += kwargs["timeout"]
            raise DeviceError("read timed out")

        tester.next_status.side_effect = no_stream
        with SupervisedSession(tester, None) as supervised:
            supervised.mark_started("started")
            result = run_active_session(
                session=supervised,
                printer=lambda _: None,
                timeout_minutes=1,
                sample_event="test-sample",
                clock=clock,
                sleeper=clock.sleep,
            )
        self.assertEqual(result.stop_reason, SessionStopReason.TIMEOUT)
        self.assertEqual(result.elapsed_seconds, 60)
        tester.status.assert_not_called()
        tester.stop.assert_called_once()

    def test_late_capacity_cutoff_is_a_timeout(self):
        clock = FakeClock()
        tester = Mock()

        def late_cutoff(**kwargs):
            clock.now = 61
            return _status(state=0x14)

        tester.next_status.side_effect = late_cutoff
        with SupervisedSession(tester, None) as supervised:
            supervised.mark_started("started")
            result = run_active_session(
                session=supervised,
                printer=lambda _: None,
                timeout_minutes=1,
                sample_event="test-sample",
                stop_on_cutoff=True,
                clock=clock,
                sleeper=clock.sleep,
            )
        self.assertEqual(result.stop_reason, SessionStopReason.TIMEOUT)
        self.assertEqual(result.samples, 0)

    def test_late_auto_off_never_starts_float(self):
        clock = FakeClock()

        class LateTester(FakeTester):
            def next_status(self, **kwargs):
                status = super().next_status(**kwargs)
                if status.completion_reason is not None:
                    clock.now = 61
                return status

        tester = LateTester([_status(), _status(state=0x1A)])
        result = self.run_bulk(tester, clock)
        self.assertEqual(result.stop_reason, SessionStopReason.BULK_TIMEOUT)
        self.assertEqual(tester.float_calls, [])
        self.assertEqual(tester.stop_calls, 1)

    def test_auto_off_without_observed_bulk_activity_never_starts_float(self):
        clock = FakeClock()
        tester = FakeTester([_status(state=0x1A)])
        result = self.run_bulk(tester, clock, poll_interval=60)
        self.assertEqual(result.stop_reason, SessionStopReason.BULK_TIMEOUT)
        self.assertEqual(tester.float_calls, [])

    def test_deadline_expiring_during_output_prevents_float(self):
        clock = FakeClock()
        tester = FakeTester([_status(), _status(state=0x1A)])

        def slow_output(status):
            if status.completion_reason is not None:
                clock.now = 61

        result = self.run_bulk(tester, clock, printer=slow_output)
        self.assertEqual(result.stop_reason, SessionStopReason.BULK_TIMEOUT)
        self.assertEqual(tester.float_calls, [])

    def test_monitor_uses_latest_stream_after_initial_connect(self):
        tester = Mock()
        tester.status.return_value = _status()
        tester.next_status.return_value = _status()
        result = run_monitor_session(
            tester=tester,
            logger=None,
            printer=lambda _: None,
            interval=1,
            count=3,
            sleeper=lambda _: None,
        )
        self.assertEqual(result.samples, 3)
        tester.status.assert_called_once_with(latest=True)
        self.assertEqual(tester.next_status.call_count, 2)
        tester.next_status.assert_called_with(timeout=None, latest=True)

    def test_signal_handlers_restored_when_stop_logging_fails(self):
        previous = signal.getsignal(signal.SIGTERM)
        tester = Mock()
        logger = Mock()
        with self.assertRaises(OSError), SupervisedSession(tester, logger) as supervised:
            supervised.mark_started("started")
            logger.write_event.side_effect = OSError("disk full")
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)
        tester.stop.assert_called_once()

    def run_bulk(self, tester, clock, **kwargs):
        profile = make_bulk_to_float_profile(
            bulk_current_amps=0.8,
            bulk_timeout_minutes=1,
            float_current_amps=0.5,
            float_voltage_volts=13.7,
            float_timeout_minutes=1,
        )
        kwargs.setdefault("printer", lambda _: None)
        with SupervisedSession(tester, None) as supervised:
            supervised.mark_started(profile.start_event)
            return run_bulk_to_float_session(
                session=supervised,
                profile=profile,
                clock=clock,
                sleeper=clock.sleep,
                **kwargs,
            )
