# Usage guide

How to use the system, which workflow to pick, and what the watcher will and will not do on your behalf.

Read [README.md](../README.md) for installation and [docs/architecture.md](architecture.md) for the mechanics.

## 1. The mental model

| Object | Lives in | Meaning |
|---|---|---|
| **Ticket** | this repository (`tickets.sqlite`) | the unit of work: what to do, how it is tested, which model, how urgent |
| **Thread** | T3 Code | where an agent actually works: plan, edits, commands, questions |

A ticket may or may not carry a thread:

- **no `thread_id`** — the watcher starts a **new thread** when the ticket becomes `ready`;
- **with `thread_id`** — the watcher **continues that thread** with the ticket's prompt. It never starts a second thread for the same ticket.

That single rule decides the whole flow, and it is what makes adoption real rather than bookkeeping.

## 2. What runs where

```
you                watcher                          T3 thread            host
─────────────────────────────────────────────────────────────────────────────
create ticket  →   ready → claim
                   send prompt ───────────────────→  agent plans, edits
                                                     agent finishes
                   sync: thread completed   ←───────┘
                   → testing
                   run test_cmd ──────────────────────────────────────────→ shell
                   exit 0 → review                                     ←────┘
                   release: branch + push
                   → done (report written)
```

Two honest clarifications:

1. **The agent works in the thread; the test runs outside it.** The test stage executes the ticket's
   `test_cmd` on the host (or in the container) and reads the exit code. It does not ask the thread to
   run the tests, so a passing test cannot be faked by the model.
2. **Planning happens inside the thread.** There is no separate "plan gate". The ticket body is the
   contract; if you want to see a plan first, say so in the body and set *requires approval* so the
   watcher waits for you before it starts.

## 3. Two workflows

### A. New work → new thread

Use when nothing exists yet.

1. **＋ Ticket** on the board (or `/ticket` in Telegram, or `tickets/cli.py add`).
2. Fill in the title, project, model profile, **context** (acceptance criteria), optional attachment
   path, and a **test command** that means "done".
3. Move the ticket to `ready`.
4. The watcher claims it, starts one thread, and records the thread id on the ticket.
5. When the thread finishes, the watcher moves the ticket to `testing` by itself, runs the test
   command, and either sends it to `review` (exit 0) or back to `in-progress` with the failure text.
6. A ticket in `review` is released automatically: `ticket/T-xxxx-<slug>` is pushed and the ticket
   becomes `done` with a report.

### B. Existing work → adopt the thread

Use when an agent is already working, or when you want to continue an earlier thread instead of
starting over.

1. **Adopt thread** on the board, pick the thread from the list, add context and the test command.
2. The ticket is created as `ready` **with the thread id attached**.
3. The watcher sends the ticket prompt **into that same thread** and records a `continued` event.
4. The rest is identical: thread finishes → `testing` → `review` → release → `done`.

The work, the plan and the follow-up questions all stay in one thread, which is what keeps the
context intact. The board shows the ticket and its thread side by side.

### C. Work that needs your decision first

Tick **requires approval** (or `/ticket` then edit the flag in the database — the UI is the intended
path). The watcher creates an approval request instead of starting:

- Telegram sends one message with the id;
- the board shows a card with **Approve** / **Reject**; `/approve <id>` and `/reject <id>` work too;
- the first decision wins; a second one is rejected;
- `reject` moves the ticket to `blocked`, `approve` lets the watcher proceed.

## 4. Recommendations

### Choosing the harness and the model

The board does not invent a list. It reads T3's own provider cache (`~/.t3/caches/<instanceId>.json`),
so the dropdowns show exactly the harnesses and models T3 currently reports as installed and enabled:
`pi` and `opencode` on this machine, with 38 and 141 models respectively. Disabled harnesses are
listed nowhere in the pickers, and the API rejects them.

There are two ways to choose:

1. **Harness + model** — pick the harness (`pi`, `opencode`, …) and then a model from that harness.
   This is stored on the ticket as `provider` + `model` and wins over everything else.
2. **Preset** — a named pair from `~/.config/t3-workflow/config.json`. Choosing a preset fills the
   harness and model for you; leaving it on *manual* keeps whatever you selected.

Resolution order, from strongest to weakest: **ticket `provider`/`model` → ticket preset →
`defaults.profile`**. A half-filled choice (a provider without a model) is rejected, and so is a
model that the catalogue does not list for that harness.

**Model options** (`thinking`, `variant`, `effort`, `agent`) are shown under the model picker for
information. The `t3 thread start` CLI has no flag to set them, so the thread keeps the project
default options; changing them per ticket is a T3 feature, not something this tool can fake.

The bundled presets remain a convenience, not a limit:

| Preset | Provider / model | Use for |
|---|---|---|
| `fast` | `pi` · deepseek-v4.1-flash | most work: code changes, tests, documentation |
| `deep` | `pi` · qwen3.8-max | ambiguous problems, architecture, debugging that resists a quick pass |
| `cheap` | `pi` · claude-haiku-5-5 | mechanical edits, renames, small fixes |
| `build` | `opencode` · big-pickle | when you specifically want the other harness driving the thread |

### Priority

| Value | Meaning | When |
|---|---|---|
| 1 | urgent | production broken, blocking work |
| 2 | high | promised for today, or blocks somebody else |
| 3 | normal | the default queue |
| 4 | low | polish, cleanup |
| 5 | later | ideas that should not compete for the slot |

The watcher takes the lowest number first, then the oldest ticket at that priority.

### Writing the test command

- It must run from the project directory and end with an exit code: `make check`, `go test ./...`,
  `npm test -- --run`, `python3 -m unittest discover -s tests`.
- Prefer the same command CI runs, so a green ticket means a green pipeline.
- A command that prints a lot is fine; only the last 40 lines are stored as proof.
- An empty test command cannot advance: the watcher records that and sends the ticket back.

### Context and attachments

- **Context** is the ticket body: acceptance criteria, file hints, links, what not to touch. The agent
  receives it verbatim in the prompt.
- **Attachment** takes an absolute path on this machine. The path is passed to the agent, which can
  read the file; nothing is uploaded and no binary is stored in the ticket.
- Images cannot be attached through this flow. `t3 thread send` accepts text only; if an image
  matters, describe it or put it in the repository and reference the path.

### When to adopt instead of creating

| Situation | Do this |
|---|---|
| An agent already produced work in a thread | adopt that thread |
| You want to continue yesterday's thread | adopt it |
| A new, independent piece of work | create a ticket (new thread) |
| The same topic but a different deliverable | create a new ticket; keep the old thread as history |
| A thread that is still running | adopt it — the watcher queues the prompt as the next turn |

## 5. Walkthrough: adopt a running thread

```
1. Board → Adopt thread → pick "cve-injector · Fix image size [running]"
2. Profile: fast · Context: "Selesaikan perubahan ukuran gambar, tambahkan test." · Test: make check
3. Ticket T-0007 appears in Ready with the thread attached.
4. Watcher: sends that prompt to the thread (event: continued).
5. Thread finishes → watcher moves T-0007 to Testing.
6. Watcher runs make check → exit 0 → Review.
7. Release pushes ticket/t-0007-… and writes the report; T-0007 is Done.
```

If the thread fails instead, the watcher moves the ticket to `blocked` and the note says
`thread failed`. Move it back to `in-progress` after fixing the cause.

## 6. FAQ

**Does everything happen on the same thread?**
The agent's work does. Tests run outside the thread by design, and the release is git, not an agent
task. One ticket never gets a second thread.

**Can the watcher change the thread's status?**
No. T3 owns thread state; the watcher only reads it (`thread show`) and moves the ticket accordingly.

**What if the agent asks me something mid-thread?**
T3 reports the thread as waiting. The watcher leaves the ticket alone; answer in the T3 UI. Approval
requests inside a run are not answered from Telegram (the CLI has no such command).

**Why did my ticket go back to `in-progress`?**
The test failed. The output tail is in the ticket's proof and in the report. Fix, then move it to
`testing` again (or let the watcher pick it up after you push a fix into the thread).

**Can I edit a ticket?**
Not yet. Create a new one, or move it with **Force move** and add the note. Editing is a known gap.

**Why is the priority a dropdown and not a checkbox?**
A checkbox only carries two levels. Five levels let the queue stay meaningful; the labels in the
dialog spell out what each one means.

**Why are there two dropdowns instead of one?**
A harness (provider instance) hosts many models, so the choice is two steps: pick the harness, then
the model it offers. Both lists come from T3's cache, which is why a harness you disabled in T3 simply
does not appear.

**Can I let it run unattended?**
Yes, within the limits: `watcher.maxConcurrent` keeps one ticket in flight, and
`limits.maxInProgressMinutes` moves a stuck ticket to `blocked`. Push is automatic to a ticket branch;
merging remains yours.

## 7. Do and do not

- **Do** write a test command that fails before the change and passes after it.
- **Do** adopt a thread instead of duplicating work.
- **Do** use approval for tickets that touch credentials, money or production configuration.
- **Do** keep one ticket for one deliverable; split large work into several tickets.
- **Do not** put secrets in the ticket body: it is stored in SQLite and echoed in reports.
- **Do not** expect the watcher to merge or deploy; those steps are deliberate gaps.
- **Do not** run the systemd watcher and the compose watcher at the same time.
