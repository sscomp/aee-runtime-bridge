"""AEE security policy.

AEE-1 re-exports `dispatcher.safety.evaluate` so the rest of the
package has a single canonical entrypoint. AEE-2 will add per-job
`approval_required` and `approval_state` checks.
"""
from __future__ import annotations

from dispatcher.safety import (  # noqa: F401
    SafetyDecision,
    evaluate as evaluate_safety,
)

__all__ = ["SafetyDecision", "evaluate_safety"]
