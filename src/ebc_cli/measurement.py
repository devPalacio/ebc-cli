# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Jay (Python CLI contributions)
# See NOTICE for upstream attribution and port details.

"""Host-side integration of sampled EBC measurements.

The tester reports its own capacity counter, but a host-side accumulator is
useful as an independent check.  Samples are integrated with the trapezoidal
rule using the caller's monotonic elapsed timestamps:

* amp-hours are the integral of current in amperes;
* watt-hours are the integral of instantaneous power (voltage times current).

The default gap guard is deliberately conservative for the CLI's one-second
polling loop.  An interval longer than :data:`DEFAULT_MAX_GAP_SECONDS` is not
integrated because the adjacent readings may not describe what happened while
the host was unable to sample.  The current sample becomes the new baseline,
so accounting resumes normally after the gap.  Pass ``None`` to disable that
guard when a caller has a trustworthy reason to integrate across every gap.
"""

import math
from dataclasses import dataclass

from .protocol import EbcStatus

SECONDS_PER_HOUR = 3600.0
DEFAULT_MAX_GAP_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class MeasurementSummary:
    """Immutable totals produced by :class:`MeasurementAccumulator`.

    ``elapsed_seconds`` is the observed span from the first to the latest
    accepted timestamp.  It can be larger than ``integrated_seconds`` when a
    large interval was intentionally skipped.  ``sample_count`` includes the
    initial sample, duplicate timestamps, and out-of-order samples supplied to
    the accumulator; ``ignored_samples`` identifies the out-of-order subset.

    Current is integrated with its sign.  A10H status frames currently expose
    positive current magnitudes, while preserving the sign here keeps the
    accounting useful if a future adapter exposes charge/discharge direction.
    """

    sample_count: int
    integrated_intervals: int
    elapsed_seconds: float
    integrated_seconds: float
    amp_hours: float
    watt_hours: float
    skipped_intervals: int
    skipped_seconds: float
    ignored_samples: int
    first_timestamp: float | None
    last_timestamp: float | None


class MeasurementAccumulator:
    """Accumulate host-side Ah and Wh totals from decoded status samples.

    Call :meth:`add` once per decoded status and provide a timestamp from the
    same monotonic clock for every call.  The first sample establishes a
    baseline and contributes no area.  Equal timestamps replace that baseline
    without adding an interval; an out-of-order sample is counted and ignored
    so a later correctly ordered sample can still be integrated against the
    last known point.

    The class does not infer a sample period, interpolate missing readings, or
    use the tester's device timer.  It only integrates intervals it can see and
    reports any intervals skipped by the large-gap guard in its summary.
    """

    def __init__(
        self,
        *,
        max_gap_seconds: float | None = DEFAULT_MAX_GAP_SECONDS,
    ) -> None:
        if max_gap_seconds is not None:
            max_gap_seconds = _finite_number(max_gap_seconds, "max_gap_seconds")
            if max_gap_seconds <= 0:
                raise ValueError("max_gap_seconds must be greater than zero or None")
        self.max_gap_seconds = max_gap_seconds
        self._previous: tuple[float, float, float] | None = None
        self._first_timestamp: float | None = None
        self._last_timestamp: float | None = None
        self._sample_count = 0
        self._integrated_intervals = 0
        self._integrated_seconds = 0.0
        self._amp_hours = 0.0
        self._watt_hours = 0.0
        self._skipped_intervals = 0
        self._skipped_seconds = 0.0
        self._ignored_samples = 0

    def add(self, status: EbcStatus, elapsed_seconds: float) -> None:
        """Consume one decoded status sample at a monotonic timestamp.

        ``elapsed_seconds`` must be finite and non-negative.  Measurement
        values must also be finite; rejecting them prevents one malformed
        sample from poisoning all subsequent totals.
        """

        timestamp = _finite_number(elapsed_seconds, "elapsed_seconds")
        if timestamp < 0:
            raise ValueError("elapsed_seconds must be non-negative")
        current = _finite_number(status.current_amps, "current_amps")
        voltage = _finite_number(status.voltage_volts, "voltage_volts")

        self._sample_count += 1
        previous = self._previous
        if previous is None:
            self._first_timestamp = timestamp
            self._last_timestamp = timestamp
            self._previous = (timestamp, current, voltage)
            return

        previous_timestamp, previous_current, previous_voltage = previous
        delta_seconds = timestamp - previous_timestamp
        if delta_seconds < 0:
            self._ignored_samples += 1
            return

        # A repeated timestamp carries no area.  Retaining its newest
        # measurement gives the next positive interval the freshest baseline.
        if delta_seconds == 0:
            self._previous = (timestamp, current, voltage)
            return

        self._last_timestamp = timestamp
        self._previous = (timestamp, current, voltage)
        if self.max_gap_seconds is not None and delta_seconds > self.max_gap_seconds:
            self._skipped_intervals += 1
            self._skipped_seconds += delta_seconds
            return

        delta_hours = delta_seconds / SECONDS_PER_HOUR
        average_current = (previous_current + current) / 2.0
        previous_power = previous_voltage * previous_current
        current_power = voltage * current
        average_power = (previous_power + current_power) / 2.0
        self._amp_hours += average_current * delta_hours
        self._watt_hours += average_power * delta_hours
        self._integrated_intervals += 1
        self._integrated_seconds += delta_seconds

    @property
    def summary(self) -> MeasurementSummary:
        """Return a snapshot of the totals accumulated so far."""

        if self._first_timestamp is None or self._last_timestamp is None:
            elapsed_seconds = 0.0
        else:
            elapsed_seconds = max(0.0, self._last_timestamp - self._first_timestamp)
        return MeasurementSummary(
            sample_count=self._sample_count,
            integrated_intervals=self._integrated_intervals,
            elapsed_seconds=elapsed_seconds,
            integrated_seconds=self._integrated_seconds,
            amp_hours=self._amp_hours,
            watt_hours=self._watt_hours,
            skipped_intervals=self._skipped_intervals,
            skipped_seconds=self._skipped_seconds,
            ignored_samples=self._ignored_samples,
            first_timestamp=self._first_timestamp,
            last_timestamp=self._last_timestamp,
        )


def _finite_number(value: float, name: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number
