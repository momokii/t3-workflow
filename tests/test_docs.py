#!/usr/bin/env python3
"""Documentation guards: the handoff gate and the links must stay honest."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ["README.md", "HANDOFF.md", "AGENTS.md", "RESEARCH.md", "REVIEW.md", "CHANGELOG.md"] + [
    f"docs/{p.name}" for p in (ROOT / "docs").glob("*.md")
]


class DocsTests(unittest.TestCase):
    def test_handoff_prompt_requires_snapshot_and_gate(self) -> None:
        text = (ROOT / "HANDOFF.md").read_text()
        prompt = text.split("## 10. Copy-paste prompt", 1)[1]
        for needle in ("make snapshot", "STATE.md", "CONFIRMATION", "go"):
            self.assertIn(needle, prompt, f"prompt must mention {needle}")
        # the three snapshot numbers must be requested explicitly
        for needle in ("HEAD", "commit count", "uncommitted"):
            self.assertIn(needle, prompt)

    def test_relative_links_resolve(self) -> None:
        broken = []
        for name in DOCS:
            path = ROOT / name
            for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", path.read_text()):
                if target.startswith(("http", "mailto:")):
                    continue
                if not (path.parent / target).exists() and not (ROOT / target).exists():
                    broken.append(f"{name} -> {target}")
        self.assertEqual(broken, [])

    def test_handoff_does_not_claim_a_current_head(self) -> None:
        text = (ROOT / "HANDOFF.md").read_text()
        self.assertIn("at writing", text)
        self.assertIn("make snapshot", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
