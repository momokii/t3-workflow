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
from html import escape
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
DB_PATH = Path(os.environ.get("T3_TICKETS_DB", HOME / ".local/state/t3-workflow/tickets.sqlite"))
REPORTS_DIR = Path(os.environ.get("T3_REPORTS_DIR", HOME / ".local/state/t3-workflow/reports"))
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
create table if not exists approvals (
    approval_id integer primary key autoincrement,
    ticket_id   text not null,
    question    text not null,
    status      text not null default 'pending',
    created_at  text not null,
    decided_at  text,
    decided_by  text,
    note        text not null default ''
);
create index if not exists approvals_status on approvals(status, approval_id);
"""

# columns added after the first release; safe to run repeatedly
MIGRATIONS = [
    ("tickets", "proof", "text not null default ''"),
    ("tickets", "report_path", "text not null default ''"),
    ("tickets", "requires_approval", "integer not null default 0"),
    ("tickets", "pr_body_path", "text not null default ''"),
    ("tickets", "attachment_path", "text not null default ''"),
    ("tickets", "provider", "text not null default ''"),
    ("tickets", "model", "text not null default ''"),
    ("tickets", "verify_thread_id", "text not null default ''"),
    ("tickets", "verify_run_id", "text not null default ''"),
    ("tickets", "verify_provider", "text not null default ''"),
    ("tickets", "verify_model", "text not null default ''"),
    ("tickets", "verify_attempt", "integer not null default 0"),
    ("tickets", "verify_verdict", "text not null default ''"),
    ("tickets", "verify_findings", "text not null default ''"),
    ("tickets", "verify_started_at", "text not null default ''"),
    ("tickets", "verify_finished_at", "text not null default ''"),
    ("tickets", "verify_baseline", "text not null default ''"),
    ("tickets", "dev_attempts", "integer not null default 0"),
    ("tickets", "test_attempts", "integer not null default 0"),
    ("tickets", "dev_run_id", "text not null default ''"),
    ("tickets", "retry_after", "text not null default ''"),
    ("tickets", "quota_attempts", "integer not null default 0"),
]


def migrate(con: sqlite3.Connection) -> None:
    for table, column, definition in MIGRATIONS:
        existing = {row[1] for row in con.execute(f"pragma table_info({table})")}
        if column not in existing:
            con.execute(f"alter table {table} add column {column} {definition}")
    con.commit()


class TicketError(Exception):
    """Raised for a rejected operation; the message is meant for the operator."""


def report_page_path(path: Path | str) -> Path:
    """The readable HTML page that accompanies a ticket's markdown report."""
    return Path(path).with_suffix(".html")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config(path: Path | None = None) -> dict:
    path = path or CONFIG_PATH
    if not path.exists():
        raise TicketError(f"config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def project_defaults(config: dict, project: str) -> dict:
    """The defaults a project carries in config, so a ticket can be one line.

    Matching is by the T3 project title, which is what a ticket stores. A ticket still
    wins: these only fill what the caller left empty.
    """
    entries = config.get("projects") or {}
    value = entries.get(project)
    if value is None:
        lowered = (project or "").lower()
        value = next((item for key, item in entries.items() if key.lower() == lowered), None)
    return dict(value or {})


def resolve_profile(config: dict, name: str | None) -> dict:
    """Return {provider, model, options, name} for a ticket's profile.

    An unknown profile is an error, never a fallback: the human chooses the model.
    """
    profiles = config.get("models") or {}
    chosen = name or (config.get("defaults") or {}).get("profile") or ""
    if not chosen:
        raise TicketError("ticket names no model profile and config has no defaults.profile")
    profile = profiles.get(chosen)
    if not profile:
        raise TicketError(f"model profile '{chosen}' is not in config (available: {', '.join(sorted(profiles)) or 'none'})")
    if not profile.get("provider") or not profile.get("model"):
        raise TicketError(f"profile '{chosen}' must define provider and model")
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
        migrate(self.con)

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
        requires_approval: bool = False,
        attachment_path: str = "",
        provider: str = "",
        model: str = "",
    ) -> dict:
        if not title.strip():
            raise TicketError("ticket title is empty")
        if status not in STATUSES:
            raise TicketError(f"unknown status '{status}'")
        if priority < 1 or priority > 5:
            raise TicketError("priority must be 1..5")
        config = load_config(self.config_path)
        if model_profile:
            resolve_profile(config, model_profile)  # reject an unknown profile at creation
        ticket_id = self._next_id()
        stamp = now()
        self.con.execute(
            "insert into tickets (id,title,body,status,priority,project,model_profile,test_cmd,created_at,updated_at,requires_approval,attachment_path,provider,model)"
            " values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ticket_id, title.strip(), body, status, priority, project, model_profile, test_cmd,
             stamp, stamp, 1 if requires_approval else 0, attachment_path, provider, model),
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
            raise TicketError(f"unknown status '{status}'")
        if ticket["status"] == status:
            raise TicketError(f"ticket {ticket_id} is already {status}")
        allowed = status in TRANSITIONS[ticket["status"]]
        if not allowed and not force:
            raise TicketError(
                f"transition {ticket['status']} → {status} is not allowed "
                f"(allowed: {', '.join(sorted(TRANSITIONS[ticket['status']])) or 'none'}; "
                "use --force to override)"
            )
        self.con.execute(
            "update tickets set status=?, updated_at=?, note=?, retry_after=? where id=?",
            (status, now(), note or ticket["note"], "" if status == "ready" else ticket.get("retry_after", ""),
             ticket_id),
        )
        kind = "moved" if allowed else "forced"
        self._event(ticket_id, kind, f"{ticket['status']} → {status} {note}".strip())
        self.con.commit()
        if status == "done":
            self.write_report(ticket_id)
        return self.get(ticket_id)

    EDITABLE_FIELDS = (
        "title", "body", "test_cmd", "priority", "requires_approval", "attachment_path",
        "model_profile", "provider", "model", "project",
    )

    def update(self, ticket_id: str, force: bool = False, **fields) -> dict:
        """Edit the human-owned fields of a ticket, with history.

        A finished ticket is final: its report already exists as evidence. A ticket that a
        stage is working on needs `force`, the same human override the state machine uses.
        """
        ticket = self.get(ticket_id)
        if ticket["status"] == "done":
            raise TicketError("a finished ticket cannot be edited; its report is the record")
        if ticket["status"] in ("in-progress", "testing", "review") and not force:
            raise TicketError(
                f"ticket {ticket_id} is {ticket['status']}; use force to edit it mid-flight"
            )
        changes = {key: value for key, value in fields.items()
                   if key in self.EDITABLE_FIELDS and value is not None}
        if not changes:
            raise TicketError("no editable field was given")
        if "title" in changes and not str(changes["title"]).strip():
            raise TicketError("ticket title is empty")
        if "priority" in changes:
            priority = int(changes["priority"])
            if priority < 1 or priority > 5:
                raise TicketError("priority must be 1..5")
            changes["priority"] = priority
        if "requires_approval" in changes:
            changes["requires_approval"] = 1 if changes["requires_approval"] else 0
        if changes.get("model_profile"):
            resolve_profile(load_config(self.config_path), changes["model_profile"])
        columns = ", ".join(f"{key}=?" for key in changes)
        self.con.execute(
            f"update tickets set {columns}, updated_at=? where id=?",
            (*changes.values(), now(), ticket_id),
        )
        self._event(ticket_id, "edited", ", ".join(sorted(changes)))
        self.con.commit()
        return self.get(ticket_id)

    def delete(self, ticket_id: str) -> dict:
        """Delete a ticket, keeping its evidence reachable.

        The report is written first (so it can still be served after the row is gone) and a
        snapshot of the row goes into the append-only event history, which is what makes the
        deletion auditable instead of silent.
        """
        ticket = self.get(ticket_id)
        try:
            self.write_report(ticket_id)
        except OSError:
            pass
        snapshot = json.dumps(ticket, ensure_ascii=False)[:4000]
        self._event(ticket_id, "deleted", snapshot)
        self.con.execute("delete from tickets where id=?", (ticket_id,))
        self.con.commit()
        return {"id": ticket_id, "title": ticket["title"], "status": ticket["status"]}

    def write_pr_body(self, ticket_id: str, branch: str, remote_url: str) -> str:
        """PR body summary: what changed, the test evidence, and the link."""
        ticket = self.get(ticket_id)
        reports_dir = Path(os.environ.get("T3_REPORTS_DIR", REPORTS_DIR))
        target = reports_dir / f"{ticket_id}.pr.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"## {ticket_id} · {ticket['title']}",
            "",
            ticket["body"] or "",
            "",
            f"- Branch: `{branch}`",
            f"- Model profile: `{ticket['model_profile'] or 'default'}`",
        ]
        if ticket["thread_id"]:
            lines.append(f"- Thread: `{ticket['thread_id']}`")
        if ticket["proof"]:
            lines += ["", "### Test evidence", "", "```", ticket["proof"], "```"]
        if ticket["test_cmd"]:
            lines += ["", f"Test command: `{ticket['test_cmd']}`"]
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.con.execute(
            "update tickets set pr_body_path=?, updated_at=? where id=?", (str(target), now(), ticket_id)
        )
        self._event(ticket_id, "pr-body", str(target))
        self.con.commit()
        return str(target)

    def list_public(self, status: str | None = None) -> list[dict]:
        """List for HTTP: drop columns that can be large or sensitive."""
        rows = self.list(status)
        for row in rows:
            row["review_label"] = self.review_label(row)
            row.pop("proof", None)
            row.pop("verify_findings", None)   # reviewer prose is not for a list endpoint
            row.pop("verify_baseline", None)   # internal fingerprint of the tree under review
            row.pop("verify_run_id", None)     # internal anchor, not a UI concept
        return rows

    def write_report(self, ticket_id: str) -> str:
        """Write the markdown report to disk and store its path on the ticket.

        The directory is re-read from the environment on every call so a test can
        point it at a temporary folder without touching the production one.
        """
        reports_dir = Path(os.environ.get("T3_REPORTS_DIR", REPORTS_DIR))
        target = reports_dir / f"{ticket_id}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.report(ticket_id), encoding="utf-8")
        report_page_path(target).write_text(self.report_html(ticket_id, document=True), encoding="utf-8")
        self.con.execute(
            "update tickets set report_path=?, updated_at=? where id=?", (str(target), now(), ticket_id)
        )
        self._event(ticket_id, "report", str(target))
        self.con.commit()
        return str(target)

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
            raise TicketError(f"ticket {ticket_id} cannot be claimed (current status: {current})")
        self._event(ticket_id, "claimed", worker)
        self.con.commit()
        return self.get(ticket_id)

    def attach_proof(self, ticket_id: str, ok: bool, output: str) -> dict:
        """Store test evidence: result, output tail and timestamp."""
        stamp = now()
        proof = f"[{'PASS' if ok else 'FAIL'}] {stamp}\n{output.strip()[:4000]}"
        self.con.execute(
            "update tickets set proof=?, updated_at=? where id=?", (proof, stamp, ticket_id)
        )
        self._event(ticket_id, "proof", "pass" if ok else "fail")
        self.con.commit()
        return self.get(ticket_id)

    # -------------------------------------------------------- verification

    def attach_verifier(
        self, ticket_id: str, thread_id: str, run_id: str, provider: str, model: str, baseline: str
    ) -> dict:
        """Record a started review: one attempt, a thread, and the tree it is judging."""
        ticket = self.get(ticket_id)
        attempt = int(ticket.get("verify_attempt") or 0) + 1
        self.con.execute(
            "update tickets set verify_thread_id=?, verify_run_id=?, verify_provider=?, verify_model=?,"
            " verify_attempt=?, verify_verdict='', verify_findings='', verify_started_at=?,"
            " verify_finished_at='', verify_baseline=?, updated_at=? where id=?",
            (thread_id, run_id, provider, model, attempt, now(), baseline, now(), ticket_id),
        )
        self._event(ticket_id, "verify-started", f"attempt {attempt} · {provider}/{model} · run {run_id}")
        self.con.commit()
        return self.get(ticket_id)

    def set_attempts(self, ticket_id: str, *, dev: int | None = None, test: int | None = None) -> dict:
        ticket = self.get(ticket_id)
        dev_value = int(ticket.get("dev_attempts") or 0) if dev is None else int(dev)
        test_value = int(ticket.get("test_attempts") or 0) if test is None else int(test)
        self.con.execute(
            "update tickets set dev_attempts=?, test_attempts=?, updated_at=? where id=?",
            (dev_value, test_value, now(), ticket_id),
        )
        self.con.commit()
        return self.get(ticket_id)

    def schedule_retry(self, ticket_id: str, retry_after: str, quota_attempts: int) -> dict:
        """Block a ticket with a time at which the watcher may try again by itself."""
        self.con.execute(
            "update tickets set retry_after=?, quota_attempts=?, updated_at=? where id=?",
            (retry_after, int(quota_attempts), now(), ticket_id),
        )
        self._event(ticket_id, "retry-scheduled", retry_after)
        self.con.commit()
        return self.get(ticket_id)

    def due_retries(self, at: str) -> list[dict]:
        rows = self.con.execute(
            "select * from tickets where status='blocked' and retry_after != '' and retry_after <= ?"
            " order by retry_after",
            (at,),
        ).fetchall()
        return [dict(row) for row in rows]

    def clear_retry(self, ticket_id: str) -> dict:
        self.con.execute(
            "update tickets set retry_after='', updated_at=? where id=?", (now(), ticket_id)
        )
        self.con.commit()
        return self.get(ticket_id)

    def reset_attempts(self, ticket_id: str) -> dict:
        """A successful run clears the retry budget, including the resume anchor."""
        self.con.execute(
            "update tickets set dev_attempts=0, test_attempts=0, dev_run_id='', retry_after='',"
            " quota_attempts=0, updated_at=? where id=?",
            (now(), ticket_id),
        )
        self.con.commit()
        return self.get(ticket_id)

    def set_dev_run(self, ticket_id: str, run_id: str) -> dict:
        """Remember which run the dev thread is on, so `testing` waits for the new turn."""
        self.con.execute(
            "update tickets set dev_run_id=?, updated_at=? where id=?", (run_id, now(), ticket_id)
        )
        self.con.commit()
        return self.get(ticket_id)

    def attach_verify_run(self, ticket_id: str, run_id: str) -> dict:
        """Store the run id the launch did not return; it anchors the verdict to a run."""
        self.con.execute(
            "update tickets set verify_run_id=?, updated_at=? where id=?", (run_id, now(), ticket_id)
        )
        self._event(ticket_id, "verify-run", run_id)
        self.con.commit()
        return self.get(ticket_id)

    def record_verdict(self, ticket_id: str, verdict: str, findings: str = "") -> dict:
        self.con.execute(
            "update tickets set verify_verdict=?, verify_findings=?, verify_finished_at=?, updated_at=?"
            " where id=?",
            (verdict, findings[:8000], now(), now(), ticket_id),
        )
        self._event(ticket_id, f"verify-{verdict.lower()}", findings[:200])
        self.con.commit()
        return self.get(ticket_id)

    def clear_verifier(self, ticket_id: str) -> dict:
        """Forget the thread of an attempt, keeping the attempt counter for the retry budget."""
        self.con.execute(
            "update tickets set verify_thread_id='', verify_run_id='', verify_verdict='',"
            " verify_findings='', verify_started_at='', verify_finished_at='', verify_baseline='',"
            " updated_at=? where id=?",
            (now(), ticket_id),
        )
        self.con.commit()
        return self.get(ticket_id)

    def set_branch(self, ticket_id: str, branch: str, pr_url: str = "") -> dict:
        self.con.execute(
            "update tickets set branch=?, pr_url=?, updated_at=? where id=?",
            (branch, pr_url, now(), ticket_id),
        )
        self._event(ticket_id, "branch", branch)
        self.con.commit()
        return self.get(ticket_id)

    def set_report_path(self, ticket_id: str, path: str) -> dict:
        self.con.execute("update tickets set report_path=?, updated_at=? where id=?", (path, now(), ticket_id))
        self.con.commit()
        return self.get(ticket_id)

    # ---------------------------------------------------------- approvals

    def request_approval(self, ticket_id: str, question: str) -> dict:
        self.get(ticket_id)  # make sure the ticket exists
        existing = self.con.execute(
            "select * from approvals where ticket_id=? and status='pending' order by approval_id desc limit 1",
            (ticket_id,),
        ).fetchone()
        if existing:
            return dict(existing)
        self.con.execute(
            "insert into approvals (ticket_id, question, status, created_at) values (?,?,'pending',?)",
            (ticket_id, question, now()),
        )
        approval_id = self.con.execute("select last_insert_rowid() as id").fetchone()["id"]
        self._event(ticket_id, "approval-requested", question[:200])
        self.con.commit()
        return self.get_approval(approval_id)

    def get_approval(self, approval_id: int) -> dict:
        row = self.con.execute("select * from approvals where approval_id=?", (approval_id,)).fetchone()
        if row is None:
            raise TicketError(f"approval {approval_id} not found")
        return dict(row)

    def pending_approvals(self) -> list[dict]:
        rows = self.con.execute(
            "select * from approvals where status='pending' order by approval_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def decide_approval(self, approval_id: int, approved: bool, by: str, note: str = "") -> dict:
        approval = self.get_approval(approval_id)
        if approval["status"] != "pending":
            raise TicketError(f"approval {approval_id} is already {approval['status']} — second answer ignored")
        self.con.execute(
            "update approvals set status=?, decided_at=?, decided_by=?, note=? where approval_id=?",
            ("approved" if approved else "rejected", now(), by, note, approval_id),
        )
        self._event(approval["ticket_id"], "approval-decided", f"{'approved' if approved else 'rejected'} by {by}")
        self.con.commit()
        return self.get_approval(approval_id)

    def approval_state(self, ticket_id: str) -> str:
        """approved | rejected | pending | none"""
        row = self.con.execute(
            "select status from approvals where ticket_id=? order by approval_id desc limit 1",
            (ticket_id,),
        ).fetchone()
        return row["status"] if row else "none"

    def record_event(self, ticket_id: str, kind: str, detail: str = "") -> None:
        self._event(ticket_id, kind, detail)
        self.con.commit()

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
            raise TicketError(f"ticket {ticket_id} not found")
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

    def backup(self, out_dir: Path) -> Path:
        """Consistent database copy using the sqlite3 backup API."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        target = out_dir / f"tickets-{stamp}.sqlite"
        dest = sqlite3.connect(target)
        try:
            self.con.backup(dest)
        finally:
            dest.close()
        return target

    def summary(self) -> dict:
        rows = self.con.execute("select status, count(*) as n from tickets group by status").fetchall()
        return {row["status"]: row["n"] for row in rows}

    REPORT_FACTS = ("Status", "Project", "Model profile", "Priority", "Created", "Updated", "Thread")

    @staticmethod
    def report_facts(ticket: dict) -> list[tuple[str, str, bool]]:
        """(label, value, monospace) for the header, in one place for both renderers."""
        facts = [
            ("Status", ticket["status"], True),
            ("Project", ticket["project"], False),
            ("Model profile", ticket["model_profile"] or "(default config)", False),
            ("Priority", str(ticket["priority"]), False),
            ("Created", ticket["created_at"], False),
            ("Updated", ticket["updated_at"], False),
        ]
        if ticket["thread_id"]:
            facts.append(("Thread", ticket["thread_id"], True))
        return facts

    def review_facts(self, ticket: dict) -> list[tuple[str, str, bool]]:
        verdict = (ticket.get("verify_verdict") or "").strip()
        facts = [("Verdict", verdict or "not decided", True)]
        if ticket.get("verify_provider") or ticket.get("verify_model"):
            facts.append((
                "Reviewer",
                f"{ticket.get('verify_provider') or '?'} / {ticket.get('verify_model') or '?'}",
                False,
            ))
        if ticket.get("verify_thread_id"):
            facts.append(("Review thread", ticket["verify_thread_id"], True))
        facts.append(("Attempt", self.review_label(ticket), False))
        if ticket.get("verify_started_at"):
            facts.append(("Started", ticket["verify_started_at"], False))
        if ticket.get("verify_finished_at"):
            facts.append(("Finished", ticket["verify_finished_at"], False))
        return facts

    def review_label(self, ticket: dict) -> str:
        """One phrase that says what the verdict and the attempt count actually mean."""
        attempt = int(ticket.get("verify_attempt") or 0)
        verdict = (ticket.get("verify_verdict") or "").strip()
        if not attempt and not verdict:
            return "not reviewed"
        try:
            budget = int((load_config(self.config_path).get("verification") or {}).get("maxAttempts", 2) or 2)
        except TicketError:
            budget = 2
        if verdict.upper() == "ACCEPT":
            # The budget is a maximum, not a target: the denominator is noise on an acceptance.
            return "accept · first attempt" if attempt <= 1 else f"accept · attempt {attempt}"
        if verdict:
            return f"{verdict.lower()} · attempt {max(attempt, 1)} of {budget}"
        return f"pending · attempt {attempt} of {budget}"

    def report(self, ticket_id: str, t3_link: str = "") -> str:
        """Markdown report for a finished (or in-flight) ticket."""
        ticket = self.get(ticket_id)
        events = self.events(ticket_id)
        lines = [f"# {ticket['id']} · {ticket['title']}", ""]
        for label, value, mono in self.report_facts(ticket):
            lines.append(f"- **{label}**: {f'`{value}`' if mono else value}")
        if t3_link:
            lines.append(f"- **Link**: {t3_link}")
        if ticket["test_cmd"]:
            lines += ["", "## Test command", "", "```bash", ticket["test_cmd"], "```"]
        if ticket["body"]:
            lines += ["", "## Description", "", ticket["body"]]
        approval = self.approval_state(ticket_id)
        if approval != "none":
            lines += ["", f"**Approval**: `{approval}`"]
        lines += ["", "## Evidence and notes", "", ticket["note"] or "_no evidence note yet_"]
        if ticket.get("proof"):
            lines += ["", "### Test evidence", "", "```", ticket["proof"], "```"]
        verdict = (ticket.get("verify_verdict") or "").strip()
        if verdict or ticket.get("verify_attempt"):
            lines += ["", "## Independent review", ""]
            for label, value, mono in self.review_facts(ticket):
                lines.append(f"- **{label}**: {f'`{value}`' if mono else value}")
            if ticket.get("verify_findings"):
                lines += ["", "### Findings", "", ticket["verify_findings"]]
        lines += ["", "## History", "", "| Time | Event | Detail |", "|---|---|---|"]
        for event in events:
            lines.append(f"| {event['at'][:19]} | {event['kind']} | {event['detail'].replace('|', '/')} |")
        return "\n".join(lines) + "\n"

    REPORT_STYLE = """
    body { font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, system-ui, sans-serif;
           margin: 0; padding: 28px; color: #16181d; background: #f6f7f9; }
    main { max-width: 820px; margin: 0 auto; background: #fff; border: 1px solid #dfe3ea;
           border-radius: 12px; padding: 24px 26px; }
    h1 { font-size: 22px; margin: 0 0 12px; } h2 { font-size: 16px; margin: 22px 0 8px; }
    h3 { font-size: 14px; margin: 16px 0 6px; } p { margin: 8px 0; }
    code { background: #eef1f5; border: 1px solid #dfe3ea; border-radius: 5px; padding: 1px 4px; font-size: 12px; }
    pre { background: #eef1f5; border: 1px solid #dfe3ea; border-radius: 9px; padding: 10px 12px;
          overflow-x: auto; font-size: 12px; }
    table { border-collapse: collapse; width: 100%; font-size: 13px; margin: 8px 0; display: block; overflow-x: auto; }
    th, td { border-bottom: 1px solid #dfe3ea; padding: 6px 10px; text-align: left; vertical-align: top; }
    dt { color: #5b6472; float: left; clear: left; width: 130px; } dd { margin: 0 0 4px 140px; word-break: break-all; }
    .verdict { font-weight: 700; }
    """

    def report_html(self, ticket_id: str, document: bool = False) -> str:
        """A self-contained HTML rendering of the report, built from the ticket itself.

        The board inserts the fragment (`document=False`); the release writes the full page next
        to the markdown so the delivered document reads properly on a phone. Every field is
        escaped: a ticket title is user input.
        """
        ticket = self.get(ticket_id)
        events = self.events(ticket_id)
        esc = escape
        body: list[str] = [f"<h1>{esc(ticket['id'])} · {esc(ticket['title'])}</h1>", "<dl>"]
        for label, value, mono in self.report_facts(ticket):
            shown = f"<code>{esc(value)}</code>" if mono else esc(value)
            body.append(f"<dt>{esc(label)}</dt><dd>{shown}</dd>")
        body.append("</dl>")
        if ticket["test_cmd"]:
            body += ["<h2>Test command</h2>", f"<pre><code>{esc(ticket['test_cmd'])}</code></pre>"]
        if ticket["body"]:
            body += ["<h2>Description</h2>", f"<p>{esc(ticket['body'])}</p>"]
        approval = self.approval_state(ticket_id)
        if approval != "none":
            body.append(f"<p><strong>Approval</strong>: {esc(approval)}</p>")
        body += ["<h2>Evidence and notes</h2>", f"<p>{esc(ticket['note'] or 'no evidence note yet')}</p>"]
        if ticket.get("proof"):
            body += ["<h3>Test evidence</h3>", f"<pre>{esc(ticket['proof'])}</pre>"]
        verdict = (ticket.get("verify_verdict") or "").strip()
        if verdict or ticket.get("verify_attempt"):
            body += ["<h2>Independent review</h2>", "<dl>"]
            for label, value, mono in self.review_facts(ticket):
                shown = f"<code>{esc(value)}</code>" if mono else esc(value)
                body.append(f"<dt>{esc(label)}</dt><dd>{shown}</dd>")
            body.append("</dl>")
            if ticket.get("verify_findings"):
                body += ["<h3>Findings</h3>", f"<pre>{esc(ticket['verify_findings'])}</pre>"]
        body += ["<h2>History</h2>", "<table><thead><tr><th>Time</th><th>Event</th><th>Detail</th></tr></thead><tbody>"]
        for event in events:
            body.append(
                f"<tr><td>{esc(event['at'][:19])}</td><td>{esc(event['kind'])}</td>"
                f"<td>{esc(event['detail'])}</td></tr>"
            )
        body += ["</tbody></table>"]
        fragment = "\n".join(body)
        if not document:
            return fragment
        return (
            "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>{esc(ticket['id'])} · {esc(ticket['title'])}</title>\n"
            f"<style>{self.REPORT_STYLE}</style>\n</head>\n<body>\n<main>\n"
            f"{fragment}\n</main>\n</body>\n</html>\n"
        )

    def events(self, ticket_id: str) -> list[dict]:
        rows = self.con.execute(
            "select * from events where ticket_id=? order by event_id", (ticket_id,)
        ).fetchall()
        return [dict(row) for row in rows]
