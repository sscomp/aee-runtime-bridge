"""One success contract for terminal writes, independent reads and MCP results.

SHA-256 detects accidental/post-write changes within the private store. It is
not authentication against a writer able to replace both record and digest.
Code Mode has no independently observable expected-inner-operation contract;
its results are deliberately unsupported for accepted-success projection.
"""
import hashlib
import hmac
import json
import re
from datetime import datetime

INTEGRITY_VERSION = 'aee-job-sha256-v1'
SUCCESS_VERSION = 'aee-completed-v1'


def reject(code, message, **fields):
    from .store import JobError
    raise JobError(code, message, **fields)


def digest_record(record):
    body = {key: value for key, value in record.items() if key != 'integrity'}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def seal_record(record):
    record['integrity'] = {'version': INTEGRITY_VERSION, 'sha256': digest_record(record)}


def verify_integrity(record):
    proof = record.get('integrity')
    if (not isinstance(proof, dict) or set(proof) != {'version', 'sha256'}
            or proof['version'] != INTEGRITY_VERSION or not isinstance(proof['sha256'], str)
            or not re.fullmatch('[a-f0-9]{64}', proof['sha256'])):
        reject('PERSISTED_INTEGRITY_FAILED', 'Missing, malformed or unsupported job integrity proof')
    try:
        actual = digest_record(record)
    except (ValueError, TypeError, UnicodeError):
        reject('PERSISTED_INTEGRITY_FAILED', 'Job content cannot be integrity verified')
    if not hmac.compare_digest(actual, proof['sha256']):
        reject('PERSISTED_INTEGRITY_FAILED', 'Job content integrity mismatch')


def validate_native_receipt(receipt, thread, broker_receipt=None, *, accepted=False):
    """Validate lifecycle and outcomes; accepted results also require expectations.

    The legacy execution parser keeps its existing error taxonomy. The same
    receipt validator runs on preserved, bounded proof at each success boundary.
    """
    from .native_telemetry import REVISION, MAX_CALLS, TOOLS, ID
    def incomplete():
        reject('TOOL_EVIDENCE_INCOMPLETE', 'Native tool completion evidence is missing or inconsistent',
               exit_code=0, failure={'contract': REVISION, 'evidence_complete': False})
    if (not isinstance(thread, str) or not ID.fullmatch(thread)
            or not isinstance(receipt, dict) or receipt.get('revision') != REVISION
            or receipt.get('type') != 'aee.native_tool_receipt'
            or receipt.get('complete') is not True or receipt.get('conversation') != thread
            or not isinstance(receipt.get('tools'), list) or len(receipt['tools']) > MAX_CALLS):
        incomplete()
    if any(k in receipt for k in ('failure', 'error', 'error_code', 'interrupted')):
        incomplete()
    direct, failures, ids = {}, 0, set()
    for index, tool in enumerate(receipt['tools'], 1):
        if (not isinstance(tool, dict) or tool.get('conversation') != thread
                or type(tool.get('sequence')) is not int or tool['sequence'] != index
                or type(tool.get('success')) is not bool
                or not isinstance(tool.get('tool_name'), str) or tool['tool_name'] not in TOOLS
                or not isinstance(tool.get('source'), str) or tool['source'] not in {'direct', 'code_mode'}
                or not isinstance(tool.get('call_id'), str) or not ID.fullmatch(tool['call_id'])
                or tool['call_id'] in ids):
            incomplete()
        for flag in ('readonly_violation', 'unverified_completion'):
            if flag in tool and type(tool[flag]) is not bool:
                incomplete()
        ids.add(tool['call_id'])
        if tool['source'] == 'direct':
            direct[tool['call_id']] = tool['tool_name']
        failed = (not tool['success'] or tool.get('readonly_violation', False)
                  or any(k in tool for k in ('failure', 'error', 'error_code', 'interrupted')))
        if tool.get('unverified_completion'):
            incomplete()
        if tool['tool_name'] in {'exec_command', 'write_stdin'} and tool['success']:
            if type(tool.get('exit_code')) is not int:
                incomplete()
            failed = failed or tool['exit_code'] != 0
        failures += bool(failed)
    if broker_receipt is not None:
        if (not isinstance(broker_receipt, dict) or broker_receipt.get('valid') is not True
                or broker_receipt.get('response_complete') is not True
                or any(k in broker_receipt for k in ('failure', 'error', 'error_code', 'interrupted'))
                or type(broker_receipt.get('expected_calls')) is not dict
                or broker_receipt['expected_calls'] != direct):
            if accepted:
                reject('REQUIRED_OPERATION_INCOMPLETE', 'Expected native operations are not proven complete')
            incomplete()
    if failures:
        reject('REQUIRED_TOOL_FAILED', 'A required native tool or Code Mode cell failed', exit_code=0,
               failure={'contract': REVISION, 'evidence_complete': True,
                        'tool_failures': failures, 'recovery_permitted': False})
    if accepted:
        if broker_receipt is None:
            reject('REQUIRED_OPERATION_INCOMPLETE', 'Independent expected-operation proof is required')
        if any(t['source'] == 'code_mode' or t['tool_name'] in {'exec', 'wait'} for t in receipt['tools']):
            reject('REQUIRED_OPERATION_UNVERIFIABLE', 'Code Mode expected inner operations are unsupported')
    # Only fixed safe fields cross the persistence boundary, never raw outputs/arguments.
    fields = {'conversation', 'sequence', 'success', 'tool_name', 'source', 'call_id',
              'exit_code', 'readonly_violation', 'unverified_completion'}
    sanitized = {'type': 'aee.native_tool_receipt', 'revision': REVISION, 'complete': True,
                 'conversation': thread, 'tools': [{k: v for k, v in t.items() if k in fields}
                                                  for t in receipt['tools']]}
    broker = None if broker_receipt is None else {k: broker_receipt[k]
                for k in ('valid', 'response_complete', 'expected_calls')}
    return {'contract': REVISION, 'evidence_complete': True, 'tool_count': len(ids),
            'recovery_permitted': False, 'receipt': sanitized, 'broker_receipt': broker}


def validate_success(record, *, persisted=True):
    """Raise a bounded structured error unless every success requirement holds."""
    if persisted:
        verify_integrity(record)
    status = record.get('status')
    if status != 'completed':
        fallback = {'failed': 'EXECUTION_FAILED', 'timed_out': 'EXECUTION_TIMEOUT',
                    'cancelled': 'EXECUTION_CANCELLED'}
        reject(record.get('error_code') or fallback.get(status, 'JOB_NOT_COMPLETE'),
               record.get('error') or 'Job did not complete successfully')
    if (type(record.get('exit_code')) is not int or record['exit_code'] != 0
            or record.get('error_code') is not None or record.get('error') is not None):
        reject('INVALID_TERMINAL_STATE', 'Completed job contradicts its execution outcome')
    execution = record.get('execution')
    if not isinstance(execution, dict):
        reject('RESULT_INCOMPLETE', 'Completed job lacks execution proof')
    if any(key in execution for key in ('native_tool_failure', 'failure', 'error', 'error_code',
                                        'interrupted', 'fatal_error', 'timed_out', 'cancelled')):
        reject('INVALID_TERMINAL_STATE', 'Completed job contains execution failure metadata')
    if record.get('artifacts') != [] or type(record.get('truncated')) is not bool:
        reject('RESULT_INCOMPLETE', 'Completed job has malformed result metadata')
    if not isinstance(record.get('summary'), str) or not record['summary'].strip():
        reject('RESULT_INCOMPLETE', 'Completed job lacks a final result')
    try:
        stamps = [datetime.fromisoformat(record[k]) for k in ('created_at', 'started_at', 'finished_at')]
        if any(t.tzinfo is None for t in stamps) or not stamps[0] <= stamps[1] <= stamps[2]:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        reject('RESULT_INCOMPLETE', 'Completed job lacks consistent lifecycle timestamps')
    outcome = execution.get('result_contract')
    if (not isinstance(outcome, dict) or outcome.get('version') != SUCCESS_VERSION
            or type(outcome.get('process_exit_code')) is not int or outcome['process_exit_code'] != record['exit_code']
            or outcome.get('turn_completed') is not True):
        reject('INVALID_TERMINAL_STATE', 'Completed job lacks a consistent successful final turn')
    if any(k in outcome for k in ('failure', 'error', 'error_code', 'interrupted', 'fatal_error')):
        reject('INVALID_TERMINAL_STATE', 'Completed final turn contains failure metadata')
    proof = execution.get('native_tool_evidence')
    if (not isinstance(proof, dict) or proof.get('evidence_complete') is not True
            or proof.get('recovery_permitted') is not False or type(proof.get('tool_count')) is not int):
        reject('TOOL_EVIDENCE_INCOMPLETE', 'Completed job lacks native receipt proof')
    checked = validate_native_receipt(proof.get('receipt'), outcome.get('thread'),
                                      proof.get('broker_receipt'), accepted=True)
    if proof != checked:
        reject('TOOL_EVIDENCE_INCOMPLETE', 'Persisted receipt summary contradicts its operation proof')
    return True
