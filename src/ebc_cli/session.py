# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Jay (Python CLI contributions)
# See NOTICE for upstream attribution and port details.

"""Supervised tester-operation lifecycle and status sampling."""

import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, Self

from .capacity import CapacityAssessment, CapacityPolicy
from .device import DeviceError
from .measurement import MeasurementAccumulator, MeasurementSummary
from .modes import EbcModeCommand
from .profiles import BulkToFloatProfile
from .protocol import EbcCompletionReason, EbcStatus


class Tester(Protocol):
    """The device operations required by a supervised session."""

    def begin_mode(self, command: EbcModeCommand) -> EbcStatus: ...

    def adjust_mode(self, command: EbcModeCommand) -> None: ...

    def next_status(self, *, timeout: float | None = None, latest: bool = False) -> EbcStatus: ...

    def status(self, *, timeout: float | None = None, latest: bool = False) -> EbcStatus: ...

    def stop(self) -> None: ...


class StatusLogger(Protocol):
    """The logging operations needed by a supervised session."""

    def write(
        self,
        status: EbcStatus,
        *,
        event: str = "status",
        session_elapsed_seconds: float | None = None,
    ) -> None: ...

    def write_event(self, event: str) -> None: ...


class SessionStopReason(StrEnum):
    """Why a supervised active operation ended normally."""

    TIMEOUT = "timeout"
    BULK_TIMEOUT = "bulk-timeout"
    FLOAT_TIMEOUT = "float-timeout"
    DEVICE_CUTOFF = "device-cutoff"
    MODE_AUTO_OFF = "mode-auto-off"
    END_CURRENT = "end-current"
    ZERO_CURRENT = "zero-current"


@dataclass(frozen=True, slots=True)
class SessionResult:
    """The durable outcome and terminal snapshot of an active operation."""

    stop_reason: SessionStopReason
    samples: int
    elapsed_seconds: float
    final_status: EbcStatus | None = None
    measurement: MeasurementSummary | None = None
    capacity: CapacityAssessment | None = None


@dataclass(frozen=True, slots=True)
class MonitorResult:
    """The outcome of a finite read-only monitoring run."""

    samples: int


class _SessionSignal(Exception):
    """A termination signal raised while a tester operation is supervised."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


class SupervisedSession:
    """Keep an active tester operation bounded and stop it on every exit path.

    The tester is a stateful peripheral: closing the serial port does not stop
    a charge or discharge already running on its own. This lifecycle seam
    makes STOP best-effort on exceptions, Ctrl-C, termination signals, and
    normal timeout/cutoff completion.
    """

    _SIGNALS = tuple(
        signum
        for signum in (
            getattr(signal, "SIGINT", None),
            getattr(signal, "SIGTERM", None),
            getattr(signal, "SIGHUP", None),
        )
        if signum is not None
    )

    def __init__(self, tester: Tester, logger: StatusLogger | None):
        self.tester = tester
        self.logger = logger
        self._active = False
        self._stopping = False
        self._old_handlers: dict[int, object] = {}

    def __enter__(self) -> Self:
        for signum in self._SIGNALS:
            try:
                self._old_handlers[signum] = signal.signal(signum, self._handle_signal)
            except OSError, ValueError:
                # signal.signal is only legal in the main thread. Unit tests
                # and embedders may run the CLI elsewhere; finally still
                # provides cleanup for ordinary exceptions in that case.
                continue
        return self

    def __exit__(self, exc_type: object, _exc: object, _traceback: object) -> None:
        try:
            if self._active:
                if exc_type is None:
                    event = "session-stop-sent"
                elif isinstance(exc_type, type) and issubclass(exc_type, _SessionSignal):
                    event = "signal-stop-sent"
                elif isinstance(exc_type, type) and issubclass(exc_type, KeyboardInterrupt):
                    event = "operator-interrupt-stop-sent"
                else:
                    event = "error-stop-sent"
                try:
                    self.stop(event)
                except Exception as stop_error:
                    if not isinstance(_exc, BaseException):
                        raise
                    _exc.add_note(f"STOP cleanup failed: {stop_error}")
        finally:
            for signum, handler in self._old_handlers.items():
                try:
                    signal.signal(signum, handler)
                except OSError, ValueError:
                    continue

    def mark_started(self, event: str) -> None:
        """Mark the operation live after its start frame has been sent."""

        self._active = True
        if self.logger is not None:
            self.logger.write_event(event)

    def stop(self, event: str, *, raise_errors: bool = False) -> bool:
        """Send STOP once and record whether that attempt succeeded."""

        if not self._active or self._stopping:
            return True
        self._stopping = True
        try:
            self.tester.stop()
        except Exception as stop_error:
            if self.logger is not None:
                try:
                    self.logger.write_event(f"{event}-failed")
                except Exception as log_error:  # noqa: BLE001 - preserve the STOP transport error
                    stop_error.add_note(f"Could not log STOP failure: {log_error}")
            if raise_errors:
                raise
            return False
        else:
            if self.logger is not None:
                self.logger.write_event(event)
            return True
        finally:
            self._active = False

    def _handle_signal(self, signum: int, _frame: object) -> None:
        if not self._stopping:
            raise _SessionSignal(signum)


class _ReadDeadlineReached(Exception):
    """The session deadline expired while waiting for an observation."""


def read_live_status(
    tester: Tester,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    require_streamed: bool = False,
) -> EbcStatus:
    """Read the latest observation, optionally forbidding cached CONNECT polls."""

    def remaining() -> float | None:
        if deadline is None:
            return None
        seconds = deadline - clock()
        if seconds <= 0:
            raise _ReadDeadlineReached
        return seconds

    try:
        return tester.next_status(timeout=remaining(), latest=True)
    except DeviceError:
        if require_streamed:
            remaining()
            raise
        # Poll-only firmware needs CONNECT. Recompute the budget so a failed
        # streamed read cannot give the fallback another full session timeout.
        try:
            return tester.status(timeout=remaining(), latest=True)
        except DeviceError:
            remaining()
            raise


def run_active_session(
    *,
    session: SupervisedSession,
    printer: Callable[[EbcStatus], None],
    timeout_minutes: int,
    sample_event: str,
    stop_on_cutoff: bool = False,
    completion: Callable[[EbcStatus, int], SessionStopReason | None] | None = None,
    require_streamed: bool = False,
    on_sample: Callable[[EbcStatus, int], None] | None = None,
    capacity_policy: CapacityPolicy | None = None,
    started_at: float | None = None,
    poll_interval: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> SessionResult:
    """Log live status until cutoff or timeout, stopping the tester on exit.

    Capacity failures retain their original exception and traceback, with a
    ``capacity_assessment: CapacityAssessment`` attribute, even without logging.
    """

    tester = session.tester
    logger = session.logger
    session_started = clock() if started_at is None else started_at
    deadline = session_started + timeout_minutes * 60
    samples = 0
    final_status: EbcStatus | None = None
    accounting = capacity_policy.accounting if capacity_policy else MeasurementAccumulator()
    try:
        while True:
            if clock() >= deadline:
                return _finish_session(
                    session=session,
                    stop_event="timeout-stop-sent",
                    stop_reason=SessionStopReason.TIMEOUT,
                    samples=samples,
                    session_started=session_started,
                    clock=clock,
                    final_status=final_status,
                    accounting=accounting,
                    capacity_policy=capacity_policy,
                )

            try:
                status = read_live_status(
                    tester,
                    deadline=deadline,
                    clock=clock,
                    require_streamed=require_streamed or capacity_policy is not None,
                )
            except _ReadDeadlineReached:
                continue
            received_at = clock()
            if received_at >= deadline:
                continue
            final_status = status
            sample_elapsed = received_at - session_started
            if capacity_policy is not None:
                capacity_policy.add(status, sample_elapsed)
            else:
                accounting.add(status, sample_elapsed)
            samples += 1

            # Evaluate receipt-time policies before synchronous output can distort
            # sample gaps or dwell. The public callback still takes status/count.
            if completion is not None:
                completion_reason = completion(status, samples)
            elif stop_on_cutoff and status.completion_reason is EbcCompletionReason.CAPACITY_CUTOFF:
                completion_reason = SessionStopReason.DEVICE_CUTOFF
            else:
                completion_reason = None
            # Capacity samples must survive an output callback interrupt/failure.
            if logger is not None and capacity_policy is not None:
                logger.write(status, event=sample_event, session_elapsed_seconds=sample_elapsed)
            printer(status)
            if logger is not None and capacity_policy is None:
                logger.write(status, event=sample_event, session_elapsed_seconds=sample_elapsed)
            if (
                not (capacity_policy is not None and completion_reason is not None)
                and clock() >= deadline
            ):
                continue
            if completion_reason is not None:
                return _finish_session(
                    session=session,
                    stop_event=f"{completion_reason.value}-stop-sent",
                    stop_reason=completion_reason,
                    samples=samples,
                    session_started=session_started,
                    clock=clock,
                    final_status=final_status,
                    accounting=accounting,
                    capacity_policy=capacity_policy,
                )

            if on_sample is not None:
                on_sample(status, samples)
            remaining = deadline - clock()
            if remaining > 0:
                sleeper(min(poll_interval, remaining))
    except BaseException as exc:
        if capacity_policy is not None:
            _record_capacity_failure(exc, capacity_policy, logger, clock() - session_started)
        raise


def _record_capacity_failure(
    exc: BaseException,
    policy: CapacityPolicy,
    logger: StatusLogger | None,
    elapsed_seconds: float,
) -> None:
    """Attach a typed partial assessment before best-effort durable logging."""

    if isinstance(exc, _SessionSignal):
        termination = "signal"
    elif isinstance(exc, KeyboardInterrupt):
        termination = "interrupted"
    else:
        termination = "error"
    assessment: CapacityAssessment = policy.assess(termination, elapsed_seconds)
    setattr(exc, "capacity_assessment", assessment)  # noqa: B010 - exceptions keep their original type
    if logger is not None:
        try:
            logger.write_event(assessment.event)
        except Exception as log_error:  # noqa: BLE001 - do not replace the original failure
            exc.add_note(f"Could not persist partial capacity assessment: {log_error}")


def _finish_session(
    *,
    session: SupervisedSession,
    stop_event: str,
    stop_reason: SessionStopReason,
    samples: int,
    session_started: float,
    clock: Callable[[], float],
    final_status: EbcStatus | None,
    accounting: MeasurementAccumulator,
    capacity_policy: CapacityPolicy | None = None,
) -> SessionResult:
    session.stop(stop_event, raise_errors=True)
    elapsed = max(0.0, clock() - session_started)
    capacity = capacity_policy.assess(stop_reason.value, elapsed) if capacity_policy else None
    if capacity is not None and session.logger is not None:
        session.logger.write_event(capacity.event)
    return SessionResult(
        stop_reason=stop_reason,
        samples=samples,
        elapsed_seconds=elapsed,
        final_status=final_status,
        measurement=accounting.summary,
        capacity=capacity,
    )


def run_bulk_to_float_session(
    *,
    session: SupervisedSession,
    printer: Callable[[EbcStatus], None],
    profile: BulkToFloatProfile,
    on_float_start: Callable[[EbcStatus], None] | None = None,
    poll_interval: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> SessionResult:
    """Run PbAc bulk charging, then a bounded CCCV float hold.

    The phase transition is driven only by the source-defined PbAc auto-off
    status. If that state does not arrive before the bulk deadline, the
    tester is stopped and the float phase is never started.
    """

    tester = session.tester
    logger = session.logger
    session_started = clock()
    phase = "bulk"
    phase_deadline = session_started + profile.bulk_timeout_minutes * 60
    samples = 0
    bulk_activity_seen = False
    final_status: EbcStatus | None = None
    accounting = MeasurementAccumulator()
    while True:
        now = clock()
        if now >= phase_deadline:
            if phase == "bulk":
                stop_event = "bulk-timeout-stop-sent"
                stop_reason = SessionStopReason.BULK_TIMEOUT
            else:
                stop_event = "float-timeout-stop-sent"
                stop_reason = SessionStopReason.FLOAT_TIMEOUT
            return _finish_session(
                session=session,
                stop_event=stop_event,
                stop_reason=stop_reason,
                samples=samples,
                session_started=session_started,
                clock=clock,
                final_status=final_status,
                accounting=accounting,
            )

        try:
            status = read_live_status(tester, deadline=phase_deadline, clock=clock)
        except _ReadDeadlineReached:
            continue
        if clock() >= phase_deadline:
            continue
        final_status = status
        sample_elapsed = clock() - session_started
        accounting.add(status, sample_elapsed)
        printer(status)
        if logger is not None:
            event = profile.bulk_sample_event if phase == "bulk" else profile.float_sample_event
            logger.write(status, event=event, session_elapsed_seconds=sample_elapsed)
        samples += 1

        # A completion code alone cannot prove that the newly started bulk
        # phase ran: firmware may briefly repeat its preceding operation.
        if phase == "bulk" and status.completion_reason is None and status.current_amps > 0:
            bulk_activity_seen = True
        if clock() >= phase_deadline:
            continue
        if (
            phase == "bulk"
            and bulk_activity_seen
            and status.completion_reason is EbcCompletionReason.LEAD_ACID_AUTO_OFF
        ):
            float_baseline = tester.begin_mode(profile.float_start_command)
            final_status = float_baseline
            if logger is not None:
                logger.write(
                    float_baseline,
                    event=profile.float_baseline_event,
                    session_elapsed_seconds=clock() - session_started,
                )
                logger.write_event(profile.float_start_event)
            if on_float_start is not None:
                on_float_start(float_baseline)
            phase = "float"
            phase_deadline = clock() + profile.float_timeout_minutes * 60
            continue

        remaining = phase_deadline - clock()
        if remaining <= 0:
            continue
        sleeper(min(poll_interval, remaining))


def run_monitor_session(
    *,
    tester: Tester,
    logger: StatusLogger | None,
    printer: Callable[[EbcStatus], None],
    interval: float,
    count: int | None,
    sleeper: Callable[[float], None] = time.sleep,
) -> MonitorResult:
    """Poll and log read-only status frames until *count* is reached."""

    samples = 0
    while True:
        status = tester.status(latest=True) if samples == 0 else read_live_status(tester)
        printer(status)
        if logger is not None:
            logger.write(status)
        samples += 1
        if count is not None and samples >= count:
            return MonitorResult(samples=samples)
        sleeper(interval)
