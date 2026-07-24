"""WO-INCOMPLETE-DELIVERY-AUTORESCUE targeted tests.

Covers the rescue lifecycle introduced by WO-INCOMPLETE-DELIVERY-AUTORESCUE.
Mirrors the test shape of tests/test_wo_completion_gate.py: uses the
default TaskManager() (which initializes the shared dispatcher DB) and
unique titles per test case for log isolation.

Test matrix:
  1. Rescue failure path — artifact still missing after rescue → failed.
  2. max_rescues=0 disables rescue (WO-COMPLETION-GATE-MVP behavior).
  3. No expected_artifacts → no rescue transition (compatibility).
  4. max_rescues clamp — >5 → 5, negatives → 0, None → 1.
  5. incomplete_delivery legal transitions (non-terminal state).
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from dispatcher.manager import TaskManager
from dispatcher.models import is_legal_transition


class TestRescueLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="wo-rescue-")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _create_start_complete(self, *, title, expected_artifacts=None,
                                max_rescues=None, output_text="done"):
        m = TaskManager()
        t = m.create(
            title=title,
            type="research",
            input_text="placeholder",
            expected_artifacts=expected_artifacts,
            max_rescues=max_rescues,
        )
        m.start(t.task_id, hermes_run_id=f"{title}-run")
        out = m.complete(t.task_id, output_text=output_text)
        return m, t, out

    def test_rescue_fails_when_artifact_still_missing(self):
        """Rescue runs; artifact still missing → failed with reason."""
        missing = Path(self.tmpdir) / "never.md"
        m, t, out = self._create_start_complete(
            title="wo-rescue-fail",
            expected_artifacts=[str(missing)],
            max_rescues=1,
        )
        self.assertEqual(out.status, "failed")
        self.assertGreaterEqual(out.rescue_count, 1)
        self.assertIn("missing_expected_artifacts", out.error_message or "")

    def test_max_rescues_zero_disables_rescue(self):
        """max_rescues=0 → no rescue attempt, direct fail."""
        missing = Path(self.tmpdir) / "no_rescue.md"
        m, t, out = self._create_start_complete(
            title="wo-rescue-disabled",
            expected_artifacts=[str(missing)],
            max_rescues=0,
        )
        self.assertEqual(out.status, "failed")
        self.assertEqual(out.rescue_count, 0)
        self.assertIn("missing_expected_artifacts", out.error_message or "")

    def test_no_expected_artifacts_no_rescue(self):
        """No contract → completed, rescue_count stays 0."""
        m, t, out = self._create_start_complete(
            title="wo-no-contract",
            max_rescues=5,
        )
        self.assertEqual(out.status, "completed")
        self.assertEqual(out.rescue_count, 0)

    def test_legacy_null_max_rescues_uses_default(self):
        """max_rescues=None → default 1."""
        m = TaskManager()
        t = m.create(
            title="wo-legacy-default",
            type="research",
            input_text="x",
            expected_artifacts=[str(Path(self.tmpdir) / "legacy.md")],
            max_rescues=None,
        )
        self.assertEqual(t.max_rescues, 1)

    def test_clamp_high(self):
        m = TaskManager()
        t = m.create(
            title="wo-clamp-high", type="research", input_text="x",
            max_rescues=99,
        )
        self.assertEqual(t.max_rescues, 5)

    def test_clamp_negative(self):
        m = TaskManager()
        t = m.create(
            title="wo-clamp-neg", type="research", input_text="x",
            max_rescues=-3,
        )
        self.assertEqual(t.max_rescues, 0)


class TestIncompleteDeliveryNonTerminal(unittest.TestCase):
    def test_legal_transitions_include_rescue_edges(self):
        self.assertTrue(is_legal_transition("running", "incomplete_delivery"))
        self.assertTrue(is_legal_transition("incomplete_delivery", "running"))
        self.assertTrue(is_legal_transition("incomplete_delivery", "completed"))
        self.assertTrue(is_legal_transition("incomplete_delivery", "failed"))


if __name__ == "__main__":
    unittest.main()