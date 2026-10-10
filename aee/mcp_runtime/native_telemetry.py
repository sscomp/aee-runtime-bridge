"""Job-private native OTLP collector; no raw arguments/output are persisted.

Pinned 0.159.2 emits structured tool lifecycle/results. Command tool success
means dispatch success, so its machine-generated result header is decoded too.
Only the immutable relay emits this AEE receipt after native shutdown/flush.
The collector has no broker/upstream route and is unreachable by native tools'
network seccomp sandbox. Missing, conflicting or over-limit evidence fails shut.
"""
import hashlib
import http.server
import json
import os
import re
import threading

REVISION = 'codex-native-otlp-r3-1'
MAX_BODY = 2 * 1024**2
MAX_TOTAL = 16 * 1024**2
MAX_CALLS = 128
ID = re.compile(r'[A-Za-z0-9_-]{1,100}')
TOOLS = {'exec', 'wait', 'exec_command', 'write_stdin', 'view_image', 'apply_patch', 'curr_time'}

# Bounded transport-phase diagnostics (Stage 3B R2 transport-capture design):
# fixed enum fields and size buckets only; emitted into the receipt solely on
# the incomplete branch and solely when AEE_TRANSPORT_DIAGNOSTICS=1. They are
# never an input to the ``complete`` decision and never carry raw payload,
# identifiers or exception text.
TRANSPORT_SCHEMA_VERSION = 1
TRANSPORT_MAX_EVENTS = 16
_SIZE_BUCKETS = ((4 * 1024, '0-4KiB'), (64 * 1024, '4-64KiB'), (512 * 1024, '64-512KiB'),
                 (MAX_BODY, '512KiB-2MiB'))


def size_bucket(size):
    for limit, name in _SIZE_BUCKETS:
        if size <= limit:
            return name
    return '>2MiB'


def count_bucket(count):
    if count <= 0:
        return '0'
    if count <= 16:
        return '1-16'
    if count <= 64:
        return '17-64'
    if count <= 256:
        return '65-256'
    return '>256'


class _FlaggedRejection(ValueError):
    """Stage rejection whose transport event already emitted (internal marker)."""


def attributes(items):
    result = {}
    if not isinstance(items, list) or len(items) > 128:
        raise ValueError()
    for item in items:
        key, value = item['key'], item['value']
        if key in result or not isinstance(key, str) or not isinstance(value, dict) or len(value) != 1:
            raise ValueError()
        kind, scalar = next(iter(value.items()))
        if kind in {'stringValue', 'boolValue', 'intValue', 'doubleValue'}:
            result[key] = scalar
    return result


def bounded_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError()
    return value


def hashed_element(value, limit=16):
    """Safe bounded hash of a call identity; never the raw identifier."""
    return hashlib.sha256(value.encode()).hexdigest()[:limit]


def tool_identity(a):
    name, namespace = a['tool_name'], a['tool_namespace']
    if name not in TOOLS or namespace not in {'functions', 'clock'}:
        raise ValueError()
    return bounded_id(a['call_id']), name, bounded_id(a['conversation.id'])


def success(value):
    if value is True or value == 'true':
        return True
    if value is False or value == 'false':
        return False
    raise ValueError()


def command_exit(output):
    # Decode only the pinned native header BEFORE Output:, never command text,
    # script stdout, model claims or a substring chosen by model code.
    if not isinstance(output, str) or '\nOutput:\n' not in output[:512]:
        raise ValueError()
    header = output[:512].split('\nOutput:\n', 1)[0]
    match = re.fullmatch(
        r'(?:Chunk ID: [a-zA-Z0-9_-]{1,40}\n)?Wall time: [0-9]+\.[0-9]{4} seconds\n'
        r'Process exited with code (-?[0-9]+)(?:\nOriginal token count: [0-9]+)?', header)
    if not match:
        # Running sessions/unrecognized headers cannot establish terminal success.
        raise ValueError()
    value = int(match[1])
    if not -255 <= value <= 255:
        raise ValueError()
    return value


class Evidence:
    def __init__(self):
        self.lock = threading.RLock()
        self.invalid = False
        self.total = self.requests = self.roots = 0
        self.calls, self.ready, self.logs, self.traces = {}, {}, {}, {}
        self.conversations = set()
        self.transport_events = []
        self.transport_totals = {'accepted_requests': 0, 'rejected_requests': 0,
                                 'codex_exec_seen': 0, 'codex_exec_counted': 0,
                                 'invalid_sites': {'framing': 0, 'budget': 0, 'parse': 0,
                                                   'validation': 0, 'transport': 0}}

    def transport_event(self, phase, outcome, reason_code=None, size=None, records=None,
                        root_present=None):
        """Record one bounded transport-phase event; enums and buckets only.

        Never raw text, identifiers, payload or exception messages; capped list;
        pure observation on the receipt path, never a decision input.
        """
        with self.lock:
            if outcome == 'accepted':
                self.transport_totals['accepted_requests'] += 1
            elif outcome == 'rejected':
                self.transport_totals['rejected_requests'] += 1
                site = {'framing': 'framing', 'budget': 'budget', 'parse': 'parse',
                        'ingest': 'validation', 'transport': 'transport'}.get(phase)
                if site is not None:
                    self.transport_totals['invalid_sites'][site] += 1
            if len(self.transport_events) >= TRANSPORT_MAX_EVENTS:
                return
            event = {'event_sequence': len(self.transport_events) + 1,
                     'phase': phase, 'outcome': outcome}
            if reason_code is not None:
                event['reason_code'] = reason_code
            if size is not None:
                event['payload_size_bucket'] = size_bucket(size)
            if records is not None:
                event['records_bucket'] = count_bucket(records)
            if root_present is not None:
                event['root_span_present'] = root_present
            self.transport_events.append(event)

    def put(self, target, key, value):
        if key in target and target[key] != value:
            raise ValueError()
        if key not in target and len(target) >= MAX_CALLS:
            raise ValueError()
        target[key] = value

    def tool(self, a, *, log):
        call, name, conversation = tool_identity(a)
        seq = int(a['tool_result_seq'])
        if not 1 <= seq <= MAX_CALLS:
            raise ValueError()
        record = {'call_id': call, 'tool_name': name, 'conversation': conversation,
                  'sequence': seq, 'success': success(a['success'])}
        if log:
            if name in {'exec_command', 'write_stdin'} and record['success']:
                try:
                    record['exit_code'] = command_exit(a.get('output'))
                except ValueError:
                    record['unverified_completion'] = True
            if name in {'exec', 'wait'} and record['success']:
                # A yielded/terminated cell is not verified completion. R3 uses
                # a conservative synchronous-cell policy, with no recovery bypass.
                output = a.get('output')
                if not isinstance(output, str) or not output.startswith('Script completed\nWall time '):
                    record['unverified_completion'] = True
            if name == 'apply_patch':
                record['readonly_violation'] = True
        self.put(self.logs if log else self.traces, call, record)

    def accept(self, path, payload):
        with self.lock:
            self.requests += 1
            self.total += len(payload)
            if self.requests > 256 or self.total > MAX_TOTAL:
                self.invalid = True
                self.transport_event('budget', 'rejected',
                                     reason_code='REQUEST_BUDGET' if self.requests > 256 else 'TOTAL_BUDGET',
                                     size=len(payload))
                raise ValueError()
            # Diagnostics only: stage/reason tracking changes no raise sites, no
            # accepted/rejected conditions, and no exception classes caught.
            stage = 'parse'
            state = {'records': None, 'seen_roots': 0, 'counted_roots': 0}
            try:
                body = json.loads(payload)
                stage = 'body_shape'
                if not isinstance(body, dict):
                    raise ValueError()
                state['records'] = 0
                if path == '/v1/logs':
                    for resource in body['resourceLogs']:
                        stage = 'logs_structure'
                        for scope in resource['scopeLogs']:
                            for item in scope['logRecords']:
                                a = attributes(item.get('attributes', []))
                                event_name = a.get('event.name')
                                if event_name == 'codex.tool_result':
                                    stage = 'tool_result_log'
                                    self.tool(a, log=True)
                                    state['records'] += 1
                                elif event_name == 'codex.conversation_starts':
                                    stage = 'conversation_record'
                                    self.conversations.add(bounded_id(a['conversation.id']))
                                    state['records'] += 1
                elif path == '/v1/traces':
                    for resource in body['resourceSpans']:
                        stage = 'traces_structure'
                        for scope in resource['scopeSpans']:
                            for span in scope['spans']:
                                stage = 'traces_span'
                                if span.get('name') == 'codex.exec':
                                    state['seen_roots'] += 1
                                    self.transport_totals['codex_exec_seen'] += 1
                                    if int(span['endTimeUnixNano']) <= int(span['startTimeUnixNano']):
                                        self.transport_event('ingest', 'rejected',
                                                             reason_code='SPAN_ORDER_INVALID',
                                                             size=len(payload),
                                                             records=state['records'],
                                                             root_present='rejected_order')
                                        raise _FlaggedRejection()
                                    self.roots += 1
                                    self.transport_totals['codex_exec_counted'] += 1
                                    state['counted_roots'] += 1
                                    state['records'] += 1
                                stage = 'traces_span_events'
                                for item in span.get('events', []):
                                    a = attributes(item.get('attributes', []))
                                    event = a.get('event.name')
                                    if event == 'codex.tool_result':
                                        stage = 'tool_result_event'
                                        self.tool(a, log=False)
                                        state['records'] += 1
                                    elif event in {'codex.tool_call_received', 'codex.tool_result_ready'}:
                                        stage = 'tool_call_event'
                                        call, name, conversation = tool_identity(a)
                                        source = a['tool_source']
                                        if source not in {'direct', 'code_mode'}:
                                            raise ValueError()
                                        self.put(self.calls if event == 'codex.tool_call_received' else self.ready,
                                                 call, {'tool_name': name, 'conversation': conversation,
                                                        'source': source})
                                        state['records'] += 1
                else:
                    stage = 'unknown_path'
                    raise ValueError()
            except (ValueError, TypeError, KeyError, OverflowError, AttributeError) as error:
                self.invalid = True
                if isinstance(error, _FlaggedRejection):
                    raise ValueError() from None
                if stage in ('parse', 'body_shape'):
                    self.transport_event('parse', 'rejected',
                                         reason_code='PARSE_NOT_JSON' if stage == 'parse' else 'BODY_NOT_OBJECT',
                                         size=len(payload))
                else:
                    self.transport_event('ingest', 'rejected', reason_code=stage.upper(),
                                         size=len(payload), records=state['records'],
                                         root_present=('seen_unresolved' if state['seen_roots']
                                                       and path == '/v1/traces' else None))
                raise ValueError() from None
            root_present = None
            if path == '/v1/traces':
                root_present = 'accepted' if state['counted_roots'] else 'absent'
            self.transport_event('ingest', 'accepted', size=len(payload),
                                 records=state['records'], root_present=root_present)

    def receipt(self):
        """Emit the AEE receipt; name each violated aggregate predicate if incomplete.

        ``complete`` is computed exactly as before (same predicates, same order,
        same short-circuit expression) — the ``incomplete_reasons`` mirror is
        additive only (emitted on the incomplete branch), never an input to the
        decision, and call identities are hashed (sha256 prefix), never raw.
        """
        with self.lock:
            keys = set(self.calls)
            ready_keys, log_keys, trace_keys = set(self.ready), set(self.logs), set(self.traces)
            complete = (not self.invalid and self.roots == 1 and len(self.conversations) == 1
                        and keys == ready_keys == log_keys == trace_keys)
            reasons = []

            def reason(predicate_id, observed, expected=None):
                if len(reasons) < 8:
                    item = {'predicate_id': predicate_id, 'observed': observed}
                    if expected is not None:
                        item['expected'] = expected
                    reasons.append(item)

            if self.invalid:
                reason('collector_invalid', True, False)
            if self.roots != 1:
                reason('root_exec_spans', self.roots, 1)
            if len(self.conversations) != 1:
                reason('conversation_count', len(self.conversations), 1)
            if not (keys == ready_keys and log_keys == ready_keys and trace_keys == ready_keys):
                # Same boolean as the chained equality above; enumerated per set
                # under a union reference so each absent membership is visible.
                union = keys | ready_keys | log_keys | trace_keys
                for name, members in (('calls', keys), ('tool_result_ready', ready_keys),
                                      ('tool_result_logs', log_keys),
                                      ('tool_result_traces', trace_keys)):
                    missing = union - members
                    if missing:
                        if len(reasons) < 8:
                            reasons.append({'predicate_id': 'four_set_membership:' + name,
                                            'observed': len(members), 'expected': len(union),
                                            'missing': sorted(hashed_element(element)
                                                              for element in missing)[:16]})
                        complete = False
            sequences = []
            tools = []
            for call, record in sorted(self.logs.items(), key=lambda pair: pair[1]['sequence']):
                base = {k: record[k] for k in ('call_id', 'tool_name', 'conversation', 'sequence', 'success')}
                if (self.traces.get(call) != base or self.ready.get(call) != self.calls.get(call)
                        or self.calls.get(call, {}).get('tool_name') != record['tool_name']
                        or self.calls.get(call, {}).get('conversation') != record['conversation']
                        or record['conversation'] not in self.conversations):
                    complete = False
                    if len(reasons) < 8:
                        detail = [name for name, violated in (
                            ('trace_record_mismatch', self.traces.get(call) != base),
                            ('ready_calls_record_mismatch',
                             self.ready.get(call) != self.calls.get(call)),
                            ('calls_tool_name_mismatch',
                             self.calls.get(call, {}).get('tool_name') != record['tool_name']),
                            ('calls_conversation_mismatch',
                             self.calls.get(call, {}).get('conversation') != record['conversation']),
                            ('conversation_unregistered',
                             record['conversation'] not in self.conversations)) if violated]
                        reasons.append({'predicate_id': 'per_call_pairing',
                                        'observed': hashed_element(call), 'expected': 'aligned',
                                        'detail': detail})
                sequences.append(record['sequence'])
                tools.append({**record, 'source': self.calls.get(call, {}).get('source')})
            if sequences != list(range(1, len(tools) + 1)):
                complete = False
                if len(reasons) < 8:
                    reasons.append({'predicate_id': 'sequence_contiguity',
                                    'observed': len(sequences), 'expected': '1..N',
                                    'detail': sorted(sequences)[:16]})
            receipt = {'type': 'aee.native_tool_receipt', 'revision': REVISION, 'complete': complete,
                       'conversation': next(iter(self.conversations), None), 'tools': tools}
            if not complete:
                receipt['incomplete_reasons'] = reasons
                if os.environ.get('AEE_TRANSPORT_DIAGNOSTICS', '') == '1':
                    # Additive, bounded, enum-and-bucket-only phase record; the
                    # finalize event closes the ordering question (it names the
                    # last observed phase relative to receipt emission). Nothing
                    # here can change ``complete`` or any predicate outcome.
                    self.transport_event('finalize', 'observed')
                    receipt['transport_diagnostics'] = {
                        'schema_version': TRANSPORT_SCHEMA_VERSION,
                        'totals': {'requests_total': self.requests,
                                   **self.transport_totals},
                        'events': list(self.transport_events),
                    }
            return receipt


class Collector(http.server.HTTPServer):
    # One bounded handler is sufficient for batched local exports. No unbounded
    # thread pool or background file/log buffer. Native exporter retries cannot
    # turn a collector failure into success.
    def __init__(self):
        self.evidence = Evidence()
        super().__init__(('127.0.0.1', 18081), Handler)


class Handler(http.server.BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(2)
        super().setup()

    def log_message(self, *args):
        pass

    def reply(self, code):
        self.send_response(code)
        self.send_header('Content-Length', '2')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(b'{}')

    def do_POST(self):
        evidence = self.server.evidence
        try:
            values = self.headers.get_all('Content-Length', [])
            # Same rejection set and order as the former compound predicate; the
            # sequential split only attaches the stage-specific reason code.
            if self.path not in {'/v1/logs', '/v1/traces'}:
                reason = 'PATH_UNRECOGNIZED'
            elif len(values) != 1 or not values[0].isdigit():
                reason = 'CONTENT_LENGTH_INVALID'
            elif self.headers.get('Transfer-Encoding') or self.headers.get('Content-Encoding'):
                reason = 'ENCODING_REJECTED'
            elif not self.headers.get('Content-Type', '').startswith('application/json'):
                reason = 'CONTENT_TYPE_REJECTED'
            else:
                reason = None
            if reason is not None:
                evidence.transport_event('framing', 'rejected', reason_code=reason)
                raise ValueError()
            size = int(values[0])
            if size <= 0:
                evidence.transport_event('framing', 'rejected', reason_code='EMPTY_BODY', size=size)
                raise ValueError()
            if size > MAX_BODY:
                evidence.transport_event('framing', 'rejected', reason_code='SIZE_OVER', size=size)
                raise ValueError()
            payload = self.rfile.read(size)
            if len(payload) != size:
                evidence.transport_event('framing', 'rejected', reason_code='BODY_TRUNCATED', size=size)
                raise ValueError()
            evidence.accept(self.path, payload)
            self.reply(200)
        except OSError:
            evidence.transport_event('transport', 'rejected', reason_code='IO_ERROR')
            evidence.invalid = True
            self.reply(400)
        except ValueError:
            evidence.invalid = True
            self.reply(400)

    def do_GET(self):
        self.reply(403)

    def do_CONNECT(self):
        self.reply(403)
