import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from ebc_cli.capabilities import A10H_CAPABILITIES, EbcCapabilities
from ebc_cli.device import DeviceError, EbcDevice
from ebc_cli.modes import (
    CCCV_CHARGE,
    CONSTANT_CURRENT_DISCHARGE,
    LEAD_ACID_CHARGE,
    EbcModeCommand,
    build_mode_packet,
)
from ebc_cli.protocol import (
    A05_MODEL,
    A10H_MODEL,
    CONNECT,
    DISCONNECT,
    EBC_A05_MODEL_ID,
    EBC_A10H_MODEL_ID,
    FRAME_PREFIX,
    FRAME_SUFFIX,
    MODEL_REGISTRY,
    STATUS_CHECKSUM_INDEX,
    STOP,
    EbcModel,
    EbcSerialSettings,
    ProtocolError,
    checksum,
    get_model,
    parse_status,
    register_model,
)


class ModelRegistryTests(unittest.TestCase):
    def test_a05_source_definition_is_registered_read_only(self) -> None:
        model = get_model(EBC_A05_MODEL_ID)

        self.assertIs(model, A05_MODEL)
        self.assertEqual(model.name, "EBC-A05")
        self.assertEqual(model.serial, EbcSerialSettings(9600, 8, "even", 1))
        self.assertEqual((model.current_factor, model.voltage_factor), (10.0, 1.0))
        self.assertEqual(model.connect_packet, CONNECT)
        self.assertEqual(model.stop_packet, STOP)
        self.assertEqual(model.disconnect_packet, DISCONNECT)
        self.assertFalse(model.supports_control)

    def test_a10h_registry_contains_the_validated_wire_behavior(self) -> None:
        model = get_model(EBC_A10H_MODEL_ID)

        self.assertIs(model, A10H_MODEL)
        self.assertIs(model, MODEL_REGISTRY[EBC_A10H_MODEL_ID])
        self.assertEqual(model.name, "EBC-A10H")
        self.assertEqual(model.serial, EbcSerialSettings(9600, 8, "even", 1))
        self.assertEqual((model.current_factor, model.voltage_factor), (1.0, 1.0))
        self.assertIs(model.capabilities, A10H_CAPABILITIES)
        self.assertEqual(model.connect_packet, CONNECT)
        self.assertEqual(model.stop_packet, STOP)
        self.assertEqual(model.disconnect_packet, DISCONNECT)
        self.assertTrue(model.supports_control)

    def test_a10h_modes_use_the_registered_envelope_and_packet_layout(self) -> None:
        cases = (
            (
                EbcModeCommand(LEAD_ACID_CHARGE, current_amps=0.7, cells=6, timeout_minutes=720),
                "FA6100460006030022F8",
            ),
            (
                EbcModeCommand(
                    CCCV_CHARGE, current_amps=0.5, voltage_volts=13.7, timeout_minutes=120
                ),
                "FA71003205AA007894F8",
            ),
            (
                EbcModeCommand(
                    CONSTANT_CURRENT_DISCHARGE,
                    current_amps=0.8,
                    cutoff_volts=10.5,
                    timeout_minutes=1440,
                ),
                "FA010050045A060009F8",
            ),
        )
        for command, expected in cases:
            with self.subTest(mode=command.mode.command):
                command.validate_live(A10H_MODEL.capabilities)
                packet = build_mode_packet(
                    command,
                    capabilities=A10H_MODEL.capabilities,
                    current_factor=A10H_MODEL.current_factor,
                    voltage_factor=A10H_MODEL.voltage_factor,
                )
                self.assertEqual(packet.hex().upper(), expected)

    def test_unregistered_model_is_not_controlled_by_default(self) -> None:
        self.assertIsNone(get_model(0x99))
        model = EbcModel(model_id=0x99, name="unknown", serial=EbcSerialSettings())
        self.assertFalse(model.supports_control)
        self.assertIsNone(model.capabilities)

    def test_read_only_registered_model_cannot_start_a_mode(self) -> None:
        device = EbcDevice(model=A05_MODEL)
        device._fd = 42
        device._write_all = Mock()
        with self.assertRaises(DeviceError):
            device.begin_mode(EbcModeCommand(CCCV_CHARGE, current_amps=0.5, voltage_volts=13.7))
        device._write_all.assert_not_called()
        device._fd = None

    def test_controllable_model_requires_a_capability_envelope(self) -> None:
        with self.assertRaisesRegex(ValueError, "must provide capabilities"):
            EbcModel(
                model_id=0x97,
                name="test-control",
                serial=EbcSerialSettings(),
                supports_control=True,
            )

    def test_device_uses_its_registered_model_capabilities_before_io(self) -> None:
        model = replace(A10H_MODEL, capabilities=EbcCapabilities(charge_current_max_amps=0.10))
        with patch.dict(MODEL_REGISTRY, {model.model_id: model}):
            device = EbcDevice(model=model)
            device._fd = 42
            device._write_all = Mock()
            device._read_status_frame = Mock()
            with self.assertRaisesRegex(
                ProtocolError, "charge current must be between 0.05 and 0.1 A"
            ):
                device.begin_mode(
                    EbcModeCommand(
                        CCCV_CHARGE, current_amps=0.2, voltage_volts=13.7, timeout_minutes=10
                    )
                )
            device._write_all.assert_not_called()
            device._read_status_frame.assert_not_called()
            device._fd = None

    def test_registry_rejects_duplicate_model_ids_without_explicit_replace(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "already registered"):
            register_model(A10H_MODEL)

    def test_device_rejects_an_unregistered_model_instance(self) -> None:
        model = EbcModel(model_id=0x05, name="test-model", serial=EbcSerialSettings())

        with self.assertRaisesRegex(DeviceError, "not registered"):
            EbcDevice(model=model)

    def test_registered_model_scaling_is_used_by_status_parser(self) -> None:
        model = EbcModel(
            model_id=0x98,
            name="test-model",
            serial=EbcSerialSettings(),
            current_factor=2.0,
            voltage_factor=4.0,
        )
        register_model(model)
        try:
            status = parse_status(status_frame(model_id=model.model_id))
        finally:
            MODEL_REGISTRY.pop(model.model_id)

        self.assertAlmostEqual(status.current_amps, 0.35)
        self.assertAlmostEqual(status.voltage_volts, 3.0)

    def test_a05_source_scaling_is_used_by_status_parser(self) -> None:
        status = parse_status(status_frame(model_id=EBC_A05_MODEL_ID))

        self.assertEqual(status.model_name, "EBC-A05")
        self.assertAlmostEqual(status.current_amps, 0.07)
        self.assertAlmostEqual(status.voltage_volts, 12.0)


def status_frame(*, model_id: int) -> bytes:
    frame = bytearray(19)
    frame[0] = FRAME_PREFIX
    frame[2:4] = bytes((0, 70))
    frame[4:6] = bytes((50, 0))
    frame[6:8] = bytes((0, 100))
    frame[16] = model_id
    frame[STATUS_CHECKSUM_INDEX] = checksum(frame, STATUS_CHECKSUM_INDEX)
    frame[-1] = FRAME_SUFFIX
    return bytes(frame)


if __name__ == "__main__":
    unittest.main()
