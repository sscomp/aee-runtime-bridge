"""AEE-5 Repository — unit tests.

Tests the in-memory repository (used as a stand-in
for the SQLite one in unit tests) and the SQLite
repository's idempotent schema migration.
"""
from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from aee.runtimes.models import (
    DispatchRecord,
    RuntimeCapabilities,
    RuntimeDescriptor,
    RuntimeHealth,
    RuntimeLimits,
)
from aee.runtimes.repository import (
    InMemoryRuntimeRepository,
    SqliteRuntimeRepository,
    ensure_aee5_schema,
)


def _mk(runtime_id: str = "r1", **kwargs) -> RuntimeDescriptor:
    base = dict(
        runtime_id=runtime_id,
        runtime_type="aee_lightweight",
        display_name=runtime_id,
        capabilities=RuntimeCapabilities(["task.shell"]),
        limits=RuntimeLimits(),
        health=RuntimeHealth(),
    )
    base.update(kwargs)
    return RuntimeDescriptor(**base)


class TestInMemoryRepository(unittest.TestCase):
    def setUp(self):
        self.repo = InMemoryRuntimeRepository()

    def test_insert_and_get(self):
        self.repo.insert(_mk("r1"))
        d = self.repo.get("r1")
        self.assertIsNotNone(d)
        self.assertEqual(d.runtime_id, "r1")

    def test_duplicate_insert_raises(self):
        self.repo.insert(_mk("r1"))
        with self.assertRaises(KeyError):
            self.repo.insert(_mk("r1"))

    def test_update_replaces(self):
        self.repo.insert(_mk("r1"))
        d = _mk("r1", display_name="updated")
        self.repo.update(d)
        self.assertEqual(self.repo.get("r1").display_name, "updated")

    def test_delete(self):
        self.repo.insert(_mk("r1"))
        self.assertTrue(self.repo.delete("r1"))
        self.assertIsNone(self.repo.get("r1"))

    def test_list_filters(self):
        self.repo.insert(_mk("r1", enabled=True, runtime_type="aee_lightweight"))
        self.repo.insert(_mk("r2", enabled=False, runtime_type="shell"))
        self.repo.insert(_mk("r3", enabled=True, runtime_type="shell"))
        self.assertEqual(len(self.repo.list_all()), 3)
        self.assertEqual(len(self.repo.list_all(enabled=True)), 2)
        self.assertEqual(len(self.repo.list_all(runtime_type="shell")), 2)
        self.assertEqual(len(self.repo.list_all(enabled=True, runtime_type="shell")), 1)

    def test_set_enabled(self):
        self.repo.insert(_mk("r1"))
        self.repo.set_enabled("r1", False)
        self.assertFalse(self.repo.get("r1").enabled)
        self.repo.set_enabled("r1", True)
        self.assertTrue(self.repo.get("r1").enabled)

    def test_set_enabled_missing(self):
        self.assertFalse(self.repo.set_enabled("missing", False))

    def test_update_health(self):
        self.repo.insert(_mk("r1"))
        self.repo.update_health("r1", "healthy", message="ok")
        d = self.repo.get("r1")
        self.assertEqual(d.health.status, "healthy")
        self.assertEqual(d.health.message, "ok")
        self.assertIsNotNone(d.health.last_checked_at)

    def test_dispatch_record_crud(self):
        rec = DispatchRecord(
            dispatch_id="d1",
            task_id="t1",
            run_id="r1",
            selected_runtime_id="r1",
            selected_runtime_type="aee_lightweight",
        )
        self.repo.insert_dispatch_record(rec)
        out = self.repo.list_dispatch_records(task_id="t1")
        self.assertEqual(len(out), 1)
        self.repo.update_dispatch_status("d1", "dispatched")
        out = self.repo.list_dispatch_records(task_id="t1")
        self.assertEqual(out[0].dispatch_status, "dispatched")
        self.assertEqual(self.repo.count_dispatch_records(), 1)


class TestSqliteRepository(unittest.TestCase):
    """Test the SQLite repository against an in-memory
    SQLite connection (so tests don't touch the real DB)."""

    def setUp(self):
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        ensure_aee5_schema(self._conn)
        self.repo = SqliteRuntimeRepository(conn_factory=lambda: self._conn)

    def test_insert_and_get(self):
        d = _mk("r1", display_name="Test")
        self.repo.insert(d)
        out = self.repo.get("r1")
        self.assertIsNotNone(out)
        self.assertEqual(out.display_name, "Test")
        self.assertEqual(out.capabilities.to_list(), ["task.shell"])

    def test_insert_duplicate_raises(self):
        self.repo.insert(_mk("r1"))
        with self.assertRaises(KeyError):
            self.repo.insert(_mk("r1"))

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.repo.get("missing"))

    def test_update_replaces(self):
        self.repo.insert(_mk("r1"))
        d = _mk("r1", display_name="updated")
        self.repo.update(d)
        self.assertEqual(self.repo.get("r1").display_name, "updated")

    def test_update_missing_raises(self):
        with self.assertRaises(KeyError):
            self.repo.update(_mk("missing"))

    def test_delete(self):
        self.repo.insert(_mk("r1"))
        self.assertTrue(self.repo.delete("r1"))
        self.assertIsNone(self.repo.get("r1"))
        # Re-delete returns False.
        self.assertFalse(self.repo.delete("r1"))

    def test_list_filters(self):
        self.repo.insert(_mk("r1", enabled=True, runtime_type="aee_lightweight"))
        self.repo.insert(_mk("r2", enabled=False, runtime_type="shell"))
        self.repo.insert(_mk("r3", enabled=True, runtime_type="shell"))
        self.assertEqual(len(self.repo.list_all()), 3)
        self.assertEqual(len(self.repo.list_all(enabled=True)), 2)
        self.assertEqual(len(self.repo.list_all(runtime_type="shell")), 2)
        self.assertEqual(len(self.repo.list_all(enabled=True, runtime_type="shell")), 1)

    def test_set_enabled(self):
        self.repo.insert(_mk("r1"))
        self.repo.set_enabled("r1", False)
        self.assertFalse(self.repo.get("r1").enabled)
        self.repo.set_enabled("r1", True)
        self.assertTrue(self.repo.get("r1").enabled)

    def test_update_health(self):
        self.repo.insert(_mk("r1"))
        self.repo.update_health("r1", "healthy", message="ok")
        d = self.repo.get("r1")
        self.assertEqual(d.health.status, "healthy")
        self.assertEqual(d.health.message, "ok")

    def test_update_health_missing_returns_false(self):
        self.assertFalse(self.repo.update_health("missing", "healthy"))

    def test_dispatch_record_crud(self):
        rec = DispatchRecord(
            dispatch_id="d1",
            task_id="t1",
            run_id="r1",
            selected_runtime_id="r1",
            selected_runtime_type="aee_lightweight",
            rejected_reasons={"rt2": ["missing cap"]},
        )
        self.repo.insert_dispatch_record(rec)
        out = self.repo.list_dispatch_records(task_id="t1")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].rejected_reasons, {"rt2": ["missing cap"]})
        # First status update — succeeds.
        self.assertTrue(
            self.repo.update_dispatch_status("d1", "dispatched")
        )
        # Update with a missing dispatch_id should fail.
        self.assertFalse(
            self.repo.update_dispatch_status("does-not-exist", "dispatched")
        )
        self.assertEqual(
            self.repo.list_dispatch_records(task_id="t1")[0].dispatch_status,
            "dispatched",
        )
        self.assertEqual(self.repo.count_dispatch_records(), 1)

    def test_idempotent_migration(self):
        # Re-run the migration on the same conn; should be a no-op.
        ensure_aee5_schema(self._conn)
        row = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='runtimes'"
        ).fetchone()
        self.assertIsNotNone(row)
        row2 = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='dispatch_records'"
        ).fetchone()
        self.assertIsNotNone(row2)


if __name__ == "__main__":
    unittest.main()
