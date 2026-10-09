import hashlib
import io
import http.client
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aee.mcp_runtime.broker import (Broker, ControlHandler, InferenceHandler, Lease, OpenAITransport,
                                    PolicyError, UnixServer, read_credential, validate_request)
from aee.mcp_runtime.broker_client import UnixHTTP, control
from aee.mcp_runtime.executor import execute_codex
from aee.mcp_runtime.process import Limits, run_bounded
from aee.mcp_runtime.profiles import codex_arguments, resolve_codex, select_profile
from aee.mcp_runtime.runtime import broker_arguments, resolve_executor
from aee.mcp_runtime.sandbox import sandbox_command
from aee.mcp_runtime.store import JobError

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / 'fixtures'
NATIVE = Path(os.environ.get('AEE_TEST_NATIVE_CODEX', '/nonexistent/qualification/codex'))
JOB = 'A3JOB-20261003-' + 'a' * 32


def request_body():
    return {'model': 'gpt-6.1-sol', 'reasoning': {'effort': 'high'}, 'input': [
        {'role': 'user', 'content': [{'type': 'input_text', 'text': 'synthetic inspection'}]}],
        'stream': True, 'store': False}


def events():
    item = {'id': 'msg_fixture', 'type': 'message', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': 'P2C offline inference fixture', 'annotations': []}]}
    response = {'id': 'resp_fixture', 'object': 'response', 'created_at': 1, 'status': 'completed',
                'model': 'gpt-6.1-sol', 'output': [item],
                'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
    stream = [
        {'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
        {'type': 'response.output_item.added', 'output_index': 0, 'item': {**item, 'status': 'in_progress', 'content': []}},
        {'type': 'response.content_part.added', 'item_id': item['id'], 'output_index': 0, 'content_index': 0,
         'part': {'type': 'output_text', 'text': '', 'annotations': []}},
        {'type': 'response.output_text.delta', 'item_id': item['id'], 'output_index': 0, 'content_index': 0,
         'delta': 'P2C offline inference fixture'},
        {'type': 'response.output_text.done', 'item_id': item['id'], 'output_index': 0, 'content_index': 0,
         'text': 'P2C offline inference fixture'},
        {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
        {'type': 'response.completed', 'response': response},
    ]
    return b''.join(('event: ' + e['type'] + '\ndata: ' + json.dumps(e) + '\n\n').encode() for e in stream)


class FixtureTransport:
    def __init__(self):
        self.requests = []

    def open(self, body):
        self.requests.append(json.loads(body))
        return io.BytesIO(), io.BytesIO(events())


class P2CBroker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='aee-p2c-broker-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.transport = FixtureTransport()
        self.broker = Broker(self.root/'jobs', os.getuid(), self.transport)
        self.addCleanup(self.broker.close)

    def call(self, path, method='POST', target='/v1/responses', body=None, headers=None):
        c = UnixHTTP(path)
        try:
            # GET/CONNECT are rejected before reading a body. Sending the POST
            # fixture after their headers races the server's connection close.
            payload = None
            if method == 'POST' or body is not None:
                payload = json.dumps(request_body() if body is None else body)
            c.request(method, target, payload,
                      headers or {'Content-Type': 'application/json'})
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def test_validated_inference_works_and_socket_revokes(self):
        path = self.broker.create(JOB)
        status, value = self.call(path)
        self.assertEqual(status, 200)
        self.assertIn(b'P2C offline inference fixture', value)
        self.assertEqual(self.transport.requests[0]['max_output_tokens'], 16384)
        self.assertFalse(self.transport.requests[0]['store'])
        self.broker.revoke(JOB)
        self.assertFalse(Path(path).exists())

    def test_paths_destinations_connect_get_and_models_denied(self):
        path = self.broker.create(JOB)
        for method, target in [('CONNECT', 'example.invalid:443'), ('GET', '/v1/models'),
                               ('POST', 'https://example.invalid/v1/responses'),
                               ('POST', '/v1/responses?target=example.invalid'),
                               ('POST', '/v1/responses/compact')]:
            with self.subTest(target=target):
                self.assertNotEqual(self.call(path, method=method, target=target)[0], 200)
        self.assertEqual(self.transport.requests, [])

    def test_request_model_tools_remote_media_and_state_denied(self):
        variants = [{'model': 'other'}, {'reasoning': {'effort': 'low'}}, {'store': True},
                    {'tools': [{'type': 'web_search'}]}, {'tools': [{'type': 'mcp', 'server_url': 'https://example.invalid'}]},
                    {'previous_response_id': 'another-job'}, {'conversation': 'another-job'},
                    {'input': [{'type': 'message', 'content': [{'type': 'input_image', 'image_url': 'https://example.invalid'}]}]},
                    {'max_output_tokens': 999999}, {'stream': False}]
        variants += [{'input':[{'type':'additional_tools','tools':[{'type':'namespace','name':'collaboration',
                      'tools':[{'type':'function','name':'spawn_agent'}]}]}]}]
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(PolicyError):
                validate_request('POST', '/v1/responses', {}, json.dumps({**request_body(), **variant}).encode())

    def test_request_encoding_and_body_limits(self):
        for headers in [{'transfer-encoding': 'chunked'}, {'content-encoding': 'gzip'}]:
            with self.assertRaises(PolicyError):
                validate_request('POST', '/v1/responses', headers, json.dumps(request_body()).encode())
        with self.assertRaises(PolicyError):
            validate_request('POST', '/v1/responses', {}, b'x' * (2 * 1024**2 + 1))

    def test_expiry_request_budget_and_single_lease(self):
        path = self.broker.create(JOB)
        with self.assertRaises(PolicyError):
            self.broker.create(JOB.replace('a', 'b'))
        lease = self.broker.jobs[JOB][0].state
        lease.requests = 32
        self.assertEqual(self.call(path)[0], 403)
        lease.deadline = 0
        self.broker.expire()
        self.assertFalse(Path(path).exists())

    def test_response_overflow_and_total_job_budget_stop_stream(self):
        class LargeTransport:
            def open(self,body):return io.BytesIO(),io.BytesIO(b'x'*65536)
        self.broker.transport=LargeTransport();path=self.broker.create(JOB)
        with patch('aee.mcp_runtime.broker.MAX_RESPONSE',8192):
            status,body=self.call(path)
        self.assertEqual(status,200)
        self.assertLessEqual(len(body),8192)  # interrupted, never successful complete SSE
        lease=self.broker.jobs[JOB][0].state
        lease.bytes=64*1024**2
        self.assertEqual(self.call(path)[0],502)

    def test_peer_uid_denied(self):
        path = self.broker.create(JOB)
        self.broker.jobs[JOB][0].uid = os.getuid() + 1
        with self.assertRaises((OSError, http.client.RemoteDisconnected)):
            self.call(path)
        self.assertEqual(self.transport.requests, [])

    def test_credential_permissions_symlink_and_no_error_echo(self):
        key = self.root / 'upstream-credential'
        value = 'synthetic-' + 'fixture-only-material'
        key.write_text(value);key.chmod(0o600)
        self.assertEqual(read_credential(key), value)
        key.chmod(0o644)
        with self.assertRaises(PolicyError): read_credential(key)
        key.chmod(0o600)
        link = self.root / 'linked';link.symlink_to(key)
        with self.assertRaises(OSError): read_credential(link)
        class BrokenTransport:
            def open(self, body): raise RuntimeError(value)
        self.broker.transport = BrokenTransport()
        status, body = self.call(self.broker.create(JOB))
        self.assertEqual(status, 502);self.assertNotIn(value.encode(), body)

    def test_no_incoming_auth_or_proxy_headers_forwarded(self):
        path = self.broker.create(JOB)
        self.call(path, headers={'Authorization': 'Bearer fixture', 'Host': 'example.invalid',
                                'X-Forwarded-Host': 'example.invalid'})
        self.assertEqual(set(self.transport.requests[0]) - set(request_body()), {'max_output_tokens'})
        with patch('http.client.HTTPSConnection') as factory:
            response = factory.return_value.getresponse.return_value
            response.status=200;response.getheader.return_value='text/event-stream'
            OpenAITransport('synthetic-' + 'fixture-only-material').open(b'{}')
            self.assertEqual(factory.call_args.args, ('api.openai.com', 443))
            self.assertEqual(factory.return_value.request.call_args.args[:2], ('POST', '/v1/responses'))

    def test_control_socket_separate_from_job_socket(self):
        server = UnixServer(self.root/'control.sock', ControlHandler, uid=os.getuid(), state=self.broker)
        thread=threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        path = control(self.root/'control.sock', 'lease', JOB)['socket']
        self.assertEqual(self.call(path, target='/lease', body={'job_id': JOB})[0], 403)
        self.assertTrue(control(self.root/'control.sock', 'revoke', JOB)['ok'])

    def test_namespace_broker_delta_keeps_files_and_other_egress_denied(self):
        path = self.broker.create(JOB)
        binary = self.root/'fixture'
        subprocess.run(['/usr/bin/gcc', '-O2', str(ROOT/'tests/mcp/fixtures/codex_probe.c'), '-o', str(binary)], check=True)
        identity = resolve_codex(str(binary));workspace=self.root/'workspace';workspace.mkdir()
        manifest=self.root/'manifest.json';manifest.write_text('{}')
        secret=self.root/'outside-credential';secret.write_text('synthetic boundary material')
        for task, expected in [('NETWORK_CHECK','DENIED'),('READ:'+str(secret),'DENIED'),
                               ('READ:/workspace/.git/config','DENIED'),('ENV_CHECK','SAFE')]:
            result=execute_codex(identity,select_profile(),task,str(workspace),manifest,
                                 limits=Limits(timeout=5),broker_socket=path)
            self.assertEqual(result.summary,expected)

    def test_provider_arguments_are_configuration_and_task_last(self):
        argv=broker_arguments(codex_arguments(select_profile()))
        self.assertEqual(argv[-1], '-')
        self.assertIn('model_providers.aee_broker.requires_openai_auth=false', argv)
        self.assertIn('model_providers.aee_broker.supports_websockets=false', argv)
        self.assertNotIn('OPENAI_API_KEY', ' '.join(argv))

    def test_upstream_tool_call_history_without_namespace_admitted(self):
        # Real api.openai.com Responses serialization of the client's own final
        # turn omits `namespace` on custom tool call items; the reviewed R1
        # qualification fixtures carry `functions`. Only both spellings pass;
        # the name allowlist stays `exec` (read-only) — any other name or
        # namespace is a policy bypass attempt.
        tools = json.loads((FIXTURES / 'codex-r1-approved-tools.json').read_text())
        body = request_body()
        body['input'].insert(0, {'type': 'additional_tools', 'role': 'developer', 'tools': tools})
        body['input'] += [
            {'type': 'custom_tool_call', 'id': 'tcx', 'call_id': 'cx', 'input': 'text("x")',
             'status': 'completed', 'name': 'exec'},
            {'type': 'custom_tool_call_output', 'id': 'tcxout', 'call_id': 'cx', 'output': 'x'},
        ]
        self.assertEqual(json.loads(validate_request('POST', '/v1/responses', {}, json.dumps(body).encode()))['input'],
                         body['input'])
    def test_tool_call_name_and_namespace_drift_rejected(self):
        tools = json.loads((FIXTURES / 'codex-r1-approved-tools.json').read_text())
        for name, namespace in [('exec_command', None), ('spawn_agent', None), ('exec', 'unknown'),
                                ('exec', 'functions' + 'x'), ('exec', '')]:
            with self.subTest(name=name, namespace=namespace):
                body = request_body()
                body['input'].insert(0, {'type': 'additional_tools', 'role': 'developer', 'tools': tools})
                call = {'type': 'custom_tool_call', 'id': 'tcx', 'call_id': 'cx', 'input': 'x', 'name': name}
                if namespace is not None:
                    call['namespace'] = namespace
                body['input'] += [call, {'type': 'custom_tool_call_output', 'id': 'o', 'call_id': 'cx', 'output': 'x'}]
                with self.assertRaises(PolicyError):
                    validate_request('POST', '/v1/responses', {}, json.dumps(body).encode())


    @unittest.skipUnless(NATIVE.exists(), 'operator-pinned native CLI contract probe unavailable')
    def test_actual_native_cli_completes_approved_single_agent_contract(self):
        path=self.broker.create(JOB);identity=resolve_codex(str(NATIVE))
        workspace=self.root/'workspace';workspace.mkdir()
        manifest=self.root/'manifest.json';manifest.write_text('{}')
        result=execute_codex(identity,select_profile(),'Return the fixture response.',str(workspace),manifest,
                             limits=Limits(timeout=20),broker_socket=path)
        self.assertEqual(result.summary,'P2C offline inference fixture')
        self.assertEqual(result.exit_code,0)
        self.assertEqual(len(self.transport.requests),1)


if __name__ == '__main__': unittest.main()
