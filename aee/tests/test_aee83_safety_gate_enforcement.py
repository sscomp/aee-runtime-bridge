"""AEE-8.3 — Safety-gate profile enforcement (read-only activation).

Targeted tests for the profile-aware enforcement added to
``dispatcher.safety.evaluate()``.

Covers:
  - Backward compatibility (profile=None is identical to pre-AEE-8.3)
  - Profile-aware enforcement (mini rejects cron/subagent, edge rejects
    writes, full allows all, developer allows within sandbox)
  - Descriptor integration (enforcement fields are read correctly)
  - Isolation contract (lazy import, no module-top descriptor import)
  - Unknown profile path (defers to AEE-8.1 validation contract)
  - Intent detection (cron, subagent, write patterns)

Run:
    cd /home/ubuntu/hermes-runtime-bridge
    PYTHONPATH=. python3 -m unittest aee.tests.test_aee83_safety_gate_enforcement -v
"""
from __future__ import annotations

import ast
import importlib
import sys
import unittest
from pathlib import Path

# Make `dispatcher` importable when run from the repo root.
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dispatcher.safety import (
    SafetyDecision,
    evaluate,
    _has_cron_intent,
    _has_subagent_intent,
    _has_write_intent,
)
from aee.profiles.descriptor import (
    KNOWN_PROFILES,
    UnknownProfileError,
    get_descriptor,
)


# ---------------------------------------------------------------------------
# 1. Backward compatibility
# ---------------------------------------------------------------------------


class BackwardCompatTests(unittest.TestCase):
    """profile=None must be byte-for-byte identical to pre-AEE-8.3."""

    def test_no_profile_arg_backward_compat(self):
        """evaluate(text, mode) without profile == evaluate(text, mode, profile=None)."""
        d1 = evaluate("ls -la /home/ubuntu", mode="normal")
        d2 = evaluate("ls -la /home/ubuntu", mode="normal", profile=None)
        self.assertEqual(d1.action, d2.action)
        self.assertEqual(d1.reason, d2.reason)
        self.assertEqual(d1.matched, d2.matched)
        self.assertEqual(d1.needs_human, d2.needs_human)
        self.assertEqual(d1.meta, d2.meta)

    def test_profile_none_explicit_same_as_omitted(self):
        """Explicit profile=None is identical to omitting the arg."""
        d1 = evaluate("echo hello", mode="normal")
        d2 = evaluate("echo hello", mode="normal", profile=None)
        self.assertEqual(d1.to_dict(), d2.to_dict())

    def test_existing_mode_restrictions_still_work(self):
        """Pre-existing mode-based restrictions are unaffected by profile=None."""
        # Blocklist still fires
        d = evaluate("rm -rf /", mode="normal")
        self.assertEqual(d.action, "block")
        # Allow still fires
        d = evaluate("ls -la /home/ubuntu", mode="normal")
        self.assertEqual(d.action, "allow")
        # Ops mode allowlist still fires
        d = evaluate("ls -la /home/ubuntu", mode="ops")
        self.assertEqual(d.action, "allow")

    def test_empty_profile_string_treated_as_none(self):
        """profile='' should be treated as None (no enforcement)."""
        d1 = evaluate("echo hello", mode="normal", profile=None)
        d2 = evaluate("echo hello", mode="normal", profile="")
        self.assertEqual(d1.to_dict(), d2.to_dict())


# ---------------------------------------------------------------------------
# 2. Profile-aware enforcement
# ---------------------------------------------------------------------------


class ProfileAwareEnforcementTests(unittest.TestCase):
    """Profile=mini/edge/full/developer enforcement behavior."""

    # --- mini ---

    def test_mini_rejects_cron_creation(self):
        """profile=mini + cron intent → block."""
        d = evaluate("hermes cron add my-job", mode="normal", profile="mini")
        self.assertEqual(d.action, "block")
        self.assertIn("can_create_cron", d.meta.get("violation", ""))

    def test_mini_rejects_subagent_delegation(self):
        """profile=mini + subagent intent → block."""
        d = evaluate("delegate this task to a subagent", mode="normal", profile="mini")
        self.assertEqual(d.action, "block")
        self.assertIn("can_delegate_subagents", d.meta.get("violation", ""))

    def test_mini_allows_non_restricted(self):
        """profile=mini + non-restricted text → allow."""
        d = evaluate("ls -la /home/ubuntu", mode="normal", profile="mini")
        self.assertEqual(d.action, "allow")

    def test_mini_allows_non_cron_text_with_cron_word_fragment(self):
        """Words containing 'cron' as substring but not intent should not be blocked."""
        # 'crontab' is a cron intent pattern, but 'neocron' is not
        d = evaluate("read the neocron documentation", mode="normal", profile="mini")
        self.assertEqual(d.action, "allow")

    # --- edge ---

    def test_edge_rejects_write_intent(self):
        """profile=edge + write intent → block."""
        d = evaluate("write_file /home/ubuntu/test.txt", mode="normal", profile="edge")
        self.assertEqual(d.action, "block")
        self.assertIn("is_read_only", d.meta.get("violation", ""))

    def test_edge_rejects_db_mutation(self):
        """profile=edge + DB INSERT → block."""
        d = evaluate("INSERT INTO tasks VALUES (1)", mode="normal", profile="edge")
        self.assertEqual(d.action, "block")

    def test_edge_rejects_git_commit(self):
        """profile=edge + git commit → block."""
        d = evaluate("git commit -m 'test'", mode="normal", profile="edge")
        self.assertEqual(d.action, "block")

    def test_edge_allows_read_only(self):
        """profile=edge + read-only text → allow."""
        d = evaluate("ls -la /home/ubuntu", mode="normal", profile="edge")
        self.assertEqual(d.action, "allow")

    def test_edge_rejects_cron(self):
        """profile=edge also cannot create cron (can_create_cron=False)."""
        d = evaluate("hermes cron add my-job", mode="normal", profile="edge")
        self.assertEqual(d.action, "block")

    def test_edge_rejects_subagent(self):
        """profile=edge also cannot delegate subagents."""
        d = evaluate("spawn a subagent for this", mode="normal", profile="edge")
        self.assertEqual(d.action, "block")

    # --- full ---

    def test_full_allows_cron(self):
        """profile=full + cron intent → allow (can_create_cron=True)."""
        d = evaluate("hermes cron add my-job", mode="normal", profile="full")
        self.assertEqual(d.action, "allow")

    def test_full_allows_subagent(self):
        """profile=full + subagent intent → allow (can_delegate_subagents=True)."""
        d = evaluate("delegate this task to a subagent", mode="normal", profile="full")
        self.assertEqual(d.action, "allow")

    def test_full_allows_write(self):
        """profile=full + write intent → allow (is_read_only=False)."""
        d = evaluate("write_file /home/ubuntu/test.txt", mode="normal", profile="full")
        self.assertEqual(d.action, "allow")

    # --- developer ---

    def test_developer_allows_subagent(self):
        """profile=developer + subagent intent → allow (can_delegate_subagents=True)."""
        d = evaluate("delegate this task to a subagent", mode="normal", profile="developer")
        self.assertEqual(d.action, "allow")

    def test_developer_rejects_cron(self):
        """profile=developer + cron intent → block (can_create_cron=False)."""
        d = evaluate("hermes cron add my-job", mode="normal", profile="developer")
        self.assertEqual(d.action, "block")

    def test_developer_allows_write(self):
        """profile=developer + write intent → allow (is_read_only=False)."""
        d = evaluate("write_file /tmp/test.txt", mode="normal", profile="developer")
        self.assertEqual(d.action, "allow")


# ---------------------------------------------------------------------------
# 3. Descriptor integration
# ---------------------------------------------------------------------------


class DescriptorIntegrationTests(unittest.TestCase):
    """Verify the descriptor enforcement fields are read correctly."""

    def test_mini_can_create_cron_false(self):
        desc = get_descriptor("mini")
        self.assertFalse(desc.can_create_cron)

    def test_full_can_create_cron_true(self):
        desc = get_descriptor("full")
        self.assertTrue(desc.can_create_cron)

    def test_edge_is_read_only_true(self):
        desc = get_descriptor("edge")
        self.assertTrue(desc.is_read_only)

    def test_developer_can_delegate_subagents_true(self):
        desc = get_descriptor("developer")
        self.assertTrue(desc.can_delegate_subagents)

    def test_mini_can_delegate_subagents_false(self):
        desc = get_descriptor("mini")
        self.assertFalse(desc.can_delegate_subagents)

    def test_edge_can_create_cron_false(self):
        desc = get_descriptor("edge")
        self.assertFalse(desc.can_create_cron)

    def test_full_is_read_only_false(self):
        desc = get_descriptor("full")
        self.assertFalse(desc.is_read_only)


# ---------------------------------------------------------------------------
# 4. Isolation contract
# ---------------------------------------------------------------------------


class IsolationContractTests(unittest.TestCase):
    """safety.py must not import descriptor at module top (lazy only)."""

    def test_no_descriptor_import_at_module_top(self):
        """The AST of safety.py must not have a top-level import of
        aee.profiles.descriptor."""
        safety_path = ROOT / "dispatcher" / "safety.py"
        source = safety_path.read_text()
        tree = ast.parse(source)
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(
                        "aee.profiles.descriptor",
                        alias.name,
                        "safety.py has top-level import of aee.profiles.descriptor",
                    )
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                self.assertNotIn(
                    "aee.profiles.descriptor",
                    mod,
                    "safety.py has top-level from-import of aee.profiles.descriptor",
                )

    def test_no_dispatcher_db_subprocess_import(self):
        """safety.py must not import dispatcher.db, sqlite3, or subprocess."""
        safety_path = ROOT / "dispatcher" / "safety.py"
        source = safety_path.read_text()
        tree = ast.parse(source)
        forbidden = {"sqlite3", "subprocess", "dispatcher.db", "dispatcher.manager"}
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for f in forbidden:
                        self.assertNotIn(
                            f,
                            alias.name,
                            f"safety.py imports forbidden module {f!r} at top level",
                        )
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for f in forbidden:
                    self.assertNotIn(
                        f,
                        mod,
                        f"safety.py imports forbidden module {f!r} at top level",
                    )

    def test_descriptor_import_is_inside_function(self):
        """The import of aee.profiles.descriptor must be inside evaluate()."""
        safety_path = ROOT / "dispatcher" / "safety.py"
        source = safety_path.read_text()
        tree = ast.parse(source)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "evaluate":
                for child in ast.walk(node):
                    if isinstance(child, ast.ImportFrom):
                        if child.module and "aee.profiles.descriptor" in child.module:
                            found = True
        self.assertTrue(
            found,
            "aee.profiles.descriptor import not found inside evaluate() function",
        )


# ---------------------------------------------------------------------------
# 5. Unknown profile path
# ---------------------------------------------------------------------------


class UnknownProfileTests(unittest.TestCase):
    """Unknown profile must defer to AEE-8.1 validation contract."""

    def test_unknown_profile_raises_unknown_profile_error(self):
        """evaluate with unknown profile must raise UnknownProfileError."""
        with self.assertRaises(UnknownProfileError):
            evaluate("ls -la", mode="normal", profile="bogus")

    def test_unknown_profile_error_carries_profile_name(self):
        """The error must carry the rejected profile name."""
        try:
            evaluate("ls -la", mode="normal", profile="nonexistent")
        except UnknownProfileError as e:
            self.assertEqual(e.profile, "nonexistent")
        else:
            self.fail("UnknownProfileError not raised")


# ---------------------------------------------------------------------------
# 6. Intent detection
# ---------------------------------------------------------------------------


class IntentDetectionTests(unittest.TestCase):
    """Verify the intent-detection helpers work correctly."""

    # --- cron intent ---

    def test_cron_intent_hermes_cron(self):
        self.assertTrue(_has_cron_intent("hermes cron add my-job"))

    def test_cron_intent_crontab(self):
        self.assertTrue(_has_cron_intent("edit crontab -e"))

    def test_cron_intent_scheduled_job(self):
        self.assertTrue(_has_cron_intent("create a scheduled job"))

    def test_cron_intent_no_match(self):
        self.assertFalse(_has_cron_intent("ls -la /home/ubuntu"))

    def test_cron_intent_case_insensitive(self):
        self.assertTrue(_has_cron_intent("CRON add new"))

    # --- subagent intent ---

    def test_subagent_intent_delegate(self):
        self.assertTrue(_has_subagent_intent("delegate this task"))

    def test_subagent_intent_subagent(self):
        self.assertTrue(_has_subagent_intent("spawn a subagent"))

    def test_subagent_intent_no_match(self):
        self.assertFalse(_has_subagent_intent("ls -la /home/ubuntu"))

    # --- write intent ---

    def test_write_intent_write_file(self):
        self.assertTrue(_has_write_intent("write_file /tmp/test.py"))

    def test_write_intent_git_commit(self):
        self.assertTrue(_has_write_intent("git commit -m 'test'"))

    def test_write_intent_insert(self):
        self.assertTrue(_has_write_intent("INSERT INTO tasks VALUES (1)"))

    def test_write_intent_no_match(self):
        self.assertFalse(_has_write_intent("ls -la /home/ubuntu"))

    def test_write_intent_case_insensitive(self):
        self.assertTrue(_has_write_intent("DROP TABLE tasks"))


# ---------------------------------------------------------------------------
# 7. Known profile matrix
# ---------------------------------------------------------------------------


class KnownProfileMatrixTests(unittest.TestCase):
    """Test all known profiles from the descriptor table."""

    def test_all_known_profiles_accepted(self):
        """evaluate() must accept all profiles in KNOWN_PROFILES without error."""
        for p in KNOWN_PROFILES:
            with self.subTest(profile=p):
                d = evaluate("ls -la /home/ubuntu", mode="normal", profile=p)
                self.assertEqual(d.action, "allow",
                                 f"profile={p} should allow benign text")

    def test_full_no_restriction_on_any_intent(self):
        """full profile must not be blocked by any intent type."""
        texts = [
            "hermes cron add my-job",
            "delegate this task to a subagent",
            "write_file /home/ubuntu/test.txt",
            "git commit -m 'test'",
        ]
        for t in texts:
            with self.subTest(text=t):
                d = evaluate(t, mode="normal", profile="full")
                self.assertEqual(d.action, "allow",
                                 f"full profile should allow {t!r}")


if __name__ == "__main__":
    unittest.main()