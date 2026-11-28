"""Derive a project's test command from what the project itself declares.

A ticket's proof is a shell command the host runs; the board should not make the owner type
one per ticket when the repository already says how it is tested. Detection is conservative:
it reads declarations (Makefile targets, manifests, README-adjacent files) and never invents a
command from the ticket text, because a model-authored command would be judged by the model
that wrote it.

`needs_setup` marks a declaration that cannot run unattended (a database, Docker, a browser, a
watch process). The board shows such a command as a suggestion but does not fill the field:
running it would fail three times and block the ticket for the wrong reason.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

MAKEFILES = ("Makefile", "makefile", "GNUmakefile")
MAKE_TARGET = re.compile(r"^([A-Za-z0-9_.-]+)\s*:", re.MULTILINE)
SETUP_MARKERS = (
    "docker", "compose", "alembic", "psql", "pg_isready", "pg_ctl", "createdb",
    "npm ci", "pip install", "uv sync", "curl ", "git clone", "playwright", "cypress",
)
WATCH_MARKERS = ("--watch", " watch", "dev", "serve", "preview", "start")
SERVICE_MARKERS = ("postgres", "database_url", "psycopg", "testcontainers", "docker")


def _info(command: str = "", source: str = "", needs_setup: bool = False) -> dict:
    return {"command": command, "source": source, "needs_setup": needs_setup}


def _recipe(text: str, target: str) -> str:
    lines = text.splitlines()
    active = False
    recipe: list[str] = []
    for line in lines:
        if re.match(rf"^{re.escape(target)}\s*:", line):
            active = True
            continue
        if active:
            if line.startswith("\t"):
                recipe.append(line)
            elif line.strip():
                break
    return "\n".join(recipe).lower()


@lru_cache(maxsize=8)
def _python_can_import(module: str) -> bool:
    try:
        proc = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            capture_output=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _python(root: Path) -> str:
    venv = root / ".venv/bin/python"
    if os.access(venv, os.X_OK):
        return str(venv)
    return "python3"


def _tests_need_services(root: Path) -> bool:
    """A conftest that reaches for a database turns a declared command into a setup task."""
    candidates = list(root.glob("tests/conftest.py")) + list(root.glob("*/tests/conftest.py"))
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        if any(marker in text for marker in SERVICE_MARKERS):
            return True
    return False


def _makefile(root: Path) -> dict:
    for name in MAKEFILES:
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        targets = set(MAKE_TARGET.findall(text))
        for target in ("check", "test"):
            if target in targets:
                recipe = _recipe(text, target)
                setup = (any(marker in recipe for marker in SETUP_MARKERS)
                         or _tests_need_services(root))
                return _info(f"make {target}", f"{name} target '{target}'", setup)
    return _info()


def _go(root: Path) -> dict:
    if not (root / "go.mod").is_file():
        return _info()
    command = "go build ./... && go vet ./... && go test -count=1 ./..."
    web = root / "web/package.json"
    try:
        scripts = (json.loads(web.read_text(encoding="utf-8")).get("scripts") or {}) if web.is_file() else {}
    except (OSError, json.JSONDecodeError):
        scripts = {}
    if scripts.get("test"):
        command += " && npm test --prefix web -- --run"
    return _info(command, "go.mod (build, vet, test)")


def _package(root: Path) -> dict:
    path = root / "package.json"
    if not path.is_file():
        return _info()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _info()
    script = (data.get("scripts") or {}).get("test")
    if not script:
        return _info()
    lowered = script.lower()
    if any(marker in lowered for marker in ("playwright", "cypress", "e2e")):
        return _info("npm test", "package.json 'test' script (browser suite)", True)
    if "vitest run" in lowered or "--run" in lowered:
        return _info("npm test", "package.json 'test' script")
    dependencies = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
    if "vitest" in dependencies:
        return _info("npm test -- --run", "package.json 'test' script (vitest)")
    if any(marker in f" {lowered}" for marker in WATCH_MARKERS):
        return _info("npm test", "package.json 'test' script (may watch)", True)
    return _info("npm test", "package.json 'test' script")


def _pytest_declared(root: Path) -> bool:
    if (root / "pytest.ini").is_file() or (root / "tox.ini").is_file():
        return True
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        return "[tool.pytest" in pyproject.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _python_project(root: Path) -> dict:
    python = _python(root)
    venv = python != "python3"
    if _pytest_declared(root):
        if venv or _python_can_import("pytest"):
            return _info(f"{python} -m pytest -q", "pytest configuration")
        return _info("", "pytest is declared but not installed", True)
    if (root / "tests").is_dir() or any(root.glob("test_*.py")):
        return _info(f"{python} -m unittest discover -s tests", "Python tests directory")
    candidates = sorted(path for path in root.glob("*/tests") if path.is_dir())
    if len(candidates) == 1:
        sub = candidates[0].relative_to(root)
        return _info(f"{python} -m unittest discover -s {sub}", f"tests directory under {sub.parent}")
    if len(candidates) > 1:
        return _info("", "several test directories; choose one", True)
    return _info()


def detect_test_command_info(root: Path | str) -> dict:
    """The command a project declares, where it came from, and whether it needs setup."""
    path = Path(root).expanduser()
    try:
        path = path.resolve()
    except OSError:
        return _info()
    if not path.is_dir() or path == Path.home().resolve():
        return _info()  # never treat the home directory as a project root
    for rule in (_makefile, _go, _package,
                 lambda item: _info("cargo test", "Cargo.toml") if (item / "Cargo.toml").is_file() else _info(),
                 _python_project):
        found = rule(path)
        if found["command"] or found["needs_setup"]:
            return found
    return _info()


def detect_test_command(root: Path | str) -> tuple[str, str]:
    """Backward-compatible pair for callers that only need command and source."""
    found = detect_test_command_info(root)
    return found["command"], found["source"]
