"""Command-line interface for the cautious first EBC CLI release."""

from __future__ import annotations

import argparse
import sys
import time
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path

from .csvlog import StatusCsvLogger
from .device import DeviceError, EbcDevice
from .protocol import EbcStatus, ProtocolError, make_lead_acid_charge_plan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ebc",
        description="Read status from a ZKETECH EBC tester and inspect charge plans.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("status", "read one status frame; never changes tester mode"),
        ("monitor", "stream status frames until interrupted; never changes tester mode"),
    ):
        command = subcommands.add_parser(name, help=help_text)
        command.add_argument("--device", default="/dev/ttyUSB0", help="serial device path")
        command.add_argument("--timeout", type=float, default=1.0, help="response timeout in seconds")
        _add_log_options(command)
        if name == "monitor":
            command.add_argument("--interval", type=float, default=1.0, help="seconds between samples")
            command.add_argument(
                "--count", type=int, help="stop after this many samples (default: run until interrupted)"
            )

    plan = subcommands.add_parser(
        "plan-lead-acid-charge",
        help="print a PbAc packet only; this command never transmits it",
    )
    plan.add_argument("--current", required=True, type=float, help="planned current in amps")
    plan.add_argument("--cells", type=int, default=6, help="series cell count (12 V SLA = 6)")
    plan.add_argument(
        "--timeout-minutes", type=int, default=720, help="planned safety timeout in minutes"
    )

    recovery = subcommands.add_parser(
        "recover-lead-acid",
        help="start a bounded, supervised PbAc recovery profile",
    )
    _add_device_options(recovery)
    recovery.add_argument(
        "--current",
        type=float,
        default=0.20,
        help="recovery current in amps (0.01 to 0.20; default: 0.20)",
    )
    recovery.add_argument(
        "--timeout-minutes",
        type=int,
        default=30,
        help="hard timeout in minutes (1 to 30; default: 30)",
    )
    recovery.add_argument(
        "--execute",
        action="store_true",
        help="required: allow this command to transmit the recovery start frame",
    )
    recovery.add_argument(
        "--i-am-present",
        action="store_true",
        help="required: confirm you will monitor the battery for the full attempt",
    )

    float_charge = subcommands.add_parser(
        "float-charge",
        help="start a bounded CCCV float-charge profile for a 12 V lead-acid battery",
    )
    _add_device_options(float_charge)
    float_charge.add_argument(
        "--current",
        type=float,
        default=0.50,
        help="current limit in amps (0.01 to 2.00; default: 0.50)",
    )
    float_charge.add_argument(
        "--voltage",
        type=float,
        default=13.7,
        help="float voltage in volts (13.5 to 13.8; default: 13.7)",
    )
    float_charge.add_argument(
        "--timeout-minutes",
        type=int,
        default=120,
        help="hard timeout in minutes (1 to 240; default: 120)",
    )
    float_charge.add_argument("--execute", action="store_true", help="required to transmit")
    float_charge.add_argument(
        "--i-am-present", action="store_true", help="required: confirm this is supervised"
    )

    capacity = subcommands.add_parser(
        "capacity-test",
        help="run and automatically log a bounded 12 V lead-acid CC capacity discharge",
    )
    _add_device_options(capacity)
    capacity.add_argument(
        "--current", type=float, default=0.8, help="discharge current in amps (default: 0.8)"
    )
    capacity.add_argument(
        "--cutoff-voltage", type=float, default=10.5, help="cutoff voltage in volts (default: 10.5)"
    )
    capacity.add_argument(
        "--timeout-minutes", type=int, default=1440, help="hard timeout (default: 1440)"
    )
    capacity.add_argument("--execute", action="store_true", help="required to transmit")
    capacity.add_argument(
        "--i-am-present", action="store_true", help="required: confirm this is supervised"
    )

    stop = subcommands.add_parser("stop", help="send an immediate tester stop frame")
    _add_device_options(stop)
    stop.add_argument("--execute", action="store_true", help="required to transmit the stop frame")
    return parser


def _add_device_options(command: argparse.ArgumentParser) -> None:
    command.add_argument("--device", default="/dev/ttyUSB0", help="serial device path")
    command.add_argument("--timeout", type=float, default=1.0, help="response timeout in seconds")
    _add_log_options(command)


def _add_log_options(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--log-dir",
        type=Path,
        default=Path.home() / "ebc-logs",
        help="directory for the automatic session CSV (default: ~/ebc-logs)",
    )
    command.add_argument("--no-log", action="store_true", help="do not create a session CSV")


def _command_log_context(args: argparse.Namespace):
    if args.no_log:
        return nullcontext(None), None
    filename = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_{args.command}.csv"
    path = args.log_dir.expanduser() / filename
    return StatusCsvLogger(path), path


def _report_log(path: Path | None) -> None:
    if path is not None:
        print(f"Log: {path}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "plan-lead-acid-charge":
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

        if args.command == "recover-lead-acid":
            if not args.execute or not args.i_am_present:
                raise DeviceError("recovery requires both --execute and --i-am-present")
            if not 0.01 <= args.current <= 0.20:
                raise DeviceError("recovery current must be between 0.01 and 0.20 A")
            if not 1 <= args.timeout_minutes <= 30:
                raise DeviceError("recovery timeout must be between 1 and 30 minutes")
            log_context, log_path = _command_log_context(args)
            with EbcDevice(args.device, args.timeout) as tester, log_context as logger:
                baseline = tester.begin_lead_acid_recovery(
                    current_amps=args.current, timeout_minutes=args.timeout_minutes
                )
                if logger is not None:
                    logger.write(baseline, event="recovery-baseline")
            print(
                "Recovery started: PbAc, 6 cells, 14.4 V target, "
                f"{args.current:.2f} A, {args.timeout_minutes}-minute hard timeout"
            )
            print("Baseline: ", end="")
            _print_status(baseline)
            _report_log(log_path)
            return 0

        if args.command == "float-charge":
            if not args.execute or not args.i_am_present:
                raise DeviceError("float charge requires both --execute and --i-am-present")
            if not 0.01 <= args.current <= 2.00:
                raise DeviceError("float-charge current must be between 0.01 and 2.00 A")
            if not 13.5 <= args.voltage <= 13.8:
                raise DeviceError("float-charge voltage must be between 13.5 and 13.8 V")
            if not 1 <= args.timeout_minutes <= 240:
                raise DeviceError("float-charge timeout must be between 1 and 240 minutes")
            log_context, log_path = _command_log_context(args)
            with EbcDevice(args.device, args.timeout) as tester, log_context as logger:
                baseline = tester.begin_float_charge(
                    current_amps=args.current,
                    voltage_volts=args.voltage,
                    timeout_minutes=args.timeout_minutes,
                )
                if logger is not None:
                    logger.write(baseline, event="float-charge-baseline")
            print(
                f"Float charge started: {args.voltage:.1f} V, {args.current:.2f} A limit, "
                f"{args.timeout_minutes}-minute hard timeout"
            )
            print("Baseline: ", end="")
            _print_status(baseline)
            _report_log(log_path)
            return 0

        if args.command == "capacity-test":
            if not args.execute or not args.i_am_present:
                raise DeviceError("capacity test requires both --execute and --i-am-present")
            if not 0.1 <= args.current <= 5.0:
                raise DeviceError("capacity-test current must be between 0.1 and 5.0 A")
            if not 10.2 <= args.cutoff_voltage <= 11.5:
                raise DeviceError("capacity-test cutoff must be between 10.2 and 11.5 V")
            if not 1 <= args.timeout_minutes <= 2880:
                raise DeviceError("capacity-test timeout must be between 1 and 2880 minutes")
            log_context, log_path = _command_log_context(args)
            with EbcDevice(args.device, args.timeout) as tester, log_context as logger:
                baseline = tester.begin_constant_current_discharge(
                    current_amps=args.current,
                    cutoff_volts=args.cutoff_voltage,
                    timeout_minutes=args.timeout_minutes,
                )
                if logger is not None:
                    logger.write(baseline, event="capacity-test-baseline")
                print(
                    f"Capacity test started: {args.current:.2f} A to {args.cutoff_voltage:.1f} V, "
                    f"{args.timeout_minutes}-minute hard timeout",
                    flush=True,
                )
                _report_log(log_path)
                try:
                    while True:
                        try:
                            status = tester.next_status()
                        except DeviceError:
                            status = tester.status()
                        _print_status(status)
                        if logger is not None:
                            logger.write(status, event="capacity-sample")
                        # The bundled controller maps a CC discharge auto-off to 0x14.
                        if status.raw[1] == 0x14:
                            tester.stop()
                            if logger is not None:
                                logger.write_event("device-cutoff-stop-sent")
                            print("Device cutoff reached; stop frame sent.", flush=True)
                            return 0
                except KeyboardInterrupt:
                    tester.stop()
                    if logger is not None:
                        logger.write_event("operator-interrupt-stop-sent")
                    raise

        if args.command == "stop":
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

        if args.command == "status":
            log_context, log_path = _command_log_context(args)
            with EbcDevice(args.device, args.timeout) as tester, log_context as logger:
                status = tester.status()
                _print_status(status)
                if logger is not None:
                    logger.write(status)
            _report_log(log_path)
            return 0

        if args.command == "monitor":
            if args.interval <= 0:
                raise DeviceError("interval must be greater than zero")
            if args.count is not None and args.count <= 0:
                raise DeviceError("count must be greater than zero")
            log_context, log_path = _command_log_context(args)
            with EbcDevice(args.device, args.timeout) as tester, log_context as logger:
                samples = 0
                while True:
                    status = tester.status()
                    _print_status(status)
                    if logger is not None:
                        logger.write(status)
                    samples += 1
                    if args.count is not None and samples >= args.count:
                        break
                    time.sleep(args.interval)
            print(f"Logged {samples} sample(s)")
            _report_log(log_path)
            return 0
    except (DeviceError, ProtocolError) as exc:
        print(f"ebc: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


def _print_status(status: EbcStatus) -> None:
    # Kept separate from serial code so a later JSON/CSV formatter has a small seam.
    print(
        f"{status.model_name}: {status.voltage_volts:.3f} V  "
        f"{status.current_amps:.3f} A  {status.power_watts:.3f} W  "
        f"{status.capacity_ah:.3f} Ah  {status.elapsed_minutes} min"
        ,
        flush=True,
    )
