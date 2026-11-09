# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Jay (Python CLI contributions)
# See NOTICE for upstream attribution and port details.

"""Terminal tools for ZKETECH EBC battery testers.

The CLI is one consumer of this package; the same objects are importable for
embedding:

    from ebc_cli import EbcDevice, make_float_charge_profile, run_profile

``run_profile`` and ``run_bulk_to_float`` start and stop the tester inside a
``SupervisedSession``. Starting a mode directly on ``EbcDevice`` sends the
start frame but does not guarantee a STOP frame when your code raises.
"""

from .capabilities import A10H_CAPABILITIES, CapabilityError, EbcCapabilities
from .capacity import (
    CapacityAssessment,
    CapacityOutcome,
    CapacityRequest,
    CutoffEvidence,
    CutoffKind,
    CutoffSample,
)
from .csvlog import StatusCsvLogger
from .device import DeviceError, EbcDevice
from .measurement import MeasurementAccumulator, MeasurementSummary
from .modes import (
    SOURCE_OPERATING_MODES,
    EbcModeCommand,
    EbcModeKind,
    EbcModePhase,
    EbcModeValue,
    EbcOperatingMode,
    OperatingModeType,
    build_mode_packet,
    get_operating_mode,
)
from .profiles import (
    BulkToFloatProfile,
    ProfileError,
    TesterProfile,
    make_bulk_to_float_profile,
    make_capacity_test_profile,
    make_charge_profile,
    make_float_charge_profile,
    make_recovery_profile,
)
from .protocol import (
    A05_MODEL,
    A10H_MODEL,
    EBC_A05_MODEL_ID,
    EbcModel,
    EbcSerialSettings,
    EbcStatus,
    ProtocolError,
    get_model,
    register_model,
)
from .runner import BulkToFloatRun, ProfileRun, run_bulk_to_float, run_profile
from .session import (
    MonitorResult,
    SessionResult,
    SessionStopReason,
    StatusLogger,
    SupervisedSession,
    Tester,
    run_active_session,
    run_bulk_to_float_session,
    run_monitor_session,
)

__all__ = [
    "A05_MODEL",
    "A10H_CAPABILITIES",
    "A10H_MODEL",
    "EBC_A05_MODEL_ID",
    "SOURCE_OPERATING_MODES",
    "BulkToFloatProfile",
    "BulkToFloatRun",
    "CapabilityError",
    "CapacityAssessment",
    "CapacityOutcome",
    "CapacityRequest",
    "CutoffEvidence",
    "CutoffKind",
    "CutoffSample",
    "DeviceError",
    "EbcCapabilities",
    "EbcDevice",
    "EbcModeCommand",
    "EbcModeKind",
    "EbcModePhase",
    "EbcModeValue",
    "EbcModel",
    "EbcOperatingMode",
    "EbcSerialSettings",
    "EbcStatus",
    "MeasurementAccumulator",
    "MeasurementSummary",
    "MonitorResult",
    "OperatingModeType",
    "ProfileError",
    "ProfileRun",
    "ProtocolError",
    "SessionResult",
    "SessionStopReason",
    "StatusCsvLogger",
    "StatusLogger",
    "SupervisedSession",
    "Tester",
    "TesterProfile",
    "build_mode_packet",
    "get_model",
    "get_operating_mode",
    "make_bulk_to_float_profile",
    "make_capacity_test_profile",
    "make_charge_profile",
    "make_float_charge_profile",
    "make_recovery_profile",
    "register_model",
    "run_active_session",
    "run_bulk_to_float",
    "run_bulk_to_float_session",
    "run_monitor_session",
    "run_profile",
]
