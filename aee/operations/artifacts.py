"""AEE-7.2 — minimal read-only operational surface for artifacts.

This module is the *service* layer between the artifact
repository (a low-level SQLite-backed store) and any future
HTTP/RPC endpoint. AEE-7.2 ships the service layer + contract
tests but does **not** add the HTTP endpoint — the endpoint
belongs in AEE-7.3 (DAG engine surface) where the request
auth, rate-limiting, and pagination strategy can be designed
together with the rest of the operational surface.

Why a service layer
-------------------
The repository exposes ``find()`` / ``get()`` / ``latest()`` on
artifacts and ``list_policy_events()`` on policy events. The
service layer turns those into a single, narrow read API
(:class:`ArtifactService`) that:

* never returns the full file content (only metadata);
* never accepts write/delete parameters;
* is safe to expose to a future HTTP endpoint without further
  filtering;
* does not depend on FastAPI / Starlette / any HTTP framework.

Shape
-----
* :class:`ArtifactSummary` — what :meth:`ArtifactService.list_by_task`
  returns. Path, exists, size, mtime, sha256, kind, version,
  artifact_id, plus the ``collected_at`` timestamp. The
  ``task_id`` is included so the caller can correlate with
  dispatch_records / policy_events.
* :class:`ArtifactPolicyEvent` — what
  :meth:`ArtifactService.list_policy_events` returns. The
  decision code, accepted flag, realpath, original path,
  detail message, source, ``task_id``, and timestamp. We
  surface ``code`` / ``accepted`` / ``detail`` so the caller
  can render a "why was this rejected?" explanation.
* :func:`summarize_artifact` — single-Artifact → ArtifactSummary.
  Used by the service and reused by tests; pure function.

Security model
--------------
The service is read-only by construction: there is no
``save()`` / ``delete()`` / ``record_event()`` method. The
*caller* (a future HTTP endpoint) is responsible for auth;
the service itself does not check API keys because it is
designed to be embedded in the same process as the
repository (i.e. a trusted boundary). AEE-7.3 will wrap this
in a FastAPI router that *does* check auth.

The service does **not** read file contents; ``exists`` and
``size`` are the only disk-derived fields and they come from
the repository (which already has them cached at
``collect()`` time). The service cannot leak a secret that
the repository did not already record.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Sequence

from ..artifacts.errors import ArtifactNotFoundError
from ..artifacts.models import Artifact
from ..artifacts.repository import ArtifactRepository

# Local type alias for the in-memory / sqlite policy-event row shape
# (a dict with the keys documented in
# `aee.artifacts.repository.POLICY_EVENT_REQUIRED_KEYS`).
PolicyEventLike = dict

log = logging.getLogger("aee.operations.artifacts")


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactSummary:
    """Read-only metadata view of a single ``Artifact``.

    Never includes the file content. All fields are taken
    directly from the repository row; if a field is ``None``
    (e.g. ``size`` when the file does not exist), it is
    surfaced as ``None`` rather than coerced to 0.
    """

    artifact_id: str
    task_id: str
    path: str
    kind: str
    version: int
    exists: bool
    size: Optional[int]
    mtime: Optional[str]
    sha256: Optional[str]
    collected_at: str


@dataclass(frozen=True)
class ArtifactPolicyEvent:
    """Read-only metadata view of a single policy decision.

    Mirrors the audit event shape. The ``code`` is the
    policy-decision code (``OK`` / ``OUTSIDE_ROOTS`` /
    ``SYMLINK_ESCAPE`` / ``BROKEN_SYMLINK`` etc.) — see
    :mod:`aee.artifacts.policy` for the full list.
    """

    decision_id: str
    task_id: str
    code: str
    accepted: bool
    realpath: str
    original: str
    detail: str
    source: str
    artifact_id: Optional[str]
    recorded_at: str


# ---------------------------------------------------------------------------
# Pure helpers (also reused by tests)
# ---------------------------------------------------------------------------


def summarize_artifact(art: Artifact, task_id: str) -> ArtifactSummary:
    """Turn a repository ``Artifact`` into the read-only DTO.

    The ``task_id`` argument is explicit because the
    repository's ``Artifact`` does not always carry it
    (the in-memory impl does; the SQLite impl round-trips
    through the row, which does).

    The ``version`` field comes from a side-channel attribute
    on the in-memory repo (``art.__dict__["_version"]``). The
    SQLite impl exposes it via the row's ``version`` column;
    if neither is present we fall back to 1 (the schema's
    default for the first observation).
    """
    version = art.__dict__.get("_version", 0)
    if not version:
        # The SQLite row carries a top-level ``version`` column
        # that doesn't round-trip through the frozen dataclass.
        # We don't have direct access to the row here, so the
        # caller (list_by_task / latest) is expected to pass an
        # artifact that was just loaded and therefore has either
        # the side-channel or a synthesized version. If we see
        # 0/None, treat as 1 (schema default).
        version = 1
    artifact_id = art.artifact_id or ""
    return ArtifactSummary(
        artifact_id=artifact_id,
        task_id=task_id,
        path=art.path,
        kind=art.kind,
        version=int(version),
        exists=bool(art.exists),
        size=art.size,
        mtime=art.mtime,
        sha256=art.sha256,
        collected_at=art.collected_at,
    )


def policy_event_to_dto(
    event: PolicyEventLike, task_id: str
) -> ArtifactPolicyEvent:
    """Turn a raw policy event row into the read-only DTO.

    The event shape is documented in :data:`repository.POLICY_EVENT_REQUIRED_KEYS`.
    """
    return ArtifactPolicyEvent(
        decision_id=str(event.get("decision_id", "")),
        task_id=task_id,
        code=str(event.get("code", "")),
        accepted=bool(event.get("accepted", False)),
        realpath=str(event.get("realpath", "")),
        original=str(event.get("original", "")),
        detail=str(event.get("detail", "")),
        source=str(event.get("source", "")),
        artifact_id=(
            str(event["artifact_id"])
            if event.get("artifact_id") is not None
            else None
        ),
        recorded_at=str(event.get("ts", event.get("recorded_at", ""))),
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ArtifactService:
    """Read-only operational surface for artifacts.

    Construct one per process. The constructor takes the
    artifact repository as a dependency; tests pass an
    ``InMemoryArtifactRepository`` and the bridge passes the
    live ``SqliteArtifactRepository`` (via the dispatcher
    connection).

    The service is intentionally small: list/get by task and
    by artifact_id, plus policy-event lookup. There is no
    write or delete method.
    """

    def __init__(self, repo: ArtifactRepository) -> None:
        self._repo = repo

    # ---- artifacts --------------------------------------------------------

    def list_by_task(
        self,
        task_id: str,
        *,
        path: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 100,
    ) -> List[ArtifactSummary]:
        """List artifact metadata for a task, newest first.

        ``path`` and ``kind`` are optional filters. ``limit``
        defaults to 100 to keep the response bounded; the
        future HTTP endpoint will paginate beyond that.
        """
        if not task_id:
            raise ValueError("task_id is required")
        if limit <= 0 or limit > 1000:
            raise ValueError(
                f"limit must be in (0, 1000], got {limit}"
            )
        items: Sequence[Artifact] = self._repo.find(
            task_id=task_id, path=path, kind=kind
        )
        # Newest first: sort by collected_at descending,
        # breaking ties on artifact_id for determinism.
        items = sorted(
            items,
            key=lambda a: (a.collected_at, a.artifact_id),
            reverse=True,
        )
        items = items[:limit]
        result = [summarize_artifact(a, task_id) for a in items]
        log.debug(
            "ArtifactService.list_by_task: task_id=%s count=%d path=%s kind=%s",
            task_id,
            len(result),
            path,
            kind,
        )
        return result

    def get(self, artifact_id: str) -> Optional[ArtifactSummary]:
        """Fetch a single artifact by ``artifact_id``.

        Returns ``None`` if the artifact does not exist. The
        ``task_id`` is taken from the artifact itself when
        available (SQLite impl stores it on the row); for
        repos that don't carry it, we fall back to an empty
        string and log a debug message.
        """
        if not artifact_id:
            raise ValueError("artifact_id is required")
        try:
            art = self._repo.get(artifact_id)
        except ArtifactNotFoundError:
            log.debug("ArtifactService.get: artifact_id=%s not found", artifact_id)
            return None
        if art is None:
            return None
        # The Artifact dataclass does not always expose
        # task_id (it depends on the implementation). The
        # SQLite impl does; the in-memory impl does not.
        task_id = getattr(art, "task_id", "") or ""
        if not task_id:
            log.debug(
                "ArtifactService.get: artifact_id=%s has no task_id "
                "(in-memory impl); returning empty string",
                artifact_id,
            )
        return summarize_artifact(art, task_id)

    def latest(self, task_id: str, path: str) -> Optional[ArtifactSummary]:
        """Fetch the latest version of an artifact by (task_id, path)."""
        if not task_id:
            raise ValueError("task_id is required")
        if not path:
            raise ValueError("path is required")
        art = self._repo.latest(task_id=task_id, path=path)
        if art is None:
            return None
        return summarize_artifact(art, task_id)

    # ---- policy events ---------------------------------------------------

    def list_policy_events(
        self,
        task_id: str,
        *,
        accepted: Optional[bool] = None,
        limit: int = 100,
    ) -> List[ArtifactPolicyEvent]:
        """List policy decisions for a task, newest first.

        ``accepted`` filters to ``True`` / ``False`` only
        (both inclusive when ``None``).
        """
        if not task_id:
            raise ValueError("task_id is required")
        if limit <= 0 or limit > 1000:
            raise ValueError(
                f"limit must be in (0, 1000], got {limit}"
            )
        events: Sequence[PolicyEventLike] = self._repo.list_policy_events(
            task_id=task_id, limit=limit
        )
        if accepted is not None:
            events = [e for e in events if bool(e.get("accepted")) is accepted]
        result = [policy_event_to_dto(e, task_id) for e in events]
        log.debug(
            "ArtifactService.list_policy_events: task_id=%s count=%d accepted=%s",
            task_id,
            len(result),
            accepted,
        )
        return result


__all__ = [
    "ArtifactSummary",
    "ArtifactPolicyEvent",
    "summarize_artifact",
    "policy_event_to_dto",
    "ArtifactService",
]
