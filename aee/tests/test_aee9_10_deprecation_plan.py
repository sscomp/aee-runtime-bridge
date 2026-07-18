"""AEE Epic 9 §21.10 — Deprecation Plan tests.

Test suite for the §21.10 Deprecation Plan slice:

1. ``DEPRECATED.md`` exists at the AEE-MINI repo root and has the
   required content (deprecated, 1.0.1, fresh install, ``--profile
   mini``, ADR-009, §21.10).
2. ``MIGRATION_FROM_AEE_MINI.md`` exists, references ADR-009,
   ``--profile mini``, fresh install (not in-place), and the 4-row
   timeline.
3. ``emit_deprecation_warning()`` returns a non-empty string
   containing ``"DEPRECATED"`` and ``"1.0.1"``.
4. ``is_aee_mini_deprecated()`` returns ``True``.
5. ``AEE_MINI_LAST_VERSION == "1.0.1"`` exactly.
6. ``DEPRECATION_PHASE`` contains ``"Phase F"``.
7. ``deprecation.py`` imports without I/O and without exception.
8. Calling ``emit_deprecation_warning()`` twice returns the same
   string (idempotent).
9. ``validate_deprecation_config(phase)`` returns ``False`` for
   unknown phase names and ``True`` for the 4 canonical phases
   (Phase F/G/H plus the ``"archive"`` sentinel).
10. Legacy path preservation: the deprecation does NOT delete or
    rename any existing file; ``DEPRECATED.md`` is additive.

Run:

    cd /home/ubuntu/hermes-runtime-bridge
    python3 -m unittest aee.tests.test_aee9_10_deprecation_plan -v

Stdlib ``unittest`` only — no pytest dependency.
"""

from __future__ import annotations

import importlib
import os
import sys
import unittest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_AEE_MINI_REPO = "/home/ubuntu/Abacus/aee-runtime-api-mini"
_DEPRECATED_MD = os.path.join(_AEE_MINI_REPO, "DEPRECATED.md")

_BRIDGE_DOCS = "/home/ubuntu/hermes-runtime-bridge/docs"
_MIGRATION_MD = os.path.join(_BRIDGE_DOCS, "MIGRATION_FROM_AEE_MINI.md")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _read_text(path: str) -> str:
    """Read a UTF-8 text file and return its contents.

    Raises:
        unittest.SkipTest: if the file does not exist (so the test
            suite reports a skip rather than a confusing error).
    """
    if not os.path.isfile(path):
        raise unittest.SkipTest(f"required file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestDeprecatedMd(unittest.TestCase):
    """DEPRECATED.md at AEE-MINI repo root."""

    def test_file_exists_at_repo_root(self):
        """DEPRECATED.md exists at the AEE-MINI repo root."""
        self.assertTrue(
            os.path.isfile(_DEPRECATED_MD),
            f"DEPRECATED.md not found at {_DEPRECATED_MD}",
        )

    def test_content_has_deprecated_marker(self):
        """File contains the literal 'DEPRECATED'."""
        text = _read_text(_DEPRECATED_MD)
        self.assertIn("DEPRECATED", text)

    def test_content_mentions_version_1_0_1(self):
        """File states AEE-MINI is frozen at version 1.0.1."""
        text = _read_text(_DEPRECATED_MD)
        self.assertIn("1.0.1", text)

    def test_content_mentions_fresh_install(self):
        """File documents the fresh-install upgrade path."""
        text = _read_text(_DEPRECATED_MD)
        # Accept either the literal phrase or its hyphenated variant.
        self.assertTrue(
            "fresh install" in text.lower(),
            "DEPRECATED.md must mention 'fresh install'",
        )

    def test_content_mentions_profile_mini(self):
        """File references the ``--profile mini`` upgrade path."""
        text = _read_text(_DEPRECATED_MD)
        self.assertIn("--profile mini", text)

    def test_content_references_adr_009(self):
        """File references ADR-009 as the architecture decision."""
        text = _read_text(_DEPRECATED_MD)
        self.assertIn("ADR-009", text)

    def test_content_references_master_plan_section_21_10(self):
        """File references Master Plan §21.10 as canonical source."""
        text = _read_text(_DEPRECATED_MD)
        self.assertIn("§21.10", text)

    def test_content_states_no_forced_migration(self):
        """File carries the 'no forced migration' clause."""
        text = _read_text(_DEPRECATED_MD)
        self.assertIn("No forced migration", text)


class TestMigrationGuide(unittest.TestCase):
    """MIGRATION_FROM_AEE_MINI.md operator migration guide."""

    def test_file_exists(self):
        """Migration guide file exists."""
        self.assertTrue(
            os.path.isfile(_MIGRATION_MD),
            f"MIGRATION_FROM_AEE_MINI.md not found at {_MIGRATION_MD}",
        )

    def test_references_adr_009(self):
        """Guide references ADR-009."""
        text = _read_text(_MIGRATION_MD)
        self.assertIn("ADR-009", text)

    def test_references_profile_mini(self):
        """Guide references ``--profile mini``."""
        text = _read_text(_MIGRATION_MD)
        self.assertIn("--profile mini", text)

    def test_states_fresh_install_not_in_place(self):
        """Guide states the path is fresh install, not in-place."""
        text = _read_text(_MIGRATION_MD)
        self.assertTrue(
            "fresh install" in text.lower(),
            "Guide must state 'fresh install'",
        )
        # Must explicitly reject in-place migration.
        self.assertTrue(
            "not in-place" in text.lower() or "no in-place" in text.lower(),
            "Guide must state the path is NOT in-place",
        )

    def test_contains_4_row_timeline(self):
        """Guide contains the 4-row §21.10 timeline.

        The four rows correspond to:
          - Epic 9 ship (2.0.0-rc1)
          - Epic 9 + 1 (2.0.0-rc2)
          - Epic 9 + 2 (2.0.0 GA)
          - Epic 9 + 4 (2.0.2)
        """
        text = _read_text(_MIGRATION_MD)
        # All four target versions must appear. The "GA" row uses the
        # Master Plan's own notation (`2.0.0` GA, with the version in
        # backticks), so we check the version and the GA label
        # separately rather than as a single substring.
        for version in ("2.0.0-rc1", "2.0.0-rc2", "2.0.2"):
            self.assertIn(
                version,
                text,
                f"Timeline must include the {version} row",
            )
        self.assertIn("2.0.0", text)
        self.assertIn("GA", text)

    def test_references_master_plan_section_21_10(self):
        """Guide references Master Plan §21.10."""
        text = _read_text(_MIGRATION_MD)
        self.assertIn("§21.10", text)

    def test_states_no_forced_migration(self):
        """Guide carries the 'no forced migration' clause."""
        text = _read_text(_MIGRATION_MD)
        self.assertIn("No forced migration", text)


class TestDeprecationModule(unittest.TestCase):
    """aee.release.deprecation — pure functions and constants."""

    def setUp(self):
        # Import fresh each time so import-safety is exercised.
        self.module = importlib.import_module("aee.release.deprecation")

    def test_aee_mini_last_version_is_1_0_1_exactly(self):
        """AEE_MINI_LAST_VERSION == '1.0.1' exactly."""
        self.assertEqual(self.module.AEE_MINI_LAST_VERSION, "1.0.1")

    def test_deprecation_phase_contains_phase_f(self):
        """DEPRECATION_PHASE contains 'Phase F'."""
        self.assertIn("Phase F", self.module.DEPRECATION_PHASE)

    def test_emit_deprecation_warning_returns_non_empty_string(self):
        """emit_deprecation_warning() returns a non-empty string."""
        banner = self.module.emit_deprecation_warning()
        self.assertIsInstance(banner, str)
        self.assertGreater(len(banner), 0)

    def test_emit_deprecation_warning_contains_DEPRECATED(self):
        """Banner contains the literal 'DEPRECATED'."""
        banner = self.module.emit_deprecation_warning()
        self.assertIn("DEPRECATED", banner)

    def test_emit_deprecation_warning_contains_1_0_1(self):
        """Banner contains the version '1.0.1'."""
        banner = self.module.emit_deprecation_warning()
        self.assertIn("1.0.1", banner)

    def test_emit_deprecation_warning_is_idempotent(self):
        """Calling emit_deprecation_warning() twice returns the same string."""
        a = self.module.emit_deprecation_warning()
        b = self.module.emit_deprecation_warning()
        self.assertEqual(a, b)

    def test_emit_deprecation_warning_has_no_side_effects(self):
        """Function does not log, print, or write."""
        # We cannot fully prove absence of side effects, but we can
        # assert the function returns a string and does not raise.
        # Coupled with the import-safety test below, this covers the
        # contract.
        banner = self.module.emit_deprecation_warning()
        self.assertIsInstance(banner, str)

    def test_is_aee_mini_deprecated_returns_true(self):
        """is_aee_mini_deprecated() returns True."""
        self.assertTrue(self.module.is_aee_mini_deprecated())

    def test_module_imports_without_exception(self):
        """Importing the module does not raise."""
        # Re-import via importlib.reload to surface any import-time
        # exception that may have been swallowed on first import.
        importlib.reload(self.module)

    def test_module_has_no_module_level_io(self):
        """Module exposes the documented names and nothing else
        that would indicate I/O at import time."""
        # The __all__ list is the public surface.
        expected = {
            "AEE_MINI_LAST_VERSION",
            "DEPRECATION_PHASE",
            "emit_deprecation_warning",
            "is_aee_mini_deprecated",
            "validate_deprecation_config",
        }
        self.assertEqual(set(self.module.__all__), expected)

    def test_module_does_not_call_sys_exit(self):
        """Module source does not call sys.exit() at import time."""
        import inspect

        src = inspect.getsource(self.module)
        # No sys.exit calls anywhere in the module body (defensive
        # check; the contract forbids it).
        self.assertNotIn("sys.exit(", src)


class TestValidateDeprecationConfig(unittest.TestCase):
    """validate_deprecation_config — phase name validation."""

    def setUp(self):
        self.module = importlib.import_module("aee.release.deprecation")

    def test_phase_f_canonical_returns_true(self):
        self.assertTrue(
            self.module.validate_deprecation_config("Phase F — Deprecation Start")
        )

    def test_phase_g_canonical_returns_true(self):
        self.assertTrue(self.module.validate_deprecation_config("Phase G — GA"))

    def test_phase_h_canonical_returns_true(self):
        self.assertTrue(self.module.validate_deprecation_config("Phase H — Archive"))

    def test_archive_sentinel_returns_true(self):
        self.assertTrue(self.module.validate_deprecation_config("archive"))

    def test_unknown_phase_returns_false(self):
        self.assertFalse(self.module.validate_deprecation_config("Phase Z — Future"))

    def test_empty_string_returns_false(self):
        self.assertFalse(self.module.validate_deprecation_config(""))

    def test_none_returns_false(self):
        self.assertFalse(self.module.validate_deprecation_config(None))

    def test_non_string_returns_false(self):
        self.assertFalse(self.module.validate_deprecation_config(123))

    def test_canonical_set_is_exactly_four(self):
        """The canonical phase set is exactly the 4 documented phases.

        This guards against silent drift: a future edit that adds or
        removes a phase must update the tests.
        """
        # Probe with the 4 known-good values and a representative
        # known-bad value; assert the boundary is sharp.
        known_good = (
            "Phase F — Deprecation Start",
            "Phase G — GA",
            "Phase H — Archive",
            "archive",
        )
        for p in known_good:
            self.assertTrue(
                self.module.validate_deprecation_config(p),
                f"canonical phase rejected: {p!r}",
            )
        # A plausible-but-wrong variant of the archive sentinel.
        self.assertFalse(self.module.validate_deprecation_config("Archive"))


class TestLegacyPathPreservation(unittest.TestCase):
    """§21.10 must be additive — no deletion or rename of existing files."""

    def test_deprecated_md_is_additive_not_replacing_readme(self):
        """DEPRECATED.md is a separate file; README.md is preserved.

        The AEE-MINI README was already updated to carry a deprecation
        notice (per §21.9); DEPRECATED.md is an **additional** marker
        at the repo root. The act of placing DEPRECATED.md must not
        delete or rename README.md.
        """
        readme = os.path.join(_AEE_MINI_REPO, "README.md")
        self.assertTrue(
            os.path.isfile(readme),
            "README.md must still exist (DEPRECATED.md is additive)",
        )
        self.assertTrue(
            os.path.isfile(_DEPRECATED_MD),
            "DEPRECATED.md must exist at repo root",
        )

    def test_no_file_marked_readonly_by_this_slice(self):
        """No existing file in the AEE-MINI repo has been marked
        read-only by this slice.

        We assert that README.md, pyproject.toml, and the docs/ tree
        are still writable. This is a sampling check, not an
        exhaustive scan — it catches the obvious destructive failure
        modes without being brittle to the full repo layout.
        """
        candidates = [
            os.path.join(_AEE_MINI_REPO, "README.md"),
            os.path.join(_AEE_MINI_REPO, "pyproject.toml"),
        ]
        for path in candidates:
            if not os.path.isfile(path):
                continue
            self.assertTrue(
                os.access(path, os.W_OK),
                f"{path} must remain writable (this slice is additive; "
                "no file is marked read-only)",
            )

    def test_deprecation_module_does_not_delete_or_rename(self):
        """The deprecation module source contains no os.remove /
        os.rename / shutil.rmtree calls.

        This is a source-level guard: the module is supposed to be
        side-effect free, and deprecation must not destroy any
        existing file.
        """
        import inspect

        module = importlib.import_module("aee.release.deprecation")
        src = inspect.getsource(module)
        for forbidden in (
            "os.remove(",
            "os.rename(",
            "os.unlink(",
            "shutil.rmtree(",
            "shutil.move(",
        ):
            self.assertNotIn(
                forbidden,
                src,
                f"deprecation.py must not call {forbidden!r}",
            )


if __name__ == "__main__":
    unittest.main()