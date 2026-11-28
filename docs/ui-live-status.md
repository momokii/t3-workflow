# What the board can show live

Question answered here: **what can the board show about a running ticket without inventing data, and
what does each option cost?** Every claim below carries a badge:

- `[measured]` — a command was run on this machine and produced the stated number or result.
- `[code]` — quoted or read from a file in this repository (or a service unit) as it exists now.
- `[schema]` — read from the live SQLite schema (`sqlite_master`).
- `[inference]` — reasoning, not verified here. Never presented as fact.

Snapshot: measured against a **dirty working tree** on top of `9697a05` (`watcher.py`,
`board/server.py`, `board/static/index.html`, `tickets/store.py` all modified, `watcher.py` mtime
`Nov 28 16:06`). Another process was editing those files *while this research ran* and committed them
mid-session as **`e0b7fae` — "feat(board): show the watcher, the live thread and a real dialog for
every wait"** (`16:12:05 WIB`) `[measured]`. The quotes below are exact bytes from that tree; verified
present in `e0b7fae` (`write_heartbeat` ×4 in `watcher.py`, `/api/attachments` in `board/server.py`),
so read them against that commit rather than moving line numbers. All T3 and ticket reads used
`?mode=ro` + `PRAGMA query_only=1`; no thread was started and nothing was written to T3.

One finding dominates the rest: **the live plumbing is committed but not deployed.** `board/server.py`
at `e0b7fae` serves `/api/tickets/<id>/live`, `/api/attachments` and a `heartbeat` field, but the
board process on `:8787` was started `15:52:53 WIB` and the watcher `13:39:11 WIB` — both before that
commit — so at measurement time `[measured]`:

| Probe (same token, same host) | Result |
|---|---|
| `POST /api/attachments` | `404 {"error": "not found"}` |
| `GET /api/tickets/T-0003/live` | `404 {"error": "not found"}` |
| `GET /api/tickets/T-0003/nonsense` (control) | `404 {"error": "not found"}` — identical body |
| `GET /api/tickets/T-0003/events` (older route) | `200` in `3.29 ms` |
| `GET /api/tickets` → keys | `tickets, approvals, statuses, transitions, profiles, defaultProfile, profileDetails, summary` — no `heartbeat` |

So the deployed board can show **nothing live today**; the rest of this page describes what the code
at `e0b7fae` can show once both units are restarted, and what it costs.

---

## what is measurable

### The four honest sources

| Source | Proves | Cannot prove |
|---|---|---|
| `~/.local/state/t3-workflow/tickets.sqlite` — `tickets`, `events`, `approvals` `[schema]` | the ticket's own status, note, attempts, and an append-only `events` trail with `at` | what the agent is doing inside its thread; it never sees intermediate work |
| `watcher.json` heartbeat `[code]` | when the watcher last finished a tick, its interval, its last one-line message, its pid | that the watcher is making progress *inside* a tick |
| T3 `statev2.sqlite` (read-only) `[schema]` | thread/run status, live turn items, completed messages | token-level text of an in-flight answer |
| `~/.local/state/t3-workflow/reports/` `[code]` | the finished evidence per ticket | anything before the ticket is done |

Nothing else is available without inventing it. Notably the board has **no** stream of tokens, no
per-second progress percentage, and no ETA — no source carries those fields.

### Where a thread's conversation lives `[schema]`

```
orchestration_v2_projection_messages
  message_id TEXT PK, thread_id, run_id, node_id,
  role TEXT ('user'|'assistant'), streaming INTEGER,
  created_at TEXT, updated_at TEXT, payload_json TEXT, search_text TEXT

orchestration_v2_projection_turn_items
  turn_item_id TEXT PK, thread_id, run_id, node_id, provider_thread_id,
  provider_turn_id, parent_item_id, ordinal INTEGER, type TEXT, status TEXT,
  updated_at TEXT, payload_json TEXT

orchestration_v2_projection_runs
  run_id TEXT PK, thread_id, ordinal, provider, provider_thread_id,
  status TEXT, requested_at TEXT, completed_at TEXT, payload_json, provider_instance_id

orchestration_v2_projection_provider_turns
  provider_turn_id TEXT PK, thread_id, provider_thread_id, node_id, run_attempt_id,
  ordinal, status, started_at, completed_at, payload_json
```

The tables that matter for a log are `messages` (what was said) and `turn_items` (what is happening).
`payload_json` on a message carries `['createdBy','creationSource','id','threadId','runId','nodeId',
'role','text','attachments','streaming','createdAt','updatedAt']` `[measured]`, so `text` is reachable
with `json_extract(payload_json,'$.text')` without parsing in Python.

Relevant indexes `[schema]` — this is what makes the DB path cheap:

```
orchestration_v2_projection_messages_thread_created_idx  (thread_id, created_at)
orchestration_v2_projection_messages_run_idx             (run_id)
orchestration_v2_projection_turn_items_thread_ordinal_idx(thread_id, ordinal)
orchestration_v2_projection_runs_thread_status_idx       (thread_id, status)
```

### What a *running* run actually updates `[measured]`

This is the load-bearing measurement. A live run was sampled twice, 12 s apart, then again after 60 s
(thread `mcp:dc80cd16-bca0-4965-a56b-8df2b39eab2e`, which is ticket `T-0003`, status `running`):

| Field | T0 | T+12 s | T+60 s |
|---|---|---|---|
| `messages` for that run | 5 rows, byte-identical lengths, last `updated_at` `09:06:29.648Z` | **unchanged** | **unchanged** |
| `turn_items` count | 96 | **100** | 103 |
| `turn_items` max `updated_at` | `09:06:36.395Z` | `09:06:49.222Z` | `09:07:26.545Z` |
| `runs.status` | `running` | `running` | `running` |
| wall clock | `09:07:22Z` | — | — |

Conclusion: **`turn_items` is the live channel; `messages` is not.** Messages appear when an assistant
turn *closes*, so a 60 s window during one long tool call shows no message change at all. A "live log"
built only on `messages` will look frozen during exactly the moments a human is watching.

`turn_items` also carries the honest per-step vocabulary. For that same thread `[measured]`:
`reasoning 34`, `command_execution 32`, `file_change 21`, `dynamic_tool 19`, `assistant_message 5`,
`user_message 1`, `error 1`; statuses `completed 112`, `running 1`. A `turn_item` payload exposes
`['completedAt','id','input','nativeItemRef','nodeId','ordinal','output','parentItemId',
'providerThreadId','providerTurnId','runId','startedAt','status','threadId','title','toolName','type',
'updatedAt']` `[measured]` — enough for one honest line per step: *file changed*, *command running*,
*tool finished*.

Two cautions, both measured:

- **`streaming` is never 1.** All `5,420` message rows observed had `streaming = 0` `[measured]`. The
  column exists, the projection does not publish partial text through it.
- **`updated_at` is not a cursor.** `3,425 / 5,447` message rows have `updated_at > created_at`, with
  gap median `3.1 s`, p90 `17.9 s`, max `28,711 s` `[measured]`. Most rewrite within seconds of insert,
  but a long tail rewrites hours later for reasons unrelated to new text. Use `message_id` presence as
  the cursor, not `updated_at > last_seen` `[inference]`.

### Two real rows, trimmed `[measured]`

From the **live** thread (ticket `T-0003`, run still `running`):

```
role=assistant  created_at=2026-11-28T09:09:50.796Z
text='Live run caught another real bug: a timed-out `git push` crashes the unit instead of retrying. Fixing.'
```

From a finished thread (`1a022144-3688-4c06-a9ba-8f71d7e89984`):

```
role=assistant  created_at=2026-11-28T08:01:37.080Z
text='<a status report in the owner's language: 55 words, two Markdown headings, a bullet list>'
```

The literal text is paraphrased here because the repository keeps its files in English; the shape is
what matters for the log (a long assistant message with Markdown, not a single line).

### How many rows a live thread accumulates `[measured]`

Current DB totals: `messages 5,447`, `turn_items 51,295`, `runs 1,227`
(`completed 1076, failed 120, interrupted 20, cancelled 6, running 3, rolled_back 2`),
`threads 324`, `provider_turns 1,226`. The file is **1.60 GiB** with a **46.6 MiB** `-wal`.

Per live run, the honest average is `turn_items 41.9` and `messages 4.4`. The observed live thread
`T-0003` reached **113 turn items and 6 messages over ~11 minutes** (`08:59:12.996Z` → `09:10:16.502Z`),
i.e. roughly **10 turn items per minute**, in bursts (`+4` in `12 s`, then `+2` in `60 s`).

Size is the trap: `turn_items.payload_json` averages **9,188 B** and peaks at **2,838,316 B** `[measured]`.
Shipping whole payloads to a browser is not an option; select `type`, `status`, `ordinal`,
`updated_at` and `title` only `[inference]`.

---

## live log options

### Option A — read `statev2.sqlite` directly `[measured]`

```sql
select role, streaming, created_at, json_extract(payload_json,'$.text') as text
from orchestration_v2_projection_messages
where thread_id = ?
order by created_at desc
limit 12;
```

`EXPLAIN QUERY PLAN` reports
`SEARCH orchestration_v2_projection_messages USING INDEX orchestration_v2_projection_messages_thread_created_idx (thread_id=?)`
— a simple `order by` **is** a usable log, because `(thread_id, created_at)` is indexed.

| Measurement | Result |
|---|---|
| warm query, 20 rows | `1.4 ms`, then `0.1 ms`, `0.1 ms` |
| connect + first statement | `0.2 ms` |
| **fresh Python process**, read 12 messages | **`1.4 ms`**, `7,675 B` of JSON (3 runs, identical) |
| live activity via `turn_items` ordered by `updated_at` | `0.08–0.68 ms` (plan: index search on `thread_id`, then `USE TEMP B-TREE FOR ORDER BY`) |

Ordering by `updated_at` is *not* index-backed but stays sub-millisecond because the `thread_id` filter
is indexed first `[measured]`.

Costs and risks: the board process must open a 1.6 GiB database read-only and keep a WAL-aware
connection; it needs to know T3's private schema, which is versioned only by vendor code, so an
upgrade can move it silently `[inference]`. The connection is read-only (`mode=ro` + `query_only`) but
it must not block the writer: the `-shm` must stay readable, and `busy_timeout` should be small
`[inference]`.

### Option B — shell out to the CLI `[measured]`

```
t3 thread show <thread> --json --messages N
```

| Run | Wall | Peak RSS | Payload |
|---|---|---|---|
| `--messages 5`, finished thread | `1.20 s` / `1.15 s` / `1.23 s` | `299,240 kB` | `6,061 B` |
| `--messages 12` | `1.17 s` | `299,220 kB` | `8,888 B` |
| `--messages 100` | `1.47 s` | `298,848 kB` | `82,875 B` |
| `--messages 10`, **live** thread | `0.91 s` / `0.91 s` / `0.90 s` | `299,156 kB` | `5` messages |

Latency is **dominated by process start, not by message count** (5 → 100 messages costs `0.27 s`),
which matches a ~300 MB runtime image `[inference]`.

Returned keys `[measured]`:

```
top level : "thread", "messages"
messages[]: "role", "runId", "text"          ← no timestamp
thread    : threadId, projectId, title, link, status, latestRunId, activeRunId,
            providerInstanceId, model, runtimeMode, interactionMode, runCount, itemCount,
            pendingRequestCount, branch, worktreePath, createdAt, updatedAt, settled, snoozed, …
```

Two facts that decide the design:

1. **No timestamps.** `messages[]` carries only `role`, `runId`, `text`. A log with "role/text/time"
   cannot come from the CLI; the time must come from the DB `[measured]`.
2. **No partial content.** On a thread whose run was `running`, the CLI returned exactly the same
   `5` messages the DB had, `thread.status = "running"`, and nothing in flight `[measured]`.

The `thread` object is genuinely useful and cheap *relative to the same call*: `status`, `latestRunId`,
`activeRunId`, `itemCount`, `link` — the reason the existing endpoint shells out at all `[code]`.

### Compared

| | DB read | CLI subprocess |
|---|---|---|
| Latency, 12 messages | **`1.4 ms`** (fresh process) | **`900–1,230 ms`** |
| Memory | Python `sqlite3`, no subprocess | **~299 MB** peak per call `[measured]` |
| Timestamps | yes (`created_at`, `updated_at`) | **no** |
| Live granularity | `turn_items`, sub-second | messages only, turn-complete |
| Coupling | T3's private schema | T3's public CLI contract |
| Failure surface | `sqlite3.Error` | exit code, non-JSON stdout, 25 s timeout `[code]` |

Recommendation `[inference]`: **DB for the log body, CLI never on the poll path.** At a 5 s cadence the
CLI path is `~18 %` of one core spent spawning a 300 MB runtime per open modal, versus microseconds;
keep the CLI for the operator actions it already owns (`thread send`, `watcher --once`). If the schema
coupling is unacceptable, poll the CLI at ≥ 30 s and label the modal "snapshot", not "live".

The existing endpoint in the tree does the opposite — `[code]`:

```python
proc = subprocess.run(
    [cli, "thread", "show", thread_id, "--json", "--messages", "12"],
    capture_output=True, text=True, timeout=25,
)
...
"text": (item.get("text") or "")[:4000],
```

---

## heartbeat design

### What exists `[code]`

`watcher.py` already writes an atomic JSON file next to the ticket DB:

```python
def write_heartbeat(db_path: Path | str, interval: int, status: str, message: str) -> None:
    """A small file the board can read to say when the watcher last looked."""
    path = Path(db_path).with_name(HEARTBEAT_NAME)          # HEARTBEAT_NAME = "watcher.json"
    payload = {
        "at": datetime.now(timezone.utc).isoformat(),
        "interval": int(interval),
        "status": status,
        "message": message[:300],
        "pid": os.getpid(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass  # a heartbeat is never allowed to stop the loop
```

and the loop calls it once per tick `[code]`:

```python
def loop(store_factory, config: dict, t3_cli: str, interval: int, worker: str,
         db_path: Path | None = None) -> None:
    print(f"watcher started (interval {interval}s)", flush=True)
    while True:
        if not (config.get("watcher") or {}).get("enabled", True):
            if db_path:
                write_heartbeat(db_path, interval, "paused", "watcher.enabled=false")
            print("watcher paused by config (watcher.enabled=false)", flush=True)
            time.sleep(max(60, interval))
            continue
        store = store_factory()
        try:
            message = step(store, config, t3_cli, dry_run=False, worker=worker)
            status = "ok"
        except TicketError as exc:
            message = f"error: {exc}"
            status = "error"
        finally:
            heartbeat_path = store.path if db_path is None else db_path
            store.close()
        write_heartbeat(heartbeat_path, interval, status, message)
        print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)
        time.sleep(interval)
```

The interval comes from config, `intervalSeconds: 120` in the live config `[measured]`:

```python
interval = args.interval or int((config.get("watcher") or {}).get("intervalSeconds", 120))
```

Service unit restart policy `[code]`, confirmed by systemd `[measured]`
(`Restart=on-failure`, `RestartUSec=15s`, `NRestarts=0`, `is-active=active`):

```
ExecStart=@PYTHON@ @REPO@/watcher.py --loop
Restart=on-failure
RestartSec=15
```

### File or SQLite row?

| Heartbeat shape | Median write | p95 | Side effect |
|---|---|---|---|
| file, tmp + `os.replace` (current) | **`0.094 ms`** | `0.146 ms` | 131 B file, atomic, no schema change |
| row in `tickets.sqlite` (WAL, upsert + commit) | **`0.488 ms`** | `0.605 ms` | one fsync-ish commit per tick |

`[measured]`, 200 iterations each, same temp directory. Both are negligible at one tick per 120 s
(`0.83` commits/min). The file wins on three counts `[inference]`: ~5× cheaper, no schema migration for
a value that is not ticket data, and it is readable by the board even if `tickets.sqlite` is locked.

So the cheapest honest heartbeat is **already implemented**: one atomic 131 B file write per tick.

### Last check / next check, and the honesty problem

`read_heartbeat` already derives the two numbers a board needs `[code]`:

```python
age = max(0, int((datetime.now(timezone.utc) - checked).total_seconds()))
interval = int(data.get("interval") or 0)
data["ageSeconds"] = age
data["stale"] = bool(interval and age > max(90, interval * 3))
```

With the live config that is: **last check = `at`**, **next check ≈ `at + 120 s`**, **stale when
`age > 360 s`** `[measured]` from config + code.

But `write_heartbeat` is called **after** `step()` returns, so `at` means *"last tick finished"*, not
*"still working"* `[code]`. A single tick can run far longer than the interval: `TEST_TIMEOUT = 1800`
(30 min) for the test stage and `timeout=300` on the `t3` calls `[code]`. During such a tick the file
ages past `stale = 360 s` while the watcher is perfectly healthy — the board would lie "stalled".

Cheapest honest fix, keeping one write per tick `[inference]`:

1. Write a `phase` **before** the work (`"checking"`) and again after (`"ok"` / `"error"`), i.e. two
   `0.094 ms` writes per tick instead of one. Then `at` with `phase == "checking"` means *"inside a
   tick since `at`"* and staleness must be judged against the longest legitimate stage, not `3 × 120 s`.
2. Or keep one write and let the board show `at` as "last finished check" and read the stage from
   `tickets.sqlite` (`tickets.status`, `claimed_at`) rather than from the heartbeat.

Either way the wording must be "last check finished at …" — never "watcher alive" `[inference]`.

Note the deployed state: `watcher.json` is **absent** right now `[measured]`, because the running
process was started before the heartbeat existed, so `read_heartbeat` returns `None` today.

---

## upload design

### What the browser can hand you

The WHATWG File API spec is **not available offline** on this machine `[measured]`, so the following is
`[inference]` except where a local artifact settles it. The local TypeScript DOM typings
(`…/typescript/lib/lib.dom.d.ts`) do settle the important part `[code]`:

```ts
interface File extends Blob {
    readonly lastModified: number;
    /** …returns the name of the file represented by a File object.
     *  For security reasons, the path is excluded from this property. */
    readonly name: string;
    readonly webkitRelativePath: string;
}
interface Blob {
    readonly size: number;      // bytes
    readonly type: string;      // MIME type
}
```

So a drag-and-drop handler gets **`name`, `size`, `type`, `lastModified` and the bytes** — and
explicitly **no absolute path**. `webkitRelativePath` is folder-picker relative, not a filesystem path.

Implication for `t3 thread send` `[code]` + `[inference]`: the CLI signature is
`t3 thread send <thread> <message>` where message is *text or `-` to read stdin* — there is no file
argument (`--help`, T3 `0.1.9`). Therefore the browser cannot hand the agent a file directly; the bytes
must land on this host, and only the resulting **path** can travel inside the message text. That is
exactly what `build_prompt` already does `[code]`:

```python
if ticket.get("attachment_path"):
    parts += ["", f"Attachment (read it from this path when needed): {ticket['attachment_path']}"]
```

### The endpoint that exists `[code]`

`board/server.py` (working tree) implements the smallest honest version:

```python
if path == "/api/attachments":
    if not self._authorised():
        self._json(401, {"error": "invalid token"}); return
    length = int(self.headers.get("Content-Length", 0) or 0)
    if length <= 0:
        self._json(400, {"error": "empty upload"}); return
    if length > self.max_upload_bytes:
        self._json(413, {"error": "file is larger than 20 MB"}); return
    raw_name = unquote(self.headers.get("X-Filename", "")).strip()
    name = Path(raw_name).name
    if not name or name.startswith("."):
        self._json(400, {"error": "a filename is required"}); return
    target_dir = Path(os.environ.get("T3_ATTACHMENTS_DIR", self.attachment_dir))
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{uuid.uuid4().hex[:8]}-{name}"
    target.write_bytes(self.rfile.read(length))
    self._json(200, {"path": str(target), "name": name, "size": length})
```

with `max_upload_bytes = 20 * 1024 * 1024` and
`attachment_dir = ~/.local/state/t3-workflow/attachments` `[code]`. The browser side sends the raw body
with the name in an `X-Filename` header and stores the returned `path` into
`form.attachment_path` `[code]`.

Why this shape is right for a stdlib-only server `[inference]`:

- **Size limit before reading.** `Content-Length` is checked first, so an oversized body is rejected
  without buffering it. 20 MB is a defensible ceiling for a prompt attachment; the alternative
  (streaming to disk with a running total) is more code for no benefit at this scale.
- **Filename is not trusted.** `Path(raw_name).name` drops every directory component, and a leading
  `.` is refused, so neither `../../etc/passwd` nor `.bashrc` becomes a target. A random 8-hex prefix
  prevents collisions and keeps the original name visible for a human.
- **Storage outside the repo**, under existing state, overridable by `T3_ATTACHMENTS_DIR` for tests.
- **Text-only reply.** The response returns the path; the path is what the prompt carries. No file
  handle, no base64 into the message, no invented "upload" tool on the T3 side.
- Gaps worth closing if this ships `[inference]`: no `Content-Type` check (harmless when the file is
  only read by a human or an agent, not executed), no per-ticket directory (names are unique anyway),
  and no cleanup policy for old attachments.

The drag-and-drop front end already handles `dragenter`/`dragover`/`dragleave`/`drop`, states the
failure in a status line (`'Upload failed: ' + err.message`), and reports
`Attached <name> (<n> KB) · <path>` `[code]`.

---

## accessibility/polling

### What the modal does today `[code]`

```js
function openLive(ticket) {
  liveTicket = ticket; liveFailures = 0;
  document.getElementById('live-log').textContent = '';
  document.getElementById('dlg-live').showModal();
  pollLive();
  if (liveTimer) clearInterval(liveTimer);
  liveTimer = setInterval(pollLive, 5000);
}
document.getElementById('dlg-live').addEventListener('close', () => {
  if (liveTimer) clearInterval(liveTimer);
  liveTimer = null; liveTicket = null;
});
document.getElementById('live-refresh').addEventListener('click', () => {
  liveFailures = 0; pollLive();
});
```

with

```js
} catch (err) {
  liveFailures += 1;
  setStatus(status, 'Could not read the thread: ' + (err.message || err));
  if (liveFailures >= 3 && liveTimer) {
    clearInterval(liveTimer); liveTimer = null;
    setStatus(status, 'Stopped after three failures; press Refresh.');
  }
}
```

and markup `<div class="live" id="live-log" role="log" aria-live="polite">` plus a manual
`Refresh` button `[code]`. The board list itself refreshes every `15,000 ms` `[code]`.

So the four structural decisions are already right: poll on open, **stop on close**, give up after
three failures with an explicit "press Refresh", and offer a manual refresh beside the log.

### Cadence

`[inference]` with the measured inputs above: 5 s is a reasonable floor for a log whose granularity is
`turn_items` (observed bursts of ~4 items in 12 s, ~2 items/min when idle). Going below 2 s buys nothing
because the underlying projection does not change that fast; going above 10 s makes a running command
look dead. With the **CLI** path at `0.9–1.2 s` per call, 5 s means the modal is fetching ~20 % of the
time and spawning a 300 MB process every 5 s — if the CLI is kept, the cadence must rise to ≥ 30 s.
With the **DB** path (`1.4 ms`) the cadence is bounded by courtesy, not cost.

Recommended shape `[inference]`: fixed 5 s while the dialog is open and the document is visible; pause
when `document.hidden` is true; resume on `visibilitychange`. That avoids polling a modal nobody sees.

### Failure behaviour

The current "3 failures then stop, press Refresh" is honest and should stay `[inference]`. What it lacks
is backoff: three failures at 5 s intervals is 15 s of noise, and a single transient error resets the
counter (`liveFailures = 0` on any success). A graded policy is cheap:

1. failure 1 → keep 5 s, show the error inline (as now);
2. failures 2–3 → back off to 15 s, then 30 s, still automatic;
3. failure 4 → stop, keep the last good log on screen, state "stopped after N failures; press Refresh";
4. `401`/`404` (token expired, ticket has no thread yet) → do **not** retry at all: these are permanent
   until the human acts, and the API already returns exactly those codes with a reason `[code]`.

Never blank the log on failure — a stale-but-labelled log is evidence, an empty one is a lie
`[inference]`.

### Live regions

WAI-ARIA APG (Authoring Practices) pages are **not available offline** on this machine `[measured]`, so
everything in this subsection is `[inference]`, supported only by the local WCAG reference
(`~/.agents/skills/accessibility/references/WCAG.md`) which lists **2.2.2 Pause, Stop, Hide** ("moving/
blinking content can be paused") and **4.1.3 Status Messages** ("status messages announced to screen
readers") `[code]`.

The current implementation has one real defect `[code]` + `[inference]`: `pollLive` rebuilds the whole
log on every tick (`log.textContent = ''` then re-append all messages) inside a container marked
`role="log" aria-live="polite"`. Depending on the screen reader, that re-announces the entire
conversation every 5 s — a 4.1.3 problem created by trying to satisfy 4.1.3.

The fix is structural, not cosmetic `[inference]`:

- Keep **`aria-live` off on the log container** (or `role="log"` without `aria-live`), and append only
  *new* lines; the log then behaves like a console a screen-reader user reads on demand.
- Put the *status* in a separate small region with `role="status"` / `aria-live="polite"` — one line,
  e.g. "3 new lines · thread running · last check 14 s ago". This is the 4.1.3 message; it changes far
  less often and is worth announcing.
- Keep `aria-atomic="false"` on the status line so only the changed text is read.
- Never move focus, and never let new content push the scroll position: the current code already
  preserves it with an `atBottom` check `[code]`.
- Errors go through the existing assertive toast (`role="status" aria-live="polite"` in markup, with a
  longer 6 s dwell for errors) — an interrupted log load does not warrant `aria-live="assertive"`.

Satisfying 2.2.2 is already handled in spirit: nothing animates, and the poll stops on close. The manual
`Refresh` button is the "pause" affordance a reader might want; it should be reachable *before* the log
in DOM order so keyboard users can stop the churn without tabbing through it `[inference]`.

### Reduced motion

`board/static/index.html` already honours it `[code]`:

```css
@media (prefers-reduced-motion: reduce) { article.card { transition: none; } }
```

For a live log this should mean slightly more than disabling transitions `[inference]`: no auto-scroll
animation (the current code jumps with `scrollTop = scrollHeight`, which is fine), no spinner if one is
added, and a new message should not fade or slide in — appearance must be instant. Text updates that
appear without motion are already compliant; motion is the only thing this query needs to remove.

---

## sources

All paths on this machine; all T3 and ticket reads were `mode=ro` + `PRAGMA query_only=1`.

| Claim | Source | Badge |
|---|---|---|
| DB/table columns, indexes | `~/.t3/userdata/statev2.sqlite` → `sqlite_master` | `[schema]` |
| Row counts, streaming, payload shapes, growth | queries run against that DB | `[measured]` |
| Live-vs-frozen behaviour over 12 s / 60 s | same DB, sampled twice per window | `[measured]` |
| CLI latency, RSS, payload size, keys | `t3 thread show <id> --json --messages N`, `/usr/bin/time -f %e %M`, 3 runs each | `[measured]` |
| CLI has no file argument | `t3 thread send --help` (T3 `0.1.9`, `~/.local/bin/t3` → `~/.t3/runtime/versions/0.1.9/t3`) | `[code]` |
| Loop, heartbeat writer/reader, stale rule | `watcher.py` (`loop`, `write_heartbeat`), `board/server.py` (`read_heartbeat`) at `e0b7fae` | `[code]` |
| Interval, concurrency, stage timeouts | `~/.config/t3-workflow/config.json` (`intervalSeconds: 120`), `watcher.py` (`TEST_TIMEOUT`, `timeout=300`) | `[code]` `[measured]` |
| Restart policy | `deploy/t3-watcher.service.in`, `~/.config/systemd/user/t3-watcher.service`, `systemctl --user show` | `[code]` `[measured]` |
| Heartbeat write cost | 200 iterations, file vs WAL row, `time.perf_counter` | `[measured]` |
| Deployed board lacks `/live`, `/api/attachments`, `heartbeat` | `curl` to `100.124.184.116:8787` with the board token (never printed), plus `ActiveEnterTimestamp` `15:52:53` vs commit `e0b7fae` at `16:12:05` | `[measured]` |
| Upload limits, sanitising, storage dir | `board/server.py` `/api/attachments`, `max_upload_bytes`, `attachment_dir` | `[code]` |
| Drag-and-drop front end | `board/static/index.html` (`attachment-drop`, `pollLive`, `openLive`) | `[code]` |
| `File` has no path | WHATWG File API spec **not available offline**; `…/typescript/lib/lib.dom.d.ts` `interface File extends Blob` | `[code]` for the typings, `[inference]` for spec wording |
| WCAG 2.2.2 / 4.1.3 | `~/.agents/skills/accessibility/references/WCAG.md` | `[code]` |
| WAI-ARIA APG | **not available offline** — no local copy was found | `[measured]` (absence) |
| Cadence, backoff, live-region restructuring, recommendations | reasoning over the measurements above | `[inference]` |

Related pages: `docs/architecture.md` (data model, state machine), `docs/api.md` (endpoints),
`docs/operations.md` (the watcher under systemd), `docs/usage.md` (operator workflow).
