"""AEE Job — runtime-neutral job model.

AEE-1 keeps this minimal: a Job describes what we want done; the
adapter decides how to actually run it. Persistence (SQLite) still
uses the existing `dispatcher/models.py` `Task` for AEE-1; AEE-2
will introduce a `Job` row in the DB and a `Worker` claim protocol.

Attributes common to both AEE Job and dispatcher Task:
  * title, type, mode, priority, input
  * session_id, client_source, model_name
  * runtime_type, adapter_name (new in AEE-1)
  * external_run_id, worker_id, claim_token_hash (new in AEE-1)

We deliberately do NOT inherit from the dispatcher `Task` — Job is
the contract every adapter sees, independent of storage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Job:
    """Runtime-neutral job description.

    `runtime_type` and `adapter_name` select the concrete adapter at
    dispatch time. Defaults keep backward compat with the old
    `hermes_run_id` flow.
    """

    title: str
    type: str = "ops"
    mode: str = "normal"
    priority: int = 50
    input: str = ""
    session_id: Optional[str] = None
    client_source: Optional[str] = None
    model_name: Optional[str] = None
    # AEE-1 fields:
    runtime_type: str = "hermes"
    adapter_name: str = "hermes"
    # Set after submit():
    external_run_id: Optional[str] = None
    # Set on worker claim (AEE-2):
    worker_id: Optional[str] = None
    # Free-form payload for adapters that need more than the above
    # (e.g. Pi Agent step list, Claude Code repo path).
    spec: Dict[str, Any] = field(default_factory=dict)
    # Optional artifact paths the dispatcher should stat() at
    # completion (Phase 4 delivery).
    expected_artifacts: List[str] = field(default_factory=list)

    # -- Convenience -----------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "type": self.type,
            "mode": self.mode,
            "priority": self.priority,
            "input": self.input,
            "session_id": self.session_id,
            "client_source": self.client_source,
            "model_name": self.model_name,
            "runtime_type": self.runtime_type,
            "adapter_name": self.adapter_name,
            "external_run_id": self.external_run_id,
            "worker_id": self.worker_id,
            "spec": dict(self.spec),
            "expected_artifacts": list(self.expected_artifacts),
        }


@dataclass
class JobCreate:
    """Request body for `POST /jobs` (AEE-2). Mirrors Job minus the
    adapter-managed fields. Kept here for AEE-1 so the schema is
    stable before the new endpoint lands."""

    title: str
    type: str = "ops"
    mode: str = "normal"
    priority: int = 50
    input: str = ""
    session_id: Optional[str] = None
    client_source: Optional[str] = None
    model_name: Optional[str] = None
    runtime_type: str = "hermes"
    adapter_name: str = "hermes"
    expected_artifacts: List[str] = field(default_factory=list)
    spec: Dict[str, Any] = field(default_factory=dict)

    def to_job(self) -> Job:
        return Job(
            title=self.title,
            type=self.type,
            mode=self.mode,
            priority=self.priority,
            input=self.input,
            session_id=self.session_id,
            client_source=self.client_source,
            model_name=self.model_name,
            runtime_type=self.runtime_type,
            adapter_name=self.adapter_name,
            spec=dict(self.spec),
            expected_artifacts=list(self.expected_artifacts),
        )


__all__ = ["Job", "JobCreate"]
