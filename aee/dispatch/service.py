"""AEE-5 Dispatch Service — the seam between job creation and
Runtime selection.

This module is the only place in AEE-5 that calls
`RuntimeSelector.select()`. API controllers (`aee.api.jobs`,
`app.py`) do NOT select a Runtime themselves; they
hand the Job to `dispatch_service.select_runtime_for_job()`,
which returns a `DispatchRecord` (or raises
`RuntimeNotFoundError`).

Backwards compatibility
-----------------------
AEE-4 callers that don't pass `runtime_requirements` get
the default `aee-lightweight-local` Runtime selected.
The path through the AEE-4 wiring (which uses
`adapter_registry.get(adapter_name)` to talk to the
runtime) is unchanged; the AEE-5 dispatch service sits
*above* the adapter_registry and only decides which
descriptor to hand the adapter. The adapter is still
selected by `Job.adapter_name` (AEE-1) — the
descriptor's `runtime_type` and the adapter's
`runtime_type` are correlated but distinct concepts.
"""
from __future__ import annotations

import secrets
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from aee.runtimes.errors import RuntimeNotFoundError
from aee.runtimes.models import (
    DispatchRecord,
    DispatchStatus,
    RuntimeDescriptor,
    RuntimeSelectionResult,
    TaskRuntimeRequirements,
)
from aee.runtimes.registry import RuntimeRegistry, runtime_registry as _default_registry
from aee.runtimes.selector import RuntimeSelector, select_runtime


DEFAULT_RUNTIME_ID = "aee-lightweight-local"
"""Built-in Runtime id (matches `builtins.aee_lightweight.py`)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_dispatch_id() -> str:
    return f"dispatch-{secrets.token_hex(8)}"


class DispatchService:
    """Selects a Runtime for a Job and writes a DispatchRecord.

    The service is stateless beyond the registry /
    selector it wraps. Methods are synchronous; the
    API handlers in `aee.api.jobs` call them inline
    (the operations are O(N) over a small registry —
    < 100 Runtimes expected in v1).

    Concurrency
    -----------
    The selector is deterministic; the dispatch record
    write is serialized via the registry's own lock.
    Two parallel calls to `select_runtime_for_job()`
    with the same Job can both succeed and write two
    records — that's intentional. The records are an
    audit trail, not a coordination primitive.
    """

    def __init__(
        self,
        registry: Optional[RuntimeRegistry] = None,
        selector: Optional[RuntimeSelector] = None,
        *,
        default_runtime_id: str = DEFAULT_RUNTIME_ID,
        allow_unknown_health: bool = True,
    ) -> None:
        self._registry = registry or _default_registry
        self._selector = selector or RuntimeSelector(
            allow_unknown_health=allow_unknown_health
        )
        self._default_runtime_id = default_runtime_id
        self._allow_unknown_health = allow_unknown_health
        self._lock = threading.Lock()

    # ---- Public API ----------------------------------------------------

    def select_runtime_for_job(
        self,
        *,
        task_id: str,
        run_id: str,
        runtime_requirements: Optional[TaskRuntimeRequirements] = None,
    ) -> DispatchRecord:
        """Select a Runtime for a job and persist a `selected`
        dispatch record.

        Raises:
            RuntimeNotFoundError: when no Runtime matches
                the requirements. The exception's
                `to_dict()` payload is what the API
                surfaces and the manager logs to
                `task_events`.

        Returns:
            A `DispatchRecord` with `dispatch_status =
            "selected"` and `selected_runtime_id` set.
            The record is already persisted; callers
            that complete (or fail) the dispatch
            should call `mark_dispatched()` /
            `mark_dispatch_failed()` to update the
            status.
        """
        runtimes = self._registry.list_runtimes()
        if runtime_requirements is not None and runtime_requirements.is_empty():
            # Empty requirements == no constraints (treat as None).
            runtime_requirements = None

        try:
            selection: RuntimeSelectionResult = self._selector.select(
                task=runtime_requirements, available_runtimes=runtimes
            )
        except RuntimeNotFoundError as exc:
            # Re-raise with the task_id / run_id set on the
            # structured error. The API layer doesn't need
            # to know about DispatchRecord; it just needs
            # the error.
            exc.task_id = task_id
            exc.run_id = run_id
            # Persist a *failed* dispatch record so the
            # audit trail records the attempt.
            failed = DispatchRecord(
                dispatch_id=_new_dispatch_id(),
                task_id=task_id,
                run_id=run_id,
                selected_runtime_id="",
                selected_runtime_type="",
                candidate_count=0,
                attempt_number=1,
                dispatch_status=DispatchStatus.FAILED,
                failure_code=exc.code,
                failure_message=exc.message,
                rejected_reasons={
                    item["runtime_id"]: item["rejected_reasons"]
                    for item in exc.evaluated_runtimes
                },
            )
            try:
                self._registry.record_dispatch(failed)
            except Exception:  # pragma: no cover — best-effort
                pass
            raise

        chosen = self._registry.get_runtime(selection.selected_runtime_id)
        record = DispatchRecord(
            dispatch_id=_new_dispatch_id(),
            task_id=task_id,
            run_id=run_id,
            selected_runtime_id=chosen.runtime_id,
            selected_runtime_type=chosen.runtime_type,
            selection_reason=selection.selection_reason,
            candidate_count=selection.candidate_count,
            attempt_number=1,
            dispatch_status=DispatchStatus.SELECTED,
            rejected_reasons=selection.rejected_reasons,
        )
        self._registry.record_dispatch(record)
        return record

    def mark_dispatched(self, dispatch_id: str) -> bool:
        """Flip a `selected` record to `dispatched`."""
        return self._registry.update_dispatch_status(
            dispatch_id, DispatchStatus.DISPATCHED
        )

    def mark_dispatch_failed(
        self,
        dispatch_id: str,
        *,
        failure_code: str,
        failure_message: str,
    ) -> bool:
        return self._registry.update_dispatch_status(
            dispatch_id,
            DispatchStatus.FAILED,
            failure_code=failure_code,
            failure_message=failure_message,
        )

    def list_dispatches(
        self,
        *,
        task_id: Optional[str] = None,
        runtime_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[DispatchRecord]:
        return self._registry.list_dispatches(
            task_id=task_id, runtime_id=runtime_id, limit=limit
        )

    # ---- Convenience ---------------------------------------------------

    def default_runtime(self) -> Optional[RuntimeDescriptor]:
        """Return the default Runtime, or None if not registered.

        Used by the AEE-4 compat path: a job without
        `runtime_requirements` is dispatched to the
        default Runtime if it's still healthy; otherwise
        the selector runs and may return a different
        Runtime.
        """
        try:
            return self._registry.get_runtime(self._default_runtime_id)
        except RuntimeNotFoundError:
            return None


# Module-level singleton.
dispatch_service = DispatchService()


__all__ = [
    "DEFAULT_RUNTIME_ID",
    "DispatchService",
    "dispatch_service",
]
