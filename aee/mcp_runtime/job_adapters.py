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

from .process import Limits, run_bounded, run_streamed
from .event_stream import EventParser
from .store import AGENT_NAMES, JobError, output_limit_context

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
    write_enforcement: str = "UNSUPPORTED"   # NATIVE_SANDBOX | UNSUPPORTED
    write_note: str = "no write-mode argv is implemented for this adapter"

    def build_argv(self, working_directory: str, executable: str,
                   mode: str = "read_only") -> List[str]:
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
            write_enforcement="NATIVE_SANDBOX",
            write_note=(
                "codex exec --sandbox workspace-write: writes confined to the "
                "job working directory and /tmp by the codex native sandbox; "
                "network access stays disabled by default"),
        )

    def build_argv(self, working_directory: str, executable: str,
                   mode: str = "read_only") -> List[str]:
        # `-` reads the task from stdin instead of argv. --ephemeral keeps
        # session files off disk; --json yields the parsed event stream the
        # gateway already understands.
        if mode == "autonomous":
            return [executable, "exec", "--json", "--ephemeral", "--skip-git-repo-check",
                    "--dangerously-bypass-approvals-and-sandbox", "--cd", ".", "-"]
        sandbox = "workspace-write" if mode == "write" else "read-only"
        return [executable, "exec", "--json", "--ephemeral", "--skip-git-repo-check",
                "--sandbox", sandbox, "--cd", working_directory, "-"]


@_spec
class _ClaudeAdapter(AdapterSpec):
    def __init__(self) -> None:
        super().__init__(
            name="claude",
            default_executable="claude",
            env_executable="A3_ADAPTER_CLAUDE_BIN",
            version_argv=("--version",),
            read_only_enforcement="TOOL_ALLOWLIST",
            enforcement_note=(
                "claude restricted safe mode with only Read/Glob/Grep and an empty strict "
                "MCP configuration: no command/code/edit/write tools, hooks or plugins. "
                "Plan mode alone is not a read-only enforcement boundary"),
            write_enforcement="NATIVE_SANDBOX",
            write_note=(
                "claude -p --permission-mode acceptEdits (headless): file "
                "edits inside the working directory are auto-accepted; every "
                "tool that would need an interactive prompt (e.g. Bash) is "
                "denied in non-interactive mode"),
        )

    def build_argv(self, working_directory: str, executable: str,
                   mode: str = "read_only") -> List[str]:
        # Task arrives on stdin (no positional prompt) so it is never in argv.
        # `--output-format json` alone emits one final result JSON object;
        # adding `--verbose` streams the whole event array instead and its
        # intermediate events alone overflow the 64 KiB stdout capture cap
        # long before the final result arrives. The
        # single-object shape still carries result/usage/is_error, and the
        # parser below keeps accepting both shapes for forward compatibility.
        if mode == "autonomous":
            return [executable, "-p", "--no-session-persistence",
                    "--dangerously-skip-permissions", "--output-format", "stream-json", "--verbose"]
        if mode == "read_only":
            return [executable, "-p", "--no-session-persistence", "--permission-mode", "plan",
                    "--restricted", "--safe-mode", "--tools", "Read,Glob,Grep",
                    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                    "--output-format", "json"]
        return [executable, "-p", "--no-session-persistence",
                "--permission-mode", "acceptEdits", "--output-format", "json"]

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
            if last.get("subtype") == "error_max_turns":
                raise JobError("TURN_BUDGET_EXHAUSTED", "Claude exhausted configured turn budget")
            if last.get("is_error") is True:
                raise JobError("EXECUTION_FAILED", "Executor reported a failed turn")
            result = last.get("result")
            if isinstance(result, str) and result.strip():
                encoded = len(result.encode())
                if encoded > limits.result_bytes:
                    raise JobError(
                        "OUTPUT_LIMIT_EXCEEDED", "Final message exceeds payload byte limit",
                        context=output_limit_context(
                            'native_final_message', 'final_message_utf8', 'utf8_bytes',
                            limit=limits.result_bytes, observed=encoded, measured=True))
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
                "pi -p --tools read --no-mcp --no-extensions: bash/edit/write and MCP tools disabled, "
                "only the read tool is available in the non-interactive run"),
            write_enforcement="UNSUPPORTED",
            write_note=(
                "pi has no verifiable write-scope confinement in its "
                "non-interactive vocabulary on this host; write dispatch is "
                "refused rather than simulated"),
        )

    def build_argv(self, working_directory: str, executable: str,
                   mode: str = "read_only") -> List[str]:
        return [executable, "-p", "--no-session", "--mode", "json",
                "--no-extensions", "--no-mcp", "--no-skills", "--no-prompt-templates",
                "--no-approve", "--tools", "read,bash,edit,write" if mode == "autonomous" else "read"]


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

    def build_argv(self, working_directory: str, executable: str,
                   mode: str = "read_only") -> List[str]:
        if mode != "autonomous":
            raise NotImplementedError  # existing constrained-mode mapping remains unsupported
        return [executable, "chat", "--yolo", "--query-file", "-", "--format", "stream-json"]


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
            "enforcement_note": spec.enforcement_note,
            "write_enforcement": spec.write_enforcement,
            "write_note": spec.write_note}


def refuse_unsupported(agent: str, mode: str = "read_only") -> None:
    """Fail-closed admission for adapters that cannot enforce the requested mode.

    Registry-only check (no binary resolution): an adapter that cannot
    enforce the requested mode honestly is refused whether or not its CLI
    happens to be installed.
    """
    spec = _ADAPTERS.get((agent or "").strip().lower())
    if spec is None:
        raise JobError("INVALID_AGENT",
                       f"agent '{agent[:60]}' is not a registered adapter; "
                       f"supported adapters: {', '.join(sorted(AGENT_NAMES))}")
    if mode == "autonomous":
        return
    if mode == "write":
        if spec.write_enforcement == "UNSUPPORTED":
            raise JobError("ADAPTER_UNSUPPORTED",
                           f"agent '{spec.name}' cannot enforce write mode on this "
                           "host; dispatch refused")
        return
    if spec.read_only_enforcement == "UNSUPPORTED":
        raise JobError("ADAPTER_UNSUPPORTED",
                       f"agent '{spec.name}' cannot enforce read_only on this "
                       "host; dispatch refused")


def adapter_execution_metadata(agent: str, mode: str = "read_only") -> Dict[str, object]:
    """Execution proof recorded at admission; re-checked at dispatch time."""
    identity = adapter_identity(agent)
    if mode == 'autonomous':
        verify_autonomous_capabilities(agent, identity['executable'])
    elif agent == 'pi':
        verify_pi_capabilities(identity['executable'])
    elif agent == 'claude' and mode == 'read_only':
        verify_claude_readonly_capabilities(identity['executable'])
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
        completed = run_bounded([executable, *spec.version_argv], env=_VERSION_ENV, cwd='/',
                                limits=Limits(timeout=VERSION_PROBE_TIMEOUT,
                                              stdout_bytes=4096, stderr_bytes=4096))
        text = completed.stdout.decode("utf-8", "replace").strip()
        return text.splitlines()[0][:200] if text else f"exit={completed.returncode}"
    except (OSError, JobError):
        return "unavailable"


def _execute_adapter_job(spec: AdapterSpec, executable: str, execution: Dict[str, object],
                        task: str, working_directory: str, *, limits: Limits,
                        cancel_event=None, budget=None, spool=None, redact=lambda x: x,
                        mode: str = "read_only") -> Dict[str, object]:
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
    # Provider credentials stay available; control-plane authority never enters child env.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('AEE_', 'A3_', 'MCP_BRIDGE_'))
           and key not in {"PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"}}
    if mode == "autonomous":
        verify_autonomous_capabilities(spec.name, executable)
    elif mode == "write":
        if spec.write_enforcement == "UNSUPPORTED":
            raise JobError("ADAPTER_UNSUPPORTED",
                           f"agent '{spec.name}' cannot enforce write mode on this host")
    elif spec.read_only_enforcement == "UNSUPPORTED":
        raise JobError("ADAPTER_UNSUPPORTED",
                       f"agent '{spec.name}' cannot enforce read_only on this host")
    from .trusted_dispatch import check_execution, PROOF
    authorization = execution.get('authorization') or {}
    if mode == 'autonomous' or authorization.get('schema') == PROOF:
        original_cwd = authorization['working_directory']
        check_execution(authorization, spec.name, mode, task, original_cwd)
    if spec.name == 'claude' and mode == 'read_only':
        verify_claude_readonly_capabilities(executable)
    # Native sandbox roots must use the canonical workspace path. A /proc fd
    # alias is suitable for Popen's pinned cwd, but Codex file tools reject it
    # as outside the workspace-write project. Autonomous keeps its native '.'
    # mapping; trusted read/write retain their original sandbox root semantics.
    native_workdir = (authorization['working_directory']
                      if authorization.get('schema') == PROOF and mode != 'autonomous'
                      else working_directory)
    argv = spec.build_argv(native_workdir, executable, mode)
    if budget is not None:
        if spool is None:
            raise JobError('SPOOL_BOUNDARY', 'Durable spool required for budget execution')
        if spec.name == 'claude' and budget.idle_timeout_seconds is not None and '--verbose' not in argv:
            # Idle accounting requires observable progress, not final-only JSON.
            argv[argv.index('--output-format') + 1] = 'stream-json'
            argv.append('--verbose')
        if budget.max_turns is not None:
            if spec.name != 'claude':
                raise JobError('INVALID_EXECUTION_BUDGET', 'Adapter does not support max_turns')
            argv += ['--max-turns', str(budget.max_turns)]
        def on_start(pid):
            if mode == 'autonomous' or authorization.get('schema') == PROOF:
                from pathlib import Path
                lines = Path(f'/proc/{pid}/status').read_text().splitlines()
                execution['process_identity'] = {'pid': pid, 'launcher': 'process_guard',
                    'status': [line for line in lines if line.startswith(
                        ('Uid:', 'Gid:', 'Groups:', 'CapEff:', 'NoNewPrivs:'))]}
        parser = EventParser(spec.name, budget.result_inline_bytes, budget.memory_capture_bytes)
        completed = run_streamed(argv, env=env, stdin=task.encode('utf-8'), on_start=on_start,
                                 budget=budget, spool=spool, parser=parser,
                                 cwd=working_directory, cancel_event=cancel_event, redact=redact)
        execution['tool_event_record'] = parser.tool_evidence()
        if completed.returncode != 0:
            error = JobError('EXECUTION_FAILED', f'Executor exited {completed.returncode}',
                             exit_code=completed.returncode)
            error.streaming = completed.streaming
            raise error
        return {'summary': completed.final,
                'log_excerpt': completed.streaming['streams']['stderr']['tail'][:limits.log_chars],
                'exit_code': completed.returncode, 'streaming': completed.streaming}
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
    elif spec.name == "pi":
        parser = EventParser("pi", limits.result_bytes)
        parser.feed(completed.stdout, eof=True)
        summary = parser.result()
    else:
        summary = stdout.strip()
        if not summary:
            raise JobError("EXECUTION_FAILED", "Executor produced no final agent message")
        encoded = len(summary.encode())
        if encoded > limits.result_bytes:
            raise JobError(
                "OUTPUT_LIMIT_EXCEEDED", "Final message exceeds payload byte limit",
                context=output_limit_context(
                    'native_final_message', 'final_message_utf8', 'utf8_bytes',
                    limit=limits.result_bytes, observed=encoded, measured=True))
    return {"summary": summary, "log_excerpt": stderr.strip()[:limits.log_chars],
            "exit_code": completed.returncode}

def verify_pi_capabilities(executable):
    """Fail closed on CLI builds lacking the required isolation flags."""
    try:
        completed = run_bounded([executable, '--help'], env=_VERSION_ENV,
                                limits=Limits(timeout=20))
        help_text = completed.stdout.decode('utf-8', 'replace')
    except (OSError, JobError):
        raise JobError('ADAPTER_UNSUPPORTED', 'Pi isolation capability probe unavailable') from None
    required = ('--no-mcp', '--no-extensions', '--no-skills', '--no-prompt-templates',
                '--no-approve', '--mode', '--no-session', '--tools')
    if completed.returncode != 0 or any(flag not in help_text for flag in required):
        raise JobError('ADAPTER_UNSUPPORTED', 'Pi CLI does not advertise required read-only/JSON flags')


_AUTONOMOUS_FLAGS = {
    'codex': ('--dangerously-bypass-approvals-and-sandbox', '--json', '--ephemeral', '--skip-git-repo-check', '--cd'),
    'claude': ('--dangerously-skip-permissions', '--print', '--output-format', '--no-session-persistence'),
    'pi': ('--print', '--mode', '--no-session', '--no-extensions', '--no-mcp', '--no-skills',
           '--no-prompt-templates', '--no-approve', '--tools'),
    'hermes': ('--query-file', '--format', '--yolo'),
}


def verify_autonomous_capabilities(agent, executable):
    args = ['exec', '--help'] if agent == 'codex' else ['chat', '--help'] if agent == 'hermes' else ['--help']
    try:
        result = run_bounded([executable, *args], env=_VERSION_ENV, cwd='/',
                             limits=Limits(timeout=20, stdout_bytes=65536, stderr_bytes=8192))
        text = result.stdout.decode('utf-8', 'replace')
        if result.returncode or any(flag not in text for flag in _AUTONOMOUS_FLAGS.get(agent, ())):
            raise ValueError('missing native capabilities')
        if agent not in _AUTONOMOUS_FLAGS:
            raise ValueError('unregistered autonomous mapping')
    except (OSError, ValueError, JobError):
        raise JobError('ADAPTER_UNSUPPORTED', 'Installed CLI lacks verified headless autonomous flags') from None


def autonomous_capability(agent):
    try:
        spec, executable = resolve_adapter(agent)
    except JobError as exc:
        return {'autonomous_supported': False, 'reason': exc.code}
    try:
        verify_autonomous_capabilities(agent, executable)
        return {'autonomous_supported': True, 'reason': 'native flags verified; provider readiness requires real job'}
    except JobError as exc:
        return {'autonomous_supported': False, 'reason': exc.code}


def execute_adapter_job(spec, executable, execution, task, working_directory, **kwargs):
    from .trusted_dispatch import PROOF
    if (kwargs.get('mode') != 'autonomous'
            and (execution.get('authorization') or {}).get('schema') != PROOF):
        return _execute_adapter_job(spec, executable, execution, task, working_directory, **kwargs)
    fd = os.open(working_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        expected = (execution.get('authorization') or {}).get('directory_identity')
        if expected != {'device': info.st_dev, 'inode': info.st_ino}:
            raise JobError('INVALID_WORKING_DIRECTORY', 'Working directory changed since admission')
        # Parent holds fd across Popen; child chdir resolves this pinned inode.
        pinned = f'/proc/{os.getpid()}/fd/{fd}'
        return _execute_adapter_job(spec, executable, execution, task, pinned, **kwargs)
    finally:
        os.close(fd)


def verify_claude_readonly_capabilities(executable):
    required = ('--restricted', '--safe-mode', '--tools', '--strict-mcp-config', '--mcp-config')
    try:
        result = run_bounded([executable, '--help'], env=_VERSION_ENV, cwd='/',
                             limits=Limits(timeout=20, stdout_bytes=65536, stderr_bytes=8192))
        text = result.stdout.decode('utf-8', 'replace')
        if result.returncode or any(flag not in text for flag in required):
            raise ValueError('missing read-only controls')
    except (OSError, ValueError, JobError):
        raise JobError('ADAPTER_UNSUPPORTED', 'Claude CLI lacks verified read-only tool isolation flags') from None
