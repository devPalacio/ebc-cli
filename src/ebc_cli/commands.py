# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Jay (Python CLI contributions)
# See NOTICE for upstream attribution and port details.

"""Command handlers for the ebc CLI."""

import argparse
import math
import sys
from pathlib import Path

from .capabilities import A10H_CAPABILITIES
from .capacity import format_capacity_assessment
from .csvlog import StatusCsvLogger
from .device import DeviceError
from .diagnostics import diagnose_device
from .modes import (
    LEAD_ACID_CHARGE,
    SOURCE_OPERATING_MODES,
    EbcModeCommand,
    EbcModeKind,
    EbcModePhase,
    EbcOperatingMode,
    build_mode_packet,
    get_operating_mode,
)
from .profiles import (
    BulkToFloatProfile,
    ProfileKind,
    TesterProfile,
    make_bulk_to_float_profile,
    make_capacity_test_profile,
    make_charge_profile,
    make_float_charge_profile,
    make_recovery_profile,
)
from .protocol import EbcStatus, ProtocolError
from .report import ReportError, format_report, load_replay_log, summarize_replay_log
from .resources import command_resources, reserve_log_path
from .runner import run_bulk_to_float, run_profile
from .session import (
    SessionResult,
    SessionStopReason,
    SupervisedSession,
    run_active_session,
    run_monitor_session,
)


def execute_command(args: argparse.Namespace) -> int:
    """Execute one parsed command and return its process exit code."""

    handlers = {
        "doctor": handle_doctor,
        "plan-lead-acid-charge": handle_plan_lead_acid_charge,
        "recover-lead-acid": handle_recover_lead_acid,
        "float-charge": handle_float_charge,
        "bulk-to-float": handle_bulk_to_float,
        "capacity-test": handle_capacity_test,
        "charge": handle_charge,
        "stop": handle_stop,
        "status": handle_status,
        "monitor": handle_monitor,
        "modes": handle_list_modes,
        "list-modes": handle_list_modes,
        "plan-mode": handle_plan_mode,
        "run-mode": handle_run_mode,
        "report": handle_report,
    }
    return handlers[args.command](args)


def handle_doctor(args: argparse.Namespace) -> int:
    report, ready = diagnose_device(args.device)
    print(report)
    return 0 if ready else 2


def handle_plan_lead_acid_charge(args: argparse.Namespace) -> int:
    packet = build_mode_packet(
        EbcModeCommand(
            LEAD_ACID_CHARGE,
            current_amps=args.current,
            cells=args.cells,
            timeout_minutes=args.timeout_minutes,
        ),
        capabilities=A10H_CAPABILITIES,
    )
    voltage = args.cells * 2.4
    print("PLAN ONLY — no command was sent to the tester")
    print(f"PbAc: {args.cells} cells, {voltage:.1f} V target, {args.current:.3g} A")
    print(f"Safety timeout: {args.timeout_minutes} minutes")
    print(f"Frame: {packet.hex().upper()}")
    return 0


def handle_recover_lead_acid(args: argparse.Namespace) -> int:
    _require_supervised_execution(args, "recovery")
    profile = make_recovery_profile(
        current_amps=args.current,
        timeout_minutes=args.timeout_minutes,
    )
    return _run_profile(args, profile)


def handle_float_charge(args: argparse.Namespace) -> int:
    _require_supervised_execution(args, "float charge")
    profile = make_float_charge_profile(
        current_amps=args.current,
        voltage_volts=args.voltage,
        timeout_minutes=args.timeout_minutes,
    )
    return _run_profile(args, profile)


def handle_bulk_to_float(args: argparse.Namespace) -> int:
    _require_supervised_execution(args, "bulk-to-float")
    profile = make_bulk_to_float_profile(
        bulk_current_amps=args.bulk_current,
        bulk_timeout_minutes=args.bulk_timeout_minutes,
        float_current_amps=args.float_current,
        float_voltage_volts=args.float_voltage,
        float_timeout_minutes=args.float_timeout_minutes,
    )
    return _run_bulk_to_float_profile(args, profile)


def handle_charge(args: argparse.Namespace) -> int:
    _require_supervised_execution(args, "charge")
    profile = make_charge_profile(
        current_amps=args.current,
        voltage_volts=args.voltage,
        end_current_amps=args.end_current,
        timeout_minutes=args.timeout_minutes,
        end_current_dwell_seconds=args.end_current_dwell_seconds,
        voltage_tolerance_volts=args.voltage_tolerance,
        max_sample_gap_seconds=args.max_sample_gap_seconds,
    )
    return _run_profile(args, profile)


def handle_capacity_test(args: argparse.Namespace) -> int:
    _require_supervised_execution(args, "capacity test")
    profile = make_capacity_test_profile(
        current_amps=args.current,
        cutoff_volts=args.cutoff_voltage,
        timeout_minutes=args.timeout_minutes,
    )
    return _run_profile(
        args,
        profile,
        report_log_before_sampling=True,
        show_baseline=False,
    )


def _run_profile(
    args: argparse.Namespace,
    profile: TesterProfile,
    *,
    report_log_before_sampling: bool = False,
    show_baseline: bool = True,
) -> int:
    with _open_resources(args) as resources:

        def on_baseline(baseline: EbcStatus) -> None:
            print(profile.summary, flush=profile.kind.value == "capacity-test")
            if show_baseline:
                print("Baseline: ", end="")
                print_status(baseline)
            if report_log_before_sampling:
                _report_log(resources.log_path)

        try:
            run = run_profile(
                resources.tester,
                profile,
                logger=resources.logger,
                on_baseline=on_baseline,
                on_sample=print_status,
            )
        except BaseException as exc:
            if profile.kind is ProfileKind.CAPACITY_TEST:
                try:
                    partial = getattr(exc, "capacity_assessment", None)
                    if partial is None and resources.log_path is not None:
                        partial = summarize_replay_log(load_replay_log(resources.log_path)).capacity
                    if partial is not None:
                        print(format_capacity_assessment(partial), flush=True)
                except (OSError, ReportError) as report_error:
                    exc.add_note(f"Could not report partial capacity log: {report_error}")
            raise
        _report_session_result(run.result)
    if not report_log_before_sampling:
        _report_log(resources.log_path)
    return 0


def _run_bulk_to_float_profile(args: argparse.Namespace, profile: BulkToFloatProfile) -> int:
    with _open_resources(args) as resources:

        def on_baseline(baseline: EbcStatus) -> None:
            print(profile.summary, flush=True)
            print("Bulk baseline: ", end="")
            print_status(baseline)

        run = run_bulk_to_float(
            resources.tester,
            profile,
            logger=resources.logger,
            on_baseline=on_baseline,
            on_sample=print_status,
            on_float_start=_print_float_start,
        )
        _report_session_result(run.result)
    _report_log(resources.log_path)
    return 0


def _open_resources(
    args: argparse.Namespace,
    *,
    lock_device: bool = True,
    no_log: bool | None = None,
):
    return command_resources(
        device=args.device,
        timeout=args.timeout,
        command=args.command,
        log_dir=args.log_dir,
        no_log=args.no_log if no_log is None else no_log,
        lock_device=lock_device,
    )


def _require_supervised_execution(args: argparse.Namespace, operation: str) -> None:
    if not args.execute or not args.i_am_present:
        raise DeviceError(f"{operation} requires both --execute and --i-am-present")


def _report_session_result(result: SessionResult) -> None:
    if result.capacity is not None:
        print(format_capacity_assessment(result.capacity), flush=True)
    stop_messages = {
        SessionStopReason.TIMEOUT: "Hard timeout reached; stop frame sent.",
        SessionStopReason.BULK_TIMEOUT: (
            "Bulk phase timeout reached; stop frame sent; float phase was not started."
        ),
        SessionStopReason.FLOAT_TIMEOUT: "Float hold timeout reached; stop frame sent.",
        SessionStopReason.DEVICE_CUTOFF: "Device cutoff reached; stop frame sent.",
        SessionStopReason.MODE_AUTO_OFF: "Device mode auto-off reached; stop frame sent.",
        SessionStopReason.END_CURRENT: "End-current dwell reached near target voltage; stop frame sent.",
        SessionStopReason.ZERO_CURRENT: "Device current reached zero; stop frame sent.",
    }
    print(stop_messages[result.stop_reason], flush=True)
    print(
        f"Session: {result.elapsed_seconds:.1f} s, {result.samples} active sample(s)",
        flush=True,
    )
    if result.final_status is not None:
        print("Terminal: ", end="")
        print_status(result.final_status)
    if result.measurement is not None:
        print(
            f"Host integration: {result.measurement.amp_hours:.3f} Ah, "
            f"{result.measurement.watt_hours:.3f} Wh",
            flush=True,
        )
        if result.measurement.skipped_intervals:
            print(
                f"Skipped {result.measurement.skipped_intervals} interval(s) over "
                f"{result.measurement.skipped_seconds:.1f} s",
                flush=True,
            )


def handle_stop(args: argparse.Namespace) -> int:
    if not args.execute:
        raise DeviceError("stop requires --execute")
    # STOP must remain available even when the requested log directory is
    # unavailable. Open only the unlogged emergency scope until the frame has
    # been sent, then make a best-effort log record independently.
    with _open_resources(args, lock_device=False, no_log=True) as resources:
        resources.tester.stop()
    print("Stop frame sent.")
    if args.no_log:
        return 0
    try:
        log_path = reserve_log_path(args.command, args.log_dir)
        with StatusCsvLogger(log_path) as logger:
            logger.write_event("stop-sent")
    except Exception as exc:  # noqa: BLE001 - STOP success must survive any logger failure.
        print(f"Stop frame sent, but logging failed: {exc}", file=sys.stderr)
    else:
        _report_log(log_path)
    return 0


def handle_status(args: argparse.Namespace) -> int:
    with _open_resources(args) as resources:
        status = resources.tester.fresh_status()
        print_status(status)
        if resources.logger is not None:
            resources.logger.write(status)
    _report_log(resources.log_path)
    return 0


def handle_monitor(args: argparse.Namespace) -> int:
    if not math.isfinite(args.interval) or args.interval <= 0:
        raise DeviceError("interval must be finite and greater than zero")
    if args.count is not None and args.count <= 0:
        raise DeviceError("count must be greater than zero")
    with _open_resources(args) as resources:
        result = run_monitor_session(
            tester=resources.tester,
            logger=resources.logger,
            printer=print_status,
            interval=args.interval,
            count=args.count,
        )
    print(f"Logged {result.samples} sample(s)")
    _report_log(resources.log_path)
    return 0


def handle_report(args: argparse.Namespace) -> int:
    """Replay one CSV session and print its read-only measurement summary."""

    log = load_replay_log(args.path)
    print(format_report(summarize_replay_log(log)))
    return 0


def handle_list_modes(_args: argparse.Namespace) -> int:
    """Print source-defined operating modes without opening the tester."""

    print("Operating modes from EBC-controller ebcc.init:")
    for mode in SOURCE_OPERATING_MODES:
        print(_format_operating_mode(mode))
    print("No connection opened; no command was sent to the tester.")
    return 0


def _format_operating_mode(mode: EbcOperatingMode) -> str:
    """Render one source mode while keeping its control status explicit."""

    operations = []
    if mode.start_opcode is not None:
        operations.append(f"start=0x{mode.start_opcode:02X}")
    if mode.adjust_opcode is not None:
        operations.append(f"adjust=0x{mode.adjust_opcode:02X}")
    if mode.cont_opcode is not None:
        operations.append(f"continue=0x{mode.cont_opcode:02X}")
    if mode.auto_off_code is not None:
        operations.append(f"auto-off=0x{mode.auto_off_code:02X}")
    if mode.test_value is not None:
        operations.append(f"value={_test_value_label(mode.test_value)}")
    if mode.cell_voltage_volts is not None:
        operations.append(f"cell={mode.cell_voltage_volts:g} V")
    details = ", ".join(operations) or "no wire frame (program control)"
    return (
        f"[{mode.section:02d}] {mode.command:<7} {mode.name} "
        f"[{mode.mode_type.value}; {details}; {mode.control_summary}]"
    )


def _test_value_label(test_value: str) -> str:
    return {"I": "current", "P": "power", "R": "resistance"}[test_value]


def handle_plan_mode(args: argparse.Namespace) -> int:
    """Build one source-defined mode packet without opening the tester."""

    command = _mode_command_from_args(args, phase=EbcModePhase(args.phase), require_observed=True)
    packet = build_mode_packet(command)

    print("PLAN ONLY — no command was sent to the tester")
    print(f"Mode: {command.mode.command} ({command.mode.name}), phase={command.phase.value}")
    print(f"Frame: {packet.hex().upper()}")
    return 0


def handle_run_mode(args: argparse.Namespace) -> int:
    """Run one source-defined tester mode under the host safety lifecycle."""

    _require_supervised_execution(args, f"{args.mode} mode")
    command = _mode_command_from_args(args, phase=EbcModePhase.START, require_observed=False)
    command.validate_live(A10H_CAPABILITIES)
    return _run_mode(args, command)


def _mode_command_from_args(
    args: argparse.Namespace,
    *,
    phase: EbcModePhase,
    require_observed: bool,
) -> EbcModeCommand:
    mode = get_operating_mode(args.mode)
    if mode is None:
        raise ProtocolError(f"unknown source operating mode: {args.mode!r}")
    if mode.kind is EbcModeKind.PROGRAM_CONTROL:
        raise ProtocolError(f"{mode.command} is host program control and has no serial packet")
    accepted = {
        EbcModeKind.CELL_CHARGE: {"current", "cells"},
        EbcModeKind.CCCV_CHARGE: {"current", "voltage"},
        EbcModeKind.CONSTANT_CURRENT_DISCHARGE: {"current", "cutoff_voltage"},
        EbcModeKind.CONSTANT_POWER_DISCHARGE: {"power", "cutoff_voltage"},
        EbcModeKind.SOFTWARE_CONSTANT_RESISTANCE: {
            "resistance",
            "cutoff_voltage",
            "observed_voltage",
        },
    }[mode.kind]
    for name in (
        "current",
        "cells",
        "voltage",
        "cutoff_voltage",
        "power",
        "resistance",
        "observed_voltage",
    ):
        if getattr(args, name, None) is not None and name not in accepted:
            raise ProtocolError(f"{mode.command} does not accept --{name.replace('_', '-')}")
    observed_voltage = getattr(args, "observed_voltage", None)
    if require_observed and mode.kind is EbcModeKind.SOFTWARE_CONSTANT_RESISTANCE:
        observed_voltage = _required_mode_arg(
            args.observed_voltage, "--observed-voltage", mode.command
        )
    return EbcModeCommand(
        mode=mode,
        timeout_minutes=args.timeout_minutes,
        current_amps=args.current,
        cells=args.cells,
        voltage_volts=args.voltage,
        cutoff_volts=args.cutoff_voltage,
        power_watts=args.power,
        resistance_ohms=args.resistance,
        observed_voltage_volts=observed_voltage,
        phase=phase,
    )


def _run_mode(args: argparse.Namespace, command: EbcModeCommand) -> int:
    mode = command.mode
    with (
        _open_resources(args) as resources,
        SupervisedSession(resources.tester, resources.logger) as session,
    ):
        baseline = resources.tester.begin_mode(command)
        session.mark_started(f"{mode.command}-started")
        if resources.logger is not None:
            resources.logger.write(baseline, event=f"{mode.command}-baseline")
        print(
            f"{mode.command} started: {mode.name}, timeout {command.timeout_minutes} minutes",
            flush=True,
        )
        print("Baseline: ", end="")
        print_status(baseline)
        result = run_active_session(
            session=session,
            printer=print_status,
            timeout_minutes=command.timeout_minutes,
            sample_event=f"{mode.command}-sample",
            completion=_mode_completion(mode),
            on_sample=_mode_sample_action(resources.tester, command),
        )
        _report_session_result(result)
    _report_log(resources.log_path)
    return 0


def _mode_completion(mode: EbcOperatingMode):
    def completion(status: EbcStatus, samples: int) -> SessionStopReason | None:
        if mode.auto_off_code is not None and status.state_byte == mode.auto_off_code:
            return SessionStopReason.MODE_AUTO_OFF
        if samples > 3 and status.current_amps < 0.0001:
            return SessionStopReason.ZERO_CURRENT
        return None

    return completion


def _mode_sample_action(tester, command: EbcModeCommand):
    if command.mode.kind is not EbcModeKind.SOFTWARE_CONSTANT_RESISTANCE:
        return None

    def adjust(status: EbcStatus, samples: int) -> None:
        if samples % 3 == 0:
            tester.adjust_mode(
                command.with_observed_voltage(status.voltage_volts, phase=EbcModePhase.ADJUST)
            )

    return adjust


def _required_mode_arg(value: object, flag: str, mode_command: str):
    if value is None:
        raise ProtocolError(f"{mode_command} requires {flag}")
    return value


def _report_log(path: Path | None) -> None:
    if path is not None:
        print(f"Log: {path}")


def print_status(status: EbcStatus) -> None:
    """Render one status line for the terminal."""

    state = "unknown" if status.state_byte is None else f"0x{status.state_byte:02X}"
    print(
        f"{status.model_name}: {status.voltage_volts:.3f} V  "
        f"{status.current_amps:.3f} A  {status.power_watts:.3f} W  "
        f"{status.capacity_ah:.3f} Ah  state {state}  "
        f"device timer {status.device_timer_minutes} min",
        flush=True,
    )


def _print_float_start(baseline: EbcStatus) -> None:
    print("PbAc bulk auto-off reached; starting CCCV float hold.", flush=True)
    print("Float baseline: ", end="")
    print_status(baseline)
