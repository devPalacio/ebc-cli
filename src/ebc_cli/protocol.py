"""Source-compatible framing and measurement conversion for EBC testers.

The EBC-controller Lazarus source uses a nine-byte command frame and a
nineteen-byte status frame. Checksums are XORs over the bytes after the frame
prefix, excluding the checksum byte itself.
"""

import math
from dataclasses import dataclass
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
MODEL_NAMES = {EBC_A10H_MODEL_ID: "EBC-A10H"}


class EbcCompletionReason(StrEnum):
    """Completion reasons confirmed from the A10H status stream."""

    CAPACITY_CUTOFF = "capacity-cutoff"


_COMPLETION_REASONS = {0x14: EbcCompletionReason.CAPACITY_CUTOFF}


class ProtocolError(ValueError):
    """The tester sent an incomplete or invalid protocol frame."""


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
        return MODEL_NAMES.get(self.model_id, f"unknown (0x{self.model_id:02X})")

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
    frame: bytes, *, current_factor: float = 1.0, voltage_factor: float = 1.0
) -> EbcStatus:
    """Validate and decode a status response.

    EBC-A10H uses factors of one for both current and voltage. Other model
    factors can be supplied explicitly once their model is known.
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
