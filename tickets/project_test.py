"""Derive a project's test command from what the project itself declares.

A ticket's proof comes from a shell command the host runs; the board should not make the
owner type one per ticket when the repository already says how it is tested. Detection is
deliberately conservative: it reads declarations (Makefile targets, manifests) and never
invents a command from the ticket text, because a model-authored command would be judged by
the model that wrote it. When nothing is declared, the field stays empty and the ticket
blocks at `testing` rather than guessing.

Order matters: the first declaration wins, and `check` beats `test` because a repository that
separates the two usually means "check" as the full gate.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

MAKEFILES = ("Makefile", "makefile", "GNUmakefile")
MAKE_TARGET = re.compile(r"^([A-Za-z0-9_.-]+)\s*:", re.MULTILINE)


def _makefile_command(root: Path) -> tuple[str, str]:
    for name in MAKEFILES:
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        targets = set(MAKE_TARGET.findall(text))
        if "check" in targets:
            return "make check", f"{name} target 'check'"
        if "test" in targets:
            return "make test", f"{name} target 'test'"
    return "", ""


def _package_command(root: Path) -> tuple[str, str]:
    path = root / "package.json"
    if not path.is_file():
        return "", ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", ""
    if not (data.get("scripts") or {}).get("test"):
        return "", ""
    dependencies = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
    if "vitest" in dependencies:
        return "npm test -- --run", "package.json 'test' script (vitest)"
    return "npm test", "package.json 'test' script"


def _pytest_declared(root: Path) -> bool:
    if (root / "pytest.ini").is_file():
        return True
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        return "[tool.pytest" in pyproject.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def detect_test_command(root: Path | str) -> tuple[str, str]:
    """Return (command, source) for a project folder, or ("", "") when nothing is declared."""
    root = Path(root).expanduser()
    if not root.is_dir():
        return "", ""
    for detect in (
        _makefile_command,
        lambda path: ("go test ./...", "go.mod") if (path / "go.mod").is_file() else ("", ""),
        _package_command,
        lambda path: ("cargo test", "Cargo.toml") if (path / "Cargo.toml").is_file() else ("", ""),
        lambda path: ("python3 -m pytest -q", "pytest configuration") if _pytest_declared(path) else ("", ""),
        lambda path: (
            ("python3 -m unittest discover -s tests", "Python tests directory")
            if (path / "tests").is_dir() or any(path.glob("test_*.py"))
            else ("", "")
        ),
    ):
        command, source = detect(root)
        if command:
            return command, source
    return "", ""
