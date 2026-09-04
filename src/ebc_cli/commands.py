"""Command handlers for the ebc CLI."""

import argparse
import time
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path

from .csvlog import StatusCsvLogger
from .device import DeviceError, EbcDevice
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
    if not args.execute or not args.i_am_present:
        raise DeviceError("recovery requires both --execute and --i-am-present")
    if not 0.01 <= args.current <= 0.20:
        raise DeviceError("recovery current must be between 0.01 and 0.20 A")
    if not 1 <= args.timeout_minutes <= 30:
        raise DeviceError("recovery timeout must be between 1 and 30 minutes")
    log_context, log_path = _command_log_context(args)
    with (
        EbcDevice(args.device, args.timeout) as tester,
        log_context as logger,
        SupervisedSession(tester, logger) as session,
    ):
        baseline = tester.begin_lead_acid_recovery(
            current_amps=args.current, timeout_minutes=args.timeout_minutes
        )
        if logger is not None:
            logger.write(baseline, event="recovery-baseline")
        session.mark_started("recovery-started")
        print(
            "Recovery started: PbAc, 6 cells, 14.4 V target, "
            f"{args.current:.2f} A, {args.timeout_minutes}-minute hard timeout"
        )
        print("Baseline: ", end="")
        print_status(baseline)
        run_active_session(
            session=session,
            tester=tester,
            logger=logger,
            printer=print_status,
            timeout_minutes=args.timeout_minutes,
            sample_event="recovery-sample",
        )
    _report_log(log_path)
    return 0


def handle_float_charge(args: argparse.Namespace) -> int:
    if not args.execute or not args.i_am_present:
        raise DeviceError("float charge requires both --execute and --i-am-present")
    if not 0.01 <= args.current <= 2.00:
        raise DeviceError("float-charge current must be between 0.01 and 2.00 A")
    if not 13.5 <= args.voltage <= 13.8:
        raise DeviceError("float-charge voltage must be between 13.5 and 13.8 V")
    if not 1 <= args.timeout_minutes <= 240:
        raise DeviceError("float-charge timeout must be between 1 and 240 minutes")
    log_context, log_path = _command_log_context(args)
    with (
        EbcDevice(args.device, args.timeout) as tester,
        log_context as logger,
        SupervisedSession(tester, logger) as session,
    ):
        baseline = tester.begin_float_charge(
            current_amps=args.current,
            voltage_volts=args.voltage,
            timeout_minutes=args.timeout_minutes,
        )
        if logger is not None:
            logger.write(baseline, event="float-charge-baseline")
        session.mark_started("float-charge-started")
        print(
            f"Float charge started: {args.voltage:.1f} V, {args.current:.2f} A limit, "
            f"{args.timeout_minutes}-minute hard timeout"
        )
        print("Baseline: ", end="")
        print_status(baseline)
        run_active_session(
            session=session,
            tester=tester,
            logger=logger,
            printer=print_status,
            timeout_minutes=args.timeout_minutes,
            sample_event="float-charge-sample",
        )
    _report_log(log_path)
    return 0


def handle_capacity_test(args: argparse.Namespace) -> int:
    if not args.execute or not args.i_am_present:
        raise DeviceError("capacity test requires both --execute and --i-am-present")
    if not 0.1 <= args.current <= 5.0:
        raise DeviceError("capacity-test current must be between 0.1 and 5.0 A")
    if not 10.2 <= args.cutoff_voltage <= 11.5:
        raise DeviceError("capacity-test cutoff must be between 10.2 and 11.5 V")
    if not 1 <= args.timeout_minutes <= 2880:
        raise DeviceError("capacity-test timeout must be between 1 and 2880 minutes")
    log_context, log_path = _command_log_context(args)
    with (
        EbcDevice(args.device, args.timeout) as tester,
        log_context as logger,
        SupervisedSession(tester, logger) as session,
    ):
        baseline = tester.begin_constant_current_discharge(
            current_amps=args.current,
            cutoff_volts=args.cutoff_voltage,
            timeout_minutes=args.timeout_minutes,
        )
        if logger is not None:
            logger.write(baseline, event="capacity-test-baseline")
        session.mark_started("capacity-test-started")
        print(
            f"Capacity test started: {args.current:.2f} A to {args.cutoff_voltage:.1f} V, "
            f"{args.timeout_minutes}-minute hard timeout",
            flush=True,
        )
        _report_log(log_path)
        run_active_session(
            session=session,
            tester=tester,
            logger=logger,
            printer=print_status,
            timeout_minutes=args.timeout_minutes,
            sample_event="capacity-sample",
            stop_on_cutoff=True,
        )
    return 0


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
