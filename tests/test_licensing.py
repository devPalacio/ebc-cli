# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Jay (Python CLI contributions)
# See NOTICE for upstream attribution and port details.

import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LicensingTests(unittest.TestCase):
    def test_package_declares_gpl_and_includes_notices(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        self.assertEqual(project["license"], "GPL-3.0-only")
        self.assertEqual(project["license-files"], ["LICENSE", "NOTICE"])
        self.assertFalse(any(value.startswith("License ::") for value in project["classifiers"]))

    def test_license_and_port_notice_are_present(self):
        license_text = (ROOT / "LICENSE").read_text()
        self.assertTrue(license_text.lstrip().startswith("GNU GENERAL PUBLIC LICENSE"))
        self.assertIn("Version 3, 29 June 2007", license_text)
        self.assertIn("END OF TERMS AND CONDITIONS", license_text)
        notice = (ROOT / "NOTICE").read_text()
        self.assertIn("GPL-3.0-only", notice)
        self.assertIn("JOGAsoft", notice)
        self.assertIn("Port/modification notice, 2026-11-07", notice)
        self.assertIn("WITHOUT ANY WARRANTY", notice)

    def test_package_sources_identify_license_and_attribution(self):
        sources = sorted((ROOT / "src" / "ebc_cli").glob("*.py"))
        self.assertTrue(sources)
        for source in sources:
            with self.subTest(source=source.name):
                text = source.read_text()
                self.assertTrue(text.startswith("# SPDX-License-Identifier: GPL-3.0-only\n"))
                self.assertIn("# See NOTICE for upstream attribution and port details.", text)


if __name__ == "__main__":
    unittest.main()
