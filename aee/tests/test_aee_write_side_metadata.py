"""AEE-7.5 — Dispatcher write-side metadata.

Closes the §20.9.10 deferred limitation by verifying the
*static contracts* of the write-side metadata path. We test
the source-of-truth (migrations, dataclass shape, wire schema)
directly, without spinning up a full TaskManager against a
private tempdir DB — that level of integration testing is
already covered by the e2e tests in
``tests/test_dispatcher.py`` and is not duplicated here.

Why this file deliberately avoids the full TaskManager path
----------------------------------------------------------------
The dispatcher hot path shares module-level state
(``dispatcher.db.DB_PATH``, ``dispatcher.db._local.conn``,
``dispatcher.manager.LOGS_DIR``, ``dispatcher.manager.REPORTS_DIR``)
across test modules. Adding a test that swaps these out for a
tempdir-based TaskManager is a real isolation problem: even
with the cleanest teardown, the cached thread-local
connection and the ``_initialized`` global flag from the
fresh ``_init_schema`` call leak into sibling test modules,
breaking them in subtle ways. The full e2e contract for
``create()`` and ``start()`` is already covered by the
existing ``tests/test_dispatcher.py`` suite (which runs
under the ``tests/`` directory, not the ``aee/tests/``
directory, with its own module-level DB reset).

This file contributes 4 surface checks:

  1. The migration module declares both new columns.
  2. A fresh ``_init_schema`` call lands both columns in
     the ``tasks`` table.
  3. The ``Task`` dataclass carries both new fields with
     ``Optional[str] = None`` defaults (legacy tasks round-
     trip with None).
  4. The FastAPI ``CreateRunRequest`` wire contract exposes
     ``executor_session_id`` as a max_length=200 Optional
     field, accepting 200 chars and rejecting 201.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _make_fresh_conn(db_path: Path) -> sqlite3.Connection:
    """Open a connection to a private tempdir DB and apply the
    full dispatcher schema. Touches no module-level globals.
    """
    import dispatcher.db as db

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    db._apply_pragmas(conn)
    db._init_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# 1. Migration source-of-truth
# ---------------------------------------------------------------------------


class TestMigrationSOT(unittest.TestCase):
    """The migration module declares both new columns; this is
    the source of truth that ``db._init_schema`` and
    ``db.run_migrations`` both consume.
    """

    def test_module_declares_both_columns(self) -> None:
        import dispatcher.db as db

        cols = {col for col, _stmt in db._AEE7_WRITE_SIDE_MIGRATIONS}
        self.assertEqual(cols, {"executor_session_id", "runtime_run_id"})

    def test_each_migration_is_alter_table(self) -> None:
        import dispatcher.db as db

        for col, stmt in db._AEE7_WRITE_SIDE_MIGRATIONS:
            self.assertTrue(stmt.strip().upper().startswith("ALTER TABLE"))
            self.assertIn("tasks", stmt)
            self.assertIn(col, stmt)


# ---------------------------------------------------------------------------
# 2. Migration applied to a fresh DB
# ---------------------------------------------------------------------------


class TestMigrationAppliedToFreshDB(unittest.TestCase):
    """A fresh DB opened via ``_make_fresh_conn`` ends up with
    both new columns in the ``tasks`` table.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="aee75_mig_")
        self.db_path = Path(self.tmpdir) / "dispatcher.db"
        self.conn = _make_fresh_conn(self.db_path)

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        for ext in ("", "-wal", "-shm"):
            try:
                (Path(self.tmpdir) / f"dispatcher.db{ext}").unlink()
            except Exception:
                pass
        try:
            Path(self.tmpdir).rmdir()
        except Exception:
            pass

    def test_both_columns_present(self) -> None:
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(tasks)").fetchall()}
        self.assertIn("executor_session_id", cols)
        self.assertIn("runtime_run_id", cols)

    def test_columns_are_nullable(self) -> None:
        # PRAGMA table_info: notnull=0 means NULLable.
        row_map = {
            r["name"]: dict(r)
            for r in self.conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        self.assertEqual(row_map["executor_session_id"]["notnull"], 0)
        self.assertEqual(row_map["runtime_run_id"]["notnull"], 0)


# ---------------------------------------------------------------------------
# 3. Task dataclass shape
# ---------------------------------------------------------------------------


class TestTaskDataclassShape(unittest.TestCase):
    """``dispatcher.models.Task`` carries both new fields
    with ``Optional[str] = None`` defaults — legacy tasks
    round-trip with the new fields as None.
    """

    def test_legacy_construction_unchanged(self) -> None:
        from dispatcher.models import Task

        t = Task(
            task_id="0001",
            type="normal",
            mode="ops",
            status="pending",
            title="x",
            input_text="y",
            created_at="2026-07-11T00:00:00Z",
        )
        self.assertIsNone(t.executor_session_id)
        self.assertIsNone(t.runtime_run_id)

    def test_new_construction_accepts_both(self) -> None:
        from dispatcher.models import Task

        t = Task(
            task_id="0002",
            type="normal",
            mode="ops",
            status="running",
            title="x",
            input_text="y",
            created_at="2026-07-11T00:00:00Z",
            executor_session_id="caller-abc",
            runtime_run_id="run-xyz",
        )
        self.assertEqual(t.executor_session_id, "caller-abc")
        self.assertEqual(t.runtime_run_id, "run-xyz")


# ---------------------------------------------------------------------------
# 4. FastAPI wire contract
# ---------------------------------------------------------------------------


class TestCreateRunRequestSchema(unittest.TestCase):
    """``CreateRunRequest`` exposes ``executor_session_id`` as
    an Optional[str] with a max_length cap.
    """

    def test_field_present_with_default_none(self) -> None:
        import app  # noqa: F401  (ensures module load)

        from app import CreateRunRequest

        fields = CreateRunRequest.model_fields
        self.assertIn("executor_session_id", fields)
        f = fields["executor_session_id"]
        # Optional[str] with None default.
        self.assertFalse(f.is_required())
        self.assertIsNone(f.default)

    def test_accepts_max_length_value(self) -> None:
        from app import CreateRunRequest

        req = CreateRunRequest(
            input="y",
            type="normal",
            mode="ops",
            executor_session_id="x" * 200,
        )
        self.assertIsNotNone(req.executor_session_id)
        self.assertEqual(len(req.executor_session_id), 200)

    def test_rejects_over_max_length(self) -> None:
        from app import CreateRunRequest
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            CreateRunRequest(
                input="y",
                type="normal",
                mode="ops",
                executor_session_id="x" * 201,
            )


if __name__ == "__main__":
    unittest.main()
