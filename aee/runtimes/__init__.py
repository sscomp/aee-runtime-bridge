"""AEE-5 Runtime Registry — public API.

This package is the formal home of AEE-5's runtime metadata
and selection. The original AEE-1 `aee.core.registry` keeps
its own narrower role: the `adapter_registry` (a
Protocol-based registry of wire-protocol adapters) and the
`worker_registry` (AEE-2 worker fleet). AEE-5's Runtime
Registry sits one layer above both — it is the policy
layer that decides *which* Runtime should be picked for
a given job, while the adapter registry decides *how*
to talk to that Runtime's wire protocol.
"""
from __future__ import annotations

from .models import (  # noqa: F401
    DispatchRecord,
    DispatchStatus,
    RuntimeDescriptor,
    RuntimeCapabilities,
    RuntimeHealth,
    RuntimeHealthStatus,
    RuntimeLimits,
    RuntimeSelectionResult,
    TaskRuntimeRequirements,
)
from .errors import (  # noqa: F401
    RuntimeNotFoundError,
    RuntimeRegistryError,
    RuntimeValidationError,
)
from .registry import RuntimeRegistry, bootstrap_default_runtimes, runtime_registry  # noqa: F401
from .selector import RuntimeSelector, select_runtime  # noqa: F401
from .repository import (  # noqa: F401
    InMemoryRuntimeRepository,
    RuntimeRepository,
    SqliteRuntimeRepository,
)
from .health import (  # noqa: F401
    RUNTIME_HEALTHY_STATUSES,
    is_dispatchable,
    is_health_status,
)

__all__ = [
    # Models
    "DispatchRecord",
    "DispatchStatus",
    "RuntimeDescriptor",
    "RuntimeCapabilities",
    "RuntimeHealth",
    "RuntimeHealthStatus",
    "RuntimeLimits",
    "RuntimeSelectionResult",
    "TaskRuntimeRequirements",
    # Errors
    "RuntimeNotFoundError",
    "RuntimeRegistryError",
    "RuntimeValidationError",
    # Service
    "RuntimeRegistry",
    "runtime_registry",
    "RuntimeSelector",
    "select_runtime",
    # Repository
    "InMemoryRuntimeRepository",
    "RuntimeRepository",
    "SqliteRuntimeRepository",
    # Health
    "RUNTIME_HEALTHY_STATUSES",
    "is_dispatchable",
    "is_health_status",
]
