"""Argument parser construction for the ebc command."""

import argparse
from pathlib import Path


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
        command.add_argument(
            "--timeout", type=float, default=1.0, help="response timeout in seconds"
        )
        _add_log_options(command)
        if name == "monitor":
            command.add_argument(
                "--interval", type=float, default=1.0, help="seconds between samples"
            )
            command.add_argument(
                "--count",
                type=int,
                help="stop after this many samples (default: run until interrupted)",
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
