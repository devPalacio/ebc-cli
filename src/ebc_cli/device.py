# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Jay (Python CLI contributions)
# See NOTICE for upstream attribution and port details.

"""Minimal POSIX serial adapter for cautious EBC operations."""

import fcntl
import math
import os
import select
import termios
import time
from pathlib import Path
from typing import Self

from .diagnostics import permission_hint
from .modes import EbcModeCommand, EbcModeKind, build_mode_packet
from .protocol import (
    EBC_A10H_MODEL_ID,
    EbcModel,
    EbcStatus,
    find_valid_status_frame,
    get_model,
    parse_status,
)

# ``latest=True`` is deliberately a bounded drain. A tester can stream status
# frames continuously, so a loop that drains until the fd is no longer ready
# would never return under load. These limits are well above a normal polling
# interval's backlog while still leaving a finite worst case.
_LATEST_DRAIN_MAX_BYTES = 64 * 1024
_LATEST_DRAIN_MAX_FRAMES = 4096


class DeviceError(RuntimeError):
    """The serial device could not be opened or did not answer safely."""


class EbcDevice:
    """A 9600 8E1 EBC connection."""

    def __init__(
        self,
        path: str | Path = "/dev/ttyUSB0",
        timeout: float = 1.0,
        *,
        lock_device: bool = True,
        model: EbcModel | None = None,
    ):
        try:
            numeric_timeout = float(timeout)
        except (TypeError, ValueError, OverflowError) as exc:
            raise DeviceError("timeout must be a finite number greater than zero") from exc
        if not math.isfinite(numeric_timeout) or numeric_timeout <= 0:
            raise DeviceError("timeout must be a finite number greater than zero")
        self.path = Path(path)
        self.timeout = numeric_timeout
        self.lock_device = lock_device
        self.model = model or get_model(EBC_A10H_MODEL_ID)
        if self.model is None:  # pragma: no cover - registry invariant
            raise DeviceError("the default EBC-A10H model is not registered")
        if get_model(self.model.model_id) is not self.model:
            raise DeviceError(
                f"model {self.model.name} is not registered; register it before opening a device"
            )
        self._fd: int | None = None
        self._rx_buffer = bytearray()
        self._control_active = False

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def open(self) -> None:
        """Open and configure the port using the selected model's settings."""

        if self._fd is not None:
            return
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except FileNotFoundError as exc:
            raise DeviceError(f"serial device does not exist: {self.path}") from exc
        except PermissionError as exc:
            raise DeviceError(
                f"permission denied opening {self.path}; {permission_hint(self.path)}"
            ) from exc
        except OSError as exc:
            raise DeviceError(f"cannot open {self.path}: {exc.strerror}") from exc

        try:
            self._acquire_lock(fd)
        except DeviceError:
            os.close(fd)
            raise

        try:
            attrs = termios.tcgetattr(fd)
            attrs[0] = 0  # input flags: raw
            attrs[1] = 0  # output flags: raw
            cflags = attrs[2]
            cflags &= ~termios.CSIZE
            cflags &= ~termios.CSTOPB
            cflags &= ~getattr(termios, "CRTSCTS", 0)
            cflags &= ~termios.PARODD
            cflags |= termios.CS8 | termios.CREAD | termios.CLOCAL | termios.PARENB
            serial = self.model.serial
            baudrate = getattr(termios, f"B{serial.baudrate}", None)
            if baudrate is None:
                raise DeviceError(f"unsupported baudrate for {self.model.name}: {serial.baudrate}")
            data_bits = getattr(termios, f"CS{serial.data_bits}", None)
            if data_bits is None:
                raise DeviceError(
                    f"unsupported data width for {self.model.name}: {serial.data_bits}"
                )
            cflags &= ~termios.CSIZE
            cflags |= data_bits
            cflags &= ~(termios.PARENB | termios.PARODD)
            parity = serial.parity.casefold()
            if parity == "even":
                cflags |= termios.PARENB
            elif parity == "odd":
                cflags |= termios.PARENB | termios.PARODD
            if serial.stop_bits == 2:
                cflags |= termios.CSTOPB
            attrs[2] = cflags
            attrs[3] = 0  # local flags: raw
            attrs[4] = baudrate
            attrs[5] = baudrate
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIOFLUSH)
        except DeviceError:
            os.close(fd)
            raise
        except (OSError, termios.error) as exc:
            os.close(fd)
            raise DeviceError(f"cannot configure {self.path}: {exc}") from exc
        self._fd = fd
        self._rx_buffer.clear()
        self._control_active = False

    def close(self) -> None:
        fd = self._fd
        if fd is None:
            self._rx_buffer.clear()
            self._control_active = False
            return

        # Closing a port does not stop the tester. If this connection started
        # a profile, make a best-effort stop before releasing the descriptor so
        # serial errors and context-manager cleanup cannot strand a live load.
        if self._control_active:
            try:
                self._write_all(self.model.stop_packet)
            except DeviceError, OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass
        finally:
            self._fd = None
            self._rx_buffer.clear()
            self._control_active = False

    def status(self, *, timeout: float | None = None, latest: bool = False) -> EbcStatus:
        """Request and decode one status frame without changing tester mode."""

        self._require_open()
        if timeout is None:
            self._write_all(self.model.connect_packet)
            if latest:
                return parse_status(self._read_status_frame(latest=True))
            return parse_status(self._read_status_frame())

        effective_timeout = self._effective_timeout(timeout)
        deadline = time.monotonic() + effective_timeout
        self._write_all(self.model.connect_packet, timeout=self._remaining(deadline))
        return parse_status(self._read_status_frame(deadline=deadline, latest=latest))

    def fresh_status(self) -> EbcStatus:
        """Skip the initial response and await a subsequent streamed observation.

        CONNECT can yield a cached reading. Clear pending input first, then
        require a follow-up frame rather than silently falling back to that
        initial response. Neither frame carries a measurement timestamp, so
        this mitigates the observed cache behavior, not arbitrary firmware staleness.
        Each read is bounded by the configured timeout.
        """

        self._require_open()
        self._discard_pending_input()
        self.status()
        try:
            # A tester may emit several follow-up frames while CONNECT is
            # being handled. Return the newest complete observation rather
            # than the oldest queued one.
            return self.next_status(latest=True)
        except DeviceError as exc:
            raise DeviceError(f"could not confirm a fresh status sample: {exc}") from exc

    def begin_mode(self, command: EbcModeCommand) -> EbcStatus:
        """Establish a baseline and start one source-defined device mode."""

        self._validate_live_command(command)

        def frame_for(baseline: EbcStatus) -> bytes:
            if not command.mode.is_control_ready_for(baseline.model_id):
                raise DeviceError(
                    f"mode {command.mode.command} is not enabled for {baseline.model_name}"
                )
            effective = command
            if command.mode.kind is EbcModeKind.SOFTWARE_CONSTANT_RESISTANCE:
                effective = command.with_observed_voltage(baseline.voltage_volts)
            return build_mode_packet(
                effective,
                current_factor=self.model.current_factor,
                voltage_factor=self.model.voltage_factor,
                capabilities=self.model.capabilities,
            )

        return self._begin_frame(frame_for)

    def adjust_mode(self, command: EbcModeCommand) -> None:
        """Send a validated continuous-mode adjustment packet."""

        self._validate_live_command(command)
        self._require_open()
        if not command.mode.is_control_ready_for(self.model.model_id):
            raise DeviceError(f"mode {command.mode.command} is not enabled for {self.model.name}")
        frame = build_mode_packet(
            command,
            current_factor=self.model.current_factor,
            voltage_factor=self.model.voltage_factor,
            capabilities=self.model.capabilities,
        )
        self._write_all(frame)

    def _validate_live_command(self, command: EbcModeCommand) -> None:
        if not isinstance(command, EbcModeCommand):
            raise DeviceError("mode control requires an EbcModeCommand")
        model = self.model
        assert model is not None  # The constructor requires a registered model.
        if not model.supports_control or model.capabilities is None:
            raise DeviceError(
                f"unsupported tester model: {model.name}; this CLI has no validated control profile"
            )
        command.validate_live(model.capabilities)

    def next_status(self, *, timeout: float | None = None, latest: bool = False) -> EbcStatus:
        """Read the tester's next unsolicited status frame."""

        self._require_open()
        if timeout is None and not latest:
            return parse_status(self._read_status_frame())
        if timeout is None:
            return parse_status(self._read_status_frame(latest=True))
        return parse_status(
            self._read_status_frame(timeout=self._effective_timeout(timeout), latest=latest)
        )

    def _begin_frame(self, frame_for) -> EbcStatus:
        self._require_open()
        # CONNECT is a read-only status query, so identify the tester before
        # sending STOP or a profile command to an unknown model.
        self._require_supported_model(self.status())

        # This mirrors the upstream controller's connection sequence after
        # the model has been verified.
        self._write_all(self.model.stop_packet)
        self._write_all(self.model.connect_packet)
        baseline = parse_status(self._read_status_frame())
        self._require_supported_model(baseline)
        frame = frame_for(baseline)
        # CONNECT can leave extra status frames in both the adapter buffer and
        # the kernel queue. They belong to the pre-start state; dropping them
        # prevents a completion frame from terminating a newly started session
        # (or triggering its next phase) before the start frame is observed.
        self._discard_pending_input()
        # Mark active before writing the profile: a partial write may still
        # have reached the tester, and close() must then attempt STOP.
        self._control_active = True
        self._write_all(frame)
        return baseline

    def stop(self) -> None:
        """Immediately send the EBC stop frame."""

        self._require_open()
        # STOP is the emergency path: do not require a status response or
        # model identification first, since a stalled tester may be unable to
        # answer. Profile-start commands still require a verified A10H model.
        self._write_all(self.model.stop_packet)
        self._control_active = False

    def _write_all(self, data: bytes, *, timeout: float | None = None) -> None:
        assert self._fd is not None
        deadline = None
        if timeout is not None:
            deadline = time.monotonic() + self._effective_timeout(timeout)
        view = memoryview(data)
        while view:
            try:
                written = os.write(self._fd, view)
            except BlockingIOError:
                self._wait_writable(timeout=self._remaining(deadline))
                continue
            except OSError as exc:
                raise DeviceError(f"serial write failed on {self.path}: {exc}") from exc
            if written <= 0:
                raise DeviceError(f"serial write made no progress on {self.path}")
            view = view[written:]

    def _read_status_frame(
        self,
        *,
        timeout: float | None = None,
        deadline: float | None = None,
        latest: bool = False,
    ) -> bytes:
        assert self._fd is not None
        started = time.monotonic()
        if deadline is None:
            effective_timeout = self._effective_timeout(timeout)
            deadline = started + effective_timeout
        else:
            effective_timeout = max(0.0, deadline - started)
        while True:
            frame = self._take_status_frame()
            if frame is not None:
                if latest:
                    return self._drain_latest(frame, deadline)
                return frame
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeviceError(
                    "tester did not return a checksum-valid status frame within "
                    f"{effective_timeout:g}s (received {self._rx_buffer.hex().upper() or 'no data'})"
                )
            readable, _, _ = select.select([self._fd], [], [], remaining)
            if not readable:
                continue
            try:
                chunk = os.read(self._fd, 256)
            except BlockingIOError:
                continue
            if not chunk:
                raise DeviceError("serial device closed while waiting for status")
            self._rx_buffer.extend(chunk)

    def _take_status_frame(self) -> bytes | None:
        frame = find_valid_status_frame(self._rx_buffer)
        if frame is None:
            return None
        start = self._rx_buffer.find(frame)
        del self._rx_buffer[: start + len(frame)]
        return frame

    def _drain_latest(self, frame: bytes, deadline: float) -> bytes:
        """Return the newest frame available without waiting for the stream."""

        bytes_read = 0
        frames_read = 0
        while bytes_read < _LATEST_DRAIN_MAX_BYTES and frames_read < _LATEST_DRAIN_MAX_FRAMES:
            while frames_read < _LATEST_DRAIN_MAX_FRAMES:
                pending = self._take_status_frame()
                if pending is None:
                    break
                frame = pending
                frames_read += 1
            if frames_read >= _LATEST_DRAIN_MAX_FRAMES:
                break
            if time.monotonic() >= deadline:
                break
            # A zero-time select is intentional: latest observation must not
            # wait for a future frame or turn a continuous stream into an
            # unbounded read loop.
            readable, _, _ = select.select([self._fd], [], [], 0)
            if not readable:
                break
            remaining_bytes = _LATEST_DRAIN_MAX_BYTES - bytes_read
            try:
                chunk = os.read(self._fd, min(256, remaining_bytes))
            except BlockingIOError:
                # The fd can become unreadable between select() and read().
                # Return the newest frame already observed; retrying forever
                # here would defeat the bounded latest-observation contract.
                break
            if not chunk:
                break
            self._rx_buffer.extend(chunk)
            bytes_read += len(chunk)
        # A final read may have filled the byte budget exactly. Consume the
        # complete frames from that read before deciding whether the budget
        # was exceeded, otherwise a finite queue ending at the limit would be
        # reported as an overflow.
        while frames_read < _LATEST_DRAIN_MAX_FRAMES:
            pending = self._take_status_frame()
            if pending is None:
                break
            frame = pending
            frames_read += 1
        if (bytes_read >= _LATEST_DRAIN_MAX_BYTES or frames_read >= _LATEST_DRAIN_MAX_FRAMES) and (
            find_valid_status_frame(self._rx_buffer) is not None
            or bool(select.select([self._fd], [], [], 0)[0])
        ):
            raise DeviceError("status stream backlog exceeded bounded latest observation drain")
        return frame

    def _discard_pending_input(self) -> None:
        """Drop all input queued before a profile start boundary."""

        assert self._fd is not None
        self._rx_buffer.clear()
        try:
            termios.tcflush(self._fd, termios.TCIFLUSH)
        except (OSError, termios.error) as exc:
            raise DeviceError(f"cannot clear pending input on {self.path}: {exc}") from exc

    def _effective_timeout(self, timeout: float | None) -> float:
        if timeout is None:
            return self.timeout
        try:
            value = float(timeout)
        except (TypeError, ValueError, OverflowError) as exc:
            raise DeviceError("timeout must be a finite number greater than zero") from exc
        if not math.isfinite(value) or value <= 0:
            raise DeviceError("timeout must be a finite number greater than zero")
        return min(self.timeout, value)

    @staticmethod
    def _remaining(deadline: float | None) -> float | None:
        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DeviceError("serial operation exceeded its timeout")
        return remaining

    def _wait_writable(self, *, timeout: float | None = None) -> None:
        assert self._fd is not None
        effective_timeout = self._effective_timeout(timeout)
        _, writable, _ = select.select([], [self._fd], [], effective_timeout)
        if not writable:
            raise DeviceError("serial device did not become writable")

    def _acquire_lock(self, fd: int) -> None:
        """Acquire this process's advisory lock on the open serial device."""

        if not self.lock_device:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeviceError(f"serial device is already in use: {self.path}") from exc
        except OSError as exc:
            raise DeviceError(f"cannot lock {self.path}: {exc}") from exc

    def _require_open(self) -> None:
        if self._fd is None:
            raise DeviceError("serial device is not open")

    def _require_supported_model(self, status: EbcStatus) -> EbcModel:
        model = get_model(status.model_id)
        if model is None or not model.supports_control:
            raise DeviceError(
                f"unsupported tester model: {status.model_name}; this CLI has no validated control profile"
            )
        if model.model_id != self.model.model_id:
            raise DeviceError(
                f"tester reported {model.name}, but this connection is configured for {self.model.name}"
            )
        return model
