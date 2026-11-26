"""Adapt installed Hermes oneshot execution to AEE's stdin/JSONL contract.

No Hermes configuration or installation is changed. Events come from Hermes'
actual agent callbacks and completed run record, never from model-authored JSON.
Only autonomous mode is supported; this adapter claims no filesystem sandbox.
"""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import json
import os
import sys
import threading

VERSION = 'hermes-aee-oneshot 1'


def tool_failed(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return False
    if not isinstance(value, dict):
        return False
    if value.get('error') or value.get('is_error') or value.get('isError'):
        return True
    if value.get('success') is False:
        return True
    for name in ('exit_code', 'returncode'):
        code = value.get(name)
        if code is not None and (type(code) is not int or code != 0):
            return True
    return False


def run(prompt, output, native=None):
    launch_cwd = os.getcwd()
    lock = threading.Lock()
    started, completed = set(), set()
    failed = False
    observed = None

    def emit(event):
        with lock:
            output.write(json.dumps(event, ensure_ascii=False) + '\n')
            output.flush()

    def on_start(tool_id, name, arguments):
        nonlocal failed
        with lock:
            if not isinstance(tool_id, str) or not tool_id or tool_id in started:
                failed = True
            else:
                started.add(tool_id)
        emit({'type': 'tool_use'})

    def on_complete(tool_id, name, arguments, result):
        nonlocal failed
        error = tool_failed(result)
        with lock:
            if tool_id not in started or tool_id in completed:
                error = True
            completed.add(tool_id)
            failed = failed or error
        # Do not forward arguments, raw tool output, or credentials.
        emit({'type': 'tool_result', 'is_error': error})

    original = None
    agent_module = None
    try:
        os.environ['HERMES_YOLO_MODE'] = '1'
        os.environ['HERMES_ACCEPT_HOOKS'] = '1'
        with open(os.devnull, 'w') as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
            if native is None:
                # Native CLI bootstrap loads the existing Hermes profile/env.
                import hermes_cli.main  # noqa: F401
                from hermes_cli.config import apply_terminal_config_to_env
                apply_terminal_config_to_env(override=False)
                if os.environ.get('TERMINAL_ENV', 'local').lower() != 'local':
                    raise RuntimeError('AEE requires the local Hermes terminal backend')
                from hermes_cli import oneshot
                import run_agent as agent_module
            else:
                agent_module, oneshot = native
            # Hermes terminal tools otherwise fall back to the user's home,
            # independently of the launcher's pinned process cwd. Override only
            # this worker's cwd carrier; never change shared Hermes config.
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
            code = oneshot.run_oneshot(prompt)
        valid = (code == 0 and isinstance(observed, dict)
                 and observed.get('completed') is True
                 and not observed.get('failed') and not observed.get('partial')
                 and not failed and started == completed)
        answer = observed.get('final_response') if isinstance(observed, dict) else None
        valid = valid and isinstance(answer, str) and bool(answer.strip())
        emit({'type': 'result', 'exit_code': 0 if valid else 1,
              'text': answer if valid else 'Hermes did not produce a verified successful run.'})
        return 0 if valid else 1
    except Exception as exc:
        # Exception strings can contain provider credentials or request bodies.
        emit({'type': 'result', 'exit_code': 1,
              'text': 'Hermes execution failed (' + type(exc).__name__ + ').'})
        return 1
    finally:
        if original is not None:
            agent_module.AIAgent = original


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
    return run(prompt, sys.stdout)


if __name__ == '__main__':
    code = main()
    # Mirror Hermes oneshot shutdown; avoid native-extension finalizer crashes.
    from hermes_cli.main import _cleanup_oneshot_runtime, _exit_after_oneshot
    _cleanup_oneshot_runtime()
    _exit_after_oneshot(code)
