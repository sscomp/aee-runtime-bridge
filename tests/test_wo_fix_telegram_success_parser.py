"""WO-FIX-TELEGRAM-SUCCESS-PARSER — focused regression tests for
dispatcher/notifier.py JSON shape recognition.

Background
----------
``hermes send --json`` returns ``{"success": true, "message_id": "..."}``.
The notifier previously only recognized ``ok`` and ``sent`` fields, so a
real successful send was mis-parsed as ``sent=False``. The fix adds
``success`` as the primary key while preserving ``ok`` and ``sent`` as
fallbacks.

These tests monkey-patch ``subprocess.run`` so no real Telegram message
is sent. Each test exercises one JSON shape and asserts the parser
returns ``sent=True`` with the right ``message_id`` (or ``sent=False``
with a populated ``last_error`` for the failure cases).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from typing import Any, Dict
from unittest import mock

from dispatcher import db as db_mod
from dispatcher import ids
from dispatcher.manager import TaskManager
from dispatcher.notifier import notify_completed_hermes_gateway


def _make_fake_hermes_send(result: Dict[str, Any]):
    """Return a fake subprocess.run that returns the configured
    ``result`` dict (json-dumped to stdout) when called with a
    ``hermes send`` argv. Any other argv falls through to the real
    subprocess.run."""

    stdout_payload = json.dumps(result)
    # Treat any truthy success/ok/sent as exit 0 unless the caller
    # explicitly overrides returncode (used to test the exit=0 +
    # no-positive-field path).
    ok_flag = bool(
        result.get("success",
                   result.get("ok",
                              result.get("sent", False)))
    )
    returncode = result.get("_returncode", 0 if ok_flag else 1)
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
    dispatcher.db."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="wo-fix-tg-")
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


class TestSuccessFieldRecognition(_TempDbMixin, unittest.TestCase):
    """Verify the parser recognizes ``success`` (the actual ``hermes
    send --json`` field) as a positive delivery indicator."""

    def test_success_true_alone_returns_sent_true(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "WO-FIX-SuccessOnly")
        m.complete(task_id, output_text="done")
        os.environ["TELEGRAM_CHAT_ID"] = "12345"

        fake = _make_fake_hermes_send({
            "success": True,
            "platform": "telegram",
            "chat_id": "12345",
            "message_id": "8044",
            "mirrored": True,
        })
        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake):
            result = notify_completed_hermes_gateway(task_id)

        self.assertTrue(result["sent"], f"expected sent=True, got {result}")
        self.assertEqual(result["method"], "hermes_send")
        self.assertEqual(result["recipient"], "12345")
        self.assertEqual(result["message_id"], "8044")
        self.assertIsNone(result["last_error"])

    def test_success_true_with_int_message_id(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "WO-FIX-SuccessInt")
        m.complete(task_id, output_text="done")
        os.environ["TELEGRAM_CHAT_ID"] = "12345"

        fake = _make_fake_hermes_send({
            "success": True,
            "message_id": 42,
        })
        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake):
            result = notify_completed_hermes_gateway(task_id)

        self.assertTrue(result["sent"])
        self.assertEqual(result["message_id"], 42)

    def test_success_false_returns_sent_false(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "WO-FIX-SuccessFalse")
        m.complete(task_id, output_text="done")
        os.environ["TELEGRAM_CHAT_ID"] = "12345"

        fake = _make_fake_hermes_send({
            "success": False,
            "error": "telegram bot token invalid",
        })
        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake):
            result = notify_completed_hermes_gateway(task_id)

        self.assertFalse(result["sent"])
        self.assertIsNone(result["message_id"])
        self.assertIn("telegram bot token invalid", result["last_error"])


class TestOkFieldBackwardCompat(_TempDbMixin, unittest.TestCase):
    """Verify the legacy ``ok`` field still works as a fallback."""

    def test_ok_true_alone_returns_sent_true(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "WO-FIX-OkOnly")
        m.complete(task_id, output_text="done")
        os.environ["TELEGRAM_CHAT_ID"] = "12345"

        fake = _make_fake_hermes_send({
            "ok": True,
            "message_id": 7,
        })
        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake):
            result = notify_completed_hermes_gateway(task_id)

        self.assertTrue(result["sent"])
        self.assertEqual(result["message_id"], 7)

    def test_ok_false_returns_sent_false(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "WO-FIX-OkFalse")
        m.complete(task_id, output_text="done")
        os.environ["TELEGRAM_CHAT_ID"] = "12345"

        fake = _make_fake_hermes_send({
            "ok": False,
            "error": "rate limited",
        })
        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake):
            result = notify_completed_hermes_gateway(task_id)

        self.assertFalse(result["sent"])
        self.assertIn("rate limited", result["last_error"])


class TestSentFieldBackwardCompat(_TempDbMixin, unittest.TestCase):
    """Verify the ``sent`` field still works as a fallback."""

    def test_sent_true_alone_returns_sent_true(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "WO-FIX-SentOnly")
        m.complete(task_id, output_text="done")
        os.environ["TELEGRAM_CHAT_ID"] = "12345"

        fake = _make_fake_hermes_send({
            "sent": True,
            "message_id": 99,
        })
        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake):
            result = notify_completed_hermes_gateway(task_id)

        self.assertTrue(result["sent"])
        self.assertEqual(result["message_id"], 99)

    def test_sent_false_returns_sent_false(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "WO-FIX-SentFalse")
        m.complete(task_id, output_text="done")
        os.environ["TELEGRAM_CHAT_ID"] = "12345"

        fake = _make_fake_hermes_send({
            "sent": False,
            "error": "chat_id not found",
        })
        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake):
            result = notify_completed_hermes_gateway(task_id)

        self.assertFalse(result["sent"])
        self.assertIn("chat_id not found", result["last_error"])


class TestFieldPrecedence(_TempDbMixin, unittest.TestCase):
    """Verify ``success`` takes precedence over ``ok`` and ``sent``."""

    def test_success_takes_precedence_over_ok(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "WO-FIX-PrecSucOk")
        m.complete(task_id, output_text="done")
        os.environ["TELEGRAM_CHAT_ID"] = "12345"

        # success=True should win even if ok=False.
        fake = _make_fake_hermes_send({
            "success": True,
            "ok": False,
            "message_id": 100,
        })
        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake):
            result = notify_completed_hermes_gateway(task_id)

        self.assertTrue(result["sent"])
        self.assertEqual(result["message_id"], 100)

    def test_ok_takes_precedence_over_sent(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "WO-FIX-PrecOkSent")
        m.complete(task_id, output_text="done")
        os.environ["TELEGRAM_CHAT_ID"] = "12345"

        # success absent, ok=True should win over sent=False.
        fake = _make_fake_hermes_send({
            "ok": True,
            "sent": False,
            "message_id": 200,
        })
        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake):
            result = notify_completed_hermes_gateway(task_id)

        self.assertTrue(result["sent"])
        self.assertEqual(result["message_id"], 200)


class TestNoPositiveFieldReturnsSentFalse(_TempDbMixin, unittest.TestCase):
    """Verify absence of all three positive fields returns sent=False."""

    def test_no_success_no_ok_no_sent_returns_sent_false(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "WO-FIX-NoFields")
        m.complete(task_id, output_text="done")
        os.environ["TELEGRAM_CHAT_ID"] = "12345"

        fake = _make_fake_hermes_send({
            "platform": "telegram",
            "chat_id": "12345",
            "_returncode": 0,
        })
        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake):
            result = notify_completed_hermes_gateway(task_id)

        self.assertFalse(result["sent"])
        self.assertIsNone(result["message_id"])
        # Default error message when none of success/ok/sent is true.
        self.assertIn("ok=False", result["last_error"])


if __name__ == "__main__":
    unittest.main()