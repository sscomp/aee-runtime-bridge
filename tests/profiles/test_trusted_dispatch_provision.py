"""Trusted-dispatch provisioning tests (work order AEE_V2_PHASE3, gate P3-G4).

Covers: policy generation with runtime fail-closed validation, permissions,
no-secret leakage, revoke, explicit-only write/autonomous enablement.
Traceability: F1; C03/C04.
"""
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import provision_trusted_dispatch as prov  # noqa: E402


class Provisioning(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='aee-tdp-test-')
        self.policy_path = Path(self.tmp) / 'pol' / 'trusted-dispatch-policy.json'
        self._old_stdin = sys.stdin
        sys.stdin = io.StringIO('test-principal-bearer-value-0123456789abcdef\n')

    def tearDown(self):
        sys.stdin = self._old_stdin

    def _args(self, **over):
        args = prov.argparse.Namespace(
            policy_path=str(self.policy_path), principal='host-principal',
            key_env=None, key_stdin=True, agents='', set_agents='claude,codex',
            enable_write_autonomous=True, disable_write_autonomous=False,
            overwrite_invalid=False, revoke=[], dry_run=False)
        for key, value in over.items():
            setattr(args, key, value)
        return args

    def test_apply_writes_private_valid_policy(self):
        result = prov.build_policy({}, self._args(), 'f' * 64)
        self.assertEqual(result['schema'], 'aee-secure-mcp-trusted-dispatch-v1')
        self.assertEqual(result['principals'], {'f' * 64: 'host-principal'})
        self.assertEqual(result['agents'], ['claude', 'codex'])
        self.assertTrue(result['enabled'])

    def test_write_autonomous_requires_explicit_optin(self):
        result = prov.build_policy({}, self._args(enable_write_autonomous=False),
                                   'f' * 64)
        self.assertFalse(result['enabled'])

    def test_rerun_preserves_an_explicit_optin(self):
        # P4 F-9: --enable-write-autonomous is a first-time flag; a later
        # re-run without it preserves the earlier explicit opt-in (and the
        # docs say exactly that — no silent disable, no silent enable).
        existing = {'principals': {'a' * 64: 'old'}, 'agents': ['claude'],
                    'enabled': True}
        result = prov.build_policy(existing, self._args(enable_write_autonomous=False),
                                   'c' * 64)
        self.assertTrue(result['enabled'])
        self.assertEqual(result['principals'],
                         {'a' * 64: 'old', 'c' * 64: 'host-principal'})
        with self.assertRaises(SystemExit):
            prov.build_policy(existing,
                              self._args(disable_write_autonomous=True),
                              'c' * 64)

    def test_invalid_existing_policy_blocksWithout_overwrite(self):
        # P4 F-10: a corrupt/unknown-schema policy must not be silently
        # rebuilt over (its principals would be dropped); either refused or
        # replaced only with --overwrite-invalid.
        self.policy_path.parent.mkdir(parents=True)
        self.policy_path.write_text('{not json')
        with self.assertRaises(SystemExit):
            prov._load_existing(self.policy_path)
        self.assertEqual(prov._load_existing(self.policy_path,
                                             overwrite_invalid=True), {})
        self.policy_path.write_text(json.dumps(
            {'schema': 'aee-secure-mcp-trusted-dispatch-v0'}))
        with self.assertRaises(SystemExit):
            prov._load_existing(self.policy_path)
        self.assertEqual(prov._load_existing(self.policy_path,
                                             overwrite_invalid=True), {})

    def test_revoke_removes_only_named_fingerprint(self):
        existing = {'principals': {'a' * 64: 'old', 'b' * 64: 'other'},
                    'agents': ['claude'], 'enabled': True}
        rebuilt = prov.build_policy(existing, self._args(revoke=['a' * 64]),
                                    'c' * 64)
        self.assertNotIn('a' * 64, rebuilt['principals'])
        self.assertIn('b' * 64, rebuilt['principals'])
        self.assertIn('c' * 64, rebuilt['principals'])

    def test_unknown_agent_refused(self):
        with self.assertRaises(SystemExit):
            prov.build_policy({}, self._args(set_agents='claude,totally-new'),
                              'f' * 64)

    def test_full_cli_apply_and_runtime_validation(self):
        out = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = out
        try:
            code = None
            argv = sys.argv
            sys.argv = ['provision', '--policy-path', str(self.policy_path),
                        '--principal', 'host-principal', '--key-stdin',
                        '--set-agents', 'claude,codex,pi',
                        '--enable-write-autonomous']
            try:
                prov.main()
            finally:
                sys.argv = argv
        finally:
            sys.stdout = old_stdout
        receipt = json.loads(out.getvalue())
        self.assertEqual(receipt['runtime_validation'], 'PASS')
        self.assertEqual(receipt['mode'], 'apply')
        blob = out.getvalue()
        # Fingerprints are print-safe by design; the raw bearer is not.
        expected_fp = hashlib.sha256(
            b'test-principal-bearer-value-0123456789abcdef').hexdigest()
        self.assertIn(expected_fp, blob)
        self.assertNotIn('test-principal-bearer-value', blob)
        self.assertEqual(oct(self.policy_path.stat().st_mode & 0o777), '0o600')
        self.assertEqual(oct(self.policy_path.parent.stat().st_mode & 0o777), '0o700')

    def test_runtime_fail_closed_on_garbage_policy(self):
        self.policy_path.parent.mkdir(parents=True, exist_ok=True)
        self.policy_path.write_text('{"schema": "wrong"}')
        os.environ['AEE_TRUSTED_DISPATCH_POLICY'] = str(self.policy_path)
        import importlib
        td = importlib.import_module('aee.mcp_runtime.trusted_dispatch')
        with self.assertRaises(td.JobError):
            td.TrustedDispatch.configured().policy()


if __name__ == '__main__':
    unittest.main()