# Architecture

How the four pieces fit together, what each one owns, and where the boundaries are.

## Components

```mermaid
flowchart LR
    TG[Telegram] <--> BR[bridge/t3_bridge.py]
    WEB[Browser on the tailnet] <--> BD[board/server.py]
    BR --> ST[(tickets.sqlite)]
    BD --> ST
    WT[watcher.py] --> ST
    WT --> T3[t3 CLI]
    BR --> T3
    BD --> T3DB[(T3 statev2.sqlite<br/>read only)]
    ST --> REP[reports/]
    T3 --> SERVER[T3 Code server]
```

| Component | Owns | Never does |
|---|---|---|
| `tickets/store.py` | the state machine, atomic claim, approvals, proofs, reports | call T3 or the network |
| `watcher.py` | the dev, test and release stages | invent a model profile; push the base branch |
| `bridge/t3_bridge.py` | Telegram commands, audit log, approval notices | answer a T3 approval request itself |
| `board/server.py` + `index.html` | HTTP API and the single-page board | write a ticket outside `store.move`/`store.add` |
| `scripts/alert.py` | one Telegram alert per failing unit | retry, restart, or decide anything |

T3 remains the owner of projects, threads and runs. This repository reads `statev2.sqlite` for two things only: the project list and recent threads (both read-only, `mode=ro`).

## Data model

`tickets`

| Column | Type | Meaning |
|---|---|---|
| `id` | text | `T-0001`, assigned from the row count |
| `title`, `body` | text | what to do |
| `status` | text | one of the seven states |
| `priority` | integer | 1 (highest) … 5 |
| `project` | text | T3 project title or absolute path |
| `model_profile` | text | key into `config.models`; empty means `defaults.profile` |
| `test_cmd` | text | command the test stage runs in the project directory |
| `requires_approval` | integer | 0/1; 1 makes the dev stage wait for an approval |
| `thread_id` | text | T3 thread, filled by the dev stage or by adoption |
| `branch`, `pr_url` | text | filled by the release stage |
| `claimed_at`, `claimed_by` | text | set by the atomic claim |
| `proof` | text | exit status plus output tail from the test stage |
| `report_path`, `pr_body_path` | text | files written on completion |
| `note` | text | last human or stage note |
| `created_at`, `updated_at` | text | ISO-8601 UTC |

`events` — append-only history: `created`, `moved`, `forced`, `claimed`, `thread`, `proof`, `approval-requested`, `approval-decided`, `branch`, `report`, `pr-body`.

`approvals` — `approval_id`, `ticket_id`, `question`, `status` (`pending`/`approved`/`rejected`), `created_at`, `decided_at`, `decided_by`, `note`.

Migrations run on open (`MIGRATIONS` in `store.py`); a column is added with `alter table` when missing, so an older database keeps working.

## State machine

```
backlog  → ready, blocked
ready    → in-progress, backlog, blocked
in-progress → testing, ready, blocked
testing  → review, in-progress, blocked
review   → done, in-progress, blocked
blocked  → ready, backlog
done     → (terminal)
```

`store.move()` refuses anything else. `force=True` is the human override and is recorded as `forced` so the report distinguishes a decision from a transition. The watcher never forces: every stage move it makes is one of the allowed transitions above.

## Stages

### Dev stage

```mermaid
sequenceDiagram
    participant W as watcher
    participant S as store
    participant T as t3 CLI
    W->>S: next_ready()
    W->>S: approval_state() (when the ticket requires it)
    W->>S: claim(id, worker)  %% UPDATE … WHERE status='ready'
    alt ticket carries a thread_id (adopted or continued)
        W->>T: thread send <threadId> - --json
        W->>S: record_event(continued)
    else
        W->>T: thread start <project> - --provider … --model … --json
        T-->>W: {"threadId": …}
        W->>S: attach_thread(id, threadId)
    end
```

The claim is a single conditional `UPDATE`; a second worker gets `rowcount = 0` and reports the current status instead of starting a duplicate thread.

### Sync stage

Every loop starts by reconciling tickets with the threads they point at:

| Thread status | Ticket move |
|---|---|
| `completed`, `idle` | `testing` — the agent's work is done, now prove it |
| `failed`, `cancelled`, `interrupted` | `blocked` with the thread status in the note |
| `running`, `waiting`, unknown | left alone |

This is what makes adoption work: the agent finishes inside its own thread and the watcher advances
the ticket from that status rather than from a guess.

### Test stage

```mermaid
sequenceDiagram
    participant W as watcher
    participant P as project directory
    participant S as store
    W->>P: sh -c <test_cmd>
    P-->>W: exit code + output
    W->>S: attach_proof(id, ok, tail)
    alt exit code 0
        W->>S: move(id, review)
    else
        W->>S: move(id, in-progress) with the failure in the note
    end
```

A ticket without `test_cmd` cannot advance: the stage records that fact and sends it back to `in-progress`.

### Release stage

```mermaid
sequenceDiagram
    participant W as watcher
    participant G as git
    participant S as store
    W->>S: proof must start with [PASS]
    W->>G: remote get-url origin (fail loudly when absent)
    W->>G: checkout -b ticket/T-xxxx-<slug>
    W->>G: add -A && commit
    W->>G: push -u origin <branch>
    W->>S: set_branch + write_pr_body
    W->>S: move(id, done) → report written
```

The base branch is never pushed. `compare_url()` turns an HTTPS or SSH remote into a `…/compare/<base>...<branch>?expand=1` link; a local path yields no link.

## How the harness and model are chosen

```
ticket.provider + ticket.model      validated against ~/.t3/caches/<instanceId>.json
        ↓ if empty
ticket.model_profile (a preset)     from ~/.config/t3-workflow/config.json
        ↓ if empty
config.defaults.profile
```

The catalogue is T3's own provider cache: the same file the T3 UI reads, refreshed by the server
(`checkedAt`). A disabled harness or an unknown model is rejected when the ticket is created, so a
ticket cannot carry a choice that would fail later in the watcher.

Model options (`thinking`, `variant`, `effort`) are reported by the catalogue but **not applied**:
`t3 thread start` accepts `--provider` and `--model` only. The thread therefore uses the project's
default options. Applying per-ticket options would need a CLI flag that does not exist today.

## Concurrency and ordering

- `step()` drains `review`, then `testing`, then `ready`; a waiting test never competes with a new thread.
- `watcher.maxConcurrent` counts `in-progress` tickets; above the limit the stage returns "waiting" and does nothing.
- `limits.maxInProgressMinutes` sweeps stale `in-progress` tickets to `blocked` at the start of each step.
- The bridge and the board share the same store file; SQLite's locking handles it, and the claim is the only place that needs atomicity.

## Failure behaviour

| Failure | Result |
|---|---|
| T3 CLI missing or failing | ticket moves to `blocked` with the exit code and output |
| Output of `thread start` is not JSON | ticket moves to `blocked`, no thread recorded |
| Test command missing or timing out | proof records it, ticket returns to `in-progress` |
| No git remote | release raises; the ticket stays in `review` |
| Non-passing proof | release raises; only a human override moves it |
| Unit crash | systemd restarts it (`Restart=on-failure`) and `OnFailure=` sends one Telegram alert |

## Why no daemon on the T3 side

The target of this tooling is the operator's machine, not a fleet. Every T3 interaction is a one-shot CLI call or a database read, which keeps the system readable and means a dead service never leaves a half-written state in T3: the worst case is a ticket that stays where it was.
