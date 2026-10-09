import hashlib
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

from aee.mcp_runtime.migration import migrate, rollback_copy
from aee.mcp_runtime.packaging import build_bundle, fixture_pointer, verify_bundle, safe_output, runtime_manifest_plan
from aee.mcp_runtime.profiles import resolve_codex
from aee.mcp_runtime.readiness import notify, wait_health
from aee.mcp_runtime.runtime import resolve_executor, require_deployment_ready
from aee.mcp_runtime.resource_policy import PROFILE_PATH, resource_record
from aee.mcp_runtime.sandbox_policy import PROFILE_PATH as SANDBOX_PROFILE, sandbox_record, FAILURE_REVISION
from aee.mcp_runtime.store import JobError, JobStore

ROOT=Path(__file__).resolve().parents[2]


class P2CPinIntegrity(unittest.TestCase):
    def test_sandbox_pin_matches_profile_bytes_and_cross_references(self):
        profile=json.loads(SANDBOX_PROFILE.read_text())
        self.assertEqual(profile['bwrap_path'], '/usr/bin/bwrap')
        self.assertRegex(profile['bwrap_sha256'], r'^[a-f0-9]{64}$')
        self.assertRegex(hashlib.sha256(SANDBOX_PROFILE.read_bytes()).hexdigest(), r'^[a-f0-9]{64}$')
        self.assertEqual(profile['requires_resource_revision'], resource_record()['id'])
        self.assertEqual(profile['failure_semantics'], FAILURE_REVISION)
        self.assertEqual(sandbox_record(),
                         {'id': profile['revision'],
                          'sha256': hashlib.sha256(SANDBOX_PROFILE.read_bytes()).hexdigest(),
                          'failure_semantics': FAILURE_REVISION})
        self.assertTrue(profile['bwrap_provenance']['release'].startswith('https://github.com/openai/codex/releases/tag/'))


class P2CDeployment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(prefix='aee-p2c-packaging-tests-')
        cls.root=Path(cls.tmp.name);cls.repo=cls.root/'reviewed';cls.repo.mkdir()
        (cls.repo/'requirements-mcp.lock').write_text('reviewed synthetic lock\n')
        (cls.repo/'safe.txt').write_text('reviewed source')
        units=cls.repo/'config/p2c/systemd';units.mkdir(parents=True)
        (units/'fixture.service').write_text('[Service]\nExecStart=/usr/bin/true\n')
        (cls.repo/'config/p2c/resource-profile.json').write_bytes(PROFILE_PATH.read_bytes())
        (cls.repo/'config/p2c/sandbox-profile.json').write_bytes(SANDBOX_PROFILE.read_bytes())
        subprocess.run(['git','init','-q',str(cls.repo)],check=True)
        subprocess.run(['git','-C',str(cls.repo),'add','--','safe.txt','requirements-mcp.lock',
                        'config/p2c/systemd/fixture.service','config/p2c/resource-profile.json',
                        'config/p2c/sandbox-profile.json'],check=True)
        subprocess.run(['git','-C',str(cls.repo),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
                        'commit','-qm','reviewed fixture'],check=True)
        cls.native=cls.root/'native';cls.native.mkdir()
        subprocess.run(['/usr/bin/gcc','-O2',str(ROOT/'tests/mcp/fixtures/codex_probe.c'),'-o',str(cls.native/'codex')],check=True)
        (cls.native/'codex-code-mode-host').write_bytes((cls.native/'codex').read_bytes())
        (cls.native/'codex-code-mode-host').chmod(0o755)

    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='aee-p2c-deployment-');self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)

    def build(self,name='release'):
        root=self.path/name
        build_bundle(self.repo,root,self.native/'codex')
        return root

    def test_manifest_deterministic_stage_and_integrity(self):
        root=self.build();a=verify_bundle(root)
        self.assertEqual(a['source_commit'],subprocess.check_output(['git','-C',str(self.repo),'rev-parse','HEAD'],text=True).strip())
        self.assertEqual(a['codex']['version'],'codex-cli 0.0.0-fixture')
        self.assertEqual(a['native_provider_compatibility'],'BLOCKED_UNVERIFIED_EXECUTOR')
        self.assertEqual(a['native_tool_runtime_compatibility'],'BLOCKED_UNVERIFIED_EXECUTOR')
        self.assertEqual(a['resource_containment'],'BLOCKED_RESOURCE_EVIDENCE_REQUIRED')
        self.assertEqual(a['resource_policy_revision'],resource_record())
        self.assertEqual(a['sandbox_policy_revision'],sandbox_record())
        self.assertEqual(a['job_failure_contract'],FAILURE_REVISION)
        self.assertEqual(a['job_failure_semantics'],'BLOCKED_R3_EVIDENCE_REQUIRED')
        plan=runtime_manifest_plan(root,'/opt/aee/releases/'+a['source_commit'])
        self.assertTrue(plan['codex']['path'].startswith('/opt/aee/releases/'))
        self.assertEqual(plan['deployment_status'],'STAGE1_NOT_DEPLOYABLE')
        self.assertEqual(a['deployment_status'],'STAGE1_NOT_DEPLOYABLE')
        with self.assertRaises(JobError) as gate:require_deployment_ready(a)
        self.assertEqual(gate.exception.code,'DEPLOYMENT_GATE_CLOSED')
        self.assertEqual(a['python']['dependency_lock_sha256'],hashlib.sha256((self.repo/'requirements-mcp.lock').read_bytes()).hexdigest())
        p=root/'source/safe.txt';p.chmod(0o600);p.write_text('tampered')
        with self.assertRaises(JobError):verify_bundle(root)

    def test_atomic_pointer_and_rollback_fixture(self):
        a=self.build('a');b=self.build('b')
        fixture_pointer(self.path,a);self.assertEqual((self.path/'current').resolve(),a)
        fixture_pointer(self.path,b);self.assertEqual((self.path/'current').resolve(),b)
        fixture_pointer(self.path,a);self.assertEqual((self.path/'current').resolve(),a)

    def test_dirty_candidate_live_and_system_output_denied(self):
        for path in ['/opt/aee/new','/etc/aee','/home/operator/aee/new',str(Path.home()/'worktrees/new'),'/tmp']:
            with self.subTest(path=path),self.assertRaises(JobError):safe_output(path)
        marker=self.repo/'unreviewed';marker.write_text('unreviewed')
        try:
            with self.assertRaises(JobError):self.build()
        finally:marker.unlink()

    def test_runtime_identity_path_digest_version_and_companion_drift(self):
        root=self.build();path=root/'deployment-manifest.json'
        with patch.dict(os.environ,{'AEE_RUNTIME_MANIFEST':str(path)}):
            identity=resolve_executor(str(root/'runner/codex'))
            self.assertEqual(identity.executable,str(root/'runner/codex'))
            with self.assertRaises(JobError):resolve_executor(str(self.native/'codex'))
            manifest=json.loads(path.read_text());manifest['codex']['version']='codex-cli unsupported'
            path.chmod(0o600);path.write_text(json.dumps(manifest))
            with self.assertRaises(JobError) as error:resolve_executor(str(root/'runner/codex'))
            self.assertEqual(error.exception.code,'EXECUTOR_IDENTITY_MISMATCH')
            manifest['codex']['version']=identity.version;manifest['codex']['code_mode_host']['sha256']='0'*64
            path.write_text(json.dumps(manifest))
            with self.assertRaises(JobError):resolve_executor(str(root/'runner/codex'))

    def legacy_store(self):
        source=self.path/'old';store=JobStore(source)
        first=store.create('codex','synthetic evidence','/workspace','read_only')
        store.update(first['job_id'],status='running',summary='preserve evidence')
        store.close()
        return source,first['job_id']

    def test_migration_dry_run_apply_evidence_permissions_and_rollback(self):
        source,job=self.legacy_store();original=(source/(job+'.json')).read_bytes()
        backup=self.path/'backup';target=self.path/'new'
        preview=migrate(source,target,backup)
        self.assertEqual(preview['interrupted'],1);self.assertFalse(target.exists());self.assertFalse(backup.exists())
        migrate(source,target,backup,apply=True)
        self.assertEqual((source/(job+'.json')).read_bytes(),original)
        new=JobStore(target)
        try:
            record=new.get(job);self.assertEqual(record['error_code'],'EXECUTION_INTERRUPTED')
            self.assertEqual(record['summary'],'preserve evidence')
        finally:new.close()
        self.assertEqual((target/(job+'.json')).stat().st_mode&0o777,0o600)
        restored=self.path/'restored'
        self.assertTrue(rollback_copy(backup,restored)['backup_verified']);self.assertFalse(restored.exists())
        rollback_copy(backup,restored,apply=True)
        self.assertEqual((restored/(job+'.json')).read_bytes(),original)

    def test_migration_unknown_entry_busy_and_corrupt_backup_fail_closed(self):
        source,job=self.legacy_store();(source/'unknown').write_text('review required')
        with self.assertRaises(JobError):migrate(source,self.path/'new',self.path/'backup')
        (source/'unknown').unlink()
        migrate(source,self.path/'new',self.path/'backup',apply=True)
        (self.path/'backup'/(job+'.json')).write_text('tampered')
        with self.assertRaises(JobError):rollback_copy(self.path/'backup',self.path/'restored',apply=True)
        self.assertFalse((self.path/'restored').exists())

    def test_migration_refuses_live_lease_and_overlapping_paths(self):
        source=self.path/'old';store=JobStore(source)
        store.create('codex','synthetic task','/workspace','read_only')
        try:
            with self.assertRaises(JobError) as busy:migrate(source,self.path/'new',self.path/'backup')
            self.assertEqual(busy.exception.code,'BUSY')
            with self.assertRaises(JobError):migrate(source,source/'nested',self.path/'backup')
        finally:store.close()

    def test_notify_socket_and_readiness_exact_identity_no_baseline_false_positive(self):
        path=str(self.path/'notify');server=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM)
        server.bind(path);server.settimeout(1);self.addCleanup(server.close)
        with patch.dict(os.environ,{'NOTIFY_SOCKET':path}):notify('READY=1')
        self.assertEqual(server.recv(64),b'READY=1')
        class Response:
            status=200
            def __init__(self,source):self.source=source
            def read(self,maximum):return json.dumps({'status':'healthy','runtime':{'source_commit':self.source}}).encode()
        with patch('http.client.HTTPConnection') as connection:
            connection.return_value.getresponse.side_effect=[Response('baseline'),Response('candidate')]
            wait_health(8791,1,expected_source='candidate')
            self.assertEqual(connection.return_value.request.call_count,2)
        with patch('http.client.HTTPConnection') as connection:
            connection.return_value.getresponse.return_value=Response('baseline')
            with self.assertRaises(RuntimeError):wait_health(8791,.05,expected_source='candidate')

    def test_systemd_candidates_parse_in_offline_render(self):
        unitroot=ROOT/'config/p2c/systemd';render=self.path/'units';render.mkdir()
        for p in unitroot.glob('*'):
            if p.is_file():
                text=p.read_text().replace('/opt/aee/venvs/mcp-py313/bin/python','/usr/bin/python3')
                (render/p.name).write_text(text)
        result=subprocess.run(['systemd-analyze','verify','--man=no','--generators=no',
                               str(render/'aee-p2c-broker.service'),str(render/'aee-p2c-gateway@restricted.service')],
                              capture_output=True,text=True,env={**os.environ,'SYSTEMD_UNIT_PATH':str(render)+':/usr/lib/systemd/system'})
        self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':unittest.main()
