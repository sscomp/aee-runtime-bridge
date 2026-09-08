"""Contract tests for the shared fail-closed notification suppression
boundary (TASK-20260908-0008) — B2/B3 work-order requirements 5a-5f.

Covers:

* (a) subprocess ``hermes send`` blocked in test/verification context.
* (b) in-process urllib direct Telegram send blocked.
* (c) spawned-child env carries no real credentials + disabled sentinel
      (helper level, sandbox-builder level, and real subprocess level).
* (d) verification/semantic smoke default-suppressed WITHOUT any manual
      env export (``scripts/a2_runner.py`` + ``scripts/a2_smoke.py``).
* (e) the production notifier path is NOT permanently disabled: with
      mock transport, a production-authorized process drives the full
      terminal lifecycle to a ``sent=True`` record.
* (f) the historical sandbox isolation regression
      (``tests/test_aee76_sandbox_round_trip.py``) still passes.

Live-send policy: NO test in this module performs a real delivery. The
production-path test (e) runs in an ISOLATED SUBPROCESS with a mocked
urllib transport and a subprocess.run that raises if ever reached; the
historical regression (f) runs under the repo conftest guard.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aee import _notification_guard as guard  # noqa: E402

_LIVE_AUDIT = ROOT / "logs" / "notification_audit.jsonl"

_MINIMAL_CHILD_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": os.environ.get("HOME", "/tmp"),
    "LANG": "C.UTF-8",
    "PYTHONPATH": str(ROOT),
}


def _insert_synthetic_task(db_path: Path, task_id: str = "T-A") -> None:
    """Raw-SQL synthetic task with an explicit never-real id (the B1b
    incident: temp-DB auto-numbering can draw a REAL production task
    id — never use TaskManager.create() for synthetic scenarios)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, title, type, owner, status, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, "a2-b2b3-contract (synthetic)", "ops", "a2",
             "running", "2026-09-08T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# (a) subprocess `hermes send` blocked in test/verification context
# ---------------------------------------------------------------------------


class TestSubprocessSendBlocked:
    def test_hermes_gateway_suppressed_before_subprocess(self, tmp_path):
        """Direct call to the notifier's subprocess entry point must be
        short-circuited by the shared gate BEFORE subprocess.run."""
        from dispatcher.notifier import notify_terminal_hermes_gateway

        with mock.patch(
            "dispatcher.notifier.subprocess.run"
        ) as mock_run, mock.patch(
            "urllib.request.urlopen"
        ) as mock_urlopen:
            result = notify_terminal_hermes_gateway("T-A", "completed")

        assert result["sent"] is False
        assert result["message_id"] is None
        assert result["attempts"] == 0
        assert "suppressed" in (result.get("last_error") or "")
        mock_run.assert_not_called()
        mock_urlopen.assert_not_called()

    def test_hermes_gateway_suppressed_even_with_credentials_armed(
        self, tmp_path
    ):
        """Credentials present in env (the arming trap) must not lift
        the suppression: fail-closed means credentials are irrelevant
        in verification contexts."""
        from dispatcher.notifier import notify_terminal_hermes_gateway

        os.environ["TELEGRAM_CHAT_ID"] = "999000111"
        try:
            with mock.patch(
                "dispatcher.notifier.subprocess.run"
            ) as mock_run:
                result = notify_terminal_hermes_gateway(
                    "T-A", "completed", chat_id="999000111"
                )
        finally:
            # The per-test re-sanitize fixture will restore the dummy;
            # drop our explicit value so nothing dirty leaks.
            os.environ.pop("TELEGRAM_CHAT_ID", None)
        assert result["sent"] is False
        assert result["attempts"] == 0
        mock_run.assert_not_called()

    def test_manager_terminal_suppressed_early_return_contract(self):
        """``TaskManager.timeout()`` under the armed gate takes the
        suppressed EARLY-RETURN contract (TASK-20260908-0007 shape,
        reviewed): the gate returns ``notifications_disabled`` without
        any subprocess / network call, nothing is persisted into
        ``task_outputs.notification_json`` (no fake bookkeeping rows),
        and the task's terminal status is untouched."""
        from tests._live_db_guard import (
            make_temp_dispatcher_db,
            point_module_to_temp_db,
        )
        from dispatcher.manager import TaskManager

        with make_temp_dispatcher_db(
            prefix="a2b2b3-a-"
        ) as db_path, point_module_to_temp_db(db_path):
            _insert_synthetic_task(db_path)
            m = TaskManager()
            with mock.patch(
                "dispatcher.notifier.subprocess.run"
            ) as mock_run, mock.patch(
                "urllib.request.urlopen"
            ) as mock_urlopen, mock.patch.object(
                TaskManager, "_notify_terminal",
                wraps=m._notify_terminal,
            ) as wrap_notify:
                task = m.timeout("T-A", reason="contract-test")
            mock_run.assert_not_called()
            mock_urlopen.assert_not_called()
            assert task.status == "timeout"
            assert wrap_notify.called
            # Early-return contract: the gate short-circuits BEFORE the
            # notifier chain, so nothing is persisted.
            out = m.get_output("T-A") or {}
            assert out.get("notification_json") is None, (
                "suppressed early return must not write fake "
                "notification_json bookkeeping rows"
            )
            # The direct gate API returns the suppressed shape.
            notif = TaskManager._notify_terminal(m, "T-A", "timeout")
            assert notif.get("sent") is False
            assert notif.get("method") == "notifications_disabled"
            assert notif.get("attempts") == 0


# ---------------------------------------------------------------------------
# (b) in-process urllib direct Telegram send blocked
# ---------------------------------------------------------------------------


class TestInprocessUrllibBlocked:
    def test_notify_terminal_inprocess_suppressed_before_transport(self):
        from dispatcher.notifier import notify_terminal_inprocess

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            result = notify_terminal_inprocess("T-A", "failed")

        assert result["sent"] is False
        assert result["method"] == "inprocess_urllib"
        assert result["attempts"] == 0
        assert "suppressed" in (result.get("last_error") or "")
        mock_urlopen.assert_not_called()

    def test_send_telegram_direct_suppressed(self):
        """The raw legacy transport helper (used by the legacy
        notify_<status> path) is gated too."""
        from dispatcher.notifier import _send_telegram

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            ok = _send_telegram("123456:ABC-fake", "000000000", "hi")
        assert ok is False
        mock_urlopen.assert_not_called()

    def test_suppressed_attempts_recorded_to_sink_not_live_audit(self):
        """Suppressed attempts land in the session suppression sink;
        the LIVE audit gains nothing for the synthetic task id."""
        from dispatcher.notifier import notify_terminal_inprocess

        live_before = _live_audit_rows()
        result = notify_terminal_inprocess("T-A", "timeout")
        assert result["sent"] is False

        sink = guard.audit_sink_file()
        assert sink is not None and sink.exists(), (
            "suppression sink must record the suppressed attempt"
        )
        rows = [
            json.loads(line)
            for line in sink.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert any(
            r.get("suppressed") is True and r.get("task_id") == "T-A"
            for r in rows
        ), f"sink missing suppressed row for T-A: {rows[-3:]}"

        live_after = _live_audit_rows()
        assert live_after == live_before, (
            f"live audit changed by this test "
            f"({live_before} -> {live_after} rows)"
        )


def _live_audit_rows() -> int:
    if not _LIVE_AUDIT.exists():
        return 0
    with _LIVE_AUDIT.open("r", encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


# ---------------------------------------------------------------------------
# (c) spawned child: no real token/chat id, notifications disabled
# ---------------------------------------------------------------------------


_PRINT_ENV_CHILD = (
    "import json, os; print(json.dumps({"
    "'TELEGRAM_BOT_TOKEN': os.environ.get('TELEGRAM_BOT_TOKEN'),"
    "'TELEGRAM_CHAT_ID': os.environ.get('TELEGRAM_CHAT_ID'),"
    "'AEE_BRIDGE_NOTIFICATIONS_DISABLED': "
    "os.environ.get('AEE_BRIDGE_NOTIFICATIONS_DISABLED'),"
    "'AEE_NOTIFICATIONS_PRODUCTION': "
    "os.environ.get('AEE_NOTIFICATIONS_PRODUCTION')}))"
)


class TestSpawnedChildIsolation:
    _REAL_SHAPED_TOKEN = "123456:AAExample-real-shaped-token-NOT-A-SECRET"
    _REAL_SHAPED_CHAT = "5132341473"  # the operator chat id FORMAT

    def test_sanitized_child_env_replaces_real_credentials(self):
        env = guard.sanitized_child_env({
            "TELEGRAM_BOT_TOKEN": self._REAL_SHAPED_TOKEN,
            "TELEGRAM_CHAT_ID": self._REAL_SHAPED_CHAT,
        })
        assert env["TELEGRAM_BOT_TOKEN"] != self._REAL_SHAPED_TOKEN
        assert env["TELEGRAM_CHAT_ID"] != self._REAL_SHAPED_CHAT
        assert env["TELEGRAM_BOT_TOKEN"] == guard.DUMMY_CREDENTIAL_ENV[
            "TELEGRAM_BOT_TOKEN"
        ]
        assert env[guard.NOTIFICATIONS_DISABLED_ENV] == "true"
        assert guard.PRODUCTION_SENTINEL_ENV not in env

    def test_sandbox_builder_enforces_disable_floor(self):
        """Even a hostile base_env that explicitly sets the disable var
        to false (or omits it) cannot unmute the sandbox child."""
        from aee.runtime_bridge_sandbox import _build_sandbox_env

        env = _build_sandbox_env(
            repo_root=ROOT,
            db_path=Path("/tmp/x/dispatcher.db"),
            log_dir=Path("/tmp/x/logs"),
            reports_dir=Path("/tmp/x/reports"),
            api_key="test-key",
            base_env={
                "TELEGRAM_BOT_TOKEN": self._REAL_SHAPED_TOKEN,
                "TELEGRAM_CHAT_ID": self._REAL_SHAPED_CHAT,
                guard.NOTIFICATIONS_DISABLED_ENV: "false",
            },
        )
        assert env["TELEGRAM_BOT_TOKEN"] != self._REAL_SHAPED_TOKEN
        assert env["TELEGRAM_CHAT_ID"] != self._REAL_SHAPED_CHAT
        assert env[guard.NOTIFICATIONS_DISABLED_ENV] == "true"

    def test_spawned_child_env_isolation_end_to_end(self):
        """A REAL subprocess spawned with the shared sanitizer's env
        sees dummies + the disabled sentinel + no production sentinel."""
        child_env = guard.sanitized_child_env({
            **_MINIMAL_CHILD_ENV,
            "TELEGRAM_BOT_TOKEN": self._REAL_SHAPED_TOKEN,
            "TELEGRAM_CHAT_ID": self._REAL_SHAPED_CHAT,
        })
        out = subprocess.run(
            [sys.executable, "-c", _PRINT_ENV_CHILD],
            capture_output=True, text=True, timeout=30,
            env=child_env, check=True,
        )
        seen = json.loads(out.stdout.strip())
        assert seen["TELEGRAM_BOT_TOKEN"] != self._REAL_SHAPED_TOKEN
        assert seen["TELEGRAM_CHAT_ID"] != self._REAL_SHAPED_CHAT
        assert seen["AEE_BRIDGE_NOTIFICATIONS_DISABLED"] == "true"
        assert seen["AEE_NOTIFICATIONS_PRODUCTION"] is None


# ---------------------------------------------------------------------------
# (d) verification / semantic smoke default-suppressed, no manual env
# ---------------------------------------------------------------------------


class TestVerificationContract:
    def test_a2_runner_arms_gate_without_manual_env(self, tmp_path):
        """A child run through scripts/a2_runner.py — with NO
        notification env vars in its environment — is suppressed by
        default (the contract: callers never export env)."""
        env = dict(_MINIMAL_CHILD_ENV)
        assert guard.NOTIFICATIONS_DISABLED_ENV not in env
        assert guard.PRODUCTION_SENTINEL_ENV not in env
        child_code = (
            "import os\n"
            "from aee._notification_guard import notifications_disabled\n"
            "assert notifications_disabled(), 'gate must default-arm'\n"
            "assert os.environ.get('AEE_BRIDGE_NOTIFICATIONS_DISABLED') "
            "== 'true', 'env sentinel must be armed by the runner'\n"
            "from unittest import mock\n"
            "from dispatcher.notifier import _send_telegram\n"
            "with mock.patch('urllib.request.urlopen') as m:\n"
            "    assert _send_telegram('123456:ABC-fake', '000000000', "
            "'nope') is False\n"
            "    m.assert_not_called()\n"
            "print('CHILD_OK')\n"
        )
        out = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "a2_runner.py"), "-"],
            input=child_code, capture_output=True, text=True, timeout=60,
            env=env, cwd=str(ROOT),
        )
        assert "CHILD_OK" in out.stdout, (
            f"a2_runner contract failed:\nstdout={out.stdout}\n"
            f"stderr={out.stderr}"
        )

    def test_a2_smoke_default_suppressed_and_temp_db_only(self):
        """The semantic smoke runner passes end-to-end with no manual
        env: synthetic task in a TEMP DB, every terminal call
        suppressed, live audit untouched."""
        live_before = _live_audit_rows()
        out = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "a2_smoke.py")],
            capture_output=True, text=True, timeout=120,
            env=dict(_MINIMAL_CHILD_ENV), cwd=str(ROOT),
        )
        assert out.returncode == 0, (
            f"a2_smoke failed:\nstdout={out.stdout}\nstderr={out.stderr}"
        )
        assert "PASS" in out.stdout
        assert _live_audit_rows() == live_before, (
            "a2_smoke must not touch the live notification audit"
        )

    def test_verification_mode_context_manager_is_balanced(self):
        """The context manager restores state on normal exit and on
        exception (fail-closed: suppression holds through the error)."""
        base_disabled = guard.notifications_disabled()
        assert base_disabled is True  # session armed
        with guard.verification_mode() as snap:
            assert snap.get("verification") is True
            assert guard.notifications_disabled() is True
        # Raising inside must still exit cleanly (finally).
        with pytest.raises(ValueError):
            with guard.verification_mode():
                raise ValueError("boom")
        assert guard.notifications_disabled() is True


# ---------------------------------------------------------------------------
# (e) production notifier path NOT permanently disabled
# ---------------------------------------------------------------------------


_PROD_CHILD = '''\
import json, os, sqlite3, sys, tempfile
from pathlib import Path as _P
sys.path.insert(0, os.environ["A2_REPO_ROOT"])
from aee import _notification_guard as g
# Mirror conftest Layer 4: redirect the notifier audit writers to a
# process-local sink so this child's stub rows (mock mid 4242, dummy
# chat 000000000) NEVER land in the live
# logs/notification_audit.jsonl.
import dispatcher.notifier as _dn
_sink = _P(tempfile.mkdtemp(prefix="a2b2b3-prod-sink-"))
def _sink_audit(record):
    with (_sink / "notification_audit.jsonl").open("a") as f:
        f.write(json.dumps(record, default=str, ensure_ascii=False) + "\\n")
def _sink_local(line):
    with (_sink / "notifier.log").open("a") as f:
        f.write(line + "\\n")
_dn._append_notification_audit = _sink_audit
_dn._append_local_log = _sink_local
# The ONE production authorization sentinel (a long-lived service
# process opts out of suppression exactly once, by design).
g.production_notifications_authorized()
# Hostile env + verification attempts must NOT silence production.
os.environ["AEE_BRIDGE_NOTIFICATIONS_DISABLED"] = "true"
g.enter_verification_mode()
assert g.notifications_disabled() is False, (
    "production sentinel must override env + verification mode")
assert g.enter_verification_mode().get("production") is True

from tests._live_db_guard import (
    make_temp_dispatcher_db, point_module_to_temp_db,
)
with make_temp_dispatcher_db(prefix="a2b2b3-prod-") as db_path, \\
        point_module_to_temp_db(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tasks (task_id, title, type, owner, status, "
        "created_at) VALUES ('T-A', 'prod-path-smoke', 'ops', 'a2', "
        "'running', '2026-09-08T00:00:00+00:00')")
    conn.commit()
    conn.close()

    from unittest import mock
    from dispatcher.manager import TaskManager
    captured = {}

    def fake_urlopen(req, *a, **k):
        captured["url_host"] = "api.telegram.org" in str(
            getattr(req, "full_url", req))
        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return json.dumps(
                    {"ok": True, "result": {"message_id": 4242}}
                ).encode()
        return _Resp()

    # Belt and braces: a subprocess spawn here would be a bug — fail
    # loudly instead of reaching for the hermes CLI.
    with mock.patch("subprocess.run",
                    side_effect=AssertionError(
                        "subprocess must not fire in production-path "
                        "mock test")), \\
            mock.patch("urllib.request.urlopen", fake_urlopen):
        m = TaskManager()
        m.complete("T-A", output_text="prod path smoke")
    out = m.get_output("T-A") or {}
    blob = out.get("notification_json")
    assert blob, "no notification persisted"
    decoded = json.loads(blob)
    assert decoded.get("sent") is True, decoded
    assert captured.get("url_host") is True, (
        "mock transport never reached — production path closed")
print("PROD_LIFECYCLE_OK")
'''


class TestProductionPathNotPermanentlyDisabled:
    @pytest.mark.allow_notification_gate
    @pytest.mark.allow_telegram_http
    def test_transport_open_path_reachable_when_gate_opted_out(self):
        """With the per-test opt-out, the gate's OPEN path passes calls
        through to the transport (mocked) — proving suppression is a
        context property, not a permanent kill switch."""
        from dispatcher.notifier import _send_telegram

        calls = []

        def _fake_urlopen(req, *a, **k):
            calls.append(req)

            class _Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return json.dumps(
                        {"ok": True, "result": {"message_id": 4242}}
                    ).encode()

            return _Resp()

        with mock.patch("urllib.request.urlopen", _fake_urlopen):
            ok = _send_telegram("123456:ABC-fake", "000000000", "hi")
        assert ok is True
        assert len(calls) == 1

    def test_production_authorized_lifecycle_via_mock_transport(
        self, tmp_path
    ):
        """(e) full isolated-subprocess proof: a production-authorized
        process drives a complete terminal lifecycle with a MOCK
        transport and reaches sent=True — the service path is intact."""
        child = tmp_path / "prod_child.py"
        child.write_text(_PROD_CHILD, encoding="utf-8")
        env = dict(_MINIMAL_CHILD_ENV)
        env["A2_REPO_ROOT"] = str(ROOT)
        out = subprocess.run(
            [sys.executable, str(child)],
            capture_output=True, text=True, timeout=120,
            env=env, cwd=str(ROOT),
        )
        assert "PROD_LIFECYCLE_OK" in out.stdout, (
            f"production-path contract failed:\nstdout={out.stdout}\n"
            f"stderr={out.stderr}"
        )

    def test_no_permanent_state_leaks_into_this_process(self):
        """The in-process session never carries the production sentinel
        (suppression stays armed for the whole pytest session)."""
        assert guard.production_authorized() is False
        assert guard.notifications_disabled() is True


# ---------------------------------------------------------------------------
# (f) historical sandbox isolation regression
# ---------------------------------------------------------------------------


class TestHistoricalRegression:
    def test_aee76_sandbox_round_trip_still_passes(self):
        """The historical isolation regression suite (B3 incident
        surface: spawned sandbox bridge children) passes under the
        shared gate."""
        out = subprocess.run(
            [sys.executable, "-m", "pytest",
             "tests/test_aee76_sandbox_round_trip.py", "-q"],
            capture_output=True, text=True, timeout=600,
            env=dict(_MINIMAL_CHILD_ENV), cwd=str(ROOT),
        )
        combined = (out.stdout or "") + (out.stderr or "")
        assert out.returncode == 0, (
            f"sandbox regression failed:\n{combined[-3000:]}"
        )
        assert " failed" not in combined.split("passed")[0][-40:]