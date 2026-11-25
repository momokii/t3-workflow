#!/usr/bin/env python3
"""Tests for proofs, approvals and the two-stage watcher."""

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

from store import TicketError, TicketStore  # noqa: E402

SPEC = importlib.util.spec_from_file_location("watcher", ROOT / "watcher.py")
watcher = importlib.util.module_from_spec(SPEC)
sys.modules["watcher"] = watcher
SPEC.loader.exec_module(watcher)

CONFIG = {
    "models": {"fast": {"provider": "pi", "model": "opencode-go/deepseek-v4.1-flash"}},
    "defaults": {"profile": "fast"},
}


def ok_runner(command, **kwargs):  # noqa: ARG001
    return subprocess.CompletedProcess(command, 0, "3 tests OK", "")


def fail_runner(command, **kwargs):  # noqa: ARG001
    return subprocess.CompletedProcess(command, 1, "1 test FAILED", "")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["T3_REPORTS_DIR"] = str(Path(tempfile.mkdtemp(prefix="reports-")) / "reports")
        self.tmp = Path(tempfile.mkdtemp(prefix="flow-"))
        self.config_path = self.tmp / "config.json"
        self.config_path.write_text(json.dumps(CONFIG))
        self.store = TicketStore(self.tmp / "t.sqlite", self.config_path)

    def tearDown(self) -> None:
        self.store.close()


class ProofTests(Base):
    def test_attach_proof_stores_text_and_event(self) -> None:
        ticket = self.store.add("x", "/tmp/p", model_profile="fast", status="testing")
        self.store.attach_proof(ticket["id"], True, "output baris 1")
        stored = self.store.get(ticket["id"])
        self.assertIn("[LULUS]", stored["proof"])
        self.assertIn("output baris 1", stored["proof"])
        kinds = [event["kind"] for event in self.store.events(ticket["id"])]
        self.assertIn("proof", kinds)

    def test_report_contains_proof(self) -> None:
        ticket = self.store.add("x", "/tmp/p", model_profile="fast", status="testing")
        self.store.attach_proof(ticket["id"], False, "gagal di test 3")
        report = self.store.report(ticket["id"])
        self.assertIn("### Bukti test", report)
        self.assertIn("gagal di test 3", report)


class ApprovalTests(Base):
    def test_request_is_deduplicated(self) -> None:
        ticket = self.store.add("x", "/tmp/p", model_profile="fast", status="ready", requires_approval=True)
        first = self.store.request_approval(ticket["id"], "mulai?")
        second = self.store.request_approval(ticket["id"], "mulai lagi?")
        self.assertEqual(first["approval_id"], second["approval_id"])
        self.assertEqual(len(self.store.pending_approvals()), 1)

    def test_decision_is_one_shot(self) -> None:
        ticket = self.store.add("x", "/tmp/p", model_profile="fast", status="ready", requires_approval=True)
        approval = self.store.request_approval(ticket["id"], "mulai?")
        self.store.decide_approval(approval["approval_id"], True, "kelanach")
        self.assertEqual(self.store.approval_state(ticket["id"]), "approved")
        with self.assertRaises(TicketError):
            self.store.decide_approval(approval["approval_id"], False, "kelanach")

    def test_reject_then_gate_blocks(self) -> None:
        ticket = self.store.add("x", "/tmp/p", model_profile="fast", status="ready", requires_approval=True)
        approval = self.store.request_approval(ticket["id"], "mulai?")
        self.store.decide_approval(approval["approval_id"], False, "kelanach")
        message = watcher.run_once(self.store, CONFIG, "/bin/true", dry_run=False)
        self.assertIn("ditolak", message)
        self.assertEqual(self.store.get(ticket["id"])["status"], "blocked")


class TestStageTests(Base):
    def test_success_moves_to_review_with_proof(self) -> None:
        ticket = self.store.add("x", "/tmp/p", model_profile="fast", status="testing", test_cmd="true")
        message = watcher.run_test(self.store, ticket, runner=ok_runner)
        self.assertIn("lulus", message)
        stored = self.store.get(ticket["id"])
        self.assertEqual(stored["status"], "review")
        self.assertIn("LULUS", stored["proof"])

    def test_failure_returns_to_in_progress(self) -> None:
        ticket = self.store.add("x", "/tmp/p", model_profile="fast", status="testing", test_cmd="false")
        message = watcher.run_test(self.store, ticket, runner=fail_runner)
        self.assertIn("gagal", message)
        stored = self.store.get(ticket["id"])
        self.assertEqual(stored["status"], "in-progress")
        self.assertIn("GAGAL", stored["proof"])

    def test_missing_test_cmd_does_not_advance(self) -> None:
        ticket = self.store.add("x", "/tmp/p", model_profile="fast", status="testing")
        watcher.run_test(self.store, ticket, runner=ok_runner)
        self.assertEqual(self.store.get(ticket["id"])["status"], "in-progress")

    def test_step_prefers_testing_over_ready(self) -> None:
        testing = self.store.add("uji", "/tmp/p", model_profile="fast", status="testing", test_cmd="true")
        self.store.add("baru", "/tmp/p", model_profile="fast", status="ready")
        message = watcher.step(self.store, CONFIG, "/bin/true", dry_run=False, runner=ok_runner)
        self.assertIn(testing["id"], message)
        self.assertEqual(self.store.get(testing["id"])["status"], "review")


class ApprovalGateTests(Base):
    def test_gate_requests_and_does_not_claim(self) -> None:
        ticket = self.store.add("x", "/tmp/p", model_profile="fast", status="ready", requires_approval=True)
        message = watcher.run_once(self.store, CONFIG, "/bin/true", dry_run=False)
        self.assertIn("butuh persetujuan", message)
        self.assertEqual(self.store.get(ticket["id"])["status"], "ready")
        self.assertEqual(len(self.store.pending_approvals()), 1)

    def test_gate_opens_after_approval(self) -> None:
        ticket = self.store.add("x", "/tmp/p", model_profile="fast", status="ready", requires_approval=True)
        approval = self.store.request_approval(ticket["id"], "mulai?")
        self.store.decide_approval(approval["approval_id"], True, "kelanach")

        def fake(command, **kwargs):  # noqa: ARG001
            return subprocess.CompletedProcess(command, 0, '{"threadId":"t-1"}', "")

        message = watcher.run_once(self.store, CONFIG, "/bin/true", dry_run=False, runner=fake)
        self.assertIn("t-1", message)
        self.assertEqual(self.store.get(ticket["id"])["status"], "in-progress")


if __name__ == "__main__":
    unittest.main(verbosity=2)
