# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Jay (Python CLI contributions)
# See NOTICE for upstream attribution and port details.

"""Capacity outcomes must follow observed discharge, not stale device state."""

import contextlib
import csv
import json
import signal
import unittest
from dataclasses import replace
from functools import partial
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from support import FakeClock, status

from ebc_cli import (
    CapacityOutcome,
    CapacityRequest,
    DeviceError,
    SessionStopReason,
    StatusCsvLogger,
    commands,
    make_capacity_test_profile,
    resources,
    run_profile,
    runner,
)
from ebc_cli.capacity import (
    ASSESSMENT_EVENT_PREFIX,
    REQUEST_EVENT_PREFIX,
    CapacityPolicy,
    format_capacity_assessment,
)
from ebc_cli.cli import main
from ebc_cli.csvlog import CSV_HEADER
from ebc_cli.protocol import checksum, encode_scaled, parse_status
from ebc_cli.report import format_report, load_replay_log, summarize_replay_log
from ebc_cli.session import _SessionSignal

FIXTURES = Path(__file__).with_name("fixtures")


def sample(*, state=0x0A, current=1.0, voltage=12.0, model=0x06):
    raw = bytearray(status(state=state).raw)
    raw[2:4] = encode_scaled(current)
    raw[4:6] = encode_scaled(voltage * 10)
    raw[16] = model
    raw[17] = checksum(bytes(raw), 17)
    return parse_status(bytes(raw))


def cutoff():
    return sample(state=0x14, current=0, voltage=10.5)


class StreamTester:
    def __init__(self, clock, observations, *, baseline=None):
        self.clock = clock
        self.observations = list(observations)
        self.baseline = baseline if baseline is not None else sample()
        self.stop_calls = 0
        self.status = Mock(return_value=sample())
        self.commands = []

    def begin_mode(self, command):
        self.clock.now = 0
        self.commands.append(command)
        return self.baseline

    def adjust_mode(self, command):
        raise AssertionError("capacity runs must not adjust the load")

    def next_status(self, *, timeout=None, latest=False):
        if not self.observations:
            self.clock.now += timeout
            raise DeviceError("read timed out")
        timestamp, observation = self.observations.pop(0)
        self.clock.now = timestamp
        if isinstance(observation, BaseException):
            raise observation
        return observation

    def stop(self):
        self.stop_calls += 1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


class CapacityTests(unittest.TestCase):
    def test_duplicate_observation_times_cannot_complete_a_capacity_measurement(self):
        policy = CapacityPolicy(CapacityRequest(1, 10.5, 1))
        policy.add(sample(), 0)
        policy.add(cutoff(), 0)
        assessment = policy.assess("device-cutoff", 0)
        self.assertIs(assessment.outcome, CapacityOutcome.INVALID)
        self.assertIn("non-increasing-observation-time", assessment.reasons)
        self.assertEqual(assessment.measurement.amp_hours, 0)

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
        tester.status.assert_not_called()
        replay = self.replay()
        self.assertEqual(result.capacity, replay.capacity)
        self.assertEqual(result.measurement, replay.measurement)
        return result

    def replay(self):
        return summarize_replay_log(load_replay_log(self.path))

    def test_stale_first_cutoff_stops_but_is_incomplete_even_with_positive_baseline(self):
        result = self.live([(0, cutoff())])
        self.assertEqual(result.stop_reason, SessionStopReason.DEVICE_CUTOFF)
        assessment = result.capacity
        self.assertEqual(assessment.outcome, "incomplete")
        self.assertEqual(assessment.reasons, ("no-observed-discharge",))
        self.assertFalse(assessment.discharge_observed)
        self.assertEqual(assessment.measurement.amp_hours, 0)
        self.assertEqual(assessment.measurement.watt_hours, 0)

    def test_nominal_discharge_then_zero_current_cutoff_completes(self):
        result = self.live([(0, sample()), (10, sample(state=0x6E)), (20, cutoff())])
        assert result.capacity is not None
        self.assertEqual(result.stop_reason, SessionStopReason.DEVICE_CUTOFF)
        self.assertIsNotNone(result.final_status)
        assert result.final_status is not None
        self.assertEqual(result.final_status.state_byte, 0x14)
        self.assertEqual(result.capacity.outcome, "completed")
        self.assertEqual(result.capacity.termination, "device-cutoff")
        self.assertEqual(result.capacity.reasons, ())
        self.assertTrue(result.capacity.discharge_observed)
        self.assertAlmostEqual(result.measurement.amp_hours, 0.004166666666666667)
        self.assertAlmostEqual(result.measurement.watt_hours, 0.05)
        self.assertEqual(result.measurement.integrated_seconds, 20)

    def test_a10h_zero_current_rebound_infers_cutoff_on_confirmation_sample(self):
        self.profile = make_capacity_test_profile(
            current_amps=1.6, cutoff_volts=12.5, timeout_minutes=1
        )
        loaded_cc = sample(state=0x0A, current=1.6, voltage=12.510)
        loaded_below_cutoff = sample(state=0x00, current=1.6, voltage=12.499)
        idle_confirmation = sample(state=0x00, current=0, voltage=12.662)
        observed = []

        result = self.live(
            [
                (0, loaded_cc),
                (10, loaded_below_cutoff),
                (20, idle_confirmation),
            ],
            on_sample=lambda status: observed.append(
                (status.state_byte, status.current_amps, status.voltage_volts)
            ),
        )

        self.assertEqual(len(observed), 3)
        self.assertEqual(observed[0][:2], (0x0A, 1.6))
        self.assertAlmostEqual(observed[0][2], 12.510)
        self.assertEqual(observed[1][:2], (0x00, 1.6))
        self.assertAlmostEqual(observed[1][2], 12.499)
        self.assertEqual(observed[2][:2], (0x00, 0))
        self.assertAlmostEqual(observed[2][2], 12.662)
        # The 12.499 V loaded sample is only a candidate. STOP follows idle confirmation.
        replay = self.replay()
        assert result.capacity is not None
        self.assertEqual(result.stop_reason.value, "inferred-cutoff")
        self.assertEqual(result.samples, 3)
        self.assertEqual(result.final_status, idle_confirmation)
        self.assertEqual(result.capacity.outcome, "completed")
        self.assertEqual(result.capacity.termination, "inferred-cutoff")
        self.assertEqual(result.capacity, replay.capacity)

    def test_startup_and_isolated_idle_state_zero_do_not_infer_cutoff(self):
        self.profile = make_capacity_test_profile(
            current_amps=1.6, cutoff_volts=12.5, timeout_minutes=1
        )
        result = self.live(
            [
                (0, sample(state=0x00, current=0, voltage=12.499)),
                (1, sample(state=0x0A, current=1.6, voltage=12.510)),
                (2, sample(state=0x00, current=0, voltage=12.662)),
            ]
        )

        assert result.capacity is not None
        self.assertEqual(result.stop_reason, SessionStopReason.TIMEOUT)
        self.assertNotEqual(result.capacity.outcome, "completed")
        self.assertNotEqual(result.capacity.termination, "inferred-cutoff")

    def test_startup_zero_current_then_normal_load_completes(self):
        self.profile = make_capacity_test_profile(
            current_amps=1.6, cutoff_volts=10.5, timeout_minutes=1
        )
        result = self.live(
            [
                (0, sample(current=0)),
                (1, sample(current=1.6)),
                (10, sample(current=1.6)),
                (20, cutoff()),
            ]
        )
        assert result.capacity is not None
        self.assertEqual(result.capacity.outcome, "completed")
        self.assertNotIn("load-current-mismatch", result.capacity.reasons)
        self.assertTrue(result.capacity.discharge_observed)

    def test_idle_capacity_run_fails_fast_after_no_discharge_watchdog(self):
        tester = StreamTester(
            self.clock,
            [
                (0, sample(state=0x00, current=0)),
                (10, sample(state=0x64, current=0)),
                (20, sample(state=0x00, current=0)),
            ],
        )
        with self.assertRaisesRegex(
            DeviceError, "capacity test has not observed positive current within 20 seconds"
        ) as caught:
            run_profile(
                tester,
                self.profile,
                clock=self.clock,
                sleeper=self.clock.sleep,
            )
        self.assertEqual(tester.stop_calls, 1)
        assessment = getattr(caught.exception, "capacity_assessment", None)
        self.assertIsNotNone(assessment)
        assert assessment is not None
        self.assertEqual(assessment.outcome, CapacityOutcome.INCOMPLETE)
        self.assertFalse(assessment.discharge_observed)
        self.assertIn("no-observed-discharge", assessment.reasons)
        self.assertEqual(assessment.elapsed_seconds, 20)

    def test_first_positive_capacity_sample_after_watchdog_fails(self):
        tester = StreamTester(self.clock, [(21, sample(current=1.0))])
        with self.assertRaisesRegex(DeviceError, "within 20 seconds of start") as caught:
            run_profile(
                tester,
                self.profile,
                clock=self.clock,
                sleeper=self.clock.sleep,
            )
        self.assertEqual(tester.stop_calls, 1)
        assessment = getattr(caught.exception, "capacity_assessment", None)
        assert assessment is not None
        self.assertTrue(assessment.discharge_observed)
        self.assertEqual(assessment.outcome, CapacityOutcome.INCOMPLETE)
        self.assertIn("error", assessment.reasons)

    def test_capacity_discharge_observed_before_watchdog_continues_normally(self):
        tester = StreamTester(
            self.clock,
            [
                (0, sample(state=0x0A, current=0)),
                (19, sample(current=1.0)),
                (25, sample(state=0x6E, current=1.0)),
                (30, cutoff()),
            ],
        )
        result = run_profile(
            tester,
            self.profile,
            clock=self.clock,
            sleeper=self.clock.sleep,
        ).result
        self.assertEqual(tester.stop_calls, 1)
        self.assertEqual(result.stop_reason, SessionStopReason.DEVICE_CUTOFF)
        assert result.capacity is not None
        self.assertEqual(result.capacity.outcome, CapacityOutcome.COMPLETED)
        self.assertTrue(result.capacity.discharge_observed)

    def test_active_states_are_real_captured_cc_states_not_opcodes(self):
        log = load_replay_log(FIXTURES / "a10h_live_cycles.csv")
        states = {
            s.status.state_byte
            for s in log.samples
            if s.event == "discharge-status" and s.status.current_amps > 0
        }
        self.assertEqual(states, {0x0A, 0x6E})
        for state in (0x01, 0x02, 0x10, 0x68, 0x74):
            with self.subTest(state=state):
                policy = CapacityPolicy(CapacityRequest(1, 10.5, 1))
                policy.add(sample(state=state), 0)
                policy.add(cutoff(), 1)
                assessment = policy.assess("device-cutoff", 1)
                self.assertEqual(assessment.outcome, "incomplete")
                self.assertIn("no-observed-discharge", assessment.reasons)

    def test_zero_only_timeout_is_incomplete_not_invalid(self):
        result = self.live(
            [
                (0, sample(current=0)),
                (10, sample(current=0)),
                (15, sample(current=0)),
                (19, sample(current=0)),
            ]
        )
        self.assertEqual(result.stop_reason, "timeout")
        self.assertEqual(result.capacity.outcome, "incomplete")
        self.assertIn("no-observed-discharge", result.capacity.reasons)
        self.assertIn("timeout", result.capacity.reasons)
        self.assertEqual(result.measurement.amp_hours, 0)

    def test_each_run_requires_fresh_poststart_activity(self):
        first = self.live([(0, sample()), (1, cutoff())])
        self.assertEqual(first.capacity.outcome, "completed")
        self.path = self.path.with_name("second.csv")
        second = self.live([(2, cutoff())])
        self.assertEqual(second.capacity.outcome, "incomplete")
        self.assertFalse(second.capacity.discharge_observed)

    def test_failed_stream_never_uses_connect_or_baseline_as_activity(self):
        tester = StreamTester(self.clock, [(0, DeviceError("stream failed"))])
        with (
            StatusCsvLogger(self.path) as logger,
            self.assertRaisesRegex(DeviceError, "stream failed"),
        ):
            run_profile(
                tester, self.profile, logger=logger, clock=self.clock, sleeper=self.clock.sleep
            )
        tester.status.assert_not_called()
        self.assertEqual(tester.stop_calls, 1)
        replay = self.replay()
        self.assertEqual(replay.capacity.outcome, "incomplete")
        self.assertEqual(replay.capacity.measurement.sample_count, 0)
        self.assertEqual(replay.capacity.measurement.amp_hours, 0)
        self.assertIn("no-observed-discharge", replay.capacity.reasons)
        self.assertIn("error", replay.capacity.reasons)

    def test_excessive_gap_invalidates_and_preserves_only_integrated_partial(self):
        result = self.live([(0, sample()), (10, sample()), (45, sample()), (55, cutoff())])
        self.assertEqual(result.capacity.outcome, "invalid")
        self.assertEqual(result.capacity.reasons, ("observation-gap",))
        self.assertEqual(result.measurement.skipped_intervals, 1)
        self.assertEqual(result.measurement.skipped_seconds, 35)
        self.assertEqual(result.measurement.integrated_seconds, 20)
        self.assertAlmostEqual(result.measurement.amp_hours, 0.004166666666666667)
        self.assertAlmostEqual(result.measurement.watt_hours, 0.05)
        self.assertEqual(result.capacity.unobserved_seconds, 35)

    def test_first_observation_delay_is_not_filled_from_positive_baseline(self):
        tester = StreamTester(self.clock, [(35, sample()), (45, cutoff())])
        with (
            StatusCsvLogger(self.path) as logger,
            self.assertRaisesRegex(DeviceError, "within 20 seconds of start") as caught,
        ):
            run_profile(
                tester,
                self.profile,
                logger=logger,
                clock=self.clock,
                sleeper=self.clock.sleep,
            )
        self.assertEqual(tester.stop_calls, 1)
        assessment = getattr(caught.exception, "capacity_assessment", None)
        self.assertIsNotNone(assessment)
        assert assessment is not None
        self.assertEqual(assessment.outcome, CapacityOutcome.INVALID)
        self.assertIn("first-observation-delay", assessment.reasons)
        self.assertEqual(assessment.measurement.sample_count, 1)
        self.assertEqual(assessment.measurement.integrated_seconds, 0)
        self.assertEqual(assessment.unobserved_seconds, 35)
        self.assertEqual(self.replay().capacity, assessment)

    def test_normal_first_delay_is_disclosed_but_not_integrated(self):
        result = self.live([(2, sample()), (12, cutoff())])
        self.assertEqual(result.capacity.outcome, "completed")
        self.assertEqual(result.capacity.unobserved_seconds, 2)
        self.assertEqual(result.measurement.integrated_seconds, 10)

    def test_gap_boundary_is_inclusive_and_replay_does_not_round_away_failure(self):
        for gap, expected in ((30, "completed"), (30.00001, "invalid")):
            with self.subTest(gap=gap):
                self.path = self.path.with_name(f"gap-{gap}.csv")
                result = self.live([(0, sample()), (gap, cutoff())])
                self.assertEqual(result.capacity.outcome, expected)

    def test_load_tolerance_is_inclusive_for_absolute_and_relative_limits(self):
        for requested, observed, expected in (
            (0.1, 0.08, "completed"),
            (0.1, 0.12, "completed"),
            (0.1, 0.07, "invalid"),
            (0.1, 0.13, "invalid"),
            (1, 0.95, "completed"),
            (1, 1.05, "completed"),
            (1, 0.94, "invalid"),
            (1, 1.06, "invalid"),
        ):
            with self.subTest(requested=requested, observed=observed):
                self.path = self.path.with_name(f"load-{requested}-{observed}.csv")
                self.profile = make_capacity_test_profile(
                    current_amps=requested, cutoff_volts=10.5, timeout_minutes=1
                )
                result = self.live(
                    [
                        (0, sample(current=observed)),
                        (1, sample(current=observed)),
                        (2, sample(current=observed)),
                        (10, cutoff()),
                    ]
                )
                self.assertEqual(result.capacity.outcome, expected)
                if expected == "invalid":
                    self.assertIn("load-current-mismatch", result.capacity.reasons)

    def test_isolated_active_mismatch_amid_good_samples_is_tolerated(self):
        result = self.live([(0, sample()), (10, sample(current=0)), (20, sample()), (30, cutoff())])
        self.assertEqual(result.capacity.outcome, "completed")
        self.assertNotIn("load-current-mismatch", result.capacity.reasons)

    def test_good_active_sample_resets_pending_mismatch_streak(self):
        mismatch = sample(current=0.8)
        result = self.live(
            [
                (0, mismatch),
                (1, mismatch),
                (2, sample()),
                (3, mismatch),
                (4, mismatch),
                (10, cutoff()),
            ]
        )
        self.assertEqual(result.capacity.outcome, "completed")
        self.assertNotIn("load-current-mismatch", result.capacity.reasons)

    def test_three_consecutive_active_mismatches_invalidate_live_and_replay(self):
        mismatch = sample(current=0.8)
        result = self.live(
            [
                (0, sample()),
                (1, mismatch),
                (2, mismatch),
                (3, mismatch),
                (10, cutoff()),
            ]
        )
        self.assertEqual(result.capacity.outcome, "invalid")
        self.assertEqual(result.capacity.reasons, ("load-current-mismatch",))

    def test_timeout_after_observed_discharge_retains_incomplete_partial(self):
        result = self.live([(0, sample()), (20, sample()), (40, sample()), (50, sample())])
        self.assertEqual(result.stop_reason, "timeout")
        self.assertEqual(result.capacity.outcome, "incomplete")
        self.assertEqual(result.capacity.reasons, ("timeout",))
        self.assertAlmostEqual(result.measurement.amp_hours, 0.013888888888888888)
        self.assertAlmostEqual(result.measurement.watt_hours, 0.16666666666666666)
        self.assertEqual(result.capacity.unobserved_seconds, 10)

    def test_unobserved_tail_invalidates_partial_before_timeout(self):
        result = self.live([(0, sample()), (10, sample())])
        self.assertEqual(result.capacity.outcome, "invalid")
        self.assertIn("last-observation-delay", result.capacity.reasons)
        self.assertIn("timeout", result.capacity.reasons)
        self.assertAlmostEqual(result.measurement.amp_hours, 0.002777777777777778)

    def test_interrupt_signal_and_read_errors_reraise_with_durable_partial_reports(self):
        for error, stop, reason in (
            (KeyboardInterrupt(), "operator-interrupt-stop-sent", "interrupted"),
            (_SessionSignal(signal.SIGTERM), "signal-stop-sent", "signal"),
            (DeviceError("serial disconnected"), "error-stop-sent", "error"),
            (OSError("read failed"), "error-stop-sent", "error"),
        ):
            with self.subTest(error=type(error).__name__):
                self.path = self.path.with_name(type(error).__name__ + ".csv")
                tester = StreamTester(self.clock, [(0, sample()), (10, sample()), (11, error)])
                with StatusCsvLogger(self.path) as logger, self.assertRaises(type(error)) as raised:
                    run_profile(
                        tester,
                        self.profile,
                        logger=logger,
                        clock=self.clock,
                        sleeper=self.clock.sleep,
                    )
                self.assertIs(raised.exception, error)
                self.assertEqual(tester.stop_calls, 1)
                replay = self.replay()
                self.assertEqual(replay.stop_event, stop)
                self.assertEqual(replay.capacity.outcome, "incomplete")
                self.assertEqual(replay.capacity.reasons, (reason,))
                self.assertAlmostEqual(replay.measurement.amp_hours, 0.002777777777777778)
                self.assertAlmostEqual(replay.measurement.watt_hours, 0.03333333333333333)
                self.assertIn("Partial: 0.003 Ah, 0.033 Wh", format_report(replay))

    def test_sample_is_durable_before_output_callback_interrupts(self):
        tester = StreamTester(self.clock, [(0, sample()), (10, sample())])

        def output(_status):
            if self.clock.now == 10:
                raise KeyboardInterrupt

        with StatusCsvLogger(self.path) as logger, self.assertRaises(KeyboardInterrupt):
            run_profile(
                tester,
                self.profile,
                logger=logger,
                clock=self.clock,
                sleeper=self.clock.sleep,
                on_sample=output,
            )
        replay = self.replay()
        self.assertEqual(replay.active_sample_count, 2)
        self.assertAlmostEqual(replay.measurement.amp_hours, 0.002777777777777778)
        self.assertEqual(replay.capacity.outcome, "incomplete")
        self.assertEqual(tester.stop_calls, 1)

    def test_corrupt_values_are_invalid_even_without_observed_discharge(self):
        for value in (float("nan"), float("inf"), -1):
            with self.subTest(value=value):
                policy = CapacityPolicy(CapacityRequest(1, 10.5, 1))
                policy.add(replace(sample(), current_amps=value), 0)
                policy.add(cutoff(), 10)
                result = policy.assess("device-cutoff", 10)
                self.assertEqual(result.outcome, "invalid")
                self.assertIn("corrupt-measurement", result.reasons)
                self.assertFalse(result.discharge_observed)

    def test_metadata_and_quality_reasons_are_durable_without_column_changes(self):
        mismatch = sample(current=0.8)
        self.live([(0, mismatch), (1, mismatch), (2, mismatch), (10, cutoff())])
        with self.path.open(newline="") as stream:
            reader = csv.DictReader(stream)
            self.assertEqual(tuple(reader.fieldnames), CSV_HEADER)
            events = [row["event"] for row in reader]
        request = next(event for event in events if event.startswith(REQUEST_EVENT_PREFIX))
        self.assertEqual(
            json.loads(request.removeprefix(REQUEST_EVENT_PREFIX)),
            {
                "current_amps": 1,
                "cutoff_volts": 10.5,
                "timeout_minutes": 1,
            },
        )
        final = json.loads(events[-1].removeprefix(ASSESSMENT_EVENT_PREFIX))
        self.assertEqual(final["outcome"], "invalid")
        self.assertEqual(final["reasons"], ["load-current-mismatch"])
        self.assertGreater(final["measurement"]["amp_hours"], 0)

    def test_legacy_cutoff_is_unverified_not_completed(self):
        report = summarize_replay_log(load_replay_log(FIXTURES / "a10h_session.csv"))
        self.assertEqual(report.capacity.outcome, "unverified")
        self.assertEqual(report.capacity.reasons, ("missing-capacity-request",))
        self.assertIn("Capacity test: unverified", format_report(report))

    def test_final_metadata_cannot_override_contradictory_samples_or_stop(self):
        for observations, stop in (
            ([(0, cutoff())], "device-cutoff-stop-sent"),
            ([(0, sample()), (10, sample())], "device-cutoff-stop-sent"),
            ([(0, sample()), (10, cutoff())], "timeout-stop-sent"),
            ([(0, sample()), (10, cutoff())], "error-stop-sent"),
            ([(0, sample()), (10, cutoff())], None),
        ):
            with self.subTest(observations=observations, stop=stop):
                self.path = self.path.with_name(f"contradiction-{len(observations)}-{stop}.csv")
                with StatusCsvLogger(self.path) as logger:
                    logger.write_event("capacity-test-started")
                    logger.write_event(CapacityRequest(1, 10.5, 1).event)
                    for elapsed, status_value in observations:
                        logger.write(
                            status_value, event="capacity-sample", session_elapsed_seconds=elapsed
                        )
                    if stop:
                        logger.write_event(stop)
                    logger.write_event(
                        ASSESSMENT_EVENT_PREFIX
                        + json.dumps(
                            {
                                "outcome": "completed",
                                "discharge_observed": True,
                                "reasons": [],
                                "elapsed_seconds": 10,
                            }
                        )
                    )
                self.assertEqual(self.replay().capacity.outcome, "incomplete")

    def test_poststop_sample_cannot_establish_completion(self):
        with StatusCsvLogger(self.path) as logger:
            logger.write_event("capacity-test-started")
            logger.write_event(CapacityRequest(1, 10.5, 1).event)
            logger.write(sample(), event="capacity-sample", session_elapsed_seconds=0)
            logger.write_event("device-cutoff-stop-sent")
            logger.write(cutoff(), event="capacity-sample", session_elapsed_seconds=10)
        self.assertEqual(self.replay().capacity.outcome, "invalid")
        self.assertIn("contradictory-capacity-evidence", self.replay().capacity.reasons)

    def test_cli_and_replay_render_same_literal_capacity_outcomes(self):
        for observations, expected in (
            ([(0, cutoff())], "Capacity test: incomplete"),
            ([(0, sample()), (10, sample()), (20, cutoff())], "Capacity test: completed"),
            (
                [
                    (0, sample(current=0.8)),
                    (1, sample(current=0.8)),
                    (2, sample(current=0.8)),
                    (10, cutoff()),
                ],
                "Capacity test: invalid",
            ),
        ):
            with self.subTest(expected=expected):
                self.path = self.path.with_name(expected.rsplit(" ", 1)[-1] + ".csv")
                tester = StreamTester(self.clock, observations)
                with (
                    StatusCsvLogger(self.path) as logger,
                    patch.object(
                        commands,
                        "_open_resources",
                        return_value=contextlib.nullcontext(
                            resources.CommandResources(tester, logger, self.path)
                        ),
                    ),
                    patch.object(
                        commands,
                        "run_profile",
                        side_effect=partial(
                            runner.run_profile, clock=self.clock, sleeper=self.clock.sleep
                        ),
                    ),
                    contextlib.redirect_stdout(StringIO()) as output,
                ):
                    code = main(
                        [
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
                    )
                self.assertEqual(code, 0)
                self.assertIn(expected, output.getvalue())
                assessment = self.replay().capacity
                text = format_capacity_assessment(assessment)
                self.assertIn(text, output.getvalue())
                with contextlib.redirect_stdout(StringIO()) as report_output:
                    self.assertEqual(main(["report", str(self.path)]), 0)
                self.assertIn(text, report_output.getvalue())

    def test_cli_capacity_start_watchdog_reports_incomplete_and_returns_error(self):
        tester = StreamTester(
            self.clock,
            [
                (0, sample(state=0x00, current=0)),
                (10, sample(state=0x64, current=0)),
                (20, sample(state=0x00, current=0)),
            ],
        )
        with (
            patch.object(resources, "EbcDevice", return_value=tester),
            patch.object(
                commands,
                "run_profile",
                side_effect=partial(runner.run_profile, clock=self.clock, sleeper=self.clock.sleep),
            ),
            contextlib.redirect_stdout(StringIO()) as output,
            contextlib.redirect_stderr(StringIO()) as errors,
        ):
            code = main(
                [
                    "capacity-test",
                    "--current",
                    "1",
                    "--cutoff-voltage",
                    "10.5",
                    "--timeout-minutes",
                    "1",
                    "--execute",
                    "--i-am-present",
                    "--log-dir",
                    self.temporary.name,
                ]
            )
        self.assertEqual(code, 2)
        self.assertEqual(tester.stop_calls, 1)
        self.assertIn("Capacity test: incomplete", output.getvalue())
        self.assertIn("no-observed-discharge", output.getvalue())
        self.assertIn("within 20 seconds of start", errors.getvalue())

    def test_cli_error_and_interrupt_exit_codes_do_not_become_success(self):
        for error, expected in (
            (DeviceError("read failed"), 2),
            (KeyboardInterrupt(), 130),
            (_SessionSignal(signal.SIGTERM), 143),
        ):
            with self.subTest(expected=expected):
                tester = StreamTester(self.clock, [(0, sample()), (10, error)])
                with (
                    patch.object(resources, "EbcDevice", return_value=tester),
                    patch.object(
                        commands,
                        "run_profile",
                        side_effect=partial(
                            runner.run_profile, clock=self.clock, sleeper=self.clock.sleep
                        ),
                    ),
                    contextlib.redirect_stdout(StringIO()) as output,
                    contextlib.redirect_stderr(StringIO()),
                ):
                    code = main(
                        [
                            "capacity-test",
                            "--current",
                            "1",
                            "--cutoff-voltage",
                            "10.5",
                            "--timeout-minutes",
                            "1",
                            "--execute",
                            "--i-am-present",
                            "--log-dir",
                            self.temporary.name,
                        ]
                    )
                self.assertEqual(code, expected)
                self.assertEqual(tester.stop_calls, 1)
                self.assertIn("Capacity test: incomplete", output.getvalue())
                self.assertIn("Partial:", output.getvalue())

    def test_baseline_output_delay_counts_against_first_observation_coverage(self):
        tester = StreamTester(self.clock, [(35, sample()), (45, cutoff())])
        with self.assertRaisesRegex(DeviceError, "within 20 seconds of start") as caught:
            run_profile(
                tester,
                self.profile,
                on_baseline=lambda _: self.clock.sleep(35),
                clock=self.clock,
                sleeper=self.clock.sleep,
            )
        self.assertEqual(tester.stop_calls, 1)
        assessment = getattr(caught.exception, "capacity_assessment", None)
        self.assertIsNotNone(assessment)
        assert assessment is not None
        self.assertEqual(assessment.outcome, CapacityOutcome.INVALID)
        self.assertIn("first-observation-delay", assessment.reasons)

    def test_cutoff_received_at_deadline_is_not_evidence_of_completion(self):
        result = self.live([(0, sample()), (20, sample()), (40, sample()), (60, cutoff())])
        self.assertEqual(result.stop_reason, "timeout")
        self.assertEqual(result.capacity.outcome, "incomplete")
        self.assertEqual(result.samples, 3)

    def test_invalid_quality_takes_precedence_over_incomplete_timeout(self):
        result = self.live(
            [
                (0, sample(current=0.8)),
                (20, sample(current=0.8)),
                (40, sample(current=0.8)),
                (50, sample(current=0.8)),
            ]
        )
        self.assertEqual(result.capacity.outcome, "invalid")
        self.assertIn("load-current-mismatch", result.capacity.reasons)
        self.assertIn("timeout", result.capacity.reasons)

    def test_stop_failure_does_not_become_completed_or_hide_original_exception(self):
        tester = StreamTester(self.clock, [(0, sample()), (10, cutoff())])
        failure = OSError("STOP write failed")
        tester.stop = Mock(side_effect=failure)
        with StatusCsvLogger(self.path) as logger, self.assertRaises(OSError) as raised:
            run_profile(
                tester, self.profile, logger=logger, clock=self.clock, sleeper=self.clock.sleep
            )
        self.assertIs(raised.exception, failure)
        tester.stop.assert_called_once()
        replay = self.replay()
        self.assertEqual(replay.capacity.outcome, "incomplete")
        self.assertEqual(replay.capacity.reasons, ("stop-failed",))
        self.assertGreater(replay.measurement.amp_hours, 0)

    def test_logger_failure_never_masks_read_error_or_prevents_stop(self):
        failure = DeviceError("serial disconnected")
        tester = StreamTester(self.clock, [(0, sample()), (10, sample()), (11, failure)])
        with StatusCsvLogger(self.path) as logger:
            original = logger.write_event

            def fail_assessment(event):
                if event.startswith(ASSESSMENT_EVENT_PREFIX) or "stop-sent" in event:
                    raise OSError("disk full")
                original(event)

            with (
                patch.object(logger, "write_event", side_effect=fail_assessment),
                self.assertRaises(DeviceError) as raised,
            ):
                run_profile(
                    tester, self.profile, logger=logger, clock=self.clock, sleeper=self.clock.sleep
                )
        self.assertIs(raised.exception, failure)
        self.assertEqual(tester.stop_calls, 1)
        self.assertTrue(any("disk full" in note for note in failure.__notes__))
        replay = self.replay()
        self.assertEqual(replay.capacity.outcome, "incomplete")
        self.assertIn("missing-stop-evidence", replay.capacity.reasons)
        self.assertAlmostEqual(replay.measurement.amp_hours, 0.002777777777777778)

    def test_missing_or_malformed_request_or_timing_is_unverified(self):
        for name, request, elapsed in (
            ("missing", None, 0),
            ("malformed", REQUEST_EVENT_PREFIX + "{}", 0),
            ("timing", CapacityRequest(1, 10.5, 1).event, None),
        ):
            with self.subTest(name=name):
                self.path = self.path.with_name(name + ".csv")
                with StatusCsvLogger(self.path) as logger:
                    logger.write_event("capacity-test-started")
                    if request:
                        logger.write_event(request)
                    logger.write(sample(), event="capacity-sample", session_elapsed_seconds=elapsed)
                    logger.write(cutoff(), event="capacity-sample", session_elapsed_seconds=10)
                    logger.write_event("device-cutoff-stop-sent")
                self.assertEqual(self.replay().capacity.outcome, "unverified")

    def test_replay_does_not_combine_appended_sessions_into_one_completion(self):
        with StatusCsvLogger(self.path) as logger:
            for value in (sample(), cutoff()):
                logger.write_event("capacity-test-started")
                logger.write_event(CapacityRequest(1, 10.5, 1).event)
                logger.write(value, event="capacity-sample", session_elapsed_seconds=0)
                logger.write_event("device-cutoff-stop-sent")
        replay = self.replay()
        self.assertEqual(replay.capacity.outcome, "unverified")
        self.assertEqual(replay.capacity.reasons, ("ambiguous-capacity-session",))

    def test_non_capacity_result_remains_optional(self):
        from ebc_cli.session import SessionResult

        self.assertIsNone(SessionResult(SessionStopReason.TIMEOUT, 0, 0).capacity)
        self.assertEqual(CapacityOutcome.COMPLETED, "completed")

    def test_replay_rejects_late_cutoff_and_reversed_observations_as_invalid(self):
        for name, times in (("late", (0, 60)), ("reversed", (10, 0))):
            with self.subTest(name=name):
                self.path = self.path.with_name(name + ".csv")
                with StatusCsvLogger(self.path) as logger:
                    logger.write_event("capacity-test-started")
                    logger.write_event(CapacityRequest(1, 10.5, 1).event)
                    logger.write(
                        sample(), event="capacity-sample", session_elapsed_seconds=times[0]
                    )
                    logger.write(
                        cutoff(), event="capacity-sample", session_elapsed_seconds=times[1]
                    )
                    logger.write_event("device-cutoff-stop-sent")
                self.assertEqual(self.replay().capacity.outcome, "invalid")

    def test_replay_inferred_cutoff_ignores_idle_tail_but_rejects_later_activity(self):
        loaded_cc = sample(state=0x0A, current=1.6, voltage=12.510)
        loaded_below_cutoff = sample(state=0x00, current=1.6, voltage=12.499)
        idle_confirmation = sample(state=0x00, current=0, voltage=12.662)
        observations = [
            (0, loaded_cc),
            (10, loaded_below_cutoff),
            (20, idle_confirmation),
        ]

        def replay_named(name, samples):
            path = self.path.with_name(name)
            with StatusCsvLogger(path) as logger:
                logger.write_event("capacity-test-started")
                logger.write_event(CapacityRequest(1.6, 12.5, 1).event)
                for elapsed, status in samples:
                    logger.write(
                        status, event="capacity-sample", session_elapsed_seconds=elapsed
                    )
                logger.write_event("signal-stop-sent")
            return summarize_replay_log(load_replay_log(path))

        without_tail = replay_named("inferred-cutoff.csv", observations)
        with_idle_tail = replay_named(
            "inferred-cutoff-idle-tail.csv",
            observations
            + [
                (30, sample(state=0x00, current=0, voltage=12.650)),
                (40, sample(state=0x00, current=0, voltage=12.640)),
            ],
        )

        assert without_tail.capacity is not None
        assert with_idle_tail.capacity is not None
        self.assertEqual(with_idle_tail.stop_event, "signal-stop-sent")
        self.assertEqual(with_idle_tail.active_sample_count, 5)
        self.assertEqual(without_tail.capacity.outcome, "completed")
        self.assertEqual(without_tail.capacity.termination, "inferred-cutoff")
        self.assertEqual(with_idle_tail.capacity.outcome, "completed")
        self.assertEqual(with_idle_tail.capacity.termination, "inferred-cutoff")
        self.assertEqual(
            with_idle_tail.capacity.measurement.amp_hours,
            without_tail.capacity.measurement.amp_hours,
        )
        self.assertEqual(
            with_idle_tail.capacity.measurement.watt_hours,
            without_tail.capacity.measurement.watt_hours,
        )

        contradictory = replay_named(
            "inferred-cutoff-contradictory.csv",
            observations + [(30, sample(state=0x0A, current=1.6, voltage=12.640))],
        )
        assert contradictory.capacity is not None
        self.assertEqual(contradictory.capacity.outcome, "invalid")
        self.assertIn("samples-after-cutoff", contradictory.capacity.reasons)

    def test_start_event_with_a_frame_is_not_ordered_start_evidence(self):
        with StatusCsvLogger(self.path) as logger:
            logger.write(sample(), event="capacity-test-started")
            logger.write_event(CapacityRequest(1, 10.5, 1).event)
            logger.write(sample(), event="capacity-sample", session_elapsed_seconds=0)
            logger.write(cutoff(), event="capacity-sample", session_elapsed_seconds=10)
            logger.write_event("device-cutoff-stop-sent")
        self.assertEqual(self.replay().capacity.outcome, "unverified")
