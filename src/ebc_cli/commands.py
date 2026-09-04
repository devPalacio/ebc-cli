"""Command handlers for the ebc CLI."""

import argparse
from pathlib import Path

from .device import DeviceError
from .modes import (
    SOURCE_OPERATING_MODES,
    EbcModeKind,
    EbcModePhase,
    EbcOperatingMode,
    build_cccv_charge_packet,
    build_cell_charge_packet,
    build_constant_resistance_packet,
    build_discharge_packet,
    get_operating_mode,
)
from .profiles import (
    BulkToFloatProfile,
    TesterProfile,
    make_bulk_to_float_profile,
    make_capacity_test_profile,
    make_float_charge_profile,
    make_recovery_profile,
)
from .protocol import EbcStatus, ProtocolError, make_lead_acid_charge_plan
from .report import format_report, load_replay_log, summarize_replay_log
from .resources import command_resources
from .session import (
    SessionResult,
    SessionStopReason,
    SupervisedSession,
    run_active_session,
    run_bulk_to_float_session,
    run_monitor_session,
)


def execute_command(args: argparse.Namespace) -> int:
    """Execute one parsed command and return its process exit code."""

    handlers = {
        "plan-lead-acid-charge": handle_plan_lead_acid_charge,
        "recover-lead-acid": handle_recover_lead_acid,
        "float-charge": handle_float_charge,
        "bulk-to-float": handle_bulk_to_float,
        "capacity-test": handle_capacity_test,
        "stop": handle_stop,
        "status": handle_status,
        "monitor": handle_monitor,
        "modes": handle_list_modes,
        "list-modes": handle_list_modes,
        "plan-mode": handle_plan_mode,
        "report": handle_report,
    }
    return handlers[args.command](args)


def handle_plan_lead_acid_charge(args: argparse.Namespace) -> int:
    packet = make_lead_acid_charge_plan(
        current_amps=args.current,
        cells=args.cells,
        timeout_minutes=args.timeout_minutes,
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
        baseline = profile.start(resources.tester)
        if resources.logger is not None:
            resources.logger.write(baseline, event=f"{profile.kind.value}-baseline")
        with SupervisedSession(resources.tester, resources.logger) as session:
            session.mark_started(profile.start_event)
            print(profile.summary, flush=profile.kind.value == "capacity-test")
            if show_baseline:
                print("Baseline: ", end="")
                print_status(baseline)
            if report_log_before_sampling:
                _report_log(resources.log_path)
            result = run_active_session(
                session=session,
                tester=resources.tester,
                logger=resources.logger,
                printer=print_status,
                timeout_minutes=profile.timeout_minutes,
                sample_event=profile.sample_event,
                stop_on_cutoff=profile.stop_on_cutoff,
            )
            _report_session_result(result)
    if not report_log_before_sampling:
        _report_log(resources.log_path)
    return 0


def _run_bulk_to_float_profile(args: argparse.Namespace, profile: BulkToFloatProfile) -> int:
    with _open_resources(args) as resources:
        baseline = profile.start_bulk(resources.tester)
        if resources.logger is not None:
            resources.logger.write(baseline, event=profile.bulk_baseline_event)
        with SupervisedSession(resources.tester, resources.logger) as session:
            session.mark_started(profile.start_event)
            print(profile.summary, flush=True)
            print("Bulk baseline: ", end="")
            print_status(baseline)
            result = run_bulk_to_float_session(
                session=session,
                tester=resources.tester,
                logger=resources.logger,
                printer=print_status,
                profile=profile,
                on_float_start=_print_float_start,
            )
            _report_session_result(result)
    _report_log(resources.log_path)
    return 0


def _open_resources(args: argparse.Namespace, *, lock_device: bool = True):
    return command_resources(
        device=args.device,
        timeout=args.timeout,
        command=args.command,
        log_dir=args.log_dir,
        no_log=args.no_log,
        lock_device=lock_device,
    )


def _require_supervised_execution(args: argparse.Namespace, operation: str) -> None:
    if not args.execute or not args.i_am_present:
        raise DeviceError(f"{operation} requires both --execute and --i-am-present")


def _report_session_result(result: SessionResult) -> None:
    stop_messages = {
        SessionStopReason.TIMEOUT: "Hard timeout reached; stop frame sent.",
        SessionStopReason.BULK_TIMEOUT: (
            "Bulk phase timeout reached; stop frame sent; float phase was not started."
        ),
        SessionStopReason.FLOAT_TIMEOUT: "Float hold timeout reached; stop frame sent.",
        SessionStopReason.DEVICE_CUTOFF: "Device cutoff reached; stop frame sent.",
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
    with _open_resources(args, lock_device=False) as resources:
        resources.tester.stop()
        if resources.logger is not None:
            resources.logger.write_event("stop-sent")
    print("Stop frame sent.")
    _report_log(resources.log_path)
    return 0


def handle_status(args: argparse.Namespace) -> int:
    with _open_resources(args) as resources:
        status = resources.tester.status()
        print_status(status)
        if resources.logger is not None:
            resources.logger.write(status)
    _report_log(resources.log_path)
    return 0


def handle_monitor(args: argparse.Namespace) -> int:
    if args.interval <= 0:
        raise DeviceError("interval must be greater than zero")
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

    mode = get_operating_mode(args.mode)
    if mode is None:
        raise ProtocolError(f"unknown source operating mode: {args.mode!r}")
    if mode.kind is EbcModeKind.PROGRAM_CONTROL:
        raise ProtocolError(f"{mode.command} is host program control and has no serial packet")

    phase = EbcModePhase(args.phase)
    if mode.kind is EbcModeKind.CELL_CHARGE:
        packet = build_cell_charge_packet(
            mode,
            current_amps=_required_mode_arg(args.current, "--current", mode.command),
            cells=_required_mode_arg(args.cells, "--cells", mode.command),
            timeout_minutes=args.timeout_minutes,
            phase=phase,
        )
    elif mode.kind is EbcModeKind.CCCV_CHARGE:
        packet = build_cccv_charge_packet(
            mode,
            current_amps=_required_mode_arg(args.current, "--current", mode.command),
            voltage_volts=_required_mode_arg(args.voltage, "--voltage", mode.command),
            timeout_minutes=args.timeout_minutes,
            phase=phase,
        )
    elif mode.kind is EbcModeKind.SOFTWARE_CONSTANT_RESISTANCE:
        packet = build_constant_resistance_packet(
            mode,
            resistance_ohms=_required_mode_arg(args.resistance, "--resistance", mode.command),
            observed_voltage_volts=_required_mode_arg(
                args.observed_voltage, "--observed-voltage", mode.command
            ),
            cutoff_volts=_required_mode_arg(args.cutoff_voltage, "--cutoff-voltage", mode.command),
            timeout_minutes=args.timeout_minutes,
            phase=phase,
        )
    elif mode.kind is EbcModeKind.CONSTANT_POWER_DISCHARGE:
        packet = build_discharge_packet(
            mode,
            value=_required_mode_arg(args.power, "--power", mode.command),
            cutoff_volts=_required_mode_arg(args.cutoff_voltage, "--cutoff-voltage", mode.command),
            timeout_minutes=args.timeout_minutes,
            phase=phase,
        )
    elif mode.kind is EbcModeKind.CONSTANT_CURRENT_DISCHARGE:
        packet = build_discharge_packet(
            mode,
            value=_required_mode_arg(args.current, "--current", mode.command),
            cutoff_volts=_required_mode_arg(args.cutoff_voltage, "--cutoff-voltage", mode.command),
            timeout_minutes=args.timeout_minutes,
            phase=phase,
        )
    else:  # pragma: no cover - the enum is exhaustive after the control check
        raise ProtocolError(f"mode {mode.command} has no plan builder")

    print("PLAN ONLY — no command was sent to the tester")
    print(f"Mode: {mode.command} ({mode.name}), phase={phase.value}")
    print(f"Frame: {packet.hex().upper()}")
    return 0


def _required_mode_arg(value: object, flag: str, mode_command: str):
    if value is None:
        raise ProtocolError(f"{mode_command} requires {flag}")
    return value


def _report_log(path: Path | None) -> None:
    if path is not None:
        print(f"Log: {path}")


def print_status(status: EbcStatus) -> None:
    """Render one status line for the terminal."""

    print(
        f"{status.model_name}: {status.voltage_volts:.3f} V  "
        f"{status.current_amps:.3f} A  {status.power_watts:.3f} W  "
        f"{status.capacity_ah:.3f} Ah  device timer {status.device_timer_minutes} min",
        flush=True,
    )


def _print_float_start(baseline: EbcStatus) -> None:
    print("PbAc bulk auto-off reached; starting CCCV float hold.", flush=True)
    print("Float baseline: ", end="")
    print_status(baseline)
