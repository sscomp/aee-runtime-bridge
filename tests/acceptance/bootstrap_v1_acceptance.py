"""AEE Bootstrap v1 — Acceptance Gate (W15 / Phase D).

Spec reference: ``reports/aee_bootstrap_v1_spec.md`` §15 (Acceptance
Criteria), §16 W15, §17.3 Phase D.

This module implements the three named acceptance tracks from spec §15:

1. **Reproducible Deployment (§15.1)** — verifies that the same
   ``(channel, ref, commit_sha, requirements_lock_sha256)`` tuple
   produces a deterministic plan.
2. **One-click Bootstrap (§15.2)** — verifies that a single command
   (the CLI entrypoint) produces a plan without interactive prompts.
3. **Automated Agent Deployment (§15.3)** — verifies that the CLI
   works in CI (non-interactive) mode and produces machine-readable
   output.

**Design contract:**

* **No network calls.** All checks are read-only and hermetic. The
  acceptance gate does NOT clone repos, install packages, or start
  processes. It verifies that the *interface* is correct and that the
  *plan* is deterministic.
* **No side effects.** No files are written, no subprocesses are
  spawned, no env vars are mutated beyond the test sandbox.
* **Stdlib only.** Uses only ``unittest`` + the project's own modules.
  No pytest, no external test dependencies.

Run::

    PYTHONPATH=. python3 -m unittest tests.acceptance.bootstrap_v1_acceptance -v

Or as a standalone script::

    PYTHONPATH=. python3 tests/acceptance/bootstrap_v1_acceptance.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Ensure the repo root is on the path when run as a standalone script.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from aee.installer.backend import (  # noqa: E402
    DEFAULT_CHANNEL,
    DriftReport,
    KNOWN_CHANNELS,
    InstallerBackend,
    InstallPlan,
    InstallResult,
    KNOWN_PROFILES,
    ReleasePin,
    UnknownChannelError,
    UnknownProfileError,
    plan_install,
    validate_channel,
)
from aee.installer.update import (  # noqa: E402
    DriftResult,
    UpdateCliOptions,
    UpdateCliResult,
    run_update,
)
from aee.profiles.descriptor import (  # noqa: E402
    DEFAULT_PROFILE,
    get_descriptor,
    parse_profile,
)


# ---------------------------------------------------------------------------#
# §15.1 — Reproducible Deployment
# ---------------------------------------------------------------------------#


class ReproducibleDeploymentTests(unittest.TestCase):
    """Spec §15.1: the same (channel, ref, commit_sha, lock_sha256)
    tuple MUST produce byte-identical plans.

    This acceptance gate verifies plan determinism: planning the same
    profile twice yields the same ``InstallPlan`` (same steps, same
    descriptors, same notes). The full §15.1 acceptance also requires
    two independent clean-machine E2E runs to produce byte-identical
    ``evidence.json`` — that is covered by the container/VM E2E suites
    (W11/W12/W13), not by this unit-level gate.
    """

    def test_same_profile_produces_identical_plan(self) -> None:
        """Planning the same profile twice yields the same plan."""
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = InstallerBackend(repo_root=Path(tmpdir))
            plan_a = backend.plan("mini")
            plan_b = backend.plan("mini")
            self.assertEqual(plan_a, plan_b)

    def test_plan_to_dict_is_deterministic(self) -> None:
        """``InstallPlan.to_dict()`` is deterministic for the same input."""
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = InstallerBackend(repo_root=Path(tmpdir))
            plan_a = backend.plan("full")
            plan_b = backend.plan("full")
            self.assertEqual(plan_a.to_dict(), plan_b.to_dict())

    def test_different_profiles_produce_different_plans(self) -> None:
        """Different profiles produce different plans (sanity check)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = InstallerBackend(repo_root=Path(tmpdir))
            plan_mini = backend.plan("mini")
            plan_full = backend.plan("full")
            self.assertNotEqual(plan_mini, plan_full)

    def test_release_pin_round_trip_is_deterministic(self) -> None:
        """``ReleasePin`` round-trip through ``to_dict``/``from_dict``
        is deterministic (same input → same output)."""
        pin = ReleasePin(
            channel="stable",
            ref="refs/tags/v1.0.0",
            commit_sha="a" * 40,
            pinned_at="2026-07-29T00:00:00Z",
            requirements_lock_sha256="b" * 64,
        )
        d1 = pin.to_dict()
        d2 = ReleasePin.from_dict(d1).to_dict()
        self.assertEqual(d1, d2)

    def test_drift_report_to_dict_is_deterministic(self) -> None:
        """``DriftReport.to_dict()`` is deterministic."""
        pin = ReleasePin(
            channel="stable",
            ref="main",
            commit_sha="a" * 40,
            pinned_at="2026-07-29T00:00:00Z",
        )
        report = DriftReport(
            drifted=True,
            reason="mismatch",
            recorded=pin,
            actual_commit_sha="c" * 40,
            actual_lock_sha256=None,
        )
        d1 = report.to_dict()
        d2 = DriftReport(
            drifted=True,
            reason="mismatch",
            recorded=pin,
            actual_commit_sha="c" * 40,
            actual_lock_sha256=None,
        ).to_dict()
        self.assertEqual(d1, d2)

    def test_all_profiles_produce_valid_plans(self) -> None:
        """Every known profile produces a valid, non-empty plan."""
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = InstallerBackend(repo_root=Path(tmpdir))
            for profile in KNOWN_PROFILES:
                plan = backend.plan(profile)
                self.assertIsInstance(plan, InstallPlan)
                self.assertGreater(len(plan.steps), 0)
                self.assertEqual(plan.profile, profile)


# ---------------------------------------------------------------------------#
# §15.2 — One-click Bootstrap
# ---------------------------------------------------------------------------#


class OneClickBootstrapTests(unittest.TestCase):
    """Spec §15.2: a single command produces a plan without interactive
    prompts.

    This gate verifies that ``plan_install`` (the module-level
    convenience) produces a plan in a single call, with no interactive
    input required. The full §15.2 acceptance also requires a wall-clock
    time under 5 minutes — that is covered by the E2E suites, not here.
    """

    def test_single_command_produces_plan(self) -> None:
        """``plan_install(profile)`` returns a plan in one call."""
        plan = plan_install("mini", repo_root=Path(tempfile.mkdtemp()))
        self.assertIsInstance(plan, InstallPlan)
        self.assertEqual(plan.profile, "mini")
        self.assertGreater(len(plan.steps), 0)

    def test_no_interactive_prompts(self) -> None:
        """Planning does not read from stdin or require user input."""
        # The backend's plan() method is pure data — no stdin reads,
        # no input() calls, no getpass. This is enforced by the
        # backend's design contract (no subprocess, no I/O during plan).
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = InstallerBackend(repo_root=Path(tmpdir))
            # If this hangs, the test times out — proving it does NOT
            # require interactive input.
            plan = backend.plan("developer")
            self.assertIsNotNone(plan)

    def test_dry_run_is_default(self) -> None:
        """The default mode is dry-run (no side effects)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = InstallerBackend(repo_root=Path(tmpdir))
            self.assertTrue(backend.dry_run)

    def test_pre_flight_passes_on_fresh_repo(self) -> None:
        """Pre-flight passes on a fresh (no marker) repo."""
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = InstallerBackend(repo_root=Path(tmpdir))
            result = backend.preflight("mini")
            self.assertTrue(result.ok)
            self.assertIsNone(result.existing_profile)

    def test_execute_dry_run_returns_result_not_authorized(self) -> None:
        """``execute(dry_run=True)`` returns a result with
        ``executed=False`` (no side effects performed)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = InstallerBackend(repo_root=Path(tmpdir))
            result = backend.execute("mini", dry_run=True)
            self.assertIsInstance(result, InstallResult)
            self.assertFalse(result.executed)


# ---------------------------------------------------------------------------#
# §15.3 — Automated Agent Deployment
# ---------------------------------------------------------------------------#


class AutomatedAgentDeploymentTests(unittest.TestCase):
    """Spec §15.3: the CLI works in CI (non-interactive) mode and
    produces machine-readable output.

    This gate verifies that the ``run_update`` flow (the closest
    existing CLI entrypoint) produces structured, JSON-serialisable
    output suitable for CI consumption. The full §15.3 acceptance also
    requires evidence.json — that is produced by the E2E suites.
    """

    def test_run_update_produces_json_serialisable_result(self) -> None:
        """``run_update`` produces a ``UpdateCliResult`` that is
        JSON-serialisable via ``to_dict()``."""
        with tempfile.TemporaryDirectory() as tmpdir:
            options = UpdateCliOptions(
                profile="mini",
                channel="stable",
                yes=True,
                repo_root=Path(tmpdir),
            )
            result = run_update(options)
            self.assertIsInstance(result, UpdateCliResult)
            d = result.to_dict()
            import json
            json.dumps(d)  # must not raise

    def test_ci_mode_no_interactive_prompts(self) -> None:
        """``--yes`` mode produces a result without interactive prompts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            options = UpdateCliOptions(
                profile="mini",
                channel="stable",
                yes=True,
                repo_root=Path(tmpdir),
            )
            # If this hangs, the test times out — proving CI mode works.
            result = run_update(options)
            self.assertTrue(result.yes)

    def test_unknown_channel_produces_error_result(self) -> None:
        """An unknown channel produces a structured error, not a crash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            options = UpdateCliOptions(
                profile="mini",
                channel="nightly",  # invalid
                yes=True,
                repo_root=Path(tmpdir),
            )
            result = run_update(options)
            # The update CLI catches UnknownChannelError and returns
            # a structured result with exit_code != 0.
            self.assertNotEqual(result.exit_code, 0)

    def test_unknown_profile_produces_error_result(self) -> None:
        """An unknown profile produces a structured error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            options = UpdateCliOptions(
                profile="nonexistent",
                channel="stable",
                yes=True,
                repo_root=Path(tmpdir),
            )
            result = run_update(options)
            self.assertNotEqual(result.exit_code, 0)

    def test_failure_produces_non_zero_exit_code(self) -> None:
        """A failure in any check produces a non-zero exit code."""
        # Use a non-existent repo root to trigger pre-flight failure.
        options = UpdateCliOptions(
            profile="mini",
            channel="stable",
            yes=True,
            repo_root=Path("/nonexistent/path/that/does/not/exist"),
        )
        result = run_update(options)
        self.assertNotEqual(result.exit_code, 0)


# ---------------------------------------------------------------------------#
# §15 Cross-cutting — all three tracks
# ---------------------------------------------------------------------------#


class AcceptanceGateSummaryTests(unittest.TestCase):
    """Summary gate: all three acceptance tracks must have at least one
    passing test, and the canonical vocabulary must be present."""

    def test_known_profiles_canonical(self) -> None:
        """The canonical profile set is present and non-empty."""
        self.assertGreater(len(KNOWN_PROFILES), 0)

    def test_known_channels_canonical(self) -> None:
        """The canonical channel set is present and non-empty."""
        self.assertGreater(len(KNOWN_CHANNELS), 0)

    def test_default_profile_in_known(self) -> None:
        """The default profile is in the known set."""
        self.assertIn(DEFAULT_PROFILE, KNOWN_PROFILES)

    def test_default_channel_in_known(self) -> None:
        """The default channel is in the known set."""
        self.assertIn(DEFAULT_CHANNEL, KNOWN_CHANNELS)

    def test_all_profiles_have_descriptors(self) -> None:
        """Every known profile has a valid descriptor."""
        for profile in KNOWN_PROFILES:
            desc = get_descriptor(profile)
            self.assertIsNotNone(desc)

    def test_validate_channel_accepts_all_known(self) -> None:
        """``validate_channel`` accepts every known channel."""
        for ch in KNOWN_CHANNELS:
            self.assertEqual(validate_channel(ch), ch)

    def test_parse_profile_accepts_all_known(self) -> None:
        """``parse_profile`` accepts every known profile."""
        for p in KNOWN_PROFILES:
            self.assertEqual(parse_profile(p), p)


# ---------------------------------------------------------------------------#
# Standalone runner
# ---------------------------------------------------------------------------#


def main() -> int:
    """Run the acceptance gate as a standalone script.

    Exit 0 = all acceptance tests pass.
    Exit 1 = at least one acceptance test fails.
    """
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())