# Deploying with Docker

The board and both workers ship as one image, driven by `docker-compose.yml`. Everything was built
and exercised on the machine this repository was written on; the commands below are the ones that ran.

```
docker compose up -d --build board                       # board only
docker compose --profile automation up -d --build        # board + bridge + watcher
```

## What runs

| Service | Profile | Container name | Command |
|---|---|---|---|
| `board` | default | `t3-board` | `board/server.py --host $T3_BOARD_HOST --port $T3_BOARD_PORT` |
| `bridge` | `automation` | `t3-bridge` | `bridge/t3_bridge.py` |
| `watcher` | `automation` | `t3-watcher` | `watcher.py --loop` |

One image (`t3-workflow:local`) serves all three; only the command differs. The image is
`python:3.12-slim` plus `git`, `openssh-client`, `ca-certificates` and `libatomic1` — the last one is
required by the T3 CLI binary, which is a Node single-executable that links against it.

## Why `network_mode: host` and identical paths

Two constraints come from T3 itself:

1. **The T3 CLI talks to the server on loopback.** It discovers the running instance through
   `$HOME/.t3`, and in a bridge network a container's `127.0.0.1` is not the host. Host networking
   removes the problem without publishing ports.
2. **T3 identifies a project by its real absolute path.** A repository mounted at `/repo` is unknown
   to the server; it must be mounted at the same path it has on the host. The compose file therefore
   mounts `${PROJECTS_ROOT}` onto itself, and `HOME` inside the container is the host `HOME`.

This makes the containers *host-integrated* rather than isolated: they share the host network and the
host user's data directories. That is a deliberate trade for a single-user workstation, and it is why
the compose file mounts the directories explicitly instead of the whole home directory.

| Mount | Mode | Why |
|---|---|---|
| `${HOME}/.t3` | read-write | the CLI's runtime and its `statev2.sqlite`; the CLI writes caches here |
| `${HOME}/.local/bin` | read-only | the `t3` symlink the configuration points at |
| `${HOME}/.local/state/t3-workflow` | read-write | tickets, reports, backups |
| `${HOME}/.config/t3-workflow` | read-only | profiles, limits, release policy, token file |
| `${HOME}/.config/t3-wa-notify` | read-only | Telegram credentials shared with `alert.py` |
| `${PROJECTS_ROOT}` | read-write | the git checkouts the watcher tests and releases |
| `${HOME}/.ssh` | read-only | credentials for `git push` in the release stage |

Files keep their owner because compose runs the container with `T3_UID`/`T3_GID` (default `1000`),
matching the host user.

## Prerequisites

- Docker with Compose v2 (`docker compose version`).
- Linux host. `network_mode: host` behaves differently on Docker Desktop; the board can still run
  there, but the CLI-based services cannot reach the host T3 server.
- T3 Code installed on the host for the `automation` profile, with `t3` on `~/.local/bin`.
- A board token in `~/.config/t3-workflow/board.env` (`T3_BOARD_TOKEN=<hex>`), mode 600.

## Setup

```bash
cp deploy/docker/.env.example .env     # then edit HOME, PROJECTS_ROOT, T3_BOARD_HOST
$EDITOR .env

docker compose config                  # validates the render
docker compose build board
docker compose up -d board
docker compose ps
```

`T3_BOARD_HOST` is the bind address inside the host network. `127.0.0.1` keeps the board local; use
the Tailscale address (`tailscale ip -4`) to reach it from other devices.

## Verify

```bash
docker compose ps                                  # board should be Up (healthy)
curl -s http://127.0.0.1:8787/healthz              # {"status":"ok",...}
T3_BOARD_HOST=127.0.0.1 deploy/docker/smoke.sh     # every read endpoint + token enforcement
```

`deploy/docker/smoke.sh` checks `/healthz`, `/api/tickets`, `/api/projects`, `/api/threads`,
`/static/logo.svg` and — when `T3_BOARD_TOKEN` is exported — that a `POST` without the token is `401`.

The workers are verified with one-shot runs, which never touch a real thread:

```bash
docker compose run --rm --no-deps watcher python3 watcher.py --once --dry-run
docker compose run --rm --no-deps bridge python3 bridge/t3_bridge.py --selftest
```

## Operations

```bash
docker compose logs -f board
docker compose restart board
docker compose --profile automation logs -f watcher

# after a code change
git pull
docker compose build
docker compose up -d
```

Stop everything without deleting state: `docker compose --profile automation down`. State lives in
the mounted directories, not in volumes, so a rebuilt image keeps the same tickets and reports.

## Switching between systemd and compose

The systemd units and the compose services do the same thing, so run **one** of them:

```bash
systemctl --user disable --now t3-bridge t3-watcher t3-board   # before compose up
# later, going back:
docker compose --profile automation down
systemctl --user enable --now t3-bridge t3-watcher t3-board
```

Running both would be two pollers on one Telegram bot (`getUpdates` returns a conflict) and two
watchers competing for tickets — the claim protects the data, but the noise is pointless.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `No T3 Code project matches '/app'` | the project is mounted at a different path than on the host | mount `${PROJECTS_ROOT}` at the same absolute path |
| `error while loading shared libraries: libatomic.so.1` | image built without `libatomic1` | rebuild from the shipped Dockerfile |
| `permission denied` writing tickets | `T3_UID`/`T3_GID` do not match the owner of `~/.local/state/t3-workflow` | set them to `id -u` / `id -g` |
| board container `unhealthy` | it binds `T3_BOARD_HOST`, while the healthcheck probes the same value | keep them consistent; `127.0.0.1` works for a local bind |
| Telegram conflict | host bridge still running alongside the container | stop one of them |
| `port is already allocated` | the host systemd board still owns the port | stop it first, or change `T3_BOARD_PORT` |

## Security notes

- No secret is baked into the image. The token arrives through `env_file`, Telegram credentials are
  mounted read-only from the host configuration.
- The container runs as the host user; it is not a privilege boundary against that user.
- Host networking exposes whatever the process binds. Binding `127.0.0.1` keeps the board local;
  binding a Tailscale address keeps it on the tailnet.
- `.dockerignore` excludes `.git`, tests, SQLite files, logs and `.env`, so an accidental
  `docker build` cannot leak state into an image layer.
