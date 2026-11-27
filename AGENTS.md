# AGENTS.md — t3-workflow

Rules for coding agents working in this repository. `README.md` explains usage,
`docs/` holds the architecture, HTTP API and operations runbook, and `RESEARCH.md`
records the design and the measured facts behind it. When behaviour changes, update the
document that owns it in the same commit: usage → `README.md`, endpoints → `docs/api.md`,
schema or state machine → `docs/architecture.md`, procedures → `docs/operations.md`.

## Read before changing behaviour

- `RESEARCH.md` §3–§6 for the lifecycle and what each stage is allowed to do.
- `tickets/store.py` for the state machine (`TRANSITIONS`) and the atomic claim.
- `watcher.py` for the dev, test and release stages, and `board/server.py` for the HTTP surface.
- `docs/architecture.md` for the data model and the sequences; `docs/api.md` for every endpoint.

## Invariants

1. **Status changes go through the store.** `move()` validates transitions; `force=True`
   is the human override and is recorded as a `forced` event, never silently.
2. **One ticket, one thread.** A ticket is claimed with a single conditional `UPDATE`;
   claim failures are expected and must be reported, not swallowed.
3. **The model profile comes from config.** A ticket names a profile; an unknown profile
   is rejected at creation and blocks the watcher. Never invent a provider or model.
4. **Approvals are one-shot.** `decide_approval()` refuses a second decision; the first
   answer wins across all channels.
5. **Test stage decides from exit codes**, not from prose. No output, no advance.
6. **Reports are evidence.** Do not hand-edit `~/.local/state/t3-workflow/reports/`;
   regenerate them from the ticket.
7. **Release pushes only a ticket branch.** The base branch is never pushed, and a ticket
   without a `[PASS]` proof is refused.
8. **Board writes need the token.** Every `POST` passes through `X-Board-Token`; `GET` is
   open to the tailnet, which is the real boundary.
9. **T3 stays the owner of projects and threads.** Read `statev2.sqlite` read-only; create
   projects through `t3 project add`, never by writing the database.

## Environment

- Tests: `make test` (bridge suite + tickets/board/watcher suite). Keep them hermetic:
  no network, no real Telegram, no real T3 calls, and `T3_REPORTS_DIR` pointed at a temp dir.
- Python 3.12, standard library only. Do not add third-party dependencies.
- The board binds the Tailscale interface; do not change it to `0.0.0.0`.
- `make check` compiles every module and runs both suites; it is what CI runs.
- Documentation is written in English. Quoted API messages stay verbatim even when the
  service returns them in another language.

## Git

- Conventional commits, one logical change per commit.
- **Pushes are automatic, on a branch.** The release stage pushes `ticket/T-xxxx-…`
  to `origin` and then finishes the ticket. It never pushes the base branch, and a
  repo without a remote makes release fail loudly instead of silently skipping.
