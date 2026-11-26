# T3 workflow automation: research and design

<!-- markdownlint-disable MD013 -->

Research and design notes for making the daily loop — T3 Code, the Pi harness with an opencode-go subscription, and Telegram notifications — seamless and automated, including the ticket queue and watcher that were built from it.

Written 2026-11-25, updated 2026-11-26. Every non-obvious claim carries a source marker: `[cli]` from `--help` output on this machine, `[t3]` from the T3 tool surface available in the harness, `[tg]` from the Telegram Bot API documentation, `[gh]` from GitHub's documentation, `[local]` from a direct measurement, `[inference]` for my own conclusion.

---

## 1. Executive summary

Three findings made the target tractable:

1. **T3 already exposes the primitives.** `t3 thread list/start/send/show` work from the CLI against the running server `[cli]`, and `thread send` explicitly queues onto a busy thread: *"A busy thread gets it as its next turn; nothing is interrupted"* `[cli]`.
2. **A two-way channel already exists.** The Telegram bot was installed, `getUpdates` supports long polling with `offset`/`timeout` `[tg]`, and the sending limits are generous for personal use: one message per second per chat, twenty per minute per group, about thirty per second for broadcasts `[tg]`.
3. **The missing piece was the middle layer**: a command interpreter, a ticket store, a scheduler and an approval gate. All of it can be built without touching the kernel and without changing T3.

What was deliberately left out: full autonomy over push and deploy. That is not a technical limit — it is a control decision, recorded in §7.

---

## 2. Measured starting point

| Component | Fact | Evidence |
|---|---|---|
| T3 server | v0.1.9, listening on `0.0.0.0:3773`, `t3code.service` active | `[local]` |
| Thread CLI | `list`, `start`, `send`, `show`; `send` has `--wait` and `--json` | `[cli]` |
| `--wait` exit codes | `1` when the run fails, `2` when it waits for an answer | `[cli]` |
| Project CLI | only `add`, `remove`, `rename` — **no `list`** | `[cli]` |
| Project list | available in `statev2.sqlite` (`projection_projects`) | `[local]` |
| Thread list | `orchestration_v2_projection_threads` plus the latest run status | `[local]` |
| Access tokens | bearer sessions with scopes (`orchestration:read/operate`, `filesystem:*`, …) | `[cli]` |
| Notifier | `t3-wa-notify` active; Telegram enabled, chat id and token stored with mode 600 | `[local]` |
| Telegram bot | `getMe` OK; no webhook; updates pulled with long polling | `[local]`, `[tg]` |
| Kanban | none at the time | `[local]` |

The immediate consequence: **the bridge needs no new API**. It calls the existing CLI and reuses the stored Telegram credentials.

---

## 3. Idea 1 — two-way Telegram into existing threads

| Capability | Mechanism | Status |
|---|---|---|
| List projects | read `statev2.sqlite` | shipped |
| List a project's threads | `t3 thread list --project <path> --json` | shipped |
| Show a thread's recent context | `t3 thread show <id> --json --messages N` | shipped |
| Send a message into a thread | `t3 thread send <id> -` (stdin) | shipped |
| Start a thread from the phone | `t3 thread start <project> - --title … --provider … --model …` | shipped |
| Wait for the result | `--wait` + `--json`; exit code 2 means "needs an answer" | available, not used by the bridge |

### Constraints that had to be respected

1. **A busy thread is not interrupted.** The message becomes the next turn `[cli]`. There is no "send now" without stopping the run, and that is a feature.
2. **Answering an approval is not a `thread send`.** T3 models approvals as pending requests, not messages `[t3]`, and the CLI has no subcommand for them `[cli]`. The bridge therefore *notifies* and records a decision in its own store; the ticket store, not T3, owns the approval state. This is the one place where the tooling adds state instead of proxying it.
3. **A bot cannot start a conversation.** The human must press Start once `[tg]`.
4. **One update mode.** Long polling and webhooks are mutually exclusive `[tg]`. Polling needs no domain, TLS or public port.
5. **Updates are kept for 24 hours** `[tg]`. A bridge down for longer loses old commands, which is the right behaviour: a stale command should not execute late.

### Security notes

The bot is **untrusted input**; only the `chat_id` allowlist stands between a stranger and the agent `[tg]`. Because the bridge can make an agent write code, the allowlist is mandatory. The bridge never interprets a message as a system instruction and never runs `push` or destructive commands on its own `[inference]`.

---

## 4. Idea 2 — tickets, watchers, testing, release

### The shape that was built

```
backlog → ready → in-progress → testing → review → done
             │          │           │         │
             │          │           │         └─ report written automatically
             │          │           └─ test watcher: run test_cmd, store proof
             │          └─ dev watcher: t3 thread start with the ticket's profile
             └─ gate: only tickets a human moved to ready are taken
```

### Where tickets live

| Option | Strength | Cost | Evidence |
|---|---|---|---|
| GitHub Issues | labels, sub-issues, dependencies, Projects, API, webhooks | needs a token scoped to the repository; truth spread across repos | `[gh]` |
| **SQLite (chosen)** | fast, offline, full control; same pattern as the notifier | the UI has to be written | `[inference]` |
| Markdown files | visible in a diff | no query, no state machine | `[inference]` |

The decision was SQLite first (see §7, decision 1); a GitHub mirror stays possible because the store is one module with a narrow API.

### Watcher shapes, from safest

| Shape | Mechanism | Risk |
|---|---|---|
| **Poller (chosen)** | read a `ready` ticket, start or test it, record the outcome | low; easy to stop |
| Agent watcher | a thread with MCP tools picks tickets and answers approvals | medium; needs a strict prompt |
| Event-driven | GitHub webhook triggers the bridge | needs a public endpoint and signature checks |

### Gates that were kept

1. **Model per ticket, not per watcher** — the profile lives in config and only a human edits it `[cli]`.
2. **The test stage does not judge code.** It runs the command and reports the exit code; no model opinion decides pass or fail `[inference]`.
3. **Push is automatic, but only to a ticket branch** — the base branch is never pushed, and a ticket without passing proof is refused.
4. **Approvals are one-shot.** The first decision wins across every channel; later ones are rejected `[inference]`.

### Failure modes and their guards

| Failure | Guard |
|---|---|
| Two workers take the same ticket | one conditional `UPDATE … WHERE status='ready'` |
| "Done" without a test run | proof is required and checked before release |
| Threads piling up | `watcher.maxConcurrent` plus a wall-clock budget |
| Runaway cost | explicit model profile per ticket; wall-clock budget (token accounting is still missing) |
| Notification flood | one message per state change; Telegram allows ~1/second per chat `[tg]` |

---

## 5. Architecture as built

```
┌──────────┐  long poll  ┌──────────────┐  subprocess  ┌────────────┐
│ Telegram │ ◄─────────► │  t3-bridge   │ ───────────► │   t3 CLI   │
└──────────┘             └──────┬───────┘              └─────┬──────┘
                                │ read/write                  │
                                ▼                             ▼
                         ┌──────────────┐             ┌──────────────┐
                         │tickets.sqlite│             │ T3 server    │
                         └──────┬───────┘             └──────┬───────┘
                                │                            │ read-only
┌──────────┐   HTTP (tailnet)   │                     ┌──────┴───────┐
│ Browser  │ ◄────────────────► │  board/server.py ──►│ statev2.sqlite│
└──────────┘                    │                     └──────────────┘
                         ┌──────┴───────┐
                         │  watcher.py  │ ──► t3 CLI (thread start), git (release)
                         └──────────────┘
```

| Component | Owns | Never does |
|---|---|---|
| `tickets/store.py` | state machine, atomic claim, approvals, proofs, reports | call T3 or the network |
| `watcher.py` | dev, test and release stages | invent a model; push the base branch |
| `bridge/t3_bridge.py` | Telegram commands, audit, notices | answer a T3 approval request |
| `board/` | HTTP API and the single-page board | write outside the store API |

Details, data model and sequences: [docs/architecture.md](docs/architecture.md).

---

## 6. Phases and completion criteria

| Phase | Scope | Completion criterion | Status |
|---|---|---|---|
| 1 | Bridge read: `/help`, `/projects`, `/threads`, `/ctx` | answers from real data, no writes | **done** |
| 1b | Bridge write: `/send`, `/new` | message reaches the thread; id validated; audited | **done** |
| 2 | Approval routing | one request, one decision, idempotent | **done** — bridge, board and store |
| 3 | Tickets in SQLite + CLI | create and move, profile validated at creation | **done** |
| 4 | Dev stage | correct command, atomic claim, failures land in `blocked` | **done**, loop included |
| 5 | Test stage | status changes only on exit code 0; proof stored | **done** |
| 6 | Release | branch, commit, push, compare link, PR body; report on `done` | **done except the PR API** |
| 7 | Board | drag and drop plus a single-pointer alternative; reports downloadable | **done** |
| 8 | Operations | alerts on unit failure, daily backups, CI, changelog, tag | **done** |

Current evidence: `make check` runs 57 + 24 tests (two integration tests skipped unless `T3_INTEGRATION=1`), tag `v0.1.0`, three services plus a backup timer active.

---

## 7. Decisions taken (2026-11-25, revised 2026-11-26)

| # | Decision | Reason | Consequence |
|---|---|---|---|
| 1 | SQLite first; GitHub Issues may follow | a persistent, proper database | one store module; a mirror can be added without a rewrite |
| 2 | The human chooses the model; the watcher follows | model choice belongs to the owner | tickets name a profile; unknown profiles are rejected |
| 3 | **Push automatically, on a ticket branch** (revised from "manual") | the owner wants the pipeline to finish the job | release pushes `ticket/T-xxxx-…`, never the base branch, and refuses unproven work |
| 4 | Telegram only for approvals | simpler, one channel | one-shot decisions; WhatsApp remains a possible fallback |
| 5 | Enable the bridge | command from the phone | `t3-bridge.service` enabled |
| 6 | Pilot on `t3-workflow` | zero risk while proving the loop | ticket T-0001, closed end to end |
| 7 | Model and harness from config | consistent distribution of work | four profiles: `fast`, `deep`, `cheap`, `build` (`pi`, `opencode`) |

### UI rules that shaped the board

| Source | Rule taken | Where it shows |
|---|---|---|
| WCAG 2.2 SC 2.5.7 (AA) | any drag action needs a single-pointer alternative `[w3c]` | per-card **Move** control plus **Force move** |
| WCAG 2.1.1 / 2.4.7 | keyboard operable, visible focus `[w3c]` | `tabindex=0` cards, `:focus-visible` outline |
| NN/g drag-and-drop guidance | grabbable signifier, feedback on grab and drop `[nng]` | `cursor: grab`, faded card while dragging, dashed drop target |
| ARIA live regions | announce state changes `[w3c]` | `aria-live="polite"` toast and announcements |

### End-to-end evidence (2026-11-25)

Ticket T-0001 ran the whole path on this repository: `backlog → ready → in-progress` (thread `mcp:72b322c1…`), `→ testing` (the watcher ran `python3 -m unittest discover -s tests`, exit 0), `→ review`, `→ done`, with the report written to `~/.local/state/t3-workflow/reports/T-0001.md`.

### Still open

- **PR creation through an API** — needs `gh` or a GitHub token; today the push is followed by a compare link and a PR body file.
- **Deploy and rollback** — not defined; the pipeline ends at a pushed branch.
- **Token accounting** — only wall-clock budgets exist.
- **Ticket editing** — no edit or delete after creation.

---

## 8. Why one repository

The board, the watcher and the bridge share one data model and change together. Splitting them would mean maintaining a cross-repository contract for something still moving quickly, and would add a second service, config and audit trail. The whole surface is about 2,600 lines of standard-library Python, so a single repository stays readable.

---

## 9. Verification of this research

| Claim | How it was checked | Result |
|---|---|---|
| The CLI has `thread send/start/list/show` | `t3 thread --help` | present |
| `send` queues rather than interrupts | `t3 thread send --help` text | quoted verbatim |
| `--wait` exit codes 1 and 2 | `--wait` help text | quoted verbatim |
| No `project list` in the CLI | `t3 project --help` | only add/remove/rename |
| Long polling works and no webhook is set | `getUpdates` returned updates; webhook info empty | `[local]` |
| Telegram sending limits | official FAQ | 1/s per chat, 20/min per group, ~30/s broadcast |
| Updates expire after 24 hours | Bot API page | quoted |
| GitHub Issues support labels and dependencies | GitHub docs | quoted |
| Project table name | `projection_projects` | confirmed; a `projects` table does not exist |
| `thread show` JSON shape | `{"thread": …, "messages": […]}` | confirmed; the first formatter read `items` and was fixed |
| Bridge works against the real server | `--selftest`, `--once`, suite | projects, threads and a negative write path verified |
| Board sees T3 data | `/api/projects` and `/api/threads` | 11 projects, 40 recent threads |
| `OnFailure` actually fires | a deliberately failing unit | the alert unit was started |
