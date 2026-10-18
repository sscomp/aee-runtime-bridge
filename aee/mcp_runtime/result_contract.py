"""One success contract for terminal writes, independent reads and MCP results.

SHA-256 detects accidental/post-write changes within the private store. It is
not authentication against a writer able to replace both record and digest.
Since the R3 review, an observed Code Mode execution is machine-substantiated:
the OTLP native receipt decodes the pinned binary's own result header (exit
code / cell completion) for every operation, and the conversation-declared
outer calls cross-match the broker's expected_calls. Accepted success records
an operation_attestation grade; anything without machine-verified completion
still fails closed.
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
    def incomplete(condition):
        # Attribution-only label naming the triggered validation condition;
        # classification and the fail-closed raise are unchanged.
        reject('TOOL_EVIDENCE_INCOMPLETE', 'Native tool completion evidence is missing or inconsistent',
               exit_code=0, failure={'contract': REVISION, 'evidence_complete': False,
                                     'condition': condition})
    if not isinstance(thread, str) or not ID.fullmatch(thread):
        incomplete('thread_missing_or_malformed')
    if (not isinstance(receipt, dict) or receipt.get('revision') != REVISION
            or receipt.get('type') != 'aee.native_tool_receipt'
            or receipt.get('complete') is not True or receipt.get('conversation') != thread
            or not isinstance(receipt.get('tools'), list) or len(receipt['tools']) > MAX_CALLS):
        incomplete('receipt_missing_or_malformed')
    if any(k in receipt for k in ('failure', 'error', 'error_code', 'interrupted')):
        incomplete('receipt_lifecycle_fields_present')
    direct, failures, ids = {}, 0, set()
    for index, tool in enumerate(receipt['tools'], 1):
        if (not isinstance(tool, dict) or tool.get('conversation') != thread
                or type(tool.get('sequence')) is not int or tool['sequence'] != index
                or type(tool.get('success')) is not bool
                or not isinstance(tool.get('tool_name'), str) or tool['tool_name'] not in TOOLS
                or not isinstance(tool.get('source'), str) or tool['source'] not in {'direct', 'code_mode'}
                or not isinstance(tool.get('call_id'), str) or not ID.fullmatch(tool['call_id'])
                or tool['call_id'] in ids):
            incomplete('tool_record_malformed')
        for flag in ('readonly_violation', 'unverified_completion'):
            if flag in tool and type(tool[flag]) is not bool:
                incomplete('tool_flag_not_boolean')
        ids.add(tool['call_id'])
        if tool['source'] == 'direct':
            direct[tool['call_id']] = tool['tool_name']
        failed = (not tool['success'] or tool.get('readonly_violation', False)
                  or any(k in tool for k in ('failure', 'error', 'error_code', 'interrupted')))
        if tool.get('unverified_completion'):
            incomplete('unverified_completion_cell')
        if tool['tool_name'] in {'exec_command', 'write_stdin'} and tool['success']:
            if type(tool.get('exit_code')) is not int:
                incomplete('exit_code_missing')
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
            incomplete('broker_receipt_invalid')
    if failures:
        reject('REQUIRED_TOOL_FAILED', 'A required native tool or Code Mode cell failed', exit_code=0,
               failure={'contract': REVISION, 'evidence_complete': True,
                        'tool_failures': failures, 'recovery_permitted': False})
    if accepted:
        if broker_receipt is None:
            reject('REQUIRED_OPERATION_INCOMPLETE', 'Independent expected-operation proof is required')
        if any(t['source'] == 'code_mode' and t['tool_name'] in {'exec', 'wait'}
               for t in receipt['tools']):
            # A code-mode cell carrying exec/wait semantics has no observed
            # completion shape in this contract: fail closed here even though
            # the surrounding operation may look successful.
            reject('REQUIRED_OPERATION_UNVERIFIABLE', 'Code Mode expected inner operations are unsupported')
    # Only fixed safe fields cross the persistence boundary, never raw outputs/arguments.
    fields = {'conversation', 'sequence', 'success', 'tool_name', 'source', 'call_id',
              'exit_code', 'readonly_violation', 'unverified_completion'}
    sanitized = {'type': 'aee.native_tool_receipt', 'revision': REVISION, 'complete': True,
                 'conversation': thread, 'tools': [{k: v for k, v in t.items() if k in fields}
                                                  for t in receipt['tools']]}
    broker = None if broker_receipt is None else {k: broker_receipt[k]
                for k in ('valid', 'response_complete', 'expected_calls')}
    result = {'contract': REVISION, 'evidence_complete': True, 'tool_count': len(ids),
              'recovery_permitted': False, 'receipt': sanitized, 'broker_receipt': broker}
    if accepted and receipt['tools']:
        # R3 operation attestation (additive; omitted for answer-only results so
        # pre-R3 persisted records and the zero-tool path validate unchanged).
        inner = [t for t in receipt['tools'] if t['source'] == 'code_mode']
        declared, observed = len(direct), len(inner)
        if not inner:
            grade, reason = 'verified', ('only broker-declared outer operations, each carrying the '
                                         'pinned binary\'s own machine-verified completion evidence')
        elif len(inner) == declared and all(t['tool_name'] in {'exec_command', 'write_stdin'}
                                            for t in inner):
            grade, reason = 'verified', ('one machine-verified code-mode execution per broker-declared '
                                         'outer call; every observed operation completed under the '
                                         'pinned binary\'s own machine-generated result header')
        else:
            grade, reason = 'partial', ('machine-verified operations completed, but observed code-mode '
                                        'executions are not in 1:1 declared-outer pairing; their inner '
                                        'operations are not itemized and only the read-only sandbox '
                                        'enforcement bounds them — no unobserved operation is claimed')
        result['operation_attestation'] = {'grade': grade, 'declared_outer_calls': declared,
                                           'observed_code_mode_executions': observed,
                                           'reason': reason}
    return result


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
                                        'output_limit_diagnostics',
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
    proof = execution.get('adapter_proof')
    if record.get('mode') == 'autonomous' and proof is None:
        reject('TOOL_EVIDENCE_INCOMPLETE', 'Autonomous result requires native adapter proof')
    if proof is not None:
        # Multi-agent recovery: jobs run by the generic adapter contract
        # (mcp_runtime/job_adapters) prove completion with adapter-shaped,
        # bounds-checked fields instead of the codex native receipt.
        if not isinstance(proof, dict) or proof.get('adapter') != record.get('agent'):
            reject('TOOL_EVIDENCE_INCOMPLETE', 'Completed job lacks consistent adapter proof')
        if any(k in proof for k in ('failure', 'error', 'error_code', 'interrupted',
                                    'fatal_error', 'timed_out', 'cancelled')):
            reject('INVALID_TERMINAL_STATE', 'Completed final turn contains failure metadata')
        autonomous = record.get('mode') == 'autonomous'
        if autonomous:
            authorization = execution.get('authorization')
            process = execution.get('process_identity')
            process_valid = False
            try:
                identity = authorization['os_identity']
                fields = dict(line.split(':', 1) for line in process['status'])
                process_valid = (type(process['pid']) is int and process['pid'] > 0
                    and [int(v) for v in fields['Uid'].split()] == [identity['uid']] * 4
                    and [int(v) for v in fields['Gid'].split()] == [identity['gid']] * 4
                    and sorted(int(v) for v in fields['Groups'].split()) == identity['groups']
                    and fields['NoNewPrivs'].strip() == '1' and int(fields['CapEff'].strip(), 16) == 0)
            except (TypeError, KeyError, ValueError, AttributeError):
                pass
            if (not isinstance(authorization, dict)
                    or authorization.get('schema') not in {'aee-autonomous-proof-v1', 'aee-secure-mcp-admission-v1'}
                    or authorization.get('schema') == 'aee-secure-mcp-admission-v1' and (
                        authorization.get('policy') != 'secure_mcp_trusted_dispatch'
                        or authorization.get('transport') != 'secure_mcp'
                        or not authorization.get('principal'))
                    or authorization.get('mode') != 'autonomous'
                    or authorization.get('profile') != 'os-native'
                    or authorization.get('agent') != record.get('agent')
                    or authorization.get('working_directory') != record.get('working_directory')
                    or proof.get('authorization') != authorization
                    or proof.get('mode') != 'autonomous'
                    or execution.get('native_profile') != 'os-native'
                    or not process_valid):
                reject('TOOL_EVIDENCE_INCOMPLETE', 'Autonomous result lacks scoped authorization and process proof')
        if not autonomous and proof.get('mode') == 'autonomous':
            reject('INVALID_TERMINAL_STATE', 'Adapter mode contradicts job mode')
        if (type(proof.get('exit_code')) is not int or proof['exit_code'] != record['exit_code']
                or proof.get('turn_completed') is not True
                or not autonomous and proof.get('read_only_enforcement') in {'UNSUPPORTED', None, ''}):
            reject('INVALID_TERMINAL_STATE', 'Completed job lacks a consistent successful final turn')
        if not isinstance(proof.get('working_directory'), str) \
                or proof['working_directory'] != record.get('working_directory'):
            reject('INVALID_TERMINAL_STATE', 'Completed adapter proof contradicts the job workdir')
        return True
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
