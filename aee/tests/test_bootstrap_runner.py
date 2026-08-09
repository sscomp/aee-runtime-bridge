"""AEE Bootstrap v1 — BootstrapRunner integration tests.

Verifies the runner drives stages 02-07 in order, records transitions,
handles resume (skip completed stages), and stops on failure.

Uses fake stage executors to avoid real subprocess/filesystem; the
unit tests in test_bootstrap_stages.py cover the real stage
implementations.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from typing import List

from aee.installer.lifecycle import (
    InMemoryMarkerStore,
    StageName,
    StageState,
)
from aee.installer.runner import BootstrapRunner, BootstrapRunResult
from aee.installer.stages.base import (
    StageContext,
    StageOutcome,
    StageResult,
    StageExecutor,
)


# ---------------------------------------------------------------------------#
# Fake executors for integration testing
# ---------------------------------------------------------------------------#


class _FakeExecutor:
    """A fake stage executor that returns a canned result."""

    def __init__(self, stage_name, outcome, message="fake"):
        self._name = stage_name
        self._outcome = outcome
        self._message = message

    @property
    def name(self):
        return self._name

    def run(self, ctx):
        return StageResult(
            stage=self._name,
            outcome=self._outcome,
            message=self._message,
            evidence={"fake": True},
        )


def _fake_executors(outcomes):
    """Build a tuple of fake executors from (StageName, outcome) pairs."""
    return tuple(
        _FakeExecutor(stage, outcome)
        for stage, outcome in outcomes
    )


# ---------------------------------------------------------------------------#
# Tests
# ---------------------------------------------------------------------------#


class BootstrapRunnerTests(unittest.TestCase):
    def test_all_stages_complete(self):
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            executors = _fake_executors([
                (StageName.CLONE, StageOutcome.COMPLETED),
                (StageName.RUNTIME_SETUP, StageOutcome.COMPLETED),
                (StageName.HEALTH_CHECK, StageOutcome.COMPLETED),
                (StageName.SMOKE_TEST, StageOutcome.COMPLETED),
                (StageName.AGENT_READY, StageOutcome.COMPLETED),
            ])
            runner = BootstrapRunner(
                Path(td), "mini",
                executors=executors,
                environ={},
            )
            r = runner.run()
            self.assertTrue(r.ok)
            self.assertTrue(r.agent_ready)
            self.assertIsNone(r.failing_stage)
            self.assertEqual(len(r.stages), 5)

    def test_stage_failure_stops_run(self):
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            executors = _fake_executors([
                (StageName.CLONE, StageOutcome.COMPLETED),
                (StageName.RUNTIME_SETUP, StageOutcome.FAILED),
                (StageName.HEALTH_CHECK, StageOutcome.COMPLETED),
                (StageName.SMOKE_TEST, StageOutcome.COMPLETED),
                (StageName.AGENT_READY, StageOutcome.COMPLETED),
            ])
            runner = BootstrapRunner(
                Path(td), "mini",
                executors=executors,
                environ={},
            )
            r = runner.run()
            self.assertFalse(r.ok)
            self.assertEqual(r.failing_stage, StageName.RUNTIME_SETUP)
            self.assertFalse(r.agent_ready)
            # Only 2 stages ran (clone + runtime_setup which failed)
            self.assertEqual(len(r.stages), 2)

    def test_dry_run_all_skipped(self):
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            executors = _fake_executors([
                (StageName.CLONE, StageOutcome.SKIPPED),
                (StageName.RUNTIME_SETUP, StageOutcome.SKIPPED),
                (StageName.HEALTH_CHECK, StageOutcome.SKIPPED),
                (StageName.SMOKE_TEST, StageOutcome.SKIPPED),
                (StageName.AGENT_READY, StageOutcome.SKIPPED),
            ])
            runner = BootstrapRunner(
                Path(td), "mini",
                executors=executors,
                environ={},
                dry_run=True,
            )
            r = runner.run()
            self.assertTrue(r.ok)
            self.assertFalse(r.agent_ready)
            self.assertEqual(len(r.stages), 5)

    def test_resume_skips_completed_stages(self):
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            store = InMemoryMarkerStore()
            # Pre-populate the store with completed markers for stages 02, 04
            from aee.installer.lifecycle import BootstrapLifecycle
            lc = BootstrapLifecycle(store)
            state = lc.start()
            lc.record_stage(StageName.CLONE, StageState.COMPLETED)
            lc.record_stage(StageName.RUNTIME_SETUP, StageState.COMPLETED)
            run_id = state.run_id

            executors = _fake_executors([
                (StageName.CLONE, StageOutcome.COMPLETED),
                (StageName.RUNTIME_SETUP, StageOutcome.COMPLETED),
                (StageName.HEALTH_CHECK, StageOutcome.COMPLETED),
                (StageName.SMOKE_TEST, StageOutcome.COMPLETED),
                (StageName.AGENT_READY, StageOutcome.COMPLETED),
            ])
            runner = BootstrapRunner(
                Path(td), "mini",
                store=store,
                executors=executors,
                environ={},
                run_id=run_id,
            )
            r = runner.run()
            self.assertTrue(r.ok)
            self.assertTrue(r.agent_ready)
            # Stages 02 and 04 should be SKIPPED (resume), 05/06/07 ran
            self.assertEqual(r.stages[0].outcome, StageOutcome.SKIPPED)
            self.assertEqual(r.stages[1].outcome, StageOutcome.SKIPPED)
            self.assertEqual(r.stages[2].outcome, StageOutcome.COMPLETED)
            self.assertEqual(r.stages[3].outcome, StageOutcome.COMPLETED)
            self.assertEqual(r.stages[4].outcome, StageOutcome.COMPLETED)

    def test_result_to_dict(self):
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            executors = _fake_executors([
                (StageName.CLONE, StageOutcome.COMPLETED),
                (StageName.RUNTIME_SETUP, StageOutcome.COMPLETED),
                (StageName.HEALTH_CHECK, StageOutcome.COMPLETED),
                (StageName.SMOKE_TEST, StageOutcome.COMPLETED),
                (StageName.AGENT_READY, StageOutcome.COMPLETED),
            ])
            runner = BootstrapRunner(
                Path(td), "full",
                executors=executors,
                environ={},
            )
            r = runner.run()
            d = r.to_dict()
            self.assertIn("run_id", d)
            self.assertIn("stages", d)
            self.assertTrue(d["ok"])
            self.assertTrue(d["agent_ready"])
            self.assertEqual(len(d["stages"]), 5)


if __name__ == "__main__":
    unittest.main()
