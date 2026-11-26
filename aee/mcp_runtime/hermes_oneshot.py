"""Adapt installed Hermes oneshot execution to AEE's stdin/JSONL contract.

No Hermes configuration or installation is changed. Events come from Hermes'
actual agent callbacks and completed run record, never from model-authored JSON.
Only autonomous mode is supported; this adapter claims no filesystem sandbox.
"""
import argparse
from contextlib import redirect_stderr, redirect_stdout
from inspect import Parameter, signature
import json
import os
import re
import sys
import threading

VERSION = 'hermes-aee-oneshot 1'

_READ_FILE_NO_PROGRESS_WARNING = re.compile(
    r'(?<=\})\n\n\[Tool loop warning: idempotent_no_progress_warning; count=([1-9][0-9]*); '
    r'read_file returned the same result \1 times\. Use the result already provided '
    r'or change the query instead of repeating it unchanged\.\]\Z'
)


def _json_fields(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError('Duplicate tool result field')
    return fields


def _invalid_json_constant(value):
    raise ValueError('Invalid tool result constant')


def _read_file_page(value):
    required = {'content', 'total_lines', 'file_size', 'truncated', 'hint', 'is_binary', 'is_image'}
    annotations = value.keys() & {'_hint', '_warning'}
    if value.keys() != required | annotations or any(
        not isinstance(value[key], str) or not value[key].strip() for key in annotations
    ):
        return False

    content, hint = value['content'], value['hint']
    total_lines, file_size = value['total_lines'], value['file_size']
    if (not isinstance(content, str) or not isinstance(hint, str)
            or type(total_lines) is not int or type(file_size) is not int
            or file_size < total_lines or value['is_binary'] is not False or value['is_image'] is not False):
        return False

    match = re.fullmatch(
        r'Use offset=([1-9][0-9]*) to continue reading \(showing ([1-9][0-9]*)-([1-9][0-9]*) of ([1-9][0-9]*) lines\)',
        hint,
    )
    if match is None:
        return False
    try:
        next_offset, start, end, hinted_total = map(int, match.groups())
    except ValueError:
        return False
    if not start <= end < total_lines == hinted_total or next_offset != end + 1:
        return False

    lines = content.split('\n')
    # Hermes numbers the empty segment after the trailing newline from sed.
    if lines[-1] == f'{next_offset}|':
        lines.pop()
    return len(lines) == end - start + 1 and _complete_numbered_lines(lines, start)


def _complete_numbered_lines(lines, start):
    return all(
        line.startswith(f'{number}|')
        and not line.endswith('... [truncated]')
        and not line.partition('|')[2].startswith('... [OUTPUT TRUNCATED - ')
        for number, line in enumerate(lines, start)
    )


def _read_file_offset(arguments):
    if not isinstance(arguments, dict):
        return None
    try:
        return max(1, int(arguments.get('offset', 1)))
    except (TypeError, ValueError):
        return 1
    except OverflowError:
        return None


def _final_read_file_page(value, offset):
    if offset is None:
        return False
    required = {'content', 'total_lines', 'file_size', 'truncated'}
    content = value.get('content')
    total_lines, file_size = value.get('total_lines'), value.get('file_size')
    if (not isinstance(content, str) or not content or type(total_lines) is not int
            or type(file_size) is not int or total_lines < 0):
        return False

    extracted = value.get('extracted_document') is True
    if extracted:
        if value.keys() != required | {'extracted_document'} or file_size <= 0:
            return False
    else:
        warning = value.get('_warning', '')
        if (value.keys() - {'_warning'} != required | {'is_binary', 'is_image'}
                or value['is_binary'] is not False or value['is_image'] is not False
                or file_size < total_lines
                or ('_warning' in value and (not isinstance(warning, str) or not warning.strip()))):
            return False

    lines = content.split('\n')
    end = total_lines
    # Text reads count newlines with wc -l and number every segment from sed.
    # The final segment can be an unterminated line or an empty trailing segment.
    if not extracted and lines[-1].startswith(f'{total_lines + 1}|'):
        end += 1
    return len(lines) == end - offset + 1 and _complete_numbered_lines(lines, offset)


def tool_failed(value, *, tool_name=None, read_offset=None):
    has_warning = False
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or stripped == 'null' or stripped.startswith(
            ('Error executing tool ', '[Tool execution cancelled')
        ):
            return True
        if not stripped.startswith(('{', '[')):
            return tool_name == 'read_file' and (
                stripped.lower().startswith(
                    'hermes note: this result is byte-identical to the read_file result earlier this turn.'
                )
                or '\n\n[Tool loop warning: ' in stripped
                or '\n\n[Tool loop hard stop: ' in stripped
            )
        # Match against the original text so trailing whitespace cannot hide a malformed suffix.
        warning = _READ_FILE_NO_PROGRESS_WARNING.search(value) if tool_name == 'read_file' else None
        has_warning = warning is not None
        payload = value[:warning.start()] if warning is not None else stripped
        try:
            value = json.loads(payload, object_pairs_hook=_json_fields,
                               parse_constant=_invalid_json_constant)
        except (ValueError, RecursionError):
            return True
    if not isinstance(value, dict) or not value or value.get('error'):
        return True
    if any(value.get(flag, False) is not False
           for flag in ('is_error', 'isError', 'failed', 'partial', 'interrupted')):
        return True
    if value.get('success', True) is not True or value.get('completed', True) is not True:
        return True
    if any(key in value and (type(value[key]) is not int or value[key] != 0)
           for key in ('exit_code', 'returncode')):
        return True
    if (tool_name == 'read_file' and value.get('status') == 'unchanged'
            and value.get('dedup') is True and value.get('content_returned') is False):
        return True
    truncated = value.get('truncated', False)
    if has_warning and truncated is False:
        return not _final_read_file_page(value, read_offset)
    return truncated is not False and not (
        truncated is True and tool_name == 'read_file' and _read_file_page(value)
    )


def _preflight(agent, oneshot, cli, config):
    agent_type = getattr(agent, 'AIAgent', None)
    if not isinstance(agent_type, type):
        raise RuntimeError('Incompatible Hermes agent API')
    calls = (
        (getattr(agent_type, 'run_conversation', None), (None, ''), {}),
        (getattr(oneshot, 'run_oneshot', None), ('',), {}),
        (getattr(config, 'apply_terminal_config_to_env', None), (), {'override': False}),
        (getattr(cli, '_cleanup_oneshot_runtime', None), (), {}),
        (getattr(cli, '_exit_after_oneshot', None), (0,), {}),
    )
    for function, args, kwargs in calls:
        if not callable(function):
            raise RuntimeError('Incompatible Hermes runtime API')
        signature(function).bind(*args, **kwargs)
    parameters = signature(agent_type.__init__).parameters
    for name in ('tool_start_callback', 'tool_complete_callback'):
        if (name not in parameters or parameters[name].kind not in
                {Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY}):
            raise RuntimeError('Incompatible Hermes tool callback API')


def run(prompt, output, native=None, *, exit_process=False):
    launch_cwd = os.getcwd()
    lock = threading.Lock()
    pending = {}
    seen = set()
    failed = False
    observed = None

    def emit(event):
        output.write(json.dumps(event, ensure_ascii=False) + '\n')
        output.flush()

    def on_start(tool_id=None, name=None, arguments=None, *extra, **fields):
        nonlocal failed
        with lock:
            if (extra or fields or type(tool_id) is not str
                    or not tool_id.strip() or len(tool_id) > 256 or tool_id in seen
                    or type(name) is not str or not name.strip()):
                failed = True
                return
            seen.add(tool_id)
            event_id = str(len(seen))
            read_offset = _read_file_offset(arguments) if name == 'read_file' else None
            pending[tool_id] = event_id, name, read_offset
            emit({'type': 'tool_use', 'tool_id': event_id})

    def on_complete(tool_id=None, name=None, arguments=None, result=None, *extra, **fields):
        nonlocal failed
        with lock:
            if (extra or fields or type(tool_id) is not str
                    or not tool_id.strip() or tool_id not in pending):
                failed = True
                return
            event_id, started_name, read_offset = pending.pop(tool_id)
            error = (type(name) is not str or name != started_name
                     or tool_failed(result, tool_name=started_name, read_offset=read_offset))
            failed = failed or error
            # Stream-local IDs pair events without disclosing provider IDs.
            emit({'type': 'tool_result', 'tool_id': event_id, 'is_error': error})

    original = None
    exit_after_oneshot = None
    try:
        os.environ['HERMES_YOLO_MODE'] = '1'
        os.environ['HERMES_ACCEPT_HOOKS'] = '1'
        with open(os.devnull, 'w') as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
            if native is None:
                # Native CLI bootstrap loads the existing Hermes profile/env.
                from hermes_cli import main as cli, config, oneshot
                import run_agent as agent_module
            else:
                agent_module, oneshot, cli, config = native
            _preflight(agent_module, oneshot, cli, config)
            exit_after_oneshot = cli._exit_after_oneshot
            config.apply_terminal_config_to_env(override=False)
            if os.environ.get('TERMINAL_ENV', 'local').lower() != 'local':
                raise RuntimeError('AEE requires the local Hermes terminal backend')
            # Hermes terminal tools otherwise fall back to the user's home.
            os.environ['TERMINAL_CWD'] = launch_cwd
            original = agent_module.AIAgent

            class ObservedAgent(original):
                def __init__(self, *args, **kwargs):
                    kwargs['tool_start_callback'] = on_start
                    kwargs['tool_complete_callback'] = on_complete
                    super().__init__(*args, **kwargs)

                def run_conversation(self, *args, **kwargs):
                    nonlocal observed
                    observed = super().run_conversation(*args, **kwargs)
                    return observed

            agent_module.AIAgent = ObservedAgent
            try:
                code = oneshot.run_oneshot(prompt)
            finally:
                cli._cleanup_oneshot_runtime()
        with lock:
            answer = observed.get('final_response') if isinstance(observed, dict) else None
            valid = (type(code) is int and code == 0 and isinstance(observed, dict)
                     and observed.get('completed') is True
                     and all(observed.get(flag, False) is False
                             for flag in ('failed', 'partial', 'interrupted', 'truncated'))
                     and not failed and not pending and isinstance(answer, str) and bool(answer.strip()))
            code = 0 if valid else 1
            emit({'type': 'result', 'exit_code': code,
                  'text': answer if valid else 'Hermes did not produce a verified successful run.'})
    except (Exception, SystemExit):
        with lock:
            emit({'type': 'result', 'exit_code': 1, 'text': 'Hermes execution failed.'})
        code = 1
    finally:
        if original is not None:
            agent_module.AIAgent = original
    if exit_process and exit_after_oneshot is not None:
        # Mirror native shutdown to avoid native-extension finalizer crashes.
        exit_after_oneshot(code)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=VERSION)
    commands = parser.add_subparsers(dest='command', required=True)
    chat = commands.add_parser('chat', help='Hermes autonomous oneshot with structured events')
    chat.add_argument('--yolo', action='store_true', required=True)
    chat.add_argument('--query-file', choices=['-'], required=True, help='Read task from stdin only')
    chat.add_argument('--format', choices=['stream-json'], required=True)
    parser.parse_args(argv)
    prompt = sys.stdin.read(1048577)
    if not prompt.strip() or len(prompt) > 1048576:
        parser.error('Task must be nonempty and at most 1 MiB')
    return run(prompt, sys.stdout, exit_process=True)


if __name__ == '__main__':
    raise SystemExit(main())
