# Code review — whole repository

Two-axis review of everything after the first commit, following the repository's own standards and the design in `RESEARCH.md`.

| Item | Value |
|---|---|
| Fixed point | `b620d56` (first commit: docs only) |
| Diff | `git diff b620d56..HEAD` — 22 files, +3.6k lines |
| Standards sources | `AGENTS.md` (invariants), `README.md`, `Makefile`, plus the Fowler smell baseline |
| Spec sources | `RESEARCH.md` §6 (six phases), §7 (decisions answered 2026-11-25), `README.md` |
| Reviewer | the same agent that wrote the code, in two explicit passes (no separate sub-agents: model lock) |

---

## Standards

Findings are quoted with the invariant they break. **Fixed** items were corrected in `a7e0d09` before this report was committed.

### Hard violations (fixed)

1. **`force=True` masked the state machine.** `watcher.py` called `store.move(..., force=True)` for `in-progress → blocked`, `testing → review`, `testing → in-progress`, `review → done` — every one of them already allowed by `TRANSITIONS`. `AGENTS.md` invariant 1 reserves `force` for the human override, so the watcher was silently exercising the escape hatch on the model's behalf. Now: `grep -c 'force=True' watcher.py` → **0**.
2. **`release.mode` was dead configuration.** The config advertised `"mode": "branch"` and the code never read it. Any other value would have been ignored, which is worse than failing. Now `watcher.py:194` rejects anything but `branch`.
3. **Release could push unproven work.** A ticket moved to `review` by hand would be pushed with no test evidence. Now `watcher.py:199` refuses unless the stored proof starts with `[LULUS]` (`watcher.py:199`).
4. **The HTTP list leaked test output.** `/api/tickets` returned the `proof` column, which can hold build logs or environment detail. Now `store.list_public()` (`tickets/store.py:249`) drops it; the detail endpoint and the report keep it.

### Judgement calls (open)

5. **`shell=True` on a ticket-supplied command** (`watcher.py:258`). The test stage runs `test_cmd` through a shell in the project directory. Ticket creation is gated by the Telegram allowlist, so the blast radius is "the owner's own account", but a hijacked Telegram session becomes code execution as this user. Mitigation to consider: a config switch `limits.allowShellTests`, or a per-ticket allowlist of binaries.
6. **`request_approval` is check-then-insert** (`tickets/store.py:318`). Two watchers racing could create two pending approvals. `watcher.maxConcurrent=1` makes it unlikely, and a unique partial index would close it.
7. **Broad `except Exception`** in `bridge.handle` (`bridge/t3_bridge.py:494`) and in the approval import shim (`:47`). Justified for a daemon and for optional imports, but programming errors become "Error tak terduga" instead of a traceback — the log line carries the type, not the stack.
8. **Project resolution duplicated** between `watcher.project_path()` (sqlite) and `bridge.resolve_project()` (sqlite + CLI). Same rule, two implementations; a shared helper in `tickets/` would prevent drift.
9. **No linter in the repo.** Nothing installed (`ruff`, `flake8`, `pyflakes`, `pylint` all absent), so style rests on review. `make check` now at least compiles every module and runs both suites before a commit.

---

## Spec

Against `RESEARCH.md` §6 and the decisions recorded in §7.

### Implemented as specified

- **Phase 1 / 1b** — bridge read and write commands, allowlist, audit.
- **Phase 2** — approval routing: request once, notify once, first decision wins, second rejected.
- **Phase 3** — tickets in SQLite (decision 1B), created from the CLI and from Telegram.
- **Phase 4** — watcher dev stage: atomic claim, thread start, thread id stored.
- **Phase 5** — test stage: runs `test_cmd`, stores exit code and output tail, `review` only on 0, otherwise back to `in-progress`.
- **Phase 7** — board: drag-and-drop plus a single-pointer control (WCAG 2.2 SC 2.5.7), approvals, responsive layout, dark mode, logo/favicon.
- **Decision 3 (revised)** — automatic push: branch `ticket/T-xxxx-…`, commit, push to `origin`, compare link, PR body file. Never the base branch.
- **Decision 7** — model and harness chosen from config profiles (`pi` and `opencode`), never invented by the watcher.

### Missing or partial

1. **Phase 6 is partial: no PR is created through an API.** The release stage pushes the branch and writes `T-xxxx.pr.md`, and the ticket stores a `compare` URL that opens GitHub's PR form. Creating the PR automatically needs `gh` or a GitHub token; neither is present on this machine.
2. **No deploy step.** The spec's "deploy" ends at a pushed branch. There is no target, no release command and no rollback path, so implementing it now would be guesswork.
3. **Phase 0 blocker: the repository has no remote.** `github.com/momokii/t3-workflow` returns 404, so release cannot push this project's tickets until a remote exists. The stage is implemented and tested against a temporary bare remote.
4. **GitHub Issues mirror** — deliberately deferred by decision 1B; not a defect.

---

## Summary

- **Standards**: 9 findings — 4 hard violations (all fixed), 5 judgement calls (2 with concrete mitigations queued: shell allowlist, partial unique index).
- **Spec**: 4 gaps — 2 real (PR API, deploy), 1 environmental blocker (no remote), 1 deferred by decision.
- Worst standards issue was the silent `force=True`; worst spec issue is that "rilis" still ends at a branch, not a pull request.

---

## Round 2 (after the board integration work)

| Finding | Status |
|---|---|
| Board showed only tickets, so 263 existing threads and 11 projects were invisible | fixed: `/api/projects`, `/api/threads`, adopt, and a new-ticket form |
| No way to register a project from the board | fixed: `/api/projects` runs `t3 project add` after validating an absolute existing path |
| `OnFailure=` was written in `[Service]` instead of `[Unit]`, so no alert ever fired | fixed: `OnFailure=t3-alert@%N.service`, verified by a deliberately failing unit |
| Backups were manual only | fixed: `t3-backup.timer` daily at 03:20 with `--keep 7` |
| No CI, no changelog, no tags | fixed: `.github/workflows/check.yml`, `CHANGELOG.md`, tag `v0.1.0` |
| No integration test against the real CLI | added, opt-in via `T3_INTEGRATION=1` so the default suite stays hermetic |

---

## Round 3 (documentation pass)

| Item | Result |
|---|---|
| Languages | `README.md`, `RESEARCH.md` and `CHANGELOG.md` rewritten in English; `AGENTS.md` and `REVIEW.md` were already English |
| New documents | `docs/architecture.md` (components, data model, sequences), `docs/api.md` (every endpoint with payloads and curl), `docs/operations.md` (runbook, restore drill, incidents) |
| Route coverage | every `/api/…` route in `board/server.py` appears in `docs/api.md` |
| Command coverage | every bridge command (`/projects` … `/reject`) appears in `README.md` |
| Configuration coverage | all twelve config keys documented with defaults and meaning |
| Runbook accuracy | the restore example used the `sqlite3` binary, which is not installed here; replaced with a Python one-liner and verified |
| Restore drill | executed against a copy: snapshot listed T-0001, counts matched, production untouched |

---

## Round 4 (published)

| Item | Result |
|---|---|
| Remote | `https://github.com/momokii/t3-workflow.git`, branch `main`, tag `v0.1.0` pushed |
| CI | the first two runs failed; the cause was a test that needed the real T3 database. Reproduced with `HOME` pointed at an empty directory, fixed in `89e1ecd`, both suites green under that condition |
| Files on the remote | workflow, deploy templates and docs confirmed via `raw.githubusercontent.com` |

---

## Round 5 (container deployment)

| Axis | Finding | Status |
|---|---|---|
| Standards | `working_dir: ${PWD}` in compose resolved to the host path, which does not exist in the image | fixed: `/app` |
| Standards | `.env` was not ignored, and it holds local paths | fixed: `.env` and `deploy/docker/.env` added to `.dockerignore` and `.gitignore` |
| Standards | the healthcheck and the service bind must agree on host and port | documented; both read `T3_BOARD_HOST`/`T3_BOARD_PORT` |
| Spec | "deployable via Docker end to end" | board, bridge and watcher all ran in containers; board served real data and enforced the token; workers produced their normal one-shot output |
| Spec | secrets must not enter the image | `.dockerignore` excludes `.env`, SQLite files, logs; token arrives through `env_file` |
| Gap | host networking is a real trade-off, not a detail | documented in `docs/docker.md`, including how to switch between compose and systemd |

---

## Round 6 (thread continuation)

| Finding | Severity | Status |
|---|---|---|
| Adoption created an `in-progress` ticket that the watcher never touched: no prompt was sent, nothing read the thread status, so the ticket could only sit there until the budget moved it to `blocked` | hard, spec | fixed: `thread_id` now means "continue this thread"; adopt creates a `ready` ticket; a sync stage advances from thread status |
| The dev stage always called `thread start`, so an adopted ticket would have duplicated the thread had it ever run | hard | fixed: branch on `thread_id` |
| Dry-run still referenced the removed `command` variable | hard | fixed with a per-path preview |
| Removing the dead `plan()` helper with a regex also removed the two new helpers | hard, process | caught by the suite; functions restored, `make check` green |
| The ticket dialog was taller than the viewport, hiding its buttons | judgement | fixed with `max-height` and internal scroll |
| Priority, model profile and the approval checkbox had no explanation in the UI | spec | fixed: labels, provider/model in the profile options, and helper text |
| Attachments | limitation | documented: a path is passed to the agent; `t3 thread send` is text only, so images cannot travel |

---

## Round 7 (model catalogue)

| Axis | Finding | Status |
|---|---|---|
| Spec | The board only offered four hard-coded profiles, so a model T3 already knew about was unreachable without editing config | fixed: `tickets/catalog.py` reads `~/.t3/caches/*.json`; tickets carry `provider` + `model`; presets became shortcuts |
| Spec | "Pick the harness" had no UI at all | fixed: harness select filters the model list; disabled harnesses never appear |
| Standards | A model option (`thinking`, `variant`) looked configurable but the CLI has no flag for it | documented in the UI and `docs/architecture.md`; options are shown read-only |
| Standards | An explicit provider without a model could reach the watcher and fail late | rejected at creation (`provider dan model harus diisi bersama`) |
| Standards | The catalogue file can be stale or half-written | parsing tolerates unreadable files and missing fields; `checkedAt` is surfaced so staleness is visible |
| Tests | Selection had no coverage | 10 catalogue tests plus 3 board tests for the endpoint and creation rules |
