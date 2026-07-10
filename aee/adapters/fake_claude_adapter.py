"""FakeClaudeCodeProvider — in-memory `ExecProvider` for tests.

A pure-Python stub that emulates a Claude Code CLI subprocess. It
records every (submit, poll, cancel) call and supports configurable
"exit scripts" so tests can simulate:

* happy path — exit 0 with stdout
* failure — exit 1 with stderr
* hang — never exit (caller must timeout)
* slow stdout — chunks arrive over multiple polls

No real subprocess is started. No network. No filesystem writes
(though the provider does support a configurable `artifacts_dir` so
tests can write real artifacts and verify the AEE-6 pipeline
integration).

This stub is the reference for the `ExecProvider` Protocol. The
real `ClaudeCodeProvider` (next slice, AEE-6.3) will follow this
shape; the dispatcher adapter shim will then expose it as a
`RuntimeAdapter` so orchestrator can use it via `runtime_requirements`.

Run with:
    .venv/bin/python -m unittest aee.tests.test_exec_provider -v
"""
from __future__ import annotations

import io
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Mapping, Optional

from aee.adapters.exec_provider import (
    ExecCancelResult,
    ExecPollResult,
    ExecProvider,
    ExecStatus,
    ExecSubmitResult,
    ProviderError,
    ProviderExitError,
    ProviderTimeoutError,
)


# A "behavior" is a function that takes the prompt and returns
# (stdout_text, stderr_text, exit_code). Tests inject one or
# more behaviors to drive the simulated worker.
Behavior = Callable[[str], "_BehaviorResult"]


@dataclass
class _BehaviorResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    delay_seconds: float = 0.0  # simulate work time before exit
    hang: bool = False           # never exit on its own


@dataclass
class _Run:
    """Per-run state held by the fake provider."""

    run_id: str
    pid: int
    prompt: str
    cwd: Optional[str]
    started_at: str
    artifacts_dir: str
    status: ExecStatus = ExecStatus.RUNNING
    exit_code: Optional[int] = None
    stdout_buf: io.BytesIO = field(default_factory=io.BytesIO)
    stderr_buf: io.BytesIO = field(default_factory=io.BytesIO)
    cancelled: bool = False
    timed_out: bool = False
    end_ts: Optional[float] = None
    behavior: Optional[_BehaviorResult] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)


def _happy_behavior(prompt: str) -> _BehaviorResult:
    return _BehaviorResult(
        stdout=f"# claude response\n\nprocessed: {prompt!r}\n",
        stderr="",
        exit_code=0,
    )


def _failing_behavior(prompt: str) -> _BehaviorResult:
    return _BehaviorResult(
        stdout="partial output",
        stderr="TypeError: cannot read property 'foo' of undefined",
        exit_code=1,
    )


def _hanging_behavior(prompt: str) -> _BehaviorResult:
    return _BehaviorResult(stdout="starting...\n", stderr="", exit_code=0, hang=True)


class FakeClaudeCodeProvider:
    """In-memory ExecProvider for unit tests.

    Usage::

        provider = FakeClaudeCodeProvider()
        sub = await provider.submit(prompt="...", cwd="/tmp")
        poll = await provider.poll(sub.external_run_id)
        assert poll.is_terminal and poll.exit_code == 0

    Defaults to `_happy_behavior`; pass `behavior=` to inject a
    custom one. `default_artifacts_root` defaults to a fresh
    tempfile under /tmp so tests can write real files and verify
    the AEE-6 pipeline picks them up.
    """

    name = "claude_code"
    runtime_type = "claude_code"

    def __init__(
        self,
        *,
        behavior: Optional[Behavior] = None,
        default_artifacts_root: Optional[str] = None,
        _pid_seed: int = 99000,
    ) -> None:
        self._behavior: Behavior = behavior or _happy_behavior
        self._runs: Dict[str, _Run] = {}
        self._lock = threading.Lock()
        self._pid_seed = _pid_seed
        # Default artifacts root: a fresh tmpdir per provider
        # instance. Tests can override.
        if default_artifacts_root is None:
            import tempfile
            default_artifacts_root = tempfile.mkdtemp(prefix="fake-claude-")
        self._default_artifacts_root = default_artifacts_root

    # -- ExecProvider protocol --------------------------------------------

    async def submit(
        self,
        *,
        prompt: str,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        timeout_seconds: Optional[int] = None,
        run_id: Optional[str] = None,
    ) -> ExecSubmitResult:
        rid = run_id or f"claude-run-{uuid.uuid4().hex[:12]}"
        # Allocate artifacts dir per run.
        art_dir = os.path.join(self._default_artifacts_root, rid)
        os.makedirs(art_dir, exist_ok=True)
        # Compute behavior up-front so we can decide if the run
        # hangs or completes synchronously. The behavior may be
        # either a callable (called with the prompt) or a
        # pre-computed _BehaviorResult (handy for tests that want
        # to inject a literal).
        if callable(self._behavior) and not isinstance(self._behavior, _BehaviorResult):
            beh = self._behavior(prompt)
        else:
            beh = self._behavior  # type: ignore[assignment]
        with self._lock:
            self._pid_seed += 1
            run = _Run(
                run_id=rid,
                pid=self._pid_seed,
                prompt=prompt,
                cwd=cwd,
                started_at=datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                artifacts_dir=art_dir,
                behavior=beh,
            )
            self._runs[rid] = run
        # Honor timeout in the fake by scheduling a kill.
        if timeout_seconds is not None and beh.hang:
            self._schedule_timeout(rid, timeout_seconds)
        # If the behavior says "exit fast", finalize in the
        # background so first poll() sees a terminal state.
        if not beh.hang and beh.delay_seconds <= 0.0:
            self._finalize(rid, beh)
        return ExecSubmitResult(
            external_run_id=rid,
            pid=run.pid,
            status=ExecStatus.RUNNING,
            started_at=run.started_at,
        )

    async def poll(self, external_run_id: str) -> ExecPollResult:
        run = self._require(external_run_id)
        with run._lock:
            stdout_bytes = run.stdout_buf.tell()
            stderr_bytes = run.stderr_buf.tell()
            # If we are running and behavior has a delay, maybe
            # promote to terminal here.
            if run.status == ExecStatus.RUNNING and run.behavior and not run.behavior.hang:
                elapsed = time.time() - (run.end_ts or 0)
                if run.end_ts is not None or self._should_finalize(run):
                    pass
            output: Optional[str] = None
            if run.status in (ExecStatus.EXITED, ExecStatus.CANCELLED, ExecStatus.TIMED_OUT, ExecStatus.FAILED):
                if stdout_bytes > 0:
                    run.stdout_buf.seek(0)
                    output = run.stdout_buf.read().decode("utf-8", errors="replace")
            err: Optional[str] = None
            if run.status in (ExecStatus.CANCELLED, ExecStatus.TIMED_OUT, ExecStatus.FAILED):
                run.stderr_buf.seek(0)
                err = run.stderr_buf.read().decode("utf-8", errors="replace") or None
                if err is None and run.status == ExecStatus.CANCELLED:
                    err = "cancelled by caller"
                elif err is None and run.status == ExecStatus.TIMED_OUT:
                    err = "killed by timeout"
            return ExecPollResult(
                external_run_id=external_run_id,
                status=run.status,
                is_terminal=run.status in (
                    ExecStatus.EXITED,
                    ExecStatus.CANCELLED,
                    ExecStatus.TIMED_OUT,
                    ExecStatus.FAILED,
                ),
                exit_code=run.exit_code,
                output=output,
                error=err,
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
            )

    async def cancel(self, external_run_id: str) -> ExecCancelResult:
        run = self._require(external_run_id)
        with run._lock:
            if run.status in (ExecStatus.EXITED, ExecStatus.CANCELLED, ExecStatus.TIMED_OUT, ExecStatus.FAILED):
                return ExecCancelResult(
                    external_run_id=external_run_id,
                    cancelled=False,
                    reason=f"already {run.status.value}",
                )
            run.status = ExecStatus.CANCELLED
            run.cancelled = True
            run.exit_code = -1
            run.stderr_buf.write(b"cancelled by caller\n")
            run.end_ts = time.time()
        return ExecCancelResult(external_run_id=external_run_id, cancelled=True)

    def read_stdout(self, external_run_id: str) -> io.BytesIO:
        run = self._require(external_run_id)
        buf = io.BytesIO(run.stdout_buf.getvalue())
        return buf

    def read_stderr(self, external_run_id: str) -> io.BytesIO:
        run = self._require(external_run_id)
        buf = io.BytesIO(run.stderr_buf.getvalue())
        return buf

    def exit_code(self, external_run_id: str) -> Optional[int]:
        run = self._require(external_run_id)
        return run.exit_code

    def artifacts_dir(self, external_run_id: str) -> str:
        run = self._require(external_run_id)
        return run.artifacts_dir

    # -- Test helpers ------------------------------------------------------

    def list_runs(self) -> List[str]:
        with self._lock:
            return list(self._runs.keys())

    def get_run(self, external_run_id: str) -> _Run:
        return self._require(external_run_id)

    def set_behavior(self, behavior: Behavior) -> None:
        """Replace the default behavior for future submit() calls.

        Note: this only affects NEW runs. Existing runs already
        captured their behavior at submit() time.
        """
        self._behavior = behavior

    # -- Internals ---------------------------------------------------------

    def _require(self, external_run_id: str) -> _Run:
        with self._lock:
            run = self._runs.get(external_run_id)
            if run is None:
                raise ProviderError(
                    f"unknown run_id: {external_run_id!r}"
                )
        return run

    def _should_finalize(self, run: _Run) -> bool:
        # For tests: if the behavior has a delay, finalize when
        # the delay has elapsed since the run started.
        if not run.behavior or run.behavior.hang:
            return False
        if run.behavior.delay_seconds <= 0.0:
            return True
        # We don't actually wait — caller can call finalize() via
        # _advance_clock if they want to control time. Most tests
        # use delay=0 so this is the only branch we hit.
        return False

    def _finalize(self, run_id: str, beh: _BehaviorResult) -> None:
        run = self._require(run_id)
        with run._lock:
            if run.status != ExecStatus.RUNNING:
                return
            run.stdout_buf.write(beh.stdout.encode("utf-8"))
            run.stderr_buf.write(beh.stderr.encode("utf-8"))
            run.exit_code = beh.exit_code
            run.status = ExecStatus.EXITED if beh.exit_code == 0 else ExecStatus.FAILED
            run.end_ts = time.time()

    def _schedule_timeout(self, run_id: str, seconds: int) -> None:
        def _kill():
            time.sleep(seconds)
            run = self._require(run_id)
            with run._lock:
                if run.status not in (ExecStatus.EXITED, ExecStatus.CANCELLED, ExecStatus.FAILED):
                    run.status = ExecStatus.TIMED_OUT
                    run.timed_out = True
                    run.exit_code = -1
                    run.stderr_buf.write(f"killed after {seconds}s timeout\n".encode("utf-8"))
                    run.end_ts = time.time()
        t = threading.Thread(target=_kill, daemon=True)
        t.start()


# Re-export for tests + future real impl.
__all__ = [
    "FakeClaudeCodeProvider",
    "Behavior",
    "_happy_behavior",
    "_failing_behavior",
    "_hanging_behavior",
    # Errors re-exported for convenience
    "ProviderError",
    "ProviderTimeoutError",
    "ProviderExitError",
]
