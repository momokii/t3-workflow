#!/usr/bin/env python3
"""API tests for the kanban board."""

from __future__ import annotations

import http.client
import importlib.util
import json
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

    def test_page_is_served(self) -> None:
        response, raw = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn("Papan Tiket", raw)
        self.assertIn("2.5.7", raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
