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
import hashlib
import json
import os
import re
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
    resolve_profile,
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
    runner = runner or subprocess.run
    notes: list[str] = []
    limit = retry_settings(config)["maxThreadAttempts"]
    for ticket in store.list("in-progress"):
        thread_id = ticket.get("thread_id")
        if not thread_id:
            continue
        anchor = thread_show(config, thread_id, messages=1, runner=runner).get("thread") or {}
        status = str(anchor.get("status") or "")
        latest = str(anchor.get("latestRunId") or "")
        if status in ("completed", "idle"):
            baseline = str(ticket.get("dev_run_id") or "")
            if baseline and (not latest or latest == baseline):
                # Findings were just sent; this is still the run that failed. Wait for the
                # thread to start its next turn instead of testing the same code again.
                notes.append(f"{ticket['id']}: waiting for the dev thread to resume")
                continue
            store.reset_attempts(ticket["id"])
            store.move(ticket["id"], "testing", f"thread finished ({status})")
            notes.append(f"{ticket['id']} → testing (thread {status})")
        elif status in ("failed", "cancelled", "interrupted", "rolled_back"):
            failure = thread_failure(thread_id)
            if failure.get("permanent"):
                detail = failure.get("message") or failure.get("class") or status
                store.move(ticket["id"], "blocked", f"thread {status}: {detail}"[:200])
                notes.append(f"{ticket['id']} → blocked (permanent: {detail[:60]})")
                continue
            reason = f"thread {status}"
            if failure.get("message"):
                reason += f": {failure['message'][:80]}"
            notes.append(retry_or_block(
                store, ticket, reason, counter="dev_attempts", limit=limit, stage="thread",
            ))
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


# ------------------------------------------------------------- retry policy

RETRY_DEFAULTS = {"maxThreadAttempts": 3, "maxTestAttempts": 3}


# Failure classes that a retry cannot fix. Measured from T3's own error item; the message is
# matched as a fallback because the class is generic (`provider_error`) for most of them.
PERMANENT_FAILURES = (
    "usage limit", "usage_limit", "usagelimit", "context length", "input token count",
    "model not found", "no longer available", "does not work with this t3",
)


def thread_failure(thread_id: str, t3_db: Path | None = None) -> dict:
    """The last run's failure item for a thread, from T3's database (read-only).

    `t3 thread show --json` reports *that* a run failed but not why; the cause lives in the
    error turn item (`failure.class/message`, `retry.attempt`). This reads it defensively:
    any read problem returns {} and the caller keeps its safe default.
    """
    db = Path(t3_db or T3_DB)
    if not db.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(
                "select r.status,"
                " json_extract(i.payload_json,'$.failure.class') as failure_class,"
                " json_extract(i.payload_json,'$.failure.message') as message,"
                " json_extract(i.payload_json,'$.retry.attempt') as attempt,"
                " json_extract(i.payload_json,'$.retry.maxAttempts') as max_attempts"
                " from orchestration_v2_projection_runs r"
                " left join orchestration_v2_projection_turn_items i"
                "   on i.run_id = r.run_id and i.type = 'error' and i.status = 'failed'"
                "  and json_extract(i.payload_json,'$.nodeId') = json_extract(r.payload_json,'$.rootNodeId')"
                " where r.thread_id = ? order by r.ordinal desc limit 1",
                (thread_id,),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    if row is None:
        return {}
    message = str(row["message"] or "")
    failure_class = str(row["failure_class"] or "")
    lowered = message.lower()
    permanent = failure_class == "usage_limit" or any(marker in lowered for marker in PERMANENT_FAILURES)
    return {
        "status": str(row["status"] or ""),
        "class": failure_class,
        "message": message[:300],
        "attempt": row["attempt"],
        "max_attempts": row["max_attempts"],
        "permanent": permanent,
    }


def retry_settings(config: dict) -> dict:
    settings = dict(RETRY_DEFAULTS)
    settings.update(config.get("retry") or {})
    return settings


def retry_or_block(store: TicketStore, ticket: dict, reason: str, *, counter: str,
                   limit: int, stage: str) -> str:
    """Give a failed ticket another turn while its budget lasts; then block it.

    Only failures that can plausibly succeed on a retry go through here (a T3 call that
    failed, a thread that died). A rejection from review, a missing model or a missing
    remote must not loop, and they never call this.
    """
    attempts = int(ticket.get(counter) or 0) + 1
    limit = max(1, int(limit))
    if counter == "dev_attempts":
        store.set_attempts(ticket["id"], dev=attempts)
    else:
        store.set_attempts(ticket["id"], test=attempts)
    if attempts >= limit:
        store.move(ticket["id"], "blocked", f"{stage}: {reason} ({attempts}/{limit} attempts)")
        return f"{ticket['id']}: {stage} failed ({attempts}/{limit}) → blocked"
    store.record_event(ticket["id"], "retry", f"{stage}: {reason} · attempt {attempts}/{limit}")
    store.move(ticket["id"], "ready", f"{stage}: {reason}; retry {attempts}/{limit}")
    return f"{ticket['id']}: {stage} failed, retry {attempts}/{limit} → ready"


def send_dev_text(store: TicketStore, ticket: dict, config: dict, text: str,
                  runner=subprocess.run) -> tuple[bool, str]:
    """Send into the ticket's dev thread and remember the run the thread was on.

    The remembered run is the resume anchor: `sync_threads()` refuses to move the ticket to
    `testing` until the thread has actually started a new run, so a failing test cannot be
    re-tested against the code that just failed.
    """
    dev = (ticket.get("thread_id") or "").strip()
    if not dev:
        return False, "no dev thread"
    data = thread_show(config, dev, messages=1, runner=runner)
    baseline = str((data.get("thread") or {}).get("latestRunId") or "")
    cli = (config.get("t3") or {}).get("cli", str(Path.home() / ".local/bin/t3"))
    try:
        proc = runner([cli, "thread", "send", dev, "-", "--json"],
                      input=text, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"send failed: {type(exc).__name__}"
    if proc.returncode != 0:
        return False, f"send failed (rc={proc.returncode}): {(proc.stderr or proc.stdout or '')[:200]}"
    if baseline:
        store.set_dev_run(ticket["id"], baseline)
    store.record_event(ticket["id"], "continued", f"thread {dev}")
    return True, ""


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
        if not dry_run:
            # Head-of-line protection: an unselectable ticket must not be picked forever.
            store.move(ticket["id"], "blocked", f"model selection: {str(exc)[:180]}")
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
            raise TicketError(retry_or_block(
                store, ticket, f"thread send {type(exc).__name__}", counter="dev_attempts",
                limit=retry_settings(config)["maxThreadAttempts"], stage="continue",
            )) from exc
        if proc.returncode != 0:
            raise TicketError(retry_or_block(
                store, ticket, f"thread send rc={proc.returncode}", counter="dev_attempts",
                limit=retry_settings(config)["maxThreadAttempts"], stage="continue",
            ))
        store.reset_attempts(ticket["id"])
        store.record_event(ticket["id"], "continued", f"thread {ticket['thread_id']}")
        return f"{ticket['id']} → continuing thread {ticket['thread_id']} (profile {profile['name']})"

    command = [t3_cli, "thread", "start", project_path(ticket["project"]), "-",
               "--title", ticket["title"][:70],
               "--provider", profile["provider"], "--model", profile["model"], "--json"]
    try:
        proc = runner(command, input=prompt, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TicketError(retry_or_block(
            store, ticket, f"t3 {type(exc).__name__}", counter="dev_attempts",
            limit=retry_settings(config)["maxThreadAttempts"], stage="start",
        )) from exc
    if proc.returncode != 0:
        raise TicketError(retry_or_block(
            store, ticket, f"t3 rc={proc.returncode}", counter="dev_attempts",
            limit=retry_settings(config)["maxThreadAttempts"], stage="start",
        ))
    try:
        thread_id = json.loads(proc.stdout)["threadId"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise TicketError(retry_or_block(
            store, ticket, "t3 output has no threadId", counter="dev_attempts",
            limit=retry_settings(config)["maxThreadAttempts"], stage="start",
        )) from exc
    store.reset_attempts(ticket["id"])
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


# -------------------------------------------------------- verification stage

VERIFICATION_DEFAULTS = {
    "enabled": True,
    "profile": "",
    "maxAttempts": 2,
    "timeoutMinutes": 20,
    "maxDiffBytes": 60000,
}
VERDICT_RE = re.compile(r"^VERDICT:\s*(ACCEPT|REJECT)$", re.IGNORECASE)
VERDICT_TOKEN = re.compile(r"^[*_`\s]*VERDICT:\s*(ACCEPT|REJECT)[*_`\s]*[.!]?$", re.IGNORECASE)
LIST_MARKER = re.compile(r"^(?:[-*+]>?\s+|\d+[.)]\s+)")


def verification_settings(config: dict) -> dict:
    settings = dict(VERIFICATION_DEFAULTS)
    settings.update(config.get("verification") or {})
    return settings


def project_fingerprint(path: str) -> str:
    """A cheap identity of what the reviewer is looking at: HEAD plus the dirty tree.

    Verification runs in the project folder, so the reviewer can see exactly the work the
    release stage will commit. The price is that it could also change that work; comparing
    this fingerprint before and after is how the verdict stays evidence.
    """
    code, head = _git(["rev-parse", "HEAD"], path)
    code_status, status = _git(["status", "--porcelain=v1", "-uall"], path)
    payload = f"{head if code == 0 else ''}\n{status if code_status == 0 else ''}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _capped(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… truncated ({len(text) - limit} more characters)"


def build_verifier_prompt(ticket: dict, status: str, diff: str, max_diff: int) -> str:
    """Everything the reviewer needs, and nothing a model had to summarise first."""
    parts = [
        f"You are reviewing another agent's work on ticket {ticket['id']}. You are the reviewer:",
        "do not edit files and do not run commands that write. Judge the change, not the author.",
        "",
        f"Title: {ticket['title']}",
        "",
        "Ticket context:",
        ticket["body"] or "(none given)",
    ]
    if ticket["test_cmd"]:
        parts += ["", f"Test command: {ticket['test_cmd']}"]
    if ticket.get("proof"):
        parts += ["", "Recorded test result, produced by the host after the agent finished:", ticket["proof"]]
    else:
        parts += ["", "Recorded test result: none"]
    parts += [
        "",
        "Working tree, `git status --porcelain=v1 -uall`:",
        status.strip() or "(clean)",
        "",
        "Diff against HEAD, `git diff HEAD`:",
        _capped(diff, int(max_diff)) or "(empty)",
        "",
        "Decide whether this change satisfies the ticket and its recorded test evidence.",
        "Give concise findings with file:line when something is wrong.",
        "End your reply with exactly one line, nothing after it:",
        "VERDICT: ACCEPT",
        "or",
        "VERDICT: REJECT",
    ]
    return "\n".join(parts).strip()


def parse_verdict(text: str) -> tuple[str, str]:
    """The verdict is the last line that *is* the token; everything else is findings.

    Models decorate: `**VERDICT: ACCEPT**`, a list marker, a trailing period, or a house-style
    line appended after the verdict (measured on real threads: 17 % emphasis, 3 % a trailing
    `Skills:` line). Stripping those is not the same as guessing: a line still has to consist
    of the token and nothing else, and two contradicting verdict lines are refused.
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    tokens: list[tuple[int, str]] = []
    for position, line in enumerate(lines):
        bare = LIST_MARKER.sub("", line).strip()
        match = VERDICT_TOKEN.match(bare)
        if match:
            tokens.append((position, match.group(1).upper()))
    if not tokens:
        return "", ""
    if len({verdict for _, verdict in tokens}) > 1:
        return "", ""  # contradictory: refuse rather than pick one
    position, verdict = tokens[-1]
    # Findings are what the reviewer wrote before the token; a house-style line after it
    # (`Skills: …`) is not part of the verdict.
    findings = "\n".join(lines[:position]).strip()
    return verdict, findings


def thread_show(config: dict, thread_id: str, messages: int = 5, runner=subprocess.run) -> dict:
    runner = runner or subprocess.run
    cli = (config.get("t3") or {}).get("cli", str(Path.home() / ".local/bin/t3"))
    try:
        proc = runner(
            [cli, "thread", "show", thread_id, "--json", "--messages", str(messages)],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}


def verifier_reply(data: dict, run_id: str = "") -> str:
    """The last assistant message of the verified run, not merely the last message.

    A thread can hold several runs, and one run several assistant messages. `thread show`
    gives each message its `role` and `runId`, so the reply that counts is chosen by run,
    never by position alone.
    """
    items = data.get("messages") or data.get("items") or []
    candidates = [
        item for item in items
        if (item.get("role") or "").lower() == "assistant" and (item.get("text") or "").strip()
    ]
    if run_id:
        matched = [item for item in candidates if str(item.get("runId") or "") == run_id]
        if matched:
            candidates = matched
        else:
            candidates = []  # no message of the verified run: nothing to judge
    for item in reversed(candidates):
        return (item.get("text") or "").strip()
    return ""


def _verify_timed_out(ticket: dict, minutes: int) -> bool:
    stamp = ticket.get("verify_started_at") or ""
    if not stamp:
        return False
    try:
        started = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - started).total_seconds() > max(1, int(minutes)) * 60


def start_verification(store: TicketStore, ticket: dict, config: dict, t3_cli: str, settings: dict,
                       runner=subprocess.run) -> str:
    # The owner's default is a reviewer that is a fresh context but the same harness and
    # model; `verification.profile` can name another preset when that changes.
    if settings.get("profile"):
        selection = resolve_profile(config, settings["profile"])
    elif ticket.get("provider") and ticket.get("model"):
        try:
            selection = resolve_ticket(ticket, config)  # validate an explicit pair
        except CatalogError as exc:
            raise TicketError(str(exc)) from exc
    else:
        selection = resolve_profile(config, ticket.get("model_profile"))
    path = project_path(ticket["project"])
    _, status = _git(["status", "--porcelain=v1", "-uall"], path)
    _, diff = _git(["diff", "HEAD"], path)
    prompt = build_verifier_prompt(ticket, status, diff, settings["maxDiffBytes"])
    baseline = project_fingerprint(path)
    command = [
        t3_cli, "thread", "start", path, "-",
        "--title", f"Review {ticket['id']}: {ticket['title'][:50]}",
        "--provider", selection["provider"], "--model", selection["model"], "--json",
    ]
    try:
        proc = runner(command, input=prompt, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        store.move(ticket["id"], "blocked", f"review start failed: {type(exc).__name__}")
        raise TicketError(f"failed to start the reviewer: {exc}") from exc
    if proc.returncode != 0:
        store.move(ticket["id"], "blocked", f"review start rc={proc.returncode}")
        raise TicketError(
            f"reviewer start failed (rc={proc.returncode}): {(proc.stderr or proc.stdout or '')[:300]}"
        )
    try:
        payload = json.loads(proc.stdout)
        thread_id = payload["threadId"]
    except (json.JSONDecodeError, KeyError) as exc:
        store.move(ticket["id"], "blocked", "review start output is not JSON")
        raise TicketError(f"reviewer output has no threadId: {(proc.stdout or '')[:200]}") from exc
    updated = store.attach_verifier(
        ticket["id"], thread_id, str(payload.get("runId") or ""),
        selection["provider"], selection["model"], baseline,
    )
    return (
        f"{ticket['id']}: review started (attempt {updated['verify_attempt']}, "
        f"{selection['provider']}/{selection['model']})"
    )


def verification_step(store: TicketStore, ticket: dict, config: dict, t3_cli: str,
                      runner=subprocess.run) -> str | None:
    """Advance review. Returns a message when it handled the ticket, None when release may run."""
    settings = verification_settings(config)
    if not settings["enabled"]:
        return None
    ticket_id = ticket["id"]
    thread_id = (ticket.get("verify_thread_id") or "").strip()
    attempt = int(ticket.get("verify_attempt") or 0)
    max_attempts = max(1, int(settings["maxAttempts"]))

    if not thread_id:
        if attempt >= max_attempts:
            store.move(ticket_id, "blocked", f"review: {attempt} attempts used, no verdict")
            return f"{ticket_id}: review attempts exhausted → blocked"
        return start_verification(store, ticket, config, t3_cli, settings, runner=runner)

    if _verify_timed_out(ticket, int(settings["timeoutMinutes"])):
        store.record_verdict(ticket_id, "TIMEOUT", f"no verdict within {settings['timeoutMinutes']} minutes")
        store.move(ticket_id, "blocked", "review: reviewer timed out")
        return f"{ticket_id}: review timed out → blocked"

    # One `thread show` answers status, run anchors and text, so the three cannot disagree.
    data = thread_show(config, thread_id, messages=10, runner=runner)
    anchor = data.get("thread") or {}
    status = str(anchor.get("status") or "")
    if not status:
        return f"{ticket_id}: waiting for the reviewer (thread not readable yet)"
    if status in ("preparing", "queued", "starting", "running"):
        return f"{ticket_id}: waiting for the reviewer ({status})"
    if status == "waiting":
        store.record_verdict(ticket_id, "FAILED", "the reviewer asked a question and needs an answer in T3")
        store.move(ticket_id, "blocked", "review: reviewer is waiting for an answer")
        return f"{ticket_id}: reviewer needs input → blocked"
    if status in ("failed", "cancelled", "interrupted", "rolled_back"):
        store.record_verdict(ticket_id, "FAILED", f"review thread status: {status}")
        store.move(ticket_id, "blocked", f"review: reviewer {status}")
        return f"{ticket_id}: reviewer {status} → blocked"
    if status not in ("completed", "idle"):
        # A status this code has never seen fails closed with its own note, instead of
        # falling through to "no verdict line".
        store.record_verdict(ticket_id, "FAILED", f"unexpected review thread status: {status}")
        store.move(ticket_id, "blocked", f"review: unexpected reviewer status {status}")
        return f"{ticket_id}: unexpected reviewer status {status} → blocked"

    run_id = str(ticket.get("verify_run_id") or "")
    latest_run = str(anchor.get("latestRunId") or "")
    run_count = anchor.get("runCount")
    if run_id and latest_run and latest_run != run_id:
        store.record_verdict(
            ticket_id, "FAILED", f"the verdict belongs to run {run_id}, not {latest_run}"
        )
        store.move(ticket_id, "blocked", "review: reviewer moved past the verified run")
        return f"{ticket_id}: stale review run → blocked"
    if not run_id and latest_run:
        store.attach_verify_run(ticket_id, latest_run)  # the launch reported no run id
        run_id = latest_run
    if isinstance(run_count, int) and run_count > 1:
        store.record_verdict(
            ticket_id, "FAILED", f"the reviewer thread ran {run_count} times; the review is not fresh"
        )
        store.move(ticket_id, "blocked", "review: reviewer had more than one run")
        return f"{ticket_id}: reviewer had {run_count} runs → blocked"

    path = project_path(ticket["project"])
    if ticket.get("verify_baseline") and project_fingerprint(path) != ticket["verify_baseline"]:
        store.record_verdict(ticket_id, "FAILED", "the project changed while the reviewer was reading it")
        store.move(ticket_id, "blocked", "review: project changed during verification")
        return f"{ticket_id}: project changed during review → blocked"

    verdict, findings = parse_verdict(verifier_reply(data, run_id))
    if verdict == "ACCEPT":
        store.record_verdict(ticket_id, "ACCEPT", findings)
        return None  # release may proceed; it is still the only path to `done`
    if verdict == "REJECT":
        store.record_verdict(ticket_id, "REJECT", findings or "no findings given")
        dev_thread = (ticket.get("thread_id") or "").strip()
        if attempt < max_attempts and dev_thread:
            message = (
                f"Your work on {ticket_id} was rejected by an independent review "
                f"(attempt {attempt}/{max_attempts}).\n\n{findings or '(no findings given)'}\n\n"
                "Address the findings in this thread."
            )
            sent, note = send_dev_text(store, ticket, config, message, runner=runner)
            if not sent:
                store.move(ticket_id, "blocked", f"review rejected; findings could not be sent: {note}")
                return f"{ticket_id}: findings could not be sent → blocked"
            store.clear_verifier(ticket_id)
            store.move(
                ticket_id, "in-progress",
                f"review rejected (attempt {attempt}/{max_attempts}): {(findings or 'no findings')[:160]}",
            )
            return f"{ticket_id}: rejected → in-progress with findings"
        store.move(
            ticket_id, "blocked",
            f"review rejected ({attempt}/{max_attempts}): {(findings or 'no findings')[:160]}",
        )
        return f"{ticket_id}: rejected → blocked"
    store.record_verdict(ticket_id, "FAILED", "the reviewer did not end with a VERDICT line")
    store.move(ticket_id, "blocked", "review: no verdict line in the reply")
    return f"{ticket_id}: no verdict → blocked"


# ---------------------------------------------------------------- test stage


def run_test(store: TicketStore, ticket: dict, config: dict | None = None,
             runner=subprocess.run) -> str:
    """Run the ticket's test command and turn the exit code into a status move.

    A failing test is not a dead end: the tail goes back into the dev thread and the ticket
    returns to `in-progress`, bounded by `retry.maxTestAttempts`. An empty test command is a
    configuration error, not something a retry can fix, so it blocks immediately.
    """
    config = config or {}
    settings = retry_settings(config)
    limit = max(1, int(settings["maxTestAttempts"]))
    command = (ticket.get("test_cmd") or "").strip()
    if not command:
        store.attach_proof(ticket["id"], False, "ticket has no test_cmd")
        store.move(ticket["id"], "blocked", "test: test_cmd is empty, a retry cannot fix that")
        return f"{ticket['id']}: test_cmd is empty → blocked"

    def fail(reason: str, output: str) -> str:
        attempts = int(ticket.get("test_attempts") or 0) + 1
        store.set_attempts(ticket["id"], test=attempts)
        if attempts >= limit:
            store.move(ticket["id"], "blocked", f"{reason} ({attempts}/{limit} attempts); see proof")
            return f"{ticket['id']}: {reason} ({attempts}/{limit}) → blocked"
        message = (
            f"Your work on {ticket['id']} failed its test ({reason}, attempt {attempts}/{limit}).\n\n"
            f"{output[:3000] or '(no output)'}\n\n"
            "Fix the cause in this thread; the test runs again when it finishes."
        )
        sent, note = send_dev_text(store, ticket, config, message, runner=runner)
        if not sent:
            store.move(ticket["id"], "blocked", f"{reason}; findings could not be sent: {note}")
            return f"{ticket['id']}: {reason}, could not reach the dev thread → blocked"
        store.move(ticket["id"], "in-progress", f"{reason}; findings sent, retry {attempts}/{limit}")
        return f"{ticket['id']}: {reason}, findings sent → in-progress ({attempts}/{limit})"

    cwd = project_path(ticket["project"])
    try:
        proc = runner(
            command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=TEST_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        output = f"{type(exc).__name__}: {exc}"
        store.attach_proof(ticket["id"], False, output)
        return fail(f"test {type(exc).__name__}", output)

    output = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
    tail = "\n".join(output.splitlines()[-40:])
    ok = proc.returncode == 0
    store.attach_proof(ticket["id"], ok, tail or "(no output)")
    if ok:
        store.reset_attempts(ticket["id"])
        store.move(ticket["id"], "review", "test passed")
        return f"{ticket['id']}: test passed (rc=0) → review"
    return fail(f"test failed (rc={proc.returncode})", tail)


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
        settings = verification_settings(config)
        if dry_run:
            if settings["enabled"] and not (review.get("verify_verdict") or ""):
                return (
                    f"[dry-run] {review['id']} · start a review thread "
                    f"(attempt {int(review.get('verify_attempt') or 0) + 1}/{settings['maxAttempts']}), "
                    "then release on ACCEPT"
                )
            return f"[dry-run] {review['id']} · release: push branch and finish the ticket"
        handled = verification_step(store, review, config, t3_cli, runner=runner)
        if handled is not None:
            return handled
        try:
            return release(store, review, config)
        except TicketError as exc:
            # A release that can never succeed (no remote, unsupported mode, failed push)
            # must not be retried by the queue forever.
            if store.get(review["id"])["status"] == "review":
                store.move(review["id"], "blocked", f"release: {str(exc)[:180]}")
            raise

    testing = next_testing(store)
    if testing is not None:
        if dry_run:
            return f"[dry-run] {testing['id']} · run test: {testing['test_cmd'] or '(empty)'}"
        try:
            return run_test(store, testing, config, runner=runner)
        except TicketError as exc:
            if store.get(testing["id"])["status"] == "testing":
                store.move(testing["id"], "blocked", f"test: {str(exc)[:180]}")
            raise
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
