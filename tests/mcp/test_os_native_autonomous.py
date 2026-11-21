"""Autonomous capability security; synthetic admission tests, never provider qualification."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from aee.mcp_runtime.authorization import Authority, authenticated_transport, digest
from aee.mcp_runtime.event_stream import EventParser
from aee.mcp_runtime.store import JobError

ROOT = Path(__file__).resolve().parents[2]


class AutonomousSecurity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='aee-aut-security-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cwd = self.root / 'any-accessible-cwd'; self.cwd.mkdir()
        self.policy = self.root / 'policy.json'
        self.policy_data = {'schema': 'aee-autonomous-policy-v1', 'enabled': True,
                            'principals': {digest('synthetic-bridge'): 'fixture-transport'},
                            'agents': ['claude', 'codex', 'pi', 'hermes']}
        self.save_policy()
        self.authority = Authority(self.policy, self.root / 'grants')
        self.context = authenticated_transport.set(digest('synthetic-bridge'))
        self.addCleanup(authenticated_transport.reset, self.context)
        self.env = patch.dict(os.environ, {'AEE_AUTONOMOUS_POLICY': str(self.policy),
            'AEE_AUTONOMOUS_GRANTS': str(self.root / 'grants'), 'A3_JOB_STORE_DIR': str(self.root / 'jobs'),
            'MCP_BRIDGE_API_KEY': 'synthetic-bridge', 'AEE_MCP_PORT': '8791',
            'AEE_MCP_REQUIRE_AUTH': 'true', 'A3_DISPATCH_ALLOWED_ROOTS': '/nonexistent-legacy-scope'}, clear=True)
        self.env.start(); self.addCleanup(self.env.stop)
        spec = importlib.util.spec_from_file_location('autonomous_fixture_gateway', ROOT / 'mcp_gateway.py')
        self.g = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.g)
        self.addCleanup(self.g.JOB_STORE.close)

    def save_policy(self):
        self.policy.write_text(json.dumps(self.policy_data)); self.policy.chmod(0o600)

    def issue(self, **kwargs):
        return self.authority.issue(kwargs.get('principal', 'fixture-transport'), kwargs.get('agent', 'claude'),
                                    kwargs.get('task', 'fixture task'), str(kwargs.get('cwd', self.cwd)))

    def consume(self, token, **kwargs):
        return self.authority.consume(token, kwargs.get('agent', 'claude'), kwargs.get('task', 'fixture task'),
                                      str(kwargs.get('cwd', self.cwd)))

    def denied(self, code, function, *args, **kwargs):
        with self.assertRaises(JobError) as error:
            function(*args, **kwargs)
        self.assertEqual(error.exception.code, code)

    def edit_grant(self, token, change):
        path = self.root / 'grants' / (digest(token) + '.pending')
        data = json.loads(path.read_text()); change(data); path.write_text(json.dumps(data))

    def test_exact_job_proof_no_plaintext_and_restart_replay(self):
        token = self.issue(); proof = self.consume(token)
        self.assertEqual(proof['os_identity']['uid'], os.geteuid())
        self.assertNotIn(token, json.dumps(proof))
        self.assertNotIn(token, ''.join(p.read_text() for p in (self.root/'grants').iterdir()))
        fresh = Authority(self.policy, self.root / 'grants')
        self.denied('AUTONOMOUS_APPROVAL_INVALID', fresh.consume, token, 'claude', 'fixture task', str(self.cwd))
        fresh.check_start(proof, 'claude', 'fixture task', str(self.cwd))

    def test_single_consumer_under_concurrency(self):
        token = self.issue()
        def consume(_):
            c = authenticated_transport.set(digest('synthetic-bridge'))
            try:
                try: return self.consume(token)['grant_id']
                except JobError: return None
            finally: authenticated_transport.reset(c)
        with ThreadPoolExecutor(8) as pool: results = list(pool.map(consume, range(8)))
        self.assertEqual(sum(x is not None for x in results), 1)

    def test_missing_and_invalid_approval(self):
        for token in [None, '', 'bad', 'aee-aut1.' + 'x' * 43]:
            self.denied('AUTONOMOUS_APPROVAL_INVALID', self.consume, token)

    def test_wrong_caller_and_metadata_identity_cannot_replace_context(self):
        token = self.issue(); authenticated_transport.set(None)
        self.denied('AUTONOMOUS_CALLER_UNVERIFIED', self.consume, token)
        authenticated_transport.set(digest('untrusted'))
        self.denied('AUTONOMOUS_CALLER_UNVERIFIED', self.consume, token)

    def test_host_uid_groups_agent_task_and_cwd_scope(self):
        for field in ['host', 'os_identity', 'profile', 'principal', 'mode']:
            token = self.issue()
            self.edit_grant(token, lambda r: r['scope'].__setitem__(field, 'mismatch'))
            self.denied('AUTONOMOUS_SCOPE_MISMATCH', self.consume, token)
        for kwargs in [{'agent': 'codex'}, {'task': 'different'}, {'cwd': self.root}]:
            self.denied('AUTONOMOUS_SCOPE_MISMATCH', self.consume, self.issue(), **kwargs)

    def test_expired_approval(self):
        token = self.issue()
        self.edit_grant(token, lambda r: r.update(issued_at=time.time()-500, expires_at=time.time()-1))
        self.denied('AUTONOMOUS_APPROVAL_EXPIRED', self.consume, token)

    def test_revoke_pending_and_consumed(self):
        token = self.issue(); self.authority.revoke(digest(token))
        self.denied('AUTONOMOUS_APPROVAL_INVALID', self.consume, token)
        token = self.issue(); proof = self.consume(token); self.authority.revoke(digest(token))
        self.denied('AUTONOMOUS_APPROVAL_INVALID', self.authority.check_start,
                    proof, 'claude', 'fixture task', str(self.cwd))

    def test_policy_revoked_after_consumption(self):
        proof = self.consume(self.issue()); self.policy_data['enabled'] = False; self.save_policy()
        self.denied('AUTONOMOUS_NOT_CONFIGURED', self.authority.check_start,
                    proof, 'claude', 'fixture task', str(self.cwd))

    def test_policy_file_permissions_symlink_and_malformed(self):
        token = self.issue(); self.policy.chmod(0o644)
        self.denied('AUTONOMOUS_NOT_CONFIGURED', self.consume, token)
        self.policy.chmod(0o600); self.policy.write_text('{')
        self.denied('AUTONOMOUS_NOT_CONFIGURED', self.consume, token)
        self.policy.unlink(); self.policy.symlink_to(self.root/'absent')
        self.denied('AUTONOMOUS_NOT_CONFIGURED', self.consume, token)

    def test_capability_file_symlink_rejected(self):
        token = self.issue(); path = self.root/'grants'/(digest(token)+'.pending')
        saved = path.read_bytes(); path.unlink(); other = self.root/'other'; other.write_bytes(saved); other.chmod(0o600)
        path.symlink_to(other)
        self.denied('AUTONOMOUS_APPROVAL_INVALID', self.consume, token)

    def test_cwd_replaced_between_admission_and_start(self):
        proof = self.consume(self.issue()); self.cwd.rename(self.root/'old'); self.cwd.mkdir()
        self.denied('AUTONOMOUS_SCOPE_MISMATCH', self.authority.check_start,
                    proof, 'claude', 'fixture task', str(self.cwd))

    def test_autonomous_no_legacy_root_and_write_still_confined(self):
        self.assertEqual(self.g._validate_working_directory(str(self.cwd), 'autonomous'), str(self.cwd))
        self.denied('INVALID_WORKING_DIRECTORY', self.g._validate_working_directory, str(self.cwd), 'read_only')
        for cwd in ['', '.', '/nonexistent-aee-fixture', '/tmp\0bad', 17]:
            self.denied('INVALID_WORKING_DIRECTORY', self.g._validate_working_directory, cwd, 'autonomous')

    def test_no_cli_probe_or_worker_for_unapproved_request(self):
        with patch.object(self.g, 'adapter_execution_metadata') as probe, patch.object(self.g, '_start_job_worker') as worker:
            self.denied('AUTONOMOUS_APPROVAL_INVALID', self.g.dispatch_job,
                        'claude', 'fixture task', str(self.cwd), 'autonomous')
            probe.assert_not_called(); worker.assert_not_called()

    def test_invalid_profile_before_worker(self):
        with patch.object(self.g, '_start_job_worker') as worker:
            self.denied('INVALID_PROFILE', self.g.dispatch_job, 'claude', 'fixture task', str(self.cwd),
                        'autonomous', execution_profile='bypass', metadata={'autonomous_approval': self.issue()})
            worker.assert_not_called()

    def test_authenticated_mcp_context_not_forwarded_claim(self):
        self.g._apply_tool_exposure_filter()
        async def run():
            app = self.g._build_auth_middleware(self.g.mcp.streamable_http_app(**getattr(self.g, '_STREAMABLE_APP_KWARGS', {})))
            async with self.g.mcp.session_manager.run():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8791') as client:
                    body = {'jsonrpc':'2.0','id':1,'method':'tools/call','params': {'name':'aee_dispatch','arguments':
                        {'agent':'claude','task':'fixture task','working_directory':str(self.cwd),'mode':'autonomous',
                         'metadata':{'autonomous_approval':self.issue(),'principal':'forged'}}}}
                    headers={'Content-Type':'application/json','Accept':'application/json, text/event-stream'}
                    response=await client.post('/mcp',json=body,headers=headers)
                    self.assertEqual(response.status_code,401)
                    headers['Authorization']='Bearer synthetic-bridge'
                    identity = {'adapter': {'adapter':'claude'}, 'kind':'job-adapter'}
                    with patch.object(self.g,'adapter_execution_metadata',return_value=identity), patch.object(self.g,'_start_job_worker'):
                        response=await client.post('/mcp',json=body,headers=headers)
                    result=json.loads(response.json()['result']['content'][0]['text'])
                    self.assertTrue(result['ok'],result)
                    record=self.g.JOB_STORE.get(result['job_id'])
                    self.assertEqual(record['execution']['authorization']['principal'],'fixture-transport')
                    self.assertNotIn(body['params']['arguments']['metadata']['autonomous_approval'],json.dumps(record))
        asyncio.run(run())

    def test_claude_readonly_omits_every_execution_and_write_tool(self):
        from aee.mcp_runtime.job_adapters import verify_claude_readonly_capabilities
        spec = self.g.adapter_specs()['claude']
        argv = spec.build_argv('/fixture', 'claude', 'read_only')
        self.assertEqual(spec.read_only_enforcement, 'TOOL_ALLOWLIST')
        self.assertEqual(argv[argv.index('--tools') + 1], 'Read,Glob,Grep')
        self.assertIn('--restricted', argv); self.assertIn('--safe-mode', argv)
        self.assertIn('--strict-mcp-config', argv)
        self.assertEqual(argv[argv.index('--mcp-config') + 1], '{"mcpServers":{}}')
        from types import SimpleNamespace
        with patch('aee.mcp_runtime.job_adapters.run_bounded', return_value=SimpleNamespace(returncode=0, stdout=b'old CLI without controls')):
            self.denied('ADAPTER_UNSUPPORTED', verify_claude_readonly_capabilities, '/fixture/claude')
        self.assertNotIn('--restricted', spec.build_argv('/fixture','claude','autonomous'))
        self.assertIn('acceptEdits', spec.build_argv('/fixture','claude','write'))

    def test_native_tool_event_counts_do_not_store_payload(self):
        p = EventParser('codex')
        p.feed(b'{"type":"item.started","item":{"type":"command_execution"}}\n'
               b'{"type":"item.completed","item":{"type":"command_execution","exit_code":0,"text":"PRIVATE-CONTENT"}}\n'
               b'{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}\n'
               b'{"type":"turn.completed"}', eof=True)
        self.assertEqual(p.result(), 'OK')
        proof = p.tool_evidence()
        self.assertEqual((proof['tools_started'],proof['tools_completed'],proof['tools_failed']), (1,1,0))
        self.assertNotIn('PRIVATE-CONTENT', json.dumps(proof))

    def test_autonomous_result_requires_own_mode_and_os_proof(self):
        from aee.mcp_runtime.result_contract import validate_success
        from datetime import datetime, timezone
        proof = self.consume(self.issue(agent='hermes'), agent='hermes')
        stamp = datetime.now(timezone.utc).isoformat()
        identity = proof['os_identity']
        process = {'pid': 1234, 'status': [
            'Uid: ' + ' '.join([str(identity['uid'])] * 4),
            'Gid: ' + ' '.join([str(identity['gid'])] * 4),
            'Groups: ' + ' '.join(map(str, identity['groups'])), 'NoNewPrivs: 1', 'CapEff: 0']}
        adapter = {'adapter':'hermes','mode':'autonomous','read_only_enforcement':'UNSUPPORTED',
                   'exit_code':0,'turn_completed':True,'working_directory':str(self.cwd),'authorization':proof}
        record = {'agent':'hermes','mode':'autonomous','status':'completed','exit_code':0,
                  'artifacts':[],'truncated':False,'summary':'fixture', 'created_at':stamp,
                  'started_at':stamp,'finished_at':stamp,'working_directory':str(self.cwd),
                  'execution':{'adapter_proof':adapter,'authorization':proof,
                               'native_profile':'os-native','process_identity':process}}
        self.assertTrue(validate_success(record, persisted=False))
        process['status'][-2] = 'NoNewPrivs: 0'
        self.denied('TOOL_EVIDENCE_INCOMPLETE', validate_success, record, persisted=False)
        process['status'][-2] = 'NoNewPrivs: 1'
        del record['execution']['authorization']
        self.denied('TOOL_EVIDENCE_INCOMPLETE', validate_success, record, persisted=False)
        del record['execution']['adapter_proof']
        self.denied('TOOL_EVIDENCE_INCOMPLETE', validate_success, record, persisted=False)

    def test_all_mappings_and_hermes_result_failure(self):
        specs=self.g.adapter_specs()
        self.assertIn('--dangerously-bypass-approvals-and-sandbox',specs['codex'].build_argv('/x','codex','autonomous'))
        self.assertIn('--dangerously-skip-permissions',specs['claude'].build_argv('/x','claude','autonomous'))
        self.assertIn('--query-file',specs['hermes'].build_argv('/x','hermes','autonomous'))
        self.assertIn('read,bash,edit,write',specs['pi'].build_argv('/x','pi','autonomous'))
        p=EventParser('hermes');p.feed(b'{"type":"result","exit_code":0,"text":"ok"}\n',eof=True)
        self.assertEqual(p.result(),'ok')
        self.assertEqual(p.tool_evidence()['events_observed'], 1)
        p=EventParser('hermes');p.feed(b'{"type":"tool_result","is_error":true}\n{"type":"result","exit_code":0,"text":"ok"}',eof=True)
        self.denied('REQUIRED_TOOL_FAILED',p.result)

if __name__=='__main__':unittest.main()
