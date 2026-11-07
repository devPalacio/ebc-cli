# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Jay (Python CLI contributions)
# See NOTICE for upstream attribution and port details.

"""Generic charge and capacity profiles, exercised without a physical tester."""

import csv
import inspect
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from functools import partial
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import Mock, call, patch

from support import FakeClock, ProfileTester, status

import ebc_cli
from ebc_cli import (
    DeviceError,
    EbcDevice,
    ProfileError,
    SessionStopReason,
    StatusCsvLogger,
    TesterProfile,
    build_mode_packet,
    cli,
    commands,
    make_capacity_test_profile,
    make_charge_profile,
    make_float_charge_profile,
    make_recovery_profile,
    resources,
    run_profile,
    runner,
)
from ebc_cli.capabilities import A10H_CAPABILITIES
from ebc_cli.modes import CCCV_CHARGE, CONSTANT_CURRENT_DISCHARGE, EbcModeCommand
from ebc_cli.parser import build_parser
from ebc_cli.profiles import ProfileKind
from ebc_cli.protocol import (
    A10H_MODEL,
    CONNECT,
    MODEL_REGISTRY,
    STATUS_CHECKSUM_INDEX,
    STOP,
    EbcStatus,
    checksum,
    encode_scaled,
    parse_status,
)
from ebc_cli.session import _SessionSignal


def _sample(*, current=0.1, voltage=4.2, state=0x10) -> EbcStatus:
    return replace(status(state=state), current_amps=current, voltage_volts=voltage)


def _baseline(voltage) -> EbcStatus:
    frame = bytearray(status().raw)
    frame[4:6] = encode_scaled(voltage * 10)
    frame[STATUS_CHECKSUM_INDEX] = checksum(bytes(frame), STATUS_CHECKSUM_INDEX)
    return parse_status(bytes(frame))


def _profile(**overrides) -> TesterProfile:
    values: dict[str, Any] = {
        "current_amps": 0.5,
        "voltage_volts": 4.2,
        "end_current_amps": 0.1,
        "timeout_minutes": 1,
    }
    values.update(overrides)
    return make_charge_profile(**values)


class _ScriptedTester(ProfileTester):
    def __init__(self, clock, observations, *, baseline=None):
        super().__init__([])
        self.clock = clock
        self.observations = list(observations)
        self.baseline = baseline if baseline is not None else _sample()
        self.commands: list[EbcModeCommand] = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.closed = True

    def begin_mode(self, command):
        self.commands.append(command)
        return self.baseline

    def next_status(self, *, timeout=None, latest=False):
        if not self.observations:
            raise DeviceError("fake status stream ended")
        timestamp, observation = self.observations.pop(0)
        self.clock.now = timestamp
        if isinstance(observation, BaseException):
            raise observation
        return observation


class ChargeProfileTests(unittest.TestCase):
    def test_public_factory_uses_existing_cccv_encoder_and_policy_defaults(self):
        self.assertIn("make_charge_profile", ebc_cli.__all__)
        profile = _profile(end_current_amps=0.037)
        self.assertIs(profile.kind, ProfileKind.CHARGE)
        self.assertEqual(profile.end_current_dwell_seconds, 30)
        self.assertEqual(profile.voltage_tolerance_volts, 0.05)
        self.assertEqual(profile.max_sample_gap_seconds, 5)
        self.assertEqual(profile.start_event, "charge-started")
        self.assertEqual(profile.sample_event, "charge-sample")
        self.assertFalse(profile.stop_on_cutoff)
        self.assertIn("measured current <= 0.037 A", profile.summary)
        self.assertEqual(
            profile.start_command,
            EbcModeCommand(CCCV_CHARGE, current_amps=0.5, voltage_volts=4.2, timeout_minutes=1),
        )
        self.assertEqual(build_mode_packet(profile.start_command).hex(), "fa71003201b4000107f8")
        self.assertEqual(
            build_mode_packet(profile.start_command),
            build_mode_packet(_profile(end_current_amps=0.009).start_command),
        )

    def test_new_profile_fields_are_keyword_only(self):
        parameters = inspect.signature(TesterProfile).parameters
        for name in (
            "end_current_amps",
            "end_current_dwell_seconds",
            "voltage_tolerance_volts",
            "max_sample_gap_seconds",
        ):
            self.assertIs(parameters[name].kind, inspect.Parameter.KEYWORD_ONLY)
        direct = TesterProfile(ProfileKind.CHARGE, 0.5, 1, 4.2, end_current_amps=0.1)
        self.assertEqual(direct, _profile())

    def test_policy_values_must_be_finite_and_positive(self):
        for name in (
            "end_current_amps",
            "end_current_dwell_seconds",
            "voltage_tolerance_volts",
            "max_sample_gap_seconds",
        ):
            for value in (None, True, "0.1", 0, -0.1, float("nan"), float("inf"), float("-inf")):
                with self.subTest(name=name, value=value), self.assertRaises(ProfileError):
                    _profile(**{name: value})

    def test_policy_thresholds_must_be_below_their_setpoints(self):
        for overrides in (
            {"end_current_amps": 0.5},
            {"end_current_amps": 0.6},
            {"voltage_tolerance_volts": 4.2},
            {"voltage_tolerance_volts": 4.3},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ProfileError):
                _profile(**overrides)

    def test_charge_live_settings_are_validated_at_profile_construction(self):
        for name, values in (
            ("current_amps", (0, 0.049, 0.501, 5.01, True, None, float("nan"), float("inf"))),
            ("voltage_volts", (0, -1, 4.201, 22.01, True, None, float("nan"), float("inf"))),
            ("timeout_minutes", (0, -1, 1.5, True, None, 61440, float("nan"), float("inf"))),
        ):
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaises(ProfileError):
                    _profile(**{name: value})
        for current, voltage in ((0.05, 0.01), (5.0, 22.0)):
            profile = _profile(
                current_amps=current,
                voltage_volts=voltage,
                end_current_amps=0.001,
                voltage_tolerance_volts=0.001,
                timeout_minutes=61439,
            )
            profile.start_command.validate_live(A10H_CAPABILITIES)

    def test_custom_capabilities_are_used_before_start(self):
        for capabilities in (
            replace(A10H_CAPABILITIES, charge_current_max_amps=0.4),
            replace(A10H_CAPABILITIES, charge_voltage_max_volts=4.1),
            replace(A10H_CAPABILITIES, current_step_amps=0.2),
            replace(A10H_CAPABILITIES, voltage_step_volts=0.5),
        ):
            with self.subTest(capabilities=capabilities), self.assertRaises(ProfileError):
                _profile(capabilities=capabilities)


class ChargeCompletionTests(unittest.TestCase):
    def _run(self, observations, *, profile=None, baseline=None, logger=None, **callbacks):
        clock = FakeClock()
        tester = _ScriptedTester(clock, observations, baseline=baseline)
        run = run_profile(
            tester,
            profile or _profile(),
            logger=logger,
            clock=clock,
            sleeper=clock.sleep,
            **callbacks,
        )
        self.assertEqual(tester.stop_calls, 1)
        return tester, run

    def test_positive_end_current_near_target_sustained_for_default_dwell(self):
        observations = [(0, _sample(current=0.5, voltage=3.8))]
        observations.extend((time, _sample(state=0x74, voltage=4.18)) for time in range(1, 32, 5))
        baselines, samples = [], []
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "charge.csv"
            with StatusCsvLogger(path) as logger:
                tester, run = self._run(
                    observations,
                    logger=logger,
                    on_baseline=baselines.append,
                    on_sample=samples.append,
                )
            with path.open(newline="", encoding="utf-8") as stream:
                events = [row["event"] for row in csv.DictReader(stream)]
        self.assertIs(run.result.stop_reason, SessionStopReason.END_CURRENT)
        self.assertEqual(run.result.elapsed_seconds, 31)
        self.assertEqual(run.result.samples, len(observations))
        assert run.result.measurement is not None
        self.assertEqual(run.result.measurement.sample_count, len(observations))
        self.assertEqual(samples, [sample for _, sample in observations])
        self.assertEqual(baselines, [run.baseline])
        self.assertEqual(tester.commands, [_profile().start_command])
        self.assertEqual(events[:3], ["schema-v1", "charge-started", "charge-baseline"])
        self.assertEqual(events[-1], "end-current-stop-sent")

    def test_rebound_voltage_departure_zero_and_unexpected_state_reset_dwell(self):
        invalid_samples = [
            _sample(current=0.101),
            _sample(current=0),
            _sample(current=-0.1),
            _sample(voltage=4.149),
            _sample(voltage=4.251),
            *(_sample(state=state) for state in (0x06, 0x68, 0x71, 0x78, 0x14, 0x1A, 0xFF)),
            replace(_sample(), raw=b""),
        ]
        for invalid in invalid_samples:
            with self.subTest(invalid=invalid):
                _, run = self._run(
                    [
                        (0, _sample()),
                        (4, _sample()),
                        (5, invalid),
                        (6, _sample()),
                        (10, _sample()),
                        (11, _sample()),
                    ],
                    profile=_profile(end_current_dwell_seconds=5),
                )
                self.assertIs(run.result.stop_reason, SessionStopReason.END_CURRENT)
                self.assertEqual(run.result.elapsed_seconds, 11)
                self.assertEqual(run.result.samples, 6)

    def test_maximum_gap_boundary_is_allowed_but_larger_gap_restarts_dwell(self):
        for gap, end_time in ((5, 5), (6, 11)):
            observations = [(0, _sample()), (gap, _sample())]
            if end_time != gap:
                observations.append((end_time, _sample()))
            with self.subTest(gap=gap):
                _, run = self._run(observations, profile=_profile(end_current_dwell_seconds=5))
                self.assertIs(run.result.stop_reason, SessionStopReason.END_CURRENT)
                self.assertEqual(run.result.elapsed_seconds, end_time)

    def test_voltage_tolerance_is_symmetric_and_inclusive(self):
        for voltage in (4.15, 4.25):
            with self.subTest(voltage=voltage):
                _, run = self._run(
                    [(0, _sample(voltage=voltage)), (1, _sample(voltage=voltage))],
                    profile=_profile(end_current_dwell_seconds=1),
                )
                self.assertIs(run.result.stop_reason, SessionStopReason.END_CURRENT)

    def test_cached_connect_responses_cannot_satisfy_end_current_dwell(self):
        clock = FakeClock()
        tester = _ScriptedTester(clock, [])

        def no_stream(**_kwargs):
            clock.sleep(1)
            raise DeviceError("no streamed observation")

        tester.next_status = Mock(side_effect=no_stream)
        tester.status = Mock(return_value=_sample())
        with self.assertRaisesRegex(DeviceError, "no streamed observation"):
            run_profile(tester, _profile(), clock=clock, sleeper=clock.sleep)
        tester.status.assert_not_called()
        self.assertEqual(tester.stop_calls, 1)

    def test_stream_read_expiring_at_deadline_times_out_without_connect_fallback(self):
        clock = FakeClock()
        tester = _ScriptedTester(clock, [(60, DeviceError("read timed out"))])
        tester.status = Mock(return_value=_sample())
        run = run_profile(tester, _profile(), clock=clock, sleeper=clock.sleep)
        self.assertIs(run.result.stop_reason, SessionStopReason.TIMEOUT)
        self.assertEqual(run.result.samples, 0)
        self.assertEqual(run.result.elapsed_seconds, 60)
        tester.status.assert_not_called()
        self.assertEqual(tester.stop_calls, 1)

    def test_non_charge_non_capacity_profiles_keep_connect_fallback(self):
        for profile in (
            make_float_charge_profile(current_amps=0.5, voltage_volts=13.7, timeout_minutes=1),
            make_recovery_profile(current_amps=0.1, timeout_minutes=1),
        ):
            clock = FakeClock()
            tester = _ScriptedTester(clock, [(0, DeviceError("poll-only firmware"))])
            tester.status = Mock(return_value=_sample(state=0x14, current=0))
            with self.subTest(kind=profile.kind):
                run = run_profile(
                    tester, profile, clock=clock, sleeper=clock.sleep, poll_interval=60
                )
                self.assertIs(run.result.stop_reason, SessionStopReason.TIMEOUT)
                self.assertEqual(run.result.samples, 1)
                tester.status.assert_called_once_with(timeout=60, latest=True)
                self.assertEqual(tester.stop_calls, 1)

    def test_cached_connect_response_cannot_supply_activity_or_auto_off(self):
        for cached, streamed in (
            (_sample(), _sample(state=0x1B, current=0)),
            (_sample(state=0x1B, current=0), _sample()),
        ):
            clock = FakeClock()
            observations = (
                [(0, DeviceError("stream missing")), (1, streamed)]
                if cached.current_amps > 0
                else [(0, streamed), (1, DeviceError("stream missing"))]
            )
            tester = _ScriptedTester(clock, observations)
            tester.status = Mock(return_value=cached)
            with self.subTest(cached=cached), self.assertRaisesRegex(DeviceError, "stream missing"):
                run_profile(tester, _profile(), clock=clock, sleeper=clock.sleep)
            tester.status.assert_not_called()
            self.assertEqual(tester.stop_calls, 1)

    def test_output_delay_cannot_hide_a_sample_gap(self):
        for output in ("callback", "logger"):
            clock = FakeClock()
            tester = _ScriptedTester(clock, [(0, _sample()), (31, _sample()), (60, _sample())])
            delays = iter((30, 4))

            def slow_output(
                _status, *, event="charge-sample", clock=clock, delays=delays, **_kwargs
            ):
                if event == "charge-sample":
                    clock.sleep(next(delays))

            logger = Mock(write=slow_output) if output == "logger" else None
            with self.subTest(output=output):
                run = run_profile(
                    tester,
                    _profile(end_current_dwell_seconds=5),
                    logger=logger,
                    on_sample=slow_output if output == "callback" else None,
                    clock=clock,
                    sleeper=clock.sleep,
                )
                self.assertIs(run.result.stop_reason, SessionStopReason.TIMEOUT)
                self.assertEqual(run.result.samples, 2)
                self.assertEqual(tester.stop_calls, 1)

    def test_output_delay_does_not_count_towards_dwell(self):
        for output in ("callback", "logger"):
            clock = FakeClock()
            tester = _ScriptedTester(clock, [(0, _sample()), (1, _sample()), (60, _sample())])
            delays = iter((0, 5))

            def slow_output(
                _status, *, event="charge-sample", clock=clock, delays=delays, **_kwargs
            ):
                if event == "charge-sample":
                    clock.sleep(next(delays))

            with self.subTest(output=output):
                run = run_profile(
                    tester,
                    _profile(end_current_dwell_seconds=5, max_sample_gap_seconds=10),
                    logger=Mock(write=slow_output) if output == "logger" else None,
                    on_sample=slow_output if output == "callback" else None,
                    clock=clock,
                    sleeper=clock.sleep,
                )
                self.assertIs(run.result.stop_reason, SessionStopReason.TIMEOUT)
                self.assertEqual(run.result.samples, 2)
                self.assertEqual(tester.stop_calls, 1)

    def test_deadline_expiring_during_output_beats_end_current_and_auto_off(self):
        for output in ("callback", "logger"):
            for terminal in (_sample(), _sample(state=0x1B, current=0)):
                clock = FakeClock()
                tester = _ScriptedTester(
                    clock,
                    [(time, _sample()) for time in range(0, 59, 5)] + [(59, terminal)],
                )

                def slow_output(_status, *, event="charge-sample", clock=clock, **_kwargs):
                    if event == "charge-sample" and clock.now == 59:
                        clock.sleep(2)

                with self.subTest(output=output, terminal=terminal):
                    run = run_profile(
                        tester,
                        _profile(end_current_dwell_seconds=59, max_sample_gap_seconds=10),
                        logger=Mock(write=slow_output) if output == "logger" else None,
                        on_sample=slow_output if output == "callback" else None,
                        clock=clock,
                        sleeper=clock.sleep,
                    )
                    self.assertIs(run.result.stop_reason, SessionStopReason.TIMEOUT)
                    self.assertEqual(run.result.elapsed_seconds, 61)
                    self.assertEqual(run.result.samples, 13)
                    self.assertEqual(tester.stop_calls, 1)

    def test_zero_low_voltage_and_unexpected_state_never_complete(self):
        for sample in (_sample(current=0), _sample(voltage=3.8), _sample(state=0x68)):
            with self.subTest(sample=sample):
                _, run = self._run([(time, sample) for time in range(0, 61, 5)])
                self.assertIs(run.result.stop_reason, SessionStopReason.TIMEOUT)
                self.assertEqual(run.result.elapsed_seconds, 60)

    def test_auto_off_requires_poststart_positive_active_current(self):
        for baseline in (_sample(current=0.5), _sample(state=0x1B, current=0)):
            with self.subTest(baseline=baseline):
                observations = [
                    (0, _sample(state=0x1B, current=0)),
                    (1, _sample(state=0x74, current=0)),
                    (2, _sample(state=0x1B, current=0)),
                    (3, _sample(current=0.5, voltage=3.8)),
                    (4, _sample(state=0x1B, current=0)),
                ]
                _, run = self._run(observations, baseline=baseline)
                self.assertIs(run.result.stop_reason, SessionStopReason.MODE_AUTO_OFF)
                self.assertEqual(run.result.elapsed_seconds, 4)
                self.assertEqual(run.result.samples, len(observations))

    def test_stale_auto_off_without_positive_activity_times_out(self):
        for state in (0x10, 0x74, 0x68):
            observations = [(0, _sample(state=state, current=0 if state != 0x68 else 0.5))]
            observations.extend((time, _sample(state=0x1B, current=0)) for time in range(1, 62, 5))
            with self.subTest(state=state):
                _, run = self._run(observations)
                self.assertIs(run.result.stop_reason, SessionStopReason.TIMEOUT)

    def test_baseline_does_not_count_towards_dwell(self):
        clock = FakeClock()
        tester = _ScriptedTester(clock, [(20, _sample()), (25, _sample())])
        run = run_profile(
            tester,
            _profile(end_current_dwell_seconds=5),
            clock=clock,
            sleeper=clock.sleep,
            on_baseline=lambda _baseline: clock.sleep(20),
        )
        self.assertIs(run.result.stop_reason, SessionStopReason.END_CURRENT)
        self.assertEqual(run.result.samples, 2)
        self.assertEqual(run.result.elapsed_seconds, 5)
        self.assertEqual(tester.stop_calls, 1)

    def test_same_profile_has_fresh_activity_and_dwell_state_on_each_run(self):
        profile = _profile(end_current_dwell_seconds=5)
        clock = FakeClock()
        tester = _ScriptedTester(clock, [(0, _sample()), (5, _sample())])
        first = run_profile(tester, profile, clock=clock, sleeper=clock.sleep)
        self.assertIs(first.result.stop_reason, SessionStopReason.END_CURRENT)
        tester.observations = [
            (10, _sample(state=0x1B, current=0)),
            (11, _sample()),
            (16, _sample()),
        ]
        clock.now = 10
        second = run_profile(tester, profile, clock=clock, sleeper=clock.sleep)
        self.assertIs(second.result.stop_reason, SessionStopReason.END_CURRENT)
        self.assertEqual(second.result.samples, 3)
        self.assertEqual(second.result.elapsed_seconds, 6)
        self.assertEqual(tester.stop_calls, 2)

    def test_deadline_wins_over_end_current_and_auto_off(self):
        for terminal in (_sample(), _sample(state=0x1B, current=0)):
            with self.subTest(terminal=terminal):
                observations = [(time, _sample()) for time in range(0, 60, 5)]
                observations.append((60, terminal))
                _, run = self._run(observations, profile=_profile(end_current_dwell_seconds=60))
                self.assertIs(run.result.stop_reason, SessionStopReason.TIMEOUT)
                self.assertEqual(run.result.samples, len(observations) - 1)

    def test_saved_status_variants_are_accepted_as_activity_not_command_opcodes(self):
        fixture = Path(__file__).with_name("fixtures") / "a10h_live_cycles.csv"
        with fixture.open(newline="", encoding="utf-8") as stream:
            captured = [
                parse_status(bytes.fromhex(row["raw_frame_hex"]))
                for row in csv.DictReader(stream)
                if row["event"] == "charge-status"
            ]
        self.assertEqual([sample.state_byte for sample in captured], [0x74, 0x10])
        _, run = self._run(
            [(0, captured[0]), (1, captured[1]), (2, _sample(state=0x1B, current=0))],
            profile=_profile(voltage_volts=13.7),
        )
        self.assertIs(run.result.stop_reason, SessionStopReason.MODE_AUTO_OFF)
        self.assertEqual(run.result.samples, 3)

    def test_serial_callback_interrupt_and_signal_errors_stop_once(self):
        for error in (DeviceError("read failed"), KeyboardInterrupt(), _SessionSignal(15)):
            clock = FakeClock()
            tester = _ScriptedTester(clock, [(0, error)])
            with self.subTest(error=error), self.assertRaises(type(error)):
                run_profile(tester, _profile(), clock=clock, sleeper=clock.sleep)
            self.assertEqual(tester.stop_calls, 1)

        def fail_callback(_status):
            raise RuntimeError("callback")

        for callback in ("on_baseline", "on_sample"):
            clock = FakeClock()
            tester = _ScriptedTester(clock, [(0, _sample())])
            with self.subTest(callback=callback), self.assertRaisesRegex(RuntimeError, "callback"):
                callbacks: dict[str, Any] = {callback: fail_callback}
                run_profile(
                    tester,
                    _profile(),
                    clock=clock,
                    sleeper=clock.sleep,
                    **callbacks,
                )
            self.assertEqual(tester.stop_calls, 1)

    def test_nonfinite_measurements_abort_and_stop(self):
        for field in ("current_amps", "voltage_volts"):
            for value in (float("nan"), float("inf"), float("-inf")):
                clock = FakeClock()
                sample = replace(_sample(), **{field: value})
                tester = _ScriptedTester(clock, [(0, sample)])
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    run_profile(tester, _profile(), clock=clock, sleeper=clock.sleep)
                self.assertEqual(tester.stop_calls, 1)


class GenericCapacityTests(unittest.TestCase):
    def test_capacity_accepts_cutoff_power_at_the_ceiling(self):
        profile = make_capacity_test_profile(current_amps=10, cutoff_volts=15, timeout_minutes=1)
        self.assertEqual((profile.current_amps, profile.cutoff_volts), (10, 15))

    def test_capacity_rejects_impossible_cutoff_power_before_io(self):
        for capabilities, current, cutoff in (
            (A10H_CAPABILITIES, 10, 20),
            (replace(A10H_CAPABILITIES, power_max_watts=20), 5, 5),
        ):
            with (
                self.subTest(capabilities=capabilities, current=current, cutoff=cutoff),
                self.assertRaisesRegex(ProfileError, "power"),
            ):
                make_capacity_test_profile(
                    current_amps=current,
                    cutoff_volts=cutoff,
                    timeout_minutes=1,
                    capabilities=capabilities,
                )

    def test_capacity_cli_rejects_impossible_cutoff_power_before_resources_open(self):
        with (
            patch.object(
                commands, "_open_resources", side_effect=AssertionError("resources opened")
            ) as opened,
            redirect_stderr(StringIO()),
        ):
            result = cli.main(
                [
                    "capacity-test",
                    "--current",
                    "10",
                    "--cutoff-voltage",
                    "20",
                    "--timeout-minutes",
                    "1",
                    "--execute",
                    "--i-am-present",
                ]
            )
        self.assertEqual(result, 2)
        opened.assert_not_called()

    def test_capacity_and_generic_cc_check_baseline_power_before_start(self):
        for source in ("capacity", "generic-cc"):
            for voltage in (12, 14.4, 15, 18):
                with self.subTest(source=source, voltage=voltage):
                    command = (
                        make_capacity_test_profile(
                            current_amps=10, cutoff_volts=12, timeout_minutes=1
                        ).start_command
                        if source == "capacity"
                        else EbcModeCommand(
                            CONSTANT_CURRENT_DISCHARGE,
                            current_amps=10,
                            cutoff_volts=12,
                            timeout_minutes=1,
                        )
                    )
                    device = EbcDevice()
                    device._fd = 42
                    baseline = _baseline(voltage)
                    device._write_all = Mock()
                    with (
                        patch.object(
                            device,
                            "_read_status_frame",
                            side_effect=[_baseline(12).raw, baseline.raw],
                        ),
                        patch.object(device, "_discard_pending_input"),
                    ):
                        if voltage > 15:
                            with self.assertRaisesRegex(DeviceError, "power"):
                                device.begin_mode(command)
                            self.assertEqual(
                                device._write_all.call_args_list,
                                [call(CONNECT), call(STOP), call(CONNECT)],
                            )
                            self.assertFalse(device._control_active)
                        else:
                            self.assertEqual(device.begin_mode(command), baseline)
                            self.assertEqual(
                                device._write_all.call_args_list,
                                [
                                    call(CONNECT),
                                    call(STOP),
                                    call(CONNECT),
                                    call(build_mode_packet(command)),
                                ],
                            )
                            self.assertTrue(device._control_active)
                    device._fd = None

    def test_baseline_guard_uses_registered_power_ceiling_not_profile_override(self):
        actual_capabilities = replace(A10H_CAPABILITIES, power_max_watts=100)
        model = replace(A10H_MODEL, capabilities=actual_capabilities)
        profile = make_capacity_test_profile(
            current_amps=10,
            cutoff_volts=12,
            timeout_minutes=1,
            capabilities=replace(A10H_CAPABILITIES, power_max_watts=300),
        )
        baseline = _baseline(12)
        with patch.dict(MODEL_REGISTRY, {model.model_id: model}):
            device = EbcDevice(model=model)
            device._fd = 42
            device._write_all = Mock()
            with (
                patch.object(device, "_read_status_frame", return_value=baseline.raw),
                patch.object(device, "_discard_pending_input"),
                self.assertRaisesRegex(DeviceError, "power"),
            ):
                device.begin_mode(profile.start_command)
        self.assertEqual(
            device._write_all.call_args_list, [call(CONNECT), call(STOP), call(CONNECT)]
        )
        self.assertFalse(device._control_active)
        device._fd = None

    def test_discharge_power_ceiling_does_not_limit_charge(self):
        capabilities = replace(A10H_CAPABILITIES, power_max_watts=1)
        model = replace(A10H_MODEL, capabilities=capabilities)
        profile = _profile(current_amps=5, voltage_volts=22, capabilities=capabilities)
        with patch.dict(MODEL_REGISTRY, {model.model_id: model}):
            device = EbcDevice(model=model)
            device._fd = 42
            device._write_all = Mock()
            with (
                patch.object(device, "_read_status_frame", return_value=_baseline(18).raw),
                patch.object(device, "_discard_pending_input"),
            ):
                device.begin_mode(profile.start_command)
            device._write_all.assert_called_with(build_mode_packet(profile.start_command))
            self.assertTrue(device._control_active)
            device._fd = None

    def test_single_cell_and_12v_settings_use_existing_cc_discharge(self):
        for current, cutoff in ((0.05, 2.5), (0.5, 3.0), (0.8, 10.5), (10.0, 12.0)):
            with self.subTest(current=current, cutoff=cutoff):
                profile = make_capacity_test_profile(
                    current_amps=current,
                    cutoff_volts=cutoff,
                    timeout_minutes=61439,
                )
                self.assertIs(profile.start_command.mode, CONSTANT_CURRENT_DISCHARGE)
                self.assertTrue(profile.stop_on_cutoff)
                clock = FakeClock()
                tester = _ScriptedTester(
                    clock,
                    [(0, _sample(state=0x14, current=0, voltage=cutoff))],
                    baseline=_baseline(4.2 if cutoff < 10 else 14.4),
                )
                run = run_profile(tester, profile, clock=clock, sleeper=clock.sleep)
                self.assertIs(run.result.stop_reason, SessionStopReason.DEVICE_CUTOFF)
                assert run.result.capacity is not None
                self.assertEqual(run.result.capacity.outcome, "incomplete")
                self.assertIn("no-observed-discharge", run.result.capacity.reasons)
                self.assertEqual(tester.commands, [profile.start_command])
                self.assertEqual(tester.stop_calls, 1)

    def test_capacity_requires_positive_cutoff_capabilities_and_representable_live_timeout(self):
        for name, values in (
            ("current_amps", (0, 0.049, 10.01, 0.051, float("nan"), float("inf"))),
            ("cutoff_volts", (None, True, 0, -1, 30.01, 2.501, float("nan"), float("inf"))),
            ("timeout_minutes", (0, -1, 1.5, True, None, 61440, float("nan"), float("inf"))),
        ):
            for value in values:
                values_by_name: dict[str, Any] = {
                    "current_amps": 0.5,
                    "cutoff_volts": 2.5,
                    "timeout_minutes": 60,
                }
                values_by_name[name] = value
                with self.subTest(name=name, value=value), self.assertRaises(ProfileError):
                    make_capacity_test_profile(**values_by_name)


class SingleCellCliTests(unittest.TestCase):
    charge_flags = (
        "--current",
        "0.5",
        "--voltage",
        "4.2",
        "--end-current",
        "0.1",
        "--timeout-minutes",
        "60",
    )
    capacity_flags = ("--current", "0.5", "--cutoff-voltage", "2.5", "--timeout-minutes", "60")

    def test_charge_and_capacity_require_every_battery_setting(self):
        parser = build_parser()
        for command, flags in (
            ("charge", self.charge_flags),
            ("capacity-test", self.capacity_flags),
        ):
            for offset in range(0, len(flags), 2):
                with (
                    self.subTest(command=command, missing=flags[offset]),
                    redirect_stderr(StringIO()),
                ):
                    with self.assertRaises(SystemExit) as caught:
                        parser.parse_args([command, *flags[:offset], *flags[offset + 2 :]])
                    self.assertEqual(caught.exception.code, 2)

    def test_charge_policy_defaults_and_overrides(self):
        parser = build_parser()
        args = parser.parse_args(["charge", *self.charge_flags])
        self.assertEqual(args.end_current_dwell_seconds, 30)
        self.assertEqual(args.voltage_tolerance, 0.05)
        self.assertEqual(args.max_sample_gap_seconds, 5)
        with patch.object(commands, "_run_profile", return_value=0) as run:
            result = cli.main(
                [
                    "charge",
                    *self.charge_flags,
                    "--end-current-dwell-seconds",
                    "12.5",
                    "--voltage-tolerance",
                    "0.025",
                    "--max-sample-gap-seconds",
                    "2.5",
                    "--execute",
                    "--i-am-present",
                ]
            )
        self.assertEqual(result, 0)
        profile = run.call_args.args[1]
        self.assertEqual(profile.end_current_dwell_seconds, 12.5)
        self.assertEqual(profile.voltage_tolerance_volts, 0.025)
        self.assertEqual(profile.max_sample_gap_seconds, 2.5)

    def test_each_confirmation_is_required_before_resources_open(self):
        for command, flags in (
            ("charge", self.charge_flags),
            ("capacity-test", self.capacity_flags),
        ):
            for confirmations in ([], ["--execute"], ["--i-am-present"]):
                with (
                    self.subTest(command=command, confirmations=confirmations),
                    patch.object(commands, "_open_resources") as opened,
                    redirect_stderr(StringIO()),
                ):
                    self.assertEqual(cli.main([command, *flags, *confirmations]), 2)
                    opened.assert_not_called()

    def test_invalid_charge_and_capacity_settings_fail_before_resources_open(self):
        cases: list[tuple[str, tuple[str, ...], str, str]] = [
            ("charge", self.charge_flags, flag, value)
            for flag, value in (
                ("--current", "0.049"),
                ("--current", "5.01"),
                ("--current", "0.501"),
                ("--voltage", "0"),
                ("--voltage", "22.01"),
                ("--voltage", "4.201"),
                ("--end-current", "0"),
                ("--end-current", "0.5"),
                ("--end-current", "nan"),
                ("--timeout-minutes", "0"),
                ("--timeout-minutes", "61440"),
                ("--end-current-dwell-seconds", "0"),
                ("--end-current-dwell-seconds", "inf"),
                ("--voltage-tolerance", "4.2"),
                ("--voltage-tolerance", "nan"),
                ("--max-sample-gap-seconds", "0"),
                ("--max-sample-gap-seconds", "inf"),
            )
        ]
        cases.extend(
            ("capacity-test", self.capacity_flags, flag, value)
            for flag, value in (
                ("--current", "0.049"),
                ("--current", "10.01"),
                ("--cutoff-voltage", "0"),
                ("--cutoff-voltage", "30.01"),
                ("--cutoff-voltage", "nan"),
                ("--timeout-minutes", "0"),
                ("--timeout-minutes", "61440"),
            )
        )
        for command, flags, flag, value in cases:
            with (
                self.subTest(command=command, flag=flag, value=value),
                patch.object(commands, "_open_resources") as opened,
                redirect_stderr(StringIO()),
            ):
                self.assertEqual(
                    cli.main(
                        [
                            command,
                            *flags,
                            flag,
                            value,
                            "--execute",
                            "--i-am-present",
                        ]
                    ),
                    2,
                )
                opened.assert_not_called()

    def test_charge_cli_runs_existing_resources_logging_and_end_current_message(self):
        clock = FakeClock()
        tester = _ScriptedTester(clock, [(0, _sample()), (1, _sample())])
        output = StringIO()
        with TemporaryDirectory() as temporary_directory:
            with (
                patch.object(resources, "EbcDevice", return_value=tester) as opened,
                patch.object(
                    commands,
                    "run_profile",
                    side_effect=partial(runner.run_profile, clock=clock, sleeper=clock.sleep),
                ),
                redirect_stdout(output),
            ):
                result = cli.main(
                    [
                        "charge",
                        *self.charge_flags,
                        "--end-current-dwell-seconds",
                        "1",
                        "--execute",
                        "--i-am-present",
                        "--log-dir",
                        temporary_directory,
                    ]
                )
            paths = list(Path(temporary_directory).glob("*.csv"))
            self.assertEqual(len(paths), 1)
            with paths[0].open(newline="", encoding="utf-8") as stream:
                events = [row["event"] for row in csv.DictReader(stream)]
        self.assertEqual(result, 0)
        opened.assert_called_once_with("/dev/ttyUSB0", 1.0, lock_device=True)
        self.assertEqual(tester.stop_calls, 1)
        self.assertTrue(tester.closed)
        self.assertIn(
            "End-current dwell reached near target voltage; stop frame sent.", output.getvalue()
        )
        self.assertIn("Log: ", output.getvalue())
        self.assertEqual(events[-1], "end-current-stop-sent")

    def test_capacity_cli_accepts_single_cell_and_12v_settings_without_logging(self):
        for current, cutoff in (("0.05", "2.5"), ("0.8", "10.5")):
            clock = FakeClock()
            tester = _ScriptedTester(clock, [(0, _sample(state=0x14, current=0))])
            with (
                self.subTest(current=current, cutoff=cutoff),
                patch.object(resources, "EbcDevice", return_value=tester),
                patch.object(
                    commands,
                    "run_profile",
                    side_effect=partial(runner.run_profile, clock=clock, sleeper=clock.sleep),
                ),
                redirect_stdout(StringIO()),
            ):
                self.assertEqual(
                    cli.main(
                        [
                            "capacity-test",
                            "--current",
                            current,
                            "--cutoff-voltage",
                            cutoff,
                            "--timeout-minutes",
                            "60",
                            "--execute",
                            "--i-am-present",
                            "--no-log",
                        ]
                    ),
                    0,
                )
            self.assertEqual(tester.commands[0].current_amps, float(current))
            self.assertEqual(tester.commands[0].cutoff_volts, float(cutoff))
            self.assertEqual(tester.stop_calls, 1)
            self.assertTrue(tester.closed)


if __name__ == "__main__":
    unittest.main()
