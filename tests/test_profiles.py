import unittest
from unittest.mock import Mock

from ebc_cli.profiles import (
    ProfileError,
    make_bulk_to_float_profile,
    make_capacity_test_profile,
    make_float_charge_profile,
    make_recovery_profile,
)


class ProfileTests(unittest.TestCase):
    def test_profile_metadata_describes_lifecycle(self) -> None:
        recovery = make_recovery_profile(current_amps=0.2, timeout_minutes=30)
        self.assertEqual(recovery.start_event, "recovery-started")
        self.assertEqual(recovery.sample_event, "recovery-sample")
        self.assertFalse(recovery.stop_on_cutoff)
        self.assertIn("14.4 V target", recovery.summary)

        capacity = make_capacity_test_profile(
            current_amps=0.8,
            cutoff_volts=10.5,
            timeout_minutes=1440,
        )
        self.assertEqual(capacity.start_event, "capacity-test-started")
        self.assertEqual(capacity.sample_event, "capacity-sample")
        self.assertTrue(capacity.stop_on_cutoff)

    def test_profile_start_dispatches_to_the_matching_device_operation(self) -> None:
        tester = Mock()
        expected_status = object()
        tester.begin_float_charge.return_value = expected_status
        profile = make_float_charge_profile(
            current_amps=0.5,
            voltage_volts=13.7,
            timeout_minutes=120,
        )

        self.assertIs(profile.start(tester), expected_status)
        tester.begin_float_charge.assert_called_once_with(
            current_amps=0.5,
            voltage_volts=13.7,
            timeout_minutes=120,
        )

    def test_bulk_to_float_profile_describes_both_stages_and_dispatches(self) -> None:
        tester = Mock()
        profile = make_bulk_to_float_profile(
            bulk_current_amps=0.8,
            bulk_timeout_minutes=1440,
            float_current_amps=0.5,
            float_voltage_volts=13.7,
            float_timeout_minutes=240,
        )

        self.assertEqual(profile.start_event, "bulk-to-float-started")
        self.assertEqual(profile.bulk_sample_event, "bulk-to-float-bulk-sample")
        self.assertEqual(profile.float_sample_event, "bulk-to-float-float-sample")
        self.assertIn("14.4 V target", profile.summary)

        profile.start_bulk(tester)
        profile.start_float(tester)
        tester.begin_lead_acid_charge.assert_called_once_with(
            current_amps=0.8,
            timeout_minutes=1440,
        )
        tester.begin_float_charge.assert_called_once_with(
            current_amps=0.5,
            voltage_volts=13.7,
            timeout_minutes=240,
        )

    def test_profile_factories_reject_unsafe_parameters(self) -> None:
        invalid_profiles = (
            lambda: make_recovery_profile(current_amps=0.21, timeout_minutes=30),
            lambda: make_float_charge_profile(
                current_amps=0.5,
                voltage_volts=14.4,
                timeout_minutes=120,
            ),
            lambda: make_capacity_test_profile(
                current_amps=0.8,
                cutoff_volts=12.0,
                timeout_minutes=1440,
            ),
            lambda: make_bulk_to_float_profile(
                bulk_current_amps=0.8,
                bulk_timeout_minutes=1440,
                float_current_amps=0.9,
                float_voltage_volts=13.7,
                float_timeout_minutes=240,
            ),
            lambda: make_bulk_to_float_profile(
                bulk_current_amps=0.8,
                bulk_timeout_minutes=1440,
                float_current_amps=0.5,
                float_voltage_volts=14.4,
                float_timeout_minutes=240,
            ),
        )
        for factory in invalid_profiles:
            with self.assertRaises(ProfileError):
                factory()


if __name__ == "__main__":
    unittest.main()
