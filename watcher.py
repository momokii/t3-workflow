#!/usr/bin/env python3
"""Watcher: dev stage and test stage, one step at a time.

Release stage `review`         → branch `ticket/T-xxxx-…`, commit, push to origin
                                → `done` (+ report); never pushes the base branch
Test stage  `testing`          → run the ticket's test command → proof
                                → `review` when exit code is 0
                                → `in-progress` with the failure note otherwise
Dev stage   `ready`            → claim → `t3 thread start` → `in-progress` (+ thread id)

The order matters: a ticket waiting in `testing` is handled before a new ticket is
started, so the loop drains work instead of piling up threads.

    python3 watcher.py --once --dry-run
    python3 watcher.py --once            # one step
    python3 watcher.py --loop            # keep running, interval from config

Model selection is never invented here: the profile comes from the ticket or from
`defaults.profile`, and both live in the config the human owns. A ticket marked
`requires_approval` waits for an approval decision recorded in the store.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "tickets"))

from catalog import CatalogError, load_catalog, resolve_ticket  # noqa: E402
from store import (  # noqa: E402
    CONFIG_PATH,
    DB_PATH,
    TicketError,
    TicketStore,
    load_config,
)

T3_DB = Path.home() / ".t3/userdata/statev2.sqlite"
TEST_TIMEOUT = 1800


def project_path(project: str, t3_db: Path = T3_DB) -> str:
    """A ticket may name a project by title or by absolute path."""
    if project.startswith("/"):
        return project
    if not t3_db.exists():
        raise TicketError(f"project '{project}' is not an absolute path and the T3 database is missing")
    con = sqlite3.connect(f"file:{t3_db}?mode=ro", uri=True, timeout=5)
    try:
        rows = con.execute(
            "select workspace_root from projection_projects where deleted_at is null and lower(title)=lower(?)",
            (project,),
        ).fetchall()
    finally:
        con.close()
    if not rows:
        raise TicketError(f"project '{project}' is not in T3 (use the exact title or an absolute path)")
    if len(rows) > 1:
        raise TicketError(f"project '{project}' is ambiguous ({len(rows)} candidates)")
    return rows[0][0]


def build_prompt(ticket: dict) -> str:
    parts = [ticket["title"], "", ticket["body"] or "(no description)"]
    if ticket.get("attachment_path"):
        parts += ["", f"Attachment (read it from this path when needed): {ticket['attachment_path']}"]
    if ticket["test_cmd"]:
        parts += ["", "Test command that must pass:", ticket["test_cmd"]]
    parts += [
        "",
        f"Ticket: {ticket['id']} · project: {ticket['project']}",
        "Finish this ticket and report the result. The watcher owns the ticket status; "
        "you do not need to move it.",
    ]
    return "\n".join(parts).strip()


def thread_status(config: dict, thread_id: str, runner=None) -> str:
    """Thread status as T3 reports it: running, completed, failed, …"""
    cli = (config.get("t3") or {}).get("cli", str(Path.home() / ".local/bin/t3"))
    try:
        proc = (runner or subprocess.run)(
            [cli, "thread", "show", thread_id, "--json", "--messages", "1"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if proc.returncode != 0:
        return "unknown"
    try:
        return str((json.loads(proc.stdout).get("thread") or {}).get("status") or "unknown")
    except json.JSONDecodeError:
        return "unknown"


def sync_threads(store: TicketStore, config: dict, runner=None) -> list[str]:
    """Move tickets whose thread already stopped to testing or blocked.

    This is what makes thread adoption real: the work happens in the same thread and
    the watcher advances the ticket from the thread's status, not from a guess.
    """
    notes: list[str] = []
    for ticket in store.list("in-progress"):
        thread_id = ticket.get("thread_id")
        if not thread_id:
            continue
        status = thread_status(config, thread_id, runner=runner)
        if status in ("completed", "idle"):
            store.move(ticket["id"], "testing", f"thread finished ({status})")
            notes.append(f"{ticket['id']} → testing (thread {status})")
        elif status in ("failed", "cancelled", "interrupted"):
            store.move(ticket["id"], "blocked", f"thread {status}")
            notes.append(f"{ticket['id']} → blocked (thread {status})")
    return notes


def approval_gate(store: TicketStore, ticket: dict) -> str | None:
    """Return None when the ticket may proceed, else a message explaining the wait."""
    if not ticket.get("requires_approval"):
        return None
    state = store.approval_state(ticket["id"])
    if state == "approved":
        return None
    if state == "pending":
        return f"{ticket['id']} is waiting for approval"
    if state == "rejected":
        store.move(ticket["id"], "blocked", "approval rejected")
        return f"{ticket['id']} was rejected → blocked"
    approval = store.request_approval(ticket["id"], f"Start ticket {ticket['id']}: {ticket['title']}")
    return f"{ticket['id']} needs approval (approval #{approval['approval_id']})"


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
        return "no ticket is ready"
    gate = approval_gate(store, ticket)
    if gate:
        return gate
    prompt = build_prompt(ticket)
    try:
        selection = resolve_ticket(ticket, config)
    except CatalogError as exc:
        raise TicketError(str(exc)) from exc
    source = selection["source"]
    if source == "ticket":
        label = "ticket choice"
    elif source.startswith("profile:"):
        label = f"preset {source.split(':', 1)[1]}"
    else:
        label = f"default {source.split(':', 1)[1]}"
    profile = {"name": label, "provider": selection["provider"], "model": selection["model"]}
    if dry_run:
        if ticket.get("thread_id"):
            return (
                f"[dry-run] {ticket['id']} · continue thread {ticket['thread_id']} "
                f"(profile {profile['name']})"
            )
        preview = [
            t3_cli, "thread", "start", project_path(ticket["project"]), "-",
            "--title", ticket["title"][:70], "--provider", profile["provider"],
            "--model", profile["model"], "--json",
        ]
        return (
            f"[dry-run] {ticket['id']} · profile {profile['name']} "
            f"({profile['provider']} / {profile['model']})\n"
            f"  {' '.join(preview[:5])} …\n"
            f"  prompt: {prompt.splitlines()[0][:70]}"
        )

    store.claim(ticket["id"], worker)

    if ticket.get("thread_id"):
        # The ticket carries a thread: continue it, never start a second one.
        command = [t3_cli, "thread", "send", ticket["thread_id"], "-", "--json"]
        try:
            proc = runner(command, input=prompt, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as exc:
            store.move(ticket["id"], "blocked", f"thread send failed: {type(exc).__name__}")
            raise TicketError(f"failed to continue thread: {exc}") from exc
        if proc.returncode != 0:
            store.move(ticket["id"], "blocked", f"thread send rc={proc.returncode}")
            raise TicketError(f"thread send failed (rc={proc.returncode}): {(proc.stderr or proc.stdout or '')[:200]}")
        store.record_event(ticket["id"], "continued", f"thread {ticket['thread_id']}")
        return f"{ticket['id']} → continuing thread {ticket['thread_id']} (profile {profile['name']})"

    command = [t3_cli, "thread", "start", project_path(ticket["project"]), "-",
               "--title", ticket["title"][:70],
               "--provider", profile["provider"], "--model", profile["model"], "--json"]
    try:
        proc = runner(command, input=prompt, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        store.move(ticket["id"], "blocked", f"start failed: {type(exc).__name__}")
        raise TicketError(f"failed to run t3: {type(exc).__name__}: {exc}") from exc
    if proc.returncode != 0:
        store.move(ticket["id"], "blocked", f"t3 rc={proc.returncode}")
        raise TicketError(f"t3 failed (rc={proc.returncode}): {(proc.stderr or proc.stdout or '')[:300]}")
    try:
        thread_id = json.loads(proc.stdout)["threadId"]
    except (json.JSONDecodeError, KeyError) as exc:
        store.move(ticket["id"], "blocked", "t3 output is not JSON")
        raise TicketError(f"t3 output has no threadId: {(proc.stdout or '')[:200]}") from exc
    store.attach_thread(ticket["id"], thread_id)
    return f"{ticket['id']} → thread {thread_id} (profile {profile['name']})"


# ---------------------------------------------------------------- release stage


def _git(args: list[str], cwd: str, timeout: int = 180) -> tuple[int, str]:
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def _slug(text: str, limit: int = 40) -> str:
    out = "".join(ch.lower() if ch.isalnum() else "-" for ch in text)
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")[:limit] or "ticket"


def compare_url(remote_url: str, base: str, branch: str) -> str:
    """Build a 'create PR' link from the remote URL, without gh or a token."""
    url = remote_url.strip()
    if url.startswith("git@"):  # git@github.com:owner/repo.git
        host, _, path = url[4:].partition(":")
        url = f"https://{host}/{path}"
    if url.endswith(".git"):
        url = url[:-4]
    if not url.startswith("http"):
        return ""
    return f"{url}/compare/{base}...{branch}?expand=1"


def release(store: TicketStore, ticket: dict, config: dict) -> str:
    """Push the ticket's work on its own branch, then finish the ticket.

    Policy (human decision): pushes happen automatically, but never to the base
    branch. The branch name carries the ticket id so a reviewer can trace a push
    back to the ticket, and the report records the branch and compare URL.
    """
    cfg = config.get("release") or {}
    if not cfg.get("enabled"):
        raise TicketError("release is disabled (release.enabled=false in config)")
    mode = cfg.get("mode", "branch")
    if mode != "branch":
        raise TicketError(f"release.mode '{mode}' is not supported yet (only 'branch')")
    proof = (ticket.get("proof") or "").strip()
    if not proof.startswith("[PASS]"):
        raise TicketError(
            f"{ticket['id']} has no passing test evidence — run the test first, or move it by hand"
        )
    path = project_path(ticket["project"])
    remote = cfg.get("remote", "origin")
    code, _ = _git(["remote", "get-url", remote], path)
    if code != 0:
        raise TicketError(f"repository {path} has no remote '{remote}' — cannot push")

    code, status = _git(["status", "--porcelain"], path)
    if code != 0:
        raise TicketError(f"git status failed in {path}")
    if not status.strip():
        store.move(ticket["id"], "done", "release: nothing to push")
        return f"{ticket['id']}: no changes — ticket finished directly"

    branch = f"ticket/{ticket['id'].lower()}-{_slug(ticket['title'])}"
    code, current = _git(["rev-parse", "--abbrev-ref", "HEAD"], path)
    if current != branch:
        code, out = _git(["checkout", "-b", branch], path)
        if code != 0:
            code, out = _git(["checkout", branch], path)
            if code != 0:
                raise TicketError(f"cannot switch to branch {branch}: {out[:200]}")

    _git(["add", "-A"], path)
    message = f"feat({ticket['id'].lower()}): {ticket['title']}"
    code, out = _git(["commit", "-m", message], path)
    if code != 0 and "nothing to commit" not in out:
        raise TicketError(f"commit failed: {out[:200]}")

    code, out = _git(["push", "-u", remote, branch], path)
    if code != 0:
        raise TicketError(f"push failed: {out[:300]}")

    _, remote_url = _git(["remote", "get-url", remote], path)
    base = (config.get("release") or {}).get("baseBranch") or "main"
    link = compare_url(remote_url, base, branch)
    body_path = store.write_pr_body(ticket["id"], branch, remote_url)
    store.set_branch(ticket["id"], branch, pr_url=link)
    store.move(ticket["id"], "done", f"release: pushed {branch} to {remote}")
    suffix = f" · PR: {link}" if link else ""
    if body_path:
        suffix += f" · body: {body_path}"
    return f"{ticket['id']}: pushed branch {branch} to {remote}{suffix}"


# ---------------------------------------------------------------- test stage


def run_test(store: TicketStore, ticket: dict, runner=subprocess.run) -> str:
    """Run the ticket's test command and turn the exit code into a status move."""
    command = (ticket.get("test_cmd") or "").strip()
    if not command:
        store.attach_proof(ticket["id"], False, "ticket has no test_cmd")
        store.move(ticket["id"], "in-progress", "test skipped: test_cmd is empty")
        return f"{ticket['id']}: test_cmd is empty → in-progress"
    cwd = project_path(ticket["project"])
    try:
        proc = runner(
            command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=TEST_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        output = f"{type(exc).__name__}: {exc}"
        store.attach_proof(ticket["id"], False, output)
        store.move(ticket["id"], "in-progress", "test timeout/error")
        return f"{ticket['id']}: test error ({type(exc).__name__}) → in-progress"

    output = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
    tail = "\n".join(output.splitlines()[-40:])
    ok = proc.returncode == 0
    store.attach_proof(ticket["id"], ok, tail or "(no output)")
    if ok:
        store.move(ticket["id"], "review", "test passed")
        return f"{ticket['id']}: test passed (rc=0) → review"
    store.move(ticket["id"], "in-progress", f"test failed (rc={proc.returncode})")
    return f"{ticket['id']}: test failed (rc={proc.returncode}) → in-progress"


def active_tickets(store: TicketStore) -> list[dict]:
    return store.list("in-progress")


def enforce_budget(store: TicketStore, config: dict) -> list[str]:
    """Flag tickets that overran their time budget so they do not hang forever.

    The budget is deliberately coarse: one minute figure in config, not a token meter,
    because the watcher never sees provider cost. Its purpose is to break a stuck loop,
    not to bill usage.
    """
    limit = int((config.get("limits") or {}).get("maxInProgressMinutes", 0) or 0)
    if limit <= 0:
        return []
    notes = []
    for ticket in active_tickets(store):
        stamp = ticket.get("claimed_at") or ticket.get("updated_at")
        if not stamp:
            continue
        try:
            started = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        minutes = (datetime.now(timezone.utc) - started).total_seconds() / 60
        if minutes > limit:
            store.move(
                ticket["id"], "blocked",
                f"time budget: {minutes:.0f} min > {limit} min",
            )
            notes.append(f"{ticket['id']} exceeded its budget ({minutes:.0f} min) → blocked")
    return notes


def next_review(store: TicketStore) -> dict | None:
    rows = [ticket for ticket in store.list("review")]
    return rows[0] if rows else None


def next_testing(store: TicketStore) -> dict | None:
    rows = [ticket for ticket in store.list("testing")]
    return rows[0] if rows else None


def step(store: TicketStore, config: dict, t3_cli: str, dry_run: bool, worker: str = "watcher",
         runner=subprocess.run) -> str:
    """One unit of work: drain `testing` first, then start new work."""
    if not dry_run:
        for note in sync_threads(store, config):
            print(note, flush=True)

    for note in enforce_budget(store, config):
        print(note, flush=True)

    review = next_review(store)
    if review is not None:
        if dry_run:
            return f"[dry-run] {review['id']} · release: push branch and finish the ticket"
        return release(store, review, config)

    testing = next_testing(store)
    if testing is not None:
        if dry_run:
            return f"[dry-run] {testing['id']} · run test: {testing['test_cmd'] or '(empty)'}"
        return run_test(store, testing, runner=runner)
    limit = int((config.get("watcher") or {}).get("maxConcurrent", 1) or 1)
    active = active_tickets(store)
    if len(active) >= limit:
        return f"{len(active)} in-progress tickets (limit {limit}) — new tickets wait"
    return run_once(store, config, t3_cli, dry_run, worker, runner=runner)


def loop(store_factory, config: dict, t3_cli: str, interval: int, worker: str) -> None:
    print(f"watcher started (interval {interval}s)", flush=True)
    while True:
        if not (config.get("watcher") or {}).get("enabled", True):
            print("watcher paused by config (watcher.enabled=false)", flush=True)
            time.sleep(max(60, interval))
            continue
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
    parser.add_argument("--once", action="store_true", help="run one step then exit")
    parser.add_argument("--loop", action="store_true", help="keep running at the configured interval")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--worker", default="watcher")
    parser.add_argument("--interval", type=int, default=0, help="seconds between steps (default from config)")
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
            print("use --once or --loop")
    except TicketError as exc:
        print(f"rejected: {exc}", file=sys.stderr)
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
