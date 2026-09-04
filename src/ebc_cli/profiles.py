"""Typed, validated descriptions of controlled tester profiles."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .protocol import EbcStatus


class ProfileError(ValueError):
    """A controlled profile contains an unsafe or incomplete parameter."""


class ProfileKind(StrEnum):
    """The controlled operations supported by this CLI."""

    RECOVERY = "recovery"
    FLOAT_CHARGE = "float-charge"
    CAPACITY_TEST = "capacity-test"


class ProfileStarter(Protocol):
    """The device operations needed to start a controlled profile."""

    def begin_lead_acid_charge(self, *, current_amps: float, timeout_minutes: int) -> EbcStatus: ...

    def begin_lead_acid_recovery(
        self, *, current_amps: float, timeout_minutes: int
    ) -> EbcStatus: ...

    def begin_float_charge(
        self, *, current_amps: float, voltage_volts: float, timeout_minutes: int
    ) -> EbcStatus: ...

    def begin_constant_current_discharge(
        self, *, current_amps: float, cutoff_volts: float, timeout_minutes: int
    ) -> EbcStatus: ...


@dataclass(frozen=True, slots=True)
class TesterProfile:
    """A validated operation that can be started on an EBC-A10H."""

    kind: ProfileKind
    current_amps: float
    timeout_minutes: int
    voltage_volts: float | None = None
    cutoff_volts: float | None = None

    def __post_init__(self) -> None:
        if self.kind is ProfileKind.RECOVERY:
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
            _require_range(
                "capacity-test current",
                self.current_amps,
                0.1,
                5.0,
                "A",
                minimum_text="0.1",
                maximum_text="5.0",
            )
            _require_range(
                "capacity-test cutoff",
                self.cutoff_volts,
                10.2,
                11.5,
                "V",
            )
            _require_range(
                "capacity-test timeout",
                self.timeout_minutes,
                1,
                2880,
                "minutes",
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
    def summary(self) -> str:
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

    def start(self, tester: ProfileStarter) -> EbcStatus:
        """Start this profile and return the tester's baseline status."""

        if self.kind is ProfileKind.RECOVERY:
            return tester.begin_lead_acid_recovery(
                current_amps=self.current_amps,
                timeout_minutes=self.timeout_minutes,
            )
        if self.kind is ProfileKind.FLOAT_CHARGE:
            return tester.begin_float_charge(
                current_amps=self.current_amps,
                voltage_volts=self.voltage_volts,
                timeout_minutes=self.timeout_minutes,
            )
        return tester.begin_constant_current_discharge(
            current_amps=self.current_amps,
            cutoff_volts=self.cutoff_volts,
            timeout_minutes=self.timeout_minutes,
        )


@dataclass(frozen=True, slots=True)
class BulkToFloatProfile:
    """A bounded six-cell PbAc bulk charge followed by a CCCV float hold."""

    bulk_current_amps: float
    bulk_timeout_minutes: int
    float_current_amps: float
    float_voltage_volts: float
    float_timeout_minutes: int

    def __post_init__(self) -> None:
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
    def summary(self) -> str:
        return (
            "Bulk-to-float started: PbAc, 6 cells, 14.4 V target, "
            f"{self.bulk_current_amps:.2f} A bulk for up to {self.bulk_timeout_minutes} minutes; "
            f"then {self.float_voltage_volts:.1f} V / {self.float_current_amps:.2f} A float "
            f"for up to {self.float_timeout_minutes} minutes"
        )

    def start_bulk(self, tester: ProfileStarter) -> EbcStatus:
        """Start the PbAc bulk phase and return the tester's baseline status."""

        return tester.begin_lead_acid_charge(
            current_amps=self.bulk_current_amps,
            timeout_minutes=self.bulk_timeout_minutes,
        )

    def start_float(self, tester: ProfileStarter) -> EbcStatus:
        """Start the CCCV float phase and return the tester's baseline status."""

        return tester.begin_float_charge(
            current_amps=self.float_current_amps,
            voltage_volts=self.float_voltage_volts,
            timeout_minutes=self.float_timeout_minutes,
        )


def make_recovery_profile(*, current_amps: float, timeout_minutes: int) -> TesterProfile:
    return TesterProfile(ProfileKind.RECOVERY, current_amps, timeout_minutes)


def make_float_charge_profile(
    *, current_amps: float, voltage_volts: float, timeout_minutes: int
) -> TesterProfile:
    return TesterProfile(
        ProfileKind.FLOAT_CHARGE,
        current_amps,
        timeout_minutes,
        voltage_volts=voltage_volts,
    )


def make_capacity_test_profile(
    *, current_amps: float, cutoff_volts: float, timeout_minutes: int
) -> TesterProfile:
    return TesterProfile(
        ProfileKind.CAPACITY_TEST,
        current_amps,
        timeout_minutes,
        cutoff_volts=cutoff_volts,
    )


def make_bulk_to_float_profile(
    *,
    bulk_current_amps: float,
    bulk_timeout_minutes: int,
    float_current_amps: float,
    float_voltage_volts: float,
    float_timeout_minutes: int,
) -> BulkToFloatProfile:
    """Build a validated, two-stage normal charge profile."""

    return BulkToFloatProfile(
        bulk_current_amps=bulk_current_amps,
        bulk_timeout_minutes=bulk_timeout_minutes,
        float_current_amps=float_current_amps,
        float_voltage_volts=float_voltage_volts,
        float_timeout_minutes=float_timeout_minutes,
    )


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
