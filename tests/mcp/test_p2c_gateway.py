"""Production policy and broker lifecycle against fixture-only gateway imports."""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aee.mcp_runtime.executor import ExecutionResult
from aee.mcp_runtime.profiles import resolve_codex
from aee.mcp_runtime.provider_contract import contract_record
from aee.mcp_runtime.resource_policy import resource_record
from aee.mcp_runtime.sandbox_policy import sandbox_record, FAILURE_REVISION
from aee.mcp_runtime.store import JobError

ROOT=Path(__file__).resolve().parents[2]


class P2CGateway(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='aee-p2c-gateway-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.binary=self.root/'codex';self.source=self.root/'source';self.source.mkdir()
        subprocess.run(['/usr/bin/gcc','-O2',str(ROOT/'tests/mcp/fixtures/codex_probe.c'),'-o',str(self.binary)],check=True)
        companion=self.root/'codex-code-mode-host';companion.write_bytes(self.binary.read_bytes());companion.chmod(0o755)
        identity=resolve_codex(str(self.binary))
        self.policy={'schema':1,'config_schema':'aee-p2c-1','source_commit':'a'*40,
                     'execution_profile':'codex-readonly-high','egress':'per-job-uds-responses-broker',
                     'codex':{'path':str(self.binary),'version':identity.version,'sha256':identity.sha256,
                              'code_mode_host':{'path':str(companion),'sha256':hashlib.sha256(companion.read_bytes()).hexdigest()}},
                     'native_provider_compatibility':'VERIFIED','deployment_status':'APPROVED_STAGE2',
                     'operator_approval_id':'synthetic-fixture-only'}
        self.policy['provider_contract']=contract_record()
        self.policy['native_tool_runtime_compatibility']='VERIFIED'  # synthetic C fixture only
        self.policy['resource_containment']='VERIFIED'  # fixture admission logic, not real containment
        self.policy['resource_policy_revision']=resource_record()
        self.policy.update(sandbox_policy_revision=sandbox_record(), job_failure_semantics='VERIFIED',
                           job_failure_contract=FAILURE_REVISION)
        containment=patch('aee.mcp_runtime.runtime.verify_current_containment',return_value={})
        containment.start();self.addCleanup(containment.stop)
        # This lifecycle suite executes a synthetic C probe. Native pin enforcement
        # is exercised without this seam in the R1 policy/native contract suite.
        pin=patch('aee.mcp_runtime.runtime.matches_reviewed_executor',return_value=True)
        pin.start();self.addCleanup(pin.stop)
        self.manifest=self.root/'deployment.json';self.manifest.write_text(json.dumps(self.policy))
        workspace=self.root/'workspace.json';workspace.write_text('{}')
        env={'AEE_RUNTIME_MANIFEST':str(self.manifest),'AEE_BROKER_CONTROL':str(self.root/'control.sock'),
             'A3_JOB_STORE_DIR':str(self.root/'jobs'),'A3_CODEX_BIN':str(self.binary),'A3_WORKSPACE_MANIFEST':str(workspace),
             'A3_DISPATCH_ALLOWED_ROOTS':str(self.source),'AEE_MCP_PORT':'8791','AEE_MCP_SURFACE':'restricted',
             'MCP_BRIDGE_API_KEY':'fixture-only-auth','AEE_MCP_REQUIRE_AUTH':'true'}
        self.environment=patch.dict(os.environ,env,clear=True);self.environment.start();self.addCleanup(self.environment.stop)
        name='aee_p2c_fixture_'+str(len(sys.modules));spec=importlib.util.spec_from_file_location(name,ROOT/'mcp_gateway.py')
        self.gateway=importlib.util.module_from_spec(spec);sys.modules[name]=self.gateway
        self.addCleanup(sys.modules.pop,name,None);spec.loader.exec_module(self.gateway)
        self.addCleanup(self.gateway.JOB_STORE.close)

    def dispatch(self):
        with patch.object(self.gateway,'_start_job_worker'):
            return self.gateway.dispatch_job('codex','synthetic inspection',str(self.source),'read_only')

    def test_discovery_and_dispatch_use_same_manifest_identity(self):
        discovered=self.gateway.discover_agent('codex');job=self.dispatch()
        self.assertEqual(discovered['executable'],job['execution']['configured']['executable'])
        self.assertEqual(discovered['version'],job['execution']['observed']['agent_version_probe'])

    def test_identity_drift_is_visible_in_discovery_and_dispatch(self):
        self.policy['codex']['sha256']='0'*64;self.manifest.write_text(json.dumps(self.policy))
        discovered=self.gateway.discover_agent('codex')
        self.assertEqual(discovered['error_code'],'EXECUTOR_IDENTITY_MISMATCH')
        with self.assertRaises(JobError) as error:self.dispatch()
        self.assertEqual(error.exception.code,'EXECUTOR_IDENTITY_MISMATCH')
        self.assertFalse(list((self.root/'jobs').glob('*.json')))

    def test_closed_deployment_gate_rejects_before_admission(self):
        self.policy['deployment_status']='STAGE1_NOT_DEPLOYABLE';self.manifest.write_text(json.dumps(self.policy))
        with self.assertRaises(JobError) as error:self.dispatch()
        self.assertEqual(error.exception.code,'DEPLOYMENT_GATE_CLOSED')
        self.assertFalse(list((self.root/'jobs').glob('*.json')))

    def test_broker_revoked_on_worker_failure_and_slot_released(self):
        job=self.dispatch();calls=[]
        def broker(path,action,job_id):
            calls.append((action,job_id));return {'socket':str(self.root/'jobs'/ (job_id+'.sock')),'ok':True}
        with patch.object(self.gateway,'broker_control',side_effect=broker),patch.object(
                self.gateway,'execute_codex',side_effect=JobError('EXECUTION_FAILED','Synthetic fixture failure')):
            self.gateway.run_codex_job(job['job_id'],'synthetic inspection',str(self.source))
        self.assertEqual(calls,[('lease',job['job_id']),('revoke',job['job_id'])])
        self.assertEqual(self.gateway.JOB_STORE.get(job['job_id'])['status'],'failed')
        self.assertEqual(self.dispatch()['status'],'queued')


if __name__=='__main__':unittest.main()
