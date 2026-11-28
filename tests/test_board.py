#!/usr/bin/env python3
"""API tests for the kanban board."""

from __future__ import annotations

import http.client
import importlib.util
import datetime
import json
import re
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tickets"))

from store import TicketStore  # noqa: E402

SPEC = importlib.util.spec_from_file_location("board_server", ROOT / "board/server.py")
board = importlib.util.module_from_spec(SPEC)
sys.modules["board_server"] = board
SPEC.loader.exec_module(board)

CONFIG = {
    "models": {"fast": {"provider": "pi", "model": "opencode-go/deepseek-v4.1-flash"}},
    "defaults": {"profile": "fast"},
    "projects": {"t3-workflow": {"test_cmd": "make check", "profile": "fast"}},
}


class BoardApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        board.Handler.token = "secret"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), board.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    def setUp(self) -> None:
        os.environ["T3_REPORTS_DIR"] = str(Path(tempfile.mkdtemp(prefix="reports-")) / "reports")
        """A fresh database per test so test order cannot interfere."""
        self.tmp = Path(tempfile.mkdtemp(prefix="board-"))
        self.config = self.tmp / "config.json"
        self.fake_cli = self.tmp / "fake-t3"
        self.args_file = self.tmp / "t3-args.txt"
        self.fake_cli.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$@\" > \"$T3_ARGS_FILE\"\n"
            "case \"$*\" in\n"
            "  *'thread show'*) printf '%s\\n' '{\"thread\":{\"title\":\"t\",\"status\":\"running\",\"model\":\"pi/m\",\"runCount\":1,\"link\":\"\"},\"messages\":[{\"role\":\"assistant\",\"runId\":\"r1\",\"text\":\"working on it\"}]}'; exit 0 ;;\n"
            "  *fail*) echo 'first line of the failure'; echo 'second line with /a/path'; exit 1 ;;\n"
            "esac\n"
        )
        self.fake_cli.chmod(0o755)
        config = json.loads(json.dumps(CONFIG))
        config["t3"] = {"cli": str(self.fake_cli)}
        self.config.write_text(json.dumps(config))
        self.db = self.tmp / "t.sqlite"
        store = TicketStore(self.db, self.config)
        store.add("ticket one", "t3-workflow", model_profile="fast", test_cmd="true", status="backlog")
        store.add("ticket two", "t3-workflow", model_profile="fast", status="ready")
        store.close()
        board.Handler.store_path = self.db
        board.Handler.config_path = self.config

        # fake T3 database: two projects, one thread
        self.t3_db = self.tmp / "t3.sqlite"
        con = sqlite3.connect(self.t3_db)
        con.executescript(
            "create table projection_projects (project_id text, title text, workspace_root text, deleted_at text);"
            "create table orchestration_v2_projection_threads (thread_id text, project_id text, title text, created_at text);"
            "create table orchestration_v2_projection_runs (run_id text, thread_id text, ordinal integer, status text);"
        )
        con.execute("insert into projection_projects values ('p1','t3-workflow','/tmp/p',null)")
        con.execute("insert into orchestration_v2_projection_threads values ('th-1','p1','Thread lama','2026-11-01')")
        con.execute("insert into orchestration_v2_projection_runs values ('r1','th-1',1,'completed')")
        con.commit()
        con.close()
        board.Handler.t3_db_path = self.t3_db

        # fake model catalogue: one active provider, one disabled
        self.catalog_dir = self.tmp / "caches"
        self.catalog_dir.mkdir()
        (self.catalog_dir / "pi.json").write_text(json.dumps({
            "instanceId": "pi", "displayName": "Pi", "driver": "pi",
            "enabled": True, "installed": True, "status": "ready",
            "checkedAt": "2026-11-26T16:34:25.065Z",
            "models": [{"slug": "opencode-go/deepseek-v4.1-flash", "name": "DeepSeek"},
                       {"slug": "opencode-go/qwen3.8-max", "name": "Qwen"}],
        }))
        (self.catalog_dir / "claudeAgent.json").write_text(json.dumps({
            "instanceId": "claudeAgent", "displayName": "Claude", "driver": "claudeAgent",
            "enabled": False, "installed": False, "status": "disabled",
            "models": [{"slug": "claude-opus-5-5", "name": "Opus"}],
        }))
        board.Handler.catalog_dir = self.catalog_dir

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, method: str, path: str, body: dict | None = None, token: str | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Board-Token"] = token
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse()
        raw = response.read().decode()
        conn.close()
        return response, raw

    def test_guide_page_is_served(self) -> None:
        response, raw = self.request("GET", "/guide")
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.headers.get("Content-Type", ""))
        self.assertIn("Ticket lifecycle", raw)
        self.assertIn("Test command", raw)

    def test_home_page_links_to_the_guide(self) -> None:
        response, raw = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn('href="/guide"', raw)

    def test_guide_embeds_screenshots_that_exist(self) -> None:
        response, raw = self.request("GET", "/guide")
        self.assertEqual(response.status, 200)
        sources = re.findall(r'<img src="(/static/shots/[^"]+)"', raw)
        self.assertGreaterEqual(len(sources), 3)
        for src in sources:
            self.assertTrue((board.STATIC / "shots" / Path(src).name).is_file(), src)
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("GET", src)
            image = conn.getresponse()
            body = image.read()
            conn.close()
            self.assertEqual(image.status, 200, src)
            self.assertEqual(image.headers.get("Content-Type"), "image/png", src)
            self.assertGreater(len(body), 1000, src)

    def test_dialogs_explain_their_fields(self) -> None:
        _, raw = self.request("GET", "/")
        # The adopt dialog repeats the test-command explanation and links to the guide.
        self.assertEqual(raw.count('href="/guide#test-command"'), 2)
        for phrase in (
            "Point at the repository root",
            "The 40 most recent T3 threads",
            "The label shown in ticket dialogs",
            "The T3 harness that runs the thread",
        ):
            self.assertIn(phrase, raw)

    def test_guide_button_is_visually_distinct(self) -> None:
        _, raw = self.request("GET", "/")
        self.assertIn('class="btn guide"', raw)
        self.assertIn('aria-label="Open the guide', raw)

    def test_tickets_expose_the_verdict_but_not_the_review_prose(self) -> None:
        store = TicketStore(self.db, self.config)
        store.attach_verifier("T-0001", "rev-1", "run-1", "pi", "m", "baseline-hash")
        store.record_verdict("T-0001", "ACCEPT", "internal reviewer note")
        store.close()
        response, raw = self.request("GET", "/api/tickets")
        self.assertEqual(response.status, 200)
        payload = json.loads(raw)
        ticket = next(item for item in payload["tickets"] if item["id"] == "T-0001")
        self.assertEqual(ticket["verify_verdict"], "ACCEPT")
        for hidden in ("verify_findings", "verify_baseline", "verify_run_id"):
            self.assertNotIn(hidden, ticket)
        self.assertEqual(payload["verification"]["maxAttempts"], 2)

    def test_project_defaults_fill_a_new_ticket(self) -> None:
        response, raw = self.request("GET", "/api/tickets")
        payload = json.loads(raw)
        self.assertEqual(payload["projectDefaults"]["t3-workflow"]["test_cmd"], "make check")
        response, raw = self.request(
            "POST", "/api/tickets",
            {"title": "one line", "project": "t3-workflow", "profile": "fast"},
            token="secret",
        )
        self.assertEqual(response.status, 200, raw)
        ticket = json.loads(raw)["ticket"]
        self.assertEqual(ticket["test_cmd"], "make check")

    def test_edit_endpoint_updates_a_ticket(self) -> None:
        response, raw = self.request(
            "POST", "/api/tickets/T-0001/edit",
            {"title": "edited", "priority": 1}, token="secret",
        )
        self.assertEqual(response.status, 200, raw)
        self.assertEqual(json.loads(raw)["ticket"]["title"], "edited")

    def test_delete_requires_confirm_and_keeps_the_report(self) -> None:
        response, raw = self.request("POST", "/api/tickets/T-0001/delete", {}, token="secret")
        self.assertEqual(response.status, 400)
        response, raw = self.request(
            "POST", "/api/tickets/T-0001/delete", {"confirm": True}, token="secret"
        )
        self.assertEqual(response.status, 200, raw)
        self.assertEqual(json.loads(raw)["deleted"]["id"], "T-0001")
        report, body = self.request("GET", "/api/report/T-0001")
        self.assertEqual(report.status, 200)
        self.assertIn("T-0001", body)

    def test_the_board_offers_edit_and_delete(self) -> None:
        response, raw = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        for needle in ("/edit", "/delete", "Delete", "confirm: true", "edit", "Save changes"):
            self.assertIn(needle, raw)

    def test_a_ticket_whose_project_left_t3_is_marked(self) -> None:
        store = TicketStore(self.db, self.config)
        store.add("orphan", "no-such-project", model_profile="fast", status="backlog")
        store.close()
        response, raw = self.request("GET", "/api/tickets")
        payload = json.loads(raw)
        by_project = {ticket["project"]: ticket for ticket in payload["tickets"]}
        self.assertTrue(by_project["no-such-project"]["project_missing"])
        self.assertFalse(by_project["t3-workflow"]["project_missing"])

    def test_folder_picker_requires_the_token_and_refuses_system_paths(self) -> None:
        response, _ = self.request("GET", "/api/fs?path=")
        self.assertEqual(response.status, 401)
        folder = Path(tempfile.mkdtemp(prefix="fs-"))
        (folder / "child").mkdir()
        response, raw = self.request("GET", f"/api/fs?path={folder}", token="secret")
        self.assertEqual(response.status, 200, raw)
        payload = json.loads(raw)
        self.assertEqual(payload["path"], str(folder.resolve()))
        self.assertIn("child", [entry["name"] for entry in payload["directories"]])
        # outside home is allowed when the filesystem allows it (the process runs as the owner)
        response, _ = self.request("GET", "/api/fs?path=/tmp", token="secret")
        self.assertEqual(response.status, 200)
        # system and hidden trees are refused before any listing
        for blocked in ("/proc", "/sys", f"{folder}/.hidden"):
            response, _ = self.request("GET", f"/api/fs?path={blocked}", token="secret")
            self.assertEqual(response.status, 403, blocked)

    def test_the_board_has_readers_for_reports_threads_and_folders(self) -> None:
        _, raw = self.request("GET", "/")
        for element in ("dlg-report", "dlg-thread", "dlg-fs", "data-browse", "test-warning", "project-rows"):
            self.assertIn(element, raw)
        # Copy is compared with normalised whitespace, so wrapping the source cannot break a test.
        text = " ".join(raw.split())
        for phrase in ("How projects and tickets relate", "ALL of its threads"):
            self.assertIn(phrase, text)

    def test_removing_a_project_needs_the_exact_title_and_runs_force(self) -> None:
        os.environ["T3_ARGS_FILE"] = str(self.args_file)
        response, _ = self.request("POST", "/api/projects/p1/remove", {}, token="secret")
        self.assertEqual(response.status, 400)
        response, _ = self.request(
            "POST", "/api/projects/p1/remove", {"confirm": "wrong title"}, token="secret"
        )
        self.assertEqual(response.status, 400)
        response, raw = self.request(
            "POST", "/api/projects/p1/remove", {"confirm": "t3-workflow"}, token="secret"
        )
        self.assertEqual(response.status, 200, raw)
        args = self.args_file.read_text().split()
        self.assertEqual(args[:3], ["project", "remove", "p1"])
        self.assertIn("--force", args)
        response, _ = self.request(
            "POST", "/api/projects/missing/remove", {"confirm": "x"}, token="secret"
        )
        self.assertEqual(response.status, 404)

    def test_project_removal_needs_the_token(self) -> None:
        response, _ = self.request("POST", "/api/projects/p1/remove", {"confirm": "t3-workflow"})
        self.assertEqual(response.status, 401)

    def test_report_html_is_escaped_and_carries_the_review(self) -> None:
        store = TicketStore(self.db, self.config)
        store.update("T-0001", title="<script>alert(1)</script>")
        store.attach_verifier("T-0001", "rev-1", "run-1", "pi", "m", "baseline")
        store.record_verdict("T-0001", "ACCEPT", "looks right")
        store.close()
        response, raw = self.request("GET", "/api/report/T-0001?format=html")
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.headers.get("Content-Type", ""))
        self.assertIn("Independent review", raw)
        self.assertIn("Started", raw)   # the two renderers share one fact list
        self.assertIn("&lt;script&gt;", raw)
        self.assertNotIn("<script>alert(1)</script>", raw)

    def test_the_payload_carries_a_review_label(self) -> None:
        store = TicketStore(self.db, self.config)
        store.attach_verifier("T-0001", "rev-1", "run-1", "pi", "m", "base")
        store.record_verdict("T-0001", "ACCEPT", "ok")
        store.close()
        _, raw = self.request("GET", "/api/tickets")
        ticket = next(item for item in json.loads(raw)["tickets"] if item["id"] == "T-0001")
        self.assertEqual(ticket["review_label"], "accept · first attempt")

    def test_a_deleted_tickets_html_report_is_served_from_disk(self) -> None:
        store = TicketStore(self.db, self.config)
        store.delete("T-0001")           # writes the markdown and the page first
        store.close()
        response, raw = self.request("GET", "/api/report/T-0001?format=html")
        self.assertEqual(response.status, 200)
        self.assertIn("T-0001", raw)

    def test_the_board_explains_its_own_labels(self) -> None:
        _, raw = self.request("GET", "/")
        text = " ".join(raw.split())
        for phrase in ("What the board means", "Priority", "urgent", "later",
                       "project missing", "review", "Force move"):
            self.assertIn(phrase, text)
        self.assertIn('aria-label="What the columns, priorities and badges mean"', raw)

    def test_the_layout_contract_that_keeps_the_actions_reachable(self) -> None:
        _, raw = self.request("GET", "/")
        style = raw.split("</style>")[0]
        for rule in ("position: sticky", "max-height: 88dvh", "max-height: 26dvh"):
            self.assertIn(rule, style)
        # inside the project dialog, the scrollable list comes before the sticky action row
        dialog = raw[raw.index('id="dlg-project"'):]
        dialog = dialog[:dialog.index("</dialog>")]
        self.assertLess(dialog.index('id="project-rows"'), dialog.index('class="row actions"'))

    def test_registering_the_same_folder_twice_is_refused_clearly(self) -> None:
        con = sqlite3.connect(self.t3_db)
        con.execute("insert into projection_projects values ('p9','brain','%s',null)" % self.tmp)
        con.commit(); con.close()
        response, raw = self.request(
            "POST", "/api/projects",
            {"title": "brain-obsidian", "path": str(self.tmp)}, token="secret",
        )
        self.assertEqual(response.status, 409, raw)
        self.assertIn("already the project 'brain'", json.loads(raw)["error"])

    def test_a_cli_failure_is_reported_as_one_line(self) -> None:
        os.environ["T3_ARGS_FILE"] = str(self.args_file)
        folder = Path(tempfile.mkdtemp(prefix="new-project-"))
        response, raw = self.request(
            "POST", "/api/projects",
            {"title": "fail", "path": str(folder)}, token="secret",
        )
        self.assertEqual(response.status, 409, raw)
        self.assertEqual(json.loads(raw)["error"], "first line of the failure")

    def test_waits_show_a_progress_bar_and_a_spinner(self) -> None:
        _, raw = self.request("GET", "/")
        for needle in ('id="progress"', "spinner", "is-busy", "withBusy"):
            self.assertIn(needle, raw)

    def test_projects_report_the_test_command_they_declare(self) -> None:
        workspace = Path(tempfile.mkdtemp(prefix="declared-"))
        (workspace / "Makefile").write_text("check:\n\ttrue\n")
        con = sqlite3.connect(self.t3_db)
        con.execute("insert into projection_projects values ('p7','declared','%s',null)" % workspace)
        con.commit(); con.close()
        _, raw = self.request("GET", "/api/projects")
        project = next(item for item in json.loads(raw)["projects"] if item["title"] == "declared")
        self.assertEqual(project["test_command"], "make check")
        self.assertIn("Makefile", project["test_command_source"])

    def test_a_new_ticket_takes_the_projects_test_command(self) -> None:
        workspace = Path(tempfile.mkdtemp(prefix="declared-"))
        (workspace / "Makefile").write_text("check:\n\ttrue\n")
        con = sqlite3.connect(self.t3_db)
        con.execute("insert into projection_projects values ('p8','declared2','%s',null)" % workspace)
        con.commit(); con.close()
        response, raw = self.request(
            "POST", "/api/tickets",
            {"title": "one line", "project": "declared2", "profile": "fast"},
            token="secret",
        )
        self.assertEqual(response.status, 200, raw)
        self.assertEqual(json.loads(raw)["ticket"]["test_cmd"], "make check")

    def test_a_project_that_needs_setup_is_not_auto_filled(self) -> None:
        workspace = Path(tempfile.mkdtemp(prefix="needs-setup-"))
        (workspace / "Makefile").write_text("check:\n\ttrue\n")
        tests = workspace / "tests"
        tests.mkdir()
        (tests / "conftest.py").write_text("DATABASE_URL = 'postgresql://x'\n")
        con = sqlite3.connect(self.t3_db)
        con.execute("insert into projection_projects values ('p10','needs-setup','%s',null)" % workspace)
        con.commit(); con.close()
        _, raw = self.request("GET", "/api/projects")
        project = next(item for item in json.loads(raw)["projects"] if item["title"] == "needs-setup")
        self.assertTrue(project["test_command_needs_setup"])
        response, raw = self.request(
            "POST", "/api/tickets",
            {"title": "one line", "project": "needs-setup", "profile": "fast"},
            token="secret",
        )
        self.assertEqual(response.status, 200, raw)
        self.assertEqual(json.loads(raw)["ticket"]["test_cmd"], "")

    def test_the_payload_carries_watcher_liveness(self) -> None:
        heartbeat = self.db.with_name("watcher.json")
        heartbeat.write_text(json.dumps({
            "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "interval": 120, "status": "ok", "message": "no ticket is ready",
        }))
        _, raw = self.request("GET", "/api/tickets")
        watcher = json.loads(raw)["watcher"]
        self.assertFalse(watcher["stale"])
        self.assertEqual(watcher["interval"], 120)
        self.assertLess(watcher["ageSeconds"], 60)

    def test_live_status_returns_thread_messages(self) -> None:
        con = sqlite3.connect(self.db)
        con.execute("update tickets set thread_id='th-1' where id='T-0002'")
        con.commit(); con.close()
        response, raw = self.request("GET", "/api/tickets/T-0002/live", token="secret")
        self.assertEqual(response.status, 200, raw)
        payload = json.loads(raw)
        self.assertEqual(payload["thread"]["status"], "running")
        self.assertIn("working on it", payload["messages"][0]["text"])

    def test_live_status_needs_a_thread(self) -> None:
        response, _ = self.request("GET", "/api/tickets/T-0001/live", token="secret")
        self.assertEqual(response.status, 400)

    def test_an_uploaded_attachment_is_stored_and_returned(self) -> None:
        os.environ["T3_ATTACHMENTS_DIR"] = str(self.tmp / "attachments")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/attachments", body=b"hello file",
                     headers={"X-Board-Token": "secret", "X-Filename": "spec%20note.txt"})
        response = conn.getresponse()
        raw = response.read().decode()
        conn.close()
        self.assertEqual(response.status, 200, raw)
        payload = json.loads(raw)
        self.assertEqual(payload["name"], "spec note.txt")
        self.assertTrue(Path(payload["path"]).is_file())
        self.assertEqual(Path(payload["path"]).read_bytes(), b"hello file")

    def test_the_board_has_confirm_dialogs_and_a_drop_zone(self) -> None:
        _, raw = self.request("GET", "/")
        for needle in ('id="dlg-confirm"', 'id="attachment-drop"', 'id="watcher"',
                       'id="dlg-live"', 'class="req"'):
            self.assertIn(needle, raw)

    def test_list_tickets(self) -> None:
        response, raw = self.request("GET", "/api/tickets")
        self.assertEqual(response.status, 200)
        data = json.loads(raw)
        self.assertEqual(len(data["tickets"]), 2)
        self.assertIn("transitions", data)
        self.assertEqual(data["profiles"], ["fast"])

    def test_move_allowed_without_token_is_rejected(self) -> None:
        response, _ = self.request("POST", "/api/tickets/T-0001/move", {"status": "ready"})
        self.assertEqual(response.status, 401)

    def test_move_allowed_with_token(self) -> None:
        response, raw = self.request("POST", "/api/tickets/T-0001/move", {"status": "ready"}, token="secret")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(raw)["ticket"]["status"], "ready")

    def test_move_invalid_transition_is_rejected(self) -> None:
        response, raw = self.request("POST", "/api/tickets/T-0002/move", {"status": "done"}, token="secret")
        self.assertEqual(response.status, 409)
        self.assertIn("not allowed", json.loads(raw)["error"])

    def test_force_move_is_allowed_and_recorded(self) -> None:
        response, raw = self.request(
            "POST", "/api/tickets/T-0002/move", {"status": "done", "force": True}, token="secret"
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(raw)["ticket"]["status"], "done")
        store = TicketStore(self.db, self.config)
        kinds = [event["kind"] for event in store.events("T-0002")]
        store.close()
        self.assertIn("forced", kinds)

    def test_report_endpoint_returns_markdown(self) -> None:
        response, raw = self.request("GET", "/api/report/T-0001")
        self.assertEqual(response.status, 200)
        self.assertIn("text/markdown", response.getheader("Content-Type"))
        self.assertIn("# T-0001", raw)
        self.assertIn("## History", raw)

    def test_report_unknown_ticket_is_404(self) -> None:
        response, _ = self.request("GET", "/api/report/T-9999")
        self.assertEqual(response.status, 404)

    def test_tickets_include_pending_approvals(self) -> None:
        store = TicketStore(self.db, self.config)
        approval = store.request_approval("T-0001", "mulai?")
        store.close()
        response, raw = self.request("GET", "/api/tickets")
        data = json.loads(raw)
        self.assertEqual(data["approvalCount"], 1)
        self.assertEqual(data["approvals"][0]["approval_id"], approval["approval_id"])

    def test_decide_approval_requires_token(self) -> None:
        store = TicketStore(self.db, self.config)
        approval = store.request_approval("T-0001", "mulai?")
        store.close()
        response, _ = self.request("POST", f"/api/approvals/{approval['approval_id']}/decide", {"approved": True})
        self.assertEqual(response.status, 401)

    def test_decide_approval_then_second_attempt_conflicts(self) -> None:
        store = TicketStore(self.db, self.config)
        approval = store.request_approval("T-0001", "mulai?")
        store.close()
        path = f"/api/approvals/{approval['approval_id']}/decide"
        first, _ = self.request("POST", path, {"approved": True}, token="secret")
        self.assertEqual(first.status, 200)
        second, raw = self.request("POST", path, {"approved": False}, token="secret")
        self.assertEqual(second.status, 409)
        self.assertIn("already", json.loads(raw)["error"])

    def test_healthz_reports_ticket_count(self) -> None:
        response, raw = self.request("GET", "/healthz")
        self.assertEqual(response.status, 200)
        data = json.loads(raw)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["tickets"], 2)

    def test_lists_t3_projects_and_threads(self) -> None:
        response, raw = self.request("GET", "/api/projects")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(raw)["projects"][0]["title"], "t3-workflow")
        response, raw = self.request("GET", "/api/threads")
        data = json.loads(raw)
        self.assertEqual(data["threads"][0]["thread_id"], "th-1")
        self.assertEqual(data["threads"][0]["status"], "completed")

    def test_create_ticket_from_board(self) -> None:
        body = {"title": "from the board", "project": "t3-workflow", "profile": "fast", "priority": 2}
        response, raw = self.request("POST", "/api/tickets", body, token="secret")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(raw)["ticket"]["title"], "from the board")
        response, _ = self.request("POST", "/api/tickets", {"title": "x", "project": "p"})
        self.assertEqual(response.status, 401)

    def test_create_ticket_rejects_unknown_profile(self) -> None:
        body = {"title": "x", "project": "t3-workflow", "profile": "no-such-profile"}
        response, raw = self.request("POST", "/api/tickets", body, token="secret")
        self.assertEqual(response.status, 409)
        self.assertIn("profile", json.loads(raw)["error"])

    def test_adopt_existing_thread(self) -> None:
        body = {"title": "Thread lama", "project": "t3-workflow", "profile": "fast"}
        response, raw = self.request("POST", "/api/threads/th-1/adopt", body, token="secret")
        self.assertEqual(response.status, 200)
        ticket = json.loads(raw)["ticket"]
        self.assertEqual(ticket["thread_id"], "th-1")
        self.assertEqual(ticket["status"], "ready")  # the watcher continues this thread

    def test_create_ticket_accepts_body_and_attachment(self) -> None:
        body = {
            "title": "with attachment", "project": "t3-workflow", "profile": "fast",
            "body": "long context", "attachment_path": "/tmp/spec.pdf",
        }
        response, raw = self.request("POST", "/api/tickets", body, token="secret")
        self.assertEqual(response.status, 200)
        ticket = json.loads(raw)["ticket"]
        self.assertEqual(ticket["body"], "long context")
        self.assertEqual(ticket["attachment_path"], "/tmp/spec.pdf")

    def test_catalog_lists_providers_with_models(self) -> None:
        response, raw = self.request("GET", "/api/catalog")
        self.assertEqual(response.status, 200)
        data = json.loads(raw)
        ids = {p["instanceId"]: p for p in data["providers"]}
        self.assertTrue(ids["pi"]["enabled"])
        self.assertFalse(ids["claudeAgent"]["enabled"])
        self.assertEqual(len(ids["pi"]["models"]), 2)
        self.assertEqual([p["name"] for p in data["presets"]], ["fast"])

    def test_create_ticket_with_explicit_harness_and_model(self) -> None:
        body = {"title": "choose model", "project": "t3-workflow",
                "provider": "pi", "model": "opencode-go/qwen3.8-max"}
        response, raw = self.request("POST", "/api/tickets", body, token="secret")
        self.assertEqual(response.status, 200)
        ticket = json.loads(raw)["ticket"]
        self.assertEqual(ticket["provider"], "pi")
        self.assertEqual(ticket["model"], "opencode-go/qwen3.8-max")

    def test_create_ticket_rejects_disabled_provider_and_unknown_model(self) -> None:
        for provider, model in (("claudeAgent", "claude-opus-5-5"), ("pi", "no-such-model"), ("pi", "")):
            with self.subTest(provider=provider, model=model):
                body = {"title": "x", "project": "t3-workflow", "provider": provider, "model": model}
                response, raw = self.request("POST", "/api/tickets", body, token="secret")
                self.assertEqual(response.status, 409)
                self.assertIn("error", json.loads(raw))

    def test_tickets_expose_profile_details(self) -> None:
        response, raw = self.request("GET", "/api/tickets")
        details = json.loads(raw)["profileDetails"]
        self.assertEqual(details[0]["name"], "fast")
        self.assertEqual(details[0]["provider"], "pi")

    def test_register_project_validates_path(self) -> None:
        response, raw = self.request(
            "POST", "/api/projects", {"title": "new", "path": "/not/here"}, token="secret"
        )
        self.assertEqual(response.status, 400)
        self.assertIn("path", json.loads(raw)["error"])

    def test_page_is_served(self) -> None:
        response, raw = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn("Ticket Board", raw)
        self.assertIn("2.5.7", raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
