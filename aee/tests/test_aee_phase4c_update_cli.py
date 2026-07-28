"""AEE Phase 4C — ``aee update`` CLI targeted tests.

These tests verify the Phase 4C update CLI surface
(:mod:`aee.installer.update`) and the ``aee update`` argparse
wiring for the seven approved flags: ``--channel``, ``--ref``,
``--yes``, ``--offline-bundle``, ``--log-format``, ``--execute``,
``--json``.

Coverage:

1. **UpdateCliOptions dataclass** — defaults, to_dict shape, frozen.
2. **run_update dry-run default** — no flags → exit 0, executed=False,
   drift projected.
3. **--execute** — exit 6 (ExecuteNotAuthorized), execute_requested=True,
   executed=False, pre-flight ok.
4. **--channel** — switching channel recorded; default stable.
5. **--ref** (audit-only) — exit 0, ref recorded, note mentions the ref,
   no git operations.
6. **--yes** (audit-only) — exit 0, yes=True, note mentions --yes.
7. **--offline-bundle** (audit-only) — exit 0, recorded.
8. **--log-format** (audit-only) — exit 0, recorded.
9. **All flags combined** — exit 6 (because --execute), all metadata
   recorded.
10. **Unknown profile** — exit 3, plan=None, preflight=None.
11. **Unknown channel** — exit 3 (reuses profile-invalid code for
    unknown channel), plan=None.
12. **Profile switch rejected** — exit 5 when existing marker differs.
13. **Pre-flight failed** — exit 4 when repo_root missing.
14. **Drift detection** — projected drift surfaces exit 9 in dry-run
    when the recorded pin mismatches the on-disk HEAD.
15. **Drift detection — no pin marker** — no drift (fresh install).
16. **to_dict JSON-serializable** — full result serializes via
    ``json.dumps`` without raising.
17. **CLI plumbing** — ``aee update --execute`` returns 6; ``--json``
    emits a JSON object with ``phase=4C``; ``aee update`` (no flags)
    returns 0.
18. **No subprocess** — AST scan of update.py confirms no
    ``subprocess`` / ``os.system`` / ``os.popen`` usage.
19. **Approved flags only** — argparse rejects unknown flags like
    ``--update`` (no recursion).
20. **Backward compat** — existing install/doctor/prepare subcommands
    still work; ``aee install`` (no flags) preserves Phase 9.2 dispatch.
21. **DriftResult to_dict** — shape + JSON-serializable.
22. **KNOWN_CHANNELS / DEFAULT_CHANNEL** — vocabulary pinned.
23. **validate_channel** — accepts known, rejects unknown.
24. **detect_drift** — read-only, returns DriftResult.
25. **Exit code vocabulary** — no new codes outside {0,3,4,5,6,9}.

Run: ``PYTHONPATH=. python3 -m unittest aee.tests.test_aee_phase4c_update_cli -v``
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
from aee.installer.lifecycle import EXIT_DRIFT_DETECTED
from aee.installer.update import (
    DEFAULT_CHANNEL,
    KNOWN_CHANNELS,
    DriftResult,
    UpdateCliOptions,
    UpdateCliResult,
    detect_drift,
    run_update,
    validate_channel,
)


# ---------------------------------------------------------------------------#
# Helpers
# ---------------------------------------------------------------------------#


def _run_cli(argv):
    """Run the CLI with argv, capturing stdout/stderr. Returns (rc, out, err)."""
    buf = io.StringIO()
    err = io.StringIO()
    with patch("sys.stdout", buf), patch("sys.stderr", err):
        rc = main(argv)
    return rc, buf.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------#
# 1. UpdateCliOptions dataclass
# ---------------------------------------------------------------------------#


class UpdateCliOptionsTests(unittest.TestCase):
    """``UpdateCliOptions`` defaults + to_dict + frozen."""

    def test_defaults(self) -> None:
        opts = UpdateCliOptions()
        self.assertEqual(opts.profile, "full")
        self.assertEqual(opts.channel, DEFAULT_CHANNEL)
        self.assertIsNone(opts.ref)
        self.assertFalse(opts.yes)
        self.assertIsNone(opts.offline_bundle)
        self.assertIsNone(opts.log_format)
        self.assertFalse(opts.execute)
        self.assertIsNone(opts.repo_root)

    def test_to_dict_shape(self) -> None:
        opts = UpdateCliOptions(
            profile="mini",
            channel="rc",
            ref="v2.0.0",
            yes=True,
            offline_bundle="/tmp/bundle.tar",
            log_format="json",
            execute=True,
            repo_root=Path("/tmp/test"),
        )
        d = opts.to_dict()
        self.assertEqual(d["profile"], "mini")
        self.assertEqual(d["channel"], "rc")
        self.assertEqual(d["ref"], "v2.0.0")
        self.assertTrue(d["yes"])
        self.assertEqual(d["offline_bundle"], "/tmp/bundle.tar")
        self.assertEqual(d["log_format"], "json")
        self.assertTrue(d["execute"])
        self.assertEqual(d["repo_root"], "/tmp/test")

    def test_frozen(self) -> None:
        opts = UpdateCliOptions()
        with self.assertRaises((AttributeError, Exception)):
            opts.execute = True  # type: ignore[misc]

    def test_to_dict_repo_root_none(self) -> None:
        opts = UpdateCliOptions()
        self.assertIsNone(opts.to_dict()["repo_root"])


# ---------------------------------------------------------------------------#
# 2. run_update — dry-run default
# ---------------------------------------------------------------------------#


class RunUpdateDryRunTests(unittest.TestCase):
    """``run_update`` with no flags → exit 0, executed=False."""

    def test_default_exit_0(self) -> None:
        result = run_update(UpdateCliOptions())
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertFalse(result.executed)
        self.assertFalse(result.execute_requested)

    def test_default_plan_present(self) -> None:
        result = run_update(UpdateCliOptions())
        self.assertIsNotNone(result.plan)
        self.assertTrue(len(result.plan.steps) > 0)

    def test_default_preflight_ok(self) -> None:
        result = run_update(UpdateCliOptions())
        self.assertIsNotNone(result.preflight)
        self.assertTrue(result.preflight.ok)

    def test_default_no_error(self) -> None:
        result = run_update(UpdateCliOptions())
        # No drift in repo root (cwd is the repo, no pin marker).
        self.assertEqual(result.error, "")

    def test_default_drift_present(self) -> None:
        result = run_update(UpdateCliOptions())
        self.assertIsNotNone(result.drift)

    def test_default_channel_stable(self) -> None:
        result = run_update(UpdateCliOptions())
        self.assertEqual(result.channel, "stable")


# ---------------------------------------------------------------------------#
# 3. --execute
# ---------------------------------------------------------------------------#


class ExecuteFlagTests(unittest.TestCase):
    """``--execute`` → exit 6, execute_requested=True, executed=False."""

    def test_execute_exit_6(self) -> None:
        result = run_update(UpdateCliOptions(execute=True))
        self.assertEqual(result.exit_code, EXIT_EXECUTE_NOT_AUTHORIZED)

    def test_execute_requested_recorded(self) -> None:
        result = run_update(UpdateCliOptions(execute=True))
        self.assertTrue(result.execute_requested)

    def test_execute_not_executed(self) -> None:
        result = run_update(UpdateCliOptions(execute=True))
        self.assertFalse(result.executed)

    def test_execute_note_present(self) -> None:
        result = run_update(UpdateCliOptions(execute=True))
        self.assertTrue(any("--execute" in n for n in result.notes))

    def test_execute_preflight_still_ok(self) -> None:
        result = run_update(UpdateCliOptions(execute=True))
        self.assertIsNotNone(result.preflight)
        self.assertTrue(result.preflight.ok)


# ---------------------------------------------------------------------------#
# 4. --channel
# ---------------------------------------------------------------------------#


class ChannelFlagTests(unittest.TestCase):
    """``--channel`` switching recorded; default stable."""

    def test_channel_default_stable(self) -> None:
        result = run_update(UpdateCliOptions())
        self.assertEqual(result.channel, "stable")

    def test_channel_rc(self) -> None:
        result = run_update(UpdateCliOptions(channel="rc"))
        self.assertEqual(result.channel, "rc")
        self.assertEqual(result.exit_code, EXIT_OK)

    def test_channel_dev(self) -> None:
        result = run_update(UpdateCliOptions(channel="dev"))
        self.assertEqual(result.channel, "dev")
        self.assertEqual(result.exit_code, EXIT_OK)


# ---------------------------------------------------------------------------#
# 5. --ref (audit-only)
# ---------------------------------------------------------------------------#


class RefFlagTests(unittest.TestCase):
    """``--ref <ref>`` → exit 0, ref recorded, note mentions ref."""

    def test_ref_exit_0(self) -> None:
        result = run_update(UpdateCliOptions(ref="v2.0.0"))
        self.assertEqual(result.exit_code, EXIT_OK)

    def test_ref_recorded(self) -> None:
        result = run_update(UpdateCliOptions(ref="v2.0.0"))
        self.assertEqual(result.ref, "v2.0.0")

    def test_ref_note(self) -> None:
        result = run_update(UpdateCliOptions(ref="v2.0.0"))
        self.assertTrue(any("v2.0.0" in n for n in result.notes))

    def test_ref_no_git_ops(self) -> None:
        result = run_update(UpdateCliOptions(ref="v2.0.0"))
        self.assertFalse(result.executed)


# ---------------------------------------------------------------------------#
# 6. --yes (audit-only)
# ---------------------------------------------------------------------------#


class YesFlagTests(unittest.TestCase):
    """``--yes`` → exit 0, yes=True, note mentions --yes."""

    def test_yes_exit_0(self) -> None:
        result = run_update(UpdateCliOptions(yes=True))
        self.assertEqual(result.exit_code, EXIT_OK)

    def test_yes_recorded(self) -> None:
        result = run_update(UpdateCliOptions(yes=True))
        self.assertTrue(result.yes)

    def test_yes_note(self) -> None:
        result = run_update(UpdateCliOptions(yes=True))
        self.assertTrue(any("--yes" in n for n in result.notes))

    def test_yes_does_not_bypass_execute(self) -> None:
        result = run_update(UpdateCliOptions(yes=True, execute=True))
        self.assertEqual(result.exit_code, EXIT_EXECUTE_NOT_AUTHORIZED)


# ---------------------------------------------------------------------------#
# 7. --offline-bundle (audit-only)
# ---------------------------------------------------------------------------#


class OfflineBundleFlagTests(unittest.TestCase):
    """``--offline-bundle <path>`` → exit 0, recorded."""

    def test_offline_bundle_exit_0(self) -> None:
        result = run_update(UpdateCliOptions(offline_bundle="/tmp/b.tar"))
        self.assertEqual(result.exit_code, EXIT_OK)

    def test_offline_bundle_recorded(self) -> None:
        result = run_update(UpdateCliOptions(offline_bundle="/tmp/b.tar"))
        self.assertEqual(result.offline_bundle, "/tmp/b.tar")

    def test_offline_bundle_note(self) -> None:
        result = run_update(UpdateCliOptions(offline_bundle="/tmp/b.tar"))
        self.assertTrue(any("/tmp/b.tar" in n for n in result.notes))


# ---------------------------------------------------------------------------#
# 8. --log-format (audit-only)
# ---------------------------------------------------------------------------#


class LogFormatFlagTests(unittest.TestCase):
    """``--log-format <format>`` → exit 0, recorded."""

    def test_log_format_exit_0(self) -> None:
        result = run_update(UpdateCliOptions(log_format="json"))
        self.assertEqual(result.exit_code, EXIT_OK)

    def test_log_format_recorded(self) -> None:
        result = run_update(UpdateCliOptions(log_format="json"))
        self.assertEqual(result.log_format, "json")

    def test_log_format_note(self) -> None:
        result = run_update(UpdateCliOptions(log_format="json"))
        self.assertTrue(any("json" in n for n in result.notes))


# ---------------------------------------------------------------------------#
# 9. All flags combined
# ---------------------------------------------------------------------------#


class AllFlagsCombinedTests(unittest.TestCase):
    """All seven flags → exit 6 (because --execute), all metadata recorded."""

    def test_all_flags_exit_6(self) -> None:
        result = run_update(UpdateCliOptions(
            channel="rc",
            ref="v2.0.0",
            yes=True,
            offline_bundle="/tmp/b.tar",
            log_format="json",
            execute=True,
        ))
        self.assertEqual(result.exit_code, EXIT_EXECUTE_NOT_AUTHORIZED)

    def test_all_flags_metadata(self) -> None:
        result = run_update(UpdateCliOptions(
            channel="rc",
            ref="v2.0.0",
            yes=True,
            offline_bundle="/tmp/b.tar",
            log_format="json",
            execute=True,
        ))
        self.assertTrue(result.execute_requested)
        self.assertEqual(result.channel, "rc")
        self.assertEqual(result.ref, "v2.0.0")
        self.assertTrue(result.yes)
        self.assertEqual(result.offline_bundle, "/tmp/b.tar")
        self.assertEqual(result.log_format, "json")

    def test_all_flags_not_executed(self) -> None:
        result = run_update(UpdateCliOptions(
            channel="rc",
            ref="v2.0.0",
            yes=True,
            offline_bundle="/tmp/b.tar",
            log_format="json",
            execute=True,
        ))
        self.assertFalse(result.executed)


# ---------------------------------------------------------------------------#
# 10. Unknown profile
# ---------------------------------------------------------------------------#


class UnknownProfileTests(unittest.TestCase):
    """Unknown profile → exit 3, plan=None, preflight=None."""

    def test_unknown_profile_exit_3(self) -> None:
        result = run_update(UpdateCliOptions(profile="bogus"))
        self.assertEqual(result.exit_code, EXIT_PROFILE_INVALID)

    def test_unknown_profile_no_plan(self) -> None:
        result = run_update(UpdateCliOptions(profile="bogus"))
        self.assertIsNone(result.plan)

    def test_unknown_profile_no_preflight(self) -> None:
        result = run_update(UpdateCliOptions(profile="bogus"))
        self.assertIsNone(result.preflight)

    def test_unknown_profile_error_msg(self) -> None:
        result = run_update(UpdateCliOptions(profile="bogus"))
        self.assertIn("unknown profile", result.error)

    def test_unknown_profile_with_execute(self) -> None:
        result = run_update(UpdateCliOptions(profile="bogus", execute=True))
        self.assertEqual(result.exit_code, EXIT_PROFILE_INVALID)
        self.assertTrue(result.execute_requested)


# ---------------------------------------------------------------------------#
# 11. Unknown channel
# ---------------------------------------------------------------------------#


class UnknownChannelTests(unittest.TestCase):
    """Unknown channel → exit 3 (reuses profile-invalid code), plan=None."""

    def test_unknown_channel_exit_3(self) -> None:
        # Bypass argparse by constructing options directly.
        result = run_update(UpdateCliOptions(channel="bogus"))
        self.assertEqual(result.exit_code, EXIT_PROFILE_INVALID)

    def test_unknown_channel_no_plan(self) -> None:
        result = run_update(UpdateCliOptions(channel="bogus"))
        self.assertIsNone(result.plan)

    def test_unknown_channel_error_msg(self) -> None:
        result = run_update(UpdateCliOptions(channel="bogus"))
        self.assertIn("unknown channel", result.error)


# ---------------------------------------------------------------------------#
# 12. Profile switch rejected
# ---------------------------------------------------------------------------#


class ProfileSwitchTests(unittest.TestCase):
    """Existing marker with different profile → exit 5."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        marker = Path(self.tmpdir) / ".aee-profile"
        marker.write_text("mini", encoding="utf-8")

    def test_profile_switch_exit_5(self) -> None:
        result = run_update(UpdateCliOptions(
            profile="full",
            repo_root=Path(self.tmpdir),
        ))
        self.assertEqual(result.exit_code, EXIT_PROFILE_SWITCH_REJECTED)

    def test_profile_switch_error(self) -> None:
        result = run_update(UpdateCliOptions(
            profile="full",
            repo_root=Path(self.tmpdir),
        ))
        self.assertIn("profile switch", result.error.lower())

    def test_profile_switch_with_execute(self) -> None:
        result = run_update(UpdateCliOptions(
            profile="full",
            repo_root=Path(self.tmpdir),
            execute=True,
        ))
        self.assertEqual(result.exit_code, EXIT_PROFILE_SWITCH_REJECTED)


# ---------------------------------------------------------------------------#
# 13. Pre-flight failed (repo_root missing)
# ---------------------------------------------------------------------------#


class PreFlightFailedTests(unittest.TestCase):
    """Missing repo_root → exit 4."""

    def test_missing_repo_root_exit_4(self) -> None:
        result = run_update(UpdateCliOptions(
            profile="full",
            repo_root=Path("/nonexistent/path/that/does/not/exist"),
        ))
        self.assertEqual(result.exit_code, EXIT_PRE_FLIGHT_FAILED)

    def test_missing_repo_root_with_execute(self) -> None:
        result = run_update(UpdateCliOptions(
            profile="full",
            repo_root=Path("/nonexistent/path/that/does/not/exist"),
            execute=True,
        ))
        self.assertEqual(result.exit_code, EXIT_PRE_FLIGHT_FAILED)


# ---------------------------------------------------------------------------#
# 14. Drift detection — projected drift surfaces exit 9
# ---------------------------------------------------------------------------#


class DriftDetectionTests(unittest.TestCase):
    """Projected drift detection: recorded pin mismatch → exit 9."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        # Create a fake git repo structure.
        git_dir = Path(self.tmpdir) / ".git"
        git_dir.mkdir()
        # Fake HEAD pointing to a ref.
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        refs = git_dir / "refs" / "heads"
        refs.mkdir(parents=True)
        (refs / "main").write_text(
            "abcdef1234567890abcdef1234567890abcdef12\n",
            encoding="utf-8",
        )
        # Fake requirements.lock.
        (Path(self.tmpdir) / "requirements.lock").write_text(
            "lock-content-v1\n", encoding="utf-8",
        )

    def _write_pin(self, commit: str, lock: str) -> None:
        pin = Path(self.tmpdir) / ".aee-pin"
        pin.write_text(
            "commit_sha={c}\nrequirements_lock_sha256={l}\n".format(
                c=commit, l=lock,
            ),
            encoding="utf-8",
        )

    def test_drift_commit_mismatch_exit_9(self) -> None:
        import hashlib
        lock_sha = hashlib.sha256(b"lock-content-v1\n").hexdigest()
        self._write_pin(
            commit="9999999999999999999999999999999999999999",
            lock=lock_sha,
        )
        result = run_update(UpdateCliOptions(repo_root=Path(self.tmpdir)))
        self.assertEqual(result.exit_code, EXIT_DRIFT_DETECTED)
        self.assertTrue(result.drift.would_drift)

    def test_drift_lock_mismatch_exit_9(self) -> None:
        self._write_pin(
            commit="abcdef1234567890abcdef1234567890abcdef12",
            lock="9999999999999999999999999999999999999999999999999999999999999999",
        )
        result = run_update(UpdateCliOptions(repo_root=Path(self.tmpdir)))
        self.assertEqual(result.exit_code, EXIT_DRIFT_DETECTED)
        self.assertTrue(result.drift.would_drift)

    def test_no_drift_when_pin_matches_exit_0(self) -> None:
        import hashlib
        lock_sha = hashlib.sha256(b"lock-content-v1\n").hexdigest()
        self._write_pin(
            commit="abcdef1234567890abcdef1234567890abcdef12",
            lock=lock_sha,
        )
        result = run_update(UpdateCliOptions(repo_root=Path(self.tmpdir)))
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertFalse(result.drift.would_drift)

    def test_drift_note_present(self) -> None:
        self._write_pin(
            commit="9999999999999999999999999999999999999999",
            lock="d82bacffb7a78ae44ddbd809867cd45002bc548afab15d969221475befb3701f",
        )
        result = run_update(UpdateCliOptions(repo_root=Path(self.tmpdir)))
        self.assertTrue(any("drift" in n.lower() for n in result.notes))

    def test_drift_execute_takes_precedence_over_drift(self) -> None:
        """When --execute is requested AND drift is detected, exit 6
        (the shell path is gated) — not 9."""
        self._write_pin(
            commit="9999999999999999999999999999999999999999",
            lock="d82bacffb7a78ae44ddbd809867cd45002bc548afab15d969221475befb3701f",
        )
        result = run_update(UpdateCliOptions(
            repo_root=Path(self.tmpdir),
            execute=True,
        ))
        self.assertEqual(result.exit_code, EXIT_EXECUTE_NOT_AUTHORIZED)


# ---------------------------------------------------------------------------#
# 15. Drift detection — no pin marker (fresh install)
# ---------------------------------------------------------------------------#


class DriftNoPinTests(unittest.TestCase):
    """No pin marker → no drift (fresh install)."""

    def test_no_pin_no_drift(self) -> None:
        tmpdir = tempfile.mkdtemp()
        result = run_update(UpdateCliOptions(repo_root=Path(tmpdir)))
        self.assertFalse(result.drift.would_drift)
        self.assertIsNone(result.drift.recorded_commit_sha)
        self.assertIsNone(result.drift.recorded_lock_sha256)

    def test_no_pin_exit_0(self) -> None:
        tmpdir = tempfile.mkdtemp()
        result = run_update(UpdateCliOptions(repo_root=Path(tmpdir)))
        self.assertEqual(result.exit_code, EXIT_OK)


# ---------------------------------------------------------------------------#
# 16. to_dict JSON-serializable
# ---------------------------------------------------------------------------#


class JsonSerializableTests(unittest.TestCase):
    """``UpdateCliResult.to_dict()`` is JSON-serializable."""

    def test_dry_run_serializable(self) -> None:
        result = run_update(UpdateCliOptions())
        json.dumps(result.to_dict())

    def test_execute_serializable(self) -> None:
        result = run_update(UpdateCliOptions(execute=True))
        json.dumps(result.to_dict())

    def test_all_flags_serializable(self) -> None:
        result = run_update(UpdateCliOptions(
            channel="rc",
            ref="v2.0.0",
            yes=True,
            offline_bundle="/tmp/b.tar",
            log_format="json",
            execute=True,
        ))
        json.dumps(result.to_dict())

    def test_unknown_profile_serializable(self) -> None:
        result = run_update(UpdateCliOptions(profile="bogus"))
        json.dumps(result.to_dict())

    def test_drift_result_serializable(self) -> None:
        result = run_update(UpdateCliOptions())
        json.dumps(result.drift.to_dict())


# ---------------------------------------------------------------------------#
# 17. CLI plumbing
# ---------------------------------------------------------------------------#


class CliPlumbingTests(unittest.TestCase):
    """CLI ``aee update`` with Phase 4C flags routes correctly."""

    def test_cli_execute_returns_6(self) -> None:
        rc, out, err = _run_cli(["update", "--execute"])
        self.assertEqual(rc, EXIT_EXECUTE_NOT_AUTHORIZED)
        self.assertIn("Phase 4C", out)

    def test_cli_dry_run_returns_0(self) -> None:
        rc, out, err = _run_cli(["update"])
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("aee update", out)

    def test_cli_channel_rc(self) -> None:
        rc, out, err = _run_cli(["update", "--channel", "rc"])
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("rc", out)

    def test_cli_channel_dev(self) -> None:
        rc, out, err = _run_cli(["update", "--channel", "dev"])
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("dev", out)

    def test_cli_ref(self) -> None:
        rc, out, err = _run_cli(["update", "--ref", "v2.0.0"])
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("v2.0.0", out)

    def test_cli_yes(self) -> None:
        rc, out, err = _run_cli(["update", "--yes"])
        self.assertEqual(rc, EXIT_OK)

    def test_cli_offline_bundle(self) -> None:
        rc, out, err = _run_cli(["update", "--offline-bundle", "/tmp/b.tar"])
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("/tmp/b.tar", out)

    def test_cli_log_format(self) -> None:
        rc, out, err = _run_cli(["update", "--log-format", "json"])
        self.assertEqual(rc, EXIT_OK)

    def test_cli_json_output(self) -> None:
        rc, out, err = _run_cli(["update", "--execute", "--json"])
        self.assertEqual(rc, EXIT_EXECUTE_NOT_AUTHORIZED)
        payload = json.loads(out)
        self.assertEqual(payload["phase"], "4C")
        self.assertTrue(payload["execute_requested"])
        self.assertEqual(payload["exit_code"], EXIT_EXECUTE_NOT_AUTHORIZED)
        self.assertEqual(payload["subcommand"], "update")
        self.assertEqual(payload["default_channel"], "stable")
        self.assertEqual(payload["known_channels"], ["stable", "rc", "dev"])

    def test_cli_json_dry_run(self) -> None:
        rc, out, err = _run_cli(["update", "--channel", "rc", "--json"])
        self.assertEqual(rc, EXIT_OK)
        payload = json.loads(out)
        self.assertEqual(payload["channel"], "rc")
        self.assertEqual(payload["exit_code"], EXIT_OK)
        self.assertIn("drift", payload)
        self.assertIn("would_drift", payload["drift"])

    def test_cli_help_lists_all_flags(self) -> None:
        err = io.StringIO()
        buf = io.StringIO()
        with patch("sys.stdout", buf), patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as cm:
                main(["update", "--help"])
        self.assertEqual(cm.exception.code, 0)
        help_text = buf.getvalue()
        for flag in (
            "--channel", "--ref", "--yes",
            "--offline-bundle", "--log-format",
            "--execute", "--json",
        ):
            self.assertIn(flag, help_text, msg="missing flag: " + flag)


# ---------------------------------------------------------------------------#
# 18. No subprocess
# ---------------------------------------------------------------------------#


class NoSubprocessTests(unittest.TestCase):
    """``update.py`` does not import subprocess or use os.system."""

    def test_no_subprocess_import(self) -> None:
        import aee.installer.update as mod
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
        import aee.installer.update as mod
        source = open(mod.__file__).read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                self.assertNotIn(
                    node.attr, ("system", "popen"),
                    msg="os.system / os.popen call found",
                )


# ---------------------------------------------------------------------------#
# 19. Approved flags only — unknown flags rejected
# ---------------------------------------------------------------------------#


class ApprovedFlagsOnlyTests(unittest.TestCase):
    """Argparse rejects unknown flags."""

    def test_unknown_flag_rejected(self) -> None:
        err = io.StringIO()
        with patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as cm:
                main(["update", "--bogus"])
        self.assertEqual(cm.exception.code, 2)

    def test_force_flag_rejected(self) -> None:
        err = io.StringIO()
        with patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as cm:
                main(["update", "--force"])
        self.assertEqual(cm.exception.code, 2)


# ---------------------------------------------------------------------------#
# 20. Backward compat — existing subcommands still work
# ---------------------------------------------------------------------------#


class BackwardCompatTests(unittest.TestCase):
    """Existing install/doctor/prepare subcommands are untouched."""

    def test_install_no_flags_still_works(self) -> None:
        rc, out, err = _run_cli(["install"])
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("aee install (dry-run / §21.3 installer backend)", out)

    def test_doctor_still_works(self) -> None:
        # Doctor may fail/caveat depending on environment; just check
        # it does not crash the CLI routing.
        rc, out, err = _run_cli(["doctor", "--no-network"])
        # Doctor exit codes: 0, 7, 8. Any of these means routing works.
        self.assertIn(rc, (0, 7, 8))

    def test_prepare_still_works(self) -> None:
        rc, out, err = _run_cli(["prepare", "--no-network"])
        # Prepare exit codes: 0, 4, 5, 6, 7, 8. Any means routing works.
        self.assertIn(rc, (0, 4, 5, 6, 7, 8))

    def test_install_execute_still_returns_6(self) -> None:
        rc, out, err = _run_cli(["install", "--execute"])
        self.assertEqual(rc, EXIT_EXECUTE_NOT_AUTHORIZED)


# ---------------------------------------------------------------------------#
# 21. DriftResult to_dict
# ---------------------------------------------------------------------------#


class DriftResultTests(unittest.TestCase):
    """``DriftResult.to_dict()`` shape + JSON-serializable."""

    def test_to_dict_shape(self) -> None:
        d = DriftResult(
            would_drift=True,
            recorded_commit_sha="abc",
            on_disk_commit_sha="def",
            recorded_lock_sha256="lock1",
            on_disk_lock_sha256="lock2",
            reason="mismatch",
        )
        result = d.to_dict()
        self.assertTrue(result["would_drift"])
        self.assertEqual(result["recorded_commit_sha"], "abc")
        self.assertEqual(result["on_disk_commit_sha"], "def")
        self.assertEqual(result["recorded_lock_sha256"], "lock1")
        self.assertEqual(result["on_disk_lock_sha256"], "lock2")
        self.assertEqual(result["reason"], "mismatch")

    def test_to_dict_json_serializable(self) -> None:
        d = DriftResult(
            would_drift=False,
            recorded_commit_sha=None,
            on_disk_commit_sha=None,
            recorded_lock_sha256=None,
            on_disk_lock_sha256=None,
            reason="",
        )
        json.dumps(d.to_dict())

    def test_frozen(self) -> None:
        d = DriftResult(
            would_drift=False,
            recorded_commit_sha=None,
            on_disk_commit_sha=None,
            recorded_lock_sha256=None,
            on_disk_lock_sha256=None,
            reason="",
        )
        with self.assertRaises((AttributeError, Exception)):
            d.would_drift = True  # type: ignore[misc]


# ---------------------------------------------------------------------------#
# 22. KNOWN_CHANNELS / DEFAULT_CHANNEL vocabulary
# ---------------------------------------------------------------------------#


class ChannelVocabularyTests(unittest.TestCase):
    """Channel vocabulary is pinned."""

    def test_known_channels(self) -> None:
        self.assertEqual(KNOWN_CHANNELS, ("stable", "rc", "dev"))

    def test_default_channel(self) -> None:
        self.assertEqual(DEFAULT_CHANNEL, "stable")

    def test_default_in_known(self) -> None:
        self.assertIn(DEFAULT_CHANNEL, KNOWN_CHANNELS)


# ---------------------------------------------------------------------------#
# 23. validate_channel
# ---------------------------------------------------------------------------#


class ValidateChannelTests(unittest.TestCase):
    """``validate_channel`` accepts known, rejects unknown."""

    def test_accepts_stable(self) -> None:
        self.assertEqual(validate_channel("stable"), "stable")

    def test_accepts_rc(self) -> None:
        self.assertEqual(validate_channel("rc"), "rc")

    def test_accepts_dev(self) -> None:
        self.assertEqual(validate_channel("dev"), "dev")

    def test_rejects_unknown(self) -> None:
        with self.assertRaises(ValueError):
            validate_channel("bogus")

    def test_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            validate_channel("")


# ---------------------------------------------------------------------------#
# 24. detect_drift
# ---------------------------------------------------------------------------#


class DetectDriftTests(unittest.TestCase):
    """``detect_drift`` is read-only and returns DriftResult."""

    def test_no_marker_no_drift(self) -> None:
        tmpdir = tempfile.mkdtemp()
        result = detect_drift(Path(tmpdir))
        self.assertFalse(result.would_drift)
        self.assertIsNone(result.recorded_commit_sha)

    def test_marker_match_no_drift(self) -> None:
        tmpdir = tempfile.mkdtemp()
        git_dir = Path(tmpdir) / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text(
            "ref: refs/heads/main\n", encoding="utf-8",
        )
        refs = git_dir / "refs" / "heads"
        refs.mkdir(parents=True)
        (refs / "main").write_text(
            "abcdef1234567890abcdef1234567890abcdef12\n",
            encoding="utf-8",
        )
        (Path(tmpdir) / "requirements.lock").write_text(
            "lock-content-v1\n", encoding="utf-8",
        )
        import hashlib
        lock_sha = hashlib.sha256(b"lock-content-v1\n").hexdigest()
        pin = Path(tmpdir) / ".aee-pin"
        pin.write_text(
            "commit_sha=abcdef1234567890abcdef1234567890abcdef12\n"
            "requirements_lock_sha256={s}\n".format(s=lock_sha),
            encoding="utf-8",
        )
        result = detect_drift(Path(tmpdir))
        self.assertFalse(result.would_drift)

    def test_marker_mismatch_drift(self) -> None:
        tmpdir = tempfile.mkdtemp()
        git_dir = Path(tmpdir) / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text(
            "ref: refs/heads/main\n", encoding="utf-8",
        )
        refs = git_dir / "refs" / "heads"
        refs.mkdir(parents=True)
        (refs / "main").write_text(
            "abcdef1234567890abcdef1234567890abcdef12\n",
            encoding="utf-8",
        )
        pin = Path(tmpdir) / ".aee-pin"
        pin.write_text(
            "commit_sha=9999999999999999999999999999999999999999\n"
            "requirements_lock_sha256=unknown\n",
            encoding="utf-8",
        )
        result = detect_drift(Path(tmpdir))
        self.assertTrue(result.would_drift)
        self.assertIn("commit_sha", result.reason)


# ---------------------------------------------------------------------------#
# 25. Exit code vocabulary
# ---------------------------------------------------------------------------#


class ExitCodeVocabularyTests(unittest.TestCase):
    """Phase 4C reuses the existing exit code vocabulary + drift code 9."""

    def test_all_exit_codes_in_allowed_set(self) -> None:
        allowed = {
            EXIT_OK, EXIT_PROFILE_INVALID, EXIT_PRE_FLIGHT_FAILED,
            EXIT_PROFILE_SWITCH_REJECTED, EXIT_EXECUTE_NOT_AUTHORIZED,
            EXIT_DRIFT_DETECTED,
        }
        self.assertIn(run_update(UpdateCliOptions()).exit_code, allowed)
        self.assertIn(
            run_update(UpdateCliOptions(execute=True)).exit_code, allowed
        )
        self.assertIn(
            run_update(UpdateCliOptions(profile="bogus")).exit_code, allowed
        )
        self.assertIn(
            run_update(UpdateCliOptions(channel="bogus")).exit_code, allowed
        )


if __name__ == "__main__":
    unittest.main()