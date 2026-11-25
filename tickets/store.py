#!/usr/bin/env python3
"""Ticket store for the T3 workflow.

Design decisions that come from the user, not from taste:
  - SQLite is the source of truth (choice 1B); GitHub Issues can be added later as
    a mirror without changing this module's API.
  - Model and harness selection are persistent configuration owned by the human
    (choice 2 and 7). A ticket names a *profile*; the watcher only reads it and
    refuses to invent one.
  - Claiming is atomic: one `UPDATE ... WHERE status='ready'` decides the winner,
    so two watchers cannot take the same ticket.

States and the only transitions allowed:

    backlog → ready → in-progress → testing → review → done
       ↑        ↓          ↓           ↓         ↓
       └──── blocked ◄──────┴───────────┴─────────┘
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
DB_PATH = Path(os.environ.get("T3_TICKETS_DB", HOME / ".local/state/t3-workflow/tickets.sqlite"))
CONFIG_PATH = Path(os.environ.get("T3_WORKFLOW_CONFIG", HOME / ".config/t3-workflow/config.json"))

STATUSES = ("backlog", "ready", "in-progress", "testing", "review", "done", "blocked")
TRANSITIONS = {
    "backlog": {"ready", "blocked"},
    "ready": {"in-progress", "backlog", "blocked"},
    "in-progress": {"testing", "ready", "blocked"},
    "testing": {"review", "in-progress", "blocked"},
    "review": {"done", "in-progress", "blocked"},
    "blocked": {"ready", "backlog"},
    "done": set(),
}

SCHEMA = """
create table if not exists tickets (
    id            text primary key,
    title         text not null,
    body          text not null default '',
    status        text not null,
    priority      integer not null default 3,
    project       text not null,
    model_profile text not null default '',
    test_cmd      text not null default '',
    thread_id     text,
    branch        text,
    pr_url        text,
    claimed_at    text,
    claimed_by    text,
    created_at    text not null,
    updated_at    text not null,
    note          text not null default ''
);
create table if not exists events (
    event_id  integer primary key autoincrement,
    ticket_id text not null,
    at        text not null,
    kind      text not null,
    detail    text not null default ''
);
create index if not exists tickets_status on tickets(status, priority, created_at);
"""


class TicketError(Exception):
    """Raised for a rejected operation; the message is meant for the operator."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config(path: Path | None = None) -> dict:
    path = path or CONFIG_PATH
    if not path.exists():
        raise TicketError(f"config tidak ada: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_profile(config: dict, name: str | None) -> dict:
    """Return {provider, model, options, name} for a ticket's profile.

    An unknown profile is an error, never a fallback: the human chooses the model.
    """
    profiles = config.get("models") or {}
    chosen = name or (config.get("defaults") or {}).get("profile") or ""
    if not chosen:
        raise TicketError("tiket tidak menyebut profil model dan config tidak punya defaults.profile")
    profile = profiles.get(chosen)
    if not profile:
        raise TicketError(f"profil model '{chosen}' tidak ada di config (pilihan: {', '.join(sorted(profiles)) or 'kosong'})")
    if not profile.get("provider") or not profile.get("model"):
        raise TicketError(f"profil '{chosen}' harus punya provider dan model")
    return {"name": chosen, **profile}


class TicketStore:
    def __init__(self, path: Path | None = None, config_path: Path | None = None) -> None:
        self.path = Path(path or DB_PATH)
        self.config_path = Path(config_path or CONFIG_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.path)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        self.con.commit()

    # -------------------------------------------------------------- helpers

    def _event(self, ticket_id: str, kind: str, detail: str = "") -> None:
        self.con.execute(
            "insert into events (ticket_id, at, kind, detail) values (?,?,?,?)",
            (ticket_id, now(), kind, detail),
        )

    def _next_id(self) -> str:
        row = self.con.execute("select count(*) as n from tickets").fetchone()
        return f"T-{row['n'] + 1:04d}"

    def close(self) -> None:
        self.con.close()

    # -------------------------------------------------------------- writes

    def add(
        self,
        title: str,
        project: str,
        body: str = "",
        model_profile: str = "",
        test_cmd: str = "",
        priority: int = 3,
        status: str = "backlog",
    ) -> dict:
        if not title.strip():
            raise TicketError("judul tiket kosong")
        if status not in STATUSES:
            raise TicketError(f"status '{status}' tidak dikenal")
        if priority < 1 or priority > 5:
            raise TicketError("prioritas harus 1..5")
        config = load_config(self.config_path)
        if model_profile:
            resolve_profile(config, model_profile)  # tolak profil asing saat dibuat
        ticket_id = self._next_id()
        stamp = now()
        self.con.execute(
            "insert into tickets (id,title,body,status,priority,project,model_profile,test_cmd,created_at,updated_at)"
            " values (?,?,?,?,?,?,?,?,?,?)",
            (ticket_id, title.strip(), body, status, priority, project, model_profile, test_cmd, stamp, stamp),
        )
        self._event(ticket_id, "created", f"status={status} profile={model_profile or '-'}")
        self.con.commit()
        return self.get(ticket_id)

    def move(self, ticket_id: str, status: str, note: str = "", force: bool = False) -> dict:
        """Move a ticket. `force` is the human override: it skips the state machine.

        The model follows the state machine; a human may jump anywhere, and the
        jump is recorded as a distinct event so the report shows it.
        """
        ticket = self.get(ticket_id)
        if status not in STATUSES:
            raise TicketError(f"status '{status}' tidak dikenal")
        if ticket["status"] == status:
            raise TicketError(f"tiket {ticket_id} sudah berstatus {status}")
        allowed = status in TRANSITIONS[ticket["status"]]
        if not allowed and not force:
            raise TicketError(
                f"transisi {ticket['status']} → {status} tidak diizinkan "
                f"(boleh: {', '.join(sorted(TRANSITIONS[ticket['status']])) or 'tidak ada'}; "
                "pakai --force untuk memaksa)"
            )
        self.con.execute(
            "update tickets set status=?, updated_at=?, note=? where id=?",
            (status, now(), note or ticket["note"], ticket_id),
        )
        kind = "moved" if allowed else "forced"
        self._event(ticket_id, kind, f"{ticket['status']} → {status} {note}".strip())
        self.con.commit()
        return self.get(ticket_id)

    def claim(self, ticket_id: str, worker: str) -> dict:
        """Atomic claim. Returns the ticket, or raises when somebody else won."""
        stamp = now()
        cursor = self.con.execute(
            "update tickets set status='in-progress', claimed_at=?, claimed_by=?, updated_at=?"
            " where id=? and status='ready'",
            (stamp, worker, stamp, ticket_id),
        )
        if cursor.rowcount != 1:
            self.con.rollback()
            current = self.get(ticket_id)["status"]
            raise TicketError(f"tiket {ticket_id} tidak bisa diambil (status sekarang: {current})")
        self._event(ticket_id, "claimed", worker)
        self.con.commit()
        return self.get(ticket_id)

    def attach_thread(self, ticket_id: str, thread_id: str, note: str = "") -> dict:
        self.con.execute(
            "update tickets set thread_id=?, updated_at=?, note=? where id=?",
            (thread_id, now(), note or self.get(ticket_id)["note"], ticket_id),
        )
        self._event(ticket_id, "thread", thread_id)
        self.con.commit()
        return self.get(ticket_id)

    # -------------------------------------------------------------- reads

    def get(self, ticket_id: str) -> dict:
        row = self.con.execute("select * from tickets where id=?", (ticket_id,)).fetchone()
        if row is None:
            raise TicketError(f"tiket {ticket_id} tidak ada")
        return dict(row)

    def list(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.con.execute(
                "select * from tickets where status=? order by priority asc, created_at asc", (status,)
            ).fetchall()
        else:
            rows = self.con.execute(
                "select * from tickets order by case status when 'ready' then 0 when 'in-progress' then 1"
                " when 'testing' then 2 when 'review' then 3 when 'backlog' then 4 when 'blocked' then 5 else 6 end,"
                " priority asc, created_at asc"
            ).fetchall()
        return [dict(row) for row in rows]

    def next_ready(self) -> dict | None:
        row = self.con.execute(
            "select * from tickets where status='ready' order by priority asc, created_at asc limit 1"
        ).fetchone()
        return dict(row) if row else None

    def summary(self) -> dict:
        rows = self.con.execute("select status, count(*) as n from tickets group by status").fetchall()
        return {row["status"]: row["n"] for row in rows}

    def report(self, ticket_id: str, t3_link: str = "") -> str:
        """Markdown report for a finished (or in-flight) ticket."""
        ticket = self.get(ticket_id)
        events = self.events(ticket_id)
        lines = [
            f"# {ticket['id']} · {ticket['title']}",
            "",
            f"- **Status**: `{ticket['status']}`",
            f"- **Project**: {ticket['project']}",
            f"- **Profil model**: {ticket['model_profile'] or '(default config)'}",
            f"- **Prioritas**: {ticket['priority']}",
            f"- **Dibuat**: {ticket['created_at']}",
            f"- **Diperbarui**: {ticket['updated_at']}",
        ]
        if ticket["thread_id"]:
            lines.append(f"- **Thread**: `{ticket['thread_id']}`")
        if t3_link:
            lines.append(f"- **Tautan**: {t3_link}")
        if ticket["test_cmd"]:
            lines += ["", "## Perintah test", "", "```bash", ticket["test_cmd"], "```"]
        if ticket["body"]:
            lines += ["", "## Deskripsi", "", ticket["body"]]
        lines += ["", "## Bukti & catatan", "", ticket["note"] or "_belum ada catatan bukti_"]
        lines += ["", "## Riwayat", "", "| Waktu | Kejadian | Detail |", "|---|---|---|"]
        for event in events:
            lines.append(f"| {event['at'][:19]} | {event['kind']} | {event['detail'].replace('|', '/')} |")
        return "\n".join(lines) + "\n"

    def events(self, ticket_id: str) -> list[dict]:
        rows = self.con.execute(
            "select * from events where ticket_id=? order by event_id", (ticket_id,)
        ).fetchall()
        return [dict(row) for row in rows]
