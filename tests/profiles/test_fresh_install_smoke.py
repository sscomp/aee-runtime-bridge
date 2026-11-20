"""Offline CLI coverage for operator provisioning on both gateway profiles."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.validate_profiles import EXAMPLES, ROOT


class FreshInstallSmoke(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.env_path = Path(directory.name) / 'gateway.env'
        self.store_path = Path(directory.name) / 'jobs'
        self.fixture_value = 'synthetic-value-never-printed'

    def render(self, profile, prefix_line='', *, template=None, extra=''):
        example = (template or EXAMPLES[profile]).read_text()
        self.assertIn('AEE_JOB_ID_PREFIX=<operator-job-id-prefix>', example)
        lines = [line for line in example.splitlines()
                 if line and not line.startswith(('#', 'AEE_JOB_ID_PREFIX='))]
        fields = dict(line.split('=', 1) for line in lines)
        fields['MCP_BRIDGE_API_KEY'] = self.fixture_value
        fields['A3_JOB_STORE_DIR'] = str(self.store_path)
        if profile == 'restricted':
            fields.update(A3_DISPATCH_ALLOWED_ROOTS='/fixture/work',
                          A3_CODEX_BIN='/fixture/runner/codex',
                          A3_WORKSPACE_MANIFEST='/fixture/workspace-manifest.json',
                          AEE_RUNTIME_MANIFEST='/fixture/deployment-manifest.json')
        data = ''.join(f'{key}={value}\n' for key, value in fields.items())
        self.env_path.touch(mode=0o600)
        self.env_path.write_text(data + prefix_line + extra)

    def invoke(self, *args):
        process = subprocess.run(
            [sys.executable, '-B', str(ROOT / 'scripts' / 'validate_profiles.py'), *args],
            env={'AEE_JOB_ID_PREFIX': 'A3JOB'}, capture_output=True, text=True,
            check=False,
        )
        self.assertNotIn(self.fixture_value, process.stdout + process.stderr)
        self.assertFalse(self.store_path.exists())
        return process

    def verify(self, profile, expected, code, reason=None):
        args = ['--profile', profile, '--env-path', str(self.env_path)]
        if expected is not None:
            args += ['--expected-job-id-prefix', expected]
        process = self.invoke(*args)
        self.assertEqual(process.stderr, '')
        self.assertEqual(process.returncode, code, process.stdout)
        receipt = json.loads(process.stdout)
        self.assertEqual(receipt['result'], 'PASS' if code == 0 else 'FAIL')
        if reason:
            self.assertIn(reason, ' '.join(receipt['errors']))

    def test_explicit_approved_prefixes_for_each_profile(self):
        for profile in EXAMPLES:
            for prefix in ('M2JOB', 'A2JOB', 'A3JOB', 'N2JOB'):
                with self.subTest(profile=profile, prefix=prefix):
                    self.render(profile, f'AEE_JOB_ID_PREFIX="{prefix}"\n')
                    self.verify(profile, prefix, 0)

    def test_restricted_bootstrap_template(self):
        template = ROOT / 'config' / 'p2c' / 'gateway-restricted.env.example'
        self.render('restricted', 'AEE_JOB_ID_PREFIX=N2JOB\n', template=template)
        self.verify('restricted', 'N2JOB', 0)

    def test_missing_prefix_is_allowed_only_without_smoke(self):
        for profile in EXAMPLES:
            with self.subTest(profile=profile):
                self.render(profile)
                self.verify(profile, None, 0)
                self.verify(profile, 'A3JOB', 1, 'explicitly provisioned')

    def test_rejects_missing_invalid_placeholder_or_mismatched_config(self):
        cases = [
            ('AEE_JOB_ID_PREFIX=\n', 'M2JOB', 'invalid'),
            ('AEE_JOB_ID_PREFIX=m2job\n', 'M2JOB', 'invalid'),
            ('AEE_JOB_ID_PREFIX=" M2JOB"\n', 'M2JOB', 'invalid'),
            ('AEE_JOB_ID_PREFIX="<operator-job-id-prefix>"\n', 'M2JOB', 'placeholder'),
            ('AEE_JOB_ID_PREFIX=A3JOB\n', 'M2JOB', 'does not match'),
            ('AEE_JOB_ID_PREFIX=M2JOB\nAEE_JOB_ID_PREFIX=N2JOB\n', 'M2JOB', 'does not match'),
            ('AEE_JOB_ID_PREFIX=M2JOB\n', '<expected>', 'expected job ID prefix is invalid'),
            ('export AEE_JOB_ID_PREFIX=M2JOB\n', 'M2JOB', 'single-line'),
            ('OTHER=value\\\nAEE_JOB_ID_PREFIX=M2JOB\n', 'M2JOB', 'single-line'),
            ('# comment\\\nAEE_JOB_ID_PREFIX=M2JOB\n', 'M2JOB', 'single-line'),
        ]
        for profile in EXAMPLES:
            with self.subTest(profile=profile, case='missing env file'):
                self.env_path = self.env_path.with_name(f'{profile}.env')
                self.verify(profile, 'M2JOB', 1, 'could not be read')
            for index, (line, expected, reason) in enumerate(cases):
                with self.subTest(profile=profile, case=index):
                    self.render(profile, line)
                    self.verify(profile, expected, 1, reason)

    def test_mode_and_failure_output(self):
        for profile in EXAMPLES:
            with self.subTest(profile=profile):
                self.render(profile, 'AEE_JOB_ID_PREFIX=M2JOB\n')
                self.env_path.chmod(0o644)
                self.verify(profile, 'M2JOB', 1, '0600')
                self.env_path.chmod(0o600)
                self.render(profile, 'AEE_JOB_ID_PREFIX=M2JOB\n',
                            extra=f'AEE_MCP_REQUIRE_AUTH={self.fixture_value}\n')
                self.verify(profile, 'M2JOB', 1, 'profile validation')
                self.render(profile, 'AEE_JOB_ID_PREFIX=M2JOB\n', extra=self.fixture_value)
                self.verify(profile, 'M2JOB', 1, 'single-line')

    def test_smoke_requires_both_profile_and_env_path(self):
        for args in (['--profile', 'os-native'], ['--env-path', str(self.env_path)]):
            with self.subTest(args=args):
                process = self.invoke('--expected-job-id-prefix', 'M2JOB', *args)
                self.assertEqual(process.returncode, 2)
                self.assertIn('requires --env-path and --profile', process.stderr)
