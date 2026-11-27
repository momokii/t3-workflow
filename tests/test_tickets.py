#!/usr/bin/env python3
"""Tests for the ticket store and the dry-run watcher."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tickets"))

from store import TicketError, TicketStore, resolve_profile  # noqa: E402

SPEC = importlib.util.spec_from_file_location("watcher", ROOT / "watcher.py")
watcher = importlib.util.module_from_spec(SPEC)
sys.modules["watcher"] = watcher
SPEC.loader.exec_module(watcher)

CONFIG = {
    "models": {
        "fast": {"provider": "pi", "model": "opencode-go/deepseek-v4.1-flash"},
        "deep": {"provider": "pi", "model": "opencode-go/qwen3.8-max"},
    },
    "defaults": {"profile": "fast"},
    "t3": {"cli": "/bin/true"},
}


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["T3_REPORTS_DIR"] = str(Path(tempfile.mkdtemp(prefix="reports-")) / "reports")
        self.tmp = Path(tempfile.mkdtemp(prefix="tickets-"))
        self.config = self.tmp / "config.json"
        self.config.write_text(json.dumps(CONFIG))
        self.store = TicketStore(self.tmp / "t.sqlite", self.config)

    def tearDown(self) -> None:
        self.store.close()

    def test_add_assigns_sequential_ids(self) -> None:
        first = self.store.add("one", "t3-workflow", model_profile="fast")
        second = self.store.add("two", "t3-workflow", model_profile="deep")
        self.assertEqual(first["id"], "T-0001")
        self.assertEqual(second["id"], "T-0002")

    def test_add_rejects_unknown_profile(self) -> None:
        with self.assertRaises(TicketError) as ctx:
            self.store.add("x", "t3-workflow", model_profile="no-such-profile")
        self.assertIn("is not in config", str(ctx.exception))

    def test_transitions_allowed_and_rejected(self) -> None:
        ticket = self.store.add("x", "t3-workflow", model_profile="fast", status="backlog")
        ticket = self.store.move(ticket["id"], "ready")
        self.assertEqual(ticket["status"], "ready")
        with self.assertRaises(TicketError):
            self.store.move(ticket["id"], "done")  # ready → done is not allowed

    def test_claim_is_atomic(self) -> None:
        ticket = self.store.add("x", "t3-workflow", model_profile="fast", status="ready")
        self.store.claim(ticket["id"], "watcher-a")
        with self.assertRaises(TicketError):
            self.store.claim(ticket["id"], "watcher-b")
        self.assertEqual(self.store.get(ticket["id"])["claimed_by"], "watcher-a")

    def test_claim_requires_ready(self) -> None:
        ticket = self.store.add("x", "t3-workflow", model_profile="fast", status="backlog")
        with self.assertRaises(TicketError):
            self.store.claim(ticket["id"], "watcher")

    def test_next_ready_orders_by_priority_then_age(self) -> None:
        low = self.store.add("low", "t3-workflow", model_profile="fast", status="ready", priority=4)
        high = self.store.add("high", "t3-workflow", model_profile="fast", status="ready", priority=1)
        self.assertEqual(self.store.next_ready()["id"], high["id"])
        self.store.claim(high["id"], "w")
        self.assertEqual(self.store.next_ready()["id"], low["id"])

    def test_events_recorded(self) -> None:
        ticket = self.store.add("x", "t3-workflow", model_profile="fast", status="ready")
        self.store.claim(ticket["id"], "w")
        kinds = [event["kind"] for event in self.store.events(ticket["id"])]
        self.assertEqual(kinds, ["created", "claimed"])

    def test_resolve_profile_defaults(self) -> None:
        profile = resolve_profile(CONFIG, "")
        self.assertEqual(profile["name"], "fast")
        self.assertEqual(profile["provider"], "pi")


class WatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="watcher-"))
        self.config_path = self.tmp / "config.json"
        self.config_path.write_text(json.dumps(CONFIG))
        self.store = TicketStore(self.tmp / "t.sqlite", self.config_path)

    def tearDown(self) -> None:
        self.store.close()

    def test_dry_run_changes_nothing(self) -> None:
        ticket = self.store.add("uji", "/tmp/project", model_profile="deep", status="ready")
        message = watcher.run_once(self.store, CONFIG, "/bin/true", dry_run=True)
        self.assertIn("[dry-run]", message)
        self.assertIn("deep", message)
        self.assertEqual(self.store.get(ticket["id"])["status"], "ready")

    def test_run_starts_thread_and_records_it(self) -> None:
        ticket = self.store.add("uji", "/tmp/project", model_profile="fast", status="ready")
        calls: list[list[str]] = []

        def fake_runner(command, input=None, capture_output=True, text=True, timeout=None):  # noqa: ARG001
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, '{"threadId":"abc-123"}', "")

        message = watcher.run_once(self.store, CONFIG, "/bin/true", dry_run=False, runner=fake_runner)
        self.assertIn("abc-123", message)
        stored = self.store.get(ticket["id"])
        self.assertEqual(stored["status"], "in-progress")
        self.assertEqual(stored["thread_id"], "abc-123")
        self.assertIn("--provider", calls[0])
        self.assertIn("pi", calls[0])
        self.assertIn("opencode-go/deepseek-v4.1-flash", calls[0])

    def test_failed_start_blocks_ticket(self) -> None:
        ticket = self.store.add("uji", "/tmp/project", model_profile="fast", status="ready")

        def failing_runner(command, **kwargs):  # noqa: ARG001
            return subprocess.CompletedProcess(command, 3, "", "boom")

        with self.assertRaises(TicketError):
            watcher.run_once(self.store, CONFIG, "/bin/true", dry_run=False, runner=failing_runner)
        self.assertEqual(self.store.get(ticket["id"])["status"], "blocked")

    def test_unknown_profile_blocks_before_claim(self) -> None:
        ticket = self.store.add("probe", "/tmp/project", status="ready")  # no profile, a default exists
        config = json.loads(json.dumps(CONFIG))
        config["defaults"] = {}
        with self.assertRaises(TicketError):
            watcher.run_once(self.store, config, "/bin/true", dry_run=False)
        self.assertEqual(self.store.get(ticket["id"])["status"], "ready")


if __name__ == "__main__":
    unittest.main(verbosity=2)
