import unittest

from ebc_cli.capabilities import A10H_CAPABILITIES, CapabilityError, EbcCapabilities
from ebc_cli.modes import CCCV_CHARGE, EbcModeCommand, build_mode_packet
from ebc_cli.profiles import ProfileError, make_float_charge_profile
from ebc_cli.protocol import ProtocolError


class CapabilityTests(unittest.TestCase):
    def test_a10h_matches_the_manual_setting_envelope(self) -> None:
        capabilities = A10H_CAPABILITIES

        self.assertEqual(
            (
                capabilities.charge_current_min_amps,
                capabilities.charge_current_max_amps,
                capabilities.discharge_current_min_amps,
                capabilities.discharge_current_max_amps,
            ),
            (0.05, 5.0, 0.05, 10.0),
        )
        self.assertEqual(
            (
                capabilities.voltage_min_volts,
                capabilities.voltage_max_volts,
                capabilities.current_step_amps,
                capabilities.voltage_step_volts,
            ),
            (0.0, 30.0, 0.01, 0.01),
        )

    def test_ac_variant_has_a_fixed_charge_ceiling(self) -> None:
        self.assertEqual(A10H_CAPABILITIES.max_charge_voltage(), 22.0)
        with self.assertRaises(CapabilityError):
            A10H_CAPABILITIES.validate_charge_voltage(22.01)

    def test_setting_resolution_is_enforced(self) -> None:
        with self.assertRaisesRegex(CapabilityError, "0.01 A increments"):
            A10H_CAPABILITIES.validate_charge_current(0.051)
        with self.assertRaisesRegex(CapabilityError, "between 0.05 and 10 A"):
            A10H_CAPABILITIES.validate_discharge_current(10.01)

    def test_power_uses_the_documented_150_watt_envelope(self) -> None:
        A10H_CAPABILITIES.validate_power(150.0)
        with self.assertRaisesRegex(CapabilityError, "power must be between"):
            A10H_CAPABILITIES.validate_power(150.01)
        with self.assertRaisesRegex(CapabilityError, "between 0 and 30 V"):
            A10H_CAPABILITIES.validate_voltage(30.01)

    def test_profile_and_packet_validation_use_the_registered_envelope(self) -> None:
        restricted = EbcCapabilities(charge_current_max_amps=0.10)
        with self.assertRaisesRegex(ProfileError, "charge current must be between 0.05 and 0.1 A"):
            make_float_charge_profile(
                current_amps=0.2,
                voltage_volts=13.7,
                timeout_minutes=10,
                capabilities=restricted,
            )
        with self.assertRaisesRegex(ProtocolError, "charge current must be between 0.05 and 0.1 A"):
            build_mode_packet(
                EbcModeCommand(
                    CCCV_CHARGE, current_amps=0.2, voltage_volts=13.7, timeout_minutes=10
                ),
                capabilities=restricted,
            )


if __name__ == "__main__":
    unittest.main()
