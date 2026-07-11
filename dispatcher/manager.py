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
)


def _row_to_task(row) -> Task:
    """Build a `Task` from a SQLite row.

    AEE-3: the `required_capabilities_json` storage column is
    decoded here into the `required_capabilities: list[str]`
    domain field. Callers that pass through the dataclass
    never see the JSON suffix. The raw `*_json` column is
    NOT in `_COLUMNS` — it's a storage-only detail.
    """
    raw = {c: row[c] for c in _COLUMNS if c in row.keys()}
    raw["required_capabilities"] = db.decode_capabilities(
        row["required_capabilities_json"]
    )
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
        """
        if initial_status not in {"pending", "queued"}:
            raise ValueError(f"initial_status must be pending or queued, got {initial_status}")
        # AEE-7.2: validate repo_root at the wire boundary, not deep
        # in the policy factory. Empty string and whitespace are
        # normalised to None so the DB never sees "" as a repo_root.
        if repo_root is not None:
            repo_root = repo_root.strip() or None
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
                  repo_root
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id, title, type, priority, owner, initial_status,
                    5 if initial_status == "queued" else 0, created_at,
                    input_text, openai_run_id, session_id, mode,
                    prompt_version, model_name, commit, branch,
                    caps_blob,
                    repo_root,
                ),
            )
        _append_log(task_id, "INFO", f"created title={title!r} type={type} priority={priority}")
        # AEE-7.2 observability: one INFO line at task creation
        # surfaces the per-job repo_root constraint, mode, and
        # type so the dispatcher log is queryable end-to-end
        # without joining against the events table. The path
        # is logged once (per_job_root) — ``input_text`` is
        # intentionally NOT included.
        log.info(
            "manager.create: task_id=%s type=%s mode=%s "
            "per_job_root=%s",
            task_id,
            type,
            mode,
            repo_root or "",
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
        with transaction() as conn2:
            started_at = row["started_at"] or ts
            conn2.execute(
                "UPDATE tasks SET status = 'running', hermes_run_id = ?, started_at = ? WHERE task_id = ?",
                (hermes_run_id, started_at, task_id),
            )
        _append_log(task_id, "INFO", f"started hermes_run_id={hermes_run_id}")
        self._emit_event(task_id, EventKind.STARTED, {"hermes_run_id": hermes_run_id})
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
            "SELECT status, started_at, model_name, input_text FROM tasks WHERE task_id = ?", (task_id,),
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
            "SELECT output_text, usage_json, raw_json, delivery_json FROM task_outputs WHERE task_id = ?",
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
        }

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
