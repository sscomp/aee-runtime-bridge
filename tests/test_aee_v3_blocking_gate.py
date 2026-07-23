"""AEE v3 Telegram Completion Blocking Gate — targeted tests.

Covers the runtime enforcement (blocking) upgrade from the
observability-only gate in ``test_aee_v3_telegram_gate.py``.

Required test classes from the work order:

1. **missing telegram blocked** — when
   ``enforcement_gate.blocking == true`` AND the notification gate
   returns ``sent=False`` (hermes binary missing / chat_id unset /
   subprocess error), ``complete()`` MUST raise ``NotificationBlocked``
   AND revert ``tasks.status`` from ``completed`` back to ``running``.
2. **message id required** — when ``blocking == true`` AND the gate
   returns ``sent=True`` but ``message_id is None`` (legacy fallback
   path), ``complete()`` MUST still raise ``NotificationBlocked``
   because ``FINAL_COMPLETED`` requires a non-null ``message_id``.
3. **retry / failure regression** — ``fail()`` / ``timeout()`` paths
   are NOT affected by the blocking gate (the gate is wired only
   into ``complete()``). ``fail()`` / ``timeout()`` still set
   terminal status regardless of ``blocking``.
4. **observability-only backward compat** — when ``blocking == false``
   (the default), ``complete()`` still returns ``status='completed'``
   even when notification fails (the existing 43-test suite covers
   this; we add one regression assertion here to lock the contract).
5. **shadow validation harness** — a self-contained test that
   exercises the blocking gate end-to-end against a temp SQLite DB
   with a monkey-patched ``hermes send`` subprocess, proving the
   gate can be validated without touching production config.

Tests run against a temp SQLite DB (no production dispatcher.db
mutation). The ``hermes send`` subprocess call is monkey-patched so
no real Telegram message is sent. The ``config.load`` function is
monkey-patched per-test so ``enforcement_gate.blocking`` can be
flipped without editing ``config/notify.json`` on disk.

Run:
    cd /home/ubuntu/hermes-runtime-bridge && \\
    python3 -m unittest tests.test_aee_v3_blocking_gate -v
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from typing import Any, Dict
from unittest import mock

import dispatcher.db as db_mod
from dispatcher import ids
from dispatcher.manager import (
    NotificationBlocked,
    TaskManager,
)
from dispatcher.notifier import (
    ENFORCEMENT_GATE_VERSION,
    notify_completed_with_fallback,
)
from aee.observability import EventKind


# ---------------------------------------------------------------------------
# Test helpers (mirrors test_aee_v3_telegram_gate.py patterns)
# ---------------------------------------------------------------------------


def _make_fake_hermes_send(result: Dict[str, Any]):
    """Return a fake subprocess.run that returns the configured
    ``result`` dict (json-dumped to stdout) when called with a
    ``hermes send`` argv. Any other argv falls through to the real
    subprocess.run."""
    stdout_payload = json.dumps(result)
    returncode = 0 if result.get("ok", result.get("sent", False)) else 1
    stderr_payload = result.get("error", "")

    def fake_run(argv, *args, **kwargs):
        if argv and argv[0] == "hermes" and len(argv) > 1 and argv[1] == "send":
            class _Proc:
                def __init__(self):
                    self.returncode = returncode
                    self.stdout = stdout_payload
                    self.stderr = stderr_payload
            return _Proc()
        return subprocess.run(argv, *args, **kwargs)

    return fake_run


class _TempDbMixin:
    """Per-test temp SQLite DB so tests don't clobber the real
    dispatcher.db. Resets the thread-local connection + the
    ``_initialized`` flag so each test re-initializes the schema
    against the new DB path."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="aee-v3-block-")
        self._db_path = os.path.join(self._tmpdir, "test_dispatcher.db")
        self._orig_db_path = db_mod.DB_PATH
        db_mod.DB_PATH = db_mod.Path(self._db_path)
        try:
            cached = getattr(db_mod._local, "conn", None)
            if cached is not None:
                try:
                    cached.rollback()
                except Exception:
                    pass
                try:
                    cached.close()
                except Exception:
                    pass
        except Exception:
            pass
        db_mod._local = db_mod.threading.local()
        db_mod._initialized = False
        _ = db_mod.get_conn()
        self._orig_next_task_id = ids.next_task_id
        ids._counter = 0  # type: ignore[attr-defined]
        self._orig_env = {
            k: os.environ.pop(k, None)
            for k in ("TELEGRAM_CHAT_ID", "TELEGRAM_BOT_TOKEN")
        }

    def tearDown(self) -> None:
        for k, v in self._orig_env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        db_mod.DB_PATH = self._orig_db_path
        db_mod._local = db_mod.threading.local()
        db_mod._initialized = False
        ids.next_task_id = self._orig_next_task_id
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _create_running_task(self, m: TaskManager, title: str = "T") -> str:
        t = m.create(
            title=title,
            type="ops",
            priority=50,
            input_text="test input",
            initial_status="queued",
        )
        m.start(t.task_id, hermes_run_id="run-test-1")
        return t.task_id

    def _set_blocking(self, blocking: bool) -> Any:
        """Patch ``config.load`` so ``enforcement_gate.blocking`` returns
        the requested value without editing the on-disk config."""
        def fake_load(name: str):
            if name in ("notify", "notify.json"):
                return {
                    "telegram": {
                        "enabled": True,
                        "bot_token_env": "TELEGRAM_BOT_TOKEN",
                        "chat_id_env": "TELEGRAM_CHAT_ID",
                        "notify_on": ["failed", "timeout", "completed", "cancelled"],
                        "rate_limit_per_hour": 20,
                    },
                    "enforcement_gate": {
                        "version": ENFORCEMENT_GATE_VERSION,
                        "primary_path": "hermes_send",
                        "fallback_path": "notifier.notify_completed",
                        "blocking": blocking,
                    },
                }
            # Fall back to real config for other names.
            from config import load as _real_load
            return _real_load(name)

        patcher = mock.patch("config.load", side_effect=fake_load)
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher


# ---------------------------------------------------------------------------
# 1. Missing telegram blocked
# ---------------------------------------------------------------------------


class TestBlockingGateMissingTelegram(_TempDbMixin, unittest.TestCase):
    """When ``enforcement_gate.blocking == true`` and the notification
    gate returns ``sent=False`` (hermes binary missing / chat_id
    unset / subprocess error), ``complete()`` MUST raise
    ``NotificationBlocked`` AND revert ``status='completed'`` back to
    ``running``."""

    def test_complete_raises_notification_blocked_when_hermes_binary_missing(self):
        self._set_blocking(True)
        os.environ["TELEGRAM_CHAT_ID"] = "5132341473"
        os.environ["TELEGRAM_BOT_TOKEN"] = "dummy"
        m = TaskManager()
        task_id = self._create_running_task(m, "blocking-missing-binary")
        # Make hermes binary missing — subprocess.run raises FileNotFoundError.
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("hermes not found")):
            with self.assertRaises(NotificationBlocked) as ctx:
                m.complete(task_id, output_text="done")
        payload = ctx.exception.args[0]
        self.assertEqual(payload["task_id"], task_id)
        self.assertTrue(payload["blocking"])
        self.assertEqual(payload["stage"], "evidence_completed")
        notif = payload["notification"]
        self.assertFalse(notif["sent"])
        # The task must be reverted to running (NOT still completed).
        reverted = m.get(task_id)
        self.assertIsNotNone(reverted, "task row must still exist after revert")
        self.assertEqual(reverted.status, "running")
        self.assertIsNone(reverted.finished_at)

    def test_complete_raises_notification_blocked_when_chat_id_unset(self):
        self._set_blocking(True)
        # No TELEGRAM_CHAT_ID set — gate returns sent=False with
        # "TELEGRAM_CHAT_ID not set" last_error.
        m = TaskManager()
        task_id = self._create_running_task(m, "blocking-no-chat-id")
        with self.assertRaises(NotificationBlocked) as ctx:
            m.complete(task_id, output_text="done")
        notif = ctx.exception.args[0]["notification"]
        self.assertFalse(notif["sent"])
        self.assertIn("TELEGRAM_CHAT_ID", notif.get("last_error", ""))
        reverted = m.get(task_id)
        self.assertEqual(reverted.status, "running")

    def test_complete_raises_notification_blocked_when_gateway_returns_error(self):
        self._set_blocking(True)
        os.environ["TELEGRAM_CHAT_ID"] = "5132341473"
        os.environ["TELEGRAM_BOT_TOKEN"] = "dummy"
        m = TaskManager()
        task_id = self._create_running_task(m, "blocking-gateway-error")
        # hermes send exits non-zero with error stderr.
        fake = _make_fake_hermes_send({"ok": False, "error": "gateway outage"})
        with mock.patch("subprocess.run", fake):
            with self.assertRaises(NotificationBlocked):
                m.complete(task_id, output_text="done")
        reverted = m.get(task_id)
        self.assertEqual(reverted.status, "running")

    def test_blocking_revert_clears_finished_at(self):
        """The revert must clear ``finished_at`` so the task is no
        longer terminal from the read-side
        ``compute_completion_state`` perspective."""
        self._set_blocking(True)
        os.environ["TELEGRAM_CHAT_ID"] = "5132341473"
        os.environ["TELEGRAM_BOT_TOKEN"] = "dummy"
        m = TaskManager()
        task_id = self._create_running_task(m, "blocking-finished-at-clear")
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaises(NotificationBlocked):
                m.complete(task_id, output_text="done")
        reverted = m.get(task_id)
        self.assertIsNone(reverted.finished_at)

    def test_blocking_emits_notification_failed_event_before_raise(self):
        """The NOTIFICATION_FAILED event must be emitted BEFORE the
        exception is raised so the audit trail is complete when the
        orchestrator catches the exception."""
        self._set_blocking(True)
        os.environ["TELEGRAM_CHAT_ID"] = "5132341473"
        os.environ["TELEGRAM_BOT_TOKEN"] = "dummy"
        m = TaskManager()
        task_id = self._create_running_task(m, "blocking-event-order")
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaises(NotificationBlocked):
                m.complete(task_id, output_text="done")
        # Verify NOTIFICATION_FAILED event was recorded in task_events.
        conn = db_mod.get_conn()
        rows = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? "
            "AND kind = ?",
            (task_id, EventKind.NOTIFICATION_FAILED),
        ).fetchall()
        self.assertGreaterEqual(len(rows), 1, "NOTIFICATION_FAILED event must be emitted")


# ---------------------------------------------------------------------------
# 2. Message id required (sent=True but message_id=None)
# ---------------------------------------------------------------------------


class TestBlockingGateMessageIdRequired(_TempDbMixin, unittest.TestCase):
    """When ``blocking == true`` and the gate returns ``sent=True``
    but ``message_id is None`` (legacy fallback path that doesn't
    capture message_id), ``complete()`` MUST raise
    ``NotificationBlocked`` because ``FINAL_COMPLETED`` requires a
    non-null ``message_id``."""

    def test_complete_raises_notification_blocked_when_message_id_none(self):
        """Force the legacy-fallback path by making hermes_send return
        sent=False, then making ``notify_completed`` (legacy) return
        True. The fallback synthesises message_id=None, which must
        block."""
        self._set_blocking(True)
        os.environ["TELEGRAM_CHAT_ID"] = "5132341473"
        os.environ["TELEGRAM_BOT_TOKEN"] = "dummy"
        m = TaskManager()
        task_id = self._create_running_task(m, "blocking-msg-id-none")
        # hermes send fails (sent=False), legacy notify_completed
        # returns True (synthesises message_id=None).
        fake = _make_fake_hermes_send({"ok": False, "error": "gateway down"})
        with mock.patch("subprocess.run", fake), \
             mock.patch("dispatcher.notifier.notify_completed", return_value=True):
            with self.assertRaises(NotificationBlocked) as ctx:
                m.complete(task_id, output_text="done")
        notif = ctx.exception.args[0]["notification"]
        # Legacy path sent=True but message_id=None.
        self.assertTrue(notif["sent"])
        self.assertIsNone(notif["message_id"])
        reverted = m.get(task_id)
        self.assertEqual(reverted.status, "running")

    def test_message_id_required_final_completed_needs_non_null(self):
        """Under blocking mode, a non-null message_id is REQUIRED
        for ``complete()`` to return normally. ``sent=True`` alone is
        NOT enough."""
        self._set_blocking(True)
        os.environ["TELEGRAM_CHAT_ID"] = "5132341473"
        os.environ["TELEGRAM_BOT_TOKEN"] = "dummy"
        m = TaskManager()
        task_id = self._create_running_task(m, "msg-id-required")
        # hermes send returns ok=True with a real message_id — this
        # should NOT block.
        fake = _make_fake_hermes_send({
            "ok": True,
            "message_id": 9999,
            "mirrored": True,
        })
        with mock.patch("subprocess.run", fake):
            result = m.complete(task_id, output_text="done")
        self.assertEqual(result.status, "completed")
        # Verify the completion state is final_completed.
        self.assertEqual(m.completion_state(task_id), "final_completed")


# ---------------------------------------------------------------------------
# 3. Retry / failure regression (blocking gate does NOT affect fail/timeout)
# ---------------------------------------------------------------------------


class TestBlockingGateFailureRegression(_TempDbMixin, unittest.TestCase):
    """``fail()`` / ``timeout()`` paths must NOT be affected by the
    blocking gate (the gate is wired only into ``complete()``)."""

    def test_fail_does_not_raise_notification_blocked(self):
        self._set_blocking(True)
        os.environ["TELEGRAM_CHAT_ID"] = "5132341473"
        os.environ["TELEGRAM_BOT_TOKEN"] = "dummy"
        m = TaskManager()
        task_id = self._create_running_task(m, "blocking-fail-path")
        # fail() should NOT raise NotificationBlocked even with
        # blocking=true. It may try to send a failure notification,
        # but the blocking gate only applies to complete().
        try:
            m.fail(task_id, error_message="test failure")
        except NotificationBlocked:
            self.fail("fail() must NOT raise NotificationBlocked")
        failed = m.get(task_id)
        self.assertEqual(failed.status, "failed")

    def test_timeout_does_not_raise_notification_blocked(self):
        self._set_blocking(True)
        os.environ["TELEGRAM_CHAT_ID"] = "5132341473"
        os.environ["TELEGRAM_BOT_TOKEN"] = "dummy"
        m = TaskManager()
        task_id = self._create_running_task(m, "blocking-timeout-path")
        try:
            m.timeout(task_id, reason="shadow timeout test")
        except NotificationBlocked:
            self.fail("timeout() must NOT raise NotificationBlocked")
        timed = m.get(task_id)
        self.assertEqual(timed.status, "timeout")


# ---------------------------------------------------------------------------
# 4. Observability-only backward compat (blocking=false)
# ---------------------------------------------------------------------------


class TestBlockingGateObservabilityBackwardCompat(_TempDbMixin, unittest.TestCase):
    """When ``blocking == false`` (the default), ``complete()`` still
    returns ``status='completed'`` even when notification fails. This
    is the observability-only contract from the prior iteration."""

    def test_complete_returns_completed_when_blocking_false_and_notification_fails(self):
        self._set_blocking(False)
        os.environ["TELEGRAM_CHAT_ID"] = "5132341473"
        os.environ["TELEGRAM_BOT_TOKEN"] = "dummy"
        m = TaskManager()
        task_id = self._create_running_task(m, "obs-only-fail")
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            result = m.complete(task_id, output_text="done")
        # Observability-only: status=completed even when notif fails.
        self.assertEqual(result.status, "completed")
        # completion_state should be evidence_completed (notification
        # did not confirm).
        self.assertEqual(m.completion_state(task_id), "evidence_completed")

    def test_complete_returns_completed_when_blocking_false_and_msg_id_present(self):
        self._set_blocking(False)
        os.environ["TELEGRAM_CHAT_ID"] = "5132341473"
        os.environ["TELEGRAM_BOT_TOKEN"] = "dummy"
        m = TaskManager()
        task_id = self._create_running_task(m, "obs-only-success")
        fake = _make_fake_hermes_send({
            "ok": True, "message_id": 8888, "mirrored": True,
        })
        with mock.patch("subprocess.run", fake):
            result = m.complete(task_id, output_text="done")
        self.assertEqual(result.status, "completed")
        self.assertEqual(m.completion_state(task_id), "final_completed")


# ---------------------------------------------------------------------------
# 5. Shadow validation harness (exercises blocking without production config)
# ---------------------------------------------------------------------------


class TestBlockingGateShadowValidation(_TempDbMixin, unittest.TestCase):
    """Self-contained shadow-validation harness that exercises the
    blocking gate end-to-end against a temp SQLite DB with a
    monkey-patched ``hermes send`` subprocess. Proves the gate can be
    validated without touching production config / sending real
    Telegram messages."""

    def test_shadow_validation_blocking_mode_blocks_on_failure(self):
        """Shadow run: blocking=true, hermes send fails → task
        reverts to running, NotificationBlocked raised, audit record
        written, NOTIFICATION_FAILED event emitted."""
        self._set_blocking(True)
        os.environ["TELEGRAM_CHAT_ID"] = "5132341473"
        os.environ["TELEGRAM_BOT_TOKEN"] = "dummy"
        m = TaskManager()
        task_id = self._create_running_task(m, "shadow-block")
        # Stage 1: hermes send fails → blocking gate should fire.
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaises(NotificationBlocked):
                m.complete(task_id, output_text="shadow output")
        # Verify the task is reverted.
        self.assertEqual(m.get(task_id).status, "running")
        # Stage 2: retry with hermes send succeeding → task should
        # now complete normally. This simulates the orchestrator
        # retrying after the notification gate blocked.
        fake = _make_fake_hermes_send({
            "ok": True, "message_id": 7777, "mirrored": True,
        })
        with mock.patch("subprocess.run", fake):
            result = m.complete(task_id, output_text="shadow output retry")
        self.assertEqual(result.status, "completed")
        self.assertEqual(m.completion_state(task_id), "final_completed")

    def test_shadow_validation_audit_log_written(self):
        """The audit log at ``logs/notification_audit.jsonl`` must
        record the notification outcome so it can be independently
        verified."""
        self._set_blocking(True)
        os.environ["TELEGRAM_CHAT_ID"] = "5132341473"
        os.environ["TELEGRAM_BOT_TOKEN"] = "dummy"
        m = TaskManager()
        task_id = self._create_running_task(m, "shadow-audit")
        # hermes send succeeds → audit record written with sent=True.
        fake = _make_fake_hermes_send({
            "ok": True, "message_id": 6666, "mirrored": True,
        })
        with mock.patch("subprocess.run", fake):
            m.complete(task_id, output_text="audit test")
        # The audit log path is <bridge_root>/logs/notification_audit.jsonl.
        from dispatcher.manager import _BRIDGE_ROOT
        audit_path = _BRIDGE_ROOT / "logs" / "notification_audit.jsonl"
        self.assertTrue(audit_path.exists(), "audit log must be created")
        content = audit_path.read_text(encoding="utf-8").strip().splitlines()
        # The last line should be our audit record.
        last = json.loads(content[-1])
        self.assertEqual(last["task_id"], task_id)
        self.assertTrue(last["sent"])
        self.assertEqual(last["message_id"], 6666)


# ---------------------------------------------------------------------------
# 6. Config-defensive behavior
# ---------------------------------------------------------------------------


class TestBlockingGateConfigDefensive(_TempDbMixin, unittest.TestCase):
    """The blocking gate must default to observability-only when the
    config cannot be read (defensive). A malformed config must NEVER
    silently block task completion."""

    def test_complete_defaults_to_observability_when_config_load_raises(self):
        """If ``config.load`` raises, the manager must default to
        ``blocking=False`` and ``complete()`` must NOT raise
        ``NotificationBlocked`` even when notification fails."""
        # Don't call _set_blocking — let config.load raise.
        os.environ["TELEGRAM_CHAT_ID"] = "5132341473"
        os.environ["TELEGRAM_BOT_TOKEN"] = "dummy"
        m = TaskManager()
        task_id = self._create_running_task(m, "config-defensive")
        def boom(name):
            raise RuntimeError("config load boom")
        with mock.patch("config.load", side_effect=boom), \
             mock.patch("subprocess.run", side_effect=FileNotFoundError):
            # Must NOT raise NotificationBlocked — config error
            # defaults to observability-only.
            result = m.complete(task_id, output_text="done")
        self.assertEqual(result.status, "completed")


if __name__ == "__main__":
    unittest.main()