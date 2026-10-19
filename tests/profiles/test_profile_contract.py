"""Profile contract tests (work order AEE_V2_PHASE3, gate P3-G2).

Covers: schema self-consistency, example validation, operator-env semantics,
secret placeholder rules, forbidden cross-profile artifacts, restricted
confinement requirement. Traceability: F1/F4/F8; C03/C08.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.validate_profiles import (  # noqa: E402
    load_schema, parse_env, validate_env_path, validate_examples,
    validate_fields, EXAMPLES)


class SchemaSelfConsistency(unittest.TestCase):

    def test_schema_version_pinned(self):
        schema = load_schema()
        self.assertEqual(schema['schema'], 'aee-deployment-profile-schema-v1')

    def test_exactly_one_default(self):
        defaults = [p for p, s in load_schema()['profiles'].items() if s.get('default')]
        self.assertEqual(defaults, ['os-native'])

    def test_restricted_is_explicit_opt_in(self):
        self.assertFalse(load_schema()['profiles']['restricted'].get('default'))


class ShippedExamples(unittest.TestCase):

    def test_all_examples_pass(self):
        self.assertEqual(validate_examples(), [])

    def test_os_native_example_has_no_restricted_artifacts(self):
        fields = parse_env(EXAMPLES['os-native'].read_text())
        for name in ('A3_WORKSPACE_MANIFEST', 'AEE_RUNTIME_MANIFEST', 'AEE_BROKER_CONTROL'):
            self.assertNotIn(name, fields)

    def test_os_native_example_does_not_enable_allowlist(self):
        fields = parse_env(EXAMPLES['os-native'].read_text())
        self.assertNotIn('A3_DISPATCH_ALLOWED_ROOTS', fields)

    def test_restricted_example_requires_confinement(self):
        fields = parse_env(EXAMPLES['restricted'].read_text())
        self.assertIn('A3_DISPATCH_ALLOWED_ROOTS', fields)

    def test_secret_values_never_ship(self):
        for path in EXAMPLES.values():
            text = path.read_text()
            self.assertNotIn('sk-', text)
            self.assertNotIn('Bearer ', text)


class OperatorEnvSemantics(unittest.TestCase):

    def _write(self, tmp, text):
        path = tmp / 'gw.env'
        path.write_text(text)
        path.chmod(0o600)
        return path

    def test_real_values_accepted_for_operator_env(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = self._write(Path(td), '\n'.join([
                'AEE_MCP_HOST=127.0.0.1', 'AEE_MCP_PORT=8791',
                'AEE_MCP_SURFACE=restricted', 'AEE_MCP_REQUIRE_AUTH=true',
                'MCP_BRIDGE_API_KEY=real-operator-value-0000000000000000',
                'AEE_DEPLOYMENT_PROFILE=os-native',
                'A3_JOB_STORE_DIR=/srv/aee/jobs',
            ]) + '\n')
            self.assertEqual(validate_env_path(path, 'os-native'), [])

    def test_placeholder_secret_rejected_for_operator_env(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = self._write(Path(td), '\n'.join([
                'AEE_MCP_HOST=127.0.0.1', 'AEE_MCP_PORT=8791',
                'AEE_MCP_SURFACE=restricted', 'AEE_MCP_REQUIRE_AUTH=true',
                'MCP_BRIDGE_API_KEY=<provision-private-mcp-key>',
                'AEE_DEPLOYMENT_PROFILE=os-native',
                'A3_JOB_STORE_DIR=/srv/aee/jobs',
            ]) + '\n')
            errors = validate_env_path(path, 'os-native')
            self.assertTrue(any('MCP_BRIDGE_API_KEY' in e for e in errors))

    def test_missing_required_key_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = self._write(Path(td),
                               'AEE_MCP_HOST=127.0.0.1\nAEE_MCP_PORT=8791\n')
            errors = validate_env_path(path, 'os-native')
            self.assertTrue(any('missing required key' in e for e in errors))

    def test_boundary_change_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = self._write(Path(td), '\n'.join([
                'AEE_MCP_HOST=0.0.0.0',  # loopback boundary violation
                'AEE_MCP_PORT=8791', 'AEE_MCP_SURFACE=restricted',
                'AEE_MCP_REQUIRE_AUTH=true', 'MCP_BRIDGE_API_KEY=x' * 20,
                'AEE_DEPLOYMENT_PROFILE=something-else',
                'A3_JOB_STORE_DIR=/srv/aee/jobs',
            ]) + '\n')
            errors = validate_env_path(path, 'os-native')
            self.assertTrue(any('boundary changed' in e for e in errors))

    def test_restricted_operator_env_requires_allowlist(self):
        base = ['AEE_MCP_HOST=127.0.0.1', 'AEE_MCP_PORT=8791',
                'AEE_MCP_SURFACE=restricted', 'AEE_MCP_REQUIRE_AUTH=true',
                'MCP_BRIDGE_API_KEY=x' * 20, 'AEE_DEPLOYMENT_PROFILE=restricted',
                'A3_JOB_STORE_DIR=/srv/aee/jobs',
                'A3_CODEX_BIN=/srv/aee/runner/codex',
                'A3_WORKSPACE_MANIFEST=/srv/aee/workspace-manifest.json',
                'AEE_RUNTIME_MANIFEST=/srv/aee/deployment-manifest.json',
                'AEE_BROKER_CONTROL=/run/aee-broker/control.sock']
        with tempfile.TemporaryDirectory() as td:
            path = self._write(Path(td), '\n'.join(base) + '\n')
            errors = validate_env_path(path, 'restricted')
            self.assertTrue(any('A3_DISPATCH_ALLOWED_ROOTS' in e for e in errors))
            path.write_text('\n'.join(base + ['A3_DISPATCH_ALLOWED_ROOTS=/srv/aee/work']) + '\n')
            self.assertEqual(validate_env_path(path, 'restricted'), [])


if __name__ == '__main__':
    unittest.main()