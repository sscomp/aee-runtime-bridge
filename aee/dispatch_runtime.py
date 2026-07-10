"""AEE-5 dispatch + Runtime Registry integration.

The `Job` model lives in `aee.core.job_models`; the
selector and registry live in `aee.runtimes`; the
dispatch service that glues them together lives in
`aee.dispatch`. This re-exports make the canonical
import path `from aee.dispatch_runtime import ...`
work, which is what the AEE-5 task spec implies.
"""
from __future__ import annotations

from aee.dispatch.service import (  # noqa: F401
    DEFAULT_RUNTIME_ID,
    DispatchService,
    dispatch_service,
)
from aee.runtimes.errors import (  # noqa: F401
    RuntimeNotFoundError,
    RuntimeRegistryError,
    RuntimeValidationError,
)
from aee.runtimes.models import (  # noqa: F401
    DispatchRecord,
    DispatchStatus,
    RuntimeCapabilities,
    RuntimeDescriptor,
    RuntimeHealth,
    RuntimeHealthStatus,
    RuntimeLimits,
    RuntimeSelectionResult,
    TaskRuntimeRequirements,
)
from aee.runtimes.registry import (  # noqa: F401
    RuntimeRegistry,
    bootstrap_default_runtimes,
    runtime_registry,
)
from aee.runtimes.selector import (  # noqa: F401
    RuntimeSelector,
    select_runtime,
)

__all__ = [
    "DEFAULT_RUNTIME_ID",
    "DispatchService",
    "DispatchRecord",
    "DispatchStatus",
    "RuntimeCapabilities",
    "RuntimeDescriptor",
    "RuntimeHealth",
    "RuntimeHealthStatus",
    "RuntimeLimits",
    "RuntimeNotFoundError",
    "RuntimeRegistry",
    "RuntimeRegistryError",
    "RuntimeSelectionResult",
    "RuntimeSelector",
    "RuntimeValidationError",
    "TaskRuntimeRequirements",
    "bootstrap_default_runtimes",
    "dispatch_service",
    "runtime_registry",
    "select_runtime",
]
