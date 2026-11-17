"""Offline CLI checks against a logging bridge fixture; never calls a gateway."""
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import signal
import sys
import tempfile
import time
import unittest

from aee.mcp_runtime.job_identity import JOB_ID

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT/'scripts/aee-job.sh'
JOB = 'M2JOB-20261117-' + '0123456789abcdef'*2
A3 = JOB.replace('M2JOB', 'A3JOB')


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='helper-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root/'calls.jsonl'
        self.bridge = self.root/'mock bridge'
        self.bridge.write_text('#!'+sys.executable+'\n'+'''import json, os, sys
from pathlib import Path
log = Path(os.environ['MOCK_LOG'])
Path(str(log)+'.locale').write_text(os.environ.get('LC_ALL', ''))
prior = log.read_text().splitlines() if log.exists() else []
with log.open('a') as output:
    output.write(json.dumps(sys.argv[1:])+'\\n')
if os.environ.get('MOCK_EXIT'):
    sys.exit(int(os.environ['MOCK_EXIT']))
if 'MOCK_RAW' in os.environ:
    print(os.environ['MOCK_RAW'])
    sys.exit(0)
command, job_id = sys.argv[1:]
assert command in ('job-status', 'job-result')
states = os.environ.get('MOCK_STATES', 'completed').split(',')
index = sum(json.loads(line)[0] == 'job-status' for line in prior)
state = states[min(index, len(states)-1)] if command == 'job-status' else states[-1]
value = dict(ok=True, job_id=job_id, status=state, summary='fixture result')
if command == 'job-result' and state in ('queued','running'):
    value.update(ok=False, error_code='JOB_NOT_COMPLETE')
elif command == 'job-result' and state in ('failed','timed_out','cancelled'):
    value.update(ok=False, error_code='FIXTURE_FAILURE')
print(json.dumps(value))
''')
        self.bridge.chmod(0o700)
        self.bin = self.root/'bin'
        self.bin.mkdir()
        (self.bin/'jq').symlink_to(shutil.which('jq'))
        (self.bin/'cat').symlink_to(shutil.which('cat'))
        (self.bin/'sleep').write_text('#!/bin/sh\nexit 0\n')
        (self.bin/'sleep').chmod(0o700)
        self.env = dict(PATH=str(self.bin), HOME=str(self.root/'home'),
                        AEE_BRIDGE_BIN=str(self.bridge), MOCK_LOG=str(self.log),
                        LC_ALL='C.UTF-8', BASH_COMPAT='3.2')

    def call(self, *args, script=SCRIPT, **extra):
        self.log.unlink(missing_ok=True)
        result = subprocess.run(['/bin/bash', str(script), *args],
                                env={**self.env, **extra}, cwd=self.root, text=True,
                                capture_output=True, timeout=15)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] \
            if self.log.exists() else []
        return result, calls

    def test_cli_acceptance_matches_canonical_python_fullmatch(self):
        valid = [JOB.replace('M2JOB', prefix) for prefix in
                 ('A3JOB', 'N2JOB', 'M2JOB', 'AJOB', 'FUTURE42JOB',
                  'A'+'9'*15+'JOB', 'ZJOB', 'A3JOBJOB')]
        # Calendar validity is deliberately NOT part of canonical reader grammar.
        valid += [JOB.replace('20261117', date) for date in
                  ('00000000', '99999999', '20260230', '20261301')]
        bad = ['', 'JOB-20261117-'+'a'*32, '1JOB-20261117-'+'a'*32,
               JOB.replace('M2JOB', 'A'*17+'JOB'), JOB.replace('M2JOB', 'm2JOB'),
               JOB.replace('M2JOB', 'Ｍ2JOB'), JOB.replace('M2JOB', 'ÄJOB'),
               JOB.replace('M2JOB', 'A_JOB'), JOB.replace('M2JOB', 'A-JOB'),
               JOB.replace('20261117', '2026111'), JOB.replace('20261117', '202611170'),
               JOB.replace('20261117', '2026x117'), JOB.replace('20261117', '２０２６１１１７'),
               JOB[:-1], JOB+'0', JOB[:-1]+'g', JOB.upper(),
               JOB[:-1]+'F', '../'+JOB, '/tmp/'+JOB, JOB+'/../escape',
               JOB+'\\escape', JOB+'.json', JOB+'\n', JOB+'\r', JOB+'\t',
               ' '+JOB, JOB+' ', JOB+';touch PWNED', JOB+'$(touch PWNED)',
               JOB+'`touch PWNED`', JOB+'\nM2JOB', JOB+'|cat', JOB+'&',
               JOB+'"', JOB+"'", JOB+'%', JOB+'\x1b', JOB+'é', JOB+'\u2028']
        rng = random.Random(18)
        for _ in range(40):
            position = rng.randrange(len(JOB)+1)
            bad.append(JOB[:position]+rng.choice('/. ;$\n\t_é')+JOB[position:])
        for value in valid+bad:
            with self.subTest(value=value):
                expected = JOB_ID.fullmatch(value) is not None
                result, calls = self.call(value, '--json')
                self.assertEqual(result.returncode, 0 if expected else 2, result.stderr)
                self.assertEqual(calls, [['job-status', value]] if expected else [])
                if expected:
                    self.assertEqual(json.loads(result.stdout)['data']['job_id'], value)
                    self.assertEqual(Path(str(self.log)+'.locale').read_text(), 'C.UTF-8')
        self.assertFalse((self.root/'PWNED').exists())
        self.assertFalse((self.root/'escape').exists())

    def test_status_result_watch_and_json_forwarding(self):
        cases = [((), ['job-status']), (('status',), ['job-status']),
                 (('result',), ['job-result']),
                 (('watch',), ['job-status', 'job-result'])]
        for options, commands in cases:
            for json_option in ((), ('--json',)):
                with self.subTest(options=options, json=json_option):
                    result, calls = self.call(*options, JOB, *json_option)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(calls, [[command, JOB] for command in commands])
                    if json_option:
                        rows = [json.loads(line) for line in result.stdout.splitlines()]
                        self.assertEqual([row['kind'] for row in rows],
                                         [command.removeprefix('job-') for command in commands])
        result, calls = self.call('watch', JOB, '--json', '--interval', '1',
                                  MOCK_STATES='running,completed')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [[command, JOB] for command in
                                ('job-status', 'job-status', 'job-result')])
        for bound in ('1', '3600'):
            result, _ = self.call('watch', JOB, '--interval', bound)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_help_and_argument_errors_never_forward(self):
        for args, code in [(('--help',), 0), (('-h',), 0),
                           (('watch', '--help'), 0), ((), 2), ((JOB, JOB), 2),
                           (('--unknown', JOB), 2), ((JOB, '--interval', '5'), 2),
                           (('watch', JOB, '--interval'), 2)]:
            with self.subTest(args=args):
                result, calls = self.call(*args)
                self.assertEqual(result.returncode, code)
                self.assertEqual(calls, [])
        result, _ = self.call('--help')
        self.assertIn('M2JOB', result.stdout)
        for interval in ('0', '-1', '3601', '0001', '1.5', 'abc', '1;touch PWNED'):
            with self.subTest(interval=interval):
                result, calls = self.call('watch', JOB, '--interval', interval)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(calls, [])

    def test_gateway_failures_and_identity_validation_keep_exit_codes(self):
        replies = [('not-json', 4), ('{}', 4), ('[]', 4),
                   (json.dumps(dict(ok=False, error_code='JOB_NOT_FOUND')), 3),
                   (json.dumps(dict(ok=True, job_id=A3, status='completed')), 4),
                   (json.dumps(dict(ok=True, job_id=JOB, status='unknown')), 4),
                   (json.dumps(dict(ok=False, job_id=JOB, status='running')), 4),
                   (json.dumps(dict(ok='true', job_id=JOB, status='completed')), 4)]
        for reply, code in replies:
            with self.subTest(reply=reply):
                result, calls = self.call(JOB, MOCK_RAW=reply)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(calls, [['job-status', JOB]])
        result, _ = self.call(JOB, MOCK_EXIT='7')
        self.assertEqual(result.returncode, 4)
        for state in ('queued', 'running', 'failed', 'timed_out', 'cancelled'):
            result, calls = self.call('result', JOB, '--json', MOCK_STATES=state)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(calls, [['job-result', JOB]])
            self.assertFalse(json.loads(result.stdout)['data']['ok'])

    def test_ctrl_c_stops_watch_with_existing_exit_code(self):
        process = subprocess.Popen(
            ['/bin/bash', str(SCRIPT), 'watch', JOB, '--interval', '1'],
            env={**self.env, 'MOCK_STATES': 'running'}, cwd=self.root,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not self.log.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(self.log.exists(), 'watch did not query mock bridge')
            process.send_signal(signal.SIGINT)
            _, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 130, stderr)
            calls = [json.loads(line) for line in self.log.read_text().splitlines()]
            self.assertTrue(calls)
            self.assertTrue(all(call == ['job-status', JOB] for call in calls))
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_dependencies_and_default_bridge_path(self):
        result, calls = self.call(JOB, AEE_BRIDGE_BIN='/nonexistent/bridge')
        self.assertEqual(result.returncode, 4)
        self.assertEqual(calls, [])
        target = Path(self.env['HOME'])/'.local/bin/aee-bridge'
        target.parent.mkdir(parents=True)
        target.symlink_to(self.bridge)
        result, calls = self.call(JOB, AEE_BRIDGE_BIN='')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [['job-status', JOB]])
        (self.bin/'jq').unlink()
        result, calls = self.call(JOB)
        self.assertEqual(result.returncode, 4)
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
