#!/bin/sh
# Render the systemd user units for this checkout.
#
#   deploy/install.sh                 write them to ~/.config/systemd/user
#   deploy/install.sh --print         print them instead
#   deploy/install.sh --host IP --port 8787 --python /usr/bin/python3
#
# Only the units are written; nothing is enabled or started for you.
set -eu

REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
HOME_DIR=${HOME:-$(getent passwd "$(id -u)" | cut -d: -f6)}
TARGET="$HOME_DIR/.config/systemd/user"
PRINT=0
HOST=127.0.0.1
PORT=8787
PYTHON=/usr/bin/python3

while [ $# -gt 0 ]; do
  case "$1" in
    --print) PRINT=1 ;;
    --host) HOST=$2; shift ;;
    --port) PORT=$2; shift ;;
    --python) PYTHON=$2; shift ;;
    --target) TARGET=$2; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

render() {
  sed -e "s|@REPO@|$REPO|g" -e "s|@HOME@|$HOME_DIR|g" -e "s|@PYTHON@|$PYTHON|g" \
      -e "s|@HOST@|$HOST|g" -e "s|@PORT@|$PORT|g" "$1"
}

[ "$PRINT" -eq 1 ] || mkdir -p "$TARGET"
for template in "$REPO"/deploy/*.service.in; do
  name=$(basename "$template" .in)
  if [ "$PRINT" -eq 1 ]; then
    printf '# %s\n' "$name"
    render "$template"
  else
    render "$template" > "$TARGET/$name"
    echo "wrote $TARGET/$name"
  fi
done

name=t3-backup.timer
if [ "$PRINT" -eq 1 ]; then
  printf '# %s\n' "$name"
  render "$REPO/deploy/$name"
else
  render "$REPO/deploy/$name" > "$TARGET/$name"
  echo "wrote $TARGET/$name"
fi

[ "$PRINT" -eq 1 ] || echo "next: systemctl --user daemon-reload && systemctl --user enable --now t3-bridge t3-watcher t3-board t3-backup.timer"
