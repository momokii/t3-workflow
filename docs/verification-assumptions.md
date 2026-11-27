#
> **Implementation status (2026-11-28).** All six gaps in §3 are closed in `watcher.py` and tested in
> `tests/test_verify.py`: the verdict is parsed from any line that consists of the token (markdown
> emphasis, list marker and a trailing house-style line tolerated; contradictions refused), only
> `role == "assistant"` messages count, the reply must carry the verified `runId`, a thread with
> `runCount > 1` blocks, an unknown status blocks with its own note, and one `thread show` supplies
> status, run anchors and text so the three cannot disagree.
 Verification stage: what the CLI actually returns

The verification stage (`watcher.py:394-541`) was written against four assumptions about `t3`
output. This file checks each one against the CLI binary on this machine
(`~/.t3/runtime/versions/0.1.9/t3`, `t3 v0.1.9`) and against the existing threads in
`~/.t3/userdata/statev2.sqlite` (opened `mode=ro`). No thread was created, no message was sent,
no row was written. Every claim carries a badge:

- `[measured]` — produced by a command run here, output quoted.
- `[code]` — read out of the CLI's embedded JavaScript, quoted with its line in the `strings` stream.
- `[schema]` — read out of the SQLite schema or a query result.
- `[inference]` — reasoning from the above, not directly observable.

## 1. What was measured

### 1.1 `thread start --json`

Not observable without creating a thread, so this is read from the CLI source `[code]`.

```
$ strings -n 6 ~/.t3/runtime/versions/0.1.9/t3 > /tmp/t3strings.txt
$ sed -n '1129051,1129055p' /tmp/t3strings.txt
	ThreadLaunchResult = Struct({
		threadId: ThreadId,
		projectId: ProjectId,
		runId: NullOr(RunId)
```

The handler prints exactly that value `[code]` (`sed -n '1129300,1129316p'`):

```js
const launched = yield* tools.call("t3_thread_launch", { projectId, title, message,
    ...workspaceStrategy === void 0 ? {} : { workspaceStrategy },
    ...isSome(flags.provider) && isSome(flags.model) ? { modelSelection: {
        instanceId: flags.provider.value, model: flags.model.value } } : {}
}, ThreadLaunchResult);
if (flags.wait && launched.runId !== null) { ... return yield* finishRun(...) }
yield* flags.json ? printJson(launched) : log(launched.threadId);
```

So the JSON is `{"threadId": …, "projectId": …, "runId": … | null}`: three keys, **no `status`,
no `link`, no `title`**. `threadId` is required; `runId` is `NullOr(RunId)`. The nullable value comes
from the MCP tool, which returns `runId: run?.id ?? null` `[code]`
(`sed -n '1174502p' /tmp/t3strings.txt`) — null when the launch produced no run. Because the schema
is strict, the key is always present; it can be `null`, never absent.

`--provider` without `--model` is a usage error before anything is sent `[code]`
(`sed -n '1129303p'`): `"--provider and --model go together."`.

Exit codes `[code]`:

| path | code | source |
|---|---|---|
| success | 0 | `defaultTeardown` → `onExit(0)` (`sed -n '828255p'`) |
| any CLI error (usage, unreachable server, failed tool) | 1 | `getErrorExitCode` falls through to `return 1` (`sed -n '828277,828283p'`) |
| `--wait` and the agent asks a question | 2 | `ThreadNeedsInputError` `[errorExitCode] = 2` (`sed -n '1129012p'`) |
| `--wait` and the run did not complete | 1 | `ThreadRunEndedError`, no override |
| Ctrl-C | 130 | `hasInterruptsOnly` → `onExit(130)` (`sed -n '828256p'`) |

Measured on the two failure paths that need no thread `[measured]`:

```
$ t3 thread show not-a-real-thread --json ; echo rc=$?
rc=1                       # stdout empty
stderr: Thread not-a-real-thread is no longer available.

$ t3 thread start /nonexistent-path-xyz - --provider pi < /dev/null ; echo rc=$?
rc=1                       # stdout empty
stderr: The message is empty.
```

A failing `start` therefore prints nothing on stdout, so `json.loads` would raise anyway — the
watcher's `returncode != 0` test is the primary guard and it holds.

### 1.2 `thread show --json --messages N`

Real thread, 3 runs, `completed` `[measured]`:

```
$ t3 thread show 6103ae3d-90fa-4c50-b370-d721e59d1d1e --json --messages 2   # one line, rc=0
{
  "thread": {
    "threadId": "6103ae3d-90fa-4c50-b370-d721e59d1d1e",
    "status": "completed",
    "latestRunId": "run:thread:6103ae3d-90fa-4c50-b370-d721e59d1d1e:ordinal:3",
    "activeRunId": null,
    "runCount": 3,
    "itemCount": 94,
    "pendingRequestCount": 0,
    "model": "opencode-go/deepseek-v4.1-flash",
    "runtimeMode": "full-access"
  },
  "messages": [
    { "role": "user", "runId": "run:thread:6103ae3d-…:ordinal:3", "text": "so like now you have … (237 chars)" },
    { "role": "assistant", "runId": "run:thread:6103ae3d-…:ordinal:3", "text": "Correction first: **my previous report … (2463 chars)" }
  ]
}
```

(21 of the 30 `thread` keys elided above; commands and the full list are in §4.)

Shape `[measured]`:

- Top level: exactly `thread`, `messages`.
- `thread`: `threadId, projectId, title, link, createdBy, creationSource, status, latestRunId,
  activeRunId, providerInstanceId, model, runtimeMode, interactionMode, linkedPullRequest,
  pullRequests, titleRegeneration, branch, worktreePath, parentThreadId, relationshipToParent,
  runCount, itemCount, pendingRequestCount, archived, settled, settledAt, snoozed, snoozedUntil,
  createdAt, updatedAt` — 30 keys, matching `OrchestratorMcpThreadDetail` `[code]`
  (`sed -n '833012,833040p'`).
- Each message: **only** `role`, `runId`, `text`. No `itemId`, no `type`, no `createdAt`, no
  tool fields. `[measured]`
- `text` is the full stored markdown, untruncated: the CLI pages with `readFullText` until
  `textTruncated` is false `[code]` (`sed -n '1129185,1129200p'`). Longest sampled assistant
  message: 7201 characters. `[measured]`
- The CLI prints compact single-line JSON via `JSON.stringify` `[code]`
  (`printJson = (value) => log(encodeJson$1(value))`, `sed -n '1129035p'`), so there is no
  pretty-printing to disturb `text`.

`role` is derived, not reported `[code]` (`sed -n '1129354,1129362p'`):

```js
const { thread, messages: latest } = yield* readLatestMessages(tools, threadId, flags.messages);
const messages = yield* forEach$1(latest, (item) => readFullText(tools, threadId, item).pipe(
  map$11((text) => ({ role: item.type === "user_message" ? "user" : "assistant", runId: item.runId, text }))));
if (flags.json) return yield* printJson({ thread, messages });
```

`readLatestMessages` filters to `item.type === "user_message" || item.type === "assistant_message"`
`[code]` (`sed -n '1129174p'`), so tool calls and plan steps never appear in `messages` and `role`
is always exactly `user` or `assistant`.

`--messages N` counts **messages**, not turns and not timeline items `[measured]`:

```
$ for n in 1 3 5; do t3 thread show 6103ae3d-… --json --messages $n | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["messages"]))'; done
1
3
5
$ t3 thread show 6103ae3d-… --json            # no flag
6                                             # only 6 messages exist
itemCount = 94 while the thread holds 6 messages
```

Default is 10, not "all" `[code]` (`messages: Int$2("messages")…withDefault(10)`, `sed -n '1129349p'`);
confirmed on a 9-run thread that returned exactly 10 `[measured]`.

One run can hold **several** assistant messages `[measured]` — thread `06ca204b…`, run 9:

```
(8,assistant,163) (8,assistant,100) (8,assistant,162) (8,assistant,3144)
(9,user,283) (9,assistant,214) (9,assistant,239) (9,assistant,182) (9,assistant,155) (9,assistant,7201)
```

There is no "final" marker in the payload; the last element of `messages` is the last message of the
latest run. Checked on every thread with more than one run in the database: `messages[-1].runId ==
thread.latestRunId` on 14/14. `[measured]` — via the projection tables, `select payload_json … order by
created_at desc limit 1` vs `select run_id … order by ordinal desc limit 1`.

That also fixes the shape of the "final reply" rule: it is *the last message*, not *the last assistant
message of a named run*. They coincide today only because a run always ends with an assistant message
in this data.

`runId` on a message can be `null` `[measured]` — imported v1 history:

```
$ t3 thread show 'import:pi:01a17d5a-…' --json --messages 1
{"thread":{"status":"idle","latestRunId":null,"activeRunId":null,"runCount":0,"itemCount":201,…},
 "messages":[{"role":"assistant","runId":null,"text":"No problem — here are your login details …"}]}
```

### 1.3 Status values

The domain is closed `[code]`:

```
$ sed -n '819999,820009p' /tmp/t3strings.txt
	OrchestrationV2RunStatus = Literals$3([
		"preparing", "queued", "starting", "running", "waiting",
		"completed", "interrupted", "failed", "cancelled", "rolled_back"

$ sed -n '832948p' /tmp/t3strings.txt
	OrchestratorMcpThreadStatus = Union([Literal("idle"), OrchestrationV2RunStatus]);
```

The thread's `status` is derived from the latest run `[code]` (`sed -n '899637,899652p'`):

```js
function shellStatusFromStoredRunStatus(status) {
	switch (status) {
		case null: return "idle";
		case "preparing": … case "rolled_back": return status;
		default: return "failed";
```

So: `idle` + the ten run statuses = eleven possible values, and an unrecognised stored value is
reported as `failed`, not passed through. `idle` means "no run at all": 37 live threads have zero
runs, and one of them measures `status: "idle"`. `[measured]`

What is actually present in the database `[schema]`:

```
$ sqlite3 -readonly ~/.t3/userdata/statev2.sqlite \
  "select status, count(*) from orchestration_v2_projection_runs group by status"
completed 944 | failed 114 | interrupted 20 | running 5 | cancelled 3 | rolled_back 2
```

`preparing`, `queued`, `starting`, `waiting` never appear in this snapshot because they are
in-flight states; `idle` is not a run status, it is the thread-level value for "no run".
Compared with the list assumed in the review brief, **`rolled_back` is the missing value** — and the
watcher already handles it (`watcher.py:522`). No extra value exists.

What the watcher does today with a status it was not told about `[code]`, in order
(`watcher.py:515-533`):

| status | branch | effect |
|---|---|---|
| `preparing` `queued` `starting` `running` `unknown` | `watcher.py:516` | stay in `review`, "waiting for the reviewer" |
| `waiting` | `watcher.py:518` | `record_verdict("FAILED")` → `blocked` |
| `failed` `cancelled` `interrupted` `rolled_back` | `watcher.py:522` | `record_verdict("FAILED")` → `blocked` |
| `completed`, `idle`, **anything else** | falls through to `watcher.py:527-533` | fingerprint check, then parse the verdict |

`thread_status()` maps every failure mode (missing CLI, timeout, non-zero exit, non-JSON, missing
field) to the literal `"unknown"` (`watcher.py:89-108`), which is in the *waiting* set — a good
choice. An unrecognised status string on an otherwise successful read is not caught by any branch:
it is treated as *finished*. There is no silent accept, because a verdict line must still be read
verbatim, but the ticket is then blocked with the misleading note `review: no verdict line in the
reply` instead of naming the status. `[inference]`

### 1.4 Newlines and whitespace through the CLI

Nothing is transformed on the way out `[code]`: `readFullText` concatenates stored chunks and returns
them unchanged; `printJson` is `JSON.stringify`; no `trim`, no wrap, no truncation.

`[measured]` on 300 sampled assistant messages from the projection table: 0 contain `\r`, 0 end in
whitespace, and the JSON carries real newlines as `\n` escapes that survive `json.loads` (37-line
message above; tail repr `'…either fixed.\n\nSkills: `shipping-and-launch`, `verification-before-completion`.'`).

Whitespace is therefore not the risk. Two real transformations are `[measured]`, both from the model
side rather than the CLI:

1. **A trailing line after the intended last line.** 12 of 400 sampled assistant messages (3 %) end
   with a `Skills: …` line — this harness's own convention for the end of a report. A reviewer
   running here will put that line *after* `VERDICT: …` unless the prompt wins, and today
   `parse_verdict` then reads `Skills: …` as the verdict line, returns `("", "")`, and the ticket
   goes to `blocked` with `FAILED`. Fail-closed, but it burns an attempt.
2. **Markdown emphasis.** 67 of 400 sampled assistant messages (17 %) end with a line containing
   `**`. `VERDICT_RE` is anchored (`^VERDICT:…$`), so `**VERDICT: ACCEPT**`, `` `VERDICT: ACCEPT` ``,
   `- VERDICT: ACCEPT` and `VERDICT: ACCEPT.` all fail to match.

Prose does not become a verdict by accident: 15 of 400 messages mention "VERDICT" somewhere, and
**0 of 15** contain a line that is exactly `VERDICT: ACCEPT|REJECT`. `[measured]` So scanning for the
last line that *is* the token — rather than requiring it to be the final line — is safe on this
corpus.

## 2. What the code assumes

| # | Assumption | Where |
|---|---|---|
| 1 | `t3 thread start <project> - --provider P --model M --json` prints JSON with `threadId` and `runId` | `watcher.py:479-484` |
| 2 | `t3 thread show <id> --json --messages N` returns `{"thread": {…}, "messages": [{… "text"}]}` | `watcher.py:406-421`, `watcher.py:423-430` |
| 3 | `--messages N` returns the reviewer's whole reply in `messages[-1]["text"]` | `watcher.py:423-430` |
| 4 | `thread show` status is one of `preparing, queued, starting, running, waiting, completed, failed, cancelled, interrupted, idle` | `watcher.py:515-525` |
| 5 | The verdict is the last non-empty line of that reply and reads `VERDICT: ACCEPT\|REJECT` | `watcher.py:330`, `watcher.py:394-403` |
| 6 | The verdict belongs to the run this stage started | `docs/architecture.md:170` claims it; no code checks it |

Code, verbatim `[code]`:

```python
# watcher.py:478-486
try:
    payload = json.loads(proc.stdout)
    thread_id = payload["threadId"]
except (json.JSONDecodeError, KeyError) as exc:
    ...
updated = store.attach_verifier(
    ticket["id"], thread_id, str(payload.get("runId") or ""),
    selection["provider"], selection["model"], baseline,
)

# watcher.py:423-430
def verifier_reply(config: dict, thread_id: str, runner=subprocess.run) -> str:
    data = thread_show(config, thread_id, runner=runner)
    items = data.get("messages") or data.get("items") or []
    for item in reversed(items):
        text = (item.get("text") or "").strip()
        if text:
            return text
    return ""
```

Two things `verifier_reply` throws away that the payload carries: `role` and `runId`. It also
falls back to a key (`items`) that `thread show` never emits — that key belongs to the MCP
`t3_thread_read` shape, not the CLI `show` shape. Harmless, but it documents a guess.

## 3. Gaps and recommended changes

| # | Assumption | Measured | Verdict | Smallest fix |
|---|---|---|---|---|
| 1 | `start --json` gives `threadId` + `runId` | `{threadId, projectId, runId}` with `runId: NullOr(RunId)`; the MCP tool returns `run?.id ?? null` | **partly wrong**: `threadId` is guaranteed, `runId` is nullable, and there is no `status` key | keep the `threadId` requirement; treat `runId == ""` as "learn the run id on the first poll" — read `latestRunId` + `runCount` from the first successful `thread show` and store them, instead of failing or comparing against `""` |
| 2 | `show --json` returns `{thread, messages[].text}` | exactly that; message keys are `role`, `runId`, `text` | **correct** | none for the shape; add `"runId"` and `"role"` to the fields the watcher reads |
| 3 | the verdict is `messages[-1]["text"]` | a run holds several assistant messages; the last element is the last message of the latest run (14/14 threads) | **correct in practice**, wrong as a rule: it is "last message", not "last assistant message of the verified run" | pick `max(messages, key=position where role == "assistant" and runId == verify_run_id)` and refuse when none matches |
| 4 | status ∈ the ten listed + `idle` | `idle` + 10 run statuses (including `rolled_back`), unknown → `failed`; DB shows 6 of them live | **correct and complete**; the brief's list simply omits `rolled_back`, which the code handles | none |
| 5 | unknown status handling | unknown *read failure* → `"unknown"` → wait (safe); unknown *literal* → falls through to the verdict path | **unsafe shape, safe outcome**: never a silent accept, but a status the code has never seen is treated as finished and reported as "no verdict line" | add `if status not in ("completed", "idle"): block with the status` before the fingerprint check — one line, and every future status fails closed with the right note |
| 6 | the reply keeps its newlines | newlines/whitespace preserved byte-for-byte by the CLI; 0/300 have trailing whitespace | **correct** | none |
| 7 | the verdict is the *last* non-empty line | 3 % of real assistant messages append a `Skills:` line; 17 % end with markdown emphasis; 0/15 prose mentions of "VERDICT" form a token line | **too strict**: fails closed on formatting, and blocks good reviews | normalise each line (strip, drop a leading list marker, drop surrounding `*`/`_`/`` ` ``, drop a trailing `.`), collect the lines that are exactly the token, take the last, and refuse when both values appear. Still refuses prose; tolerates a trailing house-style line |
| 8 | the verdict belongs to this run (`verify_run_id`) | `thread show` exposes `latestRunId`, `activeRunId` **and** `runCount`; `verification_step` reads none of them | **real gap** — the field exists and is unused | before parsing: require `thread["runCount"] == 1` **or** `thread["latestRunId"] == ticket["verify_run_id"]`; on mismatch, block with the run ids in the note |

### 3.1 The stored run id is never compared (design gap)

`[measured]` the payload carries all three anchors:

```
"latestRunId": "run:thread:6103ae3d-…:ordinal:3"     # the run that produced the last message
"activeRunId": null                                   # non-null only while a run is in flight
"runCount": 3                                         # how many turns this thread has had
```

A live `running` thread shows `activeRunId == latestRunId`; a finished one shows `activeRunId: null`.

`[code]` `verification_step` uses only `status` and the message text. `tickets.verify_run_id` is
written at `tickets/store.py:327` and read nowhere; `verifier_reply` drops the per-message `runId`
too. `docs/architecture.md:170` lists "A verdict exists and is the last line" as the check and
`docs/independent-verification.md:502` claims "message `runId` equals the stored `verify_run_id`" —
that check is documented but not implemented.

`[inference]` what the hole actually costs today. A second turn on the reviewer thread (a `thread
send`, a human replying in T3, or a retried send) makes the thread's last message that new turn's
message, so the stale verdict stops being read and the ticket blocks. The exposure is the window
where the thread is terminal *and* the newer turn has not produced a message yet: the old
`VERDICT: ACCEPT` is still the last text, still parses, and is accepted for a run the reviewer has
already moved past. `runCount` closes it outright: a reviewer thread created by this stage must
have `runCount == 1` at verdict time, and `latestRunId` must equal `verify_run_id`.

Smallest implementation, in `verification_step` before `parse_verdict`:

```python
data = thread_show(config, thread_id, runner=runner)          # one call, not two
run_id = str(ticket.get("verify_run_id") or "")
latest = data.get("thread") or {}
if run_id and latest.get("latestRunId") not in (None, run_id):
    store.record_verdict(ticket_id, "FAILED", f"verdict is not from run {run_id}")
    store.move(ticket_id, "blocked", "review: stale run")
    return f"{ticket_id}: reviewer moved past the verified run → blocked"
```

`activeRunId` is not needed: `latestRunId` plus `runCount` answer the question, and `runCount`
alone answers "is this still a fresh context". `thread_status()` and `verifier_reply()` also make
two separate CLI calls per poll with different `--messages` values; folding them into one
`thread show --json --messages 5` removes a race between the two reads (the status and the text
come from the same transaction, not two).

## 4. Sources

### CLI binary

`T3B=~/.t3/runtime/versions/0.1.9/t3` (`t3 v0.1.9`, Go binary with the JS bundle embedded as strings).

| Claim | Command |
|---|---|
| run status literals | `strings -n 6 $T3B > /tmp/t3strings.txt && sed -n '819999,820009p' /tmp/t3strings.txt` |
| thread status domain (`idle` ∪ run statuses) | `sed -n '832948p' /tmp/t3strings.txt` |
| thread status derivation, unknown → `failed` | `sed -n '899637,899652p' /tmp/t3strings.txt` |
| `ThreadLaunchResult = {threadId, projectId, runId}` | `sed -n '1129051,1129055p' /tmp/t3strings.txt` |
| `start` prints that value; `--provider`/`--model` pairing | `sed -n '1129285,1129316p' /tmp/t3strings.txt` |
| `show` prints `{thread, messages}`, `role` derivation | `sed -n '1129346,1129368p' /tmp/t3strings.txt` |
| `--messages` default 10 | `sed -n '1129349p' /tmp/t3strings.txt` |
| `--messages` counts user/assistant items only | `sed -n '1129150,1129184p' /tmp/t3strings.txt` (filter at `1129174`) |
| full text, no truncation | `sed -n '1129185,1129200p' /tmp/t3strings.txt` |
| `printJson` = plain JSON.stringify | `sed -n '1129035p' /tmp/t3strings.txt` |
| exit codes (0 / 1 / 2 / 130) | `sed -n '828255,828283p'`, `sed -n '1129010,1129014p'` |
| MCP launch returns `runId: run?.id ?? null` | `sed -n '1174502p' /tmp/t3strings.txt` |
| CLI help (`start`, `show`) | `t3 thread start --help`, `t3 thread show --help` |

### Commands run against the live server (all read-only)

```
t3 --version
t3 thread start --help ; t3 thread show --help ; t3 thread --help
t3 thread show 6103ae3d-90fa-4c50-b370-d721e59d1d1e --json --messages 1   # 1 → 1 message
t3 thread show 6103ae3d-90fa-4c50-b370-d721e59d1d1e --json --messages 3   # 3 → 3 messages
t3 thread show 6103ae3d-90fa-4c50-b370-d721e59d1d1e --json --messages 5   # 5 → 5 messages
t3 thread show 6103ae3d-90fa-4c50-b370-d721e59d1d1e --json                # default → 6 (all it has)
t3 thread show 06ca204b-62f4-4118-ac64-346dbd247d6b --json                 # default → 10 messages, 9 runs
t3 thread show 'mcp:72b322c1-fc37-4640-9e92-c75484955db9' --json --messages 2   # running, activeRunId set
t3 thread show 'import:pi:01a17d5a-da21-766c-8ad6-65d7b91d9479' --json --messages 1  # idle, 0 runs, runId null
t3 thread show not-a-real-thread --json                                    # rc=1, stderr, empty stdout
t3 thread start /nonexistent-path-xyz - --provider pi < /dev/null           # rc=1, stderr, empty stdout
```

No thread was started, no message was sent, no row was written.

### SQLite (opened `uri=True&mode=ro`)

```
select thread_id, title from orchestration_v2_projection_threads
  where deleted_at is null order by created_at desc limit 8
select status, count(*) from orchestration_v2_projection_runs group by status
select role, count(*) from orchestration_v2_projection_messages group by role
select count(*) from orchestration_v2_projection_threads t where t.deleted_at is null
  and not exists (select 1 from orchestration_v2_projection_runs r where r.thread_id=t.thread_id)
```

Schemas used `[schema]`:

```
orchestration_v2_projection_threads: thread_id, project_id, title, default_provider, runtime_mode,
  interaction_mode, active_provider_thread_id, created_at, updated_at, archived_at, deleted_at,
  payload_json, provider_instance_id
orchestration_v2_projection_runs:    run_id, thread_id, ordinal, provider, provider_thread_id,
  status, requested_at, completed_at, payload_json, provider_instance_id
orchestration_v2_projection_messages: message_id, thread_id, run_id, node_id, role, streaming,
  created_at, updated_at, payload_json, search_text
```

### Repository

| File:line | What it establishes |
|---|---|
| `watcher.py:89-108` | `thread_status()` maps every failure to `"unknown"` |
| `watcher.py:330` | `VERDICT_RE = re.compile(r"^VERDICT:\s*(ACCEPT\|REJECT)$", re.IGNORECASE)` |
| `watcher.py:394-403` | `parse_verdict()` — last non-empty line only |
| `watcher.py:406-421` | `thread_show()` — `--json --messages N`, `{}` on any failure |
| `watcher.py:423-430` | `verifier_reply()` — reversed scan, no `role`, no `runId` |
| `watcher.py:479-486` | `start` payload read: `threadId` required, `runId` optional |
| `watcher.py:515-525` | the status branches, including the waiting set and `rolled_back` |
| `watcher.py:527-533` | fingerprint check, then `parse_verdict(verifier_reply(...))` |
| `tickets/store.py:320-334` | `attach_verifier()` writes `verify_thread_id`, `verify_run_id` |
| `tickets/store.py:336-352` | `record_verdict()` / `clear_verifier()` |
| `docs/architecture.md:170-173` | the documented pre-release checks |
| `docs/independent-verification.md:500-505` | the claim table, including "message `runId` equals the stored `verify_run_id`" |
| `docs/readonly-verifier.md:454` | G4: `runCount == 1` and message `runId == verify_run_id` |
| `tests/test_verify.py:101-112` | the parser tests, which use `FakeT3` and never see a real payload |
