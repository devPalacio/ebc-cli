import unittest
from contextlib import redirect_stdout
from io import StringIO

from ebc_cli.cli import main
from ebc_cli.commands import _mode_command_from_args, handle_list_modes
from ebc_cli.modes import (
    LOOP_CONTROL,
    SOURCE_OPERATING_MODES,
    WAIT_CONTROL,
    EbcModeKind,
    EbcModePhase,
    OperatingModeType,
    get_operating_mode,
)
from ebc_cli.parser import build_parser
from ebc_cli.protocol import ProtocolError


class OperatingModeCatalogTests(unittest.TestCase):
    def test_catalog_matches_all_source_sections(self) -> None:
        self.assertEqual(
            [mode.section for mode in SOURCE_OPERATING_MODES],
            [1, 2, 4, 5, 6, 7, 9, 10, 11, 12, 13],
        )
        self.assertEqual(
            [mode.command for mode in SOURCE_OPERATING_MODES],
            [
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
            ],
        )

    def test_source_metadata_preserves_templates_and_mode_types(self) -> None:
        nimh = get_operating_mode("C_NIMH")
        self.assertIsNotNone(nimh)
        self.assertEqual(nimh.mode_type, OperatingModeType.CHARGE)
        self.assertEqual(nimh.test_value, "I")
        self.assertEqual(
            nimh.source_frame_templates,
            (
                "FA 21 00 00 00 00 00 00 00 F8",
                "FA 28 00 00 00 00 00 00 00 F8",
            ),
        )

        constant_resistance = get_operating_mode("d_CR")
        self.assertIsNotNone(constant_resistance)
        self.assertEqual(constant_resistance.test_value, "R")
        self.assertEqual(constant_resistance.auto_off_code, 0xFF)

    def test_program_controls_have_no_wire_template(self) -> None:
        self.assertIs(get_operating_mode("wait"), WAIT_CONTROL)
        self.assertIs(get_operating_mode("LOOP"), LOOP_CONTROL)
        self.assertEqual(WAIT_CONTROL.kind, EbcModeKind.PROGRAM_CONTROL)
        self.assertEqual(WAIT_CONTROL.source_frame_templates, ())
        with self.assertRaisesRegex(ValueError, "wire opcodes"):
            type(WAIT_CONTROL)(
                section=12,
                command="Wait",
                name="Wait",
                kind=EbcModeKind.PROGRAM_CONTROL,
                start_opcode=0x01,
            )

    def test_device_modes_are_marked_control_ready(self) -> None:
        for command in (
            "c_NiMH",
            "c_NiCd",
            "c_LiPo",
            "c_LiFe",
            "c_Pb",
            "c_CCCV",
            "d_CC",
            "d_CP",
            "d_CR",
        ):
            mode = get_operating_mode(command)
            self.assertIsNotNone(mode)
            self.assertTrue(mode.control_ready)
            self.assertIn("0x06", mode.control_summary)

    def test_modes_command_and_alias_are_read_only(self) -> None:
        parser = build_parser()
        self.assertEqual(parser.parse_args(["modes"]).command, "modes")
        self.assertEqual(parser.parse_args(["list-modes"]).command, "list-modes")

        output = StringIO()
        with redirect_stdout(output):
            result = handle_list_modes(parser.parse_args(["modes"]))
        self.assertEqual(result, 0)
        rendered = output.getvalue()
        self.assertIn("c_NiMH", rendered)
        self.assertIn("d_CP", rendered)
        self.assertIn("control not enabled", rendered)
        self.assertIn("control enabled for 0x06", rendered)
        self.assertIn("No connection opened", rendered)

    def test_plan_mode_builds_a_read_only_chemistry_charge_packet(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            result = main(
                [
                    "plan-mode",
                    "c_NiMH",
                    "--current",
                    "0.75",
                    "--cells",
                    "4",
                    "--timeout-minutes",
                    "180",
                ]
            )

        self.assertEqual(result, 0)
        rendered = output.getvalue()
        self.assertIn("PLAN ONLY", rendered)
        self.assertIn("FA21004B000400B4DAF8", rendered)
        self.assertIn("no command was sent", rendered)

    def test_plan_mode_builds_a_read_only_constant_power_packet(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            result = main(
                [
                    "plan-mode",
                    "d_CP",
                    "--power",
                    "10",
                    "--cutoff-voltage",
                    "10.5",
                    "--timeout-minutes",
                    "60",
                ]
            )

        self.assertEqual(result, 0)
        self.assertIn("FA11000A045A003C79F8", output.getvalue())

    def test_plan_mode_rejects_program_controls_and_missing_parameters(self) -> None:
        self.assertEqual(main(["plan-mode", "Wait"]), 2)
        self.assertEqual(main(["plan-mode", "c_NiMH", "--cells", "4"]), 2)

    def test_run_mode_parser_keeps_cr_voltage_for_the_live_baseline(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "run-mode",
                "d_CR",
                "--resistance",
                "10",
                "--cutoff-voltage",
                "10.5",
                "--execute",
                "--i-am-present",
            ]
        )

        command = _mode_command_from_args(
            args,
            phase=EbcModePhase.START,
            require_observed=False,
        )

        self.assertEqual(command.mode.command, "d_CR")
        self.assertIsNone(command.observed_voltage_volts)
        self.assertTrue(args.execute)
        self.assertTrue(args.i_am_present)

    def test_program_controls_cannot_build_wire_packets(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "no start packet"):
            WAIT_CONTROL.opcode_for(EbcModePhase.START)


if __name__ == "__main__":
    unittest.main()
