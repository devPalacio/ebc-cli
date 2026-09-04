import csv
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from ebc_cli.cli import main
from ebc_cli.csvlog import CSV_HEADER, StatusCsvLogger
from ebc_cli.protocol import FRAME_PREFIX, FRAME_SUFFIX, EbcStatus, checksum, parse_status
from ebc_cli.report import ReportError, load_replay_log, summarize_replay_log

FIXTURE_PATH = Path(__file__).with_name("fixtures") / "a10h_session.csv"


class ReportTests(unittest.TestCase):
    def test_replay_log_exposes_decoded_samples_and_events(self) -> None:
        first = parse_status(_status_frame(current=(0, 100), voltage=(50, 0)))
        second = parse_status(_status_frame(current=(0, 50), voltage=(45, 0)))
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "capacity.csv"
            _write_session(path, first, second)

            replay = load_replay_log(path)

        self.assertEqual(tuple(sample.status for sample in replay.samples), (first, second))
        self.assertEqual(
            tuple(sample.event for sample in replay.samples),
            ("capacity-test-sample", "capacity-test-sample"),
        )
        self.assertEqual(tuple(replay.iter_statuses()), (first, second))
        self.assertEqual(replay.events[-1], "device-cutoff-stop-sent")

    def test_report_summarizes_duration_stop_and_host_energy(self) -> None:
        first = parse_status(_status_frame(current=(0, 100), voltage=(50, 0)))
        second = parse_status(_status_frame(current=(0, 50), voltage=(45, 0)))
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "capacity.csv"
            _write_session(path, first, second)

            summary = summarize_replay_log(load_replay_log(path))

        self.assertEqual(summary.status_count, 2)
        self.assertEqual(summary.active_sample_count, 2)
        self.assertEqual(summary.duration_seconds, 10.0)
        self.assertEqual(summary.stop_event, "device-cutoff-stop-sent")
        self.assertEqual(summary.first_status, first)
        self.assertEqual(summary.final_status, second)
        self.assertAlmostEqual(summary.measurement.amp_hours, 0.75 * 10 / 3600)
        self.assertAlmostEqual(summary.measurement.watt_hours, 17.4 / 2 * 10 / 3600)

    def test_report_command_is_read_only_and_prints_summary(self) -> None:
        first = parse_status(_status_frame())
        second = parse_status(_status_frame(current=(0, 50), voltage=(45, 0)))
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "capacity.csv"
            _write_session(path, first, second)
            output = StringIO()
            with redirect_stdout(output):
                result = main(["report", str(path)])

        self.assertEqual(result, 0)
        rendered = output.getvalue()
        self.assertIn("Status samples: 2", rendered)
        self.assertIn("Stop event: device-cutoff-stop-sent", rendered)
        self.assertIn("Integrated: 0.002 Ah", rendered)
        self.assertIn("Energy: 0.019 Wh", rendered)

    def test_report_excludes_a_profile_baseline_from_active_duration(self) -> None:
        summary = summarize_replay_log(load_replay_log(FIXTURE_PATH))

        self.assertEqual(summary.status_count, 3)
        self.assertEqual(summary.active_sample_count, 2)
        self.assertEqual(summary.duration_seconds, 10.0)
        self.assertEqual(summary.stop_event, "device-cutoff-stop-sent")
        self.assertEqual(summary.final_status.voltage_volts, 10.5)

    def test_replay_rejects_a_missing_or_malformed_log(self) -> None:
        with self.assertRaises(ReportError):
            load_replay_log("/tmp/does-not-exist-ebc-report.csv")

        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "invalid.csv"
            with path.open("w", newline="", encoding="utf-8") as csv_file:
                csv.writer(csv_file).writerow(("not", "an", "ebc", "log"))
            with self.assertRaises(ReportError):
                load_replay_log(path)

    def test_report_replays_legacy_logs_without_a_schema_marker(self) -> None:
        status = parse_status(_status_frame())
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "legacy.csv"
            with path.open("w", newline="", encoding="utf-8") as csv_file:
                writer = csv.writer(csv_file)
                writer.writerow(CSV_HEADER)
                writer.writerow(
                    (
                        "2026-09-03T20:00:00+00:00",
                        "status",
                        *("" for _ in CSV_HEADER[2:-1]),
                        status.raw.hex().upper(),
                    )
                )

            report = summarize_replay_log(load_replay_log(path))

        self.assertEqual(report.schema_version, 0)
        self.assertEqual(report.status_count, 1)
        self.assertEqual(report.final_status, status)


def _write_session(path: Path, first: EbcStatus, second: EbcStatus) -> None:
    with StatusCsvLogger(path) as logger:
        logger.write(
            first,
            timestamp=datetime(2026, 9, 3, 20, 0, tzinfo=UTC),
            event="capacity-test-sample",
            session_elapsed_seconds=0.0,
        )
        logger.write(
            second,
            timestamp=datetime(2026, 9, 3, 20, 0, 10, tzinfo=UTC),
            event="capacity-test-sample",
            session_elapsed_seconds=10.0,
        )
        logger.write_event(
            "device-cutoff-stop-sent",
            timestamp=datetime(2026, 9, 3, 20, 0, 10, tzinfo=UTC) + timedelta(microseconds=1),
        )


def _status_frame(
    *, state: int = 0x68, current: tuple[int, int] = (0, 70), voltage: tuple[int, int] = (50, 0)
) -> bytes:
    frame = bytearray(19)
    frame[0] = FRAME_PREFIX
    frame[1] = state
    frame[2:4] = bytes(current)
    frame[4:6] = bytes(voltage)
    frame[6:8] = bytes((0, 100))
    frame[16] = 0x06
    frame[17] = checksum(frame, 17)
    frame[-1] = FRAME_SUFFIX
    return bytes(frame)


if __name__ == "__main__":
    unittest.main()
