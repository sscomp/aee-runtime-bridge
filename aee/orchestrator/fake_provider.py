"""AEE-7.1 FakeProvider — in-memory ``Provider`` for unit tests.

Pluggable ``behavior``:

* ``"happy"`` (default) — submit() returns a fake ProviderRun
  immediately; poll() returns COMPLETED with a fake output.
* ``"failing"`` — submit() raises ``ProviderSubmitError`` (or
  poll() returns FAILED, depending on ``fail_at``).
* ``"hanging"`` — submit() returns a fake ProviderRun; poll()
  always returns RUNNING. Used to test the dispatcher's
  timeout path.
* ``"cancel"`` — submit() returns a fake ProviderRun; poll()
  returns CANCELLED on the first call after ``cancel()``.

The fake does **not** import any subprocess / HTTP code. It is
deterministic: same input → same output, no time / randomness.

Used by ``aee/tests/test_aee7_dispatcher_e2e.py`` and any other
test that needs to exercise the orchestrator without an actual
provider.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from aee.runtimes.models import RuntimeDescriptor, TaskRuntimeRequirements

from .provider import (
    Provider,
    ProviderCancelResult,
    ProviderError,
    ProviderRun,
    ProviderStatus,
    ProviderStatusResult,
    ProviderSubmitError,
)


log = logging.getLogger("aee.orchestrator.fake")


# ---------------------------------------------------------------------------
# Errors raised by the fake provider
# ---------------------------------------------------------------------------


class ProviderBinaryMissingError(RuntimeError):
    """Raised by ``FakeProvider.submit`` when configured to simulate a
    missing binary on the host.

    The orchestrator's test fixture for the ``missing_binary`` path uses
    this exception (a :class:`RuntimeError`, not a :class:`ProviderError`),
    matching the AEE-7.1 contract: a missing binary on the real
    Claude-Code provider is a host-config error, not a provider-runtime
    error, so it should propagate up to the dispatcher as-is.
    """

    pass


# Backward-compat: ``_FakeBinaryMissingError`` is the original (private)
# name. Keep it as a re-export so older imports keep working.
_FakeBinaryMissingError = ProviderBinaryMissingError


# Public alias of :class:`ProviderStatus` so tests that want to assert
# against the result shape can write
# ``from aee.orchestrator.fake_provider import ProviderPollStatus``
# without re-importing the provider module.
ProviderPollStatus = ProviderStatus  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Per-run state
# ---------------------------------------------------------------------------


@dataclass
class _FakeRun:
    """Internal: a fake provider's per-run state."""

    external_run_id: str
    started_at: float
    prompt: str
    behavior: str
    # The "fake output" the poll() call returns when behavior
    # is "happy". Configurable per test.
    output: str = "fake-output-ok"
    exit_code: int = 0
    # When fail_at == "submit", submit raises ProviderSubmitError.
    # When fail_at == "poll", poll returns FAILED with this error.
    fail_at: str = "never"
    fail_message: str = "fake-failure"
    # When behavior == "hanging", the poll result is always
    # RUNNING. Otherwise, behavior == "happy" → COMPLETED on
    # the first poll, behavior == "failing" → FAILED on the
    # first poll, etc.
    cancel_requested: bool = False
    # Optional fake artifacts dir the provider returns via
    # ``artifacts_dir()``. Tests can mutate this to simulate
    # the worker writing files.
    artifacts_dir: Optional[str] = None
    # Captured metadata for assertions.
    submit_called_with: Dict[str, Any] = field(default_factory=dict)
    poll_call_count: int = 0


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class FakeProvider:
    """In-memory Provider for tests.

    Pluggable behavior via the constructor (defaults to "happy").
    Tests can also mutate :attr:`runs` to inspect or alter
    per-run state mid-test.
    """

    name = "fake"
    runtime_type = "fake"

    def __init__(
        self,
        *,
        descriptor: Optional[RuntimeDescriptor] = None,
        behavior: str = "happy",
        output: str = "fake-output-ok",
        fail_at: str = "never",
        fail_message: str = "fake-failure",
        artifacts_dir: Optional[str] = None,
    ) -> None:
        # Tests can pass a real descriptor (e.g. from the
        # built-in ``claude_code_local`` factory) so the
        # orchestrator's selector still picks a known type.
        if descriptor is None:
            from aee.runtimes.builtins.aee_lightweight import (
                build_default_descriptor,
            )
            descriptor = build_default_descriptor(default_runtime_id="fake")
        # Override runtime_type to "fake" so the factory
        # can register it under that key.
        self._descriptor = descriptor
        self._default_behavior = behavior
        self._default_output = output
        self._default_fail_at = fail_at
        self._default_fail_message = fail_message
        self._default_artifacts_dir = artifacts_dir
        self._runs: Dict[str, _FakeRun] = {}
        self._lock = threading.Lock()
        # Bookkeeping for assertions.
        self.submit_calls: List[Dict[str, Any]] = []
        self.cancel_calls: List[str] = []

    # ------------------------------------------------------------------
    # Provider protocol
    # ------------------------------------------------------------------

    async def submit(
        self,
        *,
        prompt: str,
        requirements: TaskRuntimeRequirements,
        repo: Any,
        run_id: Optional[str] = None,
    ) -> ProviderRun:
        rid = run_id or f"fake-run-{uuid.uuid4().hex[:10]}"
        run = _FakeRun(
            external_run_id=rid,
            started_at=time.time(),
            prompt=prompt,
            behavior=self._default_behavior,
            output=self._default_output,
            fail_at=self._default_fail_at,
            fail_message=self._default_fail_message,
            artifacts_dir=self._default_artifacts_dir,
            submit_called_with={
                "prompt": prompt,
                "requirements": requirements.to_dict()
                if requirements is not None
                else None,
                "repo": type(repo).__name__ if repo is not None else None,
            },
        )
        with self._lock:
            self._runs[rid] = run
            self.submit_calls.append(dict(run.submit_called_with))
        # Default: never fail. Tests opt in via fail_at="submit"
        # or fail_at="poll".
        if self._default_behavior == "missing_binary":
            raise _FakeBinaryMissingError(
                "fake provider: configured to raise missing-binary"
            )
        if run.fail_at == "submit":
            raise ProviderSubmitError(run.fail_message)
        return ProviderRun(
            external_run_id=rid,
            provider_name=self.name,
            runtime_type=self._descriptor.runtime_type,
            started_at=time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(run.started_at)
            ),
            metadata={
                "fake": True,
                "behavior": run.behavior,
                "artifacts_dir": run.artifacts_dir,
            },
        )

    async def poll(self, run: ProviderRun) -> ProviderStatusResult:
        with self._lock:
            fake = self._runs.get(run.external_run_id)
            if fake is None:
                return ProviderStatusResult(
                    external_run_id=run.external_run_id,
                    status=ProviderStatus.RUNNING,
                    is_terminal=False,
                )
            fake.poll_call_count += 1
            if fake.cancel_requested:
                return ProviderStatusResult(
                    external_run_id=run.external_run_id,
                    status=ProviderStatus.CANCELLED,
                    is_terminal=True,
                    error="cancelled by user",
                )
            if fake.behavior == "hanging":
                return ProviderStatusResult(
                    external_run_id=run.external_run_id,
                    status=ProviderStatus.RUNNING,
                    is_terminal=False,
                )
            if fake.fail_at == "poll":
                return ProviderStatusResult(
                    external_run_id=run.external_run_id,
                    status=ProviderStatus.FAILED,
                    is_terminal=True,
                    error=fake.fail_message,
                    exit_code=fake.exit_code or 1,
                )
            if fake.behavior == "failing":
                return ProviderStatusResult(
                    external_run_id=run.external_run_id,
                    status=ProviderStatus.FAILED,
                    is_terminal=True,
                    error=fake.fail_message,
                    exit_code=fake.exit_code or 1,
                )
            # Default "happy" path.
            return ProviderStatusResult(
                external_run_id=run.external_run_id,
                status=ProviderStatus.COMPLETED,
                is_terminal=True,
                output=fake.output,
                exit_code=fake.exit_code,
            )

    async def cancel(self, run: ProviderRun) -> ProviderCancelResult:
        with self._lock:
            self.cancel_calls.append(run.external_run_id)
            fake = self._runs.get(run.external_run_id)
            if fake is not None:
                fake.cancel_requested = True
        return ProviderCancelResult(
            external_run_id=run.external_run_id,
            cancelled=True,
            reason="cancelled by orchestrator",
        )

    def artifacts_dir(self, run: ProviderRun) -> Optional[str]:
        with self._lock:
            fake = self._runs.get(run.external_run_id)
            if fake is None:
                return None
            return fake.artifacts_dir

    # ------------------------------------------------------------------
    # Test helpers (intentionally not part of the Provider Protocol)
    # ------------------------------------------------------------------

    def list_runs(self) -> List[str]:
        with self._lock:
            return sorted(self._runs)

    def get_run(self, external_run_id: str) -> Optional[_FakeRun]:
        with self._lock:
            return self._runs.get(external_run_id)

    def set_behavior(
        self,
        external_run_id: Optional[str] = None,
        *legacy_positional,
        behavior: Optional[str] = None,
        fail_at: Optional[str] = None,
        fail_message: Optional[str] = None,
        output: Optional[str] = None,
    ) -> None:
        """Mutate a run's behavior mid-test (e.g. flip to failing).

        Accepts both the AEE-7.1 contract and the older "configure
        the default for all future runs" usage:

        * ``set_behavior(behavior="failing", fail_at="poll")`` —
          apply to all runs that have not yet been submitted
          (default fallback path).
        * ``set_behavior("run-id-abc", behavior="failing")`` —
          apply to a specific run after ``submit()``.

        The keyword-only contract lets E2E tests write the more
        readable call without forcing them to know the external
        run id at configuration time.
        """
        with self._lock:
            if external_run_id is not None and self._runs:
                fake = self._runs.get(external_run_id)
                if fake is not None:
                    if behavior is not None:
                        fake.behavior = behavior
                    if fail_at is not None:
                        fake.fail_at = fail_at
                    if fail_message is not None:
                        fake.fail_message = fail_message
                    if output is not None:
                        fake.output = output
                    return
            # Default fallback: mutate the *constructor defaults* so
            # the next submit() picks up the new behavior. This is
            # the path E2E tests use to prime the fake before submit.
            if behavior is not None:
                self._default_behavior = behavior
            if fail_at is not None:
                self._default_fail_at = fail_at
            if fail_message is not None:
                self._default_fail_message = fail_message
            if output is not None:
                self._default_output = output


__all__ = [
    "FakeProvider",
    "_FakeRun",
    "ProviderBinaryMissingError",
    "_FakeBinaryMissingError",
    "ProviderPollStatus",
]
