"""AEE-5 Runtime Registry — storage abstraction.

The repository is the only AEE-5 module that knows the
SQLite shape. The service in `registry.py` consumes a
`RuntimeRepository`; tests can substitute
`InMemoryRuntimeRepository` to keep the unit tests
DB-free.

Schema (added in `_AEE5_MIGRATIONS`):

    CREATE TABLE runtimes (
        runtime_id          TEXT PRIMARY KEY,
        runtime_type        TEXT NOT NULL,
        display_name        TEXT NOT NULL DEFAULT '',
        version             TEXT NOT NULL DEFAULT '1.0.0',
        enabled             INTEGER NOT NULL DEFAULT 1,
        endpoint            TEXT NOT NULL DEFAULT 'local',
        capabilities_json   TEXT NOT NULL DEFAULT '[]',
        labels_json         TEXT NOT NULL DEFAULT '{}',
        limits_json         TEXT NOT NULL DEFAULT '{}',
        health_json         TEXT NOT NULL DEFAULT '{}',
        registered_at       TEXT NOT NULL,
        updated_at          TEXT
    );

    CREATE TABLE dispatch_records (
        dispatch_id          TEXT PRIMARY KEY,
        task_id              TEXT NOT NULL,
        run_id               TEXT NOT NULL,
        selected_runtime_id  TEXT NOT NULL,
        selected_runtime_type TEXT NOT NULL,
        selection_reason     TEXT NOT NULL DEFAULT '',
        candidate_count      INTEGER NOT NULL DEFAULT 0,
        attempt_number       INTEGER NOT NULL DEFAULT 1,
        dispatched_at        TEXT NOT NULL,
        dispatch_status      TEXT NOT NULL DEFAULT 'selected',
        failure_code         TEXT,
        failure_message      TEXT,
        rejected_reasons_json TEXT NOT NULL DEFAULT '{}'
    );

    CREATE INDEX idx_dispatch_records_task ON dispatch_records(task_id, dispatched_at DESC);
    CREATE INDEX idx_dispatch_records_runtime ON dispatch_records(selected_runtime_id, dispatched_at DESC);
    CREATE INDEX idx_runtimes_type ON runtimes(runtime_type);
    CREATE INDEX idx_runtimes_enabled ON runtimes(enabled);

The migration is idempotent (same `pragma_table_info`
pattern as AEE-1 / AEE-3 / AEE-4).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from abc import ABC, abstractmethod
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .models import (
    DispatchRecord,
    RuntimeCapabilities,
    RuntimeDescriptor,
    RuntimeHealth,
    RuntimeLimits,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# AEE-5: `runtimes` and `dispatch_records` table migrations
# ---------------------------------------------------------------------------


_AEE5_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtimes (
  runtime_id          TEXT PRIMARY KEY,
  runtime_type        TEXT NOT NULL,
  display_name        TEXT NOT NULL DEFAULT '',
  version             TEXT NOT NULL DEFAULT '1.0.0',
  enabled             INTEGER NOT NULL DEFAULT 1,
  endpoint            TEXT NOT NULL DEFAULT 'local',
  capabilities_json   TEXT NOT NULL DEFAULT '[]',
  labels_json         TEXT NOT NULL DEFAULT '{}',
  limits_json         TEXT NOT NULL DEFAULT '{}',
  health_json         TEXT NOT NULL DEFAULT '{}',
  registered_at       TEXT NOT NULL,
  updated_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_runtimes_type ON runtimes(runtime_type);
CREATE INDEX IF NOT EXISTS idx_runtimes_enabled ON runtimes(enabled);

CREATE TABLE IF NOT EXISTS dispatch_records (
  dispatch_id          TEXT PRIMARY KEY,
  task_id              TEXT NOT NULL,
  run_id               TEXT NOT NULL,
  selected_runtime_id  TEXT NOT NULL,
  selected_runtime_type TEXT NOT NULL,
  selection_reason     TEXT NOT NULL DEFAULT '',
  candidate_count      INTEGER NOT NULL DEFAULT 0,
  attempt_number       INTEGER NOT NULL DEFAULT 1,
  dispatched_at        TEXT NOT NULL,
  dispatch_status      TEXT NOT NULL DEFAULT 'selected',
  failure_code         TEXT,
  failure_message      TEXT,
  rejected_reasons_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_dispatch_records_task
  ON dispatch_records(task_id, dispatched_at DESC);
CREATE INDEX IF NOT EXISTS idx_dispatch_records_runtime
  ON dispatch_records(selected_runtime_id, dispatched_at DESC);
"""


def ensure_aee5_schema(conn: sqlite3.Connection) -> None:
    """Create the AEE-5 tables + indexes if they don't exist.

    Idempotent: re-running on an already-migrated DB is a
    no-op. The migration is *additive* — no existing
    tables / columns are modified.
    """
    conn.executescript(_AEE5_SCHEMA)
    conn.commit()


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


def _row_to_descriptor(row: Dict[str, Any]) -> RuntimeDescriptor:
    """Build a RuntimeDescriptor from a SQLite row dict."""
    caps_raw = row.get("capabilities_json") or "[]"
    try:
        caps_list = json.loads(caps_raw)
    except (ValueError, TypeError):
        caps_list = []
    if not isinstance(caps_list, list):
        caps_list = []
    labels_raw = row.get("labels_json") or "{}"
    try:
        labels = json.loads(labels_raw)
    except (ValueError, TypeError):
        labels = {}
    if not isinstance(labels, dict):
        labels = {}
    labels = {str(k): str(v) for k, v in labels.items()}

    limits = RuntimeLimits.from_dict(
        _json_load_dict(row.get("limits_json"))
    )
    health = RuntimeHealth.from_dict(
        _json_load_dict(row.get("health_json"))
    )
    return RuntimeDescriptor(
        runtime_id=str(row.get("runtime_id", "")),
        runtime_type=str(row.get("runtime_type", "")),
        display_name=str(row.get("display_name", "") or ""),
        version=str(row.get("version", "1.0.0") or "1.0.0"),
        enabled=bool(row.get("enabled", 1)),
        endpoint=str(row.get("endpoint", "local") or "local"),
        capabilities=RuntimeCapabilities(caps_list),
        labels=labels,
        limits=limits,
        health=health,
        registered_at=str(row.get("registered_at", "") or ""),
        updated_at=row.get("updated_at"),
    )


def _json_load_dict(blob: Optional[str]) -> Optional[Dict[str, Any]]:
    if not blob:
        return None
    try:
        v = json.loads(blob)
    except (ValueError, TypeError):
        return None
    return v if isinstance(v, dict) else None


# ---------------------------------------------------------------------------
# Repository abstraction
# ---------------------------------------------------------------------------


class RuntimeRepository(ABC):
    """Storage abstraction over AEE-5 Runtime records.

    The service in `aee.runtimes.registry.RuntimeRegistry`
    consumes this interface. The default implementation
    is `SqliteRuntimeRepository`; tests can substitute
    `InMemoryRuntimeRepository`.
    """

    @abstractmethod
    def insert(self, descriptor: RuntimeDescriptor) -> RuntimeDescriptor: ...

    @abstractmethod
    def update(self, descriptor: RuntimeDescriptor) -> RuntimeDescriptor: ...

    @abstractmethod
    def delete(self, runtime_id: str) -> bool: ...

    @abstractmethod
    def get(self, runtime_id: str) -> Optional[RuntimeDescriptor]: ...

    @abstractmethod
    def list_all(
        self,
        *,
        enabled: Optional[bool] = None,
        runtime_type: Optional[str] = None,
    ) -> List[RuntimeDescriptor]: ...

    @abstractmethod
    def set_enabled(self, runtime_id: str, enabled: bool) -> bool: ...

    @abstractmethod
    def update_health(
        self,
        runtime_id: str,
        status: str,
        *,
        message: Optional[str] = None,
        last_checked_at: Optional[str] = None,
    ) -> bool: ...

    # --- Dispatch records ------------------------------------------------

    @abstractmethod
    def insert_dispatch_record(self, record: DispatchRecord) -> None: ...

    @abstractmethod
    def update_dispatch_status(
        self,
        dispatch_id: str,
        status: str,
        *,
        failure_code: Optional[str] = None,
        failure_message: Optional[str] = None,
    ) -> bool: ...

    @abstractmethod
    def list_dispatch_records(
        self,
        *,
        task_id: Optional[str] = None,
        runtime_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[DispatchRecord]: ...

    @abstractmethod
    def count_dispatch_records(
        self,
        *,
        task_id: Optional[str] = None,
        runtime_id: Optional[str] = None,
    ) -> int: ...


# ---------------------------------------------------------------------------
# In-memory repository (tests)
# ---------------------------------------------------------------------------


class InMemoryRuntimeRepository(RuntimeRepository):
    """In-memory implementation for unit tests.

    Not thread-safe (single-threaded test usage).
    """

    def __init__(self) -> None:
        self._runtimes: Dict[str, RuntimeDescriptor] = {}
        self._dispatches: Dict[str, DispatchRecord] = {}
        self._next_dispatch = 0
        self._lock = threading.Lock()

    def insert(self, descriptor: RuntimeDescriptor) -> RuntimeDescriptor:
        with self._lock:
            if descriptor.runtime_id in self._runtimes:
                raise KeyError(
                    f"runtime_id {descriptor.runtime_id!r} already exists"
                )
            self._runtimes[descriptor.runtime_id] = descriptor
            return descriptor

    def update(self, descriptor: RuntimeDescriptor) -> RuntimeDescriptor:
        with self._lock:
            if descriptor.runtime_id not in self._runtimes:
                raise KeyError(
                    f"runtime_id {descriptor.runtime_id!r} not found"
                )
            self._runtimes[descriptor.runtime_id] = descriptor
            return descriptor

    def delete(self, runtime_id: str) -> bool:
        with self._lock:
            return self._runtimes.pop(runtime_id, None) is not None

    def get(self, runtime_id: str) -> Optional[RuntimeDescriptor]:
        return self._runtimes.get(runtime_id)

    def list_all(
        self,
        *,
        enabled: Optional[bool] = None,
        runtime_type: Optional[str] = None,
    ) -> List[RuntimeDescriptor]:
        out: List[RuntimeDescriptor] = []
        for r in self._runtimes.values():
            if enabled is not None and r.enabled != enabled:
                continue
            if runtime_type and r.runtime_type != runtime_type:
                continue
            out.append(r)
        out.sort(key=lambda d: d.registered_at)
        return out

    def set_enabled(self, runtime_id: str, enabled: bool) -> bool:
        r = self._runtimes.get(runtime_id)
        if r is None:
            return False
        r.enabled = bool(enabled)
        r.updated_at = _now_iso()
        return True

    def update_health(
        self,
        runtime_id: str,
        status: str,
        *,
        message: Optional[str] = None,
        last_checked_at: Optional[str] = None,
    ) -> bool:
        r = self._runtimes.get(runtime_id)
        if r is None:
            return False
        r.health.status = status
        r.health.last_checked_at = last_checked_at or _now_iso()
        r.health.message = message
        r.updated_at = _now_iso()
        return True

    def insert_dispatch_record(self, record: DispatchRecord) -> None:
        with self._lock:
            self._dispatches[record.dispatch_id] = record

    def update_dispatch_status(
        self,
        dispatch_id: str,
        status: str,
        *,
        failure_code: Optional[str] = None,
        failure_message: Optional[str] = None,
    ) -> bool:
        rec = self._dispatches.get(dispatch_id)
        if rec is None:
            return False
        rec.dispatch_status = status
        if failure_code is not None:
            rec.failure_code = failure_code
        if failure_message is not None:
            rec.failure_message = failure_message
        return True

    def list_dispatch_records(
        self,
        *,
        task_id: Optional[str] = None,
        runtime_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[DispatchRecord]:
        out: List[DispatchRecord] = []
        for rec in self._dispatches.values():
            if task_id and rec.task_id != task_id:
                continue
            if runtime_id and rec.selected_runtime_id != runtime_id:
                continue
            out.append(rec)
        out.sort(key=lambda d: d.dispatched_at, reverse=True)
        return out[: max(0, int(limit))]

    def count_dispatch_records(
        self,
        *,
        task_id: Optional[str] = None,
        runtime_id: Optional[str] = None,
    ) -> int:
        return sum(
            1
            for rec in self._dispatches.values()
            if (not task_id or rec.task_id == task_id)
            and (not runtime_id or rec.selected_runtime_id == runtime_id)
        )


# ---------------------------------------------------------------------------
# SQLite repository
# ---------------------------------------------------------------------------


class SqliteRuntimeRepository(RuntimeRepository):
    """Default SQLite-backed implementation.

    Uses the same thread-local connection pattern as
    `dispatcher.db` (one connection per worker thread,
    shared with the rest of the bridge). Reads and
    writes are guarded by SQLite's WAL mode + a
    per-thread `sqlite3.Connection`.
    """

    def __init__(self, conn_factory=None) -> None:
        # `conn_factory` is a callable returning a
        # sqlite3.Connection. Defaults to
        # `dispatcher.db.get_conn`. Tests can pass
        # their own factory for isolation.
        self._conn_factory = conn_factory

    def _conn(self):
        if self._conn_factory is None:
            from dispatcher.db import get_conn
            return get_conn()
        return self._conn_factory()

    # ---- Runtime CRUD ---------------------------------------------------

    def insert(self, descriptor: RuntimeDescriptor) -> RuntimeDescriptor:
        conn = self._conn()
        with _txn(conn):
            existing = conn.execute(
                "SELECT 1 FROM runtimes WHERE runtime_id = ?",
                (descriptor.runtime_id,),
            ).fetchone()
            if existing is not None:
                raise KeyError(
                    f"runtime_id {descriptor.runtime_id!r} already exists"
                )
            self._write_row(descriptor, conn)
        return descriptor

    def update(self, descriptor: RuntimeDescriptor) -> RuntimeDescriptor:
        conn = self._conn()
        with _txn(conn):
            existing = conn.execute(
                "SELECT 1 FROM runtimes WHERE runtime_id = ?",
                (descriptor.runtime_id,),
            ).fetchone()
            if existing is None:
                raise KeyError(
                    f"runtime_id {descriptor.runtime_id!r} not found"
                )
            descriptor.updated_at = _now_iso()
            self._write_row(descriptor, conn, update=True)
        return descriptor

    def delete(self, runtime_id: str) -> bool:
        conn = self._conn()
        with _txn(conn):
            cur = conn.execute(
                "DELETE FROM runtimes WHERE runtime_id = ?",
                (runtime_id,),
            )
        return cur.rowcount > 0

    def get(self, runtime_id: str) -> Optional[RuntimeDescriptor]:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM runtimes WHERE runtime_id = ?",
            (runtime_id,),
        ).fetchone()
        return _row_to_descriptor(dict(row)) if row else None

    def list_all(
        self,
        *,
        enabled: Optional[bool] = None,
        runtime_type: Optional[str] = None,
    ) -> List[RuntimeDescriptor]:
        conn = self._conn()
        sql = "SELECT * FROM runtimes"
        clauses: List[str] = []
        params: List[Any] = []
        if enabled is not None:
            clauses.append("enabled = ?")
            params.append(1 if enabled else 0)
        if runtime_type:
            clauses.append("runtime_type = ?")
            params.append(runtime_type)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY registered_at ASC"
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_descriptor(dict(r)) for r in rows]

    def set_enabled(self, runtime_id: str, enabled: bool) -> bool:
        conn = self._conn()
        with _txn(conn):
            cur = conn.execute(
                "UPDATE runtimes SET enabled = ?, updated_at = ? "
                "WHERE runtime_id = ?",
                (1 if enabled else 0, _now_iso(), runtime_id),
            )
        return cur.rowcount > 0

    def update_health(
        self,
        runtime_id: str,
        status: str,
        *,
        message: Optional[str] = None,
        last_checked_at: Optional[str] = None,
    ) -> bool:
        conn = self._conn()
        row = conn.execute(
            "SELECT health_json FROM runtimes WHERE runtime_id = ?",
            (runtime_id,),
        ).fetchone()
        if row is None:
            return False
        try:
            current = json.loads(row["health_json"] or "{}")
        except (ValueError, TypeError):
            current = {}
        if not isinstance(current, dict):
            current = {}
        current["status"] = status
        current["last_checked_at"] = last_checked_at or _now_iso()
        if message is not None:
            current["message"] = message
        with _txn(conn):
            conn.execute(
                "UPDATE runtimes SET health_json = ?, updated_at = ? "
                "WHERE runtime_id = ?",
                (json.dumps(current), _now_iso(), runtime_id),
            )
        return True

    # ---- Dispatch records -----------------------------------------------

    def insert_dispatch_record(self, record: DispatchRecord) -> None:
        conn = self._conn()
        with _txn(conn):
            conn.execute(
                """
                INSERT INTO dispatch_records (
                  dispatch_id, task_id, run_id, selected_runtime_id,
                  selected_runtime_type, selection_reason, candidate_count,
                  attempt_number, dispatched_at, dispatch_status,
                  failure_code, failure_message, rejected_reasons_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.dispatch_id,
                    record.task_id,
                    record.run_id,
                    record.selected_runtime_id,
                    record.selected_runtime_type,
                    record.selection_reason,
                    int(record.candidate_count),
                    int(record.attempt_number),
                    record.dispatched_at,
                    record.dispatch_status,
                    record.failure_code,
                    record.failure_message,
                    json.dumps(record.rejected_reasons or {}),
                ),
            )

    def update_dispatch_status(
        self,
        dispatch_id: str,
        status: str,
        *,
        failure_code: Optional[str] = None,
        failure_message: Optional[str] = None,
    ) -> bool:
        conn = self._conn()
        with _txn(conn):
            cur = conn.execute(
                "UPDATE dispatch_records SET dispatch_status = ?, "
                "failure_code = COALESCE(?, failure_code), "
                "failure_message = COALESCE(?, failure_message) "
                "WHERE dispatch_id = ?",
                (status, failure_code, failure_message, dispatch_id),
            )
        return cur.rowcount > 0

    def list_dispatch_records(
        self,
        *,
        task_id: Optional[str] = None,
        runtime_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[DispatchRecord]:
        conn = self._conn()
        sql = "SELECT * FROM dispatch_records"
        clauses: List[str] = []
        params: List[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if runtime_id:
            clauses.append("selected_runtime_id = ?")
            params.append(runtime_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY dispatched_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        rows = conn.execute(sql, params).fetchall()
        return [DispatchRecord.from_row(dict(r)) for r in rows]

    def count_dispatch_records(
        self,
        *,
        task_id: Optional[str] = None,
        runtime_id: Optional[str] = None,
    ) -> int:
        conn = self._conn()
        sql = "SELECT COUNT(*) AS c FROM dispatch_records"
        clauses: List[str] = []
        params: List[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if runtime_id:
            clauses.append("selected_runtime_id = ?")
            params.append(runtime_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        row = conn.execute(sql, params).fetchone()
        return int(row["c"]) if row else 0

    # ---- internal ------------------------------------------------------

    def _write_row(
        self,
        descriptor: RuntimeDescriptor,
        conn: sqlite3.Connection,
        *,
        update: bool = False,
    ) -> None:
        caps_json = json.dumps(descriptor.capabilities.normalized())
        labels_json = json.dumps({k: str(v) for k, v in descriptor.labels.items()})
        limits_json = json.dumps(descriptor.limits.to_dict())
        health_json = json.dumps(descriptor.health.to_dict())
        if update:
            conn.execute(
                """
                UPDATE runtimes SET
                  runtime_type = ?,
                  display_name = ?,
                  version = ?,
                  enabled = ?,
                  endpoint = ?,
                  capabilities_json = ?,
                  labels_json = ?,
                  limits_json = ?,
                  health_json = ?,
                  updated_at = ?
                WHERE runtime_id = ?
                """,
                (
                    descriptor.runtime_type,
                    descriptor.display_name,
                    descriptor.version,
                    1 if descriptor.enabled else 0,
                    descriptor.endpoint,
                    caps_json,
                    labels_json,
                    limits_json,
                    health_json,
                    descriptor.updated_at or _now_iso(),
                    descriptor.runtime_id,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO runtimes (
                  runtime_id, runtime_type, display_name, version,
                  enabled, endpoint, capabilities_json, labels_json,
                  limits_json, health_json, registered_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    descriptor.runtime_id,
                    descriptor.runtime_type,
                    descriptor.display_name,
                    descriptor.version,
                    1 if descriptor.enabled else 0,
                    descriptor.endpoint,
                    caps_json,
                    labels_json,
                    limits_json,
                    health_json,
                    descriptor.registered_at,
                    descriptor.updated_at,
                ),
            )


class _TxCtx:
    """Tiny `with` wrapper around `BEGIN`/`COMMIT`.

    SQLite's `isolation_level=None` is set on the
    dispatcher's connection so we need to do
    transactions manually. This mirrors the pattern in
    `dispatcher.db.transaction()` but doesn't depend
    on it (so the repository can be used in tests
    without the dispatcher package).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self._conn.execute("BEGIN")
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self._conn.execute("COMMIT")
        else:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass


def _txn(conn: sqlite3.Connection) -> _TxCtx:
    return _TxCtx(conn)


__all__ = [
    "InMemoryRuntimeRepository",
    "RuntimeRepository",
    "SqliteRuntimeRepository",
    "ensure_aee5_schema",
]
