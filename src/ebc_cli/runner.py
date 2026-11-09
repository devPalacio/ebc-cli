# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Jay (Python CLI contributions)
# See NOTICE for upstream attribution and port details.

"""Library entry points for running a validated profile without the CLI."""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from .capacity import CapacityPolicy, CapacityRequest, CutoffKind
from .device import DeviceError
from .modes import CCCV_CHARGE
from .profiles import BulkToFloatProfile, ProfileKind, TesterProfile
from .protocol import EbcCompletionReason, EbcStatus
from .session import (
    SessionResult,
    SessionStopReason,
    StatusLogger,
    SupervisedSession,
    Tester,
    _record_capacity_failure,
    run_active_session,
    run_bulk_to_float_session,
)

__all__ = ["BulkToFloatRun", "ProfileRun", "run_bulk_to_float", "run_profile"]

CAPACITY_DISCHARGE_START_TIMEOUT_SECONDS = 20.0


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
    """Start ``profile`` and supervise it until completion or timeout.

    The start frame goes out inside a ``SupervisedSession``, so every exit path
    that reaches Python attempts STOP. A caller that starts a mode directly on
    ``EbcDevice`` gets no such guarantee.

    ``on_sample`` receives each status frame as it arrives; it is optional so
    this function never writes to stdout on its own. Raises ``DeviceError`` or
    ``ProtocolError`` from the tester, with STOP already attempted.

    Every exceptional capacity exit attaches ``capacity_assessment``, a typed
    ``CapacityAssessment``, to the original exception without replacing it or
    its traceback. Retrieve it with ``getattr(exc, "capacity_assessment", None)``;
    no logger is required. Logging only adds a best-effort durable copy.
    """

    completion: Callable[[EbcStatus, int], SessionStopReason | None] | None = (
        _charge_completion(profile, clock) if profile.kind is ProfileKind.CHARGE else None
    )
    capacity_start_sample: Callable[[EbcStatus, int], None] | None = None
    capacity = None
    if profile.kind is ProfileKind.CAPACITY_TEST:
        assert profile.cutoff_volts is not None
        capacity = CapacityPolicy(
            CapacityRequest(profile.current_amps, profile.cutoff_volts, profile.timeout_minutes)
        )
    started_at = clock() if capacity is not None else None
    try:
        with SupervisedSession(tester=tester, logger=logger) as session:
            baseline = tester.begin_mode(profile.start_command)
            started_at = clock() if capacity is not None else None
            if capacity is not None:
                assert started_at is not None
                completion, capacity_start_sample = _capacity_start_watchdog(
                    capacity,
                    started_at,
                    clock,
                    stop_on_cutoff=profile.stop_on_cutoff,
                )
            session.mark_started(profile.start_event)
            if logger is not None:
                if capacity is not None:
                    logger.write_event(capacity.request.event)
                logger.write(baseline, event=f"{profile.kind.value}-baseline")
            if on_baseline is not None:
                on_baseline(baseline)
            result = run_active_session(
                session=session,
                printer=on_sample or _discard,
                timeout_minutes=profile.timeout_minutes,
                sample_event=profile.sample_event,
                stop_on_cutoff=profile.stop_on_cutoff,
                completion=completion,
                on_sample=capacity_start_sample,
                require_streamed=profile.kind is ProfileKind.CHARGE,
                capacity_policy=capacity,
                started_at=started_at,
                poll_interval=poll_interval,
                clock=clock,
                sleeper=sleeper,
            )
    except BaseException as exc:
        if capacity is not None and not hasattr(exc, "capacity_assessment"):
            assert started_at is not None
            _record_capacity_failure(exc, capacity, logger, clock() - started_at)
        raise
    return ProfileRun(baseline=baseline, result=result)


def _capacity_start_watchdog(
    policy: CapacityPolicy,
    started_at: float,
    clock: Callable[[], float],
    *,
    stop_on_cutoff: bool,
) -> tuple[Callable[[EbcStatus, int], SessionStopReason | None], Callable[[EbcStatus, int], None]]:
    observed_at: float | None = None
    failure: DeviceError | None = None

    def completion(status: EbcStatus, _samples: int) -> SessionStopReason | None:
        nonlocal observed_at, failure
        now = clock()
        if policy.discharge_observed and observed_at is None:
            observed_at = now
        observed_late = (
            observed_at is not None
            and observed_at - started_at > CAPACITY_DISCHARGE_START_TIMEOUT_SECONDS
        )
        no_discharge_by_deadline = (
            observed_at is None and now - started_at >= CAPACITY_DISCHARGE_START_TIMEOUT_SECONDS
        )
        if observed_late or no_discharge_by_deadline:
            failure = DeviceError(
                "capacity test has not observed positive current within "
                f"{CAPACITY_DISCHARGE_START_TIMEOUT_SECONDS:g} seconds of start"
            )
        elif stop_on_cutoff and status.completion_reason is EbcCompletionReason.CAPACITY_CUTOFF:
            return SessionStopReason.DEVICE_CUTOFF
        elif (
            stop_on_cutoff
            and policy.cutoff_evidence is not None
            and policy.cutoff_evidence.kind is CutoffKind.INFERRED
        ):
            return SessionStopReason.INFERRED_CUTOFF
        return None

    def raise_failure(_status: EbcStatus, _samples: int) -> None:
        if failure is not None:
            raise failure

    return completion, raise_failure


def _charge_completion(
    profile: TesterProfile, clock: Callable[[], float]
) -> Callable[[EbcStatus, int], SessionStopReason | None]:
    assert profile.end_current_amps is not None and profile.voltage_volts is not None
    end_current = profile.end_current_amps
    target_voltage = profile.voltage_volts
    # A10H active/status variants are 0x10 (a10h_live_cycles.csv) and 0x74
    # (also captured with positive current in upstream main.lfm's console).
    # 0x71/0x78 are command opcodes, not observed active status bytes.
    active_states = {0x10, 0x74}
    charging_seen = False
    below_since: float | None = None
    last_sample_at: float | None = None

    def completion(status: EbcStatus, _samples: int) -> SessionStopReason | None:
        nonlocal charging_seen, below_since, last_sample_at
        now = clock()
        if last_sample_at is not None and now - last_sample_at > profile.max_sample_gap_seconds:
            below_since = None
        last_sample_at = now

        if charging_seen and status.state_byte == CCCV_CHARGE.auto_off_code:
            return SessionStopReason.MODE_AUTO_OFF
        if (
            status.state_byte not in active_states
            or not math.isfinite(status.current_amps)
            or status.current_amps <= 0
        ):
            below_since = None
            return None

        charging_seen = True
        if (
            status.current_amps <= end_current
            and abs(status.voltage_volts - target_voltage) <= profile.voltage_tolerance_volts
        ):
            if below_since is None:
                below_since = now
            if now - below_since >= profile.end_current_dwell_seconds:
                return SessionStopReason.END_CURRENT
        else:
            below_since = None
        return None

    return completion


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
