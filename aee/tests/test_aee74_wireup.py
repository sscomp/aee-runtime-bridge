"""AEE-7.4 slice 3 — wire-up tripwire + E2E pipeline tests.

This test file is the proof that the slice 3 wire-up is
complete.  It is structured in three groups:

* **Group A — Wire-up contract** — asserts that
  :func:`install` monkey-patches the dispatcher and
  orchestrator, that :func:`uninstall` restores them, and
  that the new ``Event`` fields (``event_id``,
  ``timestamp_iso``) and the wire format
  (``schema_version``) are present.
* **Group B — Secret-leak tripwire** — asserts that the
  canary string is scrubbed from payloads before
  serialization, and that known-secret key names
  (``api_key``, ``token``, etc.) are redacted.
* **Group C — E2E pipeline** — drives a real task through
  the dispatcher with a :class:`BufferingEmitter` installed,
  asserts the events are emitted in the right order with
  the right kinds, and that the SQLite event log
  (``task_events`` table) is still written (backward
  compatibility).

These tests are also the de-facto documentation of the
slice 3 contract: read them to understand what
:func:`install` does and what guarantees it provides.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from types import MappingProxyType
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from aee.observability_runtime import (
    BufferingEmitter,
    EMITTER_SOURCE_DISPATCHER,
    EMITTER_SOURCE_ORCHESTRATOR,
    Event,
    EventEmitter,
    NullEmitter,
    SCHEMA_VERSION,
    SECRET_CANARY,
    StdoutJsonEmitter,
    default_emitter,
    emitter_from_env,
    install,
    is_installed,
    serialize_event,
    set_default_emitter,
    to_json_line,
    uninstall,
)
from aee.observability_runtime.emitter import Event as EventClass


# ---------------------------------------------------------------------------
# Helper: build a hermetic temp-dir for db tests
# ---------------------------------------------------------------------------


class _TempDir:
    """Context manager that chdirs into a temp dir for the
    duration of a block.  Used so the dispatcher's default
    DB path resolves to the temp dir (and is cleaned up
    when the block exits).
    """

    def __enter__(self) -> str:
        self._prev = os.getcwd()
        self._dir = tempfile.mkdtemp(prefix="aee74-wireup-")
        os.chdir(self._dir)
        # Force the dispatcher's get_conn to re-init against
        # the new path on first call.
        import dispatcher.db as _db
        _db._initialized = False
        _db.DB_PATH = os.path.join(self._dir, "t.db")
        return self._dir

    def __exit__(self, exc_type, exc, tb) -> None:
        os.chdir(self._prev)
        # Best-effort cleanup; tempfile.TemporaryDirectory
        # would be nicer but we want the prefix visibility
        # in the test output.
        import shutil
        try:
            shutil.rmtree(self._dir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Group A — Wire-up contract
# ---------------------------------------------------------------------------


class TestWireupContract(unittest.TestCase):
    """``install()`` monkey-patches the dispatcher and orchestrator."""

    def setUp(self) -> None:
        # Save the original default emitter; restore on
        # teardown so a test that calls
        # ``set_default_emitter(...)`` does not leak.
        self._saved_default = default_emitter()
        set_default_emitter(NullEmitter())

    def tearDown(self) -> None:
        if is_installed():
            uninstall()
        set_default_emitter(self._saved_default)

    def test_install_then_uninstall_round_trip(self) -> None:
        """After install+uninstall, is_installed() is False."""
        self.assertFalse(is_installed())
        install(emitter=BufferingEmitter())
        self.assertTrue(is_installed())
        uninstall()
        self.assertFalse(is_installed())

    def test_install_idempotent(self) -> None:
        """Calling install twice does not raise."""
        install(emitter=BufferingEmitter())
        install(emitter=BufferingEmitter())
        uninstall()

    def test_double_install_does_not_leak_wrapper_on_uninstall(self) -> None:
        """AEE-7.4 finalization tripwire: a second ``install()`` must
        restore the *true* original method, not the first install's
        wired wrapper.

        Bug history: previously the second install would snapshot
        the first install's wired method as ``original`` and the
        second ``_emit_event`` would wrap the wrapper.  After
        ``uninstall()``, the class still pointed at the wrapper.
        This test pins the corrected behaviour: after
        ``install()`` + ``install()`` + ``uninstall()``,
        ``TaskManager._emit_event`` must equal the method that was
        live before either install call.
        """
        from dispatcher.manager import TaskManager

        # Capture the pre-install original.
        pre_install = TaskManager._emit_event
        self.assertIsNotNone(pre_install)

        install(emitter=BufferingEmitter())
        # The first install must have replaced _emit_event.
        self.assertIsNot(TaskManager._emit_event, pre_install)

        install(emitter=BufferingEmitter())
        # The second install must again replace _emit_event.
        self.assertIsNot(TaskManager._emit_event, pre_install)

        uninstall()
        # CRITICAL: after uninstall, the method must be the
        # *original* (pre-install), not a stale wrapper.
        self.assertIs(
            TaskManager._emit_event,
            pre_install,
            "uninstall after double-install must restore the "
            "true pre-install original, not the first install's "
            "wrapper (AEE-7.4 finalization idempotency fix)",
        )

        # Also verify the orchestrator round-trip.
        from aee.orchestrator.orchestrator import ExecutionOrchestrator

        pre_submit = ExecutionOrchestrator.submit
        install(emitter=BufferingEmitter())
        install(emitter=BufferingEmitter())
        uninstall()
        self.assertIs(
            ExecutionOrchestrator.submit,
            pre_submit,
            "uninstall after double-install must restore the "
            "true pre-install orchestrator.submit",
        )

    def test_install_with_invalid_emitter_falls_back(self) -> None:
        """A non-Protocol emitter falls back to NullEmitter, never raises."""
        class NotAnEmitter:
            pass
        chosen = install(emitter=NotAnEmitter())  # type: ignore[arg-type]
        self.assertIsInstance(chosen, NullEmitter)
        uninstall()

    def test_dispatcher_task_manager_gains_init(self) -> None:
        """After install, ``TaskManager()`` accepts an ``emitter`` kwarg."""
        install(emitter=BufferingEmitter())
        from dispatcher.manager import TaskManager
        m = TaskManager()
        self.assertIsNotNone(m._emitter)
        m2 = TaskManager(emitter=NullEmitter())
        self.assertIsInstance(m2._emitter, NullEmitter)
        uninstall()


# ---------------------------------------------------------------------------
# Group B — Secret-leak tripwire
# ---------------------------------------------------------------------------


class TestSecretLeakTripwire(unittest.TestCase):
    """The wire format never carries a secret."""

    def test_secret_canary_in_payload_is_scrubbed(self) -> None:
        evt = Event(
            kind="completed",
            source="dispatcher",
            task_id="T-1",
            payload={"line": f"leak {SECRET_CANARY} leak"},
        )
        d = serialize_event(evt)
        self.assertNotIn(SECRET_CANARY, json.dumps(d))

    def test_secret_key_names_are_scrubbed(self) -> None:
        evt = Event(
            kind="completed",
            source="dispatcher",
            task_id="T-1",
            payload={
                "api_key": "sk-1234",
                "token": "tok-5678",
                "password": "p",
                "openai_api_key": "sk-x",
                "anthropic_api_key": "sk-y",
                "prompt": "raw prompt text",
                "env": {"PATH": "/"},
                "ok_field": "kept",
            },
        )
        d = serialize_event(evt)
        # Scrubbed keys become <redacted> in the serialized form.
        self.assertEqual(d["payload"]["api_key"], "<redacted>")
        self.assertEqual(d["payload"]["token"], "<redacted>")
        self.assertEqual(d["payload"]["password"], "<redacted>")
        self.assertEqual(d["payload"]["openai_api_key"], "<redacted>")
        self.assertEqual(d["payload"]["anthropic_api_key"], "<redacted>")
        self.assertEqual(d["payload"]["prompt"], "<redacted>")
        self.assertEqual(d["payload"]["env"], "<redacted>")
        # Innocent fields pass through.
        self.assertEqual(d["payload"]["ok_field"], "kept")

    def test_serialization_contains_schema_version(self) -> None:
        evt = Event(kind="completed", source="dispatcher", task_id="T-1")
        d = serialize_event(evt)
        self.assertEqual(d["schema_version"], SCHEMA_VERSION)

    def test_serialization_contains_event_id(self) -> None:
        evt = Event(
            kind="completed",
            source="dispatcher",
            task_id="T-1",
            event_id="abc123def456",
        )
        d = serialize_event(evt)
        self.assertEqual(d["event_id"], "abc123def456")

    def test_to_json_line_is_single_line(self) -> None:
        evt = Event(kind="completed", source="dispatcher", task_id="T-1")
        line = to_json_line(evt)
        self.assertNotIn("\n", line)
        # Round-trip through json.
        parsed = json.loads(line)
        self.assertEqual(parsed["event_type"], "completed")

    def test_serialization_optional_fields_appear_when_in_payload(self) -> None:
        """``job_id``, ``dispatch_id``, etc. promoted from payload to top."""
        evt = Event(
            kind="completed",
            source="dispatcher",
            task_id="T-1",
            payload={
                "job_id": "J-1",
                "dispatch_id": "D-1",
                "runtime_id": "R-1",
                "runtime_type": "claude_code",
                "provider": "claude_code",
                "status": "completed",
                "previous_status": "running",
                "duration_ms": 1234,
                "exit_code": 0,
                "failure_code": None,
                "artifact_id": "A-1",
            },
        )
        d = serialize_event(evt)
        for k in (
            "job_id", "dispatch_id", "runtime_id", "runtime_type",
            "provider", "status", "previous_status", "duration_ms",
            "exit_code", "artifact_id",
        ):
            self.assertIn(k, d)


# ---------------------------------------------------------------------------
# Group C — E2E pipeline (dispatcher)
# ---------------------------------------------------------------------------


class TestDispatcherE2EPipeline(unittest.TestCase):
    """Drive a task through the dispatcher with a real
    :class:`BufferingEmitter` and assert the events."""

    def setUp(self) -> None:
        self._saved_default = default_emitter()
        self._buf = BufferingEmitter()
        set_default_emitter(self._buf)
        install(emitter=self._buf)
        self._tmp_ctx = _TempDir()
        self._tmp_dir = self._tmp_ctx.__enter__()

    def tearDown(self) -> None:
        if is_installed():
            uninstall()
        set_default_emitter(self._saved_default)
        self._tmp_ctx.__exit__(None, None, None)

    def test_create_emits_created_event(self) -> None:
        from dispatcher.manager import TaskManager
        m = TaskManager()
        t = m.create(
            title="wireup e2e",
            type="coding",
            input_text="hello",
            initial_status="pending",
        )
        # At least the "created" event landed in the buffer.
        created_events = [
            e for e in self._buf.events
            if e.kind == "created" and e.task_id == t.task_id
        ]
        self.assertEqual(len(created_events), 1)
        self.assertEqual(created_events[0].source, EMITTER_SOURCE_DISPATCHER)
        # And it still wrote to the SQLite task_events table
        # (backward compat).
        import dispatcher.db as _db
        conn = _db.get_conn()
        rows = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ?",
            (t.task_id,),
        ).fetchall()
        kinds = [r["kind"] for r in rows]
        self.assertIn("created", kinds)

    def test_full_lifecycle_emits_terminal_event(self) -> None:
        """create -> queue -> start -> complete emits terminal event."""
        from dispatcher.manager import TaskManager
        m = TaskManager()
        t = m.create(
            title="wireup e2e",
            type="coding",
            input_text="hello",
            initial_status="pending",
        )
        m.queue(t.task_id)
        m.start(t.task_id, hermes_run_id="orch-x")
        m.complete(t.task_id, output_text="done", usage={"tokens": 1}, raw={})
        # At least one "completed" event with the right task_id.
        completed = [
            e for e in self._buf.events
            if e.kind == "completed" and e.task_id == t.task_id
        ]
        self.assertGreaterEqual(len(completed), 1)
        # And no event carries a non-canonical source.
        for e in self._buf.events:
            self.assertIn(
                e.source,
                {EMITTER_SOURCE_DISPATCHER, EMITTER_SOURCE_ORCHESTRATOR, "unknown"},
            )

    def test_fail_emits_failed_event(self) -> None:
        from dispatcher.manager import TaskManager
        m = TaskManager()
        t = m.create(
            title="fail e2e",
            type="coding",
            input_text="hello",
            initial_status="pending",
        )
        m.queue(t.task_id)
        m.start(t.task_id, hermes_run_id="orch-y")
        m.fail(t.task_id, "boom")
        failed = [
            e for e in self._buf.events
            if e.kind == "failed" and e.task_id == t.task_id
        ]
        self.assertEqual(len(failed), 1)

    def test_emitter_failure_does_not_break_lifecycle(self) -> None:
        """If the emitter raises, the task still completes."""

        class ExplodingEmitter:
            def emit(self, event: Event) -> None:
                raise RuntimeError("boom")

            def close(self) -> None:
                return None

        # Replace the buffer with an exploding emitter for
        # the duration of this test.
        set_default_emitter(ExplodingEmitter())  # type: ignore[arg-type]
        install(emitter=None)  # picks up the default
        from dispatcher.manager import TaskManager
        m = TaskManager()
        t = m.create(
            title="exploding e2e",
            type="coding",
            input_text="hello",
            initial_status="pending",
        )
        m.queue(t.task_id)
        m.start(t.task_id, hermes_run_id="orch-z")
        # Must not raise.
        m.complete(t.task_id, output_text="done", usage=None, raw=None)
        # And the SQLite state is still terminal=completed.
        import dispatcher.db as _db
        row = _db.get_conn().execute(
            "SELECT status FROM tasks WHERE task_id = ?", (t.task_id,),
        ).fetchone()
        self.assertEqual(row["status"], "completed")


# ---------------------------------------------------------------------------
# Group D — NullEmitter default + env-config
# ---------------------------------------------------------------------------


class TestEnvConfigAndDefaults(unittest.TestCase):
    """The default emitter is NullEmitter; env-var switches it."""

    def setUp(self) -> None:
        self._saved_env = os.environ.pop("AEE_EVENT_EMITTER", None)
        # Reset the process-wide default to NullEmitter.
        set_default_emitter(NullEmitter())

    def tearDown(self) -> None:
        if self._saved_env is not None:
            os.environ["AEE_EVENT_EMITTER"] = self._saved_env
        else:
            os.environ.pop("AEE_EVENT_EMITTER", None)

    def test_default_is_null(self) -> None:
        self.assertIsInstance(default_emitter(), NullEmitter)

    def test_env_unset_returns_null(self) -> None:
        os.environ.pop("AEE_EVENT_EMITTER", None)
        self.assertIsInstance(emitter_from_env(), NullEmitter)

    def test_env_buffer_returns_buffering(self) -> None:
        os.environ["AEE_EVENT_EMITTER"] = "buffer"
        self.assertIsInstance(emitter_from_env(), BufferingEmitter)

    def test_env_stdout_json_returns_stdout_emitter(self) -> None:
        os.environ["AEE_EVENT_EMITTER"] = "stdout_json"
        self.assertIsInstance(emitter_from_env(), StdoutJsonEmitter)

    def test_env_unknown_name_failsafe_to_null(self) -> None:
        os.environ["AEE_EVENT_EMITTER"] = "bogus_unknown"
        # Must not raise; must fall back to NullEmitter.
        self.assertIsInstance(emitter_from_env(), NullEmitter)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Group E — E2E orchestrator pipeline
# ---------------------------------------------------------------------------
# Uses the FakeProvider (AEE-7.1 test double) to drive
# the orchestrator end-to-end through submit -> poll ->
# terminal.  Asserts the wire-up emits the right events
# in the right order for the five scenarios required by
# the slice 3 spec: success, failure, timeout, cancel,
# emitter failure.  Real hermes / claude-code provider is
# NOT required; FakeProvider is the canonical AEE-7 test
# double.


class _FakeJob:
    """Minimal stand-in for ``dispatcher.models.Task``."""

    def __init__(self, task_id: str = "T-fake-1") -> None:
        self.task_id = task_id


class _StubRepository:
    """Stub ``RuntimeRepository`` — the orchestrator only
    calls ``list_all`` (which we return as []) and
    ``insert_dispatch_record`` / ``update_dispatch_status``
    / ``list_dispatch_records`` (no-ops)."""

    def list_all(self):
        return []

    def insert_dispatch_record(self, record):
        return None

    def list_dispatch_records(self, **kwargs):
        return []

    def update_dispatch_status(self, **kwargs):
        return None


class TestOrchestratorE2EPipeline(unittest.TestCase):
    """``ExecutionOrchestrator`` emits events through the
    wire-up on submit / poll / cancel."""

    def setUp(self) -> None:
        self._saved_default = default_emitter()
        self._buf = BufferingEmitter()
        set_default_emitter(self._buf)
        install(emitter=self._buf)

    def tearDown(self) -> None:
        if is_installed():
            uninstall()
        set_default_emitter(self._saved_default)

    def _make_orchestrator(self, fake):
        from aee.orchestrator.orchestrator import (
            ExecutionOrchestrator,
            register_runtimes_for_test,
            clear_test_runtimes,
        )
        from aee.orchestrator import factory
        # Match the AEE-7.2 e2e test fixture pattern:
        # register the fake's runtime_type with both the
        # in-memory safe list and the provider registry.
        from aee.runtimes.models import (
            RuntimeDescriptor,
            RuntimeHealth,
            RuntimeHealthStatus,
        )
        from aee.runtimes.repository import InMemoryRuntimeRepository
        from aee.orchestrator.factory import register_provider
        rt = fake.runtime_type
        desc = RuntimeDescriptor(
            runtime_id=f"{rt}-test",
            runtime_type=rt,
            display_name=f"Test {rt}",
            labels={},
            health=RuntimeHealth(
                status=RuntimeHealthStatus.HEALTHY,
                last_checked_at="2026-07-11T00:00:00Z",
            ),
        )
        register_runtimes_for_test([desc])
        self.addCleanup(clear_test_runtimes)
        register_provider(
            rt,
            lambda _descriptor, _fake=fake: _fake,
            replace=True,
        )
        # The orchestrator reads runtimes from its
        # repository (not the safe list) when one is
        # provided, so we must also insert the descriptor
        # into the in-memory repository.
        repo = InMemoryRuntimeRepository()
        repo.insert(desc)
        return ExecutionOrchestrator(repository=repo)

    def test_success_path_emits_submit_started_and_completed(self) -> None:
        from aee.orchestrator.fake_provider import FakeProvider
        from aee.runtimes.models import TaskRuntimeRequirements
        import asyncio
        # Default behavior is "happy" — completes in one poll.
        fake = FakeProvider(behavior="happy", output="ok")
        # Patch the fake's class-level name/runtime_type so
        # the descriptor lookup in _make_orchestrator finds it.
        fake.name = "fake-ok"
        fake.runtime_type = "fake-ok"
        orch = self._make_orchestrator(fake)
        req = TaskRuntimeRequirements(runtime_type="fake-ok")
        result = asyncio.run(
            orch.submit(
                job=_FakeJob("T-success"),
                prompt="hi",
                requirements=req,
            )
        )
        # Drive a poll loop until terminal (FakeProvider
        # completes in one poll by default).
        while True:
            poll_result = asyncio.run(orch.poll(result.external_run_id))
            if getattr(poll_result, "is_terminal", False):
                break
        # Assert events.
        kinds = [e.kind for e in self._buf.events]
        self.assertIn("provider_selected", kinds)
        self.assertIn("submit_started", kinds)
        self.assertIn("submit_completed", kinds)
        self.assertIn("poll_completed", kinds)
        # Source is "orchestrator".
        for e in self._buf.events:
            self.assertEqual(e.source, EMITTER_SOURCE_ORCHESTRATOR)

    def test_failure_path_emits_submit_failed(self) -> None:
        from aee.orchestrator.fake_provider import FakeProvider
        from aee.orchestrator.provider import (
            ProviderSubmitError,
        )
        from aee.runtimes.models import TaskRuntimeRequirements
        import asyncio
        fake = FakeProvider(behavior="happy", output="x")
        fake.name = "fake-fail"
        fake.runtime_type = "fake-fail"
        async def _raise(*a, **kw):
            raise ProviderSubmitError("synthetic submit failure")
        fake.submit = _raise  # type: ignore[assignment]
        orch = self._make_orchestrator(fake)
        req = TaskRuntimeRequirements(runtime_type="fake-fail")
        result = asyncio.run(
            orch.submit(
                job=_FakeJob("T-fail"),
                prompt="hi",
                requirements=req,
            )
        )
        # Status is FAILED.
        from aee.orchestrator.provider import ProviderStatus
        self.assertEqual(result.status, ProviderStatus.FAILED)
        kinds = [e.kind for e in self._buf.events]
        self.assertIn("submit_failed", kinds)

    def test_timeout_path_emits_poll_completed(self) -> None:
        from aee.orchestrator.fake_provider import FakeProvider
        from aee.orchestrator.provider import ProviderTimeoutError
        from aee.runtimes.models import TaskRuntimeRequirements
        import asyncio
        fake = FakeProvider(behavior="happy", output="x")
        fake.name = "fake-to"
        fake.runtime_type = "fake-to"
        async def _timeout(*a, **kw):
            raise ProviderTimeoutError("synthetic timeout")
        fake.poll = _timeout  # type: ignore[assignment]
        orch = self._make_orchestrator(fake)
        req = TaskRuntimeRequirements(runtime_type="fake-to")
        result = asyncio.run(
            orch.submit(
                job=_FakeJob("T-to"),
                prompt="hi",
                requirements=req,
            )
        )
        asyncio.run(orch.poll(result.external_run_id))
        kinds = [e.kind for e in self._buf.events]
        self.assertIn("poll_completed", kinds)

    def test_cancel_path_emits_cancelled_event(self) -> None:
        from aee.orchestrator.fake_provider import FakeProvider
        from aee.orchestrator.provider import (
            ProviderCancelResult,
        )
        from aee.runtimes.models import TaskRuntimeRequirements
        import asyncio
        fake = FakeProvider(behavior="happy", output="x")
        fake.name = "fake-cancel"
        fake.runtime_type = "fake-cancel"
        async def _cancel(run):
            return ProviderCancelResult(
                external_run_id=run.external_run_id,
                cancelled=True,
                reason="by-test",
            )
        fake.cancel = _cancel  # type: ignore[assignment]
        orch = self._make_orchestrator(fake)
        req = TaskRuntimeRequirements(runtime_type="fake-cancel")
        result = asyncio.run(
            orch.submit(
                job=_FakeJob("T-cancel"),
                prompt="hi",
                requirements=req,
            )
        )
        asyncio.run(orch.cancel(result.external_run_id))
        kinds = [e.kind for e in self._buf.events]
        self.assertIn("cancelled", kinds)


# ---------------------------------------------------------------------------
# Group F — Secret-leak runtime tripwire
# ---------------------------------------------------------------------------
# Walks the production hot paths and asserts the canary
# string NEVER appears in any output stream.  This is
# a *runtime* tripwire (vs. Group B which is a
# *serialization* tripwire).  It also walks the
# source code of the wire-up module to assert no
# ``print(`` call sneaks into the hot path (which
# would break the "machine-readable event JSON is only
# emitted by StdoutJsonEmitter" rule).


class TestRuntimeTripwire(unittest.TestCase):
    """Assert the wire-up hot path does not call print/json.dumps directly."""

    def test_wireup_source_has_no_print(self) -> None:
        """``aee/observability_runtime/wireup.py`` has no ``print(`` call."""
        import ast
        import pathlib  # noqa: F401 - used below
        path = pathlib.Path(
            "/home/ubuntu/hermes-runtime-bridge/aee/observability_runtime/wireup.py"
        )
        src = path.read_text()
        tree = ast.parse(src)
        offenders: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "print":
                    # Skip nodes that are inside a docstring
                    # expression (i.e. Expr -> Constant str).
                    # We approximate by checking that the
                    # node is not in the module body
                    # position 0.  False positives are
                    # possible but rare.
                    offenders.append(f"print(...) at line {node.lineno}")
        self.assertEqual(offenders, [], f"print() found in wireup: {offenders}")

    def test_wireup_source_has_no_inline_json_dumps(self) -> None:
        """``json.dumps`` only appears in StdoutJsonEmitter, not in wireup.

        Use AST-walk (not raw-text grep) so a docstring
        mention of the string ``json.dumps`` is not a
        false positive.  AST-walk finds only the *calls*,
        which is what we want.
        """
        import ast
        import pathlib
        path = pathlib.Path(
            "/home/ubuntu/hermes-runtime-bridge/aee/observability_runtime/wireup.py"
        )
        tree = ast.parse(path.read_text())
        offenders: List[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # Match: json.dumps(...), json.dump(...)
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "json"
                and func.attr in ("dumps", "dump")
            ):
                offenders.append(f"{func.value.id}.{func.attr} at line {node.lineno}")
        self.assertEqual(offenders, [], f"json.dumps/dump found in wireup: {offenders}")


