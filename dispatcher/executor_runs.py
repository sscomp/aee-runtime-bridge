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
import os
import sqlite3
import sys
import threading
import uuid
from dataclasses import asdict, dataclass, field
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

-- AEE Harness v2 P0 (W-2 Durable Evidence Store): append-only per-step
-- evidence table. One row per persisted state-machine step result, keyed
-- by (run_id, step, generation, attempt) so a retry of the SAME step under
-- the SAME attempt/generation updates in place (idempotent), while a new
-- attempt or a new rescue `generation` appends a fresh row (audit
-- preserved). This is the "append-only per step" contract from the ADR:
-- prior completed steps are never erased by a later step's failure.
-- Additive + idempotent: CREATE TABLE IF NOT EXISTS, same pattern as the
-- `executor_runs` table above; existing tables/columns are untouched.
-- `task_id` is NULLable to mirror `executor_runs.task_id` (orphan runs
-- with task_id=NULL can still emit step evidence).
CREATE TABLE IF NOT EXISTS executor_run_steps (
  step_id             TEXT PRIMARY KEY,
  task_id             TEXT,
  run_id              TEXT NOT NULL,
  step                TEXT NOT NULL,
  action_type         TEXT NOT NULL,
  owner               TEXT NOT NULL,
  status              TEXT NOT NULL,
  generation          INTEGER NOT NULL DEFAULT 1,
  attempt             INTEGER NOT NULL DEFAULT 1,
  turns_used          INTEGER,
  evidence_hash       TEXT,
  summary             TEXT NOT NULL DEFAULT '',
  result_json         TEXT,
  error               TEXT,
  artifact_refs_json  TEXT NOT NULL DEFAULT '[]',
  started_at          TEXT,
  finished_at         TEXT,
  recorded_at         TEXT NOT NULL,
  UNIQUE(run_id, step, generation, attempt)
);

CREATE INDEX IF NOT EXISTS idx_run_steps_task
  ON executor_run_steps(task_id, generation, recorded_at);
CREATE INDEX IF NOT EXISTS idx_run_steps_run
  ON executor_run_steps(run_id, generation, recorded_at);
"""

# ---------------------------------------------------------------------------
# P1 run observability migration (TASK-AEE-RUN-OBSERVABILITY-P1).
# ---------------------------------------------------------------------------
# Three additive, NULLable columns on ``executor_runs`` so the
# canonical observability envelope (``dispatcher.observability``) can
# be derived from persisted evidence rather than fabricated:
#
#   * ``last_heartbeat_at`` — ISO-8601 timestamp of the most recent
#     executor heartbeat. NULL on legacy rows / pre-P1 dispatches.
#   * ``current_step``     — short human-readable step label captured
#     at the most recent progress update (e.g. "running tests",
#     "committing"). NULL on legacy rows.
#   * ``phase``            — coarse phase marker captured at write
#     time (queued|running|terminal). NULL on legacy rows; the read
#     path falls back to ``derive_phase(status)`` when NULL.
#
# All three are NULLable and have NO DEFAULT — legacy rows keep NULL
# and the observability read path treats NULL as "no evidence" (the
# stall policy returns ``missing_timestamp`` rather than fabricating).
# The migration uses the same idempotent ``pragma_table_info`` pattern
# as AEE-1 / AEE-7.2 so re-running on a migrated DB is a no-op.
_P1_OBSERVABILITY_MIGRATIONS: list[tuple[str, str]] = [
    (
        "last_heartbeat_at",
        "ALTER TABLE executor_runs ADD COLUMN last_heartbeat_at TEXT",
    ),
    (
        "current_step",
        "ALTER TABLE executor_runs ADD COLUMN current_step TEXT",
    ),
    (
        "phase",
        "ALTER TABLE executor_runs ADD COLUMN phase TEXT",
    ),
]


_init_lock = threading.Lock()
_initialized = False


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the ``executor_runs`` table + indexes if they don't exist.

    Idempotent: re-running on an already-migrated DB is a no-op. The
    migration is additive — no existing tables / columns are
    modified.
    """
    conn.executescript(_SCHEMA)
    # P1 observability columns (additive, idempotent, NULLable).
    # Same pragma_table_info pattern as AEE-1 / AEE-7.2 in
    # ``dispatcher.db``: re-running on a migrated DB is a no-op.
    import sys
    for col, stmt in _P1_OBSERVABILITY_MIGRATIONS:
        row = conn.execute(
            "SELECT 1 FROM pragma_table_info('executor_runs') WHERE name = ?",
            (col,),
        ).fetchone()
        if row is None:
            conn.execute(stmt)
            print(
                f"[executor_runs] P1 observability migration: added {col}",
                file=sys.stderr,
            )
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
    # P1 observability (TASK-AEE-RUN-OBSERVABILITY-P1). All optional,
    # NULL on legacy rows. The read path derives the canonical
    # observability envelope from these persisted values.
    last_heartbeat_at: Optional[str] = None,
    current_step: Optional[str] = None,
    phase: Optional[str] = None,
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
        # P1 observability — persisted on every upsert so the read
        # path can derive the envelope from row data alone. NULL on
        # legacy rows that pre-date the migration; the read path
        # treats NULL as "no evidence" (the stall policy returns
        # ``missing_timestamp`` rather than fabricating).
        "last_heartbeat_at": last_heartbeat_at,
        "current_step": current_step,
        "phase": phase,
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
          created_at, updated_at, completed_at,
          last_heartbeat_at, current_step, phase
        ) VALUES (
          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
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
            envelope["last_heartbeat_at"],
            envelope["current_step"],
            envelope["phase"],
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
        # P1 observability columns. Use ``.get()`` because legacy
        # rows / pre-migration schemas do not have these columns;
        # the read path treats NULL as "no evidence".
        "last_heartbeat_at": d.get("last_heartbeat_at"),
        "current_step": d.get("current_step"),
        "phase": d.get("phase"),
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

    Also runs the stale-run reconciliation on init so orphaned
    ``status='running'`` rows left by failed task-creation races
    (see ``reconcile_stale_runs``) are cleaned up automatically on
    bridge restart.
    """
    global _initialized
    with _init_lock:
        if not _initialized:
            ensure_schema(conn)
            reconcile_stale_runs(conn)
            _initialized = True


# ---------------------------------------------------------------------------
# Stale-run reconciliation (production-readiness minimal finalization).
# ---------------------------------------------------------------------------
# When ``_seed_run()`` in ``aee/runtimes/executor_cli.py`` creates an
# ``executor_runs`` row BEFORE the dispatcher task is created (P1.1
# heartbeat seeding), a task-creation failure (e.g. HERMES_API_KEY
# outage) orphans the row: it has ``task_id=None``, ``status='running'``,
# and no lifecycle code path will ever transition it to terminal.
# Without reconciliation these stale rows accumulate indefinitely and
# misleadingly appear as active running/stalled on monitoring dashboards.
#
# ``reconcile_stale_runs`` transitions orphaned rows to ``cancelled``
# (preserving audit history — no DELETE) so dead historical records
# cannot misleadingly appear as active. The audit trail (created_at,
# stdout_summary, etc.) is preserved; only status + completed_at
# change. The function is idempotent and safe to call repeatedly.

_STALE_ORPHAN_MAX_AGE_SEC = 3600  # 1 hour — well beyond any legitimate dispatch


def reconcile_stale_runs(
    conn: sqlite3.Connection,
    *,
    max_age_sec: int = _STALE_ORPHAN_MAX_AGE_SEC,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """Transition orphaned executor_runs to ``cancelled`` (audit-preserving).

    Targets rows that are:
      * ``status='running'`` (or ``'queued'`` / ``'started'``)
      * ``task_id IS NULL`` (never linked to a dispatcher task)
      * older than ``max_age_sec`` (default 1 hour)

    Each matched row is UPDATEd in-place:
      * ``status``       → ``'cancelled'``
      * ``completed_at``  → ``created_at`` (preserves original timestamp)
      * ``updated_at``    → now
      * ``error``         → ``'reconcile_stale_runs: orphaned executor_run with task_id=NULL'``

    No rows are deleted — the full audit history is preserved. The
    function is idempotent: re-running on a reconciled DB finds zero
    matches (all orphans are already ``cancelled``).

    Returns a summary dict with ``scanned``, ``reconciled``, and
    ``run_ids`` (the list of reconciled run_ids) for observability.
    """
    from datetime import datetime as _dt, timezone as _tz

    if now is None:
        now = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Select orphaned non-terminal rows
    orphan_statuses = ("running", "queued", "started")
    placeholders = ",".join("?" * len(orphan_statuses))
    rows = conn.execute(
        f"""
        SELECT run_id, created_at, status
          FROM executor_runs
         WHERE status IN ({placeholders})
           AND task_id IS NULL
        """,
        orphan_statuses,
    ).fetchall()

    # Age filter — only reconcile rows older than max_age_sec
    now_ts = _dt.fromisoformat(now.replace("Z", "+00:00")).timestamp()
    to_reconcile = []
    for r in rows:
        created_raw = r["created_at"]
        if not created_raw:
            # No timestamp — reconcile it (safer than leaving it)
            to_reconcile.append(r["run_id"])
            continue
        try:
            created_ts = _dt.fromisoformat(
                created_raw.replace("Z", "+00:00")
            ).timestamp()
        except (ValueError, TypeError):
            to_reconcile.append(r["run_id"])
            continue
        age = now_ts - created_ts
        if age >= max_age_sec:
            to_reconcile.append(r["run_id"])

    for rid in to_reconcile:
        conn.execute(
            """
            UPDATE executor_runs
               SET status = 'cancelled',
                   completed_at = created_at,
                   updated_at = ?,
                   error = 'reconcile_stale_runs: orphaned executor_run with task_id=NULL'
             WHERE run_id = ? AND task_id IS NULL
            """,
            (now, rid),
        )

    if to_reconcile:
        conn.commit()

    return {
        "scanned": len(rows),
        "reconciled": len(to_reconcile),
        "run_ids": to_reconcile,
    }


# ---------------------------------------------------------------------------
# P1.1 write-side activation (TASK-AEE-RUN-OBSERVABILITY-WRITE-ACTIVATION).
# ---------------------------------------------------------------------------
# Heartbeat writer: persists live executor progress into the
# ``executor_runs`` row WITHOUT going through GET /runs or
# GET /runs/{run_id}. The write path is the executor lifecycle loop
# (the poll loop in ``ClaudeCodeCliRunner.run`` and the Hermes async
# reconciliation path); the read path remains pure.
#
# Design rules (work-order §3–§8):
# 1. Heartbeats are emitted by the execution lifecycle / background
#    supervisor, NEVER by GET /runs or GET /runs/{run_id}.
# 2. A terminal row is never re-heartbeated. The writer checks the
#    persisted status first and skips the UPDATE when the row is
#    already terminal.
# 3. The cadence is deterministic: a named constant
#    (``DEFAULT_HEARTBEAT_INTERVAL_SECONDS``) overridable via the
#    ``RUN_HEARTBEAT_INTERVAL_SECONDS`` environment variable. Read
#    once per call (not cached at import time) so a runtime env change
#    is honoured without a restart, mirroring the stall-threshold
#    contract in ``dispatcher.observability``.
# 4. ``current_step`` values are restricted to the canonical
#    lifecycle vocabulary (``LIFECYCLE_STEPS``) — no model-internal
#    / fabricated step labels.
# 5. The writer is idempotent: calling it twice with the same
#    arguments is safe; the second call simply re-stamps
#    ``updated_at`` and ``last_heartbeat_at`` to the new now().

# Deterministic heartbeat cadence. The default (5s) is small enough
# that a 30s test run produces >= 2 heartbeat samples while the run
# is non-terminal, and large enough to keep the DB write cost
# negligible on long runs. Override via the env var for operators
# who need a different cadence.
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 5


def get_heartbeat_interval_seconds() -> float:
    """Return the configured heartbeat interval in seconds.

    Reads ``RUN_HEARTBEAT_INTERVAL_SECONDS`` from the environment on
    every call. A missing or malformed value falls back to
    ``DEFAULT_HEARTBEAT_INTERVAL_SECONDS``. A malformed / non-positive
    value also prints a warning to stderr — same contract as
    ``dispatcher.observability.get_stall_threshold_seconds`` so
    operators see their env var was ignored, but the write path
    never raises.
    """
    raw = os.environ.get("RUN_HEARTBEAT_INTERVAL_SECONDS")
    if raw is None:
        return float(DEFAULT_HEARTBEAT_INTERVAL_SECONDS)
    try:
        val = float(raw)
    except (ValueError, TypeError):
        print(
            f"[executor_runs] RUN_HEARTBEAT_INTERVAL_SECONDS={raw!r} is "
            f"not a number; falling back to "
            f"{DEFAULT_HEARTBEAT_INTERVAL_SECONDS}s",
            file=sys.stderr,
        )
        return float(DEFAULT_HEARTBEAT_INTERVAL_SECONDS)
    if val <= 0:
        print(
            f"[executor_runs] RUN_HEARTBEAT_INTERVAL_SECONDS={val} is "
            f"non-positive; falling back to "
            f"{DEFAULT_HEARTBEAT_INTERVAL_SECONDS}s",
            file=sys.stderr,
        )
        return float(DEFAULT_HEARTBEAT_INTERVAL_SECONDS)
    return val


# Canonical lifecycle step vocabulary (work-order §6). These are the
# ONLY values ``update_heartbeat`` accepts for ``current_step``; any
# other value is rejected so a caller cannot fabricate a
# model-internal step. The terminal steps map 1:1 to the canonical
# run statuses (``{completed, failed, timeout, cancelled}``).
LIFECYCLE_STEPS = frozenset({
    "queued",               # run accepted, executor not yet spawned
    "starting",             # executor subprocess spawn in flight
    "running",              # executor running, no specific sub-phase
    "collecting_output",    # draining stdout / stderr post-exit
    "verifying_artifacts",  # artifact verification in flight
    "completed",            # terminal: success
    "failed",               # terminal: non-zero exit / error
    "timeout",              # terminal: deadline exceeded
    "cancelled",            # terminal: cancel requested
})

# Terminal-step subset (mirrors _TERMINAL_STATUSES in observability).
_TERMINAL_STEPS = frozenset({"completed", "failed", "timeout", "cancelled"})


def update_heartbeat(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    current_step: str,
    phase: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Persist a single heartbeat for a non-terminal executor run.

    Updates the ``executor_runs`` row in-place:

      * ``last_heartbeat_at`` = now (ISO-8601 UTC)
      * ``current_step``       = ``current_step`` (validated against
                                  ``LIFECYCLE_STEPS``)
      * ``phase``              = ``phase`` (if given) — callers should
                                  pass ``"queued"`` / ``"running"`` /
                                  ``"terminal"`` from the canonical
                                  phase vocabulary in
                                  ``dispatcher.observability``
      * ``updated_at``         = now

    Safety contract (work-order §3–§8):

      * **Terminal rows are never re-heartbeated.** If the persisted
        status is in ``{completed, failed, timeout, cancelled}`` the
        function returns ``None`` without writing — a terminal run
        never receives further heartbeat updates. This is the
        work-order §5 requirement.
      * **GET /runs is a pure read.** This function is called ONLY by
        the executor lifecycle / background supervisor, never by
        the GET endpoints.
      * **Step validation.** ``current_step`` MUST be in
        ``LIFECYCLE_STEPS``; a fabricated / model-internal step is
        rejected with ``ValueError`` so the caller cannot persist a
        non-canonical label (work-order §6).
      * **Missing row.** If the run_id is not in ``executor_runs``
        the function returns ``None`` without writing — heartbeats
        only apply to rows the dispatch path already created.

    Returns the updated envelope (the same shape returned by
    :func:`get_run`) when the row was updated, or ``None`` when the
    row was skipped (terminal / missing / no-op). Never raises
    except for the ``current_step`` validation contract.
    """
    if current_step not in LIFECYCLE_STEPS:
        raise ValueError(
            f"current_step={current_step!r} is not in the canonical "
            f"LIFECYCLE_STEPS vocabulary; refusing to persist a "
            f"fabricated / model-internal step label"
        )

    # Read the existing row first — never heartbeaten terminal rows.
    existing = conn.execute(
        "SELECT status FROM executor_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if existing is None:
        return None  # missing row — only the dispatch path creates rows
    persisted_status = existing["status"]
    if persisted_status in _TERMINAL_STEPS:
        return None  # terminal — never re-heartbeat (work-order §5)

    now = _now_iso()
    conn.execute(
        """
        UPDATE executor_runs
           SET last_heartbeat_at = ?,
               current_step      = ?,
               phase             = COALESCE(?, phase),
               updated_at        = ?
         WHERE run_id = ?
        """,
        (now, current_step, phase, now, run_id),
    )
    conn.commit()
    return get_run(conn, run_id)


# ---------------------------------------------------------------------------
# AEE Harness v2 P0 — Durable step-evidence store (W-2 primitive).
# ---------------------------------------------------------------------------
# Append-only per-step evidence for the v2 state machine. Each state
# (PLAN/INSPECT/DECIDE/PATCH/TEST/VERIFY/PACKAGE/NOTIFY) persists its
# structured result here immediately upon completion, BEFORE the next
# state runs, so a later step's failure never erases prior completed
# steps (the ADR's core invariant). The store is keyed by
# (run_id, step, generation, attempt):
#   * retry of the SAME step under the SAME attempt/generation →
#     ON CONFLICT DO UPDATE (idempotent in-place replace);
#   * a new attempt or a new rescue `generation` → appends a new row
#     (audit history preserved, never erased).
# This slice implements ONLY the persistence primitive + read helper.
# No state-machine, no call site in the executor path is wired to it
# yet — that is W-3 (P1). Backward-compatible: the table is additive
# (`CREATE TABLE IF NOT EXISTS` in ``_SCHEMA``); existing runs/tasks
# have zero step-evidence rows and behave exactly as before.
#
# Vocabulary: `action_type` is the bounded-action class (inspect/decide/
# patch/test/verify/package/notify, plus plan). It is NOT validated here
# — any generic string is accepted — so the primitive stays decoupled
# from the W-1 bounded-run contract enforcer that will later restrict it.

# Canonical bounded-action classes (ADR "Bounded Run Contract" + state
# machine). Generic strings are also accepted; these are the documented
# set, exposed for callers/tests that want to reference them by name.
ACTION_TYPE_PLAN = "plan"
ACTION_TYPE_INSPECT = "inspect"
ACTION_TYPE_DECIDE = "decide"
ACTION_TYPE_PATCH = "patch"
ACTION_TYPE_TEST = "test"
ACTION_TYPE_VERIFY = "verify"
ACTION_TYPE_PACKAGE = "package"
ACTION_TYPE_NOTIFY = "notify"
ACTION_TYPES = (
    ACTION_TYPE_PLAN,
    ACTION_TYPE_INSPECT,
    ACTION_TYPE_DECIDE,
    ACTION_TYPE_PATCH,
    ACTION_TYPE_TEST,
    ACTION_TYPE_VERIFY,
    ACTION_TYPE_PACKAGE,
    ACTION_TYPE_NOTIFY,
)

# Step-evidence owner: who produced the step (ADR state-machine [LLM] vs
# [Runtime] ownership).
STEP_OWNER_LLM = "llm"
STEP_OWNER_RUNTIME = "runtime"
STEP_OWNERS = (STEP_OWNER_LLM, STEP_OWNER_RUNTIME)

# Canonical step-evidence statuses. `partial` is first-class per the ADR
# (near-complete / budget-exhausted work is marked partial, not failed).
# Not validated here — kept permissive for this primitive slice.
STEP_STATUS_RUNNING = "running"
STEP_STATUS_COMPLETED = "completed"
STEP_STATUS_FAILED = "failed"
STEP_STATUS_SKIPPED = "skipped"
STEP_STATUS_PARTIAL = "partial"
STEP_STATUSES = (
    STEP_STATUS_RUNNING,
    STEP_STATUS_COMPLETED,
    STEP_STATUS_FAILED,
    STEP_STATUS_SKIPPED,
    STEP_STATUS_PARTIAL,
)


@dataclass(frozen=True)
class StepEvidence:
    """A single persisted state-machine step result (frozen, append-only).

    Backward-compatible structured representation of one bounded action's
    evidence. Fields mirror the ADR's "Each record carries" list
    (``task_id``, ``run_id``, ``step``, ``owner``, ``status``,
    ``started_at``, ``finished_at``, ``turns_used``, ``evidence_hash``)
    plus the actionable payload (``action_type``, ``generation``,
    ``attempt``, ``summary``, ``result``, ``error``, ``artifact_refs``).

    Fields:
        task_id: Owning task (NULLable for orphan runs, mirroring
            ``executor_runs.task_id``).
        run_id: The concrete executor run this step belongs to.
        step: State-machine step name (plan/inspect/decide/patch/test/
            verify/package/notify).
        action_type: Bounded-action class (one of ``ACTION_TYPES`` or a
            generic string).
        owner: ``"llm"`` or ``"runtime"`` (ADR [LLM] vs [Runtime]).
        status: One of ``STEP_STATUSES`` (permissive in this slice).
        generation: Rescue generation counter (1 = original run; >1 =
            a rescue run linked to the same task_id). Default 1.
        attempt: Retry attempt within this step+generation. Default 1.
        turns_used: LLM turns consumed (None for runtime-owned steps).
        evidence_hash: Hex digest over the step's evidence payload
            (caller-computed; None when not yet hashed).
        summary: Short human-readable one-line summary.
        result: Structured result payload (dict); JSON-encoded on write.
        error: Error string for failed/partial steps (None otherwise).
        artifact_refs: List of artifact ids / paths this step produced
            or verified; JSON-encoded on write.
        started_at / finished_at: ISO-8601 UTC timestamps (None allowed).
        recorded_at: ISO-8601 UTC persistence timestamp (always set).
        step_id: Repository-assigned id (None until persisted).
    """
    run_id: str
    step: str
    action_type: str
    owner: str
    status: str
    task_id: Optional[str] = None
    generation: int = 1
    attempt: int = 1
    turns_used: Optional[int] = None
    evidence_hash: Optional[str] = None
    summary: str = ""
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    artifact_refs: Optional[List[str]] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    recorded_at: str = field(default_factory=_now_iso)
    step_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def append_step(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    step: str,
    action_type: str,
    owner: str,
    status: str,
    task_id: Optional[str] = None,
    generation: int = 1,
    attempt: int = 1,
    turns_used: Optional[int] = None,
    evidence_hash: Optional[str] = None,
    summary: str = "",
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    artifact_refs: Optional[List[str]] = None,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    step_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist one step-evidence row (append-only, idempotent per attempt).

    Idempotency: the composite key ``(run_id, step, generation, attempt)``
    is UNIQUE. A retry with the same key does ``ON CONFLICT DO UPDATE`` —
    it replaces the row's payload in place and preserves the original
    ``step_id`` / insertion order. A new ``attempt`` or ``generation``
    appends a new row, so the audit trail across retries/rescues is
    preserved (never erased). This matches the ADR's "append-only per
    step" across attempts/generations while being safe under retries.

    Returns the canonical envelope dict (the same shape ``list_steps``
    returns and ``StepEvidence.to_dict`` produces) so the caller can
    persist + use the evidence with one call site.
    """
    now = _now_iso()
    sid = step_id or f"step-{uuid.uuid4().hex[:16]}"
    refs = list(artifact_refs or [])
    envelope: Dict[str, Any] = {
        "step_id": sid,
        "task_id": task_id,
        "run_id": run_id,
        "step": step,
        "action_type": action_type,
        "owner": owner,
        "status": status,
        "generation": int(generation),
        "attempt": int(attempt),
        "turns_used": turns_used,
        "evidence_hash": evidence_hash,
        "summary": summary,
        "result": result,
        "error": error,
        "artifact_refs": refs,
        "started_at": started_at,
        "finished_at": finished_at,
        "recorded_at": now,
    }
    conn.execute(
        """
        INSERT INTO executor_run_steps (
          step_id, task_id, run_id, step, action_type, owner, status,
          generation, attempt, turns_used, evidence_hash, summary,
          result_json, error, artifact_refs_json,
          started_at, finished_at, recorded_at
        ) VALUES (
          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(run_id, step, generation, attempt) DO UPDATE SET
          status            = excluded.status,
          action_type       = excluded.action_type,
          owner             = excluded.owner,
          task_id           = COALESCE(excluded.task_id, executor_run_steps.task_id),
          turns_used        = excluded.turns_used,
          evidence_hash     = excluded.evidence_hash,
          summary           = excluded.summary,
          result_json       = excluded.result_json,
          error             = excluded.error,
          artifact_refs_json = excluded.artifact_refs_json,
          started_at        = COALESCE(excluded.started_at, executor_run_steps.started_at),
          finished_at       = excluded.finished_at,
          recorded_at       = excluded.recorded_at
        """,
        (
            sid,
            task_id,
            run_id,
            step,
            action_type,
            owner,
            status,
            int(generation),
            int(attempt),
            turns_used,
            evidence_hash,
            summary,
            _encode_or_none(result),
            error,
            json.dumps(refs, ensure_ascii=False),
            started_at,
            finished_at,
            now,
        ),
    )
    conn.commit()
    # Read back so the returned envelope reflects the persisted
    # step_id / started_at (COALESCE may have preserved the originals).
    row = conn.execute(
        "SELECT * FROM executor_run_steps WHERE run_id = ? AND step = ? "
        "AND generation = ? AND attempt = ?",
        (run_id, step, int(generation), int(attempt)),
    ).fetchone()
    if row is not None:
        return _row_to_step_dict(row)
    return envelope


def list_steps(
    conn: sqlite3.Connection,
    *,
    task_id: Optional[str] = None,
    run_id: Optional[str] = None,
    step: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 1000,
) -> List[Dict[str, Any]]:
    """Return ordered step-evidence rows for a task and/or run.

    At least one of ``task_id`` / ``run_id`` should be supplied (calling
    with neither returns an empty list rather than scanning the whole
    table — a bounded, deliberate no-op). Rows are ordered by
    ``generation, attempt, recorded_at`` ASC so a caller resuming from
    durable state reads steps in natural forward order; the order is
    stable across calls (same data → same order).

    Read-only: a single SELECT. Never launches an executor, mutates run
    state, or writes. Returns canonical envelope dicts (same shape as
    :func:`append_step` / ``StepEvidence.to_dict``).
    """
    if task_id is None and run_id is None:
        return []
    clauses: List[str] = []
    params: List[Any] = []
    if task_id is not None:
        clauses.append("task_id = ?")
        params.append(task_id)
    if run_id is not None:
        clauses.append("run_id = ?")
        params.append(run_id)
    if step is not None:
        clauses.append("step = ?")
        params.append(step)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    sql = (
        "SELECT * FROM executor_run_steps WHERE "
        + " AND ".join(clauses)
        + " ORDER BY generation ASC, attempt ASC, recorded_at ASC, rowid ASC LIMIT ?"
    )
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_step_dict(r) for r in rows]


def get_step(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    step: str,
    generation: int = 1,
    attempt: int = 1,
) -> Optional[Dict[str, Any]]:
    """Return one step-evidence row by its composite key, or ``None``.

    Convenience read for "resume from the last durable state of step X":
    the caller asks for the latest generation/attempt of a step. Read-only.
    """
    row = conn.execute(
        "SELECT * FROM executor_run_steps WHERE run_id = ? AND step = ? "
        "AND generation = ? AND attempt = ?",
        (run_id, step, int(generation), int(attempt)),
    ).fetchone()
    return _row_to_step_dict(row) if row is not None else None


def _row_to_step_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "step_id": row["step_id"],
        "task_id": row["task_id"],
        "run_id": row["run_id"],
        "step": row["step"],
        "action_type": row["action_type"],
        "owner": row["owner"],
        "status": row["status"],
        "generation": int(row["generation"]),
        "attempt": int(row["attempt"]),
        "turns_used": row["turns_used"],
        "evidence_hash": row["evidence_hash"],
        "summary": row["summary"] or "",
        "result": _decode_jsonl(row["result_json"], None),
        "error": row["error"],
        "artifact_refs": _decode_jsonl(row["artifact_refs_json"], []),
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "recorded_at": row["recorded_at"],
    }


__all__ = [
    "ensure_schema",
    "upsert_run",
    "get_run",
    "list_recent_runs",
    "list_runs",
    "init_executor_runs",
    "CANONICAL_RUN_STATUSES",
    # P1.1 write-side activation (TASK-AEE-RUN-OBSERVABILITY-WRITE-ACTIVATION).
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "get_heartbeat_interval_seconds",
    "LIFECYCLE_STEPS",
    "update_heartbeat",
    # P2.1 background completion sync (TASK-AEE-P2-BRIDGE-HERMES-COMPLETION-SYNC).
    "list_non_terminal_runs",
    # Stale-run reconciliation (production-readiness minimal finalization).
    "reconcile_stale_runs",
    # AEE Harness v2 P0 — durable step-evidence store (W-2 primitive).
    "StepEvidence",
    "append_step",
    "list_steps",
    "get_step",
    "ACTION_TYPES",
    "ACTION_TYPE_PLAN",
    "ACTION_TYPE_INSPECT",
    "ACTION_TYPE_DECIDE",
    "ACTION_TYPE_PATCH",
    "ACTION_TYPE_TEST",
    "ACTION_TYPE_VERIFY",
    "ACTION_TYPE_PACKAGE",
    "ACTION_TYPE_NOTIFY",
    "STEP_OWNERS",
    "STEP_OWNER_LLM",
    "STEP_OWNER_RUNTIME",
    "STEP_STATUSES",
    "STEP_STATUS_RUNNING",
    "STEP_STATUS_COMPLETED",
    "STEP_STATUS_FAILED",
    "STEP_STATUS_SKIPPED",
    "STEP_STATUS_PARTIAL",
]


# ---------------------------------------------------------------------------
# P2.1 background completion sync (TASK-AEE-P2-BRIDGE-HERMES-COMPLETION-SYNC).
# ---------------------------------------------------------------------------
# Read-only listing of non-terminal executor_runs rows, used by the
# background ExecutorRunWatcher to find Hermes-dispatched runs that are
# still in-flight so the watcher can poll the upstream adapter once per
# tick. The query is bounded (limit) and read-only (single SELECT).

_NON_TERMINAL_STATUSES = frozenset({
    "queued", "started", "running",
})


def list_non_terminal_runs(
    conn: sqlite3.Connection,
    *,
    selected_executor: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Return non-terminal executor_runs rows (queued/started/running).

    Read-only SELECT ordered newest-first by ``created_at``. The
    ``selected_executor`` filter narrows the scan to a specific
    executor (e.g. ``"hermes"``) so the watcher only polls the
    adapter that owns the run. Returns canonical envelopes (same
    shape as :func:`get_run`).
    """
    clauses: List[str] = []
    params: List[Any] = []
    placeholders = ",".join("?" * len(_NON_TERMINAL_STATUSES))
    clauses.append(f"status IN ({placeholders})")
    params.extend(sorted(_NON_TERMINAL_STATUSES))
    if selected_executor:
        clauses.append("selected_executor = ?")
        params.append(selected_executor)
    sql = "SELECT run_id FROM executor_runs WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        got = get_run(conn, r["run_id"])
        if got is not None:
            out.append(got)
    return out