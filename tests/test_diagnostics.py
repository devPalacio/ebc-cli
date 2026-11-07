import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ebc_cli.cli import main
from ebc_cli.diagnostics import diagnose_device, permission_hint


class DiagnosticsTests(unittest.TestCase):
    def hint(self, account_groups, process_groups):
        with (
            patch("ebc_cli.diagnostics.Path.stat", return_value=SimpleNamespace(st_gid=986)),
            patch(
                "ebc_cli.diagnostics.grp.getgrgid", return_value=SimpleNamespace(gr_name="dialout")
            ),
            patch(
                "ebc_cli.diagnostics.pwd.getpwuid",
                return_value=SimpleNamespace(pw_name="jay", pw_gid=1000),
            ),
            patch("ebc_cli.diagnostics.os.getgrouplist", return_value=account_groups),
            patch("ebc_cli.diagnostics.os.getgroups", return_value=process_groups),
            patch("ebc_cli.diagnostics.os.getegid", return_value=1000),
        ):
            return permission_hint("/dev/test")

    def test_stale_membership_has_password_free_remedy(self):
        hint = self.hint([1000, 986], [1000])
        self.assertIn("newgrp dialout", hint)
        self.assertNotIn("usermod", hint)

    def test_missing_membership_has_setup_remedy(self):
        self.assertIn("sudo usermod -aG dialout jay", self.hint([1000], [1000]))

    def test_existing_membership_does_not_recommend_group_change(self):
        self.assertIn("ACLs", self.hint([1000, 986], [1000, 986]))

    def test_missing_device(self):
        with tempfile.TemporaryDirectory() as directory:
            report, ready = diagnose_device(Path(directory) / "missing")
        self.assertFalse(ready)
        self.assertIn("Unavailable", report)

    def test_doctor_does_not_open_serial_device(self):
        with (
            patch("ebc_cli.device.os.open") as open_device,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["doctor", "--device", "/dev/null"]), 0)
        open_device.assert_not_called()

    def test_visible_lock_is_reported(self):
        info = os.stat("/dev/null")
        key = f"{os.major(info.st_dev):x}:{os.minor(info.st_dev):x}:{info.st_ino}"
        with patch(
            "ebc_cli.diagnostics.Path.read_text",
            return_value=f"1: FLOCK ADVISORY WRITE 123 {key} 0 EOF",
        ):
            report, ready = diagnose_device("/dev/null")
        self.assertFalse(ready)
        self.assertIn("PID 123", report)
