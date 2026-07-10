"""AEE-5 Runtime health model + dispatch policy.

Helpers that implement the AEE-5 task spec §4.7 rules:

* `healthy`   — dispatchable.
* `degraded`  — dispatchable, but ranked below healthy.
* `unknown`   — dispatchable when the policy
                `allow_unknown_health` is True; this is
                the AEE-4 compat default.
* `unhealthy` — NOT dispatchable.
* `offline`   — NOT dispatchable.

The selector consults `is_dispatchable()` to filter
candidates; ranking is then by `health_score()` (a
lower score ranks first).
"""
from __future__ import annotations

from typing import FrozenSet

from .models import RuntimeHealthStatus, RUNTIME_HEALTHY_STATUSES


def is_health_status(value: str) -> bool:
    """True iff `value` is one of the canonical 6 health values."""
    return isinstance(value, str) and value in RuntimeHealthStatus.ALL


def is_dispatchable(
    status: str, *, allow_unknown_health: bool = True
) -> bool:
    """True iff a Runtime in this health state can be dispatched to.

    `allow_unknown_health=True` is the AEE-4 compat
    default. When False, `unknown` Runtimes are
    excluded from the candidate set (operators can flip
    this once the health-check endpoint is being used
    in production).
    """
    if not is_health_status(status):
        return False
    if status in RUNTIME_HEALTHY_STATUSES:
        return True
    if status == RuntimeHealthStatus.UNKNOWN:
        return allow_unknown_health
    return False


def health_score(status: str) -> int:
    """Ranking key. Lower = better. Used by the selector.

    `healthy` ranks 0; `degraded` ranks 1; `unknown` is
    configurable (treated as 2 by default — i.e. lower
    priority than `degraded`).
    """
    table = {
        RuntimeHealthStatus.HEALTHY: 0,
        RuntimeHealthStatus.DEGRADED: 1,
        RuntimeHealthStatus.UNKNOWN: 2,
        RuntimeHealthStatus.UNHEALTHY: 99,
        RuntimeHealthStatus.OFFLINE: 100,
    }
    return table.get(status, 50)


__all__ = [
    "is_dispatchable",
    "is_health_status",
    "health_score",
]
