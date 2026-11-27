#!/usr/bin/env python3
"""Opt-in integration test against the real T3 CLI.

Skipped unless T3_INTEGRATION=1, because it talks to the running server and can
start work. It checks the shapes this project depends on, nothing more:

    T3_INTEGRATION=1 python3 -m unittest tests.test_integration_t3 -v
"""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path

CLI = Path.home() / ".local/bin/t3"
PROJECT = Path.home() / "Public/main-linux-kelanach/code-berkah-titipan-tuhan/code/personal_prjct/t3-workflow"


@unittest.skipUnless(os.environ.get("T3_INTEGRATION") == "1", "set T3_INTEGRATION=1 to run")
class T3Integration(unittest.TestCase):
    def test_cli_reports_a_version(self) -> None:
        proc = subprocess.run([str(CLI), "--version"], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("t3 v", proc.stdout)

    def test_thread_list_shape(self) -> None:
        proc = subprocess.run(
            [str(CLI), "thread", "list", "--project", str(PROJECT), "--json"],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        rows = json.loads(proc.stdout)
        self.assertIsInstance(rows, list)
        for row in rows:
            self.assertIn("threadId", row)
            self.assertIn("status", row)


if __name__ == "__main__":
    unittest.main(verbosity=2)
