# Ticket states on the card, in the report, and in a list view

Question answered here: **what is the cheapest honest per-ticket "what is happening right now"
line the board can show on every card, how can a reviewer's state be told from the database, and
what is the right accessible pattern for a tabbed report and a list view?** Badges:

- `[measured]` — a command ran on this machine and produced the stated number or result.
- `[code]` — quoted or read from a file in this repository as it exists now.
- `[schema]` — read from the live SQLite schema (`sqlite_master`).
- `[spec]` — quoted from a normative/canonical page fetched during this research (URL in `sources`).
- `[inference]` — reasoning, not verified here. Never presented as fact.

Snapshot: repository at `2f9a8ad` **plus an uncommitted working tree** (`board/server.py`,
`board/static/index.html`, `tickets/store.py`, `tests/test_board.py`). The card's state line, the
report tabs and the list view described below exist **only in that dirty tree** — `git log -S
"thread_state" -- board/server.py` returns nothing, so no commit contains them `[measured]`. The
board process on `:8787` was started `16:22:09 WIB`, i.e. from committed code without
`thread_snapshots`, so the live `GET /api/tickets` carries **no** `thread_state`/`review_state` today
`[measured]` (see `risks`). Every T3 read used `?mode=ro` + `PRAGMA query_only=ON`; no thread was
started and nothing was written to T3.

Database as measured: `statev2.sqlite` **1.75 GB** with a **48.9 MB** `-wal`, holding
`threads 328`, `runs 1,232`, `turn_items ≈51.9 k`, `runtime_requests 98` `[measured]`. Live tables grow
while this page is read, so per-table counts drift by a handful between statements.

---

## what the card can say

### One grouped query for every card

The board already knows the ticket rows and their thread ids from `tickets.sqlite`. One statement
answers "latest run", "last step", and "is a person being asked" for all of them at once `[measured]`:

```sql
with ids(thread_id) as (values (?),(?),…)
select i.thread_id,
       r.status  as run_status,
       r.ordinal as run_ordinal,
       ti.type   as last_type,
       ti.status as last_state,
       ti.updated_at as last_at,
       (select count(*) from orchestration_v2_projection_runtime_requests rr
         where rr.thread_id = i.thread_id and rr.status = 'pending') as pending_requests
from ids i
left join orchestration_v2_projection_threads t on t.thread_id = i.thread_id
left join orchestration_v2_projection_runs r
  on r.run_id = (select r2.run_id from orchestration_v2_projection_runs r2
                  where r2.thread_id = i.thread_id order by r2.ordinal desc limit 1)
left join orchestration_v2_projection_turn_items ti
  on ti.turn_item_id = (select i2.turn_item_id from orchestration_v2_projection_turn_items i2
                         where i2.thread_id = i.thread_id order by i2.ordinal desc limit 1)
```

`EXPLAIN QUERY PLAN` for that statement with two ids `[measured]`:

```
CO-ROUTINE ids
SCAN 2 CONSTANT ROWS
SCAN i
SEARCH t  USING INDEX sqlite_autoindex_orchestration_v2_projection_threads_1 (thread_id=?) LEFT-JOIN
SEARCH r  USING INDEX sqlite_autoindex_orchestration_v2_projection_runs_1 (run_id=?) LEFT-JOIN
CORRELATED SCALAR SUBQUERY 4
SEARCH r2 USING INDEX orchestration_v2_projection_runs_thread_ordinal_idx (thread_id=?)
SEARCH ti USING INDEX sqlite_autoindex_orchestration_v2_projection_turn_items_1 (turn_item_id=?) LEFT-JOIN
CORRELATED SCALAR SUBQUERY 5
SEARCH i2 USING COVERING INDEX orchestration_v2_projection_turn_items_thread_ordinal_idx (thread_id=?)
CORRELATED SCALAR SUBQUERY 3
SEARCH rr USING COVERING INDEX orchestration_v2_projection_runtime_requests_thread_status_idx (thread_id=? AND status=?)
```

Every access is an index seek; there is **no table scan and no temporary B-tree**
`[measured]`. The three indexes that carry the line `[schema]`:

```
orchestration_v2_projection_runs_thread_ordinal_idx        (thread_id, ordinal) UNIQUE
orchestration_v2_projection_turn_items_thread_ordinal_idx  (thread_id, ordinal, turn_item_id)
orchestration_v2_projection_runtime_requests_thread_status_idx (thread_id, status)
```

### The busy set: every thread whose latest run is still running

That is the load-bearing measurement, run on the live database `[measured]`:

```
threads with a latest run running : 2
rows returned                     : 2
median query time (9 runs, warm)  : 0.016 ms
best                              : 0.014 ms
JSON for the state block          : 626 B
EXPLAIN                           : SCAN runs + USE TEMP B-TREE for RIGHT PART OF ORDER BY
```

The window-function form that produces that set (`row_number() over (partition by thread_id order by
ordinal desc)`) plans as `SCAN r USING INDEX orchestration_v2_projection_runs_thread_ordinal_idx`
plus `USE TEMP B-TREE FOR RIGHT PART OF ORDER BY`; at 1,232 rows it costs microseconds `[measured]`. To ask **only** "who is working now", the partial index
already exists and is cheaper still `[schema]` + `[measured]`:

```sql
select r.thread_id, r.status, r.ordinal
from orchestration_v2_projection_runs r
where r.status in ('queued','preparing','starting','running','waiting')
  and r.ordinal = (select max(r2.ordinal) from orchestration_v2_projection_runs r2
                    where r2.thread_id = r.thread_id);
```

```
SEARCH r USING INDEX orchestration_v2_projection_runs_recovery_idx (status=?)
CORRELATED SCALAR SUBQUERY 1 → SEARCH r2 USING COVERING INDEX …runs_thread_ordinal_idx (thread_id=?)
2 rows, median 0.0118 ms over 15 runs
```

Its index predicate is `WHERE status IN ('queued','preparing','starting','running','waiting')` on
`(status, thread_id)` `[schema]`, which is exactly the "someone is working" set.

### Cost of the same information at board scale

Same statement, warm connection, 100 thread ids (the 100 busiest threads, including both running
ones) `[measured]`:

| Shape | Statement count | Rows | Median | JSON |
|---|---|---|---|---|
| one grouped query | 1 | 100 | **1.61 ms** | 27,006 B (compact keys) / 34,714 B (the working tree's `last_type`/`last_state`/`last_at` keys) |
| the loop in `thread_snapshots` | 200 (2 per thread) | 100 | 1.85 ms | same |
| fresh connection, as the board does per request | 1 | 4 | connect **0.17 ms** + query **1.40 ms** | 865 B |
| fresh connection, 100 ids | 1 | 100 | connect **0.17 ms** + query **4.05–4.29 ms** (3 runs) | 27,006 B |

So the honest cost is **~4 ms per `GET /api/tickets` on a 100-ticket board**, because the handler
opens a new read-only handle each request `[code]` + `[measured]`. The grouped query is **1.1–1.3×
faster** than the current per-thread loop, not dramatically so — at this row count both are index
seeks. The grouped form wins on statement count (1 vs 200) and on round trips, not on raw median
`[measured]` + `[inference]`.

### The one line, and the words it may use

`turn_items.type` is a closed vocabulary in this projection `[measured]`, whole table at
51,875 rows (the database is live, so every count below drifts by a handful between statements):

```
reasoning 18,867 · command_execution 17,698 · dynamic_tool 4,667 · assistant_message 4,009
file_change 2,649 · user_message 1,238 · checkpoint 1,105 · web_search 407 · subagent 265
error 244 · notification 231 · file_search 201 · user_input_request 86
run_interrupt_request 63 · todo_list 54 · system_notice 22 · run_interrupt_result 21
compaction 19 · handoff 12 · approval_request 12 · secret_request 5
```

`status` values in use: `completed 50,713 · failed 1,107 · interrupted 29 · running 13 · cancelled 13`
`[measured]`; `pending` and `waiting` exist in the index predicate `[schema]` but had **zero rows**
at measurement time.

The honest line is `run_status` first, `type` second, elapsed last — and it is **only** shown while
the run is active `[inference]`:

| `run_status` | `last_type` | Card line | Why it is honest |
|---|---|---|---|
| `running` | `command_execution` | `working · running a command · 12s ago` | both facts are the latest row T3 wrote |
| `running` | `reasoning` | `working · thinking · 40s ago` | "thinking" is `reasoning`; no tokens, no percentage |
| `running` | `file_change` | `working · editing files · 2m ago` | — |
| `running` | `subagent` | `working · waiting on a sub-agent · 3m ago` | — |
| `running` | `approval_request` / `user_input_request` / `secret_request` | `needs an answer · …` | a request item is the agent asking |
| `waiting` | any | `waiting for an answer · 6m ago` | `waiting` is in T3's own run vocabulary; zero such rows observed |
| `queued` / `preparing` / `starting` | — | `queued · not started yet` | — |
| `completed` / `idle` | any | **no line** | the column and the ticket status already say it |
| `failed` / `cancelled` / `interrupted` / `rolled_back` | any | **no line** if the ticket is already `blocked` | the ticket note carries the reason |

Two measured reasons for that last rule `[measured]`:

1. **`last_type` is nearly always `checkpoint` on a finished thread.** Of the 100 busiest threads,
   `checkpoint 85`, `run_interrupt_request 6`, `command_execution 5`, `file_change 1`,
   `assistant_message 1`, `user_message 1`, `error 1`. A finished card would read
   `thread completed · checkpoint · 2880h ago` — three pieces of noise.
2. **`relativeTime` saturates.** `[code]`

   ```js
   if (seconds < 5) return 'just now';
   if (seconds < 90) return `${seconds}s ago`;
   const minutes = Math.round(seconds / 60);
   if (minutes < 90) return `${minutes}m ago`;
   return `${Math.round(minutes / 60)}h ago`;
   ```

   An 8-day-old run renders `192h ago`.

Do **not** derive liveness from `orchestration_v2_projection_threads.updated_at`: it is rewritten by
settle/visit bookkeeping long after the run ended (measured example: last turn item `08:10:16Z`,
`threads.updated_at` `08:40:17Z`, a 30-minute gap) `[schema]` + `[measured]`.

Freshness wording matters as much as the fields `[inference]`: `last_at` means *the last write T3
made*, so a long tool call legitimately freezes it. Write `last step 2m ago`, never `idle` or
`stalled`; a stall call needs a threshold from config, not a guess.

### Is a person being asked?

Two independent markers exist `[schema]`:

- `orchestration_v2_projection_runs.status = 'waiting'` — `waiting` is in T3's own run vocabulary (it is
  in the `runs_recovery_idx` predicate `[schema]`), and the watcher treats the equivalent thread status
  as "the reviewer asked a question and needs an answer" `[code]`:
  `if status == "waiting": store.record_verdict(..., "the reviewer asked a question …")`. Note the
  watcher reads that string from `t3 thread show`'s `thread.status`, while the board reads
  `runs.status`; the two agree by construction but were only ever `0`/`0` on pending requests here `[measured]`.
- `orchestration_v2_projection_runtime_requests.status = 'pending'`, whose kind is `user_input` or
  `command` `[measured]`; this is what the CLI reports as `thread.pendingRequestCount`.

Honesty limit: **no pending request existed at measurement time** — `runtime_requests` held
`resolved 85 + cancelled 1` for `user_input`, `resolved 12` for `command`, and **0 pending**
`[measured]`. The CLI's `pendingRequestCount` for a real thread returned `0`, matching the database
`[measured]`, but only the zero case could be cross-checked. Treat the positive path as unverified
until a live waiting thread is sampled; do not ship a card line that claims it without that sample.

The turn-item equivalent — `type LIKE '%_request' AND status = 'waiting'` — is **not** usable as
written: the shipped partial index is `WHERE type = 'link_request' AND status = 'waiting'`
`[schema]`, and `link_request` does not occur in ≈51.9 k rows. The real types are
`user_input_request`, `approval_request`, `secret_request`, `run_interrupt_request` `[measured]`.

---

## reviewer state

### What the ticket row already proves

`tickets.sqlite` carries the whole review lifecycle, written by the watcher `[code]`:

| Column | Written by | Meaning |
|---|---|---|
| `verify_thread_id` | `attach_verifier()` | the reviewer thread exists |
| `verify_run_id` | `attach_verifier()` / `attach_verify_run()` | the run the verdict must belong to |
| `verify_attempt` | `attach_verifier()` | attempts spent, compared against `verification.maxAttempts` |
| `verify_started_at` | `attach_verifier()` | when the reviewer was launched |
| `verify_verdict` | `record_verdict()` only | `ACCEPT`, `REJECT`, `FAILED`, `TIMEOUT` — empty until the watcher records one |
| `verify_finished_at` | `record_verdict()` | when the watcher recorded it |
| `verify_baseline` | `attach_verifier()` | fingerprint of the tree the reviewer is judging |

`store.review_label()` already turns that into one phrase for the card `[code]`:
`accept · first attempt`, `reject · attempt 1 of 2`, `pending · attempt 1 of 2`, `not reviewed`.

### What the ticket row cannot prove, and the join that fixes it

`verify_verdict = ''` covers **two different states**: the reviewer is typing, and the reviewer
answered but the watcher has not looked yet. The ticket row cannot separate them; the T3 run can
`[code]` + `[measured]`:

| `verify_thread_id` | ticket `verify_verdict` | verify thread's latest `runs.status` | Card line |
|---|---|---|---|
| empty | empty | — | `not reviewed` |
| set | `ACCEPT` / `REJECT` / `FAILED` / `TIMEOUT` | — | `accept · first attempt` (from `review_label`) |
| set | empty | `queued` / `preparing` / `starting` / `running` | **`reviewer running · …`** |
| set | empty | `completed` / `idle` | **`reviewer finished · verdict pending`** |
| set | empty | `waiting` | `reviewer needs an answer in T3` |
| set | empty | `failed` / `cancelled` / `interrupted` / `rolled_back` | `reviewer died · no verdict` |

Columns behind the last three rows: `orchestration_v2_projection_runs.status` and `.ordinal` for the
reviewer's `thread_id` = `tickets.verify_thread_id` `[schema]`. The phrase comes from two rows the
board already holds, so **no new query is needed** — the existing grouped query covers the reviewer
thread too, because `thread_snapshots` is called with the dev **and** verify ids together `[code]`:

```python
snapshots = thread_snapshots(
    Path(self.t3_db_path),
    [ticket.get("thread_id") or "" for ticket in tickets]
    + [ticket.get("verify_thread_id") or "" for ticket in tickets],
)
```

### The "verdict pending" window is real, and it is ~1 minute

Measured on `T-0003`, whose reviewer thread is `mcp:c033f178-e49f-4fc4-a9ec-d3c54d72dbd9`
`[measured]`:

```
T3  runs.ordinal=1  status=completed  completed_at = 2026-11-28T09:29:10.345Z
ticket  verify_finished_at                        = 2026-11-28T09:30:14.379621+00:00
lag                                               = 64.03 s
```

The watcher records a verdict only when a tick observes the thread, and the tick interval is
`intervalSeconds: 120` `[code]` + `[measured]`, so the window is bounded by one tick plus a
`thread show` call. The card is therefore right to say "verdict pending" — it is a state that lasts a
minute, not a rendering artefact. Its wording must not promise that the reviewer is still working
`[inference]`.

One semantic caution: T3's CLI reports `thread.status` values including `idle`; the database has no
thread status column at all, only `runs.status` `[schema]`. `thread_state.status` is therefore the
**last run's** status, and a completed thread shows `completed`, not `idle` `[measured]`. The
watcher's review stage accepts both (`if status not in ("completed", "idle")`) `[code]`, so the
distinction never changes a decision — but the card should say `completed`, never `idle`.

---

## tabs pattern

WAI-ARIA APG **was reachable during this research** — `https://www.w3.org/WAI/ARIA/apg/patterns/tabs/`
returned `HTTP 200` and was read `[measured]`. So the pattern is quoted, not reconstructed. The local
`~/.agents/skills/accessibility/references/A11Y-PATTERNS.md` §"ARIA tabs" says the same thing offline
`[code]`.

### What the APG requires `[spec]`

> The element that serves as the container for the set of tabs has role `tablist`.
> Each element that serves as a tab has role `tab` and is contained within the element with role `tablist`.
> Each element that contains the content panel for a `tab` has role `tabpanel`.
> If the tab list has a visible label, the element with role `tablist` has `aria-labelledby` … Otherwise, the `tablist` element has a label provided by `aria-label`.
> Each element with role `tab` has the property `aria-controls` referring to its associated `tabpanel` element.
> The active `tab` element has the state `aria-selected` set to `true` and all other `tab` elements have it set to `false`.
> Each element with role `tabpanel` has the property `aria-labelledby` referring to its associated `tab` element.

Keyboard `[spec]`: `Tab` puts focus on the active tab and, from the tab list, moves focus to the next
element outside it — *"the tabpanel unless the first element containing meaningful content inside the
tabpanel is focusable"*; `Left Arrow`/`Right Arrow` move between tabs and **optionally** activate on
focus (automatic activation); `Home`/`End` are optional; `Space`/`Enter` activates when activation is
manual.

### What the report dialog already does `[code]`

`board/static/index.html` implements the pattern correctly for a horizontal, auto-activating tab list:

```js
tab.role = 'tab';
tab.setAttribute('aria-selected', index === 0 ? 'true' : 'false');
tab.tabIndex = index === 0 ? 0 : -1;                 // roving tabindex
tab.setAttribute('aria-controls', `report-panel-${section.id}`);
panel.setAttribute('role', 'tabpanel');
panel.setAttribute('aria-labelledby', tab.id);
panel.hidden = index !== 0;
```

with `<div class="tabs" id="report-tabs" role="tablist" aria-label="Report sections">` `[code]` — an
`aria-label` because the dialog title is a heading, not the tab list's own label. The key handler
covers `ArrowRight`, `ArrowLeft`, `Home`, `End` with `preventDefault()`, moves focus with
`tabs.children[index].focus()`, and `selectReportTab()` keeps exactly one `tabIndex = 0` `[code]`.
Sections come from `store.report_sections()` (`summary`, `test`, `description`, `evidence`, …), so
tab ids are stable and `aria-controls` always resolves `[code]`.

Two gaps, both small `[spec]` + `[code]`:

1. **The panel is not focusable.** APG: `Tab` from the tab list lands on *the tabpanel* "unless the
   first element containing meaningful content inside the tabpanel is focusable". The Summary panel
   is a `<dl>` with no focusable child, so a keyboard user tabs past the report body to *Open raw
   markdown*. Fix: `panel.tabIndex = 0` (harmless when the panel does contain a link).
2. **`aria-orientation` is absent.** APG only requires it for `vertical`; the default is horizontal,
   so this is correct as-is. Stated so nobody "fixes" it wrongly.

Everything else matches the pattern; no change is warranted. The dialog itself is
`<dialog aria-labelledby="dlg-report-title">` with `showModal()`, which gives focus containment and
`Esc` for free `[code]`.

### The live dot

Recommendation: **text plus colour, and the text carries the meaning** `[inference]`, supported by
WCAG 1.4.1 (colour is not the only channel) in the local reference `[code]`:

- The card already prints `thread running · reasoning · 40s ago` **and** colours an 8 px
  `.dot` `[code]`. Keep both: a reader of a greyscale screen or with colour-vision deficiency loses
  nothing, because the words are the status.
- Mark the dot `aria-hidden="true"` — it repeats the text and would otherwise be an empty span in
  the accessibility tree `[inference]`.
- Do **not** add a pulse. If one is ever added it must be disabled under `prefers-reduced-motion`,
  which the page already respects for the card transition, the spinner and the progress bar `[code]`:

  ```css
  @media (prefers-reduced-motion: reduce) {
    .spinner { animation: none; border-right-color: currentColor; opacity: .6; }
    #progress::before { animation: none; width: 100%; opacity: .5; }
  }
  ```

  A blinking dot is also WCAG 2.2.2 territory (pause/stop/hide) `[code]`; the cheapest way to satisfy
  it is to not animate at all `[inference]`.
- Contrast: `--accent` `#4f46e5` on `--surface` `#ffffff` is a dark indigo in the AA range for a
  3:1 non-text indicator `[inference]`; the dot is decorative because the words carry the state, so
  this is not load-bearing.

---

## list view

### Table semantics `[spec]`

W3C's tables tutorial (fetched, `HTTP 200`) requires a caption and `scope="col"` on header cells:

> The `scope` attribute with the value `col` defines the direction of the header cells and associates
> them with the corresponding data cells. The `scope` attribute is also needed for larger tables with
> one header row or column.

and shows `<caption>` as the table's name. The board's list view `[code]`:

```js
head.innerHTML = '<tr><th>Ticket</th><th>Title</th><th>Status</th><th>Priority</th>' +
  '<th>Project</th><th>Review</th><th>Updated</th><th>Actions</th></tr>';
```

has neither. Add `<caption class="sr-only">Tickets, one row each</caption>` and `scope="col"` on each
`<th>`; the page already ships a `.sr-only` utility for `#live` `[code]`. Genuine `<table>` markup is
the right choice here and is what makes a row announce as "T-0003, Live run caught…, Done" instead of
eight loose cells `[inference]`.

The view switch is a plain `<button id="view-toggle">` whose label flips between `List` and `Board`
and whose choice persists in `localStorage` `[code]`. That is a mode switch, not a tab set: the two
views are mutually exclusive full-page presentations, and the board/keyboard model of an `<article
draggable>` grid is not a tab panel. A `role="tablist"` there would promise arrow-key semantics the
board does not implement `[inference]`. Consider `aria-pressed` on the toggle so its state is not
carried by its label alone `[inference]`.

### Table or grid: measured

Same JSON, same CSS (extracted from `board/static/index.html`), real Chromium
(`chrome-headless-shell 159.0.8154.6`) via Playwright, 11 rebuilds each, medians `[measured]`:

| Rows | Shape | Build (JS) | Layout | Sum | p90 layout | DOM nodes | Document height @1600 / @900 |
|---|---|---|---|---|---|---|---|
| 20 | 7-column board grid | 1.4 ms | 2.8 ms | **4.2 ms** | 3.3 ms | 368 | 1,386 px / 2,458 px |
| 20 | table, 8 columns | 0.9 ms | 1.9 ms | **2.8 ms** | 2.4 ms | 271 | 804 px / 1,194 px |
| 100 | 7-column board grid | 4.2 ms | 13.8 ms | **18.0 ms** | 16.7 ms | 1,728 | 6,202 px / 11,635 px |
| 100 | table, 8 columns | 2.9 ms | 8.2 ms | **11.1 ms** | 10.7 ms | 1,311 | 3,884 px / 5,834 px |

A repeat run at `1280×900` and `900×800` produced the same ordering within ±1 ms `[measured]`. So the
table is **1.5× cheaper at 20 rows and 1.6× cheaper at 100 rows** in script + style + layout, with
1.3× fewer nodes and **0.5–0.6× the scroll height** — the grid's columns make the page twice as tall
at the narrow width and 1.6× as tall at the wide one.

What is **not** measured: paint and compositing. `requestAnimationFrame` waits in the headless shell
resolve on the vsync interval (~33 ms for two frames for *both* shapes), which hides the difference
rather than revealing it `[measured]`; separating paint needs a tracing harness this research did not
build. The paint claim therefore stays `[inference]`: fewer nodes and less scroll should also mean
less paint, but that number is unverified.

`JSON.parse` of the per-thread state block for 100 tickets (12,106 B) is **below the 0.1 ms timer
resolution** `[measured]`. Parsing is never the reason to choose one view over the other.

The strongest finding is not table-vs-grid at all `[measured]`:

| Update | Cost at 100 cards |
|---|---|
| rebuild the whole board (what a 15 s poll does today) | **18.0 ms** of blocking script + layout |
| rewrite only the state-line text of the cards already on the page | **1.6 ms** |

The poll cadence is `setInterval(() => load().catch(() => {}), 15000)` `[code]`. Re-rendering 100
cards every 15 s is 18 ms per poll, i.e. **72 ms of main thread per minute** on a busy board; patching
the text nodes instead costs ~10 % of that (6.4 ms per minute). Neither is a fire, but patching is
what keeps a poll from landing in the same frame as a drag `[inference]`. Patch the state lines and
re-render only when the ticket set or the ticket status changes; then the grid-vs-table DOM
difference stops being the deciding factor, and the answer to "which view" is the one the person
reads faster `[inference]`.

---

## risks

### Payload on `GET /api/tickets`

Measured on the live board and extrapolated `[measured]`:

| Payload | Bytes |
|---|---|
| today, 3 real tickets | 5,007 B (one ticket dict measures 1,560 B; the rest is the shared envelope) |
| 100 synthetic tickets, no state | 126,909 B (1,269 B/ticket) |
| 100 synthetic tickets, with `thread_state` + `review_state` | 153,614 B (1,536 B/ticket) |
| delta | **+26,705 B, +21 %** |
| the state block alone, 100 thread rows | 27,006 B (compact keys) / 34,714 B (`last_*` keys) |
| 25 tickets, projected | 31.7 KB → 38.4 KB |

`+21 %` on every poll is affordable on a tailnet, but it is not free: `Cache-Control: no-store` is set
on every response `[code]`, so nothing is cached and the whole body travels every 15 s per open tab.

### Polling

`load()` runs every 15 s per open tab `[code]`. Each call currently opens the T3 database twice
(`t3_projects()` and the snapshot query) plus `tickets.sqlite` `[code]`; the snapshot statement adds
**~4 ms** at 100 threads `[measured]`. Three tabs is 12 requests/minute, each a full JSON body and a
read-only handle on a 1.75 GB WAL database. That is acceptable — 4 ms every 15 s is 16 ms per
minute, **0.03 %** duty on the board's main thread — and it is also the first thing that will bite if
the board is opened on a phone and left open `[inference]`. Options, cheapest first
`[inference]`: pause polling when `document.hidden`; cache the snapshot for one interval inside the
handler; or send only per-ticket deltas (`{id, thread_state, review_state}`) once the client has the
rest.

### Auth

`GET /api/tickets` takes **no token** — by invariant 8 the tailnet is the boundary, and the only read
routes that need `X-Board-Token` are `GET /api/fs` and `GET /api/tickets/<id>/live` `[code]` + `[measured]`
(the token is required on every `POST`). Adding per-thread activity therefore publishes, to anything on the tailnet, a coarse
fingerprint of what this machine is doing right now: which thread is executing a command, at what
second its last item changed, and whether a review is in flight. It does **not** publish command
text, file names or message text — the query selects `type`, `status`, `updated_at` and a count, never
`payload_json` `[measured]`. That matters: `turn_items.payload_json` averages 9,188 B and peaks at
2,838,316 B with full tool input and output, so a careless `select *` would leak the work *and*
multiply the payload by ~30× `[measured]`. Keep the column list explicit.

Related, already true and unchanged: `thread_id` is in the public payload, and `GET /api/threads`
returns up to 200 recent T3 threads unauthenticated `[code]` — the marginal disclosure of a state line
is small next to that, but it is not zero `[inference]`.

### Schema coupling and deployment

- The line depends on T3's private projection schema (`orchestration_v2_projection_*`), versioned only
  by vendor code; an upgrade can rename a table and the card silently loses its line `[inference]`.
  The handler already fails soft: `except sqlite3.Error: return {}` yields no line rather than a
  wrong one `[code]`. Keep that behaviour, and log it once `[inference]`.
- The same statement is read by an unauthenticated route on a polling timer, through a read-only
  handle with `busy_timeout=200` `[code]`. A short timeout is right — a board must not queue behind
  the writer — but it means a busy WAL can return *no* line for one poll. The UI must not render
  "no line" as "not running" `[inference]`.
- **Today the line is invisible.** `thread_snapshots` is uncommitted, so the deployed board returns
  `thread_state: null` `[measured]`. Restarting the board before committing shows nothing; committing
  without restarting shows nothing either. This is the same failure mode `docs/ui-live-status.md`
  recorded for `/api/tickets/<id>/live`, and it is worth checking both the commit and the
  `ActiveEnterTimestamp` before believing any screenshot `[inference]`.

---

## sources

All paths on this machine unless a URL is given. T3 and ticket reads used `?mode=ro` +
`PRAGMA query_only=ON`.

| Claim | Source | Badge |
|---|---|---|
| Table columns, indexes, partial-index predicates | `~/.t3/userdata/statev2.sqlite` → `sqlite_master` | `[schema]` |
| Row counts, file/WAL size, type and status distributions | same DB, `count(*)` / `group by` | `[measured]` |
| `EXPLAIN QUERY PLAN` for the grouped, busy-set and single-thread statements | same DB, plan output quoted verbatim | `[measured]` |
| 2 busy threads; 0.016 ms grouped / 0.0118 ms partial-index; 626 B | same DB, 9 and 15 runs, `time.perf_counter` | `[measured]` |
| 100-thread timings, 1.61 ms vs 1.85 ms, 27,006 / 34,714 B | same DB, 100 busiest thread ids, 9 runs each | `[measured]` |
| Fresh-connection cost 0.17 ms + 4.05–4.29 ms | `python3 /tmp/fresh.py <ids>`, 3 runs each | `[measured]` |
| 85/100 busiest threads end on `checkpoint` | same DB, `order by ordinal desc limit 1` per thread | `[measured]` |
| Reviewer run `completed_at` `09:29:10.345Z` vs `verify_finished_at` `09:30:14.379621` → 64.03 s | `statev2.sqlite` + `tickets.sqlite`, ticket `T-0003` | `[measured]` |
| `pendingRequestCount`, `status`, `latestRunId` on the CLI thread object | `t3 thread show mcp:72b322c1-… --json --messages 1` | `[measured]` |
| Zero pending requests; `resolved`/`cancelled` only | `orchestration_v2_projection_runtime_requests`, `runs` | `[measured]` |
| Review columns, `attach_verifier`, `record_verdict`, `review_label` | `tickets/store.py` | `[code]` |
| Review stage status handling (`waiting` → needs input, `completed`/`idle` accepted) | `watcher.py` `verification_step` | `[code]` |
| Snapshot query, both-id call, soft failure | `board/server.py` `thread_snapshots`, `/api/tickets` | `[code]` |
| `thread_state` is uncommitted; deployed payload has no state | `git log -S "thread_state" -- board/server.py` empty; `ActiveEnterTimestamp 16:22:09`; `curl /api/tickets` → `thread_state: null` | `[measured]` |
| Tabs roles/states/keyboard; panel-focus rule | <https://www.w3.org/WAI/ARIA/apg/patterns/tabs/> (HTTP 200, read this session) | `[spec]` |
| Offline corroboration of the tabs pattern | `~/.agents/skills/accessibility/references/A11Y-PATTERNS.md` §"ARIA tabs" | `[code]` |
| `scope="col"` and `<caption>` for a one-header table | <https://www.w3.org/WAI/tutorials/tables/one-header/> (HTTP 200, read this session) | `[spec]` |
| WCAG 1.4.1 / 2.2.2 / 4.1.3 wording | `~/.agents/skills/accessibility/references/WCAG.md` | `[code]` |
| Render cost, node counts, scroll height, 1.6 ms incremental update | `chrome-headless-shell 159.0.8154.6` via `playwright-core` (`~/.hermes/hermes-agent/node_modules`), CSS extracted from `board/static/index.html`, 11 rebuilds per shape, medians | `[measured]` |
| Paint cost | not measurable here — rAF waits collapsed to the vsync interval for both shapes | `[inference]` |
| Payload 5,007 B today; 126,909 → 153,614 B at 100 tickets | live `curl /api/tickets` + synthetic 100-ticket body built from the same fields | `[measured]` |
| Poll cadence, `no-store`, token rules, `relativeTime`, `prefers-reduced-motion` | `board/static/index.html`, `board/server.py` `_send`, `docs/api.md` | `[code]` |
| Wording of each card line, "no line when finished", patch-don't-rebuild, delta payloads | reasoning over the measurements above | `[inference]` |

Related pages: `docs/ui-live-status.md` (the modal log, cadence and heartbeat), `docs/architecture.md`
(data model, state machine), `docs/api.md` (endpoints and auth), `docs/operations.md` (the watcher
under systemd).
