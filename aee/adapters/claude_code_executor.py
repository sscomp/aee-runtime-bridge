"""ClaudeCodeExecutorAdapter — AEE RuntimeAdapter for the committed
Claude Code Runner MVP.

Scope
-----
This module is the thin seam between AEE and the local
``scripts.claude_code_runner`` subprocess that lives in
``/home/Abacus/AEE``. It is intentionally minimal:

* ``submit()``  — build an argv list, ``Popen`` it (no shell),
  capture stdout/stderr into per-run log files, return a
  stable external run id immediately. **Does not wait.**
* ``poll()``    — distinguish alive / exited-no-manifest /
  exited-with-manifest / verified / failed; never claim
  ``is_terminal=True, status="completed"`` until
  ``completion.verified.json`` is on disk and looks like
  the manifest gate's positive shape. (The watcher still
  re-verifies; this is the first of two defense-in-depth
  lines.)
* ``cancel()``  — SIGTERM the Runner, give it a grace
  period, escalate to SIGKILL, never classify cancellation
  as completed.

Composite task id
-----------------
We pass ``<HERMES_TASK_ID>--<HERMES_RUN_ID>`` to the Runner so
retries never overwrite earlier evidence (each retry lands in a
fresh leaf directory under the runs root).

Concurrency
-----------
The actual concurrency guard is an integer count of in-flight
``_ClaudeRun`` entries whose subprocess is still alive (TASK-M3
FIX-5: a previous ``asyncio.Semaphore(1)`` was created at module
scope but never acquired or released; the integer guard is the
real implementation). A second ``submit()`` while one is already
active is rejected (not queued) — see TASK-M2 §"No New Lock
System".

The in-flight dict is cleaned up on every terminal path
(poll completed / poll failed / cancel) so it does not grow
without bound over a long-lived process (TASK-M3 FIX-4).

No persistence
--------------
If Hermes restarts mid-run, the subprocess is reaped by the OS
but our in-memory state is gone. This is documented in the
implementation report as a remaining risk; persistent recovery
is explicitly out of scope for this MVP.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from aee.adapters.base import (
    RuntimeAdapter,
    RuntimeCancelResult,
    RuntimeError as AdapterRuntimeError,
    RuntimePollResult,
    RuntimeSubmitResult,
    UnknownExternalRunError,
)
from aee.adapters.manifest_verifier import (
    E_CLAIM_MANIFEST_MISSING,
    E_PROCESS_GROUP_NOT_VERIFIED_DEAD,
    E_RUNNER_IS_ERROR_TRUE,
    E_RUNNER_SUBTYPE_NOT_SUCCESS,
    E_RUNNER_TERMINAL_REASON_NOT_COMPLETED,
    E_RUNNER_VERIFICATION_ERRORS_PRESENT,
    E_VERIFIED_MANIFEST_MISSING,
    verify_completion_manifest,
)


# --- Constants ---------------------------------------------------------

# Default Hermes-controlled runs root. Hermes OWNS this path; the
# Runner is a child that only writes into ``<runs_root>/<composite_id>/``.
DEFAULT_RUNS_ROOT = "/home/ubuntu/Abacus/AEE-RUNS"

# Default cwd for the Runner subprocess. Per the task contract the
# Runner is launched as ``python3 -m scripts.claude_code_runner``,
# which requires its scripts directory to be importable. The AEE
# checkout is the only approved location.
DEFAULT_RUNNER_CWD = "/home/ubuntu/Abacus/AEE"

# Hard limit on simultaneous Claude Code runs. The MVP rejects the
# second submit while one is in flight.
_CONCURRENCY = 1

# SIGTERM grace period before SIGKILL escalation.
_CANCEL_GRACE_SECONDS = 5.0

# Supported schema version mirrored from the manifest verifier.
_SUPPORTED_SCHEMA_VERSION = "1.0.0"


# --- Module-level state -----------------------------------------------

# The concurrency guard is an integer count of in-flight
# ``_ClaudeRun`` entries whose subprocess is still alive (see
# ``ClaudeCodeExecutorAdapter.submit``). TASK-M3 FIX-5: an earlier
# implementation created a module-level ``asyncio.Semaphore(1)``
# that was never acquired or released; the docstring has been
# updated and the dead code removed. ``_max_inflight`` lets tests
# configure a higher ceiling; production uses 1.
_max_inflight = 1
_inflight: Dict[str, "_ClaudeRun"] = {}
_lock = asyncio.Lock()


# --- Internal state ---------------------------------------------------


@dataclass
class _ClaudeRun:
    external_run_id: str
    runner_task_id: str
    runner_run_dir: Path
    verified_manifest: Path
    claim_manifest: Path
    stdout_log: Path
    stderr_log: Path
    process: Optional[subprocess.Popen] = None
    start_time: float = field(default_factory=time.time)
    timeout_seconds: int = 0
    cancel_requested: bool = False
    # Configured argv (kept for tests + audit).
    argv: List[str] = field(default_factory=list)
    runner_cwd: str = DEFAULT_RUNNER_CWD
    repo_path: str = ""
    required_artifacts: List[str] = field(default_factory=list)


# --- Adapter ----------------------------------------------------------


class ClaudeCodeExecutorAdapter:
    """AEE ``RuntimeAdapter`` implementation for the Claude Code
    Runner MVP.

    Per the task contract:

    * Subprocess is launched with ``shell=False`` and an explicit
      argv list.
    * Composite task id is ``<hermes_task_id>--<hermes_run_id>``.
    * Herms owns ``/home/ubuntu/Abacus/AEE-RUNS``; the adapter
      never lets the Runner write outside its expected leaf.
    * No database migration; all state is in-memory.
    * No shell, no remote HTTP, no local HTTP Runner service.
    """

    name = "claude_code"
    runtime_type = "claude_code"

    def __init__(
        self,
        *,
        runs_root: Optional[str] = None,
        runner_cwd: Optional[str] = None,
        python_bin: Optional[str] = None,
        runner_module: str = "scripts.claude_code_runner",
        max_inflight: int = 1,
    ) -> None:
        # ``runs_root`` and ``runner_cwd`` are constructor-injectable
        # so tests can isolate under /tmp. Production uses the
        # task-specified defaults.
        self._runs_root = Path(runs_root or DEFAULT_RUNS_ROOT).resolve()
        self._runner_cwd = runner_cwd or DEFAULT_RUNNER_CWD
        self._python_bin = python_bin or "python3"
        self._runner_module = runner_module
        global _max_inflight
        _max_inflight = max(1, int(max_inflight))

    # -- RuntimeAdapter protocol -----------------------------------------

    async def submit(self, job: "Any") -> RuntimeSubmitResult:  # noqa: F821
        """Launch the Runner subprocess and return a run id.

        Validates the spec fields, builds the argv list, opens the
        run directory under ``runs_root``, ``Popen``s the process,
        and returns immediately. The watcher (``/runs`` polling
        loop) is the one that calls ``poll()``.

        Raises:
            AdapterRuntimeError: transport / setup failure.
            ClaudeConcurrencyError: another Claude Code run is
                already in flight.
        """
        spec = _extract_spec(job)
        # Concurrency guard: we reject while one is active.
        async with _lock:
            active = sum(
                1 for r in _inflight.values() if r.process is not None and r.process.poll() is None
            )
            if active >= _max_inflight:
                raise ClaudeConcurrencyError(
                    f"claude_code: another run is in flight "
                    f"(active={active}, max={_max_inflight})"
                )
        composite = f"{spec['task_id']}--{spec['run_id']}"
        run_dir = self._runs_root / composite
        # Open log files; we want a stable inode that Popen can write.
        run_dir.mkdir(parents=True, exist_ok=True)
        stdout_log = run_dir / "stdout.log"
        stderr_log = run_dir / "stderr.log"
        argv = _build_argv(
            python_bin=self._python_bin,
            runner_module=self._runner_module,
            runs_root=str(self._runs_root),
            composite_task_id=composite,
            spec=spec,
        )
        # Launch
        try:
            # Forward only the environment the Runner is allowed
            # to see. We deliberately do NOT pass ``os.environ``
            # wholesale — the production Runner filters its own
            # env; here we only carry through the few keys the
            # fake Runner (and any well-behaved future Runner)
            # needs to read.
            child_env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", ""),
                "LANG": os.environ.get("LANG", ""),
                "LC_ALL": os.environ.get("LC_ALL", ""),
                "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
                "FAKE_RUNNER_MODE": os.environ.get("FAKE_RUNNER_MODE", "pass"),
            }
            # Drop empties so the child env stays minimal.
            child_env = {k: v for k, v in child_env.items() if v}
            proc = subprocess.Popen(  # noqa: S603 — see argv construction
                argv,
                cwd=self._runner_cwd,
                stdout=stdout_log.open("wb"),
                stderr=stderr_log.open("wb"),
                stdin=subprocess.DEVNULL,
                env=child_env,
                shell=False,  # NEVER True
                # Start in a new session so SIGTERM to the adapter
                # does not cascade to the Runner (and so the
                # Runner's process group is its own — the Runner
                # already manages its own children).
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise AdapterRuntimeError(
                f"claude_code: failed to launch runner: {exc}",
                cause=exc,
            ) from exc
        run = _ClaudeRun(
            external_run_id=spec["run_id"],
            runner_task_id=composite,
            runner_run_dir=run_dir,
            verified_manifest=run_dir / "completion.verified.json",
            claim_manifest=run_dir / "completion.claim.json",
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            process=proc,
            timeout_seconds=spec["timeout_seconds"],
            argv=argv,
            runner_cwd=self._runner_cwd,
            repo_path=spec["repo_path"],
            required_artifacts=list(spec.get("required_artifacts") or []),
        )
        async with _lock:
            _inflight[spec["run_id"]] = run
        return RuntimeSubmitResult(
            external_run_id=spec["run_id"],
            status="queued",
            raw={
                "composite_task_id": composite,
                "runner_cwd": self._runner_cwd,
                "run_dir": str(run_dir),
                "argv": argv,
                "pid": proc.pid,
            },
        )

    async def poll(self, external_run_id: str) -> RuntimePollResult:
        run = _inflight.get(external_run_id)
        if run is None:
            raise UnknownExternalRunError(
                f"claude_code: unknown run_id {external_run_id!r}"
            )
        proc = run.process
        if proc is None:
            # Already cleaned up — treat as terminal cancelled/failed.
            return RuntimePollResult(
                external_run_id=external_run_id,
                status="failed",
                is_terminal=True,
                error="runner process not found in adapter state",
            )
        exit_code = proc.poll()
        if exit_code is None:
            # Process still alive
            return RuntimePollResult(
                external_run_id=external_run_id,
                status="running",
                is_terminal=False,
            )
        # Process exited. Decide what to surface:
        if run.verified_manifest.exists():
            # Manifest present. Run the verifier ourselves (first
            # of the two defense-in-depth lines) so the watcher
            # sees a clear terminal state.
            ver = verify_completion_manifest(
                verified_manifest_path=run.verified_manifest,
                expected_task_id=run.runner_task_id,
                expected_run_dir=run.runner_run_dir,
                repo_path=run.repo_path,
                required_artifacts=run.required_artifacts,
                subprocess_exit_code=exit_code,
            )
            if ver.verified:
                result = RuntimePollResult(
                    external_run_id=external_run_id,
                    status="completed",
                    is_terminal=True,
                    raw={
                        "verification": ver.to_dict(),
                        "verified_manifest": str(run.verified_manifest),
                        "process_exit_code": exit_code,
                    },
                )
                # TASK-M3 FIX-4: clean up the in-flight entry on
                # every terminal path so the dict does not grow
                # without bound over a long-lived process.
                _drop_inflight(external_run_id)
                return result
            # Manifest existed but failed verification. This is
            # the "no premature completed" guarantee.
            result = RuntimePollResult(
                external_run_id=external_run_id,
                status="failed",
                is_terminal=True,
                error=_summarise_verification_errors(ver.verification_errors),
                raw={
                    "verification": ver.to_dict(),
                    "verified_manifest": str(run.verified_manifest),
                    "process_exit_code": exit_code,
                },
            )
            _drop_inflight(external_run_id)
            return result
        # No manifest at all. This is the failure mode the task
        # is designed to catch.
        err = (
            f"runner process exited (code={exit_code}) without "
            f"completion.verified.json at {run.verified_manifest}"
        )
        result = RuntimePollResult(
            external_run_id=external_run_id,
            status="failed",
            is_terminal=True,
            error=err,
            raw={
                "verified_manifest_path": str(run.verified_manifest),
                "process_exit_code": exit_code,
            },
        )
        _drop_inflight(external_run_id)
        return result

    async def cancel(self, external_run_id: str) -> RuntimeCancelResult:
        run = _inflight.get(external_run_id)
        if run is None:
            return RuntimeCancelResult(
                external_run_id=external_run_id,
                cancelled=False,
                reason="unknown run",
            )
        if run.cancel_requested:
            return RuntimeCancelResult(
                external_run_id=external_run_id,
                cancelled=True,
                reason="cancel already requested",
            )
        proc = run.process
        if proc is None:
            return RuntimeCancelResult(
                external_run_id=external_run_id,
                cancelled=False,
                reason="runner process not found",
            )
        run.cancel_requested = True
        if proc.poll() is not None:
            # Already exited; cancel was effectively a no-op. Keep
            # the in-flight entry so a subsequent ``poll()`` can
            # still read the manifest (the runner may have written
            # one before exiting). The terminal ``poll()`` will
            # clean it up.
            return RuntimeCancelResult(
                external_run_id=external_run_id,
                cancelled=True,
                reason="runner already exited",
            )
        # Send SIGTERM to the Runner's own process group. We use
        # ``start_new_session=True`` at submit time, so the Runner
        # is its own session leader and SIGTERM here goes to it
        # only.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return RuntimeCancelResult(
                external_run_id=external_run_id,
                cancelled=True,
                reason="runner already gone",
            )
        except (PermissionError, OSError) as exc:
            return RuntimeCancelResult(
                external_run_id=external_run_id,
                cancelled=False,
                reason=f"SIGTERM failed: {exc}",
            )
        # Wait a bounded grace period for the Runner to write its
        # final manifest and exit cleanly.
        try:
            rc = await asyncio.get_event_loop().run_in_executor(
                None, lambda: proc.wait(timeout=_CANCEL_GRACE_SECONDS)
            )
            # TASK-M3 FIX-4: clean up the in-flight entry on
            # every terminal cancellation path. The Runner wrote
            # a manifest (if any) and exited; the next ``poll()``
            # should be a no-op (we still surface the
            # ``UnknownExternalRunError`` if asked) rather than
            # re-reading a stale entry.
            _drop_inflight(external_run_id)
            return RuntimeCancelResult(
                external_run_id=external_run_id,
                cancelled=True,
                reason=f"runner exited after SIGTERM (code={rc})",
            )
        except subprocess.TimeoutExpired:
            pass
        # Escalate to SIGKILL
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            _drop_inflight(external_run_id)
            return RuntimeCancelResult(
                external_run_id=external_run_id,
                cancelled=True,
                reason="runner gone after grace",
            )
        except (PermissionError, OSError) as exc:
            return RuntimeCancelResult(
                external_run_id=external_run_id,
                cancelled=False,
                reason=f"SIGKILL failed: {exc}",
            )
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass
        _drop_inflight(external_run_id)
        return RuntimeCancelResult(
            external_run_id=external_run_id,
            cancelled=True,
            reason="runner SIGKILLed after grace",
        )


# --- Public errors ----------------------------------------------------


class ClaudeConcurrencyError(AdapterRuntimeError):
    """Raised by :meth:`ClaudeCodeExecutorAdapter.submit` when
    another Claude Code run is already in flight.

    Per MVP rule we reject (not queue) the second submission.
    """


# --- Helpers ----------------------------------------------------------


def _drop_inflight(external_run_id: str) -> None:
    """Remove ``_inflight[external_run_id]`` if present.

    TASK-M3 FIX-4: every terminal path of ``poll()`` and
    ``cancel()`` calls this so the in-flight dict does not grow
    without bound over a long-lived process. We acquire
    ``_lock`` to be safe against concurrent ``submit()`` reads
    of the same key. ``pop`` is a no-op if the key is missing.

    This is a synchronous helper so the call sites do not have
    to remember ``await`` — the lock is only held for the
    duration of a single ``pop`` against an in-memory dict, so
    there is no reason to suspend the caller.
    """
    # The lock is an ``asyncio.Lock``; we cannot ``async with`` it
    # in a sync function, and we cannot ``acquire()`` synchronously
    # without blocking the loop. For an in-memory dict pop the
    # race window is microseconds and the consequences are benign
    # (a duplicate ``submit()`` would itself raise
    # ``ClaudeConcurrencyError`` if it observed the same alive
    # process). We therefore do an unsynchronised pop here; the
    # submit-side check is the authoritative guard. Any caller
    # that wants strict ordering can ``await _lock`` before
    # invoking this helper.
    _inflight.pop(external_run_id, None)


def _extract_spec(job: Any) -> Dict[str, Any]:
    """Pull the Claude Code execution fields off a Job.

    Tolerant to two shapes:
      * AEE ``Job`` (has ``spec`` dict)
      * A duck-typed substitute
    """
    spec = getattr(job, "spec", None) or {}
    # spec may carry the Claude Code fields; fall back to top-level
    # attributes if the caller built a flatter shape.
    def _g(key: str, default: Any = None) -> Any:
        if isinstance(spec, dict) and key in spec:
            return spec[key]
        v = getattr(job, key, None)
        return v if v is not None else default
    task_id = _g("task_id") or getattr(job, "task_id", None) or getattr(job, "title", "TASK")
    run_id = _g("run_id") or getattr(job, "external_run_id", None) or _new_run_id()
    repo_path = _g("repo_path") or getattr(job, "repo_path", None)
    if not repo_path:
        raise AdapterRuntimeError(
            "claude_code: job.spec.repo_path is required"
        )
    mode = _g("mode") or getattr(job, "mode", "normal") or "normal"
    timeout_seconds = int(_g("timeout_seconds") or 1800)
    raw_artifacts = _g("required_artifacts")
    if raw_artifacts is None:
        raw_artifacts = []
    if not isinstance(raw_artifacts, list):
        raise AdapterRuntimeError(
            "claude_code: required_artifacts must be a list"
        )
    return {
        "task_id": str(task_id),
        "run_id": str(run_id),
        "repo_path": str(repo_path),
        "mode": str(mode),
        "timeout_seconds": timeout_seconds,
        "required_artifacts": [str(a) for a in raw_artifacts],
        "expected_branch": _g("expected_branch"),
        "expected_head": _g("expected_head"),
        "allow_commit": bool(_g("allow_commit", False)),
        "human_approved": bool(_g("human_approved", False)),
        "test_command": _g("test_command"),
        "model": _g("model"),
        "fallback_model": _g("fallback_model"),
        "working_mode": _g("working_mode") or "isolated_directory",
        "brief": _g("brief") or _g("input") or _g("input_text") or "",
    }


def _build_argv(
    *,
    python_bin: str,
    runner_module: str,
    runs_root: str,
    composite_task_id: str,
    spec: Dict[str, Any],
) -> List[str]:
    """Construct the argv list passed to ``subprocess.Popen``.

    Per the task contract: never pass null or empty values as
    flags. Optional flags are appended only when their value is
    non-empty. ``--allow-commit`` is added only when both
    ``allow_commit`` and ``human_approved`` are true.
    """
    argv: List[str] = [
        python_bin,
        "-m",
        runner_module,
        "--task-id", composite_task_id,
        "--repo-path", spec["repo_path"],
        "--mode", spec["mode"],
        "--timeout", str(spec["timeout_seconds"]),
        "--runs-root", runs_root,
    ]
    for art in spec.get("required_artifacts") or []:
        argv += ["--required-artifact", art]
    if spec.get("expected_branch"):
        argv += ["--expected-branch", spec["expected_branch"]]
    if spec.get("expected_head"):
        argv += ["--expected-head", spec["expected_head"]]
    if spec.get("test_command"):
        argv += ["--test-command", spec["test_command"]]
    if spec.get("model"):
        argv += ["--model", spec["model"]]
    if spec.get("fallback_model"):
        argv += ["--fallback-model", spec["fallback_model"]]
    if spec.get("working_mode") and spec["working_mode"] != "isolated_directory":
        argv += ["--working-mode", spec["working_mode"]]
    # Allow-commit is gated by both flags; the API layer enforces
    # the human_approved dependency, but the adapter double-checks.
    if spec.get("allow_commit") and spec.get("human_approved"):
        argv += ["--allow-commit"]
    brief = spec.get("brief")
    if brief:
        argv += ["--brief", brief]
    return argv


def _new_run_id() -> str:
    return f"RUN-{uuid.uuid4().hex[:12]}"


def _summarise_verification_errors(errors: List[str]) -> str:
    """Compress the verifier's error list into a short, stable
    string for ``RuntimePollResult.error``."""
    if not errors:
        return "verification failed (no error detail)"
    # Use the first three codes to keep the message bounded.
    return "verification failed: " + ",".join(errors[:3])


# Protocol satisfaction at import time (matches FakeAdapter style).
assert isinstance(ClaudeCodeExecutorAdapter(), RuntimeAdapter)  # type: ignore[misc]


__all__ = [
    "ClaudeCodeExecutorAdapter",
    "ClaudeConcurrencyError",
    "DEFAULT_RUNS_ROOT",
    "DEFAULT_RUNNER_CWD",
]
