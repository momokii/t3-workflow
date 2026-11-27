#!/bin/sh
# Smoke-test a running board container (or any board).
#
#   T3_BOARD_HOST=127.0.0.1 T3_BOARD_PORT=8787 deploy/docker/smoke.sh
#   T3_BOARD_TOKEN=… deploy/docker/smoke.sh      # also checks token enforcement
set -eu

HOST=${T3_BOARD_HOST:-127.0.0.1}
PORT=${T3_BOARD_PORT:-8787}
BASE="http://$HOST:$PORT"
fail=0

expect() {
  path=$1
  want=$2
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BASE$path" || echo 000)
  if [ "$code" = "$want" ]; then
    printf 'ok   %-16s %s\n' "$path" "$code"
  else
    printf 'FAIL %-16s %s (want %s)\n' "$path" "$code" "$want"
    fail=1
  fi
}

expect /healthz 200
expect /api/tickets 200
expect /api/projects 200
expect /api/threads 200
expect /static/logo.svg 200

if [ -n "${T3_BOARD_TOKEN:-}" ]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
    -X POST -H 'Content-Type: application/json' -d '{}' "$BASE/api/tickets" || echo 000)
  # no token header must be 401; with a token and an empty body it must be 409 (empty title)
  if [ "$code" = "401" ]; then printf 'ok   %-16s %s\n' "POST no token" "$code"
  else printf 'FAIL %-16s %s (want 401)\n' "POST no token" "$code"; fail=1; fi
fi

exit "$fail"
