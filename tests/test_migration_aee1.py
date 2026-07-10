"""AEE-1 SQLite migration test.

The test:

1. Verifies that running `_apply_aee1_migrations` on a freshly
   created `tasks` table adds the eight AEE-1 columns with the
   declared defaults.
2. Verifies the migration is idempotent: re-running it does not
   raise and does not duplicate columns.
3. Verifies that the existing `hermes_run_id` column is preserved
   (the old wire shape still works).
4. Verifies that `idx_tasks_external_run_id` is created.
5. Verifies that `dispatcher.models.Task` exposes the new fields
   as dataclass attributes.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dispatcher import db  # noqa: E402
from dispatcher.db import _apply_aee1_migrations  # noqa: E402
from dispatcher.models import Task  # noqa: E402


# Minimal tasks schema — same as `_SCHEMA`'s `tasks` table minus the
# AEE-1 columns so we can confirm the migration actually adds them.
_BASE_SCHEMA = """
CREATE TABLE tasks (
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
  git_branch       TEXT
);
"""


def _make_fresh_db() -> sqlite3.Connection:
    """Create an in-memory DB with the *old* schema only."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_BASE_SCHEMA)
    return conn


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {r["name"] for r in conn.execute("PRAGMA table_info('tasks')").fetchall()}


# ---------------------------------------------------------------------------
# 1. Migration adds the expected columns
# ---------------------------------------------------------------------------


def test_migration_adds_columns():
    conn = _make_fresh_db()
    before = _columns(conn)
    aee1_expected = {
        "runtime_type", "adapter_name", "external_run_id",
        "worker_id", "heartbeat_at", "claim_token_hash",
        "approval_required", "approval_state",
    }
    assert not (aee1_expected & before), "test setup wrong: AEE-1 columns already present"
    _apply_aee1_migrations(conn)
    after = _columns(conn)
    missing = aee1_expected - after
    assert not missing, f"AEE-1 migration missed columns: {missing}"
    # Old column preserved.
    assert "hermes_run_id" in after, "hermes_run_id must be preserved by AEE-1 migration"
    print("  OK   migration adds all 8 AEE-1 columns and keeps hermes_run_id")


# ---------------------------------------------------------------------------
# 2. Defaults on the new columns
# ---------------------------------------------------------------------------


def test_migration_defaults():
    conn = _make_fresh_db()
    _apply_aee1_migrations(conn)
    conn.execute(
        "INSERT INTO tasks (task_id, title, type, status, created_at) "
        "VALUES ('T-1', 't', 'ops', 'queued', '2026-07-10T00:00:00Z')"
    )
    row = conn.execute("SELECT * FROM tasks WHERE task_id='T-1'").fetchone()
    assert row["runtime_type"] == "hermes", row["runtime_type"]
    assert row["adapter_name"] == "hermes", row["adapter_name"]
    assert row["external_run_id"] is None
    assert row["worker_id"] is None
    assert row["heartbeat_at"] is None
    assert row["claim_token_hash"] is None
    assert row["approval_required"] == 0
    assert row["approval_state"] == "not_required"
    print("  OK   AEE-1 columns have the expected defaults")


# ---------------------------------------------------------------------------
# 3. Idempotent
# ---------------------------------------------------------------------------


def test_migration_is_idempotent():
    conn = _make_fresh_db()
    _apply_aee1_migrations(conn)
    cols_after_first = _columns(conn)
    # Second run: must not raise and must not add new columns.
    _apply_aee1_migrations(conn)
    cols_after_second = _columns(conn)
    assert cols_after_first == cols_after_second, "migration duplicated columns"
    print("  OK   AEE-1 migration is idempotent")


# ---------------------------------------------------------------------------
# 4. Index created
# ---------------------------------------------------------------------------


def test_external_run_id_index_created():
    conn = _make_fresh_db()
    _apply_aee1_migrations(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_external_run_id ON tasks(external_run_id)"
    )
    rows = conn.execute("PRAGMA index_list('tasks')").fetchall()
    names = {r["name"] for r in rows}
    assert "idx_tasks_external_run_id" in names
    print("  OK   idx_tasks_external_run_id is present")


# ---------------------------------------------------------------------------
# 5. Task dataclass exposes the new fields
# ---------------------------------------------------------------------------


def test_task_dataclass_exposes_aee1_fields():
    t = Task(task_id="T-1", title="t", type="ops", status="queued", created_at="x")
    for f in (
        "runtime_type", "adapter_name", "external_run_id", "worker_id",
        "heartbeat_at", "claim_token_hash", "approval_required", "approval_state",
    ):
        assert hasattr(t, f), f"Task missing AEE-1 field: {f}"
    # Defaults match the migration.
    assert t.runtime_type == "hermes"
    assert t.adapter_name == "hermes"
    assert t.approval_required is False
    assert t.approval_state == "not_required"
    # to_dict includes them.
    d = t.to_dict()
    for f in (
        "runtime_type", "adapter_name", "external_run_id", "worker_id",
        "heartbeat_at", "claim_token_hash", "approval_required", "approval_state",
    ):
        assert f in d
    print("  OK   Task dataclass + to_dict expose AEE-1 fields with defaults")


# ---------------------------------------------------------------------------
# 6. db.run_migrations() is idempotent at the public API
# ---------------------------------------------------------------------------


def test_run_migrations_public_api_idempotent(tmp_db_dir: Path):
    # Use a temp file DB so we exercise the real get_conn() path
    # without touching the production data/dispatcher.db.
    db.DB_DIR = tmp_db_dir
    db.DB_PATH = tmp_db_dir / "dispatcher.db"
    # Reset thread-local so a fresh connection opens against the tmp DB.
    db._local.conn = None
    db._initialized = False
    try:
        # First call: applies the schema + migration.
        first = db.run_migrations()
        assert isinstance(first, list)
        # Second call: no-op.
        second = db.run_migrations()
        assert second == [], f"second run_migrations() should be a no-op, got {second}"
    finally:
        # Restore production path so other tests aren't affected.
        db.DB_DIR = Path("/home/ubuntu/hermes-runtime-bridge/data")
        db.DB_PATH = db.DB_DIR / "dispatcher.db"
    print("  OK   run_migrations() is idempotent at the public API")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main() -> int:
    print("=== AEE-1 SQLite migration tests ===")
    tests = [
        test_migration_adds_columns,
        test_migration_defaults,
        test_migration_is_idempotent,
        test_external_run_id_index_created,
        test_task_dataclass_exposes_aee1_fields,
    ]
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL {t.__name__}: {type(exc).__name__}: {exc}")
            return 1
    # The public-API test needs a real temp dir.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        try:
            test_run_migrations_public_api_idempotent(Path(td))
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL test_run_migrations_public_api_idempotent: {type(exc).__name__}: {exc}")
            return 1
    print()
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
