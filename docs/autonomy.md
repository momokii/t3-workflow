# Autonomous loop — decisions, limits and evidence

The destination: **a ticket typed by the owner is acknowledged, planned, executed, reviewed by an
independent thread, fixed and re-reviewed until it is right, then finished with a report that reaches
Telegram — without the owner watching.** This page records the decisions that make that true, what is
enforced rather than promised, and what is deliberately out of scope.

## The loop

| Step | Implementation | Evidence on the ticket |
|---|---|---|
| Acknowledge | the watcher claims the ticket and starts/continues the thread; the bridge announces `in-progress` | `claimed` event, Telegram message |
| Plan + execute | one T3 thread with the ticket body as the contract; `thread start` or `thread send` when adopted | `thread` / `continued` events |
| Prove | the host runs `test_cmd` after the thread finishes | `proof` = `[PASS]/[FAIL]` + output tail |
| Independent review | a second thread, fresh context, same harness/model by default | `verify-started`, `verify-accept`/`verify-reject` |
| Repair | findings go into the dev thread, the ticket returns to `in-progress`; testing waits for the next run | `retry`, `continued` |
| Finish | release pushes `ticket/T-xxxx-…`, writes the report | `branch`, `report`, `done` |
| Report | the bridge uploads the markdown report as a Telegram document | `report_path` |

## Decisions

1. **Same model for the reviewer by default.** The owner chose "everything the same" for now: a fresh
   context, not a different model. `verification.profile` names another preset when that changes.
2. **The reviewer runs in the project folder**, not a worktree, so it sees the uncommitted work the
   release will commit. The tree fingerprint (`verify_baseline`) is the guard: a verdict that no longer
   matches the tree is refused.
3. **No read-only sandbox exists** in T3 0.1.9 (`docs/readonly-verifier.md`), so enforcement is evidence,
   not permissions: run anchoring, fingerprint, bounded attempts.
4. **Retries are bounded and classified** (`docs/usage.md`): transient T3
   failures and failing tests retry with a counter; configuration errors and review rejections do not
   retry blindly.
5. **The verdict is anchored to a run**: only the last assistant message of `verify_run_id` counts, and
   `runCount > 1` blocks (`docs/verification-assumptions.md`).
6. **Reports are markdown.** Telegram receives the file itself; a PDF renderer is a dependency without
   a need.
7. **Nothing is merged or deployed.** Release stops at a pushed branch, a compare link and a PR body.

## What is enforced vs. promised

Enforced in code and tests: the state machine, the atomic claim, the exit-code test gate, the verdict
parse and its run anchor, the fingerprint guard, the bounded counters, and the report file.

Prompted, not enforced: the quality of the dev agent's plan, the reviewer's judgement, and the model's
willingness to keep the verdict on its own line (tolerated decorations are listed in
`docs/verification-assumptions.md`).

## Out of scope for this effort

Pull-request creation (needs `gh` or a token), deploy + rollback (needs a real target), token/cost
reporting (T3 exposes cumulative counters only), per-ticket model options (no CLI flag), image
attachments (`thread send` is text), and a browser-driven test for the board JS (the host blocks the
browser sandbox; see HANDOFF §8).
