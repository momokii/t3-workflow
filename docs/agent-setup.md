# Agent setup contract

This page is for an agent that has been asked to install and configure `t3-workflow` on a machine the
human owns. It is a sequence of checkpoints, not a shell script to run blindly. Read
[`installation.md`](installation.md) first; that page explains the architecture and the optional
components.

## Rules for the agent

- **T3 Code is required.** Do not claim the workflow is installed if there is no running T3 server,
  no `t3` CLI, no T3 database, or no installed/signed-in agent.
- **Ask before installing system software.** The official T3 installer is a network script; show the
  command and get permission. Do not install packages, Tailscale, Docker or systemd units unless the
  human asked for them.
- **Never invent provider or model values.** Collect `providerInstanceId` and model `id` from T3's
  catalogue (`GET /api/catalog` or the board's ticket dialog) or copy an existing working profile.
- **Never print secrets.** Do not echo Telegram bot tokens, board tokens, SSH keys or vault contents
  into the conversation or the completion report.
- **Never write to the brain vault.** This repository only reads it. Do not run `brain put`, do not
  edit generated notes, and do not rewrite `10 Projects/`, `40 Archive/` or `90 Meta/`.
- **Never bind the board to `0.0.0.0`.** Use the tailnet IP or `127.0.0.1`.
- **Never force-push, reset the target project, or create tickets that modify production.** Ask before
  moving a ticket to `ready` in a repository with automation enabled.
- **Stop on a failed gate.** Report the command, the exit code and the first actionable error. Do not
  paper over a failure by inventing a profile, skipping a test or disabling verification.

## Inputs to collect from the human

Fill this table before changing anything. The values in `CHANGE_ME` are not defaults.

| Variable | Meaning | How to obtain it |
|---|---|---|
| `REPO` | absolute path for this checkout | human, or `git clone` destination |
| `PROJECT` | absolute path of the code the agent will change | human |
| `T3_BIN` | absolute path to `t3` | `command -v t3` |
| `T3_DB` | T3's `statev2.sqlite` | `${T3CODE_HOME:-$HOME/.t3}/userdata/statev2.sqlite` |
| `BOARD_HOST` | tailnet IP, or `127.0.0.1` | `tailscale ip -4` or human |
| `BOARD_PORT` | board port | default `8787` |
| `PROVIDER` / `MODEL` | T3 harness instance and model slug | `GET /api/catalog`; human chooses |
| `TELEGRAM` | optional bot token and chat id | BotFather and `/id`; never report the token |
| `BRAIN_VAULT` / `BRAIN_BIN` | optional vault and CLI | human; or `t3-brain` checkout |
| `RELEASE_ENABLED` | whether ticket branches may be pushed | human; default `false` |

## Phase 0 — inspect the machine

Run these read-only checks and record the output:

```bash
python3 --version
command -v git && git --version
command -v t3 && t3 --version
t3 status || true
test -f "${T3CODE_HOME:-$HOME/.t3}/userdata/statev2.sqlite" && echo "T3 DB: OK"
```

Stop if Python is older than 3.12, `git` is missing, or the T3 database is absent. If `t3` is missing,
show the official installer command from [installation.md](installation.md#1-install-and-start-t3-code)
and ask for permission.

## Phase 1 — checkout and baseline

```bash
git clone https://github.com/momokii/t3-workflow.git "$REPO"
cd "$REPO"
make check
```

**PASS:** `make check` exits `0`. Do not continue on a red suite.

## Phase 2 — workflow configuration and token

Create the config only if it does not already exist; an upgrade must not overwrite the human's model
presets:

```bash
install -d -m 700 "$HOME/.config/t3-workflow"
test -f "$HOME/.config/t3-workflow/config.json" || \
  cp "$REPO/config.example.json" "$HOME/.config/t3-workflow/config.json"
chmod 600 "$HOME/.config/t3-workflow/config.json"
```

Edit these values, preserving the rest of the JSON:

- `t3.cli` → `$T3_BIN`
- `t3.dbPath` → `$T3_DB`
- optional `models.<name>.provider` / `.model` → the human's chosen pair
- optional `defaults.profile` → that preset name
- `release.enabled` → `false` unless the human explicitly enabled ticket-branch pushes

Validate before starting anything:

```bash
python3 -m json.tool "$HOME/.config/t3-workflow/config.json" >/dev/null
```

Create the token once. Do not rotate an existing token without telling the human:

```bash
if ! grep -q '^T3_BOARD_TOKEN=' "$HOME/.config/t3-workflow/board.env" 2>/dev/null; then
  (umask 077; printf 'T3_BOARD_TOKEN=%s\n' "$(python3 -c "import secrets; print(secrets.token_hex(16))")" \
    >> "$HOME/.config/t3-workflow/board.env")
fi
chmod 600 "$HOME/.config/t3-workflow/board.env"
```

Initialize the store:

```bash
python3 tickets/cli.py init
python3 tickets/cli.py list
```

## Phase 3 — prove the board and T3 seam

Start a temporary loopback board and wait for health:

```bash
set -a
. "$HOME/.config/t3-workflow/board.env"
set +a
python3 board/server.py --host 127.0.0.1 --port 8787 --token "$T3_BOARD_TOKEN" &
BOARD_PID=$!
for _ in 1 2 3 4 5; do
  curl -fsS http://127.0.0.1:8787/healthz && break
  sleep 1
done
curl -fsS http://127.0.0.1:8787/api/catalog | python3 -m json.tool | head -80
curl -fsS http://127.0.0.1:8787/api/tickets | python3 -m json.tool | head -40
```

**PASS:**

- `/healthz` returns `"status": "ok"`.
- `/api/catalog` has at least one provider with `enabled: true`, `installed: true` and a model.
- `/api/tickets` returns JSON with `tickets`, `profiles` and `summary`.

If the catalogue is empty, stop and fix T3/provider sign-in. A board without a model can show the
queue, but the watcher cannot start work.

Check the token boundary:

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST http://127.0.0.1:8787/api/watcher/run
```

**PASS:** `401`. With the token, the same request may run a watcher step; do not use it as a health
check for an empty queue.

Register the project if it is not already in T3:

```bash
t3 project add "$PROJECT" --title "my-project"
```

The board can also do this through **Register project**. The project path must be absolute and must be
a git repository for the release stage.

Stop the temporary board:

```bash
kill "$BOARD_PID" 2>/dev/null || true
```

## Phase 4 — install the services

Choose the address. If the human wants tailnet access:

```bash
BOARD_HOST="$(tailscale ip -4 | head -1)"
```

Otherwise use `127.0.0.1` and an SSH tunnel. Never use `0.0.0.0`.

```bash
cd "$REPO"
deploy/install.sh --print
deploy/install.sh --host "$BOARD_HOST" --port "$BOARD_PORT"
systemctl --user daemon-reload
systemctl --user enable --now t3-board t3-watcher t3-backup.timer
sudo loginctl enable-linger "$USER"        # ask before using sudo
```

**PASS:**

```bash
systemctl --user is-active t3-board t3-watcher
curl -fsS "http://$BOARD_HOST:$BOARD_PORT/healthz"
```

Do not enable `t3-bridge` until Phase 5 is configured.

## Phase 5 — optional Telegram

Only perform this phase if the human supplied a bot token and allowlist. Do not report the token.

```bash
install -d -m 700 "$HOME/.config/t3-bridge"
test -f "$HOME/.config/t3-bridge/config.json" || \
  cp "$REPO/bridge/config.example.json" "$HOME/.config/t3-bridge/config.json"
chmod 600 "$HOME/.config/t3-bridge/config.json"
python3 "$REPO/bridge/t3_bridge.py" --selftest
```

Edit `telegram.botToken` and `telegram.allowChatIds`; ask the human for their chat id or use `/id`
before adding it. Then:

```bash
systemctl --user enable --now t3-bridge
journalctl --user -u t3-bridge -n 20 --no-pager
```

**PASS:** the log shows the bot identity or polling start, with no `401`/unknown chat for the
allowlisted id.

## Phase 6 — optional Second Brain

Skip unless the human asked for the vault view. The reference vault is
[`t3-brain`](https://github.com/momokii/t3-brain); any CLI implementing the contract in
[second-brain.md](second-brain.md) works.

```bash
git clone https://github.com/momokii/t3-brain.git "$HOME/brain"
cd "$HOME/brain"
make check
make sync
if [ -d "$HOME/brain" ]; then test -x "$HOME/brain/bin/brain"; fi
```

Add to `board.env` only if the paths are not the defaults:

```bash
printf 'BRAIN_VAULT=%s\nBRAIN_BIN=%s\n' \
  "$HOME/brain" "$HOME/brain/bin/brain" >> "$HOME/.config/t3-workflow/board.env"
```

Restart the board and verify with the token:

```bash
systemctl --user restart t3-board
set -a; . "$HOME/.config/t3-workflow/board.env"; set +a
curl -s -o /dev/null -w 'no token: %{http_code}\n' \
  "http://$BOARD_HOST:$BOARD_PORT/api/brain/notes"
curl -fsS -H "X-Board-Token: $T3_BOARD_TOKEN" \
  "http://$BOARD_HOST:$BOARD_PORT/api/brain/notes" | python3 -c \
  'import json,sys; print(len(json.load(sys.stdin)), "notes")'
git -C "$HOME/brain" status --porcelain
```

**PASS:** no token → `401`; with token → `200` and the same count as
`BRAIN_VAULT="$HOME/brain" brain list --json`; the vault's `git status` has no changes caused by the
board.

If the endpoint returns `503`, check that the CLI is executable by the service user and that
`BRAIN_VAULT` is readable. Do not create a second parser or copy note bodies into the workflow
database.

## Phase 7 — final gate

Run the complete checklist and record the exact output:

```bash
cd "$REPO"
make check
curl -fsS "http://$BOARD_HOST:$BOARD_PORT/healthz"
curl -fsS "http://$BOARD_HOST:$BOARD_PORT/api/tickets" >/dev/null && echo "tickets: OK"
curl -s -o /dev/null -w 'write without token: %{http_code}\n' \
  -X POST "http://$BOARD_HOST:$BOARD_PORT/api/watcher/run"
systemctl --user is-active t3-board t3-watcher
systemctl --user is-active t3-bridge 2>/dev/null || true
git -C "$PROJECT" status --short
```

Expected: `make check` exit `0`; health `ok`; tickets `OK`; write without token `401`; the required
services `active`; the target project clean before any ticket is released (or changes that the human
expects).

## Completion report template

Report these facts, without secrets:

```
Installed: t3-workflow <commit>
T3: <version>, database present, <provider>/<model> enabled
Board: http://<host>:<port> (tailnet|loopback), token configured, /healthz ok
Services: t3-board, t3-watcher, t3-backup.timer (bridge optional)
Tests: make check exit 0
Project: <title> at <absolute path>, git remote <yes|no>
Second Brain: <disabled|enabled, N notes, CLI path, 401/200 verified>
Telegram: <disabled|enabled, allowlist configured>
Open issues: <none|exact gate and error>
```

Do not say "ready" if a required gate failed. State the failing command and the next action.
