"""Explicit synthetic execution proof for store/protocol contract fixtures only."""
from aee.mcp_runtime.store import now


def successful_fields(summary='fixture', execution=None):
    execution = dict(execution or {})
    thread = 'fixture-thread'
    receipt = {'type': 'aee.native_tool_receipt', 'revision': 'codex-native-otlp-r3-1',
               'complete': True, 'conversation': thread, 'tools': []}
    execution['native_tool_evidence'] = {
        'contract': 'codex-native-otlp-r3-1', 'evidence_complete': True, 'tool_count': 0,
        'recovery_permitted': False, 'receipt': receipt,
        'broker_receipt': {'valid': True, 'response_complete': True, 'expected_calls': {}}}
    execution['result_contract'] = {'version': 'aee-completed-v1', 'process_exit_code': 0,
                                    'turn_completed': True, 'thread': thread}
    return {'status': 'completed', 'exit_code': 0, 'summary': summary,
            'started_at': now(), 'finished_at': now(), 'execution': execution}
