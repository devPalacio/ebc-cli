import unittest

from ebc_cli.modes import (
    CCCV_CHARGE,
    CONSTANT_CURRENT_DISCHARGE,
    CONSTANT_POWER_DISCHARGE,
    CONSTANT_RESISTANCE_DISCHARGE,
    LIFE_CHARGE,
    LIPO_CHARGE,
    LOOP_CONTROL,
    NICD_CHARGE,
    NIMH_CHARGE,
    OPERATING_MODES,
    SOURCE_OPERATING_MODES,
    WAIT_CONTROL,
    EbcModeCommand,
    EbcModeKind,
    EbcModePhase,
    EbcModeValue,
    EbcOperatingMode,
    build_cccv_charge_packet,
    build_cell_charge_packet,
    build_constant_resistance_packet,
    build_discharge_packet,
    build_mode_packet,
    encode_power,
    get_operating_mode,
)
from ebc_cli.protocol import ProtocolError, checksum


class OperatingModeCatalogTests(unittest.TestCase):
    def test_catalog_mirrors_source_mode_entries(self) -> None:
        self.assertEqual(
            tuple(mode.command for mode in SOURCE_OPERATING_MODES),
            (
                "c_NiMH",
                "c_NiCd",
                "c_LiPo",
                "c_LiFe",
                "c_Pb",
                "c_CCCV",
                "d_CC",
                "d_CP",
                "d_CR",
                "Wait",
                "Loop",
            ),
        )
        self.assertEqual(OPERATING_MODES["c_LiPo"], LIPO_CHARGE)
        self.assertIs(get_operating_mode("d_CP"), CONSTANT_POWER_DISCHARGE)
        self.assertIs(get_operating_mode("Wait"), WAIT_CONTROL)
        self.assertIs(get_operating_mode("LOOP"), LOOP_CONTROL)

    def test_source_metadata_contains_opcode_and_automatic_stop_values(self) -> None:
        expected = {
            "c_NiMH": (0x21, 0x28, None, 0x16, None),
            "c_NiCd": (0x31, 0x38, None, 0x17, None),
            "c_LiPo": (0x41, 0x48, None, 0x18, 4.2),
            "c_LiFe": (0x51, 0x58, None, 0x19, 3.65),
            "c_Pb": (0x61, 0x68, None, 0x1A, 2.4),
            "c_CCCV": (0x71, 0x78, None, 0x1B, 5.0),
            "d_CC": (0x01, 0x08, 0x07, 0x14, None),
            "d_CP": (0x11, 0x18, None, 0x15, None),
            "d_CR": (0x01, 0x08, 0x07, 0xFF, None),
        }
        for command, values in expected.items():
            mode = OPERATING_MODES[command]
            self.assertEqual(
                (
                    mode.start_opcode,
                    mode.cont_opcode,
                    mode.adjust_opcode,
                    mode.auto_off_code,
                    mode.cell_voltage_volts,
                ),
                values,
            )

    def test_empty_packet_selects_source_opcode_and_valid_checksum(self) -> None:
        packet = NIMH_CHARGE.empty_packet(EbcModePhase.START)
        self.assertEqual(packet.hex().upper(), "FA2100000000000021F8")
        self.assertEqual(packet[8], checksum(packet, 8))
        self.assertEqual(CONSTANT_CURRENT_DISCHARGE.empty_packet("adjust")[1], 0x07)


class OperatingModePacketTests(unittest.TestCase):
    def test_generic_mode_command_builds_the_standalone_packet(self) -> None:
        command = EbcModeCommand(
            mode=CONSTANT_POWER_DISCHARGE,
            power_watts=10,
            cutoff_volts=10.5,
            timeout_minutes=60,
        )
        self.assertEqual(
            build_mode_packet(command).hex().upper(),
            "FA11000A045A003C79F8",
        )

    def test_generic_constant_resistance_command_can_update_observed_voltage(self) -> None:
        command = EbcModeCommand(
            mode=CONSTANT_RESISTANCE_DISCHARGE,
            resistance_ohms=10,
            cutoff_volts=10.5,
            timeout_minutes=60,
            observed_voltage_volts=12,
        )
        adjusted = command.with_observed_voltage(11, phase=EbcModePhase.ADJUST)
        self.assertEqual(build_mode_packet(command)[2:4], bytes((0, 120)))
        self.assertEqual(build_mode_packet(adjusted)[1], 0x07)

    def test_cell_charge_modes_use_current_and_cell_count_fields(self) -> None:
        self.assertEqual(
            build_cell_charge_packet(NIMH_CHARGE, current_amps=0.75, cells=4, timeout_minutes=180)
            .hex()
            .upper(),
            "FA21004B000400B4DAF8",
        )
        self.assertEqual(
            build_cell_charge_packet(NICD_CHARGE, current_amps=0.5, cells=3, timeout_minutes=60)
            .hex()
            .upper(),
            "FA3100320003003C3CF8",
        )
        self.assertEqual(
            build_cell_charge_packet(LIPO_CHARGE, current_amps=1.0, cells=2, timeout_minutes=90)
            .hex()
            .upper(),
            "FA4100640002005A7DF8",
        )
        self.assertEqual(
            build_cell_charge_packet(LIFE_CHARGE, current_amps=0.4, cells=4, timeout_minutes=120)
            .hex()
            .upper(),
            "FA5100280004007805F8",
        )

    def test_cell_charge_builder_supports_cont_packet_and_model_scaling(self) -> None:
        packet = build_cell_charge_packet(
            NIMH_CHARGE,
            current_amps=0.7,
            cells=6,
            timeout_minutes=720,
            phase=EbcModePhase.CONT,
            current_factor=10,
        )
        self.assertEqual(packet.hex().upper(), "FA2802DC0006030003F8")

    def test_cccv_builder_matches_existing_protocol_layout(self) -> None:
        packet = build_cccv_charge_packet(
            CCCV_CHARGE,
            current_amps=0.5,
            voltage_volts=13.7,
            timeout_minutes=120,
        )
        self.assertEqual(packet.hex().upper(), "FA71003205AA007894F8")

    def test_constant_power_encoding_and_packet(self) -> None:
        self.assertEqual(encode_power(10), bytes((0, 10)))
        self.assertEqual(encode_power(240), bytes((1, 0)))
        packet = build_discharge_packet(
            CONSTANT_POWER_DISCHARGE,
            value=10,
            cutoff_volts=10.5,
            timeout_minutes=60,
        )
        self.assertEqual(packet.hex().upper(), "FA11000A045A003C79F8")
        self.assertEqual(
            build_discharge_packet(
                CONSTANT_POWER_DISCHARGE,
                value=10,
                cutoff_volts=10.5,
                timeout_minutes=60,
                phase="cont",
            )[1],
            0x18,
        )

    def test_constant_resistance_recomputes_current_from_observed_voltage(self) -> None:
        packet = build_constant_resistance_packet(
            CONSTANT_RESISTANCE_DISCHARGE,
            resistance_ohms=10,
            observed_voltage_volts=12,
            cutoff_volts=10.5,
            timeout_minutes=60,
        )
        self.assertEqual(packet.hex().upper(), "FA010078045A003C1BF8")
        adjusted = build_constant_resistance_packet(
            CONSTANT_RESISTANCE_DISCHARGE,
            resistance_ohms=10,
            observed_voltage_volts=11,
            cutoff_volts=10.5,
            timeout_minutes=60,
            phase=EbcModePhase.ADJUST,
        )
        self.assertEqual(adjusted[1], 0x07)
        self.assertEqual(adjusted[2:4], bytes((0, 110)))


class OperatingModeValidationTests(unittest.TestCase):
    def test_builder_rejects_wrong_mode_kind(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "not a CCCV"):
            build_cccv_charge_packet(
                NIMH_CHARGE,
                current_amps=0.5,
                voltage_volts=4.2,
                timeout_minutes=60,
            )
        with self.assertRaisesRegex(ProtocolError, "not a direct discharge"):
            build_discharge_packet(
                CONSTANT_RESISTANCE_DISCHARGE,
                value=10,
                cutoff_volts=10.5,
                timeout_minutes=60,
            )

    def test_builder_rejects_unsupported_phase_and_unsafe_parameters(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "no adjust"):
            build_cell_charge_packet(
                NIMH_CHARGE,
                current_amps=0.5,
                cells=4,
                timeout_minutes=60,
                phase=EbcModePhase.ADJUST,
            )
        invalid_calls = (
            lambda: build_cell_charge_packet(
                NIMH_CHARGE, current_amps=0.5, cells=0, timeout_minutes=60
            ),
            lambda: build_cell_charge_packet(
                NIMH_CHARGE, current_amps=0.5, cells=True, timeout_minutes=60
            ),
            lambda: build_discharge_packet(
                CONSTANT_POWER_DISCHARGE, value=121, cutoff_volts=10.5, timeout_minutes=60
            ),
            lambda: build_constant_resistance_packet(
                CONSTANT_RESISTANCE_DISCHARGE,
                resistance_ohms=0,
                observed_voltage_volts=12,
                cutoff_volts=10.5,
                timeout_minutes=60,
            ),
        )
        for call in invalid_calls:
            with self.assertRaises(ProtocolError):
                call()

    def test_mode_metadata_rejects_inconsistent_value_kinds(self) -> None:
        with self.assertRaisesRegex(ValueError, "power value kind"):
            EbcOperatingMode(
                command="d_test",
                name="test",
                kind=EbcModeKind.CONSTANT_POWER_DISCHARGE,
                start_opcode=1,
                cont_opcode=2,
                value=EbcModeValue.CURRENT,
            )


if __name__ == "__main__":
    unittest.main()
