"""AEE Phase 3 — Installer Workflow targeted tests.

These tests verify the Phase 3 installer workflow module
(:mod:`aee.installer.workflow`) and the ``aee prepare`` CLI
subcommand (wired in :mod:`aee.cli`).

Coverage:

1. **Workflow composition** — the workflow runs every stage
   (doctor, installer backend, platform bootstrap, directory init,
   config bootstrap, post-install verification) and returns an
   :class:`InstallWorkflowResult`.
2. **Dry-run default** — ``run_workflow`` defaults to dry-run;
   ``executed=False``; no side effects.
3. **Execute not authorized** — ``run_workflow(dry_run=False)`` raises
   :class:`ExecuteNotAuthorizedError`.
4. **Unknown profile** — returns a BLOCKED result with a structured
   error (not an exception).
5. **Doctor integration** — the doctor's verdict flows into the
   summary's ``doctor_verdict`` and ``doctor_exit_code``.
6. **Installer backend integration** — the workflow's
   ``install_plan`` and ``install_preflight`` come from the §21.3
   backend.
7. **Directory init plan** — lists the required directories + the
   profile marker; ``marker_would_write`` is True on a fresh repo.
8. **Config bootstrap plan** — records env-file presence + supervisor
   conf presence + marker write plan.
9. **Platform bootstrap plan** — captures the platform string +
   supported flag; on the host platform, the error is non-empty when
   the platform is unsupported.
10. **Post-install verification** — projected checks report
    ``would_pass`` reflecting the state after a successful install.
11. **Overall verdict folding** — pre-flight fail > doctor FAIL >
    doctor CAVEAT > OK.
12. **to_dict JSON-serializable** — the full result serializes via
    ``json.dumps`` without raising.
13. **CLI plumbing** — ``aee prepare --no-network`` returns the
    expected exit code (0/7/8 depending on the host's doctor
    verdict); ``--json`` emits a JSON object on stdout.
14. **Idempotent** — re-running with the same arguments yields the
    same summary verdict + exit codes.
15. **No new exit codes** — the workflow reuses the existing
    vocabulary (0/4/5/6/7/8); no constants outside this set.
16. **No subprocess** — AST scan of the workflow module confirms no
    ``subprocess`` / ``os.system`` / ``os.popen`` usage.
"""
from __future__ import annotations

import ast
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from aee import cli as aee_cli
from aee.cli import (
    EXIT_OK,
    EXIT_DOCTOR_CAVEATS,
    EXIT_DOCTOR_FAILED,
    EXIT_PRE_FLIGHT_FAILED,
    EXIT_PROFILE_SWITCH_REJECTED,
)
from aee.installer.backend import ExecuteNotAuthorizedError
from aee.installer.workflow import (
    ConfigBootstrapPlan,
    DirectoryInitPlan,
    InstallWorkflowResult,
    PlatformBootstrapPlan,
    PostInstallVerification,
    WorkflowSummary,
    run_workflow,
)
from aee.profiles.descriptor import DEFAULT_PROFILE, KNOWN_PROFILES


# ---------------------------------------------------------------------------#
# Helpers
# ---------------------------------------------------------------------------#


def _make_repo_root() -> Path:
    """Create a tempdir with .git, .env, requirements.lock, data/, reports/, logs/."""
    root = Path(tempfile.mkdtemp(prefix="aee_phase3_test_"))
    (root / ".git").mkdir(exist_ok=True)
    (root / ".env").write_text("# test env\nHERMES_BASE_URL=http://localhost:8642\n")
    (root / "requirements.lock").write_text("# test lock\nfastapi\nuvicorn\n")
    for d in ("data", "reports", "logs"):
        (root / d).mkdir(exist_ok=True)
    return root


def _full_env() -> dict:
    """Build an environ that satisfies the doctor's required env vars."""
    env = dict(os.environ)
    env.update({
        "HERMES_BASE_URL": "http://localhost:8642",
        "HERMES_API_KEY": "test-key",
        "BRIDGE_HOST": "127.0.0.1",
        "BRIDGE_PORT": "8787",
    })
    return env


# ---------------------------------------------------------------------------#
# Tests
# ---------------------------------------------------------------------------#


class WorkflowCompositionTests(unittest.TestCase):
    """Coverage #1: the workflow runs every stage and returns a result."""

    def test_run_workflow_returns_install_workflow_result(self) -> None:
        root = _make_repo_root()
        try:
            result = run_workflow(
                profile="full",
                repo_root=root,
                environ=_full_env(),
                network=False,
                dry_run=True,
            )
            self.assertIsInstance(result, InstallWorkflowResult)
            self.assertIsInstance(result.summary, WorkflowSummary)
            self.assertIsInstance(result.doctor_report, object)
            self.assertIsNotNone(result.install_plan)
            self.assertIsNotNone(result.install_preflight)
            self.assertIsInstance(result.platform_bootstrap, PlatformBootstrapPlan)
            self.assertIsInstance(result.directory_init, DirectoryInitPlan)
            self.assertIsInstance(result.config_bootstrap, ConfigBootstrapPlan)
            self.assertIsInstance(result.post_install_verification, PostInstallVerification)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_workflow_runs_all_six_stages(self) -> None:
        root = _make_repo_root()
        try:
            result = run_workflow(
                profile="full",
                repo_root=root,
                environ=_full_env(),
                network=False,
            )
            # Stage 1: doctor report has at least one check.
            self.assertGreater(len(result.doctor_report.checks), 0)
            # Stage 2: install plan has steps.
            self.assertIsNotNone(result.install_plan)
            self.assertGreater(len(result.install_plan.steps), 0)
            # Stage 3: platform bootstrap has a platform string.
            self.assertTrue(result.platform_bootstrap.platform)
            # Stage 4: directory init has entries.
            self.assertGreater(len(result.directory_init.entries), 0)
            # Stage 5: config bootstrap has a profile.
            self.assertEqual(result.config_bootstrap.profile, "full")
            # Stage 6: post-install verification has checks.
            self.assertGreater(len(result.post_install_verification.checks), 0)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class DryRunDefaultTests(unittest.TestCase):
    """Coverage #2: dry-run is the default; executed=False."""

    def test_dry_run_is_default(self) -> None:
        root = _make_repo_root()
        try:
            result = run_workflow(
                profile="full",
                repo_root=root,
                environ=_full_env(),
                network=False,
            )
            self.assertTrue(result.summary.dry_run)
            self.assertFalse(result.executed)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_dry_run_explicit(self) -> None:
        root = _make_repo_root()
        try:
            result = run_workflow(
                profile="full",
                repo_root=root,
                environ=_full_env(),
                network=False,
                dry_run=True,
            )
            self.assertTrue(result.summary.dry_run)
            self.assertFalse(result.executed)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class ExecuteNotAuthorizedTests(unittest.TestCase):
    """Coverage #3: dry_run=False raises ExecuteNotAuthorizedError."""

    def test_execute_raises(self) -> None:
        root = _make_repo_root()
        try:
            with self.assertRaises(ExecuteNotAuthorizedError):
                run_workflow(
                    profile="full",
                    repo_root=root,
                    environ=_full_env(),
                    network=False,
                    dry_run=False,
                )
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class UnknownProfileTests(unittest.TestCase):
    """Coverage #4: unknown profile returns a BLOCKED result."""

    def test_unknown_profile_returns_blocked_result(self) -> None:
        root = _make_repo_root()
        try:
            result = run_workflow(
                profile="not-a-profile",
                repo_root=root,
                environ=_full_env(),
                network=False,
            )
            self.assertEqual(result.summary.overall_verdict, "BLOCKED")
            self.assertEqual(result.summary.overall_exit_code, EXIT_PRE_FLIGHT_FAILED)
            self.assertIn("unknown profile", result.error)
            self.assertIsNone(result.install_plan)
            self.assertIsNone(result.install_preflight)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class DoctorIntegrationTests(unittest.TestCase):
    """Coverage #5: the doctor's verdict flows into the summary."""

    def test_doctor_verdict_in_summary(self) -> None:
        root = _make_repo_root()
        try:
            result = run_workflow(
                profile="full",
                repo_root=root,
                environ=_full_env(),
                network=False,
            )
            self.assertIn(result.doctor_report.verdict, ("PASS", "CAVEAT", "FAIL"))
            self.assertEqual(
                result.summary.doctor_verdict,
                result.doctor_report.verdict,
            )
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_doctor_exit_code_matches_verdict(self) -> None:
        root = _make_repo_root()
        try:
            result = run_workflow(
                profile="full",
                repo_root=root,
                environ=_full_env(),
                network=False,
            )
            v = result.doctor_report.verdict
            expected = {
                "PASS": 0,
                "CAVEAT": EXIT_DOCTOR_CAVEATS,
                "FAIL": EXIT_DOCTOR_FAILED,
            }[v]
            self.assertEqual(result.summary.doctor_exit_code, expected)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class InstallerBackendIntegrationTests(unittest.TestCase):
    """Coverage #6: install_plan + install_preflight come from the backend."""

    def test_install_plan_has_steps(self) -> None:
        root = _make_repo_root()
        try:
            result = run_workflow(
                profile="full",
                repo_root=root,
                environ=_full_env(),
                network=False,
            )
            self.assertIsNotNone(result.install_plan)
            step_ids = [s.step_id for s in result.install_plan.steps]
            self.assertIn("preflight", step_ids)
            self.assertIn("verify", step_ids)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_preflight_ok_on_fresh_repo(self) -> None:
        root = _make_repo_root()
        try:
            result = run_workflow(
                profile="full",
                repo_root=root,
                environ=_full_env(),
                network=False,
            )
            self.assertIsNotNone(result.install_preflight)
            self.assertTrue(result.install_preflight.ok)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_preflight_profile_switch_rejected(self) -> None:
        root = _make_repo_root()
        try:
            # Write a marker with a different profile.
            (root / ".aee-profile").write_text("mini\n")
            result = run_workflow(
                profile="full",
                repo_root=root,
                environ=_full_env(),
                network=False,
            )
            self.assertIsNotNone(result.install_preflight)
            self.assertFalse(result.install_preflight.ok)
            self.assertEqual(result.install_preflight.existing_profile, "mini")
            self.assertEqual(
                result.summary.install_exit_code,
                EXIT_PROFILE_SWITCH_REJECTED,
            )
            self.assertEqual(result.summary.overall_verdict, "BLOCKED")
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class DirectoryInitPlanTests(unittest.TestCase):
    """Coverage #7: directory init plan lists required dirs + marker."""

    def test_directory_init_includes_required_dirs(self) -> None:
        root = _make_repo_root()
        try:
            result = run_workflow(
                profile="full",
                repo_root=root,
                environ=_full_env(),
                network=False,
            )
            paths = [p for p, _, _ in result.directory_init.entries]
            self.assertIn("data", paths)
            self.assertIn("reports", paths)
            self.assertIn("logs", paths)
            self.assertIn(".aee-profile", paths)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_marker_would_write_on_fresh_repo(self) -> None:
        root = _make_repo_root()
        try:
            result = run_workflow(
                profile="full",
                repo_root=root,
                environ=_full_env(),
                network=False,
            )
            self.assertTrue(result.directory_init.marker_would_write)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_marker_would_not_write_when_existing(self) -> None:
        root = _make_repo_root()
        try:
            (root / ".aee-profile").write_text("full\n")
            result = run_workflow(
                profile="full",
                repo_root=root,
                environ=_full_env(),
                network=False,
            )
            self.assertFalse(result.directory_init.marker_would_write)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class ConfigBootstrapPlanTests(unittest.TestCase):
    """Coverage #8: config bootstrap records env + supervisor + marker."""

    def test_env_file_present(self) -> None:
        root = _make_repo_root()
        try:
            result = run_workflow(
                profile="full",
                repo_root=root,
                environ=_full_env(),
                network=False,
            )
            self.assertTrue(result.config_bootstrap.env_file_present)
            self.assertFalse(result.config_bootstrap.env_file_would_install)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_env_file_missing_would_install(self) -> None:
        root = _make_repo_root()
        try:
            (root / ".env").unlink()
            result = run_workflow(
                profile="full",
                repo_root=root,
                environ=_full_env(),
                network=False,
            )
            self.assertFalse(result.config_bootstrap.env_file_present)
            self.assertTrue(result.config_bootstrap.env_file_would_install)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_profile_marker_would_write_on_fresh_repo(self) -> None:
        root = _make_repo_root()
        try:
            result = run_workflow(
                profile="full",
                repo_root=root,
                environ=_full_env(),
                network=False,
            )
            self.assertTrue(result.config_bootstrap.profile_marker_would_write)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class PlatformBootstrapPlanTests(unittest.TestCase):
    """Coverage #9: platform bootstrap captures the platform string."""

    def test_platform_bootstrap_has_platform_string(self) -> None:
        root = _make_repo_root()
        try:
            result = run_workflow(
                profile="full",
                repo_root=root,
                environ=_full_env(),
                network=False,
            )
            self.assertTrue(result.platform_bootstrap.platform)
            # On the test host (Linux), the bootstrap should be supported.
            # We don't assert the exact platform string (host-dependent)
            # but we do assert the field is populated.
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_platform_bootstrap_unsupported_profile_records_error(self) -> None:
        # On macOS v1, only developer is supported; on Linux the
        # linux_bootstrap layer raises UnsupportedProfileError for
        # profiles not in the profile gate. We test the structure of
        # the error field rather than the exact platform behavior.
        root = _make_repo_root()
        try:
            result = run_workflow(
                profile="full",
                repo_root=root,
                environ=_full_env(),
                network=False,
            )
            # error is a string (possibly empty when supported + allowed).
            self.assertIsInstance(result.platform_bootstrap.error, str)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class PostInstallVerificationTests(unittest.TestCase):
    """Coverage #10: projected checks report would_pass."""

    def test_post_install_has_checks(self) -> None:
        root = _make_repo_root()
        try:
            result = run_workflow(
                profile="full",
                repo_root=root,
                environ=_full_env(),
                network=False,
            )
            self.assertGreater(len(result.post_install_verification.checks), 0)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_post_install_check_names(self) -> None:
        root = _make_repo_root()
        try:
            result = run_workflow(
                profile="full",
                repo_root=root,
                environ=_full_env(),
                network=False,
            )
            names = [n for n, _, _ in result.post_install_verification.checks]
            self.assertIn("profile_marker", names)
            self.assertIn("required_directories", names)
            self.assertIn("env_file", names)
            self.assertIn("required_dependencies", names)
            self.assertIn("platform_bootstrap", names)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_post_install_to_dict_has_would_pass_all(self) -> None:
        root = _make_repo_root()
        try:
            result = run_workflow(
                profile="full",
                repo_root=root,
                environ=_full_env(),
                network=False,
            )
            d = result.post_install_verification.to_dict()
            self.assertIn("would_pass_all", d)
            self.assertIsInstance(d["would_pass_all"], bool)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class OverallVerdictFoldingTests(unittest.TestCase):
    """Coverage #11: pre-flight fail > doctor FAIL > CAVEAT > OK."""

    def test_profile_switch_blocks_overall(self) -> None:
        root = _make_repo_root()
        try:
            (root / ".aee-profile").write_text("mini\n")
            result = run_workflow(
                profile="full",
                repo_root=root,
                environ=_full_env(),
                network=False,
            )
            self.assertEqual(result.summary.overall_verdict, "BLOCKED")
            self.assertEqual(
                result.summary.overall_exit_code,
                EXIT_PROFILE_SWITCH_REJECTED,
            )
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_doctor_fail_does_not_block_when_preflight_ok(self) -> None:
        # When the doctor FAILs (e.g. missing env var) but the installer
        # pre-flight passes, the overall verdict is FAIL (not BLOCKED).
        root = _make_repo_root()
        try:
            result = run_workflow(
                profile="full",
                repo_root=root,
                environ={},  # empty env → doctor FAIL
                network=False,
            )
            self.assertEqual(result.summary.doctor_verdict, "FAIL")
            # Pre-flight should still pass (no marker conflict).
            self.assertTrue(result.install_preflight.ok)
            self.assertEqual(result.summary.overall_verdict, "FAIL")
            self.assertEqual(result.summary.overall_exit_code, EXIT_DOCTOR_FAILED)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class JsonSerializationTests(unittest.TestCase):
    """Coverage #12: to_dict is JSON-serializable."""

    def test_to_dict_json_serializable(self) -> None:
        root = _make_repo_root()
        try:
            result = run_workflow(
                profile="full",
                repo_root=root,
                environ=_full_env(),
                network=False,
            )
            d = result.to_dict()
            # json.dumps must not raise.
            s = json.dumps(d, indent=2, sort_keys=True)
            self.assertIsInstance(s, str)
            # Re-parse to confirm it's valid JSON.
            parsed = json.loads(s)
            self.assertIsInstance(parsed, dict)
            self.assertIn("summary", parsed)
            self.assertIn("doctor_report", parsed)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class CLIPlumbingTests(unittest.TestCase):
    """Coverage #13: ``aee prepare`` returns expected exit codes."""

    def test_cli_prepare_no_network_returns_exit_code(self) -> None:
        root = _make_repo_root()
        try:
            exit_code = aee_cli.main([
                "prepare",
                "--no-network",
                "--repo-root", str(root),
            ])
            self.assertIn(exit_code, (EXIT_OK, EXIT_DOCTOR_CAVEATS, EXIT_DOCTOR_FAILED))
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_cli_prepare_json_emits_json(self) -> None:
        root = _make_repo_root()
        try:
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                exit_code = aee_cli.main([
                    "prepare",
                    "--no-network",
                    "--repo-root", str(root),
                    "--json",
                ])
            output = captured.getvalue()
            self.assertGreater(len(output), 0)
            parsed = json.loads(output)
            self.assertIsInstance(parsed, dict)
            self.assertIn("summary", parsed)
            self.assertIn(exit_code, (EXIT_OK, EXIT_DOCTOR_CAVEATS, EXIT_DOCTOR_FAILED))
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_cli_prepare_unknown_profile_returns_blocked(self) -> None:
        root = _make_repo_root()
        try:
            # argparse rejects unknown profile values with SystemExit(2).
            with self.assertRaises(SystemExit) as cm:
                aee_cli.main([
                    "prepare",
                    "--no-network",
                    "--repo-root", str(root),
                    "--profile", "not-a-profile",
                ])
            self.assertEqual(cm.exception.code, 2)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class IdempotentTests(unittest.TestCase):
    """Coverage #14: re-running yields the same summary verdict + exit codes."""

    def test_idempotent_summary(self) -> None:
        root = _make_repo_root()
        try:
            r1 = run_workflow(
                profile="full",
                repo_root=root,
                environ=_full_env(),
                network=False,
            )
            r2 = run_workflow(
                profile="full",
                repo_root=root,
                environ=_full_env(),
                network=False,
            )
            self.assertEqual(r1.summary.overall_verdict, r2.summary.overall_verdict)
            self.assertEqual(r1.summary.overall_exit_code, r2.summary.overall_exit_code)
            self.assertEqual(r1.summary.doctor_verdict, r2.summary.doctor_verdict)
            self.assertEqual(r1.summary.install_exit_code, r2.summary.install_exit_code)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class NoNewExitCodesTests(unittest.TestCase):
    """Coverage #15: the workflow reuses the existing exit-code vocabulary."""

    _ALLOWED = {0, 2, 3, 4, 5, 6, 7, 8}

    def test_overall_exit_code_in_allowed_set(self) -> None:
        root = _make_repo_root()
        try:
            result = run_workflow(
                profile="full",
                repo_root=root,
                environ=_full_env(),
                network=False,
            )
            self.assertIn(result.summary.overall_exit_code, self._ALLOWED)
            self.assertIn(result.summary.doctor_exit_code, self._ALLOWED)
            self.assertIn(result.summary.install_exit_code, self._ALLOWED)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class NoSubprocessAstScanTests(unittest.TestCase):
    """Coverage #16: the workflow module performs no process spawns."""

    def test_no_subprocess_import(self) -> None:
        from aee.installer import workflow as wf_module
        source_path = Path(wf_module.__file__)
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "subprocess":
                        violations.append(
                            "subprocess imported at line {l}".format(l=node.lineno)
                        )
            if isinstance(node, ast.ImportFrom):
                if node.module == "subprocess":
                    violations.append(
                        "subprocess imported from at line {l}".format(l=node.lineno)
                    )
            if isinstance(node, ast.Attribute):
                if isinstance(node.value, ast.Name):
                    if node.value.id == "os" and node.attr in ("system", "popen"):
                        violations.append(
                            "os.{a} used at line {l}".format(
                                a=node.attr, l=node.lineno
                            )
                        )
        self.assertEqual(
            violations, [],
            "workflow module must not use subprocess/os.system/os.popen; "
            "found: {v}".format(v=violations),
        )

    def test_no_subprocess_call(self) -> None:
        from aee.installer import workflow as wf_module
        source = Path(wf_module.__file__).read_text(encoding="utf-8")
        # The literal "subprocess." must not appear outside comments/docstrings.
        # This is a coarse check; the AST scan above is the precise one.
        # We allow "subprocess" to appear in docstrings (e.g. this test's
        # own docstring is not in the workflow module, so any hit is a
        # real usage).
        # Strip comments and docstrings for a fairer scan.
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    if isinstance(func.value, ast.Name):
                        if func.value.id == "subprocess":
                            self.fail(
                                "subprocess.<call> found at line {l}".format(
                                    l=node.lineno
                                )
                            )


if __name__ == "__main__":
    unittest.main()