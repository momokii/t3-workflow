# Handoff template

A starting point for moving work to another thread. Copy it, fill the brackets, delete what does not
apply. The point is not to write more prose; it is to point at evidence that can be regenerated.

## 1. Why a template

A handoff fails in three predictable ways:

1. **Stale numbers.** A state table written by hand stops being true as soon as it is committed.
   Always pair it with `make snapshot` → `STATE.md`, which is generated and git-ignored.
2. **Unread documents.** A prompt that says "read the docs" gets skimmed. Name the files, in order,
   and gate the first action behind a confirmation that quotes evidence from them.
3. **Undefined "done".** "Continue the work" produces motion, not progress. Every task needs a
   completion criterion and a verification command.

## 2. Structure

```markdown
# Handoff — [thread or project]

## 1. What this work is
[Two or three sentences. No history, no marketing.]

## 2. State (regenerate, do not trust prose)
Run `make snapshot` and read `STATE.md`. Anything written here is "at writing" only.

| Item | Value at writing |
|---|---|
| Repository / branch | [url] / [branch] |
| HEAD | [sha — or "see STATE.md"] |
| Services | [which units, or how to check] |
| Open tickets | [ids and states, or "see STATE.md"] |

## 3. Where the knowledge lives
| Question | File |
|---|---|
| How to use it | docs/usage.md |
| How it is built | docs/architecture.md |
| Endpoints | docs/api.md |
| Operations | docs/operations.md |
| Rules for agents | AGENTS.md |
| Why it is designed this way | RESEARCH.md / REVIEW.md |

## 4. Invariants
[The five to ten rules that must not be broken, each in one line, each checkable.]

## 5. What the previous session delivered
[Outcomes with evidence, not a diary. Numbers must be reproducible.]

## 6. Next tasks, in order
| # | Task | Completion criterion | Blocked by |
|---|---|---|---|
| 1 | [task] | [observable result] | [nothing / token / target] |

## 7. Known limitations (do not rediscover)
- [limitation, with the document that explains it]

## 8. Verify before trusting
```bash
make snapshot
make check
[service checks]
[one API or CLI check]
```
Expected: [what good looks like, with the current numbers].

## 9. Copy-paste prompt
```text
[The gate, the files, the confirmation, the rules, the "go" protocol.]
```
```

## 3. The gate

The prompt should force a **CONFIRMATION** block before any action, and the block should contain facts
that can be checked quickly:

- the HEAD, commit count and uncommitted-file count from `STATE.md` (three lines, five seconds to verify);
- five facts about the system, each with the file it came from;
- the resolution order or the state machine, whichever the work depends on;
- two documented limitations that must not be "fixed" by guessing.

If the confirmation is vague, the thread did not read. Stop it and ask again.

## 4. Rules that belong in every handoff

- Test and verify before claiming; paste the command and its output, not a summary of them.
- Commit per logical change; update the document that owns the behaviour in the same commit.
- Never force-push; never commit secrets; scan before committing.
- Do not spend tokens (real threads, real deployments) without an explicit instruction.
- Keep tests hermetic: no network, no real Telegram, no real T3 call.
- Reply in the owner's language; code, docs and commit messages stay English.

## 5. Checklist before you hand off

- [ ] Working tree clean, or the uncommitted files are named in §2.
- [ ] `make snapshot` runs and `STATE.md` is current.
- [ ] `make check` is green, or the failure is explained.
- [ ] Next task has a completion criterion and a verification command.
- [ ] Limitations are listed with a pointer to the document that explains them.
- [ ] The prompt names files, not just directories, and ends with "wait for go".
