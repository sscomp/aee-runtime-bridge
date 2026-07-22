"""Synchronous Claude Code CLI runner for ``POST /runs/executor``.

This finalises the direct-CLI path: it wraps the existing
:class:`aee.adapters.claude_code_provider.ClaudeCodeProvider` (an
``ExecProvider``) and runs a single prompt to completion within a
bounded timeout, returning a structured :class:`CliRunResult` with the
real stdout / stderr / exit_code / timeout_state / cancel_state.

Design notes
------------
* **Reuse, not reimplementation.** Subprocess spawn, stdout/stderr
  drain, timeout-supervisor (SIGKILL on deadline) and cancel (SIGTERM
  then SIGKILL) are all handled by ``ClaudeCodeProvider``. This module
  only orchestrates the submit → poll-to-terminal loop and translates
  the ``ExecStatus`` vocabulary into the response envelope's status
  vocabulary.
* **No shell escape.** ``ClaudeCodeProvider`` uses
  ``asyncio.create_subprocess_exec`` with an arg list; the prompt is a
  positional argv element, never interpreted by a shell.
* **``--bare`` is OFF by default** (per host constraint: ``--bare`` can
  break OAuth-hosted Claude CLI sessions). Callers may opt in via
  config (``bare: true``) or the ``ClaudeCodeCliRunner(bare=True)``
  constructor. The runner exposes both modes for test coverage.
* **No silent fallback.** A spawn failure (binary missing) raises
  :class:`ProviderError`; the caller surfaces it as a real
  ``status=failed`` envelope with the error text — never a Hermes
  substitute.

Auth-bridge (TASK-AEE-CLAUDE-CODE-EXECUTOR-RECOVERY, 2026-07-22)
----------------------------------------------------------------
The AEE-7.1 ``claude_code_provider_shim.py`` proved that mirroring
``ANTHROPIC_AUTH_TOKEN`` -> ``ANTHROPIC_API_KEY`` in the worker env
is the minimal, correct fix for "Not logged in" exits on hosts where
the only available credential is the auth-token env var (the
Ollama-Cloud bearer). The shim applies the mirror for the
orchestrator path; the executor path (``POST /runs/executor``) did
NOT, because ``ClaudeCodeCliRunner.run()`` called
``ClaudeCodeProvider.submit()`` without ``env=``, so
``_filter_env(None)`` only forwarded ``_ALLOWED_ENV_VARS ∩ os.environ``
— and when the parent env lacked ``ANTHROPIC_API_KEY`` the worker
subprocess silently exited with ``Not logged in · Please run /login``
and exit_code 1.

The mirror is reproduced here (rather than imported from the shim)
to keep the executor path dependency-light and to preserve the
existing response envelope. The mirror is pure and never mutates
``os.environ``; the resulting dict is re-filtered by
``ClaudeCodeProvider._filter_env`` so unrelated keys are dropped.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from aee.adapters.claude_code_provider import ClaudeCodeProvider
from aee.adapters.exec_provider import (
    ExecStatus,
    ProviderError,
)


def _build_claude_env_mirror(parent_env: "Mapping[str, str]") -> "Dict[str, str]":
    """Return a copy of ``parent_env`` with the ANTHROPIC_API_KEY mirror applied.

    Mirrors ``ANTHROPIC_AUTH_TOKEN`` -> ``ANTHROPIC_API_KEY`` only when
    the latter is unset, so Claude CLI 2.1.216's env-based auth code
    path can read a credential even on hosts that only expose the
    Ollama-Cloud bearer token under ``ANTHROPIC_AUTH_TOKEN``. Pure:
    never mutates the input mapping. See the AEE-7.1 case study in
    ``aee/orchestrator/claude_code_provider_shim.py`` for the canonical
    implementation this reproduces.
    """
    out: Dict[str, str] = dict(parent_env)
    if "ANTHROPIC_API_KEY" in out:
        return out
    token = out.get("ANTHROPIC_AUTH_TOKEN")
    if token:
        out["ANTHROPIC_API_KEY"] = token
    return out


# Map ExecStatus -> (envelope status, timeout_state, cancel_state).
_STATUS_MAP = {
    ExecStatus.EXITED: ("completed", None, None),
    ExecStatus.FAILED: ("failed", None, None),
    ExecStatus.TIMED_OUT: ("timeout", "exceeded", None),
    ExecStatus.CANCELLED: ("cancelled", None, "requested"),
    ExecStatus.RUNNING: ("running", None, None),
    ExecStatus.PENDING: ("running", None, None),
}


@dataclass
class CliRunResult:
    """Outcome of a single Claude Code CLI run."""
    run_id: str
    status: str  # completed | failed | timeout | cancelled | running
    stdout: str
    stderr: str
    exit_code: Optional[int]
    timeout_state: Optional[str]  # "exceeded" | None
    cancel_state: Optional[str]   # "requested" | None
    error: Optional[str] = None
    artifact_paths: List[str] = field(default_factory=list)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    pid: Optional[int] = None
    bare: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "timeout_state": self.timeout_state,
            "cancel_state": self.cancel_state,
            "error": self.error,
            "artifact_paths": list(self.artifact_paths),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "pid": self.pid,
            "bare": self.bare,
        }


class ClaudeCodeCliRunner:
    """Run a single Claude Code CLI prompt to completion."""

    def __init__(
        self,
        *,
        binary: str = "/home/ubuntu/.local/bin/claude",
        max_turns: int = 50,
        output_format: str = "text",
        bare: bool = False,
        cancel_grace_seconds: float = 5.0,
        extra_cli_args: Optional[List[str]] = None,
    ) -> None:
        self._bare = bare
        self._provider = ClaudeCodeProvider(
            binary=binary,
            max_turns=max_turns,
            output_format=output_format,
            bare=bare,
            cancel_grace_seconds=cancel_grace_seconds,
            extra_cli_args=extra_cli_args,
        )

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "ClaudeCodeCliRunner":
        extra = cfg.get("extra_cli_args") or []
        return cls(
            binary=str(cfg.get("claude_cli_binary") or "/home/ubuntu/.local/bin/claude"),
            max_turns=int(cfg.get("max_turns") or 50),
            output_format=str(cfg.get("output_format") or "text"),
            bare=bool(cfg.get("bare", False)),
            extra_cli_args=[str(a) for a in extra] if extra else None,
        )

    @property
    def bare(self) -> bool:
        return self._bare

    async def run(
        self,
        *,
        prompt: str,
        cwd: Optional[str] = None,
        timeout_sec: Optional[int] = None,
        expected_artifacts: Optional[List[str]] = None,
        cancel_event: Optional[asyncio.Event] = None,
        run_id: Optional[str] = None,
    ) -> CliRunResult:
        """Submit the prompt and poll until terminal / cancelled / deadline."""
        rid = run_id or f"claude-cli-{uuid.uuid4().hex[:12]}"
        # Auth-bridge: mirror ANTHROPIC_AUTH_TOKEN -> ANTHROPIC_API_KEY so
        # the worker subprocess can authenticate even when the parent env
        # only carries the Ollama-Cloud bearer token. ``_filter_env`` then
        # re-applies the allow-list, dropping unrelated keys. (Recovery
        # TASK-AEE-CLAUDE-CODE-EXECUTOR-RECOVERY, 2026-07-22.)
        mirrored_env = _build_claude_env_mirror(os.environ)
        try:
            submit_res = await self._provider.submit(
                prompt=prompt,
                cwd=cwd,
                env=mirrored_env,
                timeout_seconds=timeout_sec,
                run_id=rid,
            )
        except ProviderError as exc:
            return CliRunResult(
                run_id=rid,
                status="failed",
                stdout="",
                stderr=str(exc),
                exit_code=None,
                timeout_state=None,
                cancel_state=None,
                error=str(exc),
                bare=self._bare,
            )
        started_at = submit_res.started_at
        pid = submit_res.pid

        # Hard deadline safeguard = requested timeout + a generous grace,
        # in case the provider's own supervisor is slow to reap. We never
        # rely solely on this; the provider's supervisor is the primary
        # timeout authority and reports TIMED_OUT.
        grace = float(self._provider._cancel_grace_seconds) + 5.0
        hard_deadline = (time.time() + timeout_sec + grace) if timeout_sec else None

        # P1.1 write-side activation: persist a heartbeat on every poll
        # iteration whose wall-clock gap exceeds the configured cadence.
        # The heartbeat writer is best-effort: a DB error is swallowed so
        # it never breaks the dispatch. The writer skips terminal / missing
        # rows (work-order §5). Heartbeats are emitted HERE — in the
        # executor lifecycle loop — NEVER by GET /runs (work-order §4).
        from dispatcher.db import get_conn as _get_heartbeat_conn
        from dispatcher.executor_runs import (
            LIFECYCLE_STEPS as _LIFECYCLE_STEPS,
            get_heartbeat_interval_seconds as _get_hb_interval,
            update_heartbeat as _update_hb,
            upsert_run as _seed_run,
        )
        _hb_interval = _get_hb_interval()
        _last_hb_ts = 0.0
        _hb_phase = "running"

        # P1.1: seed the executor_runs row BEFORE the poll loop so the
        # live heartbeats below find an existing non-terminal row to
        # update (work-order §1 — "wire executor lifecycle persistence
        # so live runs update persisted observability fields"). The
        # terminal write in ``_persist_executor_run`` (app.py) is the
        # canonical completion path; this seed is the row-creation write
        # for the live-run observation window. Best-effort: a DB error
        # is swallowed so it never breaks the dispatch.
        try:
            from datetime import datetime as _dt, timezone as _tz
            _seed_ts = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            _seed_conn = _get_heartbeat_conn()
            _seed_run(
                _seed_conn,
                run_id=rid,
                requested_executor=None,
                selected_executor="claude-code-cli",
                status=submit_res.status or "running",
                progress=0.0,
                last_heartbeat_at=_seed_ts,
                current_step="starting",
                phase=(
                    "queued" if (submit_res.status or "") in {"queued", "pending"}
                    else "running"
                ),
            )
        except Exception:  # pragma: no cover - best-effort seed
            pass

        def _emit_heartbeat(step: str) -> None:
            nonlocal _last_hb_ts
            if step not in _LIFECYCLE_STEPS:
                return  # defensive — caller should only pass canonical steps
            now_ts = time.time()
            if (now_ts - _last_hb_ts) < _hb_interval:
                return  # cadence gate
            _last_hb_ts = now_ts
            try:
                conn = _get_heartbeat_conn()
                _update_hb(conn, run_id=rid, current_step=step, phase=_hb_phase)
            except Exception:  # pragma: no cover - best-effort, never break run
                pass

        last_status = submit_res.status
        cancel_state: Optional[str] = None
        # Initial heartbeat: the run is now running (or queued if the
        # provider reports PENDING on submit).
        _emit_heartbeat("starting")
        while True:
            poll_res = await self._provider.poll(rid)
            last_status = poll_res.status
            if poll_res.is_terminal:
                break
            if cancel_event is not None and cancel_event.is_set():
                await self._provider.cancel(rid)
                cancel_state = "requested"
                # One more poll to pick up the CANCELLED status.
                poll_res = await self._provider.poll(rid)
                last_status = poll_res.status
                break
            if hard_deadline is not None and time.time() > hard_deadline:
                # Safeguard: force a cancel if the provider hasn't reaped.
                await self._provider.cancel(rid)
                poll_res = await self._provider.poll(rid)
                last_status = poll_res.status
                break
            # P1.1: emit a running heartbeat on every poll iteration,
            # cadence-gated by ``_hb_interval``. The step is "running"
            # (the executor is in the poll loop, no specific sub-phase).
            _emit_heartbeat("running")
            await asyncio.sleep(0.1)

        # Read captured streams (read_stdout/read_stderr return BytesIO).
        try:
            stdout_bytes = self._provider.read_stdout(rid).read()
        except Exception:  # pragma: no cover - defensive
            stdout_bytes = b""
        try:
            stderr_bytes = self._provider.read_stderr(rid).read()
        except Exception:  # pragma: no cover - defensive
            stderr_bytes = b""
        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")
        exit_code = self._provider.exit_code(rid)

        status, timeout_state, mapped_cancel = _STATUS_MAP.get(
            last_status, ("failed", None, None)
        )
        # If we initiated the cancel ourselves, prefer the explicit state.
        if cancel_state == "requested" and status not in ("cancelled",):
            status = "cancelled"
            mapped_cancel = "requested"

        # Artifact paths that actually exist on disk after the run.
        import os as _os
        artifact_paths = [p for p in (expected_artifacts or []) if _os.path.exists(p)]

        return CliRunResult(
            run_id=rid,
            status=status,
            stdout=stdout_text,
            stderr=stderr_text,
            exit_code=exit_code,
            timeout_state=timeout_state,
            cancel_state=mapped_cancel,
            error=(stderr_text.strip() or None) if status in ("failed",) else None,
            artifact_paths=artifact_paths,
            started_at=started_at,
            finished_at=_now_iso(),
            pid=pid,
            bare=self._bare,
        )


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = ["ClaudeCodeCliRunner", "CliRunResult"]