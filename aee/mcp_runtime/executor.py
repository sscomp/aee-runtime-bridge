"""Codex execution on an immutable reviewed snapshot with bounded JSONL I/O."""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from .process import Limits, run_bounded
from .profiles import codex_arguments, executable_digest
from .sandbox import read_manifest, sandbox_command, snapshot_workspace
from .store import JobError, output_limit_context
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


def augmented_evidence_failure(failure, receipt, thread, broker_receipt):
    """Scalar-only state of a failing native receipt validation; never raw text.

    Same principle as the fail-shot diagnostics: classification is unchanged,
    but the evidence-complete=false path keeps enough sanitized state to
    attribute which pairing condition failed after sandbox teardown. All fields
    come from the real observed receipt/broker receipt — receipt and thread
    presence, the observed tool cells (sequence, name, source, success,
    call_id, exit_code, unverified flags), mirrored validator counts for
    missing completion/exit-code cells, and the broker's expected-call
    declaration versus the observed direct calls (correlation failure).
    """
    if not isinstance(failure, dict):
        failure = {}
    failure.setdefault('evidence_complete', False)
    summary = {'receipt_present': receipt is not None,
               'thread_present': isinstance(thread, str) and bool(thread)}
    observed_direct = None
    if isinstance(receipt, dict):
        summary['receipt_complete'] = receipt.get('complete') if type(
            receipt.get('complete')) is bool else None
        tools = receipt.get('tools')
        if isinstance(tools, list):
            summary['tool_count'] = len(tools)
            fields = ('sequence', 'tool_name', 'source', 'success', 'exit_code',
                      'unverified_completion', 'readonly_violation', 'call_id')
            cells = [{key: tool[key] for key in fields if key in tool
                      and isinstance(tool[key], (bool, int, str))}
                     for tool in tools[:128] if isinstance(tool, dict)]
            summary['tools'] = cells
            # The collector's own violated-predicate mirror, when it shipped one
            # (additive ``incomplete_reasons`` on the incomplete receipt branch):
            # scalar fields pass through; missing/detail lists are string-or-int
            # and capped. Hashed call identities only — no conversation ids.
            reasons = receipt.get('incomplete_reasons')
            if isinstance(reasons, list):
                collected = []
                for item in reasons[:8]:
                    if not isinstance(item, dict):
                        continue
                    clean = {key: value for key, value in item.items()
                             if type(key) is str and type(value) in (str, int, float)}
                    for list_key in ('missing', 'detail'):
                        nested = item.get(list_key)
                        if isinstance(nested, list) and all(
                                type(entry) in (str, int, float) for entry in nested):
                            clean[list_key] = nested[:16]
                    if clean:
                        collected.append(clean)
                if collected:
                    summary['collector_reasons'] = collected
            # Mirrors of the validator's own conditions, derived from the same
            # observed cells (counts only, no new data sources).
            summary['unverified_cells'] = sum(
                1 for tool in cells if tool.get('unverified_completion') is True)
            summary['missing_exit_code'] = sum(
                1 for tool in cells if tool.get('success') is True
                and tool.get('tool_name') in {'exec_command', 'write_stdin'}
                and type(tool.get('exit_code')) is not int)
            observed_direct = {tool['call_id']: tool['tool_name'] for tool in cells
                               if tool.get('source') == 'direct'
                               and isinstance(tool.get('call_id'), str)}
        # The collector's additive transport-phase diagnostics (bounded enum and
        # bucket fields, emitted only on the incomplete receipt branch under
        # AEE_TRANSPORT_DIAGNOSTICS=1): mirrored like collector_reasons — scalar
        # fields only, events capped at 16, nothing raw.
        transport = receipt.get('transport_diagnostics')
        if isinstance(transport, dict):
            mirror = {}
            version = transport.get('schema_version')
            if type(version) is int:
                mirror['schema_version'] = version
            totals = transport.get('totals')
            if isinstance(totals, dict):
                clean = {key: value for key, value in totals.items()
                         if type(key) is str and type(value) in (int, float)}
                sites = totals.get('invalid_sites')
                if isinstance(sites, dict):
                    clean['invalid_sites'] = {key: value for key, value in sites.items()
                                              if type(key) is str and type(value) is int}
                if clean:
                    mirror['totals'] = clean
            events = transport.get('events')
            if isinstance(events, list):
                kept = [{key: value for key, value in event.items()
                         if type(key) is str and type(value) in (str, int, float)}
                        for event in events[:16] if isinstance(event, dict)]
                kept = [event for event in kept if event]
                if kept:
                    mirror['events'] = kept
            if mirror:
                summary['transport_diagnostics'] = mirror
    if isinstance(broker_receipt, dict):
        summary['broker_valid'] = broker_receipt.get('valid') if type(
            broker_receipt.get('valid')) is bool else None
        summary['broker_response_complete'] = broker_receipt.get('response_complete') if type(
            broker_receipt.get('response_complete')) is bool else None
        expected_calls = broker_receipt.get('expected_calls')
        if isinstance(expected_calls, dict):
            summary['broker_expected_calls'] = {key: value for key, value in
                                                expected_calls.items()
                                                if isinstance(key, str) and isinstance(value, str)}
            if observed_direct is not None:
                summary['observed_direct_calls'] = observed_direct
                summary['broker_declaration_mismatch'] = (
                    summary['broker_expected_calls'] != observed_direct)
    failure['receipt_state'] = summary
    return failure


def bounded_failure(event=None, *, thread=None):
    """Scalar-only summary of the failing native event; never retains free text.

    Companion to the fail-shot REQUIRED_TOOL_FAILED semantics: classification is
    unchanged (any nonzero command exit still fails the job), but the failure
    record names the failing event so attribution survives sandbox teardown.
    Keys are short scalars only — no command text, output or arguments.
    """
    failure = {'evidence_source': 'native-jsonl', 'recovery_permitted': False}
    if isinstance(thread, str) and thread:
        failure['thread_id'] = thread[:100]
    if isinstance(event, dict):
        summary = {}
        item = event.get('item') if isinstance(event.get('item'), dict) else None
        source = item if item is not None else event
        if item is not None:
            if isinstance(item.get('type'), str):
                summary['item_type'] = item['type'][:100]
            if isinstance(item.get('status'), str):
                summary['item_status'] = item['status'][:100]
        elif isinstance(event.get('type'), str):
            summary['event_type'] = event['type'][:100]
        exit_code = source.get('exit_code')
        summary['exit_code'] = exit_code if isinstance(exit_code, int) else None
        failure['failing_event'] = summary
    return failure


def final_agent_message(stdout, limits, *, require_receipt=False, broker_receipt=None):
    def no_duplicate_keys(pairs):
        seen = {}
        for key, value in pairs:
            if key in seen:
                raise ValueError('duplicate object member')
            seen[key] = value
        return seen

    message = None
    completed = False
    receipt, thread = None, None
    for line in stdout.splitlines():
        try:
            event = json.loads(line, object_pairs_hook=no_duplicate_keys)
        except (ValueError, UnicodeError):
            raise JobError("EXECUTION_FAILED", "Executor emitted malformed event data") from None
        if not isinstance(event, dict):
            raise JobError("EXECUTION_FAILED", "Executor emitted invalid event data")
        if event.get("type") in {"turn.failed", "error"}:
            raise JobError("EXECUTION_FAILED", "Executor reported a failed turn",
                           failure=bounded_failure(event, thread=thread))
        if event.get("type") == "turn.completed":
            completed = True
        if event.get('type') == 'turn.started':
            # Only the final turn's own terminal event and answer settle the
            # stream: a new turn invalidates any earlier completion/message.
            message = None
            completed = False
        if event.get('type') == 'thread.started':
            if thread is not None:
                raise JobError('EXECUTION_FAILED', 'Executor emitted multiple thread identities')
            thread = event.get('thread_id')
        if event.get('type') == 'aee.native_tool_receipt':
            if receipt is not None:
                raise JobError('TOOL_EVIDENCE_INCOMPLETE', 'Executor emitted duplicate receipts',
                               exit_code=0, failure=augmented_evidence_failure(
                                   {'evidence_source': 'native-jsonl',
                                    'reason': 'duplicate_native_receipt'}, receipt, thread,
                                   broker_receipt))
            receipt = event
        if event.get("type") == "item.completed":
            item = event.get("item", {})
            if isinstance(item, dict):
                # Layered terminal semantics (codex tool-failure contract): a
                # completed command_execution with a nonzero integer exit code
                # is a recovered tool observation, not a job-terminal verdict —
                # the final turn verdict decides. Ambiguous command evidence
                # (exit code missing or not an integer) stays fail-closed, as
                # do error items, failed/declined non-command items and
                # turn.failed / top-level error events.
                if item.get('type') == 'command_execution':
                    if type(item.get('exit_code')) is not int:
                        raise JobError('REQUIRED_TOOL_FAILED',
                                       'Native executor reported a command outcome with no measurable exit code',
                                       exit_code=0, failure=bounded_failure(event, thread=thread))
                elif ((item.get('type') == 'error')
                      or item.get('status') in {'failed', 'declined'}):
                    raise JobError('REQUIRED_TOOL_FAILED', 'Native executor reported a failed required operation',
                                   exit_code=0, failure=bounded_failure(event, thread=thread))
            if isinstance(item, dict) and item.get("type") == "agent_message":
                value = item.get("text")
                if isinstance(value, str):
                    message = value
    if not completed or not message or not message.strip():
        raise JobError("EXECUTION_FAILED", "Executor produced no final agent message")
    if require_receipt or receipt is not None:
        try:
            native_receipt(receipt, thread, broker_receipt)
        except JobError as error:
            raise JobError(error.code, error.message, exit_code=error.exit_code,
                           failure=augmented_evidence_failure(error.failure, receipt, thread,
                                                              broker_receipt)) from None
    final_bytes = len(message.encode())
    if final_bytes > limits.result_bytes:
        raise JobError("OUTPUT_LIMIT_EXCEEDED", "Final message exceeds payload byte limit",
                       context=output_limit_context(
                           'native_final_message', 'final_message_utf8', 'utf8_bytes',
                           limit=limits.result_bytes, observed=final_bytes, measured=True))
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
        payload_bytes = len(json.dumps(payload, ensure_ascii=False).encode())
        if payload_bytes > limits.result_bytes:
            raise JobError("OUTPUT_LIMIT_EXCEEDED", "Result payload exceeds byte limit",
                           context=output_limit_context(
                               'success_payload_json', 'payload_json_utf8', 'utf8_bytes',
                               limit=limits.result_bytes, observed=payload_bytes, measured=True))
        return ExecutionResult(summary, log, len(message) > limits.summary_chars, result.returncode, snapshot, evidence, outcome)
