"""Phase 1 targeted tests — Claude Code CLI launcher --max-turns default = 80.

Verifies the three in-process sources of the default ``--max-turns``:
  1. ``aee.runtimes.executor_config._DEFAULTS["max_turns"]`` == 80
  2. ``aee.runtimes.executor_cli.ClaudeCodeCliRunner`` constructor default == 80
  3. ``aee.runtimes.executor_cli.ClaudeCodeCliRunner.from_config`` fallback == 80

And verifies the override paths still win:
  A. Per-request ``body.max_turns`` passed verbatim to the constructor.
  B. Config-file value in ``config/executor.json`` overrides the in-code
     ``_DEFAULTS`` (load_executor_config merges file > defaults).
  C. Env var ``AEE_EXECUTOR_MAX_TURNS`` overrides the config-file value.
  D. Explicit constructor ``max_turns=N`` argument wins over the default.

These tests are pure-Python and hermetic: they never spawn the real
``claude`` binary. They exercise the config + runner-factory surface
only, which is where the default lives.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

# Ensure the repo root is importable when run via ``python -m unittest``.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class TestExecutorConfigDefault(unittest.TestCase):
    """Group A — executor_config._DEFAULTS + config/executor.json."""

    def test_defaults_dict_max_turns_is_80(self) -> None:
        from aee.runtimes.executor_config import _DEFAULTS
        self.assertEqual(_DEFAULTS["max_turns"], 80)

    def test_config_file_max_turns_is_80(self) -> None:
        cfg_path = REPO_ROOT / "config" / "executor.json"
        self.assertTrue(cfg_path.exists(), "config/executor.json missing")
        with cfg_path.open() as fh:
            data = json.load(fh)
        self.assertEqual(data.get("max_turns"), 80)

    def test_load_executor_config_default_is_80(self) -> None:
        # With no env overrides and no monkeypatched config file, the merged
        # config should report max_turns == 80.
        from aee.runtimes.executor_config import load_executor_config
        with mock.patch.dict(os.environ, {}, clear=False):
            # Strip any env override that might be set in the test environment.
            os.environ.pop("AEE_EXECUTOR_MAX_TURNS", None)
            cfg = load_executor_config()
        self.assertEqual(cfg["max_turns"], 80)

    def test_env_override_AEE_EXECUTOR_MAX_TURNS_wins_over_file(self) -> None:
        """Env var override path — operator retarget at deploy time."""
        from aee.runtimes.executor_config import load_executor_config
        with mock.patch.dict(os.environ, {"AEE_EXECUTOR_MAX_TURNS": "30"}):
            cfg = load_executor_config()
        self.assertEqual(cfg["max_turns"], 30, "env override must beat file")


class TestRunnerConstructorDefault(unittest.TestCase):
    """Group B — ClaudeCodeCliRunner constructor default."""

    def test_constructor_default_max_turns_is_80(self) -> None:
        import inspect
        from aee.runtimes.executor_cli import ClaudeCodeCliRunner
        sig = inspect.signature(ClaudeCodeCliRunner.__init__)
        param = sig.parameters["max_turns"]
        self.assertEqual(param.default, 80,
                         "constructor default must be 80 (was 50)")

    def test_explicit_constructor_arg_overrides_default(self) -> None:
        # We can't run the CLI here, but we can confirm the value is
        # forwarded to the underlying ClaudeCodeProvider by inspecting
        # the provider's _max_turns attribute.
        from aee.runtimes.executor_cli import ClaudeCodeCliRunner
        runner = ClaudeCodeCliRunner(
            binary="/bin/true",  # never spawned in this test
            max_turns=30,
        )
        self.assertEqual(runner._provider._max_turns, 30,
                         "explicit constructor arg must win over default")

    def test_no_arg_uses_default_80(self) -> None:
        from aee.runtimes.executor_cli import ClaudeCodeCliRunner
        runner = ClaudeCodeCliRunner(binary="/bin/true")
        self.assertEqual(runner._provider._max_turns, 80,
                         "no-arg path must use the new default 80")


class TestFromConfigFallback(unittest.TestCase):
    """Group C — from_config() fallback when cfg has no max_turns key."""

    def test_from_config_fallback_is_80(self) -> None:
        from aee.runtimes.executor_cli import ClaudeCodeCliRunner
        # Empty cfg → falls through to the ``or 80`` fallback.
        runner = ClaudeCodeCliRunner.from_config({})
        self.assertEqual(runner._provider._max_turns, 80,
                         "from_config fallback must be 80 (was 50)")

    def test_from_config_explicit_value_wins_over_fallback(self) -> None:
        from aee.runtimes.executor_cli import ClaudeCodeCliRunner
        runner = ClaudeCodeCliRunner.from_config({"max_turns": 30})
        self.assertEqual(runner._provider._max_turns, 30,
                         "explicit cfg value must win over the 80 fallback")

    def test_from_config_reads_executor_json_value(self) -> None:
        # from_config with the real merged config (which reads
        # config/executor.json) should report 80 because we updated the
        # JSON file.
        from aee.runtimes.executor_config import load_executor_config
        from aee.runtimes.executor_cli import ClaudeCodeCliRunner
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AEE_EXECUTOR_MAX_TURNS", None)
            cfg = load_executor_config()
        runner = ClaudeCodeCliRunner.from_config(cfg)
        self.assertEqual(runner._provider._max_turns, 80,
                         "from_config(real_cfg) must read the file's 80")


class TestAppDispatchOverridePath(unittest.TestCase):
    """Group D — app.py per-request override path (body.max_turns).

    This mirrors the branch in ``app.py::create_executor_run``:

        if body.max_turns is not None:
            runner = ClaudeCodeCliRunner(..., max_turns=int(body.max_turns), ...)
        else:
            runner = ClaudeCodeCliRunner.from_config(cfg)

    We don't spin up the FastAPI app here; we just verify the
    branching logic by simulating both arms with the same factory
    call shape app.py uses.
    """

    def test_per_request_override_uses_body_value(self) -> None:
        from aee.runtimes.executor_cli import ClaudeCodeCliRunner
        body_max_turns = 30  # user explicitly asked for 30
        runner = ClaudeCodeCliRunner(
            binary="/bin/true",
            max_turns=int(body_max_turns),
        )
        self.assertEqual(runner._provider._max_turns, 30,
                         "per-request body.max_turns must be forwarded")

    def test_per_request_absent_uses_config_default(self) -> None:
        from aee.runtimes.executor_cli import ClaudeCodeCliRunner
        from aee.runtimes.executor_config import load_executor_config
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AEE_EXECUTOR_MAX_TURNS", None)
            cfg = load_executor_config()
        runner = ClaudeCodeCliRunner.from_config(cfg)
        self.assertEqual(runner._provider._max_turns, 80,
                         "absent body.max_turns must fall back to config 80")


class TestProviderConstructorUnchanged(unittest.TestCase):
    """Group E — ClaudeCodeProvider's own constructor default is intentionally 1.

    The provider's ``max_turns=1`` default is documented as the
    non-interactive default for the *provider* layer (used by the
    orchestrator path via ``claude_code_provider_shim``). The executor
    path (POST /runs/executor) always injects a value from the executor
    config (now 80). The Phase 1 work order says to change the
    *launcher/wrapper* default — that is the executor_cli layer, NOT
    the provider layer. Confirm the provider default is unchanged so
    we don't accidentally widen the surface.
    """

    def test_provider_constructor_default_is_still_1(self) -> None:
        import inspect
        from aee.adapters.claude_code_provider import ClaudeCodeProvider
        sig = inspect.signature(ClaudeCodeProvider.__init__)
        param = sig.parameters["max_turns"]
        self.assertEqual(param.default, 1,
                         "provider default must remain 1 — Phase 1 does not "
                         "touch the provider layer's documented non-interactive "
                         "default")


class TestOpenApiDescriptionDefault80(unittest.TestCase):
    """Group F — published OpenAPI description mentions default 80."""

    def test_gpt_openapi_description_says_80(self) -> None:
        path = REPO_ROOT / "gpt" / "aee_executor_openapi.json"
        self.assertTrue(path.exists())
        with path.open() as fh:
            data = json.load(fh)
        schema = data["components"]["schemas"]["ExecutorRunRequest"]
        desc = schema["properties"]["max_turns"]["description"]
        self.assertIn("default 80", desc,
                      "OpenAPI description must say 'default 80'")
        self.assertNotIn("default 50", desc,
                         "stale 'default 50' must be gone from OpenAPI")

    def test_e2e_evidence_openapi_description_says_80(self) -> None:
        path = REPO_ROOT / "AEE_GPT_E2E_EVIDENCE" / "gpt_aee_executor_openapi.json"
        self.assertTrue(path.exists())
        with path.open() as fh:
            data = json.load(fh)
        schema = data["components"]["schemas"]["ExecutorRunRequest"]
        desc = schema["properties"]["max_turns"]["description"]
        self.assertIn("default 80", desc)


if __name__ == "__main__":
    unittest.main(verbosity=2)