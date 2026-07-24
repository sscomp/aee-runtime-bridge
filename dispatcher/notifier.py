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

AEE v3 Telegram Completion Enforcement Gate
--------------------------------------------
The v3 gate is the *working* path that ``TaskManager.complete()``
calls to actually deliver a Telegram alert when a task finishes.
The legacy ``notify_completed`` (above) is a silent fallback: it
early-returns ``False`` when ``config/notify.json`` has
``enabled=false`` or when ``completed`` is not in ``notify_on`` —
both of which were the case before v3 (Gap A + Gap B).

The v3 gate fixes both:

* The primary path (``notify_completed_hermes_gateway``) shells out
  to ``hermes send --to telegram:<chat_id> --subject ... --file ...
  --json``. This is independent of the ``enabled`` flag because it
  uses the Hermes CLI (a separate process with its own credentials
  resolution), not the in-process urllib path.
* ``config/notify.json`` is flipped to ``enabled=true`` and
  ``notify_on`` now includes ``completed`` so the legacy fallback
  (``notify_completed``) can fire too.

The gate's result dict (``sent`` / ``method`` / ``recipient`` /
``message_id`` / ``ts_utc`` / ``ts_taipei`` / ``attempts`` /
``last_error``) is persisted into ``task_outputs.notification_json``
by ``TaskManager.complete()`` and read back by
``compute_completion_state`` to derive the 4-stage completion
state. See ``dispatcher/notification_state.py`` for the stage model
and ``dispatcher/manager.py:TaskManager.complete`` for the wire-up.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Dict, Optional

log = logging.getLogger("dispatcher.notifier")

# AEE v3 Telegram Completion Enforcement Gate — version pin. Bumped
# when the gate's wire-up, return shape, or persistence contract
# changes. Recorded in ``config/notify.json`` under
# ``enforcement_gate.version`` for documentation / audit.
ENFORCEMENT_GATE_VERSION = "v3.0.0"

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


def _append_notification_audit(audit_record: Dict[str, Any]) -> None:
    """AEE v3 Telegram auditability — append a JSONL audit record for
    each notification attempt so the gate's outcome can be
    independently verified after the fact.

    The audit log is append-only JSONL at
    ``<bridge_root>/logs/notification_audit.jsonl``. Each line is a
    JSON object with at least:

    * ``task_id``      — the dispatcher task id.
    * ``sent``         — bool from the gate result.
    * ``method``       — ``hermes_send`` / ``notifier.notify_completed`` / ``failed``.
    * ``recipient``    — the chat id the alert was sent to.
    * ``message_id``   — int|None — the Telegram message id (the
                          canonical completion evidence under the v3
                          contract).
    * ``ts_utc``       — ISO-8601 UTC timestamp.
    * ``ts_taipei``    — ISO-8601 Asia/Taipei timestamp.
    * ``last_error``   — str|None — populated when ``sent`` is False.
    * ``attempts``     — int — number of paths tried (1 or 2).

    This function MUST NOT raise — it is on the gate's write path
    and any exception would break the notification flow. Errors are
    logged at WARNING level and swallowed.
    """
    try:
        from dispatcher.manager import _BRIDGE_ROOT
        log_dir = _BRIDGE_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        audit_path = log_dir / "notification_audit.jsonl"
        line = json.dumps(audit_record, default=str, ensure_ascii=False)
        with audit_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as exc:  # noqa: BLE001 — never raise from the audit path
        log.warning(
            "notifier._append_notification_audit: failed to write audit record: %s: %s",
            type(exc).__name__, exc,
        )


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


# ---------------------------------------------------------------------------
# AEE v3 Telegram Completion Enforcement Gate
# ---------------------------------------------------------------------------
#
# The two functions below are the *working* notification path that
# ``TaskManager.complete()`` calls. The legacy ``notify_completed``
# (above) is the silent fallback — it early-returns ``False`` when
# the config gate is closed. The v3 gate's primary path shells out
# to the Hermes CLI (``hermes send``), which resolves its own
# Telegram credentials independently of ``config/notify.json``.
#
# Both functions return a structured dict (NOT a bool) so the
# dispatcher can persist the full outcome into
# ``task_outputs.notification_json`` and so
# ``compute_completion_state`` can derive the 4-stage completion
# state from the persisted blob.


def _now_iso_utc() -> str:
    """ISO-8601 UTC timestamp with the trailing ``Z``."""
    return datetime.now(timezone.utc).isoformat()


def _now_iso_taipei() -> str:
    """ISO-8601 timestamp in Asia/Taipei (UTC+08:00)."""
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def notify_terminal_hermes_gateway(
    task_id: str,
    status: str,
    *,
    chat_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Send a terminal-status alert via the Hermes Telegram Gateway.

    Generalized form of ``notify_completed_hermes_gateway`` that
    works for any terminal status (``"completed"``, ``"failed"``,
    ``"timeout"``, ``"cancelled"``, and any future terminal
    verdict). The ``status`` is used both as the alert body symbol
    lookup (via ``_format_alert``) and as the subject suffix so the
    operator can grep Telegram for ``AEE task failed:`` /
    ``AEE task timeout:`` etc.

    Shells out to ``hermes send --to telegram:<chat_id> --subject
    <subject> --file <tmpfile> --json`` with a 30s timeout. The
    chat id defaults to the ``TELEGRAM_CHAT_ID`` env var when not
    passed explicitly. The subject is
    ``"AEE task <status>: <task_id>"``; the body is the formatted
    alert text from ``_format_alert(task_id, status)`` (or a
    minimal ``"task <id> <status>"`` string when the formatter
    returns ``None`` — e.g. when the task row is missing).

    Returns a dict with keys:

    * ``sent``         — bool, True iff the gateway reported a
                         confirmed send.
    * ``method``       — ``"hermes_send"``.
    * ``recipient``    — the chat id the alert was sent to.
    * ``message_id``   — int|None — the Telegram message id
                         returned by the gateway (None when the
                         gateway did not return one, even on
                         success).
    * ``ts_utc``       — ISO-8601 UTC timestamp of the attempt.
    * ``ts_taipei``    — ISO-8601 Asia/Taipei timestamp of the
                         attempt.
    * ``attempts``     — 1 (the v3 path tries once; the fallback
                         is a separate call in
                         ``notify_completed_with_fallback``).
    * ``last_error``   — str|None — set when ``sent`` is False or
                         when ``message_id`` is None.

    Defensive: this function MUST NOT raise. Any
    ``subprocess.CalledProcessError`` / ``TimeoutExpired`` /
    ``JSONDecodeError`` / unexpected exception is caught and
    returned as ``sent=False`` with ``last_error`` populated. The
    temp file holding the body is always cleaned up (``finally``
    block).
    """
    ts_utc = _now_iso_utc()
    ts_taipei = _now_iso_taipei()
    resolved_chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "").strip() or None
    if not resolved_chat_id:
        return {
            "sent": False,
            "method": "hermes_send",
            "recipient": None,
            "message_id": None,
            "ts_utc": ts_utc,
            "ts_taipei": ts_taipei,
            "attempts": 1,
            "last_error": "TELEGRAM_CHAT_ID not set and no chat_id passed",
        }

    subject = f"AEE task {status}: {task_id}"
    # Build the body. _format_alert returns None when the task
    # row is missing (e.g. the task was deleted between complete()
    # and the gate firing). Fall back to a minimal string so the
    # gateway still has a body to send.
    try:
        body = _format_alert(task_id, status)
    except Exception as exc:  # noqa: BLE001 — never raise from the formatter
        body = None
        log.warning(
            "notifier.notify_terminal_hermes_gateway: _format_alert raised task_id=%s status=%s err=%s",
            task_id, status, exc,
        )
    if not body:
        body = f"task {task_id} {status}"

    tmpfile = None
    try:
        # NamedTemporaryFile so the path is stable across the
        # write + the subprocess call. delete=False so we can
        # close it (Windows-safe) before the subprocess reads it;
        # we clean up in the finally block.
        fd, tmpfile = tempfile.mkstemp(
            prefix=f"aee-v3-notif-{task_id}-",
            suffix=".txt",
            text=True,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)

        proc = subprocess.run(
            [
                "hermes", "send",
                "--to", f"telegram:{resolved_chat_id}",
                "--subject", subject,
                "--file", str(tmpfile),
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            stderr_tail = (proc.stderr or "").strip()[:500]
            return {
                "sent": False,
                "method": "hermes_send",
                "recipient": resolved_chat_id,
                "message_id": None,
                "ts_utc": ts_utc,
                "ts_taipei": ts_taipei,
                "attempts": 1,
                "last_error": (
                    f"hermes send exit={proc.returncode}: {stderr_tail}"
                ),
            }
        stdout = (proc.stdout or "").strip()
        if not stdout:
            return {
                "sent": False,
                "method": "hermes_send",
                "recipient": resolved_chat_id,
                "message_id": None,
                "ts_utc": ts_utc,
                "ts_taipei": ts_taipei,
                "attempts": 1,
                "last_error": "hermes send returned empty stdout",
            }
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError as exc:
            return {
                "sent": False,
                "method": "hermes_send",
                "recipient": resolved_chat_id,
                "message_id": None,
                "ts_utc": ts_utc,
                "ts_taipei": ts_taipei,
                "attempts": 1,
                "last_error": f"hermes send stdout JSONDecodeError: {exc}",
            }
        # The Hermes CLI ``send --json`` contract returns a dict
        # with at least ``success`` (bool) and, on success,
        # ``message_id`` (int). Older/alternate shapes used ``ok`` or
        # ``sent``; accept any of the three so the parser stays
        # forward- and backward-compatible.
        ok = bool(
            parsed.get("success",
                       parsed.get("ok",
                                  parsed.get("sent", False)))
        )
        message_id = parsed.get("message_id")
        if not ok:
            return {
                "sent": False,
                "method": "hermes_send",
                "recipient": resolved_chat_id,
                "message_id": None,
                "ts_utc": ts_utc,
                "ts_taipei": ts_taipei,
                "attempts": 1,
                "last_error": (
                    parsed.get("error")
                    or parsed.get("last_error")
                    or "hermes send returned ok=False"
                ),
            }
        return {
            "sent": True,
            "method": "hermes_send",
            "recipient": resolved_chat_id,
            "message_id": message_id,
            "ts_utc": ts_utc,
            "ts_taipei": ts_taipei,
            "attempts": 1,
            "last_error": None,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "sent": False,
            "method": "hermes_send",
            "recipient": resolved_chat_id,
            "message_id": None,
            "ts_utc": ts_utc,
            "ts_taipei": ts_taipei,
            "attempts": 1,
            "last_error": f"hermes send TimeoutExpired after 30s: {exc}",
        }
    except subprocess.CalledProcessError as exc:
        return {
            "sent": False,
            "method": "hermes_send",
            "recipient": resolved_chat_id,
            "message_id": None,
            "ts_utc": ts_utc,
            "ts_taipei": ts_taipei,
            "attempts": 1,
            "last_error": f"hermes send CalledProcessError: {exc}",
        }
    except FileNotFoundError as exc:
        # ``hermes`` binary not on PATH. Most common failure in
        # environments without the Hermes CLI installed.
        return {
            "sent": False,
            "method": "hermes_send",
            "recipient": resolved_chat_id,
            "message_id": None,
            "ts_utc": ts_utc,
            "ts_taipei": ts_taipei,
            "attempts": 1,
            "last_error": f"hermes binary not found: {exc}",
        }
    except Exception as exc:  # noqa: BLE001 — never raise from the gate
        return {
            "sent": False,
            "method": "hermes_send",
            "recipient": resolved_chat_id,
            "message_id": None,
            "ts_utc": ts_utc,
            "ts_taipei": ts_taipei,
            "attempts": 1,
            "last_error": f"hermes_send unexpected {type(exc).__name__}: {exc}",
        }
    finally:
        if tmpfile:
            try:
                os.unlink(tmpfile)
            except OSError:
                pass


# Map a terminal task status to the legacy in-process notifier
# function. ``completed`` -> ``notify_completed``, etc. Used by
# ``notify_terminal_with_fallback`` to pick the right legacy
# fallback so the gate reuses the existing per-status legacy
# notifier rather than always falling back to ``notify_completed``.
_LEGACY_NOTIFIER_BY_STATUS: Dict[str, Any] = {}  # populated below


def _legacy_notifier_for(status: str):
    """Return the legacy in-process notifier for ``status``, or
    ``None`` when no legacy notifier exists for that status.

    The lookup dereferences ``_LEGACY_NOTIFIER_BY_STATUS`` lazily
    so a test that monkey-patches the module-level
    ``notify_completed`` / ``notify_failed`` / etc. symbols
    AFTER import still sees the patched function (the dict holds
    a name, not a captured reference). This is essential for
    the existing ``test_fallback_uses_legacy_when_gateway_fails``
    test which patches ``dispatcher.notifier.notify_completed``.
    """
    fn = _LEGACY_NOTIFIER_BY_STATUS.get(status)
    if fn is None:
        return None
    # Re-resolve through the module namespace so tests that
    # patch the public ``notify_*`` symbols see the patch.
    import sys
    mod = sys.modules.get("dispatcher.notifier")
    if mod is not None:
        name = getattr(fn, "__name__", None)
        if name:
            patched = getattr(mod, name, None)
            if patched is not None:
                return patched
    return fn


def notify_terminal_with_fallback(
    task_id: str,
    status: str,
    *,
    chat_id: Optional[str] = None,
) -> Dict[str, Any]:
    """AEE v3 guaranteed completion-notification gate — try the
    Hermes Telegram Gateway first, then fall back to the legacy
    in-process notifier.

    Generalized form of the original
    ``notify_completed_with_fallback`` that works for ANY
    terminal status (``"completed"``, ``"failed"``,
    ``"timeout"``, ``"cancelled"``). The primary path
    (``notify_terminal_hermes_gateway``) is the working path. If
    it returns ``sent=False``, the legacy in-process notifier
    for the same status (e.g. ``notify_failed`` for ``failed``)
    is tried as a fallback so a missing Hermes CLI binary or a
    gateway outage does not silently drop the alert.

    This is the single notification entry point called from
    every terminal finalization path in
    ``TaskManager`` (``complete``, ``fail``, ``timeout``,
    ``cancel``). It guarantees a notification ATTEMPT for every
    terminal transition, regardless of whether the artifact /
    completion gate passed. Notification failure is recorded
    but NEVER masks the original task outcome (the caller
    keeps the terminal status the finalization path set).

    Returns a merged dict with the same key shape as
    ``notify_terminal_hermes_gateway``. ``method`` reflects
    which path actually succeeded:

    * ``"hermes_send"`` — the gateway path succeeded.
    * ``"notifier.notify_<status>"`` — the gateway failed and
      the legacy fallback succeeded.
    * ``"failed"`` — both paths failed; ``last_error`` is set
      to a combined string.

    The legacy notifier returns a bool (not a dict), so this
    wrapper synthesises the dict shape for the fallback branch:
    ``message_id`` is ``None`` (the legacy path does not
    capture the Telegram message id), ``attempts`` is 2, and
    the timestamps are stamped at the moment the fallback
    returned.
    """
    # Try the Hermes Telegram Gateway first.
    result = notify_terminal_hermes_gateway(task_id, status, chat_id=chat_id)
    if result.get("sent") and result.get("message_id") is not None:
        _append_notification_audit({
            "task_id": task_id,
            "status": status,
            "sent": True,
            "method": result.get("method"),
            "recipient": result.get("recipient"),
            "message_id": result.get("message_id"),
            "ts_utc": result.get("ts_utc"),
            "ts_taipei": result.get("ts_taipei"),
            "last_error": None,
            "attempts": result.get("attempts", 1),
        })
        return result

    # Gateway did not confirm a message_id — fall back to the
    # legacy in-process notifier for the same status.
    gateway_error = result.get("last_error")
    legacy_fn = _legacy_notifier_for(status)
    legacy_sent = False
    legacy_error: Optional[str] = None
    if legacy_fn is not None:
        try:
            legacy_sent = legacy_fn(task_id)
        except Exception as exc:  # noqa: BLE001 — never raise from the gate
            legacy_sent = False
            legacy_error = f"notifier.notify_{status} raised: {exc}"
        else:
            legacy_error = None if legacy_sent else f"notifier.notify_{status} returned False"
    else:
        legacy_error = f"no legacy notifier for status={status!r}"

    legacy_method = f"notifier.notify_{status}"
    if legacy_sent:
        legacy_result = {
            "sent": True,
            "method": legacy_method,
            "recipient": result.get("recipient"),
            "message_id": None,
            "ts_utc": _now_iso_utc(),
            "ts_taipei": _now_iso_taipei(),
            "attempts": 2,
            "last_error": None,
        }
        _append_notification_audit({
            "task_id": task_id,
            "status": status,
            "sent": True,
            "method": legacy_method,
            "recipient": legacy_result.get("recipient"),
            "message_id": None,
            "ts_utc": legacy_result.get("ts_utc"),
            "ts_taipei": legacy_result.get("ts_taipei"),
            "last_error": None,
            "attempts": 2,
        })
        return legacy_result

    # Both paths failed (or no legacy fallback exists). Merge the
    # errors so the operator can see why both were tried.
    combined = []
    if gateway_error:
        combined.append(f"gateway: {gateway_error}")
    if legacy_error:
        combined.append(f"fallback: {legacy_error}")
    failed_result = {
        "sent": False,
        "method": "failed",
        "recipient": result.get("recipient"),
        "message_id": None,
        "ts_utc": result.get("ts_utc", _now_iso_utc()),
        "ts_taipei": result.get("ts_taipei", _now_iso_taipei()),
        "attempts": 2,
        "last_error": "; ".join(combined) if combined else "both paths failed",
    }
    _append_notification_audit({
        "task_id": task_id,
        "status": status,
        "sent": False,
        "method": "failed",
        "recipient": failed_result.get("recipient"),
        "message_id": None,
        "ts_utc": failed_result.get("ts_utc"),
        "ts_taipei": failed_result.get("ts_taipei"),
        "last_error": failed_result.get("last_error"),
        "attempts": 2,
    })
    return failed_result


# Backward-compatibility aliases. Pre-existing call sites and
# tests import ``notify_completed_hermes_gateway`` and
# ``notify_completed_with_fallback``; keep those names working
# as thin wrappers around the generalized terminal gate so the
# public API surface is preserved.
def notify_completed_hermes_gateway(
    task_id: str,
    *,
    chat_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Backward-compat alias for ``notify_terminal_hermes_gateway(
    task_id, "completed", chat_id=chat_id)``."""
    return notify_terminal_hermes_gateway(task_id, "completed", chat_id=chat_id)


def notify_completed_with_fallback(
    task_id: str,
    *,
    chat_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Backward-compat alias for ``notify_terminal_with_fallback(
    task_id, "completed", chat_id=chat_id)``."""
    return notify_terminal_with_fallback(task_id, "completed", chat_id=chat_id)


# Populate the legacy-notifier lookup AFTER the per-status
# notifier functions are defined. Each entry maps a terminal
# status string to the existing in-process notifier function
# so the fallback path reuses the proven legacy code.
_LEGACY_NOTIFIER_BY_STATUS = {
    "completed": notify_completed,
    "failed": notify_failed,
    "timeout": notify_timeout,
    "cancelled": notify_cancelled,
}
