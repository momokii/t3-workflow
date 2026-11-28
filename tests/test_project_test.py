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

from project_test import detect_test_command, detect_test_command_info  # noqa: E402


class DetectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="detect-"))

    def test_makefile_check_wins_over_test(self) -> None:
        (self.root / "Makefile").write_text("test:\n\ttrue\ncheck:\n\ttrue\n")
        self.assertEqual(detect_test_command(self.root), ("make check", "Makefile target 'check'"))

    def test_makefile_test_is_used_when_there_is_no_check(self) -> None:
        (self.root / "Makefile").write_text("test:\n\ttrue\n")
        self.assertEqual(detect_test_command(self.root), ("make test", "Makefile target 'test'"))

    def test_go_module_runs_build_vet_and_test(self) -> None:
        (self.root / "go.mod").write_text("module example.com/x\n")
        command, source = detect_test_command(self.root)
        self.assertIn("go build ./...", command)
        self.assertIn("go vet ./...", command)
        self.assertIn("go test -count=1 ./...", command)
        self.assertIn("go.mod", source)

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

    def test_pytest_uses_the_projects_virtualenv(self) -> None:
        (self.root / "pytest.ini").write_text("[pytest]\n")
        venv = self.root / ".venv/bin/python"
        venv.parent.mkdir(parents=True)
        venv.write_text("#!/bin/sh\n")
        venv.chmod(0o755)
        command, source = detect_test_command(self.root)
        self.assertEqual(command, f"{venv} -m pytest -q")
        self.assertIn("pytest", source)

    def test_python_tests_directory(self) -> None:
        (self.root / "tests").mkdir()
        self.assertEqual(detect_test_command(self.root)[0], "python3 -m unittest discover -s tests")

    def test_nothing_declared_stays_empty(self) -> None:
        self.assertEqual(detect_test_command(self.root), ("", ""))

    def test_a_missing_folder_is_empty(self) -> None:
        self.assertEqual(detect_test_command(self.root / "nope"), ("", ""))

    def test_a_vitest_run_script_does_not_get_a_second_run_flag(self) -> None:
        (self.root / "package.json").write_text(json.dumps({
            "scripts": {"test": "vitest run"},
            "devDependencies": {"vitest": "^3"},
        }))
        self.assertEqual(detect_test_command(self.root)[0], "npm test")

    def test_a_declaration_that_needs_a_database_is_flagged(self) -> None:
        (self.root / "Makefile").write_text("check:\n\ttrue\n")
        tests = self.root / "tests"
        tests.mkdir()
        (tests / "conftest.py").write_text("DATABASE_URL = 'postgresql://x'\n")
        info = detect_test_command_info(self.root)
        self.assertEqual(info["command"], "make check")
        self.assertTrue(info["needs_setup"])

    def test_a_makefile_recipe_with_docker_is_flagged(self) -> None:
        (self.root / "Makefile").write_text("check:\n\tdocker compose up -d\n")
        self.assertTrue(detect_test_command_info(self.root)["needs_setup"])

    def test_one_subdirectory_tests_folder_is_reachable(self) -> None:
        sub = self.root / "service"
        (sub / "tests").mkdir(parents=True)
        command, source = detect_test_command(self.root)
        self.assertIn("discover -s service/tests", command)
        self.assertIn("service", source)

    def test_the_home_directory_is_never_a_project_root(self) -> None:
        self.assertEqual(detect_test_command(Path.home()), ("", ""))
