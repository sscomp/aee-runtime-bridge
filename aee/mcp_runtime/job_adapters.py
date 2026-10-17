"""Generic executor adapter registry for the P2C MCP gateway.

Work order AEE_FUNCTIONAL_RECOVERY_A2_A3_MULTI_AGENT: executors are
adapters (Codex, Claude Code, Pi, Hermes); none is the mandatory default
backend, and per-job ``working_directory`` is a validated input. This
module is the bounded, synchronous adapter contract the gateway dispatch
thread uses when a job is NOT routed through the approved Stage-2 Codex
broker path (the broker path stays byte-identical in ``mcp_gateway``).

Contract notes
--------------
* ``AGENT_NAMES`` lives in :mod:`aee.mcp_runtime.store` (single source of
  truth shared with the job store admission check).
* Every spec declares its ``read_only_enforcement`` honestly. A spec
  marked ``UNSUPPORTED`` is never dispatched: a request for it fails
  with ``ADAPTER_UNSUPPORTED`` rather than simulating read-only.
* The child runs ``cwd=<working_directory>``; the task itself is always
  fed on stdin so it never appears in argv or in ``/proc/*/cmdline``.
* Child env is the parent environ minus python-specific override
  variables; host CLI auth (Claude/Codex/Pi config) is required on this
  path — the approved broker path is the only one with zero host auth.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .process import Limits, run_bounded
from .store import AGENT_NAMES, JobError

# Codex-native JSONL final-message extraction is shared with the broker
# path; the adapter variant never requires a broker/native receipt.
from .executor import final_agent_message as _codex_final_message

VERSION_PROBE_TIMEOUT = 20
_VERSION_ENV = {"HOME": os.environ.get("HOME", ""), "PATH": os.environ.get("PATH", "")}


@dataclass(frozen=True)
class AdapterSpec:
    """Immutable description of one executor CLI and its safety semantics."""

    name: str
    default_executable: str
    env_executable: str            # env var that may override the binary path
    version_argv: Tuple[str, ...]  # argv suffix probing the version string
    read_only_enforcement: str     # NATIVE_SANDBOX | TOOL_DENYLIST | UNSUPPORTED
    enforcement_note: str

    def build_argv(self, working_directory: str, executable: str) -> List[str]:
        raise NotImplementedError


_ADAPTERS: Dict[str, AdapterSpec] = {}


def _spec(adapter):
    """Register a singleton adapter instance built from a spec class."""
    instance = adapter()
    name = getattr(instance, "name", None)
    if not isinstance(name, str) or not name:
        raise RuntimeError("adapter spec classes must set their name")
    _ADAPTERS[name] = instance
    return adapter


@_spec
class _CodexAdapter(AdapterSpec):
    def __init__(self) -> None:
        super().__init__(
            name="codex",
            default_executable="codex",
            env_executable="A3_ADAPTER_CODEX_BIN",
            version_argv=("--version",),
            read_only_enforcement="NATIVE_SANDBOX",
            enforcement_note=(
                "codex exec --sandbox read-only: model-run commands confined "
                "to filesystem read-only via the codex native sandbox"),
        )

    def build_argv(self, working_directory: str, executable: str) -> List[str]:
        # `-` reads the task from stdin instead of argv. --ephemeral keeps
        # session files off disk; --json yields the parsed event stream the
        # gateway already understands.
        return [executable, "exec", "--json", "--ephemeral", "--skip-git-repo-check",
                "--sandbox", "read-only", "--cd", working_directory, "-"]


@_spec
class _ClaudeAdapter(AdapterSpec):
    def __init__(self) -> None:
        super().__init__(
            name="claude",
            default_executable="claude",
            env_executable="A3_ADAPTER_CLAUDE_BIN",
            version_argv=("--version",),
            read_only_enforcement="NATIVE_SANDBOX",
            enforcement_note=(
                "claude -p --permission-mode plan: edit/write tools are denied "
                "and bash is restricted to read-only analysis by plan mode"),
        )

    def build_argv(self, working_directory: str, executable: str) -> List[str]:
        # Task arrives on stdin (no positional prompt) so it is never in argv.
        return [executable, "-p", "--permission-mode", "plan",
                "--output-format", "json", "--verbose",
                "--max-turns", "16"]

    @staticmethod
    def final_message(stdout: str, limits: Limits, *args, **kwargs) -> str:
        """Extract the final result from a claude -p --output-format json run."""
        events = []
        try:
            whole = json.loads(stdout)  # -p emits one JSON document or array
            for event in (whole if isinstance(whole, list) else [whole]):
                if isinstance(event, dict):
                    events.append(event)
        except (ValueError, UnicodeError):
            for line in stdout.splitlines():  # --verbose interleaves notices
                candidate = line.strip()
                if not candidate:
                    continue
                try:
                    event = json.loads(candidate)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(event, dict):
                    events.append(event)
        payloads = [event for event in events if event.get("type") == "result"]
        if payloads:
            last = payloads[-1]
            if last.get("is_error") is True or last.get("subtype") == "error_max_turns":
                raise JobError("EXECUTION_FAILED", "Executor reported a failed turn")
            result = last.get("result")
            if isinstance(result, str) and result.strip():
                encoded = len(result.encode())
                if encoded > limits.result_bytes:
                    raise JobError(
                        "OUTPUT_LIMIT_EXCEEDED", "Final message exceeds payload byte limit")
                return result
        raise JobError("EXECUTION_FAILED", "Executor produced no final agent message")


@_spec
class _PiAdapter(AdapterSpec):
    def __init__(self) -> None:
        super().__init__(
            name="pi",
            default_executable="pi",
            env_executable="A3_ADAPTER_PI_BIN",
            version_argv=("--version",),
            read_only_enforcement="TOOL_DENYLIST",
            enforcement_note=(
                "pi -p --tools read: bash/edit/write and MCP tools disabled, "
                "only the read tool is available in the non-interactive run"),
        )

    def build_argv(self, working_directory: str, executable: str) -> List[str]:
        return [executable, "-p", "--no-session", "--tools", "read", "-"]


@_spec
class _HermesAdapter(AdapterSpec):
    def __init__(self) -> None:
        super().__init__(
            name="hermes",
            default_executable="hermes",
            env_executable="A3_ADAPTER_HERMES_BIN",
            version_argv=("--version",),
            read_only_enforcement="UNSUPPORTED",
            enforcement_note=(
                "hermes non-interactive toolset restriction (--toolsets "
                "vocabulary) is not verifiable on this host; --safe-mode "
                "semantics are unverified. Dispatch refuses rather than "
                "simulate read-only compliance"),
        )

    def build_argv(self, working_directory: str, executable: str) -> List[str]:
        raise NotImplementedError  # refuse to build argv for UNSUPPORTED


def adapter_specs() -> Dict[str, AdapterSpec]:
    """Read-only view of the registry."""
    return dict(_ADAPTERS)


def resolve_adapter(agent: str) -> Tuple[AdapterSpec, str]:
    """Resolve one installed adapter binary.

    Raises ``INVALID_AGENT`` for a name outside the registry and
    ``ADAPTER_NOT_INSTALLED`` when the binary cannot be located. The
    env override (``A3_ADAPTER_<NAME>_BIN``) must be absolute; bare
    names fall back to ``PATH`` lookup of the default executable.
    """
    spec = _ADAPTERS.get((agent or "").strip().lower())
    if spec is None:
        raise JobError("INVALID_AGENT",
                       f"agent '{agent[:60]}' is not a registered adapter; "
                       f"supported adapters: {', '.join(sorted(AGENT_NAMES))}")
    override = os.environ.get(spec.env_executable, "").strip()
    if override:
        if not os.path.isabs(override) or not os.path.isfile(override) \
                or not os.access(override, os.X_OK):
            raise JobError("ADAPTER_NOT_INSTALLED",
                           f"{spec.env_executable} override is not an executable absolute path")
        return spec, override
    found = shutil.which(spec.default_executable)
    if found is None:
        raise JobError("ADAPTER_NOT_INSTALLED",
                       f"agent '{spec.name}' has no '{spec.default_executable}' executable on PATH")
    return spec, found


def adapter_identity(agent: str) -> Dict[str, object]:
    """Bounded discovery metadata for one adapter (path/version/sha256)."""
    spec, executable = resolve_adapter(agent)
    return {"adapter": spec.name, "executable": executable,
            "executable_sha256": _sha256(executable),
            "version": _probe_version(spec, executable),
            "read_only_enforcement": spec.read_only_enforcement,
            "enforcement_note": spec.enforcement_note}


def refuse_unsupported(agent: str) -> None:
    """Fail-closed admission for adapters that cannot enforce read-only.

    Registry-only check (no binary resolution): an UNSUPPORTED adapter is
    refused whether or not its CLI happens to be installed.
    """
    spec = _ADAPTERS.get((agent or "").strip().lower())
    if spec is None:
        raise JobError("INVALID_AGENT",
                       f"agent '{agent[:60]}' is not a registered adapter; "
                       f"supported adapters: {', '.join(sorted(AGENT_NAMES))}")
    if spec.read_only_enforcement == "UNSUPPORTED":
        raise JobError("ADAPTER_UNSUPPORTED",
                       f"agent '{spec.name}' cannot enforce read_only on this "
                       "host; dispatch refused")


def adapter_execution_metadata(agent: str) -> Dict[str, object]:
    """Execution proof recorded at admission; re-checked at dispatch time."""
    identity = adapter_identity(agent)
    return {"adapter": identity,
            "kind": "job-adapter",
            "observed": {"agent_version_probe": identity["version"]}}


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _probe_version(spec: AdapterSpec, executable: str) -> str:
    try:
        completed = subprocess.run([executable, *spec.version_argv],
                                   env=_VERSION_ENV, cwd="/", timeout=VERSION_PROBE_TIMEOUT,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        text = completed.stdout.decode("utf-8", "replace").strip()
        return text.splitlines()[0][:200] if text else f"exit={completed.returncode}"
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


def execute_adapter_job(spec: AdapterSpec, executable: str, execution: Dict[str, object],
                        task: str, working_directory: str, *, limits: Limits,
                        cancel_event=None) -> Dict[str, str]:
    """Run one bounded adapter job; returns summary/log excerpt evidence.

    Mirrors the codex worker's semantics: the child process group is
    killed on timeout/cancel, output caps are enforced per stream, and
    the task travels on stdin. Raises ``JobError`` with the same codes
    the gateway's failure path already maps to terminal states.
    """
    identity = execution.get("adapter") or {}
    observed = {"adapter": spec.name, "executable": executable,
                "executable_sha256": _sha256(executable)}
    if (identity.get("executable") != observed["executable"]
            or identity.get("executable_sha256") != observed["executable_sha256"]):
        raise JobError("EXECUTOR_IDENTITY_MISMATCH",
                       "Executor identity changed since admission")
    env = {key: value for key, value in os.environ.items()
           if key not in {"PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"}}
    if spec.read_only_enforcement == "UNSUPPORTED":
        raise JobError("ADAPTER_UNSUPPORTED",
                       f"agent '{spec.name}' cannot enforce read_only on this host")
    argv = spec.build_argv(working_directory, executable)
    completed = run_bounded(argv, env=env, stdin=task.encode("utf-8"),
                            limits=limits, cwd=working_directory,
                            cancel_event=cancel_event)
    stdout = completed.stdout.decode("utf-8", "replace")
    stderr = completed.stderr.decode("utf-8", "replace")
    if completed.returncode != 0:
        excerpt = (stderr or stdout).strip()[:limits.log_chars]
        raise JobError("EXECUTION_FAILED",
                       f"Executor exited {completed.returncode}" +
                       (f": {excerpt}" if excerpt else ""),
                       exit_code=completed.returncode)
    if spec.name == "codex":
        summary = _codex_final_message(stdout, limits, require_receipt=False)
    elif spec.name == "claude":
        summary = _ClaudeAdapter.final_message(stdout, limits)
    else:
        summary = stdout.strip()
        if not summary:
            raise JobError("EXECUTION_FAILED", "Executor produced no final agent message")
        encoded = len(summary.encode())
        if encoded > limits.result_bytes:
            raise JobError("OUTPUT_LIMIT_EXCEEDED", "Final message exceeds payload byte limit")
    return {"summary": summary, "log_excerpt": stderr.strip()[:limits.log_chars],
            "exit_code": completed.returncode}