# Independent verification stage — research and proposal

Research notes for adding a *fresh reviewer*: after the dev thread finishes and the host test stage
passes, a second thread driven by a **different** model re-checks the work in a fresh context, and the
ticket only reaches `done` when that reviewer accepts. On rejection the ticket returns to the dev
thread with the findings, up to a bounded number of attempts.

Nothing here is implemented. No ticket, report, thread or database row was created or modified while
writing this document; every claim is either a command output, a schema dump, a quoted source line or
explicitly marked as inference.

Badges used below:

| Badge | Meaning |
|---|---|
| `[measured]` | a command was run on this machine and this is its output |
| `[code]` | quoted from a file in this repository, with `path:line` |
| `[schema]` | quoted from a sqlite schema dump or a JSON payload read from `statev2.sqlite` |
| `[inference]` | my conclusion, not measured |

Repository state during research: the work started at `HEAD = 79af736` with a dirty tree (`board/server.py`,
`board/static/index.html`, `bridge/t3_bridge.py`, `tickets/catalog.py`, `tickets/cli.py`,
`tickets/store.py`, `watcher.py` — an in-flight translation pass by another agent) and that pass landed as
commit `158828b` while this document was being written. `watcher.py` was 480 lines, `tickets/store.py`
466 and `tickets/catalog.py` 178 both before and after that commit, so every `path:line` below still
resolves; all of them were re-checked after `158828b` `[measured]`.

One drift observed *during* the research, recorded because it changes what the release gate accepts:
the test-evidence marker was renamed from `[LULUS]` to `[PASS]` while this document was being written.
`watcher.py:270` now tests `proof.startswith("[PASS]")`, `tickets/store.py:299` writes `[PASS]`, and
`AGENTS.md:30`, `README.md:231`, `docs/architecture.md:136` all say `[PASS]` `[measured]`. The live ticket
database still holds the old marker — read-only query: `T-0001` → `[LULUS] 2026-11-25T15:48:03…`,
`T-0002` → `[LULUS] 2026-11-26T16:20:27…` `[schema]`. A verifier prompt must therefore accept **both**
markers when reading an existing ticket's proof.

---

## 1. What was measured

### 1.1 The `t3` CLI (v0.1.9)

```
$ t3 --version
t3 v0.1.9
$ ls -la ~/.local/bin/t3
lrwxrwxrwx ... /home/kelanach/.local/bin/t3 -> /home/kelanach/.t3/runtime/versions/0.1.9/t3
```

`t3 thread --help` lists exactly four subcommands: `list`, `start`, `send`, `show`. There is no
`stop`, no `steer`, no `answer`, no `approve`, no `usage`, no `cost`. `[measured]`

Flags, verbatim from `--help` `[measured]`:

| Subcommand | Flags |
|---|---|
| `start <project> <message>` | `--base-dir`, `--title`, `--provider`, `--model`, `--worktree`, `--branch`, `--wait`, `--json` |
| `send <thread> <message>` | `--base-dir`, `--wait`, `--json` |
| `show <thread>` | `--base-dir`, `--messages <int>`, `--json` |
| `list` | `--base-dir`, `--project`, `--json` |

`t3 project --help` lists only `add`, `remove`, `rename` — there is still no `project list`. `[measured]`

Missing on purpose, verified: **no flag sets runtime mode, interaction mode, model options
(`thinking`/`effort`/`variant`), attachments, or a parent thread.** `[measured]`

### 1.2 The CLI is a thin wrapper over the MCP tools

The 0.1.9 binary embeds its own TypeScript source (`file` reports `not stripped`). Reproducible
extraction:

```
$ strings -n 4 ~/.t3/runtime/versions/0.1.9/t3 | sed -n '1443106,1443132p'
threadStartCommand = make$241("start", { ... })
    const workspaceStrategy = yield* launchWorkspaceStrategy(flags);
    if (isSome(flags.provider) !== isSome(flags.model)) return yield* new ThreadUsageError({ detail: "--provider and --model go together." });
    const launched = yield* tools.call("t3_thread_launch", {
        projectId: project.id, title, message,
        ...workspaceStrategy === void 0 ? {} : { workspaceStrategy },
        ...isSome(flags.provider) && isSome(flags.model) ? { modelSelection: {
            instanceId: flags.provider.value,
            model: flags.model.value
        } } : {}
    }, ThreadLaunchResult);
```

Consequences, all `[code]` from that source region:

- `thread start` calls the same MCP tool the agents use (`t3_thread_launch`).
- It sends `modelSelection: {instanceId, model}` and **nothing else** — no `options`, no
  `runtimeMode`, no `interactionMode`, no `delegatedFrom`. That is why model options cannot be applied
  today (`tickets/catalog.py:12-17` documents the same limit).
- The CLI decodes the tool result with a narrower schema `ThreadLaunchResult = Struct({threadId,
  projectId, runId})` (`sed -n '1442839,1442843p'`), so `t3 thread start --json` prints exactly those
  three keys (`sed -n '1442839,1442843p'`). `[code]` the watcher already relies on this
  (`watcher.py:217`).
- `thread send` always uses `mode: "queue"` (`sed -n '1443147p'`), i.e. a busy thread is never
  interrupted.
- `thread list` always calls `t3_thread_list` with `includeSubagents: false`
  (`grep -n "includeSubagents: false"` → line 1443086 of the `strings` stream).

`--wait` composes with `--json` into a different, richer payload:

```
$ strings ... | sed -n '1443028,1443046p'
finishRun = ... (tools, input) => {
    ...
    if (input.json) yield* printJson({ threadId, runId, status, reply, error });
```

`reply` is the last `assistant_message` **of that run** (`runResultText` → `readTimeline(... "messages")`
→ `findLast(item.runId === runId && item.type === "assistant_message")`), and `error` the last `error`
item. Exit codes: `ThreadNeedsInputError` carries `[errorExitCode] = 2` (`sed -n '1442796p'`), and
`ThreadRunEndedError` (any non-`completed` status) has no override, so it exits 1 — matching the help
text "Exits 1 if the run fails and 2 if it waits for an answer". `[code]`

### 1.3 What the CLI actually returned on existing threads (read-only)

```
$ t3 thread list --json --project .../personal_prjct/t3-workflow
n= 1
  mcp:72b322c1-fc37-4640-9e92-c75484955db9 running None
```

The same project has a **child** thread in the database, and `show` on it works:

```
$ t3 thread show --json --messages 1 'thread:delegated-task:command%3Amcp%3Aaebf863a-...'
threadId              = thread:delegated-task:command%3Amcp%3Aaebf863a-...
status                = running
providerInstanceId    = pi
model                 = opencode-go/deepseek-v4.1-flash
parentThreadId        = mcp:72b322c1-fc37-4640-9e92-c75484955db9
relationshipToParent  = subagent
branch / worktreePath = None / None
```

So a child thread is readable by id but **hidden from `thread list`** — consistent with the hardcoded
`includeSubagents: false`. `[measured]`

Error shape for an unknown thread (important for the watcher's "unknown" fallback):

```
$ t3 thread show --json 'thread:does-not-exist'
exit=1
stderr: Thread thread:does-not-exist is no longer available.
```

`show --json` keys, verbatim from the output (`thread` object): `threadId, projectId, title, link,
createdBy, creationSource, status, latestRunId, activeRunId, providerInstanceId, model, runtimeMode,
interactionMode, linkedPullRequest, pullRequests, titleRegeneration, branch, worktreePath,
parentThreadId, relationshipToParent, runCount, itemCount, pendingRequestCount, archived, settled,
settledAt, snoozed, snoozedUntil, createdAt, updatedAt`; `messages[]` entries carry only `role`,
`runId`, `text`. **No token or cost field.** `[measured]`

`thread list --json` object keys: `threadId, projectId, title, link, createdBy, creationSource, status,
latestRunId, providerInstanceId, model, runtimeMode, interactionMode, linkedPullRequest, pullRequests,
settled, settledAt, snoozed, snoozedUntil, parentThreadId, relationshipToParent, itemCount, createdAt,
updatedAt` — no `branch`, no `worktreePath`, no `activeRunId`, no usage. `[measured]`

Thread status is `idle` or a run status (`OrchestratorMcpThreadStatus = Union([Literal("idle"),
OrchestrationV2RunStatus])`, `sed -n '1125907p'`), where

```
OrchestrationV2RunStatus = ["preparing","queued","starting","running","waiting",
                            "completed","interrupted","failed","cancelled","rolled_back"]
```
`[code]` (`sed -n '1111406,1111416p'`)

### 1.4 T3's database, read only

Opened as `file:~/.t3/userdata/statev2.sqlite?mode=ro` (`uri=True`). Relevant schemas `[schema]`:

```
orchestration_v2_projection_threads
  thread_id, project_id, title, default_provider, runtime_mode, interaction_mode,
  active_provider_thread_id, created_at, updated_at, archived_at, deleted_at,
  payload_json, provider_instance_id

orchestration_v2_projection_provider_threads
  provider_thread_id, thread_id, owner_node_id, provider, provider_session_id, status,
  first_run_ordinal, last_run_ordinal, updated_at, payload_json, driver, provider_instance_id

orchestration_v2_projection_provider_turns
  provider_turn_id, thread_id, provider_thread_id, node_id, run_attempt_id, ordinal,
  status, started_at, completed_at, payload_json

orchestration_v2_projection_runs
  run_id, thread_id, ordinal, provider, provider_thread_id, status, requested_at,
  completed_at, payload_json, provider_instance_id

orchestration_v2_projection_run_attempts
  attempt_id, thread_id, run_id, attempt_ordinal, root_node_id, provider,
  provider_thread_id, provider_turn_id, status, payload_json, provider_instance_id

orchestration_v2_projection_messages
  message_id, thread_id, run_id, node_id, role, streaming, created_at, updated_at,
  payload_json, search_text

orchestration_v2_projection_turn_items
  turn_item_id, thread_id, run_id, node_id, provider_thread_id, provider_turn_id,
  parent_item_id, ordinal, type, status, updated_at, payload_json

orchestration_v2_projection_subagents
  subagent_id, thread_id, run_id, parent_node_id, provider, provider_thread_id,
  child_thread_id, origin, status, started_at, completed_at, updated_at, payload_json,
  driver, provider_instance_id, completion_delivery_state

orchestration_command_receipts
  command_id, aggregate_kind, aggregate_id, accepted_at, result_sequence, status, error,
  command_type
orchestration_v2_command_receipts
  command_id, thread_id, command_type, accepted_at, result_sequence, status, error
```

**(a) provider + model on a thread.** Three places: the columns `default_provider` and
`provider_instance_id` on `orchestration_v2_projection_threads`, and inside that row's `payload_json`:

```json
"providerInstanceId": "pi",
"modelSelection": {"instanceId": "pi", "model": "opencode-go/deepseek-v4.1-flash",
                   "options": [{"id": "thinking", "value": "max"}]}
```
`[schema]` (row `thread:delegated-task:command%3Amcp%3Aaebf863a-…`)

Note `options` **is** stored here even though the CLI cannot set it — the thread that carried it was
created through T3's own MCP path, not through `t3 thread start`. `[measured]`

**(b) parent / lineage.** `payload_json` carries `lineage`:

```json
"lineage": {"parentThreadId": "mcp:72b322c1-…", "relationshipToParent": "subagent",
            "rootThreadId": "mcp:72b322c1-…"}
```
`[schema]`

Distinct values across all threads, counted by `creationSource` `[schema]`:

| `creationSource` | has `lineage.parentThreadId` | `relationshipToParent` | rows |
|---|---|---|---|
| `mcp` | yes | `subagent` | 221 |
| `web` | yes | `fork` | 1 |
| `mcp` / `web` / `server` / `mobile` | no | null | 57 |

There is also a `delegatedFrom` field in the thread-create command, but it is **not** a general
lineage switch: the source comment says *"Only the server sets this, with `linkOrigin`, for a linked
environment's delegated task"* (`sed -n '1113384,1113387p'`), and the tool schema annotates it *"Set by
a linked environment launching its agent's delegated task here; ignored otherwise"*
(`sed -n '1489558p'`). `[code]`

**(c) token usage / cost.** No table has a token, cost, usage, price or billing column — the whole
database was scanned with `pragma table_info` over every table; the only match was
`scheduled_tasks.webhook_token`. `[schema]`

Usage **is** present, but only as one field inside one payload:

```
orchestration_v2_projection_provider_turns.payload_json.tokenUsage
{"usedTokens": 118072, "maxTokens": 1000000, "inputTokens": 573947,
 "cachedInputTokens": 3304448, "outputTokens": 36579, "updatedAt": "2026-11-27T14:38:53.884Z"}
```
`[schema]` (thread `mcp:72b322c1-…`, ordinal 10)

The counters are **monotonic across a thread's turns**, not per-turn deltas `[schema]`:

| thread | ordinal | inputTokens | cachedInputTokens | outputTokens | usedTokens |
|---|---|---|---|---|---|
| `mcp:72b322c1-…` | 7 | 562 603 | 2 540 416 | 28 225 | 106 917 |
| | 8 | 566 347 | 2 969 472 | 30 994 | 110 746 |
| | 9 | 570 895 | 3 190 656 | 35 351 | 116 828 |
| | 10 | 573 947 | 3 304 448 | 36 579 | 118 072 |
| `6103ae3d-…` | 1 | 49 712 | 784 640 | 14 861 | 63 406 |
| | 2 | 120 641 | 1 513 728 | 26 070 | 81 465 |

Reading: `usedTokens ≤ maxTokens` looks like the live context size, while `input/output/cached` look
like session-cumulative counters — so a per-turn cost has to be obtained by subtracting consecutive
ordinals. `[inference]` Nothing in the schema says either way, and no CLI field exposes the numbers at
all.

**(d) addressing a finished turn's final assistant message.** Three equivalent paths `[schema]`:

1. `orchestration_v2_projection_messages` where `run_id = <run>` and `role='assistant'` ordered by
   `created_at` desc limit 1 — the text is `payload_json.text`, `streaming=0` when finished.
   Measured on run `…:ordinal:10`: one `user` row, one `assistant` row (2 578 chars).
2. `orchestration_v2_projection_turn_items` where `run_id = <run>` and `type='assistant_message'` — one
   row for the same run (`ordinal 10000003`, `completed`).
3. Through the CLI, which is what this repository should keep using:
   `t3 thread show <thread> --json --messages 1` gives `messages[].{role,runId,text}`, and
   `t3 thread show` pages the full text (`readFullText` loops on `nextTextOffset`, so a long verdict is
   not truncated).

Alternative event kinds exist in the projection, but **none is a "verdict"**: distinct
`turn_items.type` values measured include `user_input_request`, `approval_request`, `error`,
`subagent`, `notification`, `system_notice` — no structured-output type.
`orchestration_v2_projection_runtime_requests.kind` only ever takes `command` or `user_input`.
`[schema]`

### 1.5 Concurrency: T3 does not cap runs per project

```
runs with status='running':
   thread:delegated-task:command%3Amcp%3A7c4ee199 | project b65cc44a | ordinal 1
   mcp:72b322c1-fc37-4640-9e92-c75484955db9      | project 01807ea7 | ordinal 11
   thread:delegated-task:command%3Amcp%3Aaebf863a | project 01807ea7 | ordinal 1
   import:pi:01a18374-…                          | project a8f583e0 | ordinal 81
   1a022144-3688-4c06-a9ba-8f71d7e89984          | project cf3876ca | ordinal 196
```

Two of those five belong to the **same project** (`01807ea7`, t3-workflow) and were running
simultaneously. `[measured]` Searching the binary for a per-project or per-thread concurrency cap found
nothing (`grep -n "maxConcurrent"` only matches HTTP/2 `maxConcurrentStreams`). `[measured]`

### 1.6 Worktrees

`t3 thread start --help`: `--worktree` = "Run in a new git worktree based on this branch, tag, or
commit, instead of the project folder", `--branch` = "Branch name for the new worktree".
`launchWorkspaceStrategy` (`sed -n '1443057,1443066p'`) rejects `--branch` without `--worktree`
("`--branch needs --worktree <base-ref>.`"). `[code]`

A worktree thread recorded in the database `[schema]`:

```
branch        = t3/audit-pilar-kit-correctness
worktreePath  = /home/kelanach/.t3/worktrees/video-maker/t3-d27395cc-67891254c3817c95
```
and on disk it really is a git worktree, not a copy `[measured]`:

```
$ cat ~/.t3/worktrees/video-maker/t3-d27395cc-67891254c3817c95/.git
gitdir: /home/kelanach/Public/…/video-maker/.git/worktrees/t3-d27395cc-67891254c3817c95
```

Scratch measurement of what a worktree contains (throwaway repo in `/tmp`, no repository touched):

```
$ git status --porcelain
 M a.txt
?? b.txt
$ git diff                 # tracked changes only; b.txt is invisible
$ git diff --stat
 a.txt | 1 +
$ git add -N b.txt && git diff --stat
 a.txt | 1 +
 b.txt | 1 +
$ git worktree add -q /tmp/vtest-wt HEAD --detach && cat /tmp/vtest-wt/a.txt
one                       # the uncommitted "+two" is NOT there
```
`[measured]`

The MCP tool description says the same in words: *"Uncommitted edits are not copied"*
(`sed -n '1489555p'`). `[code]`

### 1.7 This repository, as it stands

`[code]`:

- States: `backlog, ready, in-progress, testing, review, done, blocked` (`tickets/store.py:33`), with
  `TRANSITIONS` at `tickets/store.py:34-42` and `force=False` enforcement in `move()`
  (`tickets/store.py:199`).
- Stage order: `step()` drains `review`, then `testing`, then `ready` (`watcher.py:393-417`).
- `maxConcurrent` counts `in-progress` (`watcher.py:414`), default 1 in
  `~/.config/t3-workflow/config.json`.
- Test gate is the exit code (`watcher.py:341-347`); release refuses without a `[PASS]` proof
  (`watcher.py:270`).
- `sync_threads()` maps thread status → ticket move: `completed|idle` → `testing`,
  `failed|cancelled|interrupted` → `blocked`, anything else untouched (`watcher.py:104-122`).
- `thread_status()` returns `"unknown"` on any CLI error (`watcher.py:86-101`), so an unreachable
  server never moves a ticket.
- Report fields today: `id, title, status, project, model profile, priority, created, updated,
  thread_id, link (only if `t3_link` is passed), test_cmd, body, approval, note, proof, history table`
  (`tickets/store.py:429-460`). `write_report()` calls `self.report(ticket_id)` **without** a link
  (`tickets/store.py:272`), so a generated report has no T3 link.
- Existing event kinds in the live database: `created, moved, forced, claimed, thread, continued,
  proof, report`. `[schema]` (`~/.local/state/t3-workflow/tickets.sqlite`, read-only)
- A ticket row may end `done` with `branch = NULL` — measured on T-0001 and T-0002, because
  `release()` takes the "nothing to push" path (`watcher.py:283-284`). `[schema]`
- The dev works *in the project folder*: `release()` is where the tree is committed, `git add -A` then
  `git commit` in place (`watcher.py:296-298`).

---

## 2. What the system can and cannot do

### A. Can a verifier thread be started with a chosen provider+model from the CLI?

**Yes** — the flags exist in `--help` `[measured]`, and the embedded source shows exactly how they are
forwarded to `t3_thread_launch` `[code]`. No thread was started here, so the *behaviour* below is read
from the source, not observed.

```
t3 thread start <project-path-or-id> - \
  --title "Verify T-0007: <title>" \
  --provider pi --model opencode-go/qwen3.8-max \
  --json
```

- `-` reads the prompt from stdin; `--provider` and `--model` must be passed together
  (`--provider and --model go together.`, source line 1443109).
- JSON output is exactly `{"threadId": …, "projectId": …, "runId": …}` (`ThreadLaunchResult`,
  source line 1442839) `[code]`.
- Add `--wait --json` to block and get `{threadId, runId, status, reply, error}` instead
  (source line 1443034), with exit 1 on a failed run and exit 2 when the agent is waiting for an
  answer.

**Cannot** `[measured]`: set `thinking`/`effort`/`variant` options, set `runtimeMode` (so a verifier
thread is created with whatever the server's default is — observed `full-access` for mcp-created
threads), set `interactionMode`, attach a parent, or attach files.

### B. What context can the verifier receive without a model summarising it?

Deterministic, host-side, no model in the loop `[measured]`/`[code]`:

| Artifact | Source | Determinism |
|---|---|---|
| ticket body, title, `test_cmd`, priority | `tickets` row (`tickets/store.py:46-64`) | exact |
| test proof + exit status | `tickets.proof`, written by `attach_proof()` (`tickets/store.py:296`) | exact |
| changed files | `git status --porcelain` in the project folder | exact, includes untracked |
| the change itself | `git diff` (plus `git add -N` to include new files) | exact for tracked, needs `-N` for new |
| branch name | `tickets.branch` — **only after release**; `NULL` during verification | exact but late |
| dev thread id | `tickets.thread_id` | exact |
| dev thread's own prose | `t3 thread show <id> --json --messages N` | exact, but model-authored |

Needs a model to write prose (avoid as the primary input) `[inference]`: a "summary of what I changed",
a "why this design" note, a PR description. `store.write_pr_body()` produces a file
(`tickets/store.py:228`) but it is generated at release time, after the decision point.

Two hard limits found `[measured]`/`[code]`:

1. **Prompt size.** The prompt schema is `TrimmedNonEmptyString.check(isMaxLength(12e4))` for both
   `t3_thread_launch` (source line 1489556) and `t3_thread_send` (source line 1492104) — i.e. **120 000
   characters**. The dirty working tree of this very repository measured `git diff | wc -c` →
   `91386`. A large change does not fit; the watcher must cap and say so.
2. **No attachments over the CLI.** `thread send --help` has no attachment flag, so a diff can only
   travel as text.

`[inference]` Therefore the honest default is: send the ticket body + `test_cmd` + proof + the
`git diff` (byte-capped, with a visible "[truncated at N bytes]" marker) + a pointer to the project
directory so the verifier can read any file itself with its own tools. No dev-model summary.

### C. Can the verifier run in a separate checkout?

**Yes, but today it would see the wrong tree.** `[measured]`/`[inference]`

- T3 expresses it as `--worktree <base-ref>` (+ optional `--branch <name>`), which creates a real git
  worktree at `~/.t3/worktrees/<project-slug>/t3-<hex>` (measured above).
- The dev stage never uses `--worktree` (`watcher.py:205-207` only passes `--title/--provider/--model`),
  so the dev works **in the project folder** and its work stays uncommitted until `release()` does
  `git add -A && git commit` (`watcher.py:296-298`).
- Measured: a worktree created from `HEAD` does not contain uncommitted changes, and `git diff` in that
  worktree is empty. `[measured]`
- So a verifier in `--worktree main` would review **the code before the change**. `[inference]`

Three ways out `[inference]`:

1. **Verifier in the project folder** (no `--worktree`), like the dev: simplest, sees the real change,
   but can edit/damage the dev tree.
2. **Commit first, then worktree.** Move the `git add -A && commit` step out of `release()` into the
   verification stage, branch `ticket/T-xxxx-…`, then start the verifier with
   `--worktree ticket/T-xxxx-…`. The verifier sees exactly the committed tree, in its own checkout,
   and the release stage only pushes what was verified.
3. **Default project-folder review + explicit warning.** Cheapest, weakest isolation.

What breaks if the verifier reuses the same checkout `[inference]`: (i) a verifier with editing tools
can change the code it is judging, and the change would be indistinguishable from the dev's in
`git status`; (ii) the dev thread may still be mid-run in that same folder (nothing in T3 stops it),
so two agents write the same files; (iii) the test proof would then describe a tree that no longer
exists. `[measured]` fact behind (ii): T3 imposes no per-project concurrency cap (§1.5).

### D. How can the parent read the verdict deterministically?

What exists `[measured]`/`[code]`:

- `t3 thread show <verifier> --json` → `status` (is the run finished?) + `latestRunId`.
- `t3 thread show <verifier> --json --messages N` → `messages[].{role,runId,text}`, full text.
- `t3 thread start … --json` → the verifier's `runId`, so the watcher can read *that run's* final
  assistant message specifically, not "the last message in the thread".
- `t3 thread start … --wait --json` → `{status, reply, error}` in one blocking call.
- **No structured-output channel** exists: no verdict type in `turn_items`, no output schema on the
  thread tools, and `runtime_requests.kind` is only `command|user_input`.

So the smallest honest contract is textual plus one enforced check `[inference]`:

> The verifier's final reply **must** end with exactly one line `VERDICT: ACCEPT` or `VERDICT: REJECT`,
> and on `REJECT` must follow it with a numbered list of findings, each naming a file and a line or a
> failing command.

Enforced vs merely prompted:

| Check | Enforced by | Strength |
|---|---|---|
| the run finished | exit code 0 / `status == "completed"` | real |
| the verdict line exists and is one of two values | regex `^VERDICT: (ACCEPT\|REJECT)$` on the last non-empty line | real |
| the verdict belongs to the verification run | message `runId` equals the stored `verify_run_id` | real |
| the reviewer is a different model | compare `tickets.provider/model` vs verifier profile, both from config | real (config-level) |
| the reviewer had a fresh context | the thread id equals the one this stage created, `runCount == 1` at verdict time | real |
| the findings are correct / the review was honest | nothing | prose |
| the reviewer actually read the diff | nothing | prose |

`[inference]` Do not let the verifier write the verdict into the project tree as the *only* channel:
a file is easy to fake or forget, and it would also pollute the diff being verified.

### E. Is per-run token/cost evidence exposed anywhere?

**Not through the CLI.** `t3 thread show --json` and `t3 thread list --json` have no usage field
(keys enumerated in §1.3). There is no `t3 usage`/`t3 cost` subcommand. `README.md:240` and
`RESEARCH.md` already say token accounting is missing, and that is still true at the CLI surface.
`[measured]`

**In the database, yes — one field** `[schema]`:

```
table   orchestration_v2_projection_provider_turns
column  payload_json -> $.tokenUsage = {usedTokens, maxTokens, inputTokens,
                                        cachedInputTokens, outputTokens, updatedAt}
```

with the caveat from §1.4(c): the counters look cumulative, so a per-turn figure needs a subtraction
`[inference]`. Reading `statev2.sqlite` from the watcher would violate neither invariant 9 of
`AGENTS.md:33-34` (which already allows read-only access) nor the "no new dependencies" rule, since
`watcher.py:50-67` already opens it read-only for `project_path()`.

Honest alternative if the owner does not want a second database read `[inference]`: wall-clock per
stage (`runs.requested_at → completed_at` for the verification run) plus the attempt counter. That is
what the existing budget does (`watcher.py:354-381`).

### F. Concurrency and limits

- **Two threads of the same project at once: proven.** §1.5 shows two runs in project `01807ea7`
  running simultaneously. T3 does not serialise per project. `[measured]`
- **`watcher.maxConcurrent` counts `in-progress` tickets only** (`watcher.py:414`). A verifier thread
  does not add to that count if verification happens while the ticket sits in `review`
  (`next_review()` at `watcher.py:383`, `active_tickets()` at `watcher.py:350`). `[code]`
- **The watcher is single-threaded and one step per loop** (`loop()` → `step()`, `watcher.py:421-437`,
  default `intervalSeconds: 120`). A blocking `--wait` inside `step()` would stall every other stage
  for the whole verification. `[inference]`
- Recommendation `[inference]`: start the verifier **without** `--wait`, store `verify_thread_id` +
  `verify_run_id`, and let the next `step()` read the verdict from `thread show` — exactly the shape
  the dev stage already uses (`sync_threads()`).
- Bounded retry `[inference]`: `verification.maxAttempts = 2` (one rejection is normal, two is the
  limit), `verification.timeoutMinutes = 45` as a wall-clock cap enforced by the same
  `enforce_budget()` style sweep, no backoff (the next loop is 120 s anyway), and `blocked` — not more
  retries — once the cap is hit.

### G. Failure and escalation — see the decision table in §3

Every move below is one of the transitions in `tickets/store.py:34-42`; no `force` is used, matching
`AGENTS.md:18-19` and the Round-1 finding in `REVIEW.md` ("`grep -c 'force=True' watcher.py` → 0").

---

## 3. Recommended policy — decision table

Everything in this table is a proposal `[inference]`; the *Detection* column is the only part that
quotes measured evidence, and the transitions are checked against `tickets/store.py:34-42`.

Notation: *stay* = no `move()` call this step. All targets exist in `TRANSITIONS`.

| # | Situation | Detection (exact evidence) | Action | Event |
|---|---|---|---|---|
| 1 | `review`, no verifier yet, attempts < max | `verify_thread_id is null` and `verify_attempt < verification.maxAttempts` | *stay* in `review`; `t3 thread start <project> - --provider … --model … --json`; store `verify_thread_id`, `verify_run_id`, `verify_attempt += 1` | `verify-started` |
| 2 | `review`, verifier running | `thread show` → `status` in `preparing/queued/starting/running` | *stay* | — |
| 3 | `review`, verifier finished, verdict ACCEPT | last assistant message of `verify_run_id` matches `^VERDICT: ACCEPT$` | `release()` → `done` (existing path, `watcher.py:256`) | existing `moved` + `branch` + `report` |
| 4 | `review`, verdict REJECT, attempts < max, ticket has `thread_id` | `^VERDICT: REJECT$` | `review → in-progress`; `t3 thread send <ticket.thread_id> - --json` with the findings; clear `verify_thread_id`/`verify_run_id` | `verify-rejected` |
| 5 | `review`, verdict REJECT, attempts ≥ max | as above | `review → blocked` with the verdict and findings in the note | `verify-exhausted` |
| 6 | `review`, verdict REJECT, ticket has **no** `thread_id` | `thread_id is null` | `review → blocked` — there is no dev thread to send findings to | `verify-blocked` |
| 7 | `review`, verifier `failed/cancelled/interrupted/rolled_back` | `thread show` → `status` | `review → blocked` (an infrastructure failure is not a code verdict) | `verify-failed` |
| 8 | `review`, verifier waiting for an answer | `status == "waiting"`, or `--wait` exit 2 / `pendingRequestCount > 0` | `review → blocked`; a human must answer in T3 | `verify-needs-input` |
| 9 | `review`, verifier exceeded the wall-clock cap | `verify_started_at` older than `verification.timeoutMinutes` | `review → blocked` (do not silently accept) | `verify-timeout` |
| 10 | `review`, proof is not `[PASS]` | `release()` already raises (`watcher.py:270`) | ticket stays in `review`; the next step starts a verifier, which will reject | — |
| 11 | dev thread busy when findings arrive | nothing to detect — `thread send` uses `mode: "queue"` | the findings become the next turn (`§1.2`) | existing `continued` |
| 12 | verifier profile missing or model not in T3's catalogue | `catalog.resolve_selection()` raises `CatalogError` (`tickets/catalog.py:122-136`) | `review → blocked`, never fall back to the dev profile | `verify-misconfigured` |
| 13 | `in-progress` older than `limits.maxInProgressMinutes` | existing `enforce_budget()` (`watcher.py:354`) | `in-progress → blocked` (unchanged) | existing `moved` |

Why `review` and not a new state `[inference]`: `review` is already the only state whose exits are
`done | in-progress | blocked` — exactly the three outcomes verification needs — and it is already the
state where release runs. Adding an eighth state would require touching `STATUSES`, `TRANSITIONS`,
the board's `transitions` payload (`board/server.py:196`) and `docs/architecture.md:59`, for no
additional expressiveness. The stage order inside `review` becomes: *verify, then release* — with
release gated on the verdict, mirroring the existing `[PASS]` gate.

---

## 4. Proposed flow, inside the existing seven states

```
ready
  │  claim + `t3 thread start`                                     (dev stage)
  ▼
in-progress
  │  thread status completed|idle                                  (sync stage, exists)
  ▼
testing
  │  test_cmd exit 0                                               (test stage, exists)
  │  exit ≠ 0 ───────────────────────────────────► in-progress     (test stage, exists)
  ▼
review                                                            (verification stage, new)
  ├─ VERDICT: ACCEPT ───────────────────────────► done             (release stage, exists)
  ├─ VERDICT: REJECT, attempt < max ───────────► in-progress       (findings sent to dev thread)
  └─ REJECT at max / verifier failed / timeout /
     verifier needs input ──────────────────────► blocked          (human decides)
```

Sequence for the new stage `[inference]`:

```mermaid
sequenceDiagram
    participant W as watcher
    participant S as store
    participant G as git
    participant T as t3 CLI
    W->>S: next_review()
    W->>G: git status --porcelain ; git add -N ; git diff (capped)
    W->>T: thread start <project> - --provider <other> --model <other> --json
    T-->>W: {"threadId":…, "projectId":…, "runId":…}
    W->>S: attach_verify(threadId, runId, attempt+1)
    Note over W: ticket stays in review
    W->>T: thread show <verifier> --json --messages 5
    T-->>W: {"thread": {"status":"completed"}, "messages":[…]}
    alt last message of runId == VERDICT: ACCEPT
        W->>S: (unchanged) release → done
    else VERDICT: REJECT and attempts < max
        W->>T: thread send <ticket.thread_id> - (findings)
        W->>S: move(in-progress)
    else VERDICT: REJECT and attempts == max
        W->>S: move(blocked)
    end
```

Guard rails `[inference]`:

- The verifier thread must never be handed a new run once a verdict exists — one thread, one run, so
  `runCount == 1` is a checkable "fresh context" marker (`thread show` exposes `runCount`). `[measured]`
- The verifier's profile is resolved through `catalog.resolve_ticket()` against T3's own cache, like a
  dev profile (`tickets/catalog.py:139`), and must differ from the ticket's dev profile.
- The verifier prompt is built only from §B artifacts; it must not be the ticket body alone, or it
  cannot judge the change.

---

## 5. Minimal schema addition

Column additions, in the same shape as the existing `MIGRATIONS` list (`tickets/store.py:85-93`) so an
older database keeps working `[inference]`:

```python
("tickets", "verify_thread_id",    "text not null default ''"),
("tickets", "verify_run_id",       "text not null default ''"),
("tickets", "verify_provider",     "text not null default ''"),
("tickets", "verify_model",        "text not null default ''"),
("tickets", "verify_attempt",      "integer not null default 0"),
("tickets", "verify_verdict",      "text not null default ''"),   # ACCEPT | REJECT | '' (undecided)
("tickets", "verify_findings",     "text not null default ''"),   # the reviewer's own words, capped
("tickets", "verify_started_at",   "text not null default ''"),
("tickets", "verify_finished_at",  "text not null default ''"),
```

Why a column and not "an event only" `[inference]`: `sync_threads()` needs to find tickets whose
verifier is still running, and the release gate needs a verdict that cannot be overwritten by a later
event; `store.move()` already refuses a second move to the same status (`tickets/store.py:207-208`),
which gives the same one-shot property approvals have (`AGENTS.md:24-25`).

New event kinds, appended through the existing `record_event()` (`tickets/store.py:372`):
`verify-started`, `verify-accepted`, `verify-rejected`, `verify-exhausted`, `verify-failed`,
`verify-needs-input`, `verify-timeout`, `verify-misconfigured`, `verify-blocked`.

`list_public()` drops `proof` today (`tickets/store.py:256-261`). `verify_findings` is model prose and
should be dropped there too, or the board leaks a whole review into `/api/tickets`. `[inference]`

`report()` addition (`tickets/store.py:429`) — one section, placed before the history table:

```
## Independent verification
- Verdict: ACCEPT
- Reviewer: pi / opencode-go/qwen3.8-max (profile `deep`), thread `…`, run `…`
- Fresh context: yes (runCount 1, runId matches)
- Attempt: 1 of 2
- Findings: none
```

Bounded retry state is the `verify_attempt` column plus the events; no new config table.
Config additions `[inference]` (same file, `~/.config/t3-workflow/config.json`):

```json
"verification": {
  "enabled": true,
  "profile": "deep",
  "maxAttempts": 2,
  "timeoutMinutes": 45,
  "maxDiffBytes": 60000
}
```

`profile` is a key of `models` (`~/.config/t3-workflow/config.json`), not a new provider/model pair, so
`resolve_profile()` (`tickets/store.py:119-134`) keeps owning the "never invent a model" rule.

---

## 6. What a `done` report must contain to be evidence

Already stored today `[code]`/`[schema]`:

| Field | Where | Note |
|---|---|---|
| `proof` = `[PASS] <iso8601>\n<output tail>` | `tickets.proof`, `tickets/store.py:296-306` | exit code turned into a status, never prose |
| `note` | `tickets.note` | last stage note, overwritten on every `move()` |
| event history | `events` table | `created, moved, forced, claimed, thread, continued, proof, branch, report` |
| `branch`, `pr_url` | `tickets.branch`, `set_branch()` | may be `NULL` on the "nothing to push" path |
| `pr_body_path` | `write_pr_body()` (`tickets/store.py:228`) | `T-xxxx.pr.md` |
| `report_path` | `write_report()` (`tickets/store.py:263`) | written only on `done` |
| dev thread id | `tickets.thread_id` | |
| dev provider/model | `tickets.provider`, `tickets.model`, `model_profile` | |

Added by verification `[inference]`: `verify_provider`, `verify_model`, `verify_attempt`,
`verify_verdict`, `verify_thread_id`, `verify_run_id`, `verify_findings`, `verify_started_at`,
`verify_finished_at`, plus the `verify-accepted` event. Together with the run ids these answer "who
judged this, with which model, in which context, on which attempt, and what did they say" — which is
the difference between evidence and a story.

Two gaps worth fixing while in there `[measured]`: `write_report()` passes no `t3_link`
(`tickets/store.py:272`), so a report cannot be traced to a thread by link today; and `note` is a
single string overwritten by each move, so the verification findings need their own column rather than
the note (`verify_findings` above).

---

## 7. Open questions for the owner

1. **Which model reviews?** Every profile in `~/.config/t3-workflow/config.json` today is on the same
   harness (`pi`). Is "different model, same harness" enough, or must the verifier use a different
   harness (`opencode`)? `[measured]`: `models.fast/deep/cheap` → provider `pi`; only `build` uses
   `opencode`.
2. **Is the verifier allowed a different project folder, or must it be the same one?** Option 2 in §C
   (commit first, then `--worktree`) changes when the commit happens, and therefore what the release
   stage pushes. That is a policy change, not an implementation detail.
3. **May the verifier be given write tools at all?** The CLI cannot set `runtimeMode`, so a
   CLI-started thread gets the server default — observed `full-access` on existing mcp-created threads.
   A read-only verifier would need an MCP launch (`t3_thread_launch` with `runtimeMode`), which means
   the watcher would stop being a pure CLI client.
4. **Does a verifier rejection cost an extra attempt of the dev model, or a fresh dev thread?** §3 keeps
   the existing thread (invariant 2), which is cheaper and keeps context, but a dev model that was
   wrong twice rarely self-corrects. `[inference]`
5. **Should token usage be collected?** It exists only as
   `provider_turns.payload_json.tokenUsage` (§E) and looks cumulative `[inference]`. Reading it means
   the watcher reads a second table of T3's database — allowed by `AGENTS.md:33-34`, but a new
   coupling.
6. **Human override path.** If a verifier is wrong and the owner knows better, is `blocked → ready` plus
   `force` acceptable (today `blocked` only reaches `ready|backlog`, `tickets/store.py:40`), or does the
   owner want a `verify-override` command that records who overruled whom?
7. **Timeout value.** 45 minutes is a guess `[inference]`; measured dev turns on the main thread lasted
   16 s – 57 s (`completed_at - started_at` on turns 7–10 of `mcp:72b322c1-…`), so the real verifier
   budget is probably much smaller than the 45-minute cap.
8. **Does the verifier get the ticket's `attachment_path`?** Today the dev prompt includes it
   (`watcher.py:73-75`); the verifier inherits the same project folder, so it could read it, but the
   prompt builder would have to say so explicitly.

---

## 8. Sources

Commands — all read-only, no thread started, no ticket touched:

| Command | Purpose |
|---|---|
| `t3 --version`, `t3 --help`, `t3 thread --help` | CLI surface |
| `t3 thread start --help`, `send --help`, `show --help`, `list --help`, `t3 project --help` | flags |
| `t3 thread list --json --project <path>` | subagent threads hidden (n=1) |
| `t3 thread show --json --messages 1 <child-thread>` | `parentThreadId`, `relationshipToParent` |
| `t3 thread show --json 'thread:does-not-exist'` | exit 1, stderr message |
| `strings -n 4 ~/.t3/runtime/versions/0.1.9/t3 \| sed -n '…'` | embedded CLI source: lines 1442796, 1442839, 1443028-1443046, 1443057, 1443086, 1443109, 1443147, 1489555-1489556, 1492104, 1111406-1111416, 1125907 |
| `python3` + `sqlite3` on `file:~/.t3/userdata/statev2.sqlite?mode=ro` | schema dumps, `payload_json` samples, `tokenUsage`, running-run census, lineage census |
| `python3` + `sqlite3` on `file:~/.local/state/t3-workflow/tickets.sqlite?mode=ro` | ticket columns, event kinds, `branch` nulls |
| `git status --porcelain`, `git diff`, `git add -N`, `git worktree add` in `/tmp/vtest` | what a worktree contains |
| `cat ~/.t3/worktrees/video-maker/t3-d27395cc-…/.git` | real git worktree |
| `cat ~/.config/t3-workflow/config.json` | profiles, `maxConcurrent`, `limits` |

Repository files, cited by line:

- `watcher.py:9, 46-47, 50-67, 71-83, 86-101, 104-122, 125-142, 144-226, 256-318, 321-347, 350-381,
  383-391, 393-419, 421-437`
- `tickets/store.py:24-42, 44-83 (SCHEMA), 85-93 (MIGRATIONS), 119-134 (resolve_profile), 199-226
  (move), 228-255 (write_pr_body), 256-261 (list_public), 263-278 (write_report), 280-294 (claim),
  296-306 (attach_proof), 307-314 (set_branch), 372-375 (record_event), 376-385 (attach_thread),
  429-460 (report), 462-466 (events)`
- `tickets/catalog.py:12-17 (model options are not applied), 122-136 (resolve_selection), 139-178
  (resolve_ticket)`
- `board/server.py:179-205, 206-216, 218-228, 271-300`
- `AGENTS.md:16-34` (invariants 1–9), `AGENTS.md:36-44` (environment)
- `docs/architecture.md:31-72` (data model, state machine), `:97-145` (sync/test/release stages),
  `:165-171` (concurrency), `:172-182` (failure behaviour)
- `docs/usage.md:15-17` (one thread per ticket), `:185-203` (limits, unattended running)
- `README.md:236-240` (limits and budgets), `:327` (wall-clock budget only)
- `RESEARCH.md` §4 (gates), §6 (phases), §7 (decisions), "Still open" (token accounting)
- `REVIEW.md` Round 1 finding 1 (`force=True`), Round 6 (thread continuation)

T3 documentation present on this machine, quoted:

- `~/.t3/caches/pi-t3-mcp-extension.ts:10` — the orchestration instructions T3 injects into every
  session (subagent vs `delegate_task`, `isolation:"worktree"`, "never make a child's checkout in the
  shell").
- The same text is embedded in the server binary at `strings` line 1256024.
- `delegate_task`'s own tool description embedded in the binary (`sed -n '1489544,1489562p'`), including
  the `workspaceStrategy` sentence *"Uncommitted edits are not copied"*.
