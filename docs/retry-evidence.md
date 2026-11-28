# Retry evidence — what a dev-thread failure records, and which failures may be retried

Research notes for one question: when a dev thread fails (provider error, rate limit, timeout,
crash), what does **this machine** actually record, so a watcher can separate a transient failure
from a permanent one instead of retrying a permanent one forever?

Nothing here is implemented. No ticket, thread, run or database row was created or modified while
writing this document. The T3 database was opened with `mode=ro` only; the `t3` CLI was executed with
`--help` only (its other subcommands need a running server, and starting one writes).

Badges used below:

| Badge | Meaning |
|---|---|
| `[measured]` | a command was run on this machine and this is its output |
| `[code]` | quoted from a file in this repository, or from the JS embedded in the `t3` binary, with an offset/`path:line` |
| `[schema]` | quoted from a sqlite column dump or a typed schema in the binary |
| `[inference]` | my conclusion, not measured |

**Repository state.** `HEAD = dadcebe` with a dirty tree. `watcher.py` and `tickets/store.py` were
being rewritten *while this document was written* (09:30–09:31), so every `watcher.py:N` and
`tickets/store.py:N` below refers to the snapshot taken at `2026-11-28T09:37:25+07:00`, and those line
numbers have already moved:

```
sha256  f9475e5fd4ab13f32d968d00a2265be863cd3927f0f89f4c3591201672624c6d  watcher.py   (908 lines)
sha256  db835be4ca427c1d1dd93ae49aa78d00b5a2f55e8ab52d4805206427a4659276  tickets/store.py (576 lines)
```

All T3 numbers below are one snapshot at `2026-11-28T09:41:38+07:00`; the database is live, so counts
drift. `[measured]`

---

## 1. What is recorded

### 1.1 The six tables

`sqlite3` is not installed; the dump is via `python3 -c` with `sqlite3.connect("file:…?mode=ro", uri=True)` `[measured]`.

```
orchestration_v2_projection_runs
  run_id:TEXT, thread_id:TEXT, ordinal:INTEGER, provider:TEXT, provider_thread_id:TEXT,
  status:TEXT, requested_at:TEXT, completed_at:TEXT, payload_json:TEXT, provider_instance_id:TEXT   1129 rows
orchestration_v2_projection_run_attempts
  attempt_id:TEXT, thread_id:TEXT, run_id:TEXT, attempt_ordinal:INTEGER, root_node_id:TEXT,
  provider:TEXT, provider_thread_id:TEXT, provider_turn_id:TEXT, status:TEXT, payload_json:TEXT,
  provider_instance_id:TEXT                                                                       1129 rows
orchestration_v2_projection_provider_turns
  provider_turn_id:TEXT, thread_id:TEXT, provider_thread_id:TEXT, node_id:TEXT, run_attempt_id:TEXT,
  ordinal:INTEGER, status:TEXT, started_at:TEXT, completed_at:TEXT, payload_json:TEXT              1128 rows
orchestration_v2_projection_threads
  thread_id, project_id, title, default_provider, runtime_mode, interaction_mode,
  active_provider_thread_id, created_at, updated_at, archived_at, deleted_at, payload_json,
  provider_instance_id                                                                             296 rows
orchestration_events
  sequence, event_id, aggregate_kind, stream_id, stream_version, event_type, occurred_at, command_id,
  causation_event_id, correlation_id, actor_kind, payload_json, metadata_json,
  application_event_version                                                                     39406 rows
orchestration_v2_effect_outbox
  effect_id, command_id, thread_id, effect_type, payload_json, status, attempt_count, available_at,
  lease_owner, lease_expires_at, created_at, updated_at, completed_at, last_error                 1081 rows
```

Status distribution `[measured]`:

```
runs            completed 987 · failed 114 · interrupted 20 · cancelled 3 · rolled_back 2 · running 3
run_attempts    completed 988 · failed 114 · interrupted 21 · cancelled 3 · running 3
provider_turns  completed 988 · failed 113 · interrupted 21 · cancelled 3 · running 3
```

### 1.2 The error is not in the run, the attempt or the turn

Every one of these three payloads is **status-only**. Keys, verbatim, for the 10 most recent
failed/cancelled/interrupted runs (`select … where status in (…) limit 10`) `[schema]`:

```
runs.payload_json        activeAttemptId checkpointId completedAt contextHandoffId id modelSelection
                         ordinal providerInstanceId providerThreadId queuePosition requestedAt
                         rootNodeId startedAt status threadId userMessageId
                         (+ workStartedAt: 1 run, + delegatedCompletion: 2 runs)
run_attempts.payload_json attemptOrdinal completedAt id nativeThreadId providerInstanceId
                         providerThreadId providerTurnId reason rootNodeId runId startedAt status
provider_turns.payload_json completedAt id nativeTurnRef nodeId ordinal providerThreadId
                         runAttemptId startedAt status tokenUsage | turnTokenUsage
```

There is no `error`, `errorMessage`, `errorCode`, `retryable`, `failure` or `exitCode` key in any of
them — searching for those names returns nothing `[measured]`.

Two more dead ends `[measured]`:

- `run_attempts.attempt_ordinal = 1` for **all 1129 rows**, and `payload_json.$.reason = "initial"` for
  all 1129. T3 never writes a second attempt row; its provider-level retry happens *inside one
  provider turn*, so the attempt table cannot count retries.
- `orchestration_v2_effect_outbox`: all 1081 rows `status='succeeded'`, `attempt_count=1`, and
  `last_error` is empty in every row. Nothing to learn there.
- `orchestration_events` has no `run.failed` / `run-attempt.failed` event type; failures appear as
  `run.updated` carrying `status: "failed"` and the same payload as the projection `[measured]`. The
  log is also **pruned**: `event-log-pruned-through = 379893` of 417674 `[measured]`, so it is not a
  durable record.

### 1.3 The error lives in one place: an `error` turn item

`[measured]` — `orchestration_v2_projection_turn_items` holds 220 rows with `type = 'error'`, in four
titles:

```
Provider error                    107
Provider recovered                106
Usage limit reached                 6
Provider session failed to open     1
```

Their payload is the only row in the whole database that carries a failure `[schema]`:

```json
{"id":"turn-item:provider:pi:native-item:terminal-failure:…", "threadId":"…", "runId":"…",
 "nodeId":"…", "providerTurnId":"…", "ordinal":76, "status":"failed", "type":"error",
 "title":"Provider error",
 "failure":{"class":"provider_error","message":"Request timed out.","code":null,"retryable":false},
 "retry":{"attempt":3,"maxAttempts":3,"retryDelayMs":8000}}
```

So the four fields a retry policy needs are all present, and only here:

| field | values seen | meaning |
|---|---|---|
| `failure.class` | `provider_error`, `usage_limit` | the category |
| `failure.message` | `Request timed out.`, `Connection error.`, `429: …`, `Model not found: …`, `400: …` | the readable cause |
| `failure.code` | `null` (206), `APIError` (6), `UnknownError` (8) | provider/JS error name, rarely set |
| `failure.retryable` | `true` (106), `false` (98), `null` (16) | T3's own verdict |
| `failure.resetAt` | **never set** — 0 rows | declared for `usage_limit`, but empty on this machine |
| `retry.attempt` / `retry.maxAttempts` | `1..3` / `3` | how many times T3 already retried inside the turn |
| `retry.retryDelayMs` | `2000`, `4000`, `8000` | exponential, base 2 s |

`retryable` is not a guess: the embedded adapter sets it `true` on `auto_retry_start` and `false` on
`auto_retry_end` with `success !== true`, i.e. **`false` means "pi already exhausted its auto-retries"**
`[code]` (`t3` binary offset 164920095 / 164921478, `src/…/piProvider`):

```js
case "auto_retry_start": { …
  const failure = makeProviderFailure({ message: …, class: "provider_error", retryable: true });
case "auto_retry_end": {
  if (event["success"] === true) { … emitProviderRetry(turn, recoveredRetry, "completed", …); return; }
  const failure = makeProviderFailure({ message: piRecordString(event,"finalError") ?? "Pi auto-retry failed.",
                                        class: "provider_error", retryable: false });
```

and the title is chosen from the status and class `[code]` (offset 158496635):

```js
function makeProviderRetryTurnItem(input) {
  let title = "Provider retry";
  if (input.status === "completed") title = "Provider recovered";
  else if (input.status === "failed") title = input.failure.class === "usage_limit" ? "Usage limit reached" : "Provider error";
  else if (input.status === "interrupted" || input.status === "cancelled") title = "Provider retry stopped";
```

The failure class enum is closed and small `[schema]` (offset 152234657):

```js
OrchestrationV2ProviderFailureClass = Literals$3([
  "usage_limit", "provider_error", "transport_error", "permission_error", "validation_error", "unknown" ]);
OrchestrationV2ProviderFailure = Struct({ class: …, message: TrimmedNonEmptyString(4096),
  code: NullOr(…128), retryable: NullOr(Boolean), resetAt: optional(NullOr(IsoDateTime)) });
OrchestrationV2ProviderRetry = Struct({ attempt: PositiveInt, maxAttempts: NullOr(PositiveInt),
  retryDelayMs: NullOr(NonNegativeInt) });
```

Only `provider_error` and `usage_limit` actually occur here; `transport_error`, `permission_error`,
`validation_error` and `unknown` are reachable in the type but were never written `[measured]`.

### 1.4 Status decides whether an error item exists at all

`[measured]`, one query per status:

```
run status    runs   with a terminal error item   without
failed         114            114                    0
interrupted     20              3                   17
cancelled        3              1                    2
rolled_back      2              0                    2
completed      987             97 (all "Provider recovered", retryable=true)
```

And for all 114 failed runs the error item sits on the **root node** of the run — 0 exceptions
`[measured]`. That is exactly the predicate T3 itself uses to decide whether to surface an error
`[code]` (offset 155502616, `packages/shared/src/orchestrationV2ThreadError.ts`):

```js
function latestRootProviderFailure(run, turnItems) {
  if (run?.status !== "failed") return null;
  for (const item of turnItems) {
    if (item.type !== "error" || item.status !== "failed" || item.runId !== run.id
        || item.nodeId !== run.rootNodeId) continue; …
}
function threadErrorSummary(failure, sessionError) {
  return { usageLimitResetAt: currentFailure?.class === "usage_limit" ? currentFailure.resetAt ?? null : null,
           lastError: sessionError ?? failure?.message ?? null,
           lastErrorClass: currentFailure?.class ?? null,
           lastErrorCode: currentFailure?.code ?? null };
}
```

Consequence `[inference]`: `interrupted`, `cancelled` and `rolled_back` carry **no** failure evidence,
because they are not provider failures — they are a human stop, a superseded run, or an operator
rollback. A watcher must not read them as "the provider failed".

### 1.5 The failure taxonomy, measured

Only failed runs, duration = the error item's `startedAt`→`completedAt` `[measured]`:

| provider | title | message (trimmed to 40 chars) | n | min/median/max minutes |
|---|---|---|---|---|
| pi | Provider error | `Request timed out.` | 85 | 0.3 / 0.4 / 15.3 |
| opencode | Usage limit reached | `The usage limit has been reached` | 6 | 0.0 / 0.0 / 0.0 |
| opencode | Provider error | `getaddrinfo EAI_AGAIN chatgpt.com` | 6 | 0.0 / 0.0 / 0.0 |
| pi | Provider error | `Connection error.` | 6 | 0.2 / 0.2 / 1.0 |
| pi | Provider error | `429: …rate_limit_error…` | 3 | 1.9 / 1.9 / 2.8 |
| pi | Provider error | `400: …Model is unavailable.` | 2 | 1.4 / 2.2 / 3.0 |
| opencode | Provider error | `Model not found: opencode-go/beam-free…` | 2 | 0.0 / 0.0 / 0.0 |
| pi | Provider error | `terminated` | 1 | 10.5 / 10.5 / 10.5 |
| pi | Provider error | `400: …context length of (262144)…` | 1 | 1.9 / 1.9 / 1.9 |
| pi | Provider error | `429: …GoUsageLimitError … resets in 10d 8h` | 1 | 0.0 / 0.0 / 0.0 |
| codex | Provider session failed to open | `Codex 0.147.0 does not work with this T3…` | 1 | 0.0 / 0.0 / 0.0 |

A retry therefore costs a median of **24 seconds**, not a quarter of an hour `[measured]`. The one
15-minute outlier is the shape of a hang, not of a normal timeout.

Related evidence for the "needs a human" failure: T3 skips its own retry of a session-open failure
when sign-in is required `[code]` (offset 166671789):

```js
const requiresSignIn = "cause" in failure && isProviderAdapterOpenSessionError(failure.cause) && failure.cause.requiresSignIn === true;
if (input.willRetry === true && !requiresSignIn) return yield* failure;
yield* settleStartFailure({ signal: "provider-session-open-failure", title: "Provider session failed to open", error: … });
```

### 1.6 The CLI cannot see any of it

`t3 thread show --json` is the only surface `watcher.py` uses. Its flags `[measured]`:

```
USAGE  t3 thread show [flags] <thread>
FLAGS  --base-dir string   --messages integer   --json
```

Its handler prints exactly two keys `[code]` (offset 166823660, `src/cli/thread.ts`):

```js
const { thread, messages: latest } = yield* readLatestMessages(tools, threadId, flags.messages);
const messages = yield* forEach$1(latest, (item) => readFullText(tools, threadId, item).pipe(map$11((text) => ({
  role: item.type === "user_message" ? "user" : "assistant", runId: item.runId, text }))));
if (flags.json) return yield* printJson({ thread, messages });
```

`thread` there is `OrchestratorMcpThreadDetail`, i.e. what `t3_thread_read` returns `[schema]`
(offset 152790773):

```js
OrchestratorMcpThreadDetail = Struct({ threadId, projectId, title, link?, createdBy, creationSource,
  status, latestRunId, activeRunId, providerInstanceId, model, runtimeMode, interactionMode,
  linkedPullRequest, pullRequests?, titleRegeneration, branch, worktreePath, parentThreadId,
  relationshipToParent, runCount, itemCount, pendingRequestCount, archived, settled, settledAt,
  snoozed, snoozedUntil, createdAt, updatedAt });
```

**There is no error key.** `lastError`, `lastErrorClass`, `lastErrorCode`, `usageLimitResetAt` exist
only on a *different* type, `OrchestrationV2ThreadShell` `[schema]` (offset 152253227), which
`thread show` does not use. `messages` carry `{role, runId, text}` and nothing else. `status` is
`Union([Literal("idle"), OrchestrationV2RunStatus])` `[schema]`, so `failed` / `cancelled` /
`interrupted` / `rolled_back` are visible — the *fact* of failure, never the *cause*.

No CLI command exposes attempts or a retry counter `[measured]`:

```
$ t3 thread --help
SUBCOMMANDS  list  start  send  show
```

`runCount` is exposed (the server maps it `runCount: projection.runs.length` `[code]`, offset
168747929), and that is what `verification_step` already reads. But "how many runs" is not "why it
failed".

So a watcher that wants the class must either read `statev2.sqlite` read-only (the repo already does
this for `projection_projects` in `project_path()`, `watcher.py:53-72`), or move to a `t3` version
whose `thread show` returns the shell. `[inference]`

### 1.7 The query that answers the question

The same predicate T3 uses, expressed in SQL over the projection `[measured]`:

```sql
select r.run_id, r.status,
       json_extract(i.payload_json,'$.failure.class')     as class,
       json_extract(i.payload_json,'$.failure.message')   as message,
       json_extract(i.payload_json,'$.failure.retryable') as retryable,
       json_extract(i.payload_json,'$.retry.attempt')     as attempt,
       json_extract(i.payload_json,'$.retry.maxAttempts') as max_attempts
from orchestration_v2_projection_runs r
left join orchestration_v2_projection_turn_items i
       on i.run_id = r.run_id and i.type = 'error' and i.status = 'failed'
      and json_extract(i.payload_json,'$.nodeId') = json_extract(r.payload_json,'$.rootNodeId')
where r.thread_id = ?          -- last matching row wins: order by r.ordinal desc limit 1
order by r.ordinal desc limit 1;
```

Run against the two newest threads that ever failed, this returns nothing for both — because their
*latest* run is not a failed one `[measured]`:

```
import:pi:01a18374-…  ordinal:93   running    class: None, retryable: None, attempt: None
d240a80f-…            ordinal:128  completed  class: None, retryable: None, attempt: None
```

That is the whole point of "latest": a thread that failed at ordinal 76 and succeeded at 93 must not
be retried, and the query above already says so `[inference]`.

---

## 2. Failure catalogue

One row per way a dev thread can end badly, with what is recorded here and whether a retry is right.
Measured counts are from §1.5; the verdicts are `[inference]` from that evidence.

| failure kind | what this machine records | transient? | evidence |
|---|---|---|---|
| provider timeout (`Request timed out.`) | error item `class=provider_error`, `retryable=false`, `retry=3/3`; run `failed` | **transient** | 85 runs, median 0.4 min `[measured]` |
| connection drop (`Connection error.`) | same, `retryable=false` after 3 | **transient** | 6 runs `[measured]` |
| HTTP 429 rate limit | `provider_error`, message carries the 429 body, `retryable=false` after 3 | **transient** | 3 runs `[measured]` |
| upstream 5xx (`Model is unavailable.`) | `provider_error`, `retryable=false` | **transient** | 2 runs `[measured]` |
| DNS failure (`getaddrinfo EAI_AGAIN`) | `provider_error`, `code=UnknownError`, `retryable=null` (never retried inside the turn) | **transient** | 6 runs, 0.0 min `[measured]` |
| usage limit, monthly (`GoUsageLimitError … resets in 10d 8h`) | `provider_error`, message contains the reset window, `resetAt` **absent** | transient-but-too-late: retrying now loops for 10 days | 1 run `[measured]` |
| usage limit, generic | `class=usage_limit`, `code=APIError`, message `The usage limit has been reached`, `retryable=null` | same: must wait, not retry | 6 runs `[measured]` |
| model not found (`Model not found: opencode-go/beam-free`) | `provider_error`, `code=UnknownError`, `retryable=null` | **permanent** — the model is gone | 2 runs `[measured]` |
| context overflow (`Input token count (454749) exceeds … 262144`) | `provider_error` 400, `retryable=false` | **permanent** for this thread — a retry re-sends the same history | 1 run `[measured]` |
| provider binary/version mismatch (`Codex 0.147.0 does not work…`) | title `Provider session failed to open`, `provider_error`, `retryable=null` | **permanent** — needs an upgrade | 1 run `[measured]` |
| provider process died (`terminated`) | `provider_error`, `retryable=false` | ambiguous; a retry is cheap | 1 run, 10.5 min `[measured]` |
| run interrupted | **nothing** — no error item, run `interrupted` | not a provider failure; retry is a judgement call | 17/20 interrupted runs have no error item `[measured]` |
| run cancelled | **nothing** — no error item, run `cancelled` | **not** retryable: a human stopped it | 2/3 no error item `[measured]` |
| run rolled back | **nothing** — no error item, run `rolled_back` | not retryable: an operator reverted it | 2/2 no error item `[measured]` |
| thread `waiting` (reviewer asked a question) | no error item; `runtime_requests` has 86 `user_input` rows `[measured]` | **not** retryable: needs a human answer | `[schema]` |
| `t3` CLI crashed / timed out / returned non-JSON | nothing at all in T3 — only the watcher's own note | transient, **except** a missing binary | `watcher.py:273-307` `[code]` |
| `t3` server not running | CLI exit ≠ 0, `ThreadServerNotRunningError: No running T3 Code server uses …` `[code]` | transient only if something will restart it | offset 166813386 `[code]` |

---

## 3. Proposed retry policy

### 3.1 The working tree already retries — the committed tree does not

This matters for the decision table below. At `HEAD = dadcebe`, every failure moved a ticket to
`blocked`; in the working tree at 09:31 a bounded retry landed `[code]`:

```python
RETRY_DEFAULTS = {"maxThreadAttempts": 3, "maxTestAttempts": 3}          # watcher.py:158
def retry_or_block(store, ticket, reason, *, counter, limit, stage):     # watcher.py:167
    attempts = int(ticket.get(counter) or 0) + 1
    if attempts >= limit:
        store.move(ticket["id"], "blocked", f"{stage}: {reason} ({attempts}/{limit} attempts)")
    store.record_event(ticket["id"], "retry", f"{stage}: {reason} · attempt {attempts}/{limit}")
    store.move(ticket["id"], "ready", f"{stage}: {reason}; retry {attempts}/{limit}")
```

with `dev_attempts` / `test_attempts` / `dev_run_id` columns (`tickets/store.py:103-105`),
`set_attempts`/`reset_attempts`/`set_dev_run` (`tickets/store.py:341-368`), and the resume anchor that
stops `testing` from re-running the test against the code that just failed (`watcher.py:120-129`,
`watcher.py:189-215`). The state machine is unchanged: a retry is `in-progress → ready`, a budget
exhausted is `blocked` — no new status (`tickets/store.py:34-41`). That is the right shape.

### 3.2 Every current path that ends in `blocked`

`watcher.py`, snapshot above; notes verbatim `[code]`:

| # | line | note | trigger | transient? |
|---|---|---|---|---|
| 1 | 150 | `approval rejected` | a human rejected the approval | **no** — a decision, not a failure |
| 2 | 182 | `{stage}: {reason} ({attempts}/{limit} attempts)` | retry budget exhausted | terminal by design |
| 3 | 583 | `review start failed: {ExceptionName}` | spawning the reviewer crashed | **yes** |
| 4 | 586 | `review start rc={rc}` | `t3 thread start` exited ≠ 0 | **yes** |
| 5 | 594 | `review start output is not JSON` | CLI/server hiccup, or a CLI version change | **yes** (but see 10) |
| 6 | 619 | `review: {attempt} attempts used, no verdict` | review budget exhausted | terminal by design |
| 7 | 625 | `review: reviewer timed out` | no verdict in `verification.timeoutMinutes` | **yes** — a retry is the normal answer |
| 8 | 638 | `review: reviewer is waiting for an answer` | the reviewer asked a question | **no** — needs a human |
| 9 | 642 | `review: reviewer {status}` | reviewer thread `failed`/`cancelled`/`interrupted`/`rolled_back` | **mixed**: `failed` yes; `cancelled` = a human stopped it, **no** |
| 10 | 648 | `review: unexpected reviewer status {status}` | a status this code has never seen | **no** — version drift, needs a human |
| 11 | 658 | `review: reviewer moved past the verified run` | the verdict belongs to another run | **no** — the evidence is void |
| 12 | 667 | `review: reviewer had more than one run` | the review is not fresh | **no** — same |
| 13 | 673 | `review: project changed during verification` | the tree moved under the reviewer | **no** for this verdict; a *new* review is the answer |
| 14 | 691 | `review rejected; findings could not be sent: {note}` | the send back into the dev thread failed | **yes** |
| 15 | 700 | `review rejected ({attempt}/{max}): {findings}` | review budget exhausted | terminal by design |
| 16 | 705 | `review: no verdict line in the reply` | the reviewer wrote no `VERDICT:` line | **yes** — a formatting failure, worth one more reviewer |
| 17 | 726 | `test: test_cmd is empty, a retry cannot fix that` | a ticket with no test command | **no** |
| 18 | 733 | `{reason} ({attempts}/{limit} attempts); see proof` | test budget exhausted | terminal by design |
| 19 | 742 | `{reason}; findings could not be sent: {note}` | the send back into the dev thread failed | **yes** — *and it blocks with budget left over* |
| 20 | 794 | `time budget: {minutes:.0f} min > {limit} min` | `limits.maxInProgressMinutes` overrun | **no** — and a retry would re-trigger, because `move()` does not touch `claimed_at` |

Three failure paths are worse than any `blocked` above, because they **neither block nor count** `[code]`:

- **Unknown model / preset** — `run_once` raises before claiming (`watcher.py:236`), so the ticket stays
  `ready`. `next_ready()` returns the same lowest-priority ticket every loop (`tickets/store.py:498`),
  so one ticket with a stale `model_profile` **head-of-line blocks every other ticket**, forever, with
  no note. `docs/usage.md:135` says this case ends in `blocked`; it does not.
- **Missing remote / missing proof / push failure** — `release()` raises (`watcher.py:352-374`), so the
  ticket stays `review` and `release()` is re-run every `watcher.intervalSeconds`, forever, with no
  attempt counter and no note. `docs/usage.md:135` says this ends in `blocked`; it does not.
- **`thread show` returning `{}`** (CLI missing, server down, timeout) — `sync_threads` gets
  `status = ""` and neither advances nor retries (`watcher.py:107-136`), so an `in-progress` ticket
  stalls silently. Combined with `limits.maxInProgressMinutes` it will eventually hit row 20, but the
  note will blame the clock, not the CLI.

### 3.3 Decision table

Target: never retry what a retry cannot change, and never leave a failure uncounted. Same statuses;
the only new columns are optional (§3.4).

| current `blocked` path (row above) | policy | smallest state change |
|---|---|---|
| 3, 4, 5 reviewer launch crashed / rc≠0 / non-JSON | **retry with backoff**, budget `verification.maxAttempts` | `record_verdict('FAILED', …)` + reuse the retry counter; `review → in-progress` or stay and clear `verify_thread_id` |
| 7 reviewer timed out | **retry with backoff** (one missed review is noise) | as above, verdict `TIMEOUT` already recorded |
| 9 reviewer `failed` **only** | **retry with backoff** | as above |
| 9 reviewer `cancelled` / `interrupted` | **ask a human** — a person stopped it | keep `blocked`, note `review: reviewer cancelled by a human` |
| 8 reviewer waiting for an answer | **ask a human** (T3 already surfaces it) | keep `blocked`; no retry ever |
| 10 unexpected reviewer status | **ask a human** (version drift) | keep `blocked` |
| 11, 12 stale / multi-run review | **retry immediately** as a *new* review (not the same run) | `clear_verifier()`, `review → in-progress`, new thread next loop |
| 13 project changed during review | **retry immediately** as a new review | same |
| 14, 19 findings could not be sent | **retry with backoff** on the send only, not a new budget line | `record_event('retry', …)` and stay `in-progress`; do **not** block |
| 16 no verdict line | **one more reviewer**, then block | budget already counted by `verify_attempt` |
| 6, 15 review budget exhausted | **block** | unchanged |
| 1 approval rejected | **block** | unchanged |
| 17 empty `test_cmd` | **block** | unchanged |
| 2, 18 dev/test budget exhausted | **block** | unchanged |
| 20 time budget overrun | **block**, and stop claiming a retry will help | unchanged; do **not** reset `claimed_at` on a retry, or the budget restarts |
| — unknown model / preset | **block** with a note naming the missing profile; never retry | `ready → blocked`, note `model: profile 'x' is not in config` |
| — no remote / no proof / push failed | **block** for the permanent ones, **retry with backoff** for a network push | `review → blocked` note `release: repository … has no remote`, or `record_event('retry')` |
| — `thread show` unreadable | **retry with backoff** on the read, bounded, with its own counter | `record_event('retry', 'thread show unreadable')`; keep `in-progress` |

Two rules fall out of the measurement and are worth stating separately `[inference]`:

1. **Do not retry on `class == "usage_limit"`.** T3's `resetAt` is never populated here
   (0 of 220 error items `[measured]`), so the wait must be parsed from the message (`resets in 10d 8h`)
   or simply be "block until a human clears it". A 3-attempt budget at 2/4/8 s burns out inside one
   minute and lands in `blocked` looking like a permanent fault — which is exactly what happened to the
   6 `usage_limit` runs (0.0 min each `[measured]`).
2. **`failure.retryable == false` does not mean "do not retry".** It means *pi* already retried 3×
   inside the turn. All 98 terminal timeouts/429s/5xx carry `false` and every one of them is the kind of
   failure that succeeds later `[measured]`. The class and the message decide, not the flag.

### 3.4 Bounded attempts — the sketch

Keep the counters that exist. Add a *delay* and a *class* without adding a status:

```python
RETRY_DEFAULTS = {                       # watcher.py:158, extended
    "maxThreadAttempts": 3, "maxTestAttempts": 3,   # already there
    "maxReviewAttempts": 2,              # == verification.maxAttempts
    "maxReadAttempts": 3,                # CLI unreadable
    "backoffSeconds": [30, 120, 600],    # index attempt-1, clamp to the last
}

# classification, from the error item (§1.2). Default is "block": an unclassified
# failure is a bug in this table, not a reason to loop.
TRANSIENT    = {"Request timed out.", "Connection error.", "terminated"}
TRANSIENT_RE = (r"^429:", r"^5\d\d ", r"EAI_AGAIN")
PERMANENT_RE = (r"^Model not found", r"context length", r"does not work with this T3")

def classify(failure):                   # -> "retry" | "wait" | "block"
    if failure is None:                       return "retry"   # crashed/interrupted, no evidence
    if failure["class"] == "usage_limit":     return "wait"     # never a 3-attempt loop
    msg = failure["message"] or ""
    if any(p in msg for p in PERMANENT_RE):   return "block"
    if msg in TRANSIENT or any(re.match(p, msg) for p in TRANSIENT_RE): return "retry"
    return "block"
```

and one place that applies it, replacing `retry_or_block`'s unconditional "yes":

```python
def after_failure(store, ticket, *, stage, counter, limit, failure, config):
    decision = classify(failure)
    if decision == "block":
        store.move(ticket["id"], "blocked", f"{stage}: {(failure or {}).get('message','no evidence')[:120]}")
        return
    attempts = int(ticket.get(counter) or 0) + 1
    store.set_attempts(ticket["id"], **{counter.replace("_attempts",""): attempts})
    if attempts >= limit:
        store.move(ticket["id"], "blocked", f"{stage}: {decision} exhausted ({attempts}/{limit})")
        return
    delay = backoff(counter, attempts, config)          # 30 / 120 / 600 s
    store.record_event(ticket["id"], "retry", f"{stage}: attempt {attempts}/{limit}, due in {delay}s")
    store.set_retry_after(ticket["id"], delay)          # see below
    store.move(ticket["id"], "ready", f"{stage}: retry {attempts}/{limit} in {delay}s")
```

For the delay there are two ways to express it without a new status, in order of preference:

- **A. Reuse `events`** (no schema change): `next_ready()` gains
  `and not exists (select 1 from events e where e.ticket_id = tickets.id and e.kind = 'retry'
  and e.at > ?)` with `?` = now − backoff. The `retry` event that `retry_or_block` already writes is
  the clock.
- **B. A `retry_after` column** (`text`, default `''`, like the other migrations at
  `tickets/store.py:80-105`): explicit, visible on the board, and one `where retry_after <= ?`.

Nothing else is required: `blocked → ready` already exists, `in-progress → ready` already exists, and
the attempt counters are columns the store already models.

### 3.5 Failures that MUST NOT be retried automatically

| failure | why a retry loops forever | evidence |
|---|---|---|
| **unknown model / preset** (`model profile 'x' is not in config`, `Model not found: …`) | the config is not going to change itself; and here it does not even block — it re-raises every loop and head-of-line blocks every other ticket | `watcher.py:236` `[code]`; 2 measured `Model not found` runs `[measured]` |
| **no remote on the repository** | `git remote get-url origin` fails identically every time; with `release()` raising, the ticket sits in `review` and is retried every interval | `watcher.py:365` `[code]` |
| **missing test command** | a retry cannot invent a command; already blocked explicitly | `watcher.py:726` `[test: test_cmd is empty…]` `[code]` |
| **review rejection** | the reviewer's findings are the input; re-running the same review with the same code yields the same rejection, and the budget is `verification.maxAttempts = 2` | `watcher.py:700` `[code]`; `watcher.py:406-412` defaults |
| **usage limit** (both kinds) | the reset is hours-to-days away and `resetAt` is never recorded, so a bounded 3-attempt retry cannot outlive it | 6 + 1 runs, 0.0 min each `[measured]` |
| **context overflow** (`exceeds the model's maximum context length`) | a retry re-sends the same history and fails the same way | 1 measured run `[measured]` |
| **provider version mismatch** (`Codex 0.147.0 does not work…`) | needs an upgrade, not a turn | 1 run, `Provider session failed to open` `[measured]` |
| **`verification.enabled = false` / `release.enabled = false`** | a disabled stage is a policy, not a fault | `watcher.py:352, 610` `[code]` |

One design constraint from the live data `[measured]`: **do not parse the `note` to decide anything.**
The live ticket database holds notes in the previous language (`release: tidak ada perubahan untuk
di-push`, `test lulus`, `thread selesai (completed)`) while the current code writes English — so the
note is a human-readable trace, and a retry policy must key on the counters, the `events` kinds, and
the failure class instead.

---

## 4. Sources

**Commands** (all read-only; the database was opened `mode=ro`, `sqlite3` is not installed so every
query went through `python3 -c`):

```
python3 -c 'sqlite3.connect("file:/home/kelanach/.t3/userdata/statev2.sqlite?mode=ro", uri=True)'  # §1.1–1.5, 1.7
t3 thread show --help          # §1.6  (t3 v0.1.9, ~/.local/bin/t3 -> ~/.t3/runtime/versions/0.1.9/t3)
t3 thread --help               # §1.6  no attempts/retry surface
t3 --help                      # §1.6  subcommand list
git rev-parse HEAD; git status --short; sha256sum watcher.py tickets/store.py   # header
```

**Binary offsets** in `~/.t3/runtime/versions/0.1.9/t3` (embedded JS, grepped by offset with
`data[off-…:off+…]`; the binary is an unstripped ELF containing the server and the CLI):

```
152234657  OrchestrationV2ProviderFailureClass / ProviderFailure / ProviderRetry   [schema]
152253227  OrchestrationV2ThreadShell (lastError, lastErrorClass, lastErrorCode,
           usageLimitResetAt)                                                        [schema]
152790773  OrchestratorMcpThreadDetail (no error key)                                [schema]
152792872  OrchestratorMcpThreadReadResult, OrchestratorMcpThreadTimelineItem        [schema]
152787627  OrchestratorMcpThreadStatus = "idle" | RunStatus                          [schema]
152768680  OrchestrationV2RunStatus list                                             [schema]
155502616  latestRootProviderFailure / threadErrorSummary                            [code]
158496635  makeProviderFailureTurnItem / makeProviderRetryTurnItem (titles)          [code]
164920095  pi adapter: auto_retry_start -> retryable true                            [code]
164921478  pi adapter: auto_retry_end  -> retryable false ("already gave up")        [code]
166671789  provider-session-open failure, requiresSignIn skips the retry             [code]
166813386  ThreadServerNotRunningError                                               [code]
166823660  thread show handler: printJson({ thread, messages })                      [code]
168747929  runCount: projection.runs.length                                          [code]
```

**Repository files** (line numbers = the snapshot in the header):

```
watcher.py:53-72      project_path() reads statev2.sqlite read-only (precedent)
watcher.py:107-136    sync_threads: status "" falls through both branches
watcher.py:150        blocked: approval rejected
watcher.py:158-186    RETRY_DEFAULTS, retry_settings, retry_or_block
watcher.py:182        blocked: budget exhausted
watcher.py:189-215    send_dev_text + resume anchor
watcher.py:236        CatalogError re-raised, ticket stays ready
watcher.py:273-307    t3 send/start failure paths
watcher.py:343-374    release(): disabled / no proof / no remote / git failures raise
watcher.py:506-520    thread_show(): returns the whole `thread` object, reads only .status
watcher.py:406-412    VERIFICATION_DEFAULTS (maxAttempts 2, timeoutMinutes 20)
watcher.py:583-594    blocked: reviewer launch failures
watcher.py:619-705    blocked: every review outcome
watcher.py:726-742    blocked: test outcomes
watcher.py:794        blocked: time budget
tickets/store.py:34-41   TRANSITIONS (no new statuses needed)
tickets/store.py:80-105  MIGRATIONS incl. dev_attempts / test_attempts / dev_run_id
tickets/store.py:341-368 set_attempts / reset_attempts / set_dev_run
tickets/store.py:498     next_ready(): the head-of-line ticket
docs/usage.md:130-138    retry table the working tree implements
docs/usage.md:135        "unknown model / no remote / no verdict -> blocked" (false for the first two)
docs/architecture.md:141-144  dev/test retry rows
docs/operations.md:109-111    operator view of a bounce
```

**Live ticket database** `~/.local/state/t3-workflow/tickets.sqlite`, read-only `[measured]`:
3 tickets, `backlog 1 · done 2`, no `blocked`, notes in the previous language — hence §3.5's
"do not parse the note".
