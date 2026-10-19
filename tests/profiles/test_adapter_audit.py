"""Adapter audit + deployment-profile diagnostics tests (gates P3-G4/G5).

Covers service-identity adapter resolution semantics (env override precedence,
strict missing required), and the additive aee_status gateway diagnostic field
(deployment profile). Traceability: F2/F8; C05–C07, C01/C02.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import audit_adapters  # noqa: E402


class AdapterAudit(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='aee-audit-test-')
        self.bin_dir = Path(self.tmp) / 'bin'
        self.bin_dir.mkdir()
        self.old_path = os.environ.get('PATH')
        self.old_overrides = {k: os.environ.get(k)
                              for k in ('A3_ADAPTER_CLAUDE_BIN', 'A3_ADAPTER_PI_BIN')}
        stub = self.bin_dir / 'stub-agent'
        stub.write_text('#!/bin/sh\necho stub-version-1\n')
        stub.chmod(0o755)

    def tearDown(self):
        if self.old_path is not None:
            os.environ['PATH'] = self.old_path
        for k, v in self.old_overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _audit(self, strict=False, expect=''):
        result, code = audit_adapters.audit(
            expect={a for a in expect.split(',') if a}, strict=strict)
        return result, code

    def test_report_shape_and_resolution_fields(self):
        os.environ['PATH'] = f"{self.bin_dir}:" + (os.environ.get('PATH') or '')
        result, code = self._audit()
        self.assertEqual(code, 0)
        agents = {e['agent'] for e in result['adapters']}
        self.assertIn('codex', agents)
        for entry in result['adapters']:
            self.assertTrue(('resolved' in entry and 'executable' in entry)
                            or ('error_code' in entry and 'reason' in entry))

    def test_override_precedence_reported(self):
        override = str(self.bin_dir / 'override-claude')
        (self.bin_dir / 'override-claude').write_text('#!/bin/sh\nexit 0\n')
        os.chmod(override, 0o755)
        os.environ['A3_ADAPTER_CLAUDE_BIN'] = override
        result, _ = self._audit()
        entry = next(e for e in result['adapters'] if e['agent'] == 'claude')
        self.assertTrue(entry['resolved'])
        self.assertEqual(entry['executable'], override)

    def test_strict_missing_required_fails_closed(self):
        os.environ['PATH'] = '/nonexistent-adapters'
        result, code = self._audit(strict=True, expect='claude,pi')
        self.assertEqual(code, 1)
        self.assertIn('claude', result['missing_required'])
        self.assertIn('pi', result['missing_required'])

    def test_no_secret_printed(self):
        os.environ['PATH'] = f"{self.bin_dir}:" + (os.environ.get('PATH') or '')
        os.environ['MCP_BRIDGE_API_KEY'] = 'secret-value-0000000000000000'
        result, _ = self._audit()
        blob = json.dumps(result)
        self.assertNotIn('secret-value-0000000000000000', blob)
        del os.environ['MCP_BRIDGE_API_KEY']


class DeploymentProfileDiagnostics(unittest.TestCase):
    """aee_status gateway diagnostics: additive, validated, import-time label.

    The profile label is read once at module import (deployment configuration,
    not per-request state), so each case is evaluated in a clean subprocess
    with the candidate label in the environment.
    """

    def _eval(self, label):
        code = 'import mcp_gateway as g; print(g.AEE_DEPLOYMENT_PROFILE)'
        env = {**os.environ, 'AEE_DEPLOYMENT_PROFILE': label}
        proc = subprocess.run([sys.executable, '-c', code], capture_output=True,
                              text=True, env=env, cwd=str(ROOT),
                              timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
        return proc.stdout.strip()

    def test_valid_label_kept(self):
        self.assertEqual(self._eval('os-native'), 'os-native')
        self.assertEqual(self._eval('restricted'), 'restricted')

    def test_invalid_label_degrades_to_none(self):
        self.assertEqual(self._eval('bad value!'), 'None')
        self.assertEqual(self._eval('x' * 40), 'None')
        self.assertEqual(self._eval(''), 'None')

if __name__ == '__main__':
    unittest.main()