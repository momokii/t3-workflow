# Handoff — t3-workflow development thread

Context for the thread `[Main] t3-workflow` (project `t3-workflow`). Written 2026-11-26 from the
"overall task" thread on this VM, after the model-catalogue work landed.

Read this file, then `AGENTS.md`, then whichever document matches the task. Everything below was
measured on this machine; the numbers are from the moment of writing, not from memory.

## 1. What this repository is

A ticket queue that drives T3 Code: a ticket becomes (or continues) an agent thread, the thread's work
is tested from the host by exit code, and a passing ticket is pushed as its own branch. It ships three
services, a kanban board, a Telegram bridge, a Docker deployment and the systemd units.

```
ticket ─► thread (agent works) ─► sync on thread status ─► test (exit code) ─► branch + push ─► report
```

## 2. Current state

Hand-written state goes stale the moment the handoff is committed. Regenerate it instead:

```bash
make snapshot      # writes STATE.md: git, services, tickets, catalogue, docs
cat STATE.md
```

`STATE.md` is generated and git-ignored. The values below were measured when this file was written;
treat them as a hint and let `STATE.md` and §9 tell you the truth.

| Item | Value |
|---|---|
| Remote | `https://github.com/momokii/t3-workflow.git`, `main` tracking `origin/main` |
| HEAD (at writing) | `7865eeb`, deliberately not updated: `make snapshot` is the source |
| Tags | `v0.1.0`, `v0.1.1` |
| CI | GitHub Actions `check` green on `main`; confirm the current head in `STATE.md` or the Actions tab |
| Tests | `make check` green; the counts come from the command, not from this table |
| Services | `t3-bridge`, `t3-watcher`, `t3-board`, `t3-backup.timer` — all active (systemd user) |
| Board | `http://100.124.184.116:8787` (tailnet only), token in `~/.config/t3-workflow/board.env` |
| Tickets | `T-0001` done, `T-0002` done (thread-continuation E2E), `T-0003` backlog (explicit model demo) |
| Catalogue | 10 providers cached; enabled: `pi` (38 models), `opencode` (141); presets `fast`, `deep`, `cheap`, `build`; default `fast` |
| Container | `Dockerfile` + `docker-compose.yml` (board default, bridge/watcher under `automation`) |

## 3. Where the knowledge lives

| Question | Read |
|---|---|
| How do I use it, which workflow, model/priority guidance | `docs/usage.md` |
| How is it built, data model, state machine, sequences | `docs/architecture.md` |
| Every HTTP endpoint with payloads and errors | `docs/api.md` |
| Runbook: services, pause, rotate token, backup/restore, incidents | `docs/operations.md` |
| Container deployment, host-network trade-off, troubleshooting | `docs/docker.md` |
| Why the design is what it is, measured facts, decisions | `RESEARCH.md` |
| Review history (standards and spec, seven rounds) | `REVIEW.md` |
| Invariants an agent must keep | `AGENTS.md` |
| The same material as a page, with screenshots | `/guide` on the board (`board/static/guide.html`) |
| Why the loop is safe to leave alone | `docs/autonomy.md` |
| What a failed run records, and what may retry | `docs/retry-evidence.md` |
| What each project declares as its test command | `docs/test-command-detection.md` |
| Live status, heartbeat and upload measurements | `docs/ui-live-status.md` |
| Shape of a handoff like this one | `docs/handoff-template.md` |

## 4. Architecture in one screen

| Component | Role | Key file |
|---|---|---|
| Store | state machine, atomic claim, approvals, proofs, reports | `tickets/store.py` |
| Catalogue | harnesses/models from `~/.t3/caches/*.json`, validates a choice | `tickets/catalog.py` |
| Watcher | dev → sync → test → release stages, concurrency and budget | `watcher.py` |
| Bridge | Telegram commands, notices, audit | `bridge/t3_bridge.py` |
| Board | JSON API + single-page kanban | `board/server.py`, `board/static/index.html` |
| Deploy | unit templates, installer, Dockerfile, compose | `deploy/`, `Dockerfile`, `docker-compose.yml` |

Resolution order for a thread: **ticket `provider`+`model` → ticket preset → `defaults.profile`**.
A ticket that already carries `thread_id` is continued with `t3 thread send`; otherwise a new thread is
started. The sync stage moves a ticket to `testing` when T3 reports the thread `completed`, and to
`blocked` when it reports `failed`.

## 5. What the last session delivered (the part to carry over)

1. **Live model catalogue.** The board used to offer four hard-coded profiles. It now reads T3's
   provider cache (`~/.t3/caches/<instanceId>.json` — `enabled`, `installed`, `status`, `models`,
   `optionDescriptors`) and offers harness → model. `GET /api/catalog` exposes it; tickets store
   `provider` + `model`; presets remain shortcuts. Disabled harnesses never appear, and the API
   rejects a disabled harness, an unknown model, or a provider without a model.
2. **Model options are shown but not applied.** `thinking`, `variant`, `effort`, `agent` come from the
   catalogue, and `t3 thread start` has no flag for them, so the thread uses the project default.
   This is documented in the UI, `docs/usage.md`, `docs/architecture.md`, README and `REVIEW.md`
   (Round 7). Do not invent a workaround.
3. **Thread continuation.** Adoption now creates a `ready` ticket that keeps the thread; the watcher
   sends the ticket prompt into that same thread and records a `continued` event. A sync stage
   advances the ticket from the thread's status. Proven end to end on `T-0002` with a real thread:
   prompt sent → agent replied → `completed` → `testing` → `test` → `review` → `done` + report.
4. **Board UI.** Every dialog explains each field (ticket, adopt, register project); the ticket dialog
   opens immediately and fills its pickers from T3, with a loading state and submit disabled until ready.
   The header carries a distinct **Guide** link; `/guide` serves the lifecycle, the test command and three
   screenshots (board, new ticket, adopt) captured from a throwaway database. Profile details come from
   the catalogue, and adoption says that the same thread is continued.
5. **Autonomous repair loop.** A failing test sends its output into the dev thread; a crashed T3 call
   or a dead thread returns the ticket to `ready`. Both are bounded (`retry.maxTestAttempts`,
   `retry.maxThreadAttempts`) and counted on the ticket (`test_attempts`, `dev_attempts`); the
   `dev_run_id` anchor keeps `testing` from re-running against the code that just failed. The failure
   class is read from T3's error item: a usage limit, a context overflow, a missing model or a provider
   version mismatch blocks immediately, while timeouts, connection drops, 429s and 5xx retry. An
   unselectable ticket or a release that can never succeed blocks instead of blocking the queue
   `docs/retry-evidence.md` and `docs/autonomy.md` record the evidence and the decisions.
6. **Independent review before release.** `review` starts a second thread in a fresh context (same
   harness and model by default, `verification.profile` to override) that receives the ticket, the test
   proof, `git status` and `git diff HEAD`, and must end with `VERDICT: ACCEPT` or `VERDICT: REJECT`; a
   rejection sends the findings into the dev thread and spends one of `maxAttempts` (2). A missing
   verdict, a failed reviewer, a timeout or a tree that changed mid-review blocks the ticket. The guard
   is a tree fingerprint, because T3 0.1.9 has no read-only mode (`docs/readonly-verifier.md`).
7. **Plan acknowledgement.** The bridge forwards the first assistant message of the dev run (up to 900
   characters) after the "in progress" ack, once per run (`plannedRuns` in its state file). A run that
   has not produced an assistant message yet is retried on the next poll.
8. **End-to-end proven on a throwaway project.** `T-0004` (real dev thread, real reviewer thread,
   same model) went ticket → thread → `completed` → test PASS → review `ACCEPT` in one attempt →
   `ticket/t-0004-…` pushed to a local bare origin → `done`, with the report on disk (including the
   independent review and its findings) and delivered to Telegram. The run also exposed a production
   bug: `sync_threads()` passed `runner=None` into `thread_show()`, which crashed the service; fixed
   and covered by a regression test.
8. **Verification:** both suites green (`make check`), board `/healthz`, `/api/*` and `/guide` exercised
   over the tailnet, Docker image built and run (board healthy, workers one-shot, host services
   untouched), CI green.

## 6. Rules for this thread

From `AGENTS.md`, repeated because they are the ones that get broken:

1. Status changes go through `store.move`; `force=True` is the human override and is recorded as
   `forced`. The watcher itself never forces.
2. One ticket, one thread. A ticket with `thread_id` continues that thread; it never starts a second.
3. The model choice comes from config or the catalogue, never from the model's own opinion.
4. Approvals are one-shot: the first decision wins, a second is rejected.
5. The test stage decides from exit codes. A missing or failing command never advances.
6. Reports are evidence; do not hand-edit files under `~/.local/state/t3-workflow/reports/`.
7. Push is automatic **on a ticket branch**, never the base branch; a ticket without a passing proof
   is refused.
8. Keep tests hermetic: no network, no real Telegram, no real T3 call, `T3_REPORTS_DIR` pointed at a
   temporary directory.
9. Documentation is English. When behaviour changes, update the document that owns it in the same
   commit: usage → `README`/`docs/usage.md`, endpoints → `docs/api.md`, schema/state machine →
   `docs/architecture.md`, procedures → `docs/operations.md`.
10. `make check` before every commit; one logical change per commit; no force-push.

## 7. Recommended next work, in order

| # | Task | Acceptance criteria | Why first |
|---|---|---|---|
| 1 | ~~Edit and delete a ticket~~ | delivered: store + API + CLI + board dialog; a deleted ticket keeps its report and an audit snapshot | done |
| 2 | ~~Plan acknowledgement~~ | delivered: the bridge forwards the first assistant message of the run, once per run (`plannedRuns` in the bridge state) | done |
| 3 | ~~Scheduler for a quota window~~ | delivered as a bounded backoff (`retry.maxQuotaAttempts` × `retry.quotaBackoffMinutes`), because T3 exposes no reset time | done |
| 2 | **Per-ticket cost evidence** — investigate what T3 exposes per run before building | a documented finding: where token/usage data lives, or a clear "not available"; then the smallest honest feature | wall-clock budget is the only guard now |
| 3 | **Deploy + rollback** for one real target | a documented target, a deploy command, a tested rollback, prove with one release | pipeline stops at a pushed branch today |
| 4 | **Pull-request automation** | needs `gh` or a GitHub token; until then the compare link + `T-xxxx.pr.md` stand | explicitly deferred by the owner |

Do not start #3 without a target from the owner, and do not start #4 without a token.

## 8. Known limitations (do not rediscover)

- No ticket edit/delete yet; no image attachments (`t3 thread send` is text only; a path is passed
  instead); no per-ticket model options; no deploy; no PR creation; no token accounting; Telegram-only
  approvals; Docker deployment uses host networking (see `docs/docker.md`).
- The reviewer has no sandbox: T3 0.1.9 has no read-only runtime mode, so the watcher refuses a verdict
  when the project tree changed during the review instead (`docs/readonly-verifier.md`).
- The board's JavaScript has no browser-driven test in CI; it is checked by `node --check`, the HTTP
  tests and the screenshots in `board/static/shots/`.

## 9. Verify the environment before trusting it

```bash
cd /home/kelanach/Public/main-linux-kelanach/code-berkah-titipan-tuhan/code/personal_prjct/t3-workflow
git log --oneline -1 && git status --porcelain
make snapshot && sed -n '1,20p' STATE.md
make check
systemctl --user is-active t3-bridge t3-watcher t3-board
curl -s http://100.124.184.116:8787/healthz
python3 tickets/cli.py list
curl -s http://100.124.184.116:8787/api/catalog | python3 -c "import json,sys;print([(p['instanceId'],len(p['models'])) for p in json.load(sys.stdin)['providers'] if p['enabled']])"
```

Expected: clean tree, both suites green, three services active, `{"status":"ok",…}`,
three tickets, `[('opencode', 141), ('pi', 38)]`.

## 10. Copy-paste prompt

```text
You are continuing development of the repository t3-workflow on this VM.

Working directory:
/home/kelanach/Public/main-linux-kelanach/code-berkah-titipan-tuhan/code/personal_prjct/t3-workflow

First run `make snapshot` and read `STATE.md`. It is generated, so it is the only current
state you can trust; any table in the documents is "at writing" only.

Then read these files in order, and do not write any code before you finish:
1. HANDOFF.md              (state, rules, next tasks)
2. AGENTS.md               (invariants you must keep)
3. docs/usage.md           (how the system is used: tickets, threads, harness/model, priority)
4. docs/architecture.md    (data model, state machine, stages, resolution order)
5. docs/api.md             (every HTTP endpoint and its errors)
6. docs/handoff-template.md (the shape this handoff follows)

Then reply with a block titled CONFIRMATION containing:
- the HEAD, commit count and uncommitted-file count exactly as `STATE.md` printed them;
- 5 facts about the system, each with the file it came from;
- the resolution order for provider/model;
- the rule that decides whether a ticket continues a thread or starts one;
- 2 limitations that are documented and must not be "fixed" by guessing.
Do not do anything else until I reply "go".

Then wait. When I say "go", pick the first unfinished item from HANDOFF.md §7 unless I name another,
and work it end to end:
- write or update tests first where a seam exists;
- run `make check` (both suites) before each commit;
- commit per logical change with conventional commits, and push to origin/main (never force-push);
- update the document that owns the behaviour you changed, in the same commit;
- finish with evidence: the test output, the HTTP/CLI output, and the commit hashes.

Ground rules:
- Do not run a real T3 thread or spend tokens unless I ask for it; `--dry-run` first.
- Do not edit ~/.local/state/t3-workflow/reports/ by hand; regenerate from the ticket.
- Do not add third-party Python dependencies; the runtime is the standard library.
- Do not run the systemd watcher and the compose watcher at the same time.
- Keep passwords, tokens and `.env` out of commits; scan before you commit.
- Reply to me in Indonesian; code, docs and commit messages stay in English.
```
