"""AEE Epic 9.9 §21.9 — Documentation Migration targeted tests.

Tests the §21.9 Documentation Migration contract from the authoritative
Master Plan (``/home/ubuntu/Abacus/AEE/AEE_MASTER_PLAN.md`` §21.9,
line 7798) and the §21.A acceptance criterion item 9 (line 7856):

    §21.9 — Unified ``README.md`` documents all four profiles; AEE-MINI
    ``README.md`` has deprecation notice.

§21.9 proposal (verbatim from line 7802):

    Unified repo's ``README.md`` is the **single entry point**. Documents
    all four profiles, the ``--profile`` flag, the installer, and links to
    this Master Plan. AEE-MINI's
    ``docs/HERMES_ADAPTER_CONTRACT_MATRIX.md`` is **moved** (not copied)
    into the unified repo's ``docs/`` and updated to reference the
    unified API. AEE-MINI's ``README.md`` is replaced with a
    **deprecation notice** pointing to the unified repo +
    ``--profile mini``. No documentation is deleted; AEE-MINI docs are
    archived in the frozen repo.

Coverage (per workorder §6):

  * §21.A item 9a — unified ``README.md`` documents all four profiles
    (``full``, ``mini``, ``edge``, ``developer``).
  * §21.A item 9b — unified ``README.md`` mentions ``--profile`` and
    ``install.sh``.
  * §21.A item 9c — unified ``README.md`` references the Master Plan.
  * §21.A item 9d — AEE-MINI ``README.md`` contains a deprecation
    notice.
  * §21.A item 9e — AEE-MINI ``README.md`` is short (deprecation
    notice only; the long adapter matrix is gone).
  * §21.9 "moved not copied" — the unified repo has
    ``docs/HERMES_ADAPTER_CONTRACT_MATRIX.md`` (exists, non-empty).
  * §21.9 "updated to reference unified API" — moved file's header
    references the unified adapter path ``aee/adapters/hermes_adapter.py``
    and mentions §21.9 migration.
  * §21.9 "no documentation deleted" — AEE-MINI archive copy still
    exists on disk.
  * §21.9 cross-reference — unified ``README.md`` references the
    moved matrix file.
  * Profile matrix consistency — the four profile names in the README
    match ``aee.profiles.descriptor.KNOWN_PROFILES`` (single source of
    truth).
  * Backward compat — the existing bridge endpoints table is preserved
    in ``README.md`` (``POST /runs`` and ``/health``).
  * Invalid-state handling — ``KNOWN_PROFILES`` is exactly
    ``(full, mini, edge, developer)``.
  * Broken-link/reference detection — the Master Plan path referenced
    in the README exists on disk.

Stdlib only (``unittest``, ``pathlib``, ``os``). No pytest, no
subprocess, no network.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

# Bootstrap sys.path so ``aee`` is importable when the test is run
# directly via ``python -m unittest aee.tests.test_aee99...``.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))

sys.path.insert(0, _REPO_ROOT)

from aee.profiles.descriptor import KNOWN_PROFILES, DEFAULT_PROFILE  # noqa: E402


# ---------------------------------------------------------------------------
# Filesystem constants — canonical paths used by the §21.9 deliverables.
# ---------------------------------------------------------------------------

_UNIFIED_README = Path(_REPO_ROOT) / "README.md"
_UNIFIED_MOVED_MATRIX = Path(_REPO_ROOT) / "docs" / "HERMES_ADAPTER_CONTRACT_MATRIX.md"

_AEE_MINI_REPO_ROOT = Path("/home/ubuntu/Abacus/aee-runtime-api-mini")
_AEE_MINI_README = _AEE_MINI_REPO_ROOT / "README.md"
_AEE_MINI_ARCHIVE_MATRIX = (
    _AEE_MINI_REPO_ROOT / "docs" / "HERMES_ADAPTER_CONTRACT_MATRIX.md"
)

_MASTER_PLAN_PATH = Path("/home/ubuntu/Abacus/AEE/AEE_MASTER_PLAN.md")


def _read(path: Path) -> str:
    """Read a file as UTF-8 text; raise with a useful message if absent."""
    if not path.is_file():
        raise FileNotFoundError(f"required file missing: {path}")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# §21.A item 9a — unified README documents all four profiles
# ---------------------------------------------------------------------------

class TestUnifiedReadmeProfiles(unittest.TestCase):
    """§21.A item 9a: Unified ``README.md`` documents all four profiles."""

    def setUp(self):
        self.readme = _read(_UNIFIED_README)

    def test_readme_exists_and_nonempty(self):
        self.assertTrue(_UNIFIED_README.is_file())
        self.assertGreater(len(self.readme), 1000)

    def test_readme_mentions_all_four_profile_names(self):
        for name in KNOWN_PROFILES:
            with self.subTest(profile=name):
                self.assertIn(name, self.readme)

    def test_readme_contains_profile_matrix_table(self):
        """The §21.1 matrix should be rendered with all four columns."""
        for name in KNOWN_PROFILES:
            with self.subTest(profile=name):
                self.assertIn(f"`{name}`", self.readme)

    def test_readme_profile_order_matches_descriptor(self):
        """The README's first-mention order of the four profiles matches
        the canonical ``(full, mini, edge, developer)`` tuple from
        ``descriptor.py`` (single source of truth)."""
        positions = {name: self.readme.find(name) for name in KNOWN_PROFILES}
        for name in KNOWN_PROFILES:
            with self.subTest(profile=name):
                self.assertGreater(positions[name], -1,
                                   f"profile {name!r} not found in README")
        ordered = sorted(KNOWN_PROFILES, key=lambda n: positions[n])
        self.assertEqual(tuple(ordered), tuple(KNOWN_PROFILES))


# ---------------------------------------------------------------------------
# §21.A item 9b — README mentions --profile flag and install.sh
# ---------------------------------------------------------------------------

class TestUnifiedReadmeProfileFlagAndInstaller(unittest.TestCase):
    """§21.A item 9b: README mentions ``--profile`` and ``install.sh``."""

    def setUp(self):
        self.readme = _read(_UNIFIED_README)

    def test_readme_mentions_profile_flag(self):
        self.assertIn("--profile", self.readme)

    def test_readme_mentions_install_sh(self):
        self.assertIn("install.sh", self.readme)

    def test_readme_mentions_docker_run_profile(self):
        """§21.5 Docker selection surface should also be documented."""
        self.assertIn("docker run", self.readme)
        self.assertIn("--profile", self.readme)


# ---------------------------------------------------------------------------
# §21.A item 9c — README references the Master Plan
# ---------------------------------------------------------------------------

class TestUnifiedReadmeMasterPlanReference(unittest.TestCase):
    """§21.A item 9c: README references the Master Plan."""

    def setUp(self):
        self.readme = _read(_UNIFIED_README)

    def test_readme_mentions_master_plan(self):
        self.assertIn("Master Plan", self.readme)

    def test_readme_contains_master_plan_filename(self):
        self.assertIn("AEE_MASTER_PLAN.md", self.readme)

    def test_readme_contains_master_plan_absolute_path(self):
        """The Master Plan path referenced in the README should exist
        on disk (broken-link/reference detection)."""
        self.assertIn(str(_MASTER_PLAN_PATH), self.readme)


# ---------------------------------------------------------------------------
# §21.A item 9d — AEE-MINI README has a deprecation notice
# ---------------------------------------------------------------------------

class TestAeeMiniReadmeDeprecationNotice(unittest.TestCase):
    """§21.A item 9d: AEE-MINI ``README.md`` has a deprecation notice."""

    def setUp(self):
        self.readme = _read(_AEE_MINI_README)

    def test_aee_mini_readme_exists(self):
        self.assertTrue(_AEE_MINI_README.is_file())

    def test_readme_contains_deprecation_marker(self):
        lowered = self.readme.lower()
        self.assertIn("deprecat", lowered)

    def test_readme_redirects_to_profile_mini(self):
        lowered = self.readme.lower()
        self.assertIn("--profile mini", lowered)

    def test_readme_redirects_to_unified_repo(self):
        self.assertIn("/home/ubuntu/hermes-runtime-bridge", self.readme)

    def test_readme_references_master_plan_section_21_10(self):
        """The deprecation notice should point operators to §21.10
        for the full deprecation timeline."""
        self.assertIn("§21.10", self.readme)

    def test_readme_states_frozen_at_1_0_1(self):
        """AEE-MINI 1.0.1 is the last release of the line; the notice
        should say so."""
        self.assertIn("1.0.1", self.readme)

    def test_readme_preserves_original_title(self):
        """Old links should still resolve visually — the H1 is
        preserved."""
        self.assertTrue(self.readme.lstrip().startswith("# AEE Runtime API Mini"))


# ---------------------------------------------------------------------------
# §21.A item 9e — AEE-MINI README is short (deprecation notice only)
# ---------------------------------------------------------------------------

class TestAeeMiniReadmeIsShort(unittest.TestCase):
    """§21.A item 9e: AEE-MINI ``README.md`` is the deprecation notice
    only; the long adapter matrix is gone (moved, not duplicated here)."""

    def test_aee_mini_readme_line_count_is_small(self):
        line_count = len(_read(_AEE_MINI_README).splitlines())
        self.assertLess(line_count, 100,
                        f"AEE-MINI README should be a short deprecation "
                        f"notice (<100 lines); got {line_count}")

    def test_aee_mini_readme_does_not_document_adapter_contract(self):
        """The long adapter contract matrix should NOT live in the
        AEE-MINI README anymore (it has been moved to the unified
        repo). The README may *mention* the matrix by name, but it
        should not contain the contract tables themselves."""
        readme = _read(_AEE_MINI_README)
        self.assertNotIn("VERIFIED_FROM_CODE", readme)
        self.assertNotIn("VERIFIED_FROM_TEST_STUB", readme)


# ---------------------------------------------------------------------------
# §21.9 — "moved not copied" — the unified repo has the matrix file
# ---------------------------------------------------------------------------

class TestMovedMatrixFileExists(unittest.TestCase):
    """§21.9: the matrix is **moved** into the unified repo's ``docs/``."""

    def test_unified_matrix_file_exists(self):
        self.assertTrue(_UNIFIED_MOVED_MATRIX.is_file(),
                        f"missing moved matrix: {_UNIFIED_MOVED_MATRIX}")

    def test_unified_matrix_file_nonempty(self):
        content = _read(_UNIFIED_MOVED_MATRIX)
        self.assertGreater(len(content), 1000)

    def test_unified_matrix_file_has_expected_title(self):
        content = _read(_UNIFIED_MOVED_MATRIX)
        self.assertIn("Hermes Adapter Contract Matrix", content)


# ---------------------------------------------------------------------------
# §21.9 — "updated to reference the unified API"
# ---------------------------------------------------------------------------

class TestMovedMatrixHeaderReferencesUnifiedApi(unittest.TestCase):
    """§21.9: the moved file is updated to reference the unified API."""

    def setUp(self):
        self.content = _read(_UNIFIED_MOVED_MATRIX)

    def test_header_references_unified_adapter_path(self):
        self.assertIn("aee/adapters/hermes_adapter.py", self.content)

    def test_header_does_not_reference_aee_mini_adapter_path(self):
        """The AEE-MINI adapter path ``aee_runtime_api/adapters/hermes.py``
        should NOT appear as the *target file* (it may appear in the
        migration note as the source). The Target file line must point
        at the unified path."""
        # The AEE-MINI path may appear in the migration provenance
        # note, but the **Target file:** directive must be the unified
        # path. Check that the Target file line is the unified path.
        for line in self.content.splitlines():
            if line.strip().startswith("**Target file:**"):
                self.assertIn("aee/adapters/hermes_adapter.py", line)
                self.assertNotIn("aee_runtime_api/adapters/hermes.py", line)
                return
        self.fail("Target file directive not found in moved matrix")

    def test_header_mentions_section_21_9_migration(self):
        lowered = self.content.lower()
        self.assertIn("§21.9", self.content) or self.assertIn("21.9", lowered)

    def test_header_mentions_migration_provenance(self):
        """The moved file should note that it was moved from AEE-MINI."""
        lowered = self.content.lower()
        self.assertIn("migrated", lowered)
        self.assertIn("aee-mini", lowered) or self.assertIn("AEE-MINI", self.content)


# ---------------------------------------------------------------------------
# §21.9 — "no documentation deleted" — AEE-MINI archive still on disk
# ---------------------------------------------------------------------------

class TestAeeMiniArchivePreserved(unittest.TestCase):
    """§21.9: no documentation is deleted; the AEE-MINI frozen archive
    stays on disk untouched."""

    def test_aee_mini_archive_matrix_still_exists(self):
        self.assertTrue(_AEE_MINI_ARCHIVE_MATRIX.is_file(),
                        f"AEE-MINI archive matrix should still exist: "
                        f"{_AEE_MINI_ARCHIVE_MATRIX}")

    def test_aee_mini_archive_matrix_is_nonempty(self):
        content = _read(_AEE_MINI_ARCHIVE_MATRIX)
        self.assertGreater(len(content), 1000)


# ---------------------------------------------------------------------------
# §21.9 — cross-reference: unified README references the moved matrix
# ---------------------------------------------------------------------------

class TestUnifiedReadmeCrossReferencesMovedMatrix(unittest.TestCase):
    """§21.9: the unified README should cross-reference
    ``docs/HERMES_ADAPTER_CONTRACT_MATRIX.md``."""

    def setUp(self):
        self.readme = _read(_UNIFIED_README)

    def test_readme_references_moved_matrix_filename(self):
        self.assertIn("HERMES_ADAPTER_CONTRACT_MATRIX.md", self.readme)

    def test_readme_references_moved_matrix_relative_path(self):
        self.assertIn("docs/HERMES_ADAPTER_CONTRACT_MATRIX.md", self.readme)


# ---------------------------------------------------------------------------
# Profile matrix consistency — README vs descriptor.KNOWN_PROFILES
# ---------------------------------------------------------------------------

class TestProfileMatrixConsistency(unittest.TestCase):
    """Single source of truth: the four profile names in the README must
    match ``aee.profiles.descriptor.KNOWN_PROFILES`` exactly."""

    def setUp(self):
        self.readme = _read(_UNIFIED_README)

    def test_readme_profile_set_matches_descriptor(self):
        readme_profiles = {
            name for name in KNOWN_PROFILES if name in self.readme
        }
        self.assertEqual(readme_profiles, set(KNOWN_PROFILES))

    def test_known_profiles_is_exactly_four_canonical_values(self):
        """Invalid-state handling: ``KNOWN_PROFILES`` is exactly the
        canonical ``(full, mini, edge, developer)`` tuple."""
        self.assertEqual(tuple(KNOWN_PROFILES),
                         ("full", "mini", "edge", "developer"))

    def test_default_profile_is_full(self):
        self.assertEqual(DEFAULT_PROFILE, "full")


# ---------------------------------------------------------------------------
# Backward compat — existing bridge endpoints table preserved
# ---------------------------------------------------------------------------

class TestBackwardCompatBridgeContent(unittest.TestCase):
    """§21.9 grows the README; it does not lose the existing bridge
    endpoint reference."""

    def setUp(self):
        self.readme = _read(_UNIFIED_README)

    def test_readme_preserves_post_runs_endpoint(self):
        # The README's Endpoints table renders POST /runs in a markdown
        # table cell, so the literal "POST /runs" (with single spaces)
        # may not appear — accept the table-cell form ``POST | `/runs```
        # or the prose form ``POST /runs``.
        forms = ["POST /runs", "POST  | `/runs`", "POST | `/runs`",
                 "POST `/runs`"]
        self.assertTrue(
            any(form in self.readme for form in forms),
            "none of the expected POST /runs renderings found in README",
        )

    def test_readme_preserves_health_endpoint(self):
        self.assertIn("/health", self.readme)

    def test_readme_preserves_endpoints_section(self):
        self.assertIn("Endpoints", self.readme)

    def test_readme_preserves_safety_guard_section(self):
        self.assertIn("Safety guard", self.readme)

    def test_readme_preserves_layout_section(self):
        self.assertIn("Layout", self.readme)

    def test_readme_preserves_do_not_pack_section(self):
        self.assertIn("DO NOT pack", self.readme)


# ---------------------------------------------------------------------------
# Broken-link / reference detection — Master Plan path exists on disk
# ---------------------------------------------------------------------------

class TestMasterPlanPathResolves(unittest.TestCase):
    """The Master Plan path referenced in the README must exist on disk."""

    def test_master_plan_path_exists(self):
        self.assertTrue(_MASTER_PLAN_PATH.is_file(),
                        f"Master Plan not found at {_MASTER_PLAN_PATH}")

    def test_master_plan_path_is_referenced_in_readme(self):
        readme = _read(_UNIFIED_README)
        self.assertIn(str(_MASTER_PLAN_PATH), readme)


# ---------------------------------------------------------------------------
# Both READMEs coexist — the unified README is the entry point, the
# AEE-MINI README is the deprecation notice. (Sanity check.)
# ---------------------------------------------------------------------------

class TestBothReadmesCoexist(unittest.TestCase):
    """Both READMEs exist; the unified README is the entry point and the
    AEE-MINI README is the deprecation notice."""

    def test_unified_readme_exists(self):
        self.assertTrue(_UNIFIED_README.is_file())

    def test_aee_mini_readme_exists(self):
        self.assertTrue(_AEE_MINI_README.is_file())

    def test_unified_readme_is_longer_than_aee_mini_readme(self):
        """The unified README is the single entry point; the AEE-MINI
        README is a short deprecation notice."""
        unified_size = _UNIFIED_README.stat().st_size
        mini_size = _AEE_MINI_README.stat().st_size
        self.assertGreater(unified_size, mini_size,
                           f"unified README ({unified_size}B) should be "
                           f"larger than AEE-MINI deprecation notice "
                           f"({mini_size}B)")


if __name__ == "__main__":
    unittest.main()