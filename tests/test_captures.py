import csv
import unittest
from pathlib import Path

from ebc_cli.csvlog import CSV_HEADER, CSV_SCHEMA_VERSION, schema_version_from_event
from ebc_cli.protocol import EBC_A10H_MODEL_ID, parse_status

FIXTURE_PATH = Path(__file__).with_name("fixtures") / "a10h_session.csv"


class CapturedSessionFixtureTests(unittest.TestCase):
    def test_a10h_fixture_has_versioned_header_and_valid_status_frames(self) -> None:
        with FIXTURE_PATH.open(newline="", encoding="utf-8") as fixture_file:
            reader = csv.DictReader(fixture_file)
            self.assertEqual(tuple(reader.fieldnames or ()), CSV_HEADER)
            rows = list(reader)

        schema_rows = [row for row in rows if schema_version_from_event(row["event"]) is not None]
        status_rows = [row for row in rows if row["raw_frame_hex"]]

        self.assertEqual(len(schema_rows), 1)
        self.assertEqual(
            schema_version_from_event(schema_rows[0]["event"]),
            CSV_SCHEMA_VERSION,
        )
        self.assertEqual(len(status_rows), 3)

        decoded = [parse_status(bytes.fromhex(row["raw_frame_hex"])) for row in status_rows]
        self.assertTrue(all(status.model_id == EBC_A10H_MODEL_ID for status in decoded))
        self.assertEqual([status.voltage_volts for status in decoded], [12.6, 12.3, 10.5])
        self.assertEqual([status.current_amps for status in decoded], [0.8, 0.8, 0.0])
        self.assertEqual([status.capacity_ah for status in decoded], [0.0, 0.2, 6.4])

    def test_fixture_measurement_columns_match_raw_frames(self) -> None:
        with FIXTURE_PATH.open(newline="", encoding="utf-8") as fixture_file:
            rows = [row for row in csv.DictReader(fixture_file) if row["raw_frame_hex"]]

        for row in rows:
            status = parse_status(bytes.fromhex(row["raw_frame_hex"]))
            self.assertEqual(float(row["voltage_v"]), status.voltage_volts)
            self.assertEqual(float(row["current_a"]), status.current_amps)
            self.assertEqual(float(row["capacity_ah"]), status.capacity_ah)
            self.assertEqual(int(row["device_timer_min"]), status.device_timer_minutes)
            self.assertEqual(row["model_id"], f"0x{status.model_id:02X}")
            self.assertEqual(row["model_name"], status.model_name)


if __name__ == "__main__":
    unittest.main()
