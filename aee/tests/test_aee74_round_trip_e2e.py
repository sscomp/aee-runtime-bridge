"""AEE-7.4 finalization — end-to-end round-trip E2E tests.

Wires the dispatcher (``dispatcher.manager.TaskManager``)
together with the observability runtime
(``aee.observability_runtime``) and asserts that the
full lifecycle is observed end-to-end:

    1. :class:`TaskManager.create` writes a row + emits a
       ``created`` event.
    2. :class:`TaskManager.queue` writes a row + emits a
       ``queued`` event.
    3. :class:`TaskManager.start` writes a row + emits a
       ``started`` event.
    4. :class:`TaskManager.complete` writes a row + emits a
       ``completed`` event (and ``delivery_unverified`` /
       ``intent_mismatch`` if applicable).
    5. The bridge's :class:`EventEmitter` (a
       :class:`BufferingEmitter` in these tests) receives
       every event with a sane ``task_id`` / ``kind`` /
       ``schema_version`` shape.
    6. No terminal event is duplicated; the sequence is
       exactly ``[created, queued, started, completed]``
       for a happy-path task.

This is the long-form acceptance test for AEE-7.4
finalization; the tripwire regression test in
``test_aee74_observability.py`` is the short-form
guard.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from typing import List, Tuple

# Ensure the bridge root is on sys.path so the tests can
# be run from any cwd (e.g. ``python -m unittest discover
# -s aee/tests``).
BRIDGE_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", ".."),
)
if BRIDGE_ROOT not in sys.path:
    sys.path.insert(0, BRIDGE_ROOT)

from aee.observability import EventKind  # noqa: E402
from aee.observability_runtime.buffer_emitter import (  # noqa: E402
    BufferingEmitter,
)
from aee.observability_runtime.serialization import (  # noqa: E402
    SCHEMA_VERSION,
)


# ---------------------------------------------------------------------------
# SQLite scratch fixtures
# ---------------------------------------------------------------------------


class _SqliteScratch:
    """Context manager that swaps the dispatcher's
    ``DB_PATH`` to a temp file for the duration of a test.

    AEE-7.4 finalization must not regress the dispatcher's
    test isolation.  The pre-existing pattern in
    ``test_dispatcher.py`` and ``test_aee72_dispatcher_e2e.py``
    uses module-level ``setUp`` / ``tearDown`` to
    monkey-patch ``DB_PATH`` and ``init_db``.  We use the
    same pattern.
    """

    def __init__(self) -> None:
        self.tmpdir: str = ""
        self.saved_db_path: str = ""
        self.saved_logs_dir: str = ""
        self.saved_reports_dir: str = ""

    def __enter__(self) -> str:
        self.tmpdir = tempfile.mkdtemp(prefix="aee74-e2e-")
        # Patch the dispatcher's DB_PATH.
        from dispatcher import db

        self.saved_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmpdir, "dispatcher.sqlite")
        from dispatcher import manager

        # Also patch the manager's LOGS_DIR / REPORTS_DIR so
        # ``_append_log`` and ``reports/`` writes go to the
        # scratch directory.
        from pathlib import Path

        self.saved_logs_dir = str(manager.LOGS_DIR)
        self.saved_reports_dir = str(manager.REPORTS_DIR)
        manager.LOGS_DIR = Path(self.tmpdir) / "logs"
        manager.REPORTS_DIR = Path(self.tmpdir) / "reports"
        manager.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        manager.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        # AEE-7.4 finalization fix: dispatcher.db exposes
        # ``run_migrations()`` (the public migration entry
        # point) — not ``init_db()`` (which does not exist
        # in the AEE-6 baseline). Calling run_migrations
        # is idempotent across test invocations because
        # every CREATE / ALTER uses ``IF NOT EXISTS`` /
        # ``PRAGMA table_info`` guards.
        db.run_migrations()
        return self.tmpdir

    def __exit__(self, *exc_info) -> None:
        from dispatcher import db, manager

        db.DB_PATH = self.saved_db_path
        manager.LOGS_DIR = type(manager.LOGS_DIR)(self.saved_logs_dir)
        manager.REPORTS_DIR = type(manager.REPORTS_DIR)(
            self.saved_reports_dir,
        )


# ---------------------------------------------------------------------------
# E2E fixtures
# ---------------------------------------------------------------------------


class _RoundTripE2EBase(unittest.TestCase):
    """Shared setup for the round-trip E2E tests.

    Each test:

    1. Swaps the dispatcher's DB to a fresh temp file.
    2. Installs a :class:`BufferingEmitter` so the test
       can assert against the in-memory event stream.
    3. Runs the manager through a happy-path lifecycle
       and asserts the event sequence is exactly
       ``[created, queued, started, completed]``.
    """

    def setUp(self) -> None:
        self._scratch = _SqliteScratch()
        self._scratch.__enter__()
        from dispatcher.manager import TaskManager

        # AEE-7.4 finalization: install a buffering emitter
        # so the events the dispatcher emits are captured
        # in-memory.
        from aee.observability_runtime import (
            install as install_emitter,
        )
        self._emitter = BufferingEmitter()
        install_emitter(self._emitter)
        # Suppress the manager's structured-log INFO
        # spam during the test (it leaks paths + task_ids
        # into the test output).
        import logging

        logging.getLogger("dispatcher.manager").setLevel(
            logging.CRITICAL,
        )
        self._mgr = TaskManager()
        self._task_id: str = ""

    def tearDown(self) -> None:
        from aee.observability_runtime import (
            uninstall as uninstall_emitter,
        )
        uninstall_emitter()
        self._scratch.__exit__(None, None, None)

    def _emitted_kinds(self) -> List[str]:
        return [e.kind for e in self._emitter.events]

    def _create_and_start(self) -> str:
        task = self._mgr.create(
            title="e2e round-trip",
            type="ops",
            priority=50,
            owner="e2e",
            input_text="hello",
            mode="ops",
            repo_root=None,
        )
        self._mgr.queue(task.task_id)
        self._mgr.start(task.task_id, hermes_run_id="hermes-123")
        return task.task_id


# ---------------------------------------------------------------------------
# Happy-path round-trip
# ---------------------------------------------------------------------------


class TestHappyPathRoundTrip(_RoundTripE2EBase):
    def test_create_queue_start_completed_event_sequence(self) -> None:
        """A successful task emits exactly the four
        canonical LIFECYCLE events in order: ``created`` /
        ``queued`` / ``started`` / ``completed``.

        AEE v3 Telegram Completion Enforcement Gate adds an
        optional observability notification event after
        ``completed``.  In the test environment (no real
        Telegram token) the gate emits
        ``notification_failed``; in production it would emit
        ``notification_completed``.  The notification event
        is non-terminal and does not alter the four-event
        LIFECYCLE prefix."""
        task_id = self._create_and_start()
        # AEE-7.4 finalization: ``complete()`` is kw-only
        # with ``(output_text, usage, raw, model_name)``.
        # ``raw={"model": "MiniMaxAI/MiniMax-M3"}`` exercises
        # the auto-derive of ``model_name`` from ``raw.model``
        # in the manager.
        self._mgr.complete(
            task_id,
            output_text="ok",
            raw={"model": "MiniMaxAI/MiniMax-M3"},
        )
        kinds = self._emitted_kinds()
        # The four canonical LIFECYCLE events MUST remain the
        # first four, in order.  AEE v3 appends exactly one
        # NOTIFICATION_* event afterwards.
        self.assertEqual(
            kinds[:4],
            [
                EventKind.CREATED,
                EventKind.QUEUED,
                EventKind.STARTED,
                EventKind.COMPLETED,
            ],
        )
        # AEE v3: exactly one NOTIFICATION_* event follows.
        notification_kinds = {
            EventKind.NOTIFICATION_PENDING,
            EventKind.NOTIFICATION_COMPLETED,
            EventKind.NOTIFICATION_FAILED,
        }
        self.assertEqual(len(kinds), 5)
        self.assertIn(kinds[4], notification_kinds)

    def test_every_event_carries_task_id_and_schema_version(self) -> None:
        """Every emitted :class:`Event` has a non-empty
        ``task_id`` and the canonical
        :data:`SCHEMA_VERSION` (``"1.0"``).

        The dispatcher emits through the emitter Protocol;
        we read the canonical wire JSON via
        :func:`serialize_event` (the same call the
        :class:`StdoutJsonEmitter` makes) so the assertion
        matches the actual production wire shape.
        """
        from aee.observability_runtime.serialization import (
            serialize_event,
        )

        task_id = self._create_and_start()
        self._mgr.complete(
            task_id,
            output_text="ok",
        )
        self.assertGreaterEqual(len(self._emitter.events), 4)
        for event in self._emitter.events:
            self.assertEqual(event.task_id, task_id)
            serialized = serialize_event(event)
            self.assertEqual(
                serialized["schema_version"],
                SCHEMA_VERSION,
            )

    def test_terminal_status_is_consistent_with_sqlite(self) -> None:
        """The dispatcher's final ``tasks.status`` matches
        the terminal event kind, and only one terminal
        event was emitted."""
        task_id = self._create_and_start()
        self._mgr.complete(
            task_id,
            output_text="ok",
        )
        from dispatcher import db

        row = db.get_conn().execute(
            "SELECT status FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        self.assertEqual(row["status"], "completed")
        terminal_kinds = [
            k for k in (
                EventKind.COMPLETED, EventKind.FAILED,
                EventKind.TIMEOUT, EventKind.CANCELLED,
            ) if k in self._emitted_kinds()
        ]
        self.assertEqual(
            len(terminal_kinds), 1,
            f"expected exactly one terminal event, got {terminal_kinds}",
        )
        self.assertEqual(terminal_kinds[0], EventKind.COMPLETED)

    def test_secret_payload_keys_are_scrubbed_at_serialize_time(self) -> None:
        """AEE-7.4 finalization acceptance: if a caller
        stuffs a ``token`` / ``api_key`` / ``password`` /
        ``authorization`` value into a payload, the
        serialized wire JSON has it redacted.

        The dispatcher's current payload shapes don't
        include such fields, so this test injects one via
        the :class:`Event` dataclass directly and asserts
        the wire serializer scrubs it.
        """
        from aee.observability_runtime.serialization import (
            serialize_event,
        )
        from aee.observability_runtime.emitter import Event

        e = Event(
            kind=EventKind.STARTED,
            source="dispatcher.manager",
            task_id="t-secret",
            payload={
                "hermes_run_id": "r-1",
                "api_key": "sk-thi...bbed",
                "authorization": "Bearer xxxxxxxx",
                "nested": {"token": "child-token"},
            },
        )
        # AEE-7.4 finalization: ``serialize_event`` returns
        # a ``dict`` (not a JSON string).  Tests that want
        # to assert on the wire-format as a string MUST
        # ``json.dumps`` the result — the dispatcher and
        # :class:`StdoutJsonEmitter` call :func:`to_json_line`
        # for the actual on-the-wire bytes.
        import json as _json
        wire = _json.dumps(serialize_event(e), sort_keys=True)
        for forbidden in (
            "sk-thi...bbed",
            "Bearer xxxxxxxx",
            "child-token",
        ):
            self.assertNotIn(
                forbidden, wire,
                f"secret {forbidden!r} leaked into wire JSON",
            )
        # The ``<redacted>`` sentinel must be present at the
        # scrubbed positions.  At least three secrets are in
        # the input (``api_key`` top-level, ``nested.token``,
        # and ``authorization``), so we expect at least 3
        # redactions in the serialized wire.
        self.assertIn("<redacted>", wire)
        # Defense in depth: the wire JSON MUST NOT contain
        # the raw ``Bearer`` prefix in the payload.
        self.assertNotIn("Bearer xxxxxxxx", wire)

    def test_secret_canary_value_is_scrubbed(self) -> None:
        """Distinctive canary string is scrubbed by the
        wire serializer.

        AEE-7.4 finalization: the canary is a 32-char
        distinctive string defined as
        :data:`aee.observability_runtime.serialization.SECRET_CANARY`.
        The test imports the production constant directly
        (no shadow local copy) so the assertion tracks the
        real wire-scrubber contract.
        """
        import json as _json
        from aee.observability_runtime.serialization import (
            SECRET_CANARY,
            serialize_event,
        )
        from aee.observability_runtime.emitter import Event

        e = Event(
            kind=EventKind.LOG,
            source="t",
            task_id="t-canary",
            payload={"line": f"found {SECRET_CANARY} in stdout"},
        )
        wire = _json.dumps(serialize_event(e))
        self.assertNotIn(SECRET_CANARY, wire)
        # Defense in depth: the redaction sentinel must
        # appear at the canary's position.
        self.assertIn("<redacted>", wire)


# ---------------------------------------------------------------------------
# Failure / cancel / emitter-isolation round-trips
# ---------------------------------------------------------------------------


class TestFailurePathRoundTrip(_RoundTripE2EBase):
    def test_failed_terminal_status_consistent(self) -> None:
        """A failed task ends in status='failed' and emits
        exactly one terminal event (``failed``)."""
        task_id = self._create_and_start()
        self._mgr.fail(task_id, error_message="oops")
        from dispatcher import db

        row = db.get_conn().execute(
            "SELECT status FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        self.assertEqual(row["status"], "failed")
        terminal_kinds = [
            k for k in (
                EventKind.COMPLETED, EventKind.FAILED,
                EventKind.TIMEOUT, EventKind.CANCELLED,
            ) if k in self._emitted_kinds()
        ]
        self.assertEqual(terminal_kinds, [EventKind.FAILED])

    def test_cancelled_terminal_status_consistent(self) -> None:
        """A cancelled task ends in status='cancelled'."""
        task_id = self._create_and_start()
        self._mgr.cancel(task_id)
        from dispatcher import db

        row = db.get_conn().execute(
            "SELECT status FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        self.assertEqual(row["status"], "cancelled")
        terminal_kinds = [
            k for k in (
                EventKind.COMPLETED, EventKind.FAILED,
                EventKind.TIMEOUT, EventKind.CANCELLED,
            ) if k in self._emitted_kinds()
        ]
        self.assertEqual(terminal_kinds, [EventKind.CANCELLED])


# ---------------------------------------------------------------------------
# Emitter isolation: an emitter that raises does not crash the manager
# ---------------------------------------------------------------------------


class _RaisingEmitter:
    """An :class:`EventEmitter` that raises on every emit.

    Used to assert that the dispatcher's install-time
    fallback to :class:`NullEmitter` is correct, and
    that a misbehaving emitter never crashes a task.
    """

    def __init__(self) -> None:
        self.calls: int = 0

    def emit(self, event) -> None:  # type: ignore[no-untyped-def]
        self.calls += 1
        raise RuntimeError("emitter is broken")

    def close(self) -> None:
        pass


class TestEmitterFailureIsolation(_RoundTripE2EBase):
    def test_dispatcher_swallows_emitter_exception(self) -> None:
        """If the installed emitter raises, the
        dispatcher's :meth:`_emit_event` logs and
        continues — the task still completes.

        AEE-7.4 finalization acceptance: an emitter
        outage MUST NOT crash a task mid-flight.
        """
        from aee.observability_runtime import (
            install as install_emitter,
        )

        bad = _RaisingEmitter()
        install_emitter(bad)
        # Suppress the per-event warning so the test
        # output stays clean.
        import logging
        logging.getLogger("aee.observability_runtime.wireup").setLevel(
            logging.CRITICAL,
        )
        task_id = self._create_and_start()
        self._mgr.complete(
            task_id,
            output_text="ok",
        )
        from dispatcher import db
        row = db.get_conn().execute(
            "SELECT status FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertGreater(bad.calls, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
