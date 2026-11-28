# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.1] — 2026-11-26

### Fixed

- The dry-run watcher test used a project title, so it needed the real T3 database and failed on
  GitHub Actions. It now uses an absolute path, matching the other hermetic tests.
- Documentation now describes the published remote, the CI badge, and the fact that a repository
  without a remote cannot release.

## [Unreleased]

### Added

- **Remove a project from the board, with a typed confirmation.** `POST /api/projects/<id>/remove`
  requires `confirm` to equal the project title, then runs `t3 project remove --force` — the only
  thread deletion T3's CLI offers. The Register-project dialog lists existing projects and warns that
  tickets and reports are kept (their cards show **project missing**) while the project and all its
  threads are deleted.
- **Autonomous ticket loop.** A ticket is acknowledged, executed in a T3 thread, proved by the host's
  test command, reviewed by a second thread in a fresh context, repaired from the reviewer's findings
  (bounded), then pushed and reported. Telegram carries the acknowledgement, the plan, retries, the
  verdict and the report document; every ticket-driven message starts with `🤖 ticket automation` so it
  differs from a reply to a typed command.
- **Independent review gate.** `review` releases only on `VERDICT: ACCEPT` from the reviewer's last
  assistant message of the verified run (run id + `runCount == 1`); findings go into the dev thread on
  rejection, and a deleted reviewer thread, a missing verdict, a timeout or a tree that changed during
  the review blocks the ticket. Measured facts: `docs/verification-assumptions.md`,
  `docs/readonly-verifier.md`.
- **Bounded, classified retries.** A failing test sends its output into the dev thread; a crashed call
  or a dead thread returns the ticket to `ready`; a usage limit blocks with `retry_after` and retries
  itself after the backoff window; context overflow, a removed model and a provider mismatch block at
  once. Evidence: `docs/retry-evidence.md`.
- **Project defaults and one-line tickets.** `projects.<title>` can carry `test_cmd`, `profile` and a
  provider/model pair; the dialog and `cli add` use them, and the test command also suggests common
  commands and warns live while empty. The watcher still never invents a command: empty blocks at
  `testing`.
- **Edit and delete with history.** `store.update()` edits the human-owned fields (refused on `done`,
  `force` mid-flight); `store.delete()` writes the report, records a `deleted` snapshot and removes the
  row. Exposed in the board, `cli edit|delete` and the API; the report is still served after deletion.
- **The board reads as well as writes.** `report` opens a rendered reader (headings, lists, tables,
  code, links) with a raw link; `thread` shows title, project, status, created time and id with copy;
  every path field has a token-guarded folder picker (`GET /api/fs`, names only, system/hidden paths
  refused).
- **Board UX.** Dialogs open immediately with a loading state and a prefetched, cached catalogue; cards
  carry a review badge and a **project missing** marker when T3 lost the project.
- **Guide page** at `/guide` with screenshots, corrected structure and the failure/deletion tables.

### Fixed

- **The watcher crashed while polling a thread.** `sync_threads()` without a runner passed `None` into
  `thread_show()`, exiting the unit mid-loop; both normalise `runner or subprocess.run`, with a
  regression test. Found by the first real end-to-end run.
- **A thread or project deleted in T3 left the ticket waiting.** The watcher now reads "no longer
  available" and blocks the ticket with that reason; a missing project shows a badge instead of looking
  normal.
- **The review verdict could be read from the wrong run.** The stage stores and compares the run id,
  requires one run, reads only assistant messages, tolerates markdown emphasis/trailing house-style
  lines and refuses contradicting verdicts.
- **The guide's table of contents swallowed a section**, producing a broken two-column render; the list
  closes before the sections and tables scroll.
- **The empty-test-command warning could stay visible after a project default filled the field**, and a
  filled attachment path dead-ended the folder picker; both fixed.

### Changed

- **English everywhere a person reads text**; the passing-proof marker is `[PASS]`. A guard fails on
  Indonesian text in tracked files.
- Documentation aligned with the behaviour: README, AGENTS.md (invariants 6, 10, 11), `docs/usage.md`,
  `docs/architecture.md`, `docs/api.md`, `docs/operations.md`, the guide, `docs/autonomy.md`.

## [0.1.1] — 2026-11-26

### Fixed

- The dry-run watcher test used a project title, so it needed the real T3 database and failed on
  GitHub Actions. It now uses an absolute path, matching the other hermetic tests.
- Documentation now describes the published remote, the CI badge, and the fact that a repository
  without a remote cannot release.

## [Unreleased]

### Added

- **The board reads as well as writes.** `report` opens the markdown in a dialog rendered with headings,
  lists, tables, code and links (plus a link to the raw file); `thread` shows the thread's title,
  project, status and created time beside its id, with a copy button, instead of a toast with the raw
  id. Both are client-side, with no dependency.
- **A folder picker for every path field.** `GET /api/fs` lists names inside the home directory only
  and requires the board token; the attachment and project dialogs use it (file mode and folder mode).
- **The test command explains itself.** The field suggests common commands and warns live while empty;
  the guide states that a project can default it and that the watcher blocks rather than inventing one.
- **Automation messages are labelled.** Every ticket-driven Telegram message starts with
  `🤖 ticket automation`, so it reads differently from a reply to a command typed by hand.

### Fixed

- **A usage limit retries itself, within bounds.** T3's error item marks a usage limit permanent but
  never carries a reset time, so the watcher blocks the ticket with `retry_after` and `quota_attempts`
  and returns it to `ready` after `retry.quotaBackoffMinutes` (default 60), up to
  `retry.maxQuotaAttempts` (default 3) windows; then it waits for a person. Context overflow, a removed
  model and a provider version mismatch still block immediately, because a retry cannot fix them. A
  successful start clears the quota budget with the other counters.

### Fixed

- **The board reflects a deletion made in T3 instead of silently waiting.** When a ticket's thread is
  gone, the watcher reads the CLI's "no longer available" answer and blocks the ticket with that reason
  (including a deleted reviewer thread); when a ticket's project no longer exists in T3, `GET
  /api/tickets` marks it `project_missing` and the card shows a badge. Deleting a ticket still never
  deletes a T3 thread — a thread is T3's work history — and a deleted ticket keeps its report and a
  snapshot in the event log. A blocked card now says how to retry: **Move → Ready**.

### Fixed

- **The plan arrives on Telegram.** After the "in progress" acknowledgement the bridge forwards the dev
  thread's first assistant message of the run (trimmed to 900 characters), once per run, keyed by
  `thread:latestRunId` in its state file. It is read from the thread T3 already keeps, not asked for.

### Fixed

- **Edit and delete a ticket**, with history. `store.update()` edits the human-owned fields (title,
  context, test command, priority, attachment, preset, provider/model, project), refuses a `done`
  ticket and requires `force` while a stage is working; `store.delete()` writes the report first,
  records a `deleted` event with a row snapshot and removes the ticket, and `/api/report/<id>` keeps
  serving the file. Exposed as `POST /api/tickets/<id>/edit`, `POST /api/tickets/<id>/delete`
  (`confirm: true`), a board edit dialog with a delete confirmation, and
  `tickets/cli.py edit|delete`.
- **Project defaults** (`projects.<title>` in config): the ticket dialog prefills the test command and
  preset from the selected project, and `tickets/cli.py add` uses them when `--test`/`--profile` are
  omitted, so a ticket can be a title plus a target. The map ships in `GET /api/tickets` as
  `projectDefaults`.

### Fixed

- **The watcher crashed in production while polling a thread.** The service calls `sync_threads()`
  without a runner; that `None` reached `thread_show()` as the callable and raised `TypeError`,
  exiting the unit mid-loop twenty seconds after it started the dev thread. Both functions normalise
  `runner or subprocess.run`, and a regression test asserts the callable reaches the polling helper.
  Found by the first real end-to-end run (`T-0004`), not by the hermetic suite.
- Successful report delivery is audited (`{"action": "report"}`) and logged, so a delivered document
  can be told apart from a silent one.

### Added

- **Retries are classified, not guessed.** The watcher reads the last run's error item from T3's
  database (`failure.class`, `failure.message`, read-only) and blocks at once on a usage limit, a
  context overflow, a removed model or a provider version mismatch, while timeouts, connection drops,
  429s, 5xx and DNS failures keep the bounded retry. Measured evidence and the taxonomy:
  `docs/retry-evidence.md`.
- **A stuck ticket no longer starves the queue.** An unselectable model/preset blocks the ticket
  instead of leaving it `ready` to be picked forever, and a release that can never succeed (no remote,
  unsupported mode, failed push) blocks with its reason.

### Fixed

- **The loop repairs itself.** A failing test now sends its output tail into the dev thread and the
  ticket returns to `in-progress`, bounded by `retry.maxTestAttempts`; a crashed `t3` call or a dead
  thread returns the ticket to `ready`, bounded by `retry.maxThreadAttempts`. Counters live on the
  ticket (`test_attempts`, `dev_attempts`) and `dev_run_id` stops `testing` from re-running against the
  code that just failed. A missing test command, an unknown model, a missing remote and a review
  rejection never retry blindly. `docs/autonomy.md` is the decision record.
- **Telegram acknowledges the work and delivers the report.** The bridge announces a claimed ticket, a
  retry, the review verdict, and uploads the markdown report as a document on `done` (stdlib multipart
  upload, no new dependency).
- **The review outcome is visible where you look.** A card carries `review accept 1/2` (or reject,
  failed, timeout) with text as well as colour; `tickets/cli.py show` prints the verdict, reviewer and
  findings. `GET /api/tickets` adds `verification: {enabled, maxAttempts}` and keeps `verify_findings`,
  `verify_baseline` and `verify_run_id` private.

### Fixed

- **The review verdict is anchored to the run that produced it.** The stage stored `verify_run_id` and
  never compared it, and it read the last message rather than the last assistant message. Measured
  against real threads: `thread start --json` may return `runId: null`, `thread show` carries
  `latestRunId`/`runCount`/per-message `runId`/`role`, and real replies decorate the verdict line
  (17 % with markdown emphasis, 3 % with a `Skills:` line after it). The stage now reads one
  `thread show`, learns the run id when the launch omits it, refuses a stale run (`latestRunId`
  mismatch) or a second run (`runCount > 1`), ignores non-assistant text, and accepts a verdict line
  that consists of the token after stripping list markers and emphasis — while refusing two
  contradicting verdicts and any prose. An unknown thread status now blocks with its own note instead
  of falling through to "no verdict line". See `docs/verification-assumptions.md`.

### Added

- **The guide has screenshots and covers registering and adopting.** Three captures taken from a real
  board running on a throwaway database (headless Chromium, 2× DPR off, no real ticket data): the board
  with all seven columns, the New ticket dialog, and the Adopt a T3 thread dialog. The section on
  projects now explains what `Register project` does and why the path must already exist.
- **Per-field explanations in every dialog.** The register and adopt dialogs now explain each field the
  way the ticket dialog does: what the project name is for, why the path is the repository root, which
  threads are listed, why the preset is informational, and what the test command proves.
- **A separate Guide button.** It is accent-coloured with a book icon and an accessible label, instead of
  looking like a fourth action next to New ticket, Adopt and Register.

### Changed

- README, AGENTS.md (invariant 10: review before release), docs/architecture.md, docs/operations.md and
  docs/api.md now describe the review stage, the four stages, the verification columns and the guide page.

### Fixed

- **Independent review before release.** A ticket in `review` now starts a second thread in a fresh
  context (same harness and model by default, `verification.profile` to override) that receives the
  ticket, the test proof, `git status` and `git diff HEAD`, and must end with `VERDICT: ACCEPT` or
  `VERDICT: REJECT`. Rejections send the findings into the dev thread and cost one attempt (default 2);
  a missing verdict, a failed reviewer, a timeout or a project that changed mid-review blocks the ticket.
  The tree fingerprint is what keeps a verdict tied to the code it judged, because T3 0.1.9 has no
  read-only runtime mode (`docs/readonly-verifier.md`). Nine ticket columns, five event kinds and an
  "Independent review" section in the report; `verify_findings` is hidden from `GET /api/tickets`.

### Changed

- **Guide page** at `/guide`, linked from the board header: the lifecycle and who moves a ticket, what the
  test command is for and how its exit code decides, the model resolution order, priorities, approvals,
  adoption, limits, Telegram commands, where the data lives and the gaps that remain. The ticket dialog
  links to its test-command section from the field itself.
- `load_catalog_cached()`: the catalogue is re-read only when the cache files change (names, mtimes,
  sizes) instead of on every request; `GET /api/catalog` and ticket creation share it.

### Fixed

- **New ticket and Adopt thread open immediately.** Both dialogs used to wait for their network calls
  before appearing, so a slow T3 call looked like a dead button; they now open with a loading state,
  disable submit until the data arrives, name the failure inside the dialog, and the catalogue is
  prefetched after the board loads.
- The board dropped its cached catalogue on every 15-second refresh, which could leave the pickers empty
  until the next dialog open; the refresh now preserves it.

### Changed

- **One language, English, everywhere a person reads text.** API errors, board labels and toasts,
  Telegram replies and help, CLI output, watcher notes, report and PR-body sections, script messages
  and the docs quotes now speak English; the board page declares `lang="en"`. The passing-proof marker
  is now `[PASS]` (it used to be the Indonesian word for *pass*), so the release gate,
  `AGENTS.md`, `README.md`, the runbook and the architecture diagram moved with it. `tests/test_language.py` scans every tracked text file and fails
  on Indonesian vocabulary, so the next copied error string cannot drift back.

### Added

- `docs/handoff-template.md`: the structure and gate a handoff should follow, including
  snapshot-first verification and a checklist.
- `make snapshot` / `scripts/snapshot.py`: writes `STATE.md` with the measured git state, service
  states, ticket summary, model catalogue and documentation list, so a handoff or review can point at
  a regenerable file instead of prose numbers.
- **Live model catalogue**: harnesses and models read from T3's provider cache, with per-ticket
  `provider` + `model` selection that overrides presets, validated at creation (disabled harness,
  unknown model and half-filled pairs are rejected). Presets remain as shortcuts; `GET /api/catalog`
  exposes the catalogue to the board.
- **Thread continuation**: a ticket that carries a `thread_id` is continued with `t3 thread send`
  instead of starting a second thread, and a sync stage advances the ticket from the thread's status.
- Adopting a thread now creates a `ready` ticket that keeps the thread, instead of an `in-progress`
  ticket the watcher never touched.
- Ticket context (`body`) and an optional attachment path, carried into the prompt.
- Profile details (provider and model) in the board, priority labels with an explanation, and helper
  text for the test command and the approval checkbox.
- [docs/usage.md](docs/usage.md): workflows, model and priority guidance, FAQ.
- Container deployment: `Dockerfile`, `docker-compose.yml` (board by default, `bridge` and `watcher`
  under the `automation` profile), `deploy/docker/.env.example`, `deploy/docker/smoke.sh`, and
  [docs/docker.md](docs/docker.md).
- `GET /healthz` on the board, used by the container healthcheck.
- Documentation set: [docs/architecture.md](docs/architecture.md), [docs/api.md](docs/api.md), [docs/operations.md](docs/operations.md).
- Board actions to create a ticket, register a T3 project, and adopt an existing T3 thread as a ticket.

## [0.1.0] — 2026-11-26

First release: the ticket → thread → test → release loop, with a board and a Telegram bridge.

### Added

- **Telegram bridge** (`bridge/t3_bridge.py`): `/projects`, `/threads`, `/ctx`, `/send`, `/new`, `/tickets`, `/ticket`, `/pending`, `/approve`, `/reject`, `/id`, `/help`. Long polling, chat allowlist, JSONL audit. `/send` and `/ctx` accept a thread id or a project name that has exactly one thread.
- **Ticket store** (`tickets/`): state machine `backlog → ready → in-progress → testing → review → done` plus `blocked`, atomic claim, recorded `--force` override, approvals, proofs, markdown reports written on completion.
- **Model profiles** in `~/.config/t3-workflow/config.json`: `fast`, `deep`, `cheap`, `build`, spanning the `pi` and `opencode` providers. The watcher reads a profile and never chooses a model itself.
- **Watcher** (`watcher.py`): dev stage (start a thread), test stage (run `test_cmd`, store proof, advance only on exit code 0), release stage (branch `ticket/T-xxxx-…`, commit, push, compare link, PR body file), a concurrency limit and a wall-clock budget.
- **Kanban board** (`board/`): drag and drop plus a single-pointer move control (WCAG 2.2 SC 2.5.7), approvals with approve/reject, ticket creation, project registration, thread adoption, responsive layout, dark mode, logo and favicon, per-ticket reports.
- **Services**: `t3-bridge`, `t3-watcher`, `t3-board`, `t3-backup.timer` (daily, retention 7), `t3-alert@.service` for `OnFailure`.
- **Tests**: 57 workflow/board tests plus 24 bridge tests, and one integration suite that is skipped unless `T3_INTEGRATION=1`.
- **Tooling**: `make test`, `make check`, GitHub Actions workflow, tag `v0.1.0`.

### Known limitations

- A repository without a remote cannot release; the stage fails loudly instead of skipping.
- No pull-request creation through an API (requires `gh` or a GitHub token); a compare link and a PR body file are produced instead.
- No deploy or rollback step.
- Tickets cannot be edited or deleted after creation.
