import unittest

from ebc_cli.device import DeviceError, EbcDevice
from ebc_cli.protocol import (
    A10H_MODEL,
    CONNECT,
    DISCONNECT,
    EBC_A10H_MODEL_ID,
    FRAME_PREFIX,
    FRAME_SUFFIX,
    MODEL_NAMES,
    MODEL_REGISTRY,
    STATUS_CHECKSUM_INDEX,
    STOP,
    EbcModel,
    EbcProfile,
    EbcSerialSettings,
    ProtocolError,
    checksum,
    get_model,
    make_cccv_charge_plan,
    make_constant_current_discharge_plan,
    make_lead_acid_charge_plan,
    parse_status,
    register_model,
)


class ModelRegistryTests(unittest.TestCase):
    def test_a10h_registry_contains_the_validated_wire_behavior(self) -> None:
        model = get_model(EBC_A10H_MODEL_ID)

        self.assertIs(model, A10H_MODEL)
        self.assertIs(model, MODEL_REGISTRY[EBC_A10H_MODEL_ID])
        self.assertEqual(model.name, "EBC-A10H")
        self.assertEqual(model.serial, EbcSerialSettings(9600, 8, "even", 1))
        self.assertEqual((model.current_factor, model.voltage_factor), (1.0, 1.0))
        self.assertEqual(model.connect_packet, CONNECT)
        self.assertEqual(model.stop_packet, STOP)
        self.assertEqual(model.disconnect_packet, DISCONNECT)
        self.assertTrue(model.supports_control)

    def test_a10h_registry_builds_all_existing_profile_packets(self) -> None:
        self.assertEqual(
            A10H_MODEL.build_profile(
                EbcProfile.LEAD_ACID_CHARGE,
                current_amps=0.7,
                cells=6,
                timeout_minutes=720,
            ),
            make_lead_acid_charge_plan(current_amps=0.7, cells=6, timeout_minutes=720),
        )
        self.assertEqual(
            A10H_MODEL.build_profile(
                EbcProfile.CCCV_CHARGE,
                current_amps=0.5,
                voltage_volts=13.7,
                timeout_minutes=120,
            ),
            make_cccv_charge_plan(current_amps=0.5, voltage_volts=13.7, timeout_minutes=120),
        )
        self.assertEqual(
            A10H_MODEL.build_profile(
                EbcProfile.CONSTANT_CURRENT_DISCHARGE,
                current_amps=0.8,
                cutoff_volts=10.5,
                timeout_minutes=1440,
            ),
            make_constant_current_discharge_plan(
                current_amps=0.8, cutoff_volts=10.5, timeout_minutes=1440
            ),
        )

    def test_unregistered_model_is_not_controlled_by_default(self) -> None:
        self.assertIsNone(get_model(0x05))

        read_only_model = EbcModel(
            model_id=0x05,
            name="EBC-A05",
            serial=EbcSerialSettings(),
        )
        with self.assertRaisesRegex(ProtocolError, "does not support control"):
            read_only_model.build_profile(EbcProfile.CCCV_CHARGE)

    def test_registry_rejects_duplicate_model_ids_without_explicit_replace(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "already registered"):
            register_model(A10H_MODEL)

    def test_device_rejects_an_unregistered_model_instance(self) -> None:
        model = EbcModel(model_id=0x05, name="test-model", serial=EbcSerialSettings())

        with self.assertRaisesRegex(DeviceError, "not registered"):
            EbcDevice(model=model)

    def test_registered_model_scaling_is_used_by_status_parser(self) -> None:
        model = EbcModel(
            model_id=0x05,
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
            MODEL_NAMES.pop(model.model_id, None)

        self.assertEqual(status.current_amps, 0.35)
        self.assertEqual(status.voltage_volts, 3.0)


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
