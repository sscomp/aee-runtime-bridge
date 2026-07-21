"""Pydantic request/response models + envelope builder for
``POST /runs/executor``.

The schema matches work-order §2.4 exactly. The response carries both
the executor routing decision (``requested_executor`` /
``selected_executor`` / ``routing``) and the full evidence envelope
(``artifact_paths``, ``stdout_summary``, ``stderr_summary``,
``exit_code``, ``timeout_state``, ``cancel_state``, ``git_evidence``,
``artifact_verification``, ``telegram_result``).

Routing is truthful: when ``executor=claude-code-cli`` is requested the
``routing.effective_executor`` is ``claude-code-cli`` and ``was_forced``
is ``False`` — MiniMax-M3 is never reported on this endpoint (it never
calls the GPT→MiniMax routing layer).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


SUPPORTED_STATUS = {
    "queued", "running", "completed", "failed", "timeout", "cancelled",
}


class ExecutorRunRequest(BaseModel):
    executor: Optional[str] = Field(
        None,
        description=(
            "Executor to dispatch to. Accepted aliases: "
            "`claude-code-cli`, `claude_code`, `claude-code`. "
            "Canonical response value is `claude-code-cli`. "
            "`hermes` selects the legacy Hermes provider. "
            "If omitted, the configured default executor is used."
        ),
    )
    prompt: str = Field(
        ...,
        min_length=1,
        max_length=64_000,
        description="The worker prompt, forwarded verbatim (never rewritten).",
    )
    expected_artifacts: Optional[List[str]] = Field(
        None,
        description="Absolute paths the run is expected to produce; verified in the response.",
    )
    timeout_sec: Optional[int] = Field(
        None,
        ge=1,
        description="Run deadline in seconds. Falls back to the configured default.",
    )
    repo_path: Optional[str] = Field(
        None,
        description=(
            "Working directory for the executor / repo for read-only git evidence. "
            "Must lie inside the configured repo allow-list."
        ),
    )
    max_turns: Optional[int] = Field(
        None,
        ge=1,
        description="Override the configured Claude Code CLI --max-turns.",
    )

    @field_validator("expected_artifacts")
    @classmethod
    def _artifacts_abs(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        for p in v:
            if not isinstance(p, str) or not p.startswith("/"):
                raise ValueError(
                    f"expected_artifacts entries must be absolute paths: {p!r}"
                )
        return v


class ExecutorRunResponse(BaseModel):
    selected_executor: str
    requested_executor: Optional[str] = None
    run_id: str
    task_id: Optional[str] = None
    status: str
    progress: float = 0.0
    routing: Dict[str, Any]
    artifact_paths: List[str] = Field(default_factory=list)
    stdout_summary: str = ""
    stderr_summary: str = ""
    exit_code: Optional[int] = None
    timeout_state: Optional[str] = None
    cancel_state: Optional[str] = None
    git_evidence: Optional[Dict[str, Any]] = None
    artifact_verification: List[Dict[str, Any]] = Field(default_factory=list)
    telegram_result: Dict[str, Any] = Field(default_factory=dict)
    # Runtime identity metadata (work-order Part B). Factual only; fields
    # are None / "unknown" when their source is unavailable. Built by
    # ``aee.runtimes.runtime_identity.collect_runtime_identity``.
    runtime_identity: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


def build_routing(
    *,
    requested: Optional[str],
    selected: str,
    selection_source: str,
    effective_executor: str,
    effective_model: Optional[str] = None,
    was_forced: bool = False,
    reason: str = "explicit",
) -> Dict[str, Any]:
    """Build the truthful routing decision object."""
    return {
        "requested_executor": requested,
        "selected_executor": selected,
        "effective_executor": effective_executor,
        "selection_source": selection_source,
        "was_forced": was_forced,
        "reason": reason,
        "effective_model": effective_model,
    }


def build_executor_response(
    *,
    requested_executor: Optional[str],
    selected_executor: str,
    run_id: str,
    status: str,
    routing: Dict[str, Any],
    task_id: Optional[str] = None,
    progress: float = 0.0,
    artifact_paths: Optional[List[str]] = None,
    stdout_summary: str = "",
    stderr_summary: str = "",
    exit_code: Optional[int] = None,
    timeout_state: Optional[str] = None,
    cancel_state: Optional[str] = None,
    git_evidence: Optional[Dict[str, Any]] = None,
    artifact_verification: Optional[List[Dict[str, Any]]] = None,
    telegram_result: Optional[Dict[str, Any]] = None,
    runtime_identity: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble the full response envelope as a plain dict."""
    return {
        "selected_executor": selected_executor,
        "requested_executor": requested_executor,
        "run_id": run_id,
        "task_id": task_id,
        "status": status,
        "progress": progress,
        "routing": routing,
        "artifact_paths": list(artifact_paths or []),
        "stdout_summary": stdout_summary,
        "stderr_summary": stderr_summary,
        "exit_code": exit_code,
        "timeout_state": timeout_state,
        "cancel_state": cancel_state,
        "git_evidence": git_evidence,
        "artifact_verification": list(artifact_verification or []),
        "telegram_result": dict(telegram_result or {}),
        "runtime_identity": dict(runtime_identity) if runtime_identity else None,
        "error": error,
    }


__all__ = [
    "ExecutorRunRequest",
    "ExecutorRunResponse",
    "SUPPORTED_STATUS",
    "build_routing",
    "build_executor_response",
]