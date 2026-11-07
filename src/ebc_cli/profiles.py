# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Jay (Python CLI contributions)
# See NOTICE for upstream attribution and port details.

"""Typed, validated descriptions of controlled tester profiles."""

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from .capabilities import A10H_CAPABILITIES, CapabilityError, EbcCapabilities
from .modes import (
    CCCV_CHARGE,
    CONSTANT_CURRENT_DISCHARGE,
    LEAD_ACID_CHARGE,
    EbcModeCommand,
)
from .protocol import ProtocolError


class ProfileError(ValueError):
    """A controlled profile contains an unsafe or incomplete parameter."""


class ProfileKind(StrEnum):
    """The controlled operations supported by this CLI."""

    RECOVERY = "recovery"
    CHARGE = "charge"
    FLOAT_CHARGE = "float-charge"
    CAPACITY_TEST = "capacity-test"


@dataclass(frozen=True, slots=True)
class TesterProfile:
    """A validated operation that can be started on an EBC-A10H."""

    kind: ProfileKind
    current_amps: float
    timeout_minutes: int
    voltage_volts: float | None = None
    cutoff_volts: float | None = None
    capabilities: EbcCapabilities = A10H_CAPABILITIES
    end_current_amps: float | None = field(default=None, kw_only=True)
    end_current_dwell_seconds: float = field(default=30, kw_only=True)
    voltage_tolerance_volts: float = field(default=0.05, kw_only=True)
    max_sample_gap_seconds: float = field(default=5, kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.capabilities, EbcCapabilities):
            raise ProfileError("capabilities must be an EbcCapabilities instance")
        if self.kind is ProfileKind.CHARGE:
            _require_positive("charge current", self.current_amps)
            _require_positive("charge voltage", self.voltage_volts)
            _require_positive("end current", self.end_current_amps)
            _require_positive("end-current dwell", self.end_current_dwell_seconds)
            _require_positive("voltage tolerance", self.voltage_tolerance_volts)
            _require_positive("maximum sample gap", self.max_sample_gap_seconds)
            assert self.end_current_amps is not None and self.voltage_volts is not None
            if self.end_current_amps >= self.current_amps:
                raise ProfileError("end current must be less than the charge current limit")
            if self.voltage_tolerance_volts >= self.voltage_volts:
                raise ProfileError("voltage tolerance must be less than the charge voltage target")
            try:
                self.start_command.validate_live(self.capabilities)
            except ProtocolError as exc:
                raise ProfileError(str(exc)) from exc
            return
        if self.kind is ProfileKind.RECOVERY:
            _validate_capability(self.capabilities.validate_charge_current, self.current_amps)
            _require_range(
                "recovery current",
                self.current_amps,
                0.05,
                0.20,
                "A",
                minimum_text="0.05",
                maximum_text="0.20",
            )
            _require_range(
                "recovery timeout",
                self.timeout_minutes,
                1,
                30,
                "minutes",
            )
            return
        if self.kind is ProfileKind.FLOAT_CHARGE:
            _validate_capability(self.capabilities.validate_charge_current, self.current_amps)
            _validate_capability(
                self.capabilities.validate_charge_voltage,
                self.voltage_volts,
            )
            _require_range(
                "float-charge current",
                self.current_amps,
                0.05,
                2.00,
                "A",
                minimum_text="0.05",
                maximum_text="2.00",
            )
            _require_range(
                "float-charge voltage",
                self.voltage_volts,
                13.5,
                13.8,
                "V",
            )
            _require_range(
                "float-charge timeout",
                self.timeout_minutes,
                1,
                240,
                "minutes",
            )
            return
        if self.kind is ProfileKind.CAPACITY_TEST:
            _require_positive("capacity-test current", self.current_amps)
            _require_positive("capacity-test cutoff", self.cutoff_volts)
            try:
                self.start_command.validate_live(self.capabilities)
            except ProtocolError as exc:
                raise ProfileError(str(exc)) from exc
            assert self.cutoff_volts is not None
            if self.current_amps * self.cutoff_volts > self.capabilities.power_max_watts:
                raise ProfileError(
                    "capacity-test cutoff power exceeds "
                    f"{self.capabilities.power_max_watts:g} W discharge limit"
                )
            return
        raise ProfileError(f"unsupported profile kind: {self.kind!r}")

    @property
    def start_event(self) -> str:
        return f"{self.kind.value}-started"

    @property
    def sample_event(self) -> str:
        return (
            "capacity-sample"
            if self.kind is ProfileKind.CAPACITY_TEST
            else f"{self.kind.value}-sample"
        )

    @property
    def stop_on_cutoff(self) -> bool:
        return self.kind is ProfileKind.CAPACITY_TEST

    @property
    def start_command(self) -> EbcModeCommand:
        """Return the device-neutral command for this validated profile."""

        if self.kind is ProfileKind.RECOVERY:
            return EbcModeCommand(
                LEAD_ACID_CHARGE,
                current_amps=self.current_amps,
                cells=6,
                timeout_minutes=self.timeout_minutes,
            )
        if self.kind in {ProfileKind.FLOAT_CHARGE, ProfileKind.CHARGE}:
            return EbcModeCommand(
                CCCV_CHARGE,
                current_amps=self.current_amps,
                voltage_volts=self.voltage_volts,
                timeout_minutes=self.timeout_minutes,
            )
        return EbcModeCommand(
            CONSTANT_CURRENT_DISCHARGE,
            current_amps=self.current_amps,
            cutoff_volts=self.cutoff_volts,
            timeout_minutes=self.timeout_minutes,
        )

    @property
    def summary(self) -> str:
        if self.kind is ProfileKind.CHARGE:
            return (
                f"Charge started: CCCV, {self.voltage_volts:g} V target, "
                f"{self.current_amps:g} A limit, {self.timeout_minutes}-minute hard timeout; "
                f"end at measured current <= {self.end_current_amps:g} A within "
                f"{self.voltage_tolerance_volts:g} V of target for "
                f"{self.end_current_dwell_seconds:g} seconds "
                f"(maximum sample gap {self.max_sample_gap_seconds:g} seconds)"
            )
        if self.kind is ProfileKind.RECOVERY:
            return (
                "Recovery started: PbAc, 6 cells, 14.4 V target, "
                f"{self.current_amps:.2f} A, {self.timeout_minutes}-minute hard timeout"
            )
        if self.kind is ProfileKind.FLOAT_CHARGE:
            return (
                f"Float charge started: {self.voltage_volts:.1f} V, "
                f"{self.current_amps:.2f} A limit, {self.timeout_minutes}-minute hard timeout"
            )
        return (
            f"Capacity test started: {self.current_amps:.2f} A to {self.cutoff_volts:.1f} V, "
            f"{self.timeout_minutes}-minute hard timeout"
        )


@dataclass(frozen=True, slots=True)
class BulkToFloatProfile:
    """A bounded six-cell PbAc bulk charge followed by a CCCV float hold."""

    bulk_current_amps: float
    bulk_timeout_minutes: int
    float_current_amps: float
    float_voltage_volts: float
    float_timeout_minutes: int
    capabilities: EbcCapabilities = A10H_CAPABILITIES

    def __post_init__(self) -> None:
        if not isinstance(self.capabilities, EbcCapabilities):
            raise ProfileError("capabilities must be an EbcCapabilities instance")
        _validate_capability(self.capabilities.validate_charge_current, self.bulk_current_amps)
        _validate_capability(self.capabilities.validate_charge_current, self.float_current_amps)
        _validate_capability(
            self.capabilities.validate_charge_voltage,
            self.float_voltage_volts,
        )
        _require_range(
            "bulk current",
            self.bulk_current_amps,
            0.05,
            2.00,
            "A",
            minimum_text="0.05",
            maximum_text="2.00",
        )
        _require_range("bulk timeout", self.bulk_timeout_minutes, 1, 1440, "minutes")
        _require_range(
            "float current",
            self.float_current_amps,
            0.05,
            2.00,
            "A",
            minimum_text="0.05",
            maximum_text="2.00",
        )
        if self.float_current_amps > self.bulk_current_amps:
            raise ProfileError("float current must not exceed bulk current")
        _require_range("float voltage", self.float_voltage_volts, 13.5, 13.8, "V")
        _require_range("float timeout", self.float_timeout_minutes, 1, 240, "minutes")

    @property
    def start_event(self) -> str:
        return "bulk-to-float-started"

    @property
    def bulk_baseline_event(self) -> str:
        return "bulk-to-float-bulk-baseline"

    @property
    def bulk_sample_event(self) -> str:
        return "bulk-to-float-bulk-sample"

    @property
    def float_start_event(self) -> str:
        return "bulk-to-float-float-started"

    @property
    def float_baseline_event(self) -> str:
        return "bulk-to-float-float-baseline"

    @property
    def float_sample_event(self) -> str:
        return "bulk-to-float-float-sample"

    @property
    def bulk_start_command(self) -> EbcModeCommand:
        """Return the device-neutral command for the bulk phase."""

        return EbcModeCommand(
            LEAD_ACID_CHARGE,
            current_amps=self.bulk_current_amps,
            cells=6,
            timeout_minutes=self.bulk_timeout_minutes,
        )

    @property
    def float_start_command(self) -> EbcModeCommand:
        """Return the device-neutral command for the float phase."""

        return EbcModeCommand(
            CCCV_CHARGE,
            current_amps=self.float_current_amps,
            voltage_volts=self.float_voltage_volts,
            timeout_minutes=self.float_timeout_minutes,
        )

    @property
    def summary(self) -> str:
        return (
            "Bulk-to-float started: PbAc, 6 cells, 14.4 V target, "
            f"{self.bulk_current_amps:.2f} A bulk for up to {self.bulk_timeout_minutes} minutes; "
            f"then {self.float_voltage_volts:.1f} V / {self.float_current_amps:.2f} A float "
            f"for up to {self.float_timeout_minutes} minutes"
        )


def make_recovery_profile(
    *, current_amps: float, timeout_minutes: int, capabilities: EbcCapabilities = A10H_CAPABILITIES
) -> TesterProfile:
    return TesterProfile(
        ProfileKind.RECOVERY, current_amps, timeout_minutes, capabilities=capabilities
    )


def make_float_charge_profile(
    *,
    current_amps: float,
    voltage_volts: float,
    timeout_minutes: int,
    capabilities: EbcCapabilities = A10H_CAPABILITIES,
) -> TesterProfile:
    return TesterProfile(
        ProfileKind.FLOAT_CHARGE,
        current_amps,
        timeout_minutes,
        voltage_volts=voltage_volts,
        capabilities=capabilities,
    )


def make_charge_profile(
    *,
    current_amps: float,
    voltage_volts: float,
    end_current_amps: float,
    timeout_minutes: int,
    end_current_dwell_seconds: float = 30,
    voltage_tolerance_volts: float = 0.05,
    max_sample_gap_seconds: float = 5,
    capabilities: EbcCapabilities = A10H_CAPABILITIES,
) -> TesterProfile:
    """Build a bounded CCCV charge with a measured, host-side end-current threshold."""

    return TesterProfile(
        ProfileKind.CHARGE,
        current_amps,
        timeout_minutes,
        voltage_volts=voltage_volts,
        capabilities=capabilities,
        end_current_amps=end_current_amps,
        end_current_dwell_seconds=end_current_dwell_seconds,
        voltage_tolerance_volts=voltage_tolerance_volts,
        max_sample_gap_seconds=max_sample_gap_seconds,
    )


def make_capacity_test_profile(
    *,
    current_amps: float,
    cutoff_volts: float,
    timeout_minutes: int,
    capabilities: EbcCapabilities = A10H_CAPABILITIES,
) -> TesterProfile:
    return TesterProfile(
        ProfileKind.CAPACITY_TEST,
        current_amps,
        timeout_minutes,
        cutoff_volts=cutoff_volts,
        capabilities=capabilities,
    )


def make_bulk_to_float_profile(
    *,
    bulk_current_amps: float,
    bulk_timeout_minutes: int,
    float_current_amps: float,
    float_voltage_volts: float,
    float_timeout_minutes: int,
    capabilities: EbcCapabilities = A10H_CAPABILITIES,
) -> BulkToFloatProfile:
    """Build a validated, two-stage normal charge profile."""

    return BulkToFloatProfile(
        bulk_current_amps=bulk_current_amps,
        bulk_timeout_minutes=bulk_timeout_minutes,
        float_current_amps=float_current_amps,
        float_voltage_volts=float_voltage_volts,
        float_timeout_minutes=float_timeout_minutes,
        capabilities=capabilities,
    )


def _require_positive(label: str, value: float | None) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ProfileError(f"{label} must be finite and positive")


def _require_range(
    label: str,
    value: float | None,
    minimum: float,
    maximum: float,
    unit: str,
    *,
    minimum_text: str | None = None,
    maximum_text: str | None = None,
) -> None:
    if value is None or not minimum <= value <= maximum:
        minimum_text = minimum_text or f"{minimum:g}"
        maximum_text = maximum_text or f"{maximum:g}"
        raise ProfileError(f"{label} must be between {minimum_text} and {maximum_text} {unit}")


def _validate_capability(check: Callable[[float], None], value: float | None) -> None:
    if value is None:
        return
    try:
        check(value)
    except CapabilityError as exc:
        raise ProfileError(str(exc)) from exc
