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
import os
import sqlite3
import subprocess
import uuid
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tickets"))

from catalog import CatalogError, load_catalog_cached, resolve_selection  # noqa: E402
from project_test import detect_test_command_info  # noqa: E402
from store import (  # noqa: E402
    CONFIG_PATH,
    DB_PATH,
    STATUSES,
    TRANSITIONS,
    TicketError,
    TicketStore,
    load_config,
    project_defaults,
)

STATIC = Path(__file__).resolve().parent / "static"

# Names only, token-guarded, and never these trees: virtual filesystems and the
# directories whose listing is nobody's business on a tailnet page.
FS_BLOCKED_ROOTS = ("/proc", "/sys", "/dev", "/root", "/boot", "/run")


T3_DB = Path.home() / ".t3/userdata/statev2.sqlite"
CATALOG_DIR = Path.home() / ".t3/caches"


def project_test_command(config: dict, title: str, workspace_root: str) -> dict:
    """A project's configured default wins; otherwise read what the project declares."""
    configured = (project_defaults(config, title) or {}).get("test_cmd", "")
    if configured:
        return {"command": configured, "source": "project default", "needs_setup": False}
    return detect_test_command_info(Path(workspace_root))


def read_heartbeat(store_path: Path) -> dict | None:
    path = Path(store_path).with_name("watcher.json")
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        checked = datetime.fromisoformat(data["at"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None
    age = max(0, int((datetime.now(timezone.utc) - checked).total_seconds()))
    interval = int(data.get("interval") or 0)
    data["ageSeconds"] = age
    data["stale"] = bool(interval and age > max(90, interval * 3))
    return data


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


def t3_threads(db_path: Path, limit: int = 200) -> list[dict]:
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
    max_upload_bytes = 20 * 1024 * 1024
    attachment_dir = str(Path.home() / ".local/state/t3-workflow/attachments")
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

    @staticmethod
    def _fs_listing(raw: str) -> tuple[int, dict]:
        """Names only, token-guarded, and never the trees nobody should list here."""
        home = Path.home().resolve()
        try:
            target = Path(raw or home).expanduser().resolve()
        except OSError as exc:
            return 400, {"error": str(exc)}
        text = str(target)
        if any(text == root or text.startswith(root + "/") for root in FS_BLOCKED_ROOTS):
            return 403, {"error": "a system path is not readable from the board"}
        if any(part.startswith(".") for part in target.parts[1:]):
            return 403, {"error": "hidden paths are not listed"}
        if not target.is_dir():
            return 404, {"error": "not a directory"}
        directories, files = [], []
        try:
            children = sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        except OSError as exc:
            return 400, {"error": str(exc)}
        for child in children:
            if child.name.startswith("."):
                continue
            try:
                if child.is_dir():
                    directories.append({"name": child.name, "path": str(child)})
                elif child.is_file():
                    files.append({"name": child.name, "path": str(child), "size": child.stat().st_size})
            except OSError:
                continue
        return 200, {
            "path": text,
            "parent": str(target.parent) if target != home else "",
            "home": str(home),
            "directories": directories,
            "files": files,
        }

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
        if path == "/api/fs":
            if not self._authorised():
                self._json(401, {"error": "invalid token"})
                return
            query = parse_qs(urlparse(self.path).query)
            status, payload = self._fs_listing((query.get("path") or [""])[0])
            self._json(status, payload)
            return
        if path.startswith("/api/tickets/") and path.endswith("/live"):
            if not self._authorised():
                self._json(401, {"error": "invalid token"})
                return
            ticket_id = path.split("/")[3]
            store = self._store()
            try:
                ticket = store.get(ticket_id)
            except TicketError as exc:
                self._json(404, {"error": str(exc)})
                return
            finally:
                store.close()
            thread_id = (ticket.get("thread_id") or "").strip() or (ticket.get("verify_thread_id") or "").strip()
            if not thread_id:
                self._json(400, {"error": "this ticket has no thread yet"})
                return
            config = load_config(Path(self.config_path))
            cli = (config.get("t3") or {}).get("cli", str(Path.home() / ".local/bin/t3"))
            try:
                proc = subprocess.run(
                    [cli, "thread", "show", thread_id, "--json", "--messages", "12"],
                    capture_output=True, text=True, timeout=25,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                self._json(502, {"error": f"t3 thread show failed: {type(exc).__name__}"})
                return
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip().splitlines()
                self._json(502, {"error": (detail[0] if detail else "t3 thread show failed")[:300]})
                return
            try:
                data = json.loads(proc.stdout)
            except json.JSONDecodeError:
                self._json(502, {"error": "t3 thread show did not return JSON"})
                return
            messages = [
                {"role": item.get("role"), "runId": item.get("runId"),
                 "text": (item.get("text") or "")[:4000]}
                for item in (data.get("messages") or [])
            ]
            thread = data.get("thread") or {}
            self._json(200, {
                "thread_id": thread_id,
                "thread": {
                    "title": thread.get("title"),
                    "status": thread.get("status"),
                    "model": thread.get("model"),
                    "link": thread.get("link"),
                    "runCount": thread.get("runCount"),
                },
                "messages": messages,
                "ticket": {"id": ticket["id"], "status": ticket["status"], "note": ticket["note"]},
            })
            return
        if path == "/api/projects":
            config = load_config(Path(self.config_path))
            items = t3_projects(Path(self.t3_db_path))
            for item in items:
                info = project_test_command(config, item["title"], item["workspace_root"])
                item["test_command"] = info["command"]
                item["test_command_source"] = info["source"]
                item["test_command_needs_setup"] = info["needs_setup"]
            self._json(200, {"projects": items})
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
                titles = {item["title"] for item in t3_projects(Path(self.t3_db_path))}
                tickets = store.list_public()
                for ticket in tickets:
                    project = (ticket.get("project") or "").strip()
                    if project.startswith("/"):
                        ticket["project_missing"] = not Path(project).is_dir()
                    else:
                        # An empty title set means T3 could not be read; say nothing.
                        ticket["project_missing"] = bool(titles) and project not in titles
                self._json(200, {
                    "tickets": tickets,
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
                    "projectDefaults": config.get("projects") or {},
                    "watcher": read_heartbeat(Path(self.store_path)),
                })
            finally:
                store.close()
            return
        if path.startswith("/api/report/"):
            ticket_id = path.rsplit("/", 1)[-1]
            query = parse_qs(urlparse(self.path).query)
            as_html = (query.get("format") or [""])[0].lower() == "html"
            store = self._store()
            kind = "text/html; charset=utf-8" if as_html else "text/markdown; charset=utf-8"
            try:
                text = store.report_html(ticket_id) if as_html else store.report(ticket_id)
            except TicketError as exc:
                # A deleted ticket keeps its report: the file is the evidence that outlives
                # the row, and the delete path writes it before removing anything.
                reports = Path(os.environ.get(
                    "T3_REPORTS_DIR", Path.home() / ".local/state/t3-workflow/reports"
                ))
                fallback = reports / f"{ticket_id}.{'html' if as_html else 'md'}"
                if fallback.is_file():
                    self._send(200, fallback.read_bytes(), kind)
                else:
                    self._json(404, {"error": str(exc)})
                return
            finally:
                store.close()
            self._send(200, text.encode(), kind)
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
        if path == "/api/watcher/run":
            if not self._authorised():
                self._json(401, {"error": "invalid token"})
                return
            try:
                proc = subprocess.run(
                    [sys.executable, str(ROOT / "watcher.py"), "--once",
                     "--db", str(self.store_path), "--config", str(self.config_path)],
                    capture_output=True, text=True, timeout=600,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
                return
            output = ((proc.stdout or "") + (proc.stderr or "")).strip()
            self._json(200 if proc.returncode == 0 else 409, {
                "ok": proc.returncode == 0,
                "output": output[-4000:],
                "heartbeat": read_heartbeat(Path(self.store_path)),
            })
            return
        if path == "/api/attachments":
            if not self._authorised():
                self._json(401, {"error": "invalid token"})
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length <= 0:
                self._json(400, {"error": "empty upload"})
                return
            if length > self.max_upload_bytes:
                self._json(413, {"error": "file is larger than 20 MB"})
                return
            raw_name = unquote(self.headers.get("X-Filename", "")).strip()
            name = Path(raw_name).name
            if not name or name.startswith("."):
                self._json(400, {"error": "a filename is required"})
                return
            target_dir = Path(os.environ.get("T3_ATTACHMENTS_DIR", self.attachment_dir))
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / f"{uuid.uuid4().hex[:8]}-{name}"
                target.write_bytes(self.rfile.read(length))
            except OSError as exc:
                self._json(500, {"error": str(exc)})
                return
            self._json(200, {"path": str(target), "name": name, "size": length})
            return
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
                project = str(payload.get("project", ""))
                config = load_config(Path(self.config_path))
                defaults = project_defaults(config, project)
                detected = ""
                if not project.startswith("/"):
                    match = next(
                        (item for item in t3_projects(Path(self.t3_db_path))
                         if item["title"].lower() == project.lower()),
                        None,
                    )
                    if match:
                        info = project_test_command(config, match["title"], match["workspace_root"])
                        if info["command"] and not info["needs_setup"]:
                            detected = info["command"]
                ticket = store.add(
                    title=str(payload.get("title", "")),
                    project=project,
                    body=str(payload.get("body", "")),
                    model_profile=str(payload.get("profile", "")) or defaults.get("profile", ""),
                    test_cmd=(str(payload.get("test_cmd", "")) or defaults.get("test_cmd", "")
                              or detected),
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
            for project in t3_projects(Path(self.t3_db_path)):
                try:
                    if Path(project["workspace_root"]).expanduser().resolve() == folder.resolve():
                        self._json(409, {
                            "error": f"that folder is already the project '{project['title']}' in T3",
                        })
                        return
                except OSError:
                    continue
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
                detail = (proc.stderr or proc.stdout or "failed").strip().splitlines()
                self._json(409, {"error": (detail[0] if detail else "failed")[:300]})
                return
            self._json(200, {"message": (proc.stdout or "project registered").strip().splitlines()[0][:300],
                             "projects": t3_projects(Path(self.t3_db_path))})
            return
        if path.startswith("/api/projects/") and path.endswith("/remove"):
            if not self._authorised():
                self._json(401, {"error": "invalid token"})
                return
            payload = self._payload()
            if payload is None:
                return
            project_id = path.split("/")[3]
            projects = t3_projects(Path(self.t3_db_path))
            project = next((item for item in projects if item["project_id"] == project_id), None)
            if project is None:
                self._json(404, {"error": "unknown project"})
                return
            if str(payload.get("confirm", "")).strip() != project["title"]:
                self._json(400, {"error": "confirm must be the exact project title"})
                return
            config = load_config(Path(self.config_path))
            cli = (config.get("t3") or {}).get("cli", str(Path.home() / ".local/bin/t3"))
            try:
                proc = subprocess.run(
                    [cli, "project", "remove", project_id, "--force"],
                    capture_output=True, text=True, timeout=120,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
                return
            if proc.returncode != 0:
                self._json(409, {"error": (proc.stderr or proc.stdout or "failed").strip()[:300]})
                return
            self._json(200, {
                "removed": {"project_id": project_id, "title": project["title"]},
                "projects": t3_projects(Path(self.t3_db_path)),
            })
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
        editing = path.startswith("/api/tickets/") and (
            path.endswith("/edit") or path.endswith("/delete")
        )
        if not editing and not (path.startswith("/api/tickets/") and path.endswith("/move")):
            self._json(404, {"error": "not found"})
            return
        if editing:
            if not self._authorised():
                self._json(401, {"error": "invalid token"})
                return
            payload = self._payload()
            if payload is None:
                return
            ticket_id = path.split("/")[3]
            store = self._store()
            try:
                if path.endswith("/delete"):
                    if not payload.get("confirm"):
                        self._json(400, {"error": "deleting needs confirm: true"})
                        return
                    self._json(200, {"deleted": store.delete(ticket_id)})
                    return
                provider = str(payload.get("provider", "")).strip()
                model = str(payload.get("model", "")).strip()
                if provider or model:
                    try:
                        resolve_selection(
                            load_catalog_cached(
                                Path(self.catalog_dir),
                                config=load_config(Path(self.config_path)),
                            ),
                            provider, model,
                        )
                    except CatalogError as exc:
                        self._json(409, {"error": str(exc)})
                        return
                fields = {key: payload[key] for key in store.EDITABLE_FIELDS if key in payload}
                ticket = store.update(ticket_id, force=bool(payload.get("force", False)), **fields)
                self._json(200, {"ticket": ticket})
            except TicketError as exc:
                self._json(409, {"error": str(exc)})
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            finally:
                store.close()
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
