"""AEE-5 Dispatch Service — the seam between job creation and
Runtime selection."""
from .service import (  # noqa: F401
    DEFAULT_RUNTIME_ID,
    DispatchService,
    dispatch_service,
)

__all__ = ["DEFAULT_RUNTIME_ID", "DispatchService", "dispatch_service"]
