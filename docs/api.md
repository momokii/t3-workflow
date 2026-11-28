# Board HTTP API

The board is a single Python file (`board/server.py`) serving one page and a small JSON API. It binds the Tailscale interface by default; `GET` is open to anyone on the tailnet, every `POST` requires the token.

```
POST /api/tickets                      create a ticket
POST /api/tickets/<id>/move            move a ticket
POST /api/tickets/<id>/edit            edit a ticket's fields
POST /api/tickets/<id>/delete          delete a ticket (report kept)
POST /api/approvals/<id>/decide        approve or reject
POST /api/projects                     register a T3 project
POST /api/projects/<id>/remove         remove a project and all of its threads (confirm = title)
POST /api/threads/<thread>/adopt       adopt a T3 thread as a ticket

GET  /                                 the board page
GET  /guide                            how it works: lifecycle, test command, models, limits
GET  /static/<file>                    logo, favicon, touch icon
GET  /api/tickets                      tickets, approvals, profiles, counts
GET  /api/tickets/<id>/events          the ticket's event history
GET  /api/report/<id>                  the ticket's markdown report
GET  /api/catalog                      harnesses, models, presets, defaults
GET  /api/projects                     T3 projects
GET  /api/threads                      recent T3 threads (up to 200)
GET  /api/fs?path=<dir>                folder picker: names under a directory (token)
GET  /api/tickets/<id>/live            thread status and last messages (token)
POST /api/watcher/run                  run one watcher step now (token)
POST /api/attachments                  upload a file for a ticket (token, 20 MB)
```

Authentication: `X-Board-Token: <token>` on every `POST`, and on `GET /api/fs` — that route lists
filenames from this machine, so it is not open like the rest of `GET`. The token comes from `--token`, or
`T3_BOARD_TOKEN` in the environment (the systemd unit reads it from
`~/.config/t3-workflow/board.env`). A missing or wrong token returns `401 {"error": "invalid token"}`.

Start it:

```bash
python3 board/server.py --host 100.124.184.116 --port 8787 --token "$(grep -o '[0-9a-f]\{32\}' ~/.config/t3-workflow/board.env)"
```

---

## GET /guide

A standalone page served from `board/static/guide.html`: the lifecycle and who moves a ticket, what the
test command is for and how its exit code decides, the harness/model resolution order, priorities,
approvals, adoption, limits and failure behaviour, Telegram commands, where the data lives, and the
gaps that are not built yet. It is linked from the board header. `200 text/html`, `404` if the file is
missing.

`GET /api/catalog` is served from a snapshot cached on the cache directory's mtime and file sizes, so a
provider probe that rewrites `~/.t3/caches/*.json` invalidates it; `loadedAt` therefore reports when the
snapshot was read, not when the request arrived.

---

## GET /api/tickets

Everything the board needs in one call.

```bash
curl -s http://127.0.0.1:8787/api/tickets | python3 -m json.tool
```

```json
{
  "tickets": [
    {
      "id": "T-0001",
      "title": "Pilot: prove the ticket loop",
      "status": "done",
      "priority": 2,
      "project": "t3-workflow",
      "model_profile": "fast",
      "test_cmd": "make check",
      "thread_id": "mcp:72b322c1-…",
      "branch": "ticket/t-0001-pilot-prove-the-ticket-loop",
      "pr_url": "",
      "report_path": "/home/…/reports/T-0001.md",
      "claimed_at": "2026-11-25T15:20:16+00:00",
      "claimed_by": "watcher",
      "created_at": "2026-11-25T15:14:43+00:00",
      "updated_at": "2026-11-25T15:48:19+00:00",
      "note": "release: ticket/… pushed to origin"
    }
  ],
  "approvals": [],
  "approvalCount": 0,
  "summary": { "done": 1 },
  "statuses": ["backlog", "ready", "in-progress", "testing", "review", "done", "blocked"],
  "transitions": { "backlog": ["blocked", "ready"], "done": [] },
  "profiles": ["build", "cheap", "deep", "fast"],
  "defaultProfile": "fast"
}
```

Each ticket also carries `thread_state` and `review_state` (from T3's latest run and turn item:
`status`, `last_type`, `last_state`, `last_at`) so a card can say "thread running · command execution ·
3s ago" without another request. `null` means the thread has no run yet or T3's database could not be
read; the card then says the thread is starting rather than guessing a failure.

`proof` is deliberately absent from this response — it can contain build logs or environment detail. The
review is trimmed the same way: a ticket exposes `verify_verdict`, `verify_attempt`, `verify_provider`,
`verify_model`, `verify_started_at` and `verify_finished_at` so a card can show the outcome, while
`verify_findings` (reviewer prose), `verify_baseline` (a tree fingerprint) and `verify_run_id` (an internal
anchor) stay private. The response also carries `verification: {"enabled": bool, "maxAttempts": int}` so the
board can render `2/2` without guessing. Use `GET /api/report/<id>` or the CLI to read the findings.

## GET /api/catalog

Everything the model pickers need, read from `~/.t3/caches/<instanceId>.json`.

```bash
curl -s http://127.0.0.1:8787/api/catalog | python3 -m json.tool | head -30
```

```json
{
  "providers": [
    {
      "instanceId": "pi", "displayName": "Pi", "driver": "pi",
      "enabled": true, "installed": true, "status": "ready",
      "checkedAt": "2026-11-26T16:34:25.065Z",
      "models": [
        { "slug": "opencode-go/deepseek-v4.1-flash", "name": "DeepSeek V4.1 Flash 1017",
          "subProvider": "opencode-go", "isDefault": false,
          "options": [ { "id": "thinking", "label": "Thinking",
                         "values": [ { "id": "low", "label": "Low" }, { "id": "high", "label": "High" } ] } ] }
      ]
    }
  ],
  "presets": [ { "name": "fast", "provider": "pi", "model": "opencode-go/deepseek-v4.1-flash" } ],
  "defaultProfile": "fast",
  "source": "/home/kelanach/.t3/caches",
  "loadedAt": "2026-11-26T…"
}
```

Providers that T3 reports as `enabled: false` are still listed (with their models) so the API can
explain why a pick fails; the board only offers the enabled ones.

## POST /api/tickets

```bash
curl -s -X POST http://127.0.0.1:8787/api/tickets \
  -H "X-Board-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"title":"Add a filter","project":"t3-workflow","profile":"fast",
       "test_cmd":"make check","priority":2,"requires_approval":false}'
```

| Field | Required | Default |
|---|---|---|
| `title` | yes | — |
| `project` | yes | — |
| `profile` | no | `defaults.profile`; a preset name |
| `provider` | no | harness instance id; must be enabled when sent |
| `model` | no | model slug from that harness; must exist in the catalogue |
| `test_cmd` | no | empty (the test stage will refuse to advance) |
| `priority` | no | `3` |
| `status` | no | `backlog` |
| `requires_approval` | no | `false` |

Returns `{"ticket": {…}}`. Rejections with `409`:

- unknown preset — `model profile '<name>' is not in config`;
- unknown or disabled harness — `unknown provider '<id>'` / `is not enabled in T3`;
- model missing from that harness — `model '<slug>' is not offered by provider '<id>'`;
- a provider without a model — `provider and model must be set together`.

An explicit `provider`/`model` pair takes precedence over `profile`; a ticket with neither falls back
to `defaults.profile`. A project may carry defaults (`projects.<title>` in config); `test_cmd`, `profile`,
`provider` and `model` left out of the request are filled from them, and `GET /api/tickets` returns the
map as `projectDefaults` so the board can prefill its dialog.

## GET /api/catalog

Everything the model pickers need, read from `~/.t3/caches/<instanceId>.json`.

```bash
curl -s http://127.0.0.1:8787/api/catalog | python3 -m json.tool | head -30
```

```json
{
  "providers": [
    {
      "instanceId": "pi", "displayName": "Pi", "driver": "pi",
      "enabled": true, "installed": true, "status": "ready",
      "checkedAt": "2026-11-26T16:34:25.065Z",
      "models": [
        { "slug": "opencode-go/deepseek-v4.1-flash", "name": "DeepSeek V4.1 Flash 1017",
          "subProvider": "opencode-go", "isDefault": false,
          "options": [ { "id": "thinking", "label": "Thinking",
                         "values": [ { "id": "low", "label": "Low" }, { "id": "high", "label": "High" } ] } ] }
      ]
    }
  ],
  "presets": [ { "name": "fast", "provider": "pi", "model": "opencode-go/deepseek-v4.1-flash" } ],
  "defaultProfile": "fast",
  "source": "/home/kelanach/.t3/caches",
  "loadedAt": "2026-11-26T…"
}
```

Providers that T3 reports as `enabled: false` are still listed (with their models) so the API can
explain why a pick fails; the board only offers the enabled ones.

## POST /api/tickets/<id>/edit

```bash
curl -s -X POST http://127.0.0.1:8787/api/tickets/T-0001/edit \
  -H "X-Board-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"title":"Better title","test_cmd":"make check","priority":2,"force":true}'
```

Editable fields: `title`, `body`, `test_cmd`, `priority`, `requires_approval`, `attachment_path`,
`model_profile`, `provider`, `model`, `project`. A `done` ticket is refused (`409`); a ticket a stage is
working on (`in-progress`, `testing`, `review`) needs `"force": true`, which is the human override and is
recorded as an `edited` event. An explicit provider/model pair is validated against the catalogue like
creation.

## POST /api/tickets/<id>/delete

```bash
curl -s -X POST http://127.0.0.1:8787/api/tickets/T-0001/delete \
  -H "X-Board-Token: $TOKEN" -H 'Content-Type: application/json' -d '{"confirm": true}'
```

Returns `200 {"deleted": {"id": "T-0001", …}}`. Without `"confirm": true` it returns `400`; the report is
written before deletion, a `deleted` event stores a snapshot of the row, and `GET /api/report/<id>` keeps
serving the file afterwards.

## POST /api/tickets/<id>/move

```bash
curl -s -X POST http://127.0.0.1:8787/api/tickets/T-0001/move \
  -H "X-Board-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"status":"ready","note":"reviewed"}'
```

| Field | Meaning |
|---|---|
| `status` | target state |
| `note` | replaces the ticket's note |
| `force` | `true` skips the state machine and records a `forced` event |

Responses: `200 {"ticket": {…}}`, `409 {"error": "transition … is not allowed (allowed: …)"}`, `401` without the token.

## POST /api/approvals/<id>/decide

```bash
curl -s -X POST http://127.0.0.1:8787/api/approvals/1/decide \
  -H "X-Board-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"approved":true,"note":"go"}'
```

The first decision wins; a second one returns `409 {"error": "approval 1 is already approved — second answer ignored"}`.

## GET /api/report/<id>

Returns `text/markdown`: status, project, profile, thread, test command, description, proof, approval state and the full event table. `404` for an unknown ticket.

`?format=json` returns the named sections the board's tabs render: `{"sections": [{"id", "title",
"lead", "html"}]}` (Summary, Test command, What was asked, Evidence, Independent review, History).
`?format=html` returns the same report as an HTML fragment, built by the server with every ticket field escaped. The board inserts it into the report dialog; the release also writes a standalone `T-xxxx.html` page next to the markdown, and that page is what Telegram receives as the report document.

```bash
curl -s http://127.0.0.1:8787/api/report/T-0001 | head -20
```

## POST /api/projects

Registration goes through T3 itself.

```bash
curl -s -X POST http://127.0.0.1:8787/api/projects \
  -H "X-Board-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"title":"my-project","path":"/home/kelanach/projects/my-project"}'
```

Validation: `title` non-empty, `path` absolute and an existing directory (`400` otherwise), and the folder
must not already be a project (`409` with `that folder is already the project '<title>' in T3`). It then runs
`t3 project add <path> --title <title>`; a non-zero exit returns `409` with the first line of its output.

## POST /api/projects/<id>/remove

Irreversible, and the only way to delete threads from here: T3's CLI has no per-thread delete, and
`t3 project remove --force` deletes the project together with every thread in it.

```bash
curl -s -X POST http://127.0.0.1:8787/api/projects/a5655872-…/remove \
  -H "X-Board-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"confirm": "e2e-loop-1795834852"}'
```

`confirm` must equal the project's title exactly, so a stray click cannot delete anything. Responses:
`200 {"removed": {"project_id", "title"}, "projects": […]}`; `400` when `confirm` is missing or differs;
`401` without the token; `404` for an unknown id; `409` when the CLI refuses. Tickets that pointed at the
project are kept, their reports stay on disk, and their cards show **project missing**.

## GET /api/projects

```json
{ "projects": [ { "project_id": "cf3876ca-…", "title": "kelanach", "workspace_root": "/home/kelanach" } ] }
```

Read from T3's `statev2.sqlite`, `deleted_at is null`, ordered by title. Each entry also carries the
`test_command` the project declares (`Makefile` target, a `go.mod` build/vet/test chain, a
`package.json` script, `Cargo.toml`, pytest configuration resolved through the project's `.venv`, or a
`tests/` directory), its `test_command_source`, and `test_command_needs_setup` when the declaration needs
a database, Docker, a browser or a watch process. A configured `projects.<title>.test_cmd` wins and
reports `project default`. An omitted `test_cmd` in `POST /api/tickets` is filled from the same order,
but never from a command flagged `needs_setup`.

## GET /api/tickets/<id>/live

The live modal's source: every thread the ticket owns — `implementation` and `review` — with each one's
status, model, last twelve messages and last twenty turn items, read from T3's database through its
indexes. Token required. `400` when the ticket has no thread, `404` for an unknown ticket; a thread that
cannot be read carries its own `error` instead of hiding the other.

```json
{ "threads": [
    { "role": "implementation", "thread_id": "mcp:…",
      "thread": { "title": "T-0003 · Brain Research", "status": "running",
                  "model": "opencode-go/deepseek-v4.1-flash", "runCount": 1 },
      "messages": [ { "role": "assistant", "at": "2026-11-28T09:09:50Z", "text": "…" } ],
      "activity": [ { "type": "command_execution", "status": "completed",
                      "at": "2026-11-28T09:09:51Z", "title": "pytest -q", "tool": "shell" } ] } ] }
```

## POST /api/tickets/<id>/answer

Answers a thread that stopped with `waiting` (the agent asked a question). Body:
`{"thread": "implementation" | "review", "text": "…"}`. The text is sent with `t3 thread send`, an
`answered` event is recorded, and a `blocked` ticket is moved to `ready` and claimed immediately — so the
watcher does **not** send the ticket prompt a second time. `409` when the ticket is not `blocked`,
`in-progress` or `review`, or when the CLI refuses; `400` for empty text or no thread. Token required.

`messages` only changes when an assistant turn closes; `activity` (turn items) is what grows during a
running step, which is why the modal shows both.

## POST /api/watcher/run

Runs `watcher.py --once` with the board's database and config, then answers with the last 4 000
characters of its output and the fresh heartbeat (`200`; `409` when the step reported an error). It is the
header's **Check now**: the claim is atomic, so this cannot start a ticket twice. Token required.

## POST /api/attachments

The drop zone's target. Send the file bytes as the body, the name URL-encoded in `X-Filename`, and the
board token. The file is copied to `~/.local/state/t3-workflow/attachments/<id>-<name>` (override with
`T3_ATTACHMENTS_DIR`); the response is `{"path", "name", "size"}` and the path is what the ticket passes
to the agent. Limits: `20 MB` (`413`), empty body (`400`), missing or dot-leading name (`400`), token
required (`401`).

## GET /api/fs

The folder picker behind the attachment and project path fields.

```bash
curl -s "http://127.0.0.1:8787/api/fs?path=$HOME" -H "X-Board-Token: $TOKEN"
```

```json
{ "path": "/home/kelanach", "parent": "/home",
  "home": "/home/kelanach",
  "directories": [ { "name": "projects", "path": "/home/kelanach/projects" } ],
  "files": [ { "name": "notes.md", "path": "/home/kelanach/notes.md", "size": 1234 } ] }
```

`path` defaults to the home directory and accepts `~`. Names starting with `.` are hidden from the
listing. `/proc`, `/sys`, `/dev`, `/root`, `/boot`, `/run` and any path containing a hidden directory are
refused. Errors: `401` without the token, `403` for those paths, `404` when the path is not a directory,
`400` when it cannot be read. Only names and sizes are returned — never file contents.

## GET /api/threads

The 200 most recent threads, with the latest run's status.

```json
{
  "threads": [
    { "thread_id": "thread:delegated-task:…", "title": "Refresh screenshots",
      "project": "cve-injector", "workspace_root": "/home/…/cve-injector",
      "status": "completed", "created_at": "2026-11-25T…" }
  ]
}
```

## POST /api/threads/<thread>/adopt

Turns an existing thread into an `in-progress` ticket, so work already running in T3 can be handled from the board.

```bash
curl -s -X POST "http://127.0.0.1:8787/api/threads/$THREAD_ID/adopt" \
  -H "X-Board-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"title":"Refresh screenshots","project":"cve-injector","profile":"fast"}'
```

The resulting ticket has `status=in-progress`, `thread_id` set to the adopted thread, and the note `adopted from a T3 thread`.

## GET /api/tickets/<id>/events

```json
{ "events": [ { "event_id": 1, "ticket_id": "T-0001", "at": "…", "kind": "created", "detail": "status=backlog profile=fast" } ] }
```

## Errors

| Status | Meaning |
|---|---|
| `400` | malformed JSON, missing field, invalid path |
| `401` | missing or wrong `X-Board-Token` |
| `404` | unknown ticket, report, or static file |
| `409` | the store refused the operation (invalid transition, unknown profile, second decision) |

All error bodies are `{"error": "<message>"}`; the message is written for the operator, not for a machine.
The examples above quote those messages verbatim, so a few are not in English —
that is the string the service returns, not a translation of it.
