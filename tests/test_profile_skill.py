# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Jay (Python CLI contributions)
# See NOTICE for upstream attribution and port details.

import contextlib
import re
import shlex
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from ebc_cli.cli import main

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/create-charge-profile/SKILL.md"


class ChargeProfileSkillTests(unittest.TestCase):
    def test_skill_metadata_and_links(self):
        content = SKILL.read_text()
        self.assertTrue(content.startswith("---\n"))
        metadata, separator, body = content[4:].partition("\n---\n")
        self.assertTrue(separator)
        self.assertTrue(body.strip())
        self.assertIn("name: create-charge-profile\n", metadata)
        self.assertIn("license: GPL-3.0-only\n", metadata)
        self.assertIn("platforms: [linux]\n", metadata)
        description = next(
            line.removeprefix("description: ")
            for line in metadata.splitlines()
            if line.startswith("description: ")
        )
        self.assertLessEqual(len(description), 60)
        self.assertTrue(description.endswith("."))
        self.assertNotIn("/home/", content)
        for link in re.findall(r"\]\(([^)]+)\)", body):
            with self.subTest(link=link):
                self.assertTrue((SKILL.parent / link).resolve().is_file())

    def test_documented_example_runs_without_opening_a_tester(self):
        examples = re.findall(r"```bash\n(.*?)```", SKILL.read_text(), re.DOTALL)
        self.assertTrue(examples)
        for example in examples:
            tokens = shlex.split(example)
            self.assertEqual(tokens[:3], ["uv", "run", "ebc"])
            self.assertEqual(tokens[3], "plan-mode")
            self.assertNotIn("--execute", tokens)
            output = StringIO()
            with (
                patch(
                    "ebc_cli.device.EbcDevice.open",
                    side_effect=AssertionError("unexpected serial access"),
                ) as open_device,
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(main(tokens[3:]), 0)
            open_device.assert_not_called()
            self.assertIn("PLAN ONLY", output.getvalue())
            self.assertIn("FA71003205AA007894F8", output.getvalue())


if __name__ == "__main__":
    unittest.main()
