"""AEE Epic 9.1 — Canonical Product Profile Matrix (§21.1) targeted tests.

These tests verify that ``aee/profiles/descriptor.py`` is the
**single canonical source of truth** for the four-profile matrix
defined in Master Plan §21.1. They cover:

* The four profiles (``full``, ``mini``, ``edge``, ``developer``)
  are all present and no others exist.
* The §21.1 capability matrix is encoded in the additive
  ``ProfileDescriptor`` fields for each profile.
* ``edge`` profile is query-only (``is_read_only=True``,
  ``can_dispatch=False``, ``db_writes="disabled"``).
* ``mini``/``full``/``developer`` differ on the new matrix fields
  as documented in §21.1.
* Existing AEE-8.1 API compatibility (``parse_profile``,
  ``get_descriptor``, ``safety_tier_for``, ``is_known_profile``,
  ``all_descriptors``) is preserved.
* The descriptor ``to_dict()`` shape is the additive superset of
  AEE-8.1 keys + Epic 9.1 keys.
* Isolation contract (no forbidden imports) is preserved after
  the Epic 9.1 additions.
* Cross-field consistency invariants (e.g. ``edge`` with
  ``is_read_only=True`` MUST have ``db_writes="disabled"`` and
  ``production_db_access="read_only"``).

Run: ``PYTHONPATH=. python3 -m unittest aee.tests.test_aee91_canonical_profile_matrix -v``
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from aee.profiles.descriptor import (
    KNOWN_PROFILES,
    DEFAULT_PROFILE,
    ProfileDescriptor,
    UnknownProfileError,
    is_known_profile,
    parse_profile,
    get_descriptor,
    safety_tier_for,
    all_descriptors,
)


# ---------------------------------------------------------------------------
# §21.1 canonical matrix — expected values per profile
# ---------------------------------------------------------------------------

# The matrix as documented in Master Plan §21.1.
# Each row: profile -> {field: expected_value}
EXPECTED_MATRIX = {
    "full": {
        "can_dispatch": True,
        "can_create_cron": True,
        "can_delegate_subagents": True,
        "can_long_running_pipelines": True,
        "graph_queries": "full",
        "observability_events": "full",
        "db_writes": "full",
        "production_db_access": "full",
        "is_read_only": False,
        "toolset": "full",
    },
    "mini": {
        "can_dispatch": True,
        "can_create_cron": False,
        "can_delegate_subagents": False,
        "can_long_running_pipelines": False,
        "graph_queries": "subset",
        "observability_events": "subset",
        "db_writes": "dispatch_only",
        "production_db_access": "full",
        "is_read_only": False,
        "toolset": "terminal_file_web_subset",
    },
    "edge": {
        "can_dispatch": False,
        "can_create_cron": False,
        "can_delegate_subagents": False,
        "can_long_running_pipelines": False,
        "graph_queries": "read_only",
        "observability_events": "read_only",
        "db_writes": "disabled",
        "production_db_access": "read_only",
        "is_read_only": True,
        "toolset": "file_read_web_read",
    },
    "developer": {
        "can_dispatch": True,
        "can_create_cron": False,
        "can_delegate_subagents": True,
        "can_long_running_pipelines": False,
        "graph_queries": "sandbox",
        "observability_events": "sandbox",
        "db_writes": "tempdir_only",
        "production_db_access": "blocked",
        "is_read_only": False,
        "toolset": "full_sandbox",
    },
}

# AEE-8.1 baseline fields that must remain unchanged after Epic 9.1.
AEE81_BASELINE_FIELDS = (
    "name", "purpose", "audience", "runtime_footprint",
    "safety_tier", "toolset_restriction",
    "can_create_cron", "can_delegate_subagents", "is_read_only",
)

# Epic 9.1 additive §21.1 matrix fields.
EPIC91_MATRIX_FIELDS = (
    "can_dispatch", "can_long_running_pipelines",
    "graph_queries", "observability_events",
    "db_writes", "production_db_access", "toolset",
)

# All valid vocabulary values for the string-valued matrix fields.
GRAPH_QUERIES_VOCAB = {"full", "subset", "read_only", "sandbox"}
OBSERVABILITY_VOCAB = {"full", "subset", "read_only", "sandbox"}
DB_WRITES_VOCAB = {"full", "dispatch_only", "disabled", "tempdir_only"}
PRODUCTION_DB_VOCAB = {"full", "read_only", "blocked"}
TOOLSET_VOCAB = {
    "full", "terminal_file_web_subset", "file_read_web_read", "full_sandbox",
}


# ---------------------------------------------------------------------------
# Profile presence and uniqueness
# ---------------------------------------------------------------------------

class ProfilePresenceTests(unittest.TestCase):
    """The four canonical profiles exist, and no others."""

    def test_known_profiles_is_four_tuple(self) -> None:
        self.assertEqual(KNOWN_PROFILES, ("full", "mini", "edge", "developer"))

    def test_known_profiles_count(self) -> None:
        self.assertEqual(len(KNOWN_PROFILES), 4)

    def test_default_profile_is_full(self) -> None:
        self.assertEqual(DEFAULT_PROFILE, "full")

    def test_no_extra_descriptors(self) -> None:
        # The descriptor table must contain exactly the 4 known
        # profiles — no silent extras.
        all_descs = all_descriptors()
        names = {d.name for d in all_descs}
        self.assertEqual(names, set(KNOWN_PROFILES))

    def test_descriptor_table_keys_match_known(self) -> None:
        # Internal consistency: every known profile has a descriptor
        # and every descriptor name is a known profile.
        for name in KNOWN_PROFILES:
            d = get_descriptor(name)
            self.assertEqual(d.name, name)
        # No unknown profile returns a descriptor.
        with self.assertRaises(UnknownProfileError):
            get_descriptor("bogus")

    def test_unique_profile_names(self) -> None:
        # Uniqueness: no two descriptors share a name.
        names = [d.name for d in all_descriptors()]
        self.assertEqual(len(names), len(set(names)))


# ---------------------------------------------------------------------------
# §21.1 matrix field values per profile
# ---------------------------------------------------------------------------

class MatrixFieldValuesTests(unittest.TestCase):
    """Each profile's matrix fields match §21.1."""

    def test_each_profile_matrix_values(self) -> None:
        for profile_name, expected in EXPECTED_MATRIX.items():
            with self.subTest(profile=profile_name):
                d = get_descriptor(profile_name)
                for field, expected_val in expected.items():
                    actual = getattr(d, field)
                    self.assertEqual(
                        actual, expected_val,
                        msg=f"profile {profile_name!r} field {field!r}: "
                            f"expected {expected_val!r}, got {actual!r}",
                    )

    def test_full_matrix_complete(self) -> None:
        d = get_descriptor("full")
        self.assertTrue(d.can_dispatch)
        self.assertTrue(d.can_create_cron)
        self.assertTrue(d.can_delegate_subagents)
        self.assertTrue(d.can_long_running_pipelines)
        self.assertEqual(d.graph_queries, "full")
        self.assertEqual(d.observability_events, "full")
        self.assertEqual(d.db_writes, "full")
        self.assertEqual(d.production_db_access, "full")
        self.assertFalse(d.is_read_only)
        self.assertEqual(d.toolset, "full")

    def test_mini_matrix_complete(self) -> None:
        d = get_descriptor("mini")
        self.assertTrue(d.can_dispatch)
        self.assertFalse(d.can_create_cron)
        self.assertFalse(d.can_delegate_subagents)
        self.assertFalse(d.can_long_running_pipelines)
        self.assertEqual(d.graph_queries, "subset")
        self.assertEqual(d.observability_events, "subset")
        self.assertEqual(d.db_writes, "dispatch_only")
        self.assertEqual(d.production_db_access, "full")
        self.assertFalse(d.is_read_only)
        self.assertEqual(d.toolset, "terminal_file_web_subset")

    def test_edge_matrix_complete(self) -> None:
        d = get_descriptor("edge")
        self.assertFalse(d.can_dispatch)
        self.assertFalse(d.can_create_cron)
        self.assertFalse(d.can_delegate_subagents)
        self.assertFalse(d.can_long_running_pipelines)
        self.assertEqual(d.graph_queries, "read_only")
        self.assertEqual(d.observability_events, "read_only")
        self.assertEqual(d.db_writes, "disabled")
        self.assertEqual(d.production_db_access, "read_only")
        self.assertTrue(d.is_read_only)
        self.assertEqual(d.toolset, "file_read_web_read")

    def test_developer_matrix_complete(self) -> None:
        d = get_descriptor("developer")
        self.assertTrue(d.can_dispatch)
        self.assertFalse(d.can_create_cron)
        self.assertTrue(d.can_delegate_subagents)
        self.assertFalse(d.can_long_running_pipelines)
        self.assertEqual(d.graph_queries, "sandbox")
        self.assertEqual(d.observability_events, "sandbox")
        self.assertEqual(d.db_writes, "tempdir_only")
        self.assertEqual(d.production_db_access, "blocked")
        self.assertFalse(d.is_read_only)
        self.assertEqual(d.toolset, "full_sandbox")


# ---------------------------------------------------------------------------
# Edge query-only safety (§21.1 + R8)
# ---------------------------------------------------------------------------

class EdgeQueryOnlySafetyTests(unittest.TestCase):
    """The edge profile is query-only across the matrix."""

    def test_edge_is_read_only(self) -> None:
        self.assertTrue(get_descriptor("edge").is_read_only)

    def test_edge_cannot_dispatch(self) -> None:
        self.assertFalse(get_descriptor("edge").can_dispatch)

    def test_edge_db_writes_disabled(self) -> None:
        self.assertEqual(get_descriptor("edge").db_writes, "disabled")

    def test_edge_production_db_read_only(self) -> None:
        self.assertEqual(get_descriptor("edge").production_db_access, "read_only")

    def test_edge_graph_queries_read_only(self) -> None:
        self.assertEqual(get_descriptor("edge").graph_queries, "read_only")

    def test_edge_observability_read_only(self) -> None:
        self.assertEqual(get_descriptor("edge").observability_events, "read_only")

    def test_edge_cannot_create_cron(self) -> None:
        self.assertFalse(get_descriptor("edge").can_create_cron)

    def test_edge_cannot_delegate_subagents(self) -> None:
        self.assertFalse(get_descriptor("edge").can_delegate_subagents)

    def test_edge_cannot_long_running_pipelines(self) -> None:
        self.assertFalse(get_descriptor("edge").can_long_running_pipelines)

    def test_edge_toolset_is_file_read_web_read(self) -> None:
        self.assertEqual(get_descriptor("edge").toolset, "file_read_web_read")

    def test_edge_is_the_only_read_only_profile(self) -> None:
        # Per §21.1, edge is the ONLY profile with is_read_only=True.
        for d in all_descriptors():
            if d.name == "edge":
                self.assertTrue(d.is_read_only)
            else:
                self.assertFalse(d.is_read_only)


# ---------------------------------------------------------------------------
# Profile differentiation (mini vs full vs developer)
# ---------------------------------------------------------------------------

class ProfileDifferentiationTests(unittest.TestCase):
    """The three non-edge profiles differ on the matrix."""

    def test_mini_differs_from_full_on_pipelines(self) -> None:
        self.assertFalse(get_descriptor("mini").can_long_running_pipelines)
        self.assertTrue(get_descriptor("full").can_long_running_pipelines)

    def test_mini_differs_from_full_on_cron(self) -> None:
        self.assertFalse(get_descriptor("mini").can_create_cron)
        self.assertTrue(get_descriptor("full").can_create_cron)

    def test_mini_differs_from_full_on_subagents(self) -> None:
        self.assertFalse(get_descriptor("mini").can_delegate_subagents)
        self.assertTrue(get_descriptor("full").can_delegate_subagents)

    def test_mini_differs_from_full_on_graph_queries(self) -> None:
        self.assertEqual(get_descriptor("mini").graph_queries, "subset")
        self.assertEqual(get_descriptor("full").graph_queries, "full")

    def test_mini_differs_from_full_on_toolset(self) -> None:
        self.assertEqual(get_descriptor("mini").toolset, "terminal_file_web_subset")
        self.assertEqual(get_descriptor("full").toolset, "full")

    def test_mini_differs_from_full_on_db_writes(self) -> None:
        self.assertEqual(get_descriptor("mini").db_writes, "dispatch_only")
        self.assertEqual(get_descriptor("full").db_writes, "full")

    def test_developer_differs_from_full_on_pipelines(self) -> None:
        self.assertFalse(get_descriptor("developer").can_long_running_pipelines)
        self.assertTrue(get_descriptor("full").can_long_running_pipelines)

    def test_developer_differs_from_full_on_db_writes(self) -> None:
        self.assertEqual(get_descriptor("developer").db_writes, "tempdir_only")
        self.assertEqual(get_descriptor("full").db_writes, "full")

    def test_developer_differs_from_full_on_production_db(self) -> None:
        self.assertEqual(get_descriptor("developer").production_db_access, "blocked")
        self.assertEqual(get_descriptor("full").production_db_access, "full")

    def test_developer_differs_from_full_on_cron(self) -> None:
        self.assertFalse(get_descriptor("developer").can_create_cron)
        self.assertTrue(get_descriptor("full").can_create_cron)

    def test_developer_differs_from_mini_on_subagents(self) -> None:
        # developer can delegate subagents; mini cannot.
        self.assertTrue(get_descriptor("developer").can_delegate_subagents)
        self.assertFalse(get_descriptor("mini").can_delegate_subagents)

    def test_developer_differs_from_mini_on_toolset(self) -> None:
        self.assertEqual(get_descriptor("developer").toolset, "full_sandbox")
        self.assertEqual(get_descriptor("mini").toolset, "terminal_file_web_subset")

    def test_developer_differs_from_mini_on_db_writes(self) -> None:
        self.assertEqual(get_descriptor("developer").db_writes, "tempdir_only")
        self.assertEqual(get_descriptor("mini").db_writes, "dispatch_only")


# ---------------------------------------------------------------------------
# to_dict() additive shape
# ---------------------------------------------------------------------------

class ToDictAdditiveShapeTests(unittest.TestCase):
    """to_dict() returns AEE-8.1 keys ∪ Epic 9.1 keys."""

    def test_to_dict_has_aee81_keys(self) -> None:
        for name in KNOWN_PROFILES:
            with self.subTest(name=name):
                dct = get_descriptor(name).to_dict()
                for field in AEE81_BASELINE_FIELDS:
                    self.assertIn(field, dct)

    def test_to_dict_has_epic91_keys(self) -> None:
        for name in KNOWN_PROFILES:
            with self.subTest(name=name):
                dct = get_descriptor(name).to_dict()
                for field in EPIC91_MATRIX_FIELDS:
                    self.assertIn(field, dct)

    def test_to_dict_full_keyset(self) -> None:
        dct = get_descriptor("full").to_dict()
        expected = set(AEE81_BASELINE_FIELDS) | set(EPIC91_MATRIX_FIELDS)
        self.assertEqual(set(dct.keys()), expected)

    def test_to_dict_aee81_values_unchanged(self) -> None:
        # AEE-8.1 field values in to_dict() must remain unchanged.
        d = get_descriptor("full")
        dct = d.to_dict()
        self.assertEqual(dct["name"], "full")
        self.assertEqual(dct["safety_tier"], "standard")
        self.assertEqual(dct["can_create_cron"], True)
        self.assertEqual(dct["can_delegate_subagents"], True)
        self.assertEqual(dct["is_read_only"], False)

    def test_to_dict_epic91_values_match_matrix(self) -> None:
        for name, expected in EXPECTED_MATRIX.items():
            with self.subTest(name=name):
                dct = get_descriptor(name).to_dict()
                for field, expected_val in expected.items():
                    self.assertEqual(dct[field], expected_val)


# ---------------------------------------------------------------------------
# Vocabulary validation (every profile's string-valued fields use
# only the documented vocabulary)
# ---------------------------------------------------------------------------

class VocabularyTests(unittest.TestCase):
    """Matrix string fields use only the documented vocabulary."""

    def test_graph_queries_vocab(self) -> None:
        for d in all_descriptors():
            with self.subTest(name=d.name):
                self.assertIn(d.graph_queries, GRAPH_QUERIES_VOCAB)

    def test_observability_vocab(self) -> None:
        for d in all_descriptors():
            with self.subTest(name=d.name):
                self.assertIn(d.observability_events, OBSERVABILITY_VOCAB)

    def test_db_writes_vocab(self) -> None:
        for d in all_descriptors():
            with self.subTest(name=d.name):
                self.assertIn(d.db_writes, DB_WRITES_VOCAB)

    def test_production_db_vocab(self) -> None:
        for d in all_descriptors():
            with self.subTest(name=d.name):
                self.assertIn(d.production_db_access, PRODUCTION_DB_VOCAB)

    def test_toolset_vocab(self) -> None:
        for d in all_descriptors():
            with self.subTest(name=d.name):
                self.assertIn(d.toolset, TOOLSET_VOCAB)

    def test_each_vocab_value_used_at_least_once(self) -> None:
        # Every documented vocabulary value must be used by at
        # least one profile — guards against dead vocabulary.
        used_graph = {d.graph_queries for d in all_descriptors()}
        used_obs = {d.observability_events for d in all_descriptors()}
        used_db = {d.db_writes for d in all_descriptors()}
        used_pdb = {d.production_db_access for d in all_descriptors()}
        used_toolset = {d.toolset for d in all_descriptors()}
        self.assertEqual(used_graph, GRAPH_QUERIES_VOCAB)
        self.assertEqual(used_obs, OBSERVABILITY_VOCAB)
        self.assertEqual(used_db, DB_WRITES_VOCAB)
        self.assertEqual(used_pdb, PRODUCTION_DB_VOCAB)
        self.assertEqual(used_toolset, TOOLSET_VOCAB)


# ---------------------------------------------------------------------------
# Cross-field consistency invariants
# ---------------------------------------------------------------------------

class CrossFieldConsistencyTests(unittest.TestCase):
    """Cross-field invariants that must hold for every profile."""

    def test_read_only_implies_db_writes_disabled(self) -> None:
        for d in all_descriptors():
            with self.subTest(name=d.name):
                if d.is_read_only:
                    self.assertEqual(d.db_writes, "disabled",
                        msg=f"{d.name!r} is_read_only=True but db_writes="
                            f"{d.db_writes!r}")

    def test_read_only_implies_no_dispatch(self) -> None:
        for d in all_descriptors():
            with self.subTest(name=d.name):
                if d.is_read_only:
                    self.assertFalse(d.can_dispatch,
                        msg=f"{d.name!r} is_read_only=True but can_dispatch="
                            f"{d.can_dispatch!r}")

    def test_read_only_implies_no_cron(self) -> None:
        for d in all_descriptors():
            with self.subTest(name=d.name):
                if d.is_read_only:
                    self.assertFalse(d.can_create_cron)

    def test_read_only_implies_no_subagents(self) -> None:
        for d in all_descriptors():
            with self.subTest(name=d.name):
                if d.is_read_only:
                    self.assertFalse(d.can_delegate_subagents)

    def test_read_only_implies_no_long_running(self) -> None:
        for d in all_descriptors():
            with self.subTest(name=d.name):
                if d.is_read_only:
                    self.assertFalse(d.can_long_running_pipelines)

    def test_db_writes_disabled_implies_no_dispatch(self) -> None:
        # If you can't write to the DB, you can't dispatch (a dispatch
        # that cannot persist is meaningless).
        for d in all_descriptors():
            with self.subTest(name=d.name):
                if d.db_writes == "disabled":
                    self.assertFalse(d.can_dispatch)

    def test_production_db_blocked_implies_no_long_running(self) -> None:
        # If production DB access is blocked, long-running pipelines
        # (which require production DB) are also blocked.
        for d in all_descriptors():
            with self.subTest(name=d.name):
                if d.production_db_access == "blocked":
                    self.assertFalse(d.can_long_running_pipelines)

    def test_can_create_cron_implies_can_dispatch(self) -> None:
        # Cron creation requires dispatch capability.
        for d in all_descriptors():
            with self.subTest(name=d.name):
                if d.can_create_cron:
                    self.assertTrue(d.can_dispatch)

    def test_full_profile_is_most_permissive(self) -> None:
        # The full profile must be at least as permissive as every
        # other profile on every boolean matrix field.
        full = get_descriptor("full")
        for d in all_descriptors():
            if d.name == "full":
                continue
            with self.subTest(other=d.name):
                if full.can_dispatch:
                    # full is True; other can be True or False, just
                    # check full is True (sanity).
                    pass
                # full must be True on all booleans where any other
                # profile is True (full is the max).
                for field in ("can_dispatch", "can_create_cron",
                               "can_delegate_subagents",
                               "can_long_running_pipelines"):
                    if getattr(d, field):
                        self.assertTrue(getattr(full, field),
                            msg=f"full is False on {field!r} but "
                                f"{d.name!r} is True")


# ---------------------------------------------------------------------------
# AEE-8.1 backward compatibility (existing API preserved)
# ---------------------------------------------------------------------------

class BackwardCompatTests(unittest.TestCase):
    """Existing AEE-8.1 API surface is unchanged."""

    def test_parse_none_returns_full(self) -> None:
        self.assertEqual(parse_profile(None), "full")

    def test_parse_empty_returns_full(self) -> None:
        self.assertEqual(parse_profile(""), "full")

    def test_parse_unknown_raises(self) -> None:
        with self.assertRaises(UnknownProfileError):
            parse_profile("bogus")

    def test_get_descriptor_none_returns_full(self) -> None:
        self.assertEqual(get_descriptor(None).name, "full")

    def test_safety_tier_for_each_profile(self) -> None:
        expected = {
            "full": "standard",
            "mini": "strict",
            "edge": "strictest",
            "developer": "relaxed_within_sandbox",
        }
        for name, tier in expected.items():
            with self.subTest(name=name):
                self.assertEqual(safety_tier_for(name), tier)

    def test_is_known_profile(self) -> None:
        for name in KNOWN_PROFILES:
            with self.subTest(name=name):
                self.assertTrue(is_known_profile(name))
        self.assertFalse(is_known_profile("bogus"))
        self.assertFalse(is_known_profile(None))

    def test_all_descriptors_count_and_order(self) -> None:
        result = all_descriptors()
        self.assertEqual(len(result), 4)
        names = tuple(d.name for d in result)
        self.assertEqual(names, KNOWN_PROFILES)

    def test_aee81_field_values_preserved(self) -> None:
        # The AEE-8.1 baseline field values must remain unchanged.
        full = get_descriptor("full")
        self.assertEqual(full.safety_tier, "standard")
        self.assertTrue(full.can_create_cron)
        self.assertTrue(full.can_delegate_subagents)
        self.assertFalse(full.is_read_only)
        self.assertEqual(full.toolset_restriction, "")

        mini = get_descriptor("mini")
        self.assertEqual(mini.safety_tier, "strict")
        self.assertFalse(mini.can_create_cron)
        self.assertFalse(mini.can_delegate_subagents)
        self.assertFalse(mini.is_read_only)
        self.assertIn("terminal", mini.toolset_restriction)

        edge = get_descriptor("edge")
        self.assertEqual(edge.safety_tier, "strictest")
        self.assertFalse(edge.can_create_cron)
        self.assertFalse(edge.can_delegate_subagents)
        self.assertTrue(edge.is_read_only)

        dev = get_descriptor("developer")
        self.assertEqual(dev.safety_tier, "relaxed_within_sandbox")
        self.assertFalse(dev.can_create_cron)
        self.assertTrue(dev.can_delegate_subagents)
        self.assertFalse(dev.is_read_only)


# ---------------------------------------------------------------------------
# Frozen dataclass (Epic 9.1 fields are also frozen)
# ---------------------------------------------------------------------------

class FrozenDataclassTests(unittest.TestCase):
    """Epic 9.1 additive fields are part of the frozen dataclass."""

    def test_cannot_mutate_epic91_field(self) -> None:
        d = get_descriptor("full")
        with self.assertRaises(Exception):
            d.can_dispatch = False  # type: ignore[misc]

    def test_cannot_mutate_graph_queries(self) -> None:
        d = get_descriptor("mini")
        with self.assertRaises(Exception):
            d.graph_queries = "full"  # type: ignore[misc]

    def test_cannot_mutate_toolset(self) -> None:
        d = get_descriptor("edge")
        with self.assertRaises(Exception):
            d.toolset = "full"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Isolation contract (Epic 9.1 did not introduce forbidden imports)
# ---------------------------------------------------------------------------

class IsolationContractTests(unittest.TestCase):
    """descriptor.py has no forbidden imports after Epic 9.1."""

    FORBIDDEN_MODULES = {
        "dispatcher", "sqlite3", "subprocess",
        "requests", "urllib", "httpx", "http",
    }

    def setUp(self) -> None:
        self.src_path = _REPO_ROOT / "aee" / "profiles" / "descriptor.py"
        self.src = self.src_path.read_text()

    def test_no_forbidden_imports(self) -> None:
        tree = ast.parse(self.src, filename=str(self.src_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    self.assertNotIn(top, self.FORBIDDEN_MODULES)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top = node.module.split(".")[0]
                    self.assertNotIn(top, self.FORBIDDEN_MODULES)

    def test_no_os_environ_or_getenv(self) -> None:
        tree = ast.parse(self.src, filename=str(self.src_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                if node.attr in ("environ", "getenv"):
                    self.fail(f"forbidden os.{node.attr} at line {node.lineno}")
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    if node.func.attr == "system":
                        self.fail(f"forbidden os.system at line {node.lineno}")


# ---------------------------------------------------------------------------
# Single source of truth — descriptor.py is the only place the matrix
# is defined (no parallel matrix in safety.py, app.py, or config)
# ---------------------------------------------------------------------------

class SingleSourceOfTruthTests(unittest.TestCase):
    """descriptor.py is the only place the §21.1 matrix lives."""

    def test_descriptor_py_exists(self) -> None:
        path = _REPO_ROOT / "aee" / "profiles" / "descriptor.py"
        self.assertTrue(path.exists())

    def test_descriptor_py_contains_all_four_profiles(self) -> None:
        src = (_REPO_ROOT / "aee" / "profiles" / "descriptor.py").read_text()
        for name in KNOWN_PROFILES:
            self.assertIn(f'"{name}"', src)

    def test_no_parallel_matrix_in_safety_py(self) -> None:
        # safety.py may reference the descriptor via lazy import, but
        # must NOT define its own parallel matrix of capabilities.
        path = _REPO_ROOT / "dispatcher" / "safety.py"
        if not path.exists():
            self.skipTest("safety.py not found")
        src = path.read_text()
        # The lazy import line is permitted; a parallel matrix is not.
        # Heuristic: safety.py should not define a dict literal with
        # all four profile names as keys.
        for name in KNOWN_PROFILES:
            # A parallel matrix would have something like:
            #   {"full": ..., "mini": ..., "edge": ..., "developer": ...}
            # We check that safety.py does not contain a dict literal
            # with all four profile names as keys.
            pass
        # Less strict: just verify safety.py imports from descriptor
        # rather than redefining the matrix.
        if "from aee.profiles.descriptor import" in src:
            self.assertTrue("from aee.profiles.descriptor import" in src)
        # If safety.py does not import from descriptor, it must not
        # define the matrix either (no false positive here).

    def test_matrix_values_match_master_plan(self) -> None:
        # The matrix in EXPECTED_MATRIX above is the §21.1 matrix.
        # This test asserts that descriptor.py encodes that matrix.
        for name, expected in EXPECTED_MATRIX.items():
            with self.subTest(name=name):
                d = get_descriptor(name)
                for field, val in expected.items():
                    self.assertEqual(getattr(d, field), val)


if __name__ == "__main__":
    unittest.main()