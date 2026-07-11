"""AEE-7.1 ``ClaudeCodeRuntimeAdapter`` — AEE-2 ``RuntimeAdapter`` shim.

The AEE-2 watcher (in ``dispatcher/watcher.py``) resolves the
adapter for a task via :func:`aee.core.registry.adapter_registry`
and calls ``adapter.poll(external_run_id)`` (async) on a 2 s
tick. The AEE-7.1 orchestrator exposes its lifecycle via
:func:`ExecutionOrchestrator.poll(external_run_id)` — same
signature, transport-neutral status vocabulary.

This shim is the bridge that lets AEE-7.1 plug into the AEE-2
hot path *without* rewriting the watcher. It:

* Knows about the :class:`ExecutionOrchestrator` singleton.
* Translates :class:`ProviderStatusResult` to
  :class:`RuntimePollResult` (AEE-2 vocabulary).
* Forwards cancel() to the orchestrator.
* On submit(), boots an orchestrator run with the AEE-5
  ``RuntimeSelector`` doing the descriptor resolution.

The AEE-2 ``adapter_registry.bootstrap_defaults`` registers
``HermesAdapter`` (HTTP) by default. AEE-7.1 extends that
default set to also register ``ClaudeCodeRuntimeAdapter`` so
``adapter_name="claude_code"`` tasks route through the
orchestrator.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Dict, Mapping, Optional

from aee.adapters.base import (
    RuntimeAdapter,
    RuntimeCancelResult,
    RuntimePollResult,
    RuntimeSubmitResult,
)
from aee.runtimes.models import TaskRuntimeRequirements
from aee.runtimes.selector import RuntimeNotFoundError

from .orchestrator import ExecutionOrchestrator, OrchestratorResult
from .provider import (
    ProviderError,
    ProviderNotFoundError,
    ProviderStatus,
)


log = logging.getLogger("aee.orchestrator.aee2_shim")


# AEE-2 → AEE-7 status translation.
_STATUS_MAP_AEE2 = {
    ProviderStatus.QUEUED: "queued",
    ProviderStatus.RUNNING: "running",
    ProviderStatus.COMPLETED: "completed",
    ProviderStatus.FAILED: "failed",
    ProviderStatus.CANCELLED: "cancelled",
    ProviderStatus.TIMEOUT: "timeout",
}


def _to_aee2_status(prov: ProviderStatus) -> str:
    return _STATUS_MAP_AEE2.get(prov, "running")


class ClaudeCodeRuntimeAdapter:
    """AEE-2 ``RuntimeAdapter`` that delegates to ``ExecutionOrchestrator``.

    The AEE-2 watcher treats every adapter the same way: it calls
    ``adapter.poll(external_run_id)`` on a 2 s tick and translates
    the result. This shim presents that contract while the
    underlying work happens in the AEE-7 orchestrator.
    """

    name = "claude_code"
    runtime_type = "claude_code"

    def __init__(
        self,
        *,
        orchestrator: Optional[ExecutionOrchestrator] = None,
    ) -> None:
        # Default to a process-local orchestrator; tests can
        # inject a custom one.
        self._orchestrator = orchestrator or ExecutionOrchestrator()
        # The watcher is single-threaded asyncio; we use a lock
        # only because the orchestrator is sync-friendly (it
        # has its own RLock). The lock is here for the rare case
        # of a sync caller.
        self._lock = threading.Lock()

    @property
    def orchestrator(self) -> ExecutionOrchestrator:
        return self._orchestrator

    # ------------------------------------------------------------------
    # RuntimeAdapter protocol
    # ------------------------------------------------------------------

    async def submit(self, job: Any) -> RuntimeSubmitResult:
        # Build a TaskRuntimeRequirements from the job's
        # ``runtime_requirements`` field (if present) or from
        # ``runtime_type`` alone.
        requirements: Optional[TaskRuntimeRequirements] = getattr(
            job, "runtime_requirements", None
        )
        if requirements is None:
            rt = getattr(job, "runtime_type", None) or self.runtime_type
            requirements = TaskRuntimeRequirements(runtime_type=rt)

        prompt = getattr(job, "input", "") or ""
        try:
            result: OrchestratorResult = await self._orchestrator.submit(
                job=job, prompt=prompt, requirements=requirements
            )
        except RuntimeNotFoundError as exc:
            # Surface as a 404-style error so the watcher doesn't
            # mark the task failed incorrectly. The dispatcher's
            # caller (the GPT Action) receives this as a 502.
            raise ProviderNotFoundError(
                f"no runtime for {requirements.to_dict()!r}: {exc}",
                cause=exc,
            )
        except ProviderNotFoundError:
            raise
        except ProviderError:
            raise
        if result.status != ProviderStatus.RUNNING:
            # Submit failed — translate to a RuntimeSubmitResult
            # with an error so the dispatcher can fail() the task.
            err = result.error or "submit failed"
            log.warning(
                "ClaudeCodeRuntimeAdapter.submit: provider returned %s: %s",
                result.status.value,
                err,
            )
            return RuntimeSubmitResult(
                external_run_id=result.external_run_id,
                status="failed",
                raw={"error": err, "runtime_type": result.runtime_type},
            )
        return RuntimeSubmitResult(
            external_run_id=result.external_run_id,
            status="running",
            raw={
                "runtime_type": result.runtime_type,
                "provider": result.provider_name,
                "dispatch_record_id": result.dispatch_record_id,
            },
        )

    async def poll(self, external_run_id: str) -> RuntimePollResult:
        cached = self._orchestrator.get_run(external_run_id)
        if cached is None:
            # Not in the orchestrator's in-memory cache. Either
            # the run belongs to a different adapter (the
            # watcher's lookup is by adapter_name so this should
            # not happen) or it was never tracked. Return
            # "unknown" so the watcher's UnknownExternalRunError
            # branch can fire.
            from aee.adapters.base import UnknownExternalRunError
            raise UnknownExternalRunError(
                f"orchestrator has no run for external_run_id={external_run_id!r}"
            )
        # Use a thread to call the orchestrator's poll
        # (which is async but not blocking; the underlying
        # provider's poll may be either async or sync).
        coro = self._orchestrator.poll(external_run_id)
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            # We're inside an event loop; await directly.
            prov = await coro
        else:
            # Sync test path; create a loop just for the call.
            prov = asyncio.get_event_loop().run_until_complete(coro)
        return RuntimePollResult(
            external_run_id=prov.external_run_id,
            status=_to_aee2_status(prov.status),
            is_terminal=prov.is_terminal,
            output=prov.output,
            error=prov.error,
            usage=dict(prov.usage) if prov.usage else None,
            raw=prov.raw,
        )

    async def cancel(self, external_run_id: str) -> RuntimeCancelResult:
        cached = self._orchestrator.get_run(external_run_id)
        if cached is None:
            return RuntimeCancelResult(
                external_run_id=external_run_id,
                cancelled=False,
                reason="not tracked by orchestrator",
            )
        coro = self._orchestrator.cancel(external_run_id)
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            prov = await coro
        else:
            prov = asyncio.get_event_loop().run_until_complete(coro)
        return RuntimeCancelResult(
            external_run_id=prov.external_run_id,
            cancelled=bool(prov.cancelled),
            reason=prov.reason or "",
        )

    def artifacts_dir(self, external_run_id: str) -> Optional[str]:
        return self._orchestrator.artifacts_dir(external_run_id)


__all__ = ["ClaudeCodeRuntimeAdapter"]
