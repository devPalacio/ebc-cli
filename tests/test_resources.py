import csv
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from ebc_cli.resources import command_resources


class ResourceTests(unittest.TestCase):
    def test_no_log_scope_owns_only_the_tester(self) -> None:
        tester = _tester_mock()
        with (
            patch("ebc_cli.resources.EbcDevice", return_value=tester) as device_factory,
            command_resources(
                device="/dev/test",
                timeout=1.0,
                command="status",
                log_dir="/tmp/not-used",
                no_log=True,
            ) as resources,
        ):
            self.assertIs(resources.tester, tester)
            self.assertIsNone(resources.logger)
            self.assertIsNone(resources.log_path)

        tester.__enter__.assert_called_once_with()
        tester.__exit__.assert_called_once_with(None, None, None)
        device_factory.assert_called_once_with("/dev/test", 1.0, lock_device=True)

    def test_lock_bypass_is_forwarded_for_emergency_scope(self) -> None:
        tester = _tester_mock()
        with (
            patch("ebc_cli.resources.EbcDevice", return_value=tester) as device_factory,
            command_resources(
                device="/dev/test",
                timeout=1.0,
                command="stop",
                log_dir="/tmp/not-used",
                no_log=True,
                lock_device=False,
            ),
        ):
            pass

        device_factory.assert_called_once_with("/dev/test", 1.0, lock_device=False)

    def test_logged_scope_creates_the_timestamped_csv(self) -> None:
        tester = _tester_mock()
        with TemporaryDirectory() as temporary_directory:
            with (
                patch("ebc_cli.resources.EbcDevice", return_value=tester),
                command_resources(
                    device="/dev/test",
                    timeout=1.0,
                    command="status",
                    log_dir=temporary_directory,
                    no_log=False,
                ) as resources,
            ):
                self.assertIs(resources.tester, tester)
                self.assertIsNotNone(resources.logger)
                self.assertIsNotNone(resources.log_path)
                self.assertTrue(resources.log_path.name.endswith("_status.csv"))
                self.assertTrue(resources.log_path.exists())

            with resources.log_path.open(newline="", encoding="utf-8") as log_file:
                rows = list(csv.reader(log_file))

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "timestamp")
        self.assertEqual(rows[0][1], "event")
        self.assertEqual(rows[1][1], "schema-v1")


def _tester_mock() -> MagicMock:
    tester = MagicMock()
    tester.__enter__.return_value = tester
    return tester


if __name__ == "__main__":
    unittest.main()
