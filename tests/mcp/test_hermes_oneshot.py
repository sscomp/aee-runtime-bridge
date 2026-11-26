"""Hermes run records and real callback results govern AEE completion."""
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aee.mcp_runtime.hermes_oneshot import run
from aee.mcp_runtime.event_stream import EventParser
from aee.mcp_runtime.job_adapters import adapter_specs, verify_autonomous_capabilities
from aee.mcp_runtime.store import JobError


class HermesOneshot(unittest.TestCase):
    def execute(self, *, result=None, tool_result='{"exit_code":0}', incomplete=False,
                exception=None):
        record = {'completed': True, 'final_response': 'verified final'} if result is None else result
        testcase = self
        class Agent:
            def __init__(self, **kwargs):
                testcase.assertEqual(os.environ['TERMINAL_CWD'], os.getcwd())
                self.callbacks = kwargs
            def run_conversation(self, prompt):
                if exception:
                    raise exception
                self.callbacks['tool_start_callback']('id1', 'terminal', {'secret': 'private-args'})
                if not incomplete:
                    self.callbacks['tool_complete_callback']('id1', 'terminal', {}, tool_result)
                return record
        module = SimpleNamespace(AIAgent=Agent)
        def oneshot(prompt):
            agent = module.AIAgent()
            agent.run_conversation(prompt)
            print('native plain-text noise must not enter JSONL')
            return 0
        output = io.StringIO()
        with patch.dict('os.environ', {'TERMINAL_CWD': '/unrelated-home-default'}, clear=True):
            code = run('private prompt', output, (module, SimpleNamespace(run_oneshot=oneshot)))
        self.assertIs(module.AIAgent, Agent)
        self.assertNotIn('private-args', output.getvalue())
        self.assertNotIn('private prompt', output.getvalue())
        self.assertNotIn('native plain-text noise', output.getvalue())
        parser = EventParser('hermes')
        parser.feed(output.getvalue().encode(), eof=True)
        return code, parser, output.getvalue()

    def test_success_has_native_callback_evidence(self):
        code, parser, _ = self.execute()
        self.assertEqual(code, 0)
        self.assertEqual(parser.result(), 'verified final')
        self.assertEqual(parser.tool_evidence()['tools_completed'], 1)

    def test_failed_tool_cannot_be_hidden_by_successful_agent_answer(self):
        for result in ['{"exit_code":1}', '{"error":"denied"}', '{"success":false}']:
            code, parser, _ = self.execute(tool_result=result)
            self.assertEqual(code, 1)
            with self.assertRaises(JobError):
                parser.result()

    def test_incomplete_callbacks_and_partial_runs_refuse_success(self):
        cases = [{'incomplete': True}, {'result': {'completed': False, 'final_response': 'ok'}},
                 {'result': {'completed': True, 'partial': True, 'final_response': 'ok'}},
                 {'result': {'completed': True, 'failed': True, 'final_response': 'ok'}}]
        for case in cases:
            code, parser, _ = self.execute(**case)
            self.assertEqual(code, 1)
            with self.assertRaises(JobError):
                parser.result()

    def test_provider_exception_does_not_leak_message(self):
        code, parser, output = self.execute(exception=RuntimeError('secret-provider-token'))
        self.assertEqual(code, 1)
        self.assertNotIn('secret-provider-token', output)
        with self.assertRaises(JobError):
            parser.result()

    def test_wrapper_and_native_keep_distinct_stdin_argv(self):
        adapter = adapter_specs()['hermes']
        work = '/fixture/work'
        for name, options in [('hermes-aee', []), ('hermes', ['--toolsets', 'hermes-cli'])]:
            with self.subTest(name=name):
                path = str(Path('/fixture/bin') / name)
                self.assertEqual(adapter.build_argv(work, path, 'autonomous'),
                                 [path, 'chat', '--yolo', *options,
                                  '--query-file', '-', '--format', 'stream-json'])
                for mode in ('read_only', 'write'):
                    with self.assertRaises(NotImplementedError):
                        adapter.build_argv(work, path, mode)
        self.assertEqual(adapter.read_only_enforcement, 'UNSUPPORTED')
        self.assertEqual(adapter.write_enforcement, 'UNSUPPORTED')

    @patch('aee.mcp_runtime.job_adapters.run_bounded')
    def test_capability_matches_the_command_that_will_run(self, probe):
        help_text = b'--query-file --format --yolo'
        probe.return_value = SimpleNamespace(returncode=0, stdout=help_text)
        verify_autonomous_capabilities('hermes', '/fixture/bin/hermes-aee')
        with self.assertRaises(JobError) as error:
            verify_autonomous_capabilities('hermes', '/fixture/bin/hermes')
        self.assertEqual(error.exception.code, 'ADAPTER_UNSUPPORTED')
        probe.return_value.stdout += b' --toolsets'
        verify_autonomous_capabilities('hermes', '/fixture/bin/hermes')
