"""Current compatibility documentation and deprecation API contracts.

External AEE-MINI archive ownership was retired; see docs/legacy-retirement.md.
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

from aee.profiles.descriptor import KNOWN_PROFILES, DEFAULT_PROFILE, get_descriptor  # noqa: E402


# ---------------------------------------------------------------------------
# Filesystem constants — canonical paths used by the §21.9 deliverables.
# ---------------------------------------------------------------------------

_UNIFIED_README = Path(_REPO_ROOT) / "README.md"
_LEGACY_REFERENCE = Path(_REPO_ROOT) / "docs" / "legacy-http-profiles.md"
_UNIFIED_MOVED_MATRIX = Path(_REPO_ROOT) / "docs" / "HERMES_ADAPTER_CONTRACT_MATRIX.md"




def _read(path: Path) -> str:
    """Read a file as UTF-8 text; raise with a useful message if absent."""
    if not path.is_file():
        raise FileNotFoundError(
            f"required document/fixture missing: {path.relative_to(_REPO_ROOT)}; "
            "see aee/tests/fixtures/historical_docs/README.md"
        )
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# §21.A item 9a — unified README documents all four profiles
# ---------------------------------------------------------------------------

class TestUnifiedReadmeProfiles(unittest.TestCase):
    """§21.A item 9a: The README-linked legacy reference documents all four profiles."""

    def setUp(self):
        self.reference = _read(_LEGACY_REFERENCE)

    def test_readme_exists_and_nonempty(self):
        self.assertTrue(_UNIFIED_README.is_file())
        self.assertGreater(len(_read(_UNIFIED_README)), 1000)
        self.assertGreater(len(self.reference), 1000)

    def test_readme_mentions_all_four_profile_names(self):
        for name in KNOWN_PROFILES:
            with self.subTest(profile=name):
                self.assertIn(name, self.reference)

    def test_readme_contains_profile_matrix_table(self):
        """Documented matrix order and values match canonical descriptors."""
        rows = [
            [cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in self.reference.splitlines() if line.startswith("|")
        ]
        self.assertIn(["Capability", *[f"`{p}`" for p in KNOWN_PROFILES]], rows)
        documented = {row[0]: row[1:] for row in rows if len(row) == 5}
        fields = {
            "Dispatch": "can_dispatch",
            "Cron creation": "can_create_cron",
            "Subagent delegation": "can_delegate_subagents",
            "Long-running pipelines": "can_long_running_pipelines",
            "Graph queries": "graph_queries",
            "Observability events": "observability_events",
            "DB writes": "db_writes",
            "Production DB access": "production_db_access",
            "Toolset": "toolset",
        }
        for label, field in fields.items():
            expected = []
            for profile in KNOWN_PROFILES:
                value = getattr(get_descriptor(profile), field)
                expected.append(("allowed" if value else "blocked")
                                if isinstance(value, bool) else value)
            with self.subTest(capability=label):
                self.assertEqual(documented.get(label), expected)

    def test_readme_profile_order_matches_descriptor(self):
        """The legacy reference's first-mention order of the four profiles matches
        the canonical ``(full, mini, edge, developer)`` tuple from
        ``descriptor.py`` (single source of truth)."""
        positions = {name: self.reference.find(name) for name in KNOWN_PROFILES}
        for name in KNOWN_PROFILES:
            with self.subTest(profile=name):
                self.assertGreater(positions[name], -1,
                                   f"profile {name!r} not found in legacy reference")
        ordered = sorted(KNOWN_PROFILES, key=lambda n: positions[n])
        self.assertEqual(tuple(ordered), tuple(KNOWN_PROFILES))


# ---------------------------------------------------------------------------
# §21.A item 9b — README mentions --profile flag and install.sh
# ---------------------------------------------------------------------------

class TestUnifiedReadmeProfileFlagAndInstaller(unittest.TestCase):
    """§21.A item 9b: The legacy reference documents ``--profile`` and ``install.sh``."""

    def setUp(self):
        self.reference = _read(_LEGACY_REFERENCE)

    def test_readme_mentions_profile_flag(self):
        self.assertIn("--profile", self.reference)

    def test_readme_mentions_install_sh(self):
        self.assertIn("install.sh", self.reference)

    def test_readme_mentions_docker_run_profile(self):
        """§21.5 Docker selection surface should also be documented."""
        self.assertIn("docker run", self.reference)
        self.assertIn("--profile", self.reference)


# ---------------------------------------------------------------------------
# §21.A item 9c — README references the Master Plan
# ---------------------------------------------------------------------------

class TestUnifiedReadmeMasterPlanReference(unittest.TestCase):
    """§21.A item 9c: The legacy reference explains external Master Plan context."""

    def setUp(self):
        self.reference = _read(_LEGACY_REFERENCE)

    def test_readme_mentions_master_plan(self):
        self.assertIn("Master Plan", self.reference)

    def test_readme_contains_master_plan_filename(self):
        self.assertIn("AEE_MASTER_PLAN.md", self.reference)

    def test_legacy_reference_links_to_repository_migration_guide(self):
        self.assertIn("[migration guide](MIGRATION_FROM_AEE_MINI.md)", self.reference)
        self.assertTrue((_LEGACY_REFERENCE.parent / "MIGRATION_FROM_AEE_MINI.md").is_file())
        self.assertIn("not packaged", self.reference)


# ---------------------------------------------------------------------------
# §21.A item 9d — AEE-MINI README has a deprecation notice
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# §21.A item 9e — AEE-MINI README is short (deprecation notice only)
# ---------------------------------------------------------------------------



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



# ---------------------------------------------------------------------------
# §21.9 — cross-reference: unified README references the moved matrix
# ---------------------------------------------------------------------------

class TestUnifiedReadmeCrossReferencesMovedMatrix(unittest.TestCase):
    """The linked legacy reference cross-references the repository adapter matrix."""

    def setUp(self):
        self.reference = _read(_LEGACY_REFERENCE)

    def test_readme_references_moved_matrix_filename(self):
        self.assertIn("HERMES_ADAPTER_CONTRACT_MATRIX.md", self.reference)

    def test_readme_references_moved_matrix_relative_path(self):
        self.assertIn("[Hermes adapter contract matrix](HERMES_ADAPTER_CONTRACT_MATRIX.md)",
                      self.reference)
        self.assertEqual((_LEGACY_REFERENCE.parent / "HERMES_ADAPTER_CONTRACT_MATRIX.md").resolve(),
                         _UNIFIED_MOVED_MATRIX.resolve())


# ---------------------------------------------------------------------------
# Profile matrix consistency — README vs descriptor.KNOWN_PROFILES
# ---------------------------------------------------------------------------

class TestProfileMatrixConsistency(unittest.TestCase):
    """Single source of truth: the four profile names in the legacy reference must
    match ``aee.profiles.descriptor.KNOWN_PROFILES`` exactly."""

    def setUp(self):
        self.reference = _read(_LEGACY_REFERENCE)

    def test_readme_profile_set_matches_descriptor(self):
        readme_profiles = {
            name for name in KNOWN_PROFILES if name in self.reference
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
    """The linked legacy reference preserves the existing bridge endpoint reference."""

    def setUp(self):
        self.reference = _read(_LEGACY_REFERENCE)

    def test_readme_preserves_post_runs_endpoint(self):
        # The README's Endpoints table renders POST /runs in a markdown
        # table cell, so the literal "POST /runs" (with single spaces)
        # may not appear — accept the table-cell form ``POST | `/runs```
        # or the prose form ``POST /runs``.
        forms = ["POST /runs", "POST  | `/runs`", "POST | `/runs`",
                 "POST `/runs`"]
        self.assertTrue(
            any(form in self.reference for form in forms),
            "none of the expected POST /runs renderings found in legacy reference",
        )

    def test_readme_preserves_health_endpoint(self):
        self.assertIn("/health", self.reference)

    def test_readme_preserves_endpoints_section(self):
        self.assertIn("Endpoints", self.reference)

    def test_readme_preserves_safety_guard_section(self):
        self.assertIn("Safety guard", self.reference)

    def test_readme_preserves_layout_section(self):
        self.assertIn("Layout", self.reference)

    def test_readme_preserves_do_not_pack_section(self):
        self.assertIn("DO NOT pack", self.reference)


# ---------------------------------------------------------------------------
# Broken-link / reference detection — Master Plan path exists on disk
# ---------------------------------------------------------------------------

class TestHistoricalContextReference(unittest.TestCase):
    """The authentic historical Master Plan fixture must exist for preservation checks."""


    def test_master_plan_is_identified_as_external_historical_context(self):
        reference = _read(_LEGACY_REFERENCE)
        self.assertIn("`AEE_MASTER_PLAN.md`", reference)
        self.assertIn("external historical material", reference)
        self.assertIn("[migration guide](MIGRATION_FROM_AEE_MINI.md)", reference)


# ---------------------------------------------------------------------------
# Both READMEs coexist — the unified README is the entry point, the
# AEE-MINI README is the deprecation notice. (Sanity check.)
# ---------------------------------------------------------------------------

class TestRepositoryReadmeExists(unittest.TestCase):
    """The repository README remains the current entry point."""

    def test_unified_readme_exists(self):
        self.assertTrue(_UNIFIED_README.is_file())




class TestCanonicalReadmeLinks(unittest.TestCase):
    def test_mcp_entrypoint_and_legacy_reference_are_discoverable(self):
        readme = _read(_UNIFIED_README)
        for label, target in (
            ("legacy HTTP/profile reference", "docs/legacy-http-profiles.md"),
            ("host setup", "docs/deployment.md"),
            ("tunnel setup", "docs/chatgpt-mcp.md"),
            ("Plugin checklist", "docs/chatgpt-mcp.md"),
        ):
            with self.subTest(target=target):
                self.assertIn(f"[{label}]({target})", readme)
                self.assertTrue((Path(_REPO_ROOT) / target).is_file())
        self.assertIn("Do not use the legacy HTTP installer for this route", readme)

    def test_restricted_mcp_contract_remains_explicit(self):
        readme = _read(_UNIFIED_README)
        for tool in ("aee_status", "aee_agents", "aee_dispatch",
                     "aee_job_status", "aee_job_result"):
            self.assertIn(f"`{tool}`", readme)
        self.assertIn("127.0.0.1:8791/mcp (restricted)", readme)
        self.assertIn("`codex` / `read_only`", readme)
        self.assertIn("`aee_exec` remains local/full only", readme)


if __name__ == "__main__":
    unittest.main()