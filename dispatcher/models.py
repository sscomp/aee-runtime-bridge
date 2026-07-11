"""Data models for the Task Dispatcher.

Pydantic models are used for the API surface (validated, JSON-serializable).
Plain dataclasses are used internally where validation is already done by
the caller.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TaskStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskType(str, Enum):
    RESEARCH = "research"
    CODING = "coding"
    OPS = "ops"
    REVIEW = "review"
    NORMAL = "normal"


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------

@dataclass
class Task:
    task_id: str
    title: str
    type: str
    priority: int = 50
    owner: str = "m2"
    status: str = "pending"
    progress_pct: int = 0
    progress_step: Optional[str] = None
    created_at: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_sec: Optional[float] = None
    input_text: Optional[str] = None
    hermes_run_id: Optional[str] = None
    openai_run_id: Optional[str] = None
    session_id: Optional[str] = None
    mode: Optional[str] = None
    result_path: Optional[str] = None
    error_message: Optional[str] = None
    warning_count: int = 0
    retry_count: int = 0
    prompt_version: Optional[str] = None
    model_name: Optional[str] = None
    git_commit: Optional[str] = None
    git_branch: Optional[str] = None
    # AEE-1: runtime-neutral task fields. The dispatcher previously
    # used `hermes_run_id` as the canonical "external" id; from
    # AEE-1 onward, `external_run_id` is canonical and
    # `hermes_run_id` is preserved for backward compat as an alias
    # for the same value.
    runtime_type: str = "hermes"
    adapter_name: str = "hermes"
    external_run_id: Optional[str] = None
    worker_id: Optional[str] = None
    heartbeat_at: Optional[str] = None
    claim_token_hash: Optional[str] = None
    approval_required: bool = False
    approval_state: str = "not_required"
    # AEE-3: capability-based routing. The job requires a worker
    # whose `capabilities` is a superset of this list. Defaults to
    # an empty list, which means "no capability filter" (any
    # worker with the right `adapter_name` can claim). Values are
    # always stored normalized: lowercase, trimmed, deduped, sorted.
    required_capabilities: List[str] = field(default_factory=list)
    # AEE-7.2: per-job repo_root constraint. The dispatcher
    # persists this verbatim and the artifact pipeline resolves
    # it into an ArtifactPolicy at collect() time. ``None``
    # means "no per-job constraint" — the pipeline keeps its
    # permissive default. The field is purely additive; legacy
    # tasks (pre-AEE-7.2) round-trip with ``None``.
    repo_root: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Drop very large blobs from default serialization; callers can fetch
        # the full input/output via /tasks/{id}/result.
        if len(d.get("input_text") or "") > 2000:
            d["input_text"] = (d["input_text"] or "")[:2000] + "...[truncated]"
        return d


@dataclass
class TaskEvent:
    id: int
    task_id: str
    ts: str
    kind: str
    payload: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

# Legal status transitions. Anything not listed is rejected by the manager.
LEGAL_TRANSITIONS: Dict[str, List[str]] = {
    "pending":   ["queued", "running", "cancelled", "failed", "timeout"],
    "queued":    ["running", "cancelled", "failed", "timeout"],
    "running":   ["waiting", "completed", "failed", "cancelled", "timeout"],
    "waiting":   ["running", "cancelled", "failed", "timeout"],
    "completed": [],
    "failed":    ["queued"],   # retry path
    "cancelled": [],
    "timeout":   ["queued"],   # retry path
}


# Allowed progress percentages.
LEGAL_PROGRESS_PCTS = (0, 5, 10, 25, 40, 60, 80, 95, 100)


def is_legal_transition(from_status: str, to_status: str) -> bool:
    return to_status in LEGAL_TRANSITIONS.get(from_status, [])


def is_legal_progress(pct: int) -> bool:
    return pct in LEGAL_PROGRESS_PCTS
