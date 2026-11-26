#!/usr/bin/env python3
"""CLI for the ticket store.

    python3 tickets/cli.py init
    python3 tickets/cli.py add "Judul" --project t3-workflow --profile fast --test "python3 -m unittest"
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
    if ticket["note"]:
        print(f"  note    : {ticket['note']}")
    if events:
        print("  riwayat :")
        for event in events:
            print(f"    - {event['at'][:19]}  {event['kind']}  {event['detail']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--config", default=str(CONFIG_PATH))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="buat database dan tabel")
    sub.add_parser("profiles", help="tampilkan profil model dari config")

    add = sub.add_parser("add", help="buat tiket")
    add.add_argument("title")
    add.add_argument("--project", required=True)
    add.add_argument("--body", default="")
    add.add_argument("--profile", default="", help="nama profil di config; kosong = defaults.profile")
    add.add_argument("--test", dest="test_cmd", default="")
    add.add_argument("--priority", type=int, default=3)
    add.add_argument("--status", default="backlog", choices=STATUSES)

    listing = sub.add_parser("list", help="daftar tiket")
    listing.add_argument("--status", default=None, choices=STATUSES)

    show = sub.add_parser("show", help="detail satu tiket")
    show.add_argument("ticket")

    move = sub.add_parser("move", help="pindahkan status")
    move.add_argument("ticket")
    move.add_argument("status", choices=STATUSES)
    move.add_argument("--note", default="")
    move.add_argument("--force", action="store_true", help="lewati state machine (override manusia)")

    backup = sub.add_parser("backup", help="salin database tiket dan laporan ke folder")
    backup.add_argument("--out", default=str(Path.home() / ".local/state/t3-workflow/backups"))
    backup.add_argument("--keep", type=int, default=7, help="jumlah backup terbaru yang disimpan (0 = semua)")

    report = sub.add_parser("report", help="tulis laporan markdown untuk satu tiket")
    report.add_argument("ticket")
    report.add_argument("--out", default="", help="path tujuan; kosong = cetak ke stdout")

    args = parser.parse_args()
    store = TicketStore(Path(args.db), Path(args.config))
    try:
        if args.command == "init":
            print(f"database siap: {store.path}")
        elif args.command == "profiles":
            config = load_config(Path(args.config))
            for name, profile in sorted((config.get("models") or {}).items()):
                mark = " (default)" if name == (config.get("defaults") or {}).get("profile") else ""
                print(f"{name}{mark}: {profile.get('provider')} / {profile.get('model')}")
        elif args.command == "add":
            ticket = store.add(
                title=args.title,
                project=args.project,
                body=args.body,
                model_profile=args.profile,
                test_cmd=args.test_cmd,
                priority=args.priority,
                status=args.status,
            )
            print_ticket(ticket)
        elif args.command == "list":
            tickets = store.list(args.status)
            if not tickets:
                print("(tidak ada tiket)")
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
                        print(f"dipangkas: {old.name}")
                    for old in sorted(Path(args.out).glob("reports-*"))[:-args.keep]:
                        if old.is_dir():
                            shutil.rmtree(old, ignore_errors=True)
                            print(f"dipangkas: {old.name}")
            print(f"backup: {target}")
        elif args.command == "report":
            text = store.report(args.ticket)
            if args.out:
                out = Path(args.out)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(text, encoding="utf-8")
                print(f"laporan ditulis: {out}")
            else:
                print(text)
    except TicketError as exc:
        print(f"ditolak: {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
