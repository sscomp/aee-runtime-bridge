"""Telegram env sanitizer + subprocess env helper (TASK-20260908-0007).

Why this exists
---------------

The pytest-process-level ``hermes send`` sentinel in ``conftest.py``
cannot see inside *spawned child processes*. On 2026-09-07 a sandbox
bridge child (``tests/test_aee76_sandbox_round_trip.py`` -> ``aee/
runtime_bridge_sandbox.py`` -> ``uvicorn app:app``) inherited the real
production credentials because ``app.py`` runs ``load_dotenv()`` at
import time with the repo root as cwd, loading
``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID`` from the repo ``.env``
into the child. The child's notification gate then delivered 4 REAL
Telegram messages (audit message_id 2930-2933) during a pytest run.

Root cause chain:

1. sandbox ``_build_sandbox_env()`` builds a whitelist env WITHOUT the
   Telegram variables (so nothing blocks the child from loading them);
2. child imports ``app`` -> ``load_dotenv()`` (python-dotenv
   ``override=False`` by default: variables ALREADY SET in the
   environment win over the ``.env`` file);
3. child's notifier gate fires ``hermes send`` (the Hermes CLI resolves
   its own credentials from ``~/.hermes/.env``) -> real send.

Fix strategy (this module):

* ``DUMMY_TELEGRAM_ENV`` — dummy values for every credential variable
  the notification paths consult. When present in a child env, step 2
  above becomes a no-op for these keys and the child sees only dummies.
* ``sanitized_env()`` — public helper for tests that spawn subprocesses:
  start from a base env (default: current ``os.environ``), strip
  real Telegram credentials, inject dummies. Pass the result as
  ``env=`` to ``subprocess.Popen`` / ``run``.
* ``sanitize_os_environ()`` / ``restore_os_environ()`` — in-process
  session-wide sanitization used by ``tests/conftest.py`` at import
  time (before any test module imports ``app`` or spawns a child).

Note on ``BRIDGE_API_KEY``: sanitizing it to a dummy does NOT weaken
tests — every sandbox/bridge child that needs an API key gets one
explicitly from its builder (``_build_sandbox_env``), and tests that
call the live bridge HTTP API use ``tests/_live_db_guard.py`` helpers,
not the raw env value. A dummy value simply guarantees a leaked
credential can never be a real one.

Public surface
--------------

* :data:`DUMMY_TELEGRAM_ENV` — dict of dummy credential values.
* :data:`TELEGRAM_ENV_KEYS` — the env var names covered.
* :data:`TELEGRAM_API_HOSTS` — hostnames whose HTTP calls are blocked
  in-process by the conftest guard.
* :func:`url_host` — extract the lowercase host from a URL / Request.
* :func:`sanitized_env` — build a subprocess env with real Telegram
  credentials replaced by dummies.
* :func:`sanitize_os_environ` / :func:`restore_os_environ` — sanitize /
  restore the CURRENT process's ``os.environ`` (save-restore semantics).

Hard rules
----------

* This module NEVER reads, prints, or logs the *values* of real
  credentials — it only moves them between ``os.environ`` and a
  private saved-copy dict that stays in memory.
* It never writes to ``.env`` or any config file. env-only, memory-only.
"""
from __future__ import annotations

import os
import urllib.parse
from typing import Dict, Optional

# ---------------------------------------------------------------------------
# The credential variables covered by the sanitizer
# ---------------------------------------------------------------------------

TELEGRAM_ENV_KEYS: tuple = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "BRIDGE_API_KEY",
)

DUMMY_TELEGRAM_ENV: Dict[str, str] = {
    "TELEGRAM_BOT_TOKEN": "test-token-dummy",
    "TELEGRAM_CHAT_ID": "000000000",
    "BRIDGE_API_KEY": "test-bridge-key-dummy",
}

# Hosts whose HTTP calls are treated as REAL Telegram sends during tests.
TELEGRAM_API_HOSTS: frozenset = frozenset({"api.telegram.org"})

# Private saved copy of the pre-sanitization values (in-memory only).
_SAVED_ENV: Dict[str, Optional[str]] = {}
_SANITIZED: bool = False


# ---------------------------------------------------------------------------
# URL host extraction (used by the conftest HTTP guard)
# ---------------------------------------------------------------------------


def url_host(url) -> Optional[str]:
    """Return the lowercase host of ``url``, or ``None``.

    ``url`` may be a plain string or an ``urllib.request.Request``.
    Never raises.
    """
    try:
        if hasattr(url, "full_url"):  # urllib.request.Request
            url = url.full_url
        if not isinstance(url, str):
            parsed = getattr(url, "host", None)
            return str(parsed).lower() if parsed else None
        host = urllib.parse.urlsplit(url).hostname
        return str(host).lower() if host else None
    except Exception:  # noqa: BLE001 — never raise from the guard
        return None


# ---------------------------------------------------------------------------
# Public helper: build a sanitized env for subprocess spawning
# ---------------------------------------------------------------------------


def sanitized_env(base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Return a copy of ``base`` (default: current ``os.environ``) with
    real Telegram credentials replaced by dummies.

    Tests that spawn bridge/uvicorn/executor child processes MUST pass
    this as ``env=`` to ``subprocess.Popen`` / ``subprocess.run`` so the
    child never sees production credentials — even though the child's
    ``load_dotenv()`` would otherwise re-load them from the repo
    ``.env`` (dummy values already present in the env win because
    ``load_dotenv()`` does not override existing variables).

    TASK-20260908-0008: the notification-disabled sentinel from the
    shared suppression boundary is injected as well, so every child
    spawned with this env is notification-suppressed at source (the
    shared gate, not just dummy credentials).

    Always returns a NEW dict; the input (and ``os.environ``) is never
    mutated.
    """
    env = dict(os.environ if base is None else base)
    for key, dummy in DUMMY_TELEGRAM_ENV.items():
        env[key] = dummy
    try:
        from aee import _notification_guard as _task0008_guard

        env[_task0008_guard.NOTIFICATIONS_DISABLED_ENV] = "true"
    except Exception:  # noqa: BLE001 — fallback to the literal name
        env["AEE_BRIDGE_NOTIFICATIONS_DISABLED"] = "true"
    # B4 (TASK dispatcher-DB fail-closed): point any dispatcher-aware
    # child at THIS process's isolated temp DB. A child whose
    # ``AEE_BRIDGE_DB_PATH`` is unset would otherwise fall back to the
    # production ``data/dispatcher.db``; the sentinel makes the child's
    # sandbox bootstrap rebind to the parent's temp DB instead. When
    # this process has no isolated binding (a genuinely
    # production-context parent), no sentinel is injected — production
    # behavior is unchanged.
    try:
        from aee import _db_guard as _b4_guard

        sentinel = _b4_guard.child_db_sentinel()
        if sentinel is not None:
            env["AEE_BRIDGE_DB_PATH"] = str(sentinel)
    except Exception:  # noqa: BLE001 — notification sanitization must not fail
        pass
    return env


# ---------------------------------------------------------------------------
# In-process session-wide sanitization (used by conftest at import time)
# ---------------------------------------------------------------------------


def sanitize_os_environ() -> None:
    """Replace real Telegram credential values in THIS process's
    ``os.environ`` with dummies, saving the originals for
    :func:`restore_os_environ`.

    Idempotent: the first call saves the originals; later calls keep
    the dummies in place (and never overwrite the saved originals with
    dummy values). Session-scoped: restore at interpreter/pytest exit.
    """
    global _SANITIZED
    if not _SANITIZED:
        for key in TELEGRAM_ENV_KEYS:
            _SAVED_ENV[key] = os.environ.get(key)  # may be None (absent)
        _SANITIZED = True
    for key, dummy in DUMMY_TELEGRAM_ENV.items():
        os.environ[key] = dummy


def restore_os_environ() -> None:
    """Restore the pre-sanitization ``os.environ`` values saved by
    :func:`sanitize_os_environ`.

    A variable that was absent before sanitization is REMOVED again
    (so the parent shell / pytest invocation is left exactly as found).
    """
    for key, value in _SAVED_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    _SAVED_ENV.clear()
    globals()["_SANITIZED"] = False


def is_sanitized() -> bool:
    """True iff ``os.environ`` currently holds ONLY dummy Telegram
    credential values (never the real ones). Used by the guard's
    self-test."""
    return all(
        os.environ.get(key) == DUMMY_TELEGRAM_ENV[key]
        for key in TELEGRAM_ENV_KEYS
    )