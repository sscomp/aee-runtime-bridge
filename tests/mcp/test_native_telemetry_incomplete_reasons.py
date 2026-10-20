"""Regression: bounded collector-side incomplete_reasons on the receipt.

The Stage3B R2 work order requires the collector itself (not the downstream
validator) to name which aggregate predicate broke ``complete``. The decision
is logic-equivalent to the pre-patch expression; the reasons mirror it additively
and are only emitted on the incomplete branch. Call identities are hashed.
"""
import hashlib
import json
import unittest

from aee.mcp_runtime.native_telemetry import Evidence


CONV = 'thr_reasons_1'
CALL = 'call_' + 'a' * 10


def attr(key, value):
    return {'key': key, 'value': {'stringValue': value}}


def int_attr(key, value):
    return {'key': key, 'value': {'intValue': value}}


def tool_record(tool, call_id=CALL, conv=CONV, seq=1, success=True, namespace=None):
    return [attr('event.name', 'codex.tool_result'),
            attr('tool_name', tool),
            attr('tool_namespace', namespace or ('clock' if tool == 'curr_time' else 'functions')),
            attr('call_id', call_id),
            attr('conversation.id', conv),
            int_attr('tool_result_seq', seq),
            attr('success', 'true' if success else 'false')]


def call_event(event, tool, source='direct', call_id=CALL, conv=CONV, seq=None, success=True):
    fields = [attr('event.name', event), attr('tool_name', tool),
              attr('tool_namespace', 'clock' if tool == 'curr_time' else 'functions'),
              attr('call_id', call_id), attr('conversation.id', conv)]
    if event == 'codex.tool_result':
        fields += [int_attr('tool_result_seq', seq or 1), attr('success', 'true' if success else 'false')]
    else:
        fields.append(attr('tool_source', source))
    return {'name': event, 'attributes': fields}


def root_span():
    return {'name': 'codex.exec', 'startTimeUnixNano': 1, 'endTimeUnixNano': 2, 'events': []}


def log_record(attributes):
    return {'attributes': attributes}


def logs_body(records):
    return json.dumps({'resourceLogs': [{'scopeLogs': [{'logRecords': [
        r if isinstance(r, dict) else log_record(r) for r in records]}]}]})


def traces_body(spans):
    return json.dumps({'resourceSpans': [{'scopeSpans': [{'spans': spans}]}]})


class CollectorPredicateReasons(unittest.TestCase):
    def setUp(self):
        self.evidence = Evidence()

    def test_valid_run_is_complete_with_no_reason_key(self):
        self.evidence.accept('/v1/logs', logs_body([
            [attr('event.name', 'codex.conversation_starts'), attr('conversation.id', CONV)]]))
        span = {'name': 'codex.exec', 'startTimeUnixNano': 1, 'endTimeUnixNano': 2, 'events': [
            call_event('codex.tool_call_received', 'curr_time'),
            call_event('codex.tool_result_ready', 'curr_time'),
            call_event('codex.tool_result', 'curr_time')]}
        self.evidence.accept('/v1/traces', traces_body([span]))
        self.evidence.accept('/v1/logs', logs_body([tool_record('curr_time')]))
        receipt = self.evidence.receipt()
        self.assertTrue(receipt['complete'])
        self.assertNotIn('incomplete_reasons', receipt)
        self.assertEqual(receipt['tools'][0]['tool_name'], 'curr_time')
        self.assertEqual(receipt['tools'][0]['source'], 'direct')

    def reason_list(self):
        return self.evidence.receipt()['incomplete_reasons']

    def test_missing_ready_event_names_four_set_membership(self):
        self.evidence.accept('/v1/logs', logs_body([
            [attr('event.name', 'codex.conversation_starts'), attr('conversation.id', CONV)]]))
        span = {'name': 'codex.exec', 'startTimeUnixNano': 1, 'endTimeUnixNano': 2, 'events': [
            call_event('codex.tool_call_received', 'curr_time'),
            call_event('codex.tool_result', 'curr_time')]}
        self.evidence.accept('/v1/traces', traces_body([span]))
        self.evidence.accept('/v1/logs', logs_body([tool_record('curr_time')]))
        self.assertFalse(self.evidence.receipt()['complete'])
        reasons = self.reason_list()
        membership = [r for r in reasons if r['predicate_id'] == 'four_set_membership:tool_result_ready']
        self.assertEqual(len(membership), 1)
        self.assertEqual(membership[0]['observed'], 0)
        self.assertEqual(membership[0]['expected'], 1)
        self.assertEqual(membership[0]['missing'][0],
                         hashlib.sha256(CALL.encode()).hexdigest()[:16])
        # Raw call identity never rides in the reasons.
        self.assertNotIn(CALL, json.dumps(reasons))

    def test_two_root_exec_spans_report_predicate(self):
        self.evidence.accept('/v1/logs', logs_body([
            [attr('event.name', 'codex.conversation_starts'), attr('conversation.id', CONV)],
            tool_record('curr_time')]))
        span = {'name': 'codex.exec', 'startTimeUnixNano': 1, 'endTimeUnixNano': 2, 'events': [
            call_event('codex.tool_call_received', 'curr_time'),
            call_event('codex.tool_result_ready', 'curr_time'),
            call_event('codex.tool_result', 'curr_time')]}
        self.evidence.accept('/v1/traces', traces_body([span, root_span()]))
        reasons = self.reason_list()
        roots = [r for r in reasons if r['predicate_id'] == 'root_exec_spans']
        self.assertEqual(roots, [{'predicate_id': 'root_exec_spans', 'observed': 2, 'expected': 1}])

    def test_malformed_export_sets_collector_invalid(self):
        with self.assertRaises(ValueError):
            self.evidence.accept('/v1/logs', logs_body([tool_record('not_a_tool')]))
        self.assertTrue(self.evidence.receipt()['complete'] is False)
        self.assertEqual([r for r in self.reason_list()
                          if r['predicate_id'] == 'collector_invalid'][0]['observed'], True)

    def test_two_conversations_report_predicate(self):
        self.evidence.accept('/v1/logs', logs_body([
            [attr('event.name', 'codex.conversation_starts'), attr('conversation.id', CONV)],
            [attr('event.name', 'codex.conversation_starts'), attr('conversation.id', CONV + 'X')]]))
        reasons = self.reason_list()
        conv = [r for r in reasons if r['predicate_id'] == 'conversation_count']
        self.assertEqual(conv, [{'predicate_id': 'conversation_count', 'observed': 2, 'expected': 1}])

    def test_per_call_pairing_mismatch_names_the_check(self):
        self.evidence.accept('/v1/logs', logs_body([
            [attr('event.name', 'codex.conversation_starts'), attr('conversation.id', CONV)]]))
        span = {'name': 'codex.exec', 'startTimeUnixNano': 1, 'endTimeUnixNano': 2, 'events': [
            call_event('codex.tool_call_received', 'curr_time'),
            call_event('codex.tool_result_ready', 'curr_time'),
            call_event('codex.tool_result', 'curr_time')]}
        self.evidence.accept('/v1/traces', traces_body([span]))
        # The log names the call 'exec' while calls/ready say 'curr_time'.
        self.evidence.accept('/v1/logs', logs_body([tool_record('exec', namespace='functions')]))
        reasons = self.reason_list()
        pairing = [r for r in reasons if r['predicate_id'] == 'per_call_pairing']
        self.assertEqual(len(pairing), 1)
        self.assertEqual(pairing[0]['observed'], hashlib.sha256(CALL.encode()).hexdigest()[:16])
        self.assertIn('calls_tool_name_mismatch', pairing[0]['detail'])

    def test_sequence_gap_reports_contiguity(self):
        self.evidence.accept('/v1/logs', logs_body([
            [attr('event.name', 'codex.conversation_starts'), attr('conversation.id', CONV)]]))
        span = {'name': 'codex.exec', 'startTimeUnixNano': 1, 'endTimeUnixNano': 2, 'events': [
            call_event('codex.tool_call_received', 'curr_time', call_id=CALL + 'X'),
            call_event('codex.tool_result_ready', 'curr_time', call_id=CALL + 'X'),
            call_event('codex.tool_call_received', 'curr_time', call_id=CALL + 'Y'),
            call_event('codex.tool_result_ready', 'curr_time', call_id=CALL + 'Y'),
            call_event('codex.tool_result', 'curr_time', call_id=CALL + 'X'),
            call_event('codex.tool_result', 'curr_time', call_id=CALL + 'Y', seq=3)]}
        self.evidence.accept('/v1/traces', traces_body([span]))
        self.evidence.accept('/v1/logs', logs_body([tool_record('curr_time', call_id=CALL + 'X', seq=1),
                                                    tool_record('curr_time', call_id=CALL + 'Y', seq=3)]))
        reasons = self.reason_list()
        cont = [r for r in reasons if r['predicate_id'] == 'sequence_contiguity']
        self.assertEqual(len(cont), 1)
        self.assertEqual(cont[0]['observed'], 2)
        self.assertEqual(cont[0]['detail'], [1, 3])

    def test_reasons_are_bounded(self):
        self.evidence.accept('/v1/logs', logs_body([
            [attr('event.name', 'codex.conversation_starts'), attr('conversation.id', CONV)]]))
        events = []
        records = []
        for n in range(20):
            call_id = CALL + f'{n:02d}'
            events += [call_event('codex.tool_call_received', 'curr_time', call_id=call_id),
                       call_event('codex.tool_result', 'curr_time', call_id=call_id, seq=n + 1)]
            records.append(tool_record('curr_time', call_id=call_id, seq=n + 1))
        span = {'name': 'codex.exec', 'startTimeUnixNano': 1, 'endTimeUnixNano': 2, 'events': events}
        self.evidence.accept('/v1/traces', traces_body([span]))
        self.evidence.accept('/v1/logs', logs_body(records))
        reasons = self.reason_list()
        self.assertLessEqual(len(reasons), 8)
        membership = [r for r in reasons
                      if r['predicate_id'] == 'four_set_membership:tool_result_ready'][0]
        self.assertLessEqual(len(membership['missing']), 16)


class ExecutorCarriesCollectorReasons(unittest.TestCase):
    @staticmethod
    def stream(events):
        return ("\n".join(events) + "\n").encode()

    def test_incomplete_receipt_failure_keeps_collector_reasons(self):
        from aee.mcp_runtime.executor import final_agent_message
        from aee.mcp_runtime.process import Limits
        from aee.mcp_runtime.store import JobError
        from aee.mcp_runtime.native_telemetry import REVISION
        reasons = [{'predicate_id': 'four_set_membership:tool_result_ready',
                    'observed': 0, 'expected': 1,
                    'missing': [hashlib.sha256(CALL.encode()).hexdigest()[:16]]}]
        receipt = json.dumps({
            'type': 'aee.native_tool_receipt', 'revision': REVISION, 'complete': False,
            'conversation': 'thr_reasons_exec',
            'incomplete_reasons': reasons,
            'tools': [{'sequence': 1, 'tool_name': 'exec_command', 'source': 'code_mode',
                       'success': True, 'exit_code': 0, 'conversation': 'thr_reasons_exec'}],
        })
        events = [
            '{"type":"thread.started","thread_id":"thr_reasons_exec"}',
            '{"type":"turn.started"}',
            '{"type":"turn.completed"}',
            receipt,
            '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
        ]
        with self.assertRaises(JobError) as error:
            final_agent_message(self.stream(events), Limits(), require_receipt=True)
        self.assertEqual(error.exception.code, "TOOL_EVIDENCE_INCOMPLETE")
        self.assertEqual(error.exception.failure["receipt_state"]["collector_reasons"], reasons)
        raw = json.dumps(error.exception.failure)
        self.assertLessEqual(len(raw), 20000)


if __name__ == "__main__":
    unittest.main()