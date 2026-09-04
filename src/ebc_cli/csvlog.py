"""Durable CSV recording of decoded EBC status samples."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import TextIO

from .protocol import EbcStatus


CSV_HEADER = (
    "timestamp",
    "event",
    "session_elapsed_s",
    "voltage_v",
    "current_a",
    "power_w",
    "capacity_ah",
    "device_timer_min",
    "model_id",
    "model_name",
    "raw_frame_hex",
)


class StatusCsvLogger:
    """Append flushed status records to a CSV file.

    Each row includes the raw protocol frame so a capacity-test record remains
    useful even if a later decoder improvement changes an interpreted field.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self._file: TextIO | None = None
        self._writer: csv.writer | None = None

    def __enter__(self) -> "StatusCsvLogger":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        needs_header = not self.path.exists() or self.path.stat().st_size == 0
        self._file = self.path.open("a", encoding="utf-8", newline="")
        self._writer = csv.writer(self._file)
        if needs_header:
            self._writer.writerow(CSV_HEADER)
            self._file.flush()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        if self._file is not None:
            self._file.close()
        self._file = None
        self._writer = None

    def write(
        self,
        status: EbcStatus,
        *,
        timestamp: datetime | None = None,
        event: str = "status",
        session_elapsed_seconds: float | None = None,
    ) -> None:
        """Write and flush a status observation immediately."""

        if self._file is None or self._writer is None:
            raise RuntimeError("CSV logger is not open")
        observed_at = timestamp or datetime.now().astimezone()
        self._writer.writerow(
            (
                observed_at.isoformat(timespec="seconds"),
                event,
                ""
                if session_elapsed_seconds is None
                else f"{max(0.0, session_elapsed_seconds):.3f}",
                f"{status.voltage_volts:.3f}",
                f"{status.current_amps:.3f}",
                f"{status.power_watts:.3f}",
                f"{status.capacity_ah:.3f}",
                status.device_timer_minutes,
                f"0x{status.model_id:02X}",
                status.model_name,
                status.raw.hex().upper(),
            )
        )
        self._file.flush()

    def write_event(self, event: str, *, timestamp: datetime | None = None) -> None:
        """Record a non-measurement command event in the same session CSV."""

        if self._file is None or self._writer is None:
            raise RuntimeError("CSV logger is not open")
        observed_at = timestamp or datetime.now().astimezone()
        self._writer.writerow((observed_at.isoformat(timespec="seconds"), event, *("" for _ in CSV_HEADER[2:])))
        self._file.flush()
