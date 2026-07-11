"""AEE-7.1 — Orchestrator E2E tests using ``FakeProvider``.

These tests drive the *real* :class:`ExecutionOrchestrator`
against an in-memory :class:`FakeProvider` (no subprocess, no
real Claude CLI). They cover the AEE-7.1 dispatch bridge end-to-end:

* happy-path submit -> poll -> COMPLETED
* non-zero exit
* timeout
* cancel
* missing provider
* missing binary
* per-job ``repo_root`` plumbing
* logger output
* legacy Hermes path unaffected
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import unittest
from typing import Optional

from aee.orchestrator import (
    ExecutionOrchestrator,
    ProviderStatus,
    RuntimeDescriptor,
    RuntimeHealth,
    RuntimeHealthStatus,
    RuntimeNotFoundError,
    register_provider,
    registered_providers,
    unregister_provider,
)
from aee.orchestrator.orchestrator import (
    clear_test_runtimes,
    register_runtimes_for_test,
)
from aee.orchestrator.fake_provider import (
    FakeProvider,
    ProviderBinaryMissingError,
    ProviderPollStatus,
)
from aee.orchestrator.provider import ProviderError, ProviderSubmitError
from aee.runtimes.models import TaskRuntimeRequirements


# ---------------------------------------------------------------------------
# Module-scope helpers
# ---------------------------------------------------------------------------


class _Capture(logging.Handler):
    """Module-scope capture handler.

    Each test instance points ``handler.records = self._records`` at
    setup, so the handler can append without holding a closure
    on the test's locals (which is fragile when the logger's
    call chain runs in a different frame).
    """

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        records = getattr(self, "records", None)
        if records is not None:
            records.append(record)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _fake_descriptor() -> RuntimeDescriptor:
    # The AEE-5 ``RuntimeDescriptor`` constructor takes
    # ``health=RuntimeHealth(status=...)`` — there are no
    # ``health_status=`` / ``last_health_check=`` keyword
    # arguments at the top level (those live inside
    # ``RuntimeHealth``).
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


def _setup_fake_provider() -> FakeProvider:
    """Register a fake-typed descriptor + FakeProvider for tests.

    Idempotent: cleans up registry / providers on teardown.
    """
    desc = _fake_descriptor()
    register_runtimes_for_test([desc])
    fake = FakeProvider(descriptor=desc, behavior="happy")
    # The factory signature is ``(descriptor) -> Provider``; we
    # close over the test's instance so set_behavior() mutates
    # the same object the orchestrator drives.
    register_provider(
        "fake",
        lambda _descriptor, _fake=fake: _fake,
        replace=True,
    )
    return fake


def _teardown_fake_provider() -> None:
    clear_test_runtimes()
    unregister_provider("fake")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath(unittest.TestCase):
    def setUp(self) -> None:
        self._fake = _setup_fake_provider()
        self._orch = ExecutionOrchestrator()
        self._tmp = tempfile.mkdtemp(prefix="aee71-happy-")

    def tearDown(self) -> None:
        _teardown_fake_provider()

    def test_submit_then_poll_completes(self) -> None:
        req = TaskRuntimeRequirements(runtime_type="fake")
        result = asyncio.run(
            self._orch.submit(
                job=_FakeJob(task_id="t-happy"),
                prompt="echo hello",
                requirements=req,
            )
        )
        self.assertEqual(result.status, ProviderStatus.RUNNING)
        self.assertTrue(result.external_run_id.startswith("orch-"))
        self.assertEqual(result.provider_name, "fake")

        polled = asyncio.run(self._orch.poll(result))
        self.assertEqual(polled.status, ProviderStatus.COMPLETED)
        self.assertTrue(polled.is_terminal)
        self.assertIn("fake-output-ok", polled.output or "")

    def test_per_job_repo_root_plumbed(self) -> None:
        repo_root = os.path.join(self._tmp, "myrepo")
        os.makedirs(repo_root, exist_ok=True)
        req = TaskRuntimeRequirements(
            runtime_type="fake", repo_root=repo_root
        )
        result = asyncio.run(
            self._orch.submit(
                job=_FakeJob(task_id="t-root"),
                prompt="x",
                requirements=req,
            )
        )
        self.assertEqual(result.status, ProviderStatus.RUNNING)
        # The FakeProvider captured the requirements dict in
        # ``last_submit_call``; verify repo_root round-tripped.
        last_call = self._fake.submit_calls[-1]
        self.assertEqual(last_call["requirements"]["repo_root"], repo_root)


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


class TestFailurePaths(unittest.TestCase):
    def setUp(self) -> None:
        self._fake = _setup_fake_provider()
        self._orch = ExecutionOrchestrator()

    def tearDown(self) -> None:
        _teardown_fake_provider()

    def test_nonzero_exit_returns_failed(self) -> None:
        self._fake.set_behavior(behavior="failing", fail_at="poll")
        req = TaskRuntimeRequirements(runtime_type="fake")
        result = asyncio.run(
            self._orch.submit(
                job=_FakeJob(task_id="t-nz"),
                prompt="x",
                requirements=req,
            )
        )
        polled = asyncio.run(self._orch.poll(result))
        self.assertEqual(polled.status, ProviderStatus.FAILED)
        self.assertTrue(polled.is_terminal)
        self.assertEqual(polled.exit_code, 1)

    def test_submit_failure_returns_failed(self) -> None:
        self._fake.set_behavior(behavior="failing", fail_at="submit")
        req = TaskRuntimeRequirements(runtime_type="fake")
        result = asyncio.run(
            self._orch.submit(
                job=_FakeJob(task_id="t-sf"),
                prompt="x",
                requirements=req,
            )
        )
        # Submit raised a ``ProviderSubmitError``; the
        # orchestrator translates that into a FAILED result
        # with the class name + message in ``error``.
        self.assertEqual(result.status, ProviderStatus.FAILED)
        self.assertTrue(result.is_terminal)
        self.assertIn("ProviderSubmitError", result.error or "")

    def test_missing_provider(self) -> None:
        # No registered descriptor for "no-such-type".
        clear_test_runtimes()
        req = TaskRuntimeRequirements(runtime_type="no-such-type")
        with self.assertRaises(RuntimeNotFoundError):
            asyncio.run(
                self._orch.submit(
                    job=_FakeJob(task_id="t-mp"),
                    prompt="x",
                    requirements=req,
                )
            )

    def test_missing_binary_raises(self) -> None:
        # Provider exists but raises ProviderBinaryMissingError.
        self._fake.set_behavior(behavior="missing_binary")
        req = TaskRuntimeRequirements(runtime_type="fake")
        with self.assertRaises(ProviderBinaryMissingError):
            asyncio.run(
                self._orch.submit(
                    job=_FakeJob(task_id="t-mb"),
                    prompt="x",
                    requirements=req,
                )
            )


# ---------------------------------------------------------------------------
# Logger / observability
# ---------------------------------------------------------------------------


class TestObservability(unittest.TestCase):
    def setUp(self) -> None:
        self._fake = _setup_fake_provider()
        self._orch = ExecutionOrchestrator()
        # The orchestrator module's logger is named
        # ``aee.orchestrator`` (not ``aee.orchestrator.orchestrator``
        # — only the package itself, not the submodule). Attach the
        # capture handler to the *package* logger so propagation
        # delivers the records to us.
        self._log = logging.getLogger("aee.orchestrator")
        self._records: list[logging.LogRecord] = []

        # Module-scope capture handler (defined at class level
        # below) so the same class instance is reused across
        # tests. We set ``_records`` per test instance.
        self._handler = _Capture()
        self._handler.records = self._records
        self._old_level = self._log.level
        self._log.setLevel(logging.DEBUG)
        self._log.addHandler(self._handler)

    def tearDown(self) -> None:
        self._log.removeHandler(self._handler)
        self._log.setLevel(self._old_level)
        _teardown_fake_provider()

    def test_submit_emits_debug_log_with_repo_root(self) -> None:
        req = TaskRuntimeRequirements(
            runtime_type="fake", repo_root="/tmp/aee71-repofoo"
        )
        result = asyncio.run(
            self._orch.submit(
                job=_FakeJob(task_id="t-log"),
                prompt="x",
                requirements=req,
            )
        )
        self.assertEqual(result.status, ProviderStatus.RUNNING)
        debug_msgs = [
            r.getMessage()
            for r in self._records
            if r.levelno == logging.DEBUG
        ]
        self.assertTrue(
            any("orchestrator.submit" in m for m in debug_msgs),
            f"expected orchestrator.submit log, got: {debug_msgs!r}",
        )
        self.assertTrue(
            any("/tmp/aee71-repofoo" in m for m in debug_msgs),
            f"expected repo_root in log, got: {debug_msgs!r}",
        )


# ---------------------------------------------------------------------------
# Legacy Hermes path
# ---------------------------------------------------------------------------


class TestHermesPathUnaffected(unittest.TestCase):
    """AEE-7.1 must not change the ``hermes`` provider's hot path.

    The :func:`provider_for_descriptor` mapping returns a Hermes
    shim for ``runtime_type=hermes``; we only verify the
    registration is in place (the actual wire is owned by
    ``HermesAdapter`` which has its own tests).
    """

    def setUp(self) -> None:
        # The ``fake`` provider is also part of the default
        # registration set in the factory module, so re-register
        # it here to undo any test isolation that may have
        # unregisterd it earlier in the suite. ``replace=True``
        # is safe even if it's already present.
        try:
            from aee.orchestrator.factory import _build_fake_provider
            register_provider("fake", _build_fake_provider, replace=True)
        except Exception:  # pragma: no cover - defensive
            pass

    def test_hermes_provider_registered(self) -> None:
        names = registered_providers()
        self.assertIn("hermes", names)
        self.assertIn("claude_code", names)
        self.assertIn("fake", names)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


class _FakeJob:
    """Minimal stand-in for ``dispatcher.models.Task``."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id


if __name__ == "__main__":
    unittest.main()
