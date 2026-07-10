"""AEE-5 Runtime domain models.

Dataclasses that describe Runtime metadata, task requirements,
selection results, and dispatch records. These are
runtime-neutral and storage-free — the `repository.py`
module is the only place that knows the SQL shape.

Naming note
-----------
The pre-AEE-5 term "runtime type" survives in two
distinct places:

* `aee.core.job_models.Job.runtime_type` — the AEE-1
  adapter-type key (e.g. "hermes", "aee_lightweight",
  "fake"). This is the wire-protocol dimension.
* `aee.runtimes.RuntimeDescriptor.runtime_type` — the
  AEE-5 logical runtime family (e.g. "aee_lightweight",
  "claude_code", "shell"). This is the metadata /
  policy dimension.

The two are intentionally decoupled: one Runtime
descriptor can be backed by one adapter today, multiple
adapters later. The job's `runtime_type` (AEE-1)
controls adapter selection; the descriptor's
`runtime_type` controls Runtime selection. They use
the same vocabulary because the built-in Runtime
(`aee-lightweight-local`) ships with a single
conforming adapter today; future Runtimes may have
more than one adapter each.

The two previously co-existing AEE-1 fields,
`Job.runtime_type` and `Job.adapter_name`, are
preserved unchanged. AEE-5 only adds
`Job.runtime_requirements` (a `TaskRuntimeRequirements`).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Health model
# ---------------------------------------------------------------------------


class RuntimeHealthStatus:
    """The 5-status set from the AEE-5 task spec §4.7.

    `unknown` is the 6th value (used as the schema default
    for legacy Runtimes that have never reported health).
    It is treated as dispatchable when the policy
    `allow_unknown_health` is true (the AEE-4 compat
    default).
    """

    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    OFFLINE = "offline"

    ALL = (UNKNOWN, HEALTHY, DEGRADED, UNHEALTHY, OFFLINE)


# The status set that can be *dispatched to* in the
# default policy. `unknown` is *not* in this set; it's
# dispatchable only when `allow_unknown_health` is true
# (so the AEE-4 compat path keeps working for Runtimes
# that haven't reported health yet).
RUNTIME_HEALTHY_STATUSES = frozenset(
    {RuntimeHealthStatus.HEALTHY, RuntimeHealthStatus.DEGRADED}
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Runtime descriptor
# ---------------------------------------------------------------------------


@dataclass
class RuntimeCapabilities:
    """Capability set a Runtime advertises.

    Capabilities are stored normalized (lowercase, trimmed,
    deduped, sorted) the same way worker capabilities
    already are. Two Runtimes with the same set in
    different orders are equal.
    """

    capabilities: List[str] = field(default_factory=list)

    def normalized(self) -> List[str]:
        """Lowercase, trim, drop empties, dedupe, sort."""
        out: List[str] = []
        seen: set = set()
        for c in self.capabilities or []:
            if not isinstance(c, str):
                continue
            n = c.strip().lower()
            if not n or n in seen:
                continue
            seen.add(n)
            out.append(n)
        out.sort()
        return out

    def to_list(self) -> List[str]:
        """Return the canonical form (always normalized)."""
        return self.normalized()

    def to_dict(self) -> Dict[str, Any]:
        return {"capabilities": self.normalized()}


@dataclass
class RuntimeLimits:
    """Capacity + timeout limits for a Runtime."""

    max_concurrency: int = 1
    timeout_seconds: int = 1800

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_concurrency": int(self.max_concurrency),
            "timeout_seconds": int(self.timeout_seconds),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "RuntimeLimits":
        if not data:
            return cls()
        if not isinstance(data, dict):
            return cls()
        return cls(
            max_concurrency=int(data.get("max_concurrency", 1) or 1),
            timeout_seconds=int(data.get("timeout_seconds", 1800) or 1800),
        )


@dataclass
class RuntimeHealth:
    """A Runtime's current health state.

    `last_checked_at` is set when the health status is
    updated via `update_runtime_health()`. `unknown` is
    the pre-AEE-5 default.
    """

    status: str = RuntimeHealthStatus.UNKNOWN
    last_checked_at: Optional[str] = None
    message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "last_checked_at": self.last_checked_at,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "RuntimeHealth":
        if not data:
            return cls()
        if not isinstance(data, dict):
            return cls()
        status = data.get("status") or RuntimeHealthStatus.UNKNOWN
        if not isinstance(status, str):
            status = RuntimeHealthStatus.UNKNOWN
        if status not in RuntimeHealthStatus.ALL:
            status = RuntimeHealthStatus.UNKNOWN
        return cls(
            status=status,
            last_checked_at=data.get("last_checked_at"),
            message=data.get("message"),
        )


@dataclass
class RuntimeDescriptor:
    """A registered AEE Runtime.

    Fields are documented in the AEE-5 task spec §4.1.
    `runtime_id` is the unique key; `runtime_type` is the
    logical family. `enabled` is the dispatchable flag;
    `health` is the runtime health state. `endpoint` and
    `version` are descriptive only.
    """

    runtime_id: str
    runtime_type: str
    display_name: str = ""
    version: str = "1.0.0"
    enabled: bool = True
    endpoint: str = "local"
    capabilities: RuntimeCapabilities = field(default_factory=RuntimeCapabilities)
    labels: Dict[str, str] = field(default_factory=dict)
    limits: RuntimeLimits = field(default_factory=RuntimeLimits)
    health: RuntimeHealth = field(default_factory=RuntimeHealth)
    registered_at: str = field(default_factory=_now_iso)
    updated_at: Optional[str] = None

    # ---- Helpers --------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe dict representation."""
        return {
            "runtime_id": self.runtime_id,
            "runtime_type": self.runtime_type,
            "display_name": self.display_name,
            "version": self.version,
            "enabled": bool(self.enabled),
            "endpoint": self.endpoint,
            "capabilities": self.capabilities.to_list(),
            "labels": dict(self.labels),
            "limits": self.limits.to_dict(),
            "health": self.health.to_dict(),
            "registered_at": self.registered_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RuntimeDescriptor":
        """Build a descriptor from a JSON-style dict.

        Performs minimal validation; the registry's
        `register_runtime()` does the deeper checks.
        """
        if not isinstance(data, dict):
            raise ValueError("RuntimeDescriptor payload must be a dict")
        caps_raw = data.get("capabilities") or []
        if not isinstance(caps_raw, list):
            caps_raw = []
        labels_raw = data.get("labels") or {}
        if not isinstance(labels_raw, dict):
            labels_raw = {}
        # Coerce label values to strings.
        labels = {str(k): str(v) for k, v in labels_raw.items()}
        return cls(
            runtime_id=str(data.get("runtime_id", "")).strip(),
            runtime_type=str(data.get("runtime_type", "")).strip(),
            display_name=str(data.get("display_name", "")).strip(),
            version=str(data.get("version", "1.0.0")).strip() or "1.0.0",
            enabled=bool(data.get("enabled", True)),
            endpoint=str(data.get("endpoint", "local")).strip() or "local",
            capabilities=RuntimeCapabilities(caps_raw),
            labels=labels,
            limits=RuntimeLimits.from_dict(data.get("limits")),
            health=RuntimeHealth.from_dict(data.get("health")),
            registered_at=str(
                data.get("registered_at") or _now_iso()
            ),
            updated_at=data.get("updated_at"),
        )


# ---------------------------------------------------------------------------
# Task Runtime requirements
# ---------------------------------------------------------------------------


@dataclass
class TaskRuntimeRequirements:
    """Optional Runtime requirements attached to a Job.

    All fields are optional. When all fields are empty /
    None, the dispatcher falls back to the default
    `aee-lightweight-local` Runtime (AEE-4 compat).

    Semantics:
      * `runtime_type` — exact-match filter; if set, only
        Runtimes of this type are candidates.
      * `required_capabilities` — every Runtime in
        candidate set must have every required capability.
      * `required_labels` — every Runtime in candidate set
        must have every required label (k=v) (subset).
      * `preferred_runtime_ids` — sort key: preferred
        Runtimes rank first (after required filters).
      * `preferred_capabilities` — tie-breaker weight:
        Runtimes with more of these get a higher score.
      * `excluded_runtime_ids` — hard exclude; Runtimes in
        this set are NEVER candidates.
    """

    runtime_type: Optional[str] = None
    required_capabilities: List[str] = field(default_factory=list)
    preferred_capabilities: List[str] = field(default_factory=list)
    required_labels: Dict[str, str] = field(default_factory=dict)
    preferred_runtime_ids: List[str] = field(default_factory=list)
    excluded_runtime_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "runtime_type": self.runtime_type,
            "required_capabilities": list(self.required_capabilities),
            "preferred_capabilities": list(self.preferred_capabilities),
            "required_labels": dict(self.required_labels),
            "preferred_runtime_ids": list(self.preferred_runtime_ids),
            "excluded_runtime_ids": list(self.excluded_runtime_ids),
        }

    @classmethod
    def from_dict(
        cls, data: Optional[Dict[str, Any]]
    ) -> "TaskRuntimeRequirements":
        if not data:
            return cls()
        if not isinstance(data, dict):
            return cls()
        rt = data.get("runtime_type")
        if rt is not None:
            rt = str(rt).strip() or None
        req_caps = data.get("required_capabilities") or []
        if not isinstance(req_caps, list):
            req_caps = []
        pref_caps = data.get("preferred_capabilities") or []
        if not isinstance(pref_caps, list):
            pref_caps = []
        req_labels = data.get("required_labels") or {}
        if not isinstance(req_labels, dict):
            req_labels = {}
        # Coerce label values to strings.
        req_labels = {str(k): str(v) for k, v in req_labels.items()}
        pref_ids = data.get("preferred_runtime_ids") or []
        if not isinstance(pref_ids, list):
            pref_ids = []
        excl_ids = data.get("excluded_runtime_ids") or []
        if not isinstance(excl_ids, list):
            excl_ids = []
        return cls(
            runtime_type=rt,
            required_capabilities=[str(c) for c in req_caps if c],
            preferred_capabilities=[str(c) for c in pref_caps if c],
            required_labels=req_labels,
            preferred_runtime_ids=[str(c) for c in pref_ids if c],
            excluded_runtime_ids=[str(c) for c in excl_ids if c],
        )

    def is_empty(self) -> bool:
        """True iff no field constrains the selector."""
        return (
            not self.runtime_type
            and not self.required_capabilities
            and not self.preferred_capabilities
            and not self.required_labels
            and not self.preferred_runtime_ids
            and not self.excluded_runtime_ids
        )


# ---------------------------------------------------------------------------
# Selection result + dispatch record
# ---------------------------------------------------------------------------


class DispatchStatus:
    """Outcome of a dispatch attempt.

    Mirrors the AEE-5 task spec §4.6 vocabulary:

      * `selected` — a Runtime was chosen.
      * `dispatched` — the Job was actually handed to the
        adapter (this is the AEE-1 wire-protocol step; AEE-5
        records it as the success terminal state).
      * `failed` — the dispatch failed (e.g. no Runtime
        matched, or the adapter raised).
    """

    SELECTED = "selected"
    DISPATCHED = "dispatched"
    FAILED = "failed"
    ALL = (SELECTED, DISPATCHED, FAILED)


@dataclass
class RuntimeSelectionResult:
    """The selector's return type.

    `selected_runtime_id` is None when the result is
    `AEE_RUNTIME_NOT_FOUND`. `rejected_runtime_ids` is
    the list of `(runtime_id, reason)` pairs the selector
    considered but rejected; the API surfaces this so
    operators can see *why* a job couldn't be placed.
    """

    selected_runtime_id: Optional[str] = None
    selection_reason: str = ""
    candidate_count: int = 0
    evaluated_runtime_ids: List[str] = field(default_factory=list)
    rejected_reasons: Dict[str, List[str]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "selected_runtime_id": self.selected_runtime_id,
            "selection_reason": self.selection_reason,
            "candidate_count": int(self.candidate_count),
            "evaluated_runtime_ids": list(self.evaluated_runtime_ids),
            "rejected_reasons": {
                k: list(v) for k, v in (self.rejected_reasons or {}).items()
            },
        }

    @property
    def is_matched(self) -> bool:
        return self.selected_runtime_id is not None


@dataclass
class DispatchRecord:
    """A historical record of a single dispatch attempt.

    Persisted in the `dispatch_records` table. The record
    captures the AEE-5 task spec §4.6 fields exactly. The
    AEE-5 selector and the dispatch service are the only
    writers.
    """

    dispatch_id: str
    task_id: str
    run_id: str
    selected_runtime_id: str
    selected_runtime_type: str
    selection_reason: str = ""
    candidate_count: int = 0
    attempt_number: int = 1
    dispatched_at: str = field(default_factory=_now_iso)
    dispatch_status: str = DispatchStatus.SELECTED
    failure_code: Optional[str] = None
    failure_message: Optional[str] = None
    rejected_reasons: Dict[str, List[str]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dispatch_id": self.dispatch_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "selected_runtime_id": self.selected_runtime_id,
            "selected_runtime_type": self.selected_runtime_type,
            "selection_reason": self.selection_reason,
            "candidate_count": int(self.candidate_count),
            "attempt_number": int(self.attempt_number),
            "dispatched_at": self.dispatched_at,
            "dispatch_status": self.dispatch_status,
            "failure_code": self.failure_code,
            "failure_message": self.failure_message,
            "rejected_reasons": {
                k: list(v) for k, v in (self.rejected_reasons or {}).items()
            },
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "DispatchRecord":
        """Build from a SQLite-shaped row (or any dict)."""
        rr_raw = row.get("rejected_reasons") or row.get("rejected_reasons_json")
        rr: Dict[str, List[str]] = {}
        if rr_raw:
            if isinstance(rr_raw, str):
                try:
                    parsed = json.loads(rr_raw)
                except (ValueError, TypeError):
                    parsed = {}
                if isinstance(parsed, dict):
                    rr = {str(k): list(v) for k, v in parsed.items() if v}
            elif isinstance(rr_raw, dict):
                rr = {str(k): list(v) for k, v in rr_raw.items() if v}
        return cls(
            dispatch_id=str(row.get("dispatch_id", "")).strip(),
            task_id=str(row.get("task_id", "")).strip(),
            run_id=str(row.get("run_id", "")).strip(),
            selected_runtime_id=str(row.get("selected_runtime_id", "")).strip(),
            selected_runtime_type=str(
                row.get("selected_runtime_type", "")
            ).strip(),
            selection_reason=str(row.get("selection_reason", "") or ""),
            candidate_count=int(row.get("candidate_count", 0) or 0),
            attempt_number=int(row.get("attempt_number", 1) or 1),
            dispatched_at=str(row.get("dispatched_at", "") or ""),
            dispatch_status=str(
                row.get("dispatch_status", "") or DispatchStatus.SELECTED
            ),
            failure_code=row.get("failure_code"),
            failure_message=row.get("failure_message"),
            rejected_reasons=rr,
        )


__all__ = [
    "RuntimeCapabilities",
    "RuntimeLimits",
    "RuntimeHealth",
    "RuntimeHealthStatus",
    "RuntimeDescriptor",
    "TaskRuntimeRequirements",
    "RuntimeSelectionResult",
    "DispatchRecord",
    "DispatchStatus",
    "RUNTIME_HEALTHY_STATUSES",
]
