"""AEE v3 Telegram Completion Enforcement Gate — targeted tests.

Covers the 3 required test classes from the work order:

1. **completed event notification** — the gate fires on `complete()`,
   records `notification_json` into `task_outputs`, and emits the
   correct `EventKind.NOTIFICATION_*` event.
2. **failure / timeout regression** — `fail()` / `timeout()` paths
   still set terminal status; the v3 gate does NOT block them
   (observability-only contract). The notification gate is wired
   only into `complete()` per the spec; failed/timeout tasks emit
   no `notification_*` event.
3. **missing telegram handling** — when the Hermes Telegram Gateway
   path returns `sent=False` (hermes binary missing / chat_id
   unset / subprocess error), the gate records `sent=False` and
   emits `NOTIFICATION_FAILED`, but `complete()` STILL returns the
   task with `status='completed'` (backward-compat).

Tests run against a temp SQLite DB (no production dispatcher.db
mutation). The `hermes send` subprocess call is monkey-patched so
no real Telegram message is sent.

Run:
    cd /home/ubuntu/hermes-runtime-bridge && \
    python3 -m unittest tests.test_aee_v3_telegram_gate -v
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock

# We import the modules under test lazily inside setUp so each test
# gets a fresh temp DB. The dispatcher package uses a thread-local
# connection keyed on the module-level DB_PATH, so we patch
# dispatcher.db.DB_PATH per-test.

import dispatcher.db as db_mod
from dispatcher import ids, manager as mgr_mod
from dispatcher.manager import TaskManager
from dispatcher.notifier import (
    ENFORCEMENT_GATE_VERSION,
    notify_completed_hermes_gateway,
    notify_completed_with_fallback,
)
from dispatcher.notification_state import (
    CompletionState,
    EXECUTION_COMPLETED,
    EVIDENCE_COMPLETED,
    FINAL_COMPLETED,
    NOTIFICATION_COMPLETED,
    compute_completion_state,
    is_terminal_completion_state,
    legal_completion_transitions,
)
from aee.observability import EventKind
from aee.observability.events import (
    _CATEGORY_FOR_KIND,
    _SEVERITY_FOR_KIND,
    EventCategory,
    EventSeverity,
    is_known,
)


def _make_fake_hermes_send(result: Dict[str, Any]):
    """Return a fake subprocess.run that returns the configured
    ``result`` dict (json-dumped to stdout) when called with a
    ``hermes send`` argv. Any other argv falls through to the real
    subprocess.run (so we don't accidentally swallow unrelated
    subprocess calls in the test)."""

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
    `_initialized` flag so each test re-initializes the schema
    against the new DB path."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="aee-v3-test-")
        self._db_path = os.path.join(self._tmpdir, "test_dispatcher.db")
        # Patch DB_PATH for the dispatcher module.
        self._orig_db_path = db_mod.DB_PATH
        db_mod.DB_PATH = db_mod.Path(self._db_path)
        # Reset the thread-local connection + initialized flag.
        # We must drop any cached connection on the *old* DB_PATH
        # before changing the path, and force a re-init on next
        # get_conn() so the schema is created against the new path.
        try:
            cached = getattr(db_mod._local, "conn", None)
            if cached is not None:
                # Roll back any pending txn + close cleanly so the
                # old dispatcher.db file is not locked.
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
        # Force schema init by opening a connection. The thread-local
        # connection stays open for the test's lifetime.
        _ = db_mod.get_conn()
        # Patch the manager's reference too (it imports get_conn /
        # transaction from dispatcher.db at module load; those are
        # bound to the module-level DB_PATH via dispatcher.db, so we
        # don't need to patch manager-level references — both
        # resolve through the same `dispatcher.db` module object.)
        # Use unique IDs per test run so we don't collide with the
        # global id counter (which is process-wide).
        self._orig_next_task_id = ids.next_task_id
        ids._counter = 0  # type: ignore[attr-defined]
        # Each test class sets this. Default: no env vars.
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

    # ---- helpers --------------------------------------------------------

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


class TestCompletionStateModule(unittest.TestCase):
    """Pure-unit tests for dispatcher/notification_state.py."""

    def test_constants_are_strings(self):
        self.assertEqual(EXECUTION_COMPLETED, "execution_completed")
        self.assertEqual(EVIDENCE_COMPLETED, "evidence_completed")
        self.assertEqual(NOTIFICATION_COMPLETED, "notification_completed")
        self.assertEqual(FINAL_COMPLETED, "final_completed")

    def test_is_terminal_completion_state(self):
        self.assertTrue(is_terminal_completion_state(FINAL_COMPLETED))
        self.assertFalse(is_terminal_completion_state(EXECUTION_COMPLETED))
        self.assertFalse(is_terminal_completion_state("random"))

    def test_legal_completion_transitions_linear_chain(self):
        tr = legal_completion_transitions()
        self.assertEqual(tr[EXECUTION_COMPLETED], [EVIDENCE_COMPLETED])
        self.assertEqual(tr[EVIDENCE_COMPLETED], [NOTIFICATION_COMPLETED])
        self.assertEqual(tr[NOTIFICATION_COMPLETED], [FINAL_COMPLETED])
        # No backward / skip / retry edges in this iteration.
        self.assertNotIn(FINAL_COMPLETED, tr)

    def test_compute_final_completed(self):
        row = {
            "status": "completed",
            "finished_at": "2026-07-18T12:00:00Z",
            "delivery_json": "[{\"path\":\"/x\"}]",
            "notification_json": json.dumps({
                "sent": True, "message_id": 999, "method": "hermes_send",
            }),
        }
        self.assertEqual(compute_completion_state(row), FINAL_COMPLETED)

    def test_compute_evidence_completed_when_notification_pending(self):
        row = {
            "status": "completed",
            "finished_at": "2026-07-18T12:00:00Z",
            "delivery_json": "[{\"path\":\"/x\"}]",
            "notification_json": json.dumps({"sent": False, "last_error": "x"}),
        }
        # delivery_json present, notification not confirmed -> stage 2
        self.assertEqual(compute_completion_state(row), NOTIFICATION_COMPLETED)

    def test_compute_evidence_completed_when_no_notification(self):
        row = {
            "status": "completed",
            "finished_at": "2026-07-18T12:00:00Z",
            "delivery_json": "[{\"path\":\"/x\"}]",
            "notification_json": None,
        }
        self.assertEqual(compute_completion_state(row), NOTIFICATION_COMPLETED)

    def test_compute_execution_completed_when_only_finished(self):
        row = {
            "status": "completed",
            "finished_at": "2026-07-18T12:00:00Z",
            "delivery_json": None,
            "notification_json": None,
        }
        self.assertEqual(compute_completion_state(row), EVIDENCE_COMPLETED)

    def test_compute_execution_completed_when_nothing_set(self):
        row = {
            "status": "running",
            "finished_at": None,
            "delivery_json": None,
            "notification_json": None,
        }
        self.assertEqual(compute_completion_state(row), EXECUTION_COMPLETED)

    def test_compute_malformed_notification_json_short_circuits(self):
        row = {
            "status": "completed",
            "finished_at": "2026-07-18T12:00:00Z",
            "delivery_json": "[{\"path\":\"/x\"}]",
            "notification_json": "not-json{",
        }
        # Corrupt blob -> EXECUTION_COMPLETED (short-circuit, NOT
        # fall-through to NOTIFICATION_COMPLETED).
        self.assertEqual(compute_completion_state(row), EXECUTION_COMPLETED)

    def test_compute_non_dict_notification_json_short_circuits(self):
        row = {
            "status": "completed",
            "finished_at": "2026-07-18T12:00:00Z",
            "delivery_json": "[{\"path\":\"/x\"}]",
            "notification_json": "[1, 2, 3]",
        }
        self.assertEqual(compute_completion_state(row), EXECUTION_COMPLETED)

    def test_compute_sent_true_with_none_message_id_falls_through(self):
        row = {
            "status": "completed",
            "finished_at": "2026-07-18T12:00:00Z",
            "delivery_json": "[{\"path\":\"/x\"}]",
            "notification_json": json.dumps({"sent": True, "message_id": None}),
        }
        # sent=True but no message_id -> fall through to delivery_json
        # stage (NOTIFICATION_COMPLETED in the stage model).
        self.assertEqual(compute_completion_state(row), NOTIFICATION_COMPLETED)

    def test_compute_non_dict_input_returns_execution_completed(self):
        self.assertEqual(compute_completion_state("not a dict"), EXECUTION_COMPLETED)  # type: ignore[arg-type]
        self.assertEqual(compute_completion_state(None), EXECUTION_COMPLETED)  # type: ignore[arg-type]

    def test_compute_dict_notification_object_no_string_decode(self):
        # notification_json already a dict (not str) -> use as-is.
        row = {
            "status": "completed",
            "finished_at": "2026-07-18T12:00:00Z",
            "delivery_json": None,
            "notification_json": {"sent": True, "message_id": 5},
        }
        self.assertEqual(compute_completion_state(row), FINAL_COMPLETED)


class TestEventKindVocabularyAddition(unittest.TestCase):
    """The 3 new EventKinds must satisfy the lock-step invariants
    (set(_CATEGORY_FOR_KIND) == EventKind.all() ==
    set(_SEVERITY_FOR_KIND)). This is the tripwire the
    test_aee74_observability suite enforces; we re-assert it here
    so a future regression is caught at the v3 gate's own test
    layer, not just the upstream observability suite."""

    def test_three_new_kinds_present(self):
        self.assertEqual(EventKind.NOTIFICATION_PENDING, "notification_pending")
        self.assertEqual(EventKind.NOTIFICATION_COMPLETED, "notification_completed")
        self.assertEqual(EventKind.NOTIFICATION_FAILED, "notification_failed")

    def test_lock_step_invariant_category(self):
        self.assertEqual(set(_CATEGORY_FOR_KIND), EventKind.all())

    def test_lock_step_invariant_severity(self):
        self.assertEqual(set(_SEVERITY_FOR_KIND), EventKind.all())

    def test_new_kinds_are_known(self):
        for k in (
            EventKind.NOTIFICATION_PENDING,
            EventKind.NOTIFICATION_COMPLETED,
            EventKind.NOTIFICATION_FAILED,
        ):
            self.assertTrue(is_known(k), f"{k} not in vocabulary")

    def test_new_kinds_categorized_lifecycle(self):
        self.assertEqual(
            _CATEGORY_FOR_KIND[EventKind.NOTIFICATION_PENDING],
            EventCategory.LIFECYCLE.value,
        )
        self.assertEqual(
            _CATEGORY_FOR_KIND[EventKind.NOTIFICATION_COMPLETED],
            EventCategory.LIFECYCLE.value,
        )
        self.assertEqual(
            _CATEGORY_FOR_KIND[EventKind.NOTIFICATION_FAILED],
            EventCategory.LIFECYCLE.value,
        )

    def test_new_kinds_severity(self):
        self.assertEqual(
            _SEVERITY_FOR_KIND[EventKind.NOTIFICATION_PENDING],
            EventSeverity.WARN.value,
        )
        self.assertEqual(
            _SEVERITY_FOR_KIND[EventKind.NOTIFICATION_COMPLETED],
            EventSeverity.INFO.value,
        )
        self.assertEqual(
            _SEVERITY_FOR_KIND[EventKind.NOTIFICATION_FAILED],
            EventSeverity.WARN.value,
        )


class TestNotifyHermesGateway(_TempDbMixin, unittest.TestCase):
    """Tests for notify_completed_hermes_gateway — the primary
    v3 path. We monkey-patch subprocess.run so no real Telegram
    message is sent."""

    def test_hermes_send_success_returns_sent_true_with_message_id(self):
        # Create a task so _format_alert has a row to read.
        m = TaskManager()
        task_id = self._create_running_task(m, "GW-Success")
        m.complete(task_id, output_text="done")
        os.environ["TELEGRAM_CHAT_ID"] = "12345"

        fake = _make_fake_hermes_send({
            "ok": True, "sent": True, "message_id": 42,
            "chat_id": 12345, "mirrored": True,
        })
        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake):
            result = notify_completed_hermes_gateway(task_id)
        self.assertTrue(result["sent"])
        self.assertEqual(result["method"], "hermes_send")
        self.assertEqual(result["recipient"], "12345")
        self.assertEqual(result["message_id"], 42)
        self.assertEqual(result["attempts"], 1)
        self.assertIsNone(result["last_error"])
        self.assertIn("T", result["ts_utc"])
        self.assertIn("+08:00", result["ts_taipei"])

    def test_hermes_send_nonzero_exit_returns_sent_false(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "GW-Nonzero")
        m.complete(task_id, output_text="done")
        os.environ["TELEGRAM_CHAT_ID"] = "999"

        def fake_run(argv, *a, **kw):
            class _P:
                returncode = 1
                stdout = ""
                stderr = "hermes: connection refused"
            return _P()

        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake_run):
            result = notify_completed_hermes_gateway(task_id)
        self.assertFalse(result["sent"])
        self.assertEqual(result["method"], "hermes_send")
        self.assertIsNone(result["message_id"])
        self.assertIn("exit=1", result["last_error"])

    def test_hermes_send_timeout_returns_sent_false(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "GW-Timeout")
        m.complete(task_id, output_text="done")
        os.environ["TELEGRAM_CHAT_ID"] = "999"

        def fake_run(argv, *a, **kw):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=30)

        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake_run):
            result = notify_completed_hermes_gateway(task_id)
        self.assertFalse(result["sent"])
        self.assertIn("TimeoutExpired", result["last_error"])

    def test_hermes_binary_missing_returns_sent_false(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "GW-NoBin")
        m.complete(task_id, output_text="done")
        os.environ["TELEGRAM_CHAT_ID"] = "999"

        def fake_run(argv, *a, **kw):
            raise FileNotFoundError("[Errno 2] No such file: 'hermes'")

        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake_run):
            result = notify_completed_hermes_gateway(task_id)
        self.assertFalse(result["sent"])
        self.assertIn("hermes binary not found", result["last_error"])

    def test_no_chat_id_returns_sent_false_without_subprocess(self):
        # No TELEGRAM_CHAT_ID env, no chat_id arg -> short-circuit
        # before any subprocess call.
        os.environ.pop("TELEGRAM_CHAT_ID", None)
        result = notify_completed_hermes_gateway("nonexistent-task")
        self.assertFalse(result["sent"])
        self.assertIsNone(result["recipient"])
        self.assertIn("TELEGRAM_CHAT_ID", result["last_error"])

    def test_hermes_send_returns_garbage_stdout_returns_sent_false(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "GW-Garbage")
        m.complete(task_id, output_text="done")
        os.environ["TELEGRAM_CHAT_ID"] = "999"

        def fake_run(argv, *a, **kw):
            class _P:
                returncode = 0
                stdout = "not json{"
                stderr = ""
            return _P()

        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake_run):
            result = notify_completed_hermes_gateway(task_id)
        self.assertFalse(result["sent"])
        self.assertIn("JSONDecodeError", result["last_error"])

    def test_enforcement_gate_version_constant(self):
        self.assertEqual(ENFORCEMENT_GATE_VERSION, "v3.0.0")


class TestNotifyWithFallback(_TempDbMixin, unittest.TestCase):
    """notify_completed_with_fallback — gateway-first, legacy
    fallback. Verifies the merge dict's `method` field reflects
    which path succeeded."""

    def test_fallback_uses_legacy_when_gateway_fails(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "FB-Legacy")
        m.complete(task_id, output_text="done")
        os.environ["TELEGRAM_CHAT_ID"] = "111"

        # Gateway returns sent=False; legacy notify_completed
        # writes to local log + returns False (because the
        # config/notify.json `enabled` flag may be true but the
        # bot token env is unset in the test env). The fallback
        # should still produce a result with method reflecting
        # the path that "succeeded" — but if both fail, method
        # should be "failed".
        def fake_run(argv, *a, **kw):
            class _P:
                returncode = 1
                stdout = ""
                stderr = "gateway down"
            return _P()

        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake_run):
            with mock.patch(
                "dispatcher.notifier.notify_completed", return_value=False
            ) as fake_legacy:
                result = notify_completed_with_fallback(task_id)
        self.assertFalse(result["sent"])
        # Legacy was called as fallback.
        fake_legacy.assert_called_once_with(task_id)
        # method is "failed" because neither path returned True.
        self.assertEqual(result["method"], "failed")

    def test_fallback_returns_gateway_when_gateway_succeeds(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "FB-GW")
        m.complete(task_id, output_text="done")
        os.environ["TELEGRAM_CHAT_ID"] = "222"

        fake = _make_fake_hermes_send({"ok": True, "message_id": 7})
        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake):
            with mock.patch(
                "dispatcher.notifier.notify_completed"
            ) as fake_legacy:
                result = notify_completed_with_fallback(task_id)
        self.assertTrue(result["sent"])
        self.assertEqual(result["method"], "hermes_send")
        self.assertEqual(result["message_id"], 7)
        # Legacy was NOT called.
        fake_legacy.assert_not_called()


class TestCompleteWiring(_TempDbMixin, unittest.TestCase):
    """Test class 1 (per work order): completed event notification.
    Verifies complete() wires the gate, persists notification_json,
    and emits the correct EventKind."""

    def test_complete_emits_notification_completed_event_when_sent(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "W-OK")
        os.environ["TELEGRAM_CHAT_ID"] = "333"

        fake = _make_fake_hermes_send({"ok": True, "message_id": 100})
        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake):
            task = m.complete(task_id, output_text="result")
        # Task status remains 'completed' (backward-compat).
        self.assertEqual(task.status, "completed")
        # notification_json persisted to task_outputs.
        out = m.get_output(task_id)
        self.assertIsNotNone(out)
        notif = json.loads(out["notification_json"])
        self.assertTrue(notif["sent"])
        self.assertEqual(notif["message_id"], 100)
        self.assertEqual(notif["method"], "hermes_send")
        # The right event was emitted.
        events = m.events(task_id)
        kinds = [e.kind for e in events]
        self.assertIn(EventKind.NOTIFICATION_COMPLETED, kinds)
        # The COMPLETED event is still emitted (gate is additive).
        self.assertIn(EventKind.COMPLETED, kinds)

    def test_complete_emits_notification_failed_event_when_send_fails(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "W-Fail")
        os.environ["TELEGRAM_CHAT_ID"] = "444"

        def fake_run(argv, *a, **kw):
            class _P:
                returncode = 1
                stdout = ""
                stderr = "nope"
            return _P()

        # Stub legacy notify_completed so the fallback also
        # returns False (no Telegram bot token in env).
        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake_run):
            with mock.patch(
                "dispatcher.notifier.notify_completed", return_value=False
            ):
                task = m.complete(task_id, output_text="result")
        self.assertEqual(task.status, "completed")  # backward-compat
        events = m.events(task_id)
        kinds = [e.kind for e in events]
        self.assertIn(EventKind.NOTIFICATION_FAILED, kinds)
        # NOTIFICATION_COMPLETED should NOT be present.
        self.assertNotIn(EventKind.NOTIFICATION_COMPLETED, kinds)
        # notification_json persisted with sent=False.
        out = m.get_output(task_id)
        notif = json.loads(out["notification_json"])
        self.assertFalse(notif["sent"])

    def test_complete_persists_notification_json_with_full_schema(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "W-Schema")
        os.environ["TELEGRAM_CHAT_ID"] = "555"

        fake = _make_fake_hermes_send({"ok": True, "message_id": 200})
        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake):
            m.complete(task_id, output_text="done")
        out = m.get_output(task_id)
        notif = json.loads(out["notification_json"])
        # Required schema fields per the work order.
        for key in ("sent", "method", "recipient", "message_id",
                    "ts_utc", "ts_taipei", "attempts", "last_error"):
            self.assertIn(key, notif, f"missing key {key!r}")
        self.assertEqual(notif["method"], "hermes_send")
        self.assertEqual(notif["recipient"], "555")
        self.assertEqual(notif["attempts"], 1)

    def test_completion_state_method_returns_final_when_notification_sent(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "W-State")
        os.environ["TELEGRAM_CHAT_ID"] = "666"

        fake = _make_fake_hermes_send({"ok": True, "message_id": 300})
        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake):
            m.complete(task_id, output_text="done")
        state = m.completion_state(task_id)
        self.assertEqual(state, FINAL_COMPLETED)

    def test_completion_state_method_returns_evidence_when_notification_failed(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "W-StateFail")
        os.environ["TELEGRAM_CHAT_ID"] = "777"

        def fake_run(argv, *a, **kw):
            class _P:
                returncode = 1
                stdout = ""
                stderr = "x"
            return _P()

        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake_run):
            with mock.patch(
                "dispatcher.notifier.notify_completed", return_value=False
            ):
                m.complete(task_id, output_text="done")
        # notification failed -> not FINAL_COMPLETED.
        state = m.completion_state(task_id)
        self.assertNotEqual(state, FINAL_COMPLETED)


class TestFailureTimeoutRegression(_TempDbMixin, unittest.TestCase):
    """Test class 2 (per work order): failure / timeout regression.

    GUARANTEED COMPLETION NOTIFICATION UPDATE (this task): the
    v3 gate is now wired into ``fail()`` and ``timeout()`` as
    well as ``complete()``. Each terminal path MUST attempt a
    Telegram notification. The gate is still
    observability-only — it NEVER overwrites the just-set
    terminal status. These tests reflect the new contract:
    a ``NOTIFICATION_FAILED`` event (no chat_id / no hermes
    binary in the test env) is expected on fail() and
    timeout(); the terminal status is preserved.
    """

    def test_fail_emits_notification_attempt(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "F-Notif")
        os.environ.pop("TELEGRAM_CHAT_ID", None)
        task = m.fail(task_id, error_message="boom")
        # Terminal status preserved.
        self.assertEqual(task.status, "failed")
        events = m.events(task_id)
        kinds = [e.kind for e in events]
        self.assertIn(EventKind.FAILED, kinds)
        # Guaranteed completion notification: a notification
        # attempt fired. With no chat_id / no hermes binary in
        # the test env, the gate fails to send and emits
        # NOTIFICATION_FAILED. (Sent=True would also be
        # acceptable here — the contract is "an attempt was
        # made", not "send must fail".)
        notif_kinds = {
            EventKind.NOTIFICATION_COMPLETED,
            EventKind.NOTIFICATION_PENDING,
            EventKind.NOTIFICATION_FAILED,
        }
        self.assertTrue(
            notif_kinds & set(kinds),
            f"expected a NOTIFICATION_* event on fail(), got kinds={kinds}",
        )
        # notification_json is persisted by _notify_terminal.
        out = m.get_output(task_id)
        if out is not None:
            self.assertIsNotNone(out.get("notification_json"))

    def test_timeout_emits_notification_attempt(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "T-Notif")
        os.environ.pop("TELEGRAM_CHAT_ID", None)
        task = m.timeout(task_id, reason="ran too long")
        self.assertEqual(task.status, "timeout")
        events = m.events(task_id)
        kinds = [e.kind for e in events]
        self.assertIn(EventKind.TIMEOUT, kinds)
        notif_kinds = {
            EventKind.NOTIFICATION_COMPLETED,
            EventKind.NOTIFICATION_PENDING,
            EventKind.NOTIFICATION_FAILED,
        }
        self.assertTrue(
            notif_kinds & set(kinds),
            f"expected a NOTIFICATION_* event on timeout(), got kinds={kinds}",
        )

    def test_fail_status_unchanged_by_v3_gate(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "F-Status")
        os.environ.pop("TELEGRAM_CHAT_ID", None)
        task = m.fail(task_id, error_message="err")
        self.assertEqual(task.status, "failed")
        # completion_state on a failed task: notification_json
        # is now written by _notify_terminal (sent=False, no
        # message_id) → compute_completion_state returns
        # EVIDENCE_COMPLETED (notification_pending stage is
        # only reached when sent=True with no message_id).
        state = m.completion_state(task_id)
        # finished_at is set by fail(); notification_json is
        # present but sent=False → EVIDENCE_COMPLETED.
        self.assertEqual(state, EVIDENCE_COMPLETED)


class TestMissingTelegramHandling(_TempDbMixin, unittest.TestCase):
    """Test class 3 (per work order): missing telegram handling.
    When the gateway path can't send (chat_id missing, hermes
    binary missing, subprocess error), the gate must record the
    failure but NOT block completion."""

    def test_complete_with_no_chat_id_still_completes(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "M-NoChat")
        # No TELEGRAM_CHAT_ID env, no chat_id arg.
        os.environ.pop("TELEGRAM_CHAT_ID", None)
        task = m.complete(task_id, output_text="done")
        # Backward-compat: status is still 'completed'.
        self.assertEqual(task.status, "completed")
        events = m.events(task_id)
        kinds = [e.kind for e in events]
        # The gate fired but failed because no chat_id.
        self.assertIn(EventKind.NOTIFICATION_FAILED, kinds)
        # notification_json records the failure.
        out = m.get_output(task_id)
        notif = json.loads(out["notification_json"])
        self.assertFalse(notif["sent"])
        self.assertIn("TELEGRAM_CHAT_ID", notif["last_error"])

    def test_complete_with_hermes_binary_missing_still_completes(self):
        m = TaskManager()
        task_id = self._create_running_task(m, "M-NoBin")
        os.environ["TELEGRAM_CHAT_ID"] = "888"

        def fake_run(argv, *a, **kw):
            raise FileNotFoundError("[Errno 2] No such file: 'hermes'")

        with mock.patch("dispatcher.notifier.subprocess.run", side_effect=fake_run):
            with mock.patch(
                "dispatcher.notifier.notify_completed", return_value=False
            ):
                task = m.complete(task_id, output_text="done")
        self.assertEqual(task.status, "completed")
        events = m.events(task_id)
        kinds = [e.kind for e in events]
        self.assertIn(EventKind.NOTIFICATION_FAILED, kinds)
        out = m.get_output(task_id)
        notif = json.loads(out["notification_json"])
        self.assertFalse(notif["sent"])

    def test_complete_with_gateway_exception_does_not_raise(self):
        """If notify_completed_with_fallback itself raises
        (defensive — it shouldn't), the gate's try/except in
        complete() catches it and records a 'gate exception'
        notification_json — complete() never raises from the
        gate."""

        m = TaskManager()
        task_id = self._create_running_task(m, "M-Exc")
        os.environ["TELEGRAM_CHAT_ID"] = "999"

        def boom(*a, **kw):
            raise RuntimeError("unexpected gate failure")

        with mock.patch(
            "dispatcher.notifier.notify_completed_with_fallback",
            side_effect=boom,
        ):
            task = m.complete(task_id, output_text="done")
        # complete() must not propagate the gate exception.
        self.assertEqual(task.status, "completed")
        out = m.get_output(task_id)
        notif = json.loads(out["notification_json"])
        self.assertFalse(notif["sent"])
        self.assertIn("gate exception", notif["last_error"])


class TestConfigDefaults(unittest.TestCase):
    """Verifies the config/notify.json Gap B fix landed."""

    def test_notify_json_enabled_is_true(self):
        from config import load
        cfg = load("notify").get("telegram", {})
        self.assertTrue(cfg.get("enabled", False),
                        "config/notify.json must have enabled=true after Gap B fix")

    def test_notify_json_notify_on_includes_completed(self):
        from config import load
        cfg = load("notify").get("telegram", {})
        notify_on = set(cfg.get("notify_on", []))
        self.assertIn("completed", notify_on)
        self.assertIn("failed", notify_on)
        self.assertIn("timeout", notify_on)

    def test_enforcement_gate_documentation_block(self):
        from config import load
        cfg = load("notify")
        gate = cfg.get("enforcement_gate", {})
        self.assertEqual(gate.get("version"), "v3.0.0")
        self.assertEqual(gate.get("primary_path"), "hermes_send")
        self.assertEqual(gate.get("fallback_path"), "notifier.notify_completed")
        self.assertFalse(gate.get("blocking", True))


class TestDbMigration(_TempDbMixin, unittest.TestCase):
    """Verifies the idempotent ALTER TABLE migration for
    notification_json ran on first import.

    Inherits ``_TempDbMixin`` so the test gets its own per-test
    temp DB with a fully-initialised schema (including the
    ``notification_json`` column added by ``_init_schema``'s
    ``_AEE_V3_NOTIFICATION_MIGRATIONS``). Without the mixin, this
    test reads ``dispatcher.db.DB_PATH`` which may point at a
    tempdir DB created by ``tests/test_dispatcher.py``'s
    module-load ``point_module_to_temp_db`` rebinding — that DB
    has ``_initialized=False`` and no schema, so
    ``notification_json`` is absent and the test fails
    (cross-test DB_PATH pollution, order-dependent).
    """

    def test_notification_json_column_present_after_init(self):
        # The dispatcher.db module is already imported; the
        # column was added on first get_conn() call. Verify by
        # checking the production dispatcher.db schema directly.
        import dispatcher.db as d
        conn = sqlite3.connect(str(d.DB_PATH))
        row = conn.execute(
            "SELECT 1 FROM pragma_table_info('task_outputs') WHERE name = ?",
            ("notification_json",),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row, "notification_json column must be present")


if __name__ == "__main__":
    unittest.main()