#!/usr/bin/env python3
"""The state snapshot is generated, not hand-written."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tickets"))

from store import TicketStore  # noqa: E402

CONFIG = {"models": {"fast": {"provider": "pi", "model": "opencode-go/deepseek-v4.1-flash"}},
          "defaults": {"profile": "fast"}}


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="snapshot-"))
        self.config = self.tmp / "config.json"
        self.config.write_text(json.dumps(CONFIG))
        self.caches = self.tmp / "caches"
        self.caches.mkdir()
        (self.caches / "pi.json").write_text(json.dumps({
            "instanceId": "pi", "displayName": "Pi", "enabled": True, "installed": True,
            "status": "ready", "models": [{"slug": "m1", "name": "M1"}],
        }))
        self.db = self.tmp / "t.sqlite"
        store = TicketStore(self.db, self.config)
        store.add("tiket snapshot", "/tmp/p", model_profile="fast", status="ready")
        store.close()
        self.out = self.tmp / "STATE.md"

    def render(self) -> str:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts/snapshot.py"),
             "--out", str(self.out), "--db", str(self.db), "--config", str(self.config),
             "--catalog-dir", str(self.caches), "--no-services"],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return self.out.read_text()

    def test_writes_every_section(self) -> None:
        text = self.render()
        for heading in ("## Git", "## Services", "## Tickets", "## Model catalogue", "## Documentation"):
            self.assertIn(heading, text)

    def test_reflects_git_and_tickets(self) -> None:
        text = self.render()
        head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        self.assertIn(head, text)
        self.assertIn("T-0001", text)
        self.assertIn("tiket snapshot", text)
        self.assertIn("`pi` | True | 1", text)

    def test_is_not_a_hand_edited_file(self) -> None:
        text = self.render()
        self.assertIn("Do not hand-edit", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
