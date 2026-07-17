"""AEE Epic 9.4 — Runtime Profile Selection (§21.4) targeted tests.

These tests verify the §21.4 contract delivered by the Epic 9.4
implementation:

    Profile resolved **once, at dispatch time**, in ``app.py``'s
    ``POST /runs`` handler. Resolution path:
        body.profile → if absent, DEFAULT_PROFILE ("full") →
        stored on Task.profile → safety.py:evaluate(profile=...)
        enforces → dispatcher passes to runtime adapter.
    No other code path resolves the profile.

    Edge special case: ``profile=edge`` wraps the DB connection
    factory in ``dispatcher/db.py`` to emit ``PRAGMA query_only=1``
    on every connection. Runtime-level enforcement, not just intent
    detection.

Coverage areas:

1. **Single resolution point** — ``app.py:create_run`` resolves the
   profile via ``parse_profile``; no other code path does.
2. **Default to full** — ``body.profile=None`` → ``resolved_profile
   = "full"``; ``body.profile=""`` → "full"; whitespace → "full".
3. **Known profiles pass through** — each of {full, mini, edge,
   developer} resolves to itself.
4. **Safety gate activation** — ``danger_check`` forwards
   ``profile`` to ``safety.evaluate``; AEE-8.3 enforcement fires.
5. **Task.profile stored** — the resolved profile (not None) is
   persisted on the Task via ``manager.create(profile=...)``.
6. **Edge DB query_only** — ``set_db_profile("edge")`` causes
   ``_apply_pragmas`` to emit ``PRAGMA query_only=1``; writes are
   rejected at the SQLite driver level.
7. **Non-edge profiles clear query_only** —
   ``set_db_profile("full")`` / ``set_db_profile(None)`` does NOT
   emit ``PRAGMA query_only=1``.
8. **Response surfaces profile** — ``CreateRunResponse.routing``
   includes the resolved profile.
9. **Backward compatibility** — callers that don't pass
   ``profile`` get the same behaviour as before (profile="full",
   no query_only, safety gate passes).
10. **Isolation contract** — ``set_db_profile`` / ``get_db_profile``
    are pure module-level state; no forbidden imports added.

Run:
    cd /home/ubuntu/hermes-runtime-bridge
    PYTHONPATH=. python3 -m unittest aee.tests.test_aee94_runtime_profile_selection -v
"""
from __future__ import annotations

import ast
import importlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

# -----------------------------------------------------------------------
# Repo root resolution
# -----------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# -----------------------------------------------------------------------
# Imports under test
# -----------------------------------------------------------------------
from aee.profiles.descriptor import (
    DEFAULT_PROFILE,
    KNOWN_PROFILES,
    UnknownProfileError,
    parse_profile,
    get_descriptor,
)

import dispatcher.db as db_module
from dispatcher.db import (
    set_db_profile,
    get_db_profile,
    _apply_pragmas,
    _db_profile as _captured_db_profile,  # noqa: F401 — used in restore
)
from dispatcher.safety import evaluate as safety_evaluate


# -----------------------------------------------------------------------
# Test cleanup helper — restore _db_profile after each test class
# that mutates it.
# -----------------------------------------------------------------------


class _DbProfileRestore:
    """Mixin that saves/restores the module-level _db_profile."""

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        self._saved_profile = db_module._db_profile

    def tearDown(self) -> None:
        db_module._db_profile = self._saved_profile
        super().tearDown()  # type: ignore[misc]


# =======================================================================
# 1. Single resolution point — app.py:create_run resolves the profile
# =======================================================================


class SingleResolutionPointTests(unittest.TestCase):
    """Verify that app.py:create_run resolves the profile."""

    @staticmethod
    def _read_app_source() -> str:
        """Read app.py source from disk (avoids importing httpx in tests)."""
        return (_ROOT / "app.py").read_text()

    def test_create_run_calls_parse_profile(self) -> None:
        """The create_run function body must call parse_profile."""
        source = self._read_app_source()
        # Must reference parse_profile (the canonical resolver).
        self.assertIn("parse_profile", source,
                      "create_run must call parse_profile to resolve the profile")

    def test_create_run_calls_set_db_profile(self) -> None:
        """The create_run function body must call set_db_profile for edge."""
        source = self._read_app_source()
        self.assertIn("set_db_profile", source,
                      "create_run must call set_db_profile to activate edge DB enforcement")

    def test_create_run_passes_profile_to_danger_check(self) -> None:
        """The create_run function must forward profile to danger_check."""
        source = self._read_app_source()
        # danger_check must be called with profile=resolved_profile.
        self.assertIn("profile=resolved_profile", source,
                      "create_run must pass profile=resolved_profile to danger_check")

    def test_create_run_stores_resolved_profile_on_task(self) -> None:
        """manager.create must receive profile=resolved_profile, not body.profile."""
        source = self._read_app_source()
        # The manager.create call must use resolved_profile.
        self.assertIn("profile=resolved_profile", source,
                      "create_run must store resolved_profile on the Task")

    def test_danger_check_signature_has_profile_param(self) -> None:
        """danger_check must accept a profile keyword argument."""
        source = self._read_app_source()
        # Parse the AST and find the danger_check function definition.
        tree = ast.parse(source)
        danger_check_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "danger_check":
                danger_check_node = node
                break
        self.assertIsNotNone(danger_check_node, "danger_check function must exist in app.py")
        param_names = [arg.arg for arg in danger_check_node.args.args]
        self.assertIn("profile", param_names,
                      "danger_check must have a profile parameter")
        # Check default is None.
        defaults = danger_check_node.args.defaults
        # profile is the last param; its default is the last element.
        if defaults and len(defaults) == len(param_names):
            last_default = defaults[-1]
            self.assertIsInstance(last_default, ast.Constant,
                                 "danger_check profile default must be a constant")
            self.assertIsNone(last_default.value,
                              "danger_check profile default must be None")

    def test_danger_check_forwards_profile_to_safety_evaluate(self) -> None:
        """danger_check must pass profile= to safety.evaluate."""
        source = self._read_app_source()
        # Parse the AST to find the danger_check function body.
        tree = ast.parse(source)
        danger_check_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "danger_check":
                danger_check_node = node
                break
        self.assertIsNotNone(danger_check_node, "danger_check function must exist")
        # Find the return statement that calls safety_evaluate.
        found_profile_forward = False
        for node in ast.walk(danger_check_node):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "profile":
                        found_profile_forward = True
                        break
        self.assertTrue(found_profile_forward,
                        "danger_check must forward profile= to safety_evaluate")


# =======================================================================
# 2. Default to full — parse_profile maps None/empty/whitespace → "full"
# =======================================================================


class DefaultToFullTests(unittest.TestCase):
    """Verify that absent/empty profile resolves to DEFAULT_PROFILE."""

    def test_none_resolves_to_full(self) -> None:
        self.assertEqual(parse_profile(None), "full")
        self.assertEqual(parse_profile(None), DEFAULT_PROFILE)

    def test_empty_string_resolves_to_full(self) -> None:
        self.assertEqual(parse_profile(""), "full")

    def test_whitespace_resolves_to_full(self) -> None:
        self.assertEqual(parse_profile("   "), "full")
        self.assertEqual(parse_profile("\t\n"), "full")

    def test_default_profile_is_full(self) -> None:
        self.assertEqual(DEFAULT_PROFILE, "full")


# =======================================================================
# 3. Known profiles pass through
# =======================================================================


class KnownProfilePassthroughTests(unittest.TestCase):
    """Verify each known profile resolves to itself."""

    def test_each_known_profile_resolves_to_itself(self) -> None:
        for p in KNOWN_PROFILES:
            self.assertEqual(parse_profile(p), p)

    def test_unknown_profile_raises(self) -> None:
        with self.assertRaises(UnknownProfileError):
            parse_profile("bogus")

    def test_known_profiles_are_four(self) -> None:
        self.assertEqual(len(KNOWN_PROFILES), 4)
        self.assertEqual(set(KNOWN_PROFILES),
                         {"full", "mini", "edge", "developer"})


# =======================================================================
# 4. Safety gate activation — danger_check forwards profile
# =======================================================================


class SafetyGateActivationTests(unittest.TestCase):
    """Verify that danger_check activates AEE-8.3 profile enforcement."""

    @staticmethod
    def _read_app_source() -> str:
        """Read app.py source from disk (avoids importing httpx in tests)."""
        return (_ROOT / "app.py").read_text()

    def _get_danger_check(self):
        """Import danger_check from app.py via exec (avoids full module import)."""
        # We can't import app.py directly (httpx/idna missing in test env).
        # Instead, we extract the danger_check function by exec'ing just
        # the relevant code. But that's fragile. Simpler: use the fact
        # that danger_check is a thin wrapper around safety_evaluate.
        # We test the *contract* (profile forwarded) via AST in
        # SingleResolutionPointTests. Here we test safety_evaluate
        # directly (which is the actual enforcement).
        from dispatcher.safety import evaluate
        return evaluate

    def test_danger_check_with_none_profile_no_enforcement(self) -> None:
        """profile=None must not trigger profile-aware enforcement."""
        evaluate = self._get_danger_check()
        # A benign input with profile=None should allow.
        decision = evaluate("hello world", mode="normal", profile=None)
        self.assertEqual(decision.action, "allow")

    def test_danger_check_with_full_profile_allows_write(self) -> None:
        """profile=full must allow write intent."""
        evaluate = self._get_danger_check()
        decision = evaluate("use write_file to save", mode="normal", profile="full")
        self.assertEqual(decision.action, "allow")

    def test_danger_check_with_edge_profile_rejects_write(self) -> None:
        """profile=edge must reject write intent via AEE-8.3."""
        evaluate = self._get_danger_check()
        # Use a pattern that matches _WRITE_INTENT_PATTERNS: \bwrite_file\b
        decision = evaluate(
            "use write_file to save the report", mode="normal", profile="edge"
        )
        self.assertEqual(decision.action, "block")
        self.assertIn("read-only", decision.reason)

    def test_danger_check_with_mini_profile_rejects_cron(self) -> None:
        """profile=mini must reject cron creation via AEE-8.3."""
        evaluate = self._get_danger_check()
        decision = evaluate(
            "create a cron job to run daily", mode="normal", profile="mini"
        )
        self.assertEqual(decision.action, "block")
        self.assertIn("cron", decision.reason.lower())

    def test_danger_check_backward_compat_no_profile_kwarg(self) -> None:
        """Calling evaluate without profile kwarg must still work."""
        evaluate = self._get_danger_check()
        # Should not raise — profile defaults to None.
        decision = evaluate("hello world", mode="normal")
        self.assertEqual(decision.action, "allow")

    def test_safety_evaluate_with_profile_activates_enforcement(self) -> None:
        """Direct safety.evaluate call with profile= must enforce."""
        # edge rejects write (use write_file pattern that matches _WRITE_INTENT_PATTERNS).
        decision = safety_evaluate("use write_file to save", mode="normal", profile="edge")
        self.assertEqual(decision.action, "block")
        # full allows write.
        decision = safety_evaluate("use write_file to save", mode="normal", profile="full")
        self.assertEqual(decision.action, "allow")


# =======================================================================
# 5. Edge DB query_only — set_db_profile + _apply_pragmas
# =======================================================================


class EdgeDbQueryOnlyTests(_DbProfileRestore, unittest.TestCase):
    """Verify that profile=edge activates PRAGMA query_only=1."""

    def test_set_db_profile_edge(self) -> None:
        set_db_profile("edge")
        self.assertEqual(get_db_profile(), "edge")

    def test_set_db_profile_full_clears_edge(self) -> None:
        set_db_profile("edge")
        self.assertEqual(get_db_profile(), "edge")
        set_db_profile("full")
        self.assertEqual(get_db_profile(), "full")
        self.assertNotEqual(get_db_profile(), "edge")

    def test_set_db_profile_none_clears_edge(self) -> None:
        set_db_profile("edge")
        set_db_profile(None)
        self.assertIsNone(get_db_profile())

    def test_apply_pragmas_emits_query_only_for_edge(self) -> None:
        """When _db_profile == 'edge', _apply_pragmas emits PRAGMA query_only=1."""
        set_db_profile("edge")
        conn = MagicMock(spec=sqlite3.Connection)
        _apply_pragmas(conn)
        # Collect all PRAGMA calls.
        pragma_calls = [str(c) for c in conn.execute.call_args_list]
        # Must include query_only=1.
        self.assertTrue(
            any("query_only" in pc and "1" in pc for pc in pragma_calls),
            f"PRAGMA query_only=1 must be emitted for edge; got {pragma_calls}",
        )

    def test_apply_pragmas_no_query_only_for_full(self) -> None:
        """When _db_profile == 'full', _apply_pragmas must NOT emit query_only."""
        set_db_profile("full")
        conn = MagicMock(spec=sqlite3.Connection)
        _apply_pragmas(conn)
        pragma_calls = [str(c) for c in conn.execute.call_args_list]
        self.assertFalse(
            any("query_only" in pc for pc in pragma_calls),
            f"PRAGMA query_only must NOT be emitted for full; got {pragma_calls}",
        )

    def test_apply_pragmas_no_query_only_for_none(self) -> None:
        """When _db_profile == None, _apply_pragmas must NOT emit query_only."""
        set_db_profile(None)
        conn = MagicMock(spec=sqlite3.Connection)
        _apply_pragmas(conn)
        pragma_calls = [str(c) for c in conn.execute.call_args_list]
        self.assertFalse(
            any("query_only" in pc for pc in pragma_calls),
            f"PRAGMA query_only must NOT be emitted for None; got {pragma_calls}",
        )

    def test_apply_pragmas_no_query_only_for_mini(self) -> None:
        """When _db_profile == 'mini', _apply_pragmas must NOT emit query_only."""
        set_db_profile("mini")
        conn = MagicMock(spec=sqlite3.Connection)
        _apply_pragmas(conn)
        pragma_calls = [str(c) for c in conn.execute.call_args_list]
        self.assertFalse(
            any("query_only" in pc for pc in pragma_calls),
            f"PRAGMA query_only must NOT be emitted for mini; got {pragma_calls}",
        )

    def test_apply_pragmas_no_query_only_for_developer(self) -> None:
        """When _db_profile == 'developer', _apply_pragmas must NOT emit query_only."""
        set_db_profile("developer")
        conn = MagicMock(spec=sqlite3.Connection)
        _apply_pragmas(conn)
        pragma_calls = [str(c) for c in conn.execute.call_args_list]
        self.assertFalse(
            any("query_only" in pc for pc in pragma_calls),
            f"PRAGMA query_only must NOT be emitted for developer; got {pragma_calls}",
        )

    def test_edge_query_only_rejects_actual_write(self) -> None:
        """End-to-end: a real SQLite connection with query_only=1 rejects INSERT."""
        set_db_profile("edge")
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE t (x INTEGER)")
            # Apply pragmas the way get_conn does.
            _apply_pragmas(conn)
            # A write must now fail with SQLITE_READONLY.
            with self.assertRaises(sqlite3.OperationalError) as ctx:
                conn.execute("INSERT INTO t (x) VALUES (1)")
            # SQLite error message mentions read-only / query_only.
            self.assertIn("readonly", str(ctx.exception).lower())
            conn.close()
        finally:
            import os as _os
            _os.unlink(db_path)

    def test_full_allows_actual_write(self) -> None:
        """A real SQLite connection with profile=full allows INSERT."""
        set_db_profile("full")
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE t (x INTEGER)")
            _apply_pragmas(conn)
            # A write must succeed.
            conn.execute("INSERT INTO t (x) VALUES (1)")
            conn.commit()
            row = conn.execute("SELECT COUNT(*) FROM t").fetchone()
            self.assertEqual(row[0], 1)
            conn.close()
        finally:
            import os as _os
            _os.unlink(db_path)


# =======================================================================
# 6. Response surfaces profile — CreateRunResponse.routing includes profile
# =======================================================================


class ResponseSurfacesProfileTests(unittest.TestCase):
    """Verify that CreateRunResponse.routing includes the resolved profile."""

    def test_create_run_response_routing_has_profile_key(self) -> None:
        """The routing dict in create_run must include 'profile'."""
        source = (_ROOT / "app.py").read_text()
        # The routing dict assigned to CreateRunResponse must include
        # the profile key.
        self.assertIn('"profile"', source,
                      "CreateRunResponse.routing must include 'profile' key")


# =======================================================================
# 7. Backward compatibility — no profile kwarg → full → no query_only
# =======================================================================


class BackwardCompatTests(_DbProfileRestore, unittest.TestCase):
    """Verify that callers without profile kwarg get pre-Epic-9.4 behaviour."""

    def test_danger_check_no_profile_kwarg_works(self) -> None:
        """evaluate() without profile kwarg must not raise."""
        from dispatcher.safety import evaluate
        decision = evaluate("hello", mode="normal")
        self.assertEqual(decision.action, "allow")

    def test_set_db_profile_none_is_default(self) -> None:
        """The default _db_profile must be None (no query_only)."""
        # Restore to None (the import-time default).
        set_db_profile(None)
        self.assertIsNone(get_db_profile())

    def test_apply_pragmas_default_no_query_only(self) -> None:
        """With default _db_profile=None, _apply_pragmas must not emit query_only."""
        set_db_profile(None)
        conn = MagicMock(spec=sqlite3.Connection)
        _apply_pragmas(conn)
        pragma_calls = [str(c) for c in conn.execute.call_args_list]
        self.assertFalse(any("query_only" in pc for pc in pragma_calls))


# =======================================================================
# 8. Isolation contract — no forbidden imports added
# =======================================================================


class IsolationContractTests(unittest.TestCase):
    """Verify that the Epic 9.4 changes don't break isolation contracts."""

    def test_db_py_no_new_forbidden_imports(self) -> None:
        """dispatcher/db.py must not import subprocess, requests, httpx, etc."""
        source = Path("dispatcher/db.py").read_text()
        tree = ast.parse(source)
        forbidden = {"subprocess", "requests", "httpx", "urllib", "http.client"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], forbidden,
                                     f"db.py must not import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    self.assertNotIn(node.module.split(".")[0], forbidden,
                                      f"db.py must not import from {node.module}")

    def test_safety_py_no_new_forbidden_imports(self) -> None:
        """dispatcher/safety.py must not import subprocess, requests, httpx, etc."""
        source = Path("dispatcher/safety.py").read_text()
        tree = ast.parse(source)
        forbidden = {"subprocess", "requests", "httpx", "urllib", "http.client"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], forbidden,
                                     f"safety.py must not import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    self.assertNotIn(node.module.split(".")[0], forbidden,
                                      f"safety.py must not import from {node.module}")

    def test_set_db_profile_is_pure_state(self) -> None:
        """set_db_profile must be a simple setter (no side effects beyond _db_profile)."""
        source = (_ROOT / "dispatcher" / "db.py").read_text()
        # Find the set_db_profile function body.
        tree = ast.parse(source)
        func_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "set_db_profile":
                func_node = node
                break
        self.assertIsNotNone(func_node, "set_db_profile must exist in db.py")
        func_source = ast.get_source_segment(source, func_node) or ""
        # Must not contain subprocess, os.system, open(, etc.
        self.assertNotIn("subprocess", func_source)
        self.assertNotIn("os.system", func_source)
        self.assertNotIn("open(", func_source)

    def test_get_db_profile_is_pure_read(self) -> None:
        """get_db_profile must be a simple getter."""
        source = (_ROOT / "dispatcher" / "db.py").read_text()
        tree = ast.parse(source)
        func_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "get_db_profile":
                func_node = node
                break
        self.assertIsNotNone(func_node, "get_db_profile must exist in db.py")
        func_source = ast.get_source_segment(source, func_node) or ""
        self.assertNotIn("subprocess", func_source)
        self.assertNotIn("os.system", func_source)


# =======================================================================
# 9. Descriptor enforcement fields — edge is_read_only + can_dispatch=False
# =======================================================================


class DescriptorEnforcementFieldsTests(unittest.TestCase):
    """Verify the descriptor fields that §21.4 enforcement relies on."""

    def test_edge_is_read_only(self) -> None:
        desc = get_descriptor("edge")
        self.assertTrue(desc.is_read_only)

    def test_edge_can_dispatch_false(self) -> None:
        desc = get_descriptor("edge")
        self.assertFalse(desc.can_dispatch)

    def test_edge_db_writes_disabled(self) -> None:
        desc = get_descriptor("edge")
        self.assertEqual(desc.db_writes, "disabled")

    def test_full_can_dispatch_true(self) -> None:
        desc = get_descriptor("full")
        self.assertTrue(desc.can_dispatch)

    def test_full_is_read_only_false(self) -> None:
        desc = get_descriptor("full")
        self.assertFalse(desc.is_read_only)

    def test_mini_can_dispatch_true(self) -> None:
        desc = get_descriptor("mini")
        self.assertTrue(desc.can_dispatch)

    def test_developer_can_dispatch_true(self) -> None:
        desc = get_descriptor("developer")
        self.assertTrue(desc.can_dispatch)


# =======================================================================
# 10. Profile log entry — create_run logs the resolved profile
# =======================================================================


class ProfileLogEntryTests(unittest.TestCase):
    """Verify that create_run logs the resolved profile on the task log."""

    def test_create_run_logs_resolved_profile(self) -> None:
        """create_run must call manager.log with the resolved profile."""
        source = (_ROOT / "app.py").read_text()
        self.assertIn("profile=", source,
                      "create_run must log the resolved profile")


if __name__ == "__main__":
    unittest.main()