"""Targeted tests for DSH terminal Telegram notification
(TASK-20260825-0009).

Covers the required scenarios:

1. DSH completed  -> generic in-process notifier invoked once
   (manager second-chance path) and ``notification_json`` records the
   confirmed delivery.
2. DSH failed/timeout -> notifier invoked once.
3. Notification failure (urllib transport raises) does NOT alter the
   run's terminal status (best-effort).
4. Duplicate lifecycle reconciliation does NOT send a duplicate
   Telegram for the same terminal run (idempotent via persisted
   ``notification_json``).
5. Secrets not included: the sent message text and the persisted
   result dict contain no token / chat-id values; only
   ``credential_presence`` booleans.

Design: every test runs against a temp SQLite dispatcher DB (no
production ``dispatcher.db`` mutation). The ``hermes send`` subprocess
is monkey-patched to FAIL so the v3 hermes gate does not confirm,
forcing the in-process second chance to run. The legacy
``notify_completed`` / ``notify_failed`` / ``notify_timeout`` are
monkey-patched to return ``False`` (simulating the "legacy disabled"
production state). The in-process ``urllib`` transport is monkey-
patched so no real network call is made. ``TELEGRAM_BOT_TOKEN`` /
``TELEGRAM_CHAT_ID`` are set to fake test values (not real secrets).

These tests do NOT touch the production DB, do NOT send real Telegram
messages, do NOT depend on the Hermes CLI, and do NOT interfere with
the read-only run TASK-20260825-0008.

Run:
    cd /home/box/aee-runtime-bridge && \
    .venv/bin/python -m pytest tests/test_dsh_telegram_terminal_notify.py -v
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import contextmanager
from typing import Any, Dict
from unittest import mock

import dispatcher.db as db_mod
from dispatcher import manager as mgr_mod
from dispatcher.manager import TaskManager


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _failing_hermes_send(error: str = "hermes binary not found"):
    """Fake ``subprocess.run`` that mimics the no-Hermes-CLI failure."""
    def fake_run(argv, *args, **kwargs):
        class _Proc:
            returncode = 127
            stdout = ""
            stderr = error
        return _Proc()
    return fake_run


def _fake_urlopen_ok(message_id: int, captured: Dict[str, Any]):
    """Fake ``urllib.request.urlopen`` returning a Telegram ``ok`` response
    with ``result.message_id``. Captures the request for body inspection."""
    @contextmanager
    def _cm():
        class _Resp:
            def read(self_inner):
                return json.dumps({
                    "ok": True,
                    "result": {"message_id": message_id, "text": "ok"},
                }).encode("utf-8")
        yield _Resp()

    def fake_urlopen(req, timeout=10):
        captured["req"] = req
        captured["url"] = req.full_url
        captured["data"] = req.data
        return _cm()

    return fake_urlopen


def _fake_urlopen_raises(captured: Dict[str, Any]):
    """Fake ``urllib.request.urlopen`` that raises URLError (transport
    failure)."""
    def fake_urlopen(req, timeout=10):
        captured["req"] = req
        raise urllib.error.URLError("transport down")
    return fake_urlopen


def _extract_text(req) -> str:
    """Parse the ``text`` field out of a urlencoded urllib request body."""
    data = req.data
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    parsed = urllib.parse.parse_qs(data or "")
    return (parsed.get("text") or [""])[0]


# ---------------------------------------------------------------------------
# Temp DB mixin (mirrors tests/test_guaranteed_completion_notification.py)
# ---------------------------------------------------------------------------


class _TempDbMixin:
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="aee-dsh-tg-test-")
        self._db_path = os.path.join(self._tmpdir, "dispatcher.db")
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        if not hasattr(db_mod, "_local"):
            db_mod._local = mock.MagicMock()
        self._orig_get_conn = db_mod.get_conn
        self._orig_transaction = db_mod.transaction
        db_mod.get_conn = lambda: self._conn

        @contextmanager
        def _fake_transaction():
            yield self._conn
            self._conn.commit()

        db_mod.transaction = _fake_transaction
        self._orig_mgr_get_conn = mgr_mod.get_conn
        self._orig_mgr_transaction = mgr_mod.transaction
        mgr_mod.get_conn = db_mod.get_conn
        mgr_mod.transaction = db_mod.transaction
        db_mod._init_schema(self._conn)
        # Fake bridge-env Telegram credentials (NOT real secrets).
        os.environ["TELEGRAM_BOT_TOKEN"] = "TEST-TOKEN-DO-NOT-LEAK-123"
        os.environ["TELEGRAM_CHAT_ID"] = "TEST-CHAT-DO-NOT-LEAK-456"

    def tearDown(self) -> None:
        for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            os.environ.pop(k, None)
        db_mod.get_conn = self._orig_get_conn
        db_mod.transaction = self._orig_transaction
        mgr_mod.get_conn = self._orig_mgr_get_conn
        mgr_mod.transaction = self._orig_mgr_transaction
        self._conn.close()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _create_running_task(self, m: TaskManager, title: str = "dsh-headless:T") -> str:
        t = m.create(
            title=title,
            type="ops",
            input_text="do something",
            mode="normal",
        )
        m.start(t.task_id, hermes_run_id="run-" + t.task_id)
        return t.task_id

    def _notification_json(self, m: TaskManager, task_id: str) -> Dict[str, Any]:
        out = m.get_output(task_id)
        return json.loads(out["notification_json"])

    def _patches(self, urlopen_fake):
        """Context manager stack: hermes fails, legacy disabled, urllib mocked."""
        return (
            mock.patch("dispatcher.notifier.subprocess.run",
                       side_effect=_failing_hermes_send()),
            mock.patch("dispatcher.notifier.notify_completed", return_value=False),
            mock.patch("dispatcher.notifier.notify_failed", return_value=False),
            mock.patch("dispatcher.notifier.notify_timeout", return_value=False),
            mock.patch("dispatcher.notifier.notify_cancelled", return_value=False),
            mock.patch("dispatcher.notifier.urllib.request.urlopen",
                       side_effect=urlopen_fake),
        )


# ---------------------------------------------------------------------------
# 1 + 2. completed / failed / timeout -> notifier invoked once
# ---------------------------------------------------------------------------


class TestDshTerminalNotifiesOnce(_TempDbMixin, unittest.TestCase):

    def test_completed_notifies_once_inprocess(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "dsh-headless:DSH-OK")
        captured: Dict[str, Any] = {}
        p1, p2, p3, p4, p5, p6 = self._patches(_fake_urlopen_ok(4242, captured))
        with p1, p2, p3, p4, p5, p6 as mock_urlopen:
            task = m.complete(task_id, output_text="done")
        self.assertEqual(task.status, "completed")
        notif = self._notification_json(m, task_id)
        self.assertTrue(notif["sent"])
        self.assertEqual(notif.get("method"), "inprocess_urllib")
        self.assertEqual(notif.get("message_id"), 4242)
        # The in-process transport was invoked exactly once.
        self.assertEqual(mock_urlopen.call_count, 1)

    def test_failed_notifies_once_inprocess(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "dsh-headless:DSH-FAIL")
        captured: Dict[str, Any] = {}
        p1, p2, p3, p4, p5, p6 = self._patches(_fake_urlopen_ok(5151, captured))
        with p1, p2, p3, p4, p5, p6 as mock_urlopen:
            task = m.fail(task_id, error_message="dsh-headless: failed exit=2")
        self.assertEqual(task.status, "failed")
        notif = self._notification_json(m, task_id)
        self.assertTrue(notif["sent"])
        self.assertEqual(notif.get("method"), "inprocess_urllib")
        self.assertEqual(notif.get("message_id"), 5151)
        self.assertEqual(mock_urlopen.call_count, 1)

    def test_timeout_notifies_once_inprocess(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "dsh-headless:DSH-TIMEOUT")
        captured: Dict[str, Any] = {}
        p1, p2, p3, p4, p5, p6 = self._patches(_fake_urlopen_ok(6262, captured))
        with p1, p2, p3, p4, p5, p6 as mock_urlopen:
            task = m.timeout(task_id, reason="dsh-headless timeout after 1800s")
        self.assertEqual(task.status, "timeout")
        notif = self._notification_json(m, task_id)
        self.assertTrue(notif["sent"])
        self.assertEqual(notif.get("method"), "inprocess_urllib")
        self.assertEqual(notif.get("message_id"), 6262)
        self.assertEqual(mock_urlopen.call_count, 1)


# ---------------------------------------------------------------------------
# 3. notification failure preserves terminal status (best-effort)
# ---------------------------------------------------------------------------


class TestNotificationFailurePreservesStatus(_TempDbMixin, unittest.TestCase):

    def test_transport_failure_keeps_completed(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "dsh-headless:DSH-OK-FAIL")
        captured: Dict[str, Any] = {}
        p1, p2, p3, p4, p5, p6 = self._patches(_fake_urlopen_raises(captured))
        with p1, p2, p3, p4, p5, p6:
            task = m.complete(task_id, output_text="done")
        # The run is STILL completed even though the Telegram transport failed.
        self.assertEqual(task.status, "completed")
        notif = self._notification_json(m, task_id)
        self.assertFalse(notif["sent"])
        # The in-process attempt was made (best-effort) but did not confirm.
        self.assertIsNone(notif.get("message_id"))

    def test_transport_failure_keeps_failed(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "dsh-headless:DSH-FAIL-FAIL")
        captured: Dict[str, Any] = {}
        p1, p2, p3, p4, p5, p6 = self._patches(_fake_urlopen_raises(captured))
        with p1, p2, p3, p4, p5, p6:
            task = m.fail(task_id, error_message="boom")
        self.assertEqual(task.status, "failed")
        notif = self._notification_json(m, task_id)
        self.assertFalse(notif["sent"])


# ---------------------------------------------------------------------------
# 4. duplicate lifecycle reconciliation -> no duplicate Telegram
# ---------------------------------------------------------------------------


class TestReconcileIdempotency(_TempDbMixin, unittest.TestCase):

    def test_duplicate_reconcile_no_duplicate_telegram(self):
        """Watcher preempts to timeout (delivers a timeout Telegram),
        then the executor reconciles to completed twice. The operator
        must NOT receive a duplicate Telegram for the same terminal
        run: the prior confirmed delivery (sent=True + message_id)
        blocks the reconcile fire, so the in-process transport is
        invoked exactly once (for the watcher timeout)."""
        m = TaskManager()
        task = m.create(
            title="dsh-headless:DSH-RECONCILE",
            type="ops", input_text="x", mode="normal",
            initial_status="queued",
        )
        task_id = task.task_id
        placeholder = f"dsh-headless-pending-{task_id}"
        m.start(task_id, placeholder)

        captured: Dict[str, Any] = {}
        p1, p2, p3, p4, p5, p6 = self._patches(_fake_urlopen_ok(7777, captured))
        with p1, p2, p3, p4, p5, p6 as mock_urlopen:
            # Watcher preemption -> timeout (fires _notify_terminal once,
            # in-process delivers message_id=7777).
            m.timeout(task_id, "watcher preempted placeholder")
            # Executor force-reconcile to completed (prior is a confirmed
            # delivery -> idempotency guard skips the fire).
            m.reconcile_executor_completion(
                task_id, run_id="dsh-real-run-001",
                status="completed", output_text="done", exit_code=0,
            )
            # A duplicate reconcile must also skip.
            m.reconcile_executor_completion(
                task_id, run_id="dsh-real-run-001",
                status="completed", output_text="done", exit_code=0,
            )
        # The in-process transport was invoked EXACTLY ONCE.
        self.assertEqual(mock_urlopen.call_count, 1)
        # The persisted notification record is the watcher-timeout one
        # (confirmed delivery), not overwritten by the reconciles.
        notif = self._notification_json(m, task_id)
        self.assertTrue(notif["sent"])
        self.assertEqual(notif.get("message_id"), 7777)
        # And the task row ended up completed (reconcile did its job).
        self.assertEqual(m.get(task_id).status, "completed")

    def test_reconcile_fires_when_prior_attempt_not_delivered(self):
        """When the watcher's prior notification did NOT deliver
        (sent=False, e.g. no credentials at the time), a reconcile to
        a corrected verdict fires the corrected notification — this is
        NOT a duplicate delivery because nothing was delivered before."""
        m = TaskManager()
        task = m.create(
            title="dsh-headless:DSH-RECONCILE-NOCRED",
            type="ops", input_text="x", mode="normal",
            initial_status="queued",
        )
        task_id = task.task_id
        m.start(task_id, f"dsh-headless-pending-{task_id}")

        # Phase 1: watcher timeout with NO credentials -> prior attempt
        # is sent=False (not delivered).
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        os.environ.pop("TELEGRAM_CHAT_ID", None)
        captured1: Dict[str, Any] = {}
        p1, p2, p3, p4, p5, p6 = self._patches(_fake_urlopen_ok(1111, captured1))
        with p1, p2, p3, p4, p5, p6 as mock_urlopen1:
            m.timeout(task_id, "watcher preempted")
        self.assertEqual(mock_urlopen1.call_count, 0)  # no creds -> no send attempted
        notif0 = self._notification_json(m, task_id)
        self.assertFalse(notif0["sent"])

        # Phase 2: credentials become present (operator provisioned them),
        # executor reconciles to completed -> corrected notification fires.
        os.environ["TELEGRAM_BOT_TOKEN"] = "TEST-TOKEN-DO-NOT-LEAK-123"
        os.environ["TELEGRAM_CHAT_ID"] = "TEST-CHAT-DO-NOT-LEAK-456"
        captured2: Dict[str, Any] = {}
        q1, q2, q3, q4, q5, q6 = self._patches(_fake_urlopen_ok(2222, captured2))
        with q1, q2, q3, q4, q5, q6 as mock_urlopen2:
            m.reconcile_executor_completion(
                task_id, run_id="dsh-real-run-002",
                status="completed", output_text="done", exit_code=0,
            )
        # The corrected-verdict notification was sent exactly once.
        self.assertEqual(mock_urlopen2.call_count, 1)
        notif1 = self._notification_json(m, task_id)
        self.assertTrue(notif1["sent"])
        self.assertEqual(notif1.get("message_id"), 2222)
        self.assertEqual(notif1.get("status"), "completed")


# ---------------------------------------------------------------------------
# 5. secrets not included
# ---------------------------------------------------------------------------


class TestSecretsNotIncluded(_TempDbMixin, unittest.TestCase):

    def test_body_and_record_have_no_credentials(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "dsh-headless:DSH-SECRET")
        captured: Dict[str, Any] = {}
        p1, p2, p3, p4, p5, p6 = self._patches(_fake_urlopen_ok(9999, captured))
        with p1, p2, p3, p4, p5, p6:
            task = m.complete(task_id, output_text="done")
        self.assertEqual(task.status, "completed")

        token_val = "TEST-TOKEN-DO-NOT-LEAK-123"
        chat_val = "TEST-CHAT-DO-NOT-LEAK-456"

        # (a) The persisted notification_json blob has no credential values.
        out = m.get_output(task_id)
        blob_str = out["notification_json"]
        self.assertNotIn(token_val, blob_str)
        self.assertNotIn(chat_val, blob_str)
        notif = json.loads(blob_str)
        # Only presence booleans are surfaced.
        self.assertEqual(
            set(notif.get("credential_presence", {}).keys()),
            {"bot_token", "chat_id"},
        )
        for v in notif.get("credential_presence", {}).values():
            self.assertIsInstance(v, bool)
        # recipient is redacted, not the chat id.
        self.assertNotIn(chat_val, str(notif.get("recipient")))

        # (b) The sent message TEXT has no credential values.
        text = _extract_text(captured["req"])
        self.assertNotIn(token_val, text)
        self.assertNotIn(chat_val, text)

        # (c) The body carries the required concise, secret-free fields.
        self.assertIn(task_id, text)
        self.assertIn("completed", text)
        self.assertIn("dsh-headless", text)  # executor label
        self.assertIn("UTC", text)
        self.assertIn("Taipei", text)


if __name__ == "__main__":
    unittest.main()