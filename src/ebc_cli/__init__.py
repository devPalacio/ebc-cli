# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Jay (Python CLI contributions)
# See NOTICE for upstream attribution and port details.

"""Terminal tools for ZKETECH EBC battery testers."""

from .measurement import MeasurementAccumulator, MeasurementSummary
from .modes import (
    SOURCE_OPERATING_MODES,
    EbcModeKind,
    EbcModePhase,
    EbcModeValue,
    EbcOperatingMode,
    OperatingModeType,
    get_operating_mode,
)
from .protocol import (
    A05_MODEL,
    A10H_MODEL,
    EBC_A05_MODEL_ID,
    EbcModel,
    EbcProfile,
    EbcSerialSettings,
    EbcStatus,
    ProtocolError,
    get_model,
    register_model,
)

__all__ = [
    "A05_MODEL",
    "A10H_MODEL",
    "EBC_A05_MODEL_ID",
    "SOURCE_OPERATING_MODES",
    "EbcModeKind",
    "EbcModePhase",
    "EbcModeValue",
    "EbcModel",
    "EbcOperatingMode",
    "EbcProfile",
    "EbcSerialSettings",
    "EbcStatus",
    "MeasurementAccumulator",
    "MeasurementSummary",
    "OperatingModeType",
    "ProtocolError",
    "get_model",
    "get_operating_mode",
    "register_model",
]
