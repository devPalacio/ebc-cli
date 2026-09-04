"""Command handlers for the ebc CLI."""

import argparse
import time
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path

from .csvlog import StatusCsvLogger
from .device import DeviceError, EbcDevice
from .profiles import (
    TesterProfile,
    make_capacity_test_profile,
    make_float_charge_profile,
    make_recovery_profile,
)
from .protocol import EbcStatus, make_lead_acid_charge_plan
from .session import SupervisedSession, run_active_session


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
    log_context, log_path = _command_log_context(args)
    with (
        EbcDevice(args.device, args.timeout) as tester,
        log_context as logger,
        SupervisedSession(tester, logger) as session,
    ):
        baseline = profile.start(tester)
        if logger is not None:
            logger.write(baseline, event=f"{profile.kind.value}-baseline")
        session.mark_started(profile.start_event)
        print(profile.summary, flush=profile.kind.value == "capacity-test")
        if show_baseline:
            print("Baseline: ", end="")
            print_status(baseline)
        if report_log_before_sampling:
            _report_log(log_path)
        run_active_session(
            session=session,
            tester=tester,
            logger=logger,
            printer=print_status,
            timeout_minutes=profile.timeout_minutes,
            sample_event=profile.sample_event,
            stop_on_cutoff=profile.stop_on_cutoff,
        )
    if not report_log_before_sampling:
        _report_log(log_path)
    return 0


def _require_supervised_execution(args: argparse.Namespace, operation: str) -> None:
    if not args.execute or not args.i_am_present:
        raise DeviceError(f"{operation} requires both --execute and --i-am-present")


def handle_stop(args: argparse.Namespace) -> int:
    if not args.execute:
        raise DeviceError("stop requires --execute")
    log_context, log_path = _command_log_context(args)
    with EbcDevice(args.device, args.timeout) as tester, log_context as logger:
        tester.stop()
        if logger is not None:
            logger.write_event("stop-sent")
    print("Stop frame sent.")
    _report_log(log_path)
    return 0


def handle_status(args: argparse.Namespace) -> int:
    log_context, log_path = _command_log_context(args)
    with EbcDevice(args.device, args.timeout) as tester, log_context as logger:
        status = tester.status()
        print_status(status)
        if logger is not None:
            logger.write(status)
    _report_log(log_path)
    return 0


def handle_monitor(args: argparse.Namespace) -> int:
    if args.interval <= 0:
        raise DeviceError("interval must be greater than zero")
    if args.count is not None and args.count <= 0:
        raise DeviceError("count must be greater than zero")
    log_context, log_path = _command_log_context(args)
    with EbcDevice(args.device, args.timeout) as tester, log_context as logger:
        samples = 0
        while True:
            status = tester.status()
            print_status(status)
            if logger is not None:
                logger.write(status)
            samples += 1
            if args.count is not None and samples >= args.count:
                break
            time.sleep(args.interval)
    print(f"Logged {samples} sample(s)")
    _report_log(log_path)
    return 0


def _command_log_context(args: argparse.Namespace):
    if args.no_log:
        return nullcontext(None), None
    filename = f"{datetime.now(tz=UTC).astimezone().strftime('%Y%m%d-%H%M%S')}_{args.command}.csv"
    path = args.log_dir.expanduser() / filename
    return StatusCsvLogger(path), path


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
