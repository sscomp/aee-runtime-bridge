"""Codex discovery fallback to the generic adapter registry (no broker pin).

On deployments without a reviewed Stage-2 broker pin (A3_CODEX_BIN absent or
unavailable), dispatch still runs codex through the generic adapter registry
(job_adapters.resolve_adapter). The discovery surface must report that same
fallback instead of swallowing an installed, dispatchable adapter behind
AGENT_UNAVAILABLE — while non-AGENT_UNAVAILABLE broker findings (identity
drift, contract violations) stay visible. Offline fixtures only.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_C = Path(__file__).parent / "fixtures/codex_probe.c"


class CodexDiscoveryFallback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="aee-codex-fallback-fixture-")
        cls.binary = Path(cls.build.name) / "codex"
        subprocess.run(["/usr/bin/gcc", "-Wall", "-Wextra", "-O2", str(FIXTURE_C),
                        "-o", str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def load(self, environment):
        name = "aee_codex_discovery_fallback_" + str(len(sys.modules))
        spec = importlib.util.spec_from_file_location(name, ROOT / "mcp_gateway.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        with patch.dict(os.environ, environment, clear=True):
            spec.loader.exec_module(module)
        return module

    @staticmethod
    def base_env(overrides=None):
        # A3_CODEX_BIN points at a path that cannot exist: the broker identity
        # always fails closed with AGENT_UNAVAILABLE, never a contract error.
        env = {"A3_JOB_STORE_DIR": tempfile.mkdtemp(prefix="aee-fallback-jobs-"),
               "A3_CODEX_BIN": "/nonexistent/pinned-codex",
               "AEE_MCP_PORT": "8791", "AEE_MCP_SURFACE": "restricted",
               "AEE_MCP_REQUIRE_AUTH": "true",
               "MCP_BRIDGE_API_KEY": "FALLBACK_SYNTHETIC_AUTH"}
        env.update(overrides or {})
        return env

    def test_registry_fallback_reports_installed_adapter(self):
        gateway = self.load(self.base_env({"A3_ADAPTER_CODEX_BIN": str(self.binary)}))
        with patch.dict(os.environ, {"A3_ADAPTER_CODEX_BIN": str(self.binary)}):
            discovered = gateway.discover_agent("codex")
        self.assertTrue(discovered["installed"])
        self.assertTrue(discovered["available"])
        self.assertEqual(discovered["executable"], str(self.binary))
        self.assertEqual(discovered["version"], "codex-cli 0.0.0-fixture")

    def test_registry_fallback_reports_absent_adapter(self):
        gateway = self.load(self.base_env())
        # Constrain PATH lookup so the test is deterministic on hosts that do
        # (or do not) have a real codex on PATH: no override, no PATH hit.
        with patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            discovered = gateway.discover_agent("codex")
        self.assertFalse(discovered["installed"])
        self.assertFalse(discovered["available"])
        self.assertEqual(discovered["error_code"], "ADAPTER_NOT_INSTALLED")
        self.assertIsNone(discovered["executable"])