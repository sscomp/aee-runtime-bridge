"""Regression: bounded transport-phase diagnostics on the native OTLP collector.

The Stage 3B R2 transport-capture work order requires the collector to record,
per request, which boundary (framing / budget / parse / validation / ingest /
finalize) accepted or rejected each export — with fixed enums and size buckets
only, off by default (serialized only when AEE_TRANSPORT_DIAGNOSTICS=1 and the
receipt is incomplete), and never as an input to the ``complete`` decision.
Framing cases run through a real job-private Collector over HTTP; evidence-level
cases use Evidence directly (budget, span validation).
"""
import http.client
import json
import os
import threading
import unittest
from unittest import mock

from aee.mcp_runtime.native_telemetry import (Evidence, Collector, MAX_BODY, MAX_TOTAL,
                                              TRANSPORT_MAX_EVENTS)
from aee.mcp_runtime.executor import augmented_evidence_failure


CONV = 'thr_transport_1'
CALL = 'call_' + 'b' * 10


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


def root_span(start=1, end=2):
    return {'name': 'codex.exec', 'startTimeUnixNano': start, 'endTimeUnixNano': end, 'events': []}


def logs_body(records):
    return json.dumps({'resourceLogs': [{'scopeLogs': [{'logRecords': [
        r if isinstance(r, dict) else {'attributes': r} for r in records]}]}]})


def traces_body(spans):
    return json.dumps({'resourceSpans': [{'scopeSpans': [{'spans': spans}]}]})


def conversation_body(conv=CONV):
    return logs_body([[attr('event.name', 'codex.conversation_starts'), attr('conversation.id', conv)]])


EVENT_KEYS = {'event_sequence', 'phase', 'outcome', 'reason_code', 'payload_size_bucket',
              'records_bucket', 'root_span_present'}
PHASES = {'framing', 'budget', 'parse', 'ingest', 'transport', 'finalize'}


class TransportDiagnostics(unittest.TestCase):
    def setUp(self):
        self.evidence = Evidence()
        self._env = mock.patch.dict(os.environ, {'AEE_TRANSPORT_DIAGNOSTICS': '1'})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def _valid_run(self):
        self.evidence.accept('/v1/logs', conversation_body())
        span = {'name': 'codex.exec', 'startTimeUnixNano': 1, 'endTimeUnixNano': 2, 'events': [
            call_event('codex.tool_call_received', 'curr_time'),
            call_event('codex.tool_result_ready', 'curr_time'),
            call_event('codex.tool_result', 'curr_time', source='code_mode')]}
        self.evidence.accept('/v1/traces', traces_body([span]))
        self.evidence.accept('/v1/logs', logs_body([tool_record('curr_time')]))

    def test_positive_control_complete_no_diagnostics_key(self):
        self._valid_run()
        receipt = self.evidence.receipt()
        self.assertTrue(receipt['complete'])
        self.assertNotIn('incomplete_reasons', receipt)
        self.assertNotIn('transport_diagnostics', receipt)
        self.assertEqual(self.evidence.transport_totals['codex_exec_counted'], 1)
        self.assertEqual(self.evidence.transport_totals['accepted_requests'], 3)
        self.assertEqual(self.evidence.transport_totals['invalid_sites'], {
            'framing': 0, 'budget': 0, 'parse': 0, 'validation': 0, 'transport': 0})

    def test_missing_root_serializes_phases_only_with_flag(self):
        self.evidence.accept('/v1/logs', conversation_body())
        self.evidence.accept('/v1/logs', logs_body([tool_record('curr_time')]))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('AEE_TRANSPORT_DIAGNOSTICS', None)
            off = self.evidence.receipt()
        self.assertFalse(off['complete'])
        self.assertIn('incomplete_reasons', off)
        self.assertNotIn('transport_diagnostics', off)  # off by default
        on = self.evidence.receipt()
        self.assertFalse(on['complete'])
        # Decision semantics are unchanged for this evidence state: the same
        # reason set the pre-patch union logic produces (root missing + set
        # membership + pairing), with root_exec_spans first.
        self.assertEqual(on['incomplete_reasons'][0]['predicate_id'], 'root_exec_spans')
        block = on['transport_diagnostics']
        self.assertEqual(block['schema_version'], 1)
        self.assertEqual(block['totals']['requests_total'], 2)
        self.assertEqual(block['totals']['codex_exec_seen'], 0)
        self.assertEqual(block['totals']['codex_exec_counted'], 0)
        self.assertTrue(any(e['phase'] == 'finalize' for e in block['events']))
        for event in block['events']:
            self.assertTrue(set(event) <= EVENT_KEYS)
            self.assertIn(event['phase'], PHASES)

    def test_parse_rejection_codes(self):
        with self.assertRaises(ValueError):
            self.evidence.accept('/v1/logs', b'notjson')
        self.assertTrue(self.evidence.invalid)
        event = self.evidence.transport_events[-1]
        self.assertEqual((event['phase'], event['reason_code']), ('parse', 'PARSE_NOT_JSON'))
        receipt = self.evidence.receipt()
        self.assertFalse(receipt['complete'])
        # parse failure flags invalid; the empty evidence state also names the
        # absent root and conversation — same predicates the pre-patch
        # expression produced.
        self.assertEqual(receipt['incomplete_reasons'][0]['predicate_id'], 'collector_invalid')
        self.evidence2 = Evidence()
        with self.assertRaises(ValueError):
            self.evidence2.accept('/v1/logs', json.dumps([1, 2]).encode())
        self.assertTrue(self.evidence2.invalid)
        self.assertEqual(self.evidence2.transport_events[-1]['reason_code'], 'BODY_NOT_OBJECT')

    def test_budget_rejection_codes(self):
        with self.assertRaises(ValueError):
            self.evidence.accept('/v1/logs', b'x' * (MAX_TOTAL + 1))
        self.assertTrue(self.evidence.invalid)
        event = self.evidence.transport_events[-1]
        self.assertEqual((event['phase'], event['reason_code']), ('budget', 'TOTAL_BUDGET'))
        other = Evidence()
        other.requests = 256  # next request crosses the request bound
        with self.assertRaises(ValueError):
            other.accept('/v1/traces', b'tiny')
        self.assertTrue(other.invalid)
        self.assertEqual(other.transport_events[-1]['reason_code'], 'REQUEST_BUDGET')

    def test_span_order_rejected_and_not_counted(self):
        self.evidence.accept('/v1/logs', conversation_body())
        with self.assertRaises(ValueError):
            self.evidence.accept('/v1/traces', traces_body([root_span(start=5, end=2)]))
        self.assertTrue(self.evidence.invalid)
        event = self.evidence.transport_events[-1]
        self.assertEqual((event['phase'], event['reason_code'], event['root_span_present']),
                         ('ingest', 'SPAN_ORDER_INVALID', 'rejected_order'))
        self.assertEqual(self.evidence.transport_totals['codex_exec_seen'], 1)
        self.assertEqual(self.evidence.transport_totals['codex_exec_counted'], 0)
        self.assertEqual(self.evidence.roots, 0)

    def test_batch_rejection_after_root_seen_reports_seen_unresolved(self):
        bad = {'name': 'tool-span', 'startTimeUnixNano': 1, 'endTimeUnixNano': 2, 'events': [
            {'name': 'e', 'attributes': [attr('event.name', 'codex.tool_call_received'),
                                         attr('tool_name', 'not-a-tool'),
                                         attr('tool_namespace', 'functions'),
                                         attr('call_id', CALL),
                                         attr('conversation.id', CONV),
                                         attr('tool_source', 'direct')]}]}
        with self.assertRaises(ValueError):
            self.evidence.accept('/v1/traces', traces_body([root_span(), bad]))
        self.assertTrue(self.evidence.invalid)
        self.assertEqual(self.evidence.roots, 1)  # root still ingested before the failure
        event = self.evidence.transport_events[-1]
        self.assertEqual(event['reason_code'], 'TOOL_CALL_EVENT')
        self.assertEqual(event['root_span_present'], 'seen_unresolved')

    def test_unknown_span_events_ignored_like_the_receipt_contract(self):
        self.evidence.accept('/v1/traces', traces_body([
            {'name': 'codex.exec', 'startTimeUnixNano': 1, 'endTimeUnixNano': 2,
             'events': [{'name': 'other', 'attributes': [attr('event.name', 'unrelated.x')]}]}]))
        self.assertFalse(self.evidence.invalid)
        self.assertEqual(self.evidence.transport_events[-1]['root_span_present'], 'accepted')

    def test_no_raw_values_or_exception_text_in_diagnostics(self):
        secret_conv, secret_call = 'secret_conversation_42', 'call_secret0000'
        try:
            self.evidence.accept('/v1/logs', b'{"resourceLogs": [{"scopeLogs": [{}]}]}')
        except ValueError:
            pass
        self.evidence.accept('/v1/logs', conversation_body(conv=secret_conv))
        self.evidence.accept('/v1/traces', traces_body([{**root_span(),
            'events': [call_event('codex.tool_call_received', 'curr_time',
                                  call_id=secret_call)]}]))
        self.evidence.accept('/v1/logs', logs_body([tool_record('curr_time', call_id=secret_call)]))
        receipt = self.evidence.receipt()
        # The raw identifiers may appear in the base receipt (long-standing
        # contract); the transport diagnostics block must never carry them.
        dump = json.dumps(receipt['transport_diagnostics'])
        self.assertNotIn(secret_conv, dump)
        self.assertNotIn(secret_call, dump)
        for event in self.evidence.transport_events:
            for value in event.values():
                self.assertNotIsInstance(value, type(ValueError('text')))

    def test_events_capped_at_limit(self):
        for _ in range(TRANSPORT_MAX_EVENTS + 6):
            try:
                self.evidence.accept('/v1/logs', b'nope')
            except ValueError:
                pass
        self.assertEqual(len(self.evidence.transport_events), TRANSPORT_MAX_EVENTS)
        self.assertEqual([e['event_sequence'] for e in self.evidence.transport_events],
                         list(range(1, TRANSPORT_MAX_EVENTS + 1)))

    def test_size_buckets_are_fixed_enums(self):
        from aee.mcp_runtime.native_telemetry import size_bucket
        self.assertEqual([size_bucket(s) for s in (0, 4096, 65536, 524288, MAX_BODY, MAX_TOTAL)],
                         ['0-4KiB', '0-4KiB', '4-64KiB', '64-512KiB', '512KiB-2MiB', '>2MiB'])


class CollectorFramingHTTP(unittest.TestCase):
    def setUp(self):
        self.collector = Collector()
        thread = threading.Thread(target=self.collector.serve_forever,
                                  kwargs={'poll_interval': 0.02}, daemon=True)
        thread.start()
        self.thread = thread

    def tearDown(self):
        self.collector.shutdown()
        self.collector.server_close()

    def post(self, path, body=None, headers=None, declared=None):
        connection = http.client.HTTPConnection('127.0.0.1', 18081, timeout=5)
        try:
            send = dict(headers or {})
            if declared is not None:
                send['Content-Length'] = str(declared)
            connection.request('POST', path, body=body, headers=send)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_json_content_length_over_size_headers(self):
        payload = logs_body([tool_record('curr_time')]).encode()
        status, _ = self.post('/v1/logs', payload,
                              {'Content-Type': 'application/json'})  # ok baseline
        self.assertEqual(status, 200)
        status, _ = self.post('/v1/metrics', b'{}', {'Content-Type': 'application/json'})
        self.assertEqual(status, 400)
        status, _ = self.post('/v1/logs', b'x', {'Content-Type': 'application/json'}, declared=0)
        self.assertEqual(status, 400)
        status, _ = self.post('/v1/logs', None, {'Content-Type': 'application/json'},
                              declared=MAX_BODY + 1)
        self.assertEqual(status, 400)
        self.assertEqual(self.collector.evidence.transport_events[1]['reason_code'],
                         'PATH_UNRECOGNIZED')
        self.assertEqual(self.collector.evidence.transport_events[2]['reason_code'], 'EMPTY_BODY')
        self.assertEqual(self.collector.evidence.transport_events[3]['reason_code'], 'SIZE_OVER')
        self.assertTrue(self.collector.evidence.invalid)  # each framing reject flags invalid

    def test_truncated_body_rejected_with_bucket(self):
        import socket
        client = socket.create_connection(('127.0.0.1', 18081), timeout=5)
        try:
            client.sendall(b'POST /v1/logs HTTP/1.1\r\nHost: collector\r\n'
                           b'Content-Type: application/json\r\nContent-Length: 4096\r\n\r\n'
                           b'{"partial":')
            client.shutdown(socket.SHUT_WR)  # EOF mid-body: the read falls short
            client.recv(64)
        finally:
            client.close()
        event = self.collector.evidence.transport_events[-1]
        self.assertEqual((event['phase'], event['reason_code'], event['payload_size_bucket']),
                         ('framing', 'BODY_TRUNCATED', '0-4KiB'))
        self.assertTrue(self.collector.evidence.invalid)
        self.assertEqual(self.collector.evidence.transport_totals['invalid_sites']['framing'], 1)

    def test_encoding_and_type_rejected(self):
        status, _ = self.post('/v1/logs', b'{}',
                              {'Content-Type': 'application/json', 'Content-Encoding': 'gzip'})
        self.assertEqual(status, 400)
        status, _ = self.post('/v1/logs', b'{}', {'Content-Type': 'text/plain'})
        self.assertEqual(status, 400)
        codes = [e['reason_code'] for e in self.collector.evidence.transport_events]
        self.assertIn('ENCODING_REJECTED', codes)
        self.assertIn('CONTENT_TYPE_REJECTED', codes)

    def test_accepted_over_http_uses_same_path_as_direct(self):
        status, _ = self.post('/v1/logs', conversation_body().encode(),
                              {'Content-Type': 'application/json'})
        self.assertEqual(status, 200)
        event = self.collector.evidence.transport_events[-1]
        self.assertEqual((event['phase'], event['outcome'], event['records_bucket']),
                         ('ingest', 'accepted', '1-16'))
        self.assertFalse(self.collector.evidence.invalid)


class ExecutorCarriesTransportDiagnostics(unittest.TestCase):
    def test_mirror_is_scalar_bounded_and_filtered(self):
        receipt = {'complete': False,
                   'transport_diagnostics': {
                       'schema_version': 1,
                       'totals': {'requests_total': 5, 'junk': {'nested': 'dropped'},
                                  'accepted_requests': 2},
                       'events': [{'event_sequence': 1, 'phase': 'framing', 'outcome': 'rejected',
                                   'reason_code': 'BODY_TRUNCATED', 'payload_size_bucket': '0-4KiB',
                                   'leak': {'not': 'a scalar'}},
                                  'not-a-dict'] + [{'event_sequence': i, 'phase': 'ingest',
                                                    'outcome': 'accepted'} for i in range(2, 42)]}}
        failure = augmented_evidence_failure({}, receipt, 'thr', None)
        state = failure['receipt_state']
        block = state['transport_diagnostics']
        self.assertEqual(block['schema_version'], 1)
        self.assertEqual(block['totals']['requests_total'], 5)
        self.assertNotIn('junk', block['totals'])
        # events[:16] slices before dropping non-dict entries: 15 cleaned ones
        # here, and the mirror never exceeds 16 regardless.
        self.assertEqual(len(block['events']), 15)
        self.assertLessEqual(len(block['events']), 16)
        self.assertEqual(block['events'][0]['reason_code'], 'BODY_TRUNCATED')
        self.assertNotIn('leak', block['events'][0])
        self.assertNotIn('transport_diagnostics',
                         augmented_evidence_failure({}, {'complete': True}, 'thr', None)['receipt_state'])