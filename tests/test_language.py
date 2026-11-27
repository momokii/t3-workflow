#!/usr/bin/env python3
"""Guard: everything a person reads is written in English.

The owner asked for one language across the app: API errors, board text, Telegram
replies, CLI output, reports, scripts and documentation. Indonesian still slips in
easily — a new error string is usually copied from the nearest existing one — so this
test fails on it instead of relying on review.

Two exemptions, both deliberate:

* this file, which has to contain the words it forbids;
* `REVIEW.md`, a historical record of review rounds. It quotes messages as they were
  at the time and is not rewritten retroactively.

Only files tracked by git are checked: an untracked draft is not part of what the app
ships, and a draft being written by another agent must not turn this guard red. A
tracked file that grows an Indonesian line is caught before it is committed.

Technical words that are identical in both languages (`status`, `model`, `provider`,
`thread`, `ticket` spelled `tiket` is *not* included for that reason) are not in the
list; the list holds function words and Indonesian-only vocabulary.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "build", "dist"}
SUFFIXES = {".py", ".html", ".md", ".sh", ".service", ".timer", ".yml", ".yaml", ".json", ".toml", ".txt"}
EXTRA_NAMES = {"Makefile", "Dockerfile", "docker-compose.yml", ".dockerignore"}

SELF = Path(__file__).resolve()
HISTORY = {ROOT / "REVIEW.md", ROOT / "STATE.md"}

# Function words and Indonesian-only vocabulary. Short ambiguous words (2-3 letters)
# are left out on purpose: they would flag machine text without adding real coverage.
INDONESIAN = (
    "adopsi antre anggaran belum berhenti berkas bisa bukti catatan daftar dari dengan dibuat "
    "dijalankan dikerjakan dimatikan dipakai dipindah dipotong disetujui ditolak gagal "
    "hasil harus jalankan juga judul kalau karena keluar kirim konteks kosong laporan "
    "lewat lulus masuk menunggu milik muat nama oleh pada pakai paksa perintah pilih "
    "pindah prioritas profil proyek rahasia riwayat salah salin selesai semua sedang "
    "siap simpan sudah tanpa tepat terkirim tiket tidak tutup ubah ulangi untuk wajib "
    "waktu yang"
).split()

PATTERN = re.compile(r"\b(" + "|".join(INDONESIAN) + r")\b", re.IGNORECASE)


def candidate_files() -> list[Path]:
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.split("\0")
        paths = [ROOT / name for name in listed if name]
    except (OSError, subprocess.CalledProcessError):  # no git (a tarball), fall back to a walk
        paths = [path for path in ROOT.rglob("*") if path.is_file()]
    files = []
    for path in paths:
        if path == SELF or path in HISTORY:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.suffix in SUFFIXES or path.name in EXTRA_NAMES:
            files.append(path)
    return sorted(files)


def offences() -> list[str]:
    found = []
    for path in candidate_files():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:  # a binary file with a text suffix
            continue
        for number, line in enumerate(lines, 1):
            match = PATTERN.search(line)
            if match:
                rel = path.relative_to(ROOT)
                found.append(f"{rel}:{number}: '{match.group(0)}' in {line.strip()[:100]}")
    return found


class LanguageTests(unittest.TestCase):
    def test_user_facing_text_is_english(self) -> None:
        found = offences()
        self.assertEqual(
            found,
            [],
            f"{len(found)} line(s) still contain Indonesian text:\n" + "\n".join(found[:20]),
        )

    def test_the_guard_catches_a_known_indonesian_line(self) -> None:
        """The detector itself must work; a green suite is not proof of a working check."""
        self.assertTrue(PATTERN.search("tiket tidak ada di T3"))
        self.assertIsNone(PATTERN.search("the ticket is not in T3"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
