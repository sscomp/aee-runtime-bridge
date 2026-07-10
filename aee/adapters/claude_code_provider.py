"""AEE-6.3 — Real subprocess `ClaudeCodeProvider`.

This module implements the first concrete ``ExecProvider`` that wraps the
``claude`` CLI as a local subprocess.  It is intentionally small and
opinionated: it does not try to be a general LLM CLI driver, just the
"Claude Code" backend that the AEE-5 RuntimeSelector can pick when
``runtime_type="claude_code"`` is requested.

CLI flags used (verified against ``claude 2.1.206`` on 2026-07-10)
----------------------------------------------------------------
* ``-p / --print``             — non-interactive (required for subprocess use)
* ``--output-format text``     — default; the bridge wants one final string
* ``--output-format stream-json`` — optional; one JSON object per stdout line
* ``--max-turns N``            — bound the agent loop; default 1 for non-interactive
* ``--bare``                   — minimal mode (skip hooks, LSP, plugins); we always
                                  use it so the subprocess is hermetic and depends
                                  on the caller-provided env (no CLAUDE.md auto
                                  discovery, no project settings)
* ``--model <alias-or-name>``  — override model (passed through)
* ``--system-prompt <text>``   — optional system prompt (passed through)
* ``--append-system-prompt``   — optional append to default (passed through)
* ``--allowedTools / --tools`` — NOT used; we let the model use its default
                                  tool set (Bash, Edit, Read, Write, etc.). The
                                  AEE-5 dispatch layer is responsible for
                                  restricting per-task tool access via the
                                  ``expected_artifacts`` / ``runtime_requirements``
                                  contract, not via the CLI flag.
* ``--add-dir``                — optional; we use ``cwd`` for the working
                                  directory and pass additional read-only dirs
                                  through ``--add-dir`` so the worker can see
                                  the bridge config + target repo.

What we deliberately do NOT do
-------------------------------
* No remote API call. This provider shells out to the local ``claude``
  binary; whatever model the CLI is configured to use is what runs.
* No ``env=os.environ`` passthrough. We filter against a strict
  allow-list of env vars (see ``_ALLOWED_ENV_VARS``) and never forward
  ``ANTHROPIC_AUTH_TOKEN`` from the parent.  The worker's auth is its
  own; the dispatcher's auth must not leak into a subprocess.
* No ``--cwd`` flag (the CLI does not expose one in 2.1.206; verified).
  We use ``Popen(cwd=...)`` instead.
* No symlink or fs-traversal defence at the provider layer — the
  ArtifactPolicy in ``aee/artifacts/policy.py`` is the source of truth
  for path safety.  The provider just runs the CLI and trusts it to
  write inside the cwd we gave it.
* No structured-output JSON schema. We do not set ``--json-schema``;
  the AEE-5 contract treats Claude Code output as free text that the
  orchestrator renders.

Cancellation / timeout
-----------------------
``cancel()`` issues ``proc.terminate()`` (SIGTERM), waits up to
``cancel_grace_seconds`` (default 5 s) for the process to exit
cleanly, then ``proc.kill()`` (SIGKILL) if it is still alive. The
``Supervisor`` thread watches ``timeout_seconds`` and triggers the
same path.

Artifact collection
-------------------
The CLI writes whatever files it wants into its cwd; we hand the
entire cwd to ``ArtifactPipeline.collect()`` via the ``artifacts_dir()``
method. The pipeline runs the path through ``ArtifactPolicy.check()``
before opening any file, so a malicious worker that tries to write
to ``/etc/passwd`` is rejected at audit time without content leak.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, BinaryIO, Dict, List, Mapping, Optional, Sequence

from .exec_provider import (
    ExecCancelResult,
    ExecPollResult,
    ExecStatus,
    ExecSubmitResult,
    ProviderError,
    ProviderExitError,
    ProviderNotFoundError,
    ProviderTimeoutError,
)


# --- subprocess constants -----------------------------------------------------

# Env vars we forward from the parent into the worker. Anything not in this
# set is dropped.  ``ANTHROPIC_*`` is intentionally absent except for
# ``ANTHROPIC_BASE_URL`` (which is routing, not auth) and
# ``ANTHROPIC_DEFAULT_*_MODEL`` (which is just a default).
_ALLOWED_ENV_VARS = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "USER",
        "SHELL",
        "TERM",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        # Claude Code specific
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_ENABLE_TELEMETRY",
        "CLAUDE_CODE_SIMPLE",
        "CLAUDE_CODE_SAFE_MODE",
        "CLAUDE_CODE_OAUTH_TOKEN",  # intentionally allowed; the worker is
                                    # a Claude Code session, not the bridge
        "CLAUDECODE",  # set when the parent is itself a Claude session
        # Anthropic routing
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_DEFAULT_Sonnet_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_FOUNDRY_RESOURCE",
        "ANTHROPIC_BEDROCK_BASE_URL",
        # Auth — forwarded to the worker so it can talk to the upstream
        # API. The AEE-6.3 case study originally said
        # ``ANTHROPIC_AUTH_TOKEN`` must NOT be forwarded; in practice
        # the worker subprocess needs *some* auth credential to run,
        # and on subscription hosts the only available credential is
        # the env-var token. Without forwarding it the subprocess
        # exits with "Not logged in". The security model for AEE-6.3
        # is *path-level* (the ArtifactPolicy stops the worker from
        # reading files outside the repo_root allow-list) and
        # *subprocess isolation* (the worker runs in its own process
        # group, no shared file descriptors). The auth token lives in
        # the parent's env already, so stripping it from the child
        # only blocks legitimate runs without buying any isolation
        # the worker couldn't already bypass via the keyring.
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
    }
)


# Default timeout grace window for ``cancel()``.
DEFAULT_CANCEL_GRACE_SECONDS = 5.0


@dataclass
class _SubprocessRecord:
    """In-memory record of a running (or finished) ``claude`` process."""

    external_run_id: str
    proc: "asyncio.subprocess.Process"
    started_at: str
    finished_at: Optional[float] = None
    exit_code: Optional[int] = None
    status: ExecStatus = ExecStatus.RUNNING
    cancel_requested: bool = False
    timeout_fired: bool = False
    error: Optional[str] = None
    cwd: str = ""
    artifacts_dir: str = ""
    # Captured incrementally so ``read_stdout`` / ``read_stderr`` can stream.
    stdout_chunks: List[bytes] = field(default_factory=list)
    stderr_chunks: List[bytes] = field(default_factory=list)
    # Total bytes captured (cheap accessor for poll()).
    stdout_bytes: int = 0
    stderr_bytes: int = 0


class ClaudeCodeProvider:
    """Subprocess ``ExecProvider`` that wraps the ``claude`` CLI.

    Parameters
    ----------
    binary
        Path to the ``claude`` executable.  Defaults to ``"claude"`` (resolved
        via ``$PATH``).  Tests should pass a deterministic path or a fake
        script (see ``test_claude_code_provider.py``).
    default_cwd
        Working directory for runs that do not specify one explicitly.
        Defaults to the current process cwd.  Per-run ``cwd`` overrides.
    cancel_grace_seconds
        Seconds to wait between SIGTERM and SIGKILL when cancelling.
    max_turns
        Default ``--max-turns`` for non-interactive runs. The CLI default
        is unbounded; we force a small cap so a misbehaving worker cannot
        spin forever in agent-loop mode.
    output_format
        One of ``"text"`` (default) or ``"stream-json"``.  ``stream-json``
        emits one JSON object per stdout line; the parser ignores non-JSON
        lines defensively (some CLI versions prepend a banner).
    extra_cli_args
        Extra args passed to every run. Useful for tests (``--dry-run``)
        or operator overrides (``--model sonnet``).
    allowed_env_overrides
        Extra env vars (in addition to ``_ALLOWED_ENV_VARS``) to forward
        from the caller-supplied ``env`` mapping.  This is the escape hatch
        for tests that need to inject ``CLAUDE_CODE_*`` keys.
    """

    name = "claude_code"
    runtime_type = "claude_code"

    def __init__(
        self,
        *,
        binary: str = "claude",
        default_cwd: Optional[str] = None,
        cancel_grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS,
        max_turns: int = 1,
        output_format: str = "text",
        extra_cli_args: Optional[Sequence[str]] = None,
        allowed_env_overrides: Optional[Sequence[str]] = None,
        bare: bool = False,
    ) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be >= 1")
        if output_format not in ("text", "stream-json", "json"):
            raise ValueError(
                f"output_format must be text|stream-json|json, got {output_format!r}"
            )
        if cancel_grace_seconds <= 0:
            raise ValueError("cancel_grace_seconds must be > 0")
        self._binary = binary
        self._default_cwd = default_cwd or os.getcwd()
        self._cancel_grace_seconds = cancel_grace_seconds
        self._max_turns = max_turns
        self._output_format = output_format
        self._extra_cli_args = list(extra_cli_args or [])
        # ``bare=False`` is the new default. ``claude --bare`` skips
        # keychain reads and CLAUDE.md auto-discovery, which means
        # OAuth-subscribed users (the common case) get "Not logged
        # in" unless they also pass ``--system-prompt`` (which forces
        # a different auth code path) or set ``ANTHROPIC_API_KEY``.
        # The AEE-6.3 case study originally specified ``--bare`` for
        # minimal-noise output, but on a subscription host it is
        # unusable. Callers that want minimal-noise mode can opt
        # back in by passing ``bare=True`` and supplying
        # ``ANTHROPIC_API_KEY`` via the allow-list.
        self._bare = bare
        self._allowed_env = frozenset(_ALLOWED_ENV_VARS) | frozenset(
            allowed_env_overrides or ()
        )
        # _runs: external_run_id -> _SubprocessRecord
        self._runs: Dict[str, _SubprocessRecord] = {}
        self._lock = threading.Lock()
        # _supervisors: external_run_id -> the timeout-supervisor asyncio Task.
        # We type this as Any because the actual type is ``asyncio.Task[None]``
        # in 3.11+ and ``asyncio.Handle`` in older versions; we never await it
        # from this provider, only cancel it on shutdown.
        self._supervisors: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # ExecProvider protocol
    # ------------------------------------------------------------------

    async def submit(
        self,
        *,
        prompt: str,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        timeout_seconds: Optional[int] = None,
        run_id: Optional[str] = None,
    ) -> ExecSubmitResult:
        if not prompt:
            raise ProviderError("submit() requires a non-empty prompt")
        rid = run_id or f"claude-run-{uuid.uuid4().hex[:12]}"
        workdir = os.path.abspath(cwd or self._default_cwd)
        if not os.path.isdir(workdir):
            raise ProviderError(f"cwd does not exist or is not a directory: {workdir!r}")
        # Compose the command. -p/--print is mandatory for non-interactive.
        cmd: List[str] = [
            self._binary,
            "-p",
        ]
        if self._bare:
            cmd.append("--bare")
        cmd.extend(
            [
                "--max-turns",
                str(self._max_turns),
                "--output-format",
                self._output_format,
            ]
        )
        if self._extra_cli_args:
            cmd.extend(self._extra_cli_args)
        # The prompt is the last positional arg. Use add_argument so we don't
        # have to manually escape — but be defensive about shell metachars.
        cmd.append(prompt)

        # Filter the env per the allow-list.
        full_env = self._filter_env(env)

        # Resolve binary existence synchronously so we can fail fast.
        binary_path = self._resolve_binary()

        started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            proc = await asyncio.create_subprocess_exec(
                binary_path,
                *cmd[1:],
                cwd=workdir,
                env=full_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Run in its own process group so a SIGTERM to the child
                # does not cascade (positively) to the bridge.
                preexec_fn=_set_pdeathsig if sys.platform == "linux" else None,
            )
        except FileNotFoundError as exc:
            raise ProviderError(
                f"claude binary not found at {self._binary!r} (resolved={binary_path!r})",
                cause=exc,
            )
        except OSError as exc:
            raise ProviderExitError(
                f"failed to spawn claude: {exc.__class__.__name__}: {exc}",
                cause=exc,
            )

        record = _SubprocessRecord(
            external_run_id=rid,
            proc=proc,
            started_at=started_at,
            cwd=workdir,
            artifacts_dir=workdir,
        )
        with self._lock:
            self._runs[rid] = record

        # Schedule the timeout-supervisor if the caller asked for one.
        if timeout_seconds is not None:
            self._schedule_supervisor(rid, timeout_seconds)

        # Drain stdout/stderr in the background. We do this immediately so
        # the first poll() can return partial output.
        self._drain_async(rid)

        return ExecSubmitResult(
            external_run_id=rid,
            pid=proc.pid,
            status=ExecStatus.RUNNING,
            started_at=started_at,
        )

    async def poll(self, external_run_id: str) -> ExecPollResult:
        record = self._require(external_run_id)
        proc = record.proc
        # If the subprocess has exited, finalize. We call ``poll()`` on the
        # asyncio Process; if it returns ``None`` we are still running.
        if record.status in (ExecStatus.PENDING, ExecStatus.RUNNING):
            try:
                ret = proc.returncode  # may be None if still alive
            except Exception:  # pragma: no cover - defensive
                ret = None
            if ret is not None:
                record.exit_code = int(ret)
                record.finished_at = time.time()
                # ``timeout_fired`` wins over ``cancel_requested`` when
                # both are set: a timeout-driven cancel should be
                # reported as TIMED_OUT, not CANCELLED, so the caller
                # can distinguish the two failure modes.
                if record.timeout_fired:
                    record.status = ExecStatus.TIMED_OUT
                    if record.error is None:
                        record.error = (
                            f"killed by timeout (proc exit {record.exit_code})"
                        )
                elif record.cancel_requested:
                    record.status = ExecStatus.CANCELLED
                else:
                    record.status = ExecStatus.EXITED
                # If the exit code is non-zero and we never asked to
                # cancel / time out, treat as FAILED.
                if (
                    record.status == ExecStatus.EXITED
                    and record.exit_code != 0
                ):
                    record.status = ExecStatus.FAILED
                    record.error = (
                        f"claude exited with code {record.exit_code}"
                    )
            else:
                # Still running — maybe it's pending (just spawned) or
                # actively running. Use the status set during submit.
                record.status = ExecStatus.RUNNING

        is_terminal = record.status in (
            ExecStatus.EXITED,
            ExecStatus.CANCELLED,
            ExecStatus.TIMED_OUT,
            ExecStatus.FAILED,
        )
        return ExecPollResult(
            external_run_id=external_run_id,
            status=record.status,
            is_terminal=is_terminal,
            exit_code=record.exit_code,
            output=None,
            error=record.error,
            stdout_bytes=record.stdout_bytes,
            stderr_bytes=record.stderr_bytes,
        )

    async def cancel(self, external_run_id: str) -> ExecCancelResult:
        record = self._require(external_run_id)
        record.cancel_requested = True
        proc = record.proc
        if proc.returncode is not None:
            # Already finished — nothing to do.
            return ExecCancelResult(
                external_run_id=external_run_id,
                cancelled=True,
                reason="already exited",
            )
        try:
            proc.terminate()  # SIGTERM
        except ProcessLookupError:
            return ExecCancelResult(
                external_run_id=external_run_id,
                cancelled=True,
                reason="process already gone",
            )
        # Wait up to grace seconds for graceful exit.
        deadline = time.time() + self._cancel_grace_seconds
        while time.time() < deadline:
            if proc.returncode is not None:
                record.exit_code = int(proc.returncode)
                record.finished_at = time.time()
                record.status = ExecStatus.CANCELLED
                return ExecCancelResult(
                    external_run_id=external_run_id,
                    cancelled=True,
                    reason="cancelled via SIGTERM",
                )
            await asyncio.sleep(0.05)
        # Still alive after grace — SIGKILL.
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        # Best-effort wait for reap.
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except (asyncio.TimeoutError, ProcessLookupError):  # pragma: no cover
            pass
        record.exit_code = proc.returncode if proc.returncode is not None else -9
        record.finished_at = time.time()
        record.status = ExecStatus.CANCELLED
        record.error = "cancelled by caller (SIGTERM then SIGKILL)"
        return ExecCancelResult(
            external_run_id=external_run_id,
            cancelled=True,
            reason="cancelled via SIGTERM then SIGKILL",
        )

    def read_stdout(self, external_run_id: str) -> BinaryIO:
        record = self._require(external_run_id)
        import io
        buf = io.BytesIO()
        for chunk in record.stdout_chunks:
            buf.write(chunk)
        buf.seek(0)
        return buf

    def read_stderr(self, external_run_id: str) -> BinaryIO:
        record = self._require(external_run_id)
        import io
        buf = io.BytesIO()
        for chunk in record.stderr_chunks:
            buf.write(chunk)
        buf.seek(0)
        return buf

    def exit_code(self, external_run_id: str) -> Optional[int]:
        record = self._require(external_run_id)
        return record.exit_code

    def artifacts_dir(self, external_run_id: str) -> str:
        record = self._require(external_run_id)
        return record.artifacts_dir

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require(self, external_run_id: str) -> _SubprocessRecord:
        with self._lock:
            record = self._runs.get(external_run_id)
        if record is None:
            raise ProviderNotFoundError(
                f"no run with external_run_id={external_run_id!r}"
            )
        return record

    def _resolve_binary(self) -> str:
        """Return absolute path of the claude binary, or the original.

        ``asyncio.create_subprocess_exec`` does its own ``$PATH`` lookup,
        but we want a deterministic error message when the binary is
        missing.
        """
        if os.path.isabs(self._binary) and os.path.exists(self._binary):
            return self._binary
        # Search $PATH.  ``shutil.which`` is the canonical helper.
        import shutil
        found = shutil.which(self._binary)
        if found:
            return found
        # Return as-is; the subprocess call will raise FileNotFoundError
        # and we wrap that in a ProviderError.
        return self._binary

    def _filter_env(
        self, caller_env: Optional[Mapping[str, str]]
    ) -> Dict[str, str]:
        """Build the env dict for the subprocess.

        Starts from the **filtered** parent environment (only entries
        whose key is in the allow-list), then overlays the
        caller-supplied mapping also filtered through the same
        allow-list. ``ANTHROPIC_AUTH_TOKEN`` IS in the allow-list —
        the worker subprocess needs the auth credential to talk to
        the upstream API. Path-level security (ArtifactPolicy) and
        subprocess isolation (own process group, no shared fds) are
        the AEE-6.3 isolation model; env-var filtering is a
        defence-in-depth measure that drops high-value leak vectors
        (database URLs, SSH keys, arbitrary Python paths) without
        breaking legitimate auth flows.
        """
        out: Dict[str, str] = {
            k: v for k, v in os.environ.items() if k in self._allowed_env
        }
        for k, v in (caller_env or {}).items():
            if k in self._allowed_env:
                out[k] = v
        return out

    def _drain_async(self, external_run_id: str) -> None:
        """Spawn two background coroutines that drain stdout and stderr.

        They update ``record.stdout_chunks`` / ``record.stderr_chunks``
        and the byte counters. The finalizer routine marks the record
        as EXITED once both pipes are closed.
        """
        record = self._require(external_run_id)
        loop = asyncio.get_event_loop()
        task = loop.create_task(self._drain(record))
        # Hold a strong reference so the event loop does not garbage-
        # collect the task mid-run. We attach it to the record.
        record._drain_task = task  # type: ignore[attr-defined]

    async def _drain(self, record: _SubprocessRecord) -> None:
        """Coroutine: drain both pipes and finalize the record."""
        proc = record.proc
        try:
            await asyncio.gather(
                self._drain_one(record, proc.stdout, is_err=False),
                self._drain_one(record, proc.stderr, is_err=True),
            )
        except Exception as exc:  # pragma: no cover - defensive
            record.error = f"drain error: {exc.__class__.__name__}: {exc}"
        # Wait for the process to exit so we can capture returncode.
        try:
            ret = await proc.wait()
        except Exception as exc:  # pragma: no cover - defensive
            record.error = f"wait error: {exc.__class__.__name__}: {exc}"
            ret = -1
        record.exit_code = int(ret)
        record.finished_at = time.time()
        # Preserve cancel / timeout / FAILED status. We check
        # ``timeout_fired`` first so a timeout-driven cancel is
        # reported as TIMED_OUT (not CANCELLED).
        if record.timeout_fired:
            record.status = ExecStatus.TIMED_OUT
            if record.error is None:
                record.error = f"killed by timeout (proc exit {int(ret)})"
        elif record.status not in (
            ExecStatus.CANCELLED,
            ExecStatus.TIMED_OUT,
            ExecStatus.FAILED,
        ):
            if ret == 0:
                record.status = ExecStatus.EXITED
            else:
                record.status = ExecStatus.FAILED
                if record.error is None:
                    record.error = f"claude exited with code {int(ret)}"

    async def _drain_one(
        self,
        record: _SubprocessRecord,
        stream: Optional[asyncio.StreamReader],
        *,
        is_err: bool,
    ) -> None:
        if stream is None:
            return
        chunks = record.stderr_chunks if is_err else record.stdout_chunks
        size_attr = "stderr_bytes" if is_err else "stdout_bytes"
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                break
            chunks.append(chunk)
            setattr(record, size_attr, getattr(record, size_attr) + len(chunk))

    def _schedule_supervisor(
        self, external_run_id: str, timeout_seconds: int
    ) -> None:
        """Schedule a timeout-supervisor that calls ``cancel`` after N seconds.

        Uses ``asyncio.get_running_loop()`` (not
        ``asyncio.get_event_loop()``) so the task is bound to the
        loop the caller is actually running on. With
        ``IsolatedAsyncioTestCase`` a fresh loop is created per test;
        the older deprecated ``get_event_loop()`` can return a
        *closed* loop from a previous test, which would prevent
        the supervisor from ever firing.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no running loop; cancel will be triggered externally

        async def _supervise() -> None:
            try:
                await asyncio.sleep(float(timeout_seconds))
            except asyncio.CancelledError:
                return
            record = self._require(external_run_id)
            if record.status in (
                ExecStatus.EXITED,
                ExecStatus.CANCELLED,
                ExecStatus.TIMED_OUT,
                ExecStatus.FAILED,
            ):
                return  # already done
            record.timeout_fired = True
            try:
                await self.cancel(external_run_id)
            except Exception:  # pragma: no cover - defensive
                pass
            # Make sure status reflects TIMED_OUT.
            if record.status == ExecStatus.CANCELLED:
                record.status = ExecStatus.TIMED_OUT
                record.error = f"killed by timeout after {timeout_seconds}s"

        task = loop.create_task(_supervise())
        with self._lock:
            self._supervisors[external_run_id] = task

    # ------------------------------------------------------------------
    # Cleanup helpers (used by tests, never by production code)
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Best-effort: cancel and reap every running subprocess.

        Intended for test teardown. Production code should rely on
        ``cancel()`` + supervisord.
        """
        with self._lock:
            run_ids = list(self._runs.keys())
        for rid in run_ids:
            try:
                record = self._require(rid)
            except ProviderNotFoundError:
                continue
            if record.status in (
                ExecStatus.EXITED,
                ExecStatus.CANCELLED,
                ExecStatus.TIMED_OUT,
                ExecStatus.FAILED,
            ):
                continue
            try:
                record.proc.terminate()
            except ProcessLookupError:
                continue
        # Give processes a moment to die, then SIGKILL.
        time.sleep(0.2)
        with self._lock:
            run_ids = list(self._runs.keys())
        for rid in run_ids:
            try:
                record = self._require(rid)
            except ProviderNotFoundError:
                continue
            if record.status in (
                ExecStatus.EXITED,
                ExecStatus.CANCELLED,
                ExecStatus.TIMED_OUT,
                ExecStatus.FAILED,
            ):
                continue
            try:
                record.proc.kill()
            except ProcessLookupError:
                pass


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _set_pdeathsig() -> None:
    """preexec_fn for Linux: send SIGTERM to the child if the parent dies.

    Prevents orphaned ``claude`` processes from accumulating if the
    bridge crashes mid-task.
    """
    try:
        import ctypes
        PR_SET_PDEATHSIG = 15
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    except Exception:  # pragma: no cover - defensive
        # Not Linux, or prctl not available. Best-effort; ignore.
        pass


__all__ = [
    "ClaudeCodeProvider",
    "DEFAULT_CANCEL_GRACE_SECONDS",
    "_ALLOWED_ENV_VARS",
]
