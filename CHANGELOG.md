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
