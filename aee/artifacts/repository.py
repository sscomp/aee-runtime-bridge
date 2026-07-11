"""AEE-6 Artifact storage.

Two implementations share the same Protocol:

* `InMemoryArtifactRepository` — for tests + the AEE Lightweight
  Runtime's no-DB path.
* `SqliteArtifactRepository` — production. Uses the existing
  `dispatcher.db` connection so we share one SQLite file
  (the canonical `runtime_data/dispatcher.db`).

Schema
------
One table, additive to the AEE-5 schema:

    CREATE TABLE IF NOT EXISTS artifacts (
      artifact_id          TEXT PRIMARY KEY,
      task_id              TEXT NOT NULL,
      path                 TEXT NOT NULL,
      kind                 TEXT NOT NULL,
      sha256               TEXT,
      size                 INTEGER,
      mtime                TEXT,
      exists               INTEGER NOT NULL DEFAULT 0,
      content_type         TEXT NOT NULL DEFAULT '',
      classification_source TEXT NOT NULL DEFAULT '',
      collected_at         TEXT NOT NULL,
      version              INTEGER NOT NULL DEFAULT 1,
      UNIQUE(task_id, path, version)
    );

`UNIQUE(task_id, path, version)` makes re-collect idempotent: a
second call to `collect()` for the same task/path bumps the
version and inserts a new row, while the old row stays for
audit. `version=1` is the first observation.
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Protocol, runtime_checkable

from .errors import ArtifactError, ArtifactNotFoundError
from .models import Artifact


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ArtifactRepository(Protocol):
    """The contract every Artifact store must implement.

    The bridge dispatcher depends on this Protocol only; tests use
    the in-memory implementation, production uses SQLite. Both
    must satisfy `runtime_checkable` so the dispatcher's call site
    can `isinstance(x, ArtifactRepository)` defensively.
    """

    def save(self, artifact: Artifact) -> Artifact: ...

    def get(self, artifact_id: str) -> Artifact: ...

    def find(
        self,
        task_id: str,
        *,
        path: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> List[Artifact]: ...

    def latest(self, task_id: str, path: str) -> Optional[Artifact]: ...

    def record_policy_event(self, event: dict) -> None: ...

    def list_policy_events(
        self,
        task_id: Optional[str] = None,
        *,
        code: Optional[str] = None,
        accepted: Optional[bool] = None,
        limit: int = 1000,
    ) -> List[dict]: ...

    def update_policy_event_artifact_id(
        self, *, decision_id: str, artifact_id: str
    ) -> None: ...


# AEE-6.3 — artifact policy event payload shape (informational;
# not enforced at runtime, but documented for callers).
POLICY_EVENT_REQUIRED_KEYS = frozenset(
    {"decision_id", "code", "accepted", "realpath", "original",
     "detail", "source", "artifact_id"}
)


# ---------------------------------------------------------------------------
# In-memory implementation (tests + AEE Lightweight)
# ---------------------------------------------------------------------------


@dataclass
class InMemoryArtifactRepository:
    """A pure-Python ArtifactRepository. No SQLite dependency.

    Stores artifacts in a list and computes a stable `artifact_id`
    from the uuid4 generated at first save. Re-saving the same
    `(task_id, path)` appends a new record (version+1 semantics)
    so callers can ask for `latest(task_id, path)`.
    """

    _items: List[Artifact] = None  # type: ignore[assignment]
    _by_id: Dict[str, Artifact] = None  # type: ignore[assignment]
    _version: Dict[tuple, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._items is None:
            self._items = []
        if self._by_id is None:
            self._by_id = {}
        if self._version is None:
            self._version = {}

    def save(self, artifact: Artifact) -> Artifact:
        key = (artifact.task_id, artifact.path)
        next_v = self._version.get(key, 0) + 1
        self._version[key] = next_v
        new_id = artifact.artifact_id or f"art-{uuid.uuid4().hex[:12]}"
        # Re-create the dataclass with the new id + version baked
        # in. Frozen=True means we can't mutate; this is the
        # idiomatic way to "update" a frozen dataclass.
        saved = Artifact(
            path=artifact.path,
            task_id=artifact.task_id,
            kind=artifact.kind,
            sha256=artifact.sha256,
            size=artifact.size,
            mtime=artifact.mtime,
            exists=artifact.exists,
            content_type=artifact.content_type,
            classification_source=artifact.classification_source,
            artifact_id=new_id,
            collected_at=artifact.collected_at,
        )
        # Carry the version in a side-channel so the SQLite impl
        # can mirror it. In-memory we attach via a private attr.
        saved.__dict__["_version"] = next_v  # type: ignore[attr-defined]
        self._items.append(saved)
        self._by_id[new_id] = saved
        return saved

    def get(self, artifact_id: str) -> Artifact:
        rec = self._by_id.get(artifact_id)
        if rec is None:
            raise ArtifactNotFoundError(
                f"artifact_id not found: {artifact_id!r}"
            )
        return rec

    def find(
        self,
        task_id: str,
        *,
        path: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> List[Artifact]:
        out = [a for a in self._items if a.task_id == task_id]
        if path is not None:
            out = [a for a in out if a.path == path]
        if kind is not None:
            out = [a for a in out if a.kind == kind]
        return out

    def latest(self, task_id: str, path: str) -> Optional[Artifact]:
        matches = [a for a in self._items
                   if a.task_id == task_id and a.path == path]
        if not matches:
            return None
        # Highest version wins. The version is stashed in __dict__.
        return max(matches, key=lambda a: a.__dict__.get("_version", 0))

    # Test-friendly extras
    def __len__(self) -> int:
        return len(self._items)

    def all(self) -> Iterable[Artifact]:
        return list(self._items)

    def record_policy_event(self, event: dict) -> None:
        """AEE-6.3 — append a policy decision to the in-memory audit log.

        Tests that use the in-memory repo can inspect ``self._policy_events``
        to assert that the policy was actually consulted.
        """
        if not hasattr(self, "_policy_events"):
            self._policy_events = []  # type: ignore[attr-defined]
        self._policy_events.append(dict(event))  # type: ignore[attr-defined]

    def list_policy_events(
        self,
        task_id: Optional[str] = None,
        *,
        code: Optional[str] = None,
        accepted: Optional[bool] = None,
        limit: int = 1000,
    ) -> List[dict]:
        """AEE-7.2 — read back policy events for the in-memory audit log.

        Mirrors the SQLite impl signature: ``task_id`` filters the
        primary subject; ``code`` / ``accepted`` are optional
        equality filters; ``limit`` caps the result count.
        """
        events = getattr(self, "_policy_events", [])
        out: List[dict] = []
        for ev in events:
            if task_id is not None and ev.get("task_id") != task_id:
                continue
            if code is not None and ev.get("code") != code:
                continue
            if accepted is not None and bool(ev.get("accepted")) is not accepted:
                continue
            out.append(dict(ev))
        # Newest first by recorded_at (fall back to insertion order).
        out.sort(key=lambda e: e.get("recorded_at", ""), reverse=True)
        return out[: int(limit)]

    def update_policy_event_artifact_id(
        self, *, decision_id: str, artifact_id: str
    ) -> None:
        """AEE-7.1 — backfill ``artifact_id`` on the audit row.

        ``ArtifactPipeline.collect()`` writes the policy decision
        audit row *before* it has saved the placeholder Artifact
        (the rejection flow). The pipeline then needs to update
        the row's ``artifact_id`` so the audit ↔ artifact join
        is intact. The default Protocol in this module does not
        require this hook; callers that don't expose it get a
        silent no-op via the ``hasattr`` guard in ``collect.py``.
        """
        for event in getattr(self, "_policy_events", []):
            if event.get("decision_id") == decision_id:
                event["artifact_id"] = artifact_id
                return

    @property
    def policy_events(self) -> List[dict]:
        if not hasattr(self, "_policy_events"):
            return []
        return list(self._policy_events)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# SQLite implementation
# ---------------------------------------------------------------------------


# AEE-6 schema. Idempotent (CREATE TABLE IF NOT EXISTS). The
# `_init_schema` step in dispatcher.db.py will append a call to
# `ensure_aee6_schema(conn)` in a follow-up; this slice does NOT
# touch dispatcher.db to keep the AEE-5 baseline intact.
_AEE6_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id           TEXT PRIMARY KEY,
  task_id               TEXT NOT NULL,
  path                  TEXT NOT NULL,
  kind                  TEXT NOT NULL,
  sha256                TEXT,
  size                  INTEGER,
  mtime                 TEXT,
  file_exists           INTEGER NOT NULL DEFAULT 0,
  content_type          TEXT NOT NULL DEFAULT '',
  classification_source TEXT NOT NULL DEFAULT '',
  collected_at          TEXT NOT NULL,
  version               INTEGER NOT NULL DEFAULT 1,
  UNIQUE(task_id, path, version)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_task
  ON artifacts(task_id, collected_at DESC);

CREATE INDEX IF NOT EXISTS idx_artifacts_task_path
  ON artifacts(task_id, path);

-- AEE-6.3: artifact policy decision audit log. One row per
-- `policy.check(path)` call. Used by ops to investigate "why
-- was this file skipped" reports. Pure additive table; no
-- dependency on artifacts above. (task_id nullable so the
-- pre-collect validation pass can write events too.)
CREATE TABLE IF NOT EXISTS artifact_policy_events (
  decision_id   TEXT PRIMARY KEY,
  task_id       TEXT,
  code          TEXT NOT NULL,
  accepted      INTEGER NOT NULL,
  realpath      TEXT NOT NULL,
  original      TEXT NOT NULL,
  detail        TEXT NOT NULL,
  source        TEXT NOT NULL,
  artifact_id   TEXT,
  recorded_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ape_task
  ON artifact_policy_events(task_id, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_ape_code
  ON artifact_policy_events(code);
"""


def ensure_aee6_schema(conn: sqlite3.Connection) -> None:
    """Idempotent migration; safe to call on every DB open.

    Follows the same pattern as `ensure_aee5_schema` in
    `dispatcher/db.py:101` (the `_PHASE4_MIGRATIONS` list) —
    additive, `IF NOT EXISTS`, no destructive ALTER.
    """
    conn.executescript(_AEE6_SCHEMA)
    conn.commit()


@dataclass
class SqliteArtifactRepository:
    """SQLite-backed ArtifactRepository.

    Re-uses the existing dispatcher.db connection so the bridge
    only opens one SQLite file. The connection is expected to
    already have the AEE-6 schema applied (call
    `ensure_aee6_schema(conn)` first if not).
    """

    conn: sqlite3.Connection

    def save(self, artifact: Artifact) -> Artifact:
        # Bump version atomically: SELECT max(version) ... then INSERT.
        # We rely on the UNIQUE(task_id, path, version) constraint
        # to surface a race; the caller can retry.
        cur = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM artifacts "
            "WHERE task_id = ? AND path = ?",
            (artifact.task_id, artifact.path),
        )
        next_version = int(cur.fetchone()[0]) + 1
        new_id = artifact.artifact_id or f"art-{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            """
            INSERT OR REPLACE INTO artifacts (
                artifact_id, task_id, path, kind, sha256, size, mtime,
                file_exists, content_type, classification_source,
                collected_at, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id,
                artifact.task_id,
                artifact.path,
                artifact.kind,
                artifact.sha256,
                artifact.size,
                artifact.mtime,
                1 if artifact.exists else 0,
                artifact.content_type,
                artifact.classification_source,
                artifact.collected_at,
                next_version,
            ),
        )
        self.conn.commit()
        return Artifact(
            path=artifact.path,
            task_id=artifact.task_id,
            kind=artifact.kind,
            sha256=artifact.sha256,
            size=artifact.size,
            mtime=artifact.mtime,
            exists=artifact.exists,
            content_type=artifact.content_type,
            classification_source=artifact.classification_source,
            artifact_id=new_id,
            collected_at=artifact.collected_at,
        )

    def get(self, artifact_id: str) -> Artifact:
        cur = self.conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?",
            (artifact_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ArtifactNotFoundError(
                f"artifact_id not found: {artifact_id!r}"
            )
        return _row_to_artifact(row)

    def find(
        self,
        task_id: str,
        *,
        path: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> List[Artifact]:
        sql = "SELECT * FROM artifacts WHERE task_id = ?"
        args: List[Any] = [task_id]
        if path is not None:
            sql += " AND path = ?"
            args.append(path)
        if kind is not None:
            sql += " AND kind = ?"
            args.append(kind)
        sql += " ORDER BY collected_at DESC"
        cur = self.conn.execute(sql, args)
        return [_row_to_artifact(r) for r in cur.fetchall()]

    def latest(self, task_id: str, path: str) -> Optional[Artifact]:
        cur = self.conn.execute(
            "SELECT * FROM artifacts WHERE task_id = ? AND path = ? "
            "ORDER BY version DESC LIMIT 1",
            (task_id, path),
        )
        row = cur.fetchone()
        return _row_to_artifact(row) if row is not None else None

    def record_policy_event(self, event: dict) -> None:
        """AEE-6.3 — persist a policy decision to the audit log.

        The dispatcher calls this once per `policy.check()` invocation
        (accept OR reject), so the audit trail is symmetric. A
        `task_id` may be ``None`` for the pre-collect sweep that
        validates the orchestrator's expected_artifacts before any
        artifact is saved.
        """
        from datetime import datetime, timezone

        recorded_at = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self.conn.execute(
            """
            INSERT OR REPLACE INTO artifact_policy_events (
                decision_id, task_id, code, accepted, realpath,
                original, detail, source, artifact_id, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.get("decision_id", ""),
                event.get("task_id"),
                event.get("code", ""),
                1 if event.get("accepted") else 0,
                event.get("realpath", ""),
                event.get("original", ""),
                event.get("detail", ""),
                event.get("source", ""),
                event.get("artifact_id"),
                recorded_at,
            ),
        )
        self.conn.commit()

    def update_policy_event_artifact_id(
        self, *, decision_id: str, artifact_id: str
    ) -> None:
        """AEE-7.1 — backfill ``artifact_id`` on a previously recorded row.

        Called by ``ArtifactPipeline.collect()`` after the
        placeholder Artifact for a *rejected* path has been
        saved. The audit row was written with ``artifact_id=NULL``
        because the placeholder didn't exist yet. The dispatcher
        relies on ``artifact_policy_events.artifact_id`` to join
        the audit log with the artifacts table, so the link has
        to be present even on the rejection path.
        """
        self.conn.execute(
            "UPDATE artifact_policy_events SET artifact_id = ? "
            "WHERE decision_id = ?",
            (artifact_id, decision_id),
        )
        self.conn.commit()

    def list_policy_events(
        self,
        task_id: Optional[str] = None,
        *,
        code: Optional[str] = None,
        accepted: Optional[bool] = None,
        limit: int = 1000,
    ) -> List[dict]:
        """Read back policy events. Useful for ops debugging."""
        sql = "SELECT * FROM artifact_policy_events WHERE 1=1"
        args: List[Any] = []
        if task_id is not None:
            sql += " AND task_id = ?"
            args.append(task_id)
        if code is not None:
            sql += " AND code = ?"
            args.append(code)
        if accepted is not None:
            sql += " AND accepted = ?"
            args.append(1 if accepted else 0)
        sql += " ORDER BY recorded_at DESC LIMIT ?"
        args.append(int(limit))
        cur = self.conn.execute(sql, args)
        return [dict(r) for r in cur.fetchall()]


def _row_to_artifact(row: sqlite3.Row) -> Artifact:
    return Artifact(
        path=row["path"],
        task_id=row["task_id"],
        kind=row["kind"],
        sha256=row["sha256"],
        size=row["size"],
        mtime=row["mtime"],
        exists=bool(row["file_exists"]),
        content_type=row["content_type"],
        classification_source=row["classification_source"],
        artifact_id=row["artifact_id"],
        collected_at=row["collected_at"],
    )


__all__ = [
    "ArtifactRepository",
    "InMemoryArtifactRepository",
    "SqliteArtifactRepository",
    "ensure_aee6_schema",
    "POLICY_EVENT_REQUIRED_KEYS",
]
