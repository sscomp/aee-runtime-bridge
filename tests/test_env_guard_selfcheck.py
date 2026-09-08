"""Guard self-checks for the Telegram env sanitizer (TASK-20260908-0007).

These tests verify the three isolation layers added to
``tests/conftest.py`` + ``tests/_env_guard.py``:

1. **Session sanitization** — the pytest process's ``os.environ`` holds
   ONLY dummy Telegram credentials for the whole session (installed at
   conftest import time, before any test module runs).
2. **HTTP guard** — in-process ``urllib.request.urlopen`` calls and
   ``http.client`` connections to ``api.telegram.org`` raise
   ``AssertionError``; calls to other hosts pass through. (Verified
   with a stubbed underlying transport — no network.)
3. **Subprocess env choke point** — a spawned child sees the sanitized
   environment, both for inherited env and for an explicit
   whitelist-style env missing the Telegram keys.

The last class deliberately spawns with the RAW environment under
``@pytest.mark.raw_subprocess_env`` to prove the pre-fix leak shape
(parent env inherited verbatim) is neutralized by Layer 1 alone —
this is the regression that produced audit message_id 2930-2933 on
2026-09-07.

No test in this module talks to the network.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tests._env_guard as env_guard  # noqa: E402

_PRINT_ENV_CHILD = (
    "import json, os; "
    "print(json.dumps({k: os.environ.get(k) for k in "
    "('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'BRIDGE_API_KEY')}))"
)


def _conftest_module():
    """Return the already-imported conftest module.

    pytest imports ``tests/conftest.py`` as ``tests.conftest`` (the
    ``tests`` package has ``__init__.py``), so this hits sys.modules
    cache instead of re-executing the conftest.
    """
    import tests.conftest as conftest  # noqa: PLC0415

    return conftest


def _child_env_json(*args, **kwargs) -> dict:
    """Run the print-env child and return its parsed env snapshot."""
    out = subprocess.run(
        [sys.executable, "-c", _PRINT_ENV_CHILD],
        capture_output=True, text=True, timeout=30, check=True,
        *args, **kwargs,
    )
    import json  # noqa: PLC0415

    return json.loads(out.stdout.strip())


# ---------------------------------------------------------------------------
# Layer 1 — session-wide sanitization
# ---------------------------------------------------------------------------


class TestSessionSanitization:
    def test_telegram_vars_are_dummies_in_pytest_process(self):
        for key, dummy in env_guard.DUMMY_TELEGRAM_ENV.items():
            assert os.environ.get(key) == dummy, (
                f"{key}={os.environ.get(key)!r} — session sanitization "
                f"(conftest Layer 1) is not active; a child spawned now "
                f"could see production credentials"
            )

    def test_is_sanitized_agrees(self):
        assert env_guard.is_sanitized()

    def test_sanitized_env_helper_returns_fresh_dict_with_dummies(self):
        env = env_guard.sanitized_env()
        for key, dummy in env_guard.DUMMY_TELEGRAM_ENV.items():
            assert env[key] == dummy
        # A new dict — os.environ itself was not handed out.
        assert env is not os.environ


# ---------------------------------------------------------------------------
# Layer 2 — api.telegram.org HTTP guard
# ---------------------------------------------------------------------------


class TestTelegramHttpGuard:
    def test_urlopen_to_telegram_api_raises(self, monkeypatch):
        import urllib.request as urlreq  # noqa: PLC0415

        conftest = _conftest_module()
        calls = []

        def _fake_real_urlopen(req, *args, **kwargs):
            calls.append(req)
            return "REAL-TRANSPORT-REACHED"

        # Replace the (session-guarded) urlopen with a stand-in "real",
        # then reinstall the conftest guard on top of it.
        monkeypatch.setattr(urlreq, "urlopen", _fake_real_urlopen)
        conftest._install_telegram_http_guard()
        try:
            req = urlreq.Request("https://api.telegram.org/botX/sendMessage")
            with pytest.raises(AssertionError) as excinfo:
                urlreq.urlopen(req, timeout=1)
            assert "api.telegram.org" in str(excinfo.value)
            assert "allow_telegram_http" in str(excinfo.value)
            assert calls == [], "the underlying transport must never be reached"
        finally:
            conftest._install_telegram_http_guard._restore()  # type: ignore[attr-defined]

    def test_urlopen_to_localhost_passes_through(self, monkeypatch):
        import urllib.request as urlreq  # noqa: PLC0415

        conftest = _conftest_module()

        def _fake_real_urlopen(req, *args, **kwargs):
            return "PASSED-THROUGH"

        monkeypatch.setattr(urlreq, "urlopen", _fake_real_urlopen)
        conftest._install_telegram_http_guard()
        try:
            req = urlreq.Request("http://127.0.0.1:8787/health")
            assert urlreq.urlopen(req, timeout=1) == "PASSED-THROUGH"
        finally:
            conftest._install_telegram_http_guard._restore()  # type: ignore[attr-defined]

    def test_httpsconnection_to_telegram_blocked_at_init(self):
        from http.client import HTTPSConnection  # noqa: PLC0415

        with pytest.raises(AssertionError) as excinfo:
            HTTPSConnection("api.telegram.org")
        assert "api.telegram.org" in str(excinfo.value)

    def test_httpsconnection_to_localhost_allowed(self):
        from http.client import HTTPSConnection  # noqa: PLC0415

        conn = HTTPSConnection("127.0.0.1", 8787)
        assert conn.host == "127.0.0.1"
        conn.close()


# ---------------------------------------------------------------------------
# Layer 3 — subprocess env choke point
# ---------------------------------------------------------------------------


class TestSubprocessEnvSanitization:
    def test_child_inherits_sanitized_env_when_env_omitted(self):
        child_env = _child_env_json()
        for key, dummy in env_guard.DUMMY_TELEGRAM_ENV.items():
            assert child_env.get(key) == dummy, (
                f"child saw {key}={child_env.get(key)!r}; the sanitized "
                f"default env injection (conftest Layer 3) failed"
            )

    def test_explicit_env_missing_telegram_keys_gets_dummies(self):
        # Whitelist-style env (like the sandbox builder's) without any
        # Telegram keys — the guard must fill the dummies in so a child
        # load_dotenv() cannot re-import real credentials over the gap.
        whitelist = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
        }
        child_env = _child_env_json(env=whitelist)
        for key, dummy in env_guard.DUMMY_TELEGRAM_ENV.items():
            assert child_env.get(key) == dummy

    def test_explicit_env_keeps_deliberate_dummy_values(self):
        whitelist = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "TELEGRAM_BOT_TOKEN": "deliberate-fake",
            "TELEGRAM_CHAT_ID": "111111111",
            "BRIDGE_API_KEY": "deliberate-bridge-key",
        }
        child_env = _child_env_json(env=whitelist)
        assert child_env["TELEGRAM_BOT_TOKEN"] == "deliberate-fake"
        assert child_env["TELEGRAM_CHAT_ID"] == "111111111"
        assert child_env["BRIDGE_API_KEY"] == "deliberate-bridge-key"


# ---------------------------------------------------------------------------
# The 2026-09-07 incident shape: RAW inherited env must still be safe
# ---------------------------------------------------------------------------


class TestRawEnvLeakShapeIsNeutralized:
    @pytest.mark.raw_subprocess_env
    def test_raw_inherited_env_is_still_dummy_via_layer1(self):
        # raw_subprocess_env disables Layer 3; the child now inherits
        # os.environ exactly as the pre-fix tests did. It must STILL be
        # safe because Layer 1 sanitized the session env at conftest
        # import time.
        child_env = _child_env_json()
        for key, dummy in env_guard.DUMMY_TELEGRAM_ENV.items():
            assert child_env.get(key) == dummy, (
                f"RAW-env child saw {key}={child_env.get(key)!r} — this is "
                f"the 2026-09-07 leak shape (audit message_id 2930-2933)"
            )