"""E2E-01 D1 minimum exception instrumentation.

Focused behavior, redaction, and classification tests for the generic
non-``JobError`` exception path around ``execute_codex`` in the gateway
(``run_codex_job``). Fixture-only environment; no production access.
Assertions inspect captured journal output and the persisted public record,
not source text.
"""
import hashlib
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aee.mcp_runtime.profiles import resolve_codex
from aee.mcp_runtime.provider_contract import contract_record
from aee.mcp_runtime.resource_policy import resource_record
from aee.mcp_runtime.sandbox_policy import sandbox_record, FAILURE_REVISION
from aee.mcp_runtime.store import JobError

ROOT = Path(__file__).resolve().parents[2]

# Public failure contract of the generic non-JobError branch. Byte/field
# compatible with the schema observed on failed E2E job
# A3JOB-20261012-d70d315b073eb5dcb1b3f61bd19c0b74.
GENERIC_FAILURE_STATUS = "failed"
GENERIC_FAILURE_CODE = "EXECUTION_FAILED"
GENERIC_FAILURE_MESSAGE = "Isolated executor failed"


class _JournalCapture(logging.Handler):
    """Records formatted journal lines emitted by the gateway logger."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class E2E01ExceptionDiagnostics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='aee-e2e01-d1-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.binary = self.root / 'codex'
        self.source = self.root / 'source'
        self.source.mkdir()
        subprocess.run(['/usr/bin/gcc', '-O2',
                        str(ROOT / 'tests/mcp/fixtures/codex_probe.c'),
                        '-o', str(self.binary)], check=True)
        companion = self.root / 'codex-code-mode-host'
        companion.write_bytes(self.binary.read_bytes())
        companion.chmod(0o755)
        identity = resolve_codex(str(self.binary))
        self.policy = {'schema': 1, 'config_schema': 'aee-p2c-1', 'source_commit': 'a' * 40,
                       'execution_profile': 'codex-readonly-high',
                       'egress': 'per-job-uds-responses-broker',
                       'codex': {'path': str(self.binary), 'version': identity.version,
                                 'sha256': identity.sha256,
                                 'code_mode_host': {'path': str(companion),
                                                    'sha256': hashlib.sha256(companion.read_bytes()).hexdigest()}},
                       'native_provider_compatibility': 'VERIFIED',
                       'deployment_status': 'APPROVED_STAGE2',
                       'operator_approval_id': 'synthetic-fixture-only'}
        self.policy['provider_contract'] = contract_record()
        self.policy['native_tool_runtime_compatibility'] = 'VERIFIED'
        self.policy['resource_containment'] = 'VERIFIED'
        self.policy['resource_policy_revision'] = resource_record()
        self.policy.update(sandbox_policy_revision=sandbox_record(),
                           job_failure_semantics='VERIFIED',
                           job_failure_contract=FAILURE_REVISION)
        containment = patch('aee.mcp_runtime.runtime.verify_current_containment',
                            return_value={})
        containment.start()
        self.addCleanup(containment.stop)
        pin = patch('aee.mcp_runtime.runtime.matches_reviewed_executor',
                    return_value=True)
        pin.start()
        self.addCleanup(pin.stop)
        self.manifest = self.root / 'deployment.json'
        self.manifest.write_text(json.dumps(self.policy))
        workspace = self.root / 'workspace.json'
        workspace.write_text('{}')
        env = {'AEE_RUNTIME_MANIFEST': str(self.manifest),
               'AEE_BROKER_CONTROL': str(self.root / 'control.sock'),
               'A3_JOB_STORE_DIR': str(self.root / 'jobs'),
               'A3_CODEX_BIN': str(self.binary),
               'A3_WORKSPACE_MANIFEST': str(workspace),
               'A3_DISPATCH_ALLOWED_ROOTS': str(self.source),
               'AEE_MCP_PORT': '8791', 'AEE_MCP_SURFACE': 'restricted',
               'MCP_BRIDGE_API_KEY': 'fixture-only-auth',
               'AEE_MCP_REQUIRE_AUTH': 'true'}
        self.environment = patch.dict(os.environ, env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        name = 'aee_e2e01_d1_fixture_' + str(len(sys.modules))
        spec = importlib.util.spec_from_file_location(name, ROOT / 'mcp_gateway.py')
        self.gateway = importlib.util.module_from_spec(spec)
        sys.modules[name] = self.gateway
        self.addCleanup(sys.modules.pop, name, None)
        spec.loader.exec_module(self.gateway)
        self.addCleanup(self.gateway.JOB_STORE.close)
        self.journal = _JournalCapture()
        self.gateway.log.addHandler(self.journal)
        self.addCleanup(self.gateway.log.removeHandler, self.journal)

    def dispatch(self):
        with patch.object(self.gateway, '_start_job_worker'):
            return self.gateway.dispatch_job('codex', 'synthetic inspection',
                                             str(self.source), 'read_only')

    def run_with_exception(self, exc):
        """Dispatch, then run the worker with execute_codex raising ``exc``."""
        job = self.dispatch()
        broker_calls = []

        def broker(path, action, job_id):
            broker_calls.append((action, job_id))
            return {'socket': str(self.root / 'jobs' / (job_id + '.sock')), 'ok': True}

        before = len(self.journal.lines)
        before_record = self.gateway.JOB_STORE.get(job['job_id'])
        with patch.object(self.gateway, 'broker_control', side_effect=broker), \
                patch.object(self.gateway, 'execute_codex', side_effect=exc):
            self.gateway.run_codex_job(job['job_id'], 'synthetic inspection',
                                       str(self.source))
        record = self.gateway.JOB_STORE.get(job['job_id'])
        return {
            'job': job,
            'new_lines': self.journal.lines[before:],
            'before_record': before_record,
            'record': record,
            'broker_calls': broker_calls,
        }

    def assert_generic_public_contract(self, outcome):
        """The generic failure path must preserve byte/field public semantics."""
        record = outcome['record']
        self.assertEqual(record['status'], GENERIC_FAILURE_STATUS)
        self.assertEqual(record['error_code'], GENERIC_FAILURE_CODE)
        self.assertEqual(record['error'], GENERIC_FAILURE_MESSAGE)
        self.assertIsNone(record.get('exit_code'))
        self.assertIsNone(record.get('log_excerpt'))
        # No schema drift beyond the pre-failure field set: the D1 patch adds
        # journal-only diagnostics and must not persist any new field.
        self.assertEqual(sorted(record), sorted(outcome['before_record']))
        # Lifecycle unchanged: slot released, broker lease revoked.
        self.assertEqual(outcome['broker_calls'],
                         [('lease', outcome['job']['job_id']),
                          ('revoke', outcome['job']['job_id'])])

    def assert_journal_failure_line(self, outcome, exc_type, exc_errno):
        """Exactly one new journal line, in the fixed D1 format, nothing else."""
        failure_lines = [line for line in outcome['new_lines']
                         if 'event=job_failed' in line]
        self.assertEqual(len(failure_lines), 1)
        expected = (f"event=job_failed job_id={outcome['job']['job_id']} "
                    f"error_code=EXECUTION_FAILED exc_type={exc_type} "
                    f"exc_errno={exc_errno}")
        self.assertEqual(failure_lines[0], expected)

    def assert_no_sensitive_payload(self, outcome, *secrets):
        """Journal and persisted record must not leak the injected payloads."""
        combined = '\n'.join(outcome['new_lines']) + '\n' + json.dumps(outcome['record'])
        for secret in secrets:
            self.assertNotIn(secret, combined)

    def test_runtime_error_logs_bounded_class_name_only(self):
        outcome = self.run_with_exception(
            RuntimeError('SECRET-MARKER-runtime <script>alert(1)</script>'))
        self.assert_journal_failure_line(outcome, 'RuntimeError', 'none')
        self.assert_generic_public_contract(outcome)
        self.assert_no_sensitive_payload(
            outcome, 'SECRET-MARKER-runtime', '<script>', 'Traceback')

    def test_oserror_logs_only_sanitized_errno(self):
        outcome = self.run_with_exception(OSError(24, 'SECRET-MARKER-oserror'))
        self.assert_journal_failure_line(outcome, 'OSError', '24')
        self.assert_generic_public_contract(outcome)
        self.assert_no_sensitive_payload(outcome, 'SECRET-MARKER-oserror',
                                         'Too many open files')

    def test_hostile_class_name_and_noninteger_errno_fall_back(self):
        class Hostile(Exception):
            pass
        Hostile.__name__ = '<script>alert(1)</script> hostile name'
        hostile = Hostile('SECRET-MARKER-hostile')
        hostile.errno = '24; rm -rf /'
        outcome = self.run_with_exception(hostile)
        self.assert_journal_failure_line(outcome, 'Unknown', 'none')
        self.assert_generic_public_contract(outcome)
        self.assert_no_sensitive_payload(outcome, 'SECRET-MARKER-hostile',
                                         'rm -rf', '<script>', 'hostile name')

    def test_boolean_and_out_of_range_errno_sentinel(self):
        class BoolErrno(Exception):
            errno = True
        class HugeErrno(Exception):
            errno = 65536
        class NegativeErrno(Exception):
            errno = -1
        for exc in (BoolErrno('SECRET-MARKER-bool'),
                    HugeErrno('SECRET-MARKER-huge'),
                    NegativeErrno('SECRET-MARKER-neg')):
            with self.subTest(exc_type=type(exc).__name__):
                outcome = self.run_with_exception(exc)
                self.assert_journal_failure_line(outcome, type(exc).__name__, 'none')
                self.assert_generic_public_contract(outcome)
                self.assert_no_sensitive_payload(outcome, 'SECRET-MARKER')

    def test_errno_property_raising_uses_fixed_fallback(self):
        class RaisingErrno(Exception):
            @property
            def errno(self):
                raise RuntimeError('SECRET-MARKER-errno-getter')
        outcome = self.run_with_exception(RaisingErrno('SECRET-MARKER-raising'))
        self.assert_journal_failure_line(outcome, 'RaisingErrno', 'none')
        self.assert_generic_public_contract(outcome)
        self.assert_no_sensitive_payload(outcome, 'SECRET-MARKER-errno-getter',
                                         'SECRET-MARKER-raising')

    def test_classified_job_error_path_is_unchanged(self):
        outcome = self.run_with_exception(
            JobError('EXECUTION_FAILED', 'Synthetic fixture failure'))
        self.assertEqual(outcome['record']['status'], 'failed')
        self.assertEqual(outcome['record']['error_code'], 'EXECUTION_FAILED')
        self.assertEqual(outcome['record']['error'], 'Synthetic fixture failure')
        failure_lines = [line for line in outcome['new_lines']
                         if 'event=job_failed' in line]
        self.assertEqual(len(failure_lines), 1)
        # Classified path keeps its original fixed format: no D1 fields.
        self.assertIn('error_code=EXECUTION_FAILED', failure_lines[0])
        self.assertNotIn('exc_type=', failure_lines[0])
        self.assertNotIn('exc_errno=', failure_lines[0])

    def test_success_path_emits_no_d1_diagnostics(self):
        # The store validates completed records against the result contract,
        # so build a fully valid synthetic outcome via the dedicated fixture.
        sys.path.insert(0, str(ROOT / 'tests/mcp'))
        try:
            from completed_result_fixtures import successful_fields
        finally:
            sys.path.pop(0)
        fixture = successful_fields()
        proof_evidence = fixture['execution']['native_tool_evidence']
        outcome_contract = fixture['execution']['result_contract']

        job = self.dispatch()
        snapshot_lines = len(self.journal.lines)
        before = self.gateway.JOB_STORE.get(job['job_id'])

        def broker(path, action, job_id):
            return {'socket': str(self.root / 'jobs' / (job_id + '.sock')), 'ok': True}

        class FakeResult:
            snapshot = {'sanity': 'fixture'}
            evidence = proof_evidence
            outcome = outcome_contract
            exit_code = 0
            summary = 'synthetic summary'
            log_excerpt = None
            truncated = False

        with patch.object(self.gateway, 'broker_control', side_effect=broker), \
                patch.object(self.gateway, 'execute_codex', return_value=FakeResult()):
            self.gateway.run_codex_job(job['job_id'], 'synthetic inspection',
                                       str(self.source))
        record = self.gateway.JOB_STORE.get(job['job_id'])
        self.assertEqual(record['status'], 'completed')
        self.assertEqual(sorted(record), sorted(before))
        for line in self.journal.lines[snapshot_lines:]:
            self.assertNotIn('exc_type=', line)
            self.assertNotIn('exc_errno=', line)


if __name__ == '__main__':
    unittest.main()