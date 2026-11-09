# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Jay (Python CLI contributions)
# See NOTICE for upstream attribution and port details.

"""Replay and summarize status records captured in an EBC CSV session."""

import csv
import json
import math
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .capacity import (
    ASSESSMENT_EVENT_PREFIX,
    REQUEST_EVENT_PREFIX,
    CapacityAssessment,
    CapacityOutcome,
    CapacityPolicy,
    CapacityRequest,
    CutoffKind,
    format_capacity_assessment,
)
from .csvlog import CSV_HEADER, CSV_SCHEMA_VERSION, schema_version_from_event
from .measurement import MeasurementAccumulator, MeasurementSummary
from .protocol import EbcStatus, ProtocolError, parse_status


class ReportError(ValueError):
    """A CSV session could not be replayed or summarized safely."""


@dataclass(frozen=True, slots=True)
class ReplaySample:
    """One decoded status observation retained in a replayable session."""

    timestamp: datetime
    event: str
    elapsed_seconds: float | None
    status: EbcStatus

    @property
    def is_active(self) -> bool:
        """Whether this row belongs to an active profile sample stream."""

        return self.event.endswith("-sample")


@dataclass(frozen=True, slots=True)
class ReplayLog:
    """Validated status samples and event names loaded from one CSV file."""

    path: Path
    schema_version: int
    samples: tuple[ReplaySample, ...]
    events: tuple[str, ...]
    records: tuple[ReplaySample | str, ...] = ()

    def iter_statuses(self) -> Iterator[EbcStatus]:
        """Replay decoded statuses in the order they were recorded."""

        yield from (sample.status for sample in self.samples)


@dataclass(frozen=True, slots=True)
class SessionReport:
    """Summary values derived from a validated replay log."""

    path: Path
    schema_version: int
    status_count: int
    active_sample_count: int
    duration_seconds: float
    stop_event: str | None
    first_status: EbcStatus
    final_status: EbcStatus
    measurement: MeasurementSummary
    capacity: CapacityAssessment | None = None


def load_replay_log(path: str | Path) -> ReplayLog:
    """Load and validate status frames from a CSV session log."""

    log_path = Path(path).expanduser()
    try:
        csv_file = log_path.open(newline="", encoding="utf-8")
    except OSError as exc:
        raise ReportError(f"cannot read session log {log_path}: {exc}") from exc

    with csv_file:
        reader = csv.DictReader(csv_file)
        if tuple(reader.fieldnames or ()) != CSV_HEADER:
            raise ReportError(
                f"unsupported CSV header in {log_path}; expected schema columns "
                f"{', '.join(CSV_HEADER)}"
            )

        samples: list[ReplaySample] = []
        events: list[str] = []
        records: list[ReplaySample | str] = []
        schema_versions: list[int] = []
        for line_number, row in enumerate(reader, start=2):
            event = (row.get("event") or "").strip()
            if not event:
                raise ReportError(f"line {line_number}: event is required")
            try:
                schema_version = schema_version_from_event(event)
            except ValueError as exc:
                raise ReportError(f"line {line_number}: {exc}") from exc
            if schema_version is not None:
                schema_versions.append(schema_version)
                continue

            events.append(event)
            timestamp = _parse_timestamp(row.get("timestamp", ""), line_number)
            raw_hex = (row.get("raw_frame_hex") or "").strip()
            if not raw_hex:
                records.append(event)
                continue
            try:
                status = parse_status(bytes.fromhex(raw_hex))
            except (ValueError, ProtocolError) as exc:
                raise ReportError(f"line {line_number}: invalid status frame: {exc}") from exc
            samples.append(
                ReplaySample(
                    timestamp=timestamp,
                    event=event,
                    elapsed_seconds=_parse_elapsed(row.get("session_elapsed_s", ""), line_number),
                    status=status,
                )
            )
            records.append(samples[-1])

    schema_version = _resolve_schema_version(schema_versions, log_path)
    if not samples:
        raise ReportError(f"session log {log_path} contains no status samples")
    return ReplayLog(
        path=log_path,
        schema_version=schema_version,
        samples=tuple(samples),
        events=tuple(events),
        records=tuple(records),
    )


def summarize_replay_log(log: ReplayLog) -> SessionReport:
    """Calculate a terminal and host-side measurement summary from a replay."""

    active_samples = tuple(sample for sample in log.samples if sample.is_active)
    measurement_samples = active_samples or log.samples
    accounting = MeasurementAccumulator()
    first_timestamp = measurement_samples[0].timestamp.timestamp()
    for sample in measurement_samples:
        elapsed = sample.elapsed_seconds
        if elapsed is None:
            elapsed = sample.timestamp.timestamp() - first_timestamp
        accounting.add(sample.status, elapsed)

    measurement = accounting.summary
    explicit_elapsed = (
        sample.elapsed_seconds
        for sample in measurement_samples
        if sample.elapsed_seconds is not None
    )
    duration_seconds = max(explicit_elapsed, default=measurement.elapsed_seconds)
    stop_event = next(
        (event for event in reversed(log.events) if _is_stop_event(event)),
        None,
    )
    capacity = _capacity_assessment(log, measurement, duration_seconds)
    if capacity is not None:
        measurement = capacity.measurement
        duration_seconds = capacity.elapsed_seconds
    return SessionReport(
        path=log.path,
        schema_version=log.schema_version,
        status_count=len(log.samples),
        active_sample_count=len(active_samples),
        duration_seconds=duration_seconds,
        stop_event=stop_event,
        first_status=log.samples[0].status,
        final_status=log.samples[-1].status,
        measurement=measurement,
        capacity=capacity,
    )


def _capacity_assessment(
    log: ReplayLog, measurement: MeasurementSummary, duration: float
) -> CapacityAssessment | None:
    if not any(event.startswith("capacity-") for event in log.events):
        return None
    active = tuple(sample for sample in log.samples if sample.is_active)
    if not active:
        measurement = MeasurementAccumulator().summary

    def unverified(reason: str) -> CapacityAssessment:
        return CapacityAssessment(
            CapacityOutcome.UNVERIFIED, (reason,), False, measurement, "unknown", duration
        )

    requests = [event for event in log.events if event.startswith(REQUEST_EVENT_PREFIX)]
    if not requests:
        return unverified("missing-capacity-request")
    if (
        len(requests) != 1
        or log.events.count("capacity-test-started") != 1
        or sum(event.endswith("-started") for event in log.events) != 1
    ):
        return unverified("ambiguous-capacity-session")
    try:
        request = CapacityRequest.from_event(requests[0])
    except ValueError:
        return unverified("invalid-capacity-request")
    if any(sample.elapsed_seconds is None for sample in active):
        return unverified("missing-poststart-timestamps")
    if any(sample.event not in {"capacity-sample", "capacity-test-sample"} for sample in active):
        return unverified("mixed-profile-samples")
    records = log.records
    if not records:
        return unverified("missing-ordered-evidence")
    try:
        start_index = records.index("capacity-test-started")
        request_index = records.index(requests[0])
    except ValueError:
        return unverified("missing-ordered-evidence")
    sample_indices = [
        index
        for index, record in enumerate(records)
        if isinstance(record, ReplaySample) and record.is_active
    ]
    if not start_index < request_index or any(index <= request_index for index in sample_indices):
        return unverified("missing-poststart-evidence")
    policy = CapacityPolicy(request)
    elapsed = duration
    for sample in active:
        assert sample.elapsed_seconds is not None
        policy.add(sample.status, sample.elapsed_seconds)

    # Final metadata preserves elapsed time and corrupt-value diagnostics. It
    # cannot supply discharge, cutoff, or successful STOP evidence.
    for event in log.events:
        if not event.startswith(ASSESSMENT_EVENT_PREFIX):
            continue
        try:
            data = json.loads(event.removeprefix(ASSESSMENT_EVENT_PREFIX))
            final_elapsed = data["elapsed_seconds"]
            if isinstance(final_elapsed, bool) or not isinstance(final_elapsed, (int, float)):
                raise TypeError("invalid elapsed time")
            if not math.isfinite(final_elapsed) or final_elapsed < 0:
                raise ValueError("invalid elapsed time")
            elapsed = max(elapsed, final_elapsed)
            if "corrupt-measurement" in data.get("reasons", []):
                policy.flag("corrupt-measurement")
        except KeyError, TypeError, ValueError, OverflowError:
            policy.flag("contradictory-capacity-evidence")
    stops = [
        (index, record)
        for index, record in enumerate(records)
        if isinstance(record, str) and _is_stop_event(record)
    ]
    termination = "missing-stop-evidence"
    if stops:
        stop_index, stop = stops[-1]
        if len(stops) != 1 or stop_index <= max(sample_indices, default=request_index):
            policy.flag("contradictory-capacity-evidence")
        termination = {
            "operator-interrupt-stop-sent": "interrupted",
            "signal-stop-sent": "signal",
            "error-stop-sent": "error",
            "timeout-stop-sent": "timeout",
            "device-cutoff-stop-sent": "device-cutoff",
            "inferred-cutoff-stop-sent": "inferred-cutoff",
        }.get(stop, "stop-failed" if stop.endswith("-failed") else "unknown-stop")
        if (
            stop == "signal-stop-sent"
            and policy.cutoff_evidence is not None
            and policy.cutoff_evidence.kind is CutoffKind.INFERRED
        ):
            termination = "inferred-cutoff"
    return policy.assess(termination, elapsed)


def format_report(summary: SessionReport) -> str:
    """Render a stable human-readable report for a replay summary."""

    lines = [
        f"Report: {summary.path}",
        f"Status samples: {summary.status_count}",
        f"Active samples: {summary.active_sample_count}",
        f"Duration: {_format_duration(summary.duration_seconds)}",
        f"Stop event: {summary.stop_event or 'none'}",
        f"Integrated: {summary.measurement.amp_hours:.3f} Ah",
        f"Energy: {summary.measurement.watt_hours:.3f} Wh",
        (
            "Terminal: "
            f"{summary.final_status.voltage_volts:.3f} V, "
            f"{summary.final_status.current_amps:.3f} A, "
            f"{summary.final_status.capacity_ah:.3f} Ah"
        ),
    ]
    if summary.measurement.skipped_intervals:
        lines.append(
            f"Skipped intervals: {summary.measurement.skipped_intervals} "
            f"({summary.measurement.skipped_seconds:.1f} s)"
        )
    if summary.capacity is not None:
        lines.append(format_capacity_assessment(summary.capacity))
    return "\n".join(lines)


def _parse_timestamp(value: str, line_number: int) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ReportError(f"line {line_number}: invalid timestamp {value!r}") from exc


def _parse_elapsed(value: str, line_number: int) -> float | None:
    if not value:
        return None
    try:
        elapsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ReportError(f"line {line_number}: invalid session elapsed time {value!r}") from exc
    if not math.isfinite(elapsed) or elapsed < 0:
        raise ReportError(
            f"line {line_number}: session elapsed time must be finite and non-negative"
        )
    return elapsed


def _resolve_schema_version(versions: list[int], path: Path) -> int:
    if not versions:
        return 0
    if len(set(versions)) != 1:
        raise ReportError(f"session log {path} contains multiple schema versions")
    version = versions[0]
    if version != CSV_SCHEMA_VERSION:
        raise ReportError(f"unsupported CSV schema version {version} in {path}")
    return version


def _is_stop_event(event: str) -> bool:
    return event.endswith(("-stop-sent", "-stop-sent-failed"))


def _format_duration(seconds: float) -> str:
    total_seconds = max(0, round(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"
