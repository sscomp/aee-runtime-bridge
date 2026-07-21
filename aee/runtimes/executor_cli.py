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
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from aee.adapters.claude_code_provider import ClaudeCodeProvider
from aee.adapters.exec_provider import (
    ExecStatus,
    ProviderError,
)


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
        try:
            submit_res = await self._provider.submit(
                prompt=prompt,
                cwd=cwd,
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

        last_status = submit_res.status
        cancel_state: Optional[str] = None
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