"""AEE-7 Orchestrator package.

Multi-provider orchestration seam between the dispatcher and the
runtime backends (Hermes HTTP, Claude Code subprocess, future
plugins).

Public surface:

* :class:`Provider`     — the unified async protocol.
* :class:`ProviderRun`  — opaque handle to a running submission.
* :class:`ProviderStatus` / :class:`ProviderCancel` — return shapes.
* :class:`ProviderError` and friends — the error vocabulary.
* :class:`ExecutionOrchestrator` — the single-step coordinator that
  the dispatcher calls into.
* :func:`provider_for_descriptor` — factory mapping a
  ``RuntimeDescriptor`` to a concrete ``Provider``.
* :func:`register_provider` / :func:`registered_providers` —
  the runtime-typed registry of factory functions.
"""
from __future__ import annotations

# Errors
from aee.runtimes.errors import RuntimeNotFoundError
from aee.runtimes.models import (
    RuntimeDescriptor,
    RuntimeHealth,
    RuntimeHealthStatus,
)

from .provider import (
    Provider,
    ProviderCancel,
    ProviderCancelResult,
    ProviderError,
    ProviderExitError,
    ProviderNotFoundError,
    ProviderRun,
    ProviderStatus,
    ProviderStatusResult,
    ProviderSubmitError,
    ProviderTimeoutError,
)
from .factory import (
    provider_for_descriptor,
    register_provider,
    registered_providers,
    unregister_provider,
)
from .orchestrator import (
    ExecutionOrchestrator,
    OrchestratorResult,
    clear_test_runtimes,
    register_runtimes_for_test,
)

__all__ = [
    # Protocols / shapes
    "Provider",
    "ProviderRun",
    "ProviderStatus",
    "ProviderStatusResult",
    "ProviderCancel",
    "ProviderCancelResult",
    # Errors
    "ProviderError",
    "ProviderExitError",
    "ProviderSubmitError",
    "ProviderTimeoutError",
    "ProviderNotFoundError",
    "RuntimeNotFoundError",
    # Orchestrator
    "ExecutionOrchestrator",
    "OrchestratorResult",
    "clear_test_runtimes",
    "register_runtimes_for_test",
    # Registry helpers
    "provider_for_descriptor",
    "register_provider",
    "unregister_provider",
    "registered_providers",
    # Common descriptor constructors (so tests can build a fake
    # descriptor without reaching into ``aee.runtimes.models``)
    "RuntimeDescriptor",
    "RuntimeHealth",
    "RuntimeHealthStatus",
]
