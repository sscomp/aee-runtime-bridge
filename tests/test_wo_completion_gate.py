"""WO-COMPLETION-GATE-MVP tests.

Covers the deterministic completion gate for explicitly-declared
`expected_artifacts`. The gate is a HARD gate: if the caller declares
artifact paths at create() time and any are missing on disk at
complete() time, the task transitions to `failed` (NOT `completed`)
with `error_message` containing the `missing_expected_artifacts`
reason prefix. Empty list / None = no contract → existing behavior.

Test matrix:
  1. No `expected_artifacts` kwarg → existing behavior, `completed`.
  2. Empty `expected_artifacts=[]` → existing behavior, `completed`.
  3. All declared artifacts present → `completed`.
  4. Some declared artifacts missing → `failed` with reason.
  5. All declared artifacts missing → `failed` with reason.
  6. Legacy NULL row (no column) → existing behavior, `completed`.
  7. Error message contains the missing path(s) (deterministic content).
  8. `expected_artifacts` is persisted and round-trips via get().
  9. DELIVERY_UNVERIFIED event is emitted on gate failure with the
     `gate=missing_expected_artifacts` payload.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from dispatcher.db import get_conn
from dispatcher.manager import TaskManager


class TestCompletionGate(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="wo-gate-")
        self.existing = Path(self.tmpdir) / "exists.md"
        self.existing.write_text("hello", encoding="utf-8")
        self.missing = Path(self.tmpdir) / "does_not_exist.md"
        self.missing2 = Path(self.tmpdir) / "also_missing.md"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _create_start_complete(
        self,
        *,
        title: str,
        expected_artifacts=None,
        output_text: str = "done",
    ):
        m = TaskManager()
        t = m.create(
            title=title,
            type="research",
            input_text="placeholder input",
            expected_artifacts=expected_artifacts,
        )
        m.start(t.task_id, hermes_run_id="test-gate-run")
        out = m.complete(t.task_id, output_text=output_text)
        return m, t, out

    def test_no_kwargs_existing_behavior(self):
        """No expected_artifacts kwarg → existing behavior, completed."""
        m, t, out = self._create_start_complete(title="wo-no-kwarg")
        self.assertEqual(out.status, "completed")
        self.assertEqual(out.expected_artifacts, [])

    def test_empty_list_existing_behavior(self):
        """Empty expected_artifacts=[] → no contract → completed."""
        m, t, out = self._create_start_complete(
            title="wo-empty-list", expected_artifacts=[]
        )
        self.assertEqual(out.status, "completed")
        self.assertEqual(out.expected_artifacts, [])

    def test_all_artifacts_present_completes(self):
        """All declared artifacts present → completed."""
        m, t, out = self._create_start_complete(
            title="wo-all-present",
            expected_artifacts=[str(self.existing)],
        )
        self.assertEqual(out.status, "completed")
        self.assertEqual(out.expected_artifacts, [str(self.existing)])

    def test_some_missing_fails(self):
        """Some declared artifacts missing → failed with reason."""
        m, t, out = self._create_start_complete(
            title="wo-some-missing",
            expected_artifacts=[str(self.existing), str(self.missing)],
        )
        self.assertEqual(out.status, "failed")
        self.assertIn("missing_expected_artifacts", out.error_message or "")
        self.assertIn(str(self.missing), out.error_message or "")

    def test_all_missing_fails(self):
        """All declared artifacts missing → failed with reason."""
        m, t, out = self._create_start_complete(
            title="wo-all-missing",
            expected_artifacts=[str(self.missing), str(self.missing2)],
        )
        self.assertEqual(out.status, "failed")
        self.assertIn("missing_expected_artifacts", out.error_message or "")
        self.assertIn(str(self.missing), out.error_message or "")
        self.assertIn(str(self.missing2), out.error_message or "")

    def test_legacy_null_row_completes(self):
        """Legacy NULL row (no expected_artifacts_json) → completed."""
        # Insert a task with NULL expected_artifacts_json by creating
        # one without the kwarg, then completing it directly. The
        # gate reads the column as NULL → empty list → no gate.
        m = TaskManager()
        t = m.create(title="wo-legacy", type="research", input_text="x")
        m.start(t.task_id, hermes_run_id="legacy-run")
        # Force-set the column to NULL to simulate a legacy row.
        conn = get_conn()
        conn.execute(
            "UPDATE tasks SET expected_artifacts_json = NULL WHERE task_id = ?",
            (t.task_id,),
        )
        conn.commit()
        out = m.complete(t.task_id, output_text="done")
        self.assertEqual(out.status, "completed")

    def test_error_message_contains_missing_paths(self):
        """Error message is deterministic and contains missing paths."""
        m, t, out = self._create_start_complete(
            title="wo-error-content",
            expected_artifacts=[str(self.missing)],
        )
        self.assertEqual(out.status, "failed")
        # Deterministic prefix — orchestrator can pattern-match.
        self.assertTrue(
            (out.error_message or "").startswith("missing_expected_artifacts:")
        )
        # Contains the declared count and missing count.
        self.assertIn("1 of 1", out.error_message or "")
        self.assertIn(str(self.missing), out.error_message or "")

    def test_expected_artifacts_round_trip(self):
        """expected_artifacts is persisted and round-trips via get()."""
        m = TaskManager()
        t = m.create(
            title="wo-roundtrip",
            type="research",
            input_text="x",
            expected_artifacts=[str(self.existing), str(self.missing)],
        )
        # Round-trip: should be sorted (deduped) and persisted.
        fetched = m.get(t.task_id)
        self.assertEqual(
            fetched.expected_artifacts,
            sorted([str(self.existing), str(self.missing)]),
        )

    def test_delivery_unverified_event_emitted_on_gate_failure(self):
        """DELIVERY_UNVERIFIED event with gate payload is emitted."""
        m, t, out = self._create_start_complete(
            title="wo-event",
            expected_artifacts=[str(self.missing)],
        )
        self.assertEqual(out.status, "failed")
        # Inspect the event log for the pre-rescue DELIVERY_UNVERIFIED
        # event. When the autorescue path is active (default
        # ``max_rescues=1``), the manager emits a SECOND
        # ``delivery_unverified`` event with
        # ``gate=missing_expected_artifacts_post_rescue`` after the
        # rescue attempt fails. Selecting by ``ORDER BY ts DESC`` would
        # non-deterministically return the post-rescue event (whose
        # ``gate`` differs from the value this test asserts). Filter
        # explicitly on the pre-rescue ``gate`` payload to make the
        # assertion order-independent.
        conn = get_conn()
        rows = conn.execute(
            "SELECT kind, payload_json FROM task_events "
            "WHERE task_id = ? AND kind = 'delivery_unverified' "
            "ORDER BY ts ASC",
            (t.task_id,),
        ).fetchall()
        self.assertTrue(len(rows) >= 1, "expected delivery_unverified event")
        pre_rescue = None
        for row in rows:
            p = json.loads(row["payload_json"])
            if p.get("gate") == "missing_expected_artifacts":
                pre_rescue = p
                break
        self.assertIsNotNone(
            pre_rescue,
            "expected pre-rescue delivery_unverified event with "
            "gate=missing_expected_artifacts, got: "
            + json.dumps([json.loads(r["payload_json"]) for r in rows]),
        )
        assert pre_rescue is not None  # for type-checkers
        self.assertEqual(pre_rescue.get("missing_count"), 1)
        self.assertEqual(pre_rescue.get("declared_count"), 1)
        self.assertIn(str(self.missing), pre_rescue.get("missing_paths", []))


if __name__ == "__main__":
    unittest.main()