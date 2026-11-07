# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Jay (Python CLI contributions)
# See NOTICE for upstream attribution and port details.

"""Validated electrical limits for registered EBC tester models."""

import math
from dataclasses import dataclass


class CapabilityError(ValueError):
    """A requested setting is outside a tester's documented envelope."""


@dataclass(frozen=True, slots=True)
class EbcCapabilities:
    """The electrical setting envelope documented for one EBC model."""

    charge_current_min_amps: float = 0.05
    charge_current_max_amps: float = 5.00
    discharge_current_min_amps: float = 0.05
    discharge_current_max_amps: float = 10.00
    voltage_min_volts: float = 0.00
    voltage_max_volts: float = 30.00
    charge_voltage_max_volts: float = 22.00
    current_step_amps: float = 0.01
    voltage_step_volts: float = 0.01
    power_min_watts: float = 0.01
    power_max_watts: float = 150.00

    def __post_init__(self) -> None:
        for label in (
            "charge_current_min_amps",
            "charge_current_max_amps",
            "discharge_current_min_amps",
            "discharge_current_max_amps",
            "voltage_min_volts",
            "voltage_max_volts",
            "charge_voltage_max_volts",
            "current_step_amps",
            "voltage_step_volts",
            "power_min_watts",
            "power_max_watts",
        ):
            value = getattr(self, label)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{label} must be numeric")
            if not math.isfinite(value):
                raise ValueError(f"{label} must be finite")
            if value < 0:
                raise ValueError(f"{label} must be non-negative")
        if self.charge_current_min_amps > self.charge_current_max_amps:
            raise ValueError("charge current minimum must not exceed maximum")
        if self.discharge_current_min_amps > self.discharge_current_max_amps:
            raise ValueError("discharge current minimum must not exceed maximum")
        if self.voltage_min_volts > self.voltage_max_volts:
            raise ValueError("voltage minimum must not exceed maximum")
        if not self.voltage_min_volts <= self.charge_voltage_max_volts <= self.voltage_max_volts:
            raise ValueError("charge voltage maximum must be within the voltage range")
        if self.current_step_amps <= 0 or self.voltage_step_volts <= 0:
            raise ValueError("setting steps must be positive")
        if self.power_min_watts > self.power_max_watts:
            raise ValueError("power minimum must not exceed maximum")

    def max_charge_voltage(self) -> float:
        """Return the highest charge target supported by this model variant."""

        return self.charge_voltage_max_volts

    def validate_charge_current(self, value: float) -> None:
        """Validate a charge-current setting against the documented envelope."""

        self._validate_setting(
            value,
            minimum=self.charge_current_min_amps,
            maximum=self.charge_current_max_amps,
            step=self.current_step_amps,
            label="charge current",
            unit="A",
        )

    def validate_discharge_current(self, value: float) -> None:
        """Validate a discharge-current setting against the documented envelope."""

        self._validate_setting(
            value,
            minimum=self.discharge_current_min_amps,
            maximum=self.discharge_current_max_amps,
            step=self.current_step_amps,
            label="discharge current",
            unit="A",
        )

    def validate_voltage(self, value: float, *, label: str = "voltage") -> None:
        """Validate a voltage setting against the absolute 0--30 V range."""

        self._validate_setting(
            value,
            minimum=self.voltage_min_volts,
            maximum=self.voltage_max_volts,
            step=self.voltage_step_volts,
            label=label,
            unit="V",
        )

    def validate_charge_voltage(self, value: float) -> None:
        """Validate a charge target against this model variant's ceiling."""

        self._validate_setting(
            value,
            minimum=self.voltage_min_volts,
            maximum=self.charge_voltage_max_volts,
            step=self.voltage_step_volts,
            label="charge voltage",
            unit="V",
        )

    def validate_power(self, value: float) -> None:
        """Validate a constant-power load setting against the product envelope."""

        self._validate_setting(
            value,
            minimum=self.power_min_watts,
            maximum=self.power_max_watts,
            step=0.01,
            label="power",
            unit="W",
        )

    def _validate_setting(
        self,
        value: float,
        *,
        minimum: float,
        maximum: float,
        step: float,
        label: str,
        unit: str,
    ) -> None:
        numeric = _finite_value(label, value)
        if not minimum <= numeric <= maximum:
            raise CapabilityError(f"{label} must be between {minimum:g} and {maximum:g} {unit}")
        quotient = numeric / step
        if not math.isclose(quotient, round(quotient), rel_tol=0.0, abs_tol=1e-9):
            raise CapabilityError(f"{label} must use {step:g} {unit} increments")


def _finite_value(label: str, value: float) -> float:
    if isinstance(value, bool):
        raise CapabilityError(f"{label} must be numeric")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CapabilityError(f"{label} must be numeric") from exc
    if not math.isfinite(numeric):
        raise CapabilityError(f"{label} must be finite")
    return numeric


A10H_CAPABILITIES = EbcCapabilities()
