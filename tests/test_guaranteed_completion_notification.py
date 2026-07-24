"""Guaranteed Completion Notification — targeted deterministic tests.

Covers the 7 required scenarios from the work order:

  a. PASS (completed) sends/attempts notification
  b. missing_expected_artifacts FAIL sends/attempts notification
  c. rescue failure still notifies
  d. CANCELLED notifies
  e. TIMEOUT notifies
  f. Telegram transport failure preserves original status and
     records notification failure
  g. idempotency / duplicate terminal handling

Design: every test runs against a temp SQLite DB (no production
dispatcher.db mutation). The ``hermes send`` subprocess call is
monkey-patched so no real Telegram message is sent. The
``TELEGRAM_CHAT_ID`` env var is set in every test so the gate
goes through the gateway path (not the early-return-no-chat_id
branch) unless a test explicitly clears it.

Run:
    cd /home/ubuntu/hermes-runtime-bridge && \
    python3 -m unittest tests.test_guaranteed_completion_notification -v
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from typing import Any, Dict, List
from unittest import mock

import dispatcher.db as db_mod
from dispatcher import ids, manager as mgr_mod
from dispatcher.manager import TaskManager
from aee.observability import EventKind


def _make_fake_hermes_send(result: Dict[str, Any]):
    """Build a fake ``subprocess.run`` that returns a JSON payload
    mimicking ``hermes send --json``."""
    def fake_run(argv, *args, **kwargs):
        class _Proc:
            returncode = 0
            stdout = json.dumps(result)
            stderr = ""
        return _Proc()
    return fake_run


def _make_failing_hermes_send(error: str = "gateway down"):
    def fake_run(argv, *args, **kwargs):
        class _Proc:
            returncode = 1
            stdout = ""
            stderr = error
        return _Proc()
    return fake_run


class _TempDbMixin:
    """Spin up a temp SQLite dispatcher DB for each test."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="aee-gcn-test-")
        self._db_path = os.path.join(self._tmpdir, "dispatcher.db")
        # Patch the module-level get_conn / transaction to use a
        # fresh sqlite3 connection backed by the temp DB. The
        # connection is thread-local-ish (one connection, no
        # thread check) which is enough for the unittest model.
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        # Patch db_mod._local to use our connection.
        if not hasattr(db_mod, "_local"):
            db_mod._local = mock.MagicMock()
        # Save originals.
        self._orig_get_conn = db_mod.get_conn
        self._orig_transaction = db_mod.transaction
        db_mod.get_conn = lambda: self._conn
        # transaction() returns a context manager that yields the
        # connection and commits on exit. Our connection is a raw
        # sqlite3.Connection; mimic the transaction behavior.
        from contextlib import contextmanager

        @contextmanager
        def _fake_transaction():
            yield self._conn
            self._conn.commit()

        db_mod.transaction = _fake_transaction
        # Also patch mgr_mod's references (they import get_conn /
        # transaction at module level in some helpers).
        self._orig_mgr_get_conn = mgr_mod.get_conn
        self._orig_mgr_transaction = mgr_mod.transaction
        mgr_mod.get_conn = db_mod.get_conn
        mgr_mod.transaction = db_mod.transaction
        # Initialize schema.
        db_mod._init_schema(self._conn)
        # Set TELEGRAM_CHAT_ID for the gateway path.
        os.environ["TELEGRAM_CHAT_ID"] = "99999"

    def tearDown(self) -> None:
        import shutil
        os.environ.pop("TELEGRAM_CHAT_ID", None)
        db_mod.get_conn = self._orig_get_conn
        db_mod.transaction = self._orig_transaction
        mgr_mod.get_conn = self._orig_mgr_get_conn
        mgr_mod.transaction = self._orig_mgr_transaction
        self._conn.close()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _create_running_task(self, m: TaskManager, title: str = "T") -> str:
        t = m.create(
            title=title,
            type="coding",
            input_text="do something",
            mode="coding",
        )
        m.start(t.task_id, hermes_run_id="run-" + t.task_id)
        return t.task_id


class TestPassNotifies(_TempDbMixin, unittest.TestCase):
    """(a) PASS sends/attempts notification."""

    def test_complete_sends_notification(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "P-OK")
        fake = _make_fake_hermes_send({"success": True, "message_id": 100})
        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake):
            task = m.complete(task_id, output_text="done")
        self.assertEqual(task.status, "completed")
        events = m.events(task_id)
        kinds = [e.kind for e in events]
        self.assertIn(EventKind.COMPLETED, kinds)
        self.assertIn(EventKind.NOTIFICATION_COMPLETED, kinds)
        out = m.get_output(task_id)
        notif = json.loads(out["notification_json"])
        self.assertTrue(notif["sent"])
        self.assertEqual(notif["message_id"], 100)
        self.assertEqual(notif["method"], "hermes_send")


class TestMissingExpectedArtifactsNotifies(_TempDbMixin, unittest.TestCase):
    """(b) missing_expected_artifacts FAIL sends/attempts
    notification WITHOUT needing a report artifact."""

    def test_missing_artifact_fail_notifies(self):
        m = TaskManager()
        # Create a task with a declared expected artifact that
        # does NOT exist on disk.
        fake_path = "/tmp/does-not-exist-gcn-test-12345.md"
        t = m.create(
            title="M-Missing",
            type="coding",
            input_text="produce a file",
            mode="coding",
            expected_artifacts=[fake_path],
            max_rescues=0,  # skip rescue so we go straight to fail
        )
        m.start(t.task_id, hermes_run_id="run-" + t.task_id)
        fake = _make_fake_hermes_send({"success": True, "message_id": 200})
        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake):
            task = m.complete(t.task_id, output_text="done")
        # The completion gate fires missing_expected_artifacts →
        # fail().
        self.assertEqual(task.status, "failed")
        self.assertIn("missing_expected_artifacts", task.error_message or "")
        events = m.events(t.task_id)
        kinds = [e.kind for e in events]
        self.assertIn(EventKind.FAILED, kinds)
        # Notification MUST be attempted even though the artifact
        # is missing and the completion gate failed.
        self.assertIn(EventKind.NOTIFICATION_COMPLETED, kinds)
        out = m.get_output(t.task_id)
        notif = json.loads(out["notification_json"])
        self.assertTrue(notif["sent"])
        self.assertEqual(notif["message_id"], 200)


class TestRescueFailureNotifies(_TempDbMixin, unittest.TestCase):
    """(c) rescue failure still notifies."""

    def test_rescue_failure_still_notifies(self):
        m = TaskManager()
        fake_path = "/tmp/does-not-exist-gcn-rescue-98765.md"
        t = m.create(
            title="R-RescueFail",
            type="coding",
            input_text="produce a file",
            mode="coding",
            expected_artifacts=[fake_path],
            max_rescues=1,  # one rescue attempt, then fail
        )
        m.start(t.task_id, hermes_run_id="run-" + t.task_id)
        fake = _make_fake_hermes_send({"success": True, "message_id": 300})
        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake):
            task = m.complete(t.task_id, output_text="done")
        # Rescue runs (max_rescues=1), artifact still missing →
        # fail() with missing_expected_artifacts_post_rescue.
        self.assertEqual(task.status, "failed")
        self.assertIn(
            "missing_expected_artifacts",
            task.error_message or "",
        )
        events = m.events(t.task_id)
        kinds = [e.kind for e in events]
        self.assertIn(EventKind.FAILED, kinds)
        # Notification MUST fire on the rescue-failure path.
        self.assertIn(EventKind.NOTIFICATION_COMPLETED, kinds)
        out = m.get_output(t.task_id)
        notif = json.loads(out["notification_json"])
        self.assertTrue(notif["sent"])
        self.assertEqual(notif["message_id"], 300)


class TestCancelledNotifies(_TempDbMixin, unittest.TestCase):
    """(d) CANCELLED notifies."""

    def test_cancel_notifies(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "C-Notif")
        fake = _make_fake_hermes_send({"success": True, "message_id": 400})
        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake):
            task = m.cancel(task_id)
        self.assertEqual(task.status, "cancelled")
        events = m.events(task_id)
        kinds = [e.kind for e in events]
        self.assertIn(EventKind.CANCELLED, kinds)
        self.assertIn(EventKind.NOTIFICATION_COMPLETED, kinds)
        out = m.get_output(task_id)
        notif = json.loads(out["notification_json"])
        self.assertTrue(notif["sent"])
        self.assertEqual(notif["message_id"], 400)


class TestTimeoutNotifies(_TempDbMixin, unittest.TestCase):
    """(e) TIMEOUT notifies."""

    def test_timeout_notifies(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "T-Notif")
        fake = _make_fake_hermes_send({"success": True, "message_id": 500})
        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake):
            task = m.timeout(task_id, reason="ran too long")
        self.assertEqual(task.status, "timeout")
        events = m.events(task_id)
        kinds = [e.kind for e in events]
        self.assertIn(EventKind.TIMEOUT, kinds)
        self.assertIn(EventKind.NOTIFICATION_COMPLETED, kinds)
        out = m.get_output(task_id)
        notif = json.loads(out["notification_json"])
        self.assertTrue(notif["sent"])
        self.assertEqual(notif["message_id"], 500)


class TestTransportFailurePreservesStatus(_TempDbMixin, unittest.TestCase):
    """(f) Telegram transport failure preserves the original
    task outcome and records the notification failure."""

    def test_complete_transport_failure_preserves_completed(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "F-Transport")
        with mock.patch(
            "dispatcher.notifier.subprocess.run",
            side_effect=_make_failing_hermes_send("gateway down"),
        ):
            with mock.patch(
                "dispatcher.notifier.notify_completed", return_value=False
            ):
                task = m.complete(task_id, output_text="done")
        # Status is STILL completed even though the notification
        # transport failed.
        self.assertEqual(task.status, "completed")
        events = m.events(task_id)
        kinds = [e.kind for e in events]
        self.assertIn(EventKind.NOTIFICATION_FAILED, kinds)
        out = m.get_output(task_id)
        notif = json.loads(out["notification_json"])
        self.assertFalse(notif["sent"])
        self.assertEqual(notif["method"], "failed")
        self.assertIsNotNone(notif["last_error"])

    def test_fail_transport_failure_preserves_failed(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "F-TransportFail")
        with mock.patch(
            "dispatcher.notifier.subprocess.run",
            side_effect=_make_failing_hermes_send("gateway down"),
        ):
            with mock.patch(
                "dispatcher.notifier.notify_failed", return_value=False
            ):
                task = m.fail(task_id, error_message="boom")
        # Status is STILL failed.
        self.assertEqual(task.status, "failed")
        events = m.events(task_id)
        kinds = [e.kind for e in events]
        self.assertIn(EventKind.NOTIFICATION_FAILED, kinds)
        out = m.get_output(task_id)
        notif = json.loads(out["notification_json"])
        self.assertFalse(notif["sent"])

    def test_cancel_transport_failure_preserves_cancelled(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "C-TransportFail")
        with mock.patch(
            "dispatcher.notifier.subprocess.run",
            side_effect=_make_failing_hermes_send("gateway down"),
        ):
            with mock.patch(
                "dispatcher.notifier.notify_cancelled", return_value=False
            ):
                task = m.cancel(task_id)
        self.assertEqual(task.status, "cancelled")
        events = m.events(task_id)
        kinds = [e.kind for e in events]
        self.assertIn(EventKind.NOTIFICATION_FAILED, kinds)

    def test_timeout_transport_failure_preserves_timeout(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "T-TransportFail")
        with mock.patch(
            "dispatcher.notifier.subprocess.run",
            side_effect=_make_failing_hermes_send("gateway down"),
        ):
            with mock.patch(
                "dispatcher.notifier.notify_timeout", return_value=False
            ):
                task = m.timeout(task_id, reason="slow")
        self.assertEqual(task.status, "timeout")
        events = m.events(task_id)
        kinds = [e.kind for e in events]
        self.assertIn(EventKind.NOTIFICATION_FAILED, kinds)


class TestIdempotency(_TempDbMixin, unittest.TestCase):
    """(g) idempotency / duplicate terminal handling.

    The dedup boundary is the ``is_legal_transition`` guard at
    the top of each terminal method. Once a task is in a
    terminal status, the next call to fail/complete/timeout/
    cancel raises ``IllegalTransition`` BEFORE reaching
    ``_notify_terminal``, so the notification is fired exactly
    once per terminal transition.
    """

    def test_double_fail_raises_illegal_transition_no_second_notification(self):
        from dispatcher.manager import IllegalTransition
        m = TaskManager()
        task_id = self._create_running_task(m, "I-DoubleFail")
        send_count = {"n": 0}

        def counting_fake(argv, *a, **kw):
            send_count["n"] += 1
            class _P:
                returncode = 0
                stdout = json.dumps({"success": True, "message_id": 700})
                stderr = ""
            return _P()

        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=counting_fake):
            task = m.fail(task_id, error_message="first fail")
            self.assertEqual(task.status, "failed")
            # Second fail raises IllegalTransition.
            with self.assertRaises(IllegalTransition):
                m.fail(task_id, error_message="second fail")
        # The hermes send subprocess was called EXACTLY ONCE
        # (the second fail never reached the gate).
        self.assertEqual(send_count["n"], 1)

    def test_complete_after_fail_raises_illegal_transition(self):
        from dispatcher.manager import IllegalTransition
        m = TaskManager()
        task_id = self._create_running_task(m, "I-CompleteAfterFail")
        with mock.patch(
            "dispatcher.notifier.subprocess.run",
            side_effect=_make_fake_hermes_send({"success": True, "message_id": 800}),
        ):
            m.fail(task_id, error_message="fail first")
            with self.assertRaises(IllegalTransition):
                m.complete(task_id, output_text="late")
            with self.assertRaises(IllegalTransition):
                m.cancel(task_id)
            with self.assertRaises(IllegalTransition):
                m.timeout(task_id, reason="late")


class TestBackwardCompatAliases(unittest.TestCase):
    """The backward-compat aliases
    (notify_completed_hermes_gateway, notify_completed_with_fallback)
    must still work and route through the generalized gate."""

    def test_aliases_exist_and_route(self):
        from dispatcher.notifier import (
            notify_completed_hermes_gateway,
            notify_completed_with_fallback,
            notify_terminal_hermes_gateway,
            notify_terminal_with_fallback,
        )
        self.assertTrue(callable(notify_completed_hermes_gateway))
        self.assertTrue(callable(notify_completed_with_fallback))
        # The alias must produce the same shape as the generalized
        # function for status="completed". We can call both with
        # no chat_id and compare the last_error.
        os.environ.pop("TELEGRAM_CHAT_ID", None)
        r1 = notify_completed_hermes_gateway("alias-test-1")
        r2 = notify_terminal_hermes_gateway("alias-test-1", "completed")
        self.assertEqual(r1.get("last_error"), r2.get("last_error"))
        self.assertEqual(r1.get("method"), r2.get("method"))
        r3 = notify_completed_with_fallback("alias-test-2")
        r4 = notify_terminal_with_fallback("alias-test-2", "completed")
        self.assertEqual(r3.get("method"), r4.get("method"))


if __name__ == "__main__":
    unittest.main()