"""AEE-7.1 — Claude Code env-routing tests (Ollama-Cloud / minimax-m3:cloud).

These tests pin the AUTH RESCUE behavior verified on 2026-07-11:

* The host runs Claude CLI 2.1.206 against the Ollama-Cloud API
  (``ANTHROPIC_BASE_URL`` points at ``https://ollama.com``) with the
  ``minimax-m3:cloud`` model.
* The parent process stores the Ollama-Cloud bearer token under
  ``ANTHROPIC_AUTH_TOKEN``; Claude CLI 2.1.206 only honours
  ``ANTHROPIC_API_KEY`` in its default ``bare=False`` code path.
* The shim's ``_build_claude_env_mirror`` mirrors
  ``ANTHROPIC_AUTH_TOKEN`` -> ``ANTHROPIC_API_KEY`` *only* when the
  latter is unset.
* The underlying ``ClaudeCodeProvider._filter_env`` allow-list still
  drops unrelated keys (e.g. ``OLLAMA_API_KEY``,
  ``DATABASE_URL``) and never logs the token.
* ``--bare=True`` combined with the absence of
  ``ANTHROPIC_API_KEY`` triggers a warning, not a silent failure.

These tests do **not** spawn a real ``claude`` subprocess; they
exercise the env-mirror and allow-list layers directly.
"""
from __future__ import annotations

import io
import logging
import os
import re
import unittest
from typing import Dict, Mapping


from aee.adapters.claude_code_provider import (
    ClaudeCodeProvider,
    _ALLOWED_ENV_VARS,
)
from aee.orchestrator.claude_code_provider_shim import (
    ClaudeCodeExecProvider,
    _build_claude_env_mirror,
)
from aee.runtimes.models import (
    RuntimeDescriptor,
    RuntimeHealth,
    RuntimeHealthStatus,
)


# A canonical fake "Ollama-Cloud + minimax-m3:cloud" parent env. The
# token values are 12-byte placeholders; they are NEVER logged or
# asserted against verbatim.
_PARENT_OLLAMA_CLOUD = {
    "PATH": "/usr/bin",
    "HOME": "/home/ubuntu",
    "ANTHROPIC_BASE_URL": "https://ollama.com",
    "ANTHROPIC_AUTH_TOKEN": "REDACTED-token-1234",
    "ANTHROPIC_MODEL": "minimax-m3:cloud",
    "ANTHROPIC_DEFAULT_Sonnet_MODEL": "minimax-m3:cloud",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "minimax-m3:cloud",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "minimax-m3:cloud",
    "OLLAMA_API_KEY": "REDACTED-ollama-key-5678",
    "DATABASE_URL": "REDACTED-db-url",
    "SSH_AUTH_SOCK": "/tmp/REDACTED.sock",
}


# ----------------------------------------------------------------------
# 1. Env-mirror purity tests (no subprocess, no log emission)
# ----------------------------------------------------------------------


class TestEnvMirrorPurity(unittest.TestCase):
    """The mirror returns a copy and never mutates the parent env."""

    def test_mirror_does_not_mutate_parent(self) -> None:
        snapshot: Dict[str, str] = dict(_PARENT_OLLAMA_CLOUD)
        _build_claude_env_mirror(snapshot)
        self.assertEqual(snapshot, _PARENT_OLLAMA_CLOUD)
        # And the returned object is a new dict, not the same object.
        out = _build_claude_env_mirror(snapshot)
        self.assertIsNot(out, snapshot)

    def test_mirror_injects_api_key_when_only_auth_token_set(self) -> None:
        out = _build_claude_env_mirror(_PARENT_OLLAMA_CLOUD)
        self.assertIn("ANTHROPIC_API_KEY", out)
        # The mirrored value MUST equal the auth token value (so the
        # worker can authenticate against the Ollama-Cloud proxy).
        self.assertEqual(
            out["ANTHROPIC_API_KEY"], _PARENT_OLLAMA_CLOUD["ANTHROPIC_AUTH_TOKEN"]
        )
        # The original ANTHROPIC_AUTH_TOKEN must still be present.
        self.assertEqual(
            out["ANTHROPIC_AUTH_TOKEN"], _PARENT_OLLAMA_CLOUD["ANTHROPIC_AUTH_TOKEN"]
        )

    def test_mirror_does_not_overwrite_explicit_api_key(self) -> None:
        parent = dict(_PARENT_OLLAMA_CLOUD)
        parent["ANTHROPIC_API_KEY"] = "REDACTED-explicit"
        out = _build_claude_env_mirror(parent)
        self.assertEqual(out["ANTHROPIC_API_KEY"], "REDACTED-explicit")
        self.assertNotEqual(
            out["ANTHROPIC_API_KEY"], out["ANTHROPIC_AUTH_TOKEN"]
        )

    def test_mirror_no_op_when_neither_set(self) -> None:
        parent = {"PATH": "/usr/bin", "HOME": "/home/ubuntu"}
        out = _build_claude_env_mirror(parent)
        self.assertNotIn("ANTHROPIC_API_KEY", out)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", out)
        # Other keys are preserved.
        self.assertEqual(out["PATH"], "/usr/bin")

    def test_mirror_empty_auth_token_does_not_inject(self) -> None:
        parent = {"PATH": "/x", "ANTHROPIC_AUTH_TOKEN": ""}
        out = _build_claude_env_mirror(parent)
        self.assertNotIn("ANTHROPIC_API_KEY", out)


# ----------------------------------------------------------------------
# 2. Allow-list integration tests
# ----------------------------------------------------------------------


class TestAllowListAfterMirror(unittest.TestCase):
    """The mirror's output, once filtered by the AEE-6 allow-list,
    must contain exactly the keys Claude CLI needs to authenticate
    against Ollama-Cloud, and MUST NOT contain unrelated secrets.
    """

    def setUp(self) -> None:
        self._provider = ClaudeCodeProvider(
            binary="/bin/true",  # never spawned in these tests
            max_turns=1,
            output_format="text",
            bare=False,
        )

    def _filter(self, parent: Mapping[str, str]) -> Dict[str, str]:
        return self._provider._filter_env(_build_claude_env_mirror(parent))

    def test_filtered_env_has_anthropic_api_key(self) -> None:
        filtered = self._filter(_PARENT_OLLAMA_CLOUD)
        self.assertIn("ANTHROPIC_API_KEY", filtered)
        self.assertEqual(
            filtered["ANTHROPIC_API_KEY"],
            _PARENT_OLLAMA_CLOUD["ANTHROPIC_AUTH_TOKEN"],
        )

    def test_filtered_env_has_anthropic_base_url(self) -> None:
        filtered = self._filter(_PARENT_OLLAMA_CLOUD)
        self.assertEqual(
            filtered["ANTHROPIC_BASE_URL"],
            "https://ollama.com",
        )

    def test_filtered_env_has_anthropic_model(self) -> None:
        # AEE-7.1 — ANTHROPIC_MODEL is the new entry; it must be
        # present in the allow-list so the worker can route to
        # minimax-m3:cloud.
        filtered = self._filter(_PARENT_OLLAMA_CLOUD)
        self.assertIn("ANTHROPIC_MODEL", filtered)
        self.assertEqual(filtered["ANTHROPIC_MODEL"], "minimax-m3:cloud")
        # And the entry is in the module-level allow-list (so a
        # future refactor that drops the constant will fail this
        # test loudly).
        self.assertIn("ANTHROPIC_MODEL", _ALLOWED_ENV_VARS)

    def test_filtered_env_drops_ollama_api_key(self) -> None:
        # The Ollama-Cloud key is for the Ollama REST API; Claude CLI
        # does not read it, and the AEE-6 allow-list must drop it.
        filtered = self._filter(_PARENT_OLLAMA_CLOUD)
        self.assertNotIn("OLLAMA_API_KEY", filtered)
        self.assertNotIn("OLLAMA_API_KEY", _ALLOWED_ENV_VARS)

    def test_filtered_env_drops_unrelated_secrets(self) -> None:
        filtered = self._filter(_PARENT_OLLAMA_CLOUD)
        for k in (
            "DATABASE_URL",
            "SSH_AUTH_SOCK",
            "AWS_SECRET_ACCESS_KEY",
            "GITHUB_TOKEN",
        ):
            self.assertNotIn(k, filtered, f"{k} should be dropped by allow-list")

    def test_filtered_env_preserves_path_and_home(self) -> None:
        filtered = self._filter(_PARENT_OLLAMA_CLOUD)
        self.assertEqual(filtered["PATH"], "/usr/bin")
        self.assertEqual(filtered["HOME"], "/home/ubuntu")


# ----------------------------------------------------------------------
# 3. Secret-leak guard: the shim must NEVER log the token value
# ----------------------------------------------------------------------


class TestSecretNeverLogged(unittest.TestCase):
    """A defense-in-depth guard: the token value must never appear in
    any log record produced by the shim or by the
    ClaudeCodeProvider. This catches a class of regressions where a
    future patch adds a ``log.debug("env: %r", env)`` that would
    silently leak the credential.
    """

    SECRET_CANARY = "CANARY-SECRET-TOKEN-abcdef1234567890XYZ"
    AUTH_CANARY = "CANARY-AUTH-TOKEN-fedcba0987654321ABC"

    def setUp(self) -> None:
        # Attach a capture handler to every logger that could
        # potentially see the env. We use the root logger to be
        # exhaustive — the assertion is "the canary string must
        # not appear in any record's formatted output".
        self._buf = io.StringIO()
        handler = logging.StreamHandler(self._buf)
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        self._handler = handler
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        # Pre-seed the env with the canary values; we never want to
        # actually use these for real auth, so the test runs a noop
        # ``_filter_env`` that must not echo them.
        self._saved_env = dict(os.environ)
        os.environ["ANTHROPIC_API_KEY"] = self.SECRET_CANARY
        os.environ["ANTHROPIC_AUTH_TOKEN"] = self.AUTH_CANARY

    def tearDown(self) -> None:
        # Restore the env.
        os.environ.clear()
        os.environ.update(self._saved_env)
        logging.getLogger().removeHandler(self._handler)

    def test_filter_env_does_not_log_token(self) -> None:
        provider = ClaudeCodeProvider(
            binary="/bin/true", max_turns=1, output_format="text", bare=False
        )
        # Run the filter under multiple plausible envs.
        for env in (
            {"ANTHROPIC_API_KEY": self.SECRET_CANARY},
            {"ANTHROPIC_AUTH_TOKEN": self.AUTH_CANARY},
            {
                "ANTHROPIC_API_KEY": self.SECRET_CANARY,
                "ANTHROPIC_AUTH_TOKEN": self.AUTH_CANARY,
            },
        ):
            provider._filter_env(env)  # must not log
        log_output = self._buf.getvalue()
        self.assertNotIn(self.SECRET_CANARY, log_output)
        self.assertNotIn(self.AUTH_CANARY, log_output)

    def test_env_mirror_does_not_log_token(self) -> None:
        _build_claude_env_mirror(
            {"ANTHROPIC_AUTH_TOKEN": self.AUTH_CANARY, "OLLAMA_API_KEY": self.SECRET_CANARY}
        )
        log_output = self._buf.getvalue()
        self.assertNotIn(self.AUTH_CANARY, log_output)
        self.assertNotIn(self.SECRET_CANARY, log_output)

    def test_construct_shim_with_bare_no_api_key_logs_warning_only(self) -> None:
        """Forcing ``bare=True`` while ``ANTHROPIC_API_KEY`` is missing
        must produce a warning (not a silent failure). The warning
        message must NOT echo the token value.
        """
        # Force the env to lack ANTHROPIC_API_KEY (only AUTH_TOKEN).
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ["ANTHROPIC_AUTH_TOKEN"] = self.AUTH_CANARY
        descriptor = RuntimeDescriptor(
            runtime_id="claude-code-shim",
            runtime_type="claude_code",
            display_name="claude_code",
            health=RuntimeHealth(
                status=RuntimeHealthStatus.HEALTHY,
                last_checked_at="2026-07-11T00:00:00Z",
            ),
        )
        # The constructor logs a warning when bare=True and
        # ANTHROPIC_API_KEY is missing.
        ClaudeCodeExecProvider(descriptor=descriptor, bare=True)
        log_output = self._buf.getvalue()
        # The warning is descriptive, not a token echo.
        self.assertNotIn(self.AUTH_CANARY, log_output)
        # The warning mentions the variable name so the operator
        # can act on it.
        self.assertIn("ANTHROPIC_API_KEY", log_output)
        self.assertIn("bare", log_output.lower())


# ----------------------------------------------------------------------
# 4. Model name propagation (sanity check that the model field is
#    carried through into the AEE-6 allow-list end-to-end)
# ----------------------------------------------------------------------


class TestModelRoutingMetadata(unittest.TestCase):
    """The selected model id (``minimax-m3:cloud``) is set via
    ``ANTHROPIC_MODEL`` in the parent env. After mirror + filter it
    must be present in the dict the subprocess sees, with value
    exactly equal to what the parent set.
    """

    def setUp(self) -> None:
        self._provider = ClaudeCodeProvider(
            binary="/bin/true", max_turns=1, output_format="text", bare=False
        )

    def test_model_value_preserved_through_mirror_and_filter(self) -> None:
        parent = {
            "ANTHROPIC_BASE_URL": "https://ollama.com",
            "ANTHROPIC_AUTH_TOKEN": "REDACTED",
            "ANTHROPIC_MODEL": "minimax-m3:cloud",
        }
        out = self._provider._filter_env(_build_claude_env_mirror(parent))
        self.assertEqual(out["ANTHROPIC_MODEL"], "minimax-m3:cloud")
        self.assertEqual(out["ANTHROPIC_API_KEY"], "REDACTED")
        self.assertEqual(out["ANTHROPIC_BASE_URL"], "https://ollama.com")

    def test_default_models_preserved(self) -> None:
        # The legacy ANTHROPIC_DEFAULT_*_MODEL vars must also survive.
        parent = {
            "ANTHROPIC_AUTH_TOKEN": "REDACTED",
            "ANTHROPIC_DEFAULT_Sonnet_MODEL": "minimax-m3:cloud",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "minimax-m3:cloud",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "minimax-m3:cloud",
        }
        out = self._provider._filter_env(_build_claude_env_mirror(parent))
        for k in (
            "ANTHROPIC_DEFAULT_Sonnet_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
        ):
            self.assertEqual(out.get(k), "minimax-m3:cloud")


if __name__ == "__main__":
    unittest.main()
