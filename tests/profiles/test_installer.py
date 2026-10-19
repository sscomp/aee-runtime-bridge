"""Canonical installer tests (work order AEE_V2_PHASE3, gates P3-G3/G4/G5).

Covers preflight failure modes, dry-run purity, apply transaction, idempotency,
unmanaged-collision refusal, rollback, uninstall, template substitution and
no-secret receipt guarantees. Traceability: F6, F2; C01/C02/C013.
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import install_aee_gateway as installer  # noqa: E402


class InstallerFixture(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='aee-inst-test-')
        base = Path(self.tmp)
        self.release = base / 'release'
        self.release.mkdir()
        gateway = ROOT / 'mcp_gateway.py'
        (self.release / 'mcp_gateway.py').write_text(gateway.read_text())
        py = base / 'fakepython'
        py.write_text('#!/bin/sh\nexit 0\n')
        py.chmod(0o755)
        self.python = str(py)
        self.env = base / 'gw.env'
        self.env.write_text('\n'.join([
            'AEE_MCP_HOST=127.0.0.1', 'AEE_MCP_PORT=18799',
            'AEE_MCP_SURFACE=restricted', 'AEE_MCP_REQUIRE_AUTH=true',
            'MCP_BRIDGE_API_KEY=test-fixture-key-value-000000000000',
            'AEE_DEPLOYMENT_PROFILE=os-native',
            f'A3_JOB_STORE_DIR={base}/state/jobs',
        ]) + '\n')
        self.env.chmod(0o600)
        self.jobs = base / 'state' / 'jobs'

    def _args(self, **over):
        args = installer.argparse.Namespace(
            profile='os-native', release_dir=str(self.release), python=self.python,
            env_path=str(self.env), port=18799, check=False, dry_run=True,
            apply=False, user_unit_dir=self.tmp + '/units', user_unit_name=None,
            state_dir=None, staging_dir=None, systemd_bin='/bin/false',
            enable=False, start=False, upgrade_authorized=False,
            rollback_last=False, uninstall=False,
            backup_dir=self.tmp + '/backups', receipt=None, unit_mode_user=True)
        for key, value in over.items():
            setattr(args, key, value)
        return args


class Preflight(InstallerFixture):

    def test_dry_run_preflight_ok(self):
        receipt, code = installer.apply_args(self._args())
        self.assertEqual((receipt['result'], code), ('OK', 0))
        self.assertEqual(receipt['mode'], 'dry-run')
        self.assertEqual(receipt['backups_dir'], None)

    def test_dry_run_writes_nothing(self):
        installer.apply_args(self._args())
        self.assertFalse(Path(self.tmp, 'units').exists())
        self.assertFalse(Path(self.tmp, 'staging').exists())
        self.assertFalse(self.jobs.exists())

    def test_world_readable_env_rejected(self):
        bad = Path(self.tmp) / 'bad.env'
        bad.write_text((ROOT / 'config' / 'profiles' / 'os-native.env.example').read_text())
        bad.chmod(0o644)
        receipt, code = installer.apply_args(self._args(dry_run=False, env_path=str(bad)))
        self.assertEqual(receipt['result'], 'PREFLIGHT_FAILED')
        self.assertTrue(any('0600' in e for e in receipt['errors']))

    def test_unmanaged_unit_collision_refused(self):
        units = Path(self.tmp) / 'units'
        units.mkdir()
        (units / 'aee-gateway.service').write_text('[Unit]\nnot ours\n')
        receipt, _ = installer.apply_args(self._args())
        self.assertEqual(receipt['result'], 'PREFLIGHT_FAILED')
        self.assertTrue(any('unmanaged' in e for e in receipt['errors']))

    def test_port_collision_refused(self):
        import socket
        blocker = socket.socket()
        blocker.bind(('127.0.0.1', 18799))
        blocker.listen(1)
        try:
            receipt, _ = installer.apply_args(self._args())
            self.assertEqual(receipt['result'], 'PREFLIGHT_FAILED')
            self.assertTrue(any('port' in e for e in receipt['errors']))
        finally:
            blocker.close()

    def test_missing_secret_placeholder_in_shipped_artifact_rejected(self):
        # apply over a staged env copy that still carries a placeholder secret
        staged = Path(self.tmp) / 'staged.env'
        staged.write_text(self.env.read_text().replace(
            'MCP_BRIDGE_API_KEY=test-fixture-key-value-000000000000',
            'MCP_BRIDGE_API_KEY=<provision-private-mcp-key>'))
        staged.chmod(0o600)
        receipt, _ = installer.apply_args(
            self._args(dry_run=False, env_path=str(staged)))
        self.assertEqual(receipt['result'], 'PREFLIGHT_FAILED')
        self.assertTrue(any('placeholder' in e for e in receipt['errors']))


class ApplyTransaction(InstallerFixture):

    def test_apply_writes_unit_sidecar_and_jobs_dir(self):
        receipt, code = installer.apply_args(self._args(dry_run=False, apply=True))
        self.assertEqual((receipt['result'], code), ('OK', 0))
        unit = Path(self.tmp) / 'units' / 'aee-gateway.service'
        self.assertTrue(unit.is_file())
        self.assertTrue(Path(str(unit) + '.aee-installer.json').is_file())
        self.assertTrue(self.jobs.is_dir())
        rendered = unit.read_text()
        self.assertNotIn('<RELEASE_DIR>', rendered)
        self.assertNotIn('<ENV_FILE>', rendered)
        self.assertIn(str(self.release), rendered)
        self.assertIn(str(self.env), rendered)
        self.assertIn('NoNewPrivileges=yes', rendered)
        self.assertIn('WantedBy=default.target', rendered)  # user-scope unit marker
        # No secret value may leak into the rendered unit (only the env path).
        self.assertNotIn('test-fixture-key-value-000000000000', rendered)

    def test_idempotent_rerun_changes_nothing(self):
        unit = Path(self.tmp) / 'units' / 'aee-gateway.service'
        first, _ = installer.apply_args(self._args(dry_run=False, apply=True))
        stat = unit.stat()
        second, _ = installer.apply_args(self._args(dry_run=False, apply=True))
        self.assertEqual(second['changed'], [])
        self.assertEqual(unit.stat().st_mtime_ns, stat.st_mtime_ns)

    def test_rollback_restores_prior_state(self):
        unit = Path(self.tmp) / 'units' / 'aee-gateway.service'
        installer.apply_args(self._args(dry_run=False, apply=True))
        self.env.write_text(self.env.read_text() + '\n# operator edit\n')
        installer.apply_args(self._args(dry_run=False, apply=True,
                                        upgrade_authorized=True))
        receipt, code = installer.rollback_last(self._args())
        self.assertEqual((receipt['result'], code), ('ROLLBACK_OK', 0))
        restored = Path(receipt['restored'][0]['restored']).read_text()
        self.assertNotIn('# operator edit', restored)

    def test_uninstall_removes_unit_sidecar_only(self):
        installer.apply_args(self._args(dry_run=False, apply=True))
        unit = Path(self.tmp) / 'units' / 'aee-gateway.service'
        receipt, _ = installer.uninstall(self._args(dry_run=False, apply=True))
        self.assertFalse(unit.exists())
        self.assertFalse(Path(str(unit) + '.aee-installer.json').exists())
        # Operator data (env, jobs) is never auto-removed.
        self.assertTrue(self.env.exists())
        self.assertTrue(self.jobs.exists())

    def test_receipt_contains_var_names_not_values(self):
        receipt, _ = installer.apply_args(self._args(dry_run=False, apply=True))
        blob = json.dumps(receipt)
        self.assertNotIn('test-fixture-key-value-000000000000', blob)
        self.assertIn('MCP_BRIDGE_API_KEY', receipt['env_keys'])


class RestrictedStaging(InstallerFixture):

    def _restricted_env(self):
        env = Path(self.tmp) / 'restricted.env'
        env.write_text('\n'.join([
            'AEE_MCP_HOST=127.0.0.1', 'AEE_MCP_PORT=18799',
            'AEE_MCP_SURFACE=restricted', 'AEE_MCP_REQUIRE_AUTH=true',
            'MCP_BRIDGE_API_KEY=test-fixture-key-value-000000000000',
            'AEE_DEPLOYMENT_PROFILE=restricted',
            'A3_DISPATCH_ALLOWED_ROOTS=/tmp/aee-restricted-work',
            'A3_JOB_STORE_DIR=%s/state/jobs' % self.tmp,
            'A3_CODEX_BIN=%s/runner-codex' % self.release,
            'A3_WORKSPACE_MANIFEST=%s/workspace-manifest.json' % self.release,
            'AEE_RUNTIME_MANIFEST=%s/deployment-manifest.json' % self.release,
            'AEE_BROKER_CONTROL=/run/aee-broker/control.sock',
        ]) + '\n')
        env.chmod(0o600)
        return env

    def test_restricted_stages_and_never_takes_privileges(self):
        args = self._args(dry_run=False, apply=True, profile='restricted',
                          env_path=str(self._restricted_env()),
                          staging_dir=self.tmp + '/staging', unit_mode_user=False)
        receipt, code = installer.apply_args(args)
        self.assertEqual(receipt['result'], 'OK')
        staged = Path(self.tmp) / 'staging' / 'etc-aee-gateway-restricted.env.example'
        self.assertTrue(staged.is_file())
        self.assertIn('ProtectHome=yes', installer._render_unit(
            args, Path(args.env_path), Path(self.tmp)))

    def test_restricted_refuses_user_scope_apply(self):
        args = self._args(dry_run=False, apply=True, profile='restricted',
                          env_path=str(self._restricted_env()),
                          staging_dir=self.tmp + '/staging', unit_mode_user=True)
        receipt, _ = installer.apply_args(args)
        self.assertEqual(receipt['result'], 'PREFLIGHT_FAILED')


if __name__ == '__main__':
    unittest.main()