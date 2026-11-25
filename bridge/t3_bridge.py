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
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
if str(WORKFLOW_ROOT / "tickets") not in sys.path:
    sys.path.insert(0, str(WORKFLOW_ROOT / "tickets"))

try:  # pragma: no cover - hanya berbeda bila repo dipindah
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
        return False, "botToken kosong"
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
    text = text if len(text) <= MAX_REPLY else text[: MAX_REPLY - 20] + "\n… (dipotong)"
    ok, detail = telegram(config, "sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": True})
    if not ok:
        log(f"gagal kirim balasan: {detail}")


# ---------------------------------------------------------------- t3 access


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
        raise RuntimeError("modul tickets tidak tersedia di sebelah bridge")
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
        log(f"baca projects gagal: {exc}")
        return []
    return [dict(row) for row in rows]


def resolve_project(config: dict, needle: str) -> tuple[dict | None, str]:
    needle = needle.strip()
    if not needle:
        return None, "nama project kosong"
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
        return None, f"project '{needle}' tidak ada. Pakai /projects."
    names = ", ".join(sorted(p["title"] for p in partial)[:6])
    return None, f"'{needle}' ambigu: {names}"


def thread_rows(config: dict, project: dict) -> list[dict]:
    code, out = t3_run(config, ["thread", "list", "--project", project["workspace_root"], "--json"])
    if code != 0:
        raise RuntimeError(f"t3 thread list gagal (rc={code}): {out[:200]}")
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"output bukan JSON: {exc}") from exc


def thread_show(config: dict, thread_id: str, messages: int) -> dict:
    code, out = t3_run(config, ["thread", "show", thread_id, "--json", "--messages", str(messages)])
    if code != 0:
        raise RuntimeError(f"t3 thread show gagal (rc={code}): {out[:200]}")
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"output bukan JSON: {exc}") from exc


# ---------------------------------------------------------------- commands


def cmd_help() -> str:
    return (
        "Tiket:\n"
        "/tickets [status] — daftar tiket\n"
        "/ticket <project> <teks> — tiket baru (backlog)\n"
        "/pending — persetujuan menunggu\n"
        "/approve <id> [catatan] — setujui\n"
        "/reject <id> [catatan] — tolak\n"
        "\nPerintah:\n"
        "/projects — daftar project\n"
        "/threads <project> — thread terbaru\n"
        "/ctx <thread> [n] — pesan terakhir\n"
        "/send <thread> <teks> — kirim ke thread (thread sibuk: masuk antrean turn berikutnya)\n"
        "/new <project> <teks> — mulai thread baru\n"
        "/id — chat id ini\n"
        "\nPersetujuan (approval) tetap dijawab di UI T3, bukan dari sini."
    )


def cmd_tickets(config: dict, status: str = "") -> str:
    store = ticket_store(config)
    try:
        rows = store.list(status or None)
    finally:
        store.close()
    if not rows:
        return f"tidak ada tiket{' berstatus ' + status if status else ''}"
    lines = [f"{t['id']} [{t['status']}] p{t['priority']} {t['title'][:50]}" for t in rows[:20]]
    return f"Tiket ({len(rows)}):\n" + "\n".join(lines)


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
    return f"{ticket['id']} dibuat di {project} [{ticket['status']}] · profil {profile or '(default)'}"


def cmd_pending(config: dict) -> str:
    store = ticket_store(config)
    try:
        rows = store.pending_approvals()
    finally:
        store.close()
    if not rows:
        return "tidak ada persetujuan menunggu"
    return "Menunggu persetujuan:\n" + "\n".join(
        f"#{row['approval_id']} {row['ticket_id']}: {row['question'][:60]}" for row in rows
    )


def cmd_decide(config: dict, raw_id: str, approved: bool, note: str, chat_id: str) -> str:
    try:
        approval_id = int(raw_id.lstrip("#"))
    except ValueError:
        return "id persetujuan harus angka"
    store = ticket_store(config)
    try:
        approval = store.decide_approval(approval_id, approved, f"telegram:{chat_id}", note)
    finally:
        store.close()
    audit({"action": "approve" if approved else "reject", "approval": approval_id})
    return f"#{approval_id} {'disetujui' if approved else 'ditolak'} ({approval['ticket_id']})"


def cmd_projects(config: dict) -> str:
    items = projects(config)
    if not items:
        return "Tidak ada project terbaca dari database T3."
    lines = [f"{index + 1}. {p['title']}  ({str(p['project_id'])[:8]})" for index, p in enumerate(items)]
    return "Project:\n" + "\n".join(lines)


def cmd_threads(config: dict, needle: str) -> str:
    project, error = resolve_project(config, needle)
    if not project:
        return error
    rows = thread_rows(config, project)
    if not rows:
        return f"{project['title']}: belum ada thread."
    lines = []
    for row in rows[:15]:
        status = row.get("status") or "-"
        title = (row.get("title") or "(tanpa judul)")[:60]
        lines.append(f"• {title}\n  {row['threadId']}  [{status}]")
    return f"Thread di {project['title']} ({len(rows)}):\n" + "\n".join(lines)


def cmd_ctx(config: dict, thread_id: str, count: int = 3) -> str:
    if not THREAD_ID.match(thread_id):
        return "Format thread id tidak valid (UUID, dengan atau tanpa awalan mcp:)."
    data = thread_show(config, thread_id, count)
    thread = data.get("thread") or {}
    # `t3 thread show --json` mengembalikan {"thread": {...}, "messages": [...]}
    items = data.get("messages") or data.get("items") or []
    head = f"{thread.get('title') or '(tanpa judul)'}\nstatus: {thread.get('status')}  model: {thread.get('model')}\n{thread.get('link') or ''}"
    body = []
    for item in items[-count:]:
        role = item.get("role") or item.get("createdBy") or item.get("type") or "?"
        text = (item.get("text") or "").strip().replace("\n", " ")
        if text:
            body.append(f"[{role}] {text[:300]}")
    return head + ("\n\n" + "\n\n".join(body) if body else "\n\n(tidak ada pesan terbaca)")


def cmd_send(config: dict, thread_id: str, text: str) -> str:
    if not THREAD_ID.match(thread_id):
        return "Format thread id tidak valid (UUID, dengan atau tanpa awalan mcp:)."
    if not text.strip():
        return "Teks kosong."
    if not config["bridge"].get("allowWrites", True):
        return "Penulisan dimatikan (bridge.allowWrites=false)."
    if config["bridge"].get("dryRun"):
        return f"[dry-run] t3 thread send {thread_id} << {text[:120]}"
    code, out = t3_run(config, ["thread", "send", thread_id, "-", "--json"], stdin=text)
    audit({"action": "send", "threadId": thread_id, "rc": code, "chars": len(text)})
    if code != 0:
        return f"Gagal kirim (rc={code}): {out[:300]}"
    return "Terkirim. Thread sibuk akan menerimanya sebagai turn berikutnya."


def cmd_new(config: dict, needle: str, text: str) -> str:
    project, error = resolve_project(config, needle)
    if not project:
        return error
    if not text.strip():
        return "Teks kosong."
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
        return f"Gagal membuat thread (rc={code}): {out[:300]}"
    try:
        data = json.loads(out)
        return f"Thread dibuat: {data.get('threadId')}\n{data.get('link') or ''}"
    except json.JSONDecodeError:
        return f"Thread dibuat.\n{out[:300]}"


def handle(config: dict, chat_id: str, text: str) -> str | None:
    """Return a reply for an allowlisted chat, or None when the chat is unknown."""
    if str(chat_id) not in {str(item) for item in config["telegram"]["allowChatIds"]}:
        log(f"tolak chat tidak dikenal: {chat_id}")
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
                return "Pakai: /ticket <project> <teks>"
            return cmd_ticket_new(config, parts[0], parts[1])
        if command == "pending":
            return cmd_pending(config)
        if command in ("approve", "reject"):
            parts = rest.split(maxsplit=1)
            if not parts:
                return f"Pakai: /{command} <id> [catatan]"
            note = parts[1] if len(parts) > 1 else ""
            return cmd_decide(config, parts[0], command == "approve", note, str(chat_id))
        if command == "projects":
            return cmd_projects(config)
        if command == "threads":
            return cmd_threads(config, rest) if rest else "Pakai: /threads <project>"
        if command == "ctx":
            parts = rest.split(maxsplit=1)
            if not parts:
                return "Pakai: /ctx <thread-id> [jumlah]"
            count = 3
            if len(parts) > 1 and parts[1].strip().isdigit():
                count = max(1, min(10, int(parts[1].strip())))
            return cmd_ctx(config, parts[0], count)
        if command == "send":
            parts = rest.split(maxsplit=1)
            if len(parts) < 2:
                return "Pakai: /send <thread-id> <teks>"
            return cmd_send(config, parts[0], parts[1])
        if command == "new":
            parts = rest.split(maxsplit=1)
            if len(parts) < 2:
                return "Pakai: /new <project> <teks>"
            return cmd_new(config, parts[0], parts[1])
        return f"Perintah /{command} tidak dikenal.\n\n" + cmd_help()
    except StoreError as exc:
        log(f"ditolak /{command}: {exc}")
        return f"Ditolak: {str(exc)[:300]}"
    except RuntimeError as exc:
        log(f"error perintah /{command}: {exc}")
        return f"Error: {str(exc)[:300]}"
    except Exception as exc:  # bridge tidak boleh mati karena satu perintah
        log(f"error tak terduga /{command}: {type(exc).__name__}: {exc}")
        return f"Error tak terduga: {type(exc).__name__}: {str(exc)[:200]}"


# ---------------------------------------------------------------- loop


class Stop(Exception):
    pass


def _stop(signum, frame):  # noqa: ARG001
    raise Stop()


def notify_approvals(config: dict, state: dict) -> int:
    """Kirim satu pesan untuk tiap persetujuan yang belum pernah diberitahukan."""
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
            f"Persetujuan #{key} · {approval['ticket_id']}\n{approval['question']}\n\n"
            f"Balas: /approve {key} atau /reject {key}",
        )
        notified.add(key)
        sent += 1
    state["notifiedApprovals"] = sorted(notified)[-200:]
    save_state(state)
    return sent


def poll_once(config: dict, state: dict) -> int:
    """Satu putaran getUpdates. Mengembalikan jumlah perintah yang dijawab."""
    payload = {
        "timeout": int(config["bridge"].get("pollTimeout", 25)),
        "limit": 20,
        "allowed_updates": ["message"],
    }
    if state.get("offset") is not None:
        payload["offset"] = state["offset"]
    ok, result = telegram(config, "getUpdates", payload, timeout=payload["timeout"] + 15)
    if not ok:
        log(f"getUpdates gagal: {result}")
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
    log("bridge mulai (long polling)")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while True:
        try:
            notify_approvals(config, state)
            result = poll_once(config, state)
        except Stop:
            raise
        except Exception as exc:  # daemon tidak boleh mati karena satu putaran
            log(f"error putaran: {type(exc).__name__}: {exc}")
            result = -1
        if result < 0:
            time.sleep(5)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", metavar="TEXT", help="proses satu teks sebagai chat allowlist pertama, cetak balasan, tidak mengirim")
    parser.add_argument("--chat", default=None, help="chat id untuk --once (default: yang pertama di allowlist)")
    parser.add_argument("--selftest", action="store_true", help="periksa konfigurasi tanpa memanggil Telegram")
    args = parser.parse_args()

    config = load_config()

    if args.selftest:
        chats = config["telegram"]["allowChatIds"]
        print(f"config      : {CONFIG_PATH}")
        print(f"token ada   : {bool(config['telegram']['botToken'])}")
        print(f"allowChatIds: {chats}")
        print(f"t3 cli      : {config['t3']['cli']} ({'ada' if Path(config['t3']['cli']).exists() else 'TIDAK ADA'})")
        print(f"db          : {config['t3']['dbPath']} ({'ada' if Path(config['t3']['dbPath']).exists() else 'TIDAK ADA'})")
        print(f"project     : {len(projects(config))} terbaca")
        print(f"dryRun      : {config['bridge'].get('dryRun')} | allowWrites: {config['bridge'].get('allowWrites')}")
        return 0

    if args.once is not None:
        chat = args.chat or (config["telegram"]["allowChatIds"] or ["0"])[0]
        answer = handle(config, str(chat), args.once)
        print(answer if answer is not None else "(chat tidak diizinkan)")
        return 0

    try:
        return poll_forever(config)
    except Stop:
        log("bridge berhenti (sinyal)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
