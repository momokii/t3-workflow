# AGENTS.md — t3-workflow

Rules for coding agents working in this repository. `README.md` explains usage;
`RESEARCH.md` holds the design and the measured facts behind it.

## Read before changing behaviour

- `RESEARCH.md` §3–§6 for the lifecycle and what each stage is allowed to do.
- `tickets/store.py` for the state machine (`TRANSITIONS`) and the atomic claim.
- `watcher.py` for the dev/test stages, and `board/server.py` for the HTTP surface.

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

## Environment

- Tests: `make test` (bridge suite + tickets/board/watcher suite). Keep them hermetic:
  no network, no real Telegram, no real T3 calls, and `T3_REPORTS_DIR` pointed at a temp dir.
- Python 3.12, standard library only. Do not add third-party dependencies.
- The board binds the Tailscale interface; do not change it to `0.0.0.0`.

## Git

- Conventional commits, one logical change per commit.
- **No push.** This repository has no remote by design; the human pushes.
