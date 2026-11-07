# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Jay (Python CLI contributions)
# See NOTICE for upstream attribution and port details.

"""The shared encoder must not erase profile policy or live-start validation."""

import unittest
from unittest.mock import Mock

from ebc_cli.capabilities import A10H_CAPABILITIES
from ebc_cli.device import EbcDevice
from ebc_cli.modes import (
    CCCV_CHARGE,
    CONSTANT_POWER_DISCHARGE,
    CONSTANT_RESISTANCE_DISCHARGE,
    LEAD_ACID_CHARGE,
    EbcModeCommand,
    build_mode_packet,
)
from ebc_cli.profiles import (
    ProfileError,
    make_bulk_to_float_profile,
    make_float_charge_profile,
    make_recovery_profile,
)
from ebc_cli.protocol import ProtocolError


class ProfileModeMergerTests(unittest.TestCase):
    def test_float_recipe_and_generic_cccv_use_one_command_and_encoder(self):
        profile = make_float_charge_profile(
            current_amps=0.5, voltage_volts=13.7, timeout_minutes=120
        )
        command = EbcModeCommand(
            CCCV_CHARGE, current_amps=0.5, voltage_volts=13.7, timeout_minutes=120
        )
        self.assertEqual(profile.start_command, command)
        self.assertEqual(
            build_mode_packet(profile.start_command, capabilities=A10H_CAPABILITIES).hex().upper(),
            "FA71003205AA007894F8",
        )
        self.assertFalse(profile.stop_on_cutoff)

    def test_generic_cccv_does_not_relax_float_recipe_limits(self):
        command = EbcModeCommand(
            CCCV_CHARGE, current_amps=0.5, voltage_volts=14.4, timeout_minutes=120
        )
        command.validate_live(A10H_CAPABILITIES)
        with self.assertRaisesRegex(ProfileError, "float-charge voltage"):
            make_float_charge_profile(current_amps=0.5, voltage_volts=14.4, timeout_minutes=120)
        with self.assertRaisesRegex(ProfileError, "recovery current"):
            make_recovery_profile(current_amps=0.5, timeout_minutes=30)

    def test_bulk_and_float_keep_distinct_commands_and_phase_events(self):
        profile = make_bulk_to_float_profile(
            bulk_current_amps=0.8,
            bulk_timeout_minutes=1440,
            float_current_amps=0.5,
            float_voltage_volts=13.7,
            float_timeout_minutes=240,
        )
        self.assertIs(profile.bulk_start_command.mode, LEAD_ACID_CHARGE)
        self.assertIs(profile.float_start_command.mode, CCCV_CHARGE)
        self.assertEqual(profile.bulk_start_command.cells, 6)
        self.assertEqual(profile.float_start_command.voltage_volts, 13.7)
        self.assertEqual(profile.bulk_start_command.timeout_minutes, 1440)
        self.assertEqual(profile.float_start_command.timeout_minutes, 240)
        self.assertEqual(profile.float_start_event, "bulk-to-float-float-started")

    def test_direct_device_start_cannot_bypass_static_live_checks(self):
        invalid = (
            EbcModeCommand(CONSTANT_POWER_DISCHARGE, power_watts=0.5, cutoff_volts=10.5),
            EbcModeCommand(CONSTANT_POWER_DISCHARGE, power_watts=10.5, cutoff_volts=10.5),
            EbcModeCommand(CCCV_CHARGE, current_amps=0.5, voltage_volts=13.7, timeout_minutes=0),
            EbcModeCommand(CCCV_CHARGE, current_amps=0.5, voltage_volts=13.7, timeout_minutes=True),
            EbcModeCommand(CCCV_CHARGE, current_amps=0.5, voltage_volts=13.7, timeout_minutes=1.5),
            EbcModeCommand(
                CONSTANT_RESISTANCE_DISCHARGE, resistance_ohms=float("nan"), cutoff_volts=10.5
            ),
            EbcModeCommand(CONSTANT_RESISTANCE_DISCHARGE, resistance_ohms=10, cutoff_volts=31),
            EbcModeCommand(LEAD_ACID_CHARGE, current_amps=0.5, cells=True),
        )
        for command in invalid:
            with self.subTest(command=command):
                device = EbcDevice()
                device._fd = 42
                device._write_all = Mock()
                device._read_status_frame = Mock()
                with self.assertRaises(ProtocolError):
                    device.begin_mode(command)
                device._write_all.assert_not_called()
                device._read_status_frame.assert_not_called()
                device._fd = None

    def test_offline_planning_is_not_live_request_approval(self):
        command = EbcModeCommand(CONSTANT_POWER_DISCHARGE, power_watts=0.5, cutoff_volts=10.5)
        self.assertEqual(build_mode_packet(command)[2:4], b"\x00\x00")
        with self.assertRaisesRegex(ProtocolError, "whole watts"):
            command.validate_live(A10H_CAPABILITIES)


if __name__ == "__main__":
    unittest.main()
