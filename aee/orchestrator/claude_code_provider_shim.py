"""AEE-7.1 ClaudeCodeExecProvider — ``Provider`` wrapper over ``ClaudeCodeProvider``.

The AEE-6 ``ClaudeCodeProvider`` (in ``aee/adapters/``) is the
*subprocess* provider; it owns the ``claude`` CLI invocation,
timeout supervisor, stdout/stderr drain, and SIGTERM/SIGKILL
cancel. AEE-7.1 introduces a separate ``Provider`` Protocol
(in ``aee/orchestrator/provider.py``); this module adapts the
``ClaudeCodeProvider`` to that Protocol so the orchestrator can
pick ``runtime_type="claude_code"`` the same way it picks
``"hermes"``.

Translation
-----------
The ``ClaudeCodeProvider`` returns
``ExecSubmitResult`` / ``ExecPollResult`` / ``ExecCancelResult``
— the AEE-6 *subprocess* vocabulary. The ``Provider`` Protocol
returns ``ProviderRun`` / ``ProviderStatusResult`` /
``ProviderCancelResult`` — the AEE-7 *transport-neutral*
vocabulary. The wrapper translates ``ExecStatus`` to
``ProviderStatus`` and re-emits ``exit_code`` / ``stderr``
truncation on the orchestrator side.

Security
--------
The AEE-6 ``ClaudeCodeProvider._ALLOWED_ENV_VARS`` allowlist is
preserved. The shim adds an **auth-mirror** step before the
subprocess spawn: if the parent env has ``ANTHROPIC_AUTH_TOKEN`` but
no ``ANTHROPIC_API_KEY``, the shim injects a redacted mirror so
Claude CLI 2.1.206's normal code path (without ``--bare``) can
authenticate against the Ollama-Cloud proxy that sits behind
``ANTHROPIC_BASE_URL``. The original parent env is never mutated;
the mirror is computed in a fresh dict that is then passed through
the AEE-6 allow-list.

AEE-7.1 also flags the use of ``--bare`` in live provider configs as
a likely-bad combination: ``--bare`` STRICTLY reads
``ANTHROPIC_API_KEY`` (not ``ANTHROPIC_AUTH_TOKEN``) and silently
refuses keychain / OAuth. We default to ``bare=False`` in the shim's
constructor and emit a log warning if a caller forces ``bare=True``
while ``ANTHROPIC_API_KEY`` is not in the parent env.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Mapping, Optional

from aee.adapters.claude_code_provider import ClaudeCodeProvider
from aee.adapters.exec_provider import (
    ExecCancelResult as ClaudeExecCancelResult,
    ExecPollResult as ClaudeExecPollResult,
    ExecStatus as ClaudeExecStatus,
    ExecSubmitResult as ClaudeExecSubmitResult,
    ProviderError as ClaudeProviderError,
    ProviderExitError as ClaudeProviderExitError,
    ProviderNotFoundError as ClaudeProviderNotFoundError,
    ProviderTimeoutError as ClaudeProviderTimeoutError,
)
from aee.runtimes.models import RuntimeDescriptor, TaskRuntimeRequirements

from .provider import (
    Provider,
    ProviderCancelResult,
    ProviderError,
    ProviderExitError,
    ProviderNotFoundError,
    ProviderRun,
    ProviderStatus,
    ProviderStatusResult,
    ProviderSubmitError,
    ProviderTimeoutError,
)


log = logging.getLogger("aee.orchestrator.claude_code")


# AEE-7.1 AUTH RESCUE — env mirror.
#
# On the Abacus-AI host, the only auth credential the Claude CLI
# subprocess can read is ``ANTHROPIC_API_KEY`` (the variable it
# honours in its default ``bare=False`` code path). The parent
# process stores the Ollama-Cloud bearer token under
# ``ANTHROPIC_AUTH_TOKEN`` instead. We bridge the two by mirroring
# the token into ``ANTHROPIC_API_KEY`` *only if* the latter is not
# already set; the original env is never mutated. The resulting
# dict is then re-filtered by the underlying
# ``ClaudeCodeProvider._filter_env`` allow-list, so unrelated keys
# (e.g. ``OLLAMA_API_KEY``, ``DATABASE_URL``, ``SSH_AUTH_SOCK``) are
# dropped — only the allow-list makes it into the subprocess env.
#
# We never log the token value. The mirror dict is intended to be
# ephemeral; callers should not retain it past the ``submit()`` call.
def _build_claude_env_mirror(parent_env: "Mapping[str, str]") -> "Dict[str, str]":
    """Return a copy of ``parent_env`` with the ANTHROPIC_API_KEY mirror applied.

    Mirroring rules
    ---------------
    * If ``parent_env`` already has ``ANTHROPIC_API_KEY`` set, return
      a shallow copy unchanged.
    * Else, if ``parent_env`` has ``ANTHROPIC_AUTH_TOKEN`` set,
      return a copy with ``ANTHROPIC_API_KEY`` set to that same value.
    * Else, return a shallow copy unchanged (no auth available; the
      subprocess will exit "Not logged in" and the caller will see
      the error).

    The function is pure: it does not mutate ``parent_env``.
    """
    out: Dict[str, str] = dict(parent_env)
    if "ANTHROPIC_API_KEY" in out:
        return out
    token = out.get("ANTHROPIC_AUTH_TOKEN")
    if token:
        out["ANTHROPIC_API_KEY"] = token
    return out


# stderr truncation. The dispatcher's ``error_message`` column is
# bounded (500 chars); we keep the orchestrator result on a
# similarly tight budget.
_STDERR_TAIL_BYTES = 4 * 1024  # 4 KiB
_STDERR_DECODE_ERRORS = "replace"


class ClaudeCodeExecProvider:
    """Wrap the AEE-6 ``ClaudeCodeProvider`` as a :class:`Provider`.

    The AEE-6 ``ClaudeCodeProvider`` is a fully self-contained
    subprocess manager; the wrapper is a thin shim that translates
    result shapes. Per-run state lives in
    ``ClaudeCodeProvider._runs``; the wrapper's
    ``ProviderRun.metadata`` only carries the descriptor and the
    requirements.
    """

    name = "claude_code_exec"
    runtime_type = "claude_code"

    def __init__(
        self,
        *,
        descriptor: RuntimeDescriptor,
        provider: Optional[ClaudeCodeProvider] = None,
        binary: str = "claude",
        max_turns: int = 1,
        output_format: str = "text",
        # AEE-7.1 — override the underlying provider's ``bare`` flag.
        # Default ``False`` (hermetic off) so Claude CLI's normal code
        # path reads ``ANTHROPIC_API_KEY`` (which the shim mirrors
        # from ``ANTHROPIC_AUTH_TOKEN`` when needed). Set ``True`` only
        # if you are sure ``ANTHROPIC_API_KEY`` is already in the
        # parent env, otherwise the subprocess will exit with
        # "Not logged in".
        bare: bool = False,
    ) -> None:
        self._descriptor = descriptor
        # Allow callers to inject a custom provider (tests use
        # FakeClaudeCodeProvider, which satisfies the same
        # ExecProvider protocol).
        self._bare = bool(bare)
        if provider is not None:
            self._provider = provider
        else:
            # AEE-7.1 — if bare=True is forced but ANTHROPIC_API_KEY
            # is not in the parent env, log a warning so the operator
            # is not surprised by a "Not logged in" exit.
            if self._bare and not os.environ.get("ANTHROPIC_API_KEY"):
                log.warning(
                    "ClaudeCodeExecProvider: bare=True requested but "
                    "ANTHROPIC_API_KEY is not in the parent env; "
                    "Claude CLI 2.1.206 will refuse to authenticate "
                    "(it strictly reads ANTHROPIC_API_KEY in --bare mode). "
                    "Mirror ANTHROPIC_AUTH_TOKEN -> ANTHROPIC_API_KEY "
                    "in the parent env, or set bare=False."
                )
            self._provider = ClaudeCodeProvider(
                binary=binary,
                max_turns=max_turns,
                output_format=output_format,
                bare=self._bare,
            )

    @property
    def descriptor(self) -> RuntimeDescriptor:
        return self._descriptor

    # ------------------------------------------------------------------
    # Provider protocol
    # ------------------------------------------------------------------

    async def submit(
        self,
        *,
        prompt: str,
        requirements: TaskRuntimeRequirements,
        repo: Any,
        run_id: Optional[str] = None,
    ) -> ProviderRun:
        # Resolve the workdir. The AEE-7.1 contract is:
        # 1. If ``requirements.repo_root`` is set, use it (AEE-7.2
        #    will enforce; AEE-7.1 only configures).
        # 2. Else, fall back to the descriptor's labels (if any).
        # 3. Else, the provider's default_cwd.
        workdir: Optional[str] = None
        rr_root = getattr(requirements, "repo_root", None)
        if isinstance(rr_root, str) and rr_root.strip():
            workdir = rr_root.strip()
        if not workdir:
            label_root = (self._descriptor.labels or {}).get("default_workdir")
            if isinstance(label_root, str) and label_root.strip():
                workdir = label_root.strip()
        if not workdir:
            workdir = None  # provider default

        # Compute timeout_seconds from the descriptor's limits.
        timeout_seconds: Optional[int] = None
        limits = self._descriptor.limits
        if limits is not None and getattr(limits, "timeout_seconds", None):
            try:
                timeout_seconds = int(limits.timeout_seconds)
            except (TypeError, ValueError):
                timeout_seconds = None

        try:
            res: ClaudeExecSubmitResult = await self._provider.submit(
                prompt=prompt,
                cwd=workdir,
                # AEE-7.1 — env-mirror. Pass a fresh dict that
                # includes the ANTHROPIC_API_KEY mirror when the
                # parent has ANTHROPIC_AUTH_TOKEN. The underlying
                # ClaudeCodeProvider re-applies the allow-list, so
                # any unrelated keys here are dropped.
                env=_build_claude_env_mirror(os.environ),
                timeout_seconds=timeout_seconds,
                run_id=run_id,
            )
        except ClaudeProviderNotFoundError as exc:
            raise ProviderNotFoundError(str(exc), cause=exc) from exc
        except ClaudeProviderTimeoutError as exc:
            raise ProviderTimeoutError(str(exc), cause=exc) from exc
        except ClaudeProviderExitError as exc:
            raise ProviderExitError(str(exc), cause=exc) from exc
        except ClaudeProviderError as exc:
            raise ProviderSubmitError(str(exc), cause=exc) from exc

        return ProviderRun(
            external_run_id=res.external_run_id,
            provider_name=self.name,
            runtime_type=self._descriptor.runtime_type,
            started_at=res.started_at,
            metadata={
                "pid": res.pid,
                "cwd": workdir or os.getcwd(),
                "timeout_seconds": timeout_seconds,
            },
        )

    async def poll(self, run: ProviderRun) -> ProviderStatusResult:
        try:
            res: ClaudeExecPollResult = await self._provider.poll(run.external_run_id)
        except ClaudeProviderTimeoutError as exc:
            return ProviderStatusResult(
                external_run_id=run.external_run_id,
                status=ProviderStatus.TIMEOUT,
                is_terminal=True,
                error=str(exc),
            )
        except ClaudeProviderExitError as exc:
            return ProviderStatusResult(
                external_run_id=run.external_run_id,
                status=ProviderStatus.FAILED,
                is_terminal=True,
                error=str(exc),
            )
        except ClaudeProviderError as exc:
            raise ProviderError(
                f"claude code provider poll failed: {exc}", cause=exc
            ) from exc
        return self._translate_poll(res)

    async def cancel(self, run: ProviderRun) -> ProviderCancelResult:
        try:
            res: ClaudeExecCancelResult = await self._provider.cancel(
                run.external_run_id
            )
        except ClaudeProviderError as exc:
            log.warning(
                "claude_code_provider.cancel: %s: %s", type(exc).__name__, exc
            )
            return ProviderCancelResult(
                external_run_id=run.external_run_id,
                cancelled=False,
                reason=f"{type(exc).__name__}: {exc}",
            )
        return ProviderCancelResult(
            external_run_id=res.external_run_id,
            cancelled=bool(res.cancelled),
            reason=res.reason or "",
        )

    def artifacts_dir(self, run: ProviderRun) -> Optional[str]:
        """Return the worker's cwd (the AEE-6 contract)."""
        return run.metadata.get("cwd") if run.metadata else None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _read_stderr_tail(self, external_run_id: str) -> str:
        """Read the last ``_STDERR_TAIL_BYTES`` from the worker's stderr."""
        stream = self._provider.read_stderr(external_run_id)
        try:
            stream.seek(0, 2)
            end = stream.tell()
            start = max(0, end - _STDERR_TAIL_BYTES)
            stream.seek(start)
            data = stream.read(end - start)
            return data.decode("utf-8", errors=_STDERR_DECODE_ERRORS)
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def _translate_poll(
        self, res: ClaudeExecPollResult
    ) -> ProviderStatusResult:
        """Translate ``ExecStatus`` to ``ProviderStatus``."""
        raw_status = res.status
        if isinstance(raw_status, ClaudeExecStatus):
            raw_str = raw_status.value
        else:
            raw_str = str(raw_status)
        status_map = {
            ClaudeExecStatus.PENDING.value: ProviderStatus.QUEUED,
            ClaudeExecStatus.RUNNING.value: ProviderStatus.RUNNING,
            ClaudeExecStatus.EXITED.value: ProviderStatus.COMPLETED,
            ClaudeExecStatus.CANCELLED.value: ProviderStatus.CANCELLED,
            ClaudeExecStatus.TIMED_OUT.value: ProviderStatus.TIMEOUT,
            ClaudeExecStatus.FAILED.value: ProviderStatus.FAILED,
        }
        norm = status_map.get(raw_str, ProviderStatus.RUNNING)
        error: Optional[str] = None
        if res.error:
            error = res.error[:500]
        if error is None and res.stderr_bytes:
            try:
                tail = self._read_stderr_tail(res.external_run_id)
                if tail:
                    error = tail[-500:]
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "claude_code_provider: read_stderr failed: %s", exc
                )
        return ProviderStatusResult(
            external_run_id=res.external_run_id,
            status=norm,
            is_terminal=norm.is_terminal,
            output=None,
            error=error,
            exit_code=res.exit_code,
            raw={"exec_status": raw_str, "exit_code": res.exit_code},
        )


def _translate_poll(
    res: ClaudeExecPollResult,
) -> ProviderStatusResult:
    """Translate ``ExecStatus`` to ``ProviderStatus`` (legacy entry point)."""
    return _LegacyCompatClaudeCodeProvider._translate_poll_static(res)


class _LegacyCompatClaudeCodeProvider:
    """Back-compat helper: the legacy module-level
    ``_translate_poll`` is preserved as a static-style shim so any
    test that imported it directly still works.
    """

    @staticmethod
    def _translate_poll_static(
        res: ClaudeExecPollResult,
    ) -> ProviderStatusResult:
        raw_status = res.status
        if isinstance(raw_status, ClaudeExecStatus):
            raw_str = raw_status.value
        else:
            raw_str = str(raw_status)
        status_map = {
            ClaudeExecStatus.PENDING.value: ProviderStatus.QUEUED,
            ClaudeExecStatus.RUNNING.value: ProviderStatus.RUNNING,
            ClaudeExecStatus.EXITED.value: ProviderStatus.COMPLETED,
            ClaudeExecStatus.CANCELLED.value: ProviderStatus.CANCELLED,
            ClaudeExecStatus.TIMED_OUT.value: ProviderStatus.TIMEOUT,
            ClaudeExecStatus.FAILED.value: ProviderStatus.FAILED,
        }
        norm = status_map.get(raw_str, ProviderStatus.RUNNING)
        error: Optional[str] = res.error[:500] if res.error else None
        return ProviderStatusResult(
            external_run_id=res.external_run_id,
            status=norm,
            is_terminal=norm.is_terminal,
            output=None,
            error=error,
            exit_code=res.exit_code,
            raw={"exec_status": raw_str, "exit_code": res.exit_code},
        )


__all__ = ["ClaudeCodeExecProvider"]
