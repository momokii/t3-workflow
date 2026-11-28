# Installation

This is the public, end-to-end setup guide for a fresh Linux machine. **T3 Code is required.** This
page covers the T3 Code installation, the optional Telegram bridge and the optional Second Brain view.

For an agent that will perform the setup, read [`agent-setup.md`](agent-setup.md) after this page. The
agent guide turns these steps into checkpoints, stop conditions and a completion report.

## What you are installing

`t3-workflow` is an **extension of T3 Code**, not a replacement for it. T3 owns projects, threads,
agents and live runs. This repository owns the ticket queue, the test/review gates, the release step,
the board and the reports.

| Component | Required? | What it needs |
|---|---|---|
| T3 Code server + `t3` CLI | **Yes** for the ticket loop | a running T3 server, at least one installed and signed-in agent, a project registered with an absolute path |
| This repository (board, watcher, bridge) | Yes | Linux, Python 3.12, `git`, a target git repository |
| Tailscale (or loopback + SSH tunnel) | Yes for remote access | the board must not bind `0.0.0.0` |
| Telegram bot | Optional | BotFather token and one allowlisted chat id |
| Second Brain vault + brain CLI | Optional | a Markdown vault, the `brain` CLI, read access from the board user |
| Docker | Optional | Docker with Compose v2; see [docker.md](docker.md) |

Tested with T3 Code `0.1.9` and Python `3.12`. The board reads T3's `statev2.sqlite`, so a T3 version
that changes that schema can require a code update; run `make check` after upgrading T3.

## 1. Install and start T3 Code

Follow the official source for your platform: <https://github.com/pingdotgg/t3code#install>. The
command-line install on Linux is:

```bash
curl -fsSL https://t3.codes/install.sh | sh
t3 --version
```

Start the server. Either a background service with remote access, or a foreground server on a headless
machine:

```bash
t3 up          # background service, access settings and optional remote access
# or
t3 serve       # foreground server, no browser
```

Install and sign in to at least one agent in T3 Code. T3 can drive Claude Code, Codex, Cursor,
OpenCode, Pi, Grok, and other ACP agents; the official provider guide is
<https://github.com/pingdotgg/t3code/blob/main/docs/user/install.md#providers>. The exact provider does
not matter to `t3-workflow`; what matters is that T3 reports at least one provider as installed,
enabled and authenticated.

Check the server and the data directory:

```bash
t3 status
test -f "$HOME/.t3/userdata/statev2.sqlite" && echo "T3 database present"
```

If you use `T3CODE_HOME` or `--base-dir`, the database is under that directory instead:

```bash
test -f "${T3CODE_HOME:-$HOME/.t3}/userdata/statev2.sqlite" && echo "T3 database present"
```

Register the repository you want the agent to work on. T3 identifies a project by its absolute path, so
this path must not be a relative path or a symlink:

```bash
PROJECT=/absolute/path/to/your/project
t3 project add "$PROJECT" --title "my-project"
```

The board can also register a project later with **Register project**.

## 2. Get the workflow repository

```bash
git clone https://github.com/momokii/t3-workflow.git
cd t3-workflow
make check
```

`make check` is the gate. It compiles every Python module and runs both test suites; it must exit `0`
before you configure services. No pip install, Node build or CDN is needed.

## 3. Create the workflow configuration

The repository ships a safe template at [`config.example.json`](../config.example.json). Copy it and
replace the two `CHANGE_ME` paths:

```bash
install -d -m 700 "$HOME/.config/t3-workflow"
cp -n config.example.json "$HOME/.config/t3-workflow/config.json"
chmod 600 "$HOME/.config/t3-workflow/config.json"
```

Edit `t3.cli` and `t3.dbPath`:

```json
"t3": {
  "cli": "/home/you/.local/bin/t3",
  "dbPath": "/home/you/.t3/userdata/statev2.sqlite",
  "timeoutSeconds": 180
}
```

`t3.cli` must be executable by the service user. `t3.dbPath` must be the database that belongs to the
running T3 server. The rest of the template is safe to start with:

- `release.enabled` is `false` in the template. Set it to `true` only after the target project has a
  git remote and you want automatic ticket-branch pushes.
- `models` is empty. The board can create a ticket by choosing an enabled harness and model from T3's
  live catalogue. If you want a CLI preset or a default for `tickets/cli.py`, add one:

```json
"models": {
  "agent": {
    "provider": "your-provider-instance-id",
    "model": "your-model-slug"
  }
},
"defaults": { "profile": "agent" }
```

Do not invent these values. After the board is running, `GET /api/catalog` shows the
`providerInstanceId` and model `id` values for the providers T3 reports as enabled and installed. The
ticket dialog uses the same catalogue. Project defaults can go under `projects.<title>`; see
[usage.md](usage.md).

The file must remain mode `600`; it contains provider/model configuration but no Telegram token.

## 4. Create the board token and the database

The board uses one token for every mutating request, for `/api/fs` and for `/api/brain/*`:

```bash
install -d -m 700 "$HOME/.config/t3-workflow"
if ! grep -q '^T3_BOARD_TOKEN=' "$HOME/.config/t3-workflow/board.env" 2>/dev/null; then
  (umask 077; printf 'T3_BOARD_TOKEN=%s\n' "$(python3 -c "import secrets; print(secrets.token_hex(16))")" \
    >> "$HOME/.config/t3-workflow/board.env")
fi
chmod 600 "$HOME/.config/t3-workflow/board.env"
```

Do not overwrite an existing `board.env` on an upgrade: it would invalidate every open page and script.

Initialize the SQLite database:

```bash
python3 tickets/cli.py init
python3 tickets/cli.py list
```

## 5. Start the board by hand first

Before installing services, prove the T3/board seam on loopback:

```bash
set -a
. "$HOME/.config/t3-workflow/board.env"
set +a

python3 board/server.py --host 127.0.0.1 --port 8787 --token "$T3_BOARD_TOKEN" &
sleep 1
curl -fsS http://127.0.0.1:8787/healthz
curl -fsS http://127.0.0.1:8787/api/catalog | python3 -m json.tool | head
```

`/healthz` must answer `{"status": "ok", ...}`. `/api/catalog` must list an enabled provider with at
least one model. If it does not, fix T3 and its provider login first; the workflow cannot start a
thread without that model.

Open `http://127.0.0.1:8787/` in a browser, choose **＋ Ticket**, fill the project from T3, choose the
harness/model, set a test command (for example `make check`) and move the ticket to `ready`. Then run
one dry-run step before enabling the watcher service:

```bash
T3_BOARD_TOKEN="$T3_BOARD_TOKEN" python3 watcher.py --once --dry-run
```

Stop the temporary board when you are satisfied:

```bash
kill %1
```

The full ticket lifecycle, test-command rules and review gate are in [usage.md](usage.md). The board
is a queue, not a second copy of T3: one ticket, one thread, and the release step pushes only a ticket
branch.

## 6. Choose the board address

The board is meant for a private tailnet. Do **not** bind `0.0.0.0`: most `GET` routes are visible to
anyone who can reach the port, and the token protects writes, filesystem listing and the brain routes.

**With Tailscale (recommended):**

```bash
tailscale ip -4 | head -1
BOARD_HOST="$(tailscale ip -4 | head -1)"
```

The unit binds that address, so Tailscale must be up before the board starts. If the address changes,
rerender the units with the new value.

**Without Tailscale:** keep `BOARD_HOST=127.0.0.1` and reach it through an SSH tunnel:

```bash
ssh -L 8787:127.0.0.1:8787 you@the-machine
```

Then open `http://127.0.0.1:8787/` on your laptop.

## 7. Install the systemd user units

```bash
deploy/install.sh --print                                  # inspect first
deploy/install.sh --host "$BOARD_HOST" --port 8787
systemctl --user daemon-reload
systemctl --user enable --now t3-board t3-watcher t3-backup.timer
```

The units read the token and optional brain settings from
`~/.config/t3-workflow/board.env`. `deploy/install.sh` writes files only; it does not enable anything.

Allow the user services to start after logout or boot:

```bash
sudo loginctl enable-linger "$USER"
```

Verify:

```bash
systemctl --user is-active t3-board t3-watcher
systemctl --user status t3-board --no-pager
curl -fsS "http://$BOARD_HOST:8787/healthz"
```

Restart the board after changing `board.env`, and the watcher after changing `config.json`. Static
files under `board/static/` are re-read without a restart.

The operations runbook covers paused automation, token rotation, backups, restore and incidents:
[operations.md](operations.md).

## 8. Optional: Telegram bridge

The bridge is separate from the board and is disabled until you configure it.

1. Create a bot with [BotFather](https://core.telegram.org/bots#botfather).
2. Put the token and an allowlist of chat ids in a bridge config:

```bash
install -d -m 700 "$HOME/.config/t3-bridge"
cp -n bridge/config.example.json "$HOME/.config/t3-bridge/config.json"
chmod 600 "$HOME/.config/t3-bridge/config.json"
```

Edit `telegram.botToken` and `telegram.allowChatIds`. If you do not know your chat id, start the bridge
once, send `/id` to the bot and add the number it returns; unknown chats are rejected and logged.

```bash
python3 bridge/t3_bridge.py --selftest
systemctl --user enable --now t3-bridge
journalctl --user -u t3-bridge -n 20
```

Telegram is untrusted input: keep the bot token private, keep the allowlist short, and leave
`bridge.allowWrites` off until you have tested read commands. The bridge never answers a T3 approval
request by itself.

## 9. Optional: Second Brain

The vault view is read-only and optional. It needs a Markdown vault, the `brain` CLI and read access
from the board user. The reference implementation is
[`t3-brain`](https://github.com/momokii/t3-brain); any CLI that implements the same `list`, `graph` and
`get` JSON contract works.

```bash
git clone https://github.com/momokii/t3-brain.git "$HOME/brain"
cd "$HOME/brain"
make check
make sync
```

Add the paths to `board.env`, then restart the board:

```bash
cat >> "$HOME/.config/t3-workflow/board.env" <<'EOF'
BRAIN_VAULT=/home/you/brain
BRAIN_BIN=/home/you/brain/bin/brain
EOF
systemctl --user restart t3-board
```

The full vault structure, frontmatter, CLI contract, verification and troubleshooting are in
[second-brain.md](second-brain.md#setup). The board never writes to `~/brain`; every brain route needs
the board token.

## 10. Optional: Docker

The compose file runs the board and, under the `automation` profile, the bridge and watcher. It uses
host networking because T3 identifies projects by their real path and the CLI talks to the T3 server on
loopback. Read [docker.md](docker.md) before using it; do not run the compose watcher and the systemd
watcher at the same time.

## Verification checklist

Run these on the installed machine. A setup is not complete until every applicable line passes:

```bash
make check
curl -fsS "http://$BOARD_HOST:8787/healthz"
curl -fsS "http://$BOARD_HOST:8787/api/tickets" | python3 -m json.tool | head
curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST "http://$BOARD_HOST:8787/api/watcher/run"          # 401 without the token
systemctl --user is-active t3-board t3-watcher
if [ -d "$HOME/brain" ]; then test -x "$HOME/brain/bin/brain"; fi   # optional
```

Expected results:

- `make check` exits `0`.
- `/healthz` contains `"status": "ok"`.
- `/api/tickets` returns JSON with `tickets`, `profiles`, `summary` and `watcher`.
- A `POST` without `X-Board-Token` returns `401`.
- `t3-board` and `t3-watcher` are `active`.
- If Second Brain is enabled, `GET /api/brain/notes` returns the same count as
  `brain list --json`, and `git -C "$HOME/brain" status --porcelain` is empty with respect to board
  activity.

## Troubleshooting

| Symptom | Check |
|---|---|
| `t3: command not found` | official installer; `command -v t3`; put `~/.local/bin` on `PATH` |
| board starts but `/api/catalog` is empty | T3 server not running, no agent signed in, or cache not written; `t3 status`, T3 provider setup |
| `config not found` | `~/.config/t3-workflow/config.json`; or `T3_WORKFLOW_CONFIG` |
| ticket stays `ready` | watcher not running, no free slot, approval pending, or no usable profile/model |
| thread start fails | T3 server reachable and project path exists; `t3 project add` used the same absolute path |
| release blocked | no `origin` remote, `release.enabled=false`, or no `[PASS]` proof |
| Second Brain 503 | `BRAIN_BIN` executable by the service user, `BRAIN_VAULT` readable, CLI supports `list/graph/get --json` |
| board unreachable from another device | `BOARD_HOST` is the tailnet IP (not `127.0.0.1`), Tailscale is up, `ss -ltn` shows the bind |
| `401` after a token rotation | reload the page; update scripts that still hold the old token |

For service-level incidents, backup, restore and failover, use the measured procedures in
[operations.md](operations.md).
