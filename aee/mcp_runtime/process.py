"""Bounded pipe transport and whole-process-group cleanup; never shell=True."""
from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass

from .store import JobError


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
                    raise JobError("OUTPUT_LIMIT_EXCEEDED", "Executor output exceeded byte limit")
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
            raise JobError("OUTPUT_LIMIT_EXCEEDED", "Executor file size limit exceeded")
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
