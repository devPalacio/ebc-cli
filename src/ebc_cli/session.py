"""Supervised tester-operation lifecycle and status sampling."""

import signal
import time
from collections.abc import Callable
from typing import Protocol, Self

from .device import DeviceError
from .protocol import EbcStatus


class Tester(Protocol):
    """The device operations required by a supervised session."""

    def next_status(self) -> EbcStatus: ...

    def status(self) -> EbcStatus: ...

    def stop(self) -> None: ...


class StatusLogger(Protocol):
    """The logging operations needed by a supervised session."""

    def write(
        self, status: EbcStatus, *, event: str, session_elapsed_seconds: float | None = None
    ) -> None: ...

    def write_event(self, event: str) -> None: ...


class _SessionSignal(Exception):
    """A termination signal raised while a tester operation is supervised."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


class SupervisedSession:
    """Keep an active tester operation bounded and stop it on every exit path.

    The tester is a stateful peripheral: closing the serial port does not stop
    a charge or discharge already running on its own. This lifecycle seam
    makes STOP best-effort on exceptions, Ctrl-C, termination signals, and
    normal timeout/cutoff completion.
    """

    _SIGNALS = tuple(
        signum
        for signum in (
            getattr(signal, "SIGINT", None),
            getattr(signal, "SIGTERM", None),
            getattr(signal, "SIGHUP", None),
        )
        if signum is not None
    )

    def __init__(self, tester: Tester, logger: StatusLogger | None):
        self.tester = tester
        self.logger = logger
        self._active = False
        self._stopping = False
        self._old_handlers: dict[int, object] = {}

    def __enter__(self) -> Self:
        for signum in self._SIGNALS:
            try:
                self._old_handlers[signum] = signal.signal(signum, self._handle_signal)
            except OSError, ValueError:
                # signal.signal is only legal in the main thread. Unit tests
                # and embedders may run the CLI elsewhere; finally still
                # provides cleanup for ordinary exceptions in that case.
                continue
        return self

    def __exit__(self, exc_type: object, _exc: object, _traceback: object) -> None:
        if self._active:
            if exc_type is None:
                event = "session-stop-sent"
            elif isinstance(exc_type, type) and issubclass(exc_type, _SessionSignal):
                event = "signal-stop-sent"
            elif isinstance(exc_type, type) and issubclass(exc_type, KeyboardInterrupt):
                event = "operator-interrupt-stop-sent"
            else:
                event = "error-stop-sent"
            self.stop(event)
        for signum, handler in self._old_handlers.items():
            try:
                signal.signal(signum, handler)
            except OSError, ValueError:
                continue

    def mark_started(self, event: str) -> None:
        """Mark the operation live after its start frame has been sent."""

        self._active = True
        if self.logger is not None:
            self.logger.write_event(event)

    def stop(self, event: str, *, raise_errors: bool = False) -> bool:
        """Send STOP once and record whether that attempt succeeded."""

        if not self._active or self._stopping:
            return True
        self._stopping = True
        try:
            self.tester.stop()
        except Exception:
            if self.logger is not None:
                self.logger.write_event(f"{event}-failed")
            if raise_errors:
                raise
            return False
        else:
            if self.logger is not None:
                self.logger.write_event(event)
            return True
        finally:
            self._active = False

    def _handle_signal(self, signum: int, _frame: object) -> None:
        if not self._stopping:
            raise _SessionSignal(signum)


def read_live_status(tester: Tester) -> EbcStatus:
    """Read a status update, falling back to an explicit poll if needed."""

    try:
        return tester.next_status()
    except DeviceError:
        # Some firmware revisions only emit status after CONNECT. Keeping the
        # fallback here lets a supervised session log both variants while a
        # second failure is surfaced to the lifecycle guard.
        return tester.status()


def run_active_session(
    *,
    session: SupervisedSession,
    tester: Tester,
    logger: StatusLogger | None,
    printer: Callable[[EbcStatus], None],
    timeout_minutes: int,
    sample_event: str,
    stop_on_cutoff: bool = False,
    poll_interval: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Log live status until cutoff or timeout, stopping the tester on exit."""

    session_started = clock()
    deadline = session_started + timeout_minutes * 60
    samples = 0
    while True:
        if clock() >= deadline:
            session.stop("timeout-stop-sent", raise_errors=True)
            print("Hard timeout reached; stop frame sent.", flush=True)
            print(f"Logged {samples} active sample(s)", flush=True)
            return

        status = read_live_status(tester)
        printer(status)
        if logger is not None:
            logger.write(
                status,
                event=sample_event,
                session_elapsed_seconds=clock() - session_started,
            )
        samples += 1

        # The bundled controller maps a CC discharge auto-off to 0x14. This
        # signal is meaningful for capacity tests; charge modes remain bounded
        # by their configured timeout because completion codes vary.
        if stop_on_cutoff and status.raw[1] == 0x14:
            session.stop("device-cutoff-stop-sent", raise_errors=True)
            print("Device cutoff reached; stop frame sent.", flush=True)
            print(f"Logged {samples} active sample(s)", flush=True)
            return

        remaining = deadline - clock()
        if remaining <= 0:
            session.stop("timeout-stop-sent", raise_errors=True)
            print("Hard timeout reached; stop frame sent.", flush=True)
            print(f"Logged {samples} active sample(s)", flush=True)
            return
        sleeper(min(poll_interval, remaining))
