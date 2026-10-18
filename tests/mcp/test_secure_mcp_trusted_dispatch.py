"""Secure-MCP-only authorization, hot revocation and legacy migration regression."""
import asyncio
import json
import os
import unittest
from unittest.mock import patch

import httpx
import test_os_native_autonomous as legacy
from aee.mcp_runtime.authorization import digest, authenticated_transport
from aee.mcp_runtime.trusted_dispatch import (
    TrustedDispatch, VerifiedSecureMCP, secure_mcp_transport, check_execution, PROOF)
from aee.mcp_runtime.store import JobError


class TrustedSecurity(unittest.TestCase):
    save_policy = legacy.AutonomousSecurity.save_policy
    denied = legacy.AutonomousSecurity.denied

    def setUp(self):
        legacy.AutonomousSecurity.setUp(self)
        self.policy_data = {'schema': 'aee-secure-mcp-trusted-dispatch-v1',
            'policy': 'secure_mcp_trusted_dispatch', 'transport': 'secure_mcp', 'enabled': True,
            'principals': {digest('synthetic-bridge'): 'fixture-transport'},
            'agents': ['claude', 'codex']}
        self.save_policy()
        os.environ['AEE_TRUSTED_DISPATCH_POLICY'] = str(self.policy)
        self.authority = TrustedDispatch(self.policy)
        self.context_secure = secure_mcp_transport.set(VerifiedSecureMCP(digest('synthetic-bridge')))
        self.addCleanup(secure_mcp_transport.reset, self.context_secure)
        self.g.A3_DISPATCH_ALLOWED_ROOTS = ['/absent-legacy-root']
        self.probe = patch.object(self.g, 'adapter_execution_metadata',
            side_effect=lambda agent, mode: {'kind':'job-adapter', 'adapter':{'adapter':agent}})
        self.probe_mock = self.probe.start(); self.addCleanup(self.probe.stop)
        self.worker = patch.object(self.g, '_start_job_worker')
        self.worker_mock = self.worker.start(); self.addCleanup(self.worker.stop)

    def dispatch(self, mode='autonomous', agent='claude', metadata=None):
        record = self.g.dispatch_job(agent, 'fixture task', str(self.cwd), mode, metadata=metadata)
        self.g.JOB_STORE.update(record['job_id'],status='cancelled',finished_at=self.g._now_iso(),error_code='CANCELLED')
        return record

    def test_approved_both_agents_both_modes_without_token_or_legacy_roots(self):
        for agent in ['claude', 'codex']:
            for mode in ['write', 'autonomous']:
                record = self.dispatch(mode, agent)
                proof = record['execution']['authorization']
                self.assertEqual((proof['schema'],proof['mode'],proof['principal']),
                                 (PROOF,mode,'fixture-transport'))
                self.assertNotIn('grant_id',proof)
                self.assertFalse((self.root/'grants').exists())
                self.authority.check_start(proof,agent,mode,'fixture task',str(self.cwd))

    def test_readonly_identity_and_kill_switch(self):
        self.g.A3_DISPATCH_ALLOWED_ROOTS=[]
        self.policy_data['enabled']=False; self.save_policy()
        self.assertEqual(self.dispatch('read_only')['mode'],'read_only')
        for mode in ['write','autonomous']:
            self.denied('TRUSTED_DISPATCH_DISABLED', self.dispatch, mode)
        self.assertFalse(self.authority.status()['write_autonomous_enabled'])

    def test_unknown_missing_legacy_context_and_metadata_never_trusted(self):
        for context in [None,digest('synthetic-bridge'),VerifiedSecureMCP(digest('unknown'))]:
            secure_mcp_transport.set(context)
            for mode in ['write','autonomous']:
                self.denied('TRUSTED_DISPATCH_CALLER_UNVERIFIED',self.dispatch,mode,metadata={
                    'trusted':True,'principal':'fixture-transport','mode':'autonomous',
                    'autonomous_approval':'forged','approval':'forged'})
        self.probe_mock.assert_not_called();self.worker_mock.assert_not_called()

    def test_revocation_and_kill_switch_checked_again_before_start(self):
        proof=self.dispatch()['execution']['authorization']
        self.policy_data['principals']={};self.save_policy()
        self.denied('TRUSTED_DISPATCH_CALLER_UNVERIFIED',self.dispatch)
        self.denied('TRUSTED_DISPATCH_PROOF_INVALID',self.authority.check_start,
                    proof,'claude','autonomous','fixture task',str(self.cwd))
        self.policy_data['principals']={digest('synthetic-bridge'):'fixture-transport'}
        self.policy_data['enabled']=False;self.save_policy()
        self.denied('TRUSTED_DISPATCH_DISABLED',check_execution,
                    proof,'claude','autonomous','fixture task',str(self.cwd))

    def test_revocation_while_adapter_is_probed_prevents_persistence(self):
        def revoke(*args):
            self.policy_data['principals']={};self.save_policy()
            return {'adapter':{'adapter':'claude'}}
        self.probe_mock.side_effect=revoke
        self.denied('TRUSTED_DISPATCH_PROOF_INVALID',self.dispatch)
        self.worker_mock.assert_not_called()

    def test_unqualified_unavailable_invalid_mode(self):
        self.denied('TRUSTED_DISPATCH_AGENT_UNAPPROVED',self.dispatch,agent='hermes')
        self.denied('INVALID_MODE',self.dispatch,'root')
        self.probe_mock.side_effect=JobError('ADAPTER_NOT_INSTALLED','fixture absent')
        self.denied('ADAPTER_NOT_INSTALLED',self.dispatch)
        self.worker_mock.assert_not_called()

    def test_scope_and_cwd_substitution(self):
        proof=self.dispatch()['execution']['authorization']
        for field in ['principal','mode','profile','host','os_identity','task_sha256']:
            bad={**proof,field:'forged'}
            self.denied('TRUSTED_DISPATCH_PROOF_INVALID',self.authority.check_start,
                        bad,'claude','autonomous','fixture task',str(self.cwd))
        self.cwd.rename(self.root/'old');self.cwd.mkdir()
        self.denied('TRUSTED_DISPATCH_PROOF_INVALID',self.authority.check_start,
                    proof,'claude','autonomous','fixture task',str(self.cwd))

    def test_private_policy_malformed_absent_symlink_and_no_legacy_fallback(self):
        self.policy.chmod(0o644)
        self.denied('TRUSTED_DISPATCH_NOT_CONFIGURED',self.dispatch)
        self.policy.chmod(0o600)
        for raw in ['{','{}','{"enabled":true,"enabled":false}','null']:
            self.policy.write_text(raw)
            self.denied('TRUSTED_DISPATCH_NOT_CONFIGURED',self.dispatch)
        self.policy.unlink(); self.policy.symlink_to(self.root/'absent')
        self.denied('TRUSTED_DISPATCH_NOT_CONFIGURED',self.dispatch)
        os.environ['AEE_TRUSTED_DISPATCH_POLICY']=''
        self.denied('TRUSTED_DISPATCH_NOT_CONFIGURED',self.dispatch)

    def test_missing_proof_rejected_for_every_trusted_worker_mode(self):
        for mode in ['read_only','write','autonomous']:
            self.denied('TRUSTED_DISPATCH_PROOF_INVALID',check_execution,
                        None,'claude',mode,'fixture task',str(self.cwd))

    def test_trusted_autonomous_success_requires_process_and_scoped_proof(self):
        from aee.mcp_runtime.result_contract import validate_success
        proof=self.dispatch()['execution']['authorization']
        identity=proof['os_identity'];stamp=self.g._now_iso()
        process={'pid':1234,'status':[
            'Uid: '+' '.join([str(identity['uid'])]*4),
            'Gid: '+' '.join([str(identity['gid'])]*4),
            'Groups: '+' '.join(map(str,identity['groups'])), 'NoNewPrivs: 1','CapEff: 0']}
        adapter={'adapter':'claude','mode':'autonomous','exit_code':0,'turn_completed':True,
                 'working_directory':str(self.cwd),'authorization':proof}
        record={'agent':'claude','mode':'autonomous','status':'completed','exit_code':0,
                'artifacts':[],'truncated':False,'summary':'fixture','created_at':stamp,
                'started_at':stamp,'finished_at':stamp,'working_directory':str(self.cwd),
                'execution':{'adapter_proof':adapter,'authorization':proof,
                             'native_profile':'os-native','process_identity':process}}
        self.assertTrue(validate_success(record,persisted=False))
        proof['transport']='local'
        self.denied('TOOL_EVIDENCE_INCOMPLETE',validate_success,record,persisted=False)

    def test_legacy_proof_rejected_on_trusted_deployment(self):
        self.denied('TRUSTED_DISPATCH_PROOF_INVALID',check_execution,
            {'schema':'aee-autonomous-proof-v1'},'claude','autonomous','fixture task',str(self.cwd))

    def test_http_auth_spoofing_duplicates_unapproved_transport_and_context_reset(self):
        self.g._apply_tool_exposure_filter()
        secure_mcp_transport.set(None)
        async def run():
            app=self.g._build_auth_middleware(self.g.mcp.streamable_http_app())
            async with self.g.mcp.session_manager.run():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8791') as client:
                    for mode in ['write','autonomous']:
                        body={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'aee_dispatch','arguments':{
                            'agent':'claude','task':'fixture task','working_directory':str(self.cwd),'mode':mode,
                            'metadata':{'trusted':True,'principal':'fixture-transport'}}}}
                        base={'Content-Type':'application/json','Accept':'application/json, text/event-stream',
                            'X-User':'fixture-transport','X-Forwarded-User':'fixture-transport',
                            'X-Forwarded-For':'127.0.0.1','X-Forwarded-Authorization':'Bearer synthetic-bridge'}
                        for auth in [None,'Bearer bad','Bearer','Basic synthetic-bridge']:
                            headers={**base,**({'Authorization':auth} if auth is not None else {})}
                            response=await client.post('/mcp',json=body,headers=headers)
                            self.assertEqual(response.status_code,401)
                        duplicate=list(base.items())+[('Authorization','Bearer bad'),('Authorization','Bearer synthetic-bridge')]
                        self.assertEqual((await client.post('/mcp',json=body,headers=duplicate)).status_code,401)
                        headers={**base,'Authorization':'Bearer synthetic-bridge'}
                        self.g.AEE_MCP_SURFACE='local'
                        response=await client.post('/mcp',json=body,headers=headers)
                        self.assertEqual(json.loads(response.json()['result']['content'][0]['text'])['error_code'],
                                         'TRUSTED_DISPATCH_CALLER_UNVERIFIED')
                        self.g.AEE_MCP_SURFACE='restricted'
                        response=await client.post('/mcp',json=body,headers=headers)
                        data=json.loads(response.json()['result']['content'][0]['text'])
                        self.assertTrue(data['ok'],data)
                        self.g.JOB_STORE.update(data['job_id'],status='cancelled',finished_at=self.g._now_iso(),error_code='CANCELLED')
                        self.assertIsNone(secure_mcp_transport.get())
                        self.policy_data['principals']={digest('different'):'unapproved'};self.save_policy()
                        response=await client.post('/mcp',json=body,headers=headers)
                        self.assertEqual(json.loads(response.json()['result']['content'][0]['text'])['error_code'],
                                         'TRUSTED_DISPATCH_CALLER_UNVERIFIED')
                        self.policy_data['principals']={digest('synthetic-bridge'):'fixture-transport'};self.save_policy()
        asyncio.run(run())
