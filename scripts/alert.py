#!/usr/bin/env python3
"""Send one Telegram alert when a t3-* unit fails.

Invoked by systemd: OnFailure=t3-alert@%n.service, which runs
`alert.py %n`. Reads the bot token and chat id from the notifier config so there
is a single place that holds the credential.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

CONFIG = Path.home() / ".config/t3-wa-notify/config.json"


def main() -> int:
    unit = sys.argv[1] if len(sys.argv) > 1 else "unknown unit"
    if not CONFIG.exists():
        print("notifier config is missing", file=sys.stderr)
        return 1
    telegram = json.loads(CONFIG.read_text(encoding="utf-8")).get("telegram") or {}
    token = telegram.get("botToken")
    chat = telegram.get("chatId")
    if not token or not chat:
        print("Telegram token/chat is not set", file=sys.stderr)
        return 1
    text = f"⚠️ {unit} failed. Check: journalctl --user -u {unit} -n 50"
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat, "text": text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return 0 if response.status == 200 else 1
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"send failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
