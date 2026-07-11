"""AEE-7.2 — operational surface (read-only) for artifacts and runs.

This package contains the *service layer* between the artifact
repository (a low-level SQLite-backed store) and any future
HTTP/RPC endpoint. The HTTP endpoint itself is AEE-7.3 work
(DAG engine surface) — the service layer is intentionally
HTTP-framework-free so it can be reused by any caller
(FastAPI, CLI, internal driver) without dragging in a web
stack.
"""
from .artifacts import (
    ArtifactPolicyEvent,
    ArtifactService,
    ArtifactSummary,
    policy_event_to_dto,
    summarize_artifact,
)

__all__ = [
    "ArtifactPolicyEvent",
    "ArtifactService",
    "ArtifactSummary",
    "policy_event_to_dto",
    "summarize_artifact",
]
