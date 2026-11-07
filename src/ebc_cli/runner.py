# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Jay (Python CLI contributions)
# See NOTICE for upstream attribution and port details.

"""Library entry points for running a validated profile without the CLI."""

import time
from collections.abc import Callable
from dataclasses import dataclass

from .profiles import BulkToFloatProfile, TesterProfile
from .protocol import EbcStatus
from .session import (
    SessionResult,
    StatusLogger,
    SupervisedSession,
    Tester,
    run_active_session,
    run_bulk_to_float_session,
)

__all__ = ["BulkToFloatRun", "ProfileRun", "run_bulk_to_float", "run_profile"]


@dataclass(frozen=True, slots=True)
class ProfileRun:
    """The start baseline and the supervised result of one profile run."""

    baseline: EbcStatus
    result: SessionResult


@dataclass(frozen=True, slots=True)
class BulkToFloatRun(ProfileRun):
    """A bulk-to-float run; ``baseline`` is the bulk phase's start sample."""


def run_profile(
    tester: Tester,
    profile: TesterProfile,
    *,
    logger: StatusLogger | None = None,
    on_baseline: Callable[[EbcStatus], None] | None = None,
    on_sample: Callable[[EbcStatus], None] | None = None,
    poll_interval: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> ProfileRun:
    """Start ``profile`` and supervise it until cutoff or timeout.

    The start frame goes out inside a ``SupervisedSession``, so every exit path
    that reaches Python attempts STOP. A caller that starts a mode directly on
    ``EbcDevice`` gets no such guarantee.

    ``on_sample`` receives each status frame as it arrives; it is optional so
    this function never writes to stdout on its own. Raises ``DeviceError`` or
    ``ProtocolError`` from the tester, with STOP already attempted.
    """

    with SupervisedSession(tester=tester, logger=logger) as session:
        baseline = tester.begin_mode(profile.start_command)
        session.mark_started(profile.start_event)
        if logger is not None:
            logger.write(baseline, event=f"{profile.kind.value}-baseline")
        if on_baseline is not None:
            on_baseline(baseline)
        result = run_active_session(
            session=session,
            printer=on_sample or _discard,
            timeout_minutes=profile.timeout_minutes,
            sample_event=profile.sample_event,
            stop_on_cutoff=profile.stop_on_cutoff,
            poll_interval=poll_interval,
            clock=clock,
            sleeper=sleeper,
        )
    return ProfileRun(baseline=baseline, result=result)


def run_bulk_to_float(
    tester: Tester,
    profile: BulkToFloatProfile,
    *,
    logger: StatusLogger | None = None,
    on_baseline: Callable[[EbcStatus], None] | None = None,
    on_sample: Callable[[EbcStatus], None] | None = None,
    on_float_start: Callable[[EbcStatus], None] | None = None,
    poll_interval: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> BulkToFloatRun:
    """Run the PbAc bulk phase, then the bounded float hold.

    The float phase starts only after observed positive bulk current followed by
    the source-defined auto-off state, before the bulk deadline. If that state
    never arrives, the tester is stopped and no float start frame is sent.
    """

    with SupervisedSession(tester=tester, logger=logger) as session:
        baseline = tester.begin_mode(profile.bulk_start_command)
        session.mark_started(profile.start_event)
        if logger is not None:
            logger.write(baseline, event=profile.bulk_baseline_event)
        if on_baseline is not None:
            on_baseline(baseline)
        result = run_bulk_to_float_session(
            session=session,
            printer=on_sample or _discard,
            profile=profile,
            on_float_start=on_float_start,
            poll_interval=poll_interval,
            clock=clock,
            sleeper=sleeper,
        )
    return BulkToFloatRun(baseline=baseline, result=result)


def _discard(_status: EbcStatus) -> None:
    """Default sample sink: a library call must not print."""
