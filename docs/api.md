# Board HTTP API

The board is a single Python file (`board/server.py`) serving one page and a small JSON API. It binds the Tailscale interface by default; `GET` is open to anyone on the tailnet, every `POST` requires the token.

```
POST /api/tickets                      create a ticket
POST /api/tickets/<id>/move            move a ticket
POST /api/approvals/<id>/decide        approve or reject
POST /api/projects                     register a T3 project
POST /api/threads/<thread>/adopt       adopt a T3 thread as a ticket

GET  /                                 the board page
GET  /static/<file>                    logo, favicon, touch icon
GET  /api/tickets                      tickets, approvals, profiles, counts
GET  /api/tickets/<id>/events          the ticket's event history
GET  /api/report/<id>                  the ticket's markdown report
GET  /api/catalog                      harnesses, models, presets, defaults
GET  /api/projects                     T3 projects
GET  /api/threads                      recent T3 threads
```

Authentication: `X-Board-Token: <token>` on every `POST`. The token comes from `--token`, or `T3_BOARD_TOKEN` in the environment (the systemd unit reads it from `~/.config/t3-workflow/board.env`). A missing or wrong token returns `401 {"error": "invalid token"}`.

Start it:

```bash
python3 board/server.py --host 100.124.184.116 --port 8787 --token "$(grep -o '[0-9a-f]\{32\}' ~/.config/t3-workflow/board.env)"
```

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

`proof` is deliberately absent from this response — it can contain build logs or environment detail. Use `GET /api/report/<id>` or the CLI to read it.

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
to `defaults.profile`.

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

Validation: `title` non-empty, `path` absolute and an existing directory (`400` otherwise), then `t3 project add <path> --title <title>`. A non-zero exit from the CLI returns `409` with its stderr.

## GET /api/projects

```json
{ "projects": [ { "project_id": "cf3876ca-…", "title": "kelanach", "workspace_root": "/home/kelanach" } ] }
```

Read from T3's `statev2.sqlite`, `deleted_at is null`, ordered by title.

## GET /api/threads

The 40 most recent threads, with the latest run's status.

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
