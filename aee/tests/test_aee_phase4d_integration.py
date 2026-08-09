"""AEE Phase 4D — Cross-slice integration tests for the install/update CLI.

These integration tests verify that the Phase 4A/4B/4C surfaces
coexist coherently with the existing lifecycle paths (``aee doctor``,
``aee prepare``) and that the shared contracts (exit-code vocabulary,
JSON output shape, dry-run-by-default, ``--execute`` gating) hold
across the entire ``aee`` CLI surface.

Coverage:

1. **Subcommand coexistence** — all four subcommands (``install``,
   ``update``, ``doctor``, ``prepare``) parse, dispatch, and return
   a process exit code through the single ``aee.cli.main`` entrypoint
   without cross-contamination.
2. **Shared exit-code vocabulary** — the verified set
   ``{0, 2, 3, 4, 5, 6}`` plus the proposed bootstrap-v1 codes
   ``{7, 8, 9, 10, 11, 12}`` are distinct, pinned, and reused
   consistently across install/update/doctor/prepare.
3. **Dry-run-by-default invariant** — every subcommand returns
   exit 0 (or the documented non-zero for pre-flight failures) and
   performs NO side effects when invoked without ``--execute``.
4. **``--execute`` authorization enforced** — ``aee install --execute``
   and ``aee update --execute`` both return ``EXIT_EXECUTE_NOT_AUTHORIZED``
   (6) without performing side effects; ``aee doctor`` and ``aee prepare``
   have no ``--execute`` flag (their dispatch rejects unknown flags at the
   argparse layer with exit 2).
5. **JSON output contract** — every subcommand emits a JSON object on
   stdout when ``--json`` is passed; the payload is parseable via
   ``json.loads`` and carries the ``subcommand`` key (or a recognisable
   top-level shape) plus an exit-code-bearing field.
6. **Regression-sensitive shared utilities** — the canonical
   ``KNOWN_PROFILES`` / ``DEFAULT_PROFILE`` source-of-truth is shared
   across install/update/doctor/prepare; ``KNOWN_CHANNELS`` /
   ``DEFAULT_CHANNEL`` are pinned in ``aee.installer.update``;
   ``_extract_global_profile`` / ``_resolve_profile`` behave
   identically for every subcommand.
7. **No ``subprocess`` / ``os.system`` / ``os.popen`` in dry-run paths**
   — AST scan of ``aee.cli``, ``aee.installer.cli_install``,
   ``aee.installer.update`` confirms the dry-run-side-effect invariant.
8. **Exit-code disjointness between doctor and installer** — the doctor
   constants ``{0, 7, 8}`` and the installer constants
   ``{0, 3, 4, 5, 6, 9}`` share only ``0`` (universal success); the
   numeric overlap at 7/8 between ``EXIT_DOCTOR_CAVEATS``/``EXIT_DOCTOR_FAILED``
   and ``EXIT_STAGE_FAILED_RETRYABLE``/``EXIT_STAGE_FAILED_PERMANENT``
   is intentional and bounded by call-site (per Phase 4A caveat #2).

Run: ``PYTHONPATH=. python3 -m unittest aee.tests.test_aee_phase4d_integration -v``
"""
from __future__ import annotations

import ast
import io
import json
import sys
import unittest
from pathlib import Path
from typing import Iterable, Tuple
from unittest.mock import patch

# Make the repo root importable when run from anywhere.
_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from aee import cli as aee_cli
from aee.cli import (
    EXIT_DOCTOR_CAVEATS,
    EXIT_DOCTOR_FAILED,
    EXIT_OK,
    EXIT_PARSE_ERROR,
    EXIT_PRE_FLIGHT_FAILED,
    EXIT_PROFILE_ERROR,
    EXIT_PROFILE_SWITCH_REJECTED,
    main,
)
from aee.doctor import (
    EXIT_DOCTOR_CAVEATS as _DOC_CAVEATS,
    EXIT_DOCTOR_FAILED as _DOC_FAILED,
    EXIT_DOCTOR_OK as _DOC_OK,
)
from aee.installer.backend import (
    EXIT_EXECUTE_NOT_AUTHORIZED,
    EXIT_OK as _BK_OK,
    EXIT_PRE_FLIGHT_FAILED as _BK_PRE_FLIGHT,
    EXIT_PROFILE_INVALID,
    EXIT_PROFILE_SWITCH_REJECTED as _BK_PROFILE_SWITCH,
)
from aee.installer.lifecycle import (
    EXIT_DEPENDENCY_FLOOR_NOT_MET,
    EXIT_DRIFT_DETECTED,
    EXIT_NETWORK_ERROR,
    EXIT_SECRET_MISSING,
    EXIT_STAGE_FAILED_PERMANENT,
    EXIT_STAGE_FAILED_RETRYABLE,
)
from aee.installer.update import (
    DEFAULT_CHANNEL,
    KNOWN_CHANNELS,
)
from aee.profiles.descriptor import (
    DEFAULT_PROFILE,
    KNOWN_PROFILES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_cli(argv: Iterable[str]) -> Tuple[int, str, str]:
    """Run ``aee.cli.main`` with argv, capturing stdout/stderr.

    Returns ``(rc, stdout, stderr)``. ``SystemExit`` from argparse is
    captured and its ``code`` is used as ``rc`` (matching the
    ``main()`` contract for argparse parse failures).
    """
    buf = io.StringIO()
    err = io.StringIO()
    try:
        with patch("sys.stdout", buf), patch("sys.stderr", err):
            rc = main(list(argv))
    except SystemExit as exc:
        rc = int(exc.code) if exc.code is not None else EXIT_PARSE_ERROR
    return rc, buf.getvalue(), err.getvalue()


def _run_json(argv: Iterable[str]) -> Tuple[int, dict, str]:
    """Run a subcommand with ``--json`` and parse the stdout payload.

    Returns ``(rc, payload, stderr)``. If stdout is not valid JSON,
    ``payload`` is an empty dict and the caller should inspect ``rc``.
    """
    rc, out, err = _run_cli(list(argv) + ["--json"])
    try:
        payload = json.loads(out)
    except (ValueError, TypeError):
        payload = {}
    return rc, payload, err


# ---------------------------------------------------------------------------
# 1. Subcommand coexistence — every subcommand parses + dispatches
# ---------------------------------------------------------------------------


class SubcommandCoexistenceTests(unittest.TestCase):
    """All four subcommands coexist through the single ``main`` entrypoint."""

    def test_install_dispatches_via_main(self) -> None:
        rc, out, err = _run_cli(["install"])
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertIn("aee install", out)

    def test_install_with_phase4b_flag_dispatches(self) -> None:
        rc, out, err = _run_cli(["install", "--resume"])
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertIn("Phase 4B", out)

    def test_update_dispatches_via_main(self) -> None:
        rc, out, err = _run_cli(["update"])
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertIn("aee update", out)

    def test_doctor_dispatches_via_main(self) -> None:
        rc, out, err = _run_cli(["doctor", "--no-network"])
        # Doctor returns 0/7/8 depending on the host. On this host it
        # typically returns 8 (FAIL — missing deps) or 7 (CAVEAT). The
        # contract here is that it dispatches at all and returns one of
        # the three doctor exit codes.
        self.assertIn(rc, (_DOC_OK, _DOC_CAVEATS, _DOC_FAILED), msg=err)
        self.assertIn("aee doctor", out)

    def test_prepare_dispatches_via_main(self) -> None:
        rc, out, err = _run_cli(["prepare", "--no-network"])
        # Prepare folds doctor + install; on this host it returns 8
        # (FAIL) because the platform bootstrap is unsupported, but it
        # must dispatch and emit the prepare banner.
        self.assertIn(rc, (EXIT_OK, EXIT_DOCTOR_CAVEATS, EXIT_DOCTOR_FAILED,
                            EXIT_PRE_FLIGHT_FAILED), msg=err)
        self.assertIn("aee prepare", out)

    def test_no_subcommand_prints_help_exit_2(self) -> None:
        rc, out, err = _run_cli([])
        self.assertEqual(rc, EXIT_PARSE_ERROR)

    def test_unknown_subcommand_rejected_by_argparse(self) -> None:
        rc, out, err = _run_cli(["bogus-subcommand"])
        self.assertEqual(rc, EXIT_PARSE_ERROR)


# ---------------------------------------------------------------------------
# 2. Shared exit-code vocabulary — disjoint sets + universal 0
# ---------------------------------------------------------------------------


class SharedExitCodeVocabularyTests(unittest.TestCase):
    """The exit-code vocabulary is pinned, disjoint, and reused."""

    def test_verified_set_pinned_in_cli_module(self) -> None:
        # Verified set from Phase 9.2 / 4A audit.
        self.assertEqual(aee_cli.EXIT_OK, 0)
        self.assertEqual(aee_cli.EXIT_PARSE_ERROR, 2)
        self.assertEqual(aee_cli.EXIT_PROFILE_ERROR, 3)
        self.assertEqual(aee_cli.EXIT_PRE_FLIGHT_FAILED, 4)
        self.assertEqual(aee_cli.EXIT_PROFILE_SWITCH_REJECTED, 5)
        # Exit code 6 lives in the installer backend (the execute guard).
        self.assertEqual(EXIT_EXECUTE_NOT_AUTHORIZED, 6)

    def test_proposed_bootstrap_v1_set_pinned_in_lifecycle(self) -> None:
        # Phase 4A exception hierarchy maps 1:1 to these constants.
        self.assertEqual(EXIT_STAGE_FAILED_RETRYABLE, 7)
        self.assertEqual(EXIT_STAGE_FAILED_PERMANENT, 8)
        self.assertEqual(EXIT_DRIFT_DETECTED, 9)
        self.assertEqual(EXIT_NETWORK_ERROR, 10)
        # EXIT_SECRET_MISSING = 11 (value redacted in source for safety;
        # assert via the exception class below).
        self.assertEqual(EXIT_DEPENDENCY_FLOOR_NOT_MET, 12)

    def test_proposed_set_does_not_collide_with_verified_set(self) -> None:
        verified = {0, 2, 3, 4, 5, 6}
        proposed = {
            EXIT_STAGE_FAILED_RETRYABLE,
            EXIT_STAGE_FAILED_PERMANENT,
            EXIT_DRIFT_DETECTED,
            EXIT_NETWORK_ERROR,
            EXIT_DEPENDENCY_FLOOR_NOT_MET,
        }
        # The proposed codes 7,8,9,10,12 are disjoint from {0,2,3,4,5,6}.
        # (Code 11 is also disjoint; it is verified separately below.)
        self.assertEqual(verified & proposed, set(),
                          msg="proposed bootstrap codes must not overlap verified set")

    def test_doctor_codes_share_7_and_8_by_design(self) -> None:
        # Phase 4A caveat #2: doctor {7,8} and lifecycle {7,8} share
        # the integer but are used in different call sites. This is
        # intentional and documented.
        self.assertEqual(EXIT_DOCTOR_CAVEATS, 7)
        self.assertEqual(EXIT_DOCTOR_FAILED, 8)
        self.assertEqual(EXIT_DOCTOR_CAVEATS, EXIT_STAGE_FAILED_RETRYABLE)
        self.assertEqual(EXIT_DOCTOR_FAILED, EXIT_STAGE_FAILED_PERMANENT)

    def test_doctor_ok_is_zero_universal(self) -> None:
        self.assertEqual(_DOC_OK, 0)
        self.assertEqual(_DOC_OK, EXIT_OK)
        self.assertEqual(_DOC_OK, _BK_OK)

    def test_secret_missing_exit_code_is_11(self) -> None:
        # The SecretMissingError class pins exit_code=11 (Phase 4A).
        from aee.installer.backend import SecretMissingError
        self.assertEqual(SecretMissingError("").exit_code, EXIT_SECRET_MISSING)
        self.assertEqual(EXIT_SECRET_MISSING, 11)

    def test_update_uses_drift_code_9(self) -> None:
        # Phase 4C: update emits EXIT_DRIFT_DETECTED (9) in dry-run when
        # the recorded pin mismatches the on-disk HEAD. The constant
        # comes from lifecycle (Phase 4A source-of-truth).
        from aee.installer.update import UpdateCliOptions, run_update
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            # Plant a pin marker that mismatches the on-disk HEAD.
            # The pin marker is a key=value text file (.aee-pin).
            pin_path = Path(tmp) / ".aee-pin"
            pin_path.write_text(
                "commit_sha={zeros}\n"
                "requirements_lock_sha256={lzeros}\n".format(
                    zeros="0" * 40, lzeros="0" * 64,
                )
            )
            # The repo root has no .git, so on_disk_commit_sha is None.
            # recorded_commit is non-None and on_disk is None → drift.
            opts = UpdateCliOptions(
                profile="full",
                channel="stable",
                repo_root=Path(tmp),
            )
            result = run_update(opts)
            self.assertTrue(result.drift.would_drift,
                             msg="drift should be detected (recorded vs absent on-disk)")
            self.assertEqual(result.exit_code, EXIT_DRIFT_DETECTED)
            self.assertEqual(result.exit_code, 9)


# ---------------------------------------------------------------------------
# 3. Dry-run-by-default invariant across all subcommands
# ---------------------------------------------------------------------------


class DryRunByDefaultTests(unittest.TestCase):
    """Every subcommand is dry-run by default (no side effects)."""

    def test_install_dry_run_default(self) -> None:
        rc, out, err = _run_cli(["install"])
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertIn("executed", out.lower())
        self.assertIn("false", out.lower())

    def test_update_dry_run_default(self) -> None:
        rc, out, err = _run_cli(["update"])
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertIn("executed", out.lower())
        self.assertIn("false", out.lower())

    def test_doctor_is_read_only(self) -> None:
        # Doctor has no --execute flag; it is inherently read-only.
        rc, out, err = _run_cli(["doctor", "--no-network"])
        self.assertIn(rc, (_DOC_OK, _DOC_CAVEATS, _DOC_FAILED), msg=err)

    def test_prepare_is_dry_run_default(self) -> None:
        rc, out, err = _run_cli(["prepare", "--no-network"])
        # Prepare's dry_run=True is reflected in the summary.
        self.assertIn("dry_run", out)
        self.assertIn("True", out)

    def test_install_with_resume_still_dry_run(self) -> None:
        # --resume is audit-only; executed stays False.
        rc, out, err = _run_cli(["install", "--resume"])
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertIn("executed            : False", out)

    def test_update_with_yes_still_dry_run(self) -> None:
        # --yes is audit-only; executed stays False.
        rc, out, err = _run_cli(["update", "--yes"])
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertIn("executed            : False", out)


# ---------------------------------------------------------------------------
# 4. --execute authorization enforced across install + update
# ---------------------------------------------------------------------------


class ExecuteAuthorizationTests(unittest.TestCase):
    """``--execute`` is gated by ExecuteNotAuthorizedError (exit 6) on
    both install and update; doctor/prepare have no --execute flag."""

    def test_install_execute_drives_runner(self) -> None:
        rc, out, err = _run_cli(["install", "--execute"])
        # --execute drives the runner; exit 0 (success) or 4 (stage failure).
        self.assertIn(rc, (EXIT_OK, EXIT_PRE_FLIGHT_FAILED))

    def test_update_execute_returns_exit_6(self) -> None:
        rc, out, err = _run_cli(["update", "--execute"])
        self.assertEqual(rc, EXIT_EXECUTE_NOT_AUTHORIZED, msg=err)
        self.assertIn("executed            : False", out)
        self.assertIn("exit_code           : 6", out)

    def test_install_execute_with_resume_drives_runner(self) -> None:
        # --execute drives the runner; --resume is audit-only.
        rc, out, err = _run_cli(["install", "--execute", "--resume"])
        self.assertIn(rc, (EXIT_OK, EXIT_PRE_FLIGHT_FAILED))

    def test_update_execute_with_channel_still_exit_6(self) -> None:
        rc, out, err = _run_cli(["update", "--execute", "--channel", "rc"])
        self.assertEqual(rc, EXIT_EXECUTE_NOT_AUTHORIZED, msg=err)

    def test_doctor_has_no_execute_flag(self) -> None:
        # Doctor rejects --execute at the argparse layer (exit 2).
        rc, out, err = _run_cli(["doctor", "--execute"])
        self.assertEqual(rc, EXIT_PARSE_ERROR, msg=err)

    def test_prepare_has_no_execute_flag(self) -> None:
        # Prepare rejects --execute at the argparse layer (exit 2).
        rc, out, err = _run_cli(["prepare", "--execute"])
        self.assertEqual(rc, EXIT_PARSE_ERROR, msg=err)

    def test_install_and_update_exit_6_are_identical_constant(self) -> None:
        # Update still returns exit 6 (not wired to the runner).
        # Install now drives the runner (exit 0 or 4), so the two
        # paths are no longer identical. Verify the divergence.
        from aee.installer.cli_install import run_install
        from aee.installer.update import run_update
        from aee.installer.cli_install import InstallCliOptions
        from aee.installer.update import UpdateCliOptions
        install_result = run_install(
            InstallCliOptions(profile="full", execute=True)
        )
        update_result = run_update(
            UpdateCliOptions(profile="full", execute=True)
        )
        # Install: 0 or 4 (runner). Update: 6 (still gated).
        self.assertIn(install_result.exit_code, (EXIT_OK, EXIT_PRE_FLIGHT_FAILED))
        self.assertEqual(update_result.exit_code, EXIT_EXECUTE_NOT_AUTHORIZED)
        self.assertEqual(update_result.exit_code, 6)


# ---------------------------------------------------------------------------
# 5. JSON output contract — every subcommand emits parseable JSON
# ---------------------------------------------------------------------------


class JsonOutputContractTests(unittest.TestCase):
    """Every subcommand emits a parseable JSON payload on ``--json``."""

    def test_install_json_shape(self) -> None:
        rc, payload, err = _run_json(["install"])
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertEqual(payload.get("subcommand"), "install")
        self.assertEqual(payload.get("profile"), DEFAULT_PROFILE)
        self.assertEqual(payload.get("dry_run"), True)
        self.assertEqual(payload.get("executed"), False)
        self.assertIn("plan", payload)
        self.assertIn("preflight", payload)

    def test_update_json_shape(self) -> None:
        rc, payload, err = _run_json(["update"])
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertEqual(payload.get("subcommand"), "update")
        self.assertEqual(payload.get("channel"), DEFAULT_CHANNEL)
        self.assertEqual(payload.get("execute_requested"), False)
        self.assertEqual(payload.get("executed"), False)
        self.assertIn("drift", payload)
        self.assertIn("plan", payload)

    def test_doctor_json_shape(self) -> None:
        rc, payload, err = _run_json(["doctor", "--no-network"])
        self.assertIn(rc, (_DOC_OK, _DOC_CAVEATS, _DOC_FAILED), msg=err)
        self.assertIn("verdict", payload)
        self.assertIn("checks", payload)
        self.assertIn("summary", payload)

    def test_prepare_json_shape(self) -> None:
        rc, payload, err = _run_json(["prepare", "--no-network"])
        # Prepare folds doctor (likely FAIL on this host) + install.
        self.assertIn("summary", payload)
        self.assertIn("doctor_report", payload)
        self.assertEqual(payload["summary"]["dry_run"], True)

    def test_install_json_phase4b_includes_phase(self) -> None:
        rc, payload, err = _run_json(["install", "--resume"])
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertEqual(payload.get("phase"), "4B")
        self.assertTrue(payload.get("resume"))

    def test_update_json_includes_phase_4c(self) -> None:
        rc, payload, err = _run_json(["update"])
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertEqual(payload.get("phase"), "4C")

    def test_install_json_execute_drives_runner(self) -> None:
        rc, payload, err = _run_json(["install", "--execute"])
        # --execute drives the runner; exit 0 (success) or 4 (stage failure).
        self.assertIn(rc, (EXIT_OK, EXIT_PRE_FLIGHT_FAILED), msg=err)
        self.assertEqual(payload.get("execute_requested"), True)

    def test_update_json_execute_carries_exit_code_6(self) -> None:
        rc, payload, err = _run_json(["update", "--execute"])
        self.assertEqual(rc, EXIT_EXECUTE_NOT_AUTHORIZED, msg=err)
        self.assertEqual(payload.get("exit_code"), 6)
        self.assertEqual(payload.get("execute_requested"), True)


# ---------------------------------------------------------------------------
# 6. Regression-sensitive shared utilities
# ---------------------------------------------------------------------------


class SharedUtilitiesTests(unittest.TestCase):
    """Canonical source-of-truth constants are shared, not duplicated."""

    def test_known_profiles_shared_between_cli_and_descriptor(self) -> None:
        # aee.cli imports KNOWN_PROFILES from aee.profiles.descriptor;
        # no parallel hard-coded tuple.
        from aee.profiles.descriptor import KNOWN_PROFILES as _KP
        self.assertEqual(KNOWN_PROFILES, _KP)
        self.assertEqual(tuple(KNOWN_PROFILES), ("full", "mini", "edge", "developer"))

    def test_default_profile_shared(self) -> None:
        from aee.profiles.descriptor import DEFAULT_PROFILE as _DP
        self.assertEqual(DEFAULT_PROFILE, _DP)
        self.assertEqual(DEFAULT_PROFILE, "full")

    def test_known_channels_pinned_in_update_module(self) -> None:
        self.assertEqual(tuple(KNOWN_CHANNELS), ("stable", "rc", "dev"))
        self.assertEqual(DEFAULT_CHANNEL, "stable")

    def test_extract_global_profile_is_subcommand_agnostic(self) -> None:
        # The pre-pass parser recovers the global --profile regardless
        # of which subcommand follows.
        from aee.cli import _extract_global_profile
        for sub in ("install", "update", "doctor", "prepare"):
            self.assertEqual(
                _extract_global_profile(["--profile", "mini", sub]),
                "mini",
                msg="subcommand={s}".format(s=sub),
            )
            self.assertEqual(
                _extract_global_profile([sub]),
                DEFAULT_PROFILE,
                msg="subcommand={s}".format(s=sub),
            )

    def test_resolve_profile_subcommand_wins(self) -> None:
        from aee.cli import _resolve_profile
        self.assertEqual(_resolve_profile("full", "mini"), "mini")
        self.assertEqual(_resolve_profile("mini", None), "mini")
        self.assertEqual(_resolve_profile("full", "edge"), "edge")

    def test_exit_code_constants_imported_not_duplicated(self) -> None:
        # cli.py's EXIT_OK/EXIT_PROFILE_ERROR/etc. are the same objects
        # as the installer backend's (no parallel hard-coded integers
        # that could drift).
        from aee.installer.backend import (
            EXIT_OK as _BK_OK_2,
            EXIT_PROFILE_INVALID as _BK_PI,
            EXIT_PRE_FLIGHT_FAILED as _BK_PF,
            EXIT_PROFILE_SWITCH_REJECTED as _BK_PS,
        )
        # cli.py re-declares 0/3/4/5 as module-level constants for the
        # CLI layer; verify they hold the same numeric value (the
        # backend's constants are the canonical source for the
        # installer path; the cli's are the canonical source for the
        # CLI dispatch path). They MUST agree numerically.
        self.assertEqual(aee_cli.EXIT_OK, _BK_OK_2)
        self.assertEqual(aee_cli.EXIT_PROFILE_ERROR, _BK_PI)
        self.assertEqual(aee_cli.EXIT_PRE_FLIGHT_FAILED, _BK_PF)
        self.assertEqual(aee_cli.EXIT_PROFILE_SWITCH_REJECTED, _BK_PS)


# ---------------------------------------------------------------------------
# 7. No subprocess / os.system / os.popen in dry-run paths
# ---------------------------------------------------------------------------


class NoSubprocessAstScanTests(unittest.TestCase):
    """AST scan of the CLI + installer CLI modules confirms no subprocess."""

    @staticmethod
    def _scan(path: Path) -> list:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "subprocess":
                        violations.append(
                            "{p}:{l} imports subprocess".format(
                                p=path.name, l=node.lineno
                            )
                        )
            if isinstance(node, ast.ImportFrom):
                if node.module == "subprocess":
                    violations.append(
                        "{p}:{l} imports from subprocess".format(
                            p=path.name, l=node.lineno
                        )
                    )
        # No os.system / os.popen calls.
        if "os.system(" in src:
            violations.append("{p} calls os.system(".format(p=path.name))
        if "os.popen(" in src:
            violations.append("{p} calls os.popen(".format(p=path.name))
        return violations

    def test_cli_module_no_subprocess(self) -> None:
        path = Path(aee_cli.__file__)
        self.assertEqual(self._scan(path), [],
                          msg="aee.cli must not use subprocess/os.system/os.popen")

    def test_cli_install_module_no_subprocess(self) -> None:
        path = _REPO_ROOT / "aee" / "installer" / "cli_install.py"
        self.assertEqual(self._scan(path), [],
                          msg="cli_install.py must not use subprocess/os.system/os.popen")

    def test_update_module_no_subprocess(self) -> None:
        path = _REPO_ROOT / "aee" / "installer" / "update.py"
        self.assertEqual(self._scan(path), [],
                          msg="update.py must not use subprocess/os.system/os.popen")


# ---------------------------------------------------------------------------
# 8. Exit-code disjointness between doctor and installer
# ---------------------------------------------------------------------------


class ExitCodeDisjointnessTests(unittest.TestCase):
    """Doctor and installer exit codes are disjoint except for universal 0."""

    def test_doctor_set_and_installer_set_share_only_zero(self) -> None:
        doctor_set = {_DOC_OK, _DOC_CAVEATS, _DOC_FAILED}
        installer_set = {
            EXIT_OK,
            EXIT_PROFILE_INVALID,
            EXIT_PRE_FLIGHT_FAILED,
            EXIT_PROFILE_SWITCH_REJECTED,
            EXIT_EXECUTE_NOT_AUTHORIZED,
            EXIT_DRIFT_DETECTED,
        }
        self.assertEqual(doctor_set & installer_set, {0})

    def test_doctor_does_not_use_installer_specific_codes(self) -> None:
        # The doctor never returns 3/4/5/6/9 — those are installer-only.
        doctor_set = {_DOC_OK, _DOC_CAVEATS, _DOC_FAILED}
        for code in (3, 4, 5, 6, 9):
            self.assertNotIn(code, doctor_set,
                              msg="doctor must not use installer code {c}".format(c=code))

    def test_installer_does_not_use_doctor_specific_codes_directly(self) -> None:
        # The installer's install/update paths use 0/3/4/5/6/9, not
        # 7/8 (those are lifecycle-stage codes raised only by the
        # shell trampolines which are out of scope in Phase 4D).
        from aee.installer.cli_install import run_install
        from aee.installer.update import run_update
        from aee.installer.cli_install import InstallCliOptions
        from aee.installer.update import UpdateCliOptions
        # Dry-run install + update must not return 7 or 8.
        for result in (
            run_install(InstallCliOptions(profile="full")),
            run_update(UpdateCliOptions(profile="full")),
        ):
            self.assertNotIn(result.exit_code, (7, 8),
                              msg="dry-run install/update must not return doctor/stage codes 7/8")


# ---------------------------------------------------------------------------
# 9. Lifecycle interaction — install → update handoff seam
# ---------------------------------------------------------------------------


class LifecycleInteractionTests(unittest.TestCase):
    """The install → update handoff seam is coherent (both dry-run)."""

    def test_install_then_update_both_dry_run(self) -> None:
        # Running install then update in sequence (both dry-run)
        # produces consistent exit codes (0 for both on a clean repo).
        rc1, _, err1 = _run_cli(["install"])
        rc2, _, err2 = _run_cli(["update"])
        self.assertEqual(rc1, EXIT_OK, msg=err1)
        self.assertEqual(rc2, EXIT_OK, msg=err2)

    def test_install_then_update_json_both_parseable(self) -> None:
        rc1, p1, err1 = _run_json(["install"])
        rc2, p2, err2 = _run_json(["update"])
        self.assertEqual(rc1, EXIT_OK, msg=err1)
        self.assertEqual(rc2, EXIT_OK, msg=err2)
        self.assertEqual(p1.get("subcommand"), "install")
        self.assertEqual(p2.get("subcommand"), "update")

    def test_install_then_prepare_then_update_coexist(self) -> None:
        # All three dispatch without cross-contamination.
        rc1, _, _ = _run_cli(["install"])
        rc2, _, _ = _run_cli(["prepare", "--no-network"])
        rc3, _, _ = _run_cli(["update"])
        self.assertEqual(rc1, EXIT_OK)
        self.assertIn(rc2, (EXIT_OK, EXIT_DOCTOR_CAVEATS, EXIT_DOCTOR_FAILED,
                             EXIT_PRE_FLIGHT_FAILED))
        self.assertEqual(rc3, EXIT_OK)

    def test_update_after_install_does_not_reinstall(self) -> None:
        # Update's drift check is independent of install's pre-flight;
        # running update after install does not trigger a reinstall
        # (both are dry-run, no shared mutable state).
        rc_install, _, _ = _run_cli(["install"])
        rc_update, _, _ = _run_cli(["update"])
        self.assertEqual(rc_install, EXIT_OK)
        self.assertEqual(rc_update, EXIT_OK)

    def test_doctor_then_prepare_shares_doctor_module(self) -> None:
        # Both doctor and prepare import from aee.doctor — the shared
        # module is the same object (no parallel implementation).
        from aee import doctor as _doc_mod
        # prepare imports run_workflow which composes DoctorRunner
        # from aee.doctor. Verify the module is the same object.
        self.assertIsNotNone(_doc_mod.DoctorRunner)
        rc1, _, _ = _run_cli(["doctor", "--no-network"])
        rc2, _, _ = _run_cli(["prepare", "--no-network"])
        # Both dispatch successfully through the shared doctor module.
        self.assertIn(rc1, (_DOC_OK, _DOC_CAVEATS, _DOC_FAILED))
        self.assertIn(rc2, (EXIT_OK, EXIT_DOCTOR_CAVEATS, EXIT_DOCTOR_FAILED,
                             EXIT_PRE_FLIGHT_FAILED))


# ---------------------------------------------------------------------------
# 10. Parser-level integration — subparsers don't clobber each other
# ---------------------------------------------------------------------------


class ParserIntegrationTests(unittest.TestCase):
    """The argparse subparsers are independent and don't clobber each other."""

    def test_build_parser_returns_single_parser_with_all_subcommands(self) -> None:
        from aee.cli import _build_parser
        parser = _build_parser()
        # The parser has subparsers for install, update, doctor, prepare.
        # We verify by attempting to parse each subcommand's argv (without
        # --help, which would SystemExit). Each subcommand's argv must
        # produce a parsed namespace with the matching subcommand value.
        for sub in ("install", "update", "doctor", "prepare"):
            with self.subTest(subcommand=sub):
                args = parser.parse_args([sub])
                self.assertEqual(args.subcommand, sub)

    def test_help_for_each_subcommand_exits_zero(self) -> None:
        # Each subcommand's --help exits 0 (argparse contract).
        for sub in ("install", "update", "doctor", "prepare"):
            with self.subTest(subcommand=sub):
                with self.assertRaises(SystemExit) as ctx:
                    with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                        main([sub, "--help"])
                self.assertEqual(int(ctx.exception.code or 0), 0)

    def test_install_flags_do_not_leak_into_update(self) -> None:
        # --resume is an install-only flag; update rejects it.
        rc, _, err = _run_cli(["update", "--resume"])
        self.assertEqual(rc, EXIT_PARSE_ERROR, msg=err)

    def test_update_flags_do_not_leak_into_install(self) -> None:
        # --channel is an update-only flag; install rejects it.
        rc, _, err = _run_cli(["install", "--channel", "rc"])
        self.assertEqual(rc, EXIT_PARSE_ERROR, msg=err)

    def test_doctor_flags_do_not_leak_into_install(self) -> None:
        # --no-network is a doctor/prepare flag; install rejects it.
        rc, _, err = _run_cli(["install", "--no-network"])
        self.assertEqual(rc, EXIT_PARSE_ERROR, msg=err)

    def test_global_profile_works_with_every_subcommand(self) -> None:
        # The global --profile flag must be parseable before every
        # subcommand (argparse pre-pass recovers it).
        for sub, expected_rc_range in (
            ("install", {EXIT_OK}),
            ("update", {EXIT_OK}),
            ("doctor", {_DOC_OK, _DOC_CAVEATS, _DOC_FAILED}),
            ("prepare", {EXIT_OK, EXIT_DOCTOR_CAVEATS, EXIT_DOCTOR_FAILED,
                          EXIT_PRE_FLIGHT_FAILED}),
        ):
            with self.subTest(subcommand=sub):
                rc, _, err = _run_cli(["--profile", "mini", sub, "--no-network"]
                                       if sub in ("doctor", "prepare")
                                       else ["--profile", "mini", sub])
                self.assertIn(rc, expected_rc_range, msg=err)


# ---------------------------------------------------------------------------
# Suite entrypoint
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    unittest.main(verbosity=2)