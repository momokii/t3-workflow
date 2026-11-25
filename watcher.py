#!/usr/bin/env python3
"""Watcher (phase 4, dry-run first).

Takes the oldest `ready` ticket, resolves the model profile the human configured,
and starts a T3 thread for it. In dry-run it prints exactly what it would run and
touches nothing.

    python3 watcher.py --once --dry-run
    python3 watcher.py --once

Model selection is never invented here: the profile comes from the ticket or from
`defaults.profile`, and both live in the config the human owns.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "tickets"))

from store import (  # noqa: E402
    CONFIG_PATH,
    DB_PATH,
    TicketError,
    TicketStore,
    load_config,
    resolve_profile,
)

T3_DB = Path.home() / ".t3/userdata/statev2.sqlite"


def project_path(project: str, t3_db: Path = T3_DB) -> str:
    """A ticket may name a project by title or by absolute path."""
    if project.startswith("/"):
        return project
    if not t3_db.exists():
        raise TicketError(f"project '{project}' bukan path absolut dan database T3 tidak ada")
    con = sqlite3.connect(f"file:{t3_db}?mode=ro", uri=True, timeout=5)
    try:
        rows = con.execute(
            "select workspace_root from projection_projects where deleted_at is null and lower(title)=lower(?)",
            (project,),
        ).fetchall()
    finally:
        con.close()
    if not rows:
        raise TicketError(f"project '{project}' tidak ada di T3 (pakai judul persis atau path absolut)")
    if len(rows) > 1:
        raise TicketError(f"project '{project}' ambigu ({len(rows)} kandidat)")
    return rows[0][0]


def build_prompt(ticket: dict) -> str:
    parts = [ticket["title"], "", ticket["body"] or "(tanpa deskripsi)"]
    if ticket["test_cmd"]:
        parts += ["", "Perintah test yang harus lulus:", ticket["test_cmd"]]
    parts += ["", f"Tiket: {ticket['id']} · project: {ticket['project']}"]
    return "\n".join(parts).strip()


def plan(ticket: dict, config: dict, t3_cli: str) -> tuple[list[str], str, dict]:
    profile = resolve_profile(config, ticket["model_profile"])
    path = project_path(ticket["project"])
    title = ticket["title"][:70]
    command = [
        t3_cli, "thread", "start", path, "-",
        "--title", title,
        "--provider", profile["provider"],
        "--model", profile["model"],
        "--json",
    ]
    return command, build_prompt(ticket), profile


def run_once(
    store: TicketStore,
    config: dict,
    t3_cli: str,
    dry_run: bool,
    worker: str = "watcher",
    runner=subprocess.run,
) -> str:
    ticket = store.next_ready()
    if ticket is None:
        return "tidak ada tiket berstatus ready"
    command, prompt, profile = plan(ticket, config, t3_cli)
    if dry_run:
        return (
            f"[dry-run] {ticket['id']} · profil {profile['name']} ({profile['provider']} / {profile['model']})\n"
            f"  {' '.join(command[:5])} … --provider {profile['provider']} --model {profile['model']}\n"
            f"  prompt: {prompt.splitlines()[0][:70]}"
        )

    store.claim(ticket["id"], worker)
    try:
        proc = runner(command, input=prompt, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        store.move(ticket["id"], "blocked", f"start gagal: {type(exc).__name__}")
        raise TicketError(f"gagal menjalankan t3: {type(exc).__name__}: {exc}") from exc
    if proc.returncode != 0:
        store.move(ticket["id"], "blocked", f"t3 rc={proc.returncode}")
        raise TicketError(f"t3 gagal (rc={proc.returncode}): {(proc.stderr or proc.stdout or '')[:300]}")
    try:
        thread_id = json.loads(proc.stdout)["threadId"]
    except (json.JSONDecodeError, KeyError) as exc:
        store.move(ticket["id"], "blocked", "output t3 bukan JSON")
        raise TicketError(f"output t3 tidak berisi threadId: {(proc.stdout or '')[:200]}") from exc
    store.attach_thread(ticket["id"], thread_id)
    return f"{ticket['id']} → thread {thread_id} (profil {profile['name']})"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="proses satu tiket lalu keluar")
    parser.add_argument("--dry-run", action="store_true", help="cetak rencana, tidak mengubah apa pun")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--worker", default="watcher")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    t3_cli = (config.get("t3") or {}).get("cli", str(Path.home() / ".local/bin/t3"))
    store = TicketStore(Path(args.db), Path(args.config))
    try:
        if args.once:
            print(run_once(store, config, t3_cli, args.dry_run, args.worker))
        else:
            print("untuk sekarang hanya mode --once yang didukung (loop menyusul di fase berikutnya)")
    except TicketError as exc:
        print(f"ditolak: {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
