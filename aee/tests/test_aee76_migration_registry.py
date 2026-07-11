"""AEE-7.6 — structured migration registry unit tests.

Locks down the contract added in AEE-7.6:

* The structured ``Migration`` dataclass is well-formed and
  enforces its invariants (``id``, ``sql``, ``owner_slice``,
  ``idempotency_evidence`` are all non-empty).
* The :data:`WRITE_SIDE_MIGRATIONS` list is the single source
  of truth for the 2-entry AEE-7.5 write-side metadata slice
  and survives a round-trip through
  :func:`as_legacy_tuple_list` byte-for-byte.
* :func:`validate_migrations` catches all the structural
  pitfalls the dispatcher cares about: duplicate ids,
  duplicate columns, multi-statement SQL, empty evidence,
  empty owner_slice.
* Idempotency evidence is non-empty for every entry, so
  the tripwire can rely on the registry as the canonical
  "how is this safe to re-run?" answer.
"""
from __future__ import annotations

import unittest
from typing import List

from dispatcher import migrations as m


class TestMigrationDataclassInvariants(unittest.TestCase):
    """The ``Migration`` dataclass refuses to construct with
    missing required fields."""

    def test_empty_id_rejected(self):
        with self.assertRaises(ValueError):
            m.Migration(
                id="",
                version="7.6",
                name="bad",
                target_table="tasks",
                sql="ALTER TABLE tasks ADD COLUMN x TEXT",
                owner_slice="AEE-7.6",
                column="x",
                idempotency_evidence="pragma_table_info check",
            )

    def test_empty_sql_rejected(self):
        with self.assertRaises(ValueError):
            m.Migration(
                id="aee7.6-bad-001",
                version="7.6",
                name="bad",
                target_table="tasks",
                sql="   ",
                owner_slice="AEE-7.6",
                column="x",
                idempotency_evidence="pragma_table_info check",
            )

    def test_empty_owner_slice_rejected(self):
        with self.assertRaises(ValueError):
            m.Migration(
                id="aee7.6-bad-001",
                version="7.6",
                name="bad",
                target_table="tasks",
                sql="ALTER TABLE tasks ADD COLUMN x TEXT",
                owner_slice="",
                column="x",
                idempotency_evidence="pragma_table_info check",
            )

    def test_empty_idempotency_evidence_rejected(self):
        with self.assertRaises(ValueError):
            m.Migration(
                id="aee7.6-bad-001",
                version="7.6",
                name="bad",
                target_table="tasks",
                sql="ALTER TABLE tasks ADD COLUMN x TEXT",
                owner_slice="AEE-7.6",
                column="x",
                idempotency_evidence="",
            )

    def test_minimal_valid_migration_constructs(self):
        mig = m.Migration(
            id="aee7.6-good-001",
            version="7.6",
            name="add tasks.aee76_test",
            target_table="tasks",
            sql="ALTER TABLE tasks ADD COLUMN aee76_test TEXT",
            owner_slice="AEE-7.6",
            column="aee76_test",
            idempotency_evidence="pragma_table_info check",
        )
        self.assertEqual(mig.id, "aee7.6-good-001")
        self.assertEqual(mig.column, "aee76_test")
        # Default nullability + default.
        self.assertTrue(mig.nullable)
        self.assertIsNone(mig.default)


class TestLegacyTupleShape(unittest.TestCase):
    """The structured list round-trips through the legacy
    ``[(col, sql), ...]`` shape byte-for-byte.
    """

    EXPECTED_LEGACY: List[tuple] = [
        (
            "executor_session_id",
            "ALTER TABLE tasks ADD COLUMN executor_session_id TEXT",
        ),
        (
            "runtime_run_id",
            "ALTER TABLE tasks ADD COLUMN runtime_run_id TEXT",
        ),
    ]

    def test_module_level_legacy_tuple_list(self):
        self.assertEqual(m.LEGACY_TUPLE_LIST, self.EXPECTED_LEGACY)

    def test_as_legacy_tuple_list_helper(self):
        self.assertEqual(
            m.as_legacy_tuple_list(m.WRITE_SIDE_MIGRATIONS),
            self.EXPECTED_LEGACY,
        )

    def test_db_legacy_list_matches_structured(self):
        # The dispatcher's apply code reads
        # ``dispatcher.db._AEE7_WRITE_SIDE_MIGRATIONS``. That
        # list is now derived from the structured registry,
        # so the two MUST stay in sync. A drift here would
        # silently bypass the registry (the dispatcher would
        # apply the old SQL while the registry says something
        # new).
        from dispatcher import db
        self.assertEqual(
            [list(t) for t in db._AEE7_WRITE_SIDE_MIGRATIONS],
            [list(t) for t in self.EXPECTED_LEGACY],
        )

    def test_table_level_migration_uses_synthetic_column_name(self):
        # A migration with column=None (table-level) produces
        # a 2-tuple whose first element is ``<table>-level``,
        # so the dispatcher's ``for col, stmt`` loop is
        # still well-formed.
        mig = m.Migration(
            id="aee7.6-table-001",
            version="7.6",
            name="create tasks_audit",
            target_table="tasks_audit",
            sql="CREATE TABLE IF NOT EXISTS tasks_audit (id INTEGER PRIMARY KEY)",
            owner_slice="AEE-7.6",
            column=None,
            idempotency_evidence="CREATE TABLE IF NOT EXISTS (sqlite-level)",
        )
        col, stmt = mig.as_legacy_tuple()
        self.assertEqual(col, "<tasks_audit-level>")
        self.assertIn("CREATE TABLE IF NOT EXISTS", stmt)


class TestValidationTripwire(unittest.TestCase):
    """``validate_migrations`` catches the structural pitfalls."""

    def test_module_level_list_validates_clean(self):
        # The module imports successfully only if this passes,
        # so this is a belt-and-braces assertion.
        self.assertEqual(m.validate_migrations(m.WRITE_SIDE_MIGRATIONS), [])

    def test_duplicate_id_caught(self):
        bad = list(m.WRITE_SIDE_MIGRATIONS) + [
            m.Migration(
                id="aee7.5-001",  # duplicate
                version="7.5",
                name="dup",
                target_table="tasks",
                sql="ALTER TABLE tasks ADD COLUMN dup TEXT",
                owner_slice="AEE-7.5",
                column="dup",
                idempotency_evidence="pragma_table_info check",
            )
        ]
        with self.assertRaises(ValueError) as cm:
            m.validate_migrations(bad)
        self.assertIn("duplicate migration id", str(cm.exception))

    def test_duplicate_column_in_same_table_caught(self):
        bad = list(m.WRITE_SIDE_MIGRATIONS) + [
            m.Migration(
                id="aee7.5-099",
                version="7.5",
                name="dup-column",
                target_table="tasks",
                sql="ALTER TABLE tasks ADD COLUMN executor_session_id TEXT",
                owner_slice="AEE-7.5",
                column="executor_session_id",  # duplicate column
                idempotency_evidence="pragma_table_info check",
            )
        ]
        with self.assertRaises(ValueError) as cm:
            m.validate_migrations(bad)
        self.assertIn("duplicate column", str(cm.exception))

    def test_multi_statement_sql_caught(self):
        bad = [
            m.Migration(
                id="aee7.6-multi-001",
                version="7.6",
                name="multi",
                target_table="tasks",
                sql="ALTER TABLE tasks ADD COLUMN a TEXT; DROP TABLE tasks",
                owner_slice="AEE-7.6",
                column="a",
                idempotency_evidence="pragma_table_info check",
            )
        ]
        with self.assertRaises(ValueError) as cm:
            m.validate_migrations(bad)
        self.assertIn("internal ';'", str(cm.exception))

    def test_trailing_semicolon_allowed(self):
        ok = [
            m.Migration(
                id="aee7.6-trailing-001",
                version="7.6",
                name="trailing",
                target_table="tasks",
                sql="ALTER TABLE tasks ADD COLUMN a TEXT;",
                owner_slice="AEE-7.6",
                column="a",
                idempotency_evidence="pragma_table_info check",
            )
        ]
        # No raise expected.
        self.assertEqual(m.validate_migrations(ok), [])

    def test_raise_false_returns_list(self):
        # Build a migration that has a valid id (so __post_init__
        # accepts it) but is otherwise structurally broken in a way
        # that only ``validate_migrations`` catches — e.g. duplicate
        # column within the same table.
        bad = list(m.WRITE_SIDE_MIGRATIONS) + [
            m.Migration(
                id="aee7.5-099",
                version="7.5",
                name="dup-column",
                target_table="tasks",
                sql="ALTER TABLE tasks ADD COLUMN executor_session_id TEXT",
                owner_slice="AEE-7.5",
                column="executor_session_id",  # duplicate column
                idempotency_evidence="pragma_table_info check",
            )
        ]
        # raise_on_error=False: return the error list.
        errors = m.validate_migrations(bad, raise_on_error=False)
        self.assertGreater(len(errors), 0)
        # The first error mentions the duplicate column.
        self.assertTrue(any("duplicate column" in e for e in errors))


class TestFingerprints(unittest.TestCase):
    """The ``fingerprint()`` helper is deterministic."""

    def test_same_migration_same_fingerprint(self):
        m1 = m.WRITE_SIDE_MIGRATIONS[0]
        m2 = m.WRITE_SIDE_MIGRATIONS[0]
        self.assertEqual(m1.fingerprint(), m2.fingerprint())

    def test_different_sql_different_fingerprint(self):
        a = m.Migration(
            id="aee7.6-fp-001",
            version="7.6",
            name="a",
            target_table="tasks",
            sql="ALTER TABLE tasks ADD COLUMN a TEXT",
            owner_slice="AEE-7.6",
            column="a",
            idempotency_evidence="x",
        )
        b = m.Migration(
            id="aee7.6-fp-001",
            version="7.6",
            name="a",
            target_table="tasks",
            sql="ALTER TABLE tasks ADD COLUMN b TEXT",  # different sql
            owner_slice="AEE-7.6",
            column="a",
            idempotency_evidence="x",
        )
        self.assertNotEqual(a.fingerprint(), b.fingerprint())

    def test_fingerprint_is_hex_16(self):
        fp = m.WRITE_SIDE_MIGRATIONS[0].fingerprint()
        self.assertEqual(len(fp), 16)
        int(fp, 16)  # raises if not valid hex


class TestIdempotencyEvidence(unittest.TestCase):
    """Every entry in :data:`WRITE_SIDE_MIGRATIONS` carries a
    non-empty idempotency_evidence string (the tripwire
    contract).
    """

    def test_all_entries_have_evidence(self):
        for mig in m.WRITE_SIDE_MIGRATIONS:
            self.assertTrue(
                mig.idempotency_evidence,
                f"migration {mig.id!r} has empty idempotency_evidence",
            )
            self.assertIn("pragma_table_info", mig.idempotency_evidence)

    def test_columns_appear_in_idempotency_evidence(self):
        for mig in m.WRITE_SIDE_MIGRATIONS:
            self.assertIn(mig.column, mig.idempotency_evidence)


class TestOrdering(unittest.TestCase):
    """Application order is registration order — verify
    that the structured list is in the same order the
    dispatcher will apply it.
    """

    def test_write_side_order_matches_db_order(self):
        # 001 = executor_session_id, 002 = runtime_run_id.
        # The dispatcher applies in this order on a fresh DB.
        self.assertEqual(
            [x.id for x in m.WRITE_SIDE_MIGRATIONS],
            ["aee7.5-001", "aee7.5-002"],
        )
        self.assertEqual(
            [x.column for x in m.WRITE_SIDE_MIGRATIONS],
            ["executor_session_id", "runtime_run_id"],
        )


if __name__ == "__main__":
    unittest.main()
