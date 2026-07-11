"""AEE-7 Orchestrator — `Provider` Protocol + result types.

The `Provider` protocol is the *single seam* the dispatcher talks
to. It is intentionally narrower than the existing
``RuntimeAdapter`` (AEE-2, HTTP-side) and ``ExecProvider`` (AEE-6,
subprocess-side) protocols because it deliberately hides the
underlying transport. A concrete provider implementation
(e.g. ``ClaudeCodeExecProvider``) wraps an ``ExecProvider``;
another (``HermesRuntimeAdapterProvider``) wraps a
``RuntimeAdapter``.

Why a third abstraction?
------------------------
* ``RuntimeAdapter`` carries HTTP vocabulary (``status: queued /
  running / completed / failed / cancelled / timeout``) and
  ``output: str`` — fine for LLM-streaming HTTP backends.
* ``ExecProvider`` carries subprocess vocabulary
  (``ExecStatus.PENDING / RUNNING / EXITED / CANCELLED /
  TIMED_OUT / FAILED``) plus ``pid`` / ``exit_code`` / ``stderr``
  — fine for CLI tools.
* The dispatcher wants **both** behind one consistent submit /
  poll / cancel interface so the existing watcher (AEE-2) and
  the Phase 4 delivery verification do not have to know which
  transport produced the file.

AEE-7.1 keeps the two lower-level protocols unchanged; the
``Provider`` Protocol is a thin wrapper layer.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    Mapping,
    Optional,
    Protocol,
    runtime_checkable,
)

from aee.runtimes.models import TaskRuntimeRequirements


# ---------------------------------------------------------------------------
# Status vocabulary (orchestrator-side, transport-neutral)
# ---------------------------------------------------------------------------


class ProviderStatus(str, enum.Enum):
    """Transport-neutral lifecycle states.

    Maps to ``RuntimePollResult.status`` for the watcher; the watcher
    itself does not have to know whether the run came from a
    subprocess or an HTTP backend.
    """

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"

    @property
    def is_terminal(self) -> bool:
        return self in (
            ProviderStatus.COMPLETED,
            ProviderStatus.FAILED,
            ProviderStatus.CANCELLED,
            ProviderStatus.TIMEOUT,
        )


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ProviderRun:
    """Opaque handle to a single submitted run.

    Carries the ``external_run_id`` the watcher polls, plus a
    free-form ``metadata`` dict the provider implementation can use
    to keep per-run state (subprocess handle, HTTP cookie, etc.).
    The orchestrator stores this in a process-local cache.
    """

    external_run_id: str
    provider_name: str
    runtime_type: str
    started_at: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderStatusResult:
    """Returned by ``Provider.poll()``.

    Fields
    ------
    external_run_id
        Echoed back so the caller can correlate.
    status
        Orchestrator-side status (ProviderStatus enum).
    is_terminal
        Convenience property: True when the run is done.
    output
        Best-effort output text. May be empty while still running.
    error
        Backend error message; set when status is FAILED / TIMEOUT.
    exit_code
        Subprocess exit code, or HTTP status. None while running.
    usage
        Optional usage dict (token counts etc.) — provider-defined.
    raw
        Unmodified backend payload, for audit.
    """

    external_run_id: str
    status: ProviderStatus
    is_terminal: bool
    output: Optional[str] = None
    error: Optional[str] = None
    exit_code: Optional[int] = None
    usage: Optional[Dict[str, Any]] = None
    raw: Optional[Mapping[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "external_run_id": self.external_run_id,
            "status": self.status.value,
            "is_terminal": self.is_terminal,
            "output": self.output,
            "error": self.error,
            "exit_code": self.exit_code,
            "usage": dict(self.usage) if self.usage else None,
            "raw": dict(self.raw) if self.raw is not None else None,
        }


# Backward-compat alias: ProviderStatus name was reused as both
# the enum and a result dataclass. Tests in the AEE-7 series use
# the explicit name ``ProviderStatusResult`` for the result, and
# the enum is still imported as ``ProviderStatus``.
ProviderCancel = ProviderStatusResult  # placeholder removed below


@dataclass
class ProviderCancelResult:
    """Returned by ``Provider.cancel()``."""

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


# Override the alias with the real dataclass.
ProviderCancel = ProviderCancelResult  # noqa: F811


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProviderError(Exception):
    """Base class for orchestrator-level provider failures.

    Subclasses cover the dispatcher's expected failure modes:
    * :class:`ProviderSubmitError` — submit() raised (binary missing, spawn error).
    * :class:`ProviderTimeoutError` — supervisor timer fired.
    * :class:`ProviderExitError` — subprocess exited non-zero.
    * :class:`ProviderNotFoundError` — no provider registered for the requested
      ``runtime_type`` (distinct from ``RuntimeNotFoundError`` which means
      the *runtime* is not in the registry).
    """

    def __init__(self, message: str, *, cause: Optional[BaseException] = None) -> None:
        super().__init__(message)
        self.__cause__ = cause


class ProviderSubmitError(ProviderError):
    """submit() raised. Maps to manager.fail()."""


class ProviderTimeoutError(ProviderError):
    """The provider's supervisor timer fired. Maps to manager.timeout()."""


class ProviderExitError(ProviderError):
    """Subprocess exited non-zero. Maps to manager.fail()."""


class ProviderNotFoundError(ProviderError):
    """No provider implementation registered for ``runtime_type``."""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Provider(Protocol):
    """The seam AEE-7.1 talks to.

    Implementations:
    * :class:`aee.orchestrator.hermes_provider.HermesRuntimeAdapterProvider`
      — wraps ``HermesAdapter`` (HTTP).
    * :class:`aee.orchestrator.claude_code_provider_shim.ClaudeCodeExecProvider`
      — wraps ``ClaudeCodeProvider`` (subprocess).
    * :class:`aee.orchestrator.fake_provider.FakeProvider` — in-memory
      test stub with pluggable behavior.
    """

    name: str
    runtime_type: str

    async def submit(
        self,
        *,
        prompt: str,
        requirements: TaskRuntimeRequirements,
        repo: Any,  # RuntimeRepository
        run_id: Optional[str] = None,
    ) -> ProviderRun: ...

    async def poll(
        self,
        run: ProviderRun,
    ) -> ProviderStatusResult: ...

    async def cancel(
        self,
        run: ProviderRun,
    ) -> ProviderCancelResult: ...

    def artifacts_dir(self, run: ProviderRun) -> Optional[str]:
        """Return the directory the worker wrote artifacts to.

        ``None`` if the provider does not expose a directory
        (e.g. Hermes HTTP provider — artifacts are inline). The
        orchestrator uses this to drive ``ArtifactPipeline.collect()``
        over the worker's writes.
        """
        ...


__all__ = [
    "Provider",
    "ProviderRun",
    "ProviderStatus",
    "ProviderStatusResult",
    "ProviderCancelResult",
    "ProviderError",
    "ProviderSubmitError",
    "ProviderTimeoutError",
    "ProviderExitError",
    "ProviderNotFoundError",
]
