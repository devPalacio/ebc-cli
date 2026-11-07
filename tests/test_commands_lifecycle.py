import argparse
import contextlib
import os
import subprocess
import sys
import unittest
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Self
from unittest.mock import patch

from ebc_cli import commands, runner, session
from ebc_cli.device import DeviceError
from ebc_cli.modes import EbcModeCommand
from ebc_cli.profiles import make_recovery_profile
from ebc_cli.resources import CommandResources


class CommandLifecycleTests(unittest.TestCase):
    def test_profile_enters_supervision_before_start_and_logs_baseline_after_start(self) -> None:
        events: list[str] = []
        tester = _RecordingTester(events)
        logger = _RecordingLogger(events)
        resources = CommandResources(tester=tester, logger=logger, log_path=None)
        args = _args()
        result = session.SessionResult(
            stop_reason=session.SessionStopReason.TIMEOUT,
            samples=0,
            elapsed_seconds=0.0,
        )

        with (
            patch.object(
                commands, "_open_resources", return_value=contextlib.nullcontext(resources)
            ),
            patch.object(runner, "run_active_session", return_value=result),
            patch.object(commands, "print_status"),
            patch.object(commands, "_report_log"),
            patch.object(runner, "SupervisedSession", _RecordingSession),
        ):
            commands._run_profile(args, make_recovery_profile(current_amps=0.2, timeout_minutes=1))

        self.assertEqual(
            events,
            [
                "session-enter",
                "profile-start",
                "event:recovery-started",
                "baseline-log",
                "session-stop",
                "event:session-stop-sent",
                "session-exit",
            ],
        )

    def test_profile_start_validation_failure_does_not_stop_unknown_device(self) -> None:
        events: list[str] = []
        tester = _RecordingTester(events, start_error=DeviceError("unknown tester"))
        resources = CommandResources(tester=tester, logger=None, log_path=None)

        with (
            patch.object(
                commands, "_open_resources", return_value=contextlib.nullcontext(resources)
            ),
            self.assertRaisesRegex(DeviceError, "unknown tester"),
        ):
            commands._run_profile(
                _args(), make_recovery_profile(current_amps=0.2, timeout_minutes=1)
            )

        self.assertEqual(events, ["profile-start"])
        self.assertNotIn("session-stop", events)

    def test_profile_baseline_logging_failure_stops_started_device(self) -> None:
        events: list[str] = []
        tester = _RecordingTester(events)
        logger = _RecordingLogger(events, baseline_error=DeviceError("log unavailable"))
        resources = CommandResources(tester=tester, logger=logger, log_path=None)

        with (
            patch.object(
                commands, "_open_resources", return_value=contextlib.nullcontext(resources)
            ),
            self.assertRaisesRegex(DeviceError, "log unavailable"),
            patch.object(commands, "print_status"),
        ):
            commands._run_profile(
                _args(), make_recovery_profile(current_amps=0.2, timeout_minutes=1)
            )

        self.assertEqual(
            events,
            [
                "profile-start",
                "event:recovery-started",
                "baseline-log",
                "session-stop",
                "event:error-stop-sent",
            ],
        )

    def test_stop_sends_frame_when_log_directory_fails(self) -> None:
        tester = _StopTester()
        resources = CommandResources(tester=tester, logger=None, log_path=None)
        with TemporaryDirectory() as temporary_directory:
            blocked_log_dir = Path(temporary_directory) / "file"
            blocked_log_dir.write_text("not a directory", encoding="utf-8")
            args = _args(log_dir=blocked_log_dir, no_log=False, command="stop", execute=True)
            stdout = StringIO()
            stderr = StringIO()
            with (
                patch.object(
                    commands,
                    "_open_resources",
                    return_value=contextlib.nullcontext(resources),
                ) as open_resources,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                exit_code = commands.handle_stop(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(tester.stop_calls, 1)
        self.assertIn("Stop frame sent.", stdout.getvalue())
        self.assertIn("Stop frame sent, but logging failed:", stderr.getvalue())
        open_resources.assert_called_once_with(args, lock_device=False, no_log=True)

    def test_sigterm_during_baseline_logging_stops_single_profile(self) -> None:
        completed = _run_sigterm_child("recover-lead-acid")

        self.assertEqual(completed.returncode, 143)
        self.assertIn("START", completed.stdout)
        self.assertIn("BASELINE", completed.stdout)
        self.assertIn("STOP", completed.stdout)
        self.assertIn("EXIT:143", completed.stdout)
        self.assertLess(completed.stdout.index("START"), completed.stdout.index("STOP"))
        self.assertEqual(completed.stdout.count("STOP"), 1)

    def test_sigterm_during_baseline_logging_stops_bulk_to_float_profile(self) -> None:
        completed = _run_sigterm_child("bulk-to-float")

        self.assertEqual(completed.returncode, 143)
        self.assertIn("START", completed.stdout)
        self.assertIn("BASELINE", completed.stdout)
        self.assertIn("STOP", completed.stdout)
        self.assertIn("EXIT:143", completed.stdout)
        self.assertLess(completed.stdout.index("START"), completed.stdout.index("STOP"))
        self.assertEqual(completed.stdout.count("STOP"), 1)


class _RecordingTester:
    def __init__(self, events: list[str], start_error: BaseException | None = None):
        self.events = events
        self.start_error = start_error

    def begin_mode(self, _command: EbcModeCommand) -> object:
        self.events.append("profile-start")
        if self.start_error is not None:
            raise self.start_error
        return object()

    def stop(self) -> None:
        self.events.append("session-stop")


class _RecordingLogger:
    def __init__(self, events: list[str], baseline_error: BaseException | None = None):
        self.events = events
        self.baseline_error = baseline_error

    def write_event(self, event: str) -> None:
        self.events.append(f"event:{event}")

    def write(self, _status: object, **_kwargs: object) -> None:
        self.events.append("baseline-log")
        if self.baseline_error is not None:
            raise self.baseline_error


class _StopTester:
    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


class _RecordingSession:
    def __init__(self, tester: _RecordingTester, logger: _RecordingLogger | None):
        self.tester = tester
        self.logger = logger
        self.active = False

    def __enter__(self) -> Self:
        self.tester.events.append("session-enter")
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        if self.active:
            self.tester.stop()
            if self.logger is not None:
                self.logger.write_event("session-stop-sent")
        self.tester.events.append("session-exit")

    def mark_started(self, event: str) -> None:
        self.active = True
        if self.logger is not None:
            self.logger.write_event(event)


def _args(**overrides: object) -> argparse.Namespace:
    values = {
        "device": "/dev/test",
        "timeout": 1.0,
        "command": "recover-lead-acid",
        "log_dir": "/tmp/ebc-test-logs",
        "no_log": True,
        "execute": True,
        "i_am_present": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _run_sigterm_child(command: str) -> subprocess.CompletedProcess[str]:
    script = r"""
import contextlib
import os
import signal
import sys

from ebc_cli import cli, commands
from ebc_cli.resources import CommandResources


class Tester:
    def begin_mode(self, _command):
        print("START", flush=True)
        return object()

    def stop(self):
        print("STOP", flush=True)


class Logger:
    def write_event(self, event):
        print(f"EVENT:{event}", flush=True)

    def write(self, _status, **_kwargs):
        print("BASELINE", flush=True)
        os.kill(os.getpid(), signal.SIGTERM)


tester = Tester()
logger = Logger()
resources = CommandResources(tester=tester, logger=logger, log_path=None)
commands._open_resources = lambda _args: contextlib.nullcontext(resources)
if sys.argv[1] == "recover-lead-acid":
    argv = ["recover-lead-acid", "--execute", "--i-am-present"]
else:
    argv = ["bulk-to-float", "--execute", "--i-am-present"]
exit_code = cli.main(argv)
print(f"EXIT:{exit_code}", flush=True)
sys.exit(exit_code)
"""
    environment = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_root
        if not existing_pythonpath
        else f"{source_root}{os.pathsep}{existing_pythonpath}"
    )
    return subprocess.run(
        [sys.executable, "-c", script, command],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )


if __name__ == "__main__":
    unittest.main()
