"""Regressions from the N2 ChatGPT title-only/recursive dispatch incident."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aee.mcp_runtime.event_stream import EventParser
from aee.mcp_runtime.executor import final_agent_message
from aee.mcp_runtime.process import Limits, run_bounded
from aee.mcp_runtime.store import JobError


NOTICE = ('Ignored unsupported project-local config keys in '
          '/Users/fixture/.codex/config.toml: notify. If you want these '
          'settings to apply, manually set them in your user-level config.toml.')


def stream(events):
    return ('\n'.join(json.dumps(e) for e in events) + '\n').encode()


def item(message, **extra):
    return {'type': 'item.completed', 'item': {'type': 'error', 'message': message, **extra}}


START = {'type': 'turn.started'}
ANSWER = {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'verified'}}
END = {'type': 'turn.completed'}


class NativeConfigNotices(unittest.TestCase):
    def check_both(self, events, expected_error=None):
        raw = stream(events)
        parser = EventParser('codex')
        parser.feed(raw, eof=True)
        functions = [parser.result, lambda: final_agent_message(raw, Limits())]
        for function in functions:
            if expected_error:
                with self.assertRaises(JobError) as error:
                    function()
                self.assertEqual(error.exception.code, expected_error)
            else:
                self.assertEqual(function(), 'verified')
        return parser

    def test_observed_duplicate_startup_notices_are_counted_not_fatal(self):
        parser = self.check_both([item(NOTICE), item(NOTICE), START, ANSWER, END])
        self.assertEqual(parser.tool_evidence()['startup_config_notices'], 2)
        self.assertNotIn('/Users/fixture', json.dumps(parser.tool_evidence()))

    def test_runtime_or_different_errors_remain_fatal(self):
        for events in ([START, item(NOTICE), ANSWER, END],
                       [item(NOTICE.replace('notify.', 'sandbox_mode.')), START, ANSWER, END],
                       [item(NOTICE + ' extra'), START, ANSWER, END],
                       [item(NOTICE, status='failed'), START, ANSWER, END],
                       [item('session collapsed'), START, ANSWER, END],
                       [item(NOTICE), START, item('real failure'), ANSWER, END]):
            with self.subTest(events=events):
                self.check_both(events, 'REQUIRED_TOOL_FAILED')

    def test_notice_never_synthesizes_completion(self):
        raw = stream([item(NOTICE), START, ANSWER])
        parser = EventParser('codex'); parser.feed(raw, eof=True)
        for function in [parser.result, lambda: final_agent_message(raw, Limits())]:
            with self.assertRaises(JobError):
                function()

    def test_approved_broker_error_policy_stays_strict(self):
        with self.assertRaises(JobError) as error:
            final_agent_message(stream([item(NOTICE), START, ANSWER, END]),
                                Limits(), require_receipt=True)
        self.assertEqual(error.exception.code, 'REQUIRED_TOOL_FAILED')

    def test_duplicate_notice_message_is_ambiguous(self):
        raw = ('{"type":"item.completed","item":{"type":"error",'
               '"message":"real failure","message":' + json.dumps(NOTICE) + '}}\n').encode()
        parser = EventParser('codex')
        with self.assertRaises(JobError):
            parser.feed(raw, eof=True)
        with self.assertRaises(JobError):
            final_agent_message(raw, Limits())


class DispatchInput(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='aee-n2-dispatch-')
        root = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location('n2_regression_gateway', root / 'mcp_gateway.py')
        cls.g = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {'A3_JOB_STORE_DIR': cls.temp.name,
                                    'AEE_MCP_SURFACE': 'local', 'AEE_MCP_PORT': '8790'}, clear=True):
            spec.loader.exec_module(cls.g)

    @classmethod
    def tearDownClass(cls):
        cls.g.JOB_STORE.close()
        cls.temp.cleanup()

    def test_title_and_prompt_are_both_forwarded_without_losing_instructions(self):
        for task, prompt, expected in [
                ('Docker verification', 'Run docker version; report exit status.',
                 'Docker verification\n\nRun docker version; report exit status.'),
                (None, 'legacy instructions', 'legacy instructions'),
                ('canonical instructions', None, 'canonical instructions'),
                ('same instructions', 'same instructions', 'same instructions')]:
            record = dict(job_id='fixture', status='queued', agent='codex',
                          mode='autonomous', working_directory='/tmp', created_at='fixture')
            with patch.object(self.g, 'dispatch_job', return_value=record) as dispatch:
                result = json.loads(asyncio.run(self.g.aee_dispatch(
                    agent='codex', working_directory='/tmp', mode='autonomous',
                    task=task, prompt=prompt)))
            self.assertTrue(result['ok'])
            self.assertEqual(dispatch.call_args.args[1], expected)


class LocalWorkerInput(unittest.TestCase):
    def test_both_agents_receive_local_execution_role_and_full_task_on_stdin(self):
        from aee.mcp_runtime import job_adapters as adapters
        from aee.mcp_runtime.budget import ExecutionBudget
        task = 'Run pwd and docker version directly. Report real results.'
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / 'fixture'; executable.write_text('fixture')
            for agent in ['claude', 'codex']:
                for budget in [None, ExecutionBudget()]:
                    execution = {'adapter': {'executable': str(executable),
                                             'executable_sha256': adapters._sha256(executable)}}
                    stdout = (json.dumps({'type': 'result', 'result': 'verified'}).encode()
                              if agent == 'claude' else stream([START, ANSWER, END]))
                    result = SimpleNamespace(stdout=stdout, stderr=b'', returncode=0,
                                             final='verified', streaming={
                                                 'streams': {'stderr': {'tail': ''}}})
                    runner = 'run_streamed' if budget else 'run_bounded'
                    with patch.object(adapters, runner, return_value=result) as run, \
                            patch.object(adapters, 'verify_claude_readonly_capabilities'):
                        adapters._execute_adapter_job(adapters.adapter_specs()[agent], str(executable),
                            execution, task, tmp, limits=Limits(), budget=budget, spool=object())
                    data = (run.call_args.kwargs['stdin_prefix'] + run.call_args.kwargs['stdin']).decode()
                    self.assertIn('already executing this dispatched job', data)
                    self.assertIn('Do not redispatch this job', data)
                    self.assertTrue(data.endswith('Dispatched task:\n' + task))
                    self.assertEqual(execution['worker_context']['working_directory'], tmp)

    def test_worker_context_keeps_the_full_task_byte_budget(self):
        task = '🧪'.encode('utf-8') * 8000
        prefix = b'AEE local worker context\n'
        result = run_bounded(
            [sys.executable, '-c', 'import sys; print(len(sys.stdin.buffer.read()))'],
            env={}, stdin=task, stdin_prefix=prefix, limits=Limits(timeout=10),
        )
        self.assertEqual(result.stdout.strip(), str(len(task) + len(prefix)).encode())


if __name__ == '__main__':
    unittest.main()
