#!/usr/bin/env python3
"""Tests for proofs, approvals and the two-stage watcher."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
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
        self.assertIn("[PASS]", stored["proof"])
        self.assertIn("output baris 1", stored["proof"])
        kinds = [event["kind"] for event in self.store.events(ticket["id"])]
        self.assertIn("proof", kinds)

    def test_list_public_hides_proof(self) -> None:
        ticket = self.store.add("x", "/tmp/p", model_profile="fast", status="testing")
        self.store.attach_proof(ticket["id"], True, "secret output")
        public = self.store.list_public()[0]
        self.assertNotIn("proof", public)
        self.assertIn("proof", self.store.get(ticket["id"]))

    def test_report_contains_proof(self) -> None:
        ticket = self.store.add("x", "/tmp/p", model_profile="fast", status="testing")
        self.store.attach_proof(ticket["id"], False, "failed at test 3")
        report = self.store.report(ticket["id"])
        self.assertIn("### Test evidence", report)
        self.assertIn("failed at test 3", report)


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
        self.assertIn("rejected", message)
        self.assertEqual(self.store.get(ticket["id"])["status"], "blocked")


class TestStageTests(Base):
    def test_success_moves_to_review_with_proof(self) -> None:
        ticket = self.store.add("x", "/tmp/p", model_profile="fast", status="testing", test_cmd="true")
        message = watcher.run_test(self.store, ticket, runner=ok_runner)
        self.assertIn("passed", message)
        stored = self.store.get(ticket["id"])
        self.assertEqual(stored["status"], "review")
        self.assertIn("PASS", stored["proof"])

    def test_failure_sends_the_output_back_to_the_dev_thread(self) -> None:
        ticket = self.store.add("x", "/tmp/p", model_profile="fast", status="testing", test_cmd="false")
        self.store.attach_thread(ticket["id"], "dev-1")
        ticket = self.store.get(ticket["id"])  # attach_thread changed the row
        sent: list[str] = []
        outer = self

        class Runner:
            def __call__(self, command, input=None, **kwargs):
                if command[1:3] == ["thread", "show"]:
                    body = {"thread": {"status": "completed", "latestRunId": "run-old"}}
                    return subprocess.CompletedProcess(command, 0, json.dumps(body), "")
                if command[1:3] == ["thread", "send"]:
                    sent.append(input or "")
                    return subprocess.CompletedProcess(command, 0, "{}", "")
                return fail_runner(command, **kwargs)

        message = watcher.run_test(self.store, ticket, CONFIG, runner=Runner())
        self.assertIn("findings sent", message)
        stored = self.store.get(ticket["id"])
        self.assertEqual(stored["status"], "in-progress")
        self.assertIn("FAIL", stored["proof"])
        self.assertIn("failed its test", sent[0])
        self.assertEqual(stored["dev_run_id"], "run-old")   # sync waits for a new run

    def test_a_failing_test_is_bounded(self) -> None:
        config = json.loads(json.dumps(CONFIG))
        config["retry"] = {"maxTestAttempts": 1}
        ticket = self.store.add("x", "/tmp/p", model_profile="fast", status="testing", test_cmd="false")
        self.store.attach_thread(ticket["id"], "dev-1")
        watcher.run_test(self.store, ticket, config, runner=fail_runner)
        stored = self.store.get(ticket["id"])
        self.assertEqual(stored["status"], "blocked")
        self.assertIn("1/1", stored["note"])

    def test_missing_test_cmd_blocks_instead_of_looping(self) -> None:
        ticket = self.store.add("x", "/tmp/p", model_profile="fast", status="testing")
        watcher.run_test(self.store, ticket, CONFIG, runner=ok_runner)
        stored = self.store.get(ticket["id"])
        self.assertEqual(stored["status"], "blocked")
        self.assertIn("retry cannot fix", stored["note"])

    def test_step_prefers_testing_over_ready(self) -> None:
        testing = self.store.add("uji", "/tmp/p", model_profile="fast", status="testing", test_cmd="true")
        self.store.add("baru", "/tmp/p", model_profile="fast", status="ready")
        message = watcher.step(self.store, CONFIG, "/bin/true", dry_run=False, runner=ok_runner)
        self.assertIn(testing["id"], message)
        self.assertEqual(self.store.get(testing["id"])["status"], "review")


class ContinuationTests(Base):
    """A ticket that carries a thread continues that thread instead of starting one."""

    def test_ready_ticket_with_thread_sends_to_same_thread(self) -> None:
        ticket = self.store.add("lanjut", "/tmp/p", model_profile="fast", status="ready")
        self.store.attach_thread(ticket["id"], "mcp:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        calls: list[list[str]] = []

        def fake(command, **kwargs):  # noqa: ARG001
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, '{"ok":true}', "")

        message = watcher.run_once(self.store, CONFIG, "t3", dry_run=False, runner=fake)
        self.assertIn("continuing thread", message)
        self.assertEqual(calls[0][:3], ["t3", "thread", "send"])
        self.assertEqual(calls[0][3], "mcp:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        stored = self.store.get(ticket["id"])
        self.assertEqual(stored["status"], "in-progress")
        kinds = [event["kind"] for event in self.store.events(ticket["id"])]
        self.assertIn("continued", kinds)

    def test_dry_run_preview_differs_for_continuation(self) -> None:
        ticket = self.store.add("lanjut", "/tmp/p", model_profile="fast", status="ready")
        self.store.attach_thread(ticket["id"], "mcp:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        message = watcher.run_once(self.store, CONFIG, "t3", dry_run=True)
        self.assertIn("continue thread", message)
        self.assertEqual(self.store.get(ticket["id"])["status"], "ready")

    def test_prompt_carries_body_attachment_and_test(self) -> None:
        ticket = self.store.add(
            "title", "/tmp/p", model_profile="fast", status="ready",
            body="important context", test_cmd="make check",
        )
        self.store.con.execute("update tickets set attachment_path=? where id=?", ("/tmp/spec.pdf", ticket["id"]))
        self.store.con.commit()
        prompt = watcher.build_prompt(self.store.get(ticket["id"]))
        self.assertIn("important context", prompt)
        self.assertIn("/tmp/spec.pdf", prompt)
        self.assertIn("make check", prompt)


class SyncTests(Base):
    def _ticket_with_thread(self, status: str) -> dict:
        ticket = self.store.add("x", "/tmp/p", model_profile="fast", status="in-progress")
        self.store.attach_thread(ticket["id"], "mcp:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        return ticket

    def test_completed_thread_moves_to_testing(self) -> None:
        ticket = self._ticket_with_thread("in-progress")

        def fake(command, **kwargs):  # noqa: ARG001
            return subprocess.CompletedProcess(command, 0, json.dumps({"thread": {"status": "completed"}}), "")

        notes = watcher.sync_threads(self.store, CONFIG, runner=fake)
        self.assertTrue(notes)
        self.assertEqual(self.store.get(ticket["id"])["status"], "testing")

    def test_running_thread_is_left_alone(self) -> None:
        ticket = self._ticket_with_thread("in-progress")

        def fake(command, **kwargs):  # noqa: ARG001
            return subprocess.CompletedProcess(command, 0, json.dumps({"thread": {"status": "running"}}), "")

        self.assertEqual(watcher.sync_threads(self.store, CONFIG, runner=fake), [])
        self.assertEqual(self.store.get(ticket["id"])["status"], "in-progress")

    def test_a_failed_thread_is_retried_then_blocked(self) -> None:
        ticket = self._ticket_with_thread("in-progress")

        def fake(command, **kwargs):  # noqa: ARG001
            return subprocess.CompletedProcess(command, 0, json.dumps({"thread": {"status": "failed"}}), "")

        watcher.sync_threads(self.store, CONFIG, runner=fake)   # attempt 1 of 3
        self.assertEqual(self.store.get(ticket["id"])["status"], "ready")
        self.assertEqual(self.store.get(ticket["id"])["dev_attempts"], 1)
        for _ in range(2):  # attempts 2 and 3
            self.store.move(ticket["id"], "in-progress")
            watcher.sync_threads(self.store, CONFIG, runner=fake)
        stored = self.store.get(ticket["id"])
        self.assertEqual(stored["status"], "blocked")
        self.assertIn("3/3", stored["note"])

    def test_findings_wait_for_the_next_run_before_testing(self) -> None:
        ticket = self._ticket_with_thread("in-progress")
        self.store.set_dev_run(ticket["id"], "run-old")

        def fake(command, **kwargs):  # noqa: ARG001
            body = {"thread": {"status": "completed", "latestRunId": "run-old"}}
            return subprocess.CompletedProcess(command, 0, json.dumps(body), "")

        notes = watcher.sync_threads(self.store, CONFIG, runner=fake)
        self.assertEqual(self.store.get(ticket["id"])["status"], "in-progress")
        self.assertTrue(any("resume" in note for note in notes))

        def resumed(command, **kwargs):  # noqa: ARG001
            body = {"thread": {"status": "completed", "latestRunId": "run-new"}}
            return subprocess.CompletedProcess(command, 0, json.dumps(body), "")

        watcher.sync_threads(self.store, CONFIG, runner=resumed)
        self.assertEqual(self.store.get(ticket["id"])["status"], "testing")

    def test_sync_works_when_no_runner_is_given(self) -> None:
        """The service calls sync_threads() without a runner; None must not reach the CLI."""
        ticket = self._ticket_with_thread("in-progress")
        seen: list[object] = []
        original = watcher.thread_show

        def capture(config, thread_id, messages=5, runner=None):
            seen.append(runner)
            return {"thread": {"status": "running", "latestRunId": "r1"}}

        watcher.thread_show = capture
        try:
            watcher.sync_threads(self.store, CONFIG)
        finally:
            watcher.thread_show = original
        self.assertEqual(self.store.get(ticket["id"])["status"], "in-progress")
        self.assertTrue(seen)
        for runner in seen:
            self.assertTrue(callable(runner), runner)

    def test_a_thread_deleted_in_t3_blocks_instead_of_waiting(self) -> None:
        ticket = self._ticket_with_thread("in-progress")

        def fake(command, **kwargs):  # noqa: ARG001
            return subprocess.CompletedProcess(
                command, 1, "", "Thread mcp:missing is no longer available."
            )

        watcher.sync_threads(self.store, CONFIG, runner=fake)
        stored = self.store.get(ticket["id"])
        self.assertEqual(stored["status"], "blocked")
        self.assertIn("no longer exists in T3", stored["note"])

    def test_ticket_without_thread_is_ignored(self) -> None:
        ticket = self.store.add("x", "/tmp/p", model_profile="fast", status="in-progress")

        def fake(command, **kwargs):  # noqa: ARG001
            raise AssertionError("t3 must not be called for a ticket without a thread")

        self.assertEqual(watcher.sync_threads(self.store, CONFIG, runner=fake), [])
        self.assertEqual(self.store.get(ticket["id"])["status"], "in-progress")


class ApprovalGateTests(Base):
    def test_gate_requests_and_does_not_claim(self) -> None:
        ticket = self.store.add("x", "/tmp/p", model_profile="fast", status="ready", requires_approval=True)
        message = watcher.run_once(self.store, CONFIG, "/bin/true", dry_run=False)
        self.assertIn("needs approval", message)
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


class FailureClassificationTests(unittest.TestCase):
    """A permanent provider failure must block; a transient one may retry."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="failure-"))
        self.config_path = self.tmp / "config.json"
        self.config_path.write_text(json.dumps(CONFIG))
        self.store = TicketStore(self.tmp / "t.sqlite", self.config_path)
        self.t3_db = self.tmp / "t3.sqlite"
        con = sqlite3.connect(self.t3_db)
        con.executescript(
            "create table orchestration_v2_projection_runs"
            " (run_id text, thread_id text, ordinal integer, status text, payload_json text);"
            "create table orchestration_v2_projection_turn_items"
            " (run_id text, type text, status text, payload_json text);"
        )
        con.execute(
            "insert into orchestration_v2_projection_runs values ('r1','th-1',1,'failed', ?)",
            (json.dumps({"rootNodeId": "n1"}),),
        )
        con.execute(
            "insert into orchestration_v2_projection_turn_items values ('r1','error','failed', ?)",
            (json.dumps({"nodeId": "n1", "failure": {"class": "provider_error",
                                                     "message": "Request timed out."},
                         "retry": {"attempt": 3, "maxAttempts": 3}}),),
        )
        con.commit(); con.close()

    def tearDown(self) -> None:
        self.store.close()

    def _failure(self, message: str, failure_class: str = "provider_error") -> dict:
        con = sqlite3.connect(self.t3_db)
        con.execute("delete from orchestration_v2_projection_turn_items")
        con.execute(
            "insert into orchestration_v2_projection_turn_items values ('r1','error','failed', ?)",
            (json.dumps({"nodeId": "n1",
                         "failure": {"class": failure_class, "message": message}}),),
        )
        con.commit(); con.close()
        return watcher.thread_failure("th-1", self.t3_db)

    def test_a_timeout_is_not_permanent(self) -> None:
        failure = self._failure("Request timed out.")
        self.assertEqual(failure["message"], "Request timed out.")
        self.assertFalse(failure["permanent"])

    def test_a_usage_limit_is_permanent(self) -> None:
        self.assertTrue(self._failure("The usage limit has been reached", "usage_limit")["permanent"])
        self.assertTrue(self._failure("GoUsageLimitError: resets in 10d 8h")["permanent"])

    def test_context_overflow_and_a_missing_model_are_permanent(self) -> None:
        self.assertTrue(self._failure("Input token count (454749) exceeds the context length")["permanent"])
        self.assertTrue(self._failure("Model not found: opencode-go/beam-free")["permanent"])

    def test_a_missing_database_is_not_a_classification(self) -> None:
        self.assertEqual(watcher.thread_failure("th-1", self.tmp / "nope.sqlite"), {})

    def test_sync_blocks_a_permanent_failure_without_retrying(self) -> None:
        ticket = self.store.add("x", "/tmp/p", model_profile="fast", status="in-progress")
        self.store.attach_thread(ticket["id"], "th-1")
        self._failure("The usage limit has been reached", "usage_limit")

        def fake(command, **kwargs):  # noqa: ARG001
            body = {"thread": {"status": "failed", "latestRunId": "r1"}}
            return subprocess.CompletedProcess(command, 0, json.dumps(body), "")

        original = watcher.T3_DB
        watcher.T3_DB = self.t3_db
        try:
            watcher.sync_threads(self.store, CONFIG, runner=fake)
        finally:
            watcher.T3_DB = original
        stored = self.store.get(ticket["id"])
        self.assertEqual(stored["status"], "blocked")
        self.assertIn("usage limit", stored["note"])

    def _sync_failed(self, status: str = "failed") -> None:
        def fake(command, **kwargs):  # noqa: ARG001
            body = {"thread": {"status": status, "latestRunId": "r1"}}
            return subprocess.CompletedProcess(command, 0, json.dumps(body), "")

        original = watcher.T3_DB
        watcher.T3_DB = self.t3_db
        try:
            watcher.sync_threads(self.store, CONFIG, runner=fake)
        finally:
            watcher.T3_DB = original

    def test_usage_limit_schedules_a_retry_instead_of_stopping(self) -> None:
        ticket = self.store.add("x", "/tmp/p", model_profile="fast", status="in-progress")
        self.store.attach_thread(ticket["id"], "th-1")
        self._failure("The usage limit has been reached", "usage_limit")
        self._sync_failed()
        stored = self.store.get(ticket["id"])
        self.assertEqual(stored["status"], "blocked")
        self.assertTrue(stored["retry_after"])
        self.assertEqual(stored["quota_attempts"], 1)
        self.assertIn("retry 1/3", stored["note"])

    def test_a_due_quota_retry_returns_to_ready(self) -> None:
        ticket = self.store.add("x", "/tmp/p", model_profile="fast", status="in-progress")
        self.store.attach_thread(ticket["id"], "th-1")
        self._failure("The usage limit has been reached", "usage_limit")
        self._sync_failed()
        due = watcher.release_due_retries(self.store, CONFIG, at="2999-01-01T00:00:00+00:00")
        self.assertEqual(len(due), 1)
        self.assertEqual(self.store.get(ticket["id"])["status"], "ready")
        self.assertEqual(self.store.get(ticket["id"])["retry_after"], "")

    def test_a_quota_retry_in_the_future_keeps_waiting(self) -> None:
        ticket = self.store.add("x", "/tmp/p", model_profile="fast", status="in-progress")
        self.store.attach_thread(ticket["id"], "th-1")
        self._failure("The usage limit has been reached", "usage_limit")
        self._sync_failed()
        self.assertEqual(watcher.release_due_retries(self.store, CONFIG, at="2000-01-01T00:00:00+00:00"), [])
        self.assertEqual(self.store.get(ticket["id"])["status"], "blocked")

    def test_quota_retries_are_bounded(self) -> None:
        config = json.loads(json.dumps(CONFIG))
        config["retry"] = {"maxQuotaAttempts": 1, "quotaBackoffMinutes": 5}
        ticket = self.store.add("x", "/tmp/p", model_profile="fast", status="in-progress")
        self.store.attach_thread(ticket["id"], "th-1")
        self._failure("The usage limit has been reached", "usage_limit")
        original = watcher.T3_DB
        watcher.T3_DB = self.t3_db
        try:
            def fake(command, **kwargs):  # noqa: ARG001
                return subprocess.CompletedProcess(
                    command, 0, json.dumps({"thread": {"status": "failed"}}), ""
                )
            watcher.sync_threads(self.store, config, runner=fake)
            watcher.release_due_retries(self.store, config, at="2999-01-01T00:00:00+00:00")
            self.store.claim(ticket["id"], "watcher")   # the next attempt starts
            watcher.sync_threads(self.store, config, runner=fake)
        finally:
            watcher.T3_DB = original
        stored = self.store.get(ticket["id"])
        self.assertEqual(stored["status"], "blocked")
        self.assertEqual(stored["retry_after"], "")
        self.assertIn("exhausted", stored["note"])

    def test_context_overflow_does_not_schedule_a_retry(self) -> None:
        self._failure("Input token count (454749) exceeds the context length")
        ticket = self.store.add("x", "/tmp/p", model_profile="fast", status="in-progress")
        self.store.attach_thread(ticket["id"], "th-1")
        self._sync_failed()
        stored = self.store.get(ticket["id"])
        self.assertEqual(stored["status"], "blocked")
        self.assertEqual(stored["retry_after"], "")

    def test_sync_retries_a_transient_failure(self) -> None:
        ticket = self.store.add("x", "/tmp/p", model_profile="fast", status="in-progress")
        self.store.attach_thread(ticket["id"], "th-1")
        self._failure("Request timed out.")

        def fake(command, **kwargs):  # noqa: ARG001
            body = {"thread": {"status": "failed", "latestRunId": "r1"}}
            return subprocess.CompletedProcess(command, 0, json.dumps(body), "")

        original = watcher.T3_DB
        watcher.T3_DB = self.t3_db
        try:
            watcher.sync_threads(self.store, CONFIG, runner=fake)
        finally:
            watcher.T3_DB = original
        stored = self.store.get(ticket["id"])
        self.assertEqual(stored["status"], "ready")
        self.assertIn("retry", stored["note"])


class HtmlReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="report-html-"))
        os.environ["T3_REPORTS_DIR"] = str(self.tmp / "reports")
        self.config = self.tmp / "config.json"
        self.config.write_text(json.dumps(CONFIG))
        self.store = TicketStore(self.tmp / "t.sqlite", self.config)

    def tearDown(self) -> None:
        self.store.close()

    def test_every_user_field_is_escaped(self) -> None:
        ticket = self.store.add("<img src=x onerror=alert(1)>", "/tmp/p", model_profile="fast")
        html = self.store.report_html(ticket["id"])
        self.assertIn("&lt;img", html)
        self.assertNotIn("<img src=x", html)

    def test_the_standalone_page_is_a_document(self) -> None:
        ticket = self.store.add("readable", "/tmp/p", model_profile="fast")
        page = self.store.report_html(ticket["id"], document=True)
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIn("<main>", page)
        self.assertIn("readable", page)
