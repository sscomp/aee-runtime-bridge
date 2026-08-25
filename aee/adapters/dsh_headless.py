"""DshHeadlessAdapter — AEE ``RuntimeAdapter`` that shells out to the local
DeepSeek Harness (DSH) CLI in its ``headless`` profile.

This adapter is the explicit-opt-in seam introduced by the AEE-DeepSeek
Harness compatibility audit (``/workspace/AEE_DEEPSEEK_HARNESS_COMPATIBILITY.md``).
It is **not** the production default executor; Claude Code CLI keeps
that role until a separate A/B work order promotes this adapter
(recommended next task: ``AEE DSH Executor Routing A/B Validation``).

DSH headless semantics (verified against ``apps/cli/lib/bin.js`` and
``packages/bundle/headless/README.md`` at v0.1.0-rc.7):

* argv shape — ``node <cli> --profile headless <task...>``. The
  launcher parses only ``--profile`` / ``--patch`` / ``--dump-config``
  itself; everything after its flags passes verbatim to the booted
  profile's app.
* stdout — on success, the **last non-empty assistant message** of
  the run, terminated by a newline.
* stderr — empty on success; on a terminal error, the error ``code``
  and ``message`` (one line).
* exit code — ``0`` iff ``turn/end completed``; otherwise ``1`` (the
  headless bundle's ``headless-runner`` maps other end states to a
  non-zero exit through the launcher-owned ``ctx.appExit`` hook).

There is no JSON envelope in this profile, so we use the
"final assistant text" plain-stdout fallback documented in
``aee/adapters/base.py``. We deliberately do NOT claim structured
output support.

Configuration
-------------
All knobs are env-driven, mirroring the conventions used by
``ClaudeCliAdapter``:

* ``AEE_DSH_NODE_BIN``  — Node binary (default: ``node`` from PATH).
* ``AEE_DSH_CLI_PATH``  — DSH CLI entrypoint (default:
  ``/workspace/deepseek-harness/apps/cli/lib/bin.js``).
* ``AEE_DSH_PROFILE``   — profile name (default: ``headless``).
* ``AEE_DSH_TIMEOUT_SEC`` — per-run wall-clock budget in seconds
  (default: 1800 = 30 min).
* ``AEE_DSH_RUNS_ROOT`` — per-run log directory (default:
  ``/tmp/aee-dsh-headless-runs``).

The adapter also forwards a tight allow-list of environment variables
to the DSH child so the bridge's own secrets do not leak into the
worker (mirrors ``ClaudeCliAdapter._child_env``). Provider credentials
relevant to DSH (``DEEPSEEK_API_KEY``, ``DEEPSEEK_BASE_URL``) are part
of that allow-list; we additionally detect their absence so the
adapter surfaces the well-known ``MISSING_CREDENTIAL`` failure mode as
a deterministic error rather than a silent pass.

Process execution safety
------------------------
* ``shell=False`` is mandatory; argv is built as a list and the task
  text is a single argv entry.
* No string interpolation, no shell-quoting heuristics.
* stdout and stderr are captured through ``asyncio.create_subprocess_exec``
  and bounded by ``AEE_DSH_TIMEOUT_SEC`` via ``asyncio.wait_for``.
* Exit code is captured and mapped to a terminal ``RuntimePollResult``.
* Cancellation: there is no async-poll pattern here (headless is a
  synchronous one-shot CLI); ``cancel()`` reports
  ``cancelled=False`` with an explanatory reason once the process
  has exited.

Security boundary
-----------------
* No API keys, no Telegram tokens, no SSH material is ever written to
  the run logs, error messages, or returned payloads.
* Diagnostic messages about credentials report only
  ``<VARIABLE_NAME>: present|absent`` — never the value.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from aee.adapters.base import (
    RuntimeAdapter,
    RuntimeCancelResult,
    RuntimeError,
    RuntimePollResult,
    RuntimeSubmitResult,
    UnknownExternalRunError,
)


# Status vocabulary we surface to AEE. Mirrors ClaudeCliAdapter.
_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "timeout"}


# Defaults — all overridable via env. The CLI default points at the
# installed bundle on this host; the node default defers to PATH.
DEFAULT_NODE_BIN = "node"
DEFAULT_CLI_PATH = "/workspace/deepseek-harness/apps/cli/lib/bin.js"
DEFAULT_PROFILE = "headless"
DEFAULT_TIMEOUT_SEC = 60 * 30            # 30 minutes
DEFAULT_RUNS_ROOT = "/tmp/aee-dsh-headless-runs"


# Substring we look for in stderr / stdout to recognise the canonical
# DSH "no provider key" error. The headless runner reports the error
# code from the LLM capability; the exact wording may evolve but the
# sentinel is stable enough for deterministic mapping today.
#
# Per the P0 bridge work order §9 + compatibility audit §2, the live
# upstream message on this host is:
#   ``dsh: MISSING_CREDENTIAL: llm-pi-ai: no credential for provider
#     route "ollama-cloud"; its profile resolves OLLAMA_API_KEY,
#     which is not set``
# We include ``OLLAMA_API_KEY`` and ``no credential`` on the
# sentinel list so a DSH that rephrases the upstream message (or
# rotates the credential name) still lands on the credential branch.
_MISSING_CREDENTIAL_SENTINELS = (
    "OLLAMA_API_KEY",
    "DEEPSEEK_API_KEY",
    "MISSING_CREDENTIAL",
    "missing credential",
    "no credential",
    "missing_api_key",
    "no api key",
)


@dataclass
class _RunState:
    """In-memory bookkeeping for a single DSH subprocess run.

    The headless profile is a one-shot CLI: by the time ``submit()``
    returns, the child has exited and we already know the terminal
    status. We keep the state around so a later ``poll()`` returns the
    same terminal answer (idempotent) and so ``cancel()`` can answer
    coherently.
    """

    external_run_id: str
    argv: list[str]
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    final_status: str = "completed"
    final_output: Optional[str] = None
    final_error: Optional[str] = None
    exit_code: Optional[int] = None
    task_id: Optional[str] = None


class DshHeadlessAdapter:
    """AEE ``RuntimeAdapter`` that drives DeepSeek Harness headless mode.

    Registered under the executor name ``dsh-headless``; explicit
    opt-in only. The default executor in AEE remains ``claude_cli``
    (or ``hermes`` on Hermes M2 hosts); this adapter is wired
    separately through ``AdapterRegistry.register()`` or a future
    bootstrap step.
    """

    name = "dsh-headless"
    runtime_type = "dsh_headless"

    def __init__(
        self,
        *,
        node_bin: Optional[str] = None,
        cli_path: Optional[str] = None,
        profile: Optional[str] = None,
        runs_root: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
    ) -> None:
        self._node_bin = (
            node_bin
            or os.getenv("AEE_DSH_NODE_BIN")
            or DEFAULT_NODE_BIN
        )
        self._cli_path = (
            cli_path
            or os.getenv("AEE_DSH_CLI_PATH")
            or DEFAULT_CLI_PATH
        )
        self._profile = (
            profile
            or os.getenv("AEE_DSH_PROFILE")
            or DEFAULT_PROFILE
        )
        self._runs_root = Path(
            runs_root
            or os.getenv("AEE_DSH_RUNS_ROOT")
            or DEFAULT_RUNS_ROOT
        ).resolve()
        try:
            self._timeout_seconds = int(
                timeout_seconds
                if timeout_seconds is not None
                else (os.getenv("AEE_DSH_TIMEOUT_SEC") or DEFAULT_TIMEOUT_SEC)
            )
        except (TypeError, ValueError):
            self._timeout_seconds = DEFAULT_TIMEOUT_SEC
        # Per-process state. The headless bundle is one-shot, so the
        # dict only ever grows for runs the watcher's reconciler
        # inspects after ``submit()``; entries are immutable post-
        # terminal, which is why we don't bother with a lock.
        self._runs: Dict[str, _RunState] = {}

    # ------------------------------------------------------------------
    # Configuration diagnostics (no secrets)
    # ------------------------------------------------------------------

    def _credential_presence(self) -> Dict[str, str]:
        """Return ``{var: 'present'|'absent'}`` for the provider creds.

        Only the *presence* of a value is reported — the value itself
        is never returned or logged. This is the same pattern the
        compatibility audit recommended (and the same one
        ``ClaudeCliAdapter`` already uses for ``ANTHROPIC_API_KEY``).

        The bridge on this host reaches Ollama Cloud via DSH's
        ``llm-pi-ai`` provider route ``"ollama-cloud"``; the live
        ``MISSING_CREDENTIAL`` upstream message names ``OLLAMA_API_KEY``
        as the env var DSH wants (compatibility audit §2 + DSH
        v0.1.0-rc.7). ``DEEPSEEK_API_KEY`` / ``DEEPSEEK_BASE_URL``
        are kept in the report for forward-compat: a future DSH
        profile that points at DeepSeek's own API will surface
        ``DEEPSEEK_API_KEY: present|absent`` so the operator can
        see the *current* env state without re-deriving it.
        """
        return {
            "OLLAMA_API_KEY": "present" if os.getenv("OLLAMA_API_KEY") else "absent",
            "DEEPSEEK_API_KEY": "present" if os.getenv("DEEPSEEK_API_KEY") else "absent",
            "DEEPSEEK_BASE_URL": "present" if os.getenv("DEEPSEEK_BASE_URL") else "absent",
        }

    # ------------------------------------------------------------------
    # Prompt + argv construction
    # ------------------------------------------------------------------

    @staticmethod
    def _build_prompt(job: Any) -> str:
        """Render an AEE ``Job`` into the single task string the headless
        bundle consumes.

        The headless profile submits the task as one ordinary user
        message. We mirror the ClaudeCliAdapter shape: ``title`` is a
        short context header, ``input`` is the body, with ``input_text``
        as the legacy alias.
        """
        title = (getattr(job, "title", "") or "").strip()
        body = (getattr(job, "input", "") or "").strip()
        if not body:
            body = (getattr(job, "input_text", "") or "").strip()
        if title and body:
            return f"# {title}\n\n{body}"
        return body or title

    def _build_argv(self, task: str) -> list[str]:
        """Build the argv for ``node <cli> --profile <profile> <task>``.

        No shell interpolation. ``task`` is a single argv entry — the
        Node child receives it as one string, identical to what
        ``args.ts`` documents (``task...`` is variadic, words joined by
        spaces; passing one string is the documented happy path).
        """
        argv: list[str] = [self._node_bin, self._cli_path]
        argv.extend(["--profile", self._profile])
        argv.append(task)
        return argv

    def _child_env(self) -> Dict[str, str]:
        """Build a clean env for the DSH child.

        We forward a tight allow-list so we do not leak bridge secrets
        (``BRIDGE_API_KEY``, ``TELEGRAM_BOT_TOKEN``, etc.) into the
        worker. DSH-specific provider credentials are included so a
        well-configured host can drive the headless runner end-to-end.

        Per the P0 bridge work order §9, this host's DSH profile
        routes to ``ollama-cloud`` and resolves ``OLLAMA_API_KEY``;
        ``DEEPSEEK_API_KEY`` / ``DEEPSEEK_BASE_URL`` remain on the
        allow-list so a future DSH profile pointing at DeepSeek's
        own API is also covered without re-editing this file. The
        allow-list is opt-in by name — secrets never appear in any
        log, error message, or returned payload, only their
        ``present|absent`` status is surfaced (see
        :func:`_credential_presence`).
        """
        keep = {
            "PATH", "HOME", "LANG", "LC_ALL", "TZ",
            "OLLAMA_API_KEY",
            "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL",
            "DSH_HOME",
        }
        return {k: v for k, v in os.environ.items() if k in keep and v}

    def _ensure_runs_root(self) -> None:
        self._runs_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Output classification
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_failure(stderr: str, stdout: str) -> Tuple[str, str]:
        """Classify a non-zero DSH exit into an AEE failure kind.

        Returns ``(kind, message)`` where ``kind`` is one of
        ``missing_credential``, ``execution_failed``,
        ``malformed_output``, or ``unknown``. ``message`` is a safe
        diagnostic — it never contains the credential value.
        """
        haystack = f"{stderr}\n{stdout}".lower()
        for sentinel in _MISSING_CREDENTIAL_SENTINELS:
            if sentinel.lower() in haystack:
                return (
                    "missing_credential",
                    "DSH reported a missing provider credential "
                    "(DEEPSEEK_API_KEY absent or unset); see AEE_DSH_HEADLESS "
                    "diagnostics for VARIABLE_NAME presence.",
                )
        if stderr.strip():
            return ("execution_failed", stderr.strip().splitlines()[-1])
        if stdout.strip():
            return ("execution_failed", stdout.strip().splitlines()[-1])
        return ("unknown", "DSH exited non-zero with no diagnostic output")

    # ------------------------------------------------------------------
    # RuntimeAdapter protocol
    # ------------------------------------------------------------------

    async def health(self) -> Dict[str, Any]:
        """Best-effort probe used by the bridge ``/health`` endpoint.

        We do not run a real headless task here (that would require a
        valid provider key); we only verify the CLI is on disk and
        accepts ``--help``.
        """
        cli = Path(self._cli_path)
        if not cli.is_file():
            return {
                "ok": False,
                "error": f"dsh cli not found at {self._cli_path!r}",
            }
        try:
            proc = await asyncio.create_subprocess_exec(
                self._node_bin, self._cli_path,
                "--profile", self._profile, "--help",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            except asyncio.TimeoutError:
                proc.kill()
                return {"ok": False, "error": "dsh --help timed out"}
            if proc.returncode != 0:
                return {
                    "ok": False,
                    "error": f"dsh --help exit={proc.returncode}",
                }
            return {
                "ok": True,
                "version_probe": "headless --help ok",
                "credentials": self._credential_presence(),
            }
        except FileNotFoundError as exc:
            return {
                "ok": False,
                "error": f"node binary not found ({self._node_bin!r}): {exc}",
            }
        except Exception as exc:  # pragma: no cover (defensive)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def submit(self, job: Any) -> RuntimeSubmitResult:
        """Spawn one DSH headless run, wait for it to finish, return a
        terminal ``RuntimeSubmitResult``.

        The headless bundle is one-shot: by the time we return, the
        child has either printed the final assistant text (exit 0) or
        a terminal error (exit 1). We persist the full status into
        ``_runs`` so the watcher's later ``poll()`` calls are cheap and
        idempotent.
        """
        # --- Validation: empty / missing inputs ------------------------
        task = self._build_prompt(job)
        if not task:
            raise RuntimeError("dsh-headless: empty task (Job has no input/title)")

        cli = Path(self._cli_path)
        if not cli.is_file():
            raise RuntimeError(
                f"dsh-headless: CLI not found at {self._cli_path!r}; "
                "set AEE_DSH_CLI_PATH or install DeepSeek Harness"
            )

        # --- Per-run state ---------------------------------------------
        spec: Mapping[str, Any] = getattr(job, "spec", {}) or {}
        task_id = (
            spec.get("task_id")
            or getattr(job, "external_run_id", None)
            or f"job-{uuid.uuid4().hex[:8]}"
        )
        external_run_id = f"dsh-headless-{uuid.uuid4().hex}"
        self._ensure_runs_root()
        run_dir = self._runs_root / external_run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        argv = self._build_argv(task)
        env = self._child_env()
        # AEE DSH Default Executor Activation (work order §14): run the
        # DSH headless child with its working directory set to the
        # dispatched ``repo_path`` (already validated against AEE's
        # repo_allowlist by the bridge). The DSH base bundle's
        # sandbox-policy uses ``process.cwd()`` as the ``workspace-write``
        # boundary and the model's ``{{cwd}}`` working directory, so
        # aligning the child cwd with ``repo_path`` lets the autonomous
        # worker create caller-declared artifacts under that repo
        # (e.g. /workspace/AEE_DSH_LIVE_MODEL_ARTIFACT.json) under the
        # default ``workspace-write`` policy — without widening to
        # ``danger-full-access``. Falls back to the process cwd
        # (previous behaviour) when no usable repo_path is supplied.
        repo_path_raw = spec.get("repo_path")
        child_cwd: Optional[str] = None
        if isinstance(repo_path_raw, str) and repo_path_raw:
            candidate = os.path.abspath(repo_path_raw)
            if os.path.isdir(candidate):
                child_cwd = candidate
        run = _RunState(
            external_run_id=external_run_id,
            argv=argv,
            task_id=str(task_id),
        )
        self._runs[external_run_id] = run

        # --- Spawn -----------------------------------------------------
        try:
            proc = await asyncio.create_subprocess_exec(  # noqa: S603
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=child_cwd,
            )
        except FileNotFoundError as exc:
            self._runs.pop(external_run_id, None)
            raise RuntimeError(
                f"dsh-headless: failed to spawn node {self._node_bin!r}: {exc}"
            ) from exc
        except OSError as exc:
            self._runs.pop(external_run_id, None)
            raise RuntimeError(f"dsh-headless: spawn OSError: {exc}") from exc

        # --- Wait for completion (bounded by timeout) ------------------
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                stdout_b, stderr_b = await proc.communicate()
            except Exception:  # pragma: no cover (defensive)
                stdout_b, stderr_b = b"", b""
            run.finished_at = time.time()
            run.final_status = "timeout"
            run.final_error = (
                f"dsh-headless: timeout after {self._timeout_seconds}s"
            )
            run.exit_code = proc.returncode
            return RuntimeSubmitResult(
                external_run_id=external_run_id,
                status="timeout",
                raw={
                    "pid": proc.pid,
                    "argv": argv,
                    "exit_code": run.exit_code,
                    "timeout_seconds": self._timeout_seconds,
                    "credentials": self._credential_presence(),
                    "stderr_tail": _safe_tail(stderr_b),
                },
            )

        # --- Process exited within the budget --------------------------
        run.finished_at = time.time()
        run.exit_code = proc.returncode
        stdout = _safe_text(stdout_b)
        stderr = _safe_text(stderr_b)

        # Persist logs for post-mortem. We never persist the task text
        # alongside credentials — the task is unrelated to secrets and
        # the env is built from an allow-list, but we still keep the
        # log file narrow so future audit tooling can re-derive
        # ``raw`` without scanning the full stdout stream.
        (run_dir / "stdout.log").write_text(stdout, encoding="utf-8", errors="replace")
        (run_dir / "stderr.log").write_text(stderr, encoding="utf-8", errors="replace")
        (run_dir / "argv.txt").write_text(
            _argv_for_log(argv), encoding="utf-8",
        )

        if proc.returncode == 0:
            run.final_status = "completed"
            run.final_output = stdout or None
            return RuntimeSubmitResult(
                external_run_id=external_run_id,
                status="completed",
                raw={
                    "pid": proc.pid,
                    "argv": argv,
                    "exit_code": 0,
                    "credentials": self._credential_presence(),
                    "run_dir": str(run_dir),
                    "child_cwd": child_cwd,
                    # AEE DSH Default Executor Activation: surface the
                    # final assistant text so the bridge's
                    # ``dsh_stdout = raw.get("stdout_tail") or
                    # raw.get("output")`` resolution (app.py DSH
                    # branch) populates ``stdout_summary`` and the
                    # caller observes the terminal result. The
                    # failure / timeout branches already include
                    # ``stdout_tail``; the completed branch must too,
                    # otherwise a successful run returns an empty
                    # ``stdout_summary`` and the acceptance criterion
                    # (final assistant result exactly matches) is not
                    # observable in the API envelope even though the
                    # model produced it (verified in run_dir/stdout.log).
                    "stdout_tail": _safe_tail(stdout_b),
                    "output": stdout,
                },
            )

        # Non-zero exit → classify.
        kind, message = self._classify_failure(stderr, stdout)
        run.final_status = "failed"
        run.final_error = message
        # We map ``missing_credential`` to a *runtime error* (not a
        # silent failed-state poll result) because the worker has no
        # chance of succeeding without operator intervention. This is
        # the path the compatibility audit explicitly called out:
        # ``MISSING_CREDENTIAL`` must not become a silent PASS.
        if kind == "missing_credential":
            return RuntimeSubmitResult(
                external_run_id=external_run_id,
                status="failed",
                raw={
                    "pid": proc.pid,
                    "argv": argv,
                    "exit_code": proc.returncode,
                    "failure_kind": kind,
                    "credentials": self._credential_presence(),
                    "stderr_tail": _safe_tail(stderr_b),
                    "stdout_tail": _safe_tail(stdout_b),
                },
            )
        return RuntimeSubmitResult(
            external_run_id=external_run_id,
            status="failed",
            raw={
                "pid": proc.pid,
                "argv": argv,
                "exit_code": proc.returncode,
                "failure_kind": kind,
                "credentials": self._credential_presence(),
                "stderr_tail": _safe_tail(stderr_b),
            },
        )

    async def poll(self, external_run_id: str) -> RuntimePollResult:
        run = self._runs.get(external_run_id)
        if run is None:
            raise UnknownExternalRunError(
                f"dsh-headless run {external_run_id!r} not known to this adapter "
                "(lost after adapter restart?)"
            )

        # Headless is one-shot; submit() always leaves the run terminal.
        is_terminal = run.final_status in _TERMINAL_STATUSES
        raw: Dict[str, Any] = {
            "argv": run.argv,
            "exit_code": run.exit_code,
            "credentials": self._credential_presence(),
        }
        if run.finished_at is not None:
            raw["duration_sec"] = round(run.finished_at - run.started_at, 3)
        return RuntimePollResult(
            external_run_id=external_run_id,
            status=run.final_status,
            is_terminal=is_terminal,
            output=run.final_output,
            error=run.final_error,
            raw=raw,
        )

    async def cancel(self, external_run_id: str) -> RuntimeCancelResult:
        """Headless runs are one-shot; cancel is best-effort reporting.

        Because ``submit()`` waits for the child to exit, by the time
        ``cancel()`` is called the process is already gone. We treat
        that as ``cancelled=False`` with an explanatory reason (the run
        reached a terminal state in some other way).
        """
        run = self._runs.get(external_run_id)
        if run is None:
            raise UnknownExternalRunError(
                f"dsh-headless run {external_run_id!r} not known to this adapter"
            )
        return RuntimeCancelResult(
            external_run_id=external_run_id,
            cancelled=False,
            reason=(
                "dsh-headless is one-shot: the DSH child has already exited; "
                f"final status={run.final_status}"
            ),
            raw={
                "exit_code": run.exit_code,
                "final_status": run.final_status,
            },
        )


# ---------------------------------------------------------------------------
# Module-level helpers (kept private — exposed via the adapter only)
# ---------------------------------------------------------------------------


def _safe_text(data: bytes) -> str:
    """Decode subprocess output safely, bounded for downstream use.

    Mirrors the bounded-read pattern in ``ClaudeCliAdapter``: we cap at
    256 KiB so a runaway DSH run cannot blow up the dispatcher's
    response payload.
    """
    if not data:
        return ""
    if len(data) > 256 * 1024:
        data = data[-256 * 1024:]
    return data.decode("utf-8", errors="replace")


def _safe_tail(data: bytes, *, limit: int = 64 * 1024) -> str:
    """Return the last ``limit`` bytes of ``data`` as safe text.

    Used in error ``raw`` payloads so a misbehaving DSH cannot pad its
    stdout/stderr past the diagnostic budget.
    """
    if not data:
        return ""
    if len(data) > limit:
        data = data[-limit:]
    return data.decode("utf-8", errors="replace").strip()


def _argv_for_log(argv: list[str]) -> str:
    """Render ``argv`` for the on-disk audit log.

    We use ``shlex.join`` so the persisted argv round-trips through
    ``shlex.split``. No env values are present in ``argv`` (env is
    passed separately to the child), so this is safe to persist.
    """
    try:
        return shlex.join(argv)
    except Exception:  # pragma: no cover (defensive)
        return " ".join(argv)


__all__ = ["DshHeadlessAdapter"]
