"""Command handlers for the ebc CLI."""

import argparse
from pathlib import Path

from .device import DeviceError
from .profiles import (
    TesterProfile,
    make_capacity_test_profile,
    make_float_charge_profile,
    make_recovery_profile,
)
from .protocol import EbcStatus, make_lead_acid_charge_plan
from .resources import command_resources
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
        "plan-lead-acid-charge": handle_plan_lead_acid_charge,
        "recover-lead-acid": handle_recover_lead_acid,
        "float-charge": handle_float_charge,
        "capacity-test": handle_capacity_test,
        "stop": handle_stop,
        "status": handle_status,
        "monitor": handle_monitor,
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
    if result.stop_reason is SessionStopReason.TIMEOUT:
        print("Hard timeout reached; stop frame sent.", flush=True)
    else:
        print("Device cutoff reached; stop frame sent.", flush=True)
    print(
        f"Session: {result.elapsed_seconds:.1f} s, {result.samples} active sample(s)",
        flush=True,
    )
    if result.final_status is not None:
        print("Terminal: ", end="")
        print_status(result.final_status)


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
