# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Jay (Python CLI contributions)
# See NOTICE for upstream attribution and port details.

"""Source-derived operating modes for ZKETECH EBC testers.

The Lazarus controller keeps its mode catalog in ``ebcc.init``.  This module
keeps one canonical copy of that catalog and provides typed packet builders
for the device-facing entries.  Builders only construct and validate bytes;
they never open a serial device or transmit anything.

The existing CLI has validated live A10H paths for PbAc, CCCV, and
constant-current discharge.  The other source modes are available for
read-only inspection and packet research until representative captures make
their control behavior safe to enable.
"""

import math
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from .protocol import (
    EBC_A10H_MODEL_ID,
    FRAME_PREFIX,
    FRAME_SUFFIX,
    ProtocolError,
    encode_scaled,
    encode_timer,
    finalize_command,
)


class EbcModeKind(StrEnum):
    """The parameter layout used by one source-defined operating mode."""

    CELL_CHARGE = "cell-charge"
    CCCV_CHARGE = "cccv-charge"
    CONSTANT_CURRENT_DISCHARGE = "constant-current-discharge"
    CONSTANT_POWER_DISCHARGE = "constant-power-discharge"
    SOFTWARE_CONSTANT_RESISTANCE = "software-constant-resistance"
    PROGRAM_CONTROL = "program-control"


class EbcModePhase(StrEnum):
    """A command variant defined by the source mode catalog."""

    START = "start"
    CONT = "cont"
    ADJUST = "adjust"


class EbcModeValue(StrEnum):
    """The primary value accepted by a discharge mode."""

    CURRENT = "current"
    POWER = "power"
    RESISTANCE = "resistance"


class OperatingModeType(StrEnum):
    """The source ``Type`` field retained for display and auditability."""

    CHARGE = "Charge"
    CHARGE_CV = "ChargeCV"
    DISCHARGE = "Discharge"
    CONTROL = "Control"


@dataclass(frozen=True, slots=True)
class EbcOperatingMode:
    """One mode from the upstream ``ebcc.init`` catalog.

    ``section`` is the source INI section number, not a model identifier.
    Opcode fields are the second byte of the source's ten-byte command
    templates.  ``auto_off_code`` is a status state byte, and
    ``cell_voltage_volts`` is the source GUI's informational per-cell hint.
    ``validated_model_ids`` records the narrower set of models for which this
    package has reviewed live control behavior.
    """

    command: str
    name: str
    kind: EbcModeKind
    section: int | None = None
    start_opcode: int | None = None
    cont_opcode: int | None = None
    adjust_opcode: int | None = None
    auto_off_code: int | None = None
    value: EbcModeValue | None = None
    cell_voltage_volts: float | None = None
    validated_model_ids: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.command, str) or not self.command.strip():
            raise ValueError("mode command must be a non-empty string")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("mode name must be a non-empty string")
        if not isinstance(self.kind, EbcModeKind):
            try:
                object.__setattr__(self, "kind", EbcModeKind(self.kind))
            except ValueError as exc:
                raise ValueError(f"unsupported mode kind: {self.kind!r}") from exc
        if self.section is not None and (
            isinstance(self.section, bool) or not isinstance(self.section, int) or self.section <= 0
        ):
            raise ValueError("section must be a positive integer or None")

        if self.kind is EbcModeKind.PROGRAM_CONTROL:
            if self.value is not None:
                raise ValueError("program controls do not have a discharge value")
            if any(
                opcode is not None
                for opcode in (self.start_opcode, self.cont_opcode, self.adjust_opcode)
            ):
                raise ValueError("program controls do not have wire opcodes")
        elif self.kind in {EbcModeKind.CELL_CHARGE, EbcModeKind.CCCV_CHARGE}:
            if self.value is not None:
                raise ValueError("charge modes do not have a discharge value")
            if self.start_opcode is None or self.cont_opcode is None:
                raise ValueError("device charge modes require start and continuous opcodes")
        else:
            if self.value is None:
                raise ValueError(f"{self.kind.value} requires a discharge value")
            if not isinstance(self.value, EbcModeValue):
                try:
                    object.__setattr__(self, "value", EbcModeValue(self.value))
                except ValueError as exc:
                    raise ValueError(f"unsupported mode value: {self.value!r}") from exc
            expected_value = {
                EbcModeKind.CONSTANT_CURRENT_DISCHARGE: EbcModeValue.CURRENT,
                EbcModeKind.CONSTANT_POWER_DISCHARGE: EbcModeValue.POWER,
                EbcModeKind.SOFTWARE_CONSTANT_RESISTANCE: EbcModeValue.RESISTANCE,
            }[self.kind]
            if self.value is not expected_value:
                raise ValueError(
                    f"{self.kind.value} requires the {expected_value.value} value kind"
                )
            if self.start_opcode is None or self.cont_opcode is None:
                raise ValueError("device discharge modes require start and continuous opcodes")
            if self.kind is EbcModeKind.SOFTWARE_CONSTANT_RESISTANCE and self.adjust_opcode is None:
                raise ValueError("software resistance mode requires an adjust opcode")

        for label, opcode in (
            ("start_opcode", self.start_opcode),
            ("cont_opcode", self.cont_opcode),
            ("adjust_opcode", self.adjust_opcode),
            ("auto_off_code", self.auto_off_code),
        ):
            if opcode is not None and (
                isinstance(opcode, bool) or not isinstance(opcode, int) or not 0 <= opcode <= 0xFF
            ):
                raise ValueError(f"{label} must be an integer byte or None")

        if self.cell_voltage_volts is not None:
            try:
                cell_voltage = float(self.cell_voltage_volts)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("cell voltage must be numeric") from exc
            if not math.isfinite(cell_voltage) or cell_voltage <= 0:
                raise ValueError("cell voltage must be finite and positive")
            object.__setattr__(self, "cell_voltage_volts", cell_voltage)

        model_ids = frozenset(self.validated_model_ids)
        if any(
            isinstance(model_id, bool) or not isinstance(model_id, int) or not 0 <= model_id <= 0xFF
            for model_id in model_ids
        ):
            raise ValueError("validated model IDs must be integer bytes")
        object.__setattr__(self, "validated_model_ids", model_ids)

    @property
    def mode_type(self) -> OperatingModeType:
        """Return the original source ``Type`` value."""

        return {
            EbcModeKind.CELL_CHARGE: OperatingModeType.CHARGE,
            EbcModeKind.CCCV_CHARGE: OperatingModeType.CHARGE_CV,
            EbcModeKind.CONSTANT_CURRENT_DISCHARGE: OperatingModeType.DISCHARGE,
            EbcModeKind.CONSTANT_POWER_DISCHARGE: OperatingModeType.DISCHARGE,
            EbcModeKind.SOFTWARE_CONSTANT_RESISTANCE: OperatingModeType.DISCHARGE,
            EbcModeKind.PROGRAM_CONTROL: OperatingModeType.CONTROL,
        }[self.kind]

    @property
    def test_value(self) -> str | None:
        """Return the source ``TestVal`` character, when one exists."""

        if self.kind in {EbcModeKind.CELL_CHARGE, EbcModeKind.CCCV_CHARGE}:
            return "I"
        return {
            EbcModeValue.CURRENT: "I",
            EbcModeValue.POWER: "P",
            EbcModeValue.RESISTANCE: "R",
        }.get(self.value)

    @property
    def control_ready(self) -> bool:
        """Whether any reviewed model is allowed to use this mode live."""

        return bool(self.validated_model_ids)

    def is_control_ready_for(self, model_id: int) -> bool:
        """Return whether this mode is reviewed for the given model ID."""

        return model_id in self.validated_model_ids

    @property
    def control_summary(self) -> str:
        """Explain the live-control status shown by the inventory command."""

        if not self.validated_model_ids:
            return "source metadata only; control not enabled"
        models = ", ".join(f"0x{model_id:02X}" for model_id in sorted(self.validated_model_ids))
        return f"control validated for {models}"

    @property
    def source_frame_templates(self) -> tuple[str, ...]:
        """Return source frame templates in start/adjust/continue order."""

        return tuple(
            f"FA {opcode:02X} 00 00 00 00 00 00 00 F8"
            for opcode in (self.start_opcode, self.adjust_opcode, self.cont_opcode)
            if opcode is not None
        )

    def opcode_for(self, phase: EbcModePhase | str) -> int:
        """Return the opcode for a start, continuous, or adjust packet."""

        try:
            phase_kind = EbcModePhase(phase)
        except ValueError as exc:
            raise ProtocolError(f"unsupported mode phase: {phase!r}") from exc
        opcode = {
            EbcModePhase.START: self.start_opcode,
            EbcModePhase.CONT: self.cont_opcode,
            EbcModePhase.ADJUST: self.adjust_opcode,
        }[phase_kind]
        if opcode is None:
            raise ProtocolError(f"mode {self.command} has no {phase_kind.value} packet")
        return opcode

    def empty_packet(self, phase: EbcModePhase | str = EbcModePhase.START) -> bytes:
        """Return a framed, checksum-valid packet with zero parameters."""

        frame = bytearray((FRAME_PREFIX, self.opcode_for(phase), 0, 0, 0, 0, 0, 0, 0, FRAME_SUFFIX))
        return finalize_command(frame)


def _finite_nonnegative(value: float, label: str) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProtocolError(f"{label} must be numeric") from exc
    if not math.isfinite(numeric) or numeric < 0:
        raise ProtocolError(f"{label} must be finite and non-negative")
    return numeric


def _finite_positive(value: float, label: str) -> float:
    numeric = _finite_nonnegative(value, label)
    if numeric <= 0:
        raise ProtocolError(f"{label} must be positive")
    return numeric


def _validated_factor(value: float, label: str) -> float:
    return _finite_positive(value, label)


def encode_power(power_watts: float) -> bytes:
    """Encode power using the upstream controller's two-byte formula.

    The Pascal source computes ``Round(P / 240)`` and then
    ``Trunc(P - 240 * H)``.  Strict byte validation prevents the negative low
    byte that the GUI can produce for part of its nominal range.
    """

    power = _finite_nonnegative(power_watts, "power")
    high = round(power / 240)
    low = math.trunc(power - 240 * high)
    if not 0 <= high <= 0xFF or not 0 <= low <= 0xFF:
        raise ProtocolError("power is outside the source controller's two-byte range")
    return bytes((high, low))


def _apply_payload(
    mode: EbcOperatingMode,
    phase: EbcModePhase | str,
    first: bytes,
    second: bytes,
    timeout_minutes: int,
) -> bytes:
    if len(first) != 2 or len(second) != 2:
        raise ProtocolError("mode payload fields must be two bytes")
    result = bytearray(mode.empty_packet(phase))
    result[2:4] = first
    result[4:6] = second
    result[6:8] = encode_timer(timeout_minutes)
    return finalize_command(result)


def build_cell_charge_packet(
    mode: EbcOperatingMode,
    *,
    current_amps: float,
    cells: int,
    timeout_minutes: int,
    phase: EbcModePhase | str = EbcModePhase.START,
    current_factor: float = 1.0,
) -> bytes:
    """Build a NiMH, NiCd, LiPo, LiFe, or PbAc charge packet."""

    if mode.kind is not EbcModeKind.CELL_CHARGE:
        raise ProtocolError(f"mode {mode.command} is not a cell-based charge mode")
    if isinstance(cells, bool) or not isinstance(cells, int) or not 1 <= cells <= 0xFF:
        raise ProtocolError("cell count must be an integer from 1 to 255")
    return _apply_payload(
        mode,
        phase,
        encode_scaled(current_amps, _validated_factor(current_factor, "current factor")),
        bytes((0, cells)),
        timeout_minutes,
    )


def build_cccv_charge_packet(
    mode: EbcOperatingMode,
    *,
    current_amps: float,
    voltage_volts: float,
    timeout_minutes: int,
    phase: EbcModePhase | str = EbcModePhase.START,
    current_factor: float = 1.0,
    voltage_factor: float = 1.0,
) -> bytes:
    """Build a CCCV charge packet."""

    if mode.kind is not EbcModeKind.CCCV_CHARGE:
        raise ProtocolError(f"mode {mode.command} is not a CCCV charge mode")
    return _apply_payload(
        mode,
        phase,
        encode_scaled(current_amps, _validated_factor(current_factor, "current factor")),
        encode_scaled(voltage_volts, _validated_factor(voltage_factor, "voltage factor")),
        timeout_minutes,
    )


def build_discharge_packet(
    mode: EbcOperatingMode,
    *,
    value: float,
    cutoff_volts: float,
    timeout_minutes: int,
    phase: EbcModePhase | str = EbcModePhase.START,
    current_factor: float = 1.0,
    voltage_factor: float = 1.0,
) -> bytes:
    """Build a constant-current or constant-power discharge packet."""

    if mode.kind is EbcModeKind.CONSTANT_CURRENT_DISCHARGE:
        first = encode_scaled(value, _validated_factor(current_factor, "current factor"))
    elif mode.kind is EbcModeKind.CONSTANT_POWER_DISCHARGE:
        first = encode_power(value)
    else:
        raise ProtocolError(f"mode {mode.command} is not a direct discharge mode")
    return _apply_payload(
        mode,
        phase,
        first,
        encode_scaled(cutoff_volts, _validated_factor(voltage_factor, "voltage factor")),
        timeout_minutes,
    )


def build_constant_resistance_packet(
    mode: EbcOperatingMode,
    *,
    resistance_ohms: float,
    observed_voltage_volts: float,
    cutoff_volts: float,
    timeout_minutes: int,
    phase: EbcModePhase | str = EbcModePhase.START,
    current_factor: float = 1.0,
    voltage_factor: float = 1.0,
) -> bytes:
    """Build the GUI's software constant-resistance adjustment packet.

    The tester has no distinct CR opcode.  The source computes
    ``current = observed_voltage / resistance`` and sends that current using
    the CC packet family.  A caller must supply a fresh observed voltage for
    each adjustment; this function never reads hardware itself.
    """

    if mode.kind is not EbcModeKind.SOFTWARE_CONSTANT_RESISTANCE:
        raise ProtocolError(f"mode {mode.command} is not software resistance mode")
    resistance = _finite_positive(resistance_ohms, "resistance")
    observed_voltage = _finite_nonnegative(observed_voltage_volts, "observed voltage")
    current = observed_voltage / resistance
    return _apply_payload(
        mode,
        phase,
        encode_scaled(current, _validated_factor(current_factor, "current factor")),
        encode_scaled(cutoff_volts, _validated_factor(voltage_factor, "voltage factor")),
        timeout_minutes,
    )


# These entries mirror the source's device-facing sections.  Section numbers
# are INI sections, not model IDs; Wait and Loop are host program controls and
# intentionally have no serial packet templates.
NIMH_CHARGE: Final = EbcOperatingMode(
    section=1,
    command="c_NiMH",
    name="NiMH - Nickel-metal Hydride",
    kind=EbcModeKind.CELL_CHARGE,
    start_opcode=0x21,
    cont_opcode=0x28,
    auto_off_code=0x16,
)
NICD_CHARGE: Final = EbcOperatingMode(
    section=2,
    command="c_NiCd",
    name="NiCd - Nickel-cadmium",
    kind=EbcModeKind.CELL_CHARGE,
    start_opcode=0x31,
    cont_opcode=0x38,
    auto_off_code=0x17,
)
LIPO_CHARGE: Final = EbcOperatingMode(
    section=4,
    command="c_LiPo",
    name="LiPo - Lithium Ion or Polymer",
    kind=EbcModeKind.CELL_CHARGE,
    start_opcode=0x41,
    cont_opcode=0x48,
    auto_off_code=0x18,
    cell_voltage_volts=4.2,
)
LIFE_CHARGE: Final = EbcOperatingMode(
    section=5,
    command="c_LiFe",
    name="LiFe - Lithium Iron Phosphate",
    kind=EbcModeKind.CELL_CHARGE,
    start_opcode=0x51,
    cont_opcode=0x58,
    auto_off_code=0x19,
    cell_voltage_volts=3.65,
)
LEAD_ACID_CHARGE: Final = EbcOperatingMode(
    section=6,
    command="c_Pb",
    name="PbAc - Lead Acid",
    kind=EbcModeKind.CELL_CHARGE,
    start_opcode=0x61,
    cont_opcode=0x68,
    auto_off_code=0x1A,
    cell_voltage_volts=2.4,
    validated_model_ids=frozenset({EBC_A10H_MODEL_ID}),
)
CCCV_CHARGE: Final = EbcOperatingMode(
    section=7,
    command="c_CCCV",
    name="CCCV - CC and CV charging",
    kind=EbcModeKind.CCCV_CHARGE,
    start_opcode=0x71,
    cont_opcode=0x78,
    auto_off_code=0x1B,
    cell_voltage_volts=5.0,
    validated_model_ids=frozenset({EBC_A10H_MODEL_ID}),
)
CONSTANT_CURRENT_DISCHARGE: Final = EbcOperatingMode(
    section=9,
    command="d_CC",
    name="CC - Constant Current",
    kind=EbcModeKind.CONSTANT_CURRENT_DISCHARGE,
    start_opcode=0x01,
    cont_opcode=0x08,
    adjust_opcode=0x07,
    auto_off_code=0x14,
    value=EbcModeValue.CURRENT,
    validated_model_ids=frozenset({EBC_A10H_MODEL_ID}),
)
CONSTANT_POWER_DISCHARGE: Final = EbcOperatingMode(
    section=10,
    command="d_CP",
    name="CP - Constant Power",
    kind=EbcModeKind.CONSTANT_POWER_DISCHARGE,
    start_opcode=0x11,
    cont_opcode=0x18,
    auto_off_code=0x15,
    value=EbcModeValue.POWER,
)
CONSTANT_RESISTANCE_DISCHARGE: Final = EbcOperatingMode(
    section=11,
    command="d_CR",
    name="CR - Constant Resistance (software)",
    kind=EbcModeKind.SOFTWARE_CONSTANT_RESISTANCE,
    start_opcode=0x01,
    cont_opcode=0x08,
    adjust_opcode=0x07,
    auto_off_code=0xFF,
    value=EbcModeValue.RESISTANCE,
)
WAIT_CONTROL: Final = EbcOperatingMode(
    section=12,
    command="Wait",
    name="Wait",
    kind=EbcModeKind.PROGRAM_CONTROL,
)
LOOP_CONTROL: Final = EbcOperatingMode(
    section=13,
    command="Loop",
    name="Loop",
    kind=EbcModeKind.PROGRAM_CONTROL,
)


DEVICE_OPERATING_MODES: Final[tuple[EbcOperatingMode, ...]] = (
    NIMH_CHARGE,
    NICD_CHARGE,
    LIPO_CHARGE,
    LIFE_CHARGE,
    LEAD_ACID_CHARGE,
    CCCV_CHARGE,
    CONSTANT_CURRENT_DISCHARGE,
    CONSTANT_POWER_DISCHARGE,
    CONSTANT_RESISTANCE_DISCHARGE,
)
SOURCE_OPERATING_MODES: Final[tuple[EbcOperatingMode, ...]] = (
    *DEVICE_OPERATING_MODES,
    WAIT_CONTROL,
    LOOP_CONTROL,
)
OPERATING_MODES: Final = MappingProxyType({mode.command: mode for mode in SOURCE_OPERATING_MODES})


def get_operating_mode(command: str) -> EbcOperatingMode | None:
    """Look up a source mode by command name, case-insensitively."""

    if not isinstance(command, str):
        return None
    normalized = command.casefold()
    return next(
        (mode for mode in SOURCE_OPERATING_MODES if mode.command.casefold() == normalized),
        None,
    )


def make_nimh_charge_packet(
    *,
    current_amps: float,
    cells: int,
    timeout_minutes: int,
    phase: EbcModePhase | str = EbcModePhase.START,
) -> bytes:
    """Build a source-defined NiMH packet without transmitting it."""

    return build_cell_charge_packet(
        NIMH_CHARGE,
        current_amps=current_amps,
        cells=cells,
        timeout_minutes=timeout_minutes,
        phase=phase,
    )


def make_nicd_charge_packet(
    *,
    current_amps: float,
    cells: int,
    timeout_minutes: int,
    phase: EbcModePhase | str = EbcModePhase.START,
) -> bytes:
    """Build a source-defined NiCd packet without transmitting it."""

    return build_cell_charge_packet(
        NICD_CHARGE,
        current_amps=current_amps,
        cells=cells,
        timeout_minutes=timeout_minutes,
        phase=phase,
    )


def make_lipo_charge_packet(
    *,
    current_amps: float,
    cells: int,
    timeout_minutes: int,
    phase: EbcModePhase | str = EbcModePhase.START,
) -> bytes:
    """Build a source-defined LiPo packet without transmitting it."""

    return build_cell_charge_packet(
        LIPO_CHARGE,
        current_amps=current_amps,
        cells=cells,
        timeout_minutes=timeout_minutes,
        phase=phase,
    )


def make_life_charge_packet(
    *,
    current_amps: float,
    cells: int,
    timeout_minutes: int,
    phase: EbcModePhase | str = EbcModePhase.START,
) -> bytes:
    """Build a source-defined LiFe packet without transmitting it."""

    return build_cell_charge_packet(
        LIFE_CHARGE,
        current_amps=current_amps,
        cells=cells,
        timeout_minutes=timeout_minutes,
        phase=phase,
    )


def make_lead_acid_charge_packet(
    *,
    current_amps: float,
    cells: int,
    timeout_minutes: int,
    phase: EbcModePhase | str = EbcModePhase.START,
) -> bytes:
    """Build a source-defined PbAc packet without transmitting it."""

    return build_cell_charge_packet(
        LEAD_ACID_CHARGE,
        current_amps=current_amps,
        cells=cells,
        timeout_minutes=timeout_minutes,
        phase=phase,
    )


def make_cccv_charge_packet(
    *,
    current_amps: float,
    voltage_volts: float,
    timeout_minutes: int,
    phase: EbcModePhase | str = EbcModePhase.START,
) -> bytes:
    """Build a source-defined CCCV packet without transmitting it."""

    return build_cccv_charge_packet(
        CCCV_CHARGE,
        current_amps=current_amps,
        voltage_volts=voltage_volts,
        timeout_minutes=timeout_minutes,
        phase=phase,
    )


def make_constant_current_discharge_packet(
    *,
    current_amps: float,
    cutoff_volts: float,
    timeout_minutes: int,
    phase: EbcModePhase | str = EbcModePhase.START,
) -> bytes:
    """Build a source-defined constant-current packet without transmitting it."""

    return build_discharge_packet(
        CONSTANT_CURRENT_DISCHARGE,
        value=current_amps,
        cutoff_volts=cutoff_volts,
        timeout_minutes=timeout_minutes,
        phase=phase,
    )


def make_constant_power_discharge_packet(
    *,
    power_watts: float,
    cutoff_volts: float,
    timeout_minutes: int,
    phase: EbcModePhase | str = EbcModePhase.START,
) -> bytes:
    """Build a source-defined constant-power packet without transmitting it."""

    return build_discharge_packet(
        CONSTANT_POWER_DISCHARGE,
        value=power_watts,
        cutoff_volts=cutoff_volts,
        timeout_minutes=timeout_minutes,
        phase=phase,
    )


def make_constant_resistance_discharge_packet(
    *,
    resistance_ohms: float,
    observed_voltage_volts: float,
    cutoff_volts: float,
    timeout_minutes: int,
    phase: EbcModePhase | str = EbcModePhase.START,
) -> bytes:
    """Build a source-defined software-CR packet without transmitting it."""

    return build_constant_resistance_packet(
        CONSTANT_RESISTANCE_DISCHARGE,
        resistance_ohms=resistance_ohms,
        observed_voltage_volts=observed_voltage_volts,
        cutoff_volts=cutoff_volts,
        timeout_minutes=timeout_minutes,
        phase=phase,
    )
