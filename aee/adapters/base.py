"""RuntimeAdapter Protocol + shared result types.

AEE's single seam to a concrete agent / worker backend. Any concrete
adapter must implement the three async methods below; the dataclasses
returned are deliberately minimal and runtime-agnostic so the rest of
AEE (dispatcher, watcher, safety) can stay runtime-neutral.

Conventions
-----------
* `submit()` returns the external run id (e.g. Hermes' `run_id`).
  It does NOT wait for completion; that's `poll()`'s job.
* `poll()` is idempotent and cheap. It should NOT raise on a
  terminal state; the caller can check `result.is_terminal`.
* `cancel()` is best-effort. Returning `cancelled=False` with a
  reason is acceptable (e.g. upstream already finished).
* All methods raise `RuntimeError` (or subclasses) on transport
  failure so the dispatcher can decide retry policy.

The Protocol is structural — adapters do not need to inherit from
`RuntimeAdapter` to satisfy it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class RuntimeSubmitResult:
    """Returned by `RuntimeAdapter.submit()`.

    Attributes:
        external_run_id: The backend's run id (e.g. Hermes `run_id`).
            This is the value stored in `tasks.external_run_id` and
            also surfaced as `hermes_run_id` for backward compat.
        status: Backend-reported initial status. Most adapters return
            "queued" or "running" here.
        raw: Unmodified backend response, for audit.
    """

    external_run_id: str
    status: str = "queued"
    raw: Optional[Mapping[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "external_run_id": self.external_run_id,
            "status": self.status,
            "raw": dict(self.raw) if self.raw is not None else None,
        }


@dataclass
class RuntimePollResult:
    """Returned by `RuntimeAdapter.poll()`.

    Attributes:
        external_run_id: Same id returned by `submit()`.
        status: One of "queued", "running", "completed", "failed",
            "cancelled", "timeout". Adapters that use different
            vocabularies should translate.
        is_terminal: True iff the backend considers this run done
            (success or failure). The dispatcher uses this to
            transition the job to a terminal state.
        output: Best-effort output text (may be None while running).
        error: Backend error message (set when status == "failed").
        usage: Optional usage dict (tokens / cost), if the backend
            reports it. Stored verbatim.
        raw: Unmodified backend response, for audit.
    """

    external_run_id: str
    status: str
    is_terminal: bool = False
    output: Optional[str] = None
    error: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    raw: Optional[Mapping[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "external_run_id": self.external_run_id,
            "status": self.status,
            "is_terminal": self.is_terminal,
            "output": self.output,
            "error": self.error,
            "usage": self.usage,
            "raw": dict(self.raw) if self.raw is not None else None,
        }


@dataclass
class RuntimeCancelResult:
    """Returned by `RuntimeAdapter.cancel()`.

    Attributes:
        external_run_id: The id we tried to cancel.
        cancelled: True iff the backend acknowledges the cancellation.
            False is acceptable when the run already terminated.
        reason: Optional human-readable reason (e.g. "already
            completed" or upstream error).
    """

    external_run_id: str
    cancelled: bool
    reason: str = ""
    raw: Optional[Mapping[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "external_run_id": self.external_run_id,
            "cancelled": self.cancelled,
            "reason": self.reason,
            "raw": dict(self.raw) if self.raw is not None else None,
        }


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RuntimeError(Exception):
    """Base class for adapter transport / parse failures.

    Adapters should raise subclasses (or this class) for conditions
    that the dispatcher should treat as a transient infrastructure
    problem rather than a job-level failure.
    """

    def __init__(self, message: str, *, cause: Optional[BaseException] = None):
        super().__init__(message)
        self.__cause__ = cause


class AdapterNotFoundError(RuntimeError):
    """No adapter registered for the requested `adapter_name`."""


class UnknownExternalRunError(RuntimeError):
    """Backend does not recognise the `external_run_id` we asked about."""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class RuntimeAdapter(Protocol):
    """The seam AEE uses to talk to any concrete agent / worker backend.

    Concrete adapters (HermesAdapter, FakeAdapter, future Pi /
    Claude Code / MCP adapters) must implement all three methods.
    Adapters are stateless w.r.t. the job; all per-job state lives
    on the backend (or, for the fake adapter, in an in-memory dict).
    """

    name: str
    runtime_type: str  # "hermes" | "pi_agent" | "claude_code" | "fake" | ...

    async def submit(self, job: "Job") -> RuntimeSubmitResult: ...

    async def poll(self, external_run_id: str) -> RuntimePollResult: ...

    async def cancel(self, external_run_id: str) -> RuntimeCancelResult: ...


# Imported here to break the circular import (Job lives in aee.core).
# Adapter implementations can take `Job` as a parameter type-hint
# without importing it at module top-level.
from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from aee.core.job_models import Job
