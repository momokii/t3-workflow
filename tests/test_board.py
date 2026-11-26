#!/usr/bin/env python3
"""API tests for the kanban board."""

from __future__ import annotations

import http.client
import importlib.util
import json
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
}


class BoardApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        board.Handler.token = "rahasia"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), board.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    def setUp(self) -> None:
        os.environ["T3_REPORTS_DIR"] = str(Path(tempfile.mkdtemp(prefix="reports-")) / "reports")
        """Database baru per test, supaya urutan test tidak saling mempengaruhi."""
        self.tmp = Path(tempfile.mkdtemp(prefix="board-"))
        self.config = self.tmp / "config.json"
        self.config.write_text(json.dumps(CONFIG))
        self.db = self.tmp / "t.sqlite"
        store = TicketStore(self.db, self.config)
        store.add("tiket satu", "t3-workflow", model_profile="fast", test_cmd="true", status="backlog")
        store.add("tiket dua", "t3-workflow", model_profile="fast", status="ready")
        store.close()
        board.Handler.store_path = self.db
        board.Handler.config_path = self.config

        # database T3 tiruan: dua project, satu thread
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
        response, raw = self.request("POST", "/api/tickets/T-0001/move", {"status": "ready"}, token="rahasia")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(raw)["ticket"]["status"], "ready")

    def test_move_invalid_transition_is_rejected(self) -> None:
        response, raw = self.request("POST", "/api/tickets/T-0002/move", {"status": "done"}, token="rahasia")
        self.assertEqual(response.status, 409)
        self.assertIn("tidak diizinkan", json.loads(raw)["error"])

    def test_force_move_is_allowed_and_recorded(self) -> None:
        response, raw = self.request(
            "POST", "/api/tickets/T-0002/move", {"status": "done", "force": True}, token="rahasia"
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
        self.assertIn("## Riwayat", raw)

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
        first, _ = self.request("POST", path, {"approved": True}, token="rahasia")
        self.assertEqual(first.status, 200)
        second, raw = self.request("POST", path, {"approved": False}, token="rahasia")
        self.assertEqual(second.status, 409)
        self.assertIn("sudah", json.loads(raw)["error"])

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
        body = {"title": "dari papan", "project": "t3-workflow", "profile": "fast", "priority": 2}
        response, raw = self.request("POST", "/api/tickets", body, token="rahasia")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(raw)["ticket"]["title"], "dari papan")
        response, _ = self.request("POST", "/api/tickets", {"title": "x", "project": "p"})
        self.assertEqual(response.status, 401)

    def test_create_ticket_rejects_unknown_profile(self) -> None:
        body = {"title": "x", "project": "t3-workflow", "profile": "tidak-ada"}
        response, raw = self.request("POST", "/api/tickets", body, token="rahasia")
        self.assertEqual(response.status, 409)
        self.assertIn("profil", json.loads(raw)["error"])

    def test_adopt_existing_thread(self) -> None:
        body = {"title": "Thread lama", "project": "t3-workflow", "profile": "fast"}
        response, raw = self.request("POST", "/api/threads/th-1/adopt", body, token="rahasia")
        self.assertEqual(response.status, 200)
        ticket = json.loads(raw)["ticket"]
        self.assertEqual(ticket["thread_id"], "th-1")
        self.assertEqual(ticket["status"], "ready")  # watcher akan melanjutkan thread ini

    def test_create_ticket_accepts_body_and_attachment(self) -> None:
        body = {
            "title": "dengan lampiran", "project": "t3-workflow", "profile": "fast",
            "body": "konteks panjang", "attachment_path": "/tmp/spec.pdf",
        }
        response, raw = self.request("POST", "/api/tickets", body, token="rahasia")
        self.assertEqual(response.status, 200)
        ticket = json.loads(raw)["ticket"]
        self.assertEqual(ticket["body"], "konteks panjang")
        self.assertEqual(ticket["attachment_path"], "/tmp/spec.pdf")

    def test_tickets_expose_profile_details(self) -> None:
        response, raw = self.request("GET", "/api/tickets")
        details = json.loads(raw)["profileDetails"]
        self.assertEqual(details[0]["name"], "fast")
        self.assertEqual(details[0]["provider"], "pi")

    def test_register_project_validates_path(self) -> None:
        response, raw = self.request(
            "POST", "/api/projects", {"title": "baru", "path": "/tidak/ada"}, token="rahasia"
        )
        self.assertEqual(response.status, 400)
        self.assertIn("path", json.loads(raw)["error"])

    def test_page_is_served(self) -> None:
        response, raw = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn("Papan Tiket", raw)
        self.assertIn("2.5.7", raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
