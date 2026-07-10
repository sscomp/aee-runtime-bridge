"""AEE-5 runtime config loader — unit tests.

Pure tests; no DB. Build a YAML file in a tmp dir
and exercise `load_runtime_config()` +
`apply_runtime_config()`.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from aee.config import (
    RuntimeConfigError,
    apply_runtime_config,
    load_runtime_config,
)
from aee.runtimes.registry import RuntimeRegistry
from aee.runtimes.repository import InMemoryRuntimeRepository


_YAML_FULL = """
runtimes:
  auto_register_builtin: true
  default_runtime_id: aee-lightweight-local
  allow_unknown_health: true
  definitions:
    - runtime_id: r-shell-01
      runtime_type: shell
      display_name: Shell runtime
      version: "1.0.0"
      enabled: true
      endpoint: local
      capabilities:
        - task.shell
        - task.git
      labels:
        environment: sandbox
        trust_level: external
      limits:
        max_concurrency: 4
        timeout_seconds: 600
"""

_YAML_OVERRIDE_BUILTIN = """
runtimes:
  auto_register_builtin: true
  default_runtime_id: my-builtin
  definitions:
    - runtime_id: my-builtin
      runtime_type: aee_lightweight
      display_name: My renamed builtin
      capabilities:
        - task.shell
"""

_YAML_BAD = """
runtimes:
  definitions:
    - runtime_type: shell
"""


class TestLoadRuntimeConfig(unittest.TestCase):
    def test_returns_defaults_when_path_is_none(self):
        cfg = load_runtime_config(None)
        self.assertTrue(cfg["auto_register_builtin"])
        self.assertEqual(cfg["default_runtime_id"], "aee-lightweight-local")
        self.assertEqual(cfg["definitions"], [])

    def test_load_full(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(_YAML_FULL)
            tmp = f.name
        try:
            cfg = load_runtime_config(tmp)
            self.assertEqual(len(cfg["definitions"]), 1)
            d = cfg["definitions"][0]
            self.assertEqual(d.runtime_id, "r-shell-01")
            self.assertEqual(d.runtime_type, "shell")
            self.assertEqual(d.capabilities.to_list(), ["task.git", "task.shell"])
            self.assertEqual(d.labels, {"environment": "sandbox", "trust_level": "external"})
            self.assertEqual(d.limits.max_concurrency, 4)
            self.assertEqual(d.limits.timeout_seconds, 600)
        finally:
            os.unlink(tmp)

    def test_load_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_runtime_config("/nonexistent/path/config.yaml")

    def test_load_malformed_yaml_raises(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(_YAML_BAD)
            tmp = f.name
        try:
            with self.assertRaises(RuntimeConfigError):
                load_runtime_config(tmp)
        finally:
            os.unlink(tmp)

    def test_env_substitution(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(
                "runtimes:\n"
                "  definitions:\n"
                "    - runtime_id: ${MY_RUNTIME_ID}\n"
                "      runtime_type: shell\n"
                "      display_name: ${MY_RUNTIME_ID}\n"
                "      endpoint: ${MY_ENDPOINT}\n"
            )
            tmp = f.name
        try:
            env = {
                "MY_RUNTIME_ID": "r-from-env",
                "MY_ENDPOINT": "http://example.invalid",
            }
            cfg = load_runtime_config(tmp, env=env)
            d = cfg["definitions"][0]
            self.assertEqual(d.runtime_id, "r-from-env")
            self.assertEqual(d.endpoint, "http://example.invalid")
        finally:
            os.unlink(tmp)

    def test_env_undefined_raises(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(
                "runtimes:\n"
                "  definitions:\n"
                "    - runtime_id: ${UNDEFINED_VAR_XYZ}\n"
                "      runtime_type: shell\n"
            )
            tmp = f.name
        try:
            with self.assertRaises(RuntimeConfigError):
                load_runtime_config(tmp)
        finally:
            os.unlink(tmp)


class TestApplyRuntimeConfig(unittest.TestCase):
    def test_apply_registers_definitions(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(_YAML_FULL)
            tmp = f.name
        try:
            cfg = load_runtime_config(tmp)
            reg = RuntimeRegistry(InMemoryRuntimeRepository())
            summary = apply_runtime_config(cfg, reg)
            self.assertEqual(summary["builtin_registered"], 1)
            self.assertEqual(summary["definitions_registered"], 1)
            ids = {r.runtime_id for r in reg.list_runtimes()}
            self.assertIn("aee-lightweight-local", ids)
            self.assertIn("r-shell-01", ids)
        finally:
            os.unlink(tmp)

    def test_apply_uses_default_runtime_id(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(_YAML_OVERRIDE_BUILTIN)
            tmp = f.name
        try:
            cfg = load_runtime_config(tmp)
            reg = RuntimeRegistry(InMemoryRuntimeRepository())
            apply_runtime_config(cfg, reg)
            # The built-in is registered under the
            # configured id; the old id is NOT present.
            ids = {r.runtime_id for r in reg.list_runtimes()}
            self.assertIn("my-builtin", ids)
            self.assertNotIn("aee-lightweight-local", ids)
            d = reg.get_runtime("my-builtin")
            self.assertEqual(d.display_name, "My renamed builtin")
        finally:
            os.unlink(tmp)

    def test_apply_replace_overrides_existing(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(_YAML_FULL)
            tmp = f.name
        try:
            cfg = load_runtime_config(tmp)
            reg = RuntimeRegistry(InMemoryRuntimeRepository())
            apply_runtime_config(cfg, reg)
            # Re-apply with same config; ids should
            # remain the same and not duplicate.
            apply_runtime_config(cfg, reg)
            ids = {r.runtime_id for r in reg.list_runtimes()}
            self.assertEqual(len(ids), 2)
        finally:
            os.unlink(tmp)


if __name__ == "__main__":
    unittest.main()
