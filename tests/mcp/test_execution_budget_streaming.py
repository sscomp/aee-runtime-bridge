"""Deterministic fixtures: budgets, >8MiB streams, durability and crash cleanup.

Never invokes an actual model or opens production stores. Native CLI E2E is a
separate opt-in script/evidence, not claimed by these tests.
"""
import concurrent.futures
from dataclasses import replace
import errno
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from aee.mcp_runtime.budget import ExecutionBudget, BudgetPolicy
from aee.mcp_runtime.event_stream import EventParser
from aee.mcp_runtime.process import Limits, run_bounded, run_streamed
from aee.mcp_runtime.spool import SpoolStore
from aee.mcp_runtime.store import JobError, JobStore

ROOT = Path(__file__).resolve().parents[2]
FINAL = 'print(\'{"type":"result","result":"fixture-final"}\',flush=True)'


class BudgetTests(unittest.TestCase):
    def test_defaults_and_long_policy(self):
        b = ExecutionBudget(wall_timeout_seconds=901)
        self.assertIsNone(b.max_turns)
        self.assertGreater(b.wall_timeout_seconds, 900)
        self.assertEqual(b.max_concurrent_jobs, 1)

    def test_invalid_fields(self):
        invalid = [dict(wall_timeout_seconds=x) for x in (False, 0, float('nan'), float('inf'), '900', 604801, 10**1000)]
        invalid += [dict(max_turns=x) for x in (False, 0, -1, '16')]
        invalid += [dict(idle_timeout_seconds=-1), dict(memory_capture_bytes=0),
                    dict(on_spool_full='discard'), dict(max_concurrent_jobs=2),
                    dict(retention_seconds=0), dict(parser_mode='auto'),
                    dict(spool_max_bytes_per_job=1)]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(JobError):
                ExecutionBudget(**values)

    def test_sample_and_precedence(self):
        policy = BudgetPolicy(ROOT/'config/execution-budget.example.json', legacy={'wall_timeout_seconds':100})
        self.assertEqual(policy.for_agent('claude').wall_timeout_seconds, 7200)
        self.assertIsNone(policy.for_agent('codex').max_turns)
        self.assertTrue(policy.revision.startswith('execution-budget-v1:'))

    def test_policy_validation_and_owner_mode(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'policy.json'
            for raw in ('{"schema":1,"schema":1}', '{"schema":2}', '{"schema":true}',
                        '{"schema":1,"oops":1}', '{"schema":1,"adapters":{"pi":{"max_turns":16}}}',
                        '{"schema":1,"defaults":{"unknown":1}}', '{"schema":1,"defaults":{"wall_timeout_seconds":NaN}}'):
                p.write_text(raw);p.chmod(0o600)
                with self.subTest(raw=raw), self.assertRaises(JobError):BudgetPolicy(p)
            p.write_text('{"schema":1}');p.chmod(0o666)
            with self.assertRaises(JobError):BudgetPolicy(p)
            p.chmod(0o600);link=Path(t)/'link';link.symlink_to(p)
            with self.assertRaises(JobError):BudgetPolicy(link)


class ParserTests(unittest.TestCase):
    def parse(self, agent, raw, **kw):
        parser=EventParser(agent,**kw)
        raw=raw.encode() if isinstance(raw,str) else raw
        for i in range(0,len(raw),17):parser.feed(raw[i:i+17])
        parser.feed(b'',eof=True)
        return parser.result()

    def test_unicode_escape_and_boundary(self):
        final='中文 🎉 \\ " \n'
        for ascii_mode in (True,False):
            raw=json.dumps({'type':'result','result':final},ensure_ascii=ascii_mode)
            self.assertEqual(self.parse('claude',raw),final)

    def test_intermediate_large_array_and_jsonl(self):
        events=[{'type':'assistant','message':'x'*1000000},{'type':'result','result':'ok','num_turns':25}]
        for raw in (json.dumps(events),'\n'.join(map(json.dumps,events))):
            self.assertEqual(self.parse('claude',raw),'ok')

    def test_last_structured_answer_not_arbitrary_tail(self):
        events=[{'type':'item.completed','item':{'type':'agent_message','text':'x'*90000}},
                {'type':'item.completed','item':{'type':'agent_message','text':'actual-final'}},
                {'type':'turn.completed'}, {'type':'unrelated','text':'NOT_FINAL'}]
        self.assertEqual(self.parse('codex','\n'.join(map(json.dumps,events))),'actual-final')

    def test_claude_max_turns_distinct(self):
        with self.assertRaises(JobError) as e:self.parse('claude','{"type":"result","subtype":"error_max_turns","is_error":true}')
        self.assertEqual(e.exception.code,'TURN_BUDGET_EXHAUSTED')

    def test_malformed_truncated_json_utf8_and_eof(self):
        for raw in (b'\xff',b'{"type":"result","result":"ok"}',b'{"type":"result","result":"ok"}\xe4',
                    b'{"type":"result","result":"ok",}',b'[{"type":"result","result":"ok"},]',
                    b'{"type":"result","result":"ok","result":"other"}',b'{}',b'[1]',b'',
                    b'{"type":"result","result":"bad\\q"}',b'{"type":"result","result":"bad\\ud800"}'):
            # A complete success object is the one intentionally valid control.
            if raw==b'{"type":"result","result":"ok"}':
                self.assertEqual(self.parse('claude',raw),'ok');continue
            with self.subTest(raw=raw),self.assertRaises(JobError):self.parse('claude',raw)

    def test_final_oversized_never_false_success(self):
        for n in (1000,1000000):
            with self.assertRaises(JobError) as e:self.parse('claude',json.dumps({'type':'result','result':'x'*n}),result_bytes=64)
            self.assertEqual(e.exception.code,'OUTPUT_LIMIT_EXCEEDED')

    def test_codex_failures_remain_failures(self):
        for event in ({'type':'turn.failed'}, {'type':'item.completed','item':{'type':'command_execution','exit_code':1}}):
            raw=json.dumps(event)+'\n'+json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'ok'}})+'\n'+json.dumps({'type':'turn.completed'})
            with self.assertRaises(JobError):self.parse('codex',raw)

    def test_pi_structured_result(self):
        raw=json.dumps({'type':'message_end','message':{'role':'assistant','content':[{'type':'thinking','text':'NO'},{'type':'text','text':'YES'}]}})+'\n'+json.dumps({'type':'agent_settled','aborted':False})
        self.assertEqual(self.parse('pi',raw),'YES')

    def test_bounded_structural_projection(self):
        raw=json.dumps({'type':'assistant','content':[{'text':'x'*30000}]*256})+'\n'+json.dumps({'type':'result','result':'ok'})
        self.assertEqual(self.parse('claude',raw),'ok')
        with self.assertRaises(JobError):self.parse('claude','['*65+'0'+']'*65)


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='aee-budget-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=SpoolStore(self.root/'spool');self.addCleanup(self.store.close)
        self.counter=0

    def job(self,budget=None):
        self.counter+=1
        job=self.store.create('A3JOB-20261018-'+f'{self.counter:032x}',budget or ExecutionBudget(),'fixture-policy')
        self.addCleanup(job.close)
        return job

    def run_fixture(self,body,*,budget=None,cancel=None,job=None,redact=lambda x:x):
        b=budget or ExecutionBudget(wall_timeout_seconds=30)
        j=job or self.job(b)
        return run_streamed([sys.executable,'-c',body],env={},stdin=b'',budget=b,spool=j,
                            parser=EventParser('claude',b.result_inline_bytes,b.memory_capture_bytes),
                            cwd=str(self.root),cancel_event=cancel,redact=redact),j

    def assert_digest(self,job,meta):
        for s in ('stdout','stderr'):
            data=(self.root/'spool'/job.name/s).read_bytes()
            self.assertEqual(len(data),meta['streams'][s]['spool_bytes'])
            self.assertEqual(hashlib.sha256(data).hexdigest(),meta['streams'][s]['sha256'])

    def test_old_64k_baseline_reproduction(self):
        with self.assertRaises(JobError) as e:run_bounded([sys.executable,'-c','import os;os.write(1,b"x"*100000)'],env={})
        self.assertEqual(e.exception.code,'OUTPUT_LIMIT_EXCEEDED')

    def test_large_both_streams_and_bounded_capture(self):
        for n in (70000,1024**2+123,9*1024**2+7):
            body=f'import sys;sys.stdout.write(\'{{"type":"assistant","message":"\'+"x"*{n}+\'"}}\\n\');sys.stdout.flush();sys.stderr.write("e"*{n});sys.stderr.flush();'+FINAL
            result,job=self.run_fixture(body)
            self.assertEqual(result.final,'fixture-final')
            self.assertGreater(result.streaming['streams']['stdout']['observed_bytes'],n)
            self.assertEqual(result.streaming['streams']['stderr']['spool_bytes'],n)
            self.assertLessEqual(len(job.tails['stdout']),65536)
            self.assert_digest(job,result.streaming)

    def test_private_files(self):
        _,job=self.run_fixture(FINAL)
        self.assertEqual((self.root/'spool').stat().st_mode&0o777,0o700)
        for p in (self.root/'spool'/job.name).iterdir():self.assertEqual(p.stat().st_mode&0o777,0o600)

    def test_stream_quota_preserves_observed_and_written(self):
        b=ExecutionBudget(spool_max_bytes_per_stream=1024,spool_max_bytes_per_job=2048,spool_max_bytes_total=4096)
        job=self.job(b)
        with self.assertRaises(JobError) as e:self.run_fixture('import os;os.write(2,b"x"*10000)',budget=b,job=job)
        self.assertEqual(e.exception.code,'SPOOL_QUOTA_EXCEEDED')
        m=e.exception.streaming;self.assertTrue(m['streams']['stderr']['truncated'])
        self.assert_digest(job,m)
        self.assertLessEqual(sum(x['spool_bytes'] for x in m['streams'].values()),2048)

    def test_job_and_total_quota(self):
        for which in ('job','total'):
            b=ExecutionBudget(spool_max_bytes_per_stream=8192,spool_max_bytes_per_job=8192,spool_max_bytes_total=8192 if which=='total' else 16384)
            j=self.job(b)
            if which=='total':
                # Previous subcase retained bytes count towards quota; expire it explicitly.
                for old in self.store.entries():
                    if old != j.name:
                        for leaf in (self.root/'spool'/old).iterdir():leaf.unlink()
                        (self.root/'spool'/old).rmdir()
                j.append('stderr',b'x'*7000);j.close();j=self.job(b)
            with self.assertRaises(JobError) as e:self.run_fixture('import os;os.write(2,b"x"*7000);os.write(1,b"x"*7000)',budget=b,job=j)
            self.assertEqual(e.exception.code,'SPOOL_QUOTA_EXCEEDED');j.close()

    def test_disk_full_diagnostics(self):
        job=self.job();original=os.write
        def full(fd,data):
            if fd==job.files['stderr']:raise OSError(errno.ENOSPC,'fixture disk full')
            return original(fd,data)
        with patch('aee.mcp_runtime.spool.os.write',side_effect=full),self.assertRaises(JobError) as e:
            self.run_fixture('import os;os.write(2,b"fixture-diagnostic")',job=job)
        self.assertEqual(e.exception.code,'SPOOL_IO_ERROR')
        self.assertIn('fixture-diagnostic',e.exception.streaming['streams']['stderr']['tail'])

    def test_partial_write_digest(self):
        job=self.job();original=os.write
        def short(fd,data):return original(fd,data[:3]) if fd in job.files.values() else original(fd,data)
        with patch('aee.mcp_runtime.spool.os.write',side_effect=short):result,_=self.run_fixture(FINAL,job=job)
        self.assert_digest(job,result.streaming)

    def test_metadata_write_failure(self):
        job=self.job()
        with patch.object(self.store,'write_meta',side_effect=OSError(errno.ENOSPC,'fixture')),self.assertRaises(JobError) as e:
            self.run_fixture(FINAL,job=job)
        self.assertEqual(e.exception.code,'SPOOL_IO_ERROR');self.assertIsNotNone(e.exception.streaming)

    def test_timeout_retains_stderr(self):
        b=ExecutionBudget(wall_timeout_seconds=.3)
        with self.assertRaises(JobError) as e:self.run_fixture('import sys,time;print("partial",file=sys.stderr,flush=True);time.sleep(10)',budget=b)
        self.assertEqual(e.exception.code,'EXECUTION_TIMEOUT')
        self.assertIn('partial',e.exception.streaming['streams']['stderr']['tail'])

    def test_idle_not_reset_by_noisy_stderr(self):
        b=ExecutionBudget(wall_timeout_seconds=5,idle_timeout_seconds=.3)
        with self.assertRaises(JobError) as e:self.run_fixture('import time,sys\nwhile True:\n print("noise",file=sys.stderr,flush=True);time.sleep(.01)',budget=b)
        self.assertEqual(e.exception.code,'EXECUTION_IDLE_TIMEOUT')

    def test_valid_events_reset_idle(self):
        b=ExecutionBudget(wall_timeout_seconds=4,idle_timeout_seconds=.3)
        result,_=self.run_fixture('import time\nfor i in range(6):\n print(\'{"type":"assistant"}\',flush=True);time.sleep(.1)\n'+FINAL,budget=b)
        self.assertEqual(result.final,'fixture-final')

    def test_cancel_entire_group_and_retains_metadata(self):
        b=ExecutionBudget(wall_timeout_seconds=5)
        event=threading.Event();pidfile=self.root/'descendant.pid'
        body=f'import subprocess,sys,time;child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"]);open({str(pidfile)!r},"w").write(str(child.pid));print("ready",file=sys.stderr,flush=True);time.sleep(30)'
        timer=threading.Timer(.4,event.set);timer.start();self.addCleanup(timer.cancel)
        with self.assertRaises(JobError) as e:self.run_fixture(body,budget=b,cancel=event)
        self.assertEqual(e.exception.code,'EXECUTION_CANCELLED')
        self.assert_dead(int(pidfile.read_text()))

    def assert_dead(self,pid):
        for _ in range(100):
            try:
                state=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[0]
                if state=='Z':return
            except FileNotFoundError:return
            time.sleep(.01)
        self.fail('Descendant remained running after group cleanup')

    def test_gateway_crash_guard_and_restart_evidence(self):
        root=self.root/'crash-jobs';pidfile=self.root/'crash-child.pid'
        child_body=f'import os,time,json;open({str(pidfile)!r},"w").write(str(os.getpid()));print(json.dumps({{"type":"assistant"}}),flush=True);time.sleep(60)'
        script=f'''
import os,sys
from pathlib import Path
from aee.mcp_runtime.store import JobStore
from aee.mcp_runtime.spool import SpoolStore
from aee.mcp_runtime.budget import ExecutionBudget
from aee.mcp_runtime.process import run_streamed
from aee.mcp_runtime.event_stream import EventParser
store=JobStore(Path({str(root)!r}));record=store.create('claude','fixture','/tmp','read_only',execution={{'kind':'job-adapter'}})
b=ExecutionBudget();spools=SpoolStore(Path({str(root)!r})/'spool');j=spools.create(record['job_id'],b,'fixture')
store.update(record['job_id'],status='running',execution={{'streaming':j.metadata('running')}})
run_streamed([sys.executable,'-c',{child_body!r}],env={{}},stdin=b'',budget=b,spool=j,parser=EventParser('claude'))
'''
        parent=subprocess.Popen([sys.executable,'-c',script],cwd=ROOT,env={**os.environ,'PYTHONPATH':str(ROOT)},stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        self.addCleanup(lambda:parent.poll() is None and parent.kill())
        try:
            deadline=time.monotonic()+5
            while not pidfile.exists() and time.monotonic()<deadline:time.sleep(.01)
            self.assertTrue(pidfile.exists())
            time.sleep(.05)
            parent.kill();parent.wait(timeout=5)
            self.assert_dead(int(pidfile.read_text()))
            recovered=JobStore(root);self.addCleanup(recovered.close)
            record=recovered.get(recovered.list_ids()[0])
            self.assertEqual(record['error_code'],'EXECUTION_INTERRUPTED')
            self.assertEqual(record['execution']['streaming']['state'],'interrupted')
            self.assertGreater(record['execution']['streaming']['streams']['stdout']['spool_bytes'],0)
        finally:
            if parent.poll() is None:parent.kill();parent.wait()
            parent.stderr.close()

    def test_concurrent_independent_spools(self):
        jobs=[self.job() for _ in range(4)]
        def run(pair):
            i,j=pair
            return self.run_fixture(f'print(\'{{"type":"result","result":"job-{i}"}}\')',job=j)[0]
        with concurrent.futures.ThreadPoolExecutor(4) as pool:results=list(pool.map(run,enumerate(jobs)))
        self.assertEqual([r.final for r in results],[f'job-{i}' for i in range(4)])
        self.assertEqual(len({r.streaming['artifact_id'] for r in results}),4)

    def test_retention_never_removes_active_spool(self):
        b=ExecutionBudget(retention_seconds=1);j=self.job(b)
        with patch('aee.mcp_runtime.spool.time.time',return_value=time.time()+3):self.store.reconcile(1)
        self.assertTrue((self.root/'spool'/j.name).exists())
        j.finish();j.close()
        with patch('aee.mcp_runtime.spool.time.time',return_value=time.time()+3):self.store.reconcile(1)
        self.assertFalse((self.root/'spool'/j.name).exists())

    def test_path_symlink_and_hardlink_boundaries(self):
        with self.assertRaises(JobError):self.store.create('../escape',ExecutionBudget(),'fixture')
        link=self.root/'link';link.symlink_to(self.root/'spool')
        with self.assertRaises(OSError):SpoolStore(link)
        job=self.job();os.link(self.root/'spool'/job.name/'stdout',self.root/'hardlink')
        with self.assertRaises(JobError):self.store.total()

    def test_diagnostic_redaction(self):
        result,_=self.run_fixture('import sys;print("sensitive-sentinel",file=sys.stderr);'+FINAL,redact=lambda x:x.replace('sensitive-sentinel','[REDACTED]'))
        self.assertNotIn('sensitive-sentinel',json.dumps(result.streaming))

    def test_control_diagnostics_manifest_stays_bounded(self):
        body='import os;os.write(1,b"\\t"*20000);os.write(2,b"\\0"*20000);'+FINAL
        result,job=self.run_fixture(body)
        self.assertEqual(result.final,'fixture-final')
        self.assertLessEqual((self.root/'spool'/job.name/'manifest.json').stat().st_size,16384)

    def test_orphan_metadata_temporary_cleanup(self):
        job=self.job();job.finish();job.close()
        orphan=self.root/'spool'/job.name/('.manifest-'+'a'*16)
        orphan.write_bytes(b'incomplete');orphan.chmod(0o600)
        self.store.reconcile(86400)
        self.assertFalse(orphan.exists())

    def test_nonzero_exit_spool_is_failed(self):
        job=self.job()
        with self.assertRaises(JobError) as e:self.run_fixture('import sys;print("failure",file=sys.stderr);sys.exit(3)',job=job)
        self.assertEqual(e.exception.code,'EXECUTION_FAILED')
        self.assertEqual(e.exception.exit_code,3)
        self.assertEqual(e.exception.streaming['state'],'failed')
        self.assertEqual(json.loads((self.root/'spool'/job.name/'manifest.json').read_text())['state'],'failed')


class CompactTerminalTests(unittest.TestCase):
    def test_worst_case_json_expansion_keeps_failure_counters(self):
        from aee.mcp_runtime.store import compact_adapter_terminal, MAX_RECORD_BYTES
        record={'task':'\0'*8000,'working_directory':'x'*4000}
        fields={'status':'failed','error_code':'EXECUTION_TIMEOUT','error':'deadline',
                'log_excerpt':'\0'*2000,'execution':{'streaming':{'schema':'aee-stream-v1',
                  'streams':{'stdout':{'tail':'\0'*2000,'observed_bytes':90000,'spool_bytes':90000,'sha256':'a'*64},
                             'stderr':{'tail':'\0'*2000,'observed_bytes':90000,'spool_bytes':90000,'sha256':'b'*64}}}}}
        result=compact_adapter_terminal(record,fields)
        self.assertLess(len(json.dumps({**record,**result},ensure_ascii=False).encode()),MAX_RECORD_BYTES)
        self.assertEqual(result['error_code'],'EXECUTION_TIMEOUT')
        self.assertEqual(result['execution']['streaming']['streams']['stdout']['observed_bytes'],90000)
        self.assertTrue(result['execution']['streaming']['streams']['stdout']['tail_omitted'])

    def test_success_summary_clips_with_truncation(self):
        from aee.mcp_runtime.store import compact_adapter_terminal
        result=compact_adapter_terminal({'task':'\0'*8000,'working_directory':'x'*4000},
            {'status':'completed','summary':'\0'*4000,'truncated':False})
        self.assertTrue(result['truncated']);self.assertGreater(len(result['summary']),0)
