"""Telegram / push notification for Task Dispatcher events.

Phase 2 P2 — the bridge pushes a brief alert to Telegram when a task
fails or times out. Implementation notes:

* Pure stdlib (urllib) for the HTTP call to api.telegram.org so we
  don't add a new dep just for notifications.
* Rate-limited to `rate_limit_per_hour` per process (sliding window
  in-memory; survives until bridge restart).
* Reads credentials from env vars named in config/notify.json. If the
  env var is empty, the notifier is a no-op.
* Falls back to writing the alert to logs/notifier.log so the user
  can see what *would* have been sent even if Telegram is disabled.

This is the second P2 item; the first (reaper) calls into this via
`notify_timeout(task_id)`. We also expose `notify_failed(task_id)`
and `notify_completed(task_id)` for completeness.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Deque, Dict, Optional

log = logging.getLogger("dispatcher.notifier")

# ---------------------------------------------------------------------------
# Rate limiting (sliding window, in-memory; reset on bridge restart)
# ---------------------------------------------------------------------------
_SEND_HISTORY: Deque[float] = deque(maxlen=200)


def _within_rate_limit(rate_per_hour: int) -> bool:
    if rate_per_hour <= 0:
        return True
    now = time.time()
    one_hour_ago = now - 3600
    while _SEND_HISTORY and _SEND_HISTORY[0] < one_hour_ago:
        _SEND_HISTORY.popleft()
    return len(_SEND_HISTORY) < rate_per_hour


def _append_local_log(line: str) -> None:
    try:
        from dispatcher.manager import _BRIDGE_ROOT
        log_dir = _BRIDGE_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "notifier.log"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def _telegram_config() -> dict:
    from config import load
    cfg = load("notify").get("telegram", {})
    token = ""
    chat_id = ""
    if cfg.get("bot_token_env"):
        token = os.getenv(cfg["bot_token_env"], "").strip()
    if cfg.get("chat_id_env"):
        chat_id = os.getenv(cfg["chat_id_env"], "").strip()
    return {
        "enabled": bool(cfg.get("enabled", False)) and bool(token) and bool(chat_id),
        "bot_token": token,
        "chat_id": chat_id,
        "notify_on": set(cfg.get("notify_on", ["failed", "timeout"])),
        "rate_limit_per_hour": int(cfg.get("rate_limit_per_hour", 20)),
    }


# ---------------------------------------------------------------------------
# Send helpers
# ---------------------------------------------------------------------------


def _send_telegram(bot_token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not data.get("ok"):
            log.warning("telegram send: not ok: %s", data)
            return False
        return True
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        log.warning("telegram send failed: %s: %s", type(exc).__name__, exc)
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram send unexpected: %s: %s", type(exc).__name__, exc)
        return False


def _format_alert(task_id: str, status: str) -> Optional[str]:
    """Return an HTML-formatted Telegram message, or None if we
    can't find the task."""
    from dispatcher.manager import TaskManager
    m = TaskManager()
    t = m.get(task_id)
    if t is None:
        return None
    out = m.get_output(task_id) or {}
    # Task summary
    sym = {
        "failed": "❌",
        "timeout": "⏱",
        "completed": "✅",
        "cancelled": "🚫",
    }.get(status, "ℹ️")
    title = (t.title or "")[:80]
    err = (t.error_message or "")[:200]
    duration = t.duration_sec
    duration_s = f"{duration:.1f}s" if duration is not None else "?"
    hermes_run_id = t.hermes_run_id or "—"
    body = (
        f"{sym} <b>Task {status}</b>\n"
        f"<code>{task_id}</code>\n"
        f"Title: {title}\n"
        f"Type: <code>{t.type}</code> · Status: <code>{t.status}</code>\n"
        f"Duration: {duration_s}\n"
        f"Hermes run: <code>{hermes_run_id}</code>\n"
    )
    if err:
        body += f"Error: {err}\n"
    body += f"\n/logs: <code>/tasks/{task_id}/logs</code>\n/result: <code>/tasks/{task_id}/result</code>"
    return body


def _dispatch_status(task_id: str, status: str) -> bool:
    """Send a Telegram alert for `status` if configured to do so. Returns
    True iff the message was actually sent (or queued to local log)."""
    cfg = _telegram_config()
    if status not in cfg["notify_on"]:
        return False
    text = _format_alert(task_id, status)
    if text is None:
        return False
    if not _within_rate_limit(cfg["rate_limit_per_hour"]):
        _append_local_log(
            json.dumps({"ts": datetime.utcnow().isoformat() + "Z", "event": "rate_limited",
                        "task_id": task_id, "status": status})
        )
        return False
    # Always log locally first (audit trail).
    _append_local_log(
        json.dumps({"ts": datetime.utcnow().isoformat() + "Z", "event": "alert",
                    "task_id": task_id, "status": status, "text_len": len(text)})
    )
    if not cfg["enabled"]:
        return False
    sent = _send_telegram(cfg["bot_token"], cfg["chat_id"], text)
    _SEND_HISTORY.append(time.time())
    return sent


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def notify_failed(task_id: str) -> bool:
    return _dispatch_status(task_id, "failed")


def notify_timeout(task_id: str) -> bool:
    return _dispatch_status(task_id, "timeout")


def notify_completed(task_id: str) -> bool:
    return _dispatch_status(task_id, "completed")


def notify_cancelled(task_id: str) -> bool:
    return _dispatch_status(task_id, "cancelled")
