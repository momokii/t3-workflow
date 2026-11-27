# Operations runbook

Day-two operations for the four moving parts. Every command here has been run on the machine this repository was built on.

## Services at a glance

```bash
systemctl --user list-units 't3-*' --all
systemctl --user is-active t3-bridge t3-watcher t3-board
systemctl --user list-timers t3-backup.timer
```

| Unit | Failure you would notice | First check |
|---|---|---|
| `t3-bridge` | Telegram stops answering | `journalctl --user -u t3-bridge -n 50` |
| `t3-watcher` | tickets stop advancing | `journalctl --user -u t3-watcher -n 50` |
| `t3-board` | the board URL does not load | `ss -ltn \| grep 8787` |
| `t3-backup.timer` | no new file in `backups/` | `journalctl --user -u t3-backup -n 20` |

Every long-running unit carries `OnFailure=t3-alert@%N.service` in `[Unit]`, so a crash sends one Telegram message through `scripts/alert.py`.

## Start, stop, restart

```bash
systemctl --user restart t3-watcher      # after a code change
systemctl --user restart t3-bridge t3-board
systemctl --user disable --now t3-watcher   # stop automation entirely
```

Restart the service after editing any Python file: the running process keeps the old code. Static files under `board/static/` are re-read per request, so the page needs no restart.

## Pause automation without stopping the service

```bash
python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home()/".config/t3-workflow/config.json"
c = json.loads(p.read_text()); c["watcher"]["enabled"] = False
p.write_text(json.dumps(c, indent=2)+"\n")
PY
systemctl --user restart t3-watcher
journalctl --user -u t3-watcher -n 3     # "watcher paused by config"
```

Set it back to `true` and restart to resume. The bridge and the board keep working while the watcher is paused.

## Rotate the board token

```bash
NEW=$(openssl rand -hex 16)
printf 'T3_BOARD_TOKEN=%s\n' "$NEW" > ~/.config/t3-workflow/board.env
chmod 600 ~/.config/t3-workflow/board.env
systemctl --user restart t3-board
```

The page embeds the token at serve time, so a reload picks up the new one. Any script holding the old token gets `401` immediately.

## Backup

Automatic: `t3-backup.timer` runs `tickets/cli.py backup --keep 7` daily at 03:20 (persistent, so a missed run happens after boot).

```bash
python3 tickets/cli.py backup                      # on demand
ls -lh ~/.local/state/t3-workflow/backups/
```

Each run writes `tickets-YYYYMMDD-HHMMSS.sqlite` (SQLite backup API, consistent while services run) and copies `reports/` next to it. Retention keeps the newest 7 of each.

## Restore

Stop the writers first, so nothing is appended during the swap.

```bash
systemctl --user stop t3-bridge t3-watcher

LATEST=$(ls -1t ~/.local/state/t3-workflow/backups/tickets-*.sqlite | head -1)
STAMP=$(basename "$LATEST" .sqlite | cut -d- -f2-)

# keep the current database, then restore
mv ~/.local/state/t3-workflow/tickets.sqlite \
   ~/.local/state/t3-workflow/tickets.sqlite.before-restore
cp "$LATEST" ~/.local/state/t3-workflow/tickets.sqlite

# restore the reports that belong to that snapshot
rm -rf ~/.local/state/t3-workflow/reports
cp -r ~/.local/state/t3-workflow/backups/reports-"$STAMP" ~/.local/state/t3-workflow/reports

# verify before starting anything
python3 tickets/cli.py list | head
python3 -c "import sqlite3,pathlib;c=sqlite3.connect(pathlib.Path.home()/'.local/state/t3-workflow/tickets.sqlite');print('tickets:',c.execute('select count(*) from tickets').fetchone()[0]);print('approvals:',c.execute('select count(*) from approvals').fetchone()[0])"

systemctl --user start t3-bridge t3-watcher
```

Verification that the restore worked: `tickets/cli.py list` shows the tickets from that snapshot, `pending_approvals()` is consistent (no orphan `approval_id`), and the board loads with the same counts.

## Incidents

| Symptom | Likely cause | Fix |
|---|---|---|
| Bridge silent, `getUpdates` failing | network or a competing webhook | `journalctl --user -u t3-bridge -n 50`; confirm no webhook with the Bot API `getWebhookInfo` |
| Cycle repeats "no ready tickets" | nothing queued | move a ticket to `ready` |
| Ticket stuck `in-progress` | thread still running, or lost | wait for `limits.maxInProgressMinutes`, or move it by hand with `--force` |
| Release fails with "has no remote" | repository has no `origin` | `git remote add origin <url>` |
| Release fails with "has no passing test evidence" | ticket reached `review` without a test run | move it back to `testing` and let the watcher run it |
| Approval never arrives in Telegram | bridge down, or chat id not allowlisted | check the bridge log; `/id` from the chat and compare with `allowChatIds` |
| Board returns `401` on a move | page loaded before a token rotation | reload the page |
| `t3-alert@…` did not fire | `OnFailure` written under `[Service]` | it belongs in `[Unit]`; check `systemctl --user show t3-watcher -p OnFailure` |
| Review blocks with "no verdict line in the reply" | the reviewer replied without the required last line | read `/api/report/<id>`, then either send it back (`blocked → ready`) or force `done` if you accept the work yourself |
| Review blocks with "project changed during verification" | something edited the project while the reviewer was reading it | move the ticket to `ready`; the next review takes a fresh fingerprint |
| Review blocks after two rejections | `verification.maxAttempts` reached | read the findings in the note and the report; fix in the dev thread, then `blocked → ready` |
| Review blocks with "reviewer timed out" | no verdict within `verification.timeoutMinutes` | inspect the review thread in T3; raise the timeout only if the work is genuinely large |

## Where the state lives

| Path | Contents | Grows |
|---|---|---|
| `~/.local/state/t3-workflow/tickets.sqlite` | tickets, events, approvals | with tickets and moves |
| `~/.local/state/t3-workflow/reports/` | `T-xxxx.md`, `T-xxxx.pr.md` | one pair per finished ticket |
| `~/.local/state/t3-workflow/backups/` | snapshots | daily, pruned to 7 |
| `~/.local/state/t3-workflow/audit.jsonl` | every bridge command and decision | append-only; rotate by hand if it matters |
| `~/.config/t3-workflow/config.json` | profiles, limits, release policy | rarely |
| `~/.config/t3-workflow/board.env` | board token | on rotation |

## Logs

```bash
journalctl --user -u t3-watcher --since today
journalctl --user -u t3-bridge  --since today
journalctl --user -u t3-board   --since today
tail -n 100 ~/.local/state/t3-workflow/audit.jsonl
```

Watcher output is one line per step (`22:49:14 no ticket is ready`). Bridge output is one line per state change and per error. Neither logs the Telegram token.

## Upgrade

```bash
git pull                 # origin is https://github.com/momokii/t3-workflow.git
make check               # compile + both suites
systemctl --user restart t3-bridge t3-watcher t3-board
```

Migrations run on open: stopping the services is not required for a column addition, but it is the safe order. A downgrade is not supported; restore a backup instead.
## Restore drill (verified 2026-11-26)

The restore path was exercised against a copy, so production was never at risk:

```bash
DRILL=$(mktemp -d)
LATEST=$(ls -1t ~/.local/state/t3-workflow/backups/tickets-*.sqlite | head -1)
cp "$LATEST" "$DRILL/tickets.sqlite"
python3 tickets/cli.py --db "$DRILL/tickets.sqlite" list | head -3
python3 -c "import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);print('tickets:',c.execute('select count(*) from tickets').fetchone()[0]);print('approvals:',c.execute('select count(*) from approvals').fetchone()[0])" "$DRILL/tickets.sqlite"
```

Expected: the CLI prints the tickets held in the snapshot, and the counts match the snapshot rather than
the live database. A drill printing zero tickets means the wrong file was copied, not that the backup is empty.
