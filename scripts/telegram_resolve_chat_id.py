#!/usr/bin/env python3
"""Resolve the Telegram chat id for the A2 bot and print it.

Run this AFTER the operator has sent at least one message to
``@dongxinmeow_a2_bot``. The script polls ``getUpdates`` once and
prints the first chat id it finds. If no messages are present it
prints a clear instruction instead of failing noisily.

Usage::

    TELEGRAM_BOT_TOKEN=<bot token> python3 scripts/telegram_resolve_chat_id.py

The bot token MUST be provided via the ``TELEGRAM_BOT_TOKEN`` env var;
there is no hardcoded fallback (a previously embedded default was
compromised and removed — rotate it if still in use).
No side effects beyond a single HTTPS GET to ``api.telegram.org``.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error


def main() -> int:
    # The bot token is a secret and must be supplied via the
    # ``TELEGRAM_BOT_TOKEN`` environment variable. A previous revision
    # shipped a hardcoded ``DEFAULT_TOKEN`` literal here; that value is
    # treated as compromised and must be rotated by the operator. The
    # script deliberately has NO built-in fallback so a missing env var
    # fails loudly instead of silently using an exposed credential.
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("error: TELEGRAM_BOT_TOKEN env var is not set", file=sys.stderr)
        return 2
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        print(f"error: getUpdates failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if not data.get("ok"):
        print(f"error: getUpdates returned not-ok: {data}", file=sys.stderr)
        return 1

    updates = data.get("result") or []
    if not updates:
        print(
            "no messages found.\n"
            "→ Open Telegram, search for @dongxinmeow_a2_bot, send any "
            "message (e.g. 'ping'), then re-run this script.",
            file=sys.stderr,
        )
        return 3

    # Pick the most recent message and print its chat id.
    last = updates[-1]
    msg = last.get("message") or last.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    sender = (
        (chat.get("username") and f"@{chat.get('username')}")
        or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
        or "?"
    )
    if chat_id is None:
        print(f"error: last update has no chat.id: {json.dumps(last)[:200]}", file=sys.stderr)
        return 1

    print(f"TELEGRAM_CHAT_ID={chat_id}    # {sender}  (type={chat_type})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
