"""FakeAdapter — in-memory RuntimeAdapter for tests.

`FakeAdapter` records every `submit` and `poll`, and lets a test
script (or a separate test fixture) drive state transitions
explicitly. No network. No real Hermes.

Usage::

    adapter = FakeAdapter()
    submit = await adapter.submit(job)
    await adapter.mark_running(submit.external_run_id)
    await adapter.mark_completed(submit.external_run_id, output="done")
    poll = await adapter.poll(submit.external_run_id)
    assert poll.is_terminal
    assert poll.output == "done"

`FakeAdapter` is the canonical reference implementation of the
RuntimeAdapter contract; the contract tests in
`tests/test_adapter.py` use it to validate behaviour.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from aee.adapters.base import (
    RuntimeAdapter,
    RuntimeCancelResult,
    RuntimeError,
    RuntimePollResult,
    RuntimeSubmitResult,
    UnknownExternalRunError,
)


# State vocabulary (subset of AEE's JobStatus semantics).
_FAKE_STATES = {"queued", "running", "completed", "failed", "cancelled"}


@dataclass
class _FakeRun:
    external_run_id: str
    status: str = "queued"
    output: Optional[str] = None
    error: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    cancelled: bool = False
    history: List[Dict[str, Any]] = field(default_factory=list)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "external_run_id": self.external_run_id,
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "usage": self.usage,
            "cancelled": self.cancelled,
        }


class FakeAdapter:
    """In-memory `RuntimeAdapter` for unit tests."""

    name = "fake"
    runtime_type = "fake"

    def __init__(self) -> None:
        self._runs: Dict[str, _FakeRun] = {}
        self._lock = threading.Lock()
        # Submitted jobs and poll calls recorded for assertions.
        self.submitted_jobs: List[Dict[str, Any]] = []
        self.poll_calls: List[str] = []
        self.cancel_calls: List[str] = []
        # Optional hook: a callable (run, kind) -> None that tests
        # can use to simulate transient failure or latency.
        self.hook = None

    # -- Test helpers ----------------------------------------------------

    def _new_id(self) -> str:
        return f"FAKE-{uuid.uuid4().hex[:12]}"

    async def mark_running(self, external_run_id: str) -> None:
        with self._lock:
            run = self._require(external_run_id)
            run.status = "running"
            run.history.append({"event": "running"})

    async def mark_completed(
        self,
        external_run_id: str,
        *,
        output: str = "fake-output",
        usage: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            run = self._require(external_run_id)
            run.status = "completed"
            run.output = output
            run.usage = usage
            run.history.append({"event": "completed", "output": output})

    async def mark_failed(
        self,
        external_run_id: str,
        *,
        error: str = "fake-failure",
    ) -> None:
        with self._lock:
            run = self._require(external_run_id)
            run.status = "failed"
            run.error = error
            run.history.append({"event": "failed", "error": error})

    async def mark_cancelled(self, external_run_id: str) -> None:
        with self._lock:
            run = self._require(external_run_id)
            run.status = "cancelled"
            run.cancelled = True
            run.history.append({"event": "cancelled"})

    def snapshot(self, external_run_id: str) -> Dict[str, Any]:
        with self._lock:
            return self._require(external_run_id).snapshot()

    # -- RuntimeAdapter protocol ----------------------------------------

    async def submit(self, job: Any) -> RuntimeSubmitResult:  # noqa: D401
        if self.hook:
            maybe = self.hook(job, "submit")
            if maybe is not None:
                # Hook can return an exception to raise.
                raise maybe
        run_id = self._new_id()
        with self._lock:
            self._runs[run_id] = _FakeRun(external_run_id=run_id, status="queued")
            self.submitted_jobs.append(
                {
                    "external_run_id": run_id,
                    "input": getattr(job, "input_text", None) or getattr(job, "input", None),
                    "session_id": getattr(job, "session_id", None),
                    "mode": getattr(job, "mode", None),
                }
            )
        return RuntimeSubmitResult(external_run_id=run_id, status="queued")

    async def poll(self, external_run_id: str) -> RuntimePollResult:
        if self.hook:
            maybe = self.hook(external_run_id, "poll")
            if maybe is not None:
                raise maybe
        with self._lock:
            self.poll_calls.append(external_run_id)
            run = self._runs.get(external_run_id)
            if run is None:
                raise UnknownExternalRunError(
                    f"fake run {external_run_id!r} not found"
                )
            return RuntimePollResult(
                external_run_id=run.external_run_id,
                status=run.status,
                is_terminal=run.status in {"completed", "failed", "cancelled"},
                output=run.output,
                error=run.error,
                usage=run.usage,
                raw=run.snapshot(),
            )

    async def cancel(self, external_run_id: str) -> RuntimeCancelResult:
        if self.hook:
            maybe = self.hook(external_run_id, "cancel")
            if maybe is not None:
                raise maybe
        with self._lock:
            self.cancel_calls.append(external_run_id)
            run = self._runs.get(external_run_id)
            if run is None:
                # Mirror Hermes' "404 == already gone" semantics.
                return RuntimeCancelResult(
                    external_run_id=external_run_id,
                    cancelled=True,
                    reason="run not found (already gone)",
                )
            run.cancelled = True
            if run.status not in {"completed", "failed", "cancelled"}:
                run.status = "cancelled"
            run.history.append({"event": "cancelled"})
            return RuntimeCancelResult(
                external_run_id=external_run_id,
                cancelled=True,
                reason="fake cancel",
            )

    # -- Internals -------------------------------------------------------

    def _require(self, external_run_id: str) -> _FakeRun:
        run = self._runs.get(external_run_id)
        if run is None:
            raise UnknownExternalRunError(
                f"fake run {external_run_id!r} not found"
            )
        return run


# Make sure the protocol is satisfied at import time.
assert isinstance(FakeAdapter(), RuntimeAdapter)  # type: ignore[misc]


__all__ = ["FakeAdapter"]
