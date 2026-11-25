#!/usr/bin/env python3
"""Release stage: branch, commit, push to a local bare remote."""

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
    "release": {"enabled": True, "remote": "origin", "mode": "branch"},
}


def git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


class ReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["T3_REPORTS_DIR"] = str(Path(tempfile.mkdtemp(prefix="rel-reports-")) / "reports")
        self.tmp = Path(tempfile.mkdtemp(prefix="release-"))
        self.config_path = self.tmp / "config.json"
        self.config_path.write_text(json.dumps(CONFIG))
        self.store = TicketStore(self.tmp / "t.sqlite", self.config_path)

        # repo kerja + remote bare lokal
        self.remote = self.tmp / "remote.git"
        git(["init", "--bare", str(self.remote)], self.tmp)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(["init", "-b", "main"], self.repo)
        git(["config", "user.email", "t@example.com"], self.repo)
        git(["config", "user.name", "Test"], self.repo)
        (self.repo / "a.txt").write_text("awal\n")
        git(["add", "-A"], self.repo)
        git(["commit", "-m", "chore: init"], self.repo)
        git(["remote", "add", "origin", str(self.remote)], self.repo)
        git(["push", "-u", "origin", "main"], self.repo)

    def tearDown(self) -> None:
        self.store.close()

    def ticket(self):
        """Tiket yang sudah lulus test, siap masuk tahap release."""
        t = self.store.add("Tambah fitur X", str(self.repo), model_profile="fast",
                           status="review", test_cmd="true")
        self.store.attach_proof(t["id"], True, "12 tests OK")
        return self.store.get(t["id"])

    def test_release_pushes_branch_and_finishes_ticket(self) -> None:
        ticket = self.ticket()
        (self.repo / "b.txt").write_text("perubahan\n")
        message = watcher.release(self.store, ticket, CONFIG)
        self.assertIn(ticket["id"], message)

        branches = subprocess.run(
            ["git", "ls-remote", "--heads", str(self.remote)], capture_output=True, text=True
        ).stdout
        self.assertIn("ticket/", branches)

        stored = self.store.get(ticket["id"])
        self.assertEqual(stored["status"], "done")
        self.assertIn("ticket/", stored["branch"])
        self.assertTrue(stored["report_path"])
        self.assertTrue(Path(stored["report_path"]).exists())

    def test_release_leaves_pr_url_empty_for_non_http_remote(self) -> None:
        """Remote berupa path lokal tidak menghasilkan tautan PR — dan itu benar."""
        ticket = self.ticket()
        (self.repo / "b.txt").write_text("perubahan\n")
        watcher.release(self.store, ticket, CONFIG)
        self.assertEqual(self.store.get(ticket["id"])["pr_url"], "")

    def test_compare_url_from_ssh_and_https(self) -> None:
        self.assertEqual(
            watcher.compare_url("git@github.com:o/r.git", "main", "b"),
            "https://github.com/o/r/compare/main...b?expand=1",
        )
        self.assertEqual(
            watcher.compare_url("https://github.com/o/r", "main", "b"),
            "https://github.com/o/r/compare/main...b?expand=1",
        )
        self.assertEqual(watcher.compare_url("/tmp/bare.git", "main", "b"), "")

    def test_release_refuses_without_passing_proof(self) -> None:
        ticket = self.store.add("tanpa bukti", str(self.repo), model_profile="fast", status="review")
        with self.assertRaises(TicketError) as ctx:
            watcher.release(self.store, ticket, CONFIG)
        self.assertIn("bukti test", str(ctx.exception))
        self.assertEqual(self.store.get(ticket["id"])["status"], "review")

    def test_release_refuses_unknown_mode(self) -> None:
        ticket = self.ticket()
        (self.repo / "c.txt").write_text("x\n")
        config = json.loads(json.dumps(CONFIG))
        config["release"]["mode"] = "main"
        with self.assertRaises(TicketError):
            watcher.release(self.store, ticket, config)

    def test_release_writes_pr_body(self) -> None:
        ticket = self.ticket()
        (self.repo / "b.txt").write_text("perubahan\n")
        watcher.release(self.store, ticket, CONFIG)
        stored = self.store.get(ticket["id"])
        self.assertTrue(stored["pr_body_path"])
        body = Path(stored["pr_body_path"]).read_text()
        self.assertIn(ticket["id"], body)
        self.assertIn("Bukti test", body)

    def test_release_refuses_without_remote(self) -> None:
        ticket = self.ticket()
        git(["remote", "remove", "origin"], self.repo)
        with self.assertRaises(TicketError) as ctx:
            watcher.release(self.store, ticket, CONFIG)
        self.assertIn("remote", str(ctx.exception))
        self.assertEqual(self.store.get(ticket["id"])["status"], "review")

    def test_release_refuses_disabled(self) -> None:
        ticket = self.ticket()
        config = json.loads(json.dumps(CONFIG))
        config["release"]["enabled"] = False
        with self.assertRaises(TicketError):
            watcher.release(self.store, ticket, config)

    def test_release_does_not_touch_clean_tree(self) -> None:
        ticket = self.ticket()
        message = watcher.release(self.store, ticket, CONFIG)
        self.assertIn("tidak ada perubahan", message.lower())
        # tetap selesai, tapi tanpa branch baru
        self.assertEqual(self.store.get(ticket["id"])["status"], "done")


if __name__ == "__main__":
    unittest.main(verbosity=2)
