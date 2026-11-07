# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Jay (Python CLI contributions)
# See NOTICE for upstream attribution and port details.

"""Owned device and log resources for one CLI invocation."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .csvlog import StatusCsvLogger
from .device import EbcDevice


@dataclass(frozen=True, slots=True)
class CommandResources:
    """Resources whose lifetime is bounded by one command invocation."""

    tester: EbcDevice
    logger: StatusCsvLogger | None
    log_path: Path | None


@contextmanager
def command_resources(
    *,
    device: str | Path,
    timeout: float,
    command: str,
    log_dir: str | Path,
    no_log: bool,
    lock_device: bool = True,
) -> Iterator[CommandResources]:
    """Open the tester and optional flushed CSV logger as one owned scope.

    Normal commands take an advisory lock through :class:`EbcDevice`. The
    explicit ``lock_device=False`` path is reserved for the emergency STOP
    command, which must remain usable when another CLI process owns the port.
    """

    log_path = None if no_log else _log_path(command, log_dir)
    with EbcDevice(device, timeout, lock_device=lock_device) as tester:
        if log_path is None:
            yield CommandResources(tester=tester, logger=None, log_path=None)
            return
        with StatusCsvLogger(log_path) as logger:
            yield CommandResources(tester=tester, logger=logger, log_path=log_path)


def _log_path(command: str, log_dir: str | Path) -> Path:
    filename = f"{datetime.now(tz=UTC).astimezone().strftime('%Y%m%d-%H%M%S')}_{command}.csv"
    return Path(log_dir).expanduser() / filename
