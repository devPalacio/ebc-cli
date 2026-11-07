# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Jay (Python CLI contributions)
# See NOTICE for upstream attribution and port details.

"""Capacity quality, exceptional exits, and receipt-time completion regressions."""

import contextlib
import signal
import unittest
from functools import partial
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from support import FakeClock
from test_capacity import StreamTester, cutoff, sample

from ebc_cli import (
    CapacityAssessment,
    CapacityRequest,
    DeviceError,
    StatusCsvLogger,
    commands,
    make_capacity_test_profile,
    resources,
    run_profile,
    runner,
)
from ebc_cli.capacity import ASSESSMENT_EVENT_PREFIX, CapacityPolicy, format_capacity_assessment
from ebc_cli.cli import main
from ebc_cli.report import load_replay_log, summarize_replay_log
from ebc_cli.session import SessionStopReason, SupervisedSession, _SessionSignal, run_active_session


class CapacityBlockerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "capacity.csv"
        self.clock = FakeClock()
        self.profile = make_capacity_test_profile(
            current_amps=1, cutoff_volts=10.5, timeout_minutes=1
        )

    def live(self, observations, **kwargs):
        tester = StreamTester(self.clock, observations)
        with StatusCsvLogger(self.path) as logger:
            result = run_profile(
                tester,
                self.profile,
                logger=logger,
                clock=self.clock,
                sleeper=self.clock.sleep,
                **kwargs,
            ).result
        self.assertEqual(tester.stop_calls, 1)
        self.assertEqual(result.capacity, summarize_replay_log(load_replay_log(self.path)).capacity)
        return result

    def cli(self, tester, *, logger=None, log_path=None, no_log=True):
        with (
            patch.object(
                commands,
                "_open_resources",
                return_value=contextlib.nullcontext(
                    resources.CommandResources(tester, logger, log_path)
                ),
            ),
            patch.object(
                commands,
                "run_profile",
                side_effect=partial(runner.run_profile, clock=self.clock, sleeper=self.clock.sleep),
            ),
            patch.object(commands, "load_replay_log") as replay,
            contextlib.redirect_stdout(StringIO()) as output,
            contextlib.redirect_stderr(StringIO()) as errors,
        ):
            argv = [
                "capacity-test",
                "--current",
                "1",
                "--cutoff-voltage",
                "10.5",
                "--timeout-minutes",
                "1",
                "--execute",
                "--i-am-present",
            ]
            if no_log:
                argv.append("--no-log")
            code = main(argv)
        replay.assert_not_called()
        return code, output.getvalue(), errors.getvalue()

    def test_terminal_current_is_upper_bound_only_in_live_and_replay(self):
        for requested, observed, expected in (
            (0.1, 0, "completed"),
            (0.1, 0.01, "completed"),
            (0.1, 0.12, "completed"),
            (0.1, 0.13, "invalid"),
            (1, 0, "completed"),
            (1, 0.5, "completed"),
            (1, 1.05, "completed"),
            (1, 1.06, "invalid"),
            (1, 8, "invalid"),
        ):
            with self.subTest(requested=requested, observed=observed):
                self.path = self.path.with_name(f"terminal-{requested}-{observed}.csv")
                self.profile = make_capacity_test_profile(
                    current_amps=requested, cutoff_volts=10.5, timeout_minutes=1
                )
                result = self.live(
                    [
                        (0, sample(current=requested)),
                        (10, sample(state=0x14, current=observed, voltage=10.5)),
                    ]
                )
                self.assertEqual(result.stop_reason, "device-cutoff")
                self.assertEqual(result.capacity.outcome, expected)
                self.assertEqual(
                    result.capacity.reasons,
                    ("load-current-mismatch",) if expected == "invalid" else (),
                )

    def test_cutoff_rebound_allowance_is_inclusive_in_live_and_replay(self):
        for requested, observed, expected in (
            (1, 1.1, "completed"),
            (1, 1.101, "invalid"),
            (10.5, 10.5, "completed"),
            (10.5, 11.025, "completed"),
            (10.5, 11.027, "invalid"),
            (10.5, 14, "invalid"),
        ):
            with self.subTest(requested=requested, observed=observed):
                self.path = self.path.with_name(f"voltage-{requested}-{observed}.csv")
                self.profile = make_capacity_test_profile(
                    current_amps=1, cutoff_volts=requested, timeout_minutes=1
                )
                result = self.live(
                    [
                        (0, sample()),
                        (10, sample(state=0x14, current=0, voltage=observed)),
                    ]
                )
                self.assertEqual(result.stop_reason, "device-cutoff")
                self.assertEqual(result.capacity.outcome, expected)
                self.assertEqual(
                    result.capacity.reasons,
                    ("cutoff-voltage-mismatch",) if expected == "invalid" else (),
                )

    def test_inconsistent_first_cutoff_is_invalid_without_inventing_discharge(self):
        for current, voltage, model, reason in (
            (8, 10.5, 6, "load-current-mismatch"),
            (0, 14, 6, "cutoff-voltage-mismatch"),
            (0, 10.5, 7, "unrecognized-discharge-state"),
        ):
            with self.subTest(reason=reason):
                self.path = self.path.with_name(reason + ".csv")
                result = self.live(
                    [(0, sample(state=0x14, current=current, voltage=voltage, model=model))]
                )
                self.assertEqual(result.stop_reason, "device-cutoff")
                self.assertEqual(result.capacity.outcome, "invalid")
                self.assertFalse(result.capacity.discharge_observed)
                self.assertIn(reason, result.capacity.reasons)
                self.assertIn("no-observed-discharge", result.capacity.reasons)
                self.assertEqual(result.measurement.amp_hours, 0)

    def test_wrong_mode_or_model_is_invalid_before_or_after_discharge_even_at_zero(self):
        for state, model in ((0x10, 6), (0x1A, 6), (0x00, 6), (0x0A, 7), (0x6E, 7)):
            for current in (0, 1):
                for prior in (True, False):
                    with self.subTest(state=state, model=model, current=current, prior=prior):
                        self.path = self.path.with_name(
                            f"wrong-{state}-{model}-{current}-{prior}.csv"
                        )
                        observations = [sample(state=state, current=current, model=model), sample()]
                        if not prior:
                            observations.reverse()
                        result = self.live(
                            [
                                (0, observations[0]),
                                (10, observations[1]),
                                (20, cutoff()),
                            ]
                        )
                        self.assertEqual(result.capacity.outcome, "invalid")
                        self.assertEqual(result.capacity.reasons, ("unrecognized-discharge-state",))

    def test_wrong_model_cutoff_is_invalid_and_still_stops(self):
        for current in (0, 1):
            with self.subTest(current=current):
                self.path = self.path.with_name(f"wrong-cutoff-{current}.csv")
                result = self.live(
                    [
                        (0, sample()),
                        (10, sample(state=0x14, current=current, voltage=10.5, model=7)),
                    ]
                )
                self.assertEqual(result.stop_reason, "device-cutoff")
                self.assertEqual(result.capacity.outcome, "invalid")
                self.assertEqual(result.capacity.reasons, ("unrecognized-discharge-state",))

    def test_no_log_library_exceptions_preserve_object_traceback_and_typed_assessment(self):
        for error, reason in (
            (DeviceError("read failed"), "error"),
            (KeyboardInterrupt(), "interrupted"),
            (_SessionSignal(signal.SIGTERM), "signal"),
            (OSError("read failed"), "error"),
        ):
            with self.subTest(error=type(error).__name__):
                tester = StreamTester(self.clock, [(0, sample()), (10, sample()), (11, error)])
                try:
                    run_profile(tester, self.profile, clock=self.clock, sleeper=self.clock.sleep)
                except type(error) as caught:
                    self.assertIs(caught, error)
                    assessment = caught.capacity_assessment
                    self.assertIsInstance(assessment, CapacityAssessment)
                    self.assertEqual(assessment.outcome, "incomplete")
                    self.assertEqual(assessment.reasons, (reason,))
                    self.assertAlmostEqual(assessment.measurement.amp_hours, 0.002777777777777778)
                    traceback = caught.__traceback__
                    while traceback.tb_next is not None:
                        traceback = traceback.tb_next
                    self.assertEqual(traceback.tb_frame.f_code.co_name, "next_status")
                else:
                    self.fail("original exception was not raised")
                self.assertEqual(tester.stop_calls, 1)

    def test_start_and_baseline_failures_also_attach_partial_assessment(self):
        for stage in ("start", "start-event", "request-event", "baseline-log", "baseline-output"):
            with self.subTest(stage=stage):
                failure = DeviceError(stage + " failed")
                tester = StreamTester(self.clock, [])
                logger = Mock()
                on_baseline = None
                if stage == "start":
                    tester.begin_mode = Mock(side_effect=failure)
                elif stage == "start-event":
                    logger.write_event.side_effect = failure
                elif stage == "request-event":
                    logger.write_event.side_effect = [None, failure, None, None]
                elif stage == "baseline-log":
                    logger.write.side_effect = failure
                else:
                    logger = None
                    on_baseline = Mock(side_effect=failure)
                with self.assertRaises(DeviceError) as raised:
                    run_profile(
                        tester,
                        self.profile,
                        logger=logger,
                        on_baseline=on_baseline,
                        clock=self.clock,
                        sleeper=self.clock.sleep,
                    )
                self.assertIs(raised.exception, failure)
                self.assertIsInstance(failure.capacity_assessment, CapacityAssessment)
                self.assertEqual(failure.capacity_assessment.outcome, "incomplete")
                self.assertIn("no-observed-discharge", failure.capacity_assessment.reasons)
                self.assertIn("error", failure.capacity_assessment.reasons)
                self.assertEqual(tester.stop_calls, 0 if stage == "start" else 1)

    def test_direct_capacity_session_exception_without_logger_has_assessment(self):
        failure = DeviceError("stream failed")
        tester = StreamTester(self.clock, [(0, sample()), (10, failure)])
        with self.assertRaises(DeviceError) as raised, SupervisedSession(tester, None) as session:
            session.mark_started("capacity-test-started")
            run_active_session(
                session=session,
                printer=lambda _: None,
                timeout_minutes=1,
                sample_event="capacity-sample",
                capacity_policy=CapacityPolicy(CapacityRequest(1, 10.5, 1)),
                clock=self.clock,
                sleeper=self.clock.sleep,
            )
        self.assertIs(raised.exception, failure)
        self.assertEqual(failure.capacity_assessment.reasons, ("error",))
        self.assertEqual(tester.stop_calls, 1)

    def test_logged_failure_attaches_the_same_assessment_as_durable_replay(self):
        failure = KeyboardInterrupt()
        tester = StreamTester(self.clock, [(0, sample()), (10, sample()), (11, failure)])
        with StatusCsvLogger(self.path) as logger, self.assertRaises(KeyboardInterrupt) as raised:
            run_profile(
                tester, self.profile, logger=logger, clock=self.clock, sleeper=self.clock.sleep
            )
        self.assertIs(raised.exception, failure)
        self.assertIsInstance(failure.capacity_assessment, CapacityAssessment)
        self.assertEqual(
            failure.capacity_assessment, summarize_replay_log(load_replay_log(self.path)).capacity
        )

    def test_no_log_cli_errors_and_interruptions_print_partial_without_replay(self):
        for error, expected in (
            (DeviceError("read failed"), 2),
            (KeyboardInterrupt(), 130),
            (_SessionSignal(signal.SIGTERM), 143),
        ):
            with self.subTest(expected=expected):
                tester = StreamTester(self.clock, [(0, sample()), (10, sample()), (11, error)])
                code, output, _ = self.cli(tester)
                self.assertEqual(code, expected)
                self.assertEqual(tester.stop_calls, 1)
                self.assertIn(format_capacity_assessment(error.capacity_assessment), output)
                self.assertIn("Partial: 0.003 Ah, 0.033 Wh", output)

    def test_logged_cli_prefers_in_memory_assessment_over_replay(self):
        failure = DeviceError("read failed")
        tester = StreamTester(self.clock, [(0, sample()), (10, sample()), (11, failure)])
        with StatusCsvLogger(self.path) as logger:
            code, output, _ = self.cli(tester, logger=logger, log_path=self.path, no_log=False)
        self.assertEqual(code, 2)
        self.assertIn(format_capacity_assessment(failure.capacity_assessment), output)

    def test_cli_uses_replay_fallback_only_when_exception_has_no_assessment(self):
        tester = StreamTester(self.clock, [(0, sample()), (10, DeviceError("read failed"))])
        with StatusCsvLogger(self.path) as logger, self.assertRaises(DeviceError):
            run_profile(
                tester, self.profile, logger=logger, clock=self.clock, sleeper=self.clock.sleep
            )
        failure = DeviceError("legacy runner failed")
        with (
            patch.object(
                commands,
                "_open_resources",
                return_value=contextlib.nullcontext(
                    resources.CommandResources(tester, None, self.path)
                ),
            ),
            patch.object(commands, "run_profile", side_effect=failure),
            patch.object(commands, "load_replay_log", wraps=load_replay_log) as replay,
            contextlib.redirect_stdout(StringIO()) as output,
            self.assertRaises(DeviceError) as raised,
        ):
            commands._run_profile(Mock(), self.profile)
        self.assertIs(raised.exception, failure)
        replay.assert_called_once_with(self.path)
        self.assertIn("Capacity test: incomplete", output.getvalue())
        self.assertIn("Partial:", output.getvalue())

    def test_cutoff_receipt_precedes_deadline_even_if_output_passes_it(self):
        for cutoff_at, expected in ((59, "device-cutoff"), (60, "timeout")):
            with self.subTest(cutoff_at=cutoff_at):
                self.path = self.path.with_name(f"slow-output-{cutoff_at}.csv")

                def output(status):
                    if status.completion_reason is not None:
                        self.clock.sleep(2)

                result = self.live(
                    [(0, sample()), (20, sample()), (40, sample()), (cutoff_at, cutoff())],
                    on_sample=output,
                )
                self.assertEqual(result.stop_reason, expected)
                self.assertEqual(
                    result.capacity.outcome, "completed" if cutoff_at == 59 else "incomplete"
                )
                self.assertEqual(result.capacity.unobserved_seconds, 0 if cutoff_at == 59 else 20)

    def test_cutoff_receipt_precedes_deadline_even_if_logging_passes_it(self):
        tester = StreamTester(
            self.clock, [(0, sample()), (20, sample()), (40, sample()), (59, cutoff())]
        )
        with StatusCsvLogger(self.path) as logger:
            original = logger.write

            def slow_log(status, **kwargs):
                original(status, **kwargs)
                if status.completion_reason is not None:
                    self.clock.sleep(2)

            with patch.object(logger, "write", side_effect=slow_log):
                result = run_profile(
                    tester, self.profile, logger=logger, clock=self.clock, sleeper=self.clock.sleep
                ).result
        self.assertEqual(result.stop_reason, "device-cutoff")
        self.assertEqual(result.capacity.outcome, "completed")
        self.assertEqual(result.capacity, summarize_replay_log(load_replay_log(self.path)).capacity)
        self.assertEqual(tester.stop_calls, 1)

    def test_non_capacity_completion_still_loses_to_timeout_after_slow_output(self):
        for completion in (None, lambda *_: SessionStopReason.MODE_AUTO_OFF):
            with self.subTest(completion=completion):
                self.clock.now = 0
                tester = StreamTester(self.clock, [(59, cutoff())])
                with SupervisedSession(tester, None) as session:
                    session.mark_started("started")
                    result = run_active_session(
                        session=session,
                        printer=lambda _: self.clock.sleep(2),
                        timeout_minutes=1,
                        sample_event="test-sample",
                        stop_on_cutoff=True,
                        completion=completion,
                        clock=self.clock,
                        sleeper=self.clock.sleep,
                    )
                self.assertEqual(result.stop_reason, "timeout")
                self.assertEqual(tester.stop_calls, 1)

    @staticmethod
    def fail_stop_log(event):
        if "stop-sent" in event or event.startswith(ASSESSMENT_EVENT_PREFIX):
            raise OSError("disk full")

    def test_stop_failure_preserves_transport_error_when_failure_logging_also_fails(self):
        failure = DeviceError("STOP transport failed")
        tester = StreamTester(self.clock, [(0, sample()), (10, cutoff())])
        tester.stop = Mock(side_effect=failure)
        logger = Mock()
        logger.write_event.side_effect = self.fail_stop_log
        with self.assertRaises(DeviceError) as raised:
            run_profile(
                tester, self.profile, logger=logger, clock=self.clock, sleeper=self.clock.sleep
            )
        self.assertIs(raised.exception, failure)
        self.assertIsInstance(failure.capacity_assessment, CapacityAssessment)
        self.assertEqual(failure.capacity_assessment.outcome, "incomplete")
        self.assertTrue(any("disk full" in note for note in failure.__notes__))
        tester.stop.assert_called_once()

    def test_best_effort_stop_does_not_raise_failure_logging_error(self):
        failure = DeviceError("STOP transport failed")
        tester = Mock()
        tester.stop.side_effect = failure
        logger = Mock()
        session = SupervisedSession(tester, logger)
        session.mark_started("capacity-test-started")
        logger.write_event.side_effect = OSError("disk full")
        self.assertFalse(session.stop("error-stop-sent"))
        self.assertTrue(any("disk full" in note for note in failure.__notes__))
        tester.stop.assert_called_once()

    def test_cli_stop_transport_and_failure_logging_errors_still_exit_two(self):
        failure = DeviceError("STOP transport failed")
        tester = StreamTester(self.clock, [(0, sample()), (10, cutoff())])
        tester.stop = Mock(side_effect=failure)
        logger = Mock()
        logger.write_event.side_effect = self.fail_stop_log
        code, output, errors = self.cli(tester, logger=logger, no_log=False)
        self.assertEqual(code, 2)
        self.assertIn("ebc: STOP transport failed", errors)
        self.assertIn("Capacity test: incomplete", output)
        self.assertIn("Partial:", output)
        tester.stop.assert_called_once()
