#!/usr/bin/env python3
"""Backup: sqlite consistency and report copies."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tickets"))

from store import TicketStore  # noqa: E402


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="backup-"))
        self.reports = self.tmp / "reports"
        self.reports.mkdir()
        os.environ["T3_REPORTS_DIR"] = str(self.reports)
        self.config = self.tmp / "config.json"
        self.config.write_text(json.dumps({"models": {}, "defaults": {"profile": ""}}))
        self.store = TicketStore(self.tmp / "t.sqlite", self.config)
        self.store.add("satu", "/tmp/p", status="backlog")
        self.store.add("dua", "/tmp/p", status="ready")

    def tearDown(self) -> None:
        self.store.close()

    def test_backup_is_a_readable_copy(self) -> None:
        target = self.store.backup(self.tmp / "out")
        self.assertTrue(target.exists())
        con = sqlite3.connect(target)
        try:
            self.assertEqual(con.execute("select count(*) from tickets").fetchone()[0], 2)
        finally:
            con.close()

    def test_backup_does_not_touch_source(self) -> None:
        before = len(self.store.list())
        self.store.backup(self.tmp / "out")
        self.assertEqual(len(self.store.list()), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
