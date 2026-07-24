"""TaskManager — the state-machine core.

All status transitions are validated. Every state change is recorded in
`task_events` (for audit) and `logs/TASK-XXX.log` (for tail-by-eye).

This module is sync (SQLite is sync). FastAPI route handlers may call
into it directly; the heavy work (calling Hermes 8642) happens in
`app.py` background tasks.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import db, ids
from .db import get_conn, transaction
from .models import (
    LEGAL_TRANSITIONS,
    Task,
    TaskEvent,
    is_legal_transition,
)
from .progress import monotonic, validate_progress

# AEE-7.4 finalization — event-kind SOT.  See
# ``aee/observability/events.py`` for the canonical inventory.
# The dispatcher writes to ``task_events.kind``; every literal
# written here MUST resolve to a member of ``EventKind`` —
# the tripwire in ``aee/tests/test_aee74_observability.py``
# will fail the build if a new literal leaks.
from aee.observability import EventKind

# AEE v3 Telegram Completion Enforcement Gate — 4-stage completion
# state model. ``compute_completion_state`` derives the highest
# reached stage from a task row + its ``task_outputs`` row; the
# ``FINAL_COMPLETED`` constant is the only terminal stage under the
# v3 model. See ``dispatcher/notification_state.py`` for the full
# model and the reference analysis pointer.
from dispatcher.notification_state import (
    FINAL_COMPLETED,
    compute_completion_state,
)

log = logging.getLogger("dispatcher.manager")

_BRIDGE_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = _BRIDGE_ROOT / "logs"
REPORTS_DIR = _BRIDGE_ROOT / "reports"


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def _log_path(task_id: str) -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR / f"{task_id}.log"


# AEE-7.2 observability: error messages may embed stdout, env var
# dumps, or path strings. We must NOT log the full message into the
# module logger (which is shipped to syslog / journald). Instead
# surface a short, sanitized class string: the leading token before
# the first colon / newline, capped at 64 chars. Full error
# content stays in the per-task log file (which the user can read
# on demand) and in the ``tasks.error_message`` column (DB only).
_SAFE_ERROR_CLASS_LEN = 64


def _safe_error_class(error_message: str) -> str:
    """Return a short, sanitized error class hint.

    AEE-7.2 observability: never echo the full ``error_message``
    into the module logger because it may contain stdout dumps,
    file paths from /etc/, or env var values. The dispatcher
    only needs the leading "category" token (e.g.
    ``"TimeoutError"``, ``"PolicyViolationError"``,
    ``"RuntimeNotFoundError"``) to triage failures.
    """
    if not error_message:
        return ""
    head = error_message.strip().split(":", 1)[0].splitlines()[0]
    return head[:_SAFE_ERROR_CLASS_LEN]


def _append_log(task_id: str, level: str, message: str) -> None:
    """Append a single line to the per-task log."""
    line = f"{ids.now_iso()} [{level.upper()}] {message}\n"
    p = _log_path(task_id)
    with open(p, "a", encoding="utf-8") as f:
        f.write(line)


def _git_info(workdir: Optional[Path] = None) -> Tuple[Optional[str], Optional[str]]:
    """Best-effort: return (commit, branch) for the current repo, or (None, None)."""
    try:
        cwd = workdir or _BRIDGE_ROOT
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=cwd, stderr=subprocess.DEVNULL, text=True
        ).strip() or None
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd,
            stderr=subprocess.DEVNULL, text=True,
        ).strip() or None
        return commit, branch
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Row -> dataclass
# ---------------------------------------------------------------------------

_COLUMNS = (
    "task_id", "title", "type", "priority", "owner", "status",
    "progress_pct", "progress_step", "created_at", "started_at", "finished_at",
    "duration_sec", "input_text", "hermes_run_id", "openai_run_id", "session_id",
    "mode", "result_path", "error_message", "warning_count", "retry_count",
    "prompt_version", "model_name", "git_commit", "git_branch",
    # AEE-1: runtime-neutral task fields. See
    # `dispatcher/db.py::_AEE1_MIGRATIONS` for the schema origin.
    "runtime_type", "adapter_name", "external_run_id", "worker_id",
    "heartbeat_at", "claim_token_hash", "approval_required", "approval_state",
    # AEE-7.2: per-job repo_root constraint, see
    # `dispatcher/db.py::_AEE72_MIGRATIONS` for the schema origin
    # and `aee/artifacts/policy_factory.py` for the policy
    # resolution. Legacy tasks have NULL here and the manager
    # falls back to `ArtifactPolicy.permissive()` — fail-safe,
    # not fail-open.
    "repo_root",
    # AEE write-side metadata: closes §20.9.10 deferred limitation.
    # `executor_session_id` is stamped at create() time from the
    # caller's `executor_session_id` kwarg; `runtime_run_id` is
    # stamped at start() time from the provider's external run id
    # (the value passed to `manager.start(..., hermes_run_id=...)`,
    # which is aliased to `runtime_run_id` for non-Hermes runtimes).
    # Both are NULLable; legacy tasks keep NULL.
    "executor_session_id", "runtime_run_id",
    # AEE-8.2: read-only profile storage. Persisted at create()
    # time from the wire contract; not enforced. Legacy rows
    # have NULL here and the Task dataclass defaults to None.
    "profile",
    # WO-COMPLETION-GATE-MVP note: ``expected_artifacts`` is
    # decoded from the ``expected_artifacts_json`` storage
    # column in ``_row_to_task`` (same pattern as
    # ``required_capabilities`` above). It is NOT in
    # ``_COLUMNS`` because the storage suffix is decoded
    # explicitly; listing it here would cause a duplicate
    # kwarg error in ``Task(**raw)``.
    #
    # WO-INCOMPLETE-DELIVERY-AUTORESCUE: ``rescue_count`` and
    # ``max_rescues`` are stored as INTEGER columns (with
    # defaults 0 / 1) and read straight into the Task dataclass
    # without any JSON decoding. They ARE in ``_COLUMNS``
    # because there is no ``_json`` storage suffix to strip.
    "rescue_count", "max_rescues",
)


def _row_to_task(row) -> Task:
    """Build a `Task` from a SQLite row.

    AEE-3: the `required_capabilities_json` storage column is
    decoded here into the `required_capabilities: list[str]`
    domain field. Callers that pass through the dataclass
    never see the JSON suffix. The raw `*_json` column is
    NOT in `_COLUMNS` — it's a storage-only detail.

    WO-COMPLETION-GATE-MVP: same pattern for
    `expected_artifacts_json` → `expected_artifacts: list[str]`.
    NULL / malformed JSON → empty list (the default contract
    is "no declared artifacts" → existing behavior preserved).
    """
    raw = {c: row[c] for c in _COLUMNS if c in row.keys()}
    raw["required_capabilities"] = db.decode_capabilities(
        row["required_capabilities_json"]
    )
    # WO-COMPLETION-GATE-MVP: decode the declared-artifacts list.
    # Defensive: NULL / missing / malformed JSON all fall back to
    # the empty list so legacy rows keep the pre-gate behavior.
    ea_raw = row["expected_artifacts_json"] if "expected_artifacts_json" in row.keys() else None
    ea: List[str] = []
    if ea_raw:
        try:
            decoded = json.loads(ea_raw)
            if isinstance(decoded, list):
                ea = [str(p) for p in decoded if isinstance(p, str)]
        except (ValueError, TypeError):
            ea = []
    raw["expected_artifacts"] = ea
    return Task(**raw)


def _row_to_event(row) -> TaskEvent:
    payload = None
    if row["payload_json"]:
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            payload = {"_raw": row["payload_json"]}
    return TaskEvent(
        id=row["id"],
        task_id=row["task_id"],
        ts=row["ts"],
        kind=row["kind"],
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class IllegalTransition(ValueError):
    pass


class TaskNotFound(LookupError):
    pass


class NotificationBlocked(RuntimeError):
    """AEE v3 blocking completion gate — raised by ``TaskManager.complete()``
    when ``enforcement_gate.blocking == true`` AND the notification gate
    fails to confirm delivery (``sent == False`` OR ``message_id is None``).

    The exception is raised AFTER the manager has:

    1. Reverted ``tasks.status`` from ``completed`` back to ``running`` (so
       the orchestrator can retry the notification or escalate).
    2. Persisted the failed-notification blob into
       ``task_outputs.notification_json``.
    3. Emitted a ``NOTIFICATION_FAILED`` event into ``task_events``.
    4. Appended an audit record to ``logs/notification_audit.jsonl``.

    The exception's ``args[0]`` is a dict with keys: ``task_id``,
    ``notification`` (the full gate result dict), ``stage`` (the v3
    completion stage the task reached — always ``evidence_completed``
    under blocking mode because notification did not confirm).

    Callers catching this exception can inspect ``exc.args[0]`` to
    decide between retry / escalate / accept-as-observability-only.
    """

    pass


class TaskManager:
    """All task lifecycle operations. Stateless beyond the DB connection."""

    # ---- create -----------------------------------------------------------

    def create(
        self,
        *,
        title: str,
        type: str,
        input_text: str,
        session_id: Optional[str] = None,
        mode: Optional[str] = None,
        priority: int = 50,
        owner: str = "m2",
        openai_run_id: Optional[str] = None,
        prompt_version: Optional[str] = None,
        model_name: Optional[str] = None,
        workdir: Optional[Path] = None,
        initial_status: str = "queued",
        required_capabilities: Optional[List[str]] = None,
        repo_root: Optional[str] = None,
        executor_session_id: Optional[str] = None,
        profile: Optional[str] = None,
        expected_artifacts: Optional[List[str]] = None,
        max_rescues: Optional[int] = None,
    ) -> Task:
        """Create a new task. Generates the task_id, sets status, records event.

        AEE-3: `required_capabilities` is normalized (lowercased,
        trimmed, deduped, sorted) and persisted in the
        `required_capabilities_json` column. A `None` or empty
        input is stored as '[]' (no capability filter).

        AEE-7.2: ``repo_root`` is the per-job workspace constraint.
        It is persisted in the ``repo_root`` column (NULLable) and
        resolved into an ``ArtifactPolicy`` at ``complete()`` time
        by ``aee.artifacts.policy_factory.policy_for_repo_root``.
        ``None`` means "no per-job constraint" — the manager
        continues to use ``ArtifactPolicy.permissive()`` for that
        task. We deliberately do **not** broaden the existing
        default.

        AEE write-side metadata (closes §20.9.10 deferred
        limitation): ``executor_session_id`` is the caller's
        session id (the orchestrator / ChatGPT session that asked
        for the dispatch). It is persisted in the
        ``executor_session_id`` column (NULLable). Legacy callers
        that don't pass it keep NULL — the read-side identity
        validator falls back to its existing heuristics. Empty
        string / whitespace is normalised to None so the DB
        never sees "" as an executor session id.
        """
        if initial_status not in {"pending", "queued"}:
            raise ValueError(f"initial_status must be pending or queued, got {initial_status}")
        # AEE-7.2: validate repo_root at the wire boundary, not deep
        # in the policy factory. Empty string and whitespace are
        # normalised to None so the DB never sees "" as a repo_root.
        if repo_root is not None:
            repo_root = repo_root.strip() or None
        # AEE write-side metadata: same wire-boundary normalisation
        # for executor_session_id — strip + None-on-empty so legacy
        # callers passing "" don't pollute the column.
        if executor_session_id is not None:
            executor_session_id = executor_session_id.strip() or None
        # AEE-8.2: same wire-boundary normalisation for profile —
        # strip + None-on-empty so the DB never sees "" as a profile.
        # The profile is stored but NOT enforced; this is purely
        # storage plumbing.
        if profile is not None:
            profile = profile.strip() or None
        # WO-COMPLETION-GATE-MVP: normalize expected_artifacts at the
        # wire boundary. None / empty → '[]' (no contract). Non-empty
        # list → sorted unique set of absolute paths persisted as
        # JSON in the `expected_artifacts_json` column. We dedupe
        # and sort so the stored form is deterministic (same input
        # always produces the same JSON blob).
        if expected_artifacts is not None:
            expected_artifacts = sorted(set(expected_artifacts))
        # WO-FIX-ARTIFACT-PATH-CASE-PRESERVATION: persist
        # ``expected_artifacts`` with original case preserved.
        # ``encode_capabilities`` lowercases its inputs (capability
        # strings are case-insensitive identifiers), but Linux
        # filesystem paths are case-sensitive — lowercasing
        # ``/home/ubuntu/Abacus/report.md`` to
        # ``/home/ubuntu/abacus/report.md`` corrupts the contract and
        # produces a false ``missing_expected_artifacts`` failure at
        # ``complete()`` time. Use the dedicated artifact-paths helper
        # which trims/dedupes/sorts WITHOUT case-folding.
        ea_blob = db.encode_artifact_paths(expected_artifacts or [])
        # WO-INCOMPLETE-DELIVERY-AUTORESCUE: clamp ``max_rescues`` to a
        # sensible range. ``None`` means "use the schema default" (1)
        # so legacy callers that don't pass it preserve existing
        # behavior. Negative values are normalized to 0 (rescue
        # disabled — the gate falls through to ``failed`` on the
        # first miss). Capped at 5 to prevent runaway loops from a
        # misconfigured caller.
        if max_rescues is not None:
            max_rescues = max(0, min(int(max_rescues), 5))
        task_id = ids.next_task_id()
        created_at = ids.now_iso()
        commit, branch = _git_info(workdir)
        normalized_caps = db.normalize_capabilities(required_capabilities)
        caps_blob = db.encode_capabilities(normalized_caps)
        with transaction() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                  task_id, title, type, priority, owner, status,
                  progress_pct, created_at,
                  input_text, openai_run_id, session_id, mode,
                  prompt_version, model_name, git_commit, git_branch,
                  required_capabilities_json,
                  repo_root,
                  executor_session_id,
                  profile,
                  expected_artifacts_json,
                  max_rescues
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id, title, type, priority, owner, initial_status,
                    5 if initial_status == "queued" else 0, created_at,
                    input_text, openai_run_id, session_id, mode,
                    prompt_version, model_name, commit, branch,
                    caps_blob,
                    repo_root,
                    executor_session_id,
                    profile,
                    ea_blob,
                    max_rescues if max_rescues is not None else 1,
                ),
            )
        _append_log(task_id, "INFO", f"created title={title!r} type={type} priority={priority}")
        # AEE-7.2 observability: one INFO line at task creation
        # surfaces the per-job repo_root constraint, mode, and
        # type so the dispatcher log is queryable end-to-end
        # without joining against the events table. The path
        # is logged once (per_job_root) — ``input_text`` is
        # intentionally NOT included.
        # AEE write-side metadata: extend the same single INFO
        # line with the caller's executor_session_id (if set)
        # so the dispatcher log remains the single source of
        # truth for "who asked for this task". Same guard as
        # repo_root: blank when None, no PII, no input_text.
        log.info(
            "manager.create: task_id=%s type=%s mode=%s "
            "per_job_root=%s executor_session=%s",
            task_id,
            type,
            mode,
            repo_root or "",
            executor_session_id or "",
        )
        self._emit_event(task_id, EventKind.CREATED, {
            "title": title, "type": type, "priority": priority, "owner": owner,
            "session_id": session_id, "mode": mode, "openai_run_id": openai_run_id,
            "prompt_version": prompt_version, "model_name": model_name,
            # AEE-3: capability filter is part of the create event so
            # downstream consumers (audit, scheduler) see what was
            # required without having to re-fetch the task.
            "required_capabilities": normalized_caps,
            # AEE-7.2: only emit the field when actually set so the
            # event log stays compact for the legacy default case.
            **({"repo_root": repo_root} if repo_root else {}),
            # AEE write-side metadata: only emit when set, same
            # compact-log policy as repo_root.
            **({"executor_session_id": executor_session_id} if executor_session_id else {}),
        })
        if initial_status == "queued":
            _append_log(task_id, "INFO", "queued — waiting for dispatcher worker")
            self._emit_event(task_id, EventKind.QUEUED, None)
        return self.get(task_id)  # type: ignore[return-value]

    # ---- read -------------------------------------------------------------

    def get(self, task_id: str) -> Optional[Task]:
        conn = get_conn()
        row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    def get_or_raise(self, task_id: str) -> Task:
        t = self.get(task_id)
        if t is None:
            raise TaskNotFound(task_id)
        return t

    def list(
        self,
        *,
        status: Optional[str] = None,
        type: Optional[str] = None,
        limit: int = 50,
    ) -> List[Task]:
        conn = get_conn()
        sql = "SELECT * FROM tasks"
        params: List[Any] = []
        clauses = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if type:
            clauses.append("type = ?")
            params.append(type)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_task(r) for r in rows]

    def events(self, task_id: str, limit: int = 500) -> List[TaskEvent]:
        conn = get_conn()
        rows = conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT ?",
            (task_id, int(limit)),
        ).fetchall()
        return [_row_to_event(r) for r in reversed(rows)]

    def find_by_hermes_run_id(self, hermes_run_id: str) -> Optional[Task]:
        conn = get_conn()
        row = conn.execute(
            "SELECT * FROM tasks WHERE hermes_run_id = ? ORDER BY created_at DESC LIMIT 1",
            (hermes_run_id,),
        ).fetchone()
        return _row_to_task(row) if row else None

    # ---- state transitions ------------------------------------------------

    def _set_status(self, task_id: str, new_status: str) -> None:
        """Validate transition, update status, record event."""
        conn = get_conn()
        row = conn.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        old = row["status"]
        if old == new_status:
            return  # no-op
        if not is_legal_transition(old, new_status):
            raise IllegalTransition(f"{task_id}: {old} -> {new_status} not allowed")
        conn.execute(
            "UPDATE tasks SET status = ? WHERE task_id = ?",
            (new_status, task_id),
        )
        _append_log(task_id, "INFO", f"status {old} -> {new_status}")
        self._emit_event(task_id, EventKind.STATUS, {"from": old, "to": new_status})

    def queue(self, task_id: str) -> Task:
        self._set_status(task_id, "queued")
        return self.get_or_raise(task_id)

    def start(self, task_id: str, hermes_run_id: str) -> Task:
        ts = ids.now_iso()
        conn = get_conn()
        row = conn.execute("SELECT status, started_at FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        old = row["status"]
        if not is_legal_transition(old, "running"):
            raise IllegalTransition(f"{task_id}: {old} -> running not allowed")
        # AEE write-side metadata: stamp the provider's external
        # run id onto the new `runtime_run_id` column. This is
        # the first time the dispatcher knows which provider
        # accepted the job, so it is the authoritative point to
        # record it. The legacy `hermes_run_id` column keeps the
        # same value (it's the same run id — just exposed under
        # the runtime-neutral name now). Idempotent: re-starting
        # a running task re-stamps the same value.
        with transaction() as conn2:
            started_at = row["started_at"] or ts
            conn2.execute(
                "UPDATE tasks SET status = 'running', hermes_run_id = ?, runtime_run_id = ?, started_at = ? WHERE task_id = ?",
                (hermes_run_id, hermes_run_id, started_at, task_id),
            )
        _append_log(task_id, "INFO", f"started hermes_run_id={hermes_run_id}")
        self._emit_event(task_id, EventKind.STARTED, {"hermes_run_id": hermes_run_id, "runtime_run_id": hermes_run_id})
        return self.get_or_raise(task_id)

    def progress(self, task_id: str, pct: int, step: Optional[str] = None) -> Task:
        """Update progress. Pct must be in LEGAL_PROGRESS_PCTS and >= current."""
        validate_progress(pct)
        conn = get_conn()
        row = conn.execute(
            "SELECT status, progress_pct FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        err = monotonic(row["progress_pct"], pct)
        if err:
            raise ValueError(err)
        with transaction() as conn2:
            conn2.execute(
                "UPDATE tasks SET progress_pct = ?, progress_step = ? WHERE task_id = ?",
                (pct, step, task_id),
            )
        _append_log(task_id, "PROGRESS", f"{pct}% {step or ''}".rstrip())
        self._emit_event(task_id, EventKind.PROGRESS, {"pct": pct, "step": step})
        return self.get_or_raise(task_id)

    def log(self, task_id: str, line: str) -> None:
        """Append a free-form line to the task log + record a 'log' event."""
        _append_log(task_id, "LOG", line)
        self._emit_event(task_id, EventKind.LOG, {"line": line[:500]})

    def warning(self, task_id: str, message: str) -> None:
        conn = get_conn()
        conn.execute(
            "UPDATE tasks SET warning_count = warning_count + 1 WHERE task_id = ?",
            (task_id,),
        )
        _append_log(task_id, "WARN", message)
        self._emit_event(task_id, EventKind.WARNING, {"message": message[:500]})

    # WO-INCOMPLETE-DELIVERY-AUTORESCUE: automatic rescue re-validation.
    # Called from ``complete()`` when the completion gate fires with
    # rescue eligibility (``rescue_count < max_rescues``). The rescue
    # does NOT re-execute the task — it uses the persisted evidence
    # (the declared ``expected_artifacts`` list + the
    # ``missing_declared`` paths captured at gate time) and
    # re-validates the artifacts on disk. This is the MVP rescue
    # pattern: in production the missing artifacts often appear
    # shortly after ``complete()`` (e.g. the agent's ``write`` tool
    # call completes after the manager's gate check), so a single
    # synchronous re-validation closes the race without burning a
    # full retry.
    #
    # Loop prevention: ``rescue_count`` is incremented atomically
    # in the same transaction that transitions the task back to
    # ``running``. The next miss (if artifacts are still missing)
    # sees ``rescue_count == max_rescues`` and falls through to
    # ``failed`` — there is no recursive rescue.
    #
    # This method is the single producer of the
    # ``incomplete_delivery -> running`` transition; the public
    # state machine in ``LEGAL_TRANSITIONS`` permits it but no
    # other call site uses it.
    def _rescue(
        self,
        task_id: str,
        *,
        declared_artifacts: List[str],
        missing_paths: List[str],
    ) -> Task:
        """WO-INCOMPLETE-DELIVERY-AUTORESCUE: re-validate declared
        artifacts and either complete or fail the task.

        ``declared_artifacts`` is the persisted list of artifact
        paths the caller declared at create() time.
        ``missing_paths`` is the subset that was missing at gate
        time (passed in so the rescue does not need to re-scan;
        it re-stats just those paths).

        The rescue transitions ``incomplete_delivery -> running``
        (incrementing ``rescue_count``), re-stats the missing
        paths, and:

        * If all declared artifacts now exist → transitions to
          ``completed`` (via the standard complete() path, which
          re-runs the gate; the gate passes because the
          artifacts are present).
        * If any declared artifact is still missing → transitions
          to ``failed`` with the deterministic
          ``missing_expected_artifacts`` reason.

        Idempotent for the same task: calling ``_rescue()`` twice
        in a row is safe — the second call sees
          ``status='running'`` (or ``completed`` / ``failed``) and
        the state machine guards prevent the second transition.
        """
        # Atomically transition ``incomplete_delivery -> running``
        # AND increment ``rescue_count`` in the same transaction
        # so the loop counter is consistent with the observed
        # state. The transition is guarded by
        # ``is_legal_transition`` (the
        # ``incomplete_delivery -> running`` edge is in
        # ``LEGAL_TRANSITIONS``).
        conn = get_conn()
        row = conn.execute(
            "SELECT status, rescue_count, max_rescues FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        old_status = row["status"]
        if not is_legal_transition(old_status, "running"):
            # The task is no longer in ``incomplete_delivery``
            # (e.g. a concurrent caller already rescued it, or it
            # was cancelled). This is a no-op for the rescue: we
            # return the current state without raising so the
            # caller's ``complete()`` does not blow up.
            _append_log(
                task_id, "INFO",
                f"rescue: skipped (status={old_status}, "
                f"not incomplete_delivery)",
            )
            return self.get_or_raise(task_id)
        cur_count = int(row["rescue_count"]) if row["rescue_count"] is not None else 0
        with transaction() as conn_rescue:
            conn_rescue.execute(
                "UPDATE tasks SET status='running', "
                "rescue_count = rescue_count + 1, "
                "error_message=NULL WHERE task_id=?",
                (task_id,),
            )
        _append_log(
            task_id, "INFO",
            f"rescue: incomplete_delivery -> running "
            f"(rescue_count {cur_count} -> {cur_count + 1})",
        )
        self._emit_event(task_id, EventKind.STATUS, {
            "from": "incomplete_delivery",
            "to": "running",
            "reason": "auto_rescue_revalidation",
            "rescue_count": cur_count + 1,
        })
        # Re-stat the previously-missing paths. If they all exist
        # now, re-enter ``complete()`` so the standard completion
        # path runs (Phase-4 auto-scan, notification gate, etc.).
        # The gate will re-check ALL declared artifacts (not just
        # the previously-missing subset) for safety.
        still_missing: List[str] = []
        for p in missing_paths:
            try:
                os.stat(p)
            except OSError:
                still_missing.append(p)
        if not still_missing:
            _append_log(
                task_id, "INFO",
                "rescue: all declared artifacts now present; "
                "completing",
            )
            # Re-enter complete() so the standard completion
            # path runs. The gate will re-check the declared
            # artifacts; since they are now present, the gate
            # passes and the task reaches ``completed``.
            # ``output_text`` is None because the agent's
            # original output was already recorded at the first
            # complete() call (in ``task_outputs``); passing
            # None here means ``complete()`` does not overwrite
            # the persisted output.
            return self.complete(task_id, output_text=None)
        # Artifacts still missing after the rescue attempt.
        # Transition to ``failed`` with the deterministic
        # reason. The ``rescue_count`` is now >=
        # ``max_rescues`` (because the rescue incremented it),
        # so the next ``complete()`` would fall through to
        # ``failed`` anyway — but we short-circuit here to
        # avoid the extra round-trip.
        missing_repr = ", ".join(still_missing)
        gate_error = (
            f"missing_expected_artifacts: {len(still_missing)} of "
            f"{len(declared_artifacts)} declared artifact(s) still missing "
            f"after rescue: {missing_repr}"
        )[:500]
        _append_log(
            task_id, "ERROR",
            f"rescue: {len(still_missing)} of "
            f"{len(declared_artifacts)} declared artifact(s) "
            f"still missing after rescue",
        )
        self._emit_event(task_id, EventKind.DELIVERY_UNVERIFIED, {
            "gate": "missing_expected_artifacts_post_rescue",
            "declared_count": len(declared_artifacts),
            "missing_count": len(still_missing),
            "missing_paths": still_missing,
            "rescue_count": cur_count + 1,
        })
        return self.fail(task_id, gate_error)

    def complete(
        self,
        task_id: str,
        *,
        output_text: Optional[str] = None,
        usage: Optional[Dict[str, Any]] = None,
        raw: Optional[Dict[str, Any]] = None,
        model_name: Optional[str] = None,
    ) -> Task:
        ts = ids.now_iso()
        conn = get_conn()
        row = conn.execute(
            "SELECT status, started_at, model_name, input_text, expected_artifacts_json, rescue_count, max_rescues FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        if not is_legal_transition(row["status"], "completed"):
            raise IllegalTransition(f"{task_id}: {row['status']} -> completed not allowed")
        duration = _compute_duration(row["started_at"], ts)
        result_path = _write_result(task_id, output_text, usage, raw)
        # Auto-derive model_name from raw.model if not explicitly passed.
        if model_name is None and isinstance(raw, dict):
            model_name = raw.get("model")
        # Preserve existing model_name if we have nothing new.
        effective_model = model_name or row["model_name"]

        # Phase 4: delivery verification — scan the task's input for any
        # absolute file paths that look like a contract (the agent was told
        # to produce / verify a file at a specific path). If we find any,
        # stat() each one and record existence + size + mtime. A missing
        # expected file bumps warning_count so the task surfaces as
        # "completed but unverified" — never silently green.
        delivery = self._verify_expected_delivery(task_id, row["input_text"] or "")

        # WO-COMPLETION-GATE-MVP: deterministic completion gate for
        # explicitly-declared `expected_artifacts`. Unlike the Phase-4
        # auto-scan (which is observability-only and never blocks the
        # `completed` transition), this gate is a HARD gate: if the
        # caller declared any artifact paths at create() time and any
        # of them don't exist on disk at completion time, the task
        # transitions to `failed` with reason `missing_expected_artifacts`
        # INSTEAD of `completed`. Empty list / NULL = no contract →
        # existing behavior preserved.
        ea_raw = row["expected_artifacts_json"] if "expected_artifacts_json" in row.keys() else None
        declared_artifacts: List[str] = []
        if ea_raw:
            try:
                decoded_ea = json.loads(ea_raw)
                if isinstance(decoded_ea, list):
                    declared_artifacts = [str(p) for p in decoded_ea if isinstance(p, str)]
            except (ValueError, TypeError):
                declared_artifacts = []
        missing_declared: List[str] = []
        if declared_artifacts:
            for p in declared_artifacts:
                try:
                    os.stat(p)
                except OSError:
                    missing_declared.append(p)
        if missing_declared:
            # Deterministic non-success outcome. The task does NOT
            # reach `completed`. We use `failed` with an explicit
            # reason prefix so downstream consumers can pattern-match
            # on `missing_expected_artifacts` without parsing prose.
            # The full list of missing paths is included (best-effort,
            # truncated to 500 chars to fit the error_message column).
            missing_repr = ", ".join(missing_declared)
            gate_error = (
                f"missing_expected_artifacts: {len(missing_declared)} of "
                f"{len(declared_artifacts)} declared artifact(s) missing: "
                f"{missing_repr}"
            )[:500]
            _append_log(
                task_id, "ERROR",
                f"completion gate: {len(missing_declared)} of "
                f"{len(declared_artifacts)} declared artifact(s) missing",
            )
            self._emit_event(task_id, EventKind.DELIVERY_UNVERIFIED, {
                "gate": "missing_expected_artifacts",
                "declared_count": len(declared_artifacts),
                "missing_count": len(missing_declared),
                "missing_paths": missing_declared,
            })
            # WO-INCOMPLETE-DELIVERY-AUTORESCUE: deterministic rescue
            # loop prevention. When the task has rescue budget left
            # (``rescue_count < max_rescues``) the gate transitions
            # to ``incomplete_delivery`` (non-terminal) and queues
            # exactly one automatic ``_rescue()`` re-validation
            # using the persisted evidence (declared_artifacts +
            # the missing-paths list). The rescue increments
            # ``rescue_count`` and re-checks the artifacts; on
            # success the task reaches ``completed``, on failure
            # it reaches ``failed``. When ``rescue_count >=
            # max_rescues`` (or rescue is disabled via
            # ``max_rescues == 0``) the gate falls through to
            # ``failed`` directly — no rescue attempt is made.
            cur_rescue_count = int(row["rescue_count"]) if "rescue_count" in row.keys() and row["rescue_count"] is not None else 0
            cur_max_rescues = int(row["max_rescues"]) if "max_rescues" in row.keys() and row["max_rescues"] is not None else 1
            rescue_eligible = cur_rescue_count < cur_max_rescues
            if rescue_eligible:
                # Transition to non-terminal ``incomplete_delivery``
                # so the orchestrator (GPT) can observe the rescue
                # in-flight state via the read API. ``_rescue()``
                # then runs immediately (synchronously) using the
                # persisted evidence — it does NOT re-execute the
                # full task, only re-validates the declared
                # artifacts and either completes or fails the task.
                with transaction() as conn_rescue_gate:
                    conn_rescue_gate.execute(
                        "UPDATE tasks SET status='incomplete_delivery', "
                        "error_message=? WHERE task_id=?",
                        (gate_error, task_id),
                    )
                self._emit_event(task_id, EventKind.STATUS, {
                    "from": "running",
                    "to": "incomplete_delivery",
                    "reason": "missing_expected_artifacts_rescue_eligible",
                    "rescue_count": cur_rescue_count,
                    "max_rescues": cur_max_rescues,
                    "missing_paths": missing_declared,
                })
                _append_log(
                    task_id, "INFO",
                    f"completion gate: rescue eligible "
                    f"({cur_rescue_count}/{cur_max_rescues}); "
                    f"transitioning to incomplete_delivery",
                )
                # Auto-queue one rescue attempt. ``_rescue()`` is
                # idempotent for the same task: it transitions back
                # to ``running``, re-stats the declared artifacts,
                # and either completes or fails. The
                # ``rescue_count`` increment is the loop
                # prevention — once it reaches ``max_rescues`` the
                # next miss falls through to ``failed``.
                return self._rescue(
                    task_id,
                    declared_artifacts=declared_artifacts,
                    missing_paths=missing_declared,
                )
            # Rescue budget exhausted (or disabled). Fall through to
            # ``failed`` with the deterministic reason prefix. This
            # preserves the WO-COMPLETION-GATE-MVP contract: the
            # task transitions to ``failed`` (NOT ``completed``)
            # with the explicit reason so downstream consumers can
            # pattern-match on ``missing_expected_artifacts``.
            _append_log(
                task_id, "INFO",
                f"completion gate: rescue budget exhausted "
                f"({cur_rescue_count}/{cur_max_rescues}); failing",
            )
            # Delegate to fail() for the state transition + event emit
            # + executor_runs mirror. fail() emits FAILED event.
            return self.fail(task_id, gate_error)

        # AEE-7.2 observability: emit one structured INFO line at
        # terminal status so operators can grep for `task.complete`
        # without scanning per-task log files. ``model_name`` and
        # ``duration_sec`` are safe (no secrets); ``warning_bump``
        # is the missing-artifact count from Phase 4 / AEE-6.2.
        # NOTE: ``input_text`` is intentionally NOT included to
        # avoid leaking the orchestrator's prompt into a
        # per-line log.
        log.info(
            "manager.complete: task_id=%s status=completed "
            "model=%s duration_sec=%.2f warning_bump=%d "
            "artifacts=%d missing=%d",
            task_id,
            effective_model or "",
            duration,
            delivery["warning_bump"],
            len(delivery.get("artifacts") or []),
            len(delivery.get("missing_paths") or []),
        )

        with transaction() as conn2:
            conn2.execute(
                "UPDATE tasks SET status='completed', progress_pct=100, finished_at=?, "
                "duration_sec=?, result_path=?, model_name=?, warning_count = warning_count + ? "
                "WHERE task_id = ?",
                (ts, duration, result_path, effective_model, delivery["warning_bump"], task_id),
            )
            if output_text is not None or usage is not None or raw is not None or delivery["artifacts"]:
                conn2.execute(
                    "INSERT OR REPLACE INTO task_outputs (task_id, output_text, usage_json, raw_json, delivery_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        task_id,
                        output_text,
                        json.dumps(usage) if usage else None,
                        json.dumps(raw, default=str) if raw else None,
                        json.dumps(delivery["artifacts"], ensure_ascii=False) if delivery["artifacts"] else None,
                    ),
                )
        if delivery["warning_bump"] > 0:
            _append_log(
                task_id,
                "WARN",
                f"delivery verification: {delivery['warning_bump']} expected file(s) missing",
            )
            for missing in delivery["missing_paths"]:
                self._emit_event(task_id, EventKind.DELIVERY_UNVERIFIED, {"missing_path": missing})
        # Phase 4.1: intent-mismatch detection — if the agent's final
        # output ends in a "declarative intent" sentence (now let me
        # write, now writing, will create, ...) AND we have already
        # bumped warning_count for missing files, this is the exact
        # pattern where the LLM said "I'm about to do X" but never did.
        # Emit a high-priority intent_mismatch event so the orchestrator
        # can short-circuit without parsing the output text.
        intent = self._detect_intent_mismatch(
            output_text, delivery["missing_paths"]
        )
        if intent is not None:
            _append_log(task_id, "WARN", f"intent_mismatch: {intent['matched_pattern']!r}")
            self._emit_event(task_id, EventKind.INTENT_MISMATCH, intent)
        _append_log(task_id, "INFO", f"completed duration={duration:.2f}s")
        self._emit_event(task_id, EventKind.COMPLETED, {
            "duration_sec": duration, "result_path": result_path,
        })
        # AEE v3 Telegram Completion Enforcement Gate — fire notification
        # via the Hermes Telegram Gateway (the working path). The legacy
        # notifier.notify_completed is the silent fallback. The gate's
        # result is recorded in task_outputs.notification_json; a missing
        # / failed notification leaves the task in `notification_pending`
        # (non-terminal under the v3 model) but does NOT block the
        # existing `status='completed'` for backward compatibility —
        # the gate is observability-enforcement, not state-machine-blocking,
        # in this iteration (a future iteration can flip to blocking once
        # the 7-day shadow run is green).
        try:
            from dispatcher.notifier import notify_completed_with_fallback
            notif = notify_completed_with_fallback(task_id)
            notif_blob = json.dumps(notif, default=str, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 — never raise from the gate
            notif = {"sent": False, "method": "failed", "last_error": f"gate exception: {exc}"}
            notif_blob = json.dumps(notif, default=str, ensure_ascii=False)
            log.warning("manager.complete: notification gate exception task_id=%s err=%s", task_id, exc)
        # Persist notification_json into task_outputs (idempotent UPDATE —
        # the row may have been INSERTed above; use INSERT OR REPLACE pattern
        # mirroring the delivery_json write at line 527, but only updating
        # the notification_json column to avoid clobbering output_text/usage/raw).
        with transaction() as conn3:
            # If the row exists, UPDATE only notification_json. If not, INSERT
            # a stub row with NULLs for the other columns.
            cur = conn3.execute(
                "SELECT 1 FROM task_outputs WHERE task_id = ?", (task_id,)
            )
            if cur.fetchone() is None:
                conn3.execute(
                    "INSERT INTO task_outputs (task_id, notification_json) VALUES (?, ?)",
                    (task_id, notif_blob),
                )
            else:
                conn3.execute(
                    "UPDATE task_outputs SET notification_json = ? WHERE task_id = ?",
                    (notif_blob, task_id),
                )
        # Emit the appropriate event for the gate result.
        if notif.get("sent") and notif.get("message_id") is not None:
            self._emit_event(task_id, EventKind.NOTIFICATION_COMPLETED, {
                "method": notif.get("method"),
                "recipient": notif.get("recipient"),
                "message_id": notif.get("message_id"),
                "ts_utc": notif.get("ts_utc"),
                "ts_taipei": notif.get("ts_taipei"),
            })
        elif notif.get("sent") and notif.get("message_id") is None:
            # sent but no message_id — treat as pending (queued but not delivered)
            self._emit_event(task_id, EventKind.NOTIFICATION_PENDING, {
                "method": notif.get("method"),
                "last_error": notif.get("last_error"),
            })
        else:
            self._emit_event(task_id, EventKind.NOTIFICATION_FAILED, {
                "method": notif.get("method"),
                "last_error": notif.get("last_error"),
            })
        # Structured completion log — extend the AEE-7.2 INFO line
        # above with the notification gate's outcome so the dispatcher
        # log remains the single source of truth for the v3 gate
        # without requiring a join against task_outputs.
        log.info(
            "manager.complete: notification gate task_id=%s "
            "notif_sent=%s notif_method=%s notif_msg_id=%s",
            task_id,
            bool(notif.get("sent")),
            notif.get("method", ""),
            notif.get("message_id"),
        )
        # AEE v3 blocking completion gate — runtime enforcement.
        # When ``enforcement_gate.blocking == true`` AND the
        # notification gate did NOT confirm delivery (``sent ==
        # False`` OR ``message_id is None``), the manager reverts
        # the just-set ``status='completed'`` back to ``running`` so
        # the orchestrator can retry the notification or escalate,
        # and raises ``NotificationBlocked`` so the caller knows the
        # task did NOT reach ``FINAL_COMPLETED``.
        #
        # Config loading is defensive: any error reading
        # ``enforcement_gate.blocking`` defaults to ``False``
        # (observability-only) so a malformed config never blocks
        # task completion silently.
        blocking = False
        try:
            from config import load as _load_config
            _gate_cfg = _load_config("notify").get("enforcement_gate", {})
            blocking = bool(_gate_cfg.get("blocking", False))
        except Exception as _cfg_exc:  # noqa: BLE001 — never block on config error
            log.warning(
                "manager.complete: enforcement_gate config read failed task_id=%s err=%s; "
                "defaulting to observability-only",
                task_id, _cfg_exc,
            )
            blocking = False
        notif_confirmed = bool(notif.get("sent")) and notif.get("message_id") is not None
        if blocking and not notif_confirmed:
            # Revert the just-set ``status='completed'`` back to
            # ``running`` so the task is no longer terminal. The
            # revert uses a direct SQL UPDATE (NOT through
            # ``is_legal_transition``) because ``completed ->
            # running`` is intentionally NOT in the public
            # ``LEGAL_TRANSITIONS`` table (completed is terminal for
            # external callers). The revert is gated on
            # ``enforcement_gate.blocking`` and only fires from
            # inside ``complete()``, which is the single producer of
            # the ``completed`` status.
            #
            # ``finished_at`` is cleared so the task is no longer
            # terminal from the read-side ``compute_completion_state``
            # perspective (it gates on ``finished_at`` for
            # ``EVIDENCE_COMPLETED``).
            #
            # ``progress_pct`` is rolled back from 100 to the last
            # running value (80 — the pre-completion ceiling in
            # ``LEGAL_PROGRESS_PCTS``) so the dispatcher UI does not
            # show 100% on a reverted task.
            try:
                with transaction() as conn_revert:
                    conn_revert.execute(
                        "UPDATE tasks SET status='running', "
                        "finished_at=NULL, progress_pct=80 "
                        "WHERE task_id=? AND status='completed'",
                        (task_id,),
                    )
            except Exception as revert_exc:  # noqa: BLE001 — log + continue
                log.warning(
                    "manager.complete: blocking-gate revert failed task_id=%s err=%s",
                    task_id, revert_exc,
                )
            log.warning(
                "manager.complete: blocking gate reverted task_id=%s "
                "notif_sent=%s notif_method=%s notif_msg_id=%s "
                "(enforcement_gate.blocking=true, notification unconfirmed)",
                task_id,
                bool(notif.get("sent")),
                notif.get("method", ""),
                notif.get("message_id"),
            )
            raise NotificationBlocked({
                "task_id": task_id,
                "notification": notif,
                "stage": "evidence_completed",
                "blocking": True,
                "reason": (
                    "enforcement_gate.blocking=true and notification "
                    "gate did not confirm message_id"
                ),
            })
        # Task-Mapping work-order (Fix D): mirror the terminal
        # ``completed`` status into ``executor_runs`` so GET /runs
        # list/summary reflect the true lifecycle. Best-effort.
        self._sync_executor_runs_status(task_id, status="completed", exit_code=0)
        return self.get_or_raise(task_id)

    def _verify_expected_delivery(self, task_id: str, input_text: str) -> Dict[str, Any]:
        """Phase 4 + AEE-6.2: scan the task's input_text for absolute file
        paths and stat + hash + classify each one. If the agent was told
        "create file at /foo/bar", we can detect that path in the input
        and verify it exists at complete() time.

        Why input scan instead of explicit field? Because ChatGPT's
        prompts are natural-language. A separate `expected_artifacts`
        field is more explicit, but we can also offer a "best-effort
        detect" layer for free with zero prompt changes. Both layers
        can coexist.

        AEE-6.2: the per-path stat+hash is now done by the AEE-6
        `ArtifactCollector` so the same code path that the orchestrator
        can use (POST /v1/artifacts) is also wired into delivery
        verification. The legacy `delivery_json` shape (list of
        {path, exists, size, mtime}) is preserved 100%; we just add
        three more optional fields:

            {
                "path":     str,
                "exists":   bool,
                "size":     int | None,
                "mtime":    str | None,
                "sha256":   str | None,       # NEW in AEE-6.2
                "kind":     str,              # NEW in AEE-6.2
                "artifact_id": str | None,   # NEW in AEE-6.2 (None if missing)
            }

        Backward compatibility: every pre-AEE-6.2 test that checks
        only the 4 legacy fields keeps passing.

        Returns:
            {
                "artifacts":     [delivery entries, shape above],
                "missing_paths": [str],        # subset with exists=False
                "warning_bump":  int,          # 0 if nothing expected, N if N missing
            }
        """
        # Strict absolute-path regex: /foo/bar or /foo/bar.txt — must
        # contain at least one slash, no shell metachars, no spaces.
        # This intentionally misses things like "~/x" or quoted paths;
        # better to under-detect than to flag false positives.
        candidate_paths: List[str] = []
        seen: set = set()
        for match in re.finditer(
            r"(?:^|[\s,;\"'`])(/[A-Za-z0-9_\-./]+\.[A-Za-z0-9]{1,8})",
            input_text,
        ):
            p = match.group(1)
            if p in seen:
                continue
            seen.add(p)
            candidate_paths.append(p)

        if not candidate_paths:
            return {
                "artifacts": [],
                "missing_paths": [],
                "warning_bump": 0,
            }

        # AEE-6.2: build a per-task ArtifactPipeline on top of the
        # existing SQLite connection. The collector raises on
        # permission / too-large / hard OS errors; the pipeline
        # catches them and returns an `exists=False` record. So
        # this call is "best-effort" in the same way the legacy
        # os.stat block was — never raises out.
        from aee.artifacts import ArtifactPipeline, SqliteArtifactRepository
        # AEE-7.2: read the per-job repo_root from the task and turn
        # it into an ArtifactPolicy. The factory is fail-safe:
        # missing/empty repo_root → None, caller keeps its
        # permissive default. We do **not** silently widen the
        # default to repo_root; a per-job constraint is opt-in.
        row = get_conn().execute(
            "SELECT repo_root FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        repo_root = row["repo_root"] if row else None
        from aee.artifacts.policy_factory import policy_for_repo_root
        per_job_policy = policy_for_repo_root(repo_root)
        if per_job_policy is not None:
            log.info(
                "manager._verify_expected_delivery: per-job repo_root enforced task=%s root=%s",
                task_id,
                per_job_policy.allowed_roots[0],
            )
        pipeline = ArtifactPipeline(
            repo=SqliteArtifactRepository(get_conn()),
        )
        # AEE-7.2: overwrite the pipeline's default policy only when
        # a per-job constraint was set. ArtifactPipeline.policy is
        # typed ArtifactPolicy (no None), so we can't pass None
        # through the constructor — the only safe override is to
        # assign after construction. A None here means "keep the
        # pipeline's own permissive default", which is exactly what
        # the legacy code path did (AEE-6.3 + AEE-6.2).
        if per_job_policy is not None:
            pipeline.policy = per_job_policy
        try:
            persisted = pipeline.collect(task_id, candidate_paths)
        except Exception as e:  # pragma: no cover - defensive
            # If the pipeline explodes for any reason (e.g. table
            # missing because someone bypassed _init_schema), fall
            # back to a no-artifact delivery so the task still
            # completes. We log but do not raise.
            #
            # AEE-7.1: use the module logger (was a ``print(..., file=sys.stderr)``
            # which bypassed the project's logging config).
            log.warning(
                "manager._verify_expected_delivery: AEE-6.2 ArtifactPipeline failed: %s: %s",
                type(e).__name__,
                e,
                exc_info=True,
            )
            return {
                "artifacts": [],
                "missing_paths": [],
                "warning_bump": 0,
            }

        # Build the legacy-shape delivery entries, enriched with
        # AEE-6 metadata. We map each persisted Artifact back to
        # the original path string the user wrote (so the
        # `delivery_json` regex input round-trips).
        artifacts: List[Dict[str, Any]] = []
        missing: List[str] = []
        for art in persisted:
            entry: Dict[str, Any] = {
                "path": art.path,
                "exists": art.exists,
                "size": art.size,
                "mtime": art.mtime,
                # AEE-6.2 additions — back-compat safe (all optional):
                "sha256": art.sha256,
                "kind": art.kind,
                "artifact_id": art.artifact_id,
            }
            artifacts.append(entry)
            if not art.exists:
                missing.append(art.path)
        return {
            "artifacts": artifacts,
            "missing_paths": missing,
            "warning_bump": len(missing),
        }

    # Phase 4.1: declarative-intent patterns observed in 4 consecutive
    # failed macro-report tasks. Each pattern below is a literal
    # substring that the LLM emitted in the final assistant message
    # when it had decided internally NOT to perform the action but
    # still wrote an "I'm about to do X" sentence as the final answer.
    # Keep this list short and verbatim — false positives are loud
    # (every "I will create the file" success would trigger it).
    _INTENT_PATTERNS = (
        "now let me write",
        "now writing",
        "will create",
        "will write",
        "let me create",
        "let me write",
        "now let me create",
        "now let me draft",
        "i will now write",
        "i'll now write",
        "i will now create",
        "i'll now create",
    )

    def _detect_intent_mismatch(
        self,
        output_text: Optional[str],
        missing_paths: List[str],
    ) -> Optional[Dict[str, Any]]:
        """Phase 4.1: detect the "I said I would but didn't" pattern.

        Fires only when BOTH conditions hold:
          1. There is at least one missing expected artifact
             (delivery verification already raised warning_count).
          2. The agent's final output_text ends in (or contains in its
             tail) one of the declarative-intent patterns.

        Without (1) the agent might be legitimately declaring intent
        and following through (e.g. "Now writing the report" before
        the write tool fires — a hook that fires on every success
        would create noise). The combination is the smoking gun.

        Returns a structured payload for the intent_mismatch event, or
        None if no mismatch detected.
        """
        if not output_text or not missing_paths:
            return None
        # Only inspect the tail (last 600 chars) — the bug pattern
        # always manifests near the end of the response, and limiting
        # the search window avoids matching legitimate in-text
        # references like "earlier I will create the foo module".
        tail = output_text[-600:].lower()
        matched: Optional[str] = None
        for pat in self._INTENT_PATTERNS:
            if pat in tail:
                matched = pat
                break
        if matched is None:
            return None
        return {
            "matched_pattern": matched,
            "missing_paths": list(missing_paths),
            "output_tail": output_text[-300:],
            "severity": "high",
            "recommended_action": "retry_with_explicit_write_instruction",
        }

    # ------------------------------------------------------------------
    # Task-Mapping work-order: sync ``executor_runs`` row on
    # terminal lifecycle transitions (complete/fail/timeout/cancel).
    # Root cause C1 of the investigation report: ``manager.complete()``
    # and ``manager.fail()`` only updated the ``tasks`` table, leaving
    # the ``executor_runs`` row stuck at its last non-terminal status
    # (e.g. ``running`` forever). This helper mirrors the terminal
    # status into ``executor_runs`` so that ``GET /runs`` list /
    # summary / full-get reflect the true lifecycle state.
    #
    # The sync is keyed on ``run_id`` (the canonical key in
    # ``executor_runs``). For Hermes-dispatched runs the run_id is
    # the task's ``external_run_id`` / ``hermes_run_id``; for
    # executor runs it was passed into ``upsert_run`` at dispatch
    # time. We look up the run_id by ``task_id`` (the
    # ``executor_runs.task_id`` column is non-NULL for newly created
    # runs per Fix B; for pre-fix runs it may be NULL and the sync
    # is a no-op).
    #
    # Best-effort: a sync failure MUST NOT block the lifecycle
    # transition. The ``tasks`` table is already updated; this helper
    # runs after the transition has committed and only logs on error.
    # Idempotent: ``upsert_run`` is INSERT OR REPLACE on ``run_id``,
    # so repeated syncs with the same status are safe.
    def _sync_executor_runs_status(
        self,
        task_id: str,
        *,
        status: str,
        exit_code: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        try:
            from dispatcher.executor_runs import upsert_run
            conn = get_conn()
            trow = conn.execute(
                "SELECT hermes_run_id, external_run_id, adapter_name "
                "FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if trow is None:
                return
            run_id = trow["hermes_run_id"] or trow["external_run_id"]
            if not run_id:
                # No upstream run id — this task was never dispatched
                # to a runtime that produces a run_id (e.g. a
                # rejected/synthetic task). Nothing to sync.
                return
            _terminal = status in {"completed", "failed", "timeout", "cancelled"}
            _phase = "terminal" if _terminal else (
                "queued" if status in {"queued", "pending"} else "running"
            )
            _step = status if _terminal else (
                "queued" if status in {"queued", "pending"} else "running"
            )
            upsert_run(
                conn,
                run_id=run_id,
                requested_executor=None,
                selected_executor=trow["adapter_name"] or "hermes",
                task_id=task_id,
                status=status,
                progress=1.0 if _terminal else 0.0,
                exit_code=exit_code,
                error=error,
                routing={
                    "selected_executor": trow["adapter_name"] or "hermes",
                    "selection_source": "lifecycle_sync",
                },
                current_step=_step,
                phase=_phase,
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "manager._sync_executor_runs_status: sync failed "
                "task_id=%s status=%s err=%s",
                task_id, status, exc,
            )

    def fail(self, task_id: str, error_message: str) -> Task:
        ts = ids.now_iso()
        conn = get_conn()
        row = conn.execute(
            "SELECT status, started_at FROM tasks WHERE task_id = ?", (task_id,),
        ).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        if not is_legal_transition(row["status"], "failed"):
            raise IllegalTransition(f"{task_id}: {row['status']} -> failed not allowed")
        duration = _compute_duration(row["started_at"], ts)
        with transaction() as conn2:
            conn2.execute(
                "UPDATE tasks SET status='failed', finished_at=?, duration_sec=?, error_message=? WHERE task_id = ?",
                (ts, duration, error_message, task_id),
            )
        # AEE-7.2 observability: terminal-status structured log on
        # the failed branch. The error message is bounded to 500
        # chars at the event-emit layer; we do not echo the full
        # raw stdout / token / env into the module logger.
        log.warning(
            "manager.fail: task_id=%s status=failed duration_sec=%.2f "
            "error_class=%s",
            task_id,
            duration,
            _safe_error_class(error_message),
        )
        _append_log(task_id, "ERROR", f"failed: {error_message}")
        self._emit_event(task_id, EventKind.FAILED, {"error": error_message[:500]})
        # Task-Mapping work-order (Fix D): mirror the terminal
        # ``failed`` status into ``executor_runs`` so GET /runs
        # list/summary reflect the true lifecycle. Best-effort.
        self._sync_executor_runs_status(
            task_id, status="failed", exit_code=1, error=error_message[:500],
        )
        return self.get_or_raise(task_id)

    def timeout(self, task_id: str, reason: str) -> Task:
        """Mark an in-flight task as `timeout`. Distinct from `failed`
        so the reaper's actions are observable in the event log.

        `reason` should explain why (e.g. "no progress for 18m, reaper").
        """
        ts = ids.now_iso()
        conn = get_conn()
        row = conn.execute(
            "SELECT status, started_at FROM tasks WHERE task_id = ?", (task_id,),
        ).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        if not is_legal_transition(row["status"], "timeout"):
            raise IllegalTransition(f"{task_id}: {row['status']} -> timeout not allowed")
        duration = _compute_duration(row["started_at"], ts)
        with transaction() as conn2:
            conn2.execute(
                "UPDATE tasks SET status='timeout', finished_at=?, duration_sec=?, error_message=? WHERE task_id = ?",
                (ts, duration, reason, task_id),
            )
        _append_log(task_id, "WARN", f"timeout: {reason}")
        self._emit_event(task_id, EventKind.TIMEOUT, {"reason": reason[:500]})
        # Task-Mapping work-order (Fix D): mirror terminal timeout into
        # executor_runs so GET /runs reflects the true lifecycle.
        self._sync_executor_runs_status(
            task_id, status="timeout", error=reason[:500],
        )
        return self.get_or_raise(task_id)

    def cancel(self, task_id: str) -> Task:
        ts = ids.now_iso()
        conn = get_conn()
        row = conn.execute(
            "SELECT status, started_at FROM tasks WHERE task_id = ?", (task_id,),
        ).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        if not is_legal_transition(row["status"], "cancelled"):
            raise IllegalTransition(f"{task_id}: {row['status']} -> cancelled not allowed")
        duration = _compute_duration(row["started_at"], ts)
        with transaction() as conn2:
            conn2.execute(
                "UPDATE tasks SET status='cancelled', finished_at=?, duration_sec=? WHERE task_id = ?",
                (ts, duration, task_id),
            )
        _append_log(task_id, "INFO", f"cancelled duration={duration:.2f}s")
        self._emit_event(task_id, EventKind.CANCELLED, {"duration_sec": duration})
        # Task-Mapping work-order (Fix D): mirror terminal cancelled
        # into executor_runs so GET /runs reflects the true lifecycle.
        self._sync_executor_runs_status(task_id, status="cancelled")
        return self.get_or_raise(task_id)

    def retry(self, task_id: str) -> Task:
        """Create a new task cloned from `task_id`, then mark the old one as failed->retried."""
        old = self.get_or_raise(task_id)
        if old.status not in {"failed", "cancelled"}:
            raise IllegalTransition(
                f"{task_id}: cannot retry from status {old.status} (only failed/cancelled)"
            )
        new_task = self.create(
            title=f"[retry] {old.title}",
            type=old.type,
            input_text=old.input_text or "",
            session_id=old.session_id,
            mode=old.mode,
            priority=old.priority,
            owner=old.owner,
            openai_run_id=old.openai_run_id,
            prompt_version=old.prompt_version,
            model_name=old.model_name,
        )
        # Bump retry_count on the NEW task (audit trail lives in event log of old).
        conn = get_conn()
        conn.execute(
            "UPDATE tasks SET retry_count = retry_count + 1 WHERE task_id = ?",
            (new_task.task_id,),
        )
        self._emit_event(new_task.task_id, EventKind.RETRY_OF, {"original_task_id": task_id})
        return self.get_or_raise(new_task.task_id)

    def attach_openai_run_id(self, task_id: str, openai_run_id: str) -> None:
        conn = get_conn()
        conn.execute(
            "UPDATE tasks SET openai_run_id = ? WHERE task_id = ?",
            (openai_run_id, task_id),
        )
        self._emit_event(task_id, EventKind.OPENAI_RUN_ATTACHED, {"openai_run_id": openai_run_id})

    # ---- output fetch ----------------------------------------------------

    def get_output(self, task_id: str) -> Optional[Dict[str, Any]]:
        conn = get_conn()
        row = conn.execute(
            "SELECT output_text, usage_json, raw_json, delivery_json, notification_json FROM task_outputs WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            return None
        usage = None
        if row["usage_json"]:
            try:
                usage = json.loads(row["usage_json"])
            except json.JSONDecodeError:
                pass
        raw = None
        if row["raw_json"]:
            try:
                raw = json.loads(row["raw_json"])
            except json.JSONDecodeError:
                raw = {"_raw": row["raw_json"]}
        return {
            "task_id": task_id,
            "output_text": row["output_text"],
            "usage": usage,
            "raw": raw,
            "delivery_json": row["delivery_json"],
            # AEE v3 Telegram Completion Enforcement Gate — the
            # gate's result blob (sent / method / recipient /
            # message_id / ts_utc / ts_taipei / attempts /
            # last_error). NULL until complete() has fired the
            # gate (or if the gate ran before this column was
            # added — legacy rows keep NULL).
            "notification_json": row["notification_json"],
        }

    def completion_state(self, task_id: str) -> str:
        """AEE v3 Telegram Completion Enforcement Gate — read API.

        Fetch the task row + its ``task_outputs`` row, merge them
        into a single dict, and return
        ``compute_completion_state(merged_row)`` — one of the 4
        v3 completion stage strings (``execution_completed`` /
        ``evidence_completed`` / ``notification_completed`` /
        ``final_completed``).

        This is a pure read: it does NOT change ``complete()``
        behaviour, does NOT mutate any row, and does NOT emit any
        event. The orchestrator uses it to ask "how far did this
        task actually get?" without having to know the
        ``notification_json`` blob schema.

        Returns ``"execution_completed"`` (the safest non-terminal
        stage) when the task row is missing or when the
        ``task_outputs`` row has no ``notification_json`` yet —
        ``compute_completion_state`` is defensive against missing
        keys / malformed JSON.
        """
        conn = get_conn()
        task_row = conn.execute(
            "SELECT status, finished_at FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if task_row is None:
            # Defensive: a missing task collapses to the earliest
            # stage so the orchestrator's read path never breaks.
            return "execution_completed"
        out_row = conn.execute(
            "SELECT delivery_json, notification_json FROM task_outputs WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        merged: Dict[str, Any] = {
            "status": task_row["status"],
            "finished_at": task_row["finished_at"],
            "delivery_json": out_row["delivery_json"] if out_row else None,
            "notification_json": out_row["notification_json"] if out_row else None,
        }
        return compute_completion_state(merged)

    # ---- internal --------------------------------------------------------

    def _emit_event(self, task_id: str, kind: str, payload: Optional[Dict[str, Any]]) -> None:
        conn = get_conn()
        conn.execute(
            "INSERT INTO task_events (task_id, ts, kind, payload_json) VALUES (?, ?, ?, ?)",
            (task_id, ids.now_iso(), kind, json.dumps(payload, default=str) if payload else None),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_duration(started_at: Optional[str], finished_at: str) -> float:
    if not started_at:
        return 0.0
    try:
        s = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        f = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
        return max(0.0, (f - s).total_seconds())
    except ValueError:
        return 0.0


def _write_result(
    task_id: str,
    output_text: Optional[str],
    usage: Optional[Dict[str, Any]],
    raw: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Write reports/TASK-XXX/task.json and (if applicable) report.md.
    Returns the relative path written, or None.
    """
    if not output_text and not raw:
        return None
    out_dir = REPORTS_DIR / task_id
    out_dir.mkdir(parents=True, exist_ok=True)
    # Always write task.json (machine-readable metadata)
    conn = get_conn()
    row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    task_dict = _row_to_task(row).to_dict()
    task_dict["output_excerpt"] = (output_text or "")[:2000]
    task_dict["usage"] = usage
    with open(out_dir / "task.json", "w", encoding="utf-8") as f:
        json.dump(task_dict, f, indent=2, ensure_ascii=False, default=str)
    return f"reports/{task_id}/task.json"
