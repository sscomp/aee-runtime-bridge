"""AEE Epic 9.8 §21.8 — Release Strategy targeted tests.

Tests the §21.8 Release Strategy contract from the authoritative
Master Plan (`/home/ubuntu/Abacus/AEE/AEE_MASTER_PLAN.md` §21.8,
line 7792) and the §21.A acceptance criterion item 8 (line 7855):

    §21.8 — ``aee --version`` returns ``2.0.0`` (not ``1.0.1``);
    changelog references ADR-009.

Coverage (per workorder §6 — release metadata, version strategy,
dry-run safety, invalid configuration, rollback/compatibility):

  * §21.8 acceptance item 1 — ``aee.__version__`` is ``"2.0.0-rc1"``
    (the unified product version on first Epic 9 release; NOT the
    AEE-MINI ``1.0.1``).
  * §21.8 acceptance item 2 — ``aee --version`` (CLI) prints the
    unified version to stdout and exits 0.
  * §21.8 acceptance item 3 — the changelog entry references ADR-009.
  * §21.8 acceptance item 4 — AEE-MINI ``1.0.1`` is named as the last
    release of the AEE-MINI line; the changelog entry references it
    (archived, not deleted).
  * §21.8 acceptance item 5 — the upgrade path is "fresh install, not
    in-place" (§21.8 line 7796, §21.R R4 mitigation).
  * §21.8 acceptance item 6 — SemVer policy is MAJOR/MINOR/PATCH per
    the §21.8 literal.
  * §21.8 acceptance item 7 — exactly three release artifacts (Docker
    image, tarball, changelog); no more, no less.
  * §21.8 dry-run safety — :func:`build_release_plan` returns a plan
    with ``will_publish == False``, ``will_push_registry == False``,
    ``will_mutate_channels == False``, ``is_dry_run == True`` by
    default.
  * §21.8 invalid configuration — :func:`build_release_plan` with an
    explicit ``is_dry_run=False`` still returns ``will_publish ==
    False`` etc. (the release event is a separately authorizable
    follow-up; this slice never flips the safety bits).
  * §21.8 rollback / compatibility — ``aee.__version__`` is imported
    by both ``aee.cli`` and ``aee.release``; there is no parallel
    hard-coded version literal (AST scan).
  * §21.8 compatibility with §21.1–§21.7 — the ``--version`` flag
    coexists with ``--profile`` and ``install`` subcommand; existing
    §21.2 CLI behavior is unchanged (backward compat).

Run: ``PYTHONPATH=. python3 -m unittest aee.tests.test_aee98_release_strategy -v``
"""
from __future__ import annotations

import ast
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from aee import __version__ as _AEE_VERSION
from aee import cli as aee_cli
from aee.cli import PROG_NAME, _build_parser, main
from aee.release import (
    AEE_MINI_LAST_VERSION,
    AEE_PRODUCT_VERSION,
    ADR_REFERENCE,
    RELEASE_ARTIFACTS,
    SEMVER_POLICY,
    UPGRADE_PATH,
    ReleaseArtifact,
    ReleasePlan,
    build_release_plan,
    render_changelog_entry,
)
from aee.profiles.descriptor import KNOWN_PROFILES, DEFAULT_PROFILE


# ---------------------------------------------------------------------------
# §21.8 acceptance item 1 — unified product version
# ---------------------------------------------------------------------------

class TestUnifiedProductVersion(unittest.TestCase):
    """§21.8 line 7796: unified product version ``2.0.0-rc1``."""

    def test_aee_version_is_2_0_0_rc1(self):
        """``aee.__version__`` is the unified product version."""
        self.assertEqual(_AEE_VERSION, "2.0.0-rc1")

    def test_aee_version_is_not_aee_mini_version(self):
        """The unified version is NOT the AEE-MINI ``1.0.1``."""
        self.assertNotEqual(_AEE_VERSION, "1.0.1")
        self.assertNotEqual(_AEE_VERSION, "0.1.0")

    def test_release_module_version_matches_aee_version(self):
        """``aee.release.AEE_PRODUCT_VERSION`` re-exports the canonical
        version — no parallel hard-coded literal."""
        self.assertEqual(AEE_PRODUCT_VERSION, _AEE_VERSION)


# ---------------------------------------------------------------------------
# §21.8 acceptance item 2 — ``aee --version`` CLI flag
# ---------------------------------------------------------------------------

class TestCliVersionFlag(unittest.TestCase):
    """§21.A item 8 (line 7855): ``aee --version`` returns the version."""

    def test_version_flag_prints_version_and_exits_zero(self):
        """``aee --version`` prints ``aee <version>`` and exits 0."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                _build_parser().parse_args(["--version"])
        self.assertEqual(cm.exception.code, 0)
        out = buf.getvalue()
        self.assertIn(PROG_NAME, out)
        self.assertIn(_AEE_VERSION, out)

    def test_version_flag_not_aee_mini_version(self):
        """The printed version is NOT ``1.0.1`` (§21.A item 8)."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit):
                _build_parser().parse_args(["--version"])
        self.assertNotIn("1.0.1", buf.getvalue())
        self.assertNotIn("0.1.0", buf.getvalue())

    def test_version_flag_coexists_with_profile_flag(self):
        """§21.1–§21.7 compat: ``--version`` does not break ``--profile``."""
        parser = _build_parser()
        # ``--version`` exits before ``--profile`` is consumed; this just
        # confirms the parser builds without error when both flags are
        # declared.
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                parser.parse_args(["--profile", "mini", "--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn(_AEE_VERSION, buf.getvalue())

    def test_version_flag_does_not_break_install_subcommand(self):
        """§21.2 compat: ``install`` subcommand still works (no --version)."""
        rc = main(["install", "--profile", "mini", "--dry-run"])
        self.assertEqual(rc, 0)


# ---------------------------------------------------------------------------
# §21.8 acceptance item 3 — changelog references ADR-009
# ---------------------------------------------------------------------------

class TestChangelogReferencesAdr009(unittest.TestCase):
    """§21.A item 8: changelog references ADR-009."""

    def test_adr_reference_string_contains_adr_009(self):
        self.assertIn("ADR-009", ADR_REFERENCE)

    def test_render_changelog_entry_references_adr_009(self):
        entry = render_changelog_entry()
        self.assertIn("ADR-009", entry)

    def test_render_changelog_entry_references_adr_canonical_section(self):
        entry = render_changelog_entry()
        # The entry should reference the §9 ADR-009 location.
        self.assertIn("§9", entry)


# ---------------------------------------------------------------------------
# §21.8 acceptance item 4 — AEE-MINI 1.0.1 archived, referenced in changelog
# ---------------------------------------------------------------------------

class TestAeeMiniArchiveReference(unittest.TestCase):
    """§21.8 line 7796: AEE-MINI ``1.0.1`` is the last release; archived."""

    def test_aee_mini_last_version_is_1_0_1(self):
        self.assertEqual(AEE_MINI_LAST_VERSION, "1.0.1")

    def test_render_changelog_entry_references_aee_mini(self):
        entry = render_changelog_entry()
        self.assertIn("1.0.1", entry)
        self.assertIn("AEE-MINI", entry)

    def test_render_changelog_entry_says_archived_not_deleted(self):
        entry = render_changelog_entry()
        self.assertIn("Archived", entry)
        self.assertIn("not deleted", entry)


# ---------------------------------------------------------------------------
# §21.8 acceptance item 5 — upgrade path is fresh install, not in-place
# ---------------------------------------------------------------------------

class TestUpgradePathIsFreshInstall(unittest.TestCase):
    """§21.8 line 7796, §21.R R4 mitigation: fresh install, not in-place."""

    def test_upgrade_path_string_says_fresh_install(self):
        self.assertIn("fresh install", UPGRADE_PATH)

    def test_upgrade_path_string_says_not_in_place(self):
        self.assertIn("not in-place", UPGRADE_PATH)

    def test_upgrade_path_references_aee_mini_1_0_1(self):
        self.assertIn("1.0.1", UPGRADE_PATH)

    def test_upgrade_path_references_aee_2_0_0_profile_mini(self):
        self.assertIn("2.0.0", UPGRADE_PATH)
        self.assertIn("--profile mini", UPGRADE_PATH)

    def test_render_changelog_entry_contains_upgrade_path(self):
        entry = render_changelog_entry()
        self.assertIn("fresh install", entry)
        self.assertIn("not in-place", entry)


# ---------------------------------------------------------------------------
# §21.8 acceptance item 6 — SemVer policy
# ---------------------------------------------------------------------------

class TestSemVerPolicy(unittest.TestCase):
    """§21.8 line 7796: SemVer MAJOR/MINOR/PATCH."""

    def test_semver_policy_contains_major_minor_patch(self):
        self.assertIn("MAJOR", SEMVER_POLICY)
        self.assertIn("MINOR", SEMVER_POLICY)
        self.assertIn("PATCH", SEMVER_POLICY)

    def test_semver_policy_links_major_to_epic(self):
        self.assertIn("Epic", SEMVER_POLICY)

    def test_semver_policy_links_minor_to_sub_section(self):
        self.assertIn("sub-section", SEMVER_POLICY)

    def test_render_changelog_entry_contains_semver_policy(self):
        entry = render_changelog_entry()
        self.assertIn("MAJOR", entry)
        self.assertIn("MINOR", entry)
        self.assertIn("PATCH", entry)


# ---------------------------------------------------------------------------
# §21.8 acceptance item 7 — release artifacts (3, declarative)
# ---------------------------------------------------------------------------

class TestReleaseArtifacts(unittest.TestCase):
    """§21.8 line 7796: one Docker image, one tarball, one changelog."""

    def test_release_artifacts_count_is_three(self):
        self.assertEqual(len(RELEASE_ARTIFACTS), 3)

    def test_release_artifact_types(self):
        types = {a.artifact_type for a in RELEASE_ARTIFACTS}
        self.assertEqual(
            types, {"docker_image", "tarball", "changelog"}
        )

    def test_docker_image_artifact_uses_versioned_tag(self):
        docker = next(
            a for a in RELEASE_ARTIFACTS if a.artifact_type == "docker_image"
        )
        self.assertIn("{version}", docker.name_template)
        self.assertTrue(docker.name_template.startswith("aee:"))

    def test_tarball_artifact_uses_versioned_name(self):
        tarball = next(
            a for a in RELEASE_ARTIFACTS if a.artifact_type == "tarball"
        )
        self.assertIn("{version}", tarball.name_template)
        self.assertIn(".tar.gz", tarball.name_template)

    def test_changelog_artifact_is_not_publish_side(self):
        changelog = next(
            a for a in RELEASE_ARTIFACTS if a.artifact_type == "changelog"
        )
        self.assertFalse(changelog.is_publish)

    def test_release_artifacts_are_frozen_dataclass(self):
        for a in RELEASE_ARTIFACTS:
            self.assertIsInstance(a, ReleaseArtifact)
            # frozen dataclass: assigning should raise
            with self.assertRaises(Exception):
                a.artifact_type = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# §21.8 dry-run safety — build_release_plan default is dry-run, no publish
# ---------------------------------------------------------------------------

class TestReleasePlanDryRunSafety(unittest.TestCase):
    """§21.8 safety default: dry-run / plan-first, no side effects."""

    def test_default_release_plan_is_dry_run(self):
        plan = build_release_plan()
        self.assertTrue(plan.is_dry_run)

    def test_default_release_plan_will_not_publish(self):
        plan = build_release_plan()
        self.assertFalse(plan.will_publish)

    def test_default_release_plan_will_not_push_registry(self):
        plan = build_release_plan()
        self.assertFalse(plan.will_push_registry)

    def test_default_release_plan_will_not_mutate_channels(self):
        plan = build_release_plan()
        self.assertFalse(plan.will_mutate_channels)

    def test_explicit_dry_run_false_still_does_not_publish(self):
        """The release event is a separately authorizable follow-up.
        Even if a caller passes ``is_dry_run=False``, this slice does
        NOT flip the publish/registry/channel bits — there is no
        ``execute()`` path in this slice."""
        plan = build_release_plan(is_dry_run=False)
        self.assertFalse(plan.is_dry_run)
        self.assertFalse(plan.will_publish)
        self.assertFalse(plan.will_push_registry)
        self.assertFalse(plan.will_mutate_channels)

    def test_release_plan_is_frozen_dataclass(self):
        plan = build_release_plan()
        self.assertIsInstance(plan, ReleasePlan)
        with self.assertRaises(Exception):
            plan.version = "mutated"  # type: ignore[misc]

    def test_release_plan_carries_artifacts(self):
        plan = build_release_plan()
        self.assertEqual(len(plan.artifacts), 3)
        self.assertEqual(plan.artifacts, RELEASE_ARTIFACTS)

    def test_release_plan_changelog_entry_references_adr_009(self):
        plan = build_release_plan()
        self.assertIn("ADR-009", plan.changelog_entry)


# ---------------------------------------------------------------------------
# §21.8 invalid configuration — render_changelog_entry with bad inputs
# ---------------------------------------------------------------------------

class TestInvalidConfiguration(unittest.TestCase):
    """Error paths / invalid configuration guards."""

    def test_render_changelog_entry_with_empty_version_does_not_crash(self):
        # The renderer is a pure string formatter; an empty version is
        # rendered as an empty string in the header. It does NOT crash
        # and does NOT silently substitute a default.
        entry = render_changelog_entry(version="")
        self.assertIn("[", entry)
        self.assertIn("]", entry)
        # The ADR reference is still present (independent of version).
        self.assertIn("ADR-009", entry)

    def test_render_changelog_entry_with_mismatched_mini_version(self):
        entry = render_changelog_entry(mini_last="9.9.9")
        self.assertIn("9.9.9", entry)
        # The upgrade path string is the canonical one (independent of
        # mini_last); the §21.8 upgrade path is still documented.
        self.assertIn("fresh install", entry)

    def test_build_release_plan_with_custom_version(self):
        plan = build_release_plan(version="2.0.0")
        self.assertEqual(plan.version, "2.0.0")
        self.assertIn("2.0.0", plan.changelog_entry)


# ---------------------------------------------------------------------------
# §21.8 rollback / compatibility — no parallel hard-coded version literal
# ---------------------------------------------------------------------------

class TestNoParallelHardCodedVersionLiteral(unittest.TestCase):
    """§21.8 single source of truth: ``aee.__version__`` is the only
    version literal. CLI and release module both read it."""

    def test_cli_imports_aee_version(self):
        """``aee.cli`` imports ``__version__`` from ``aee``."""
        cli_path = Path(aee_cli.__file__)
        tree = ast.parse(cli_path.read_text())
        found_import = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "aee" and node.names:
                    for alias in node.names:
                        if alias.name == "__version__":
                            found_import = True
                            break
        self.assertTrue(
            found_import,
            "aee.cli should import __version__ from aee (no parallel literal)",
        )

    def test_release_module_imports_aee_version(self):
        """``aee.release`` imports ``__version__`` from ``aee``."""
        release_path = Path(_REPO_ROOT) / "aee" / "release" / "__init__.py"
        tree = ast.parse(release_path.read_text())
        found_import = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "aee" and node.names:
                    for alias in node.names:
                        if alias.name == "__version__":
                            found_import = True
                            break
        self.assertTrue(
            found_import,
            "aee.release should import __version__ from aee",
        )

    def test_cli_version_string_uses_imported_version(self):
        """The ``--version`` flag string uses the imported
        ``_AEE_VERSION`` — not a string literal of the version."""
        cli_path = Path(aee_cli.__file__)
        source = cli_path.read_text()
        # The ``--version`` argument uses ``ver=_AEE_VERSION``.
        self.assertIn("_AEE_VERSION", source)
        # There is no hard-coded ``"2.0.0-rc1"`` literal in the cli
        # source — the canonical literal lives only in
        # ``aee/__init__.py``.
        self.assertNotIn('"2.0.0-rc1"', source)
        self.assertNotIn("'2.0.0-rc1'", source)


# ---------------------------------------------------------------------------
# §21.8 compatibility with §21.1–§21.7
# ---------------------------------------------------------------------------

class TestBackwardCompatWithEpic9(unittest.TestCase):
    """§21.1–§21.7 surfaces are unchanged by §21.8."""

    def test_profile_flag_still_works_with_version_flag_present(self):
        """``--profile`` is still parsed (when ``--version`` is not
        passed, the parser must still accept ``--profile``)."""
        parser = _build_parser()
        ns = parser.parse_args(["--profile", "edge", "install", "--dry-run"])
        # ``install`` subparser overwrites args.profile with its own
        # ``--profile`` (default None); the global is recoverable via
        # ``_extract_global_profile``. Just confirm parsing succeeds.
        self.assertEqual(ns.subcommand, "install")

    def test_default_profile_unchanged(self):
        self.assertEqual(DEFAULT_PROFILE, "full")

    def test_known_profiles_unchanged(self):
        self.assertEqual(
            KNOWN_PROFILES, ("full", "mini", "edge", "developer")
        )

    def test_install_subcommand_still_returns_zero(self):
        rc = main(["install", "--profile", "full", "--dry-run"])
        self.assertEqual(rc, 0)

    def test_no_subcommand_still_returns_parse_error(self):
        rc = main([])
        self.assertEqual(rc, 2)  # EXIT_PARSE_ERROR


# ---------------------------------------------------------------------------
# §21.8 release plan content integrity
# ---------------------------------------------------------------------------

class TestReleasePlanContentIntegrity(unittest.TestCase):
    """The release plan's fields are internally consistent."""

    def test_plan_version_matches_aee_version(self):
        plan = build_release_plan()
        self.assertEqual(plan.version, _AEE_VERSION)

    def test_plan_changelog_entry_is_rendered_with_plan_version(self):
        plan = build_release_plan()
        self.assertIn(plan.version, plan.changelog_entry)

    def test_plan_artifacts_match_release_artifacts_constant(self):
        plan = build_release_plan()
        self.assertEqual(plan.artifacts, RELEASE_ARTIFACTS)

    def test_plan_semver_policy_matches_constant(self):
        plan = build_release_plan()
        self.assertEqual(plan.semver_policy, SEMVER_POLICY)

    def test_plan_upgrade_path_matches_constant(self):
        plan = build_release_plan()
        self.assertEqual(plan.upgrade_path, UPGRADE_PATH)

    def test_plan_adr_reference_matches_constant(self):
        plan = build_release_plan()
        self.assertEqual(plan.adr_reference, ADR_REFERENCE)

    def test_plan_mini_last_version_matches_constant(self):
        plan = build_release_plan()
        self.assertEqual(plan.mini_last_version, AEE_MINI_LAST_VERSION)


if __name__ == "__main__":
    unittest.main()