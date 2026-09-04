"""Minimal POSIX serial adapter for cautious EBC operations."""

import fcntl
import os
import select
import termios
import time
from pathlib import Path
from typing import Self

from .protocol import (
    CONNECT,
    EBC_A10H_MODEL_ID,
    MODEL_NAMES,
    STOP,
    EbcStatus,
    find_valid_status_frame,
    make_cccv_charge_plan,
    make_constant_current_discharge_plan,
    make_lead_acid_charge_plan,
    parse_status,
)

SUPPORTED_MODEL_ID = EBC_A10H_MODEL_ID


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
    ):
        if timeout <= 0:
            raise DeviceError("timeout must be greater than zero")
        self.path = Path(path)
        self.timeout = timeout
        self.lock_device = lock_device
        self._fd: int | None = None
        self._rx_buffer = bytearray()
        self._control_active = False

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def open(self) -> None:
        """Open and configure the port as 9600 baud, 8 data bits, even parity."""

        if self._fd is not None:
            return
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except FileNotFoundError as exc:
            raise DeviceError(f"serial device does not exist: {self.path}") from exc
        except PermissionError as exc:
            raise DeviceError(
                f"permission denied opening {self.path}; use a session in the uucp group"
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
            attrs[2] = cflags
            attrs[3] = 0  # local flags: raw
            attrs[4] = termios.B9600
            attrs[5] = termios.B9600
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIOFLUSH)
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
                self._write_all(STOP)
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

    def status(self) -> EbcStatus:
        """Request and decode one status frame without changing tester mode."""

        self._require_open()
        self._write_all(CONNECT)
        return parse_status(self._read_status_frame())

    def begin_lead_acid_recovery(self, *, current_amps: float, timeout_minutes: int) -> EbcStatus:
        """Establish a safe baseline, then start a supervised PbAc charge."""

        frame = make_lead_acid_charge_plan(
            current_amps=current_amps, cells=6, timeout_minutes=timeout_minutes
        )
        return self._begin_charge(frame)

    def begin_float_charge(
        self, *, current_amps: float, voltage_volts: float, timeout_minutes: int
    ) -> EbcStatus:
        """Establish a safe baseline, then start a CCCV float-charge profile."""

        frame = make_cccv_charge_plan(
            current_amps=current_amps,
            voltage_volts=voltage_volts,
            timeout_minutes=timeout_minutes,
        )
        return self._begin_charge(frame)

    def begin_constant_current_discharge(
        self, *, current_amps: float, cutoff_volts: float, timeout_minutes: int
    ) -> EbcStatus:
        """Establish a safe baseline, then start a CC discharge."""

        frame = make_constant_current_discharge_plan(
            current_amps=current_amps,
            cutoff_volts=cutoff_volts,
            timeout_minutes=timeout_minutes,
        )
        return self._begin_charge(frame)

    def next_status(self) -> EbcStatus:
        """Read the tester's next unsolicited status frame."""

        self._require_open()
        return parse_status(self._read_status_frame())

    def _begin_charge(self, frame: bytes) -> EbcStatus:
        self._require_open()
        # CONNECT is a read-only status query, so identify the tester before
        # sending STOP or a profile command to an unknown model.
        self._require_supported_model(self.status())

        # This mirrors the upstream GUI's connection sequence exactly after
        # the model has been verified.
        self._write_all(STOP)
        self._write_all(CONNECT)
        baseline = parse_status(self._read_status_frame())
        self._require_supported_model(baseline)
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
        self._write_all(STOP)
        self._control_active = False

    def _write_all(self, data: bytes) -> None:
        assert self._fd is not None
        view = memoryview(data)
        while view:
            try:
                written = os.write(self._fd, view)
            except BlockingIOError:
                self._wait_writable()
                continue
            except OSError as exc:
                raise DeviceError(f"serial write failed on {self.path}: {exc}") from exc
            if written <= 0:
                raise DeviceError(f"serial write made no progress on {self.path}")
            view = view[written:]

    def _read_status_frame(self) -> bytes:
        assert self._fd is not None
        deadline = time.monotonic() + self.timeout
        while True:
            frame = find_valid_status_frame(self._rx_buffer)
            if frame is not None:
                start = self._rx_buffer.find(frame)
                del self._rx_buffer[: start + len(frame)]
                return frame
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeviceError(
                    "tester did not return a checksum-valid status frame within "
                    f"{self.timeout:g}s (received {self._rx_buffer.hex().upper() or 'no data'})"
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

    def _wait_writable(self) -> None:
        assert self._fd is not None
        _, writable, _ = select.select([], [self._fd], [], self.timeout)
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

    @staticmethod
    def _require_supported_model(status: EbcStatus) -> EbcStatus:
        if status.model_id != SUPPORTED_MODEL_ID:
            model_name = MODEL_NAMES.get(status.model_id, f"unknown (0x{status.model_id:02X})")
            raise DeviceError(
                f"unsupported tester model: {model_name}; this CLI only controls EBC-A10H"
            )
        return status
