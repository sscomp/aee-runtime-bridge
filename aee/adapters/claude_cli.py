"""ClaudeCliAdapter — AEE ``RuntimeAdapter`` that shells out to the local
``claude`` CLI in non-interactive (``-p``) mode.

This is the seam used when the bridge is deployed on a host that has
the official Anthropic Claude Code CLI installed but **no** Hermes M2
runtime (AEE Mission Control / A2 / TLE-Box style deployment).

Per ``RuntimeAdapter`` Protocol in ``aee/adapters/base.py`` the adapter
implements the three async methods ``submit / poll / cancel`` plus a
``health()`` helper. State is kept in an in-process dict so the
``dispatcher.watcher`` polling loop can drive ``poll()`` cheaply.

Design notes
------------
* We do NOT block on the subprocess. ``submit()`` returns as soon as
  ``Popen`` has spawned the process; the watcher polls and reads
  stdout / stderr via the per-run log files. This mirrors the way
  ``ClaudeCodeExecutorAdapter`` works, but we deliberately keep the
  surface minimal — no manifest gate, no ArtifactPipeline; those are
  Hermes / AEE-6 concerns that we explicitly do not need when the
  Claude CLI is the terminal executor.
* We pass ``--bare`` and ``--output-format text`` so the process is
  hermetic (no plugin / hook / MCP auto-discovery) and the stdout is
  a single final response string. ``--max-turns 1`` keeps the
  subprocess bounded; raise it via the ``AEE_CLAUDE_MAX_TURNS`` env
  if a job legitimately needs more.
* Cancellation: ``cancel()`` sends ``SIGTERM`` to the OS process and
  waits up to ``cancel_grace_seconds`` (default 5 s) before
  escalating to ``SIGKILL``. If the process is already gone we
  report ``cancelled=True`` (the run reached a terminal state in
  some other way).
* Failure modes:
  - ``FileNotFoundError`` (the ``claude`` binary is missing) →
    ``RuntimeError`` with a clear message; the watcher will
    re-raise it as a transport error.
  - Non-zero exit code with no stdout → ``status="failed"`` and
    ``error=stderr``.
  - Subprocess killed by signal → ``status="cancelled"`` (SIGTERM
    is the only signal we send ourselves, so this is unambiguous).
"""
from __future__ import annotations

import asyncio
import os
import shlex
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from aee.adapters.base import (
    RuntimeAdapter,
    RuntimeCancelResult,
    RuntimeError,
    RuntimePollResult,
    RuntimeSubmitResult,
    UnknownExternalRunError,
)


# Status vocabulary we surface to AEE. Mirrors HermesAdapter.
_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "timeout"}


# Subprocess defaults — all overridable via env so the operator
# does not have to edit the source.
DEFAULT_CLAUDE_BIN = "claude"
DEFAULT_CLAUDE_ARGS = "--bare --output-format text --max-turns 1"
DEFAULT_TIMEOUT_SEC = 60 * 30            # 30 minutes
DEFAULT_CANCEL_GRACE_SEC = 5
DEFAULT_RUNS_ROOT = "/tmp/aee-claude-cli-runs"


@dataclass
class _RunState:
    """In-memory bookkeeping for a single ``claude`` subprocess."""

    external_run_id: str
    proc: Optional[subprocess.Popen] = None
    run_dir: Optional[Path] = None
    stdout_log: Optional[Path] = None
    stderr_log: Optional[Path] = None
    submitted_at: float = field(default_factory=time.time)
    prompt: str = ""
    final_status: Optional[str] = None   # set when we observe exit
    final_output: Optional[str] = None
    final_error: Optional[str] = None
    exit_code: Optional[int] = None
    task_id: Optional[str] = None


class ClaudeCliAdapter:
    """AEE ``RuntimeAdapter`` that drives the local ``claude`` CLI.

    Used as a drop-in replacement for ``HermesAdapter`` on hosts
    that do not run Hermes M2.
    """

    name = "claude_cli"
    runtime_type = "claude_cli"

    def __init__(
        self,
        *,
        claude_bin: Optional[str] = None,
        extra_args: Optional[str] = None,
        runs_root: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        cancel_grace_seconds: Optional[int] = None,
    ) -> None:
        self._claude_bin = (
            claude_bin
            or os.getenv("AEE_CLAUDE_BIN")
            or DEFAULT_CLAUDE_BIN
        )
        env_args = os.getenv("AEE_CLAUDE_ARGS")
        self._extra_args = (
            extra_args
            if extra_args is not None
            else (env_args if env_args is not None else DEFAULT_CLAUDE_ARGS)
        )
        self._runs_root = Path(
            runs_root
            or os.getenv("AEE_CLAUDE_RUNS_ROOT")
            or DEFAULT_RUNS_ROOT
        ).resolve()
        self._timeout_seconds = int(
            timeout_seconds
            or os.getenv("AEE_CLAUDE_TIMEOUT_SEC")
            or DEFAULT_TIMEOUT_SEC
        )
        self._cancel_grace = int(
            cancel_grace_seconds
            or os.getenv("AEE_CLAUDE_CANCEL_GRACE_SEC")
            or DEFAULT_CANCEL_GRACE_SEC
        )
        # Per-process state. Single-writer because all methods that
        # mutate ``_runs`` are awaited from the same event loop thread.
        self._runs: Dict[str, _RunState] = {}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_prompt(job: Any) -> str:
        """Render the ``Job`` into a single string for ``claude -p``.

        The Job dataclass (see ``aee/core/job_models.py``) gives us
        ``title`` + ``input``; we use the title as a short context
        header and the input as the main body. ``spec`` may carry
        additional fields we honour: ``cwd`` (working directory),
        ``model`` (``--model``), ``system_prompt``
        (``--system-prompt``).
        """
        title = (getattr(job, "title", "") or "").strip()
        body = (getattr(job, "input", "") or "").strip()
        # ``input_text`` is the legacy alias used by the API validator.
        if not body:
            body = (getattr(job, "input_text", "") or "").strip()
        if title and body:
            return f"# {title}\n\n{body}"
        return body or title

    def _build_argv(self, prompt: str, spec: Mapping[str, Any]) -> list[str]:
        argv = [self._claude_bin]
        if self._extra_args:
            argv.extend(shlex.split(self._extra_args))
        # Per-job model override.
        model = spec.get("model")
        if model:
            argv.extend(["--model", str(model)])
        # Per-job system prompt override.
        system_prompt = spec.get("system_prompt")
        if system_prompt:
            argv.extend(["--system-prompt", str(system_prompt)])
        argv.extend(["-p", prompt])
        return argv

    def _child_env(self) -> Dict[str, str]:
        """Build a clean env for the subprocess.

        We forward a tight allow-list so we do not leak the bridge's
        own secrets (e.g. ``BRIDGE_API_KEY``) into the worker.

        TLE-Box / A2 deployment note: the operator's ``.bashrc`` maps
        ``minimax-m3`` to ``minimax-m3:cloud`` (an Ollama-cloud
        alias). Claude Code 2.1+ warns about unknown model names and,
        in default mode, refuses to start a request — the subprocess
        then hits ``max-turns=1`` and exits 1 with empty stdout. The
        env-var escape hatch Claude Code ships with is
        ``CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1``,
        which makes it just forward the API call to Ollama. We pass
        that var through when the operator exports it (typically via
        ``.env`` ::

            AEE_CLAUDE_EXTRA_ENV=CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1

        or directly in the bridge process environment).
        """
        keep = {
            "PATH", "HOME", "LANG", "LC_ALL", "TZ",
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            # Claude Code session-level switches (operator opt-in).
            "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT",
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
        }
        env = {k: v for k, v in os.environ.items() if k in keep and v}
        return env

    def _ensure_runs_root(self) -> None:
        self._runs_root.mkdir(parents=True, exist_ok=True)

    def _summarise_final(self, run: _RunState) -> tuple[str, Optional[str], Optional[str]]:
        """Return ``(status, output, error)`` for a finished subprocess.

        Reads the stdout log (truncated to a sane size for the API
        response), inspects the exit code, and decides between
        ``completed`` / ``failed`` / ``cancelled``.
        """
        output: Optional[str] = None
        if run.stdout_log and run.stdout_log.exists():
            try:
                data = run.stdout_log.read_bytes()
            except OSError:
                data = b""
            # Bound the response body — 256 KiB is plenty for a
            # dispatch result and prevents one runaway run from
            # blowing up the dispatcher's response payload.
            if len(data) > 256 * 1024:
                data = data[-256 * 1024:]
            try:
                output = data.decode("utf-8", errors="replace")
            except Exception:  # pragma: no cover
                output = ""

        error: Optional[str] = None
        if run.stderr_log and run.stderr_log.exists():
            try:
                edata = run.stderr_log.read_bytes()
            except OSError:
                edata = b""
            if len(edata) > 64 * 1024:
                edata = edata[-64 * 1024:]
            try:
                error = edata.decode("utf-8", errors="replace").strip() or None
            except Exception:  # pragma: no cover
                error = None

        # We set ``final_status`` if a signal killed the process
        # during cancel(); honour that. Otherwise classify by exit
        # code.
        if run.final_status == "cancelled":
            return "cancelled", output, error or "cancelled by adapter"
        if run.exit_code is None:
            return "failed", output, error or "subprocess exited with no exit code"
        if run.exit_code == 0:
            return "completed", output, error
        # Non-zero. If we have stderr, surface it; otherwise generic.
        return "failed", output, error or f"claude exited with code {run.exit_code}"

    # ------------------------------------------------------------------
    # RuntimeAdapter protocol
    # ------------------------------------------------------------------

    async def health(self) -> Dict[str, Any]:
        """Best-effort probe used by the bridge ``/health`` endpoint."""
        if not self._claude_bin:
            return {"ok": False, "error": "claude binary not configured"}
        bin_path = shutil_which(self._claude_bin)
        if not bin_path:
            return {
                "ok": False,
                "error": f"claude binary not found on PATH ({self._claude_bin!r})",
            }
        try:
            proc = await asyncio.create_subprocess_exec(
                bin_path, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            return {
                "ok": proc.returncode == 0,
                "version": stdout.decode("utf-8", errors="replace").strip(),
            }
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def submit(self, job: Any) -> RuntimeSubmitResult:
        """Spawn a ``claude -p`` subprocess and return its run id."""
        if not shutil_which(self._claude_bin):
            raise RuntimeError(
                f"claude binary not found on PATH ({self._claude_bin!r}); "
                "set AEE_CLAUDE_BIN or install the Claude Code CLI"
            )

        prompt = self._build_prompt(job)
        if not prompt:
            raise RuntimeError("claude_cli: empty prompt (Job has no input/title)")

        spec: Mapping[str, Any] = getattr(job, "spec", {}) or {}
        # cwd resolution, in priority order:
        #   1. ``spec["cwd"]`` — per-job override (most specific)
        #   2. ``AEE_CLAUDE_CWD`` env var (operator override)
        #   3. ``/workspace`` — TLE-Box / A2 deployment default; gives
        #      the worker a stable playground even when the bridge
        #      itself is launched from anywhere else (e.g. systemd
        #      ``/``).  Note this is a different fallback than the
        #      Hermes / ``claude_code`` executor, which still uses
        #      ``/home/ubuntu/Abacus`` as its default — that one is
        #      set in ``config/executor.json`` and is NOT changed
        #      here.
        cwd = spec.get("cwd") or os.getenv("AEE_CLAUDE_CWD") or "/workspace"
        task_id = (
            spec.get("task_id")
            or getattr(job, "external_run_id", None)
            or f"job-{uuid.uuid4().hex[:8]}"
        )
        external_run_id = f"claude-cli-{uuid.uuid4().hex}"

        self._ensure_runs_root()
        run_dir = self._runs_root / external_run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        stdout_log = run_dir / "stdout.log"
        stderr_log = run_dir / "stderr.log"
        prompt_file = run_dir / "prompt.txt"
        prompt_file.write_text(prompt, encoding="utf-8")

        argv = self._build_argv(prompt, spec)
        env = self._child_env()

        run = _RunState(
            external_run_id=external_run_id,
            run_dir=run_dir,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            prompt=prompt,
            task_id=str(task_id),
        )
        self._runs[external_run_id] = run

        try:
            # ``shell=False`` is mandatory — the argv is constructed
            # with shlex.split and never contains untrusted tokens.
            proc = subprocess.Popen(  # noqa: S603
                argv,
                cwd=str(cwd),
                stdout=stdout_log.open("wb"),
                stderr=stderr_log.open("wb"),
                stdin=subprocess.DEVNULL,
                env=env,
                shell=False,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            self._runs.pop(external_run_id, None)
            raise RuntimeError(
                f"claude_cli: failed to spawn {self._claude_bin!r}: {exc}"
            ) from exc
        except OSError as exc:
            self._runs.pop(external_run_id, None)
            raise RuntimeError(f"claude_cli: spawn OSError: {exc}") from exc

        run.proc = proc
        return RuntimeSubmitResult(
            external_run_id=external_run_id,
            status="running",
            raw={
                "pid": proc.pid,
                "argv": argv,
                "cwd": str(cwd),
                "run_dir": str(run_dir),
                "task_id": run.task_id,
            },
        )

    async def poll(self, external_run_id: str) -> RuntimePollResult:
        run = self._runs.get(external_run_id)
        if run is None:
            raise UnknownExternalRunError(
                f"claude_cli run {external_run_id!r} not known to this adapter "
                "(lost after adapter restart?)"
            )

        proc = run.proc
        if proc is None:
            return RuntimePollResult(
                external_run_id=external_run_id,
                status="queued",
            )

        # Non-blocking poll (proc.poll() does not wait).
        rc = proc.poll()
        if rc is None:
            # Still running. Apply timeout policy.
            age = time.time() - run.submitted_at
            if age > self._timeout_seconds:
                # Timed out — kill and finalise.
                run.final_status = "timeout"
                run.final_error = (
                    f"claude_cli: timeout after {age:.1f}s "
                    f"(limit={self._timeout_seconds}s)"
                )
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=self._cancel_grace)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                run.exit_code = proc.returncode
                status, output, error = self._summarise_final(run)
                return RuntimePollResult(
                    external_run_id=external_run_id,
                    status="timeout",
                    is_terminal=True,
                    output=output,
                    error=error or run.final_error,
                    raw={"pid": proc.pid, "exit_code": run.exit_code},
                )
            return RuntimePollResult(
                external_run_id=external_run_id,
                status="running",
                is_terminal=False,
                raw={"pid": proc.pid, "age_sec": age},
            )

        # Process exited.
        run.exit_code = rc
        if run.final_status is None:
            run.final_status = "completed" if rc == 0 else "failed"
        status, output, error = self._summarise_final(run)
        return RuntimePollResult(
            external_run_id=external_run_id,
            status=status,
            is_terminal=True,
            output=output,
            error=error,
            raw={"pid": proc.pid, "exit_code": rc},
        )

    async def cancel(self, external_run_id: str) -> RuntimeCancelResult:
        run = self._runs.get(external_run_id)
        if run is None:
            raise UnknownExternalRunError(
                f"claude_cli run {external_run_id!r} not known to this adapter"
            )
        proc = run.proc
        if proc is None:
            return RuntimeCancelResult(
                external_run_id=external_run_id,
                cancelled=True,
                reason="not yet started",
            )
        if proc.poll() is not None:
            return RuntimeCancelResult(
                external_run_id=external_run_id,
                cancelled=False,
                reason=f"already terminated (exit={proc.returncode})",
            )

        run.final_status = "cancelled"
        try:
            proc.terminate()
        except ProcessLookupError:
            return RuntimeCancelResult(
                external_run_id=external_run_id,
                cancelled=True,
                reason="process already gone",
            )

        try:
            proc.wait(timeout=self._cancel_grace)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=self._cancel_grace)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass
        return RuntimeCancelResult(
            external_run_id=external_run_id,
            cancelled=True,
            reason=f"sent SIGTERM to pid {proc.pid}",
        )


# ---------------------------------------------------------------------------
# Tiny helper — duplicated here so we do not have to import ``shutil``
# at module top (we already import a lot of subprocess machinery).
# ---------------------------------------------------------------------------


def shutil_which(binary: str) -> Optional[str]:
    """Return the absolute path of ``binary`` or ``None``.

    Mirrors ``shutil.which`` but lives inline to keep this adapter
    dependency-free. ``binary`` may be a bare command name (we walk
    ``$PATH``) or an absolute path (returned as-is if executable).
    """
    if not binary:
        return None
    if os.path.isabs(binary):
        return binary if os.access(binary, os.X_OK) else None
    path_env = os.environ.get("PATH", "")
    for d in path_env.split(os.pathsep):
        if not d:
            continue
        candidate = os.path.join(d, binary)
        if os.access(candidate, os.X_OK) and os.path.isfile(candidate):
            return candidate
    return None


__all__ = ["ClaudeCliAdapter"]
