"""AEE Phase 4B — ``aee install`` CLI targeted tests.

These tests verify the Phase 4B installer CLI surface
(:mod:`aee.installer.cli_install`) and the ``aee install`` argparse
wiring for the four approved flags: ``--execute``, ``--resume``,
``--from <ref>``, ``--rollback-to <ref>``.

Coverage:

1. **InstallCliOptions dataclass** — defaults, to_dict shape, frozen.
2. **run_install dry-run default** — no flags → exit 0, executed=False.
3. **--execute** — exit 6 (ExecuteNotAuthorized), execute_requested=True,
   executed=False, pre-flight ok.
4. **--resume** (audit-only) — exit 0, resume=True, executed=False,
   note mentions --resume.
5. **--from <ref>** (audit-only) — exit 0, from_ref recorded, note
   mentions the ref, no git operations.
6. **--rollback-to <ref>** (audit-only) — exit 0, rollback_to recorded.
7. **All flags combined** — exit 6 (because --execute), all metadata
   recorded.
8. **Unknown profile** — exit 3, plan=None, preflight=None.
9. **Profile switch rejected** — exit 5 when existing marker differs.
10. **Pre-flight failed** — exit 4 when repo_root missing.
11. **to_dict JSON-serializable** — full result serializes via
    ``json.dumps`` without raising.
12. **CLI plumbing** — ``aee install --execute`` returns 6; ``--json``
    emits a JSON object with ``phase=4B``; ``aee install`` (no flags)
    preserves the Phase 9.2 dispatch path (backward compat).
13. **No subprocess** — AST scan of cli_install.py confirms no
    ``subprocess`` / ``os.system`` / ``os.popen`` usage.
14. **Approved flags only** — argparse rejects unknown flags like
    ``--update`` (the update CLI is OUT of scope).
15. **Backward compat** — ``aee install --profile mini`` with no
    Phase 4B flags still uses the Phase 9.2 dispatch (exact stdout
    text match).
"""
from __future__ import annotations

import ast
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aee.cli import main
from aee.installer.backend import (
    EXIT_EXECUTE_NOT_AUTHORIZED,
    EXIT_OK,
    EXIT_PRE_FLIGHT_FAILED,
    EXIT_PROFILE_INVALID,
    EXIT_PROFILE_SWITCH_REJECTED,
)
from aee.installer.cli_install import (
    InstallCliOptions,
    InstallCliResult,
    run_install,
)
from aee.profiles.descriptor import DEFAULT_PROFILE, KNOWN_PROFILES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_cli(argv):
    """Run the CLI with argv, capturing stdout/stderr. Returns (rc, out, err)."""
    buf = io.StringIO()
    err = io.StringIO()
    with patch("sys.stdout", buf), patch("sys.stderr", err):
        rc = main(argv)
    return rc, buf.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# 1. InstallCliOptions dataclass
# ---------------------------------------------------------------------------


class InstallCliOptionsTests(unittest.TestCase):
    """``InstallCliOptions`` defaults + to_dict + frozen."""

    def test_defaults(self) -> None:
        opts = InstallCliOptions()
        self.assertEqual(opts.profile, DEFAULT_PROFILE)
        self.assertFalse(opts.execute)
        self.assertFalse(opts.resume)
        self.assertIsNone(opts.from_ref)
        self.assertIsNone(opts.rollback_to)
        self.assertIsNone(opts.repo_root)

    def test_to_dict_shape(self) -> None:
        opts = InstallCliOptions(
            profile="mini",
            execute=True,
            resume=True,
            from_ref="v2.0.0",
            rollback_to="v1.9.0",
            repo_root=Path("/tmp/test"),
        )
        d = opts.to_dict()
        self.assertEqual(d["profile"], "mini")
        self.assertTrue(d["execute"])
        self.assertTrue(d["resume"])
        self.assertEqual(d["from_ref"], "v2.0.0")
        self.assertEqual(d["rollback_to"], "v1.9.0")
        self.assertEqual(d["repo_root"], "/tmp/test")

    def test_frozen(self) -> None:
        opts = InstallCliOptions()
        with self.assertRaises((AttributeError, Exception)):
            opts.execute = True  # type: ignore[misc]

    def test_to_dict_repo_root_none(self) -> None:
        opts = InstallCliOptions()
        self.assertIsNone(opts.to_dict()["repo_root"])


# ---------------------------------------------------------------------------
# 2. run_install — dry-run default
# ---------------------------------------------------------------------------


class RunInstallDryRunTests(unittest.TestCase):
    """``run_install`` with no flags → exit 0, executed=False."""

    def test_default_exit_0(self) -> None:
        result = run_install(InstallCliOptions())
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertFalse(result.executed)
        self.assertFalse(result.execute_requested)

    def test_default_plan_present(self) -> None:
        result = run_install(InstallCliOptions())
        self.assertIsNotNone(result.plan)
        self.assertTrue(len(result.plan.steps) > 0)

    def test_default_preflight_ok(self) -> None:
        result = run_install(InstallCliOptions())
        self.assertIsNotNone(result.preflight)
        self.assertTrue(result.preflight.ok)

    def test_default_no_error(self) -> None:
        result = run_install(InstallCliOptions())
        self.assertEqual(result.error, "")


# ---------------------------------------------------------------------------
# 3. --execute
# ---------------------------------------------------------------------------


class ExecuteFlagTests(unittest.TestCase):
    """``--execute`` → exit 6, execute_requested=True, executed=False."""

    def test_execute_exit_6(self) -> None:
        result = run_install(InstallCliOptions(execute=True))
        self.assertEqual(result.exit_code, EXIT_EXECUTE_NOT_AUTHORIZED)

    def test_execute_requested_recorded(self) -> None:
        result = run_install(InstallCliOptions(execute=True))
        self.assertTrue(result.execute_requested)

    def test_execute_not_executed(self) -> None:
        result = run_install(InstallCliOptions(execute=True))
        self.assertFalse(result.executed)

    def test_execute_note_present(self) -> None:
        result = run_install(InstallCliOptions(execute=True))
        self.assertTrue(any("--execute" in n for n in result.notes))

    def test_execute_preflight_still_ok(self) -> None:
        result = run_install(InstallCliOptions(execute=True))
        self.assertIsNotNone(result.preflight)
        self.assertTrue(result.preflight.ok)


# ---------------------------------------------------------------------------
# 4. --resume (audit-only)
# ---------------------------------------------------------------------------


class ResumeFlagTests(unittest.TestCase):
    """``--resume`` → exit 0, resume=True, note mentions --resume."""

    def test_resume_exit_0(self) -> None:
        result = run_install(InstallCliOptions(resume=True))
        self.assertEqual(result.exit_code, EXIT_OK)

    def test_resume_recorded(self) -> None:
        result = run_install(InstallCliOptions(resume=True))
        self.assertTrue(result.resume)

    def test_resume_note(self) -> None:
        result = run_install(InstallCliOptions(resume=True))
        self.assertTrue(any("--resume" in n for n in result.notes))

    def test_resume_not_executed(self) -> None:
        result = run_install(InstallCliOptions(resume=True))
        self.assertFalse(result.executed)


# ---------------------------------------------------------------------------
# 5. --from <ref> (audit-only)
# ---------------------------------------------------------------------------


class FromRefFlagTests(unittest.TestCase):
    """``--from <ref>`` → exit 0, from_ref recorded, note mentions ref."""

    def test_from_ref_exit_0(self) -> None:
        result = run_install(InstallCliOptions(from_ref="v2.0.0"))
        self.assertEqual(result.exit_code, EXIT_OK)

    def test_from_ref_recorded(self) -> None:
        result = run_install(InstallCliOptions(from_ref="v2.0.0"))
        self.assertEqual(result.from_ref, "v2.0.0")

    def test_from_ref_note(self) -> None:
        result = run_install(InstallCliOptions(from_ref="v2.0.0"))
        self.assertTrue(any("v2.0.0" in n for n in result.notes))

    def test_from_ref_no_git_ops(self) -> None:
        result = run_install(InstallCliOptions(from_ref="v2.0.0"))
        self.assertFalse(result.executed)


# ---------------------------------------------------------------------------
# 6. --rollback-to <ref> (audit-only)
# ---------------------------------------------------------------------------


class RollbackToFlagTests(unittest.TestCase):
    """``--rollback-to <ref>`` → exit 0, rollback_to recorded."""

    def test_rollback_exit_0(self) -> None:
        result = run_install(InstallCliOptions(rollback_to="v1.9.0"))
        self.assertEqual(result.exit_code, EXIT_OK)

    def test_rollback_recorded(self) -> None:
        result = run_install(InstallCliOptions(rollback_to="v1.9.0"))
        self.assertEqual(result.rollback_to, "v1.9.0")

    def test_rollback_note(self) -> None:
        result = run_install(InstallCliOptions(rollback_to="v1.9.0"))
        self.assertTrue(any("v1.9.0" in n for n in result.notes))


# ---------------------------------------------------------------------------
# 7. All flags combined
# ---------------------------------------------------------------------------


class AllFlagsCombinedTests(unittest.TestCase):
    """All four flags → exit 6 (because --execute), all metadata recorded."""

    def test_all_flags_exit_6(self) -> None:
        result = run_install(InstallCliOptions(
            execute=True,
            resume=True,
            from_ref="v2.0.0",
            rollback_to="v1.9.0",
        ))
        self.assertEqual(result.exit_code, EXIT_EXECUTE_NOT_AUTHORIZED)

    def test_all_flags_metadata(self) -> None:
        result = run_install(InstallCliOptions(
            execute=True,
            resume=True,
            from_ref="v2.0.0",
            rollback_to="v1.9.0",
        ))
        self.assertTrue(result.execute_requested)
        self.assertTrue(result.resume)
        self.assertEqual(result.from_ref, "v2.0.0")
        self.assertEqual(result.rollback_to, "v1.9.0")

    def test_all_flags_not_executed(self) -> None:
        result = run_install(InstallCliOptions(
            execute=True,
            resume=True,
            from_ref="v2.0.0",
            rollback_to="v1.9.0",
        ))
        self.assertFalse(result.executed)


# ---------------------------------------------------------------------------
# 8. Unknown profile
# ---------------------------------------------------------------------------


class UnknownProfileTests(unittest.TestCase):
    """Unknown profile → exit 3, plan=None, preflight=None."""

    def test_unknown_profile_exit_3(self) -> None:
        result = run_install(InstallCliOptions(profile="bogus"))
        self.assertEqual(result.exit_code, EXIT_PROFILE_INVALID)

    def test_unknown_profile_no_plan(self) -> None:
        result = run_install(InstallCliOptions(profile="bogus"))
        self.assertIsNone(result.plan)

    def test_unknown_profile_no_preflight(self) -> None:
        result = run_install(InstallCliOptions(profile="bogus"))
        self.assertIsNone(result.preflight)

    def test_unknown_profile_error_msg(self) -> None:
        result = run_install(InstallCliOptions(profile="bogus"))
        self.assertIn("unknown profile", result.error)

    def test_unknown_profile_with_execute(self) -> None:
        result = run_install(InstallCliOptions(profile="bogus", execute=True))
        self.assertEqual(result.exit_code, EXIT_PROFILE_INVALID)
        self.assertTrue(result.execute_requested)


# ---------------------------------------------------------------------------
# 9. Profile switch rejected
# ---------------------------------------------------------------------------


class ProfileSwitchTests(unittest.TestCase):
    """Existing marker with different profile → exit 5."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        # Write a marker for 'mini' then request 'full'.
        marker = Path(self.tmpdir) / ".aee-profile"
        marker.write_text("mini", encoding="utf-8")

    def test_profile_switch_exit_5(self) -> None:
        result = run_install(InstallCliOptions(
            profile="full",
            repo_root=Path(self.tmpdir),
        ))
        self.assertEqual(result.exit_code, EXIT_PROFILE_SWITCH_REJECTED)

    def test_profile_switch_error(self) -> None:
        result = run_install(InstallCliOptions(
            profile="full",
            repo_root=Path(self.tmpdir),
        ))
        self.assertIn("profile switch", result.error.lower())

    def test_profile_switch_with_execute(self) -> None:
        result = run_install(InstallCliOptions(
            profile="full",
            repo_root=Path(self.tmpdir),
            execute=True,
        ))
        self.assertEqual(result.exit_code, EXIT_PROFILE_SWITCH_REJECTED)


# ---------------------------------------------------------------------------
# 10. Pre-flight failed (repo_root missing)
# ---------------------------------------------------------------------------


class PreFlightFailedTests(unittest.TestCase):
    """Missing repo_root → exit 4."""

    def test_missing_repo_root_exit_4(self) -> None:
        result = run_install(InstallCliOptions(
            profile="full",
            repo_root=Path("/nonexistent/path/that/does/not/exist"),
        ))
        self.assertEqual(result.exit_code, EXIT_PRE_FLIGHT_FAILED)

    def test_missing_repo_root_with_execute(self) -> None:
        result = run_install(InstallCliOptions(
            profile="full",
            repo_root=Path("/nonexistent/path/that/does/not/exist"),
            execute=True,
        ))
        self.assertEqual(result.exit_code, EXIT_PRE_FLIGHT_FAILED)


# ---------------------------------------------------------------------------
# 11. to_dict JSON-serializable
# ---------------------------------------------------------------------------


class JsonSerializableTests(unittest.TestCase):
    """``InstallCliResult.to_dict()`` is JSON-serializable."""

    def test_dry_run_serializable(self) -> None:
        result = run_install(InstallCliOptions())
        json.dumps(result.to_dict())

    def test_execute_serializable(self) -> None:
        result = run_install(InstallCliOptions(execute=True))
        json.dumps(result.to_dict())

    def test_all_flags_serializable(self) -> None:
        result = run_install(InstallCliOptions(
            execute=True,
            resume=True,
            from_ref="v2.0.0",
            rollback_to="v1.9.0",
        ))
        json.dumps(result.to_dict())

    def test_unknown_profile_serializable(self) -> None:
        result = run_install(InstallCliOptions(profile="bogus"))
        json.dumps(result.to_dict())


# ---------------------------------------------------------------------------
# 12. CLI plumbing
# ---------------------------------------------------------------------------


class CliPlumbingTests(unittest.TestCase):
    """CLI ``aee install`` with Phase 4B flags routes correctly."""

    def test_cli_execute_returns_6(self) -> None:
        rc, out, err = _run_cli(["install", "--execute"])
        self.assertEqual(rc, EXIT_EXECUTE_NOT_AUTHORIZED)
        self.assertIn("Phase 4B", out)

    def test_cli_execute_profile_mini(self) -> None:
        rc, out, err = _run_cli(["install", "--profile", "mini", "--execute"])
        self.assertEqual(rc, EXIT_EXECUTE_NOT_AUTHORIZED)
        self.assertIn("mini", out)

    def test_cli_resume_returns_0(self) -> None:
        rc, out, err = _run_cli(["install", "--resume"])
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("resume", out.lower())

    def test_cli_from_ref(self) -> None:
        rc, out, err = _run_cli(["install", "--from", "v2.0.0"])
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("v2.0.0", out)

    def test_cli_rollback_to(self) -> None:
        rc, out, err = _run_cli(["install", "--rollback-to", "v1.9.0"])
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("v1.9.0", out)

    def test_cli_json_output(self) -> None:
        rc, out, err = _run_cli(["install", "--execute", "--json"])
        self.assertEqual(rc, EXIT_EXECUTE_NOT_AUTHORIZED)
        payload = json.loads(out)
        self.assertEqual(payload["phase"], "4B")
        self.assertTrue(payload["execute_requested"])
        self.assertEqual(payload["exit_code"], EXIT_EXECUTE_NOT_AUTHORIZED)

    def test_cli_json_dry_run(self) -> None:
        rc, out, err = _run_cli(["install", "--resume", "--json"])
        self.assertEqual(rc, EXIT_OK)
        payload = json.loads(out)
        self.assertTrue(payload["resume"])
        self.assertEqual(payload["exit_code"], EXIT_OK)


# ---------------------------------------------------------------------------
# 13. No subprocess
# ---------------------------------------------------------------------------


class NoSubprocessTests(unittest.TestCase):
    """``cli_install.py`` does not import subprocess or use os.system."""

    def test_no_subprocess_import(self) -> None:
        import aee.installer.cli_install as mod
        source = open(mod.__file__).read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(
                        alias.name, ("subprocess",),
                        msg="subprocess import found",
                    )
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn(
                    node.module, ("subprocess",),
                    msg="subprocess import found",
                )

    def test_no_os_system_call(self) -> None:
        import aee.installer.cli_install as mod
        source = open(mod.__file__).read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                self.assertNotIn(
                    node.attr, ("system", "popen"),
                    msg="os.system / os.popen call found",
                )


# ---------------------------------------------------------------------------
# 14. Approved flags only — --update rejected
# ---------------------------------------------------------------------------


class ApprovedFlagsOnlyTests(unittest.TestCase):
    """Argparse rejects unknown flags (update CLI is OUT of scope)."""

    def test_update_flag_rejected(self) -> None:
        err = io.StringIO()
        with patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as cm:
                main(["install", "--update"])
        # argparse exits 2 on unknown arguments.
        self.assertEqual(cm.exception.code, 2)

    def test_force_flag_rejected(self) -> None:
        err = io.StringIO()
        with patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as cm:
                main(["install", "--force"])
        self.assertEqual(cm.exception.code, 2)


# ---------------------------------------------------------------------------
# 15. Backward compat — no flags → Phase 9.2 dispatch
# ---------------------------------------------------------------------------


class BackwardCompatTests(unittest.TestCase):
    """``aee install`` with no Phase 4B flags preserves Phase 9.2 output."""

    def test_no_flags_uses_phase92_dispatch(self) -> None:
        rc, out, err = _run_cli(["install"])
        self.assertEqual(rc, EXIT_OK)
        # Phase 9.2 output header.
        self.assertIn("aee install (dry-run / §21.3 installer backend)", out)
        self.assertIn("profile (resolved)  : full", out)

    def test_no_flags_profile_mini(self) -> None:
        rc, out, err = _run_cli(["install", "--profile", "mini"])
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("profile (resolved)  : mini", out)

    def test_no_flags_json_still_works(self) -> None:
        rc, out, err = _run_cli(["install", "--json"])
        self.assertEqual(rc, EXIT_OK)
        payload = json.loads(out)
        # Phase 9.2 JSON shape does NOT have phase=4B.
        self.assertNotIn("phase", payload)

    def test_each_profile_backward_compat(self) -> None:
        for profile in KNOWN_PROFILES:
            with self.subTest(profile=profile):
                rc, out, err = _run_cli(["install", "--profile", profile])
                self.assertEqual(rc, EXIT_OK, msg=err)
                self.assertIn(
                    "profile (resolved)  : {p}".format(p=profile),
                    out,
                )


# ---------------------------------------------------------------------------
# 16. Exit code vocabulary — no new codes outside {0,3,4,5,6}
# ---------------------------------------------------------------------------


class ExitCodeVocabularyTests(unittest.TestCase):
    """Phase 4B reuses the existing exit code vocabulary."""

    def test_all_exit_codes_in_allowed_set(self) -> None:
        allowed = {EXIT_OK, EXIT_PROFILE_INVALID, EXIT_PRE_FLIGHT_FAILED,
                   EXIT_PROFILE_SWITCH_REJECTED, EXIT_EXECUTE_NOT_AUTHORIZED}
        # Dry-run
        self.assertIn(run_install(InstallCliOptions()).exit_code, allowed)
        # Execute
        self.assertIn(
            run_install(InstallCliOptions(execute=True)).exit_code, allowed
        )
        # Resume
        self.assertIn(
            run_install(InstallCliOptions(resume=True)).exit_code, allowed
        )
        # Unknown profile
        self.assertIn(
            run_install(InstallCliOptions(profile="bogus")).exit_code, allowed
        )


if __name__ == "__main__":
    unittest.main()