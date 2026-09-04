import math
import unittest

from ebc_cli.measurement import (
    DEFAULT_MAX_GAP_SECONDS,
    MeasurementAccumulator,
    MeasurementSummary,
)
from ebc_cli.protocol import EbcStatus


class MeasurementAccumulatorTests(unittest.TestCase):
    def test_empty_summary_has_no_measurements(self) -> None:
        summary = MeasurementAccumulator().summary

        self.assertEqual(
            summary,
            MeasurementSummary(
                sample_count=0,
                integrated_intervals=0,
                elapsed_seconds=0.0,
                integrated_seconds=0.0,
                amp_hours=0.0,
                watt_hours=0.0,
                skipped_intervals=0,
                skipped_seconds=0.0,
                ignored_samples=0,
                first_timestamp=None,
                last_timestamp=None,
            ),
        )

    def test_first_sample_establishes_a_baseline(self) -> None:
        accumulator = MeasurementAccumulator()
        accumulator.add(_status(current=2.0, voltage=12.0), 10.0)

        summary = accumulator.summary
        self.assertEqual(summary.sample_count, 1)
        self.assertEqual(summary.first_timestamp, 10.0)
        self.assertEqual(summary.last_timestamp, 10.0)
        self.assertEqual(summary.elapsed_seconds, 0.0)
        self.assertEqual(summary.integrated_intervals, 0)
        self.assertEqual(summary.amp_hours, 0.0)
        self.assertEqual(summary.watt_hours, 0.0)

    def test_trapezoidal_integration_calculates_ah_and_wh(self) -> None:
        accumulator = MeasurementAccumulator(max_gap_seconds=None)
        accumulator.add(_status(current=2.0, voltage=12.0), 0.0)
        accumulator.add(_status(current=4.0, voltage=13.0), 3600.0)

        summary = accumulator.summary
        self.assertEqual(summary.sample_count, 2)
        self.assertEqual(summary.integrated_intervals, 1)
        self.assertEqual(summary.integrated_seconds, 3600.0)
        self.assertAlmostEqual(summary.amp_hours, 3.0)
        self.assertAlmostEqual(summary.watt_hours, 38.0)

    def test_duplicate_timestamp_replaces_the_baseline(self) -> None:
        accumulator = MeasurementAccumulator(max_gap_seconds=None)
        accumulator.add(_status(current=1.0, voltage=12.0), 0.0)
        accumulator.add(_status(current=3.0, voltage=12.0), 0.0)
        accumulator.add(_status(current=3.0, voltage=12.0), 3600.0)

        self.assertEqual(accumulator.summary.sample_count, 3)
        self.assertAlmostEqual(accumulator.summary.amp_hours, 3.0)

    def test_out_of_order_sample_is_ignored_without_losing_the_baseline(self) -> None:
        accumulator = MeasurementAccumulator(max_gap_seconds=None)
        accumulator.add(_status(current=1.0, voltage=12.0), 0.0)
        accumulator.add(_status(current=1.0, voltage=12.0), 2.0)
        accumulator.add(_status(current=3.0, voltage=12.0), 1.0)
        accumulator.add(_status(current=3.0, voltage=12.0), 3.0)

        summary = accumulator.summary
        self.assertEqual(summary.sample_count, 4)
        self.assertEqual(summary.ignored_samples, 1)
        self.assertEqual(summary.integrated_intervals, 2)
        self.assertAlmostEqual(summary.amp_hours, 4.0 / 3600.0)

    def test_large_gap_is_skipped_and_accounting_resumes_after_it(self) -> None:
        accumulator = MeasurementAccumulator(max_gap_seconds=30.0)
        accumulator.add(_status(current=1.0, voltage=12.0), 0.0)
        accumulator.add(_status(current=3.0, voltage=12.0), 60.0)
        accumulator.add(_status(current=3.0, voltage=12.0), 61.0)

        summary = accumulator.summary
        self.assertEqual(summary.sample_count, 3)
        self.assertEqual(summary.skipped_intervals, 1)
        self.assertEqual(summary.skipped_seconds, 60.0)
        self.assertEqual(summary.integrated_intervals, 1)
        self.assertEqual(summary.integrated_seconds, 1.0)
        self.assertEqual(summary.elapsed_seconds, 61.0)
        self.assertAlmostEqual(summary.amp_hours, 3.0 / 3600.0)
        self.assertAlmostEqual(summary.watt_hours, 36.0 / 3600.0)

    def test_invalid_configuration_timestamps_and_values_are_rejected(self) -> None:
        self.assertEqual(DEFAULT_MAX_GAP_SECONDS, 30.0)
        for gap in (True, 0.0, -1.0, math.nan, math.inf):
            with self.subTest(gap=gap), self.assertRaises((TypeError, ValueError)):
                MeasurementAccumulator(max_gap_seconds=gap)

        accumulator = MeasurementAccumulator()
        for timestamp in (True, -1.0, math.nan, math.inf):
            with self.subTest(timestamp=timestamp), self.assertRaises((TypeError, ValueError)):
                accumulator.add(_status(), timestamp)
        for status in (
            _status(current=math.nan),
            _status(voltage=math.inf),
        ):
            with self.subTest(status=status), self.assertRaises(ValueError):
                accumulator.add(status, 0.0)


def _status(*, current: float = 1.0, voltage: float = 12.0) -> EbcStatus:
    return EbcStatus(
        voltage_volts=voltage,
        current_amps=current,
        capacity_ah=0.0,
        device_timer_minutes=0,
        model_id=0x06,
        raw=b"",
    )


if __name__ == "__main__":
    unittest.main()
