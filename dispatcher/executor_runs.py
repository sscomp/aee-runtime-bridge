"""Durable run-tracking store for ``POST /runs/executor`` dispatches.

The executor dispatch endpoint (``/runs/executor``) used to be
fire-and-forget: the response carried the full evidence envelope,
but nothing was persisted, so a subsequent ``GET /runs/{run_id}``
could not find the run unless a dispatcher task row existed. For
the Claude Code CLI executor (synchronous) there is *never* a
dispatcher task row, so clients lost the run the moment the POST
response returned.

This module adds minimal durable persistence so any
``POST /runs/executor`` run can be polled later via
``GET /runs/{run_id}`` without launching a new executor, scanning
the repo, or guessing state.

Design
------
* Single new SQLite table ``executor_runs`` in the dispatcher DB
  (``data/dispatcher.db``). Uses the same idempotent
  ``CREATE TABLE IF NOT EXISTS`` + ``pragma_table_info`` pattern
  as AEE-5 / AEE-6: re-running on an already-migrated DB is a
  no-op, and existing tables / columns are untouched.
* The schema is a flat denormalised row mirroring the response
  envelope's tracking fields. JSON-shaped fields
  (``routing_json``, ``artifact_verification_json``,
  ``git_evidence_json``, ``telegram_result_json``,
  ``runtime_identity_json``, ``artifact_paths_json``,
  ``progress_json``) are JSON-encoded strings; reads decode them.
* Writes are idempotent: ``upsert_run`` does
  ``INSERT OR REPLACE`` keyed by ``run_id``. The same run_id can be
  re-persisted as it moves from ``queued`` to ``running`` to
  ``completed`` (the Hermes async case) without growing extra rows.
* Reads are read-only ``SELECT`` + JSON decode. ``get_run`` returns
  ``None`` when the run_id is not in the table; the caller decides
  the 404 vs dispatcher-fallback path.

The module is intentionally small: it owns *one* table and four
functions (``ensure_schema``, ``upsert_run``, ``get_run``,
``list_recent_runs``). It does not import from ``dispatcher.db``
at module load time (avoids a circular import); it receives a
``sqlite3.Connection`` from the caller.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


_SCHEMA = """
CREATE TABLE IF NOT EXISTS executor_runs (
  run_id                         TEXT PRIMARY KEY,
  requested_executor             TEXT,
  selected_executor              TEXT NOT NULL,
  task_id                        TEXT,
  status                         TEXT NOT NULL,
  progress                       REAL NOT NULL DEFAULT 0.0,
  exit_code                      INTEGER,
  timeout_state                  TEXT,
  cancel_state                   TEXT,
  stdout_summary                 TEXT NOT NULL DEFAULT '',
  stderr_summary                 TEXT NOT NULL DEFAULT '',
  artifact_paths_json            TEXT NOT NULL DEFAULT '[]',
  artifact_verification_json    TEXT NOT NULL DEFAULT '[]',
  git_evidence_json              TEXT,
  telegram_result_json           TEXT NOT NULL DEFAULT '{}',
  runtime_identity_json          TEXT,
  routing_json                   TEXT NOT NULL DEFAULT '{}',
  error                          TEXT,
  created_at                     TEXT NOT NULL,
  updated_at                     TEXT NOT NULL,
  completed_at                   TEXT
);

CREATE INDEX IF NOT EXISTS idx_executor_runs_status ON executor_runs(status);
CREATE INDEX IF NOT EXISTS idx_executor_runs_created_at ON executor_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_executor_runs_selected ON executor_runs(selected_executor);
"""

_init_lock = threading.Lock()
_initialized = False


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the ``executor_runs`` table + indexes if they don't exist.

    Idempotent: re-running on an already-migrated DB is a no-op. The
    migration is additive — no existing tables / columns are
    modified.
    """
    conn.executescript(_SCHEMA)
    conn.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _encode(value: Any) -> str:
    if value is None:
        return "null" if False else "[]"
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return "null"


def _decode_jsonl(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def upsert_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    requested_executor: Optional[str],
    selected_executor: str,
    task_id: Optional[str] = None,
    status: str,
    progress: float = 0.0,
    exit_code: Optional[int] = None,
    timeout_state: Optional[str] = None,
    cancel_state: Optional[str] = None,
    stdout_summary: str = "",
    stderr_summary: str = "",
    artifact_paths: Optional[List[str]] = None,
    artifact_verification: Optional[List[Dict[str, Any]]] = None,
    git_evidence: Optional[Dict[str, Any]] = None,
    telegram_result: Optional[Dict[str, Any]] = None,
    runtime_identity: Optional[Dict[str, Any]] = None,
    routing: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    completed_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Idempotently insert or replace a run row.

    Returns the canonical envelope dict (the same shape
    ``GET /runs/{run_id}`` returns) so the caller can persist +
    respond with one call site.
    """
    now = _now_iso()
    envelope: Dict[str, Any] = {
        "run_id": run_id,
        "requested_executor": requested_executor,
        "selected_executor": selected_executor,
        "task_id": task_id,
        "status": status,
        "progress": float(progress),
        "exit_code": exit_code,
        "timeout_state": timeout_state,
        "cancel_state": cancel_state,
        "stdout_summary": stdout_summary,
        "stderr_summary": stderr_summary,
        "artifact_paths": list(artifact_paths or []),
        "artifact_verification": list(artifact_verification or []),
        "git_evidence": git_evidence,
        "telegram_result": dict(telegram_result or {}),
        "runtime_identity": runtime_identity,
        "routing": dict(routing or {}),
        "error": error,
        "created_at": now,
        "updated_at": now,
        "completed_at": completed_at,
    }

    # Preserve created_at / completed_at on update so repeated
    # upserts (queued -> running -> completed) keep the original
    # creation timestamp and only stamp completed_at once.
    existing = conn.execute(
        "SELECT created_at, completed_at FROM executor_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if existing is not None:
        envelope["created_at"] = existing["created_at"] or now
        if completed_at is None and existing["completed_at"]:
            envelope["completed_at"] = existing["completed_at"]
        if status in {"completed", "failed", "timeout", "cancelled"}:
            envelope["completed_at"] = envelope["completed_at"] or now
    else:
        if status in {"completed", "failed", "timeout", "cancelled"}:
            envelope["completed_at"] = now

    conn.execute(
        """
        INSERT OR REPLACE INTO executor_runs (
          run_id, requested_executor, selected_executor, task_id, status,
          progress, exit_code, timeout_state, cancel_state,
          stdout_summary, stderr_summary,
          artifact_paths_json, artifact_verification_json,
          git_evidence_json, telegram_result_json,
          runtime_identity_json, routing_json, error,
          created_at, updated_at, completed_at
        ) VALUES (
          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            envelope["run_id"],
            envelope["requested_executor"],
            envelope["selected_executor"],
            envelope["task_id"],
            envelope["status"],
            envelope["progress"],
            envelope["exit_code"],
            envelope["timeout_state"],
            envelope["cancel_state"],
            envelope["stdout_summary"],
            envelope["stderr_summary"],
            _encode(artifact_paths),
            _encode(artifact_verification),
            _encode_or_none(git_evidence),
            _encode(telegram_result or {}),
            _encode_or_none(runtime_identity),
            _encode(routing or {}),
            envelope["error"],
            envelope["created_at"],
            envelope["updated_at"],
            envelope["completed_at"],
        ),
    )
    conn.commit()
    return envelope


def _encode_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def get_run(conn: sqlite3.Connection, run_id: str) -> Optional[Dict[str, Any]]:
    """Return the persisted envelope for ``run_id`` or ``None`` if absent."""
    row = conn.execute(
        "SELECT * FROM executor_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    return {
        "run_id": d["run_id"],
        "requested_executor": d["requested_executor"],
        "selected_executor": d["selected_executor"],
        "task_id": d["task_id"],
        "status": d["status"],
        "progress": d["progress"],
        "exit_code": d["exit_code"],
        "timeout_state": d["timeout_state"],
        "cancel_state": d["cancel_state"],
        "stdout_summary": d["stdout_summary"] or "",
        "stderr_summary": d["stderr_summary"] or "",
        "artifact_paths": _decode_jsonl(d.get("artifact_paths_json"), []),
        "artifact_verification": _decode_jsonl(d.get("artifact_verification_json"), []),
        "git_evidence": _decode_jsonl(d.get("git_evidence_json"), None),
        "telegram_result": _decode_jsonl(d.get("telegram_result_json"), {}),
        "runtime_identity": _decode_jsonl(d.get("runtime_identity_json"), None),
        "routing": _decode_jsonl(d.get("routing_json"), {}),
        "error": d["error"],
        "created_at": d["created_at"],
        "updated_at": d["updated_at"],
        "completed_at": d["completed_at"],
    }


def list_recent_runs(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    selected_executor: Optional[str] = None,
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List recent runs (newest first). Read-only."""
    sql = "SELECT run_id FROM executor_runs"
    params: List[Any] = []
    clauses: List[str] = []
    if selected_executor:
        clauses.append("selected_executor = ?")
        params.append(selected_executor)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        got = get_run(conn, r["run_id"])
        if got is not None:
            out.append(got)
    return out


# Canonical status vocabulary accepted by the run-store layer. The
# executor_runs table records whatever the executor reports, but the
# public GET /runs endpoint validates the ``status`` query parameter
# against this set so an unknown value is a deterministic 400 rather
# than a silent empty result.
CANONICAL_RUN_STATUSES = frozenset({
    "queued", "started", "running", "completed", "failed", "timeout", "cancelled",
})


def list_runs(
    conn: sqlite3.Connection,
    *,
    limit: int = 20,
    status: Optional[str] = None,
    selected_executor: Optional[str] = None,
    since: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List recent runs (newest first) with bounded pagination/filtering.

    Read-only: performs a single SELECT against the
    ``executor_runs`` table. Does not call upstream Hermes, launch an
    executor, mutate run state, or scan the repo.

    Ordering is newest-first by ``created_at`` with a deterministic
    tie-breaker on ``run_id`` (DESC) so two runs that share a
    ``created_at`` timestamp have a stable order across calls.

    Parameters
    ----------
    limit:
        Maximum number of rows to return (1..100). The caller is
        responsible for clamping; this function trusts the value
        passed.
    status:
        Optional canonical status filter (one of
        ``CANONICAL_RUN_STATUSES``). The caller validates the value.
    selected_executor:
        Optional filter on the ``selected_executor`` column
        (``claude-code-cli`` or ``hermes``).
    since:
        Optional ISO-8601 timestamp; only runs with
        ``created_at >= since`` are returned. Compared lexically
        against the stored ISO-8601 ``created_at`` strings, which is
        correct for the ``%Y-%m-%dT%H:%M:%SZ`` format written by
        ``_now_iso``.

    Returns a list of canonical envelopes (the same shape returned
    by :func:`get_run`), ordered newest-first. An empty list is
    returned when no rows match the filters.
    """
    clauses: List[str] = []
    params: List[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if selected_executor:
        clauses.append("selected_executor = ?")
        params.append(selected_executor)
    if since:
        clauses.append("created_at >= ?")
        params.append(since)
    sql = "SELECT run_id FROM executor_runs"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    # Deterministic ordering: newest created_at first, run_id DESC
    # as a stable tie-breaker so two runs sharing a created_at
    # timestamp always come back in the same order.
    sql += " ORDER BY created_at DESC, run_id DESC LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        got = get_run(conn, r["run_id"])
        if got is not None:
            out.append(got)
    return out


def init_executor_runs(conn: sqlite3.Connection) -> None:
    """Module-level init guard for ``ensure_schema``.

    Kept for symmetry with ``dispatcher.db._init_schema``; callers
    that already hold a connection can call ``ensure_schema`` directly.
    """
    global _initialized
    with _init_lock:
        if not _initialized:
            ensure_schema(conn)
            _initialized = True


__all__ = [
    "ensure_schema",
    "upsert_run",
    "get_run",
    "list_recent_runs",
    "list_runs",
    "init_executor_runs",
    "CANONICAL_RUN_STATUSES",
]