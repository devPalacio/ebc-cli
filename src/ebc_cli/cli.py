# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Jay (Python CLI contributions)
# See NOTICE for upstream attribution and port details.

"""Process entry point for the ebc command."""

import sys

from .commands import execute_command
from .device import DeviceError
from .parser import build_parser
from .profiles import ProfileError
from .protocol import ProtocolError
from .report import ReportError
from .session import _SessionSignal


def main(argv: list[str] | None = None) -> int:
    """Parse one command, execute it, and translate expected failures."""

    args = build_parser().parse_args(argv)
    try:
        return execute_command(args)
    except _SessionSignal as exc:
        return 128 + exc.signum
    except (DeviceError, ProfileError, ProtocolError, ReportError) as exc:
        print(f"ebc: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
