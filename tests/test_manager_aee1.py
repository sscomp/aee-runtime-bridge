"""AEE-1 / AEE-2 manager-level tests.

These tests assert that `dispatcher.manager.TaskManager` and the
`dispatcher.models.Task` dataclass round-trip the AEE-1 fields
(`runtime_type`, `adapter_name`, `external_run_id`, `worker_id`,
`heartbeat_at`, `claim_token_hash`, `approval_required`,
`approval_state`) correctly through the SQLite store.

Why this file exists separately from `test_migration_aee1.py`:
the migration tests confirm the schema is there; this file
confirms the *manager* uses the schema. If a future change
drops AEE-1 columns from the row loader, `test_jobs_api.py` may
still pass on a clean DB (where columns are NULL anyway) but
`test_manager_aee1.py` will catch the regression.

Each test uses an isolated tmpdir-backed DB.
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("BRIDGE_API_KEY", "test-key")
os.environ.setdefault("DISPATCHER_API_KEY", "test-key")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMPDIR = tempfile.mkdtemp(prefix="aee2-manager-test-")

from dispatcher import db  # noqa: E402
from dispatcher import manager as mgr  # noqa: E402

# Override to tmpdir BEFORE any DB call.
db.DB_DIR = Path(_TMPDIR)
db.DB_PATH = db.DB_DIR / "dispatcher.db"
mgr.LOGS_DIR = Path(_TMPDIR) / "logs"
mgr.REPORTS_DIR = Path(_TMPDIR) / "reports"


def _fresh_db() -> None:
    db._local.conn = None
    db._initialized = False
    if db.DB_PATH.exists():
        try:
            db.DB_PATH.unlink()
        except OSError:
            pass


class TestManagerAEE1(unittest.TestCase):

    def setUp(self) -> None:
        _fresh_db()
        self.tm = mgr.TaskManager()

    def test_create_then_get_round_trips_aee1_fields(self):
        """The simplest end-to-end: create a task, then load it back
        and confirm the AEE-1 fields are still there (not silently
        dropped by the row loader)."""
        task = self.tm.create(
            title="t",
            type="ops",
            input_text="hi",
        )
        # The dispatcher's default AEE-1 fields are set by
        # migration DEFAULT clauses. Round-trip via .get().
        loaded = self.tm.get(task.task_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.runtime_type, "hermes")
        self.assertEqual(loaded.adapter_name, "hermes")
        self.assertIsNone(loaded.external_run_id)
        self.assertIsNone(loaded.worker_id)
        self.assertIsNone(loaded.heartbeat_at)
        self.assertIsNone(loaded.claim_token_hash)
        self.assertFalse(loaded.approval_required)
        self.assertEqual(loaded.approval_state, "not_required")

    def test_claim_job_writes_aee1_fields(self):
        """When `db.claim_job` runs, the AEE-1 fields on the
        resulting row should be set to the worker's values."""
        task = self.tm.create(title="t", type="ops", input_text="hi")
        # Manually mark approval_required=0 (default) — leave as is.
        # Simulate the AEE-2 claim path: call `db.claim_job` directly
        # with a token hash.
        token = "tok"
        h = hashlib.sha256(token.encode()).hexdigest()
        ok = db.claim_job(
            task_id=task.task_id,
            worker_id="w-1",
            claim_token_hash=h,
        )
        self.assertTrue(ok)
        loaded = self.tm.get(task.task_id)
        self.assertEqual(loaded.status, "running")
        self.assertEqual(loaded.worker_id, "w-1")
        self.assertIsNotNone(loaded.heartbeat_at)
        self.assertEqual(loaded.claim_token_hash, h)
        # Token can be verified.
        self.assertTrue(db.verify_claim_token(task.task_id, h))
        self.assertFalse(db.verify_claim_token(task.task_id, "wrong"))
        self.assertFalse(db.verify_claim_token(task.task_id, h.replace("a", "b")))

    def test_find_by_external_run_id_works(self):
        task = self.tm.create(title="t", type="ops", input_text="hi")
        with db.transaction() as conn2:
            conn2.execute(
                "UPDATE tasks SET external_run_id = ? WHERE task_id = ?",
                ("RUN-XYZ", task.task_id),
            )
        loaded_row = db.find_by_external_run_id("RUN-XYZ")
        self.assertIsNotNone(loaded_row)
        self.assertEqual(loaded_row["task_id"], task.task_id)
        # Task dataclass loader also picks it up.
        loaded = self.tm.get(task.task_id)
        self.assertEqual(loaded.external_run_id, "RUN-XYZ")

    def test_backfill_external_run_id_idempotent(self):
        # Two legacy tasks with hermes_run_id but no external_run_id.
        a = self.tm.create(title="a", type="ops", input_text="x")
        b = self.tm.create(title="b", type="ops", input_text="y")
        with db.transaction() as conn2:
            conn2.execute(
                "UPDATE tasks SET hermes_run_id = 'H-A' WHERE task_id = ?",
                (a.task_id,),
            )
            conn2.execute(
                "UPDATE tasks SET hermes_run_id = 'H-B' WHERE task_id = ?",
                (b.task_id,),
            )
        # First backfill: 2 rows updated.
        n1 = db.backfill_external_run_id()
        self.assertEqual(n1, 2)
        # Second backfill: 0 rows updated (idempotent).
        n2 = db.backfill_external_run_id()
        self.assertEqual(n2, 0)
        # Both rows have external_run_id set.
        self.assertEqual(self.tm.get(a.task_id).external_run_id, "H-A")
        self.assertEqual(self.tm.get(b.task_id).external_run_id, "H-B")

    def test_workers_table_persists_capabilities(self):
        rec = db.upsert_worker(
            worker_id="w-1",
            worker_name="w-1",
            worker_type="pi_agent",
            hostname="m2",
            capabilities=["shell", "python"],
            workdir_allowlist=["/tmp"],
            max_concurrent=2,
        )
        self.assertEqual(rec["worker_id"], "w-1")
        self.assertTrue(rec["registered"])
        # get_worker returns JSON-decoded lists. AEE-3: the
        # repository normalizes the worker's capabilities
        # (lowercase, trimmed, deduped, sorted), so the loaded
        # list is the canonical form, not the input order.
        loaded = db.get_worker("w-1")
        self.assertEqual(loaded["capabilities"], ["python", "shell"])
        self.assertEqual(loaded["workdir_allowlist"], ["/tmp"])
        self.assertEqual(loaded["max_concurrent"], 2)
        # Re-registering with different capabilities updates but
        # preserves registered_at.
        first_at = loaded["registered_at"]
        db.upsert_worker(
            worker_id="w-1",
            worker_name="w-1",
            worker_type="pi_agent",
            hostname="m2",
            capabilities=["shell"],
            workdir_allowlist=["/tmp"],
            max_concurrent=1,
        )
        reloaded = db.get_worker("w-1")
        self.assertEqual(reloaded["registered_at"], first_at)
        self.assertEqual(reloaded["capabilities"], ["shell"])
        self.assertEqual(reloaded["max_concurrent"], 1)

    def test_find_claimable_job_filters_by_adapter_name(self):
        # Create three jobs with different adapter_name values.
        a = self.tm.create(title="a", type="ops", input_text="x")
        b = self.tm.create(title="b", type="ops", input_text="x")
        c = self.tm.create(title="c", type="ops", input_text="x")
        with db.transaction() as conn2:
            conn2.execute(
                "UPDATE tasks SET adapter_name = 'pi_agent' WHERE task_id IN (?, ?)",
                (a.task_id, b.task_id),
            )
            conn2.execute(
                "UPDATE tasks SET adapter_name = 'claude_code' WHERE task_id = ?",
                (c.task_id,),
            )
        # A pi_agent worker should see a and b, not c.
        for _ in range(2):
            j = db.find_claimable_job(worker_type="pi_agent", capabilities=[])
            self.assertIsNotNone(j)
            # Claim it and re-list.
            db.claim_job(
                task_id=j["task_id"],
                worker_id="w-1",
                claim_token_hash="x" * 64,
            )
        # Third call: nothing left.
        j = db.find_claimable_job(worker_type="pi_agent", capabilities=[])
        self.assertIsNone(j)
        # A claude_code worker should still see c.
        j2 = db.find_claimable_job(worker_type="claude_code", capabilities=[])
        self.assertIsNotNone(j2)
        self.assertEqual(j2["task_id"], c.task_id)

    def test_task_to_dict_includes_aee1_fields(self):
        task = self.tm.create(title="t", type="ops", input_text="hi")
        d = task.to_dict()
        for f in (
            "runtime_type", "adapter_name", "external_run_id", "worker_id",
            "heartbeat_at", "claim_token_hash", "approval_required", "approval_state",
        ):
            self.assertIn(f, d, f"Task.to_dict() missing AEE-1 field: {f}")


if __name__ == "__main__":
    unittest.main()
