"""AEE-7.2 — Dispatcher E2E tests for the corrected ``dispatch_status`` flow.

AEE-7.1 surfaced a critical bug: ``dispatch_records.dispatch_status``
was written as ``failed`` even when the orchestrator completed
cleanly. The root cause was inline status mapping at two
write-sites (submit-time and post-poll) that did not stay in
sync, and a missing ``DispatchStatus.COMPLETED`` literal.

AEE-7.2 fixes the bug by introducing a single mapping helper
``_map_provider_status_to_dispatch`` in
``aee/orchestrator/orchestrator.py`` and expanding
``DispatchStatus`` with the missing terminal literals. These
tests exercise the **fixed** contract end-to-end through a
real ``ExecutionOrchestrator`` driving a ``FakeProvider``, with
a real ``InMemoryRuntimeRepository`` capturing the audit
records.

Coverage
--------
* submit -> poll COMPLETED  → ``dispatch_status='completed'``,
  ``failure_code=None``
* submit FAIL at submit-time  → ``dispatch_status='failed'``,
  ``failure_code='submit_error'`` (or as reported)
* poll FAILED                 → ``dispatch_status='failed'``,
  ``failure_code='provider_failed'``
* poll TIMEOUT                → ``dispatch_status='timed_out'``,
  ``failure_code='provider_timeout'``
* cancel                      → ``dispatch_status='cancelled'``,
  ``failure_code='provider_cancelled'``
* legacy hermes path unaffected
"""
from __future__ import annotations

import asyncio
import unittest
from typing import List

from aee.orchestrator import (
    ExecutionOrchestrator,
    ProviderStatus,
    RuntimeDescriptor,
    RuntimeHealth,
    RuntimeHealthStatus,
    register_provider,
    unregister_provider,
)
from aee.orchestrator.orchestrator import (
    _map_provider_status_to_dispatch,
    clear_test_runtimes,
    register_runtimes_for_test,
)
from aee.orchestrator.fake_provider import FakeProvider
from aee.orchestrator.provider import ProviderError, ProviderSubmitError
from aee.runtimes.models import DispatchStatus, TaskRuntimeRequirements
from aee.runtimes.repository import InMemoryRuntimeRepository


# ---------------------------------------------------------------------------
# Mapping helper unit tests (no orchestrator)
# ---------------------------------------------------------------------------


class TestMappingHelper(unittest.TestCase):
    """The helper is the *single source of truth* — pin it here."""

    def test_provider_queued_maps_to_dispatched(self) -> None:
        self.assertEqual(
            _map_provider_status_to_dispatch(ProviderStatus.QUEUED.value),
            DispatchStatus.DISPATCHED,
        )

    def test_provider_running_maps_to_dispatched(self) -> None:
        self.assertEqual(
            _map_provider_status_to_dispatch(ProviderStatus.RUNNING.value),
            DispatchStatus.DISPATCHED,
        )

    def test_provider_completed_maps_to_completed(self) -> None:
        self.assertEqual(
            _map_provider_status_to_dispatch(ProviderStatus.COMPLETED.value),
            DispatchStatus.COMPLETED,
        )

    def test_provider_failed_maps_to_failed(self) -> None:
        self.assertEqual(
            _map_provider_status_to_dispatch(ProviderStatus.FAILED.value),
            DispatchStatus.FAILED,
        )

    def test_provider_timeout_maps_to_timed_out(self) -> None:
        self.assertEqual(
            _map_provider_status_to_dispatch(ProviderStatus.TIMEOUT.value),
            DispatchStatus.TIMED_OUT,
        )

    def test_provider_cancelled_maps_to_cancelled(self) -> None:
        self.assertEqual(
            _map_provider_status_to_dispatch(ProviderStatus.CANCELLED.value),
            DispatchStatus.CANCELLED,
        )

    def test_unknown_status_falls_back_to_failed(self) -> None:
        # Defensive: an unknown status must not produce the
        # misleading ``selected`` literal; it must fall back to
        # ``failed`` so the audit row is at least truthful.
        self.assertEqual(
            _map_provider_status_to_dispatch("garbage"),
            DispatchStatus.FAILED,
        )
        self.assertEqual(
            _map_provider_status_to_dispatch(""),
            DispatchStatus.FAILED,
        )


# ---------------------------------------------------------------------------
# DispatchStatus enum completeness
# ---------------------------------------------------------------------------


class TestDispatchStatusEnum(unittest.TestCase):
    """AEE-7.2 adds COMPLETED / TIMED_OUT / CANCELLED to the enum.

    Other parts of the code (e.g. ``_PROVIDER_TO_DISPATCH``) and
    external tools (operational dashboards, audit reports) read
    these literals. Verify they exist with the right string
    values.
    """

    def test_all_literals_present(self) -> None:
        for attr, expected in [
            ("SELECTED", "selected"),
            ("DISPATCHED", "dispatched"),
            ("COMPLETED", "completed"),
            ("FAILED", "failed"),
            ("TIMED_OUT", "timed_out"),
            ("CANCELLED", "cancelled"),
        ]:
            with self.subTest(attr=attr):
                self.assertTrue(hasattr(DispatchStatus, attr))
                self.assertEqual(getattr(DispatchStatus, attr), expected)

    def test_all_includes_all_literals(self) -> None:
        # The class-level ``ALL`` tuple is what DB queries use to
        # assert membership (e.g. for "is this a known status?").
        all_lits = set(DispatchStatus.ALL)
        for lit in (
            "selected", "dispatched", "completed", "failed",
            "timed_out", "cancelled",
        ):
            self.assertIn(lit, all_lits)


# ---------------------------------------------------------------------------
# Helpers for orchestrator-driven tests
# ---------------------------------------------------------------------------


def _fake_descriptor() -> RuntimeDescriptor:
    return RuntimeDescriptor(
        runtime_id="fake-runtime",
        runtime_type="fake",
        display_name="Fake (test-only)",
        labels={},
        health=RuntimeHealth(
            status=RuntimeHealthStatus.HEALTHY,
            last_checked_at="2026-07-11T00:00:00Z",
        ),
    )


def _setup_with_repo() -> tuple[FakeProvider, InMemoryRuntimeRepository]:
    """Register a fake-typed descriptor + FakeProvider with a fresh
    in-memory repository. The repository is the
    ``dispatch_records`` audit store AND the runtime registry the
    orchestrator reads from."""
    desc = _fake_descriptor()
    fake = FakeProvider(descriptor=desc, behavior="happy")
    register_provider(
        "fake", lambda _descriptor, _fake=fake: _fake, replace=True
    )
    # The test-safe in-memory list is what the orchestrator
    # uses when no repository is supplied. With a repository
    # we must also ``insert`` the descriptor there so the
    # selector can find it.
    register_runtimes_for_test([desc])
    repo = InMemoryRuntimeRepository()
    repo.insert(desc)
    return fake, repo


class _FakeJob:
    """Minimal stand-in for ``dispatcher.models.Task``."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id


def _records_for(repo: InMemoryRuntimeRepository, run_id: str) -> List:
    out = [
        r for r in repo.list_dispatch_records(limit=200) if r.run_id == run_id
    ]
    out.sort(key=lambda r: r.dispatched_at, reverse=True)
    return out


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestDispatcherE2ECompleted(unittest.TestCase):
    def setUp(self) -> None:
        self._fake, self._repo = _setup_with_repo()
        self._orch = ExecutionOrchestrator(repository=self._repo)

    def tearDown(self) -> None:
        clear_test_runtimes()
        unregister_provider("fake")

    def test_completed_run_writes_completed_status(self) -> None:
        req = TaskRuntimeRequirements(runtime_type="fake")
        result = asyncio.run(
            self._orch.submit(
                job=_FakeJob(task_id="t-ok"),
                prompt="x",
                requirements=req,
            )
        )
        polled = asyncio.run(self._orch.poll(result))
        self.assertEqual(polled.status, ProviderStatus.COMPLETED)

        records = _records_for(self._repo, result.external_run_id)
        self.assertEqual(len(records), 1, "exactly one dispatch record")
        rec = records[0]
        # AEE-7.2 fix: dispatch_status must be ``completed`` for a
        # clean run, not ``failed`` (the AEE-7.1 bug).
        self.assertEqual(rec.dispatch_status, DispatchStatus.COMPLETED)
        self.assertIsNone(rec.failure_code)
        # failure_message may be set to "exit_code=0" — the
        # post-poll update records the exit code even on
        # success. The bug we fixed was status=failed with
        # failure_code=NULL, not a non-empty failure_message.


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


class TestDispatcherE2EFailed(unittest.TestCase):
    def setUp(self) -> None:
        self._fake, self._repo = _setup_with_repo()
        self._orch = ExecutionOrchestrator(repository=self._repo)

    def tearDown(self) -> None:
        clear_test_runtimes()
        unregister_provider("fake")

    def test_poll_nonzero_exit_writes_failed(self) -> None:
        # No error message on the failed poll: ``error=None``
        # makes the orchestrator classify this as
        # ``provider_failed`` rather than ``submit_error``
        # (which implies a synchronous submit exception).
        self._fake.set_behavior(
            behavior="failing", fail_at="poll", fail_message=""
        )
        req = TaskRuntimeRequirements(runtime_type="fake")
        result = asyncio.run(
            self._orch.submit(
                job=_FakeJob(task_id="t-fail-poll"),
                prompt="x",
                requirements=req,
            )
        )
        polled = asyncio.run(self._orch.poll(result))
        self.assertEqual(polled.status, ProviderStatus.FAILED)
        records = _records_for(self._repo, result.external_run_id)
        # The orchestrator wrote a row at submit-time (running)
        # and updated it at poll-time (failed). The most recent
        # row is the post-poll update.
        rec = records[0]
        self.assertEqual(rec.dispatch_status, DispatchStatus.FAILED)
        # failure_code carries the *reason*, not the literal
        # status. ``provider_failed`` means a non-zero exit
        # (provider did its job but reported a problem).
        self.assertEqual(rec.failure_code, "provider_failed")

    def test_submit_failure_writes_failed_with_submit_error(self) -> None:
        self._fake.set_behavior(behavior="failing", fail_at="submit")
        req = TaskRuntimeRequirements(runtime_type="fake")
        result = asyncio.run(
            self._orch.submit(
                job=_FakeJob(task_id="t-fail-submit"),
                prompt="x",
                requirements=req,
            )
        )
        # Submit raised; the orchestrator translates that into
        # a FAILED result with the class name in ``error``.
        self.assertEqual(result.status, ProviderStatus.FAILED)
        self.assertTrue(result.is_terminal)
        records = _records_for(self._repo, result.external_run_id)
        rec = records[0]
        self.assertEqual(rec.dispatch_status, DispatchStatus.FAILED)
        # failure_code distinguishes synchronous submit error
        # from asynchronous provider failure.
        self.assertEqual(rec.failure_code, "submit_error")


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


class TestDispatcherE2ETimeout(unittest.TestCase):
    def setUp(self) -> None:
        self._fake, self._repo = _setup_with_repo()
        self._orch = ExecutionOrchestrator(repository=self._repo)

    def tearDown(self) -> None:
        clear_test_runtimes()
        unregister_provider("fake")

    def test_hanging_run_writes_dispatched_until_polled(self) -> None:
        # FakeProvider behavior="hanging" returns RUNNING
        # indefinitely — the orchestrator's poll() will not
        # observe a terminal state. In production, the
        # dispatcher's reaper (Phase 4) would mark this as
        # TIMED_OUT after a deadline. For this test we only
        # verify the submit-time audit row.
        self._fake.set_behavior(behavior="hanging")
        req = TaskRuntimeRequirements(runtime_type="fake")
        result = asyncio.run(
            self._orch.submit(
                job=_FakeJob(task_id="t-hang"),
                prompt="x",
                requirements=req,
            )
        )
        records = _records_for(self._repo, result.external_run_id)
        rec = records[0]
        # Submit-time status: running → dispatched.
        self.assertEqual(rec.dispatch_status, DispatchStatus.DISPATCHED)
        self.assertIsNone(rec.failure_code)

    def test_explicit_timeout_status_writes_timed_out(self) -> None:
        # Some providers can report TIMEOUT directly (e.g. a
        # provider with a built-in deadline). Drive a happy
        # submit then update via _update_dispatch_status
        # directly to verify the mapping.
        self._fake.set_behavior(behavior="happy")
        req = TaskRuntimeRequirements(runtime_type="fake")
        result = asyncio.run(
            self._orch.submit(
                job=_FakeJob(task_id="t-to"),
                prompt="x",
                requirements=req,
            )
        )
        asyncio.run(self._orch.poll(result))
        # Simulate the reaper's terminal update.
        self._orch._update_dispatch_status(
            result.external_run_id, ProviderStatus.TIMEOUT.value
        )
        records = _records_for(self._repo, result.external_run_id)
        rec = records[0]
        self.assertEqual(rec.dispatch_status, DispatchStatus.TIMED_OUT)
        self.assertEqual(rec.failure_code, "provider_timeout")


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


class TestDispatcherE2ECancel(unittest.TestCase):
    def setUp(self) -> None:
        self._fake, self._repo = _setup_with_repo()
        self._orch = ExecutionOrchestrator(repository=self._repo)

    def tearDown(self) -> None:
        clear_test_runtimes()
        unregister_provider("fake")

    def test_cancel_writes_cancelled(self) -> None:
        req = TaskRuntimeRequirements(runtime_type="fake")
        result = asyncio.run(
            self._orch.submit(
                job=_FakeJob(task_id="t-cancel"),
                prompt="x",
                requirements=req,
            )
        )
        # Cancel before polling.
        cancel_result = asyncio.run(self._orch.cancel(result.external_run_id))
        self.assertTrue(cancel_result.cancelled)
        records = _records_for(self._repo, result.external_run_id)
        rec = records[0]
        self.assertEqual(rec.dispatch_status, DispatchStatus.CANCELLED)
        self.assertEqual(rec.failure_code, "provider_cancelled")


# ---------------------------------------------------------------------------
# Provider not configured
# ---------------------------------------------------------------------------


class TestDispatcherE2EMissingProvider(unittest.TestCase):
    def setUp(self) -> None:
        # No descriptor registered. The selector must reject.
        clear_test_runtimes()
        self._orch = ExecutionOrchestrator(
            repository=InMemoryRuntimeRepository()
        )

    def tearDown(self) -> None:
        clear_test_runtimes()

    def test_unknown_runtime_type_raises(self) -> None:
        from aee.orchestrator import RuntimeNotFoundError
        req = TaskRuntimeRequirements(runtime_type="does-not-exist")
        with self.assertRaises(RuntimeNotFoundError):
            asyncio.run(
                self._orch.submit(
                    job=_FakeJob(task_id="t-np"),
                    prompt="x",
                    requirements=req,
                )
            )


# ---------------------------------------------------------------------------
# Legacy hermes path
# ---------------------------------------------------------------------------


class TestDispatcherE2EHermesPathUnaffected(unittest.TestCase):
    """The ``hermes`` provider shim must continue to work after
    AEE-7.2 changes. The mapping helper is provider-agnostic
    (it operates on status strings), so a regression here
    would mean we accidentally hard-coded a runtime_type
    somewhere."""

    def test_provider_to_dispatch_is_pure(self) -> None:
        # The helper has no side effects, no global state, and
        # no I/O. Verify it is callable from a sync context
        # without any orchestrator setup.
        for value, expected in [
            (ProviderStatus.QUEUED.value, DispatchStatus.DISPATCHED),
            (ProviderStatus.RUNNING.value, DispatchStatus.DISPATCHED),
            (ProviderStatus.COMPLETED.value, DispatchStatus.COMPLETED),
            (ProviderStatus.FAILED.value, DispatchStatus.FAILED),
            (ProviderStatus.TIMEOUT.value, DispatchStatus.TIMED_OUT),
            (ProviderStatus.CANCELLED.value, DispatchStatus.CANCELLED),
        ]:
            with self.subTest(value=value):
                self.assertEqual(
                    _map_provider_status_to_dispatch(value), expected
                )


# ---------------------------------------------------------------------------
# AEE-7.3 — failure_code single-source-of-truth tests
# ---------------------------------------------------------------------------


class TestFailureCodeSOT(unittest.TestCase):
    """AEE-7.3 — pin ``_failure_code_for_status`` as the
    single source of truth for ``dispatch_records.failure_code``.

    AEE-7.2 left the ``failure_code`` mapping duplicated across
    two write sites (initial submit row + post-poll terminal
    update) with magic strings inlined. AEE-7.3 extracts a
    :class:`FailureCode` vocabulary and a single helper. These
    tests pin the contract:

    * the helper returns ``None`` for ``completed`` / empty /
      unknown / non-terminal status (in-flight is not a failure)
    * the helper returns ``submit_error`` when status is
      ``failed`` *and* an error message is provided
      (synchronous submit exception)
    * the helper returns ``provider_failed`` when status is
      ``failed`` *and* no error message is provided
      (asynchronous poll-time exit)
    * the helper returns ``provider_timeout`` for
      ``timeout`` and ``provider_cancelled`` for ``cancelled``
    * the helper is the only place in the module that resolves
      a status to a failure code (no duplicate enum literal,
      no fallback ternary, no magic string)
    """

    def _helper(self):
        from aee.orchestrator.orchestrator import (
            FailureCode,
            _failure_code_for_status,
        )
        return _failure_code_for_status, FailureCode

    def test_completed_returns_none(self) -> None:
        helper, _ = self._helper()
        self.assertIsNone(helper(ProviderStatus.COMPLETED.value, error=None))

    def test_empty_status_returns_none(self) -> None:
        helper, _ = self._helper()
        self.assertIsNone(helper("", error=None))

    def test_unknown_status_returns_none(self) -> None:
        helper, _ = self._helper()
        # An unmapped status must NOT invent a code; the row
        # is left with failure_code=NULL and ops notices the
        # anomaly in the status column.
        self.assertIsNone(helper("nonsense", error=None))

    def test_running_returns_none(self) -> None:
        helper, _ = self._helper()
        self.assertIsNone(helper(ProviderStatus.RUNNING.value, error=None))

    def test_queued_returns_none(self) -> None:
        helper, _ = self._helper()
        self.assertIsNone(helper(ProviderStatus.QUEUED.value, error=None))

    def test_failed_with_error_returns_submit_error(self) -> None:
        helper, FailureCode = self._helper()
        self.assertEqual(
            helper(ProviderStatus.FAILED.value, error="boom"),
            FailureCode.SUBMIT_ERROR,
        )

    def test_failed_without_error_returns_provider_failed(self) -> None:
        helper, FailureCode = self._helper()
        self.assertEqual(
            helper(ProviderStatus.FAILED.value, error=None),
            FailureCode.PROVIDER_FAILED,
        )

    def test_failed_with_empty_error_returns_provider_failed(self) -> None:
        helper, FailureCode = self._helper()
        # Empty string is treated like None for the
        # disambiguation (a blank error is not a real
        # submit-time error).
        self.assertEqual(
            helper(ProviderStatus.FAILED.value, error=""),
            FailureCode.PROVIDER_FAILED,
        )

    def test_timeout_returns_provider_timeout(self) -> None:
        helper, FailureCode = self._helper()
        self.assertEqual(
            helper(ProviderStatus.TIMEOUT.value, error=None),
            FailureCode.PROVIDER_TIMEOUT,
        )

    def test_cancelled_returns_provider_cancelled(self) -> None:
        helper, FailureCode = self._helper()
        self.assertEqual(
            helper(ProviderStatus.CANCELLED.value, error=None),
            FailureCode.PROVIDER_CANCELLED,
        )

    def test_vocabulary_is_frozen_and_unique(self) -> None:
        """All ``FailureCode`` constants are unique and form
        a complete vocabulary — no duplicate enum literal, no
        magic string allowed at call sites."""
        _, FailureCode = self._helper()
        codes = FailureCode.all()
        self.assertEqual(len(codes), 4)
        # Round-trip: every code must equal its attribute value
        # (i.e. no one is a re-export / alias of another).
        self.assertEqual(
            codes,
            frozenset(
                {
                    FailureCode.SUBMIT_ERROR,
                    FailureCode.PROVIDER_FAILED,
                    FailureCode.PROVIDER_TIMEOUT,
                    FailureCode.PROVIDER_CANCELLED,
                }
            ),
        )

    def test_helper_is_pure(self) -> None:
        """Same inputs → same output. No globals, no I/O."""
        helper, _ = self._helper()
        first = helper(ProviderStatus.FAILED.value, error="x")
        second = helper(ProviderStatus.FAILED.value, error="x")
        self.assertEqual(first, second)
        # And mutating the input (which the helper shouldn't
        # but we guard) must not affect a fresh call.
        first_again = helper(ProviderStatus.FAILED.value, error="x")
        self.assertEqual(first, first_again)


class TestFailureCodeSharedAcrossWriteSites(unittest.TestCase):
    """AEE-7.3 — verify the two ``dispatch_records`` write sites
    both use the *same* ``_failure_code_for_status`` helper.

    Concretely: the success row written at submit time, and the
    terminal row updated at poll time, must agree on the
    failure_code vocabulary. If they don't, ops dashboards
    (e.g. "show me every submit_error in the last 7 days")
    would silently miss half the rows.
    """

    def setUp(self) -> None:
        self._fake, self._repo = _setup_with_repo()
        self._orch = ExecutionOrchestrator(repository=self._repo)

    def tearDown(self) -> None:
        clear_test_runtimes()
        unregister_provider("fake")

    def test_submit_time_and_post_poll_agree_on_completed(self) -> None:
        """Submit row (running→dispatched) and post-poll row
        (completed) must BOTH call the SOT. Submit row has
        failure_code=NULL because submit succeeded; post-poll
        row also has failure_code=NULL because run completed."""
        req = TaskRuntimeRequirements(runtime_type="fake")
        result = asyncio.run(
            self._orch.submit(
                job=_FakeJob(task_id="t-share-completed"),
                prompt="x",
                requirements=req,
            )
        )
        asyncio.run(self._orch.poll(result))
        records = _records_for(self._repo, result.external_run_id)
        # _records_for returns the *most recent first*. The
        # update path overwrites in place (no insert), so
        # there is only one row. failure_code is None on it.
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].dispatch_status, DispatchStatus.COMPLETED)
        self.assertIsNone(records[0].failure_code)

    def test_submit_failure_row_uses_sot_vocabulary(self) -> None:
        """Synchronous submit failure → row exists with
        failure_code='submit_error' (FailureCode.SUBMIT_ERROR)."""
        from aee.orchestrator.orchestrator import FailureCode

        self._fake.set_behavior(behavior="failing", fail_at="submit")
        req = TaskRuntimeRequirements(runtime_type="fake")
        result = asyncio.run(
            self._orch.submit(
                job=_FakeJob(task_id="t-share-submit"),
                prompt="x",
                requirements=req,
            )
        )
        records = _records_for(self._repo, result.external_run_id)
        self.assertEqual(records[0].failure_code, FailureCode.SUBMIT_ERROR)

    def test_poll_failure_row_uses_sot_vocabulary(self) -> None:
        """Asynchronous poll failure → row exists with
        failure_code='provider_failed' (FailureCode.PROVIDER_FAILED)."""
        from aee.orchestrator.orchestrator import FailureCode

        self._fake.set_behavior(
            behavior="failing", fail_at="poll", fail_message=""
        )
        req = TaskRuntimeRequirements(runtime_type="fake")
        result = asyncio.run(
            self._orch.submit(
                job=_FakeJob(task_id="t-share-poll"),
                prompt="x",
                requirements=req,
            )
        )
        asyncio.run(self._orch.poll(result))
        records = _records_for(self._repo, result.external_run_id)
        self.assertEqual(records[0].failure_code, FailureCode.PROVIDER_FAILED)

    def test_post_poll_timeout_uses_sot_vocabulary(self) -> None:
        """Post-poll terminal update with TIMEOUT status
        → failure_code='provider_timeout'."""
        from aee.orchestrator.orchestrator import FailureCode

        self._fake.set_behavior(behavior="happy")
        req = TaskRuntimeRequirements(runtime_type="fake")
        result = asyncio.run(
            self._orch.submit(
                job=_FakeJob(task_id="t-share-to"),
                prompt="x",
                requirements=req,
            )
        )
        asyncio.run(self._orch.poll(result))
        # Simulate the dispatcher's reaper applying a
        # timeout update via the SOT.
        self._orch._update_dispatch_status(
            result.external_run_id, ProviderStatus.TIMEOUT.value
        )
        records = _records_for(self._repo, result.external_run_id)
        self.assertEqual(records[0].failure_code, FailureCode.PROVIDER_TIMEOUT)

    def test_post_poll_cancelled_uses_sot_vocabulary(self) -> None:
        """Post-poll cancel() → failure_code='provider_cancelled'."""
        from aee.orchestrator.orchestrator import FailureCode

        req = TaskRuntimeRequirements(runtime_type="fake")
        result = asyncio.run(
            self._orch.submit(
                job=_FakeJob(task_id="t-share-cancel"),
                prompt="x",
                requirements=req,
            )
        )
        asyncio.run(self._orch.cancel(result.external_run_id))
        records = _records_for(self._repo, result.external_run_id)
        self.assertEqual(
            records[0].failure_code, FailureCode.PROVIDER_CANCELLED
        )


class TestOrchestratorHasNoInlineFailureCodeMapping(unittest.TestCase):
    """AEE-7.3 — structural assertion. The two call sites
    (``_persist_dispatch`` and ``_update_dispatch_status``)
    must NOT inline any failure_code string literal. They
    must use the SOT helper.

    This guards against the regression that motivated the
    rescue: someone copy-pastes a fix into one site and
    forgets the other, and the two sites drift on the
    vocabulary. The source-text check is a coarse but
    reliable tripwire.
    """

    FORBIDDEN_LITERALS = (
        '"submit_error"',
        '"provider_failed"',
        '"provider_timeout"',
        '"provider_cancelled"',
    )

    def test_no_inline_failure_code_literal_outside_vocabulary(self) -> None:
        """AEE-7.3 — the *only* place a failure_code string
        literal may appear in ``orchestrator.py`` is inside the
        :class:`FailureCode` class body (the single source of
        truth). Any other occurrence is a regression."""
        from aee.orchestrator import orchestrator as orch_mod
        import inspect

        orch_cls = orch_mod.ExecutionOrchestrator
        # Inspect the class source, *excluding* the module
        # top-level (where FailureCode lives). This way the
        # only acceptable literals are the FailureCode
        # attribute values themselves; the audit flag must
        # not fire on the class.
        cls_src = inspect.getsource(orch_cls)
        violations: list = []
        for n, line in enumerate(cls_src.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            code = line.split("#", 1)[0]
            for lit in self.FORBIDDEN_LITERALS:
                if lit in code:
                    violations.append(
                        f"class line {n}: forbidden literal {lit} :: {line!r}"
                    )
        self.assertEqual(
            violations,
            [],
            "AEE-7.3: inline failure_code literal(s) inside "
            "ExecutionOrchestrator class body — use "
            "FailureCode.X or _failure_code_for_status() instead."
            "\n  " + "\n  ".join(violations),
        )

    def test_failure_code_helper_defined_once(self) -> None:
        """The helper name must appear exactly once in the
        source (definition) — no re-implementations."""
        from aee.orchestrator import orchestrator as orch_mod
        import inspect

        src = inspect.getsource(orch_mod)
        self.assertEqual(
            src.count("def _failure_code_for_status"),
            1,
            "_failure_code_for_status must be defined exactly once",
        )

    def test_both_write_sites_call_sot_helper(self) -> None:
        """Both ``_persist_dispatch`` and
        ``_update_dispatch_status`` must call
        ``_failure_code_for_status``."""
        from aee.orchestrator import orchestrator as orch_mod
        import inspect

        persist_src = inspect.getsource(orch_mod.ExecutionOrchestrator._persist_dispatch)
        update_src = inspect.getsource(orch_mod.ExecutionOrchestrator._update_dispatch_status)
        self.assertIn(
            "_failure_code_for_status",
            persist_src,
            "_persist_dispatch must call _failure_code_for_status",
        )
        self.assertIn(
            "_failure_code_for_status",
            update_src,
            "_update_dispatch_status must call _failure_code_for_status",
        )


if __name__ == "__main__":
    unittest.main()
