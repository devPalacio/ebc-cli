# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Jay (Python CLI contributions)
# See NOTICE for upstream attribution and port details.

"""Hardware-free helpers shared by session tests."""

from ebc_cli.device import DeviceError
from ebc_cli.modes import LEAD_ACID_CHARGE, EbcModeCommand
from ebc_cli.protocol import (
    FRAME_PREFIX,
    FRAME_SUFFIX,
    STATUS_CHECKSUM_INDEX,
    EbcStatus,
    checksum,
    parse_status,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class ProfileTester:
    def __init__(self, statuses: list[EbcStatus]):
        self.statuses = list(statuses)
        self.bulk_calls: list[dict[str, object]] = []
        self.float_calls: list[dict[str, object]] = []
        self.adjust_calls: list[EbcModeCommand] = []
        self.stop_calls = 0

    def begin_mode(self, command: EbcModeCommand) -> EbcStatus:
        fields = {"current_amps": command.current_amps, "timeout_minutes": command.timeout_minutes}
        if command.mode is LEAD_ACID_CHARGE:
            self.bulk_calls.append({**fields, "cells": command.cells})
        else:
            self.float_calls.append({**fields, "voltage_volts": command.voltage_volts})
        return status()

    def adjust_mode(self, command: EbcModeCommand) -> None:
        self.adjust_calls.append(command)

    def next_status(self, *, timeout=None, latest=False) -> EbcStatus:
        if not self.statuses:
            raise DeviceError("fake status stream ended")
        return self.statuses.pop(0)

    def status(self, *, timeout=None, latest=False) -> EbcStatus:
        return self.next_status()

    def stop(self) -> None:
        self.stop_calls += 1


def status(*, state: int = 0x68) -> EbcStatus:
    frame = bytearray(19)
    frame[0] = FRAME_PREFIX
    frame[1] = state
    frame[2:4] = bytes((0, 70))
    frame[4:6] = bytes((50, 0))
    frame[6:8] = bytes((0, 100))
    frame[16] = 0x06
    frame[STATUS_CHECKSUM_INDEX] = checksum(bytes(frame), STATUS_CHECKSUM_INDEX)
    frame[-1] = FRAME_SUFFIX
    return parse_status(bytes(frame))
