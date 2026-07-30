"""AEE v3.1 Ghost-Task Notification Fix — regression tests.

These tests cover the verified root cause documented in
``reports/aee_ghost_task_notification_fix.md``:

* Synthetic test-fixture tasks (e.g. title="aee6-success",
  hermes_run_id="run-success") created by
  ``aee/tests/test_artifacts_integration.py`` were leaking real
  Telegram notifications to the production chat because
  ``notify_terminal_with_fallback`` unconditionally fires a
  ``hermes send`` subprocess whenever ``TELEGRAM_CHAT_ID`` is set
  in the environment.

The fix adds a pre-send consistency guard (``_is_fixture_task``)
that detects fixture sentinels via ``aee.reporting.identity`` and
suppresses the notification (both the Hermes-gateway path and the
legacy fallback) before any subprocess or HTTP call.

These tests use a tempdir-backed dispatcher DB (via
``tests/_live_db_guard.point_module_to_temp_db``) and mock the
``hermes`` subprocess so no real Telegram send occurs.  They are
stdlib ``unittest`` only — no pytest, no network.

Scope
-----
1. ``test_fixture_task_suppressed_before_send`` — a task with a
   fixture-sentinel ``hermes_run_id`` (``run-success``) is
   suppressed: ``notify_terminal_with_fallback`` returns
   ``method="fixture_suppressed"``, ``sent=False``, and the
   mock subprocess is never invoked.
2. ``test_fixture_title_suppressed`` — a task with a fixture
   title (``aee6-symlink``) is suppressed even when the
   ``hermes_run_id`` is not a sentinel.
3. ``test_legitimate_task_not_suppressed`` — a task with a
   non-sentinel ``hermes_run_id`` and a non-fixture title
   passes through to the gateway path (subprocess invoked).
4. ``test_fixture_suppression_persists_notification_json`` —
   the suppression result is persisted into
   ``task_outputs.notification_json`` by
   ``TaskManager._notify_terminal`` so the orchestrator can
   see the fixture_suppressed verdict.
5. ``test_missing_task_row_fails_open`` — when the task row is
   missing (already deleted), ``_is_fixture_task`` returns
   ``None`` and the normal gate path fires (the formatter
   produces the minimal fallback body).

Run with:
    .venv/bin/python -m unittest tests.test_ghost_task_notification_fix -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests._live_db_guard import make_temp_dispatcher_db, point_module_to_temp_db


class _FixtureSuppressionBase(unittest.TestCase):
    """Common setUp/tearDown: tempdir DB + mock subprocess."""

    def setUp(self) -> None:
        self._stack = []
        self._tmpctx = make_temp_dispatcher_db(copy_production_schema=True)
        self._db_path = self._tmpctx.__enter__()
        self._stack.append(self._tmpctx)
        self._ptctx = point_module_to_temp_db(self._db_path)
        self._db_mod, self._mgr_mod = self._ptctx.__enter__()
        self._stack.append(self._ptctx)
        # Patch the subprocess.run used by the notifier so no
        # real ``hermes send`` fires.
        self._subproc_patch = patch("dispatcher.notifier.subprocess.run")
        self._mock_subproc = self._subproc_patch.start()
        self._stack.append(self._subproc_patch)
        # Ensure TELEGRAM_CHAT_ID is set so the gateway path is
        # exercised (the guard runs before the env check).
        os.environ["TELEGRAM_CHAT_ID"] = "999999999"

    def tearDown(self) -> None:
        os.environ.pop("TELEGRAM_CHAT_ID", None)
        for ctx in reversed(self._stack):
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass

    def _create_task(self, *, title, hermes_run_id, status="completed"):
        """Create and start a task so it can be completed/timed-out."""
        from dispatcher.manager import TaskManager
        m = TaskManager()
        t = m.create(title=title, type="research", input_text="test")
        m.start(t.task_id, hermes_run_id=hermes_run_id)
        return t, m

    def _make_subproc_return(self, *, ok=True, message_id=None):
        """Configure the mock subprocess to return a gateway
        success/failure shape."""
        proc = MagicMock()
        proc.returncode = 0
        if ok:
            payload = {"success": True, "message_id": message_id or 12345}
        else:
            payload = {"success": False, "error": "gateway down"}
        proc.stdout = json.dumps(payload)
        proc.stderr = ""
        self._mock_subproc.return_value = proc

    def _hermes_send_call_count(self) -> int:
        """Count how many subprocess calls were ``hermes send``
        invocations (as opposed to ``git rev-parse`` or other
        shells-out calls made during task creation)."""
        count = 0
        for call in self._mock_subproc.call_args_list:
            args = call[0]
            if args and isinstance(args[0], list):
                cmd = args[0]
                if len(cmd) >= 2 and cmd[0] == "hermes" and cmd[1] == "send":
                    count += 1
        return count


class TestFixtureSuppression(_FixtureSuppressionBase):

    def test_fixture_task_suppressed_before_send(self):
        """A task with sentinel hermes_run_id='run-success' is
        suppressed — the mock subprocess is NEVER called."""
        self._make_subproc_return(ok=True, message_id=99999)
        t, m = self._create_task(
            title="aee6-success",
            hermes_run_id="run-success",
        )
        m.complete(t.task_id, output_text="done")
        # The ``hermes send`` subprocess must not have been
        # invoked — other subprocess calls (git rev-parse during
        # task creation) are expected and are filtered out.
        self.assertEqual(
            self._hermes_send_call_count(), 0,
            "hermes send subprocess was called for a fixture task — "
            "the pre-send guard did not suppress it",
        )
        # The notification_json must record the suppression.
        out = m.get_output(t.task_id)
        notif = json.loads(out["notification_json"])
        self.assertFalse(notif["sent"])
        self.assertEqual(notif["method"], "fixture_suppressed")
        self.assertIn("fixture", notif["last_error"])

    def test_fixture_title_suppressed(self):
        """A task with a fixture title 'aee6-symlink' is suppressed
        even when hermes_run_id is not a sentinel."""
        self._make_subproc_return(ok=True, message_id=88888)
        t, m = self._create_task(
            title="aee6-symlink",
            hermes_run_id="some-legit-run-id",
        )
        m.complete(t.task_id, output_text="done")
        self.assertEqual(self._hermes_send_call_count(), 0)
        out = m.get_output(t.task_id)
        notif = json.loads(out["notification_json"])
        self.assertFalse(notif["sent"])
        self.assertEqual(notif["method"], "fixture_suppressed")

    def test_legitimate_task_not_suppressed(self):
        """A task with a non-sentinel hermes_run_id and non-fixture
        title passes through to the gateway path (subprocess
        invoked)."""
        self._make_subproc_return(ok=True, message_id=77777)
        t, m = self._create_task(
            title="Real production task",
            hermes_run_id="run_abc123def456789012345678901234abcd",
        )
        m.complete(t.task_id, output_text="done")
        # The ``hermes send`` subprocess must have been called
        # (the guard did not suppress).
        self.assertGreater(
            self._hermes_send_call_count(), 0,
            "hermes send subprocess was NOT called for a legitimate task — "
            "the pre-send guard over-suppressed it",
        )
        out = m.get_output(t.task_id)
        notif = json.loads(out["notification_json"])
        self.assertTrue(notif["sent"])
        self.assertEqual(notif["method"], "hermes_send")
        self.assertEqual(str(notif["message_id"]), "77777")

    def test_timeout_fixture_suppressed(self):
        """A timed-out fixture task (aee6-timeout / run-timeout) is
        also suppressed — covers the screenshot case."""
        self._make_subproc_return(ok=True, message_id=66666)
        t, m = self._create_task(
            title="aee6-timeout",
            hermes_run_id="run-timeout",
        )
        m.timeout(t.task_id, "no progress")
        self.assertEqual(self._hermes_send_call_count(), 0)
        out = m.get_output(t.task_id)
        notif = json.loads(out["notification_json"])
        self.assertFalse(notif["sent"])
        self.assertEqual(notif["method"], "fixture_suppressed")

    def test_fixture_suppression_persists_notification_json(self):
        """The suppression result is persisted into
        task_outputs.notification_json so the orchestrator can
        see the fixture_suppressed verdict."""
        self._make_subproc_return(ok=True, message_id=55555)
        t, m = self._create_task(
            title="aee6-success",
            hermes_run_id="run-success",
        )
        m.complete(t.task_id, output_text="done")
        out = m.get_output(t.task_id)
        self.assertIsNotNone(out["notification_json"])
        notif = json.loads(out["notification_json"])
        self.assertIn("sent", notif)
        self.assertIn("method", notif)
        self.assertIn("last_error", notif)
        self.assertIn("ts_utc", notif)
        self.assertIn("ts_taipei", notif)

    def test_missing_task_row_fails_open(self):
        """When the task row is missing (already deleted),
        _is_fixture_task returns None and the normal gate path
        fires.  This proves the guard fails open rather than
        blocking legitimate notifications."""
        self._make_subproc_return(ok=True, message_id=44444)
        from dispatcher.notifier import notify_terminal_with_fallback
        # Call with a task_id that does not exist in the DB.
        result = notify_terminal_with_fallback(
            "TASK-DOES-NOT-EXIST-9999", "completed",
        )
        # The guard failed open — the ``hermes send`` subprocess
        # was called.
        self.assertGreater(self._hermes_send_call_count(), 0)
        # The result is NOT fixture_suppressed.
        self.assertNotEqual(result.get("method"), "fixture_suppressed")


class TestIsFixtureTaskUnit(unittest.TestCase):
    """Unit tests for ``_is_fixture_task`` in isolation."""

    def setUp(self):
        self._stack = []
        self._tmpctx = make_temp_dispatcher_db(copy_production_schema=True)
        self._db_path = self._tmpctx.__enter__()
        self._stack.append(self._tmpctx)
        self._ptctx = point_module_to_temp_db(self._db_path)
        self._db_mod, self._mgr_mod = self._ptctx.__enter__()
        self._stack.append(self._ptctx)

    def tearDown(self):
        for ctx in reversed(self._stack):
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass

    def test_returns_none_for_missing_task(self):
        from dispatcher.notifier import _is_fixture_task
        self.assertIsNone(_is_fixture_task("TASK-MISSING"))

    def test_returns_fixture_info_for_sentinel_run_id(self):
        from dispatcher.manager import TaskManager
        from dispatcher.notifier import _is_fixture_task
        m = TaskManager()
        t = m.create(title="any", type="research", input_text="x")
        m.start(t.task_id, hermes_run_id="run-success")
        info = _is_fixture_task(t.task_id)
        self.assertIsNotNone(info)
        # Fixture markers are formatted strings like
        # "sentinel_hermes_run_id:'run-success'" — check that
        # the sentinel value appears in one of the markers.
        self.assertTrue(
            any("run-success" in marker for marker in info.get("fixture_markers", [])),
            f"run-success not found in markers {info.get('fixture_markers')}",
        )

    def test_returns_none_for_legitimate_run_id(self):
        from dispatcher.manager import TaskManager
        from dispatcher.notifier import _is_fixture_task
        m = TaskManager()
        t = m.create(title="Real task", type="research", input_text="x")
        m.start(t.task_id, hermes_run_id="run_abc123def456789012345678901234abcd")
        info = _is_fixture_task(t.task_id)
        self.assertIsNone(info)

    def test_returns_fixture_info_for_fixture_title(self):
        from dispatcher.manager import TaskManager
        from dispatcher.notifier import _is_fixture_task
        m = TaskManager()
        t = m.create(title="aee6-traversal", type="research", input_text="x")
        m.start(t.task_id, hermes_run_id="real-run-id")
        info = _is_fixture_task(t.task_id)
        self.assertIsNotNone(info)
        self.assertTrue(
            any("aee6-traversal" in m for m in info.get("fixture_markers", []))
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()