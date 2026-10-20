"""Bounded pipe transport and whole-process-group cleanup; never shell=True."""
from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass

from .store import JobError, output_limit_context


@dataclass(frozen=True)
class Limits:
    timeout: float = 900
    stdout_bytes: int = 65536
    stderr_bytes: int = 65536
    result_bytes: int = 65536
    summary_chars: int = 4000
    log_chars: int = 2000
    memory_bytes: int = 2 * 1024**3
    cpu_seconds: int = 300
    file_bytes: int = 65536

    def __post_init__(self):
        ranges = {"timeout": (0.01, 900), "stdout_bytes": (1, 1048576),
                  "stderr_bytes": (1, 1048576), "result_bytes": (1, 65536),
                  "summary_chars": (1, 4000), "log_chars": (1, 2000),
                  "memory_bytes": (64 * 1024**2, 2 * 1024**3),
                  "cpu_seconds": (1, 300), "file_bytes": (1, 4 * 1024**2)}
        for name, (low, high) in ranges.items():
            if not low <= getattr(self, name) <= high:
                raise JobError("INVALID_EXECUTION_PROFILE", "Resource limit outside supported bounds")


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    log_tail: bytes


def kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def run_bounded(argv, *, env, stdin=b"", limits=Limits(), cwd=None, on_start=None, cancel_event=None):
    if len(stdin) > 32000:
        raise JobError("INVALID_TASK", "Task input exceeds byte limit")
    deadline = time.monotonic() + limits.timeout
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=env, cwd=cwd,
                               start_new_session=True, close_fds=True)
    selector = selectors.DefaultSelector()
    output = {"stdout": bytearray(), "stderr": bytearray()}
    caps = {"stdout": limits.stdout_bytes, "stderr": limits.stderr_bytes}
    tail = bytearray()
    offset = 0
    try:
        if on_start:
            on_start(process.pid)
        for stream, name in [(process.stdout, "stdout"), (process.stderr, "stderr")]:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        os.set_blocking(process.stdin.fileno(), False)
        if stdin:
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        else:
            process.stdin.close()
        while selector.get_map():
            if cancel_event is not None and cancel_event.is_set():
                raise JobError('EXECUTION_CANCELLED', 'Executor cancelled by its local owner')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise JobError("EXECUTION_TIMEOUT", "Executor exceeded runtime limit")
            for key, _ in selector.select(min(remaining, 0.05)):
                stream = key.fileobj
                name = key.data
                if name == "stdin":
                    try:
                        count = os.write(stream.fileno(), stdin[offset:offset + 4096])
                        offset += count
                    except BrokenPipeError:
                        offset = len(stdin)
                    if offset == len(stdin):
                        selector.unregister(stream)
                        stream.close()
                    continue
                chunk = os.read(stream.fileno(), 8192)
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                if len(output[name]) + len(chunk) > caps[name]:
                    # K01: each cumulative raw stream cap is independent; the
                    # context is the only stream-specific discriminator.
                    raise JobError("OUTPUT_LIMIT_EXCEEDED", "Executor output exceeded byte limit",
                                   context=output_limit_context(
                                       'executor_capture_' + name, 'raw_stream', 'raw_bytes',
                                       limit=caps[name],
                                       observed=len(output[name]) + len(chunk), measured=True,
                                       stream=name))
                output[name].extend(chunk)
                tail.extend(chunk)
                del tail[:-limits.log_chars * 4]
        remaining = deadline - time.monotonic()
        if cancel_event is not None and cancel_event.is_set():
            raise JobError('EXECUTION_CANCELLED', 'Executor cancelled by its local owner')
        if remaining <= 0:
            raise JobError("EXECUTION_TIMEOUT", "Executor exceeded runtime limit")
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise JobError('EXECUTION_CANCELLED', 'Executor cancelled by its local owner')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise JobError('EXECUTION_TIMEOUT', 'Executor exceeded runtime limit')
            try:
                code = process.wait(timeout=min(remaining, 0.05))
                break
            except subprocess.TimeoutExpired:
                continue
        if code in {-signal.SIGXFSZ, 128 + signal.SIGXFSZ}:
            # The OS-observed attempt size is not recoverable here; the limit is
            # the caller-configured value (resource policy may have lowered the
            # effective prlimit), so nothing measured is fabricated.
            raise JobError("OUTPUT_LIMIT_EXCEEDED", "Executor file size limit exceeded",
                           context=output_limit_context(
                               'executor_file_size_limit', 'os_write', 'raw_bytes',
                               limit=limits.file_bytes))
        if code in {-signal.SIGXCPU, 128 + signal.SIGXCPU}:
            raise JobError("RESOURCE_LIMIT_EXCEEDED", "Executor CPU limit exceeded")
        return ProcessResult(code, bytes(output["stdout"]), bytes(output["stderr"]), bytes(tail))
    finally:
        # Also terminate descendants retaining pipes, even after the leader exits.
        kill_group(process)
        selector.close()
        for stream in [process.stdin, process.stdout, process.stderr]:
            if not stream.closed:
                stream.close()


def run_streamed(argv, *, env, stdin, budget, spool, parser, cwd=None,
                 on_start=None, cancel_event=None, redact=lambda x: x):
    """Generic-adapter runner. Legacy approved broker contract stays explicit."""
    if len(stdin) > 32000:
        raise JobError('INVALID_TASK', 'Task input exceeds byte limit')
    deadline = time.monotonic() + budget.wall_timeout_seconds
    last_progress = time.monotonic()
    process = None
    selector = selectors.DefaultSelector()
    offset = 0
    code = None
    error = None
    final = None
    try:
        import sys
        from pathlib import Path
        guarded = [sys.executable, str(Path(__file__).with_name('process_guard.py')),
                   str(os.getpid()), *argv]
        process = subprocess.Popen(guarded, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=env, cwd=cwd,
                                   start_new_session=True, close_fds=True)
        if on_start:
            on_start(process.pid)
        for stream, name in ((process.stdout, 'stdout'), (process.stderr, 'stderr')):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        os.set_blocking(process.stdin.fileno(), False)
        if stdin:
            selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
        else:
            process.stdin.close()
        while selector.get_map() or process.poll() is None:
            now = time.monotonic()
            if cancel_event is not None and cancel_event.is_set():
                raise JobError('EXECUTION_CANCELLED', 'Executor cancelled by its local owner')
            if now >= deadline:
                raise JobError('EXECUTION_TIMEOUT', 'Executor exceeded wall budget')
            if budget.idle_timeout_seconds is not None and now - last_progress >= budget.idle_timeout_seconds:
                raise JobError('EXECUTION_IDLE_TIMEOUT', 'Executor exceeded progress idle budget')
            for key, _ in selector.select(min(deadline - now, .05)):
                stream, name = key.fileobj, key.data
                if name == 'stdin':
                    try:
                        offset += os.write(stream.fileno(), stdin[offset:offset + 4096])
                    except BrokenPipeError:
                        offset = len(stdin)
                    if offset == len(stdin):
                        selector.unregister(stream)
                        stream.close()
                    continue
                chunk = os.read(stream.fileno(), 8192)
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                spool.append(name, chunk)
                if name == 'stdout':
                    previous = parser.progress
                    parser.feed(chunk)
                    if parser.progress > previous:
                        last_progress = time.monotonic()
        # Observation order defines cancellation vs successful-exit race.
        if cancel_event is not None and cancel_event.is_set():
            raise JobError('EXECUTION_CANCELLED', 'Executor cancelled by its local owner')
        code = process.wait(timeout=5)
        # Deterministic precedence at EOF (contract v2): the process-exit
        # mapping runs before the parser verdict, matching the buffered path
        # (which checks returncode before parsing stdout). Parser fatals only
        # decide a job when the process exited cleanly.
        if code in {-signal.SIGXCPU, 128 + signal.SIGXCPU, -signal.SIGXFSZ, 128 + signal.SIGXFSZ}:
            raise JobError('RESOURCE_LIMIT_EXCEEDED', 'Executor exhausted inherited OS resource budget', exit_code=code)
        if code != 0:
            raise JobError('EXECUTION_FAILED', f'Executor exited {code}', exit_code=code)
        parser.feed(b'', eof=True)
        if parser.error is not None:
            raise parser.error
        final = parser.result()
    except JobError as exc:
        error = exc
    except OSError:
        error = JobError('SPOOL_IO_ERROR', 'Stream transport or spool storage unavailable')
    finally:
        if process is not None:
            # Wait/kill failures propagate: never report safe cleanup without proof.
            kill_group(process)
            if code is None:
                code = process.returncode
            for stream in (process.stdin, process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()
        selector.close()
    try:
        metadata = spool.finish(code=error.code if error else None, exit_code=code, redact=redact)
    except OSError:
        error = JobError('SPOOL_IO_ERROR', 'Stream metadata durability failed')
        metadata = spool.metadata('failed', code=error.code, exit_code=code, redact=redact)
    except JobError as exc:
        error = exc
        metadata = spool.metadata('failed', code=error.code, exit_code=code, redact=redact)
    if error:
        error.streaming = metadata
        error.exit_code = code
        raise error
    return StreamedResult(code, final, metadata)


@dataclass(frozen=True)
class StreamedResult:
    returncode: int
    final: str | None
    streaming: dict
