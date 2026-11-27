#!/usr/bin/env python3
"""Concurrency limit and time budget."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tickets"))

from store import TicketStore  # noqa: E402

SPEC = importlib.util.spec_from_file_location("watcher", ROOT / "watcher.py")
watcher = importlib.util.module_from_spec(SPEC)
sys.modules["watcher"] = watcher
SPEC.loader.exec_module(watcher)

CONFIG = {
    "models": {"fast": {"provider": "pi", "model": "opencode-go/deepseek-v4.1-flash"}},
    "defaults": {"profile": "fast"},
    "watcher": {"maxConcurrent": 1},
    "limits": {"maxInProgressMinutes": 60},
}


class LimitTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["T3_REPORTS_DIR"] = str(Path(tempfile.mkdtemp(prefix="lim-reports-")) / "reports")
        self.tmp = Path(tempfile.mkdtemp(prefix="limits-"))
        self.config_path = self.tmp / "config.json"
        self.config_path.write_text(json.dumps(CONFIG))
        self.store = TicketStore(self.tmp / "t.sqlite", self.config_path)

    def tearDown(self) -> None:
        self.store.close()

    def test_ready_ticket_waits_when_slot_full(self) -> None:
        self.store.add("aktif", "/tmp/p", model_profile="fast", status="in-progress")
        self.store.add("baru", "/tmp/p", model_profile="fast", status="ready")
        message = watcher.step(self.store, CONFIG, "/bin/true", dry_run=False)
        self.assertIn("wait", message)
        self.assertNotIn("thread", message)

    def test_ready_ticket_starts_when_slot_free(self) -> None:
        self.store.add("baru", "/tmp/p", model_profile="fast", status="ready")

        def fake(command, **kwargs):  # noqa: ARG001
            return subprocess.CompletedProcess(command, 0, '{"threadId":"t-9"}', "")

        message = watcher.step(self.store, CONFIG, "/bin/true", dry_run=False, runner=fake)
        self.assertIn("t-9", message)

    def test_budget_marks_stale_ticket_blocked(self) -> None:
        ticket = self.store.add("macet", "/tmp/p", model_profile="fast", status="in-progress")
        old = (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat()
        self.store.con.execute("update tickets set claimed_at=? where id=?", (old, ticket["id"]))
        self.store.con.commit()

        notes = watcher.enforce_budget(self.store, CONFIG)
        self.assertTrue(notes)
        self.assertEqual(self.store.get(ticket["id"])["status"], "blocked")
        self.assertIn("budget", self.store.get(ticket["id"])["note"])

    def test_budget_disabled_does_nothing(self) -> None:
        ticket = self.store.add("macet", "/tmp/p", model_profile="fast", status="in-progress")
        old = (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat()
        self.store.con.execute("update tickets set claimed_at=? where id=?", (old, ticket["id"]))
        self.store.con.commit()
        config = json.loads(json.dumps(CONFIG))
        config["limits"]["maxInProgressMinutes"] = 0
        self.assertEqual(watcher.enforce_budget(self.store, config), [])
        self.assertEqual(self.store.get(ticket["id"])["status"], "in-progress")


if __name__ == "__main__":
    unittest.main(verbosity=2)
