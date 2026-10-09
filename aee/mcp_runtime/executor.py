"""Codex execution on an immutable reviewed snapshot with bounded JSONL I/O."""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from .process import Limits, run_bounded
from .profiles import codex_arguments, executable_digest
from .sandbox import read_manifest, sandbox_command, snapshot_workspace
from .store import JobError
from .result_contract import validate_native_receipt as native_receipt, SUCCESS_VERSION


@dataclass(frozen=True)
class ExecutionResult:
    summary: str
    log_excerpt: str
    truncated: bool
    exit_code: int
    snapshot: dict
    evidence: dict | None = None
    outcome: dict | None = None


def final_agent_message(stdout, limits, *, require_receipt=False, broker_receipt=None):
    message = None
    completed = False
    started = False
    receipt, thread = None, None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            raise JobError("EXECUTION_FAILED", "Executor emitted malformed event data") from None
        if not isinstance(event, dict):
            raise JobError("EXECUTION_FAILED", "Executor emitted invalid event data")
        if event.get("type") in {"turn.failed", "error"}:
            raise JobError("EXECUTION_FAILED", "Executor reported a failed turn")
        if event.get("type") == "turn.completed":
            completed = True
        if event.get('type') == 'turn.started':
            started = True
        if event.get('type') == 'thread.started':
            if thread is not None:
                raise JobError('EXECUTION_FAILED', 'Executor emitted multiple thread identities')
            thread = event.get('thread_id')
        if event.get('type') == 'aee.native_tool_receipt':
            if receipt is not None:
                raise JobError('TOOL_EVIDENCE_INCOMPLETE', 'Executor emitted duplicate receipts', exit_code=0)
            receipt = event
        if event.get("type") == "item.completed":
            item = event.get("item", {})
            if isinstance(item, dict) and (
                    (item.get('type') == 'error' and started) or item.get('status') in {'failed', 'declined'}
                    or (item.get('type') == 'command_execution' and item.get('exit_code') not in {None, 0})):
                raise JobError('REQUIRED_TOOL_FAILED', 'Native executor reported a failed required operation',
                               exit_code=0, failure={'evidence_source': 'native-jsonl', 'recovery_permitted': False})
            if isinstance(item, dict) and item.get("type") == "agent_message":
                value = item.get("text")
                if isinstance(value, str):
                    message = value
    if not completed or not message or not message.strip():
        raise JobError("EXECUTION_FAILED", "Executor produced no final agent message")
    if require_receipt or receipt is not None:
        native_receipt(receipt, thread, broker_receipt)
    if len(message.encode()) > limits.result_bytes:
        raise JobError("OUTPUT_LIMIT_EXCEEDED", "Final message exceeds payload byte limit")
    return message


def execute_codex(identity, profile, task, working_directory, manifest, *, limits=Limits(), redact=lambda v: v,
                  broker_socket=None, broker_receipt=None, cancel_event=None):
    if not isinstance(task, str) or not task.strip() or len(task) > 8000:
        raise JobError("INVALID_TASK", "Task must be nonempty and at most 8000 characters")
    entries = read_manifest(manifest)
    from .runtime import deployment_policy, require_deployment_ready
    policy = deployment_policy()
    resource_revision = None
    sandbox_revision = None
    if policy:
        require_deployment_ready(policy)
        resource_revision = policy['resource_policy_revision']['id']
        sandbox_revision = policy['sandbox_policy_revision']['id']
        if broker_socket is None or broker_receipt is None:
            raise JobError('TOOL_EVIDENCE_INCOMPLETE', 'Production jobs require broker corroboration')
        from .resource_policy import resource_profile
        limits = replace(limits, timeout=min(limits.timeout, resource_profile()['job_timeout_seconds']))
    if broker_socket is not None:
        # Measured native 0.159.2 initialization creates ~2 MiB private SQLite WAL.
        # Host/source writes remain denied; each tmpfs remains capped at 16 MiB.
        limits = replace(limits, file_bytes=4 * 1024**2)
    with tempfile.TemporaryDirectory(prefix="aee-p2b-runner-") as temporary:
        workspace = Path(temporary) / "workspace"
        workspace.mkdir(mode=0o700)
        snapshot = snapshot_workspace(Path(working_directory), entries, workspace)
        if executable_digest(Path(identity.executable)) != identity.sha256:
            raise JobError("AGENT_UNAVAILABLE", "Pinned executor changed since admission")
        arguments = codex_arguments(profile)
        if broker_socket is not None:
            from .runtime import broker_arguments
            arguments = broker_arguments(arguments)
            if sandbox_revision is not None:
                from .sandbox_policy import telemetry_arguments
                arguments = telemetry_arguments(arguments)
        argv = sandbox_command(Path(identity.executable), workspace, arguments, limits,
                               broker_socket=broker_socket, resource_revision=resource_revision,
                               sandbox_revision=sandbox_revision)
        result = run_bounded(argv, env={}, stdin=task.encode(), limits=limits, cancel_event=cancel_event)
        if result.returncode != 0:
            raise JobError("EXECUTION_FAILED", "Codex exited unsuccessfully inside the isolated runner",
                           exit_code=result.returncode)
        corroboration = broker_receipt() if broker_receipt is not None else None
        message = final_agent_message(result.stdout, limits, require_receipt=sandbox_revision is not None,
                                      broker_receipt=corroboration)
        evidence = None
        outcome = None
        if sandbox_revision is not None:
            events = [json.loads(line) for line in result.stdout.splitlines()]
            receipt = next(e for e in events if e.get('type') == 'aee.native_tool_receipt')
            thread = next(e['thread_id'] for e in events if e.get('type') == 'thread.started')
            evidence = native_receipt(receipt, thread, corroboration, accepted=True)
            outcome = {"version": SUCCESS_VERSION, "process_exit_code": result.returncode,
                       "turn_completed": True, "thread": thread}
        summary = redact(message)[:limits.summary_chars]
        log = redact(result.log_tail.decode(errors="replace"))[-limits.log_chars:]
        payload = {"summary": summary, "log_excerpt": log, "artifacts": [], "snapshot": snapshot}
        if len(json.dumps(payload, ensure_ascii=False).encode()) > limits.result_bytes:
            raise JobError("OUTPUT_LIMIT_EXCEEDED", "Result payload exceeds byte limit")
        return ExecutionResult(summary, log, len(message) > limits.summary_chars, result.returncode, snapshot, evidence, outcome)
