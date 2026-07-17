"""AEE-8.1 — Targeted tests for ``aee.profiles.descriptor``.

Coverage surface:

* Constants: ``KNOWN_PROFILES`` membership, ``DEFAULT_PROFILE`` value.
* :func:`is_known_profile`: known names True, unknown/None/empty/
  wrong-type/wrong-case False.
* :func:`parse_profile`: None→default, empty→default, known→self,
  unknown→raises, non-string→raises.
* :func:`get_descriptor`: default descriptor, each known profile,
  unknown→raises, frozen dataclass immutability.
* :func:`safety_tier_for`: each profile returns its tier string.
* :func:`all_descriptors`: count, order, frozenness.
* :class:`ProfileDescriptor`: ``to_dict()`` shape, field values
  per Decision MINI §3.
* :class:`UnknownProfileError`: subclass of ValueError, carries
  ``profile`` attribute.
* Isolation contract: ``aee.profiles.descriptor`` must not import
  ``dispatcher``, ``sqlite3``, ``subprocess``, ``os.environ``,
  ``os.getenv``, ``requests``, ``urllib``, ``httpx``,
  ``http.client``.
* Backward compatibility: ``parse_profile(None)`` returns
  ``DEFAULT_PROFILE``; ``get_descriptor(None)`` returns the
  ``full`` descriptor; ``safety_tier_for(None)`` returns
  ``"standard"``.
* Schema integration: ``CreateRunRequest`` in ``app.py`` accepts
  the new ``profile`` field, defaults to None, rejects unknown
  values, and preserves existing behavior when the field is
  absent.

Run: ``PYTHONPATH=. python3 -m unittest aee.tests.test_aee81_profile_descriptor -v``
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

# Ensure the repo root is on sys.path so `import app` and
# `import aee.profiles` both work.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from aee.profiles.descriptor import (
    KNOWN_PROFILES,
    DEFAULT_PROFILE,
    ProfileDescriptor,
    UnknownProfileError,
    InvalidDescriptorError,
    is_known_profile,
    parse_profile,
    get_descriptor,
    safety_tier_for,
    all_descriptors,
)
from aee.profiles import (
    KNOWN_PROFILES as _PKG_KNOWN,
    DEFAULT_PROFILE as _PKG_DEFAULT,
    ProfileDescriptor as _PKG_PD,
    UnknownProfileError as _PKG_UPE,
    is_known_profile as _PKG_ikp,
    parse_profile as _PKG_pp,
    get_descriptor as _PKG_gd,
    safety_tier_for as _PKG_stf,
    all_descriptors as _PKG_ad,
)


# ---------------------------------------------------------------------------
# Constants tests
# ---------------------------------------------------------------------------

class ConstantsTests(unittest.TestCase):
    def test_known_profiles_is_tuple(self) -> None:
        self.assertIsInstance(KNOWN_PROFILES, tuple)

    def test_known_profiles_contains_four(self) -> None:
        self.assertEqual(len(KNOWN_PROFILES), 4)

    def test_known_profiles_values(self) -> None:
        self.assertEqual(set(KNOWN_PROFILES), {"full", "mini", "edge", "developer"})

    def test_known_profiles_order(self) -> None:
        self.assertEqual(KNOWN_PROFILES, ("full", "mini", "edge", "developer"))

    def test_default_profile_is_full(self) -> None:
        self.assertEqual(DEFAULT_PROFILE, "full")

    def test_default_in_known(self) -> None:
        self.assertIn(DEFAULT_PROFILE, KNOWN_PROFILES)


# ---------------------------------------------------------------------------
# is_known_profile tests
# ---------------------------------------------------------------------------

class IsKnownProfileTests(unittest.TestCase):
    def test_known_names(self) -> None:
        for name in KNOWN_PROFILES:
            with self.subTest(name=name):
                self.assertTrue(is_known_profile(name))

    def test_none(self) -> None:
        self.assertFalse(is_known_profile(None))

    def test_empty_string(self) -> None:
        self.assertFalse(is_known_profile(""))

    def test_unknown_string(self) -> None:
        self.assertFalse(is_known_profile("bogus"))

    def test_wrong_case(self) -> None:
        self.assertFalse(is_known_profile("Full"))
        self.assertFalse(is_known_profile("MINI"))

    def test_non_string(self) -> None:
        self.assertFalse(is_known_profile(42))
        self.assertFalse(is_known_profile(["full"]))
        self.assertFalse(is_known_profile(True))


# ---------------------------------------------------------------------------
# parse_profile tests
# ---------------------------------------------------------------------------

class ParseProfileTests(unittest.TestCase):
    def test_none_returns_default(self) -> None:
        self.assertEqual(parse_profile(None), DEFAULT_PROFILE)

    def test_empty_string_returns_default(self) -> None:
        self.assertEqual(parse_profile(""), DEFAULT_PROFILE)

    def test_whitespace_returns_default(self) -> None:
        self.assertEqual(parse_profile("   "), DEFAULT_PROFILE)

    def test_known_names_return_self(self) -> None:
        for name in KNOWN_PROFILES:
            with self.subTest(name=name):
                self.assertEqual(parse_profile(name), name)

    def test_strips_whitespace(self) -> None:
        self.assertEqual(parse_profile("  full  "), "full")
        self.assertEqual(parse_profile("\tmini\n"), "mini")

    def test_unknown_raises(self) -> None:
        with self.assertRaises(UnknownProfileError) as ctx:
            parse_profile("bogus")
        self.assertEqual(ctx.exception.profile, "bogus")

    def test_non_string_raises(self) -> None:
        with self.assertRaises(UnknownProfileError):
            parse_profile(42)
        with self.assertRaises(UnknownProfileError):
            parse_profile(["full"])

    def test_unknown_profile_error_is_value_error(self) -> None:
        self.assertTrue(issubclass(UnknownProfileError, ValueError))


# ---------------------------------------------------------------------------
# get_descriptor tests
# ---------------------------------------------------------------------------

class GetDescriptorTests(unittest.TestCase):
    def test_none_returns_full_descriptor(self) -> None:
        d = get_descriptor(None)
        self.assertEqual(d.name, "full")

    def test_each_known_profile(self) -> None:
        for name in KNOWN_PROFILES:
            with self.subTest(name=name):
                d = get_descriptor(name)
                self.assertEqual(d.name, name)

    def test_unknown_raises(self) -> None:
        with self.assertRaises(UnknownProfileError):
            get_descriptor("bogus")

    def test_empty_returns_full(self) -> None:
        d = get_descriptor("")
        self.assertEqual(d.name, "full")

    def test_descriptor_is_frozen(self) -> None:
        d = get_descriptor("full")
        with self.assertRaises(Exception):
            d.name = "mini"  # type: ignore[misc]

    def test_descriptor_is_profile_descriptor(self) -> None:
        d = get_descriptor("full")
        self.assertIsInstance(d, ProfileDescriptor)


# ---------------------------------------------------------------------------
# ProfileDescriptor field value tests (per Decision MINI §3)
# ---------------------------------------------------------------------------

class DescriptorFieldValuesTests(unittest.TestCase):
    def test_full_fields(self) -> None:
        d = get_descriptor("full")
        self.assertEqual(d.name, "full")
        self.assertEqual(d.safety_tier, "standard")
        self.assertTrue(d.can_create_cron)
        self.assertTrue(d.can_delegate_subagents)
        self.assertFalse(d.is_read_only)
        self.assertEqual(d.toolset_restriction, "")

    def test_mini_fields(self) -> None:
        d = get_descriptor("mini")
        self.assertEqual(d.name, "mini")
        self.assertEqual(d.safety_tier, "strict")
        self.assertFalse(d.can_create_cron)
        self.assertFalse(d.can_delegate_subagents)
        self.assertFalse(d.is_read_only)
        self.assertIn("terminal", d.toolset_restriction)

    def test_edge_fields(self) -> None:
        d = get_descriptor("edge")
        self.assertEqual(d.name, "edge")
        self.assertEqual(d.safety_tier, "strictest")
        self.assertFalse(d.can_create_cron)
        self.assertFalse(d.can_delegate_subagents)
        self.assertTrue(d.is_read_only)

    def test_developer_fields(self) -> None:
        d = get_descriptor("developer")
        self.assertEqual(d.name, "developer")
        self.assertEqual(d.safety_tier, "relaxed_within_sandbox")
        self.assertFalse(d.can_create_cron)
        self.assertTrue(d.can_delegate_subagents)
        self.assertFalse(d.is_read_only)

    def test_to_dict_has_all_fields(self) -> None:
        d = get_descriptor("full")
        dct = d.to_dict()
        # AEE-8.1 baseline keys (always present, unchanged).
        aee81_keys = {
            "name", "purpose", "audience", "runtime_footprint",
            "safety_tier", "toolset_restriction",
            "can_create_cron", "can_delegate_subagents", "is_read_only",
        }
        # Epic 9.1 §21.1 additive matrix keys.
        epic91_keys = {
            "can_dispatch", "can_long_running_pipelines",
            "graph_queries", "observability_events",
            "db_writes", "production_db_access", "toolset",
        }
        # Contract supersession (Epic 9.1): to_dict() now returns
        # AEE-8.1 keys ∪ Epic 9.1 keys. Existing callers that only
        # read AEE-8.1 keys are unaffected (additive).
        self.assertTrue(aee81_keys.issubset(set(dct.keys())))
        self.assertEqual(set(dct.keys()), aee81_keys | epic91_keys)

    def test_to_dict_values_match(self) -> None:
        d = get_descriptor("mini")
        dct = d.to_dict()
        self.assertEqual(dct["name"], "mini")
        self.assertEqual(dct["safety_tier"], "strict")
        self.assertEqual(dct["can_create_cron"], False)
        self.assertEqual(dct["is_read_only"], False)


# ---------------------------------------------------------------------------
# safety_tier_for tests
# ---------------------------------------------------------------------------

class SafetyTierForTests(unittest.TestCase):
    def test_none_returns_standard(self) -> None:
        self.assertEqual(safety_tier_for(None), "standard")

    def test_each_profile(self) -> None:
        expected = {
            "full": "standard",
            "mini": "strict",
            "edge": "strictest",
            "developer": "relaxed_within_sandbox",
        }
        for name, tier in expected.items():
            with self.subTest(name=name):
                self.assertEqual(safety_tier_for(name), tier)

    def test_unknown_raises(self) -> None:
        with self.assertRaises(UnknownProfileError):
            safety_tier_for("bogus")


# ---------------------------------------------------------------------------
# all_descriptors tests
# ---------------------------------------------------------------------------

class AllDescriptorsTests(unittest.TestCase):
    def test_returns_tuple(self) -> None:
        result = all_descriptors()
        self.assertIsInstance(result, tuple)

    def test_count_is_four(self) -> None:
        self.assertEqual(len(all_descriptors()), 4)

    def test_order_matches_known(self) -> None:
        result = all_descriptors()
        names = tuple(d.name for d in result)
        self.assertEqual(names, KNOWN_PROFILES)

    def test_each_is_profile_descriptor(self) -> None:
        for d in all_descriptors():
            self.assertIsInstance(d, ProfileDescriptor)

    def test_tuple_is_snapshot(self) -> None:
        # The returned tuple is a snapshot; it should not be the
        # same object as the internal table's values view.
        r1 = all_descriptors()
        r2 = all_descriptors()
        self.assertEqual(r1, r2)
        # But the tuple objects themselves are equal (content), not
        # necessarily identical — we just check they're both tuples
        # with the same content.


# ---------------------------------------------------------------------------
# UnknownProfileError tests
# ---------------------------------------------------------------------------

class UnknownProfileErrorTests(unittest.TestCase):
    def test_is_value_error(self) -> None:
        self.assertTrue(issubclass(UnknownProfileError, ValueError))

    def test_carries_profile_attr(self) -> None:
        try:
            raise UnknownProfileError("bogus")
        except UnknownProfileError as e:
            self.assertEqual(e.profile, "bogus")

    def test_message_contains_profile(self) -> None:
        try:
            raise UnknownProfileError("bogus")
        except UnknownProfileError as e:
            self.assertIn("bogus", str(e))
            self.assertIn("full", str(e))  # lists known profiles


# ---------------------------------------------------------------------------
# InvalidDescriptorError tests
# ---------------------------------------------------------------------------

class InvalidDescriptorErrorTests(unittest.TestCase):
    def test_is_value_error(self) -> None:
        self.assertTrue(issubclass(InvalidDescriptorError, ValueError))


# ---------------------------------------------------------------------------
# Package re-export tests
# ---------------------------------------------------------------------------

class PackageReExportTests(unittest.TestCase):
    def test_known_profiles_re_exported(self) -> None:
        self.assertIs(_PKG_KNOWN, KNOWN_PROFILES)

    def test_default_profile_re_exported(self) -> None:
        self.assertIs(_PKG_DEFAULT, DEFAULT_PROFILE)

    def test_profile_descriptor_re_exported(self) -> None:
        self.assertIs(_PKG_PD, ProfileDescriptor)

    def test_unknown_profile_error_re_exported(self) -> None:
        self.assertIs(_PKG_UPE, UnknownProfileError)

    def test_is_known_profile_re_exported(self) -> None:
        self.assertTrue(_PKG_ikp("full"))
        self.assertFalse(_PKG_ikp("bogus"))

    def test_parse_profile_re_exported(self) -> None:
        self.assertEqual(_PKG_pp(None), "full")

    def test_get_descriptor_re_exported(self) -> None:
        self.assertEqual(_PKG_gd("mini").name, "mini")

    def test_safety_tier_for_re_exported(self) -> None:
        self.assertEqual(_PKG_stf("edge"), "strictest")

    def test_all_descriptors_re_exported(self) -> None:
        self.assertEqual(len(_PKG_ad()), 4)


# ---------------------------------------------------------------------------
# Isolation contract tests (AST-based)
# ---------------------------------------------------------------------------

class IsolationContractTests(unittest.TestCase):
    """Verify ``aee.profiles.descriptor`` has no forbidden imports."""

    FORBIDDEN_MODULES = {
        "dispatcher",
        "sqlite3",
        "subprocess",
        "requests",
        "urllib",
        "httpx",
        "http",
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
                    self.assertNotIn(
                        top, self.FORBIDDEN_MODULES,
                        msg=f"forbidden import: {alias.name} at line {node.lineno}",
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top = node.module.split(".")[0]
                    self.assertNotIn(
                        top, self.FORBIDDEN_MODULES,
                        msg=f"forbidden import: {node.module} at line {node.lineno}",
                    )

    def test_no_os_environ_or_getenv(self) -> None:
        tree = ast.parse(self.src, filename=str(self.src_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                if node.attr == "environ" or node.attr == "getenv":
                    self.fail(
                        f"forbidden os.{node.attr} access at line {node.lineno}"
                    )
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    if node.func.attr == "system":
                        self.fail(
                            f"forbidden os.system call at line {node.lineno}"
                        )

    def test_no_subprocess_calls(self) -> None:
        tree = ast.parse(self.src, filename=str(self.src_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    if "subprocess" in ast.dump(node.func):
                        self.fail(
                            f"subprocess call at line {node.lineno}"
                        )


# ---------------------------------------------------------------------------
# Backward compatibility tests
# ---------------------------------------------------------------------------

class BackwardCompatTests(unittest.TestCase):
    """Without a profile, existing behavior is preserved."""

    def test_parse_none_returns_full(self) -> None:
        self.assertEqual(parse_profile(None), "full")

    def test_get_descriptor_none_returns_full(self) -> None:
        self.assertEqual(get_descriptor(None).name, "full")

    def test_safety_tier_none_returns_standard(self) -> None:
        self.assertEqual(safety_tier_for(None), "standard")

    def test_default_descriptor_can_create_cron(self) -> None:
        # full profile can create cron — the default behavior
        d = get_descriptor(None)
        self.assertTrue(d.can_create_cron)


# ---------------------------------------------------------------------------
# Schema integration tests (app.py CreateRunRequest)
# ---------------------------------------------------------------------------

class SchemaIntegrationTests(unittest.TestCase):
    """Test that CreateRunRequest accepts and validates the profile field."""

    def setUp(self) -> None:
        # Import app lazily so test collection doesn't fail if
        # FastAPI/Pydantic aren't installed.
        try:
            import app  # noqa: F401
            self.app = app
        except Exception:
            self.app = None

    def _maybe_skip(self) -> None:
        if self.app is None:
            self.skipTest("app module not importable (missing FastAPI/Pydantic)")

    def test_profile_field_exists(self) -> None:
        self._maybe_skip()
        CreateRunRequest = self.app.CreateRunRequest
        fields = CreateRunRequest.model_fields
        self.assertIn("profile", fields)

    def test_profile_defaults_to_none(self) -> None:
        self._maybe_skip()
        CreateRunRequest = self.app.CreateRunRequest
        req = CreateRunRequest(input="test")
        self.assertIsNone(req.profile)

    def test_profile_accepts_full(self) -> None:
        self._maybe_skip()
        CreateRunRequest = self.app.CreateRunRequest
        req = CreateRunRequest(input="test", profile="full")
        self.assertEqual(req.profile, "full")

    def test_profile_accepts_mini(self) -> None:
        self._maybe_skip()
        CreateRunRequest = self.app.CreateRunRequest
        req = CreateRunRequest(input="test", profile="mini")
        self.assertEqual(req.profile, "mini")

    def test_profile_accepts_edge(self) -> None:
        self._maybe_skip()
        CreateRunRequest = self.app.CreateRunRequest
        req = CreateRunRequest(input="test", profile="edge")
        self.assertEqual(req.profile, "edge")

    def test_profile_accepts_developer(self) -> None:
        self._maybe_skip()
        CreateRunRequest = self.app.CreateRunRequest
        req = CreateRunRequest(input="test", profile="developer")
        self.assertEqual(req.profile, "developer")

    def test_profile_strips_whitespace(self) -> None:
        self._maybe_skip()
        CreateRunRequest = self.app.CreateRunRequest
        req = CreateRunRequest(input="test", profile="  mini  ")
        self.assertEqual(req.profile, "mini")

    def test_profile_empty_string_becomes_none(self) -> None:
        self._maybe_skip()
        CreateRunRequest = self.app.CreateRunRequest
        req = CreateRunRequest(input="test", profile="   ")
        self.assertIsNone(req.profile)

    def test_profile_rejects_unknown(self) -> None:
        self._maybe_skip()
        from pydantic import ValidationError
        CreateRunRequest = self.app.CreateRunRequest
        with self.assertRaises(ValidationError):
            CreateRunRequest(input="test", profile="bogus")

    def test_profile_rejects_wrong_case(self) -> None:
        self._maybe_skip()
        from pydantic import ValidationError
        CreateRunRequest = self.app.CreateRunRequest
        with self.assertRaises(ValidationError):
            CreateRunRequest(input="test", profile="Full")

    def test_existing_fields_unchanged(self) -> None:
        """Existing fields still work when profile is absent."""
        self._maybe_skip()
        CreateRunRequest = self.app.CreateRunRequest
        req = CreateRunRequest(
            input="test",
            mode="coding",
            title="my task",
            type="coding",
        )
        self.assertEqual(req.mode, "coding")
        self.assertEqual(req.title, "my task")
        self.assertEqual(req.type, "coding")
        self.assertIsNone(req.profile)


if __name__ == "__main__":
    unittest.main()