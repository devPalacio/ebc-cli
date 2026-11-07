import unittest
from dataclasses import replace

from ebc_cli.capabilities import A10H_CAPABILITIES
from ebc_cli.modes import (
    CONSTANT_RESISTANCE_DISCHARGE,
    EbcModeCommand,
    EbcModePhase,
    build_mode_packet,
)
from ebc_cli.protocol import (
    A05_MODEL,
    ProtocolError,
    decode_current,
    encode_scaled,
    finalize_command,
)


class ScaledEncodingRegressionTests(unittest.TestCase):
    def test_exact_centisteps_through_30_volts(self) -> None:
        for centisteps in range(5, 3001):
            with self.subTest(centisteps=centisteps):
                self.assertEqual(
                    encode_scaled(centisteps / 100),
                    bytes(divmod(centisteps, 240)),
                )

    def test_exact_centisteps_with_model_current_factor(self) -> None:
        self.assertEqual(A05_MODEL.current_factor, 10.0)
        for centisteps in range(5, 3001):
            with self.subTest(centisteps=centisteps):
                self.assertEqual(
                    encode_scaled(centisteps / 100, A05_MODEL.current_factor),
                    bytes(divmod(centisteps * 10, 240)),
                )

    def test_sub_cent_values_are_truncated_not_rounded(self) -> None:
        for value, factor, expected in (
            (0.00999, 1.0, bytes((0, 0))),
            (1.2891, 1.0, bytes((0, 128))),
            (2.01999, 1.0, bytes((0, 201))),
            (0.12899, 10.0, bytes((0, 128))),
            (1.2891, 0.1, bytes((0, 12))),
        ):
            with self.subTest(value=value, factor=factor):
                self.assertEqual(encode_scaled(value, factor), expected)

    def test_nonfinite_values_and_factors_are_rejected(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ProtocolError, "must be finite"):
                    encode_scaled(value)
                with self.assertRaisesRegex(ProtocolError, "must be finite"):
                    encode_scaled(1.0, value)

    def test_nonnegative_value_and_positive_factor_guards_remain(self) -> None:
        for value, factor in ((-0.01, 1.0), (1.0, 0.0), (1.0, -1.0)):
            with (
                self.subTest(value=value, factor=factor),
                self.assertRaisesRegex(ProtocolError, "non-negative.*positive"),
            ):
                encode_scaled(value, factor)

    def test_two_byte_limit_remains(self) -> None:
        self.assertEqual(encode_scaled(614.39), bytes((255, 239)))
        for value, factor in ((614.4, 1.0), (61.44, 10.0), (1e308, 1e308)):
            with (
                self.subTest(value=value, factor=factor),
                self.assertRaisesRegex(ProtocolError, "two-byte EBC range"),
            ):
                encode_scaled(value, factor)


class ConstantResistanceRegressionTests(unittest.TestCase):
    def command(self, observed_voltage: float, resistance: float = 10.0) -> EbcModeCommand:
        return EbcModeCommand(
            mode=CONSTANT_RESISTANCE_DISCHARGE,
            resistance_ohms=resistance,
            observed_voltage_volts=observed_voltage,
            cutoff_volts=10.5,
            timeout_minutes=60,
        )

    def test_ordinary_sample_and_subsequent_adjustment_use_wire_current(self) -> None:
        command = self.command(12.891)
        packet = build_mode_packet(command, capabilities=A10H_CAPABILITIES)
        self.assertEqual(packet[1], 0x01)
        self.assertEqual(packet[2:4], bytes((0, 128)))
        self.assertEqual(decode_current(packet[2:4]), 1.28)

        adjustment = command.with_observed_voltage(12.711, phase=EbcModePhase.ADJUST)
        packet = build_mode_packet(adjustment, capabilities=A10H_CAPABILITIES)
        self.assertEqual(packet[1], 0x07)
        self.assertEqual(packet[2:4], bytes((0, 127)))
        self.assertEqual(decode_current(packet[2:4]), 1.27)

    def test_unquantized_current_outside_envelope_is_rejected(self) -> None:
        for current in (0.04999, 10.001):
            for phase in (EbcModePhase.START, EbcModePhase.ADJUST):
                with self.subTest(current=current, phase=phase):
                    command = replace(self.command(current, resistance=1.0), phase=phase)
                    with self.assertRaisesRegex(
                        ProtocolError, "discharge current must be between 0.05 and 10 A"
                    ):
                        build_mode_packet(command, capabilities=A10H_CAPABILITIES)

    def test_exact_current_envelope_boundaries_are_accepted(self) -> None:
        for current in (0.05, 10.0):
            with self.subTest(current=current):
                packet = build_mode_packet(
                    self.command(current, resistance=1.0),
                    capabilities=A10H_CAPABILITIES,
                )
                self.assertEqual(decode_current(packet[2:4]), current)

    def test_model_factor_is_used_when_validating_wire_current(self) -> None:
        packet = build_mode_packet(
            self.command(12.809),
            current_factor=A05_MODEL.current_factor,
            capabilities=A10H_CAPABILITIES,
        )
        self.assertEqual(decode_current(packet[2:4], A05_MODEL.current_factor), 1.28)

    def test_quantized_current_must_still_meet_capability_resolution(self) -> None:
        for factor, capabilities in (
            (A05_MODEL.current_factor, A10H_CAPABILITIES),
            (1.0, replace(A10H_CAPABILITIES, current_step_amps=0.1)),
        ):
            with (
                self.subTest(factor=factor, step=capabilities.current_step_amps),
                self.assertRaisesRegex(ProtocolError, "discharge current must use"),
            ):
                build_mode_packet(
                    self.command(12.891),
                    current_factor=factor,
                    capabilities=capabilities,
                )

    def test_no_capability_planning_preserves_packet_bytes(self) -> None:
        for phase, opcode in ((EbcModePhase.START, 0x01), (EbcModePhase.ADJUST, 0x07)):
            with self.subTest(phase=phase):
                packet = build_mode_packet(replace(self.command(12.891), phase=phase))
                expected = bytes((0xFA, opcode, 0, 128, 4, 90, 0, 60, 0, 0xF8))
                self.assertEqual(packet, finalize_command(expected))
        packet = build_mode_packet(self.command(10.001, resistance=1.0))
        self.assertEqual(decode_current(packet[2:4]), 10.0)


if __name__ == "__main__":
    unittest.main()
