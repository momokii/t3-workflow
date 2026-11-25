#!/usr/bin/env python3
"""Tests for t3_bridge (stdlib unittest, no network, no real T3 calls)."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("t3_bridge", HERE.parent / "t3_bridge.py")
bridge = importlib.util.module_from_spec(SPEC)
sys.modules["t3_bridge"] = bridge
SPEC.loader.exec_module(bridge)

CHAT = "5918929361"
THREAD = "1a022144-3688-4c06-a9ba-8f71d7e89984"


def make_config(tmp: Path, **overrides) -> dict:
    db = tmp / "state.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "create table projection_projects (project_id text, title text, workspace_root text, deleted_at text)"
    )
    con.executemany(
        "insert into projection_projects values (?,?,?,?)",
        [
            ("cf3876ca-9b3a-46ee-bd3d-87b5f7e80926", "kelanach", "/home/kelanach", None),
            ("mcp:570b24ed", "petarasa", "/work/petarasa", None),
            ("dup-1", "demo", "/work/demo-a", None),
            ("dup-2", "demo-two", "/work/demo-b", None),
            ("gone", "lama", "/work/old", "2026-01-01"),
        ],
    )
    con.commit()
    con.close()

    cli = tmp / "fake-t3"
    cli.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$T3_ARGS_FILE\"\ncat > \"$T3_STDIN_FILE\"\n")
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)

    config = json.loads(json.dumps(bridge.DEFAULTS))
    config["telegram"]["allowChatIds"] = [CHAT]
    config["telegram"]["botToken"] = "test-token"
    config["t3"]["cli"] = str(cli)
    config["t3"]["dbPath"] = str(db)
    config["bridge"].update(overrides)
    return config


WF_CONFIG = {
    "models": {"fast": {"provider": "pi", "model": "opencode-go/deepseek-v4.1-flash"}},
    "defaults": {"profile": "fast"},
}


class TicketCommandTests(unittest.TestCase):
    """Perintah tiket dan persetujuan lewat bridge."""

    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(root / "tickets"))
        from store import TicketStore

        self.store_cls = TicketStore
        self.tmp = Path(tempfile.mkdtemp(prefix="bridge-tickets-"))
        self.wf_config = self.tmp / "wf.json"
        self.wf_config.write_text(json.dumps(WF_CONFIG))
        self.db = self.tmp / "t.sqlite"
        TicketStore(self.db, self.wf_config).close()

        self.config = json.loads(json.dumps(bridge.DEFAULTS))
        self.config["telegram"].update({"botToken": "t", "allowChatIds": [CHAT], "apiBase": "http://127.0.0.1:1"})
        self.config["workflow"] = {"dbPath": str(self.db), "configPath": str(self.wf_config)}
        bridge.STATE_DIR = self.tmp
        bridge.STATE_PATH = self.tmp / "state.json"
        bridge.LOG_PATH = self.tmp / "bridge.log"
        bridge.AUDIT_PATH = self.tmp / "audit.jsonl"

    def read(self, ticket_id: str):
        store = self.store_cls(self.db, self.wf_config)
        try:
            return store.get(ticket_id)
        finally:
            store.close()

    def test_ticket_command_creates_backlog_ticket(self) -> None:
        answer = bridge.handle(self.config, CHAT, "/ticket t3-workflow perbaiki laporan")
        self.assertIn("T-0001", answer or "")
        ticket = self.read("T-0001")
        self.assertEqual(ticket["status"], "backlog")
        self.assertEqual(ticket["model_profile"], "fast")

    def test_tickets_command_lists(self) -> None:
        bridge.handle(self.config, CHAT, "/ticket t3-workflow satu")
        answer = bridge.handle(self.config, CHAT, "/tickets")
        self.assertIn("T-0001", answer or "")
        self.assertIn("backlog", answer or "")

    def test_approve_command_decides(self) -> None:
        bridge.handle(self.config, CHAT, "/ticket t3-workflow satu")
        store = self.store_cls(self.db, self.wf_config)
        try:
            approval = store.request_approval("T-0001", "mulai?")
        finally:
            store.close()
        answer = bridge.handle(self.config, CHAT, f"/approve {approval['approval_id']}")
        self.assertIn("disetujui", answer or "")
        store = self.store_cls(self.db, self.wf_config)
        try:
            self.assertEqual(store.approval_state("T-0001"), "approved")
        finally:
            store.close()

    def test_second_decision_is_reported_not_crashing(self) -> None:
        bridge.handle(self.config, CHAT, "/ticket t3-workflow satu")
        store = self.store_cls(self.db, self.wf_config)
        try:
            approval = store.request_approval("T-0001", "mulai?")
        finally:
            store.close()
        bridge.handle(self.config, CHAT, f"/approve {approval['approval_id']}")
        answer = bridge.handle(self.config, CHAT, f"/reject {approval['approval_id']}")
        self.assertIn("sudah", answer or "")


class BridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="t3bridge-"))
        self.config = make_config(self.tmp)
        os.environ["T3_ARGS_FILE"] = str(self.tmp / "args.txt")
        os.environ["T3_STDIN_FILE"] = str(self.tmp / "stdin.txt")
        bridge.STATE_DIR = self.tmp
        bridge.STATE_PATH = self.tmp / "state.json"
        bridge.LOG_PATH = self.tmp / "bridge.log"
        bridge.AUDIT_PATH = self.tmp / "audit.jsonl"

    def args(self) -> list[str]:
        return (self.tmp / "args.txt").read_text().splitlines()

    def stdin(self) -> str:
        return (self.tmp / "stdin.txt").read_text()

    # ---------------------------------------------------------- allowlist

    def test_unknown_chat_is_rejected(self) -> None:
        self.assertIsNone(bridge.handle(self.config, "999", "/projects"))
        self.assertFalse((self.tmp / "args.txt").exists())

    def test_known_chat_is_answered(self) -> None:
        answer = bridge.handle(self.config, CHAT, "/help")
        self.assertIn("/projects", answer or "")

    # ---------------------------------------------------------- projects

    def test_projects_skips_deleted(self) -> None:
        answer = bridge.handle(self.config, CHAT, "/projects")
        self.assertIn("kelanach", answer)
        self.assertIn("petarasa", answer)
        self.assertNotIn("lama", answer)

    def test_resolve_project_exact_partial_and_id(self) -> None:
        project, _ = bridge.resolve_project(self.config, "petarasa")
        self.assertEqual(project["workspace_root"], "/work/petarasa")
        project, _ = bridge.resolve_project(self.config, "KELANACH")
        self.assertEqual(project["title"], "kelanach")
        project, _ = bridge.resolve_project(self.config, "cf3876")
        self.assertEqual(project["title"], "kelanach")

    def test_resolve_project_reports_ambiguity(self) -> None:
        # "dem" cocok sebagian dengan "demo" dan "demo-two" -> ambigu.
        # "demo" sendiri harus menang karena cocok persis.
        project, error = bridge.resolve_project(self.config, "dem")
        self.assertIsNone(project)
        self.assertIn("ambigu", error)
        exact, _ = bridge.resolve_project(self.config, "demo")
        self.assertEqual(exact["workspace_root"], "/work/demo-a")

    def test_resolve_project_missing(self) -> None:
        project, error = bridge.resolve_project(self.config, "tidak-ada")
        self.assertIsNone(project)
        self.assertIn("tidak ada", error)

    # ---------------------------------------------------------- send

    def test_send_invokes_cli_with_stdin(self) -> None:
        answer = bridge.handle(self.config, CHAT, f"/send {THREAD} halo dari telegram")
        self.assertIn("Terkirim", answer or "")
        self.assertEqual(self.args(), ["thread", "send", THREAD, "-", "--json"])
        self.assertEqual(self.stdin(), "halo dari telegram")

    def test_send_rejects_bad_thread_id(self) -> None:
        answer = bridge.handle(self.config, CHAT, "/send bukan-uuid halo")
        self.assertIn("tidak valid", answer or "")
        self.assertFalse((self.tmp / "args.txt").exists())

    def test_send_respects_allow_writes_false(self) -> None:
        self.config["bridge"]["allowWrites"] = False
        answer = bridge.handle(self.config, CHAT, f"/send {THREAD} halo")
        self.assertIn("dimatikan", answer or "")
        self.assertFalse((self.tmp / "args.txt").exists())

    def test_send_dry_run_does_not_call_cli(self) -> None:
        self.config["bridge"]["dryRun"] = True
        answer = bridge.handle(self.config, CHAT, f"/send {THREAD} halo")
        self.assertIn("dry-run", answer or "")
        self.assertFalse((self.tmp / "args.txt").exists())

    # ---------------------------------------------------------- new

    def test_new_starts_thread_with_title(self) -> None:
        answer = bridge.handle(self.config, CHAT, "/new petarasa tolong riset rendang")
        self.assertIn("Thread", answer or "")
        args = self.args()
        self.assertEqual(args[:4], ["thread", "start", "/work/petarasa", "-"])
        self.assertIn("--title", args)
        self.assertIn("tolong riset rendang", args)
        self.assertEqual(self.stdin(), "tolong riset rendang")

    # ---------------------------------------------------------- misc

    def test_unknown_command_returns_help(self) -> None:
        answer = bridge.handle(self.config, CHAT, "/halo")
        self.assertIn("tidak dikenal", answer or "")

    def test_id_command(self) -> None:
        self.assertIn(CHAT, bridge.handle(self.config, CHAT, "/id") or "")

    def test_audit_written(self) -> None:
        bridge.handle(self.config, CHAT, "/projects")
        lines = (self.tmp / "audit.jsonl").read_text().strip().splitlines()
        self.assertTrue(any('"command"' in line for line in lines))

    def test_state_roundtrip(self) -> None:
        bridge.save_state({"offset": 42, "seen": 1})
        self.assertEqual(bridge.load_state()["offset"], 42)


if __name__ == "__main__":
    unittest.main(verbosity=2)
