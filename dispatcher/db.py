"""SQLite connection + schema for the Task Dispatcher.

Single-writer design: the bridge process owns the DB; reads via CLI are
read-only connections. WAL mode is enabled so that the CLI can read
while the bridge is writing.

DB location: data/dispatcher.db (relative to bridge root).
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

# DB lives at <bridge_root>/data/dispatcher.db
_BRIDGE_ROOT = Path(__file__).resolve().parent.parent
DB_DIR = _BRIDGE_ROOT / "data"
DB_PATH = DB_DIR / "dispatcher.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  task_id          TEXT PRIMARY KEY,
  title            TEXT NOT NULL,
  type             TEXT NOT NULL,
  priority         INTEGER NOT NULL DEFAULT 50,
  owner            TEXT NOT NULL DEFAULT 'm2',
  status           TEXT NOT NULL,
  progress_pct     INTEGER NOT NULL DEFAULT 0,
  progress_step    TEXT,
  created_at       TEXT NOT NULL,
  started_at       TEXT,
  finished_at      TEXT,
  duration_sec     REAL,
  input_text       TEXT,
  hermes_run_id    TEXT,
  openai_run_id    TEXT,
  session_id       TEXT,
  mode             TEXT,
  result_path      TEXT,
  error_message    TEXT,
  warning_count    INTEGER NOT NULL DEFAULT 0,
  retry_count      INTEGER NOT NULL DEFAULT 0,
  prompt_version   TEXT,
  model_name       TEXT,
  git_commit       TEXT,
  git_branch       TEXT,
  -- AEE-3: capability-based routing. JSON-encoded list of lowercase,
  -- trimmed, deduped, sorted strings. Empty list '[]' = no filter
  -- (worker_type + adapter + status + lease rules still apply).
  required_capabilities_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_type ON tasks(type);

CREATE TABLE IF NOT EXISTS task_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id       TEXT NOT NULL,
  ts            TEXT NOT NULL,
  kind          TEXT NOT NULL,
  payload_json  TEXT,
  FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id, id);

CREATE TABLE IF NOT EXISTS task_outputs (
  task_id       TEXT PRIMARY KEY,
  output_text   TEXT,
  usage_json    TEXT,
  raw_json      TEXT,
  delivery_json TEXT,
  FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);

-- AEE-2: workers registry. Pull-based Job Claim Protocol needs
-- persistent state for registered workers (id, capabilities,
-- workdir allowlist, last heartbeat). The dataclass counterpart
-- lives in `aee.core.registry.WorkerRecord`.
CREATE TABLE IF NOT EXISTS workers (
  worker_id              TEXT PRIMARY KEY,
  worker_name            TEXT NOT NULL,
  worker_type            TEXT NOT NULL,
  hostname               TEXT,
  capabilities_json      TEXT,
  workdir_allowlist_json TEXT,
  max_concurrent         INTEGER NOT NULL DEFAULT 1,
  registered_at          TEXT NOT NULL,
  last_heartbeat_at      TEXT,
  last_job_id            TEXT
);

CREATE INDEX IF NOT EXISTS idx_workers_type ON workers(worker_type);
"""

# Phase 4: in-place column additions for existing DBs.
# SQLite supports ALTER TABLE ADD COLUMN; missing columns are added if absent.
_PHASE4_MIGRATIONS = [
    "ALTER TABLE task_outputs ADD COLUMN delivery_json TEXT",
]

# AEE-1: additive columns on `tasks` so the dispatcher can speak
# runtime-neutral while keeping the old `hermes_run_id` semantics
# intact. The migration is idempotent: we look up `pragma_table_info`
# for each column and only ALTER if missing. The AEE-1 list of
# columns to add is the same set documented in
# `Abacus/Hermes_Runtime_Bridge_to_AEE_Implementation_Plan.md`,
# section 1.3.
_AEE1_MIGRATIONS: list[tuple[str, str]] = [
    # (column_name, ALTER statement)
    ("runtime_type",     "ALTER TABLE tasks ADD COLUMN runtime_type TEXT DEFAULT 'hermes'"),
    ("adapter_name",     "ALTER TABLE tasks ADD COLUMN adapter_name TEXT DEFAULT 'hermes'"),
    ("external_run_id",  "ALTER TABLE tasks ADD COLUMN external_run_id TEXT"),
    ("worker_id",        "ALTER TABLE tasks ADD COLUMN worker_id TEXT"),
    ("heartbeat_at",     "ALTER TABLE tasks ADD COLUMN heartbeat_at TEXT"),
    ("claim_token_hash", "ALTER TABLE tasks ADD COLUMN claim_token_hash TEXT"),
    ("approval_required","ALTER TABLE tasks ADD COLUMN approval_required INTEGER DEFAULT 0"),
    ("approval_state",   "ALTER TABLE tasks ADD COLUMN approval_state TEXT DEFAULT 'not_required'"),
]

# AEE-3: capability-based job routing. Same idempotent pattern as
# AEE-1. The `*_json` suffix is a storage-only detail — the domain
# model (`dispatcher.models.Task`, `aee.core.job_models.Job`) exposes
# `required_capabilities: List[str]`. The repository layer handles
# encode/decode so callers never see the suffix.
_AEE3_MIGRATIONS: list[tuple[str, str]] = [
    (
        "required_capabilities_json",
        "ALTER TABLE tasks ADD COLUMN required_capabilities_json TEXT NOT NULL DEFAULT '[]'",
    ),
]

# AEE-4: Worker metadata + status. Adds 11 columns to `workers` so any
# registered worker can self-describe its runtime, environment, and
# current state. The columns are NULLable except `status`, which
# defaults to 'unknown' for backward compat with pre-AEE-4 workers.
# The full vocabulary and semantics live in
# `docs/runtime/Worker_Runtime_Contract.md` §3 and §4.
_AEE4_MIGRATIONS: list[tuple[str, str, str]] = [
    # (column_name, ALTER statement, default)
    # Worker runtime self-description (ADR-006 + contract §3).
    ("runtime_name",      "ALTER TABLE workers ADD COLUMN runtime_name TEXT",       "NULL"),
    ("runtime_version",   "ALTER TABLE workers ADD COLUMN runtime_version TEXT",    "NULL"),
    ("operating_system",  "ALTER TABLE workers ADD COLUMN operating_system TEXT",   "NULL"),
    ("architecture",      "ALTER TABLE workers ADD COLUMN architecture TEXT",       "NULL"),
    ("python_version",    "ALTER TABLE workers ADD COLUMN python_version TEXT",     "NULL"),
    ("node_version",      "ALTER TABLE workers ADD COLUMN node_version TEXT",       "NULL"),
    ("git_version",       "ALTER TABLE workers ADD COLUMN git_version TEXT",        "NULL"),
    ("start_time",        "ALTER TABLE workers ADD COLUMN start_time TEXT",         "NULL"),
    # Worker status (ADR-008 + contract §4). Default 'unknown' for
    # pre-AEE-4 rows; new workers set their real status on the
    # first heartbeat.
    ("status",                 "ALTER TABLE workers ADD COLUMN status TEXT NOT NULL DEFAULT 'unknown'", "'unknown'"),
    ("status_message",         "ALTER TABLE workers ADD COLUMN status_message TEXT", "NULL"),
    ("last_status_change_at",  "ALTER TABLE workers ADD COLUMN last_status_change_at TEXT", "NULL"),
]


# ---------------------------------------------------------------------------
# AEE-4: Worker status vocabulary (canonical for the runtime contract)
# ---------------------------------------------------------------------------


# The 5-status set from `docs/runtime/Worker_Runtime_Contract.md` §4.
# `unknown` is the 6th value used as the schema default for pre-AEE-4
# rows; it is NOT a documented status, just a "we have no idea" marker.
WORKER_STATUSES = (
    "idle",
    "busy",
    "offline",
    "draining",
    "error",
    "unknown",
)


def is_valid_status(status: str) -> bool:
    """True iff `status` is one of the canonical Worker Status values.

    The bridge accepts only these 6 strings on `POST /v1/workers/.../heartbeat`;
    any other value is rejected with HTTP 400. Backward-compat: `unknown`
    is in the set because the schema default is `'unknown'`; old rows
    that never had a status set will report `unknown` and that's fine.
    """
    return isinstance(status, str) and status in WORKER_STATUSES


# ---------------------------------------------------------------------------
# Capability normalization (used by both the API and the manager)
# ---------------------------------------------------------------------------


def normalize_capabilities(values: Optional[List[str]]) -> List[str]:
    """Canonicalize a capability list: lowercase, trim, drop empties,
    dedupe, sort. Returns a fresh list (does not mutate input).

    This is the single point where capability strings are
    sanitized before persisting. Adapters and the matching code
    can rely on the output being a stable, comparable form.
    """
    if not values:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        if not isinstance(v, str):
            continue
        norm = v.strip().lower()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    out.sort()
    return out


def encode_capabilities(values: Optional[List[str]]) -> str:
    """Storage helper: normalize then JSON-encode."""
    return _json.dumps(normalize_capabilities(values))


def decode_capabilities(blob: Optional[str]) -> List[str]:
    """Storage helper: JSON-decode a `required_capabilities_json`
    blob. Defensive against NULL, empty string, and malformed
    payloads — all collapse to an empty list.
    """
    if not blob:
        return []
    try:
        data = _json.loads(blob)
    except _json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return normalize_capabilities(data)


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

# Thread-local connection: FastAPI runs sync route handlers in a threadpool,
# so we need one connection per worker thread. Bridge only has 1 uvicorn
# worker (per supervisor conf) so this is mostly defensive.
_local = threading.local()
_init_lock = threading.Lock()
_initialized = False


def _ensure_dir() -> None:
    DB_DIR.mkdir(parents=True, exist_ok=True)


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    # Phase 4: in-place column additions for already-deployed DBs.
    # PRAGMA table_info returns rows for each existing column; if the
    # target column is missing, run the migration.
    for col in ("delivery_json",):
        row = conn.execute(
            "SELECT 1 FROM pragma_table_info('task_outputs') WHERE name = ?",
            (col,),
        ).fetchone()
        if row is None:
            for stmt in _PHASE4_MIGRATIONS:
                if f"ADD COLUMN {col}" in stmt:
                    conn.execute(stmt)
    # AEE-1: additive columns on `tasks`. Same idempotent pattern.
    _apply_aee1_migrations(conn)
    # AEE-3: capability matching column. Idempotent via pragma check.
    _apply_aee3_migrations(conn)
    # AEE-4: worker metadata + status columns on `workers`. Same pattern.
    _apply_aee4_migrations(conn)
    # AEE-1: index for the new lookup path. Idempotent via IF NOT EXISTS.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_external_run_id ON tasks(external_run_id)"
    )
    # AEE-4: index on `workers.status` so future AEE-5+ scheduler
    # queries ("give me all idle workers") are O(log n), not O(n).
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_workers_status ON workers(status)"
    )
    conn.commit()


def _apply_aee1_migrations(conn: sqlite3.Connection) -> None:
    """Run each AEE-1 migration if its column is not yet present.

    Idempotent: re-running on an already-migrated DB is a no-op.
    Only logs when a column is actually added (not on every
    re-init), and the message goes to stderr so it doesn't
    pollute JSON / unittest output.
    """
    import sys
    for col, stmt in _AEE1_MIGRATIONS:
        row = conn.execute(
            "SELECT 1 FROM pragma_table_info('tasks') WHERE name = ?",
            (col,),
        ).fetchone()
        if row is None:
            conn.execute(stmt)
            print(f"[db] AEE-1 migration: added tasks.{col}", file=sys.stderr)


def _apply_aee3_migrations(conn: sqlite3.Connection) -> None:
    """AEE-3: capability-based routing column.

    Same idempotent pragma check as AEE-1. Re-running on an
    already-migrated DB is a no-op. The column is `NOT NULL
    DEFAULT '[]'`, so existing rows are filled with the empty
    list automatically — no backfill required.
    """
    import sys
    for col, stmt in _AEE3_MIGRATIONS:
        row = conn.execute(
            "SELECT 1 FROM pragma_table_info('tasks') WHERE name = ?",
            (col,),
        ).fetchone()
        if row is None:
            conn.execute(stmt)
            print(f"[db] AEE-3 migration: added tasks.{col}", file=sys.stderr)


def _apply_aee4_migrations(conn: sqlite3.Connection) -> None:
    """AEE-4: worker metadata + status columns on `workers`.

    Same idempotent pragma check as AEE-1 / AEE-3. Re-running on an
    already-migrated DB is a no-op. The 10 NULLable columns default
    to NULL for legacy rows; `status` defaults to 'unknown' so the
    5-status vocabulary is honoured from the moment the migration
    lands, even for workers that haven't heartbeated yet.

    The 11 columns together implement the Worker Runtime Contract
    metadata + status fields documented in
    `docs/runtime/Worker_Runtime_Contract.md` §3 and §4.
    """
    import sys
    for col, stmt, _default in _AEE4_MIGRATIONS:
        row = conn.execute(
            "SELECT 1 FROM pragma_table_info('workers') WHERE name = ?",
            (col,),
        ).fetchone()
        if row is None:
            conn.execute(stmt)
            print(f"[db] AEE-4 migration: added workers.{col}", file=sys.stderr)


def run_migrations() -> list[str]:
    """Public entry point: applies all pending migrations.

    Returns the list of columns that were added this run (empty
    list = already up to date). Safe to call from CLI; safe to
    call twice in a row.
    """
    import sys  # local import keeps the top of the module clean
    conn = get_conn()
    added: list[str] = []
    for col, stmt in _AEE1_MIGRATIONS:
        row = conn.execute(
            "SELECT 1 FROM pragma_table_info('tasks') WHERE name = ?",
            (col,),
        ).fetchone()
        if row is None:
            conn.execute(stmt)
            print(f"[db] AEE-1 migration: added tasks.{col}", file=sys.stderr)
            added.append(col)
    # AEE-3: capability matching column. Same idempotent pattern.
    for col, stmt in _AEE3_MIGRATIONS:
        row = conn.execute(
            "SELECT 1 FROM pragma_table_info('tasks') WHERE name = ?",
            (col,),
        ).fetchone()
        if row is None:
            conn.execute(stmt)
            print(f"[db] AEE-3 migration: added tasks.{col}", file=sys.stderr)
            added.append(col)
    # AEE-4: worker metadata + status columns on `workers`.
    for col, stmt, _default in _AEE4_MIGRATIONS:
        row = conn.execute(
            "SELECT 1 FROM pragma_table_info('workers') WHERE name = ?",
            (col,),
        ).fetchone()
        if row is None:
            conn.execute(stmt)
            print(f"[db] AEE-4 migration: added workers.{col}", file=sys.stderr)
            added.append(col)
    # Indexes (idempotent).
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_external_run_id ON tasks(external_run_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_workers_status ON workers(status)"
    )
    conn.commit()
    return added


def get_conn() -> sqlite3.Connection:
    """Return a thread-local connection (initialized on first use)."""
    global _initialized
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    _ensure_dir()
    conn = sqlite3.connect(str(DB_PATH), isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    with _init_lock:
        if not _initialized:
            _init_schema(conn)
            _initialized = True
    _local.conn = conn
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Context manager for explicit transactions."""
    conn = get_conn()
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def open_ro(path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a read-only connection (for the CLI; safe to call concurrently)."""
    p = path or DB_PATH
    if not p.exists():
        raise FileNotFoundError(f"Dispatcher DB not found at {p}; has the bridge ever run?")
    # uri=True with mode=ro gives a true read-only handle that won't block on
    # the writer's WAL.
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def db_path() -> Path:
    return DB_PATH


# ---------------------------------------------------------------------------
# AEE-2: workers table CRUD + claim protocol helpers
# ---------------------------------------------------------------------------
#
# These helpers are intentionally low-level (raw SQL) so the API layer
# (`aee/api/workers.py`, `aee/api/jobs.py`) can stay thin and the
# dispatcher's existing `TaskManager` continues to be the only writer
# of `tasks` rows.
#
# All functions are sync and use the thread-local connection from
# `get_conn()`. They are safe to call from FastAPI request handlers
# (which run in a thread pool) AND from the watcher's single asyncio
# task — the latter must take care to release the GIL around the call
# if it ever becomes a hot path. Today the workers table is touched
# only on register / heartbeat / claim, all of which are O(1).


import json as _json  # local alias to avoid shadowing
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# AEE-4: optional Worker metadata fields. All NULLable except the
# status enum, which defaults to 'unknown'. The bridge accepts
# them on `POST /v1/workers/register` and persists them as-is
# (after capability normalization). See
# `docs/runtime/Worker_Runtime_Contract.md` §3 for the full schema
# and the AEE-4 conformance checklist.
_WORKER_METADATA_FIELDS = (
    "runtime_name",
    "runtime_version",
    "operating_system",
    "architecture",
    "python_version",
    "node_version",
    "git_version",
    "start_time",
)


def upsert_worker(
    *,
    worker_id: str,
    worker_name: str,
    worker_type: str,
    hostname: Optional[str],
    capabilities: List[str],
    workdir_allowlist: List[str],
    max_concurrent: int,
    # AEE-4: optional metadata fields. All default to None so the
    # AEE-2 / AEE-3 callers (and the existing tests) keep working
    # without change. The bridge's worker API may pass strings;
    # we store whatever the caller sends and only normalize
    # capabilities (the only field with a canonical form).
    runtime_name: Optional[str] = None,
    runtime_version: Optional[str] = None,
    operating_system: Optional[str] = None,
    architecture: Optional[str] = None,
    python_version: Optional[str] = None,
    node_version: Optional[str] = None,
    git_version: Optional[str] = None,
    start_time: Optional[str] = None,
    # AEE-4: optional initial status. Bridge workers normally
    # report `status` via heartbeat; some flows (e.g. the
    # closed-loop smoke test) want to set the status at register
    # time too. We accept any of the 5 canonical values plus
    # `unknown`; anything else falls back to `unknown`.
    status: Optional[str] = None,
    status_message: Optional[str] = None,
) -> Dict[str, Any]:
    """Register a new worker or update an existing one (idempotent).

    The `worker_id` is the primary key; the caller decides its value
    (the API layer uses `worker_xxx` style ids; the manager could
    derive one from `worker_name` if needed). A re-register with
    different capabilities is allowed — we keep the original
    `registered_at` so an operator can see the worker's age.
    """
    conn = get_conn()
    existing = conn.execute(
        "SELECT registered_at, status FROM workers WHERE worker_id = ?", (worker_id,)
    ).fetchone()
    is_new = existing is None
    registered_at = existing["registered_at"] if existing else _now_iso()
    # Preserve the previous status across re-registers if the caller
    # didn't pass a new one. A re-register is metadata refresh, not
    # a state change.
    prior_status = existing["status"] if existing else None
    if status is None:
        new_status = prior_status or "unknown"
    else:
        new_status = status if is_valid_status(status) else "unknown"
    # AEE-3: normalize the worker's capability list the same way
    # job requirements are normalized, so the matcher's subset
    # check is comparing strings in the same canonical form.
    normalized_caps = normalize_capabilities(capabilities)
    # Bump `last_status_change_at` only on a real status change.
    # A fresh row is "born" with status='unknown' (the schema
    # default); we don't stamp a separate change time on insert —
    # operators see the `registered_at` as the row's birth. Only
    # RE-registers that flip the status (e.g. 'unknown' -> 'idle')
    # get a fresh `last_status_change_at`.
    if is_new:
        status_change_at = None
    else:
        status_change_at = _now_iso() if new_status != prior_status else None
    with transaction() as conn2:
        conn2.execute(
            """
            INSERT INTO workers (
              worker_id, worker_name, worker_type, hostname,
              capabilities_json, workdir_allowlist_json,
              max_concurrent, registered_at, last_heartbeat_at, last_job_id,
              runtime_name, runtime_version, operating_system, architecture,
              python_version, node_version, git_version, start_time,
              status, status_message, last_status_change_at
            ) VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL,
              ?, ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?
            )
            ON CONFLICT(worker_id) DO UPDATE SET
              worker_name            = excluded.worker_name,
              worker_type            = excluded.worker_type,
              hostname               = excluded.hostname,
              capabilities_json      = excluded.capabilities_json,
              workdir_allowlist_json = excluded.workdir_allowlist_json,
              max_concurrent         = excluded.max_concurrent,
              runtime_name           = excluded.runtime_name,
              runtime_version        = excluded.runtime_version,
              operating_system       = excluded.operating_system,
              architecture           = excluded.architecture,
              python_version         = excluded.python_version,
              node_version           = excluded.node_version,
              git_version            = excluded.git_version,
              start_time             = excluded.start_time,
              status                 = excluded.status,
              status_message         = excluded.status_message,
              last_status_change_at  = COALESCE(
                excluded.last_status_change_at, workers.last_status_change_at
              )
            """,
            (
                worker_id, worker_name, worker_type, hostname,
                _json.dumps(normalized_caps),
                _json.dumps(workdir_allowlist),
                int(max_concurrent),
                registered_at,
                runtime_name, runtime_version, operating_system, architecture,
                python_version, node_version, git_version, start_time,
                new_status, status_message, status_change_at,
            ),
        )
    return {
        "worker_id": worker_id,
        "worker_name": worker_name,
        "worker_type": worker_type,
        "registered": True,
        "registered_at": registered_at,
    }


def get_worker(worker_id: str) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
    ).fetchone()
    return _row_to_worker_dict(row) if row else None


def list_workers(worker_type: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_conn()
    if worker_type:
        rows = conn.execute(
            "SELECT * FROM workers WHERE worker_type = ? ORDER BY registered_at DESC",
            (worker_type,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM workers ORDER BY registered_at DESC"
        ).fetchall()
    return [_row_to_worker_dict(r) for r in rows]


def update_worker_heartbeat(
    worker_id: str,
    *,
    job_id: Optional[str] = None,
    # AEE-4: optional status payload. The bridge accepts the 5
    # canonical values plus `unknown`; invalid values are
    # silently coerced to `unknown` (so a buggy client can't
    # wedge the column).
    status: Optional[str] = None,
    status_message: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Bump last_heartbeat_at (and optionally last_job_id / status).

    AEE-4 contract: when `status` is supplied and differs from the
    stored value, also stamp `last_status_change_at = now()`. A
    re-heartbeat with the same status is a no-op for the status
    columns; we only bump `last_heartbeat_at`. This is the
    "liveness only" mode used by every pre-AEE-4 worker.
    """
    conn = get_conn()
    now = _now_iso()
    # Read the prior status so we can decide whether to stamp
    # `last_status_change_at`. Done before the UPDATE so the
    # COALESCE below is correct.
    prior_row = conn.execute(
        "SELECT status FROM workers WHERE worker_id = ?", (worker_id,),
    ).fetchone()
    if prior_row is None:
        return None
    prior_status = prior_row["status"]
    new_status = status if (status is not None and is_valid_status(status)) else None
    with transaction() as conn2:
        if new_status is not None and new_status != prior_status:
            cur = conn2.execute(
                "UPDATE workers SET "
                "last_heartbeat_at = ?, last_job_id = COALESCE(?, last_job_id), "
                "status = ?, status_message = ?, last_status_change_at = ? "
                "WHERE worker_id = ?",
                (now, job_id, new_status, status_message, now, worker_id),
            )
        else:
            cur = conn2.execute(
                "UPDATE workers SET last_heartbeat_at = ?, "
                "last_job_id = COALESCE(?, last_job_id), "
                "status_message = COALESCE(?, status_message) "
                "WHERE worker_id = ?",
                (now, job_id, status_message, worker_id),
            )
    if cur.rowcount == 0:
        return None
    return get_worker(worker_id)


def _row_to_worker_dict(row) -> Dict[str, Any]:
    caps: List[str] = []
    if row["capabilities_json"]:
        try:
            caps = _json.loads(row["capabilities_json"])
        except _json.JSONDecodeError:
            caps = []
    allow: List[str] = []
    if row["workdir_allowlist_json"]:
        try:
            allow = _json.loads(row["workdir_allowlist_json"])
        except _json.JSONDecodeError:
            allow = []
    # AEE-4: include all 11 new metadata / status fields. Use
    # `row[k]` defensively in case a very old DB row is missing
    # one of the new columns (the AEE-4 migration is idempotent
    # but a fresh DB created with a stale `dispatcher/db.py` build
    # could in theory lack them). The defaults mirror the SQL
    # DEFAULT clauses.
    def _get(name: str, default):
        try:
            v = row[name]
        except (IndexError, KeyError):
            return default
        return v if v is not None else default
    out: Dict[str, Any] = {
        "worker_id": row["worker_id"],
        "worker_name": row["worker_name"],
        "worker_type": row["worker_type"],
        "hostname": row["hostname"],
        "capabilities": caps,
        "workdir_allowlist": allow,
        "max_concurrent": int(row["max_concurrent"]),
        "registered_at": row["registered_at"],
        "last_heartbeat_at": row["last_heartbeat_at"],
        "last_job_id": row["last_job_id"],
        # AEE-4 metadata fields (all NULLable; status defaults to
        # 'unknown' so the field is always present in the dict).
        "runtime_name": _get("runtime_name", None),
        "runtime_version": _get("runtime_version", None),
        "operating_system": _get("operating_system", None),
        "architecture": _get("architecture", None),
        "python_version": _get("python_version", None),
        "node_version": _get("node_version", None),
        "git_version": _get("git_version", None),
        "start_time": _get("start_time", None),
        "status": _get("status", "unknown"),
        "status_message": _get("status_message", None),
        "last_status_change_at": _get("last_status_change_at", None),
    }
    return out


# ---------------------------------------------------------------------------
# AEE-2: Job claim helpers
# ---------------------------------------------------------------------------


def _task_claim_eligible_where(
    *,
    worker_type: str,
    capabilities: List[str],
) -> tuple[str, list]:
    """Build the WHERE clause for `find_claimable_job`.

    A job is claimable iff:
      * status = 'queued'
      * worker_id IS NULL (nobody has claimed it yet)
      * NOT cancelled
      * approval_required = 0 OR approval_state = 'approved'
      * adapter_name == worker_type (runtime routing; 'hermes'-typed
        workers claim 'hermes'-typed jobs, 'aee_lightweight' claims
        'aee_lightweight', etc.)
      * required_capabilities ⊆ worker_capabilities  (AEE-3)
        An empty required_capabilities list always satisfies the
        subset check, so the legacy "any worker can claim" behaviour
        is preserved for jobs created without a capability filter.
    """
    where = (
        "status = 'queued' "
        "AND worker_id IS NULL "
        "AND (approval_required = 0 OR approval_state = 'approved')"
        " AND adapter_name = ?"
    )
    params: list = [worker_type]
    # AEE-3: capability subset filter. We do the gating in Python
    # (after fetching a candidate) rather than in SQL, because
    # required_capabilities is a JSON blob and SQLite has no
    # native array contains/superset operator. The WHERE clause
    # filters by the cheap predicates first; `find_claimable_job`
    # then post-filters by the JSON contents. Worst case: a
    # busy queue with many jobs whose JSON doesn't match, the
    # post-filter just looks at the top-1 candidate (LIMIT 1).
    return where, params


def _capability_subset_match(
    required: List[str], worker_caps: List[str]
) -> bool:
    """True iff `required ⊆ worker_caps`.

    AEE-3 matching rule. An empty `required` always matches
    (no filter). Capability strings are assumed pre-normalized
    (lowercase, deduped); we still defensively call
    `normalize_capabilities` here so the matcher is robust
    against direct-DB writes.
    """
    req = set(normalize_capabilities(required))
    if not req:
        return True
    have = set(normalize_capabilities(worker_caps))
    return req.issubset(have)


def find_claimable_job(
    *,
    worker_type: str,
    capabilities: List[str],
) -> Optional[Dict[str, Any]]:
    """Return the next claimable job for this worker, or None.

    Picks the highest-priority, oldest queued job matching the worker's
    type AND whose required_capabilities are a subset of the worker's
    capabilities. Does NOT update anything — that's `claim_job()`'s job.
    """
    conn = get_conn()
    where, params = _task_claim_eligible_where(
        worker_type=worker_type, capabilities=capabilities,
    )
    # Iterate up to N candidates so the post-filter can skip rows
    # whose required_capabilities are not a subset of the worker's.
    # N is small (5) to keep the worst case bounded; in practice the
    # queue is short and a single LIMIT 1 suffices.
    rows = conn.execute(
        f"SELECT * FROM tasks WHERE {where} "
        "ORDER BY priority DESC, created_at ASC LIMIT 5",
        params,
    ).fetchall()
    for row in rows:
        candidate = _row_to_task_dict(row)
        if _capability_subset_match(
            decode_capabilities(candidate.get("required_capabilities_json")),
            capabilities,
        ):
            return candidate
    return None


def _row_to_task_dict(row) -> Dict[str, Any]:
    """Lightweight row→dict for the claim path. The canonical
    `dispatcher.models.Task` is what the API layer ultimately
    returns; this helper is for the claim select and tests.

    AEE-3: we also expose `required_capabilities` as a plain
    list (decoded from the JSON blob) so callers don't have to
    know about the storage suffix. The raw `*_json` column is
    kept in the dict for direct DB access where useful.
    """
    keys = (
        "task_id", "title", "type", "priority", "owner", "status",
        "progress_pct", "progress_step", "created_at", "started_at", "finished_at",
        "duration_sec", "input_text", "hermes_run_id", "openai_run_id", "session_id",
        "mode", "result_path", "error_message", "warning_count", "retry_count",
        "prompt_version", "model_name", "git_commit", "git_branch",
        "runtime_type", "adapter_name", "external_run_id", "worker_id",
        "heartbeat_at", "claim_token_hash", "approval_required", "approval_state",
        "required_capabilities_json",
    )
    out: Dict[str, Any] = {k: row[k] for k in keys if k in row.keys()}
    # Domain-level view: list[str], not the JSON blob.
    out["required_capabilities"] = decode_capabilities(
        out.get("required_capabilities_json")
    )
    return out


def claim_job(
    *,
    task_id: str,
    worker_id: str,
    claim_token_hash: str,
) -> bool:
    """Atomically transition a queued task to running for a worker.

    Returns True iff the claim was successful. False means another
    worker beat us, or the task is no longer eligible.
    """
    conn = get_conn()
    with transaction() as conn2:
        cur = conn2.execute(
            """
            UPDATE tasks
            SET status = 'running',
                worker_id = ?,
                heartbeat_at = ?,
                claim_token_hash = ?,
                started_at = COALESCE(started_at, ?)
            WHERE task_id = ?
              AND status = 'queued'
              AND worker_id IS NULL
            """,
            (worker_id, _now_iso(), claim_token_hash, _now_iso(), task_id),
        )
        if cur.rowcount == 0:
            return False
    return True


def verify_claim_token(task_id: str, presented_token_hash: str) -> bool:
    """Check that the presented token hash matches the stored one.

    Used by heartbeat / complete / fail / cancel to authenticate the
    worker. Constant-time compare via `hmac.compare_digest` to avoid
    timing leaks.
    """
    import hmac
    conn = get_conn()
    row = conn.execute(
        "SELECT claim_token_hash, worker_id, status FROM tasks WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if row is None or row["claim_token_hash"] is None:
        return False
    return hmac.compare_digest(str(row["claim_token_hash"]), str(presented_token_hash))


def update_task_heartbeat(task_id: str) -> bool:
    conn = get_conn()
    with transaction() as conn2:
        cur = conn2.execute(
            "UPDATE tasks SET heartbeat_at = ? WHERE task_id = ? AND status = 'running'",
            (_now_iso(), task_id),
        )
    return cur.rowcount > 0


def append_task_log(task_id: str, line: str) -> bool:
    """Append a line to the task's per-task log file. Mirror of
    `dispatcher.manager._append_log`, exposed here so the AEE
    API layer doesn't need to reach into a private helper.
    """
    from pathlib import Path as _Path
    # Re-use the dispatcher's LOGS_DIR if it's been overridden
    # (test fixtures redirect it to a tmpdir); otherwise fall back
    # to <bridge_root>/logs.
    import dispatcher.manager as _mgr  # local import to avoid cycle
    log_dir = _Path(getattr(_mgr, "LOGS_DIR", _Path(__file__).resolve().parent.parent / "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    p = log_dir / f"{task_id}.log"
    with open(p, "a", encoding="utf-8") as f:
        from datetime import datetime, timezone as _tz
        ts = datetime.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        f.write(f"{ts} [LOG] {line}\n")
    # Also emit a 'log' event for /tasks/{id}/events consumers.
    conn = get_conn()
    conn.execute(
        "INSERT INTO task_events (task_id, ts, kind, payload_json) VALUES (?, ?, ?, ?)",
        (task_id, _now_iso(), "log", _json.dumps({"line": line[:500]})),
    )
    return True


def find_by_external_run_id(external_run_id: str) -> Optional[Dict[str, Any]]:
    """Lookup by the new canonical external_run_id column (AEE-1)."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM tasks WHERE external_run_id = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (external_run_id,),
    ).fetchone()
    return _row_to_task_dict(row) if row else None


def backfill_external_run_id() -> int:
    """One-shot deploy-time backfill: copy `hermes_run_id` to
    `external_run_id` for legacy tasks.

    Idempotent — only touches rows where `external_run_id IS NULL`
    AND `hermes_run_id IS NOT NULL`. Returns the number of rows
    updated.
    """
    conn = get_conn()
    cur = conn.execute(
        "UPDATE tasks SET external_run_id = hermes_run_id "
        "WHERE external_run_id IS NULL AND hermes_run_id IS NOT NULL"
    )
    conn.commit()
    return int(cur.rowcount)


def list_claimable_summary(worker_type: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Read-only list of what *would* be claimable — used by /health
    and tests. Mirrors find_claimable_job() but returns up to `limit`
    rows without locking semantics.
    """
    conn = get_conn()
    where, params = _task_claim_eligible_where(
        worker_type=worker_type, capabilities=[],
    )
    rows = conn.execute(
        f"SELECT task_id, title, priority, created_at, adapter_name, runtime_type "
        f"FROM tasks WHERE {where} ORDER BY priority DESC, created_at ASC LIMIT ?",
        params + [int(limit)],
    ).fetchall()
    return [dict(r) for r in rows]
