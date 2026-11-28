#!/usr/bin/env python3
"""The public config template must stay valid and obviously unconfigured."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ConfigExampleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))

    def test_template_has_the_sections_the_services_read(self) -> None:
        for key in ("models", "defaults", "projects", "watcher", "limits", "release",
                    "verification", "t3"):
            self.assertIn(key, self.data)
        self.assertIn("cli", self.data["t3"])
        self.assertIn("dbPath", self.data["t3"])
        self.assertFalse(self.data["release"]["enabled"], "public template must not push by default")
        self.assertEqual(self.data["models"], {}, "do not ship a fake provider/model pair")

    def test_template_cannot_create_a_ticket_with_a_fake_model(self) -> None:
        self.assertEqual(self.data["defaults"], {})
        self.assertNotIn("T3_BOARD_TOKEN", json.dumps(self.data))
        self.assertNotIn("botToken", json.dumps(self.data))

    def test_the_public_installation_pages_exist_and_name_t3(self) -> None:
        for name in ("installation.md", "agent-setup.md", "second-brain.md"):
            text = (ROOT / "docs" / name).read_text(encoding="utf-8")
            self.assertGreater(len(text), 1000, name)
        install = (ROOT / "docs" / "installation.md").read_text(encoding="utf-8")
        self.assertIn("T3 Code is required", install)
        self.assertIn("t3.codes/install.sh", install)
        brain = (ROOT / "docs" / "second-brain.md").read_text(encoding="utf-8")
        self.assertIn("## Setup", brain)
        self.assertIn("BRAIN_VAULT", brain)


if __name__ == "__main__":
    unittest.main(verbosity=2)
