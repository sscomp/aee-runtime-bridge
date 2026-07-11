"""AEE-7.1 HermesProvider — thin ``Provider`` wrapper over ``HermesAdapter``.

The AEE-2 ``HermesAdapter`` is the *legacy* HTTP adapter; it owns
the wire protocol against the ``hermes-agent`` / Hermes 8642 service.
AEE-7.1 introduces a separate ``Provider`` Protocol (in
``aee/orchestrator/provider.py``); this module adapts the
``HermesAdapter`` to that Protocol so the orchestrator can pick
``runtime_type="hermes"`` (or ``"aee_lightweight"``) the same way
it picks ``claude_code``.

Why a separate wrapper?
-----------------------
* ``HermesAdapter`` returns :class:`RuntimeSubmitResult` /
  :class:`RuntimePollResult` — the AEE-2 vocabulary.
* The ``Provider`` Protocol returns :class:`ProviderRun` /
  :class:`ProviderStatusResult` — the AEE-7 vocabulary.

The wrapper translates between the two. The legacy AEE-2 watcher
keeps talking to ``HermesAdapter`` directly; AEE-7.1+ dispatcher
hot path talks to the ``Provider`` (this wrapper). Both paths
must keep working.

This wrapper is **synchronous at the seam** but the underlying
``HermesAdapter`` is async; we use ``asyncio.get_event_loop()``
only for the rare test that calls this code outside an event
loop. In production the orchestrator's ``submit/poll/cancel``
are awaited directly.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from aee.adapters.base import (
    AdapterNotFoundError,
    RuntimeAdapter,
    RuntimeError as AdapterRuntimeError,
    UnknownExternalRunError,
)
from aee.adapters.hermes_adapter import HermesAdapter
from aee.runtimes.models import RuntimeDescriptor, TaskRuntimeRequirements

from .provider import (
    Provider,
    ProviderCancelResult,
    ProviderError,
    ProviderRun,
    ProviderStatus,
    ProviderStatusResult,
)


log = logging.getLogger("aee.orchestrator.hermes")


class HermesRuntimeAdapterProvider:
    """Wrap the AEE-2 ``HermesAdapter`` as a :class:`Provider`.

    This is the *legacy* path. The adapter already owns the
    Hermes HTTP transport; we just forward submit/poll/cancel
    and translate result shapes.
    """

    name = "hermes_runtime_adapter"
    runtime_type = "hermes"  # also matches "aee_lightweight" via factory

    def __init__(
        self,
        *,
        descriptor: RuntimeDescriptor,
        adapter: Optional[RuntimeAdapter] = None,
    ) -> None:
        self._descriptor = descriptor
        # Allow callers to inject a custom adapter (tests use
        # the FakeAdapter from aee.adapters.fake_adapter).
        self._adapter = adapter or HermesAdapter()

    @property
    def descriptor(self) -> RuntimeDescriptor:
        return self._descriptor

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
        # Build a minimal Job dict the HermesAdapter can submit.
        # We construct a fake Job; the adapter only reads
        # title/type/input/priority/owner, all of which we set.
        # We do not import the real Job to avoid a cycle.
        from aee.core.job_models import Job

        job = Job(
            title=f"orchestrator:{run_id or 'auto'}",
            type="ops",
            mode="normal",
            priority=50,
            input=prompt,
            runtime_type=self._descriptor.runtime_type,
            adapter_name=self.name,
        )
        if run_id:
            job.external_run_id = run_id
        try:
            res = await self._adapter.submit(job)
        except AdapterRuntimeError as exc:
            raise ProviderError(
                f"hermes adapter submit failed: {exc}", cause=exc
            ) from exc
        return ProviderRun(
            external_run_id=res.external_run_id,
            provider_name=self.name,
            runtime_type=self._descriptor.runtime_type,
            started_at=res.raw.get("started_at", "") if res.raw else "",
        )

    async def poll(self, run: ProviderRun) -> ProviderStatusResult:
        try:
            res = await self._adapter.poll(run.external_run_id)
        except UnknownExternalRunError as exc:
            return ProviderStatusResult(
                external_run_id=run.external_run_id,
                status=ProviderStatus.TIMEOUT,
                is_terminal=True,
                error=f"unknown run: {exc}",
            )
        except AdapterRuntimeError as exc:
            raise ProviderError(
                f"hermes adapter poll failed: {exc}", cause=exc
            ) from exc
        return _translate_poll(res)

    async def cancel(self, run: ProviderRun) -> ProviderCancelResult:
        try:
            res = await self._adapter.cancel(run.external_run_id)
        except AdapterRuntimeError as exc:
            return ProviderCancelResult(
                external_run_id=run.external_run_id,
                cancelled=False,
                reason=f"hermes adapter cancel failed: {exc}",
            )
        return ProviderCancelResult(
            external_run_id=res.external_run_id,
            cancelled=bool(res.cancelled),
            reason=res.reason or "",
            raw=dict(res.raw) if res.raw is not None else None,
        )

    def artifacts_dir(self, run: ProviderRun) -> Optional[str]:
        # Hermes is HTTP — the worker writes nothing to a local
        # directory. The dispatcher falls back to scanning the
        # ``input_text`` for absolute paths when this returns
        # ``None``.
        return None


def _translate_poll(res) -> ProviderStatusResult:  # type: ignore[no-untyped-def]
    """Translate ``RuntimePollResult`` to ``ProviderStatusResult``."""
    raw_status = (res.status or "").lower() if res.status else ""
    status_map = {
        "queued": ProviderStatus.QUEUED,
        "running": ProviderStatus.RUNNING,
        "in_progress": ProviderStatus.RUNNING,
        "started": ProviderStatus.RUNNING,
        "completed": ProviderStatus.COMPLETED,
        "succeeded": ProviderStatus.COMPLETED,
        "success": ProviderStatus.COMPLETED,
        "failed": ProviderStatus.FAILED,
        "error": ProviderStatus.FAILED,
        "cancelled": ProviderStatus.CANCELLED,
        "canceled": ProviderStatus.CANCELLED,
        "timeout": ProviderStatus.TIMEOUT,
    }
    norm = status_map.get(raw_status, ProviderStatus.RUNNING)
    return ProviderStatusResult(
        external_run_id=res.external_run_id,
        status=norm,
        is_terminal=norm.is_terminal,
        output=res.output,
        error=res.error,
        usage=dict(res.usage) if res.usage else None,
        raw=dict(res.raw) if res.raw is not None else None,
    )


__all__ = ["HermesRuntimeAdapterProvider"]
