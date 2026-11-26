#!/usr/bin/env python3
"""The unit installer renders usable systemd files."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/install.sh"
PLACEHOLDERS = ("@REPO@", "@HOME@", "@PYTHON@", "@HOST@", "@PORT@")


class DeployTests(unittest.TestCase):
    def render(self, *args: str) -> str:
        proc = subprocess.run(["sh", str(SCRIPT), "--print", *args], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_no_placeholder_survives(self) -> None:
        output = self.render("--host", "100.124.184.116")
        for token in PLACEHOLDERS:
            self.assertNotIn(token, output)

    def test_every_unit_is_rendered(self) -> None:
        output = self.render()
        for name in (
            "t3-bridge.service",
            "t3-watcher.service",
            "t3-board.service",
            "t3-alert@.service",
            "t3-backup.service",
            "t3-backup.timer",
        ):
            self.assertIn(f"# {name}", output)

    def test_long_running_units_alert_on_failure(self) -> None:
        output = self.render()
        for name in ("t3-bridge.service", "t3-watcher.service", "t3-board.service"):
            block = output.split(f"# {name}", 1)[1].split("# ", 1)[0]
            self.assertIn("OnFailure=t3-alert@%N.service", block)
            self.assertLess(block.index("OnFailure="), block.index("[Service]"))

    def test_host_and_port_are_substituted(self) -> None:
        output = self.render("--host", "100.64.0.7", "--port", "9999")
        self.assertIn("--host 100.64.0.7 --port 9999", output)

    def test_target_directory_receives_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                ["sh", str(SCRIPT), "--target", tmp], capture_output=True, text=True, timeout=60
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            written = sorted(p.name for p in Path(tmp).iterdir())
            self.assertIn("t3-watcher.service", written)
            self.assertIn("t3-backup.timer", written)


if __name__ == "__main__":
    unittest.main(verbosity=2)
