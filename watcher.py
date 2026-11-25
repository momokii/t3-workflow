#!/usr/bin/env python3
"""Watcher: dev stage and test stage, one step at a time.

Dev stage   `ready`            → claim → `t3 thread start` → `in-progress` (+ thread id)
Test stage  `testing`          → run the ticket's test command → proof
                                → `review` when exit code is 0
                                → `in-progress` with the failure note otherwise

The order matters: a ticket waiting in `testing` is handled before a new ticket is
started, so the loop drains work instead of piling up threads.

    python3 watcher.py --once --dry-run
    python3 watcher.py --once            # satu langkah
    python3 watcher.py --loop            # jalan terus, interval dari config

Model selection is never invented here: the profile comes from the ticket or from
`defaults.profile`, and both live in the config the human owns. A ticket marked
`requires_approval` waits for an approval decision recorded in the store.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
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
TEST_TIMEOUT = 1800


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


def approval_gate(store: TicketStore, ticket: dict) -> str | None:
    """Return None when the ticket may proceed, else a message explaining the wait."""
    if not ticket.get("requires_approval"):
        return None
    state = store.approval_state(ticket["id"])
    if state == "approved":
        return None
    if state == "pending":
        return f"{ticket['id']} menunggu persetujuan"
    if state == "rejected":
        store.move(ticket["id"], "blocked", "persetujuan ditolak", force=True)
        return f"{ticket['id']} ditolak → blocked"
    approval = store.request_approval(ticket["id"], f"Mulai tiket {ticket['id']}: {ticket['title']}")
    return f"{ticket['id']} butuh persetujuan (approval #{approval['approval_id']})"


# ---------------------------------------------------------------- dev stage


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
    gate = approval_gate(store, ticket)
    if gate:
        return gate
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
        store.move(ticket["id"], "blocked", f"start gagal: {type(exc).__name__}", force=True)
        raise TicketError(f"gagal menjalankan t3: {type(exc).__name__}: {exc}") from exc
    if proc.returncode != 0:
        store.move(ticket["id"], "blocked", f"t3 rc={proc.returncode}", force=True)
        raise TicketError(f"t3 gagal (rc={proc.returncode}): {(proc.stderr or proc.stdout or '')[:300]}")
    try:
        thread_id = json.loads(proc.stdout)["threadId"]
    except (json.JSONDecodeError, KeyError) as exc:
        store.move(ticket["id"], "blocked", "output t3 bukan JSON", force=True)
        raise TicketError(f"output t3 tidak berisi threadId: {(proc.stdout or '')[:200]}") from exc
    store.attach_thread(ticket["id"], thread_id)
    return f"{ticket['id']} → thread {thread_id} (profil {profile['name']})"


# ---------------------------------------------------------------- test stage


def run_test(store: TicketStore, ticket: dict, runner=subprocess.run) -> str:
    """Run the ticket's test command and turn the exit code into a status move."""
    command = (ticket.get("test_cmd") or "").strip()
    if not command:
        store.attach_proof(ticket["id"], False, "tidak ada test_cmd pada tiket")
        store.move(ticket["id"], "in-progress", "test dilewati: test_cmd kosong", force=True)
        return f"{ticket['id']}: test_cmd kosong → in-progress"
    cwd = project_path(ticket["project"])
    try:
        proc = runner(
            command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=TEST_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        output = f"{type(exc).__name__}: {exc}"
        store.attach_proof(ticket["id"], False, output)
        store.move(ticket["id"], "in-progress", "test timeout/error", force=True)
        return f"{ticket['id']}: test error ({type(exc).__name__}) → in-progress"

    output = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
    tail = "\n".join(output.splitlines()[-40:])
    ok = proc.returncode == 0
    store.attach_proof(ticket["id"], ok, tail or "(tanpa output)")
    if ok:
        store.move(ticket["id"], "review", "test lulus", force=True)
        return f"{ticket['id']}: test lulus (rc=0) → review"
    store.move(ticket["id"], "in-progress", f"test gagal (rc={proc.returncode})", force=True)
    return f"{ticket['id']}: test gagal (rc={proc.returncode}) → in-progress"


def next_testing(store: TicketStore) -> dict | None:
    rows = [ticket for ticket in store.list("testing")]
    return rows[0] if rows else None


def step(store: TicketStore, config: dict, t3_cli: str, dry_run: bool, worker: str = "watcher",
         runner=subprocess.run) -> str:
    """One unit of work: drain `testing` first, then start new work."""
    testing = next_testing(store)
    if testing is not None:
        if dry_run:
            return f"[dry-run] {testing['id']} · jalankan test: {testing['test_cmd'] or '(kosong)'}"
        return run_test(store, testing, runner=runner)
    return run_once(store, config, t3_cli, dry_run, worker, runner=runner)


def loop(store_factory, config: dict, t3_cli: str, interval: int, worker: str) -> None:
    print(f"watcher mulai (interval {interval}s)", flush=True)
    while True:
        store = store_factory()
        try:
            message = step(store, config, t3_cli, dry_run=False, worker=worker)
        except TicketError as exc:
            message = f"error: {exc}"
        finally:
            store.close()
        print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)
        time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="satu langkah lalu keluar")
    parser.add_argument("--loop", action="store_true", help="jalan terus dengan interval dari config")
    parser.add_argument("--dry-run", action="store_true", help="cetak rencana, tidak mengubah apa pun")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--worker", default="watcher")
    parser.add_argument("--interval", type=int, default=0, help="detik antar langkah (default dari config)")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    t3_cli = (config.get("t3") or {}).get("cli", str(Path.home() / ".local/bin/t3"))
    interval = args.interval or int((config.get("watcher") or {}).get("intervalSeconds", 120))

    def factory() -> TicketStore:
        return TicketStore(Path(args.db), Path(args.config))

    store = factory()
    try:
        if args.loop:
            store.close()
            loop(factory, config, t3_cli, interval, args.worker)
            return 0
        if args.once:
            print(step(store, config, t3_cli, args.dry_run, args.worker))
        else:
            print("pakai --once atau --loop")
    except TicketError as exc:
        print(f"ditolak: {exc}", file=sys.stderr)
        return 2
    finally:
        if store:
            try:
                store.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
