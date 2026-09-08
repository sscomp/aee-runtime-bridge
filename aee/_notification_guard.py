"""Shared fail-closed notification suppression boundary (TASK-20260908-0008).

Incidents this module closes
----------------------------

* B2 (2026-09-07/08): in-process ``urllib`` calls to ``api.telegram.org``
  bypassed the pytest subprocess sentinel — covered by conftest since
  TASK-0007, but the suppression state itself lived only in
  ``tests/conftest.py`` + ``tests/_env_guard.py``, invisible to
  verification scripts.
* B3 (2026-09-07): a sandbox bridge child inherited the real
  ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID`` via ``load_dotenv()``
  and delivered 4 real Telegram messages (audit mids 2930-2933).
* 2026-09-08 B1b deploy verify: a semantic smoke ran
  ``manager.timeout()/complete()`` OUTSIDE pytest (no conftest guard)
  and delivered 5 real Telegram messages (mids 3073-3077). The only
  existing suppression switch (``AEE_BRIDGE_NOTIFICATIONS_DISABLED``)
  lived in ``TaskManager._notify_terminal`` — the notifier's direct
  send entry points (``_send_telegram``, ``notify_terminal_hermes_gateway``,
  ``notify_terminal_inprocess``) had NO gate.

What this module provides (the contract)
----------------------------------------

1. A single shared suppression gate — ``notifications_disabled()`` —
   consulted by ALL notification send entry points (notifier direct
   sends + manager gate). Suppression is FAIL-CLOSED in
   test/verification contexts and requires an explicit production
   authorization sentinel to lift.
2. ``enter_verification_mode()`` / ``exit_verification_mode()`` /
   ``verification_mode()`` — the verification-mode contract: any
   verification script, semantic smoke, or ad-hoc dispatcher call must
   wrap its work in this (or run under ``scripts/a2_runner.py`` /
   ``scripts/a2_smoke.py``), and suppression is automatic — no caller
   must remember to export env vars.
3. ``production_notifications_authorized()`` — installs the ONE
   production authorization sentinel (``AEE_NOTIFICATIONS_PRODUCTION``
   via ``os.putenv``, bypassing this module's audit-sink patch). Once
   installed, suppression cannot be armed by env or verification mode:
   the production service keeps its normal notifier behavior. This is
   the ONLY way a long-lived service process opts out.
4. ``sanitized_child_env()`` — env for spawned bridge/uvicorn children:
   real Telegram credentials replaced with dummies (never read/logged —
   moved between memory structures only) plus the notification disabled
   sentinel, so no child can live-send even if it reloads credentials
   from the repo ``.env`` (``load_dotenv()`` does not override
   already-set variables).
5. ``audit_sink_file()`` — self-audit: suppressed attempts are recorded
   to a process-local sink (default ``/tmp/a2-notification-sink-<pid>/``)
   instead of the live ``logs/notification_audit.jsonl``, so
   verification runs leave the live audit frozen.

Hard rules
----------

* This module NEVER reads, prints, or logs real credential VALUES —
  it moves them between ``os.environ`` and a private in-memory dict.
* It never writes ``.env`` or any config file.
* Fail-closed: in verification mode, ``notifications_disabled()`` is
  True; suppression is only liftable via the production sentinel, and
  that sentinel makes suppression impossible — it never disables the
  production gate.
* It never raises: suppression logic must be the most reliable code in
  the process. All helpers swallow their own exceptions and default to
  the safe answer.
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

# ---------------------------------------------------------------------------
# Contract constants
# ---------------------------------------------------------------------------

#: Env var that DISABLES notifications (the suppression sentinel). Same
#: name TASK-0007 already wired into the sandbox child env and the B1b
#: smoke remediation; now the ONE shared gate consulted by every send
#: entry point (manager + notifier direct sends).
NOTIFICATIONS_DISABLED_ENV = "AEE_BRIDGE_NOTIFICATIONS_DISABLED"

#: Env var that marks a process as production-authorized (the ONE
#: production authorization sentinel, installed only by
#: :func:`production_notifications_authorized`). While installed,
#: suppression cannot be armed — the production service keeps its
#: normal notifier behavior.
PRODUCTION_SENTINEL_ENV = "AEE_NOTIFICATIONS_PRODUCTION"

#: Dummy credential values handed to spawned children instead of the
#: real ones (presence-only; the real values are never read or logged).
DUMMY_CREDENTIAL_ENV: Dict[str, str] = {
    "TELEGRAM_BOT_TOKEN": "dummy-token-not-a-real-credential",
    "TELEGRAM_CHAT_ID": "000000000",
}

#: Credential env keys covered by :func:`sanitized_child_env`.
CREDENTIAL_ENV_KEYS: tuple = tuple(DUMMY_CREDENTIAL_ENV)

_TRUTHY = {"1", "true", "yes", "on"}

# ---------------------------------------------------------------------------
# Suppression state (thread-safe)
# ---------------------------------------------------------------------------

_lock = threading.RLock()
_state: Dict[str, Any] = {
    "verification": False,      # explicit verification-mode contract
    "production": False,        # production sentinel installed
    "depth": 0,                 # nested verification-mode depth
    "sink_path": None,          # audit sink override (str or None)
}
_saved_disable_env: Optional[str] = None


def _snapshot() -> Dict[str, Any]:
    with _lock:
        return dict(_state)


def _arm_suppression_env() -> None:
    """Set the disable env var so SPAWNED children are also silent
    (children never read this module's in-memory state)."""
    global _saved_disable_env
    try:
        with _lock:
            if _saved_disable_env is None:
                _saved_disable_env = os.environ.get(NOTIFICATIONS_DISABLED_ENV)
            os.environ[NOTIFICATIONS_DISABLED_ENV] = "true"
    except Exception:  # noqa: BLE001 — keep going; state gate still holds
        pass


def _restore_suppression_env() -> None:
    global _saved_disable_env
    try:
        with _lock:
            saved, _saved_disable_env = _saved_disable_env, None
        if saved is None:
            os.environ.pop(NOTIFICATIONS_DISABLED_ENV, None)
        else:
            os.environ[NOTIFICATIONS_DISABLED_ENV] = saved
    except Exception:  # noqa: BLE001
        pass


def notifications_disabled() -> bool:
    """The ONE shared suppression gate.

    True (suppress the send) when:

    * the production sentinel is NOT installed, AND any of:
      - ``AEE_BRIDGE_NOTIFICATIONS_DISABLED`` is truthy in the env
        (explicit suppression, e.g. the sandbox child env);
      - verification mode is armed via
        :func:`enter_verification_mode` / :func:`verification_mode`.

    False (send allowed) only when the production sentinel is installed
    (production service context) — in that case even a stray disable
    env var or verification mode cannot silence the production service,
    which is the "never permanently disable production" requirement.
    """
    try:
        snap = _snapshot()
        if snap.get("production"):
            return False
        try:
            env_disabled = (
                os.environ.get(NOTIFICATIONS_DISABLED_ENV, "")
                .strip()
                .lower()
                in _TRUTHY
            )
        except Exception:  # noqa: BLE001
            env_disabled = False
        return bool(env_disabled or snap.get("verification"))
    except Exception:  # noqa: BLE001 — fail-closed on any internal error
        return True


def production_authorized() -> bool:
    """True iff the production authorization sentinel is installed."""
    try:
        return bool(_snapshot().get("production"))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Production authorization sentinel (the ONE production opt-out)
# ---------------------------------------------------------------------------


def production_notifications_authorized() -> None:
    """Install the production authorization sentinel.

    Uses ``os.putenv`` directly so the sentinel lands in the real
    process environment WITHOUT passing through the module's own
    audit-sink patch (see tests: the sink patch records env writes via
    :func:`audit_sink_file`; the production sentinel must be able to
    arm even in a verification session that patched ``os.environ``).

    After this call:

    * :func:`notifications_disabled` returns False — production
      lifecycle notifications flow normally;
    * :func:`enter_verification_mode` becomes a no-op (suppression
      cannot be armed in a production-authorized process);
    * the state is IMMUTABLE for the process lifetime — there is no
      un-authorize function by design.
    """
    try:
        with _lock:
            _state["production"] = True
        # putenv bypasses any patched os.environ (audit sink, test
        # stubs); the child-visible env also carries the sentinel.
        os.putenv(PRODUCTION_SENTINEL_ENV, "1")
    except Exception:  # noqa: BLE001 — never raise from the sentinel
        pass


# ---------------------------------------------------------------------------
# Verification-mode contract
# ---------------------------------------------------------------------------


def enter_verification_mode() -> Dict[str, Any]:
    """Arm suppression for THIS process (verification-mode contract).

    Idempotent and nestable (depth-counted). Arms the env sentinel too
    so spawned children are suppressed. Returns a snapshot dict for
    logging. No-op in a production-authorized process.
    """
    try:
        with _lock:
            if _state["production"]:
                return dict(_state)
            first = _state["depth"] == 0
            _state["depth"] += 1
            _state["verification"] = True
        if first:
            _arm_suppression_env()
        return _snapshot()
    except Exception:  # noqa: BLE001
        return {"verification": True, "error": "arm-failed-safe"}


def exit_verification_mode() -> Dict[str, Any]:
    """Leave verification mode (depth-counted; see
    :func:`enter_verification_mode`). Restores the env sentinel to its
    pre-arm value at depth 0."""
    try:
        with _lock:
            if _state["depth"] > 0:
                _state["depth"] -= 1
            if _state["depth"] == 0:
                _state["verification"] = False
            last = _state["depth"] == 0
        if last:
            _restore_suppression_env()
        return _snapshot()
    except Exception:  # noqa: BLE001
        return {"verification": False, "error": "exit-failed"}


@contextmanager
def verification_mode() -> Iterator[Dict[str, Any]]:
    """Context manager form of the verification-mode contract.

    Usage (any verification script / semantic smoke)::

        from aee._notification_guard import verification_mode
        with verification_mode():
            manager.complete("T-...")   # cannot live-send

    Fail-closed: an exception inside the block never leaves
    suppression disarmed (exit runs in ``finally``).
    """
    snap = enter_verification_mode()
    try:
        yield snap
    finally:
        exit_verification_mode()


# ---------------------------------------------------------------------------
# Suppressed-send audit (self-audit sink)
# ---------------------------------------------------------------------------


def audit_sink_file() -> Optional[Path]:
    """Path of the process-local suppression audit sink.

    Default: ``/tmp/a2-notification-sink-<pid>/notification_audit.jsonl``
    (NOT the live ``logs/notification_audit.jsonl``). Callers may
    redirect via :func:`set_audit_sink`. Returns None only if even the
    sink directory cannot be created (suppression itself still holds).
    """
    try:
        with _lock:
            override = _state.get("sink_path")
        if override:
            return Path(override)
        sink_dir = Path("/tmp") / f"a2-notification-sink-{os.getpid()}"
        sink_dir.mkdir(parents=True, exist_ok=True)
        return sink_dir / "notification_audit.jsonl"
    except Exception:  # noqa: BLE001
        return None


def set_audit_sink(path: Optional[Path]) -> None:
    """Redirect the suppression audit sink (None = default /tmp sink)."""
    try:
        with _lock:
            _state["sink_path"] = str(path) if path is not None else None
    except Exception:  # noqa: BLE001
        pass


def record_suppressed_send(source: str, detail: Dict[str, Any]) -> None:
    """Record a suppressed send attempt to the self-audit sink.

    Never raises, never touches the live audit — this is bookkeeping
    for verification runs so reviewers can count what WOULD have been
    sent without any real delivery.
    """
    try:
        sink = audit_sink_file()
        if sink is None:
            return
        record = {
            "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "suppressed": True,
            "source": source,
        }
        record.update(detail or {})
        with sink.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Child-process env sanitizer (B3 closure)
# ---------------------------------------------------------------------------


def _is_real_credential(value: Optional[str]) -> bool:
    """True iff ``value`` looks like a real (non-dummy) credential.

    Never inspects the credential's content beyond the dummy markers —
    real values are never read, printed, or logged.
    """
    if value is None:
        return False
    v = value.strip()
    if not v:
        return False
    # Dummy markers used by this module and tests/_env_guard.py.
    if v.startswith(("dummy-", "test-")):
        return False
    if v == DUMMY_CREDENTIAL_ENV["TELEGRAM_CHAT_ID"]:
        return False
    return True


def sanitized_child_env(base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Build a safe env for a spawned child (bridge/uvicorn/hermes).

    * Real Telegram credentials are REPLACED (never copied out) with
      dummies — a child's ``load_dotenv()`` cannot override already-set
      variables, so the repo ``.env`` re-load is a no-op for these keys.
    * ``AEE_BRIDGE_NOTIFICATIONS_DISABLED=true`` is injected so the
      child's notification gate is disabled at source.
    * The production sentinel is never propagated (a child is never
      production-authorized).

    Returns a NEW dict; ``os.environ`` and ``base`` are never mutated.
    """
    env = dict(os.environ if base is None else base)
    for key in CREDENTIAL_ENV_KEYS:
        value = env.get(key)
        if _is_real_credential(value):
            env[key] = DUMMY_CREDENTIAL_ENV[key]
        elif value is None:
            env[key] = DUMMY_CREDENTIAL_ENV[key]
    env[NOTIFICATIONS_DISABLED_ENV] = "true"
    env.pop(PRODUCTION_SENTINEL_ENV, None)
    return env


def sanitize_os_environ() -> None:
    """Replace real Telegram credentials in THIS process's env with
    dummies, saving the originals in memory (session-wide sanitize;
    paired with :func:`restore_os_environ`). Idempotent."""
    try:
        with _lock:
            for key in CREDENTIAL_ENV_KEYS:
                if key not in _state_originals_cache():
                    _remember_original(key)
            for key, dummy in DUMMY_CREDENTIAL_ENV.items():
                os.environ[key] = dummy
    except Exception:  # noqa: BLE001
        pass


def restore_os_environ() -> None:
    """Restore env values saved by :func:`sanitize_os_environ`."""
    try:
        with _lock:
            originals = dict(_state_originals_cache())
            _state_originals_cache().clear()
        for key, value in originals.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    except Exception:  # noqa: BLE001
        pass


# Saved-originals storage (kept out of ``_state`` which is a public
# snapshot dict).
_originals: Dict[str, Optional[str]] = {}


def _state_originals_cache() -> Dict[str, Optional[str]]:
    return _originals


def _remember_original(key: str) -> None:
    _originals[key] = os.environ.get(key)