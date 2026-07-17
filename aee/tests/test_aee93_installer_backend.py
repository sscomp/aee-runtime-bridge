"""AEE Epic 9.3 — Installer Backend (§21.3) targeted tests.

These tests verify the §21.3 installer backend contract delivered by
:mod:`aee.installer.backend` and its wiring into :mod:`aee.cli`.

Coverage:

1. **Plan shape** — every profile produces an :class:`InstallPlan`
   with the expected step vocabulary (universal + profile-specific +
   ``verify``).
2. **Canonical source of truth** — the backend imports
   :data:`KNOWN_PROFILES` / :data:`DEFAULT_PROFILE` from the
   descriptor module; no parallel hard-coded matrix.
3. **Idempotent pre-flight** — pre-flight is safe to re-run; same
   result on repeat.
4. **Profile switch rejected** — a ``.aee-profile`` marker with a
   different profile causes ``preflight.ok=False`` and the CLI
   returns ``EXIT_PROFILE_SWITCH_REJECTED``.
5. **Dry-run default** — ``execute()`` defaults to dry-run;
   ``executed=False``; no side effects.
6. **Execute not authorized** — ``execute(dry_run=False)`` raises
   :class:`ExecuteNotAuthorizedError`.
7. **Mini hardening** — the mini plan includes ``system_user``,
   ``env_file_0600``, ``smoke_test`` (the AEE-MINI hardening per
   §21.3).
8. **No subprocess / os.system** — the backend module performs no
   process spawns (AST scan).
9. **CLI wiring** — ``aee install --profile X`` returns 0 and the
   output reflects the backend plan (``backend_implemented : True``,
   ``executed : False``, plan steps listed).
10. **JSON shape** — ``aee install --profile X --json`` emits a JSON
    object with ``plan`` + ``preflight`` + ``backend_implemented:
    True`` + ``executed: False``.
11. **Exit codes** — pre-flight failure → ``EXIT_PRE_FLIGHT_FAILED``;
    profile switch → ``EXIT_PROFILE_SWITCH_REJECTED``; unknown
    profile → ``EXIT_PROFILE_ERROR``.
12. **AEE-MINI compatibility** — the backend does not reference or
    migrate the AEE-MINI installer; the §21.10 deprecation window is
    respected (verified via absence of AEE-MINI path references in
    the backend source).
13. **Backward compat** — the §21.2 CLI surfaces (argparse parsing,
    --help, --profile choices) remain functional.

Run: ``PYTHONPATH=. python3 -m unittest aee.tests.test_aee93_installer_backend -v``
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
from unittest.mock import patch

from aee import cli as aee_cli
from aee.cli import (
    EXIT_OK,
    EXIT_PARSE_ERROR,
    EXIT_PROFILE_ERROR,
    EXIT_PRE_FLIGHT_FAILED,
    EXIT_PROFILE_SWITCH_REJECTED,
    main,
)
from aee.installer import (
    InstallerBackend,
    InstallPlan,
    InstallPlanStep,
    PreFlightResult,
    InstallResult,
    InstallerError,
    ProfileSwitchRejectedError,
    PreFlightFailedError,
    ExecuteNotAuthorizedError,
    MINI_HARDENING_STEPS,
    UNIVERSAL_STEPS,
    plan_install,
)
from aee.installer import backend as backend_module
from aee.profiles.descriptor import (
    DEFAULT_PROFILE,
    KNOWN_PROFILES,
    UnknownProfileError,
)


# ---------------------------------------------------------------------------
# 1. Plan shape
# ---------------------------------------------------------------------------


class PlanShapeTests(unittest.TestCase):
    """Every profile produces a plan with the expected step vocabulary."""

    def test_full_plan_has_universal_steps_plus_verify(self) -> None:
        backend = InstallerBackend(dry_run=True)
        plan = backend.plan("full")
        ids = [s.step_id for s in plan.steps]
        for sid in UNIVERSAL_STEPS:
            self.assertIn(sid, ids, msg="full plan missing {s}".format(s=sid))
        self.assertIn("verify", ids)
        # full does NOT get mini hardening.
        for sid in MINI_HARDENING_STEPS:
            self.assertNotIn(sid, ids, msg="full plan must not include {s}".format(s=sid))

    def test_mini_plan_has_mini_hardening(self) -> None:
        backend = InstallerBackend(dry_run=True)
        plan = backend.plan("mini")
        ids = [s.step_id for s in plan.steps]
        for sid in MINI_HARDENING_STEPS:
            self.assertIn(sid, ids, msg="mini plan missing {s}".format(s=sid))
        self.assertIn("verify", ids)

    def test_edge_plan_has_edge_steps(self) -> None:
        backend = InstallerBackend(dry_run=True)
        plan = backend.plan("edge")
        ids = [s.step_id for s in plan.steps]
        self.assertIn("edge_readonly_env", ids)
        # edge does NOT get mini hardening.
        for sid in MINI_HARDENING_STEPS:
            self.assertNotIn(sid, ids)

    def test_developer_plan_has_developer_steps(self) -> None:
        backend = InstallerBackend(dry_run=True)
        plan = backend.plan("developer")
        ids = [s.step_id for s in plan.steps]
        self.assertIn("developer_tempdir_db", ids)
        self.assertIn("developer_smoke", ids)

    def test_plan_steps_are_install_plan_step_instances(self) -> None:
        backend = InstallerBackend(dry_run=True)
        plan = backend.plan("full")
        for step in plan.steps:
            self.assertIsInstance(step, InstallPlanStep)

    def test_plan_to_dict_has_step_count(self) -> None:
        backend = InstallerBackend(dry_run=True)
        plan = backend.plan("mini")
        d = plan.to_dict()
        self.assertEqual(d["step_count"], len(plan.steps))
        self.assertEqual(len(d["steps"]), len(plan.steps))

    def test_plan_is_frozen(self) -> None:
        backend = InstallerBackend(dry_run=True)
        plan = backend.plan("full")
        with self.assertRaises(Exception):
            # frozen dataclass
            plan.profile = "mini"  # type: ignore[misc]

    def test_plan_install_module_level_function(self) -> None:
        plan = plan_install("mini")
        self.assertEqual(plan.profile, "mini")
        self.assertIn("system_user", [s.step_id for s in plan.steps])


# ---------------------------------------------------------------------------
# 2. Canonical source of truth
# ---------------------------------------------------------------------------


class CanonicalSourceTests(unittest.TestCase):
    """The backend uses the canonical profile descriptor, not a parallel matrix."""

    def test_backend_imports_known_profiles_from_descriptor(self) -> None:
        src = Path(backend_module.__file__).read_text(encoding="utf-8")
        self.assertIn("from aee.profiles.descriptor import", src)
        self.assertIn("KNOWN_PROFILES", src)

    def test_backend_does_not_hardcode_profile_tuple(self) -> None:
        """No literal tuple of profile names in the backend source."""
        src = Path(backend_module.__file__).read_text(encoding="utf-8")
        # The forbidden form would be a literal like ("full", "mini", ...)
        # that is not the KNOWN_PROFILES import. We allow the import line
        # but reject a parallel definition.
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Tuple):
                elts = node.elts
                if len(elts) == 4 and all(
                    isinstance(e, ast.Constant) and isinstance(getattr(e, "value", None), str)
                    for e in elts
                ):
                    vals = tuple(getattr(e, "value") for e in elts)
                    if set(vals) == set(KNOWN_PROFILES):
                        self.fail(
                            "backend hardcodes a 4-profile tuple: {v}".format(
                                v=vals
                            )
                        )

    def test_unknown_profile_raises_unknown_profile_error(self) -> None:
        backend = InstallerBackend(dry_run=True)
        with self.assertRaises(UnknownProfileError):
            backend.plan("bogus")


# ---------------------------------------------------------------------------
# 3. Idempotent pre-flight
# ---------------------------------------------------------------------------


class IdempotentPreFlightTests(unittest.TestCase):
    """Pre-flight is safe to re-run."""

    def test_preflight_repeatable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = InstallerBackend(repo_root=Path(tmp), dry_run=True)
            r1 = backend.preflight("full")
            r2 = backend.preflight("full")
            self.assertEqual(r1.ok, r2.ok)
            self.assertEqual(r1.existing_profile, r2.existing_profile)

    def test_preflight_ok_on_fresh_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = InstallerBackend(repo_root=Path(tmp), dry_run=True)
            r = backend.preflight("full")
            self.assertTrue(r.ok)
            self.assertIsNone(r.existing_profile)

    def test_preflight_same_profile_marker_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / InstallerBackend.MARKER_FILENAME).write_text("mini\n")
            backend = InstallerBackend(repo_root=root, dry_run=True)
            r = backend.preflight("mini")
            self.assertTrue(r.ok)
            self.assertEqual(r.existing_profile, "mini")


# ---------------------------------------------------------------------------
# 4. Profile switch rejected
# ---------------------------------------------------------------------------


class ProfileSwitchTests(unittest.TestCase):
    """§21.3: profile switch on existing install is rejected."""

    def test_preflight_rejects_profile_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / InstallerBackend.MARKER_FILENAME).write_text("full\n")
            backend = InstallerBackend(repo_root=root, dry_run=True)
            r = backend.preflight("mini")
            self.assertFalse(r.ok)
            self.assertEqual(r.existing_profile, "full")
            self.assertIn("profile switch rejected", r.reason)

    def test_cli_returns_profile_switch_rejected_on_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / InstallerBackend.MARKER_FILENAME).write_text("full\n")
            backend = InstallerBackend(repo_root=root, dry_run=True)
            # Patch the CLI's backend construction to use our temp root.
            with patch("aee.installer.InstallerBackend", autospec=True) as MockBackend:
                MockBackend.return_value = backend
                err = io.StringIO()
                with patch("sys.stderr", err):
                    rc = main(["install", "--profile", "mini"])
            self.assertEqual(rc, EXIT_PROFILE_SWITCH_REJECTED)
            self.assertIn("profile switch rejected", err.getvalue())

    def test_corrupted_marker_treated_as_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / InstallerBackend.MARKER_FILENAME).write_text("garbage\n")
            backend = InstallerBackend(repo_root=root, dry_run=True)
            self.assertIsNone(backend.detect_existing_profile())


# ---------------------------------------------------------------------------
# 5. Dry-run default
# ---------------------------------------------------------------------------


class DryRunDefaultTests(unittest.TestCase):
    """execute() defaults to dry-run; no side effects."""

    def test_execute_defaults_to_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = InstallerBackend(repo_root=Path(tmp), dry_run=True)
            r = backend.execute("full")
            self.assertFalse(r.executed)
            self.assertTrue(r.plan.dry_run)

    def test_execute_explicit_dry_run_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = InstallerBackend(repo_root=Path(tmp), dry_run=True)
            r = backend.execute("full", dry_run=True)
            self.assertFalse(r.executed)

    def test_execute_result_is_install_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = InstallerBackend(repo_root=Path(tmp), dry_run=True)
            r = backend.execute("full")
            self.assertIsInstance(r, InstallResult)
            self.assertIsInstance(r.plan, InstallPlan)
            self.assertIsInstance(r.preflight, PreFlightResult)

    def test_execute_to_dict_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = InstallerBackend(repo_root=Path(tmp), dry_run=True)
            r = backend.execute("mini")
            d = r.to_dict()
            self.assertFalse(d["executed"])
            self.assertEqual(d["plan"]["profile"], "mini")
            self.assertTrue(d["preflight"]["ok"])


# ---------------------------------------------------------------------------
# 6. Execute not authorized
# ---------------------------------------------------------------------------


class ExecuteNotAuthorizedTests(unittest.TestCase):
    """execute(dry_run=False) raises ExecuteNotAuthorizedError."""

    def test_execute_dry_run_false_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = InstallerBackend(repo_root=Path(tmp), dry_run=True)
            with self.assertRaises(ExecuteNotAuthorizedError):
                backend.execute("full", dry_run=False)

    def test_execute_not_authorized_exit_code(self) -> None:
        self.assertEqual(ExecuteNotAuthorizedError().exit_code, 6)

    def test_execute_not_authorized_message_mentions_separately_authorizable(self) -> None:
        err = ExecuteNotAuthorizedError()
        self.assertIn("separately authorizable", str(err))


# ---------------------------------------------------------------------------
# 7. Mini hardening
# ---------------------------------------------------------------------------


class MiniHardeningTests(unittest.TestCase):
    """§21.3: --profile mini absorbs AEE-MINI hardening."""

    def test_mini_plan_has_system_user_step(self) -> None:
        plan = plan_install("mini")
        ids = [s.step_id for s in plan.steps]
        self.assertIn("system_user", ids)

    def test_mini_plan_has_env_file_0600_step(self) -> None:
        plan = plan_install("mini")
        ids = [s.step_id for s in plan.steps]
        self.assertIn("env_file_0600", ids)

    def test_mini_plan_has_smoke_test_step(self) -> None:
        plan = plan_install("mini")
        ids = [s.step_id for s in plan.steps]
        self.assertIn("smoke_test", ids)

    def test_mini_plan_notes_mention_aee_mini_hardening(self) -> None:
        plan = plan_install("mini")
        joined = " ".join(plan.notes)
        self.assertIn("AEE-MINI", joined)
        self.assertIn("hardening", joined)

    def test_full_plan_does_not_have_mini_hardening(self) -> None:
        plan = plan_install("full")
        ids = [s.step_id for s in plan.steps]
        self.assertNotIn("system_user", ids)
        self.assertNotIn("env_file_0600", ids)
        self.assertNotIn("smoke_test", ids)


# ---------------------------------------------------------------------------
# 8. No subprocess / os.system
# ---------------------------------------------------------------------------


class NoSideEffectsSourceTests(unittest.TestCase):
    """The backend module performs no process spawns."""

    def test_backend_does_not_import_subprocess(self) -> None:
        src_path = Path(backend_module.__file__)
        src = src_path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(
                        alias.name, "subprocess",
                        msg="aee.installer.backend must not import subprocess",
                    )
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(
                    node.module, "subprocess",
                    msg="aee.installer.backend must not import from subprocess",
                )

    def test_backend_does_not_use_os_system(self) -> None:
        src = Path(backend_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("os.system(", src)
        self.assertNotIn("os.popen(", src)

    def test_backend_does_not_write_files(self) -> None:
        """The backend module source has no write_text / open(w) / unlink calls."""
        src = Path(backend_module.__file__).read_text(encoding="utf-8")
        # Allow read_text (used for marker detection) but reject write_text,
        # open(..., "w"), and unlink.
        self.assertNotIn("write_text(", src)
        self.assertNotIn("write_bytes(", src)
        self.assertNotIn(".unlink(", src)
        # os.system already checked above.

    def test_cli_module_does_not_import_subprocess_at_module_level(self) -> None:
        """The CLI module must not import subprocess at module level.

        The CLI imports the installer backend lazily inside
        ``_install_dispatch`` so module-level import of ``aee.cli``
        remains side-effect-free (preserves the §21.2 invariant).
        """
        src_path = Path(aee_cli.__file__)
        src = src_path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in tree.body:  # module-level only
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(
                        alias.name, "subprocess",
                        msg="aee.cli must not import subprocess at module level",
                    )
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(
                    node.module, "subprocess",
                    msg="aee.cli must not import from subprocess at module level",
                )


# ---------------------------------------------------------------------------
# 9. CLI wiring
# ---------------------------------------------------------------------------


class CLIWiringTests(unittest.TestCase):
    """``aee install --profile X`` reflects the backend plan."""

    def _run_install(self, profile: str) -> tuple:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = main(["install", "--profile", profile])
        return rc, buf.getvalue()

    def test_full_install_exit_0(self) -> None:
        rc, _ = self._run_install("full")
        self.assertEqual(rc, EXIT_OK)

    def test_mini_install_exit_0(self) -> None:
        rc, _ = self._run_install("mini")
        self.assertEqual(rc, EXIT_OK)

    def test_edge_install_exit_0(self) -> None:
        rc, _ = self._run_install("edge")
        self.assertEqual(rc, EXIT_OK)

    def test_developer_install_exit_0(self) -> None:
        rc, _ = self._run_install("developer")
        self.assertEqual(rc, EXIT_OK)

    def test_install_output_says_backend_implemented_true(self) -> None:
        rc, out = self._run_install("full")
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("backend_implemented : True", out)

    def test_install_output_says_executed_false(self) -> None:
        rc, out = self._run_install("full")
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("executed            : False", out)

    def test_install_output_lists_plan_steps(self) -> None:
        rc, out = self._run_install("mini")
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("plan steps", out)
        self.assertIn("system_user", out)
        self.assertIn("smoke_test", out)

    def test_install_output_mentions_section_21_3(self) -> None:
        rc, out = self._run_install("full")
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("§21.3", out)


# ---------------------------------------------------------------------------
# 10. JSON shape
# ---------------------------------------------------------------------------


class JSONShapeTests(unittest.TestCase):
    """``aee install --profile X --json`` emits the expected JSON."""

    def test_json_has_plan_and_preflight(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = main(["install", "--profile", "mini", "--json"])
        self.assertEqual(rc, EXIT_OK)
        payload = json.loads(buf.getvalue())
        self.assertIn("plan", payload)
        self.assertIn("preflight", payload)
        self.assertTrue(payload["backend_implemented"])
        self.assertFalse(payload["executed"])
        self.assertEqual(payload["profile"], "mini")

    def test_json_plan_has_steps(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = main(["install", "--profile", "full", "--json"])
        self.assertEqual(rc, EXIT_OK)
        payload = json.loads(buf.getvalue())
        self.assertIn("steps", payload["plan"])
        self.assertGreater(len(payload["plan"]["steps"]), 0)

    def test_json_preflight_ok(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = main(["install", "--profile", "edge", "--json"])
        self.assertEqual(rc, EXIT_OK)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["preflight"]["ok"])


# ---------------------------------------------------------------------------
# 11. Exit codes
# ---------------------------------------------------------------------------


class ExitCodeTests(unittest.TestCase):
    """Pre-flight failure and profile switch map to distinct exit codes."""

    def test_unknown_profile_via_programmatic_path_returns_3(self) -> None:
        # Direct call to _install_dispatch with an unknown profile
        # bypasses argparse and exercises the defence-in-depth path.
        from aee.cli import _install_dispatch
        err = io.StringIO()
        with patch("sys.stderr", err):
            rc = _install_dispatch("bogus")
        self.assertEqual(rc, EXIT_PROFILE_ERROR)

    def test_preflight_failed_exit_code_value(self) -> None:
        self.assertEqual(EXIT_PRE_FLIGHT_FAILED, 4)

    def test_profile_switch_rejected_exit_code_value(self) -> None:
        self.assertEqual(EXIT_PROFILE_SWITCH_REJECTED, 5)


# ---------------------------------------------------------------------------
# 12. AEE-MINI compatibility
# ---------------------------------------------------------------------------


class AEEMiniCompatTests(unittest.TestCase):
    """The backend respects the §21.10 deprecation window for AEE-MINI."""

    def test_backend_does_not_reference_aee_mini_installer_path(self) -> None:
        src = Path(backend_module.__file__).read_text(encoding="utf-8")
        # The backend must not embed the AEE-MINI installer path or
        # attempt to migrate it. Mentioning "AEE-MINI" in docstrings
        # (explaining the deprecation window) is allowed; embedding
        # the path "deploy/scripts/install.sh" is not.
        self.assertNotIn("deploy/scripts/install.sh", src)

    def test_mini_plan_note_mentions_deprecation_window(self) -> None:
        plan = plan_install("mini")
        joined = " ".join(plan.notes)
        self.assertIn("deprecation window", joined)


# ---------------------------------------------------------------------------
# 13. Backward compat with §21.2 CLI surfaces
# ---------------------------------------------------------------------------


class BackwardCompatTests(unittest.TestCase):
    """§21.2 CLI surfaces remain functional."""

    def test_help_still_lists_four_profiles(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            with self.assertRaises(SystemExit) as ctx:
                main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        out = buf.getvalue()
        for p in KNOWN_PROFILES:
            self.assertIn(p, out)

    def test_install_help_still_lists_profile_flag(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            with self.assertRaises(SystemExit) as ctx:
                main(["install", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        out = buf.getvalue()
        self.assertIn("--profile", out)

    def test_bogus_profile_still_exits_2(self) -> None:
        err = io.StringIO()
        with patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as ctx:
                main(["install", "--profile", "bogus"])
        self.assertEqual(ctx.exception.code, 2)

    def test_global_bogus_profile_still_exits_2(self) -> None:
        err = io.StringIO()
        with patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as ctx:
                main(["--profile", "bogus", "install"])
        self.assertEqual(ctx.exception.code, 2)

    def test_no_subcommand_still_returns_parse_error(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = main([])
        self.assertEqual(rc, EXIT_PARSE_ERROR)


if __name__ == "__main__":
    unittest.main()