"""Hermes run records and real callback results govern AEE completion."""
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aee.mcp_runtime.hermes_oneshot import run
from aee.mcp_runtime.event_stream import EventParser
from aee.mcp_runtime.job_adapters import (
    adapter_specs, autonomous_capability, verify_autonomous_capabilities,
)
from aee.mcp_runtime.store import JobError


class HermesOneshot(unittest.TestCase):
    def execute(self, *, result=None, tool_result='{"exit_code":0}', incomplete=False,
                exception=None, callback_events=None, native_code=0, cleanup_error=None):
        record = {'completed': True, 'final_response': 'verified final'} if result is None else result
        testcase = self
        tool_id = 'private-provider-id'

        class Agent:
            def __init__(self, *, tool_start_callback, tool_complete_callback):
                testcase.assertEqual(os.environ['TERMINAL_CWD'], os.getcwd())
                self.callbacks = {'start': tool_start_callback, 'complete': tool_complete_callback}

            def run_conversation(self, prompt):
                if exception:
                    raise exception
                if callback_events is None:
                    self.callbacks['start'](tool_id, 'terminal', {'secret': 'private-args'})
                    if not incomplete:
                        self.callbacks['complete'](tool_id, 'terminal', {}, tool_result)
                else:
                    for kind, args in callback_events:
                        self.callbacks[kind](*args)
                return record

        module = SimpleNamespace(AIAgent=Agent)

        def oneshot(prompt):
            agent = module.AIAgent()
            agent.run_conversation(prompt)
            print('native plain-text noise must not enter JSONL')
            return native_code

        cli = SimpleNamespace(_cleanup_oneshot_runtime=Mock(side_effect=cleanup_error),
                              _exit_after_oneshot=Mock())
        config = SimpleNamespace(apply_terminal_config_to_env=Mock())
        output = io.StringIO()
        with patch.dict('os.environ', {'TERMINAL_CWD': '/unrelated-home-default'}, clear=True):
            code = run('private prompt', output, (module, SimpleNamespace(run_oneshot=oneshot), cli, config))
        cli._cleanup_oneshot_runtime.assert_called_once_with()
        self.assertIs(module.AIAgent, Agent)
        for private in ('private-args', 'private prompt', 'native plain-text noise', tool_id):
            self.assertNotIn(private, output.getvalue())
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

    def test_plain_text_and_structured_success(self):
        for value in ('ordinary tool output', '0', {'output': 'ok'}, '{"returncode":0}'):
            with self.subTest(value=value):
                code, parser, output = self.execute(tool_result=value)
                self.assertEqual(code, 0)
                self.assertEqual(parser.result(), 'verified final')
                self.assertNotIn('ordinary tool output', output)

    def test_absent_malformed_and_truncated_tool_results_fail(self):
        values = (None, [], 0, {}, '', '  ', 'null', '[1]', '{"exit_code":',
                  '{"exit_code":0,"exit_code":1}', '{"exit_code":false}', '{"exit_code":0.0}',
                  {'exit_code': None}, {'is_error': 'false'}, {'success': None}, {'truncated': True},
                  'Error executing tool terminal: timeout', '[Tool execution cancelled due to interruption]')
        for value in values:
            with self.subTest(value=value):
                code, parser, _ = self.execute(tool_result=value)
                self.assertEqual(code, 1)
                with self.assertRaises(JobError):
                    parser.result()

    def test_invalid_missing_unmatched_and_duplicate_ids_are_sticky(self):
        first = ('start', ('first', 'terminal', {}))
        done = ('complete', ('first', 'terminal', {}, 'valid text'))
        later = [('start', ('later', 'terminal', {})),
                 ('complete', ('later', 'terminal', {}, {'exit_code': 0}))]
        cases = (
            [('start', ([], 'terminal', {})), first, done],
            [first, ('complete', ([], 'terminal', {}, 'valid text')), done],
            [first, ('complete', (None, 'terminal', {}, 'valid text')), done],
            [first, ('complete', (' ', 'terminal', {}, 'valid text')), done],
            [first, ('complete', ('unmatched', 'terminal', {}, 'valid text')), done],
            [first, ('complete', ()), done],
            [first, ('complete', ('first', 'terminal', {}))],
            [done, first, done],
            [first, first, done],
            [first, done, done],
            [first, ('complete', ('first', 'terminal', {}, {'exit_code': 1}))],
        )
        for events in cases:
            with self.subTest(events=events):
                code, parser, _ = self.execute(callback_events=events + later)
                self.assertEqual(code, 1)
                with self.assertRaises(JobError):
                    parser.result()

    def test_parallel_pairs_can_finish_out_of_order(self):
        code, parser, _ = self.execute(callback_events=[
            ('start', ('one', 'terminal', {})), ('start', ('two', 'terminal', {})),
            ('complete', ('two', 'terminal', {}, 'valid')),
            ('complete', ('one', 'terminal', {}, {'exit_code': 0})),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(parser.result(), 'verified final')
        self.assertEqual(parser.tool_evidence()['tools_completed'], 2)

    def test_native_oneshot_exit_requires_integer_zero(self):
        for value in (False, 0.0, None, 1, -1):
            with self.subTest(value=value):
                code, parser, _ = self.execute(native_code=value)
                self.assertEqual(code, 1)
                with self.assertRaises(JobError):
                    parser.result()

    def test_incomplete_callbacks_and_partial_runs_refuse_success(self):
        cases = [{'incomplete': True}, {'result': {'completed': False, 'final_response': 'ok'}},
                 {'result': {'completed': True, 'partial': True, 'final_response': 'ok'}},
                 {'result': {'completed': True, 'failed': True, 'final_response': 'ok'}},
                 {'result': {'completed': True, 'interrupted': True, 'final_response': 'ok'}},
                 {'result': {'completed': True, 'truncated': True, 'final_response': 'ok'}},
                 {'result': {'completed': True, 'final_response': '   '}},
                 {'result': {'completed': True}}, {'result': []}]
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

    def test_cleanup_failure_prevents_success_without_details(self):
        code, parser, output = self.execute(cleanup_error=RuntimeError('private-cleanup-value'))
        self.assertEqual(code, 1)
        self.assertNotIn('private-cleanup-value', output)
        with self.assertRaises(JobError):
            parser.result()

    def test_missing_runtime_apis_and_callback_parameters_stop_before_inference(self):
        class Agent:
            def __init__(self, *, tool_start_callback, tool_complete_callback):
                pass

            def run_conversation(self, prompt):
                pass

        class NoCallbacks(Agent):
            def __init__(self, **kwargs):
                pass

        for missing in ('AIAgent', 'run_conversation', 'run_oneshot', 'apply_terminal_config_to_env',
                        '_cleanup_oneshot_runtime', '_exit_after_oneshot', 'callbacks', 'cleanup_signature', 'local_backend'):
            with self.subTest(missing=missing):
                agent = SimpleNamespace(AIAgent=NoCallbacks if missing == 'callbacks' else Agent)
                oneshot = SimpleNamespace(run_oneshot=Mock())
                cli = SimpleNamespace(_cleanup_oneshot_runtime=Mock(), _exit_after_oneshot=Mock())
                config = SimpleNamespace(apply_terminal_config_to_env=Mock())
                if missing == 'cleanup_signature':
                    cli._cleanup_oneshot_runtime = lambda unexpected: None
                with patch.dict(os.environ, {'TERMINAL_ENV': 'other' if missing == 'local_backend' else 'local'},
                                clear=True):
                    with ExitStack() as stack:
                        for obj in (agent, agent.AIAgent, oneshot, cli, config):
                            if hasattr(obj, missing):
                                stack.enter_context(patch.object(obj, missing, None))
                                break
                        output = io.StringIO()
                        self.assertEqual(run('synthetic task', output, (agent, oneshot, cli, config)), 1)
                oneshot.run_oneshot.assert_not_called()
                if missing != 'cleanup_signature':
                    cli._cleanup_oneshot_runtime.assert_not_called()
                self.assertIn('Hermes execution failed.', output.getvalue())

    def test_executable_help_discovers_modules_but_launch_checks_cleanup(self):
        executable = Path(__file__).resolve().parents[2] / 'deploy' / 'hermes-aee'
        with tempfile.TemporaryDirectory(prefix='hermes-discovery-fixture-') as tmp:
            root = Path(tmp)
            package = root / 'hermes_cli'
            package.mkdir()
            for filename, source in {
                '__init__.py': '',
                'main.py': 'def _exit_after_oneshot(code): raise SystemExit(code)\n',
                'config.py': 'def apply_terminal_config_to_env(*, override): pass\n',
                'oneshot.py': 'def run_oneshot(prompt): open("inference-called", "w").close()\n',
            }.items():
                (package / filename).write_text(source)
            (root / 'run_agent.py').write_text(
                'class AIAgent:\n'
                '    def __init__(self, *, tool_start_callback, tool_complete_callback): pass\n'
                '    def run_conversation(self, prompt): pass\n'
            )
            env = {'HOME': tmp, 'PATH': f'{Path(sys.executable).parent}:/usr/bin:/bin',
                   'PYTHONPATH': tmp}
            help_probe = subprocess.run([str(executable), 'chat', '--help'], cwd='/', env=env,
                                        capture_output=True, text=True, timeout=5)
            self.assertEqual(help_probe.returncode, 0, help_probe.stderr)
            self.assertIn('--query-file', help_probe.stdout)
            launch = subprocess.run(adapter_specs()['hermes'].build_argv(tmp, str(executable), 'autonomous'),
                                    cwd=tmp, env=env, input='synthetic prompt',
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(launch.returncode, 1, launch.stderr)
            self.assertFalse((root / 'inference-called').exists())
            self.assertEqual(launch.stderr, '')
            self.assertEqual(json.loads(launch.stdout)['exit_code'], 1)
            (package / 'main.py').write_text(
                'def _exit_after_oneshot(code): raise SystemExit(code)\n'
                'def _cleanup_oneshot_runtime(): open("cleanup-called", "w").close()\n'
            )
            (package / 'oneshot.py').write_text(
                'def run_oneshot(prompt):\n'
                '    from run_agent import AIAgent\n'
                '    AIAgent().run_conversation(prompt)\n'
                '    return 0\n'
            )
            (root / 'run_agent.py').write_text(
                'class AIAgent:\n'
                '    def __init__(self, *, tool_start_callback, tool_complete_callback):\n'
                '        self.start = tool_start_callback\n'
                '        self.complete = tool_complete_callback\n'
                '    def run_conversation(self, prompt):\n'
                '        self.start("id", "tool", {})\n'
                '        self.complete("id", "tool", {}, "synthetic tool result")\n'
                '        return {"completed": True, "final_response": "verified final"}\n'
            )
            launch = subprocess.run(adapter_specs()['hermes'].build_argv(tmp, str(executable), 'autonomous'),
                                    cwd=tmp, env=env, input='synthetic prompt',
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(launch.returncode, 0, launch.stderr)
            self.assertTrue((root / 'cleanup-called').is_file())
            parser = EventParser('hermes')
            parser.feed(launch.stdout.encode(), eof=True)
            self.assertEqual(parser.result(), 'verified final')
            self.assertEqual(parser.tool_evidence()['tools_completed'], 1)


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


    @patch('aee.mcp_runtime.job_adapters.resolve_adapter')
    @patch('aee.mcp_runtime.job_adapters.run_bounded')
    def test_help_is_flag_discovery_only_and_ignores_lookalikes(self, probe, resolve):
        executable = '/fixture/bin/hermes'
        resolve.return_value = (adapter_specs()['hermes'], executable)
        probe.return_value = SimpleNamespace(returncode=0, stdout=b'--query-file --format --toolsets --yolo-extra')
        self.assertFalse(autonomous_capability('hermes')['autonomous_supported'])
        probe.return_value.stdout = b'--query-file --format=FORMAT --toolsets --yolo'
        capability = autonomous_capability('hermes')
        self.assertTrue(capability['autonomous_supported'])
        self.assertIn('host qualification required', capability['reason'])

    def test_native_parser_requires_exact_result_and_paired_tool_evidence(self):
        start = {'type': 'tool_use', 'tool_id': 'fixture-one'}
        end = {'type': 'tool_result', 'tool_id': 'fixture-one', 'is_error': False}
        final = {'type': 'result', 'exit_code': 0, 'text': 'verified final'}

        def parse(*events):
            parser = EventParser('hermes')
            parser.feed('\n'.join(map(json.dumps, events)).encode(), eof=True)
            return parser.result()

        self.assertEqual(parse(start, end, final), 'verified final')
        alias_start = {'type': 'tool_execution_start', 'id': 'two'}
        alias_end = {'type': 'tool_execution_end', 'id': 'two', 'isError': False}
        self.assertEqual(parse(start, alias_start, end, alias_end, final), 'verified final')
        cases = (
            (start, final), (end, final), (end, start, end, final), (start, start, end, final),
            (start, end, end, final), (start, dict(end, tool_id='wrong'), final),
            (start, dict(end, tool_id=[]), final), (start, dict(end, is_error='false'), final),
            (start, {k: v for k, v in end.items() if k != 'is_error'}, final),
            (alias_start, dict(alias_end, isError=True), start, end, final),
            (start, dict(end, isError=True), final), (start, end, final, start, end),
            (dict(final, exit_code=False), final), (dict(final, exit_code=0.0),),
            (dict(final, exit_code=None),), (dict(final, text=''),), (dict(final, truncated=True),),
        )
        for events in cases:
            with self.subTest(events=events), self.assertRaises(JobError):
                parse(*events)
        with self.assertRaises(JobError):
            EventParser('hermes').feed(b'{"type":"result","exit_code":0,', eof=True)
