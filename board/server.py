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
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tickets"))

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


class Handler(BaseHTTPRequestHandler):
    store_path = DB_PATH
    config_path = CONFIG_PATH
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

    def _json(self, status: int, data) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

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

    def log_message(self, fmt: str, *args) -> None:  # ringkas
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ------------------------------------------------------------ routes

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, self._page(), "text/html; charset=utf-8")
            return
        if path.startswith("/static/"):
            name = Path(path).name  # basename saja: tidak ada traversal
            target = STATIC / name
            if target.is_file():
                kind = (
                    "text/html; charset=utf-8" if name.endswith(".html")
                    else "image/svg+xml" if name.endswith(".svg")
                    else "image/png" if name.endswith(".png")
                    else "application/octet-stream"
                )
                self._send(200, target.read_bytes(), kind)
            else:
                self._json(404, {"error": "not found"})
            return
        if path == "/api/tickets":
            store = self._store()
            try:
                config = load_config(Path(self.config_path))
                profiles = sorted((config.get("models") or {}).keys())
                pending = store.pending_approvals()
                self._json(200, {
                    "tickets": store.list(),
                    "approvals": pending,
                    "statuses": list(STATUSES),
                    "transitions": {key: sorted(value) for key, value in TRANSITIONS.items()},
                    "profiles": profiles,
                    "summary": store.summary(),
                    "approvalCount": len(pending),
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
        if path.startswith("/api/approvals/") and path.endswith("/decide"):
            if not self._authorised():
                self._json(401, {"error": "token tidak valid"})
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                approval_id = int(path.split("/")[3])
            except (json.JSONDecodeError, ValueError):
                self._json(400, {"error": "body atau id tidak valid"})
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
            self._json(401, {"error": "token tidak valid"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "body bukan JSON"})
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
    parser.add_argument("--token", default="", help="wajibkan header X-Board-Token untuk POST (fallback: env T3_BOARD_TOKEN)")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args()

    Handler.store_path = Path(args.db)
    Handler.config_path = Path(args.config)
    Handler.token = args.token or __import__("os").environ.get("T3_BOARD_TOKEN", "")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"kanban di http://{args.host}:{args.port}  (db={args.db})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("berhenti")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
