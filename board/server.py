#!/usr/bin/env python3
"""Local Kanban board for the ticket store.

    python3 board/server.py [--host 127.0.0.1] [--port 8787] [--token SECRET]

Serves one page with drag-and-drop plus a per-card "move" control. The control is
not a fallback nicety: WCAG 2.2 SC 2.5.7 requires that any drag action also be
possible with a single pointer and no dragging.

Read endpoints are open on the bound interface; the move endpoint requires the
token when one is configured.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tickets"))

from catalog import CatalogError, load_catalog_cached, resolve_selection  # noqa: E402
from store import (  # noqa: E402
    CONFIG_PATH,
    DB_PATH,
    STATUSES,
    TRANSITIONS,
    TicketError,
    TicketStore,
    load_config,
)

STATIC = Path(__file__).resolve().parent / "static"


T3_DB = Path.home() / ".t3/userdata/statev2.sqlite"
CATALOG_DIR = Path.home() / ".t3/caches"


def t3_projects(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "select project_id, title, workspace_root from projection_projects "
            "where deleted_at is null order by lower(title)"
        ).fetchall()
    finally:
        con.close()
    return [dict(row) for row in rows]


def t3_threads(db_path: Path, limit: int = 40) -> list[dict]:
    """The most recent T3 threads, so existing work can be adopted as a ticket."""
    if not db_path.exists():
        return []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            select t.thread_id, t.title, t.created_at,
                   p.title as project, p.workspace_root,
                   (select r.status from orchestration_v2_projection_runs r
                     where r.thread_id = t.thread_id order by r.ordinal desc limit 1) as status
            from orchestration_v2_projection_threads t
            left join projection_projects p on p.project_id = t.project_id
            order by t.created_at desc
            limit ?
            """,
            (limit,),
        ).fetchall()
    finally:
        con.close()
    return [dict(row) for row in rows]


class Handler(BaseHTTPRequestHandler):
    store_path = DB_PATH
    config_path = CONFIG_PATH
    t3_db_path = T3_DB
    catalog_dir = CATALOG_DIR
    token = ""
    inject_token = True
    server_version = "t3-kanban/1.0"

    # ------------------------------------------------------------ helpers

    def _store(self) -> TicketStore:
        return TicketStore(Path(self.store_path), Path(self.config_path))

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, name: str) -> None:
        """Serve a file under `board/static/`, including subfolders such as `shots/`."""
        root = STATIC.resolve()
        target = (root / name).resolve()
        if root not in target.parents or not target.is_file():
            self._json(404, {"error": "not found"})
            return
        kind = (
            "text/html; charset=utf-8" if name.endswith(".html")
            else "image/svg+xml" if name.endswith(".svg")
            else "image/png" if name.endswith(".png")
            else "application/octet-stream"
        )
        self._send(200, target.read_bytes(), kind)

    def _json(self, status: int, data) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _payload(self) -> dict | None:
        length = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "body is not JSON"})
            return None

    def _authorised(self) -> bool:
        if not self.token:
            return True
        return self.headers.get("X-Board-Token", "") == self.token

    def _page(self) -> bytes:
        """Serve the page, embedding the token when one is configured.

        Access control here is tailnet membership: only a device on the tailnet can
        load this page. The token keeps scripts and stray POSTs honest.
        """
        raw = (STATIC / "index.html").read_bytes()
        if self.token and self.inject_token:
            meta = f'<meta name="board-token" content="{self.token}">'.encode()
            raw = raw.replace(b"</head>", meta + b"</head>", 1)
        return raw

    def log_message(self, fmt: str, *args) -> None:  # concise
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ------------------------------------------------------------ routes

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, self._page(), "text/html; charset=utf-8")
            return
        if path == "/guide":
            self._send_static("guide.html")
            return
        if path.startswith("/static/"):
            self._send_static(path[len("/static/"):])
            return
        if path == "/healthz":
            store = self._store()
            try:
                summary = store.summary()
                tickets = len(store.list())
            finally:
                store.close()
            self._json(200, {"status": "ok", "tickets": tickets, "summary": summary})
            return
        if path == "/api/catalog":
            config = load_config(Path(self.config_path))
            self._json(200, load_catalog_cached(Path(self.catalog_dir), config=config))
            return
        if path == "/api/projects":
            self._json(200, {"projects": t3_projects(Path(self.t3_db_path))})
            return
        if path == "/api/threads":
            self._json(200, {"threads": t3_threads(Path(self.t3_db_path))})
            return
        if path == "/api/tickets":
            store = self._store()
            try:
                config = load_config(Path(self.config_path))
                models = config.get("models") or {}
                profiles = sorted(models.keys())
                profile_details = [
                    {"name": name, "provider": models[name].get("provider", ""),
                     "model": models[name].get("model", "")}
                    for name in profiles
                ]
                default_profile = (config.get("defaults") or {}).get("profile", "")
                pending = store.pending_approvals()
                self._json(200, {
                    "tickets": store.list_public(),
                    "approvals": pending,
                    "statuses": list(STATUSES),
                    "transitions": {key: sorted(value) for key, value in TRANSITIONS.items()},
                    "profiles": profiles,
                    "defaultProfile": default_profile,
                    "profileDetails": profile_details,
                    "summary": store.summary(),
                    "approvalCount": len(pending),
                    "verification": {
                        "enabled": bool((config.get("verification") or {}).get("enabled", True)),
                        "maxAttempts": int((config.get("verification") or {}).get("maxAttempts", 2) or 2),
                    },
                })
            finally:
                store.close()
            return
        if path.startswith("/api/report/"):
            ticket_id = path.rsplit("/", 1)[-1]
            store = self._store()
            try:
                text = store.report(ticket_id)
            except TicketError as exc:
                self._json(404, {"error": str(exc)})
                return
            finally:
                store.close()
            self._send(200, text.encode(), "text/markdown; charset=utf-8")
            return
        if path.startswith("/api/tickets/") and path.endswith("/events"):
            ticket_id = path.split("/")[3]
            store = self._store()
            try:
                self._json(200, {"events": store.events(ticket_id)})
            except TicketError as exc:
                self._json(404, {"error": str(exc)})
            finally:
                store.close()
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/tickets":
            if not self._authorised():
                self._json(401, {"error": "invalid token"})
                return
            payload = self._payload()
            if payload is None:
                return
            provider = str(payload.get("provider", "")).strip()
            model = str(payload.get("model", "")).strip()
            if provider or model:
                try:
                    resolve_selection(
                        load_catalog_cached(Path(self.catalog_dir), config=load_config(Path(self.config_path))),
                        provider, model,
                    )
                except CatalogError as exc:
                    self._json(409, {"error": str(exc)})
                    return
            store = self._store()
            try:
                ticket = store.add(
                    title=str(payload.get("title", "")),
                    project=str(payload.get("project", "")),
                    body=str(payload.get("body", "")),
                    model_profile=str(payload.get("profile", "")),
                    test_cmd=str(payload.get("test_cmd", "")),
                    priority=int(payload.get("priority", 3) or 3),
                    status=str(payload.get("status", "backlog")),
                    requires_approval=bool(payload.get("requires_approval", False)),
                    attachment_path=str(payload.get("attachment_path", "")),
                    provider=provider,
                    model=model,
                )
                self._json(200, {"ticket": ticket})
            except (TicketError, ValueError) as exc:
                self._json(409, {"error": str(exc)})
            finally:
                store.close()
            return
        if path.startswith("/api/threads/") and path.endswith("/adopt"):
            if not self._authorised():
                self._json(401, {"error": "invalid token"})
                return
            payload = self._payload()
            if payload is None:
                return
            thread_id = path.split("/")[3]
            store = self._store()
            try:
                ticket = store.add(
                    title=str(payload.get("title") or f"Thread {thread_id[:8]}"),
                    project=str(payload.get("project", "")),
                    body=str(payload.get("body", "")),
                    model_profile=str(payload.get("profile", "")),
                    test_cmd=str(payload.get("test_cmd", "")),
                    attachment_path=str(payload.get("attachment_path", "")),
                    provider=str(payload.get("provider", "")),
                    model=str(payload.get("model", "")),
                    status="ready",
                )
                ticket = store.attach_thread(
                    ticket["id"], thread_id,
                    "adopted from a T3 thread — the watcher continues the same thread",
                )
                self._json(200, {"ticket": ticket})
            except TicketError as exc:
                self._json(409, {"error": str(exc)})
            finally:
                store.close()
            return
        if path == "/healthz":
            store = self._store()
            try:
                summary = store.summary()
                tickets = len(store.list())
            finally:
                store.close()
            self._json(200, {"status": "ok", "tickets": tickets, "summary": summary})
            return
        if path == "/api/catalog":
            config = load_config(Path(self.config_path))
            self._json(200, load_catalog_cached(Path(self.catalog_dir), config=config))
            return
        if path == "/api/projects":
            if not self._authorised():
                self._json(401, {"error": "invalid token"})
                return
            payload = self._payload()
            if payload is None:
                return
            title = str(payload.get("title", "")).strip()
            folder = Path(str(payload.get("path", ""))).expanduser()
            if not title or not folder.is_absolute() or not folder.is_dir():
                self._json(400, {"error": "title is required; path must be absolute and exist"})
                return
            config = load_config(Path(self.config_path))
            cli = (config.get("t3") or {}).get("cli", str(Path.home() / ".local/bin/t3"))
            try:
                proc = subprocess.run(
                    [cli, "project", "add", str(folder), "--title", title],
                    capture_output=True, text=True, timeout=60,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
                return
            if proc.returncode != 0:
                self._json(409, {"error": (proc.stderr or proc.stdout or "failed").strip()[:300]})
                return
            self._json(200, {"message": (proc.stdout or "project ditambahkan").strip()[:300],
                             "projects": t3_projects(Path(self.t3_db_path))})
            return
        if path.startswith("/api/approvals/") and path.endswith("/decide"):
            if not self._authorised():
                self._json(401, {"error": "invalid token"})
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                approval_id = int(path.split("/")[3])
            except (json.JSONDecodeError, ValueError):
                self._json(400, {"error": "invalid body or id"})
                return
            store = self._store()
            try:
                approval = store.decide_approval(
                    approval_id, bool(payload.get("approved", False)), "board", str(payload.get("note", ""))
                )
                self._json(200, {"approval": approval})
            except TicketError as exc:
                self._json(409, {"error": str(exc)})
            finally:
                store.close()
            return
        if not (path.startswith("/api/tickets/") and path.endswith("/move")):
            self._json(404, {"error": "not found"})
            return
        if not self._authorised():
            self._json(401, {"error": "invalid token"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "body is not JSON"})
            return
        ticket_id = path.split("/")[3]
        store = self._store()
        try:
            ticket = store.move(
                ticket_id,
                str(payload.get("status", "")),
                note=str(payload.get("note", "")),
                force=bool(payload.get("force", False)),
            )
            self._json(200, {"ticket": ticket})
        except TicketError as exc:
            self._json(409, {"error": str(exc)})
        finally:
            store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--token", default="", help="require X-Board-Token on POST (falls back to env T3_BOARD_TOKEN)")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--t3-db", default=str(T3_DB))
    parser.add_argument("--catalog-dir", default=str(CATALOG_DIR))
    args = parser.parse_args()

    Handler.store_path = Path(args.db)
    Handler.config_path = Path(args.config)
    Handler.token = args.token or __import__("os").environ.get("T3_BOARD_TOKEN", "")
    Handler.t3_db_path = Path(args.t3_db)
    Handler.catalog_dir = Path(args.catalog_dir)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"board on http://{args.host}:{args.port}  (db={args.db})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
