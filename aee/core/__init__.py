"""AEE core — runtime-neutral task lifecycle and registry.

Modules:
    job_models    — `Job` dataclass + validation
    state_machine — legal status transitions
    dispatcher    — high-level create/claim/complete/cancel flow
    registry      — runtime adapter + worker registry

For AEE-1 the dispatcher still delegates to the existing
`dispatcher.manager` / `dispatcher.db` SQLite layer. AEE-2 will move
worker claim into this package.
"""
from __future__ import annotations

from .job_models import Job, JobCreate  # noqa: F401
from .state_machine import (  # noqa: F401
    JobStatus,
    can_transition,
    assert_transition,
)
from .registry import AdapterRegistry, WorkerRegistry  # noqa: F401

__all__ = [
    "Job",
    "JobCreate",
    "JobStatus",
    "can_transition",
    "assert_transition",
    "AdapterRegistry",
    "WorkerRegistry",
]
