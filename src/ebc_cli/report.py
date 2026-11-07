# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Jay (Python CLI contributions)
# See NOTICE for upstream attribution and port details.

"""Replay and summarize status records captured in an EBC CSV session."""

import csv
import math
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

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

    schema_version = _resolve_schema_version(schema_versions, log_path)
    if not samples:
        raise ReportError(f"session log {log_path} contains no status samples")
    return ReplayLog(
        path=log_path,
        schema_version=schema_version,
        samples=tuple(samples),
        events=tuple(events),
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
    explicit_elapsed = tuple(
        sample.elapsed_seconds
        for sample in measurement_samples
        if sample.elapsed_seconds is not None
    )
    duration_seconds = max(explicit_elapsed, default=measurement.elapsed_seconds)
    stop_event = next(
        (event for event in reversed(log.events) if _is_stop_event(event)),
        None,
    )
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
    )


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
