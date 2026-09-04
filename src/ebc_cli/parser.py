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

    subcommands.add_parser(
        "modes",
        aliases=("list-modes",),
        help="list operating modes imported from the EBC-controller source; never connects",
    )

    mode_plan = subcommands.add_parser(
        "plan-mode",
        help="build a source-defined mode packet without connecting or transmitting",
    )
    mode_plan.add_argument("mode", help="source mode command, such as c_NiMH or d_CP")
    mode_plan.add_argument("--current", type=float, help="current setpoint in amps")
    mode_plan.add_argument("--cells", type=int, help="series cell count for chemistry charge modes")
    mode_plan.add_argument("--voltage", type=float, help="CCCV target voltage in volts")
    mode_plan.add_argument("--cutoff-voltage", type=float, help="discharge cutoff voltage in volts")
    mode_plan.add_argument("--power", type=float, help="constant-power setpoint in watts")
    mode_plan.add_argument("--resistance", type=float, help="software-CR resistance in ohms")
    mode_plan.add_argument(
        "--observed-voltage",
        type=float,
        help="latest measured voltage for software-CR current calculation",
    )
    mode_plan.add_argument(
        "--timeout-minutes", type=int, default=60, help="device timer value (default: 60)"
    )
    mode_plan.add_argument(
        "--phase",
        choices=("start", "cont", "adjust"),
        default="start",
        help="source packet phase (default: start)",
    )

    report = subcommands.add_parser(
        "report",
        help="summarize a recorded CSV session without connecting to the tester",
    )
    report.add_argument("path", type=Path, help="CSV session log to replay")

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
        help="recovery current in amps (0.05 to 0.20; default: 0.20)",
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
        help="current limit in amps (0.05 to 2.00; default: 0.50)",
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

    bulk_to_float = subcommands.add_parser(
        "bulk-to-float",
        help="run a supervised PbAc bulk charge followed by a bounded CCCV float hold",
    )
    _add_device_options(bulk_to_float)
    bulk_to_float.add_argument(
        "--bulk-current",
        type=float,
        default=0.80,
        help="PbAc bulk current in amps (0.05 to 2.00; default: 0.80)",
    )
    bulk_to_float.add_argument(
        "--bulk-timeout-minutes",
        type=int,
        default=1440,
        help="bulk hard timeout in minutes (1 to 1440; default: 1440)",
    )
    bulk_to_float.add_argument(
        "--float-current",
        type=float,
        default=0.50,
        help="CCCV float current limit in amps (0.05 to 2.00; default: 0.50)",
    )
    bulk_to_float.add_argument(
        "--float-voltage",
        type=float,
        default=13.7,
        help="CCCV float voltage in volts (13.5 to 13.8; default: 13.7)",
    )
    bulk_to_float.add_argument(
        "--float-timeout-minutes",
        type=int,
        default=240,
        help="float-hold hard timeout in minutes (1 to 240; default: 240)",
    )
    bulk_to_float.add_argument("--execute", action="store_true", help="required to transmit")
    bulk_to_float.add_argument(
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
