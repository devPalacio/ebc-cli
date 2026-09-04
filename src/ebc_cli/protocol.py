"""Source-compatible framing and measurement conversion for EBC testers.

The EBC-controller Lazarus source uses a nine-byte command frame and a
nineteen-byte status frame. Checksums are XORs over the bytes after the frame
prefix, excluding the checksum byte itself.
"""

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from functools import reduce
from operator import xor

COMMAND_LENGTH = 10
STATUS_LENGTH = 19
FRAME_PREFIX = 0xFA
FRAME_SUFFIX = 0xF8
STATUS_CHECKSUM_INDEX = 17
COMMAND_CHECKSUM_INDEX = 8

CONNECT = bytes.fromhex("FA0500000000000005F8")
DISCONNECT = bytes.fromhex("FA0600000000000006F8")
STOP = bytes.fromhex("FA0200000000000002F8")
PB_AC_START = bytes.fromhex("FA6100000000000000F8")
CCCV_START = bytes.fromhex("FA7100000000000000F8")
CC_DISCHARGE_START = bytes.fromhex("FA0100000000000000F8")

EBC_A10H_MODEL_ID = 0x06


class EbcCompletionReason(StrEnum):
    """Completion reasons confirmed from the A10H status stream."""

    CAPACITY_CUTOFF = "capacity-cutoff"


_COMPLETION_REASONS = {0x14: EbcCompletionReason.CAPACITY_CUTOFF}


class ProtocolError(ValueError):
    """The tester sent an incomplete or invalid protocol frame."""


class EbcProfile(StrEnum):
    """Profile packet families understood by a registered tester model."""

    LEAD_ACID_CHARGE = "lead-acid-charge"
    CCCV_CHARGE = "cccv-charge"
    CONSTANT_CURRENT_DISCHARGE = "constant-current-discharge"


@dataclass(frozen=True, slots=True)
class EbcSerialSettings:
    """Serial line settings required by one EBC model."""

    baudrate: int = 9600
    data_bits: int = 8
    parity: str = "even"
    stop_bits: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.baudrate, bool) or not isinstance(self.baudrate, int):
            raise TypeError("baudrate must be an integer")
        if self.baudrate <= 0:
            raise ValueError("baudrate must be positive")
        if isinstance(self.data_bits, bool) or not isinstance(self.data_bits, int):
            raise TypeError("data_bits must be an integer")
        if self.data_bits not in (5, 6, 7, 8):
            raise ValueError("data_bits must be 5, 6, 7, or 8")
        if not isinstance(self.parity, str) or self.parity.casefold() not in {
            "none",
            "even",
            "odd",
        }:
            raise ValueError("parity must be none, even, or odd")
        if isinstance(self.stop_bits, bool) or not isinstance(self.stop_bits, int):
            raise TypeError("stop_bits must be an integer")
        if self.stop_bits not in (1, 2):
            raise ValueError("stop_bits must be 1 or 2")


@dataclass(frozen=True, slots=True)
class EbcModel:
    """Model-specific protocol behavior held by the tester registry."""

    model_id: int
    name: str
    serial: EbcSerialSettings
    current_factor: float = 1.0
    voltage_factor: float = 1.0
    connect_packet: bytes = CONNECT
    stop_packet: bytes = STOP
    disconnect_packet: bytes = DISCONNECT
    supports_control: bool = False
    profile_builders: Mapping[EbcProfile, Callable[..., bytes]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.model_id, bool) or not isinstance(self.model_id, int):
            raise TypeError("model_id must be an integer")
        if not 0 <= self.model_id <= 0xFF:
            raise ValueError("model_id must fit in one byte")
        if not isinstance(self.name, str):
            raise TypeError("model name must be a string")
        if not self.name:
            raise ValueError("model name must not be empty")
        for label, factor in (
            ("current_factor", self.current_factor),
            ("voltage_factor", self.voltage_factor),
        ):
            try:
                numeric_factor = float(factor)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"{label} must be numeric") from exc
            if not math.isfinite(numeric_factor) or numeric_factor <= 0:
                raise ValueError(f"{label} must be finite and positive")
            object.__setattr__(self, label, numeric_factor)
        for label, packet in (
            ("connect_packet", self.connect_packet),
            ("stop_packet", self.stop_packet),
            ("disconnect_packet", self.disconnect_packet),
        ):
            if not isinstance(packet, (bytes, bytearray)):
                raise TypeError(f"{label} must be bytes")
            packet = bytes(packet)
            object.__setattr__(self, label, packet)
            if len(packet) != COMMAND_LENGTH:
                raise ValueError(f"{label} must be a {COMMAND_LENGTH}-byte command")
            if packet[0] != FRAME_PREFIX or packet[-1] != FRAME_SUFFIX:
                raise ValueError(f"{label} must start with FA and end with F8")
            if packet[COMMAND_CHECKSUM_INDEX] != checksum(packet, COMMAND_CHECKSUM_INDEX):
                raise ValueError(f"{label} has an invalid checksum")
        if self.supports_control and not self.profile_builders:
            raise ValueError("a controllable model must provide profile builders")

    def build_profile(self, profile: EbcProfile | str, **kwargs: object) -> bytes:
        """Build a packet using this model's registered profile behavior."""

        try:
            profile_kind = EbcProfile(profile)
        except ValueError as exc:
            raise ProtocolError(f"unsupported profile: {profile!r}") from exc
        if not self.supports_control:
            raise ProtocolError(f"model {self.name} does not support control profiles")
        builder = self.profile_builders.get(profile_kind)
        if builder is None:
            raise ProtocolError(f"model {self.name} has no {profile_kind.value} packet builder")
        return builder(**kwargs)


def checksum(frame: bytes, checksum_index: int) -> int:
    """Return the EBC XOR checksum for a command or status frame.

    The first `0xFA` byte is deliberately not included; this matches the
    upstream controller's `for I := 2 to Pos - 1` loop.
    """

    if not 1 <= checksum_index <= len(frame):
        raise ProtocolError("checksum position is outside the frame")
    value = reduce(xor, frame[1:checksum_index], 0)
    # EBC firmware remaps the 0xF? range to 0x0? so the checksum cannot be
    # mistaken for a framing byte (FA = start, F8 = end). EBC-controller's
    # GUI defaults to "Ignore CRC" because its older XOR routine omits this.
    return value & 0x0F if (value & 0xF0) == 0xF0 else value


def finalize_command(frame: bytes) -> bytes:
    """Fill the checksum field of a ten-byte EBC command frame."""

    if len(frame) != COMMAND_LENGTH:
        raise ProtocolError(f"command must be {COMMAND_LENGTH} bytes, got {len(frame)}")
    if frame[0] != FRAME_PREFIX or frame[-1] != FRAME_SUFFIX:
        raise ProtocolError("command must start with FA and end with F8")
    result = bytearray(frame)
    result[COMMAND_CHECKSUM_INDEX] = checksum(result, COMMAND_CHECKSUM_INDEX)
    return bytes(result)


def encode_scaled(value: float, factor: float = 1.0) -> bytes:
    """Encode current or voltage as implemented by EBC-controller.

    The source first converts to milli-units, then represents it as
    `high * 2400 + low * 10`. Precision below 10 milli-units is truncated by
    the tester protocol.
    """

    try:
        numeric_value = float(value)
        numeric_factor = float(factor)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProtocolError("value and factor must be numeric") from exc
    if not math.isfinite(numeric_value) or not math.isfinite(numeric_factor):
        raise ProtocolError("value and factor must be finite")
    if numeric_value < 0 or numeric_factor <= 0:
        raise ProtocolError("value must be non-negative and factor must be positive")
    encoded = int(numeric_value * numeric_factor * 1000)
    high, remainder = divmod(encoded, 2400)
    low = remainder // 10
    if high > 0xFF or low > 0xFF:
        raise ProtocolError("value is outside the two-byte EBC range")
    return bytes((high, low))


def decode_current(data: bytes, factor: float = 1.0) -> float:
    """Decode the source controller's current representation in amperes."""

    numeric_factor = _validate_two_bytes(data, factor)
    return (data[0] * 2400 + data[1] * 10) / 1000 / numeric_factor


def decode_voltage(data: bytes, factor: float = 1.0) -> float:
    """Decode the source controller's voltage representation in volts."""

    numeric_factor = _validate_two_bytes(data, factor)
    return (data[0] * 2400 + data[1] * 10) / 10000 / numeric_factor


def decode_capacity(data: bytes) -> float:
    """Decode accumulated capacity in amp-hours."""

    _validate_two_bytes(data, 1.0)
    return (data[0] * 2400 + data[1] * 10) / 10000


def encode_timer(minutes: int) -> bytes:
    """Encode a timeout in minutes using the device's 240-minute high byte."""

    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise ProtocolError("timer must be an integer number of minutes")
    if not 0 <= minutes <= (0xFF * 240 + 0xFF):
        raise ProtocolError("timer is outside the EBC range")
    if minutes == 250:
        # The EBC-controller source treats 250 as a firmware-reserved value.
        minutes = 249
    return bytes(divmod(minutes, 240))


def decode_timer(data: bytes) -> int:
    """Decode a device timer value in minutes."""

    _validate_two_bytes(data, 1.0)
    return data[0] * 240 + data[1]


def make_lead_acid_charge_plan(
    *, current_amps: float, cells: int = 6, timeout_minutes: int = 720
) -> bytes:
    """Build, but do not transmit, the PbAc start frame.

    The EBC's dedicated PbAc firmware mode takes a cell count rather than an
    arbitrary voltage. The upstream configuration defines its target as
    `cells * 2.4 V`; six cells is therefore 14.4 V.
    """

    if not 1 <= cells <= 0xFF:
        raise ProtocolError("cell count must fit in one byte")
    result = bytearray(PB_AC_START)
    result[2:4] = encode_scaled(current_amps)
    result[4:6] = bytes((0, cells))
    result[6:8] = encode_timer(timeout_minutes)
    return finalize_command(result)


def make_cccv_charge_plan(
    *, current_amps: float, voltage_volts: float, timeout_minutes: int
) -> bytes:
    """Build a constant-current/constant-voltage charge start frame."""

    result = bytearray(CCCV_START)
    result[2:4] = encode_scaled(current_amps)
    result[4:6] = encode_scaled(voltage_volts)
    result[6:8] = encode_timer(timeout_minutes)
    return finalize_command(result)


def make_constant_current_discharge_plan(
    *, current_amps: float, cutoff_volts: float, timeout_minutes: int
) -> bytes:
    """Build a constant-current discharge start frame."""

    result = bytearray(CC_DISCHARGE_START)
    result[2:4] = encode_scaled(current_amps)
    result[4:6] = encode_scaled(cutoff_volts)
    result[6:8] = encode_timer(timeout_minutes)
    return finalize_command(result)


# The A10H is the only model currently validated for control. Additional
# models can be registered with their own serial/scaling/packet behavior after
# their wire protocol has been captured and reviewed.
A10H_MODEL = EbcModel(
    model_id=EBC_A10H_MODEL_ID,
    name="EBC-A10H",
    serial=EbcSerialSettings(baudrate=9600, data_bits=8, parity="even", stop_bits=1),
    current_factor=1.0,
    voltage_factor=1.0,
    connect_packet=CONNECT,
    stop_packet=STOP,
    disconnect_packet=DISCONNECT,
    supports_control=True,
    profile_builders={
        EbcProfile.LEAD_ACID_CHARGE: make_lead_acid_charge_plan,
        EbcProfile.CCCV_CHARGE: make_cccv_charge_plan,
        EbcProfile.CONSTANT_CURRENT_DISCHARGE: make_constant_current_discharge_plan,
    },
)

MODEL_REGISTRY: dict[int, EbcModel] = {A10H_MODEL.model_id: A10H_MODEL}
MODEL_NAMES: dict[int, str] = {model_id: model.name for model_id, model in MODEL_REGISTRY.items()}


def get_model(model_id: int) -> EbcModel | None:
    """Return the registered model for a wire-level model identifier."""

    return MODEL_REGISTRY.get(model_id)


def register_model(model: EbcModel, *, replace: bool = False) -> None:
    """Add a validated model to the registry for status decoding/control."""

    if model.model_id in MODEL_REGISTRY and not replace:
        raise ProtocolError(f"model ID 0x{model.model_id:02X} is already registered")
    MODEL_REGISTRY[model.model_id] = model
    MODEL_NAMES[model.model_id] = model.name


@dataclass(frozen=True)
class EbcStatus:
    """One validated 19-byte status response from an EBC tester."""

    voltage_volts: float
    current_amps: float
    capacity_ah: float
    device_timer_minutes: int
    model_id: int
    raw: bytes

    @property
    def elapsed_minutes(self) -> int:
        """Read-only alias for callers using the old timer field name.

        This is not host-side elapsed time. The tester's field has been
        observed to use values that do not match wall-clock test duration, so
        callers should use CSV timestamps for session timing.
        """

        return self.device_timer_minutes

    @property
    def model_name(self) -> str:
        model = get_model(self.model_id)
        return model.name if model is not None else f"unknown (0x{self.model_id:02X})"

    @property
    def completion_reason(self) -> EbcCompletionReason | None:
        """Return a known completion reason encoded by the status state byte."""

        if len(self.raw) <= 1:
            return None
        return _COMPLETION_REASONS.get(self.raw[1])

    @property
    def power_watts(self) -> float:
        return self.voltage_volts * self.current_amps


def parse_status(
    frame: bytes, *, current_factor: float | None = None, voltage_factor: float | None = None
) -> EbcStatus:
    """Validate and decode a status response.

    When scaling factors are omitted, the registered model's factors are used.
    Unknown model IDs remain readable with the A10H-compatible default factors;
    callers must still explicitly validate a model before sending controls.
    """

    if len(frame) != STATUS_LENGTH:
        raise ProtocolError(f"status must be {STATUS_LENGTH} bytes, got {len(frame)}")
    if frame[0] != FRAME_PREFIX:
        raise ProtocolError("status frame does not start with FA")
    if frame[-1] != FRAME_SUFFIX:
        raise ProtocolError("status frame does not end with F8")
    expected = checksum(frame, STATUS_CHECKSUM_INDEX)
    actual = frame[STATUS_CHECKSUM_INDEX]
    if actual != expected:
        raise ProtocolError(f"bad status checksum: expected 0x{expected:02X}, got 0x{actual:02X}")
    model = get_model(frame[16])
    if current_factor is None:
        current_factor = model.current_factor if model is not None else 1.0
    if voltage_factor is None:
        voltage_factor = model.voltage_factor if model is not None else 1.0
    return EbcStatus(
        current_amps=decode_current(frame[2:4], current_factor),
        voltage_volts=decode_voltage(frame[4:6], voltage_factor),
        capacity_ah=decode_capacity(frame[6:8]),
        device_timer_minutes=decode_timer(frame[14:16]),
        model_id=frame[16],
        raw=frame,
    )


def find_valid_status_frame(data: bytes) -> bytes | None:
    """Return the first checksum-valid status frame embedded in *data*.

    Some tester commands emit a non-status reply before the normal status
    frame. Serial reads need to resynchronize rather than assuming their next
    nineteen bytes are a status response.
    """

    last_start = len(data) - STATUS_LENGTH
    for start in range(max(last_start + 1, 0)):
        if data[start] != FRAME_PREFIX:
            continue
        candidate = data[start : start + STATUS_LENGTH]
        try:
            parse_status(candidate)
        except ProtocolError:
            continue
        return candidate
    return None


def _validate_two_bytes(data: bytes, factor: float) -> float:
    if len(data) != 2:
        raise ProtocolError("measurement must contain exactly two bytes")
    try:
        numeric_factor = float(factor)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProtocolError("factor must be numeric") from exc
    if not math.isfinite(numeric_factor) or numeric_factor <= 0:
        raise ProtocolError("factor must be finite and positive")
    return numeric_factor
