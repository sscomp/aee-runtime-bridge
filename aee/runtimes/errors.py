"""AEE-5 Runtime Registry — structured errors.

AEE-5's contract requires *structured* errors so the
selector's "no match" outcome is observable from both
the API layer and the dispatch log. The canonical
error code is `AEE_RUNTIME_NOT_FOUND`; details include
the task's required capabilities / labels and every
candidate that was rejected, with the reason for each
rejection.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


class RuntimeRegistryError(Exception):
    """Base class for AEE-5 Runtime Registry errors."""

    code: str = "AEE_RUNTIME_REGISTRY_ERROR"


class RuntimeValidationError(RuntimeRegistryError):
    """A Runtime descriptor failed validation."""

    code = "AEE_RUNTIME_VALIDATION_ERROR"


class RuntimeNotFoundError(RuntimeRegistryError):
    """No Runtime matches the requested criteria.

    This is the AEE-5 selector's "I tried everyone, none
    of them fit" exit. The structured `details` is what
    the dispatch service logs to the task's `task_events`
    table; it is the same shape the API surfaces in the
    422 response body.
    """

    code = "AEE_RUNTIME_NOT_FOUND"

    def __init__(
        self,
        message: str = "No enabled runtime satisfies the task requirements.",
        *,
        task_id: Optional[str] = None,
        run_id: Optional[str] = None,
        required_runtime_type: Optional[str] = None,
        required_capabilities: Optional[List[str]] = None,
        required_labels: Optional[Dict[str, str]] = None,
        evaluated_runtimes: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.task_id = task_id
        self.run_id = run_id
        self.required_runtime_type = required_runtime_type
        self.required_capabilities = list(required_capabilities or [])
        self.required_labels = dict(required_labels or {})
        self.evaluated_runtimes: List[Dict[str, Any]] = list(
            evaluated_runtimes or []
        )

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe representation of the structured error."""
        return {
            "code": self.code,
            "message": self.message,
            "details": {
                "task_id": self.task_id,
                "run_id": self.run_id,
                "required_runtime_type": self.required_runtime_type,
                "required_capabilities": list(self.required_capabilities),
                "required_labels": dict(self.required_labels),
                "evaluated_runtimes": list(self.evaluated_runtimes),
            },
        }


__all__ = [
    "RuntimeRegistryError",
    "RuntimeNotFoundError",
    "RuntimeValidationError",
]
