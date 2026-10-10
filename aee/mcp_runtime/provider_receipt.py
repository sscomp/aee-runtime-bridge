"""Bounded structural corroboration of upstream tool calls, never their input."""
import json
import re

ID = re.compile(r'[A-Za-z0-9_-]{1,100}')


class ResponseReceipt:
    def __init__(self, lease):
        self.lease = lease
        self.pending = bytearray()
        with lease.lock:
            lease.response_complete = False

    def feed(self, chunk):
        self.pending.extend(chunk)
        while b'\n' in self.pending:
            line, _, remainder = self.pending.partition(b'\n')
            self.pending = bytearray(remainder)
            if len(line) > 65536:
                self.invalidate()
                continue
            if not line.startswith(b'data:'):
                continue
            payload = line[5:].strip()
            if payload == b'[DONE]':
                continue
            try:
                event = json.loads(payload)
                if not isinstance(event, dict):
                    raise ValueError()
                if event.get('type') == 'response.output_item.done':
                    self.item(event['item'])
                elif event.get('type') == 'response.completed':
                    response = event['response']
                    for item in response['output']:
                        self.item(item)
                    with self.lease.lock:
                        self.lease.response_complete = (response.get('status') == 'completed'
                            and any(i.get('type') == 'message' and i.get('role') == 'assistant'
                                    for i in response['output']))
                elif event.get('type') in {'error', 'response.failed', 'response.incomplete'}:
                    self.invalidate()
            except (ValueError, KeyError, TypeError):
                self.invalidate()
        if len(self.pending) > 65536:
            self.pending.clear()
            self.invalidate()

    def invalidate(self):
        with self.lease.lock:
            self.lease.receipt_valid = False

    def item(self, item):
        if not isinstance(item, dict):
            raise ValueError()
        kind = item.get('type')
        if kind in {'custom_tool_call', 'function_call'}:
            call, name = item.get('call_id'), item.get('name')
            # Real api.openai.com Responses output items omit `namespace` on
            # custom_tool_call items (the request-side counterpart in
            # broker.validate_request accepts both spellings for the same
            # reason). Missing defaults to `functions`; any explicit other
            # namespace still invalidates the lease receipt.
            if (not isinstance(call, str) or not ID.fullmatch(call)
                    or name not in {'exec', 'wait', 'request_user_input_async'}
                    or item.get('namespace', 'functions') != 'functions'):
                raise ValueError()
            with self.lease.lock:
                if (len(self.lease.expected_calls) >= 128 and call not in self.lease.expected_calls
                        or call in self.lease.expected_calls and self.lease.expected_calls[call] != name):
                    raise ValueError()
                self.lease.expected_calls[call] = name

    def finish(self):
        if self.pending.strip():
            self.invalidate()
