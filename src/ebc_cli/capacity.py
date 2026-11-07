# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Jay (Python CLI contributions)
# See NOTICE for upstream attribution and port details.

"""Shared live/replay capacity evidence and measurement-quality policy.

This is an engineering screening policy, not a calibration guarantee. Active
CC current must be within max(0.02 A, 5% of requested current), inclusively.
At cutoff, that tolerance is intentionally upper-bound-only: zero and falling
current are allowed. Terminal voltage may rebound by max(0.10 V, 5% of requested
cutoff). A larger value flags an inconsistent measurement, not exact cutover or
battery chemistry, and never prevents STOP.
The existing 30-second integration guard also bounds first-observation and
unobserved tail delays. Shorter initial/tail delays are disclosed, never
integrated. No baseline or CONNECT observation belongs in this policy.
"""

import json
import math
from dataclasses import asdict, dataclass
from enum import StrEnum

from .measurement import DEFAULT_MAX_GAP_SECONDS, MeasurementAccumulator, MeasurementSummary
from .protocol import EbcCompletionReason, EbcStatus

REQUEST_EVENT_PREFIX = "capacity-request-v1:"
ASSESSMENT_EVENT_PREFIX = "capacity-assessment-v1:"
# Both carry positive discharge in tests/fixtures/a10h_live_cycles.csv.
CC_ACTIVE_STATES = frozenset({0x0A, 0x6E})


class CapacityOutcome(StrEnum):
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    INVALID = "invalid"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class CapacityRequest:
    current_amps: float
    cutoff_volts: float
    timeout_minutes: int

    def __post_init__(self) -> None:
        for name in ("current_amps", "cutoff_volts", "timeout_minutes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"capacity {name} must be numeric")
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"capacity {name} must be finite and positive")
        if not isinstance(self.timeout_minutes, int):
            raise TypeError("capacity timeout_minutes must be an integer")

    @property
    def event(self) -> str:
        return REQUEST_EVENT_PREFIX + json.dumps(asdict(self), sort_keys=True, allow_nan=False)

    @classmethod
    def from_event(cls, event: str) -> CapacityRequest:
        try:
            return cls(**json.loads(event.removeprefix(REQUEST_EVENT_PREFIX)))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("invalid capacity request metadata") from exc


@dataclass(frozen=True, slots=True)
class CapacityAssessment:
    outcome: CapacityOutcome
    reasons: tuple[str, ...]
    discharge_observed: bool
    measurement: MeasurementSummary
    termination: str
    elapsed_seconds: float
    request: CapacityRequest | None = None
    unobserved_seconds: float = 0.0

    @property
    def event(self) -> str:
        return ASSESSMENT_EVENT_PREFIX + json.dumps(asdict(self), sort_keys=True, allow_nan=False)


def format_capacity_assessment(assessment: CapacityAssessment) -> str:
    """Use the same result text for a live run and its durable replay."""

    label = "Measured" if assessment.outcome is CapacityOutcome.COMPLETED else "Partial"
    coverage = (
        "Observation coverage: unverified"
        if assessment.outcome is CapacityOutcome.UNVERIFIED
        else f"Unobserved: {assessment.unobserved_seconds:.3f} s (not included in integration)"
    )
    return "\n".join(
        (
            f"Capacity test: {assessment.outcome.value}",
            (
                f"{label}: {assessment.measurement.amp_hours:.3f} Ah, "
                f"{assessment.measurement.watt_hours:.3f} Wh"
            ),
            f"Capacity reasons: {', '.join(assessment.reasons) or 'none'}",
            coverage,
        )
    )


class CapacityPolicy:
    """Own evidence and integration, identically for live and replay samples."""

    def __init__(self, request: CapacityRequest):
        self.request = request
        self.accounting = MeasurementAccumulator()
        self.discharge_observed = False
        self._reasons: list[str] = []
        self._last_status: EbcStatus | None = None
        self._first_elapsed: float | None = None
        self._last_elapsed: float | None = None
        self._cutoff_seen = False
        self._cutoff_inconsistent = False

    def flag(self, reason: str) -> None:
        if reason not in self._reasons:
            self._reasons.append(reason)

    def add(self, status: EbcStatus, elapsed_seconds: float) -> None:
        """Accept only streamed poststart observations, never the start baseline."""

        values = (elapsed_seconds, status.current_amps, status.voltage_volts)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            for value in values
        ):
            self.flag("corrupt-measurement")
            return
        if elapsed_seconds >= self.request.timeout_minutes * 60:
            self.flag("contradictory-capacity-evidence")
            return
        if self._cutoff_seen:
            self.flag("samples-after-cutoff")
        self._last_status = status
        if self._first_elapsed is None:
            self._first_elapsed = elapsed_seconds
            if elapsed_seconds > DEFAULT_MAX_GAP_SECONDS:
                self.flag("first-observation-delay")
        if self._last_elapsed is not None and elapsed_seconds <= self._last_elapsed:
            self.flag("non-increasing-observation-time")
        self._last_elapsed = max(self._last_elapsed or 0.0, elapsed_seconds)
        self.accounting.add(status, elapsed_seconds)
        tolerance = max(0.02, 0.05 * self.request.current_amps)
        if status.completion_reason is EbcCompletionReason.CAPACITY_CUTOFF:
            self._cutoff_seen = True
            if status.model_id != 0x06:
                self.flag("unrecognized-discharge-state")
                self._cutoff_inconsistent = True
            difference = status.current_amps - self.request.current_amps
            allowance = max(0.10, 0.05 * self.request.cutoff_volts)
            rebound = status.voltage_volts - self.request.cutoff_volts
            if rebound > allowance and not math.isclose(
                rebound, allowance, rel_tol=0, abs_tol=1e-12
            ):
                self.flag("cutoff-voltage-mismatch")
                self._cutoff_inconsistent = True
        elif status.model_id == 0x06 and status.state_byte in CC_ACTIVE_STATES:
            if status.current_amps > 0:
                self.discharge_observed = True
            difference = abs(status.current_amps - self.request.current_amps)
        else:
            self.flag("unrecognized-discharge-state")
            return
        if difference > tolerance and not math.isclose(
            difference, tolerance, rel_tol=0, abs_tol=1e-12
        ):
            self.flag("load-current-mismatch")
            if status.completion_reason is EbcCompletionReason.CAPACITY_CUTOFF:
                self._cutoff_inconsistent = True

    def assess(self, termination: str, elapsed_seconds: float) -> CapacityAssessment:
        measurement = self.accounting.summary
        reasons = list(self._reasons)
        if measurement.skipped_intervals:
            reasons.append("observation-gap")
        if measurement.ignored_samples:
            reasons.append("out-of-order-observation")
        elapsed_seconds = max(elapsed_seconds, self._last_elapsed or 0.0)
        # A cutoff observation ends the measurement, not time spent printing or stopping.
        cutoff = (
            self._last_status is not None
            and self._last_status.completion_reason is EbcCompletionReason.CAPACITY_CUTOFF
        )
        tail = 0.0 if cutoff else max(0.0, elapsed_seconds - (self._last_elapsed or 0.0))
        if tail > DEFAULT_MAX_GAP_SECONDS:
            reasons.append("last-observation-delay")
        quality_compromised = bool(reasons)
        unobserved = (self._first_elapsed or 0.0) + tail + measurement.skipped_seconds
        if not self.discharge_observed:
            reasons.append("no-observed-discharge")
        completed = cutoff and termination == "device-cutoff"
        if termination == "device-cutoff" and not cutoff:
            reasons.append("no-cutoff-observation")
        if not completed:
            reasons.append(termination)
        # Missing evidence alone is incomplete, not an invalid zero-capacity result.
        if (
            "corrupt-measurement" in reasons
            or self._cutoff_inconsistent
            or (self.discharge_observed and quality_compromised)
        ):
            outcome = CapacityOutcome.INVALID
        elif not self.discharge_observed or not completed:
            outcome = CapacityOutcome.INCOMPLETE
        else:
            outcome = CapacityOutcome.COMPLETED
        return CapacityAssessment(
            outcome=outcome,
            reasons=tuple(dict.fromkeys(reasons)),
            discharge_observed=self.discharge_observed,
            measurement=measurement,
            termination=termination,
            elapsed_seconds=elapsed_seconds,
            request=self.request,
            unobserved_seconds=unobserved,
        )
