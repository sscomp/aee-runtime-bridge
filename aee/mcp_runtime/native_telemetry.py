"""Job-private native OTLP collector; no raw arguments/output are persisted.

Pinned 0.159.2 emits structured tool lifecycle/results. Command tool success
means dispatch success, so its machine-generated result header is decoded too.
Only the immutable relay emits this AEE receipt after native shutdown/flush.
The collector has no broker/upstream route and is unreachable by native tools'
network seccomp sandbox. Missing, conflicting or over-limit evidence fails shut.
"""
import http.server
import json
import re
import threading

REVISION = 'codex-native-otlp-r3-1'
MAX_BODY = 2 * 1024**2
MAX_TOTAL = 16 * 1024**2
MAX_CALLS = 128
ID = re.compile(r'[A-Za-z0-9_-]{1,100}')
TOOLS = {'exec', 'wait', 'exec_command', 'write_stdin', 'view_image', 'apply_patch', 'curr_time'}


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
                raise ValueError()
            try:
                body = json.loads(payload)
                if not isinstance(body, dict):
                    raise ValueError()
                if path == '/v1/logs':
                    for resource in body['resourceLogs']:
                        for scope in resource['scopeLogs']:
                            for item in scope['logRecords']:
                                a = attributes(item.get('attributes', []))
                                if a.get('event.name') == 'codex.tool_result':
                                    self.tool(a, log=True)
                                elif a.get('event.name') == 'codex.conversation_starts':
                                    self.conversations.add(bounded_id(a['conversation.id']))
                elif path == '/v1/traces':
                    for resource in body['resourceSpans']:
                        for scope in resource['scopeSpans']:
                            for span in scope['spans']:
                                if span.get('name') == 'codex.exec':
                                    if int(span['endTimeUnixNano']) <= int(span['startTimeUnixNano']):
                                        raise ValueError()
                                    self.roots += 1
                                for item in span.get('events', []):
                                    a = attributes(item.get('attributes', []))
                                    event = a.get('event.name')
                                    if event == 'codex.tool_result':
                                        self.tool(a, log=False)
                                    elif event in {'codex.tool_call_received', 'codex.tool_result_ready'}:
                                        call, name, conversation = tool_identity(a)
                                        source = a['tool_source']
                                        if source not in {'direct', 'code_mode'}:
                                            raise ValueError()
                                        self.put(self.calls if event == 'codex.tool_call_received' else self.ready,
                                                 call, {'tool_name': name, 'conversation': conversation,
                                                        'source': source})
                else:
                    raise ValueError()
            except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
                self.invalid = True
                raise ValueError() from None

    def receipt(self):
        with self.lock:
            keys = set(self.calls)
            complete = (not self.invalid and self.roots == 1 and len(self.conversations) == 1
                        and keys == set(self.ready) == set(self.logs) == set(self.traces))
            sequences = []
            tools = []
            for call, record in sorted(self.logs.items(), key=lambda pair: pair[1]['sequence']):
                base = {k: record[k] for k in ('call_id', 'tool_name', 'conversation', 'sequence', 'success')}
                if (self.traces.get(call) != base or self.ready.get(call) != self.calls.get(call)
                        or self.calls.get(call, {}).get('tool_name') != record['tool_name']
                        or self.calls.get(call, {}).get('conversation') != record['conversation']
                        or record['conversation'] not in self.conversations):
                    complete = False
                sequences.append(record['sequence'])
                tools.append({**record, 'source': self.calls.get(call, {}).get('source')})
            if sequences != list(range(1, len(tools) + 1)):
                complete = False
            return {'type': 'aee.native_tool_receipt', 'revision': REVISION, 'complete': complete,
                    'conversation': next(iter(self.conversations), None), 'tools': tools}


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
        try:
            values = self.headers.get_all('Content-Length', [])
            if (self.path not in {'/v1/logs', '/v1/traces'} or len(values) != 1
                    or not values[0].isdigit() or self.headers.get('Transfer-Encoding')
                    or self.headers.get('Content-Encoding')
                    or not self.headers.get('Content-Type', '').startswith('application/json')):
                raise ValueError()
            size = int(values[0])
            if not 0 < size <= MAX_BODY:
                raise ValueError()
            payload = self.rfile.read(size)
            if len(payload) != size:
                raise ValueError()
            self.server.evidence.accept(self.path, payload)
            self.reply(200)
        except (ValueError, OSError):
            self.server.evidence.invalid = True
            self.reply(400)

    def do_GET(self):
        self.reply(403)

    def do_CONNECT(self):
        self.reply(403)
