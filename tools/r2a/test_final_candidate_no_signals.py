"""Pure synthetic approval/postcondition tests. No production approval, keys or signals."""
import sys, os, signal

def deny_signals(event,args):
    if event in ('os.kill','os.killpg','signal.pidfd_send_signal'):
        raise AssertionError('REAL_SIGNAL_FORBIDDEN_IN_TESTS')

# Install before third-party and candidate imports, including discovery/import usage.
sys.addaudithook(deny_signals)

# Python 3.11's pidfd API does not emit the assumed audit event. Explicit
# deny functions also protect every unmocked signal API before candidate import.
def forbidden_signal(*args,**kwargs):
    raise AssertionError('REAL_SIGNAL_FORBIDDEN_IN_TESTS')
os.kill=forbidden_signal
os.killpg=forbidden_signal
signal.pidfd_send_signal=forbidden_signal

from pathlib import Path
from datetime import datetime,timedelta,timezone
import base64,copy,importlib.util,json,os,tempfile,threading,time,unittest,hashlib
from unittest.mock import patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
spec=importlib.util.spec_from_file_location('candidate',Path(__file__).with_name('r2a_retire_final_candidate.py'))
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
fixture=json.loads(Path(__file__).with_name('runtime.example.json').read_text())
fixture['executor_uid']=os.geteuid()
c.configure_runtime(fixture,hashlib.sha256(c.canonical(fixture)).hexdigest())

def proc_stat(pid=41001,comm='synthetic',state='S',ppid=1,ticks=100,flags=0):
    fields=['0']*50
    fields[0]=state;fields[1]=str(ppid);fields[6]=str(flags);fields[19]=str(ticks)
    return f'{pid} ({comm}) '+ ' '.join(fields)+'\n'

def classified_inventory():
    return {'same_tunnel_pids':[c.FORMAL_TUNNEL],'formal_consumer_pids':[c.FORMAL_TUNNEL],
            'unexpected_consumer_pids':[],'proven_non_target_pids':[],
            **{k:[] for k in c.INVENTORY_UNKNOWN},'unclassified_proc_count':0,
            'scope':c.INVENTORY_SCOPE,'global_consumer_exclusivity':'NOT_PROVEN'}

class ProcStatTests(unittest.TestCase):
    def test_names_and_required_fields(self):
        for comm in ('normal','two words','a(b)c','a)b)c','a (b) c)','line\nbreak'):
            with self.subTest(comm=comm):
                s=c.parse_proc_stat(proc_stat(comm=comm,ppid=42,ticks=987,flags=3),41001)
                self.assertEqual(s,dict(pid=41001,comm=comm,state='S',ppid=42,start_ticks=987,flags=3))
    def test_truncated_or_malformed_records_rejected(self):
        valid=proc_stat()
        for raw in ('',valid[:30],valid.rsplit(' ',1)[0],valid.replace(') S',')'),
                    valid.replace('(synthetic)','synthetic'),valid.replace(' S ',' ? '),
                    valid.replace(' 1 ',' nope ',1),proc_stat(ppid=-1),
                    proc_stat(ticks=-1),proc_stat(flags=-1),valid+' junk',
                    proc_stat().replace(' 100 ',' 1.5 '),proc_stat().replace(' 100 ',' +100 ')):
            with self.subTest(raw=raw),self.assertRaises(c.GateError):c.parse_proc_stat(raw,41001)
    def test_pid_mismatch_rejected(self):
        with self.assertRaisesRegex(c.GateError,'PID_MISMATCH'):c.parse_proc_stat(proc_stat(pid=9),41001)
    def test_invalid_pid_rejected(self):
        for pid in (0,-1,True,'41001'):
            with self.subTest(pid=pid),self.assertRaises(c.GateError):c.parse_proc_stat(proc_stat(),pid)
    def test_reader_validates_real_record_not_split_fixture(self):
        with patch.object(Path,'read_text',return_value=proc_stat(comm='a b)c')):
            self.assertEqual(c.pstat(41001)['comm'],'a b)c')
    def test_identity_start_tick_mismatch_aborts_before_other_reads(self):
        with patch.object(Path,'read_text',return_value=proc_stat(ticks=c.TARGETS[c.STUB][0]+1)),patch.object(Path,'resolve') as exe:
            with self.assertRaises(c.GateError):c.identity(c.STUB)
            exe.assert_not_called()
    def test_identity_exit_during_read_propagates(self):
        with patch.object(Path,'read_text',side_effect=FileNotFoundError),patch.object(c.signal,'pidfd_send_signal') as signal:
            with self.assertRaises(FileNotFoundError):c.identity(c.STUB)
            signal.assert_not_called()
    def test_missing_original_state_is_exit_evidence_only(self):
        with patch.object(Path,'read_text',side_effect=FileNotFoundError):self.assertIsNone(c.original_state(c.STUB))
    def test_malformed_original_state_is_not_exit_evidence(self):
        with patch.object(Path,'read_text',return_value='truncated'):
            with self.assertRaises(c.GateError):c.original_state(c.STUB)
    def test_descendant_read_failure_is_not_ignored(self):
        with patch.object(Path,'iterdir',return_value=[Path('/proc/41003')]),patch.object(c,'pstat',side_effect=PermissionError):
            with self.assertRaises(PermissionError):c.descendants()

class ConsumerInventoryTests(unittest.TestCase):
    def scan(self,rows):
        # All /proc, inode, environment, HTTP and executable evidence is synthetic.
        # All row overrides below represent fixture evidence, never live processes.
        from types import SimpleNamespace
        def row_for(path):return rows[int(path.parts[2])]
        def read_text(path,*args,**kwargs):
            row=row_for(path);pid=int(path.parts[2])
            if row.get('vanish'):raise FileNotFoundError
            if row.get('stat_error'):raise row['stat_error']
            return proc_stat(pid,comm=row.get('comm','tunnel-client'),ticks=row.get('ticks',c.FORMAL.get(pid,100)),flags=row.get('flags',0))
        def resolve(path,*args,**kwargs):
            row=row_for(path)
            if row.get('read_error'):raise row['read_error']
            return Path(row.get('exe',c.TUNNEL))
        def read_bytes(path):return row_for(path).get('argv',b'tunnel-client\0')
        def stat(path,*args,**kwargs):
            return SimpleNamespace(st_dev=1,st_ino=2 if str(path)==c.TUNNEL or row_for(path).get('exe',c.TUNNEL)==c.TUNNEL else 3)
        def env(pid):return {'CONTROL_PLANE_TUNNEL_ID':rows[pid].get('bound','synthetic-tunnel')}
        with patch.object(Path,'iterdir',return_value=[Path('/proc')/str(pid) for pid in rows]),patch.object(Path,'read_text',read_text),patch.object(Path,'resolve',resolve),patch.object(Path,'stat',stat),patch.object(Path,'read_bytes',read_bytes),patch.object(c.os,'stat',return_value=SimpleNamespace(st_dev=1,st_ino=2)),patch.object(c,'penv',side_effect=env),patch.object(c,'fetch',return_value=b'{"control_plane_tunnel_id":"synthetic-tunnel"}'):
            return c.consumers('synthetic-tunnel')
    def formal(self,**extra):return {c.FORMAL_TUNNEL:{},**extra}
    def accept(self,inv):return c.validate_consumer_inventory(inv,[c.FORMAL_TUNNEL])
    def test_single_formal_consumer_accepts_only_local_snapshot(self):
        inv=self.scan(self.formal());self.accept(inv)
        self.assertEqual(inv['global_consumer_exclusivity'],'NOT_PROVEN')
        self.assertEqual(inv['scope'],'visible_local_process_namespace_only')
    def test_identified_second_consumer_blocks(self):
        inv=self.scan({c.FORMAL_TUNNEL:{},99999:{}})
        self.assertEqual(inv['unexpected_consumer_pids'],[99999]);self.assertEqual(inv['local_inventory_status'],'BLOCKED')
        with self.assertRaises(c.GateError):self.accept(inv)
    def test_different_binding_is_proven_non_target(self):
        inv=self.scan({c.FORMAL_TUNNEL:{},99999:{'bound':'other-synthetic-tunnel'}})
        self.accept(inv);self.assertEqual(inv['proven_non_target_pids'],[99999])
    def test_unreadable_executable_is_not_a_kernel_thread(self):
        for error in (PermissionError,FileNotFoundError):
            inv=self.scan({c.FORMAL_TUNNEL:{},99999:{'comm':'unfamiliar','read_error':error}})
            self.assertEqual(inv['read_failure_pids'],[99999]);self.assertFalse(inv['proven_non_target_pids'])
            with self.assertRaises(c.GateError):self.accept(inv)
    def test_process_disappearance_is_inconclusive(self):
        inv=self.scan({c.FORMAL_TUNNEL:{},99999:{'vanish':True}})
        self.assertEqual(inv['vanished_race_pids'],[99999])
        with self.assertRaises(c.GateError):self.accept(inv)
    def test_kernel_flag_is_positive_exclusion_evidence(self):
        inv=self.scan({c.FORMAL_TUNNEL:{},99999:{'flags':c.PF_KTHREAD,'read_error':PermissionError}})
        self.accept(inv);self.assertEqual(inv['proven_non_target_pids'],[99999])
    def test_unfamiliar_executable_cannot_prove_non_consumer(self):
        inv=self.scan({c.FORMAL_TUNNEL:{},99999:{'comm':'renamed','exe':'/synthetic/renamed'}})
        self.assertEqual(inv['other_unclassified_pids'],[99999])
        with self.assertRaises(c.GateError):self.accept(inv)
    def test_missing_binding_blocks(self):
        inv=self.scan({c.FORMAL_TUNNEL:{},99999:{'bound':''}})
        self.assertEqual(inv['unresolved_candidate_pids'],[99999])
        with self.assertRaises(c.GateError):self.accept(inv)
    def test_command_line_binding_precedes_environment(self):
        inv=self.scan({c.FORMAL_TUNNEL:{},99999:{'bound':'other-tunnel','argv':b'tunnel-client\0--control-plane.tunnel-id=synthetic-tunnel\0'}})
        self.assertEqual(inv['unexpected_consumer_pids'],[99999])
        with self.assertRaises(c.GateError):self.accept(inv)
    def test_malformed_or_ambiguous_flags_are_unknown(self):
        for argv in (b'tunnel-client\0--control-plane.tunnel-id\0',
                     b'tunnel-client\0--control-plane.tunnel-id=\0',
                     b'tunnel-client\0--control-plane.tunnel-id=a\0--control-plane.tunnel-id=b\0'):
            with self.subTest(argv=argv):
                inv=self.scan({c.FORMAL_TUNNEL:{},99999:{'argv':argv}})
                self.assertEqual(inv['other_unclassified_pids'],[99999])
                with self.assertRaises(c.GateError):self.accept(inv)
    def test_formal_pid_reuse_cannot_use_formal_endpoint_binding(self):
        inv=self.scan({c.FORMAL_TUNNEL:{'ticks':c.FORMAL[c.FORMAL_TUNNEL]+1}})
        self.assertFalse(inv['formal_consumer_pids'])
        with self.assertRaises(c.GateError):self.accept(inv)
    def test_stat_permission_failure_blocks(self):
        inv=self.scan({c.FORMAL_TUNNEL:{},99999:{'stat_error':PermissionError}})
        self.assertEqual(inv['read_failure_pids'],[99999])
        with self.assertRaises(c.GateError):self.accept(inv)
    def test_malformed_stat_blocks(self):
        inv=self.scan({c.FORMAL_TUNNEL:{},99999:{'stat_error':c.GateError('PROC_STAT_FORMAT')}})
        self.assertEqual(inv['other_unclassified_pids'],[99999])
        with self.assertRaises(c.GateError):self.accept(inv)
    def test_empty_inventory_is_not_pass(self):
        inv=self.scan({});self.assertEqual(inv['local_inventory_status'],'INCONCLUSIVE')
        with self.assertRaises(c.GateError):self.accept(inv)
    def test_proc_listing_permission_failure_propagates(self):
        from types import SimpleNamespace
        with patch.object(c.os,'stat',return_value=SimpleNamespace(st_dev=1,st_ino=2)),patch.object(Path,'iterdir',side_effect=PermissionError):
            with self.assertRaises(PermissionError):c.consumers('synthetic-tunnel')
    def test_namespace_scope_cannot_be_promoted(self):
        inv=classified_inventory();inv['scope']='global'
        with self.assertRaises(c.GateError):self.accept(inv)
    def test_legacy_unclassified_count_cannot_bypass_postcheck(self):
        test=PostconditionTests();test.setUp();test.snap['consumers']['unclassified_proc_count']=7
        with self.assertRaises(c.GateError):test.check()
    def test_each_unknown_category_blocks_pre_and_post_even_with_stale_status(self):
        for key in c.INVENTORY_UNKNOWN:
            inv=classified_inventory();inv[key]=[99999];inv['local_inventory_status']='CLASSIFIED_LOCAL_SNAPSHOT_ONLY'
            with self.subTest(key=key):
                for expected in ([c.FORMAL_TUNNEL],[c.STUB,c.FORMAL_TUNNEL]):
                    inv['same_tunnel_pids']=sorted(expected)
                    with self.assertRaises(c.GateError):c.validate_consumer_inventory(inv,expected)
                test=PostconditionTests();test.setUp();test.snap['consumers']=inv
                with self.assertRaises(c.GateError):test.check()
    def test_start_ticks_change_during_inventory_blocks(self):
        first=c.parse_proc_stat(proc_stat(c.FORMAL_TUNNEL,ticks=c.FORMAL[c.FORMAL_TUNNEL],flags=c.PF_KTHREAD),c.FORMAL_TUNNEL)
        last={**first,'start_ticks':first['start_ticks']+1}
        from types import SimpleNamespace
        with patch.object(c.os,'stat',return_value=SimpleNamespace(st_dev=1,st_ino=2)),patch.object(Path,'iterdir',return_value=[Path('/proc')/str(c.FORMAL_TUNNEL)]),patch.object(c,'pstat',side_effect=[first,last]):
            inv=c.consumers('synthetic-tunnel')
        self.assertEqual(inv['other_unclassified_pids'],[c.FORMAL_TUNNEL])
        with self.assertRaises(c.GateError):self.accept(inv)

class InventoryMainBoundaryTests(unittest.TestCase):
    def run_main(self,inventory):
        # Exercise default dry-run orchestration with no real proc/HTTP/service I/O.
        with patch.object(sys,'argv',['candidate','--dry-run']),patch.object(sys,'addaudithook'),patch.object(c,'load_runtime_config'),patch.object(c,'checks',return_value=({},{})),patch.object(c.os,'pidfd_open',return_value=-1),patch.object(c.os,'close'),patch.object(c,'identity'),patch.object(c,'penv',return_value={'CONTROL_PLANE_TUNNEL_ID':'synthetic-tunnel'}),patch.object(c,'stub_listener_snapshot',return_value=[]),patch.object(c,'consumers',return_value=inventory),patch.object(c,'delivery_failures',return_value=0),patch.object(c,'protected_source_hashes',side_effect=c.GateError('UNPUBLISHED')),patch.object(c,'root_protected_read',side_effect=c.GateError('UNPROVISIONED')),patch.object(c,'emit') as emit,patch.object(c,'send_term') as send,patch.object(c.subprocess,'run') as process:
            result=c.main()
            send.assert_not_called();process.assert_not_called()
            return result,emit.call_args.kwargs
    def test_unknown_inventory_aborts_dry_run_with_zero_signals(self):
        inv=classified_inventory();inv['same_tunnel_pids']=sorted([c.STUB,c.FORMAL_TUNNEL]);inv['read_failure_pids']=[99999]
        result,event=self.run_main(inv)
        self.assertEqual(result,2);self.assertEqual(event['signals_sent'],0)
        self.assertEqual(event['code'],'CONSUMER_INVENTORY_INCONCLUSIVE')
    def test_classified_local_dry_run_still_requires_release_and_approval(self):
        inv=classified_inventory();inv['same_tunnel_pids']=sorted([c.STUB,c.FORMAL_TUNNEL])
        result,event=self.run_main(inv)
        self.assertEqual(result,3);self.assertEqual(event['signals_sent'],0)
        self.assertEqual(event['consumers']['global_consumer_exclusivity'],'NOT_PROVEN')

class ApprovalTests(unittest.TestCase):
    def setUp(self):
        # Deliberately public deterministic TEST seeds; never written as a real trust anchor.
        self.owner=Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        self.operator=Ed25519PrivateKey.from_private_bytes(bytes(range(32,64)))
        encode=lambda key:base64.b64encode(key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode()
        self.registry={'kind':'aee-r2a-dual-approval-trust-v2','owner_subject':c.OWNER_SUBJECT,'operator_subject':'synthetic-operator-only','owner_public_key_b64':encode(self.owner),'operator_public_key_b64':encode(self.operator)}
        self.now=datetime(2026,10,21,0,0,tzinfo=timezone.utc)
        self.scope={'candidate_sha256':'0'*64,'launcher_sha256':'3'*64,'runtime_config_sha256':c.CONFIG_SHA256,'host_boot_sha256':'f'*64,'targets':{str(pid):v[0] for pid,v in c.TARGETS.items()},'formal_targets':{str(pid):v for pid,v in c.FORMAL.items()}}
        self.payload={'kind':'aee-r2a-dual-approval-v4','approval_id':'SYNTHETIC-NOT-A-REAL-RELEASE','action':'retire_stub_and_attached_appserver','owner_subject':c.OWNER_SUBJECT,'operator_subject':'synthetic-operator-only','owner_released':True,'no_active_turn_or_ui':True,'shared_state_shutdown_acknowledged':True,'window_start_utc':(self.now-timedelta(seconds=1)).isoformat(),'signal_not_after_utc':(self.now+timedelta(minutes=10)).isoformat(),'observation_not_after_utc':(self.now+timedelta(minutes=14)).isoformat(),'observe_seconds':120,**self.scope}
    def sign(self,p=None,owner=None,operator=None):
        p=copy.deepcopy(p or self.payload);data=c.DOMAIN+c.canonical(p)
        return {'payload':p,'owner_signature_b64':base64.b64encode((owner or self.owner).sign(data)).decode(),'operator_signature_b64':base64.b64encode((operator or self.operator).sign(data)).decode()}
    def verify(self,e,registry=None,scope=None,remaining=300):
        return c.verify_envelope(e,registry or self.registry,scope or self.scope,self.now,remaining)
    def test_two_signatures_accept_synthetic_scope_only(self):self.assertTrue(self.verify(self.sign()))
    def test_plain_json_fields_do_not_authorize(self):
        with self.assertRaises(c.GateError):self.verify({'payload':self.payload})
    def test_supplied_public_key_cannot_override_trust(self):
        e=self.sign();e['owner_public_key_b64']=self.registry['owner_public_key_b64']
        with self.assertRaises(c.GateError):self.verify(e)
    def test_untrusted_signer_rejected(self):
        rogue=Ed25519PrivateKey.from_private_bytes(bytes([99])*32)
        with self.assertRaises(c.GateError):self.verify(self.sign(owner=rogue))
    def test_operator_signature_required(self):
        e=self.sign();e['operator_signature_b64']=e['owner_signature_b64']
        with self.assertRaises(c.GateError):self.verify(e)
    def test_signature_tampering_rejected(self):
        e=self.sign();e['payload']['approval_id']='tampered'
        with self.assertRaises(c.GateError):self.verify(e)
    def test_same_owner_operator_key_rejected(self):
        r=copy.deepcopy(self.registry);r['operator_public_key_b64']=r['owner_public_key_b64']
        with self.assertRaises(c.GateError):self.verify(self.sign(),registry=r)
    def test_owner_release_false_rejected_even_if_signed(self):
        p=copy.deepcopy(self.payload);p['owner_released']=False
        with self.assertRaises(c.GateError):self.verify(self.sign(p))
    def test_wrong_script_hash_rejected(self):
        scope=copy.deepcopy(self.scope);scope['candidate_sha256']='1'*64
        with self.assertRaises(c.GateError):self.verify(self.sign(),scope=scope)
    def test_wrong_host_boot_rejected(self):
        scope=copy.deepcopy(self.scope);scope['host_boot_sha256']='2'*64
        with self.assertRaises(c.GateError):self.verify(self.sign(),scope=scope)
    def test_wrong_target_start_rejected(self):
        scope=copy.deepcopy(self.scope);scope['targets']['41001']+=1
        with self.assertRaises(c.GateError):self.verify(self.sign(),scope=scope)
    def test_expired_approval_rejected(self):
        p=copy.deepcopy(self.payload);p['signal_not_after_utc']=(self.now-timedelta(seconds=1)).isoformat()
        with self.assertRaises(c.GateError):self.verify(self.sign(p))
    def test_insufficient_signal_budget_rejected(self):
        with self.assertRaises(c.GateError):self.verify(self.sign(),remaining=601)
    def test_shortened_observation_cannot_be_approved(self):
        p=copy.deepcopy(self.payload);p['observe_seconds']=1
        with self.assertRaises(c.GateError):self.verify(self.sign(p))
    def test_duplicate_json_key_rejected(self):
        with self.assertRaises(c.GateError):c.strict_json('{"owner_released":false,"owner_released":true}')
    def test_nonfinite_json_rejected(self):
        with self.assertRaises(c.GateError):c.strict_json('{"value":NaN}')
    def test_writable_trust_parent_rejected_without_creating_file(self):
        with self.assertRaises(c.GateError):c.root_protected_read(Path('/tmp/not-a-real-approval-root'))
    def test_fifo_approval_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory(prefix='r2a-synthetic-fixture-') as d:
            path=Path(d)/'not-an-approval';os.mkfifo(path,0o600)
            def release_fixture_only():
                try:
                    fd=os.open(path,os.O_WRONLY|os.O_NONBLOCK);os.close(fd)
                except OSError:pass
            timer=threading.Timer(1,release_fixture_only);timer.daemon=True;timer.start()
            start=time.monotonic()
            try:
                with self.assertRaises(c.GateError):c.read_private(path)
                self.assertLess(time.monotonic()-start,.5)
            finally:timer.cancel();timer.join(timeout=2)
    def test_missing_live_envelope_not_authorized(self):self.assertFalse(c.authorization(None))

class PostconditionTests(unittest.TestCase):
    def setUp(self):
        self.ticks={pid:v[0] for pid,v in c.TARGETS.items()}
        self.old=[('tcp',29001,'111'),('tcp',29002,'112')]
        self.snap={'pidfd_exited':{pid:True for pid in self.ticks},'original_states':{pid:None for pid in self.ticks},'listeners':[('tcp',29003,'211'),('tcp',29004,'212')],'consumers':classified_inventory(),'formal_health':True,'supervisor_unchanged':True,'delivery_failures':0,'delivery_baseline':0}
    def check(self):return c.validate_post_snapshot(self.snap,self.ticks,self.old)
    def test_complete_synthetic_postconditions_pass(self):self.check()
    def test_each_target_must_exit(self):
        for pid in self.ticks:
            with self.subTest(pid=pid):
                self.snap['pidfd_exited'][pid]=False
                with self.assertRaises(c.GateError):self.check()
                self.snap['pidfd_exited'][pid]=True
    def test_missing_pidfd_proof_rejected(self):
        self.snap['pidfd_exited'].pop(41003)
        with self.assertRaises(c.GateError):self.check()
    def test_missing_original_identity_evidence_rejected(self):
        self.snap['original_states'].pop(41003)
        with self.assertRaises(c.GateError):self.check()
    def test_zombie_is_not_complete_cleanup(self):
        self.snap['original_states'][41003]=(self.ticks[41003],'Z')
        with self.assertRaises(c.GateError):self.check()
    def test_pid_reuse_does_not_retarget_new_process(self):
        self.snap['original_states'][41001]=(999999,'S');self.check()
    def test_old_port_rebound_by_other_process_rejected(self):
        self.snap['listeners'].append(('tcp6',29002,'999'))
        with self.assertRaises(c.GateError):self.check()
    def test_original_listener_inode_on_other_port_rejected(self):
        self.snap['listeners'].append(('tcp',12345,'111'))
        with self.assertRaises(c.GateError):self.check()
    def test_extra_same_tunnel_consumer_rejected(self):
        self.snap['consumers']['same_tunnel_pids'].append(888888)
        with self.assertRaises(c.GateError):self.check()
    def test_unresolved_consumer_binding_rejected(self):
        self.snap['consumers']['unresolved_candidate_pids']=[888888]
        with self.assertRaises(c.GateError):self.check()
    def test_formal_health_failure_rejected(self):
        self.snap['formal_health']=False
        with self.assertRaises(c.GateError):self.check()
    def test_supervisor_change_rejected(self):
        self.snap['supervisor_unchanged']=False
        with self.assertRaises(c.GateError):self.check()
    def test_delivery_failure_increase_rejected(self):
        self.snap['delivery_failures']=1
        with self.assertRaises(c.GateError):self.check()

class ObservationTests(unittest.TestCase):
    def test_120_seconds_minimum_with_synthetic_clock_only(self):
        clock=[0.0];calls=[]
        def sleep(seconds):clock[0]+=seconds
        count=c.observe(lambda:calls.append(clock[0]),clock=lambda:clock[0],sleeper=sleep)
        self.assertEqual(clock[0],120);self.assertEqual(count,25)
    def test_intermediate_failure_not_hidden(self):
        clock=[0.0]
        def callback():
            if clock[0]>=10:raise c.GateError('SYNTHETIC_HEALTH_FAILURE')
        def sleep(seconds):clock[0]+=seconds
        with self.assertRaises(c.GateError):c.observe(callback,clock=lambda:clock[0],sleeper=sleep)
    def test_shorter_observation_rejected(self):
        with self.assertRaises(c.GateError):c.observe(lambda:None,duration=5)


class SupervisorTests(unittest.TestCase):
    def output(self,uptime='0:00:01',gateway=42001,state='RUNNING',otherpid=123):
        return (f'aee-v2-gateway {state} pid {gateway}, uptime {uptime}\n'
                f'aee-v2-tunnel-client RUNNING pid 42002, uptime {uptime}\n'
                f'other RUNNING pid {otherpid}, uptime {uptime}\n'
                f'preexisting STOPPED {uptime} arbitrary description\n')
    def test_uptime_and_inactive_description_changes_pass(self):
        a=c.parse_managed(self.output());b=c.parse_managed(self.output('9 days, 1:02:03'))
        self.assertEqual(a['aee-v2-gateway'],{'state':'RUNNING','pid':42001})
        self.assertEqual(a['preexisting'],{'state':'STOPPED','pid':None})
        self.assertTrue(c.assert_managed_unchanged(b,a))
        b=c.parse_managed(self.output().replace('preexisting STOPPED 0:00:01 arbitrary description','preexisting STOPPED pid 999, obsolete description'))
        self.assertTrue(c.assert_managed_unchanged(b,a))
    def test_formal_pid_change_fails(self):
        with self.assertRaises(c.GateError):c.parse_managed(self.output(gateway=999))
    def test_formal_state_change_fails(self):
        with self.assertRaises(c.GateError):c.parse_managed(self.output(state='STOPPING'))
    def test_other_program_pid_change_fails(self):
        with self.assertRaises(c.GateError):c.assert_managed_unchanged(c.parse_managed(self.output(otherpid=124)),c.parse_managed(self.output()))
    def test_other_program_state_change_fails(self):
        baseline=c.parse_managed(self.output());changed=copy.deepcopy(baseline);changed['other']['state']='EXITED'
        with self.assertRaises(c.GateError):c.assert_managed_unchanged(changed,baseline)
    def test_missing_program_fails(self):
        baseline=c.parse_managed(self.output());changed=copy.deepcopy(baseline);changed.pop('other')
        with self.assertRaises(c.GateError):c.assert_managed_unchanged(changed,baseline)
    def test_duplicate_or_missing_running_pid_rejected(self):
        for raw in (self.output()+'other STOPPED ignored\n',self.output().replace('pid 42001,','no pid')):
            with self.subTest(raw=raw),self.assertRaises(c.GateError):c.parse_managed(raw)

class TimeBoundaryTests(unittest.TestCase):
    setUp=ApprovalTests.setUp
    sign=ApprovalTests.sign
    verify=ApprovalTests.verify
    def at(self,seconds):return self.now+timedelta(seconds=seconds)
    def verified(self,seconds,phase='signal',remaining=0):
        return c.verify_envelope(self.sign(),self.registry,self.scope,self.at(seconds),remaining,phase)
    def test_signal_just_before_cutoff_passes(self):self.assertTrue(self.verified(599.999))
    def test_signal_at_or_after_cutoff_rejected(self):
        for seconds in (600,600.001,650):
            with self.subTest(seconds=seconds),self.assertRaises(c.GateError):self.verified(seconds)
    def test_observe_after_signal_expiry_still_checks_signatures(self):
        e=self.sign();e['operator_signature_b64']=e['owner_signature_b64']
        with self.assertRaises(c.GateError):c.verify_envelope(e,self.registry,self.scope,self.at(601),phase='observe')
    def test_observation_at_own_deadline_rejected(self):
        with self.assertRaises(c.GateError):self.verified(840,'observe')
    def test_full_120_seconds_crosses_signal_cutoff(self):
        clock=[599.0];samples=[]
        def callback():self.verified(clock[0],'observe');samples.append(clock[0])
        def sleep(seconds):clock[0]+=seconds
        n=c.observe(callback,clock=lambda:clock[0],sleeper=sleep)
        self.assertEqual(n,25);self.assertEqual(samples[-1]-samples[0],120)
        self.assertGreater(samples[-1],600)
    def test_observation_can_start_after_signal_cutoff(self):self.assertTrue(self.verified(640,'observe',150))
    def test_short_observation_tail_rejected(self):
        p=copy.deepcopy(self.payload);p['observation_not_after_utc']=self.at(819).isoformat()
        with self.assertRaises(c.GateError):self.verify(self.sign(p))
    def test_final_syscall_gate_rejects_expiry_after_expensive_authorization(self):
        class Clock(datetime):
            @staticmethod
            def now(tz):return self.at(600)
        with patch.object(c,'datetime',Clock),patch.object(c,'wait_exit',return_value=False),patch.object(c.signal,'pidfd_send_signal') as syscall:
            with self.assertRaises(c.GateError):c.send_term(999,41001,[],lambda:self.payload)
            syscall.assert_not_called()
    def test_already_exited_target_requires_no_new_signal_authorization(self):
        with patch.object(c,'wait_exit',return_value=True),patch.object(c.signal,'pidfd_send_signal') as syscall:
            c.send_term(999,41001,[],lambda:self.fail('No signal means no need for renewed signal permission'))
            syscall.assert_not_called()
    def test_each_new_signal_rechecks_deadline(self):
        class Clock(datetime):
            @staticmethod
            def now(tz):return self.at(600)
        with patch.object(c,'datetime',Clock),patch.object(c,'wait_exit',return_value=False),patch.object(c.signal,'pidfd_send_signal') as syscall:
            for pid in c.TARGETS:
                with self.assertRaises(c.GateError):c.send_term(999,pid,[],lambda:self.payload)
            syscall.assert_not_called()
    def test_old_v2_payload_not_accepted(self):
        p=copy.deepcopy(self.payload);p['kind']='aee-r2a-dual-approval-v2'
        with self.assertRaises(c.GateError):self.verify(self.sign(p))
    def test_launcher_hash_binding_rejected(self):
        scope=copy.deepcopy(self.scope);scope['launcher_sha256']='9'*64
        with self.assertRaises(c.GateError):self.verify(self.sign(),scope=scope)

class SnapshotLoaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec=importlib.util.spec_from_file_location('loader',Path(__file__).with_name('r2a_snapshot_launcher_candidate.py'))
        cls.loader=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.loader)
    def test_compile_uses_exact_hashed_bytes_without_reopen(self):
        raw=b"snapshot_result = 42\n";digest=hashlib.sha256(raw).hexdigest()
        with patch.object(Path,'read_bytes',side_effect=AssertionError('reopen forbidden')):
            ns=self.loader.execute_snapshot(raw,Path('/synthetic-only'),digest,Path('/synthetic-loader'),'a'*64)
        self.assertEqual(ns['snapshot_result'],42);self.assertEqual(ns['__r2a_loaded_sha256__'],digest)
    def test_snapshot_hash_mismatch_fails_before_compile(self):
        with self.assertRaises(ValueError):self.loader.execute_snapshot(b'raise AssertionError("MUST NOT EXECUTE")',Path('/synthetic'),'0'*64,Path('/loader'),'a'*64)
    def test_unpublished_direct_entry_is_blocked(self):
        with self.assertRaises(c.GateError):c.protected_source_hashes()
    def test_loaded_hash_and_current_protected_bytes_must_match(self):
        context={'__r2a_loaded_sha256__':'0'*64,'__r2a_loader_path__':'/synthetic-loader','__r2a_loader_sha256__':'a'*64}
        with patch.dict(c.__dict__,context),patch.object(c,'root_protected_read',return_value=b'different bytes'):
            with self.assertRaises(c.GateError):c.protected_source_hashes()
    def test_published_loader_change_fails(self):
        raw=b'synthetic candidate';context={'__r2a_loaded_sha256__':hashlib.sha256(raw).hexdigest(),'__r2a_loader_path__':'/synthetic-loader','__r2a_loader_sha256__':'a'*64}
        with patch.dict(c.__dict__,context),patch.object(c,'root_protected_read',side_effect=[raw,b'changed loader']):
            with self.assertRaises(c.GateError):c.protected_source_hashes()
    def test_user_writable_release_parent_rejected(self):
        with self.assertRaises(ValueError):self.loader.protected_read(Path('/tmp/not-a-release'),100)

class PublisherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec=importlib.util.spec_from_file_location('publisher',Path(__file__).with_name('r2a_publish_candidate.py'))
        cls.publisher=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.publisher)
    def fixture(self,d):
        p=Path(d)/'synthetic.py';p.write_bytes(b'pass\n');p.chmod(0o600)
        return p,hashlib.sha256(p.read_bytes()).hexdigest()
    def test_reviewed_private_snapshot_passes(self):
        with tempfile.TemporaryDirectory(prefix='r2a-code-fixture-') as d:
            p,h=self.fixture(d);self.assertEqual(self.publisher.checked_snapshot(p,h,100,os.geteuid()),b'pass\n')
    def test_source_hash_mismatch_rejected(self):
        with tempfile.TemporaryDirectory(prefix='r2a-code-fixture-') as d:
            p,h=self.fixture(d)
            with self.assertRaises(ValueError):self.publisher.checked_snapshot(p,'0'*64,100,os.geteuid())
    def test_symlink_source_rejected(self):
        with tempfile.TemporaryDirectory(prefix='r2a-code-fixture-') as d:
            p,h=self.fixture(d);link=Path(d)/'link';link.symlink_to(p)
            with self.assertRaises(OSError):self.publisher.checked_snapshot(link,h,100,os.geteuid())
    def test_wrong_source_mode_rejected(self):
        with tempfile.TemporaryDirectory(prefix='r2a-code-fixture-') as d:
            p,h=self.fixture(d);p.chmod(0o644)
            with self.assertRaises(ValueError):self.publisher.checked_snapshot(p,h,100,os.geteuid())
    def test_exclusive_publication_does_not_overwrite_fixture(self):
        with tempfile.TemporaryDirectory(prefix='r2a-code-fixture-') as d:
            fd=os.open(d,os.O_RDONLY|os.O_DIRECTORY)
            try:
                self.publisher.write_new(fd,'synthetic.py',b'first')
                with self.assertRaises(FileExistsError):self.publisher.write_new(fd,'synthetic.py',b'replacement')
                self.assertEqual((Path(d)/'synthetic.py').read_bytes(),b'first')
            finally:os.close(fd)
    def test_real_publication_gate_denies_non_root_without_any_writes(self):
        with patch.object(self.publisher.os,'geteuid',return_value=20001):
            with self.assertRaises(ValueError):self.publisher.publish(b'pass',b'pass','0'*64,'1'*64)

class RuntimeConfigurationTests(unittest.TestCase):
    def config(self):return copy.deepcopy(fixture)
    def configure(self,config):c.configure_runtime(config,hashlib.sha256(c.canonical(config)).hexdigest())
    def tearDown(self):self.configure(self.config())
    def test_example_is_synthetic_and_valid(self):
        self.configure(self.config());self.assertEqual(c.STUB,41001)
        self.assertTrue(str(c.ROOT).startswith('/nonexistent/example/'))
    def test_unknown_fields_rejected(self):
        cfg=self.config();cfg['extra']=True
        with self.assertRaises(c.GateError):self.configure(cfg)
    def test_missing_field_rejected(self):
        cfg=self.config();cfg.pop('key_file')
        with self.assertRaises(c.GateError):self.configure(cfg)
    def test_root_or_boolean_executor_uid_rejected(self):
        for uid in (0,-1,True,'20001'):
            cfg=self.config();cfg['executor_uid']=uid
            with self.subTest(uid=uid),self.assertRaises(c.GateError):self.configure(cfg)
    def test_duplicate_pid_rejected(self):
        cfg=self.config();cfg['formal']['gateway']['pid']=cfg['targets']['stub']['pid']
        with self.assertRaises(c.GateError):self.configure(cfg)
    def test_invalid_identity_numbers_rejected(self):
        for value in (0,-1,True,'10101'):
            cfg=self.config();cfg['targets']['stub']['start_ticks']=value
            with self.subTest(value=value),self.assertRaises(c.GateError):self.configure(cfg)
    def test_wrong_descendant_parent_rejected(self):
        cfg=self.config();cfg['targets']['appserver']['ppid']=1
        with self.assertRaises(c.GateError):self.configure(cfg)
    def test_relative_or_parent_paths_rejected(self):
        for value in ('relative','/example/../etc'):
            cfg=self.config();cfg['runtime_root']=value
            with self.subTest(value=value),self.assertRaises(c.GateError):self.configure(cfg)
    def test_invalid_or_duplicate_ports_rejected(self):
        for port in (0,65536,True,29001):
            cfg=self.config();cfg['ports']['gateway']=port
            with self.subTest(port=port),self.assertRaises(c.GateError):self.configure(cfg)
    def test_inconsistent_tunnel_executable_rejected(self):
        cfg=self.config();cfg['tunnel_executable']='/nonexistent/different'
        with self.assertRaises(c.GateError):self.configure(cfg)
    def test_invalid_config_hash_rejected(self):
        with self.assertRaises(c.GateError):c.configure_runtime(self.config(),'not-a-hash')
    def test_failed_validation_does_not_partially_assign(self):
        previous=(c.STUB,c.CONFIG_SHA256);cfg=self.config();cfg['ports']['stub']=0
        with self.assertRaises(c.GateError):self.configure(cfg)
        self.assertEqual((c.STUB,c.CONFIG_SHA256),previous)
    def test_pid_reuse_or_identity_mismatch_rejected_before_executable_check(self):
        row=c.parse_proc_stat(proc_stat(c.STUB,ticks=c.TARGETS[c.STUB][0]+1),c.STUB)
        with patch.object(c,'pstat',return_value=row):
            with self.assertRaises(c.GateError):c.identity(c.STUB)
    def test_changed_protected_config_rejected_before_trust_load(self):
        with patch.object(c,'root_protected_read',return_value=b'changed'),patch.object(c,'load_trust') as trust:
            with self.assertRaises(c.GateError):c.authorization('/nonexistent/envelope')
            trust.assert_not_called()
    def test_fixed_config_read_rejects_writable_parent(self):
        with patch.object(c,'CONFIG_FILE',Path('/tmp/nonexistent-r2a-config')):
            with self.assertRaises(c.GateError):c.load_runtime_config()
    def test_owner_signature_missing_rejected(self):
        test=ApprovalTests();test.setUp();env=test.sign();env.pop('owner_signature_b64')
        with self.assertRaises(c.GateError):test.verify(env)
    def test_config_hash_is_in_signed_scope(self):
        test=ApprovalTests();test.setUp();scope=copy.deepcopy(test.scope);scope['runtime_config_sha256']='a'*64
        with self.assertRaises(c.GateError):test.verify(test.sign(),scope=scope)
    def test_v3_approval_cannot_authorize_v4_configuration(self):
        test=ApprovalTests();test.setUp();payload=copy.deepcopy(test.payload);payload['kind']='aee-r2a-dual-approval-v3'
        with self.assertRaises(c.GateError):test.verify(test.sign(payload))
    def test_no_authorization_without_envelope(self):self.assertFalse(c.authorization(None))
    def test_publisher_rejects_root_source_uid(self):
        PublisherTests.setUpClass()
        with self.assertRaises(ValueError):PublisherTests.publisher.checked_snapshot('/nonexistent','0'*64,100,0)

class SignalGuardTests(unittest.TestCase):
    def test_audit_events_are_blocked_without_syscalls(self):
        for event in ('os.kill','os.killpg','signal.pidfd_send_signal'):
            with self.subTest(event=event),self.assertRaisesRegex(AssertionError,'REAL_SIGNAL_FORBIDDEN_IN_TESTS'):
                sys.audit(event, -1, 0)
    def test_unmocked_pidfd_api_is_blocked_even_with_invalid_fd(self):
        # fd=-1 cannot designate any process, even if the hook regresses.
        with self.assertRaisesRegex(AssertionError,'REAL_SIGNAL_FORBIDDEN_IN_TESTS'):
            c.signal.pidfd_send_signal(-1,0,None,0)
    def test_valid_signal_boundary_reaches_only_mock(self):
        test=ApprovalTests();test.setUp();sent=[]
        class Clock(datetime):
            @staticmethod
            def now(tz):return test.now
        with patch.object(c,'datetime',Clock),patch.object(c,'wait_exit',return_value=False),patch.object(c.signal,'pidfd_send_signal') as syscall:
            c.send_term(-1,c.STUB,sent,lambda:test.payload)
            syscall.assert_called_once_with(-1,c.signal.SIGTERM,None,0)
        self.assertEqual(sent,[c.STUB])
    def test_authorization_exception_aborts_without_signal_or_restart(self):
        def denied():raise c.GateError('SYNTHETIC_AUTHORIZATION_FAILURE')
        with patch.object(c,'wait_exit',return_value=False),patch.object(c.signal,'pidfd_send_signal') as syscall,patch.object(c.subprocess,'run') as process:
            with self.assertRaises(c.GateError):c.send_term(-1,c.STUB,[],denied)
            syscall.assert_not_called();process.assert_not_called()
    def test_readonly_guard_denies_signal_and_process_mutation(self):
        for event,args in (('os.kill',()),('os.killpg',()),('signal.pidfd_send_signal',()),('subprocess.Popen',(None,['supervisorctl','restart','all']))):
            with self.subTest(event=event),self.assertRaises(c.GateError):c.readonly_guard(event,args)
    def test_readonly_guard_denies_file_writes(self):
        for mode,flags in (('w',0),(None,os.O_WRONLY),(None,os.O_CREAT)):
            with self.subTest(mode=mode,flags=flags),self.assertRaises(c.GateError):c.readonly_guard('open',('synthetic',mode,flags))

if __name__=='__main__':
    unittest.main()
