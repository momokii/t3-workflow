#!/usr/bin/env python3
"""CLI for the ticket store.

    python3 tickets/cli.py init
    python3 tickets/cli.py add "Title" --project t3-workflow --profile fast --test "python3 -m unittest"
    python3 tickets/cli.py list [--status ready]
    python3 tickets/cli.py show T-0001
    python3 tickets/cli.py move T-0001 ready
    python3 tickets/cli.py profiles
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from store import (  # noqa: E402
    CONFIG_PATH,
    DB_PATH,
    STATUSES,
    TicketError,
    TicketStore,
    load_config,
    project_defaults,
    resolve_profile,
)


def print_ticket(ticket: dict, events: list[dict] | None = None) -> None:
    print(f"{ticket['id']}  [{ticket['status']}]  p{ticket['priority']}  {ticket['title']}")
    print(f"  project : {ticket['project']}")
    print(f"  profile : {ticket['model_profile'] or '(config default)'}")
    if ticket["test_cmd"]:
        print(f"  test    : {ticket['test_cmd']}")
    if ticket["thread_id"]:
        print(f"  thread  : {ticket['thread_id']}")
    if ticket.get("verify_verdict") or ticket.get("verify_attempt"):
        verdict = ticket.get("verify_verdict") or "pending"
        reviewer = " / ".join(part for part in (ticket.get("verify_provider"), ticket.get("verify_model")) if part)
        attempt = ticket.get("verify_attempt") or 0
        suffix = f" · {reviewer}" if reviewer else ""
        print(f"  review  : {verdict} · attempt {attempt}{suffix}")
        findings = (ticket.get("verify_findings") or "").strip()
        if findings:
            print("  findings:")
            for line in findings.splitlines()[:8]:
                print(f"    {line[:110]}")
    if ticket["note"]:
        print(f"  note    : {ticket['note']}")
    if events:
        print("  history :")
        for event in events:
            print(f"    - {event['at'][:19]}  {event['kind']}  {event['detail']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--config", default=str(CONFIG_PATH))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database and its tables")
    sub.add_parser("profiles", help="list the model profiles from config")

    add = sub.add_parser("add", help="create a ticket")
    add.add_argument("title")
    add.add_argument("--project", required=True)
    add.add_argument("--body", default="")
    add.add_argument("--profile", default="", help="profile name in config; empty = defaults.profile")
    add.add_argument("--test", dest="test_cmd", default="")
    add.add_argument("--priority", type=int, default=3)
    add.add_argument("--status", default="backlog", choices=STATUSES)

    listing = sub.add_parser("list", help="list tickets")
    listing.add_argument("--status", default=None, choices=STATUSES)

    show = sub.add_parser("show", help="show one ticket")
    show.add_argument("ticket")

    move = sub.add_parser("move", help="move a ticket to another status")
    move.add_argument("ticket")
    move.add_argument("status", choices=STATUSES)
    move.add_argument("--note", default="")
    move.add_argument("--force", action="store_true", help="skip the state machine (human override)")

    backup = sub.add_parser("backup", help="copy the ticket database and reports to a folder")
    backup.add_argument("--out", default=str(Path.home() / ".local/state/t3-workflow/backups"))
    backup.add_argument("--keep", type=int, default=7, help="how many recent backups to keep (0 = all)")

    edit = sub.add_parser("edit", help="change the human-owned fields of a ticket")
    edit.add_argument("ticket")
    edit.add_argument("--title")
    edit.add_argument("--body")
    edit.add_argument("--test", dest="test_cmd")
    edit.add_argument("--priority", type=int)
    edit.add_argument("--profile")
    edit.add_argument("--provider")
    edit.add_argument("--model")
    edit.add_argument("--attachment")
    edit.add_argument("--force", action="store_true", help="edit a ticket a stage is working on")

    delete = sub.add_parser("delete", help="delete a ticket, keeping its report and history")
    delete.add_argument("ticket")
    delete.add_argument("--yes", action="store_true", help="required: deletion is irreversible")

    report = sub.add_parser("report", help="write the markdown report for one ticket")
    report.add_argument("ticket")
    report.add_argument("--out", default="", help="target path; empty = print to stdout")

    args = parser.parse_args()
    store = TicketStore(Path(args.db), Path(args.config))
    try:
        if args.command == "init":
            print(f"database ready: {store.path}")
        elif args.command == "profiles":
            config = load_config(Path(args.config))
            for name, profile in sorted((config.get("models") or {}).items()):
                mark = " (default)" if name == (config.get("defaults") or {}).get("profile") else ""
                print(f"{name}{mark}: {profile.get('provider')} / {profile.get('model')}")
        elif args.command == "add":
            defaults = project_defaults(load_config(Path(args.config)), args.project)
            ticket = store.add(
                title=args.title,
                project=args.project,
                body=args.body,
                model_profile=args.profile or defaults.get("profile", ""),
                test_cmd=args.test_cmd or defaults.get("test_cmd", ""),
                priority=args.priority,
                status=args.status,
            )
            print_ticket(ticket)
        elif args.command == "list":
            tickets = store.list(args.status)
            if not tickets:
                print("(no tickets)")
            for ticket in tickets:
                print(f"{ticket['id']}  [{ticket['status']:11}] p{ticket['priority']}  {ticket['title'][:60]}")
        elif args.command == "show":
            print_ticket(store.get(args.ticket), store.events(args.ticket))
        elif args.command == "move":
            print_ticket(store.move(args.ticket, args.status, args.note, force=args.force))
        elif args.command == "backup":
            import shutil

            target = store.backup(Path(args.out))
            reports = Path(os.environ.get("T3_REPORTS_DIR", Path.home() / ".local/state/t3-workflow/reports"))
            if reports.exists():
                shutil.copytree(reports, target.parent / f"reports-{target.stem.split('-')[-1]}", dirs_exist_ok=True)
            if args.keep > 0:
                import re

                stamp = re.search(r"(\d{8}-\d{6})", target.name)
                if stamp:
                    for old in sorted(Path(args.out).glob("tickets-*.sqlite"))[:-args.keep]:
                        old.unlink(missing_ok=True)
                        print(f"pruned: {old.name}")
                    for old in sorted(Path(args.out).glob("reports-*"))[:-args.keep]:
                        if old.is_dir():
                            shutil.rmtree(old, ignore_errors=True)
                            print(f"pruned: {old.name}")
            print(f"backup: {target}")
        elif args.command == "edit":
            ticket = store.update(
                args.ticket,
                force=args.force,
                title=args.title,
                body=args.body,
                test_cmd=args.test_cmd,
                priority=args.priority,
                model_profile=args.profile,
                provider=args.provider,
                model=args.model,
                attachment_path=args.attachment,
            )
            print_ticket(ticket)
        elif args.command == "delete":
            if not args.yes:
                print("refusing to delete without --yes", file=sys.stderr)
                return 2
            removed = store.delete(args.ticket)
            print(f"deleted {removed['id']} · {removed['title']} (report kept)")
        elif args.command == "report":
            text = store.report(args.ticket)
            if args.out:
                out = Path(args.out)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(text, encoding="utf-8")
                print(f"report written: {out}")
            else:
                print(text)
    except TicketError as exc:
        print(f"rejected: {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
