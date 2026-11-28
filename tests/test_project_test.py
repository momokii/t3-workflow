#!/usr/bin/env python3
"""A project's test command is read from what the project declares."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tickets"))

from project_test import detect_test_command  # noqa: E402


class DetectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="detect-"))

    def test_makefile_check_wins_over_test(self) -> None:
        (self.root / "Makefile").write_text("test:\n\ttrue\ncheck:\n\ttrue\n")
        self.assertEqual(detect_test_command(self.root), ("make check", "Makefile target 'check'"))

    def test_makefile_test_is_used_when_there_is_no_check(self) -> None:
        (self.root / "Makefile").write_text("test:\n\ttrue\n")
        self.assertEqual(detect_test_command(self.root), ("make test", "Makefile target 'test'"))

    def test_go_module(self) -> None:
        (self.root / "go.mod").write_text("module example.com/x\n")
        self.assertEqual(detect_test_command(self.root), ("go test ./...", "go.mod"))

    def test_vitest_gets_a_single_run(self) -> None:
        (self.root / "package.json").write_text(json.dumps({
            "scripts": {"test": "vitest"},
            "devDependencies": {"vitest": "^3"},
        }))
        self.assertEqual(detect_test_command(self.root)[0], "npm test -- --run")

    def test_plain_npm_test(self) -> None:
        (self.root / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
        self.assertEqual(detect_test_command(self.root), ("npm test", "package.json 'test' script"))

    def test_cargo(self) -> None:
        (self.root / "Cargo.toml").write_text("[package]\nname = 'x'\n")
        self.assertEqual(detect_test_command(self.root), ("cargo test", "Cargo.toml"))

    def test_pytest_declaration(self) -> None:
        (self.root / "pytest.ini").write_text("[pytest]\n")
        self.assertEqual(detect_test_command(self.root)[0], "python3 -m pytest -q")

    def test_python_tests_directory(self) -> None:
        (self.root / "tests").mkdir()
        self.assertEqual(detect_test_command(self.root)[0], "python3 -m unittest discover -s tests")

    def test_nothing_declared_stays_empty(self) -> None:
        self.assertEqual(detect_test_command(self.root), ("", ""))

    def test_a_missing_folder_is_empty(self) -> None:
        self.assertEqual(detect_test_command(self.root / "nope"), ("", ""))
