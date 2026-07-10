"""AEE job state machine.

The state vocabulary is shared with the dispatcher's `Task.status`
so that AEE can drive the existing state transitions while
adapters stay runtime-neutral.

States:
    queued      — accepted, no worker has claimed it yet
    running     — a worker has claimed and is executing
    completed   — terminal: success
    failed      — terminal: error
    cancelled   — terminal: cancelled by user / reaper
    timeout     — terminal: reaped for lack of heartbeat

Legal transitions:
    queued      -> running | cancelled | failed | timeout
    running     -> completed | failed | cancelled | timeout
    (terminal)  -> (none)
"""
from __future__ import annotations

from typing import FrozenSet, Mapping


# Status values used by AEE and the underlying dispatcher.
class JobStatus:
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"

    ALL = (QUEUED, RUNNING, COMPLETED, FAILED, CANCELLED, TIMEOUT)
    TERMINAL: FrozenSet[str] = frozenset({COMPLETED, FAILED, CANCELLED, TIMEOUT})


# Adjacency list: from -> {to}
_LEGAL: Mapping[str, FrozenSet[str]] = {
    JobStatus.QUEUED: frozenset({
        JobStatus.RUNNING,
        JobStatus.CANCELLED,
        JobStatus.FAILED,
        JobStatus.TIMEOUT,
    }),
    JobStatus.RUNNING: frozenset({
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.TIMEOUT,
    }),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
    JobStatus.TIMEOUT: frozenset(),
}


def can_transition(from_status: str, to_status: str) -> bool:
    """Return True iff `from_status -> to_status` is a legal move."""
    if from_status not in _LEGAL:
        return False
    return to_status in _LEGAL[from_status]


class IllegalTransition(Exception):
    """Raised when a state transition is not allowed.

    Inherits from `Exception` so the dispatcher's existing
    `IllegalTransition` (in `dispatcher.manager`) is not shadowed;
    we re-export the canonical one below.
    """


def assert_transition(from_status: str, to_status: str) -> None:
    """Raise `IllegalTransition` if the move isn't allowed."""
    if not can_transition(from_status, to_status):
        raise IllegalTransition(
            f"illegal job state transition: {from_status!r} -> {to_status!r}"
        )


__all__ = [
    "JobStatus",
    "IllegalTransition",
    "can_transition",
    "assert_transition",
]
