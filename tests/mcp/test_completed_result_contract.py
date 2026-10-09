"""Completed-result write/reopen/MCP controls. Every persisted case reopens fresh."""
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aee.mcp_runtime.executor import execute_codex
from aee.mcp_runtime.process import Limits, ProcessResult
from aee.mcp_runtime.profiles import ExecutorIdentity, select_profile
from aee.mcp_runtime.result_contract import validate_success
from aee.mcp_runtime.store import JobError, JobStore
from completed_result_fixtures import successful_fields

ROOT = Path(__file__).resolve().parents[2]
READER = ROOT / 'scripts/diagnostics/aee_completed_result_reopen.py'


def independently_seal(record):
    body = {k: v for k, v in record.items() if k != 'integrity'}
    record['integrity'] = {'version': 'aee-job-sha256-v1', 'sha256': hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()}


def completed_with_command():
    fields = successful_fields('Verified synthetic operation')
    proof = fields['execution']['native_tool_evidence']
    proof['receipt']['tools'] = [{'call_id': 'required-command', 'tool_name': 'exec_command',
        'source': 'direct', 'conversation': 'fixture-thread', 'sequence': 1, 'success': True, 'exit_code': 0}]
    proof['tool_count'] = 1
    proof['broker_receipt']['expected_calls'] = {'required-command': 'exec_command'}
    return fields


class CompletedResultContract(unittest.TestCase):
    def test_fresh_process_positive_and_all_negative_controls(self):
        evidence = os.getenv('AEE_COMPLETED_RESULT_EVIDENCE')
        temporary = tempfile.TemporaryDirectory(prefix='aee-completed-contract-')
        self.addCleanup(temporary.cleanup)
        root = Path(evidence) / 'controls' if evidence else Path(temporary.name)
        root.mkdir(exist_ok=True)
        rows = []
        cases = [('valid_success', None, None),
                 ('completed_nonzero_exit', 'exit', 'INVALID_TERMINAL_STATE'),
                 ('completed_native_failure', 'native', 'INVALID_TERMINAL_STATE'),
                 ('completed_interruption', 'interruption', 'INVALID_TERMINAL_STATE'),
                 ('completed_explicit_failure', 'error', 'INVALID_TERMINAL_STATE'),
                 ('missing_required_operation', 'missing', 'REQUIRED_OPERATION_INCOMPLETE'),
                 ('failed_required_operation', 'failed_tool', 'REQUIRED_TOOL_FAILED'),
                 ('malformed_receipt', 'malformed', 'TOOL_EVIDENCE_INCOMPLETE'),
                 ('contradictory_receipt', 'contradictory', 'TOOL_EVIDENCE_INCOMPLETE'),
                 ('payload_mutation', 'payload', 'PERSISTED_INTEGRITY_FAILED'),
                 ('integrity_mutation', 'digest', 'PERSISTED_INTEGRITY_FAILED'),
                 ('missing_integrity', 'no_integrity', 'PERSISTED_INTEGRITY_FAILED'),
                 ('malformed_integrity', 'bad_integrity', 'PERSISTED_INTEGRITY_FAILED'),
                 ('semantic_schema_valid', 'outcome', 'INVALID_TERMINAL_STATE'),
                 ('historical_legacy', 'legacy', 'PERSISTED_INTEGRITY_FAILED'),
                 ('genuine_failed', 'failed', 'EXECUTION_FAILED'),
                 ('genuine_interrupted', 'interrupted', 'EXECUTION_INTERRUPTED'),
                 ('historical_fail_open_equivalent', 'historical', 'INVALID_TERMINAL_STATE'),
                 ('unsupported_integrity', 'unsupported', 'PERSISTED_INTEGRITY_FAILED'),
                 ('code_mode_unverifiable', 'code_mode', 'REQUIRED_OPERATION_UNVERIFIABLE'),
                 ('missing_broker_proof', 'no_broker', 'REQUIRED_OPERATION_INCOMPLETE'),
                 ('missing_result', 'no_result', 'RESULT_INCOMPLETE'),
                 ('tamper_and_redigest', 'redigest', None)]
        for name, change, error_code in cases:
            case = root / name
            case.mkdir()
            store = JobStore(case / 'jobs')
            job = store.create('codex', 'bounded synthetic contract', '/workspace', 'read_only')
            store.update(job['job_id'], status='running')
            fields = completed_with_command()
            if change in {'failed', 'interrupted'}:
                fields = {'status': 'failed', 'error_code': 'EXECUTION_INTERRUPTED' if change == 'interrupted' else 'EXECUTION_FAILED',
                          'error': 'Synthetic failure', 'exit_code': 7}
            sealed = store.update(job['job_id'], **fields)
            store.close()
            path = case / 'jobs' / (job['job_id'] + '.json')
            original = path.read_bytes()
            (case / 'original.json').write_bytes(original)
            record = json.loads(original)
            proof = record['execution'].get('native_tool_evidence') if isinstance(record['execution'], dict) else None
            if change == 'exit': record['exit_code'] = 7
            elif change == 'native': record['execution']['native_tool_failure'] = {'tool_failures': 1}
            elif change == 'interruption': record['execution']['interrupted'] = True
            elif change == 'error': record['error_code'] = 'EXECUTION_FAILED'
            elif change == 'missing': proof['receipt']['tools'] = []; proof['tool_count'] = 0
            elif change == 'failed_tool': proof['receipt']['tools'][0]['exit_code'] = 7
            elif change == 'malformed': proof['receipt']['tools'][0]['success'] = 'true'
            elif change == 'contradictory': proof['tool_count'] = 9
            elif change == 'payload': record['summary'] = 'Mutated after sealing'
            elif change == 'digest': record['integrity']['sha256'] = '0' * 64
            elif change in {'no_integrity', 'legacy'}: record.pop('integrity')
            elif change == 'bad_integrity': record['integrity'] = {'version': 'aee-job-sha256-v1', 'sha256': 7}
            elif change == 'unsupported': record['integrity']['version'] = 'aee-job-sha256-v9'
            elif change == 'outcome': record['execution']['result_contract']['turn_completed'] = False
            elif change == 'historical':
                record['exit_code'] = 7
                record['execution']['native_tool_failure'] = {'tool_failures': 1}
            elif change == 'code_mode':
                proof['receipt']['tools'][0]['source'] = 'code_mode'
                proof['broker_receipt']['expected_calls'] = {}
            elif change == 'no_broker': proof['broker_receipt'] = None
            elif change == 'no_result': record['summary'] = None
            elif change == 'redigest': record['summary'] = 'Writer changed content and recomputed digest'
            if change not in {None, 'payload', 'digest', 'no_integrity', 'legacy', 'bad_integrity', 'unsupported'}:
                independently_seal(record)
            path.write_text(json.dumps(record, ensure_ascii=False))
            final_bytes = path.read_bytes()
            process = subprocess.run([sys.executable, str(READER), str(case / 'jobs'), job['job_id']],
                                     capture_output=True, text=True, timeout=20)
            (case / 'reopen-process.json').write_text(json.dumps({'exit_code': process.returncode,
                'stdout': process.stdout, 'stderr': process.stderr, 'fresh_process': True}, indent=2))
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(process.stdout)
            (case / 'mcp-result.json').write_text(json.dumps(result, indent=2))
            row = {'case': name, 'expected_ok': error_code is None, 'expected_error': error_code,
                   'mcp': result, 'fresh_process': True, 'job_id': job['job_id'],
                   'original_sha256': hashlib.sha256(original).hexdigest(),
                   'persisted_sha256': hashlib.sha256(final_bytes).hexdigest()}
            rows.append(row)
            (root.parent / 'negative-control-matrix.json').write_text(json.dumps(rows, indent=2))
            if error_code is not None and result.get('ok') is True:
                raise SystemExit('STOP: negative control crossed MCP as success: ' + name)
            self.assertEqual(result.get('ok'), error_code is None, row)
            if error_code: self.assertEqual(result.get('error_code'), error_code, row)
            self.assertEqual(path.read_bytes(), final_bytes, 'Invalid evidence must not be rewritten')

    def test_execution_worker_persistence_and_fresh_mcp_positive(self):
        import ast
        import importlib.util
        import logging
        import time
        from datetime import datetime, timezone
        from types import SimpleNamespace
        from aee.mcp_runtime.profiles import execution_metadata
        from aee.mcp_runtime.native_telemetry import Evidence
        evidence_path = os.getenv('AEE_COMPLETED_RESULT_EVIDENCE')
        temporary = tempfile.TemporaryDirectory(prefix='aee-completed-worker-')
        self.addCleanup(temporary.cleanup)
        root = Path(evidence_path) / 'execution-positive' if evidence_path else Path(temporary.name)
        root.mkdir(exist_ok=True)
        source = root / 'source';source.mkdir()
        manifest = root / 'workspace.json';manifest.write_text('{}')
        binary = root / 'executor.elf';binary.write_bytes(b'\x7fELFsynthetic-bounded-process')
        identity = ExecutorIdentity(str(binary), hashlib.sha256(binary.read_bytes()).hexdigest(), 'synthetic-fixture')
        profile = select_profile()
        store = JobStore(root / 'jobs');self.addCleanup(store.close)
        job = store.create('codex', 'synthetic no-tool final answer', str(source), 'read_only',
                           execution=execution_metadata(profile, identity))
        e = Evidence();e.roots = 1;e.conversations.add('fixture-thread')
        receipt = e.receipt()
        events = [{'type': 'thread.started', 'thread_id': 'fixture-thread'}, {'type': 'turn.started'},
                  {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Verified execution success'}},
                  {'type': 'turn.completed'}, receipt]
        stdout = b''.join(json.dumps(v).encode() + b'\n' for v in events)
        (root / 'executor.jsonl').write_bytes(stdout)
        broker = {'valid': True, 'response_complete': True, 'expected_calls': {}}
        (root / 'broker.json').write_text(json.dumps(broker))
        tree = ast.parse((ROOT / 'mcp_gateway.py').read_text())
        names = {'run_codex_job', '_now_iso'}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        namespace = {'JOB_STORE': store, '_JobError': JobError, 'Dict': dict, 'Any': object,
            'time': time, 'datetime': datetime, 'timezone': timezone, 'log': logging.getLogger('fixture-worker'),
            'os': SimpleNamespace(getenv=lambda k: str(root / 'fixture-control')),
            '_dispatch_pool': {}, 'select_profile': select_profile, 'ExecutorIdentity': ExecutorIdentity,
            'resolve_codex': lambda p: identity, 'A3_CODEX_BIN': str(binary), 'execute_codex': execute_codex,
            'A3_LIMITS': Limits(), 'A3_WORKSPACE_MANIFEST': str(manifest), 'redact': lambda s: s,
            'deployment_policy': lambda: {'fixture': True}, 'require_deployment_ready': lambda p: None,
            'broker_control': lambda p, action, j: broker if action == 'receipt' else {'socket': str(root / 'fixture.sock')}}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / 'mcp_gateway.py'), 'exec'), namespace)
        policy = {'resource_policy_revision': {'id': 'fixture-resource'}, 'sandbox_policy_revision': {'id': 'fixture-sandbox'}}
        # Only external execution/isolation/readiness transport is doubled; actual
        # receipt parser, snapshot, worker, terminal derivation, store and MCP run.
        with patch('aee.mcp_runtime.runtime.deployment_policy', return_value=policy), \
             patch('aee.mcp_runtime.runtime.require_deployment_ready'), \
             patch('aee.mcp_runtime.sandbox_policy.telemetry_arguments', side_effect=lambda a: a), \
             patch('aee.mcp_runtime.executor.sandbox_command', return_value=['/usr/bin/true']), \
             patch('aee.mcp_runtime.executor.run_bounded', return_value=ProcessResult(0, stdout, b'', b'')):
            namespace['run_codex_job'](job['job_id'], 'synthetic no-tool final answer', str(source))
        self.assertEqual(store.get(job['job_id'])['status'], 'completed')
        store.close()
        process = subprocess.run([sys.executable, str(READER), str(root / 'jobs'), job['job_id']], capture_output=True, text=True, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertTrue(result['ok'], result)
        (root / 'mcp-result.json').write_text(json.dumps(result, indent=2))
        (root / 'reopen-process.json').write_text(json.dumps({'exit_code': process.returncode, 'fresh_process': True,
            'stdout': process.stdout, 'stderr': process.stderr, 'doubles': ['process', 'sandbox_command', 'deployment_readiness', 'broker_transport']}))

    def test_authoritative_write_rejects_all_contradictions_without_publishing(self):
        changes = [lambda f: f.update(exit_code=7),
                   lambda f: f.update(error_code='EXECUTION_FAILED'),
                   lambda f: f['execution'].update(native_tool_failure={'tool_failures': 1}),
                   lambda f: f['execution'].update(interrupted=True),
                   lambda f: f['execution']['native_tool_evidence']['receipt'].update(tools=[]),
                   lambda f: f['execution']['native_tool_evidence']['receipt']['tools'][0].update(exit_code=7),
                   lambda f: f['execution']['native_tool_evidence']['receipt'].update(complete=False),
                   lambda f: f['execution']['result_contract'].update(turn_completed=False)]
        for change in changes:
            with tempfile.TemporaryDirectory() as temporary:
                store = JobStore(Path(temporary))
                try:
                    job = store.create('codex', 'fixture', '/workspace', 'read_only')
                    original = store._job_path(job['job_id']).read_bytes()
                    fields = completed_with_command();change(fields)
                    with self.assertRaises(JobError):store.update(job['job_id'], **fields)
                    self.assertEqual(store._job_path(job['job_id']).read_bytes(), original)
                finally:store.close()

    def test_exact_metadata_types_and_nested_failure_classifications_are_rejected(self):
        changes = [lambda f: f.update(truncated=0),
                   lambda f: f['execution']['native_tool_evidence'].update(evidence_complete=1),
                   lambda f: f['execution']['native_tool_evidence'].update(recovery_permitted=0),
                   lambda f: f['execution']['native_tool_evidence'].update(tool_count=True),
                   lambda f: f['execution']['result_contract'].update(error_code='EXECUTION_FAILED'),
                   lambda f: f['execution']['native_tool_evidence']['broker_receipt'].update(error_code='EXECUTION_FAILED')]
        for change in changes:
            with tempfile.TemporaryDirectory() as temporary:
                store = JobStore(Path(temporary))
                try:
                    job = store.create('codex', 'fixture', '/workspace', 'read_only')
                    fields = completed_with_command();change(fields)
                    with self.assertRaises(JobError):store.update(job['job_id'], **fields)
                finally:store.close()

    def test_malformed_disk_records_return_structured_errors_without_startup_crash(self):
        values = [b'{"job_id": "duplicate", "job_id": "second"}',
                  b'{"truncated": NaN}', b'[' * 1500 + b'0' + b']' * 1500]
        for value in values:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                job_id = 'A3JOB-20261005-' + 'a' * 32
                (root / (job_id + '.json')).write_bytes(value)
                process = subprocess.run([sys.executable, str(READER), temporary, job_id], capture_output=True, text=True, timeout=20)
                self.assertEqual(process.returncode, 0, process.stderr)
                result = json.loads(process.stdout)
                self.assertFalse(result['ok'])
                self.assertEqual(result['error_code'], 'JOB_STORE_CORRUPT')

    def test_legacy_migration_cannot_bless_missing_integrity(self):
        from aee.mcp_runtime.migration import migrate
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = JobStore(root / 'old')
            job = store.create('codex', 'fixture', '/workspace', 'read_only')
            record = store.update(job['job_id'], **completed_with_command());store.close()
            record.pop('integrity')
            path = root / 'old' / (job['job_id'] + '.json');path.write_text(json.dumps(record))
            original = path.read_bytes()
            with self.assertRaises(JobError) as error:migrate(root / 'old', root / 'new', root / 'backup', apply=True)
            self.assertEqual(error.exception.code, 'PERSISTED_INTEGRITY_FAILED')
            self.assertFalse((root / 'new').exists())
            self.assertEqual(path.read_bytes(), original)

    def test_mcp_does_not_trust_even_an_in_memory_completed_label(self):
        # A replacement store bypassing _read must still fail at MCP projection.
        import importlib.util
        spec = importlib.util.spec_from_file_location('fixture_reader', READER)
        reader = importlib.util.module_from_spec(spec);spec.loader.exec_module(reader)
        import asyncio
        class UncheckedStore:
            def get(self, job_id):
                result = {'job_id': job_id, 'artifacts': [], 'truncated': False, **successful_fields()}
                result['exit_code'] = 7
                independently_seal(result)
                return result
        result = json.loads(asyncio.run(reader.gateway_functions(UncheckedStore())['aee_job_result']('fixture')))
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'], 'INVALID_TERMINAL_STATE')


if __name__ == '__main__':unittest.main()
