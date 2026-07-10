"""Tests for the GPT -> MiniMax-M3 routing layer.

Run:
    cd ~/hermes-runtime-bridge
    .venv/bin/python tests/test_routing.py

What this file covers:
  * `build_source_map` correctly maps each of the five bridge keys to
    its source label and skips empty env values.
  * `identify_source` returns the right label for known keys and
    "unknown" for everything else.
  * `resolve_model_for_source` forces `gpt_model` for GPT source
    regardless of caller_model, honours caller override only when
    `allow_caller_override_for_gpt=True`, and falls through to the
    caller's choice for non-GPT sources.
  * `key_present` is False on the result when the policy was built
    without `minimax_key`, so the bridge can 503 cleanly.

What this file does NOT cover (and why):
  * End-to-end /runs HTTP calls — those live in
    `tests/test_phase2.py` / `tests/phase2_acceptance.py` and need a
    real upstream. We test the policy in isolation because the
    whole point of `dispatcher/routing.py` is to be a pure-function
    layer that the bridge composes.
  * Auth — `require_auth` is tested implicitly by the phase2
    acceptance tests; we don't duplicate the bearer-header dance
    here.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

# Make the bridge root importable.
BRIDGE_ROOT = Path(__file__).resolve().parent.parent
if str(BRIDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(BRIDGE_ROOT))

from dispatcher.routing import (  # noqa: E402
    RoutingPolicy,
    build_source_map,
    identify_source,
    resolve_model_for_source,
)


# Fixed synthetic env for deterministic source-map tests.
_SYNTH_ENV = {
    "BRIDGE_API_KEY": "k-cli-001",
    "GPT_BRIDGE_API_KEY": "k-gpt-001",
    "CLAUDE_BRIDGE_API_KEY": "k-claude-001",
    "CURSOR_BRIDGE_API_KEY": "k-cursor-001",
    "MCP_BRIDGE_API_KEY": "k-mcp-001",
}


def _policy(*, with_key: bool = True) -> RoutingPolicy:
    return RoutingPolicy(
        default_model="minimax-m3",
        gpt_model="MiniMaxAI/MiniMax-M3",
        allow_caller_override_for_gpt=False,
        minimax_key="s2_test_key_***" if with_key else "",
    )


class TestBuildSourceMap(unittest.TestCase):
    def test_all_five_keys_resolve(self) -> None:
        m = build_source_map(_SYNTH_ENV)
        self.assertEqual(m["k-cli-001"], "cli")
        self.assertEqual(m["k-gpt-001"], "gpt")
        self.assertEqual(m["k-claude-001"], "claude")
        self.assertEqual(m["k-cursor-001"], "cursor")
        self.assertEqual(m["k-mcp-001"], "mcp")
        self.assertEqual(len(m), 5)

    def test_empty_values_are_skipped(self) -> None:
        env = dict(_SYNTH_ENV)
        env["CURSOR_BRIDGE_API_KEY"] = ""
        env["MCP_BRIDGE_API_KEY"] = "   "  # whitespace-only also skipped
        m = build_source_map(env)
        self.assertNotIn("k-cursor-001", m)
        self.assertNotIn("k-mcp-001", m)
        self.assertEqual(len(m), 3)

    def test_missing_env_keys_become_empty(self) -> None:
        env = {"BRIDGE_API_KEY": "k-cli-002"}
        m = build_source_map(env)
        self.assertEqual(m, {"k-cli-002": "cli"})

    def test_empty_env_returns_empty_map(self) -> None:
        self.assertEqual(build_source_map({}), {})


class TestIdentifySource(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = build_source_map(_SYNTH_ENV)

    def test_known_gpt_key(self) -> None:
        self.assertEqual(identify_source("k-gpt-001", self.sources), "gpt")

    def test_known_cli_key(self) -> None:
        self.assertEqual(identify_source("k-cli-001", self.sources), "cli")

    def test_unknown_key(self) -> None:
        self.assertEqual(identify_source("k-attacker-xxx", self.sources), "unknown")

    def test_empty_key(self) -> None:
        self.assertEqual(identify_source("", self.sources), "unknown")

    def test_empty_map(self) -> None:
        self.assertEqual(identify_source("k-anything", {}), "unknown")


class TestResolveModelForSourceGPT(unittest.TestCase):
    """The core of the routing contract: GPT -> MiniMax-M3."""

    def test_gpt_no_caller_model_forces_minimax(self) -> None:
        r = resolve_model_for_source(
            source="gpt", caller_model=None, policy=_policy()
        )
        self.assertEqual(r.model_id, "MiniMaxAI/MiniMax-M3")
        self.assertTrue(r.was_forced)
        self.assertEqual(r.source, "gpt")
        self.assertTrue(r.key_present)

    def test_gpt_with_caller_model_still_forced(self) -> None:
        r = resolve_model_for_source(
            source="gpt", caller_model="some-other-model", policy=_policy()
        )
        self.assertEqual(r.model_id, "MiniMaxAI/MiniMax-M3")
        self.assertTrue(r.was_forced)
        self.assertIn("ignored", r.reason)

    def test_gpt_caller_override_allowed(self) -> None:
        p = RoutingPolicy(
            default_model="minimax-m3",
            gpt_model="MiniMaxAI/MiniMax-M3",
            allow_caller_override_for_gpt=True,
            minimax_key="s2_test",
        )
        r = resolve_model_for_source(
            source="gpt", caller_model="custom-llm", policy=p
        )
        self.assertEqual(r.model_id, "custom-llm")
        self.assertFalse(r.was_forced)
        self.assertIn("caller override", r.reason)

    def test_gpt_override_allowed_but_no_caller_model_still_forces(self) -> None:
        p = RoutingPolicy(
            default_model="minimax-m3",
            gpt_model="MiniMaxAI/MiniMax-M3",
            allow_caller_override_for_gpt=True,
            minimax_key="s2_test",
        )
        r = resolve_model_for_source(
            source="gpt", caller_model=None, policy=p
        )
        self.assertEqual(r.model_id, "MiniMaxAI/MiniMax-M3")
        self.assertTrue(r.was_forced)

    def test_gpt_key_missing_marks_key_present_false(self) -> None:
        r = resolve_model_for_source(
            source="gpt", caller_model=None, policy=_policy(with_key=False)
        )
        self.assertEqual(r.model_id, "MiniMaxAI/MiniMax-M3")
        self.assertTrue(r.was_forced)
        self.assertFalse(r.key_present)
        # Caller (bridge /runs handler) is expected to translate
        # (was_forced=True, key_present=False) into HTTP 503.


class TestResolveModelForSourceNonGPT(unittest.TestCase):
    """Non-GPT sources must NOT be forced to MiniMax-M3."""

    def test_cli_uses_caller_model(self) -> None:
        r = resolve_model_for_source(
            source="cli", caller_model="anything-else", policy=_policy()
        )
        self.assertEqual(r.model_id, "anything-else")
        self.assertFalse(r.was_forced)
        self.assertEqual(r.source, "cli")

    def test_cli_no_caller_model_uses_default(self) -> None:
        r = resolve_model_for_source(
            source="cli", caller_model=None, policy=_policy()
        )
        self.assertEqual(r.model_id, "minimax-m3")
        self.assertFalse(r.was_forced)

    def test_claude_uses_caller_model(self) -> None:
        r = resolve_model_for_source(
            source="claude", caller_model="claude-sonnet-4-6", policy=_policy()
        )
        self.assertEqual(r.model_id, "claude-sonnet-4-6")
        self.assertFalse(r.was_forced)

    def test_claude_with_minimax_caller_model_is_not_forced(self) -> None:
        # Even if a Claude caller asks for MiniMax-M3 explicitly, we
        # do not block it — the routing rule is one-way (GPT -> MiniMax),
        # not a deny-list.
        r = resolve_model_for_source(
            source="claude", caller_model="MiniMaxAI/MiniMax-M3", policy=_policy()
        )
        self.assertEqual(r.model_id, "MiniMaxAI/MiniMax-M3")
        self.assertFalse(r.was_forced)

    def test_cursor_uses_default_when_no_caller_model(self) -> None:
        r = resolve_model_for_source(
            source="cursor", caller_model=None, policy=_policy()
        )
        self.assertEqual(r.model_id, "minimax-m3")
        self.assertFalse(r.was_forced)

    def test_mcp_uses_caller_model(self) -> None:
        r = resolve_model_for_source(
            source="mcp", caller_model="gpt-4o", policy=_policy()
        )
        self.assertEqual(r.model_id, "gpt-4o")
        self.assertFalse(r.was_forced)

    def test_unknown_source_falls_through(self) -> None:
        # Defensive: if identify_source returns "unknown" (e.g. key
        # collision where the same key was assigned to two env vars),
        # the policy must NOT silently upgrade to MiniMax-M3. The
        # bridge's require_auth() should have already 401'd, but
        # better safe.
        r = resolve_model_for_source(
            source="unknown", caller_model="kimi-k2.6:cloud", policy=_policy()
        )
        self.assertEqual(r.model_id, "kimi-k2.6:cloud")
        self.assertFalse(r.was_forced)

    def test_non_gpt_key_present_reflects_policy(self) -> None:
        # `key_present` reflects the *policy*, not the source — it's
        # true whenever `policy.minimax_key` is set, regardless of
        # which source is being routed. The bridge should only check
        # `key_present` when `was_forced=True` (i.e. source=='gpt' and
        # routing decided to send the request to MiniMax-M3).
        for src in ("cli", "claude", "cursor", "mcp", "unknown"):
            r_with = resolve_model_for_source(
                source=src, caller_model=None, policy=_policy(with_key=True)
            )
            self.assertTrue(
                r_with.key_present,
                f"key_present should be True for source={src!r} when "
                f"policy has a key",
            )
            r_without = resolve_model_for_source(
                source=src, caller_model=None, policy=_policy(with_key=False)
            )
            self.assertFalse(
                r_without.key_present,
                f"key_present should be False for source={src!r} when "
                f"policy has no key",
            )


class TestIntegration(unittest.TestCase):
    """build_source_map -> identify_source -> resolve_model_for_source."""

    def test_gpt_key_full_path(self) -> None:
        sources = build_source_map(_SYNTH_ENV)
        src = identify_source("k-gpt-001", sources)
        self.assertEqual(src, "gpt")
        r = resolve_model_for_source(
            source=src, caller_model=None, policy=_policy()
        )
        self.assertEqual(r.model_id, "MiniMaxAI/MiniMax-M3")
        self.assertTrue(r.was_forced)
        self.assertTrue(r.key_present)

    def test_cli_key_full_path(self) -> None:
        sources = build_source_map(_SYNTH_ENV)
        src = identify_source("k-cli-001", sources)
        self.assertEqual(src, "cli")
        r = resolve_model_for_source(
            source=src, caller_model="kimi-k2.6:cloud", policy=_policy()
        )
        self.assertEqual(r.model_id, "kimi-k2.6:cloud")
        self.assertFalse(r.was_forced)


if __name__ == "__main__":
    unittest.main(verbosity=2)
