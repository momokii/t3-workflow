# t3-workflow

<img src="board/static/logo.svg" alt="" width="56" height="56">

[![check](https://github.com/momokii/t3-workflow/actions/workflows/check.yml/badge.svg)](https://github.com/momokii/t3-workflow/actions/workflows/check.yml)

Ticket-driven automation for [T3 Code](https://github.com/): a ticket becomes an agent thread, the thread's work is tested with evidence, and a passing ticket is pushed as its own branch — with a local kanban board and a Telegram bridge to drive it from a phone.

```
backlog ──► ready ──► in-progress ──► testing ──► review ──► done
                     (continue the     (watcher    (independent    (branch
                      linked thread     runs tests)  review, then    + push)
                          │              │            release)         │
                    t3 thread     test command     VERDICT: ACCEPT   markdown
                       start      + exit code      gate              report
```

Everything runs on one machine, without a daemon on the target side of T3 and without third-party Python packages: the three services are standard-library scripts under `systemd --user`.

## Contents

- [What it does](#what-it-does)
- [Quick start](#quick-start)
- [Services](#services)
- [Commands](#commands)
- [Configuration](#configuration)
- [Lifecycle and states](#lifecycle-and-states)
- [Release and push policy](#release-and-push-policy)
- [Limits and budgets](#limits-and-budgets)
- [Data, backups and reports](#data-backups-and-reports)
- [Operations](#operations)
- [Security model](#security-model)
- [Testing](#testing)
- [Repository layout](#repository-layout)
- [Documentation](#documentation)
- [Known limitations](#known-limitations)

## What it does

| Piece | Responsibility |
|---|---|
| `tickets/store.py` | SQLite source of truth: tickets, allowed transitions, atomic claim, approvals, proofs, reports, edits and deletions |
| `watcher.py` | Four stages: start a thread for `ready` tickets, test `testing` tickets, review `review` tickets with a fresh thread, release on `VERDICT: ACCEPT` |
| `bridge/t3_bridge.py` | Telegram ⇄ T3 over long polling: read, send, create tickets, decide approvals |
| `board/` | Local kanban on the tailnet: drag and drop, per-card move control, approvals, ticket and project creation, thread adoption |
| `scripts/alert.py` | One Telegram message when a `t3-*` unit fails |

T3 keeps owning threads and projects; this repository owns the queue, the evidence and the release step. The board reads T3's `statev2.sqlite` to list projects and recent threads, and can adopt an existing thread as an `in-progress` ticket.

## Quick start

Requirements: Linux, Python 3.12 (standard library only), a running T3 Code server, and `t3` on `PATH`.

```bash
git clone https://github.com/momokii/t3-workflow.git
cd t3-workflow

# 1. configuration (never committed)
mkdir -p ~/.config/t3-workflow
cp bridge/config.example.json ~/.config/t3-workflow/config.json   # then edit
chmod 600 ~/.config/t3-workflow/config.json

# 2. database
python3 tickets/cli.py init

# 3. checks
make check

# 4. run by hand
python3 tickets/cli.py list
python3 watcher.py --once --dry-run
python3 board/server.py --host 127.0.0.1 --port 8787
```

The Telegram bridge needs a bot token and at least one allowlisted chat id; see [Configuration](#configuration). Without them the bridge exits with a clear message and the rest still works.

## Run it in Docker

The board and both workers ship as one image with a compose file:

```bash
cp deploy/docker/.env.example .env     # HOME, PROJECTS_ROOT, T3_BOARD_HOST
docker compose up -d --build board     # board on http://$T3_BOARD_HOST:$T3_BOARD_PORT
docker compose --profile automation up -d --build   # + bridge + watcher
deploy/docker/smoke.sh                 # health, endpoints, token enforcement
```

The containers use the host network and mount the host's T3 data and project paths, because T3
identifies a project by its absolute path and the CLI reaches its server on loopback. Read
[docs/docker.md](docs/docker.md) before switching: run either the compose services or the systemd
units, not both.

## Services

Installed as `systemd --user` units. The repository ships the templates and a renderer, so a fresh checkout can reproduce the exact units:

```bash
deploy/install.sh --print                          # inspect without writing
deploy/install.sh --host 100.124.184.116 --port 8787
systemctl --user daemon-reload
systemctl --user enable --now t3-bridge t3-watcher t3-board t3-backup.timer
```

`deploy/install.sh` substitutes the repository path, the Python interpreter, and the board host/port. It writes files and stops there: nothing is enabled or started for you.

| Unit | What it runs | Schedule |
|---|---|---|
| `t3-bridge.service` | `bridge/t3_bridge.py` | continuous long polling |
| `t3-watcher.service` | `watcher.py --loop` | every `watcher.intervalSeconds` |
| `t3-board.service` | `board/server.py --host <tailnet-ip> --port 8787` | continuous |
| `t3-backup.service` + `t3-backup.timer` | `tickets/cli.py backup --keep 7` | daily, 03:20 local, persistent |
| `t3-alert@.service` | `scripts/alert.py %i` | invoked through `OnFailure=` |

Every long-running unit sets `OnFailure=t3-alert@%N.service` **in the `[Unit]` section** — `systemd` ignores the directive under `[Service]`.

```bash
systemctl --user status t3-watcher
journalctl --user -u t3-watcher -f
```

## Commands

### From Telegram

| Command | Effect |
|---|---|
| `/projects` | list T3 projects |
| `/threads <project>` | list a project's threads with ids and status |
| `/ctx <thread> [n]` | title, status, model, link and the last n messages |
| `/send <thread or project> <text>` | send a message; a busy thread queues it as its next turn |
| `/new <project> <text>` | start a thread in a project |
| `/tickets [status]` | list tickets |
| `/ticket <project> <text>` | create a ticket in `backlog` |
| `/pending` | approvals waiting for a decision |
| `/approve <id> [note]`, `/reject <id> [note]` | decide once; a second decision is rejected |
| `/id` | the chat id, for the allowlist |
| `/help` | the list above |

`/send` and `/ctx` accept a thread id (`UUID`, optionally prefixed with `mcp:`) or a project name that has exactly one thread; when a project has several threads the reply lists their ids.

### From the board

- **＋ Ticket** — title, project, **harness and model chosen from T3's live catalogue** (or a preset
  that fills both), context, optional attachment path, test command, priority (1 urgent … 5 later),
  optional approval gate.
- **Register project** — writes through `t3 project add`; the path must be absolute and exist.
- **Adopt thread** — turns an existing T3 thread into a `ready` ticket that keeps the thread; the
  watcher sends the ticket prompt into that same thread instead of starting a new one.
- Drag a card between columns, or use the card's **Move** control (a single-pointer alternative required by WCAG 2.2 SC 2.5.7). **Force move** bypasses the state machine and is recorded as a `forced` event.
- **Report** downloads the ticket's markdown report.

### From the terminal

```bash
python3 tickets/cli.py init
python3 tickets/cli.py profiles
python3 tickets/cli.py add "Title" --project t3-workflow --profile fast --test "make check"
python3 tickets/cli.py list [--status ready]
python3 tickets/cli.py show T-0001
python3 tickets/cli.py move T-0001 ready [--note "..."] [--force]
python3 tickets/cli.py report T-0001 [--out reports/T-0001.md]
python3 tickets/cli.py backup [--out DIR] [--keep 7]

python3 watcher.py --once [--dry-run]
python3 watcher.py --loop [--interval 120]

make snapshot      # regenerate STATE.md (git, services, tickets, catalogue)
make selftest      # bridge config check + model profiles
make dry-run       # one watcher step, no side effects
```

## Configuration

`~/.config/t3-workflow/config.json`, mode `600`. `bridge/config.example.json` documents the Telegram side.

```json
{
  "models": {
    "fast":  { "provider": "pi", "model": "opencode-go/deepseek-v4.1-flash",
               "options": [{ "id": "thinking", "value": "max" }] },
    "deep":  { "provider": "pi", "model": "opencode-go/qwen3.8-max" },
    "cheap": { "provider": "pi", "model": "opencode-go/claude-haiku-5-5" },
    "build": { "provider": "opencode", "model": "opencode/big-pickle" }
  },
  "defaults": { "profile": "fast" },
  "watcher": { "enabled": true, "intervalSeconds": 120, "maxConcurrent": 1, "worker": "watcher" },
  "limits":  { "maxInProgressMinutes": 180 },
  "release": { "enabled": true, "remote": "origin", "mode": "branch", "baseBranch": "main" },
  "t3":      { "cli": "/home/kelanach/.local/bin/t3", "dbPath": "/home/kelanach/.t3/userdata/statev2.sqlite" }
}
```

| Key | Meaning |
|---|---|
| `models.<name>` | a preset: provider instance id + model slug; only a human edits this file |
| *(per ticket)* | `provider` + `model` chosen from `GET /api/catalog`; they override the preset |
| `defaults.profile` | profile used when a ticket does not name one |
| `watcher.enabled` | `false` pauses the loop without touching systemd |
| `watcher.intervalSeconds` | delay between loop steps |
| `watcher.maxConcurrent` | how many tickets may be `in-progress` before new work waits |
| `watcher.worker` | name recorded in `claimed_by` |
| `limits.maxInProgressMinutes` | a ticket older than this moves to `blocked`; `0` disables |
| `release.enabled` | enables the push stage |
| `release.remote` | git remote to push to, default `origin` |
| `release.mode` | only `branch` is implemented; anything else fails loudly |
| `release.baseBranch` | used for the compare link |
| `t3.cli`, `t3.dbPath` | the T3 binary and its SQLite database |

Telegram credentials live in `~/.config/t3-wa-notify/config.json` and are inherited by both the bridge and `scripts/alert.py`; the bridge config may override the token and allowlist.

## Lifecycle and states

```
backlog → ready → in-progress → testing → review → done
   ↑        ↓          ↓           ↓         ↓
   └──── blocked ◄──────┴───────────┴─────────┘
```

| Stage | Trigger | What happens |
|---|---|---|
| dev | ticket in `ready`, slot free, approval satisfied | claim atomically; if the ticket carries a thread, `t3 thread send` continues **that** thread, otherwise `t3 thread start` creates one; store the thread id |
| sync | every loop | a ticket in `in-progress` whose thread reports `completed`/`idle` moves to `testing`; `failed`/`cancelled` moves to `blocked` |
| test | ticket in `testing` | run the ticket's `test_cmd` in the project directory; store exit code and output tail as proof; `review` on 0; on a failure the tail goes **into the dev thread**, the ticket returns to `in-progress`, and testing waits for the thread's next run (`retry.maxTestAttempts`) |
| review | ticket in `review`, verification enabled | start a second thread in a fresh context with the ticket's harness and model (`verification.profile` overrides); it gets the ticket, the proof, `git status` and `git diff HEAD`, and must end with `VERDICT: ACCEPT` or `VERDICT: REJECT`; a rejection sends the findings into the dev thread and spends one of `verification.maxAttempts`; a missing verdict, a failed reviewer, a timeout or a tree that changed mid-review blocks the ticket |
| release | ticket in `review` with a passing proof and an accepted review | create `ticket/T-xxxx-<slug>`, commit, push, store branch and compare link, write the PR body, finish the ticket and write the report |

Stages are drained in that order, so a ticket waiting in `testing` is tested before a new thread is started, and `review` verifies before it releases.

Failures that a retry can fix are not dumped as `blocked`: a crashed `t3` call or a dead thread sends the ticket back to `ready`, a failing test sends its output into the dev thread, and both are bounded (`retry.maxThreadAttempts`, `retry.maxTestAttempts`). Configuration errors and reviewer rejections are not retried blindly — see the table in [docs/usage.md](docs/usage.md#when-something-fails-and-how-it-comes-back).

## Release and push policy

- Pushes are automatic, but **never to the base branch**: every release goes to `ticket/T-xxxx-<slug>` on `origin`.
- A ticket without a `[PASS]` (passing) proof is refused; a human can still move it by hand.
- After the push the ticket stores `branch`, `pr_url` (a `…/compare/main...branch?expand=1` link when the remote is HTTP or SSH) and a PR body file (`T-xxxx.pr.md`) containing the summary and the test evidence.
- Creating the pull request itself needs `gh` or a GitHub token; neither is required for the push to work.
- `release.mode` is validated: an unsupported value is an error, not a silent fallback.

## Limits and budgets

- `watcher.maxConcurrent` counts tickets in `in-progress`; above it, new tickets wait while testing and release keep draining.
- `limits.maxInProgressMinutes` moves a stuck ticket to `blocked` with the measured minutes in its note, so a lost thread cannot hold the slot forever.
- The budget is wall-clock only. Token accounting is not implemented (see [Known limitations](#known-limitations)).

## Data, backups and reports

| Path | Contents |
|---|---|
| `~/.local/state/t3-workflow/tickets.sqlite` | tickets, events, approvals |
| `~/.local/state/t3-workflow/reports/T-xxxx.md` | report, written automatically when a ticket reaches `done` |
| `~/.local/state/t3-workflow/reports/T-xxxx.pr.md` | PR body |
| `~/.local/state/t3-workflow/backups/` | `tickets-YYYYMMDD-HHMMSS.sqlite` plus a `reports-*` copy, retention 7 |

Backups use SQLite's own backup API, so they are consistent while the services run. Restore procedure: [docs/operations.md](docs/operations.md#restore).

## Operations

```bash
systemctl --user restart t3-watcher          # after a code change
systemctl --user stop t3-watcher             # stop automation, keep the board
journalctl --user -u t3-bridge -n 50         # bridge log
tail -f ~/.local/state/t3-workflow/audit.jsonl   # every command and decision
```

Full runbook — pause, rotate the board token, restore a backup, read a failure — is in [docs/operations.md](docs/operations.md).

## Security model

- **Telegram is untrusted input.** Only chat ids in `telegram.allowChatIds` are served; everything else is logged and dropped.
- **Board writes require a token.** Every `POST` needs `X-Board-Token`; the page receives it in a `<meta>` tag, scripts must send it. `GET` is open to the tailnet, which is the actual access boundary.
- **The release stage never pushes the base branch** and refuses a ticket without passing proof.
- **`test_cmd` runs through a shell** in the project directory. Ticket creation is gated by the allowlist, so the blast radius is "the owner's own account", but a hijacked Telegram session becomes code execution as this user. Keep the bot token private and review tickets before moving them to `ready`.
- **No third-party dependencies**: the attack surface is the standard library plus the T3 CLI.

## Testing

```bash
make test     # both suites
make check    # compile everything, then run both suites
make snapshot # write STATE.md: a measured snapshot for handoffs and reviews
```

| Suite | Covers |
|---|---|
| `tests/` | ticket store, transitions, approvals, proofs, watcher stages, release against a temporary bare remote, board HTTP API, limits, backups |
| `bridge/tests/` | command parsing, allowlist, thread resolution, ticket commands, polling loop against a fake Telegram server |

Rules the suites keep: no network, no real Telegram call, no real T3 call, and `T3_REPORTS_DIR` always points at a temporary directory so a test run cannot touch production data. One integration suite is skipped by default:

```bash
T3_INTEGRATION=1 python3 -m unittest tests.test_integration_t3 -v
```

## Repository layout

```
board/          kanban HTTP server and single-page UI (no build step)
deploy/         systemd unit templates, install.sh, docker/ (smoke test, .env example)
Dockerfile      python:3.12-slim plus git, openssh-client, libatomic1 (for the T3 CLI)
docker-compose.yml  board by default, bridge and watcher under the "automation" profile
bridge/         Telegram ⇄ T3 bridge, example config, tests
scripts/        alert.py for systemd OnFailure
tickets/        SQLite store and CLI
tests/          workflow, release, board, limits, backup, integration
watcher.py      dev + test + release stages
docs/           architecture, API, operations
```

## Documentation

| File | Purpose |
|---|---|
| [README.md](README.md) | this file: usage and configuration |
| [docs/architecture.md](docs/architecture.md) | components, data model, state machine, sequences |
| [docs/usage.md](docs/usage.md) | how to use it, which workflow to pick, model and priority guidance, FAQ |
| [docs/handoff-template.md](docs/handoff-template.md) | how to hand work to another thread without losing state |
| [docs/api.md](docs/api.md) | board HTTP API with payloads and curl examples |
| [docs/operations.md](docs/operations.md) | runbook: services, backup/restore, incidents, troubleshooting |
| [docs/docker.md](docs/docker.md) | container deployment: mounts, host networking, verification, troubleshooting |
| [RESEARCH.md](RESEARCH.md) | why the design looks like this, with the measured facts behind it |
| [REVIEW.md](REVIEW.md) | two-axis review (standards and spec) over the whole repository |
| [AGENTS.md](AGENTS.md) | invariants an agent must keep when changing this code |
| [docs/autonomy.md](docs/autonomy.md) | the autonomous loop: decisions, enforcement, out of scope |
| [docs/retry-evidence.md](docs/retry-evidence.md) | what a failed run records, and which failures may retry |
| [docs/test-command-detection.md](docs/test-command-detection.md) | what each project declares as its test command, and which declarations are safe unattended |
| [docs/ui-live-status.md](docs/ui-live-status.md) | what a running thread actually updates, heartbeat and upload measurements |
| [CHANGELOG.md](CHANGELOG.md) | released versions |

## Known limitations

1. **A repository without a remote cannot release.** The stage fails loudly instead of skipping; CI runs on GitHub Actions for `main`.
2. **No pull-request creation.** The push works; opening the PR needs `gh` or a token. The compare link and `T-xxxx.pr.md` are provided instead.
3. **No deploy step.** The pipeline ends at a pushed branch; there is no deploy target or rollback path.
4. **Wall-clock budget only.** No token accounting, no provider quota guard, no cost report. Retries are bounded by counts (`retry.*`), not by spend.
5. **Editing is bounded.** A `done` ticket is final (its report is the record); a ticket a stage is working on needs `force`. Deleting keeps the report and an audit snapshot — there is no undelete.
6. **Attachments are paths, not uploads.** `t3 thread send` accepts text only, so an image cannot travel with a ticket; a path to a file on this machine can. The finished report is delivered to Telegram as a markdown document, not a PDF.
7. **Telegram-only approvals.** WhatsApp was planned as a fallback and is not implemented.
8. **No metrics.** There is a `/healthz` endpoint for the container healthcheck, and failures are reported by `OnFailure=` and by the bridge, but nothing exports metrics.
9. **Model options are not applied per ticket.** The catalogue shows `thinking`/`variant`/`effort`,
   but `t3 thread start` has no flag for them, so the thread uses the project defaults.
10. **The Docker deployment uses host networking.** See [docs/docker.md](docs/docker.md) for the trade-off; the systemd units avoid it.
