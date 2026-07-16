"""AEE-8.2 — Dispatcher profile wiring (read-only storage).

Targeted tests for the ``profile`` field plumbing that AEE-8.2
adds to the dispatcher. The field is **stored but not enforced**:
no safety-gate, no toolset restriction, no routing change reads
it. This test file verifies the data-flow contract:

    CreateRunRequest.profile
    → Task dataclass (profile field)
    → TaskManager.create(profile=...)
    → tasks.profile column (idempotent migration)
    → manager.get() / _row_to_task() read-back

Test isolation
--------------
This module uses the same ``_live_db_guard.point_module_to_temp_db``
pattern as ``tests/test_dispatcher.py`` so the production
``data/dispatcher.db`` is never touched. Each test class that
needs a DB gets a fresh tempdir copy.

Run:
    PYTHONPATH=. python3 -m unittest aee.tests.test_aee82_dispatcher_profile_wiring -v
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Repo root resolution (mirrors the pattern in test_aee77_apply_sidecars.py)
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ---------------------------------------------------------------------------
# DB isolation: point dispatcher.db + dispatcher.manager at a tempdir
# copy before any test imports them. Same pattern as
# ``tests/test_dispatcher.py`` (AEE-7.6 live-DB guard).
# ---------------------------------------------------------------------------
_TMPDIR = Path(tempfile.mkdtemp(prefix="aee82-profile-test-"))
_guard_spec = importlib.util.spec_from_file_location(
    "_aee82_live_db_guard", _ROOT / "tests" / "_live_db_guard.py"
)
_guard_mod = importlib.util.module_from_spec(_guard_spec)
_guard_spec.loader.exec_module(_guard_mod)

_TEMP_DB_PATH = Path(_TMPDIR) / "dispatcher.db"
_point_ctx = _guard_mod.point_module_to_temp_db(_TEMP_DB_PATH)
_db, _mgr = _point_ctx.__enter__()

import atexit as _atexit


def _aee82_cleanup() -> None:
    try:
        _point_ctx.__exit__(None, None, None)
    except Exception:
        pass
    import shutil as _sh
    with __import__("contextlib").suppress(OSError):
        if _TMPDIR.exists():
            _sh.rmtree(_TMPDIR, ignore_errors=True)


_atexit.register(_aee82_cleanup)

# NOW import dispatcher modules — they see the rebound DB_PATH.
from dispatcher.models import Task  # noqa: E402
from dispatcher.manager import TaskManager  # noqa: E402
import dispatcher.db as _dbmod  # noqa: E402
import dispatcher.manager as _mgrmod  # noqa: E402

# Reset logs/reports dirs to the tempdir.
_mgrmod.LOGS_DIR = _TMPDIR / "logs"
_mgrmod.REPORTS_DIR = _TMPDIR / "reports"
_mgrmod.LOGS_DIR.mkdir(parents=True, exist_ok=True)
_mgrmod.REPORTS_DIR.mkdir(parents=True, exist_ok=True)


# ===========================================================================
# 1. Task dataclass — profile field exists, defaults to None
# ===========================================================================


class TaskProfileFieldTests(unittest.TestCase):
    """Verify the ``profile`` field on the Task dataclass."""

    def test_profile_field_exists(self):
        """Task dataclass has a ``profile`` attribute."""
        t = Task(task_id="T1", title="test", type="normal")
        self.assertTrue(hasattr(t, "profile"))

    def test_profile_defaults_to_none(self):
        """A Task created without profile has profile=None."""
        t = Task(task_id="T1", title="test", type="normal")
        self.assertIsNone(t.profile)

    def test_profile_in_to_dict(self):
        """to_dict() includes the profile key."""
        t = Task(task_id="T1", title="test", type="normal", profile="mini")
        d = t.to_dict()
        self.assertIn("profile", d)
        self.assertEqual(d["profile"], "mini")

    def test_profile_none_in_to_dict(self):
        """to_dict() includes profile=None for legacy tasks."""
        t = Task(task_id="T1", title="test", type="normal")
        d = t.to_dict()
        self.assertIn("profile", d)
        self.assertIsNone(d["profile"])

    def test_profile_round_trips_asdict(self):
        """asdict() includes the profile field."""
        t = Task(task_id="T1", title="test", type="normal", profile="edge")
        d = asdict(t)
        self.assertEqual(d["profile"], "edge")


# ===========================================================================
# 2. TaskManager.create() — profile persistence
# ===========================================================================


class ManagerCreateProfileTests(unittest.TestCase):
    """Verify manager.create() persists the profile field."""

    def setUp(self):
        self.m = TaskManager()

    def test_create_with_profile_mini(self):
        """manager.create(profile='mini') persists profile='mini'."""
        t = self.m.create(title="mini task", type="normal", input_text="x", profile="mini")
        self.assertEqual(t.profile, "mini")
        # Read back from DB
        fetched = self.m.get(t.task_id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.profile, "mini")

    def test_create_with_profile_full(self):
        """manager.create(profile='full') persists profile='full'."""
        t = self.m.create(title="full task", type="normal", input_text="x", profile="full")
        fetched = self.m.get(t.task_id)
        self.assertEqual(fetched.profile, "full")

    def test_create_with_profile_edge(self):
        """manager.create(profile='edge') persists profile='edge'."""
        t = self.m.create(title="edge task", type="normal", input_text="x", profile="edge")
        fetched = self.m.get(t.task_id)
        self.assertEqual(fetched.profile, "edge")

    def test_create_with_profile_developer(self):
        """manager.create(profile='developer') persists profile='developer'."""
        t = self.m.create(title="dev task", type="normal", input_text="x", profile="developer")
        fetched = self.m.get(t.task_id)
        self.assertEqual(fetched.profile, "developer")

    def test_create_without_profile_defaults_none(self):
        """manager.create() without profile kwarg persists None."""
        t = self.m.create(title="no profile", type="normal", input_text="x")
        fetched = self.m.get(t.task_id)
        self.assertIsNone(fetched.profile)

    def test_create_with_profile_none_explicit(self):
        """manager.create(profile=None) persists None."""
        t = self.m.create(title="explicit none", type="normal", input_text="x", profile=None)
        fetched = self.m.get(t.task_id)
        self.assertIsNone(fetched.profile)

    def test_create_with_empty_string_profile_normalised_to_none(self):
        """Empty string profile is normalised to None (wire-boundary)."""
        t = self.m.create(title="empty", type="normal", input_text="x", profile="")
        fetched = self.m.get(t.task_id)
        self.assertIsNone(fetched.profile)

    def test_create_with_whitespace_profile_normalised_to_none(self):
        """Whitespace-only profile is normalised to None."""
        t = self.m.create(title="ws", type="normal", input_text="x", profile="   ")
        fetched = self.m.get(t.task_id)
        self.assertIsNone(fetched.profile)

    def test_profile_survives_get_or_raise(self):
        """get_or_raise returns a Task with the correct profile."""
        t = self.m.create(title="survive", type="normal", input_text="x", profile="mini")
        fetched = self.m.get_or_raise(t.task_id)
        self.assertEqual(fetched.profile, "mini")


# ===========================================================================
# 3. Schema migration — idempotent, safe on existing DBs
# ===========================================================================


class SchemaMigrationTests(unittest.TestCase):
    """Verify the ``profile`` column migration is idempotent."""

    def test_profile_column_exists_after_init(self):
        """The tasks table has a ``profile`` column after _init_schema."""
        conn = _dbmod.get_conn()
        cols = [
            row["name"]
            for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        ]
        self.assertIn("profile", cols)

    def test_migration_idempotent_run_migrations_twice(self):
        """run_migrations() can be called twice without error."""
        _dbmod.run_migrations()
        _dbmod.run_migrations()
        conn = _dbmod.get_conn()
        cols = [
            row["name"]
            for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        ]
        self.assertIn("profile", cols)

    def test_migration_idempotent_init_schema_twice(self):
        """_init_schema can be called twice without error."""
        conn = _dbmod.get_conn()
        _dbmod._init_schema(conn)
        _dbmod._init_schema(conn)
        cols = [
            row["name"]
            for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        ]
        self.assertIn("profile", cols)

    def test_run_migrations_returns_empty_when_already_migrated(self):
        """run_migrations() returns [] when the column already exists."""
        _dbmod.run_migrations()  # ensure migrated
        added = _dbmod.run_migrations()  # second run
        # profile should NOT be in the added list (already present)
        self.assertNotIn("profile", added)

    def test_legacy_task_round_trips_with_none_profile(self):
        """A task created without profile round-trips with profile=None."""
        m = TaskManager()
        t = m.create(title="legacy", type="normal", input_text="x")
        fetched = m.get(t.task_id)
        self.assertIsNone(fetched.profile)
        # Verify the raw DB column is NULL
        conn = _dbmod.get_conn()
        row = conn.execute(
            "SELECT profile FROM tasks WHERE task_id = ?", (t.task_id,)
        ).fetchone()
        self.assertIsNone(row["profile"])


# ===========================================================================
# 4. Backward compatibility — existing calls unchanged
# ===========================================================================


class BackwardCompatTests(unittest.TestCase):
    """Verify pre-AEE-8.2 callers see no behavior change."""

    def setUp(self):
        self.m = TaskManager()

    def test_create_without_profile_kwarg_works(self):
        """Existing callers that don't pass profile still work."""
        t = self.m.create(title="compat", type="normal", input_text="x")
        self.assertIsNotNone(t.task_id)
        self.assertIsNone(t.profile)

    def test_to_dict_legacy_task_has_profile_none(self):
        """to_dict() for a legacy task has profile: None."""
        t = self.m.create(title="compat dict", type="normal", input_text="x")
        d = t.to_dict()
        self.assertIn("profile", d)
        self.assertIsNone(d["profile"])

    def test_existing_fields_unchanged_with_profile(self):
        """Setting profile does not corrupt other fields."""
        t = self.m.create(
            title="fields check",
            type="coding",
            input_text="x",
            profile="mini",
            priority=10,
            mode="coding",
        )
        fetched = self.m.get(t.task_id)
        self.assertEqual(fetched.title, "fields check")
        self.assertEqual(fetched.type, "coding")
        self.assertEqual(fetched.priority, 10)
        self.assertEqual(fetched.mode, "coding")
        self.assertEqual(fetched.profile, "mini")


# ===========================================================================
# 5. Isolation contract — safety.py NOT modified, descriptor isolation
# ===========================================================================


class IsolationContractTests(unittest.TestCase):
    """Verify AEE-8.2 does not touch safety.py or break descriptor isolation."""

    def test_safety_py_not_modified(self):
        """safety.py must NOT have a profile parameter (AEE-8.2 is storage-only)."""
        import ast
        safety_path = _ROOT / "dispatcher" / "safety.py"
        with open(safety_path, "r") as f:
            source = f.read()
        tree = ast.parse(source)
        # Walk the AST looking for any function with 'profile' in its args
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                arg_names = [a.arg for a in node.args.args]
                self.assertNotIn(
                    "profile",
                    arg_names,
                    f"safety.py function {node.name!r} has a 'profile' arg — "
                    "AEE-8.2 must NOT modify safety.py",
                )

    def test_descriptor_no_forbidden_imports(self):
        """aee.profiles.descriptor must not import dispatcher/sqlite3/subprocess."""
        import ast
        desc_path = _ROOT / "aee" / "profiles" / "descriptor.py"
        with open(desc_path, "r") as f:
            source = f.read()
        tree = ast.parse(source)
        forbidden = {"dispatcher", "sqlite3", "subprocess", "os"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(
                        alias.name.split(".")[0],
                        forbidden,
                        f"descriptor.py imports forbidden module {alias.name!r}",
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    self.assertNotIn(
                        node.module.split(".")[0],
                        forbidden,
                        f"descriptor.py imports from forbidden module {node.module!r}",
                    )


# ===========================================================================
# 6. Known profiles round-trip (full / mini / edge / developer)
# ===========================================================================


class KnownProfilesRoundTripTests(unittest.TestCase):
    """All four known profiles round-trip through create → get."""

    def setUp(self):
        self.m = TaskManager()

    def test_round_trip_full(self):
        t = self.m.create(title="rt-full", type="normal", input_text="x", profile="full")
        self.assertEqual(self.m.get(t.task_id).profile, "full")

    def test_round_trip_mini(self):
        t = self.m.create(title="rt-mini", type="normal", input_text="x", profile="mini")
        self.assertEqual(self.m.get(t.task_id).profile, "mini")

    def test_round_trip_edge(self):
        t = self.m.create(title="rt-edge", type="normal", input_text="x", profile="edge")
        self.assertEqual(self.m.get(t.task_id).profile, "edge")

    def test_round_trip_developer(self):
        t = self.m.create(title="rt-dev", type="normal", input_text="x", profile="developer")
        self.assertEqual(self.m.get(t.task_id).profile, "developer")


# ===========================================================================
# 7. Fresh DB schema test — profile column on a brand-new DB
# ===========================================================================


class FreshDBSchemaTests(unittest.TestCase):
    """Verify the profile column exists on a freshly created DB."""

    def test_profile_column_on_fresh_db(self):
        """A brand-new DB (via _init_schema) has the profile column."""
        import sqlite3
        tmp = Path(tempfile.mkdtemp(prefix="aee82-fresh-"))
        try:
            db_path = tmp / "fresh.db"
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            # Apply the production schema + migrations
            _dbmod._init_schema(conn)
            cols = [
                row["name"]
                for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
            ]
            self.assertIn("profile", cols)
            conn.close()
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
# 8. Existing DB migration test — simulates an old DB without profile column
# ===========================================================================


class ExistingDBMigrationTests(unittest.TestCase):
    """Verify migration is safe on an existing DB that lacks the profile column."""

    def test_migration_adds_profile_to_old_db(self):
        """An old DB (no profile column) gets it via _init_schema."""
        import sqlite3
        tmp = Path(tempfile.mkdtemp(prefix="aee82-old-"))
        try:
            db_path = tmp / "old.db"
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            # Create the base schema WITHOUT the profile column
            conn.executescript("""
                CREATE TABLE tasks (
                  task_id TEXT PRIMARY KEY,
                  title TEXT NOT NULL,
                  type TEXT NOT NULL,
                  priority INTEGER NOT NULL DEFAULT 50,
                  owner TEXT NOT NULL DEFAULT 'm2',
                  status TEXT NOT NULL,
                  progress_pct INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  input_text TEXT
                );
            """)
            conn.commit()
            # Insert a legacy row
            conn.execute(
                "INSERT INTO tasks (task_id, title, type, status, created_at, input_text) "
                "VALUES ('LEGACY-001', 'old', 'normal', 'completed', '2026-01-01T00:00:00Z', 'x')"
            )
            conn.commit()
            # Now apply _init_schema (which runs the AEE-8.2 migration)
            _dbmod._init_schema(conn)
            # The profile column should now exist
            cols = [
                row["name"]
                for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
            ]
            self.assertIn("profile", cols)
            # The legacy row should have profile=NULL
            row = conn.execute(
                "SELECT profile FROM tasks WHERE task_id = 'LEGACY-001'"
            ).fetchone()
            self.assertIsNone(row["profile"])
            conn.close()
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_migration_no_data_loss(self):
        """Migration does not lose existing data."""
        import sqlite3
        tmp = Path(tempfile.mkdtemp(prefix="aee82-noloss-"))
        try:
            db_path = tmp / "noloss.db"
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            conn.executescript("""
                CREATE TABLE tasks (
                  task_id TEXT PRIMARY KEY,
                  title TEXT NOT NULL,
                  type TEXT NOT NULL,
                  priority INTEGER NOT NULL DEFAULT 50,
                  owner TEXT NOT NULL DEFAULT 'm2',
                  status TEXT NOT NULL,
                  progress_pct INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  input_text TEXT
                );
            """)
            conn.execute(
                "INSERT INTO tasks (task_id, title, type, status, created_at, input_text, priority, owner, progress_pct) "
                "VALUES ('KEEP-001', 'important', 'coding', 'completed', '2026-01-01T00:00:00Z', 'valuable', 5, 'm2', 100)"
            )
            conn.commit()
            _dbmod._init_schema(conn)
            row = conn.execute(
                "SELECT title, type, priority, owner, status, progress_pct, input_text FROM tasks WHERE task_id = 'KEEP-001'"
            ).fetchone()
            self.assertEqual(row["title"], "important")
            self.assertEqual(row["type"], "coding")
            self.assertEqual(row["priority"], 5)
            self.assertEqual(row["owner"], "m2")
            self.assertEqual(row["status"], "completed")
            self.assertEqual(row["progress_pct"], 100)
            self.assertEqual(row["input_text"], "valuable")
            conn.close()
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
# 9. API serialization — profile in CreateRunRequest round-trips
# ===========================================================================


class APIProfileSerializationTests(unittest.TestCase):
    """Verify the profile field is serializable through the API layer.

    These tests verify the Pydantic model accepts and preserves the
    profile field. They do NOT start a live FastAPI server (that
    requires anyio/httpx which may not be installed).
    """

    def test_create_run_request_accepts_profile(self):
        """CreateRunRequest accepts profile='mini'."""
        try:
            from app import CreateRunRequest
        except ImportError:
            self.skipTest("app.py not importable (missing FastAPI/anyio)")
        req = CreateRunRequest(input="test", profile="mini")
        self.assertEqual(req.profile, "mini")

    def test_create_run_request_defaults_profile_none(self):
        """CreateRunRequest defaults profile to None."""
        try:
            from app import CreateRunRequest
        except ImportError:
            self.skipTest("app.py not importable (missing FastAPI/anyio)")
        req = CreateRunRequest(input="test")
        self.assertIsNone(req.profile)

    def test_create_run_request_rejects_unknown_profile(self):
        """CreateRunRequest rejects unknown profile values."""
        try:
            from app import CreateRunRequest
        except ImportError:
            self.skipTest("app.py not importable (missing FastAPI/anyio)")
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            CreateRunRequest(input="test", profile="bogus")

    def test_create_run_request_empty_string_profile_becomes_none(self):
        """Empty string profile is normalised to None by the validator."""
        try:
            from app import CreateRunRequest
        except ImportError:
            self.skipTest("app.py not importable (missing FastAPI/anyio)")
        req = CreateRunRequest(input="test", profile="")
        self.assertIsNone(req.profile)


if __name__ == "__main__":
    unittest.main()