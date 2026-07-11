"""AEE-7.6 — migration idempotency, ordering, and duplicate detection tests.

These tests run the dispatcher's actual migration apply code
(``db._init_schema``) against fresh in-memory SQLite databases,
asserting the structured registry behaves correctly under
realistic conditions:

* Idempotency: calling ``_init_schema`` twice does NOT raise
  because the apply path is conditional on
  ``pragma_table_info`` (the dispatcher's existing
  idempotency pattern).
* Ordering: WRITE_SIDE_MIGRATIONS applies in the order
  declared, which matches the legacy tuple list in
  ``db._AEE7_WRITE_SIDE_MIGRATIONS``. Drift between the
  two would break existing deployed DBs.
* Duplicate detection: ``validate_migrations`` catches
  duplicate ids, duplicate column-in-table, and
  multi-statement SQL.
* Fingerprint stability: ``Migration.fingerprint()`` returns
  a stable sha256 hex so future audit scripts can detect
  "the registry didn't change between two commits".
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dispatcher import db  # noqa: E402
from dispatcher import migrations  # noqa: E402


def _make_minimal_tasks_db() -> sqlite3.Connection:
    """Build a fresh in-memory SQLite with the dispatcher's
    base tasks table (no AEE-7.5 columns yet)."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(db._SCHEMA)
    return conn


class TestIdempotency(unittest.TestCase):
    """Applying the schema twice must NOT raise."""

    def test_init_schema_twice_does_not_raise(self):
        conn = _make_minimal_tasks_db()
        # First pass: AEE-7.5 columns added.
        db._init_schema(conn)
        # Second pass: the apply path is conditional
        # (pragma_table_info), so re-running is a no-op.
        db._init_schema(conn)
        # Both columns present exactly once.
        cols = [
            r[1] for r in conn.execute(
                "SELECT cid, name FROM pragma_table_info('tasks')"
            ).fetchall()
        ]
        self.assertEqual(cols.count("executor_session_id"), 1)
        self.assertEqual(cols.count("runtime_run_id"), 1)

    def test_init_schema_on_already_migrated_db_is_noop(self):
        """If the DB already has the columns (e.g. live DB
        after AEE-7.5 G2 ship), the apply pass is a no-op
        — no ALTER TABLE errors, no duplicate columns.
        """
        conn = _make_minimal_tasks_db()
        # Pre-add the columns (mimicking a DB that already
        # received the migration).
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN executor_session_id TEXT"
        )
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN runtime_run_id TEXT"
        )
        # Re-running init_schema must not raise.
        db._init_schema(conn)
        # Still exactly one of each column.
        cols = [
            r[1] for r in conn.execute(
                "SELECT cid, name FROM pragma_table_info('tasks')"
            ).fetchall()
        ]
        self.assertEqual(cols.count("executor_session_id"), 1)
        self.assertEqual(cols.count("runtime_run_id"), 1)


class TestOrdering(unittest.TestCase):
    """WRITE_SIDE_MIGRATIONS order matches the legacy
    ``_AEE7_WRITE_SIDE_MIGRATIONS`` tuple list. Drift would
    break existing deployed DBs that received the
    migrations in the original order."""

    def test_structured_and_legacy_derive_same_order(self):
        legacy = db._AEE7_WRITE_SIDE_MIGRATIONS
        # legacy is already derived from structured
        # (per db.py line 189-193) at import time. Re-derive
        # here to assert idempotence.
        derived = migrations.as_legacy_tuple_list(
            migrations.WRITE_SIDE_MIGRATIONS,
        )
        # Same column order.
        self.assertEqual(
            [c for c, _ in legacy],
            [c for c, _ in derived],
        )
        # Same SQL.
        for (_, sql_legacy), (_, sql_derived) in zip(legacy, derived):
            self.assertEqual(sql_legacy, sql_derived)

    def test_structured_order_matches_g1_g2_ship(self):
        """G1 added ``executor_session_id``; G2 added
        ``runtime_run_id``. The structured list must keep
        that order so a fresh DB applies the G1 column
        before the G2 column.
        """
        cols = [m.column for m in migrations.WRITE_SIDE_MIGRATIONS]
        self.assertEqual(
            cols,
            ["executor_session_id", "runtime_run_id"],
        )

    def test_structured_sql_matches_legacy_sql(self):
        """The structured SQL and the legacy SQL are
        byte-for-byte equal — the refactor didn't change
        the actual ALTER statement.
        """
        for m, (col, sql) in zip(
            migrations.WRITE_SIDE_MIGRATIONS,
            db._AEE7_WRITE_SIDE_MIGRATIONS,
        ):
            self.assertEqual(m.column, col)
            self.assertEqual(m.sql, sql)


class TestDuplicateDetection(unittest.TestCase):
    """``validate_migrations`` catches the obvious foot-guns."""

    def test_duplicate_id_raises(self):
        from dispatcher.migrations import (
            Migration,
            validate_migrations,
        )
        bad = [
            Migration(
                id="dup", version="1", name="x",
                sql="ALTER TABLE tasks ADD COLUMN foo TEXT",
                target_table="tasks", column="foo",
                owner_slice="aee-7.6",
                idempotency_evidence="pragma_table_info",
            ),
            Migration(
                id="dup", version="1", name="y",
                sql="ALTER TABLE tasks ADD COLUMN bar TEXT",
                target_table="tasks", column="bar",
                owner_slice="aee-7.6",
                idempotency_evidence="pragma_table_info",
            ),
        ]
        with self.assertRaises(ValueError) as cm:
            validate_migrations(bad, raise_on_error=True)
        self.assertIn("duplicate migration id", str(cm.exception).lower())

    def test_duplicate_column_in_table_raises(self):
        from dispatcher.migrations import (
            Migration,
            validate_migrations,
        )
        bad = [
            Migration(
                id="a", version="1", name="x",
                sql="ALTER TABLE tasks ADD COLUMN foo TEXT",
                target_table="tasks", column="foo",
                owner_slice="aee-7.6",
                idempotency_evidence="pragma_table_info",
            ),
            Migration(
                id="b", version="1", name="y",
                sql="ALTER TABLE tasks ADD COLUMN foo TEXT",
                target_table="tasks", column="foo",
                owner_slice="aee-7.6",
                idempotency_evidence="pragma_table_info",
            ),
        ]
        with self.assertRaises(ValueError) as cm:
            validate_migrations(bad, raise_on_error=True)
        self.assertIn("duplicate column", str(cm.exception).lower())

    def test_multistatement_sql_rejected(self):
        from dispatcher.migrations import (
            Migration,
            validate_migrations,
        )
        bad = [
            Migration(
                id="multi", version="1", name="x",
                sql=(
                    "ALTER TABLE tasks ADD COLUMN foo TEXT; "
                    "DROP TABLE tasks"
                ),
                target_table="tasks", column="foo",
                owner_slice="aee-7.6",
                idempotency_evidence="pragma_table_info",
            ),
        ]
        with self.assertRaises(ValueError) as cm:
            validate_migrations(bad, raise_on_error=True)
        self.assertIn("single statement", str(cm.exception).lower())

    def test_validate_module_list_clean(self):
        """The module-level WRITE_SIDE_MIGRATIONS list is
        already valid — validate should not raise."""
        migrations.validate_migrations(
            migrations.WRITE_SIDE_MIGRATIONS, raise_on_error=True,
        )


class TestFingerprint(unittest.TestCase):
    """The structured list has a stable fingerprint so
    audit scripts can detect 'registry changed' between
    two commits without diffing Python source."""

    def test_fingerprint_is_stable_hex(self):
        fp = migrations.WRITE_SIDE_MIGRATIONS[0].fingerprint()
        # 16 hex chars (sha256[:8], sufficient for tripwire).
        self.assertEqual(len(fp), 16)
        int(fp, 16)  # raises if not hex

    def test_fingerprint_stable_across_calls(self):
        a = migrations.WRITE_SIDE_MIGRATIONS[0].fingerprint()
        b = migrations.WRITE_SIDE_MIGRATIONS[0].fingerprint()
        self.assertEqual(a, b)

    def test_two_migrations_have_different_fingerprints(self):
        a = migrations.WRITE_SIDE_MIGRATIONS[0].fingerprint()
        b = migrations.WRITE_SIDE_MIGRATIONS[1].fingerprint()
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
