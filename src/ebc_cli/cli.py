"""Process entry point for the ebc command."""

import sys

from .commands import execute_command
from .device import DeviceError
from .parser import build_parser
from .protocol import ProtocolError
from .session import _SessionSignal


def main(argv: list[str] | None = None) -> int:
    """Parse one command, execute it, and translate expected failures."""

    args = build_parser().parse_args(argv)
    try:
        return execute_command(args)
    except _SessionSignal as exc:
        return 128 + exc.signum
    except (DeviceError, ProtocolError) as exc:
        print(f"ebc: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
