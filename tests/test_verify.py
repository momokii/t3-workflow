#!/usr/bin/env python3
"""The verification stage: a fresh thread judges the work before release.

Hermetic: a real temporary git repository (the reviewer needs a real diff), a fake `t3`
runner that answers `thread start/show/send`, and a ticket database in a temp directory.
No T3 call, no network, no report directory outside the temp tree.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tickets"))

from store import TicketError, TicketStore  # noqa: E402

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
    "verification": {"enabled": True, "maxAttempts": 2, "timeoutMinutes": 20, "maxDiffBytes": 60000},
}


class FakeT3:
    """Answers the three CLI calls the stage makes; everything else is a test bug."""

    def __init__(self, status: str = "completed", reply: str = "VERDICT: ACCEPT",
                 start_rc: int = 0, send_rc: int = 0) -> None:
        self.status = status
        self.reply = reply
        self.start_rc = start_rc
        self.send_rc = send_rc
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, command, input=None, capture_output=True, text=True, timeout=None):  # noqa: ARG002
        self.calls.append((command, input))
        name = command[1:3]
        if name == ["thread", "start"]:
            return subprocess.CompletedProcess(
                command, self.start_rc, json.dumps({"threadId": "rev-1", "runId": "run-1"}), ""
            )
        if name == ["thread", "show"]:
            body = {"thread": {"status": self.status}, "messages": [{"text": self.reply}]}
            return subprocess.CompletedProcess(command, 0, json.dumps(body), "")
        if name == ["thread", "send"]:
            return subprocess.CompletedProcess(command, self.send_rc, '{"ok":true}', "")
        raise AssertionError(f"unexpected command: {command}")

    def inputs(self, verb: str) -> list[str | None]:
        return [text for command, text in self.calls if command[1:3] == ["thread", verb]]


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class VerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="verify-"))
        os.environ["T3_REPORTS_DIR"] = str(self.tmp / "reports")
        self.config_path = self.tmp / "config.json"
        self.config_path.write_text(json.dumps(CONFIG))
        self.repo = self.tmp / "project"
        self.repo.mkdir()
        git("init", "-q", cwd=self.repo)
        git("config", "user.email", "t@example.com", cwd=self.repo)
        git("config", "user.name", "Test", cwd=self.repo)
        (self.repo / "app.py").write_text("print('one')\n")
        git("add", "app.py", cwd=self.repo)
        git("commit", "-qm", "init", cwd=self.repo)
        (self.repo / "app.py").write_text("print('two')\n")  # the work under review
        self.store = TicketStore(self.tmp / "t.sqlite", self.config_path)
        self.ticket = self.store.add(
            "Add two", str(self.repo), model_profile="fast", test_cmd="true", status="review"
        )
        self.store.attach_proof(self.ticket["id"], True, "ok")

    def tearDown(self) -> None:
        self.store.close()

    # ---------------------------------------------------------------- parser

    def test_verdict_is_the_last_line(self) -> None:
        verdict, findings = watcher.parse_verdict("Missing test in app.py:3\n\nVERDICT: ACCEPT\n")
        self.assertEqual(verdict, "ACCEPT")
        self.assertIn("Missing test", findings)

    def test_reject_keeps_the_findings(self) -> None:
        verdict, findings = watcher.parse_verdict("app.py:1 is wrong\nVERDICT: REJECT")
        self.assertEqual((verdict, findings), ("REJECT", "app.py:1 is wrong"))

    def test_prose_without_a_verdict_is_not_a_verdict(self) -> None:
        self.assertEqual(watcher.parse_verdict("looks fine to me, approve"), ("", ""))
        self.assertEqual(watcher.parse_verdict("VERDICT: ACCEPT\nbut actually no"), ("", ""))

    # ---------------------------------------------------------------- start

    def test_starts_a_reviewer_with_the_same_selection_and_a_real_diff(self) -> None:
        fake = FakeT3()
        message = watcher.verification_step(self.store, self.store.get(self.ticket["id"]), CONFIG, "/bin/true", runner=fake)
        self.assertIn("review started", message)
        stored = self.store.get(self.ticket["id"])
        self.assertEqual(stored["status"], "review")  # a review never changes status by itself
        self.assertEqual(stored["verify_thread_id"], "rev-1")
        self.assertEqual(stored["verify_run_id"], "run-1")
        self.assertEqual(stored["verify_attempt"], 1)
        self.assertEqual((stored["verify_provider"], stored["verify_model"]),
                         ("pi", "opencode-go/deepseek-v4.1-flash"))
        prompt = fake.inputs("start")[0]
        self.assertIn("git diff HEAD", prompt)
        self.assertIn("print('two')", prompt)          # the actual work, not a summary
        self.assertIn("VERDICT: ACCEPT", prompt)
        self.assertIn("[PASS]", prompt)                # the recorded test evidence
        kinds = [event["kind"] for event in self.store.events(self.ticket["id"])]
        self.assertIn("verify-started", kinds)

    def test_a_reviewer_that_cannot_start_blocks_the_ticket(self) -> None:
        fake = FakeT3(start_rc=3)
        with self.assertRaises(TicketError):
            watcher.verification_step(self.store, self.store.get(self.ticket["id"]), CONFIG, "/bin/true", runner=fake)
        self.assertEqual(self.store.get(self.ticket["id"])["status"], "blocked")

    # ---------------------------------------------------------------- verdicts

    def _attach(self, baseline: str | None = None) -> None:
        self.store.attach_verifier(
            self.ticket["id"], "rev-1", "run-1", "pi", "opencode-go/deepseek-v4.1-flash",
            baseline if baseline is not None else watcher.project_fingerprint(str(self.repo)),
        )

    def test_accept_lets_release_proceed(self) -> None:
        self._attach()
        fake = FakeT3(reply="diff reviewed\nVERDICT: ACCEPT")
        handled = watcher.verification_step(self.store, self.store.get(self.ticket["id"]), CONFIG, "/bin/true", runner=fake)
        self.assertIsNone(handled)  # None means: release may run
        stored = self.store.get(self.ticket["id"])
        self.assertEqual(stored["verify_verdict"], "ACCEPT")
        self.assertEqual(stored["status"], "review")

    def test_reject_sends_findings_to_the_dev_thread_and_returns_to_work(self) -> None:
        self.store.attach_thread(self.ticket["id"], "dev-1")
        self._attach()
        fake = FakeT3(reply="app.py:1 wrong\nVERDICT: REJECT")
        message = watcher.verification_step(self.store, self.store.get(self.ticket["id"]), CONFIG, "/bin/true", runner=fake)
        self.assertIn("in-progress", message)
        stored = self.store.get(self.ticket["id"])
        self.assertEqual(stored["status"], "in-progress")
        self.assertEqual(stored["verify_attempt"], 1)   # the attempt is spent
        self.assertEqual(stored["verify_thread_id"], "")  # a fresh reviewer next time
        self.assertIn("app.py:1 wrong", fake.inputs("send")[0])
        self.assertIn("review rejected", stored["note"])
        kinds = [event["kind"] for event in self.store.events(self.ticket["id"])]
        self.assertIn("verify-reject", kinds)
        self.assertIn("continued", kinds)

    def test_reject_without_a_dev_thread_blocks(self) -> None:
        self._attach()
        fake = FakeT3(reply="wrong\nVERDICT: REJECT")
        watcher.verification_step(self.store, self.store.get(self.ticket["id"]), CONFIG, "/bin/true", runner=fake)
        self.assertEqual(self.store.get(self.ticket["id"])["status"], "blocked")

    def test_second_rejection_blocks(self) -> None:
        self.store.attach_thread(self.ticket["id"], "dev-1")
        self._attach()
        self.store.attach_verifier(self.ticket["id"], "rev-1", "run-1", "pi", "m", "baseline")
        self.assertEqual(self.store.get(self.ticket["id"])["verify_attempt"], 2)
        fake = FakeT3(reply="still wrong\nVERDICT: REJECT")
        message = watcher.verification_step(self.store, self.store.get(self.ticket["id"]), CONFIG, "/bin/true", runner=fake)
        self.assertIn("blocked", message)
        self.assertEqual(self.store.get(self.ticket["id"])["status"], "blocked")

    def test_missing_verdict_line_blocks_instead_of_accepting(self) -> None:
        self._attach()
        fake = FakeT3(reply="I think it is probably fine")
        watcher.verification_step(self.store, self.store.get(self.ticket["id"]), CONFIG, "/bin/true", runner=fake)
        stored = self.store.get(self.ticket["id"])
        self.assertEqual(stored["status"], "blocked")
        self.assertEqual(stored["verify_verdict"], "FAILED")

    def test_a_failed_reviewer_thread_blocks(self) -> None:
        self._attach()
        fake = FakeT3(status="failed")
        watcher.verification_step(self.store, self.store.get(self.ticket["id"]), CONFIG, "/bin/true", runner=fake)
        self.assertEqual(self.store.get(self.ticket["id"])["status"], "blocked")

    def test_a_running_reviewer_keeps_the_ticket_in_review(self) -> None:
        self._attach()
        fake = FakeT3(status="running")
        message = watcher.verification_step(self.store, self.store.get(self.ticket["id"]), CONFIG, "/bin/true", runner=fake)
        self.assertIn("waiting for the reviewer", message)
        self.assertEqual(self.store.get(self.ticket["id"])["status"], "review")

    def test_a_silent_reviewer_times_out(self) -> None:
        self._attach()
        self.store.con.execute(
            "update tickets set verify_started_at=? where id=?",
            ((datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat(), self.ticket["id"]),
        )
        self.store.con.commit()
        fake = FakeT3(status="running")
        watcher.verification_step(self.store, self.store.get(self.ticket["id"]), CONFIG, "/bin/true", runner=fake)
        self.assertEqual(self.store.get(self.ticket["id"])["status"], "blocked")
        self.assertEqual(fake.calls, [])  # no point asking a thread that is over budget

    def test_a_project_that_changed_during_review_blocks(self) -> None:
        self._attach(baseline="not-the-current-tree")
        fake = FakeT3(reply="VERDICT: ACCEPT")
        watcher.verification_step(self.store, self.store.get(self.ticket["id"]), CONFIG, "/bin/true", runner=fake)
        stored = self.store.get(self.ticket["id"])
        self.assertEqual(stored["status"], "blocked")
        self.assertIn("changed during", stored["note"])

    def test_disabled_verification_goes_straight_to_release(self) -> None:
        config = json.loads(json.dumps(CONFIG))
        config["verification"]["enabled"] = False
        fake = FakeT3()
        self.assertIsNone(
            watcher.verification_step(self.store, self.store.get(self.ticket["id"]), config, "/bin/true", runner=fake)
        )
        self.assertEqual(fake.calls, [])

    # ---------------------------------------------------------------- report

    def test_report_records_the_reviewer_and_the_verdict(self) -> None:
        self._attach()
        self.store.record_verdict(self.ticket["id"], "REJECT", "app.py:1 is wrong")
        report = self.store.report(self.ticket["id"])
        self.assertIn("## Independent review", report)
        self.assertIn("`REJECT`", report)
        self.assertIn("app.py:1 is wrong", report)
        self.assertIn("Attempt", report)

    def test_public_payload_hides_the_reviewer_prose(self) -> None:
        self._attach()
        self.store.record_verdict(self.ticket["id"], "ACCEPT", "an internal note")
        row = self.store.list_public()[0]
        self.assertNotIn("verify_findings", row)
        self.assertEqual(row["verify_verdict"], "ACCEPT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
