#!/usr/bin/env python3
"""t3-bridge — Telegram ⇄ T3 Code bridge (phase 1).

Reads commands from an allowlisted Telegram chat and drives the local T3 CLI:

    /help
    /id                     echo this chat id (for the allowlist)
    /projects               list T3 projects
    /threads <project>      list a project's threads (newest first)
    /ctx <thread> [n]       last n messages of a thread
    /send <thread> <text>   send a message; a busy thread queues it as the next turn
    /new <project> <text>   start a thread in a project

Design constraints, all measured rather than assumed:
  - `t3 thread send` queues to a busy thread and never interrupts it (t3 --help).
  - T3 has no CLI for answering pending approval requests, so this bridge only
    notifies; approvals stay in the T3 UI.
  - Telegram is untrusted input: every chat id must be in the allowlist.
  - Long polling only (no webhook), so no public endpoint is needed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
if str(WORKFLOW_ROOT / "tickets") not in sys.path:
    sys.path.insert(0, str(WORKFLOW_ROOT / "tickets"))

try:  # pragma: no cover - only differs when the repo moves
    from store import TicketError as StoreError
    from store import TicketStore, load_config as load_workflow_config
except Exception:  # noqa: BLE001
    StoreError = Exception  # type: ignore[assignment]
    TicketStore = None  # type: ignore[assignment]
    load_workflow_config = None  # type: ignore[assignment]

CONFIG_PATH = Path(os.environ.get("T3_BRIDGE_CONFIG", HOME / ".config/t3-bridge/config.json"))
STATE_DIR = HOME / ".local/state/t3-bridge"
STATE_PATH = STATE_DIR / "state.json"
LOG_PATH = STATE_DIR / "bridge.log"
AUDIT_PATH = STATE_DIR / "audit.jsonl"

THREAD_ID = re.compile(
    r"^(?:mcp:)?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
MAX_REPLY = 3500

DEFAULTS = {
    "telegram": {"botToken": "", "allowChatIds": [], "apiBase": "https://api.telegram.org"},
    "t3": {
        "cli": str(HOME / ".local/bin/t3"),
        "dbPath": str(HOME / ".t3/userdata/statev2.sqlite"),
        "timeoutSeconds": 180,
    },
    "bridge": {"dryRun": False, "allowWrites": True, "pollTimeout": 25, "quiet": False},
    "workflow": {
        "dbPath": str(HOME / ".local/state/t3-workflow/tickets.sqlite"),
        "configPath": str(HOME / ".config/t3-workflow/config.json"),
    },
}


def log(message: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


def audit(entry: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"at": datetime.now(timezone.utc).isoformat(), **entry}
    with AUDIT_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_config() -> dict:
    config = json.loads(json.dumps(DEFAULTS))
    if CONFIG_PATH.exists():
        stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for section, values in stored.items():
            if isinstance(values, dict):
                config.setdefault(section, {}).update(values)
            else:
                config[section] = values
    if not config["telegram"]["botToken"]:
        fallback = HOME / ".config/t3-wa-notify/config.json"
        if fallback.exists():
            other = json.loads(fallback.read_text(encoding="utf-8")).get("telegram") or {}
            config["telegram"]["botToken"] = other.get("botToken", "")
            if not config["telegram"]["allowChatIds"] and other.get("chatId"):
                config["telegram"]["allowChatIds"] = [str(other["chatId"])]
    return config


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("state rusak, mulai baru")
    return {"offset": None, "seen": 0}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------- telegram


def telegram(config: dict, method: str, payload: dict, timeout: int = 40):
    token = config["telegram"]["botToken"]
    if not token:
        return False, "botToken is empty"
    url = f"{config['telegram']['apiBase']}/bot{token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        return False, f"http-{exc.code}: {detail}"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if not body.get("ok"):
        return False, str(body.get("description") or "not ok")[:200]
    return True, body.get("result")


def reply(config: dict, chat_id: str, text: str) -> None:
    text = text if len(text) <= MAX_REPLY else text[: MAX_REPLY - 20] + "\n… (truncated)"
    ok, detail = telegram(config, "sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": True})
    if not ok:
        log(f"failed to send reply: {detail}")


# ---------------------------------------------------------------- t3 access


def multipart_body(fields: dict, file_field: str, filename: str, content: bytes,
                   content_type: str = "text/markdown") -> tuple[bytes, str]:
    """A Telegram multipart upload, built with the standard library.

    Kept as a pure function so the bytes can be checked without a network call.
    """
    boundary = f"----t3workflow{uuid.uuid4().hex}"
    parts = b""
    for name, value in fields.items():
        parts += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()
    parts += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
        f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'
    ).encode()
    parts += content + f"\r\n--{boundary}--\r\n".encode()
    return parts, f"multipart/form-data; boundary={boundary}"


def send_document(config: dict, chat_id: str, path: Path, caption: str = "") -> bool:
    """Upload one file to the chat. Used to deliver the ticket report as evidence."""
    token = config["telegram"]["botToken"]
    if not token:
        log("report not sent: botToken is empty")
        return False
    try:
        content = Path(path).read_bytes()
    except OSError as exc:
        log(f"report not sent, cannot read {path}: {exc}")
        return False
    fields = {"chat_id": chat_id}
    if caption:
        fields["caption"] = caption[:1000]
    body, content_type = multipart_body(fields, "document", Path(path).name, content)
    request = urllib.request.Request(
        f"{config['telegram']['apiBase']}/bot{token}/sendDocument",
        data=body, headers={"Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        log(f"report not sent: {type(exc).__name__}: {exc}")
        return False
    if not payload.get("ok"):
        log(f"report not sent: {payload.get('description')}")
        return False
    audit({"action": "report", "file": Path(path).name, "chatId": str(chat_id)})
    log(f"report delivered: {Path(path).name}")
    return True


def t3_run(config: dict, args: list[str], stdin: str | None = None) -> tuple[int, str]:
    command = [config["t3"]["cli"], *args]
    try:
        proc = subprocess.run(
            command,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=float(config["t3"].get("timeoutSeconds", 180)),
        )
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    output = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    return proc.returncode, output


def ticket_store(config: dict):
    if TicketStore is None or load_workflow_config is None:
        raise RuntimeError("the tickets module is not available next to the bridge")
    workflow = config.get("workflow") or {}
    db = Path(workflow.get("dbPath", HOME / ".local/state/t3-workflow/tickets.sqlite")).expanduser()
    cfg = Path(workflow.get("configPath", HOME / ".config/t3-workflow/config.json")).expanduser()
    return TicketStore(db, cfg)


def projects(config: dict) -> list[dict]:
    path = Path(config["t3"]["dbPath"])
    if not path.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "select project_id, title, workspace_root from projection_projects "
                "where deleted_at is null order by lower(title)"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        log(f"reading projects failed: {exc}")
        return []
    return [dict(row) for row in rows]


def resolve_project(config: dict, needle: str) -> tuple[dict | None, str]:
    needle = needle.strip()
    if not needle:
        return None, "project name is empty"
    items = projects(config)
    lowered = needle.lower()
    exact = [p for p in items if (p["title"] or "").lower() == lowered]
    if len(exact) == 1:
        return exact[0], ""
    by_id = [p for p in items if str(p["project_id"]).lower().startswith(lowered)]
    if len(by_id) == 1:
        return by_id[0], ""
    partial = [p for p in items if lowered in (p["title"] or "").lower()]
    if len(partial) == 1:
        return partial[0], ""
    if not partial:
        return None, f"project '{needle}' not found. Use /projects."
    names = ", ".join(sorted(p["title"] for p in partial)[:6])
    return None, f"'{needle}' is ambiguous: {names}"


def thread_rows(config: dict, project: dict) -> list[dict]:
    code, out = t3_run(config, ["thread", "list", "--project", project["workspace_root"], "--json"])
    if code != 0:
        raise RuntimeError(f"t3 thread list failed (rc={code}): {out[:200]}")
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"output is not JSON: {exc}") from exc


def thread_show(config: dict, thread_id: str, messages: int) -> dict:
    code, out = t3_run(config, ["thread", "show", thread_id, "--json", "--messages", str(messages)])
    if code != 0:
        raise RuntimeError(f"t3 thread show failed (rc={code}): {out[:200]}")
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"output is not JSON: {exc}") from exc


# ---------------------------------------------------------------- commands


def cmd_help() -> str:
    return (
        "Tickets:\n"
        "/tickets [status] — list tickets\n"
        "/ticket <project> <text> — new ticket (backlog)\n"
        "/pending — approvals waiting\n"
        "/approve <id> [note] — approve\n"
        "/reject <id> [note] — reject\n"
        "\nCommands:\n"
        "/projects — list projects\n"
        "/threads <project> — latest threads\n"
        "/ctx <thread> [n] — most recent messages\n"
        "/send <thread> <text> — send to a thread (busy thread: next turn queue)\n"
        "/new <project> <text> — start a new thread\n"
        "/id — this chat id\n"
        "\nApprovals are still answered in the T3 UI, not from here."
    )


def cmd_tickets(config: dict, status: str = "") -> str:
    store = ticket_store(config)
    try:
        rows = store.list(status or None)
    finally:
        store.close()
    if not rows:
        return f"no tickets{' with status ' + status if status else ''}"
    lines = [f"{t['id']} [{t['status']}] p{t['priority']} {t['title'][:50]}" for t in rows[:20]]
    return f"Tickets ({len(rows)}):\n" + "\n".join(lines)


def cmd_ticket_new(config: dict, project: str, text: str) -> str:
    store = ticket_store(config)
    try:
        workflow = load_workflow_config(Path((config.get("workflow") or {}).get(
            "configPath", HOME / ".config/t3-workflow/config.json")))
        profile = (workflow.get("defaults") or {}).get("profile", "")
        ticket = store.add(text, project, model_profile=profile, status="backlog")
    finally:
        store.close()
    audit({"action": "ticket-add", "ticket": ticket["id"], "project": project})
    return f"{ticket['id']} created in {project} [{ticket['status']}] · profile {profile or '(default)'}"


def cmd_pending(config: dict) -> str:
    store = ticket_store(config)
    try:
        rows = store.pending_approvals()
    finally:
        store.close()
    if not rows:
        return "no approvals are waiting"
    return "Waiting for approval:\n" + "\n".join(
        f"#{row['approval_id']} {row['ticket_id']}: {row['question'][:60]}" for row in rows
    )


def cmd_decide(config: dict, raw_id: str, approved: bool, note: str, chat_id: str) -> str:
    try:
        approval_id = int(raw_id.lstrip("#"))
    except ValueError:
        return "approval id must be a number"
    store = ticket_store(config)
    try:
        approval = store.decide_approval(approval_id, approved, f"telegram:{chat_id}", note)
    finally:
        store.close()
    audit({"action": "approve" if approved else "reject", "approval": approval_id})
    return f"#{approval_id} {'approved' if approved else 'rejected'} ({approval['ticket_id']})"


def cmd_projects(config: dict) -> str:
    items = projects(config)
    if not items:
        return "No project could be read from the T3 database."
    lines = [f"{index + 1}. {p['title']}  ({str(p['project_id'])[:8]})" for index, p in enumerate(items)]
    return "Projects:\n" + "\n".join(lines)


def cmd_threads(config: dict, needle: str) -> str:
    project, error = resolve_project(config, needle)
    if not project:
        return error
    rows = thread_rows(config, project)
    if not rows:
        return f"{project['title']}: no threads yet."
    lines = []
    for row in rows[:15]:
        status = row.get("status") or "-"
        title = (row.get("title") or "(untitled)")[:60]
        lines.append(f"• {title}\n  {row['threadId']}  [{status}]")
    return f"Threads in {project['title']} ({len(rows)}):\n" + "\n".join(lines)


def resolve_thread(config: dict, needle: str) -> tuple[str | None, str]:
    """A UUID directly, or a project name that has exactly one thread."""
    needle = needle.strip()
    if THREAD_ID.match(needle):
        return needle, ""
    project, error = resolve_project(config, needle)
    if not project:
        return None, (
            f"{error}\n\nSend a thread id (UUID), or a project name that has exactly one thread. "
            "See the ids with /threads <project>."
        )
    rows = thread_rows(config, project)
    if not rows:
        return None, f"{project['title']} has no threads yet."
    if len(rows) > 1:
        lines = "\n".join(f"• {row['threadId']}  [{(row.get('title') or '')[:40]}]" for row in rows[:8])
        return None, f"{project['title']} has {len(rows)} threads — name one:\n{lines}"
    return rows[0]["threadId"], ""


def cmd_ctx(config: dict, thread_id: str, count: int = 3) -> str:
    resolved, error = resolve_thread(config, thread_id)
    if not resolved:
        return error
    thread_id = resolved
    data = thread_show(config, thread_id, count)
    thread = data.get("thread") or {}
    # `t3 thread show --json` returns {"thread": {...}, "messages": [...]}
    items = data.get("messages") or data.get("items") or []
    head = f"{thread.get('title') or '(untitled)'}\nstatus: {thread.get('status')}  model: {thread.get('model')}\n{thread.get('link') or ''}"
    body = []
    for item in items[-count:]:
        role = item.get("role") or item.get("createdBy") or item.get("type") or "?"
        text = (item.get("text") or "").strip().replace("\n", " ")
        if text:
            body.append(f"[{role}] {text[:300]}")
    return head + ("\n\n" + "\n\n".join(body) if body else "\n\n(no message could be read)")


def cmd_send(config: dict, thread_id: str, text: str) -> str:
    resolved, error = resolve_thread(config, thread_id)
    if not resolved:
        return error
    thread_id = resolved
    if not text.strip():
        return "The text is empty."
    if not config["bridge"].get("allowWrites", True):
        return "Writes are disabled (bridge.allowWrites=false)."
    if config["bridge"].get("dryRun"):
        return f"[dry-run] t3 thread send {thread_id} << {text[:120]}"
    code, out = t3_run(config, ["thread", "send", thread_id, "-", "--json"], stdin=text)
    audit({"action": "send", "threadId": thread_id, "rc": code, "chars": len(text)})
    if code != 0:
        return f"Send failed (rc={code}): {out[:300]}"
    return "Sent. A busy thread takes it as its next turn."


def cmd_new(config: dict, needle: str, text: str) -> str:
    project, error = resolve_project(config, needle)
    if not project:
        return error
    if not text.strip():
        return "The text is empty."
    if config["bridge"].get("dryRun"):
        return f"[dry-run] t3 thread start {project['title']} << {text[:120]}"
    title = text.strip().splitlines()[0][:70]
    code, out = t3_run(
        config,
        ["thread", "start", project["workspace_root"], "-", "--title", title, "--json"],
        stdin=text,
    )
    audit({"action": "new", "project": project["title"], "rc": code})
    if code != 0:
        return f"Failed to create the thread (rc={code}): {out[:300]}"
    try:
        data = json.loads(out)
        return f"Thread created: {data.get('threadId')}\n{data.get('link') or ''}"
    except json.JSONDecodeError:
        return f"Thread created.\n{out[:300]}"


def handle(config: dict, chat_id: str, text: str) -> str | None:
    """Return a reply for an allowlisted chat, or None when the chat is unknown."""
    if str(chat_id) not in {str(item) for item in config["telegram"]["allowChatIds"]}:
        log(f"rejected unknown chat: {chat_id}")
        audit({"action": "reject", "chatId": str(chat_id)})
        return None
    text = (text or "").strip()
    if not text:
        return None
    audit({"action": "command", "chatId": str(chat_id), "text": text[:200]})
    if text.startswith("/"):
        head, _, rest = text.partition(" ")
        command = head[1:].split("@")[0].lower()
        rest = rest.strip()
    else:
        command, rest = "help", ""

    try:
        if command in ("help", "start"):
            return cmd_help()
        if command == "id":
            return f"chat id: {chat_id}"
        if command == "tickets":
            return cmd_tickets(config, rest.strip())
        if command == "ticket":
            parts = rest.split(maxsplit=1)
            if len(parts) < 2:
                return "Usage: /ticket <project> <text>"
            return cmd_ticket_new(config, parts[0], parts[1])
        if command == "pending":
            return cmd_pending(config)
        if command in ("approve", "reject"):
            parts = rest.split(maxsplit=1)
            if not parts:
                return f"Usage: /{command} <id> [note]"
            note = parts[1] if len(parts) > 1 else ""
            return cmd_decide(config, parts[0], command == "approve", note, str(chat_id))
        if command == "projects":
            return cmd_projects(config)
        if command == "threads":
            return cmd_threads(config, rest) if rest else "Usage: /threads <project>"
        if command == "ctx":
            parts = rest.split(maxsplit=1)
            if not parts:
                return "Usage: /ctx <thread-id> [count]"
            count = 3
            if len(parts) > 1 and parts[1].strip().isdigit():
                count = max(1, min(10, int(parts[1].strip())))
            return cmd_ctx(config, parts[0], count)
        if command == "send":
            parts = rest.split(maxsplit=1)
            if len(parts) < 2:
                return "Usage: /send <thread-id> <text>"
            return cmd_send(config, parts[0], parts[1])
        if command == "new":
            parts = rest.split(maxsplit=1)
            if len(parts) < 2:
                return "Usage: /new <project> <text>"
            return cmd_new(config, parts[0], parts[1])
        return f"Unknown command /{command}.\n\n" + cmd_help()
    except StoreError as exc:
        log(f"rejected /{command}: {exc}")
        return f"Rejected: {str(exc)[:300]}"
    except RuntimeError as exc:
        log(f"command /{command} failed: {exc}")
        return f"Error: {str(exc)[:300]}"
    except Exception as exc:  # one command must not kill the bridge
        log(f"unexpected error in /{command}: {type(exc).__name__}: {exc}")
        return f"Unexpected error: {type(exc).__name__}: {str(exc)[:200]}"


# ---------------------------------------------------------------- loop


class Stop(Exception):
    pass


def _stop(signum, frame):  # noqa: ARG001
    raise Stop()


def notify_approvals(config: dict, state: dict) -> int:
    """Send one message for every approval not yet announced."""
    if TicketStore is None:
        return 0
    chats = config["telegram"]["allowChatIds"]
    if not chats:
        return 0
    store = ticket_store(config)
    try:
        pending = store.pending_approvals()
    finally:
        store.close()
    notified = set(state.get("notifiedApprovals") or [])
    sent = 0
    for approval in pending:
        key = str(approval["approval_id"])
        if key in notified:
            continue
        reply(
            config,
            str(chats[0]),
            f"Approval #{key} · {approval['ticket_id']}\n{approval['question']}\n\n"
            f"Reply: /approve {key} or /reject {key}",
        )
        notified.add(key)
        sent += 1
    state["notifiedApprovals"] = sorted(notified)[-200:]
    save_state(state)
    return sent


NOTIFY_STATUSES = ("in-progress", "ready", "review", "done", "blocked")
STATUS_ICON = {"in-progress": "▶", "ready": "↻", "review": "🔍", "done": "✅", "blocked": "⛔"}


def notify_ticket_states(config: dict, state: dict) -> int:
    """Acknowledge work, report the review verdict, and deliver the report.

    `in-progress` is the acknowledgement that a new ticket was taken; `ready` right after
    `in-progress` is a bounded retry, not a fresh ticket. A `done` ticket sends its report
    as a document, so the evidence arrives with the news instead of behind a path.
    """
    if TicketStore is None:
        return 0
    chats = config["telegram"]["allowChatIds"]
    if not chats:
        return 0
    store = ticket_store(config)
    try:
        rows = store.list()
    finally:
        store.close()
    known = dict(state.get("ticketStates") or {})
    sent = 0
    for ticket in rows:
        previous = known.get(ticket["id"])
        if previous is None:
            known[ticket["id"]] = ticket["status"]
            continue  # first sighting seeds the state; nothing is announced twice
        if previous == ticket["status"] or ticket["status"] not in NOTIFY_STATUSES:
            known[ticket["id"]] = ticket["status"]
            continue
        if ticket["status"] == "ready" and previous != "in-progress":
            known[ticket["id"]] = ticket["status"]
            continue  # only a retry is worth a message, not an ordinary queue move
        known[ticket["id"]] = ticket["status"]
        note = f"\n{ticket['note']}" if ticket.get("note") else ""
        verdict = ""
        if ticket.get("verify_verdict"):
            verdict = f" · review {ticket['verify_verdict']}"
            if ticket.get("verify_attempt"):
                verdict += f" ({ticket['verify_attempt']})"
        link = f"\n{ticket['pr_url']}" if ticket.get("pr_url") else ""
        icon = STATUS_ICON.get(ticket["status"], "•")
        reply(
            config, str(chats[0]),
            f"{icon} {ticket['id']} → {ticket['status']}: {ticket['title']}{verdict}{note}{link}",
        )
        sent += 1
        report = (ticket.get("report_path") or "").strip()
        if ticket["status"] == "done" and report:
            send_document(config, str(chats[0]), Path(report), f"{ticket['id']} report")
    state["ticketStates"] = known
    save_state(state)
    return sent


def poll_once(config: dict, state: dict) -> int:
    """One getUpdates round. Returns how many commands were answered."""
    payload = {
        "timeout": int(config["bridge"].get("pollTimeout", 25)),
        "limit": 20,
        "allowed_updates": ["message"],
    }
    if state.get("offset") is not None:
        payload["offset"] = state["offset"]
    ok, result = telegram(config, "getUpdates", payload, timeout=payload["timeout"] + 15)
    if not ok:
        log(f"getUpdates failed: {result}")
        return -1
    answered = 0
    for update in result or []:
        state["offset"] = update["update_id"] + 1
        save_state(state)
        message = update.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        text = message.get("text") or ""
        if chat_id is None or not text:
            continue
        answer = handle(config, str(chat_id), text)
        if answer:
            reply(config, str(chat_id), answer)
            answered += 1
    return answered


def poll_forever(config: dict) -> int:
    state = load_state()
    log("bridge started (long polling)")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while True:
        try:
            notify_approvals(config, state)
            notify_ticket_states(config, state)
            result = poll_once(config, state)
        except Stop:
            raise
        except Exception as exc:  # one round must not kill the daemon
            log(f"poll failed: {type(exc).__name__}: {exc}")
            result = -1
        if result < 0:
            time.sleep(5)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", metavar="TEXT", help="process one text as the first allowlisted chat, print the reply, send nothing")
    parser.add_argument("--chat", default=None, help="chat id for --once (default: the first in the allowlist)")
    parser.add_argument("--selftest", action="store_true", help="check the configuration without calling Telegram")
    args = parser.parse_args()

    config = load_config()

    if args.selftest:
        chats = config["telegram"]["allowChatIds"]
        print(f"config      : {CONFIG_PATH}")
        print(f"token set   : {bool(config['telegram']['botToken'])}")
        print(f"allowChatIds: {chats}")
        print(f"t3 cli      : {config['t3']['cli']} ({'present' if Path(config['t3']['cli']).exists() else 'MISSING'})")
        print(f"db          : {config['t3']['dbPath']} ({'present' if Path(config['t3']['dbPath']).exists() else 'MISSING'})")
        print(f"projects    : {len(projects(config))} read")
        print(f"dryRun      : {config['bridge'].get('dryRun')} | allowWrites: {config['bridge'].get('allowWrites')}")
        return 0

    if args.once is not None:
        chat = args.chat or (config["telegram"]["allowChatIds"] or ["0"])[0]
        answer = handle(config, str(chat), args.once)
        print(answer if answer is not None else "(chat not allowed)")
        return 0

    try:
        return poll_forever(config)
    except Stop:
        log("bridge stopped (signal)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
