"""AEE Bootstrap v1 — Stage Executor unit tests (stages 02-07).

Targets the stage executors in :mod:`aee.installer.stages`. All tests
are stdlib ``unittest`` — no pytest, no external deps. Uses tempfile
sandboxes for filesystem stages and fakes for subprocess.

Coverage:

* :class:`CloneStage` — in-place skip, dry-run skip, missing git_url
  failure, real clone (fake subprocess).
* :class:`RuntimeSetupStage` — dry-run skip, missing lockfile failure,
  venv creation + pip install (fake subprocess).
* :class:`HealthCheckStage` — dry-run skip, doctor PASS, doctor CAVEAT
  (completed), doctor FAIL.
* :class:`SmokeTestStage` — dry-run skip, import success + doctor
  success, import failure, doctor failure.
* :class:`AgentReadyStage` — dry-run skip, real marker write, marker
  content shape.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aee.installer.lifecycle import (
    BootstrapLifecycle,
    InMemoryMarkerStore,
    StageName,
    StageState,
)
from aee.installer.stages.base import (
    StageContext,
    StageOutcome,
    StageResult,
)
from aee.installer.stages.clone import CloneStage
from aee.installer.stages.runtime_setup import RuntimeSetupStage
from aee.installer.stages.health_check import HealthCheckStage
from aee.installer.stages.smoke_test import SmokeTestStage
from aee.installer.stages.agent_ready import AgentReadyStage


def _ctx(repo_root, profile="mini", dry_run=False, extra=None, environ=None):
    store = InMemoryMarkerStore()
    lc = BootstrapLifecycle(store)
    lc.start()
    return StageContext(
        repo_root=Path(repo_root),
        profile=profile,
        install_path=Path(repo_root) / ".venv",
        environ=environ or {},
        lifecycle=lc,
        run_id=lc.run_id,
        dry_run=dry_run,
        extra=extra or (),
    )


# ---------------------------------------------------------------------------#
# CloneStage
# ---------------------------------------------------------------------------#


class CloneStageTests(unittest.TestCase):
    def test_in_place_repo_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / ".git").mkdir()
            ctx = _ctx(td)
            r = CloneStage().run(ctx)
            self.assertEqual(r.outcome, StageOutcome.SKIPPED)
            self.assertIn("in_place", r.evidence["mode"])

    def test_dry_run_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td, dry_run=True, extra=(("git_url", "https://example.com/repo.git"),))
            r = CloneStage().run(ctx)
            self.assertEqual(r.outcome, StageOutcome.SKIPPED)
            self.assertEqual(r.evidence["mode"], "dry_run")

    def test_missing_git_url_fails(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td)
            r = CloneStage().run(ctx)
            self.assertEqual(r.outcome, StageOutcome.FAILED)
            self.assertEqual(r.error_class, "CloneTargetMissingError")

    @patch("aee.installer.stages.clone._run_subprocess")
    def test_real_clone_success(self, mock_sub):
        mock_sub.return_value = (0, "", "")
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td, extra=(("git_url", "https://example.com/repo.git"),))
            r = CloneStage().run(ctx)
            self.assertEqual(r.outcome, StageOutcome.COMPLETED)
            self.assertEqual(r.evidence["exit_code"], 0)

    @patch("aee.installer.stages.clone._run_subprocess")
    def test_clone_failure(self, mock_sub):
        mock_sub.return_value = (1, "", "fatal: not found")
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td, extra=(("git_url", "https://example.com/repo.git"),))
            r = CloneStage().run(ctx)
            self.assertEqual(r.outcome, StageOutcome.FAILED)
            self.assertEqual(r.error_class, "CloneFailedError")
            self.assertIn("fatal", r.evidence["stderr_tail"])


# ---------------------------------------------------------------------------#
# RuntimeSetupStage
# ---------------------------------------------------------------------------#


class RuntimeSetupStageTests(unittest.TestCase):
    def test_dry_run_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            lockfile = Path(td) / "requirements.lock"
            lockfile.write_text("fastapi\n")
            ctx = _ctx(td, dry_run=True)
            ctx = StageContext(
                repo_root=Path(td), profile="mini",
                install_path=Path(td) / ".venv",
                environ={}, lifecycle=ctx.lifecycle,
                run_id=ctx.run_id, dry_run=True,
                lockfile_path=lockfile,
            )
            r = RuntimeSetupStage().run(ctx)
            self.assertEqual(r.outcome, StageOutcome.SKIPPED)
            self.assertEqual(r.evidence["mode"], "dry_run")

    def test_missing_lockfile_fails(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td)
            r = RuntimeSetupStage().run(ctx)
            self.assertEqual(r.outcome, StageOutcome.FAILED)
            self.assertEqual(r.error_class, "LockfileMissingError")

    @patch("aee.installer.stages.runtime_setup._run_subprocess")
    def test_venv_create_and_install_success(self, mock_sub):
        mock_sub.return_value = (0, "Installed", "")
        with tempfile.TemporaryDirectory() as td:
            lockfile = Path(td) / "requirements.lock"
            lockfile.write_text("fastapi\n")
            ctx = StageContext(
                repo_root=Path(td), profile="mini",
                install_path=Path(td) / ".venv",
                environ={}, lifecycle=_ctx(td).lifecycle,
                run_id="test-run", dry_run=False,
                lockfile_path=lockfile,
            )
            r = RuntimeSetupStage().run(ctx)
            self.assertEqual(r.outcome, StageOutcome.COMPLETED)
            self.assertTrue(r.evidence["venv_created"])

    @patch("aee.installer.stages.runtime_setup._run_subprocess")
    def test_pip_install_failure(self, mock_sub):
        mock_sub.return_value = (1, "", "pip error")
        with tempfile.TemporaryDirectory() as td:
            lockfile = Path(td) / "requirements.lock"
            lockfile.write_text("fastapi\n")
            lc = BootstrapLifecycle(InMemoryMarkerStore())
            lc.start()
            ctx = StageContext(
                repo_root=Path(td), profile="mini",
                install_path=Path(td) / ".venv",
                environ={}, lifecycle=lc,
                run_id=lc.run_id, dry_run=False,
                lockfile_path=lockfile,
            )
            # First call creates venv (0), second call pip install fails (1)
            mock_sub.side_effect = [(0, "", ""), (1, "", "pip error")]
            r = RuntimeSetupStage().run(ctx)
            self.assertEqual(r.outcome, StageOutcome.FAILED)
            self.assertEqual(r.error_class, "PipInstallError")


# ---------------------------------------------------------------------------#
# HealthCheckStage
# ---------------------------------------------------------------------------#


class HealthCheckStageTests(unittest.TestCase):
    def test_dry_run_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td, dry_run=True)
            r = HealthCheckStage().run(ctx)
            self.assertEqual(r.outcome, StageOutcome.SKIPPED)

    @patch("aee.doctor.run_doctor")
    def test_doctor_pass(self, mock_doc):
        from aee.doctor import CheckResult, DoctorReport
        mock_doc.return_value = DoctorReport(
            verdict="PASS", profile="mini",
            checks=(CheckResult("c1", "PASS", "ok"),),
            summary={"PASS": 1, "CAVEAT": 0, "FAIL": 0},
        )
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td)
            r = HealthCheckStage().run(ctx)
            self.assertEqual(r.outcome, StageOutcome.COMPLETED)
            self.assertEqual(r.evidence["verdict"], "PASS")

    @patch("aee.doctor.run_doctor")
    def test_doctor_caveat_completes(self, mock_doc):
        from aee.doctor import CheckResult, DoctorReport
        mock_doc.return_value = DoctorReport(
            verdict="CAVEAT", profile="mini",
            checks=(CheckResult("c1", "CAVEAT", "warn"),),
            summary={"PASS": 0, "CAVEAT": 1, "FAIL": 0},
        )
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td)
            r = HealthCheckStage().run(ctx)
            self.assertEqual(r.outcome, StageOutcome.COMPLETED)
            self.assertEqual(r.evidence["verdict"], "CAVEAT")

    @patch("aee.doctor.run_doctor")
    def test_doctor_fail(self, mock_doc):
        from aee.doctor import CheckResult, DoctorReport
        mock_doc.return_value = DoctorReport(
            verdict="FAIL", profile="mini",
            checks=(CheckResult("c1", "FAIL", "broken"),),
            summary={"PASS": 0, "CAVEAT": 0, "FAIL": 1},
        )
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td)
            r = HealthCheckStage().run(ctx)
            self.assertEqual(r.outcome, StageOutcome.FAILED)
            self.assertEqual(r.error_class, "DoctorFailedError")


# ---------------------------------------------------------------------------#
# SmokeTestStage
# ---------------------------------------------------------------------------#


class SmokeTestStageTests(unittest.TestCase):
    def test_dry_run_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td, dry_run=True)
            r = SmokeTestStage().run(ctx)
            self.assertEqual(r.outcome, StageOutcome.SKIPPED)

    @patch("aee.installer.stages.smoke_test._run_subprocess")
    def test_smoke_success(self, mock_sub):
        mock_sub.return_value = (0, "aee.cli import OK", "")
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td)
            r = SmokeTestStage().run(ctx)
            self.assertEqual(r.outcome, StageOutcome.COMPLETED)

    @patch("aee.installer.stages.smoke_test._run_subprocess")
    def test_import_failure(self, mock_sub):
        mock_sub.return_value = (1, "", "ModuleNotFoundError")
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td)
            r = SmokeTestStage().run(ctx)
            self.assertEqual(r.outcome, StageOutcome.FAILED)
            self.assertEqual(r.error_class, "SmokeImportError")

    @patch("aee.installer.stages.smoke_test._run_subprocess")
    def test_doctor_failure(self, mock_sub):
        mock_sub.side_effect = [
            (0, "aee.cli import OK", ""),
            (1, "", "doctor failed"),
        ]
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td)
            r = SmokeTestStage().run(ctx)
            self.assertEqual(r.outcome, StageOutcome.FAILED)
            self.assertEqual(r.error_class, "SmokeDoctorError")


# ---------------------------------------------------------------------------#
# AgentReadyStage
# ---------------------------------------------------------------------------#


class AgentReadyStageTests(unittest.TestCase):
    def test_dry_run_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td, dry_run=True)
            r = AgentReadyStage().run(ctx)
            self.assertEqual(r.outcome, StageOutcome.SKIPPED)

    def test_marker_written(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td)
            r = AgentReadyStage().run(ctx)
            self.assertEqual(r.outcome, StageOutcome.COMPLETED)
            marker = Path(td) / "AGENT_READY"
            self.assertTrue(marker.exists())
            doc = json.loads(marker.read_text())
            self.assertEqual(doc["profile"], "mini")
            self.assertIn("timestamp", doc)
            self.assertIn("run_id", doc)

    def test_marker_content_shape(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td, profile="full")
            r = AgentReadyStage().run(ctx)
            self.assertEqual(r.outcome, StageOutcome.COMPLETED)
            doc = r.evidence["marker_content"]
            self.assertEqual(doc["profile"], "full")
            self.assertIn("version", doc)
            self.assertIn("timestamp", doc)
            self.assertIn("run_id", doc)


if __name__ == "__main__":
    unittest.main()
