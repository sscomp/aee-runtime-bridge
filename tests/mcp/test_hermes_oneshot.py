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


READ_FILE_NO_PROGRESS_WARNING = (
    '\n\n[Tool loop warning: idempotent_no_progress_warning; count=3; '
    'read_file returned the same result 3 times. Use the result already provided '
    'or change the query instead of repeating it unchanged.]'
)


def read_file_page(**fields):
    return {
        'content': '2|alpha\n3|beta',
        'total_lines': 5,
        'file_size': 30,
        'truncated': True,
        'hint': 'Use offset=4 to continue reading (showing 2-3 of 5 lines)',
        'is_binary': False,
        'is_image': False,
        **fields,
    }


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

    def read_page(self, result, *, start_name='read_file', complete_name='read_file'):
        args = {'path': 'synthetic.txt', 'offset': 2, 'limit': 2}
        return self.execute(callback_events=[
            ('start', ('page', start_name, args)),
            ('complete', ('page', complete_name, args, json.dumps(result))),
            ('start', ('later', 'terminal', {})),
            ('complete', ('later', 'terminal', {}, 'ordinary tool output')),
        ])

    def test_read_file_pages_have_complete_numbered_content(self):
        for page in (read_file_page(), read_file_page(content='2|alpha\n3|\n4|')):
            with self.subTest(page=page):
                code, parser, output = self.read_page(page)
                self.assertEqual(code, 0)
                self.assertEqual(parser.result(), 'verified final')
                self.assertEqual(parser.tool_evidence()['tools_completed'], 2)
                for private in ('alpha', 'read_file', 'synthetic.txt', 'Use offset='):
                    self.assertNotIn(private, output)

    def test_read_file_page_lookalikes_fail(self):
        page = read_file_page()
        for bad in (
            {key: value for key, value in page.items() if key != 'hint'},
            read_file_page(content=''), read_file_page(content=[]),
            read_file_page(content='2|alpha'), read_file_page(content='2|alpha\n4|beta'),
            read_file_page(content='2|alpha\n3|beta\n4|extra'),
            read_file_page(content='2|alpha\n3|beta... [truncated]'),
            read_file_page(content='2|alpha\n3|... [OUTPUT TRUNCATED - 12 chars omitted out of 30 total] ...'),
            read_file_page(hint='Use offset=5 to continue reading (showing 2-3 of 5 lines)'),
            read_file_page(total_lines='5'), read_file_page(total_lines=3), read_file_page(file_size=True),
            read_file_page(file_size=0), read_file_page(is_binary=0), read_file_page(is_image=True),
            read_file_page(truncated='true'), read_file_page(truncated_by='bytes', next_offset=4),
            read_file_page(_warning=[]),
        ):
            with self.subTest(result=bad):
                code, parser, _ = self.read_page(bad)
                self.assertEqual(code, 1)
                with self.assertRaises(JobError):
                    parser.result()

    def test_read_file_page_requires_matching_dispatched_names(self):
        for start, end in (('terminal', 'terminal'), ('terminal', 'read_file'),
                           ('read_file', 'terminal'), (None, 'read_file'), ('read_file', [])):
            with self.subTest(start=start, end=end):
                code, parser, _ = self.read_page(read_file_page(), start_name=start, complete_name=end)
                self.assertEqual(code, 1)
                with self.assertRaises(JobError):
                    parser.result()

    def test_read_file_page_does_not_override_explicit_failure(self):
        for flags in ({'error': 'denied'}, {'is_error': True}, {'failed': True},
                      {'is_error': False, 'isError': True}, {'partial': True}, {'interrupted': True},
                      {'success': False}, {'completed': False}, {'exit_code': 1}):
            with self.subTest(flags=flags):
                code, parser, _ = self.read_page(read_file_page(**flags))
                self.assertEqual(code, 1)
                with self.assertRaises(JobError):
                    parser.result()

    def execute_raw_read_file_result(self, result):
        args = {'path': 'synthetic.txt', 'offset': 2, 'limit': 2}
        return self.execute(callback_events=[
            ('start', ('page', 'read_file', args)),
            ('complete', ('page', 'read_file', args, result)),
        ])

    def test_read_file_complete_page_accepts_exact_no_progress_warning_suffix(self):
        result = json.dumps(read_file_page()) + READ_FILE_NO_PROGRESS_WARNING
        code, parser, _ = self.execute_raw_read_file_result(result)
        self.assertEqual(code, 0)
        self.assertEqual(parser.result(), 'verified final')
        self.assertEqual(parser.tool_evidence()['tools_completed'], 1)

    def test_read_file_final_text_page_accepts_exact_no_progress_warning_suffix(self):
        page = {
            'content': '2|alpha\n3|beta\n4|',
            'total_lines': 3,
            'file_size': 20,
            'truncated': False,
            'is_binary': False,
            'is_image': False,
        }
        code, parser, _ = self.execute_raw_read_file_result(json.dumps(page) + READ_FILE_NO_PROGRESS_WARNING)
        self.assertEqual(code, 0)
        self.assertEqual(parser.result(), 'verified final')
        self.assertEqual(parser.tool_evidence()['tools_completed'], 1)

    def test_read_file_final_extracted_document_accepts_exact_no_progress_warning_suffix(self):
        page = {
            'content': '2|alpha\n3|beta',
            'total_lines': 3,
            'file_size': 1234,
            'truncated': False,
            'extracted_document': True,
        }
        code, parser, _ = self.execute_raw_read_file_result(json.dumps(page) + READ_FILE_NO_PROGRESS_WARNING)
        self.assertEqual(code, 0)
        self.assertEqual(parser.result(), 'verified final')
        self.assertEqual(parser.tool_evidence()['tools_completed'], 1)

    def test_read_file_final_text_page_with_warning_rejects_missing_leading_line(self):
        page = {
            'content': '3|beta\n4|',
            'total_lines': 3,
            'file_size': 20,
            'truncated': False,
            'is_binary': False,
            'is_image': False,
        }
        code, parser, _ = self.execute_raw_read_file_result(json.dumps(page) + READ_FILE_NO_PROGRESS_WARNING)
        self.assertEqual(code, 1)
        with self.assertRaises(JobError) as caught:
            parser.result()
        self.assertEqual(caught.exception.code, 'REQUIRED_TOOL_FAILED')

    def test_read_file_final_extracted_document_with_warning_rejects_missing_leading_line(self):
        page = {
            'content': '3|beta',
            'total_lines': 3,
            'file_size': 1234,
            'truncated': False,
            'extracted_document': True,
        }
        code, parser, _ = self.execute_raw_read_file_result(json.dumps(page) + READ_FILE_NO_PROGRESS_WARNING)
        self.assertEqual(code, 1)
        with self.assertRaises(JobError) as caught:
            parser.result()
        self.assertEqual(caught.exception.code, 'REQUIRED_TOOL_FAILED')

    def test_read_file_untruncated_dictionary_with_no_progress_warning_remains_failure(self):
        result = json.dumps({'content': 'ordinary tool output', 'truncated': False}) + READ_FILE_NO_PROGRESS_WARNING
        code, parser, _ = self.execute_raw_read_file_result(result)
        self.assertEqual(code, 1)
        with self.assertRaises(JobError):
            parser.result()

    def test_read_file_final_page_with_incomplete_numbered_content_and_warning_remains_failure(self):
        page = {
            'content': '2|alpha\nbeta\n4|',
            'total_lines': 3,
            'file_size': 20,
            'truncated': False,
            'is_binary': False,
            'is_image': False,
        }
        code, parser, _ = self.execute_raw_read_file_result(json.dumps(page) + READ_FILE_NO_PROGRESS_WARNING)
        self.assertEqual(code, 1)
        with self.assertRaises(JobError):
            parser.result()

    def test_read_file_json_dedup_stub_requires_prior_page_evidence(self):
        result = json.dumps({
            'status': 'unchanged',
            'message': (
                'File unchanged since last read. The content from the earlier read_file result '
                'in this conversation is still current — refer to that instead of re-reading.'
            ),
            'path': 'synthetic.txt',
            'dedup': True,
            'content_returned': False,
        })
        code, parser, _ = self.execute_raw_read_file_result(result)
        self.assertEqual(code, 1)
        with self.assertRaises(JobError):
            parser.result()

    def test_read_file_plaintext_byte_identical_reference_requires_prior_page_evidence(self):
        note = (
            'hermes note: this result is byte-identical to the read_file result earlier this turn. '
            'Refer to that result; it has not changed. Args: {"limit":2,"offset":2,"path":"synthetic.txt"}'
        )
        for result in (f'[{note}]', note):
            with self.subTest(result=result):
                code, parser, _ = self.execute_raw_read_file_result(result)
                self.assertEqual(code, 1)
                with self.assertRaises(JobError):
                    parser.result()

    def test_read_file_explicit_error_with_no_progress_warning_suffix_remains_failure(self):
        result = json.dumps({'error': 'Permission denied'}) + READ_FILE_NO_PROGRESS_WARNING
        code, parser, _ = self.execute_raw_read_file_result(result)
        self.assertEqual(code, 1)
        with self.assertRaises(JobError):
            parser.result()

    def test_read_file_hard_stop_with_complete_page_remains_failure(self):
        result = json.dumps(read_file_page()) + (
            '\n\n[Tool loop hard stop: identical_call_streak_halt; count=5; '
            'Stopped read_file: the same call with identical arguments returned the same result '
            '5 times in a row. Stop repeating it unchanged; use the result already provided or change strategy.]'
        )
        code, parser, _ = self.execute_raw_read_file_result(result)
        self.assertEqual(code, 1)
        with self.assertRaises(JobError):
            parser.result()

    def test_read_file_cancellation_remains_failure(self):
        result = '[Tool execution cancelled — read_file was skipped due to user interrupt]'
        code, parser, _ = self.execute_raw_read_file_result(result)
        self.assertEqual(code, 1)
        with self.assertRaises(JobError):
            parser.result()

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

    def test_native_tool_completion_outcome_markers_are_sticky(self):
        start = {'type': 'tool_use', 'tool_id': 'first'}
        done = {'type': 'tool_result', 'tool_id': 'first', 'is_error': False}
        later_start = {'type': 'tool_execution_start', 'id': 'later'}
        later_done = {'type': 'tool_execution_end', 'id': 'later', 'isError': False}
        final = {'type': 'result', 'exit_code': 0, 'text': 'verified final'}
        flags = ('truncated', 'partial', 'failed', 'interrupted')

        def parse(completion):
            parser = EventParser('hermes')
            events = (start, completion, later_start, later_done, final)
            parser.feed('\n'.join(map(json.dumps, events)).encode(), eof=True)
            return parser

        clean = parse(dict(done, isError=False, **dict.fromkeys(flags, False)))
        self.assertEqual(clean.result(), 'verified final')
        self.assertEqual(clean.tool_evidence()['tools_failed'], 0)
        for flag in flags:
            for value in (True, 'false', 0, None, [], {}):
                with self.subTest(flag=flag, value=value):
                    parser = parse(dict(done, **{flag: value}))
                    with self.assertRaises(JobError) as caught:
                        parser.result()
                    explicit_failure = flag == 'failed' and value is True
                    self.assertEqual(caught.exception.code,
                                     'REQUIRED_TOOL_FAILED' if explicit_failure else 'TOOL_EVIDENCE_INCOMPLETE')
                    self.assertEqual(parser.tool_evidence()['tools_failed'], int(explicit_failure))
        with self.assertRaises(JobError) as caught:
            parse(dict(done, **read_file_page(), name='read_file')).result()
        self.assertEqual(caught.exception.code, 'TOOL_EVIDENCE_INCOMPLETE')

        duplicate = b'{"type":"tool_result","tool_id":"first","is_error":false,"truncated":true,"truncated":false}'
        parser = EventParser('hermes')
        with self.assertRaises(JobError):
            parser.feed(json.dumps(start).encode() + b'\n' + duplicate, eof=True)
