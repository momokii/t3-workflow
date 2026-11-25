#!/usr/bin/env python3
"""Integration test: the polling loop against a fake Telegram server.

Proves, without touching the real Bot API:
  - getUpdates is called with offset after the first round
  - an allowlisted command is answered through sendMessage
  - a chat that is not allowlisted gets no reply
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("t3_bridge", HERE.parent / "t3_bridge.py")
bridge = importlib.util.module_from_spec(SPEC)
sys.modules["t3_bridge"] = bridge
SPEC.loader.exec_module(bridge)

CHAT = "5918929361"


class FakeTelegram(BaseHTTPRequestHandler):
    updates: list[dict] = []
    sent: list[dict] = []
    offsets: list[int | None] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/getUpdates"):
            FakeTelegram.offsets.append(body.get("offset"))
            payload = FakeTelegram.updates.pop(0) if FakeTelegram.updates else []
            self._json({"ok": True, "result": payload})
            return
        if self.path.endswith("/sendMessage"):
            FakeTelegram.sent.append(body)
            self._json({"ok": True, "result": {"message_id": len(FakeTelegram.sent)}})
            return
        self._json({"ok": False, "description": "unknown method"})

    def _json(self, data: dict) -> None:
        raw = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args) -> None:  # senyap
        return


def update(update_id: int, chat_id: str, text: str) -> dict:
    return {"update_id": update_id, "message": {"chat": {"id": int(chat_id)}, "text": text}}


class LoopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), FakeTelegram)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        FakeTelegram.updates = []
        FakeTelegram.sent = []
        FakeTelegram.offsets = []
        self.tmp = Path(tempfile.mkdtemp(prefix="t3bridge-loop-"))

        db = self.tmp / "state.sqlite"
        con = sqlite3.connect(db)
        con.execute("create table projection_projects (project_id text, title text, workspace_root text, deleted_at text)")
        con.execute("insert into projection_projects values ('p1','kelanach','/home/kelanach',null)")
        con.commit()
        con.close()

        self.config = json.loads(json.dumps(bridge.DEFAULTS))
        self.config["telegram"].update(
            {
                "botToken": "test-token",
                "allowChatIds": [CHAT],
                "apiBase": f"http://127.0.0.1:{self.port}",
            }
        )
        self.config["t3"]["dbPath"] = str(db)
        self.config["bridge"]["pollTimeout"] = 1

        bridge.STATE_DIR = self.tmp
        bridge.STATE_PATH = self.tmp / "state.json"
        bridge.LOG_PATH = self.tmp / "bridge.log"
        bridge.AUDIT_PATH = self.tmp / "audit.jsonl"

    def test_allowlisted_command_answered_and_offset_advanced(self) -> None:
        FakeTelegram.updates = [[update(100, CHAT, "/projects")]]
        state = {"offset": None}
        answered = bridge.poll_once(self.config, state)
        self.assertEqual(answered, 1)
        self.assertEqual(state["offset"], 101)
        self.assertEqual(len(FakeTelegram.sent), 1)
        self.assertIn("kelanach", FakeTelegram.sent[0]["text"])
        self.assertEqual(FakeTelegram.sent[0]["chat_id"], CHAT)

        # putaran berikutnya mengirim offset yang sudah maju
        FakeTelegram.updates = []
        bridge.poll_once(self.config, state)
        self.assertEqual(FakeTelegram.offsets[-1], 101)

    def test_unknown_chat_gets_no_reply(self) -> None:
        FakeTelegram.updates = [[update(200, "999", "/projects")]]
        state = {"offset": None}
        answered = bridge.poll_once(self.config, state)
        self.assertEqual(answered, 0)
        self.assertEqual(FakeTelegram.sent, [])
        self.assertEqual(state["offset"], 201)


if __name__ == "__main__":
    unittest.main(verbosity=2)
