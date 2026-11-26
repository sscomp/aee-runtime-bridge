"""Configured defaults must retain explicit modes and transport authorization."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from aee.mcp_runtime.authorization import digest

ROOT = Path(__file__).resolve().parents[2]


class DispatchDefault(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='aee-default-mode-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cwd = self.root / 'workspace'
        self.cwd.mkdir()
        self.env = {
            'A3_JOB_STORE_DIR': str(self.root / 'jobs'),
            'MCP_BRIDGE_API_KEY': 'synthetic-default-mode-bearer',
            'AEE_MCP_PORT': '8791', 'AEE_MCP_REQUIRE_AUTH': 'true',
        }

    def load(self, default=None, trusted=False):
        env = dict(self.env)
        if default is not None:
            env['AEE_DEFAULT_DISPATCH_MODE'] = default
        if trusted:
            policy = self.root / 'policy.json'
            policy.write_text(json.dumps({
                'schema': 'aee-secure-mcp-trusted-dispatch-v1',
                'policy': 'secure_mcp_trusted_dispatch', 'transport': 'secure_mcp',
                'enabled': True, 'agents': ['codex'],
                'principals': {digest(env['MCP_BRIDGE_API_KEY']): 'fixture'},
            }))
            policy.chmod(0o600)
            env['AEE_TRUSTED_DISPATCH_POLICY'] = str(policy)
        context = patch.dict(os.environ, env, clear=True)
        context.start()
        self.addCleanup(context.stop)
        spec = importlib.util.spec_from_file_location('default_mode_gateway', ROOT / 'mcp_gateway.py')
        gateway = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gateway)
        self.addCleanup(gateway.JOB_STORE.close)
        gateway._apply_tool_exposure_filter()
        return gateway

    def test_unconfigured_default_stays_readonly(self):
        gateway = self.load()
        self.assertEqual(gateway.AEE_DEFAULT_DISPATCH_MODE, 'read_only')

    def test_unknown_default_refuses_startup(self):
        with self.assertRaisesRegex(ValueError, 'AEE_DEFAULT_DISPATCH_MODE'):
            self.load('invalid')

    def exercise_http(self, trusted):
        gateway = self.load('autonomous', trusted=trusted)
        async def run():
            app = gateway._build_auth_middleware(gateway.mcp.streamable_http_app())
            async with gateway.mcp.session_manager.run():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             base_url='http://127.0.0.1:8791') as client:
                    headers = {'Content-Type': 'application/json',
                               'Accept': 'application/json, text/event-stream'}
                    async def rpc(method, params=None, auth=True):
                        h = dict(headers)
                        if auth:
                            h['Authorization'] = 'Bearer ' + self.env['MCP_BRIDGE_API_KEY']
                        return await client.post('/mcp', headers=h, json={
                            'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})
                    await rpc('initialize', {'protocolVersion': '2025-11-25',
                        'capabilities': {}, 'clientInfo': {'name': 'fixture', 'version': '1'}})
                    schema = (await rpc('tools/list')).json()['result']['tools']
                    dispatch = next(t for t in schema if t['name'] == 'aee_dispatch')
                    self.assertEqual(dispatch['inputSchema']['properties']['mode']['default'], 'autonomous')
                    args = {'agent': 'codex', 'task': 'synthetic task',
                            'working_directory': str(self.cwd)}
                    self.assertEqual((await rpc('tools/call', {'name': 'aee_dispatch',
                        'arguments': args}, auth=False)).status_code, 401)
                    for mode in ([None, 'read_only', 'write'] if trusted else [None]):
                        arguments = dict(args)
                        if mode is not None:
                            arguments['mode'] = mode
                        result = (await rpc('tools/call', {'name': 'aee_dispatch',
                            'arguments': arguments})).json()['result']
                        data = json.loads(result['content'][0]['text'])
                        if not trusted:
                            self.assertFalse(data['ok'])
                            self.assertEqual(data['error_code'], 'AUTONOMOUS_NOT_CONFIGURED')
                            continue
                        self.assertTrue(data['ok'], data)
                        self.assertEqual(data['mode'], mode or 'autonomous')
                        gateway.JOB_STORE.update(data['job_id'], status='cancelled',
                            finished_at=gateway._now_iso(), error_code='CANCELLED')
        with patch.object(gateway, 'adapter_execution_metadata',
                          return_value={'kind': 'job-adapter', 'adapter': {'adapter': 'codex'}}), \
             patch.object(gateway, '_start_job_worker'):
            asyncio.run(run())

    def test_default_requires_existing_autonomous_authorization(self):
        self.exercise_http(trusted=False)

    def test_trusted_default_and_explicit_modes_keep_their_boundaries(self):
        self.exercise_http(trusted=True)
