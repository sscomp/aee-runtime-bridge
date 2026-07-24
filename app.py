"""
AEE Runtime Bridge — Phase 1 (Dispatcher-enabled)
======================================================
A thin, safe OpenAI-Custom-GPT-Action / MCP-friendly facade in front of the
Hermes M2 (Abacus.ai) `/v1/runs` API, with a built-in Task Dispatcher so
the orchestrator (ChatGPT) can track tasks across sessions.

Why a bridge?
- Hermes 8642 is the upstream "agent runtime" (sessions, SSE, tools, etc).
- A custom GPT Action needs an OpenAPI schema and a stable, auditable surface.
- Exposing 8642 directly would leak internal endpoints, keys, and the
  dashboard cookie surface. The bridge enforces:
    * its own bearer key (independent of HERMES_API_KEY)
    * a hard allowlist of endpoints
    * a simple prompt-injection blocklist on `input`
    * per-run timeout and request body shape validation

Why a Dispatcher?
- ChatGPT's run_id is session-scoped; it does not survive a GPT refresh.
- This bridge owns a separate TASK-YYYYMMDD-NNNN id for every task and
  persists it (status, progress, output, log) in SQLite, so tasks are
  queryable from any chat, CLI, or the bridge itself.

Endpoints (Phase 1):
  * /health                 (public)
  * /runs                   (existing, dispatcher-backed)
  * /runs/{run_id}          (existing, dispatcher-backed)
  * /runs/{run_id}/summary  (existing)
  * /runs/{run_id}/stop     (existing)
  * /tasks                  (new — list)
  * /tasks/{task_id}        (new — get one)
  * /tasks/{task_id}/progress (new)
  * /tasks/{task_id}/logs   (new)
  * /tasks/{task_id}/result (new)
  * /tasks/{task_id}/cancel (new)
  * /tasks/{task_id}/rerun  (new)

Run:
    source .venv/bin/activate
    uvicorn app:app --host 127.0.0.1 --port 8787
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

load_dotenv()

# ---------------------------------------------------------------------------
# Task Dispatcher (this package)
# ---------------------------------------------------------------------------
from dispatcher.manager import (
    TaskManager,
    IllegalTransition,
    TaskNotFound,
)
from dispatcher.watcher import Watcher
from dispatcher.safety import evaluate as safety_evaluate, SafetyDecision
from dispatcher.reaper import ReaperConfig, stale_count as reaper_stale_count
from dispatcher.routing import (
    RoutingPolicy,
    build_source_map,
    identify_source,
    resolve_model_for_source,
)
from config import load as config_load

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Multi-key bridge auth (added 2026-07-07)
# -----------------------------------------------------------------------------
# The bridge accepts any of the following client keys, each with its own
# blast radius:
#   * BRIDGE_API_KEY        — CLI / CI / Integration Test
#   * GPT_BRIDGE_API_KEY    — ChatGPT Custom GPT Action
#   * CLAUDE_BRIDGE_API_KEY — future Claude integration
#   * CURSOR_BRIDGE_API_KEY — future Cursor integration
#   * MCP_BRIDGE_API_KEY    — future MCP client
#
# All five are read into a single frozenset for O(1) lookup. A missing key
# disables that channel silently (you only see it via 401). To rotate a key,
# overwrite the env var and `supervisorctl restart hermes-runtime-bridge`.
CLIENT_BRIDGE_KEYS: frozenset[str] = frozenset(
    k for k in (
        os.getenv("BRIDGE_API_KEY", "").strip(),
        os.getenv("GPT_BRIDGE_API_KEY", "").strip(),
        os.getenv("CLAUDE_BRIDGE_API_KEY", "").strip(),
        os.getenv("CURSOR_BRIDGE_API_KEY", "").strip(),
        os.getenv("MCP_BRIDGE_API_KEY", "").strip(),
    ) if k
)
# Per-key source label (used by the GPT -> MiniMax-M3 routing layer).
# See `dispatcher/routing.py` for the policy.
CLIENT_KEY_SOURCES: dict[str, str] = build_source_map(dict(os.environ))

# -----------------------------------------------------------------------------
# MiniMax-M3 routing (configured 2026-07-09; per /home/ubuntu/Abacus/MiniMax-M3-routellm.md)
# -----------------------------------------------------------------------------
# This is the SINGLE place the bridge decides which model name is attached
# to an upstream call. The policy is built once at import time and re-read
# on every /runs request. To change the GPT-routed model, edit
# MINIMAX_MODEL or `default_model` below (or the corresponding env vars).
_MINIMAX_KEY_ENV = "MINIMAX" + "_" + "API_KEY"
MINIMAX_API_KEY = os.getenv(_MINIMAX_KEY_ENV, "").strip()
MINIMAX_BASE_URL = os.getenv("MINIMAX_BASE_URL", "https://routellm.abacus.ai/v1").rstrip("/")
MINIMAX_MODEL = os.getenv("MINIMAX_MODEL", "MiniMaxAI/MiniMax-M3").strip()
# When True, a GPT caller's explicit `model_name` field wins over the
# forced MiniMax-M3 routing. Default False — the whole point of this
# routing rule is that GPT ALWAYS uses MiniMax-M3.
MINIMAX_ALLOW_CALLER_OVERRIDE = (
    os.getenv("MINIMAX_ALLOW_CALLER_OVERRIDE", "false").strip().lower()
    in {"1", "true", "yes", "on"}
)
# Default model for non-GPT sources when the caller did not pass one.
# We pin this to the upstream Hermes default rather than guessing.
DEFAULT_MODEL = os.getenv(
    "BRIDGE_DEFAULT_MODEL",
    "minimax-m3",  # matches ~/.hermes/config.yaml model.default
).strip()
ROUTING_POLICY = RoutingPolicy(
    default_model=DEFAULT_MODEL,
    gpt_model=MINIMAX_MODEL,
    allow_caller_override_for_gpt=MINIMAX_ALLOW_CALLER_OVERRIDE,
    minimax_key=MINIMAX_API_KEY,
)

HERMES_BASE_URL = os.getenv("HERMES_BASE_URL", "http://127.0.0.1:8642").rstrip("/")
# HERMES_API_KEY is the upstream Hermes M2 key (machine-internal). Distinct
# from any client key above — the bridge presents it to /v1/runs on the
# caller's behalf; clients never see it.
HERMES_API_KEY = os.getenv("HERMES_API_KEY", "").strip()
DEFAULT_SESSION_ID = os.getenv("DEFAULT_SESSION_ID", "dingde-orchestrator").strip()
ALLOWED_SESSION_PREFIXES = [
    p.strip() for p in os.getenv("ALLOWED_SESSION_PREFIXES", "").split(",") if p.strip()
]

# Make sure config defaults are on disk.
from config import ensure_defaults
ensure_defaults()

# Default upstream run timeout — 15 min. Hermes itself has no global timeout
# here, so we cap the bridge-side HTTP call. Long jobs can be polled.
DEFAULT_TIMEOUT = int(os.getenv("BRIDGE_DEFAULT_TIMEOUT", "900"))
MAX_TIMEOUT = int(os.getenv("BRIDGE_MAX_TIMEOUT", "7200"))
WATCHER_TICK_SEC = float(os.getenv("DISPATCHER_WATCHER_TICK", "2.0"))


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Start/stop the background watcher that polls Hermes 8642."""
    # AEE-5: bootstrap the built-in `aee-lightweight-local`
    # Runtime descriptor (idempotent) so a fresh bridge
    # has a working default Runtime at first request.
    # Also load a `runtimes:` YAML config if `AEE_RUNTIME_CONFIG`
    # points at one. The config loader is fail-fast; a
    # malformed file aborts startup with a clear error.
    from aee.runtimes.registry import bootstrap_default_runtimes
    bootstrap_default_runtimes(force=False)
    cfg_path = os.getenv("AEE_RUNTIME_CONFIG")
    if cfg_path:
        from aee.config import load_runtime_config, apply_runtime_config, RuntimeConfigError
        from aee.runtimes.registry import runtime_registry
        try:
            parsed = load_runtime_config(cfg_path)
            apply_runtime_config(parsed, runtime_registry)
        except RuntimeConfigError as exc:
            # Fail fast — let the operator see the misconfig.
            raise
    watcher = Watcher(tick_sec=WATCHER_TICK_SEC)
    await watcher.start()
    app.state.watcher = watcher
    # P2.1 (TASK-AEE-P2-BRIDGE-HERMES-COMPLETION-SYNC): start the
    # background executor-run watcher that polls the upstream
    # adapter for non-terminal Hermes-dispatched runs and
    # reconciles the durable executor_runs row when Hermes reports
    # a terminal state. The existing dispatcher.Watcher only polls
    # the ``tasks`` table; this watcher owns the ``executor_runs``
    # namespace.
    from dispatcher.executor_watcher import ExecutorRunWatcher
    exec_watcher = ExecutorRunWatcher()
    await exec_watcher.start()
    app.state.executor_watcher = exec_watcher
    try:
        yield
    finally:
        await exec_watcher.stop()
        await watcher.stop()


app = FastAPI(
    title="AEE Runtime Bridge",
    version="1.3.0-aee2",
    description=(
        "A minimal bridge that exposes Hermes M2 (Abacus.ai) as a safe OpenAI "
        "Custom GPT Action / MCP tool surface, with a built-in Task Dispatcher "
        "that gives every task a persistent TASK-YYYYMMDD-NNNN id, progress, "
        "log, and result — queryable across chat sessions and from the CLI. "
        "AEE-2 mounts `/jobs` and `/workers` for runtime-neutral worker claim."
    ),
    lifespan=_lifespan,
)

# ---------------------------------------------------------------------------
# Safety: prompt-injection / dangerous-command blocklist
# ---------------------------------------------------------------------------
# This is a *first line of defence*, not a complete security model. It catches
# obvious cases where a third-party tool call asks Hermes to leak secrets,
# destroy data, or exfiltrate keys. More sophisticated attacks require runtime
# approval gates inside Hermes itself (out of scope for the bridge).

import re

DANGEROUS_PATTERNS: List[str] = [
    r"rm\s+-rf\s+/(?:\s|$)",   # rm -rf /   end of string or whitespace after
    r"rm\s+-rf\s+/\w",          # rm -rf /etc, /home, /var
    r"rm\s+-rf\s+~",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bpasswd\b",
    r"curl\s+[^\n]*\|\s*sh\b",
    r"wget\s+[^\n]*\|\s*sh\b",
    r"\bexport\s+API_SERVER_KEY\b",
    r"\bAPI_SERVER_KEY\s*=",    # any direct assignment
    r"cat\s+~?/?\.hermes/\.env",  # ~/.hermes/.env or .hermes/.env
    r"cat\s+/(?:home/[^/\s]+/)?\.hermes/\.env",  # /home/ubuntu/.hermes/.env
    r"cat\s+~/\.ssh",
    r"cat\s+/etc/shadow",
    r"\bprintenv\b.*HERMES",
    r":\(\)\s*\{.*:\|:&\s*\};:",   # fork bomb
]

DANGEROUS_RE = re.compile("|".join(DANGEROUS_PATTERNS), re.IGNORECASE | re.MULTILINE)

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CreateRunRequest(BaseModel):
    input: str = Field(
        ...,
        min_length=1,
        max_length=64_000,
        description="Instruction to execute on Hermes M2.",
    )
    expected_artifacts: Optional[List[str]] = Field(
        None,
        description=(
            "Phase 4: absolute file paths the task is expected to produce "
            "or verify. The dispatcher will stat() each path at task "
            "completion; missing entries bump the task's `warning_count` "
            "and are recorded in `task_outputs.delivery_json`. This is "
            "in addition to the automatic scan of `input` for absolute paths."
        ),
    )
    # WO-INCOMPLETE-DELIVERY-AUTORESCUE: rescue-loop cap. When the
    # completion gate fires (any declared artifact missing) AND
    # ``rescue_count < max_rescues``, the task transitions to the
    # non-terminal ``incomplete_delivery`` state and the dispatcher
    # auto-queues one ``_rescue()`` re-validation. The rescue
    # re-stats the declared artifacts; on success the task reaches
    # ``completed``, on failure it reaches ``failed``. Default 1
    # (one auto-rescue attempt). 0 disables rescue entirely (the
    # gate falls through to ``failed`` on the first miss — the
    # WO-COMPLETION-GATE-MVP behavior).
    max_rescues: Optional[int] = Field(
        None, ge=0, le=5,
        description=(
            "WO-INCOMPLETE-DELIVERY-AUTORESCUE: max auto-rescue attempts "
            "when the completion gate fires with missing declared "
            "artifacts. 0 = disabled (fail on first miss). Default 1. "
            "Capped at 5 to prevent runaway loops."
        ),
    )
    session_id: Optional[str] = Field(
        None,
        max_length=200,
        description="Shared Hermes session/task id. Defaults to DEFAULT_SESSION_ID.",
    )
    mode: str = Field(
        "normal",
        description="One of: normal, research, coding, ops. Forwarded as a hint.",
    )
    timeout_seconds: int = Field(
        DEFAULT_TIMEOUT,
        ge=30,
        le=MAX_TIMEOUT,
        description=f"Upstream call timeout in seconds ({30}..{MAX_TIMEOUT}).",
    )
    title: Optional[str] = Field(
        None,
        max_length=200,
        description="Human-friendly task title. Auto-generated if omitted.",
    )
    type: Optional[str] = Field(
        None,
        description="Task type: research / coding / ops / review / normal. Defaults to 'mode' or 'normal'.",
    )
    priority: int = Field(
        50, ge=0, le=100,
        description="0..100; lower is more urgent. Default 50.",
    )
    openai_run_id: Optional[str] = Field(
        None, max_length=200,
        description="ChatGPT-side run_id (for cross-reference; optional).",
    )
    prompt_version: Optional[str] = Field(
        None, max_length=64,
        description="Prompt template version, e.g. 'macro_v1'.",
    )
    model_name: Optional[str] = Field(
        None, max_length=64,
        description="Model to use, e.g. 'claude-sonnet-4-6'.",
    )
    executor_session_id: Optional[str] = Field(
        None, max_length=200,
        description=(
            "AEE write-side metadata: the caller's session id "
            "(e.g. the orchestrator or ChatGPT session that asked "
            "for this dispatch). Persisted on the `tasks` row at "
            "create time so the read-side identity validator can "
            "cite an authoritative value instead of guessing "
            "from heuristics. Optional — legacy callers that "
            "don't pass it keep the column NULL."
        ),
    )
    # AEE-8.1: optional ``profile`` field. ``None`` (the default)
    # keeps the existing behavior completely unchanged — the
    # dispatcher sees ``full``. When set to ``mini``, ``edge``, or
    # ``developer``, the profile descriptor is resolved at dispatch
    # time (read-only plumbing; no source branching, no runtime
    # mutation, no toolset enforcement yet — those are later
    # phases). See ``aee.profiles.descriptor`` for the full
    # contract and ``AEE_PROFILE_UNIFICATION_DECISION_MINI.md`` §5
    # for the phase scope.
    profile: Optional[str] = Field(
        None,
        description=(
            "AEE-8.1 profile selector. Optional. One of: "
            "full, mini, edge, developer. Defaults to 'full' "
            "when omitted or empty. Unknown values are rejected "
            "at schema validation time. This field is read-only "
            "plumbing — it does not change any call site behavior "
            "until a later phase wires it into the dispatcher."
        ),
    )
    # TASK-M2 (TASK-M2 — Executor Router + Claude Adapter + Verified
    # Manifest Gate MVP): optional ``metadata`` field carries
    # executor-routing hints. ``None`` (the default) keeps the
    # existing Hermes path completely unchanged. Recognized keys:
    #   - executor:          "hermes" | "claude_code"
    #   - repo_path:         absolute, allow-listed git repo path
    #   - working_mode:      e.g. "isolated_directory"
    #   - expected_branch:   runner pin
    #   - expected_head:     runner pin
    #   - allow_commit:      bool, requires human_approved
    #   - human_approved:    bool
    #   - required_artifacts: list of relative paths
    #   - test_command:      single string (no shell composition)
    #   - model / fallback_model
    metadata: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "TASK-M2 executor router metadata. Optional. When "
            "absent, the request is dispatched via the existing "
            "Hermes path. When present, ``executor`` selects the "
            "target backend (currently 'claude_code' or 'hermes'); "
            "the other keys are forwarded to the executor. See "
            "``aee.runtimes.executor_router.validate_metadata`` "
            "for the full validation contract."
        ),
    )

    @field_validator("mode")
    @classmethod
    def _mode_allowed(cls, v: str) -> str:
        if v not in {"normal", "research", "coding", "ops"}:
            raise ValueError("mode must be one of: normal, research, coding, ops")
        return v

    @field_validator("type")
    @classmethod
    def _type_allowed(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in {"research", "coding", "ops", "review", "normal"}:
            raise ValueError("type must be one of: research, coding, ops, review, normal")
        return v

    @field_validator("profile")
    @classmethod
    def _profile_allowed(cls, v: Optional[str]) -> Optional[str]:
        # AEE-8.1: validate the profile field at schema boundary.
        # None / empty / whitespace → None (caller did not opt in;
        # the dispatcher will default to "full" downstream). Known
        # profile names pass through. Unknown values raise
        # ValueError so Pydantic surfaces a 422 to the caller.
        # We do NOT import aee.profiles here to keep the schema
        # module isolation-safe; the validator is a pure string
        # check against the canonical set.
        if v is None:
            return v
        if not isinstance(v, str):
            raise ValueError(
                f"profile must be a string, got {type(v).__name__}"
            )
        cleaned = v.strip()
        if not cleaned:
            return None
        known = {"full", "mini", "edge", "developer"}
        if cleaned not in known:
            raise ValueError(
                f"profile must be one of: full, mini, edge, developer; "
                f"got {cleaned!r}"
            )
        return cleaned


class CreateRunResponse(BaseModel):
    run_id: str
    status: str
    session_id: str
    poll_url: str
    requires_review: bool = False
    # Phase 1 additions
    task_id: Optional[str] = None
    task_poll_url: Optional[str] = None
    progress_pct: int = 0
    # Phase 2 P3: safety decision surfaced to the caller. When
    # `needs_human` is true, the orchestrator should prompt the user
    # before proceeding to downstream actions.
    safety: Optional[Dict[str, Any]] = None
    # GPT -> MiniMax-M3 routing (2026-07-09): which source label the
    # bridge identified, which model was attached to the upstream call,
    # and whether the policy overrode the caller's choice. The orchestrator
    # (ChatGPT) can read this to confirm the request hit the intended
    # model. None values are returned when no routing decision was made.
    routing: Optional[Dict[str, Any]] = None


class ErrorBody(BaseModel):
    detail: str
    code: Optional[str] = None
    matched_pattern: Optional[str] = None


# ---------------------------------------------------------------------------
# Auth + upstream helpers
# ---------------------------------------------------------------------------


def require_auth(authorization: Optional[str]) -> str:
    """Authenticate the caller and return the source label.

    Backward-compatible with the previous `-> None` signature for any
    caller that ignores the return value (there are no such callers
    inside the bridge today, but external test scripts may exist).

    Returns one of: "cli", "gpt", "claude", "cursor", "mcp", or
    "unknown". "unknown" is returned when the presented key is not
    in any known map — but that case is rejected above with 401, so
    in practice the function never returns "unknown" on the success
    path. Tests cover the success path only.

    Raises HTTPException(401) for missing / malformed / unknown keys.
    """
    if not CLIENT_BRIDGE_KEYS:
        raise HTTPException(
            status_code=500,
            detail="Bridge client keys are not configured (set BRIDGE_API_KEY / GPT_BRIDGE_API_KEY / CLAUDE_BRIDGE_API_KEY / CURSOR_BRIDGE_API_KEY / MCP_BRIDGE_API_KEY in .env)",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    presented = authorization[len("Bearer "):].strip()
    if presented not in CLIENT_BRIDGE_KEYS:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return identify_source(presented, CLIENT_KEY_SOURCES)


def hermes_headers() -> Dict[str, str]:
    if not HERMES_API_KEY:
        raise HTTPException(status_code=500, detail="Hermes API key is not configured")
    return {
        "Authorization": f"Bearer {HERMES_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def check_session_allowed(session_id: str) -> None:
    if not ALLOWED_SESSION_PREFIXES:
        return
    if not any(session_id.startswith(p) for p in ALLOWED_SESSION_PREFIXES):
        raise HTTPException(
            status_code=403,
            detail=f"session_id must start with one of: {ALLOWED_SESSION_PREFIXES}",
        )


def danger_check(
    text: str,
    mode: str = "normal",
    profile: Optional[str] = None,
) -> SafetyDecision:
    """Phase 2 P3: replaced by safety.evaluate() (allowlist + blocklist
    + approval gate). Kept as a thin wrapper for backward compat.

    Epic 9.4 §21.4: ``profile`` is forwarded to
    :func:`dispatcher.safety.evaluate` so the AEE-8.3 profile-aware
    enforcement (is_read_only / can_create_cron / can_delegate_subagents)
    is activated at the dispatch-time safety gate. ``None`` preserves
    pre-Epic-9.4 behaviour (no profile enforcement).
    """
    return safety_evaluate(text, mode=mode, profile=profile)


# ---------------------------------------------------------------------------
# Routes — health
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> Dict[str, Any]:
    # AEE-2: Hermes reachability is reported via HermesAdapter.health()
    # instead of an inline httpx call. Behaviour-equivalent: same
    # {ok, status_code} dict, surfaced as "reachable" / "http_XXX" /
    # "error: ..." in `hermes_status`.
    hermes_status = "unknown"
    try:
        from aee.core.registry import adapter_registry
        adapter = adapter_registry.get("hermes")
        probe = await adapter.health()
        if probe.get("ok"):
            hermes_status = "reachable"
        elif "status_code" in probe:
            hermes_status = f"http_{probe['status_code']}"
        else:
            hermes_status = f"error: {probe.get('error', 'unknown')}"
    except Exception as exc:  # noqa: BLE001
        hermes_status = f"error: {type(exc).__name__}"

    # Dispatcher health: count of tasks by status.
    try:
        m = TaskManager()
        all_tasks = m.list(limit=1000)
        by_status: Dict[str, int] = {}
        for t in all_tasks:
            by_status[t.status] = by_status.get(t.status, 0) + 1
    except Exception as exc:  # noqa: BLE001
        by_status = {"_error": 0}  # type: ignore[assignment]
        _ = str(exc)[:80]

    # Reaper summary (Phase 2 P1).
    try:
        reaper_cfg = ReaperConfig.from_dict(config_load("reaper"))
        reaper_summary = reaper_stale_count(TaskManager(), reaper_cfg)
    except Exception as exc:  # noqa: BLE001
        reaper_summary = {"_error": str(exc)[:80]}

    # Safety policy summary (Phase 2 P3).
    try:
        safety_cfg = config_load("safety")
        safety_summary = {
            "mode": safety_cfg.get("mode", "blocklist_plus_allowlist"),
            "allowlist_size": len(safety_cfg.get("allowlist_commands", [])),
            "blocklist_size": len(safety_cfg.get("blocklist_substrings", [])),
            "approval_size": len(safety_cfg.get("require_approval_substrings", [])),
            "log_rejected": bool(safety_cfg.get("log_rejected", True)),
        }
    except Exception as exc:  # noqa: BLE001
        safety_summary = {"_error": str(exc)[:80]}

    # Notifier summary (Phase 2 P2).
    try:
        notify_cfg = config_load("notify").get("telegram", {})
        bot_token = os.getenv(notify_cfg.get("bot_token_env", ""), "").strip() if notify_cfg.get("bot_token_env") else ""
        chat_id = os.getenv(notify_cfg.get("chat_id_env", ""), "").strip() if notify_cfg.get("chat_id_env") else ""
        notifier_summary = {
            "enabled": bool(notify_cfg.get("enabled", False)) and bool(bot_token) and bool(chat_id),
            "notify_on": notify_cfg.get("notify_on", []),
            "rate_limit_per_hour": int(notify_cfg.get("rate_limit_per_hour", 20)),
            "bot_token_present": bool(bot_token),
            "chat_id_present": bool(chat_id),
        }
    except Exception as exc:  # noqa: BLE001
        notifier_summary = {"_error": str(exc)[:80]}

    return {
        "status": "ok",
        "service": "hermes-runtime-bridge",
        "version": "1.2.0",
        "phase": "2 — Reaper + Notifier + Safety upgrade",
        "hermes": hermes_status,
        "hermes_base_url": HERMES_BASE_URL,
        "dispatcher": {
            "tasks_total": sum(by_status.values()),
            "by_status": by_status,
        },
        "reaper": reaper_summary,
        "safety": safety_summary,
        "notifier": notifier_summary,
    }


# ---------------------------------------------------------------------------
# Routes — /runs (existing, dispatcher-backed)
# ---------------------------------------------------------------------------


RUN_LIST_LIMIT_MIN = 1
RUN_LIST_LIMIT_MAX = 100
RUN_LIST_LIMIT_DEFAULT = 20


@app.get("/runs")
async def list_runs_endpoint(
    authorization: Optional[str] = Header(None),
    limit: int = Query(
        RUN_LIST_LIMIT_DEFAULT,
        description=(
            "Maximum number of runs to return. Must be an integer in "
            f"[{RUN_LIST_LIMIT_MIN}..{RUN_LIST_LIMIT_MAX}]. Out-of-range "
            "values return a structured HTTP 400 with code "
            "'invalid_limit'. Non-integer values are rejected by the "
            "framework's JSON parser as a 422 (a separate case, see docs)."
        ),
    ),
    status: Optional[str] = Query(
        None,
        description="Filter by canonical run status. One of: queued, started, running, completed, failed, timeout, cancelled.",
    ),
    executor: Optional[str] = Query(
        None,
        description="Filter by selected_executor (e.g. 'claude-code-cli' or 'hermes').",
    ),
    since: Optional[str] = Query(
        None,
        description="ISO-8601 timestamp; only runs with created_at >= since are returned.",
    ),
) -> Dict[str, Any]:
    """List recent runs (newest first) from the durable ``executor_runs`` store.

    This endpoint is a **pure read** of persisted state. It does NOT
    perform Hermes reconciliation, launch any executor, poll
    upstream, mutate run state, or scan the repo. It reads only from
    the ``executor_runs`` SQLite table (populated best-effort by
    ``POST /runs/executor``).

    Ordering is newest-first by ``created_at`` with a deterministic
    tie-breaker on ``run_id`` (DESC) so two runs sharing a timestamp
    have a stable order across calls.

    Query parameters:
      * ``limit``    — integer, default 20, min 1, max 100. Out-of-range
        values return a structured HTTP 400 with code ``invalid_limit``.
        Non-integer values (e.g. ``?limit=abc``) are rejected by
        FastAPI's JSON parser as a 422 — a separate case documented here
        for completeness.
      * ``status``   — optional canonical status filter.
      * ``executor`` — optional selected_executor filter.
      * ``since``    — optional ISO-8601 timestamp filter on created_at.

    Response envelope:
      ``{ "items": [<canonical run summary>, ...],
         "count": <int>,
         "limit": <int>,
         "filters": { "status": ..., "executor": ..., "since": ... } }``

    Each item is the canonical envelope returned by
    ``GET /runs/{run_id}`` (without the ``source`` / ``is_terminal``
    convenience tags — those are added here for list consumers).

    Invalid ``limit`` (below 1 or above 100) returns a deterministic
    HTTP 400 with a structured ``{code, message, valid_range}`` body.
    Non-integer ``limit`` values are rejected by FastAPI's request
    parser as a 422 (a separate framework-level case, documented
    above). Invalid ``status`` or malformed ``since`` return a
    deterministic 400 with a structured ``{code, message}`` body.
    """
    require_auth(authorization)

    # Validate ``limit`` against the contract range [1..100]. The
    # ``Query`` declaration above intentionally omits ``ge``/``le`` so
    # that an out-of-range integer reaches this handler and receives
    # a deterministic 400 (with the structured envelope callers depend
    # on) rather than FastAPI's generic 422 validation response.
    if limit < RUN_LIST_LIMIT_MIN or limit > RUN_LIST_LIMIT_MAX:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_limit",
                "message": (
                    f"limit {limit} is out of range; expected an integer "
                    f"in [{RUN_LIST_LIMIT_MIN}..{RUN_LIST_LIMIT_MAX}]"
                ),
                "valid_range": {
                    "min": RUN_LIST_LIMIT_MIN,
                    "max": RUN_LIST_LIMIT_MAX,
                },
            },
        )

    # Validate ``status`` against the canonical vocabulary. An
    # unknown value is a deterministic 400 (not a silent empty list)
    # so callers can distinguish "no runs with this status" from
    # "this status string is not recognised".
    from dispatcher.executor_runs import CANONICAL_RUN_STATUSES

    if status is not None and status not in CANONICAL_RUN_STATUSES:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_status",
                "message": (
                    f"status {status!r} is not a canonical run status; "
                    f"expected one of: {sorted(CANONICAL_RUN_STATUSES)}"
                ),
                "valid_statuses": sorted(CANONICAL_RUN_STATUSES),
            },
        )

    # Validate ``since`` — accept any ISO-8601 string that
    # ``datetime.fromisoformat`` can parse (Python 3.11 handles the
    # trailing ``Z``). A malformed value is a deterministic 400.
    since_normalized: Optional[str] = None
    if since is not None:
        from datetime import datetime, timezone

        try:
            # fromisoformat in 3.11 accepts "...Z" but normalises to
            # a tz-aware datetime; we re-serialise to the bridge's
            # canonical ``%Y-%m-%dT%H:%M:%SZ`` shape so the lexical
            # comparison against stored created_at strings is sound.
            parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            since_normalized = parsed.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "invalid_since",
                    "message": (
                        f"since {since!r} is not a valid ISO-8601 timestamp; "
                        f"expected a value like '2026-07-22T00:00:00Z'"
                    ),
                },
            ) from exc

    from dispatcher.db import get_conn
    from dispatcher.executor_runs import list_runs as _list_runs

    try:
        conn = get_conn()
        rows = _list_runs(
            conn,
            limit=limit,
            status=status,
            selected_executor=executor,
            since=since_normalized,
        )
    except Exception:  # pragma: no cover - defensive
        conn = None
        rows = []

    # ------------------------------------------------------------------
    # Task-Mapping work-order: union ``tasks`` rows that have a
    # ``hermes_run_id`` (i.e. were dispatched via ``POST /runs``) into
    # the list so hermes-adapter runs are visible alongside
    # executor_runs rows. Root cause C3 of the investigation report:
    # before this fix, ``POST /runs`` only wrote ``tasks`` and
    # ``GET /runs`` only read ``executor_runs``, so hermes runs were
    # invisible to the list endpoint.
    #
    # The ``_persist_hermes_run_mapping`` helper (Fix A) now writes
    # an ``executor_runs`` row at dispatch time, so for NEW hermes
    # runs the union is a no-op (the row is already in ``rows``).
    # The union is retained for backwards compatibility with
    # pre-fix hermes runs that only have a ``tasks`` row and were
    # never mirrored into ``executor_runs`` — without the union they
    # would remain invisible forever. The union is keyed on
    # ``run_id`` so a hermes run that exists in both stores is
    # returned exactly once (the ``executor_runs`` row wins because
    # it carries the richer observability fields).
    #
    # ``tasks.status`` uses a slightly different vocabulary
    # (``pending`` / ``waiting`` are task-only); we map them onto the
    # canonical run status vocabulary: ``pending`` → ``queued``,
    # ``waiting`` → ``running``. ``started`` (executor_runs-only) is
    # not produced by the tasks path. The ``selected_executor`` for
    # unioned tasks rows is ``"hermes"`` unless the task's
    # ``adapter_name`` column says otherwise (the canonical source
    # for adapter identity is the ``tasks`` row).
    try:
        if conn is None:
            raise RuntimeError("db connection unavailable")
        seen_run_ids = {r.get("run_id") for r in rows if r.get("run_id")}
        cursor = conn.execute(
            "SELECT task_id, hermes_run_id, external_run_id, "
            "adapter_name, status, created_at, title "
            "FROM tasks WHERE hermes_run_id IS NOT NULL "
            "AND hermes_run_id != '' "
            "ORDER BY created_at DESC, hermes_run_id DESC LIMIT ?",
            (max(limit * 2, 100),),
        )
        for trow in cursor.fetchall():
            run_id = trow["hermes_run_id"] or trow["external_run_id"]
            if not run_id or run_id in seen_run_ids:
                continue
            t_status = trow["status"]
            if t_status == "pending":
                t_status = "queued"
            elif t_status == "waiting":
                t_status = "running"
            if status is not None and t_status != status:
                continue
            if executor is not None and executor != (trow["adapter_name"] or "hermes"):
                continue
            if since_normalized is not None and (trow["created_at"] or "") < since_normalized:
                continue
            rows.append({
                "run_id": run_id,
                "requested_executor": None,
                "selected_executor": trow["adapter_name"] or "hermes",
                "task_id": trow["task_id"],
                "status": t_status,
                "progress": 1.0 if t_status in {"completed", "failed", "timeout", "cancelled"} else 0.0,
                "exit_code": None,
                "timeout_state": None,
                "cancel_state": None,
                "stdout_summary": "",
                "stderr_summary": "",
                "artifact_paths": [],
                "artifact_verification": [],
                "git_evidence": None,
                "telegram_result": {},
                "runtime_identity": None,
                "routing": {
                    "selected_executor": trow["adapter_name"] or "hermes",
                    "selection_source": "tasks_union",
                },
                "error": None,
                "created_at": trow["created_at"],
                "updated_at": trow["created_at"],
                "completed_at": None,
                "last_heartbeat_at": None,
                "current_step": None,
                "phase": None,
                "title": trow["title"],
            })
            seen_run_ids.add(run_id)
    except Exception:  # pragma: no cover - defensive
        pass

    # Re-sort the unioned list newest-first by created_at with the
    # run_id DESC tie-breaker (matching ``list_runs`` ordering) and
    # re-apply the caller's limit.
    rows.sort(key=lambda r: (r.get("created_at") or "", r.get("run_id") or ""), reverse=True)
    rows = rows[:limit]

    # Augment each item with the convenience tags that
    # GET /runs/{run_id} also adds so list consumers do not have to
    # re-derive them.
    terminal_statuses = {"completed", "failed", "timeout", "cancelled"}
    # P1 observability: derive the canonical observability envelope
    # for each row from persisted evidence only. ``derive_observability``
    # is a pure function over the row dict — it does not poll
    # executors, launch work, or mutate state. GET /runs remains a
    # pure read (work-order §3).
    from dispatcher.observability import derive_observability
    items: List[Dict[str, Any]] = []
    for row in rows:
        env = dict(row)
        env.setdefault("source", "executor_runs")
        env["is_terminal"] = env.get("status") in terminal_statuses
        # Merge the observability fields into the envelope. The
        # canonical run fields (run_id, status, progress, etc.) are
        # preserved; observability fields are added alongside.
        env.update(derive_observability(env))
        items.append(env)

    return {
        "items": items,
        "count": len(items),
        "limit": limit,
        "filters": {
            "status": status,
            "executor": executor,
            "since": since_normalized if since is not None else None,
        },
    }


@app.post("/runs", response_model=CreateRunResponse)
async def create_run(
    body: CreateRunRequest,
    authorization: Optional[str] = Header(None),
) -> CreateRunResponse:
    source = require_auth(authorization)

    # -------------------------------------------------------------------------
    # Epic 9.4 §21.4 — Runtime Profile Selection (single resolution point).
    #
    # The profile is resolved **once, at dispatch time**, here in the
    # ``POST /runs`` handler. Resolution path (per Master Plan §21.4):
    #
    #   body.profile (validated against KNOWN_PROFILES by the Pydantic
    #       field_validator on CreateRunRequest) → if absent, fall back to
    #       DEFAULT_PROFILE ("full") → stored on Task.profile (AEE-8.2) →
    #       safety.py:evaluate(profile=...) enforces (AEE-8.3) → dispatcher
    #       passes to runtime adapter.
    #
    # No other code path resolves the profile. We use ``parse_profile``
    # from the descriptor module so the canonical default + validation
    # logic lives in one place (AEE-8.1 contract). ``body.profile`` has
    # already been schema-validated to be one of {full, mini, edge,
    # developer} or None; ``parse_profile`` maps None → "full".
    # -------------------------------------------------------------------------
    from aee.profiles.descriptor import parse_profile as _parse_profile
    resolved_profile = _parse_profile(body.profile)

    # Epic 9.4 §21.4 — edge profile: activate runtime-level DB
    # query_only enforcement. ``set_db_profile`` is a process-wide
    # opt-in; ``edge`` causes every subsequent ``get_conn()`` to emit
    # ``PRAGMA query_only=1``. Any other profile clears the mode so
    # the dispatcher can write. This is the §21.4 "edge special case":
    # "``profile=edge`` wraps the DB connection factory in
    # ``dispatcher/db.py`` to emit ``PRAGMA query_only=1`` on every
    # connection. Runtime-level enforcement, not just intent detection."
    from dispatcher import db as _db_module
    _db_module.set_db_profile(resolved_profile)

    # Phase 2 P3: safety policy (allowlist + blocklist + approval gate).
    # Epic 9.4 §21.4: forward the resolved profile to the safety gate so
    # AEE-8.3 profile-aware enforcement (is_read_only / can_create_cron /
    # can_delegate_subagents) is activated at dispatch time.
    decision = danger_check(
        body.input,
        mode=body.mode or "normal",
        profile=resolved_profile,
    )
    if decision.action == "block":
        manager = TaskManager()
        m = config_load("safety")
        if m.get("log_rejected", True):
            try:
                t = manager.create(
                    title=f"[REJECTED] {body.title or body.input[:60]}",
                    type="review",
                    input_text=body.input,
                    session_id=body.session_id or DEFAULT_SESSION_ID,
                    mode=body.mode,
                    # AEE-8.2: persist profile on rejected tasks too
                    # so the audit trail is consistent. The profile
                    # is stored but NOT enforced.
                    # Epic 9.4 §21.4: store the *resolved* profile
                    # (None → "full" via parse_profile) so the audit
                    # trail reflects the actual dispatch-time profile.
                    profile=resolved_profile,
                )
                manager.fail(t.task_id, f"safety reject: {decision.reason} (matched={decision.matched!r})")
            except Exception:  # noqa: BLE001
                pass
        raise HTTPException(
            status_code=400,
            detail={
                "code": "dangerous_input",
                "message": "Input rejected by bridge safety policy.",
                "decision": decision.to_dict(),
            },
        )
    if decision.action == "require_approval":
        # Don't reject — surface to caller as needs_human=True so
        # ChatGPT asks the user before proceeding.
        # We continue to create the task but mark it accordingly.
        pass

    session_id = body.session_id or DEFAULT_SESSION_ID
    check_session_allowed(session_id)

    # -------------------------------------------------------------------------
    # MiniMax-M3 routing: GPT-source requests get forced to MiniMax-M3.
    # See `dispatcher/routing.py` and the operator-facing doc at
    # `/home/ubuntu/Abacus/Hermes_GPT_MiniMax_Routing.md`.
    # -------------------------------------------------------------------------
    resolved = resolve_model_for_source(
        source=source,
        caller_model=body.model_name,
        policy=ROUTING_POLICY,
    )
    # If the policy forced GPT to MiniMax-M3 but no key is configured,
    # fail loudly with a 503 instead of silently sending a request that
    # will be rejected by the upstream with 401.
    if resolved.was_forced and not resolved.key_present:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "minimax_key_not_configured",
                "message": (
                    "GPT-source request must be routed to MiniMax-M3 but "
                    "the bridge is missing MINIMAX_API_KEY. Set it in "
                    "/home/ubuntu/hermes-runtime-bridge/.env and "
                    "supervisorctl restart hermes-runtime-bridge."
                ),
                "source": resolved.source,
                "model_wanted": resolved.model_id,
            },
        )
    # Effective model_name to attach to the dispatcher task + the upstream
    # /v1/runs payload.
    effective_model_name = resolved.model_id
    if body.model_name and body.model_name != effective_model_name:
        # Caller asked for a model but routing policy overrode it. Log
        # both sides to the dispatcher so audit trail is complete.
        override_note = (
            f"routing override: caller_model={body.model_name!r} -> "
            f"effective_model_name={effective_model_name!r} "
            f"(source={source!r}, reason={resolved.reason!r})"
        )
    else:
        override_note = (
            f"routing: effective_model_name={effective_model_name!r} "
            f"(source={source!r}, reason={resolved.reason!r})"
        )

    # ----- Dispatcher: create the task first, so we have a task_id even if
    # the upstream call fails.
    manager = TaskManager()
    task_type = body.type or body.mode or "normal"
    title = body.title or f"{task_type}: {body.input[:60]}"
    # Phase 4: if the caller passes `expected_artifacts`, append a hidden
    # hint line to input_text so the auto-scan regex picks them up. The
    # hint is invisible to the model because the upstream prompt uses
    # `body.input` directly below, but it gives the dispatcher the
    # contract to verify at complete() time.
    effective_input = body.input
    if body.expected_artifacts:
        hint = "\n\n[bridge:expected_artifacts]\n" + "\n".join(body.expected_artifacts) + "\n[/bridge]"
        effective_input = body.input + hint
    task = manager.create(
        title=title,
        type=task_type,
        input_text=effective_input,
        session_id=session_id,
        mode=body.mode,
        priority=body.priority,
        openai_run_id=body.openai_run_id,
        prompt_version=body.prompt_version,
        model_name=effective_model_name,
        # AEE write-side metadata: forward the caller's
        # session id from the wire contract to the dispatcher's
        # `manager.create(..., executor_session_id=...)` kwarg.
        # Optional — None when the caller didn't pass one.
        executor_session_id=body.executor_session_id,
        # AEE-8.2: forward the caller's profile selection from
        # the wire contract to the dispatcher's
        # `manager.create(..., profile=...)` kwarg. Stored but
        # NOT enforced — no safety-gate, no toolset restriction.
        # Optional — None when the caller didn't pass one.
        # Epic 9.4 §21.4: store the *resolved* profile (None →
        # "full" via parse_profile at the top of create_run) so
        # the Task.profile field always carries the canonical
        # profile that was active at dispatch time, never None.
        profile=resolved_profile,
        # WO-COMPLETION-GATE-MVP: forward the caller's declared
        # artifact list from the wire contract to the dispatcher's
        # `manager.create(..., expected_artifacts=...)` kwarg. The
        # dispatcher gates completion on these paths — if any are
        # missing at complete() time, the task transitions to `failed`
        # with reason `missing_expected_artifacts` instead of
        # `completed`. None / empty → no contract → existing behavior.
        expected_artifacts=body.expected_artifacts,
        # WO-INCOMPLETE-DELIVERY-AUTORESCUE: forward the caller's
        # rescue-loop cap. None → dispatcher default (1). 0 disables
        # auto-rescue entirely (the gate falls through to `failed` on
        # the first miss — the WO-COMPLETION-GATE-MVP behavior). The
        # dispatcher clamps the value to [0, 5].
        max_rescues=body.max_rescues,
    )
    task_id = task.task_id
    # Record the source + override note on the task log. This is the audit
    # trail operators will use to confirm a request came from GPT and was
    # routed to MiniMax-M3.
    manager.log(task_id, f"client_source={source!r}")
    manager.log(task_id, override_note)
    # Epic 9.4 §21.4: record the resolved profile on the task log so
    # operators can confirm which profile was active at dispatch time.
    manager.log(task_id, f"profile={resolved_profile!r}")

    # ----- Call upstream via RuntimeAdapter (AEE-2 seam).
    # AEE-2: instead of hardcoding `httpx.AsyncClient.post(
    # {HERMES_BASE_URL}/v1/runs, ...)`, the bridge now resolves the
    # adapter for this task's `runtime_type` from the registry and
    # delegates the wire call. The default adapter (`hermes`) wraps
    # the same endpoint we used to call inline; the response shape
    # returned to the caller is unchanged.
    from aee.core.job_models import Job as AEEJob
    from aee.core.registry import adapter_registry
    from aee.adapters.base import RuntimeError as AdapterRuntimeError

    job = AEEJob(
        title=task.title,
        type=task.type,
        mode=task.mode or "normal",
        priority=task.priority,
        input=body.input,           # original, not effective_input
        session_id=session_id,
        client_source=source,
        model_name=effective_model_name,
        runtime_type=task.runtime_type or "hermes",
        adapter_name=task.adapter_name or "hermes",
    )
    # ----- TASK-M2: executor router. Validate the optional
    # ``metadata`` and, when present, override the ``adapter_name``
    # / ``runtime_type`` based on the explicit opt-in. Legacy
    # callers that pass no ``metadata`` keep the existing path
    # unchanged. We deliberately do *not* silently fall back: if
    # the caller asked for ``claude_code`` and the adapter is not
    # available, we fail with a 503 (rather than downgrade to
    # Hermes).
    #
    # ``executor_decision`` is hoisted to the function scope so
    # the response's ``routing`` block can surface the actual
    # selected executor as observable evidence (TASK-20260719-0046).
    # ``None`` is the legacy/default sentinel — it means "no
    # executor metadata was supplied, default Hermes path was
    # taken". The response carries both ``requested_executor``
    # and ``selected_executor`` so the caller can verify that an
    # explicit request was honored (not silently overridden).
    executor_decision = None  # set to RoutingDecision inside the metadata branch
    if body.metadata is not None:
        from aee.runtimes.executor_router import (
            ExecutorUnavailable,
            ExecutorValidationError,
            select_executor,
            validate_metadata,
        )
        try:
            validate_metadata(body.metadata)
        except ExecutorValidationError as exc:
            manager.fail(task_id, f"executor metadata invalid: {exc.code}")
            raise HTTPException(
                status_code=400,
                detail={
                    "code": exc.code,
                    "message": exc.message,
                },
            ) from exc
        try:
            decision = select_executor(
                body.metadata,
                available_adapters=adapter_registry.names(),
            )
        except ExecutorUnavailable as exc:
            manager.fail(task_id, str(exc))
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "executor_unavailable",
                    "message": str(exc),
                },
            ) from exc
        # Apply the routing decision by overriding the Job's
        # adapter_name (the registry key). runtime_type follows.
        job.adapter_name = decision.selected_executor
        job.runtime_type = decision.selected_executor
        # For ``claude_code`` we pack the metadata into ``Job.spec``
        # so the adapter can pick it up. We deliberately do *not*
        # merge into ``job.input`` — the brief is forwarded
        # verbatim via ``metadata.brief`` (or falls back to input).
        if decision.selected_executor == "claude_code":
            spec_dict = dict(body.metadata or {})
            # Promote the original input to ``spec.brief`` so the
            # adapter can forward it to the Runner.
            spec_dict.setdefault("brief", body.input)
            spec_dict.setdefault("task_id", task_id)
            # The dispatcher's external run_id is what the watcher
            # will poll. We let the adapter generate it via
            # ``_new_run_id()`` (UUID-based) and then write it back
            # to the dispatcher via the existing
            # ``submit_result.external_run_id`` path — no need to
            # pin run_id in spec.
            spec_dict.pop("run_id", None)
            job.spec = spec_dict
        # Audit log entry — small but enough to trace which path
        # the router took for a given task.
        try:
            manager.log(
                task_id,
                f"router: requested={decision.requested_executor!r} "
                f"selected={decision.selected_executor!r} "
                f"source={decision.selection_source!r}",
            )
        except Exception:  # noqa: BLE001
            pass
        # Stash the decision so the response builder below can
        # surface the actual selected executor as observable
        # routing evidence (TASK-20260719-0046 §4).
        executor_decision = decision
    adapter = adapter_registry.get(job.adapter_name)
    try:
        submit_result = await adapter.submit(job)
    except AdapterRuntimeError as exc:
        # Preserve the original HTTP semantics:
        #   TimeoutException -> 504
        #   Other transport  -> 502
        kind = type(exc).__cause__.__name__ if exc.__cause__ else "Error"
        is_timeout = "Timeout" in kind
        manager.fail(
            task_id,
            f"Upstream {adapter.name} {'timeout' if is_timeout else 'error'}: {kind}",
        )
        raise HTTPException(
            status_code=504 if is_timeout else 502,
            detail=f"Upstream {adapter.name} {'timeout' if is_timeout else 'error'}: {kind}: {exc}",
        ) from exc

    run_id = submit_result.external_run_id
    if not run_id:
        manager.fail(task_id, f"Adapter {adapter.name} returned no external_run_id")
        raise HTTPException(
            status_code=502,
            detail=f"Adapter {adapter.name} returned no external_run_id",
        )

    # ----- Mark started; record both hermes_run_id (compat) and
    # external_run_id (AEE-1 canonical). AEE-2 writes both so legacy
    # `find_by_hermes_run_id` lookups keep working.
    try:
        manager.start(task_id, run_id)
    except IllegalTransition as exc:
        manager.warning(task_id, f"start transition: {exc}")
    # Stamp AEE-1 fields (external_run_id + adapter metadata). The
    # dispatcher's `start()` only knows `hermes_run_id`; AEE-2 fills
    # the runtime-neutral column here.
    from dispatcher import db as _db
    with _db.transaction() as conn2:
        conn2.execute(
            "UPDATE tasks SET external_run_id = ?, runtime_type = ?, "
            "adapter_name = ? WHERE task_id = ?",
            (run_id, job.runtime_type, job.adapter_name, task_id),
        )
    manager.log(task_id, f"upstream run started, hermes_run_id={run_id}, adapter={adapter.name}")

    # ------------------------------------------------------------------
    # Task-Mapping work-order: persist a durable ``executor_runs``
    # mapping row for this Hermes-dispatched run so that the read
    # paths (GET /runs list, GET /runs/{id}, GET /runs/{id}/summary)
    # can find it in the same canonical store used by
    # POST /runs/executor. Without this row, the hermes run is
    # invisible to ``GET /runs`` (list) which only reads
    # ``executor_runs`` (root cause C1/C3 of the investigation
    # report at /home/ubuntu/Abacus/AEE_RUNTIME_PERSISTENCE_TELEGRAM_INVESTIGATION.md).
    # Best-effort: a persistence failure MUST NOT break the dispatch
    # response. ``task_id`` and ``selected_executor='hermes'`` are
    # stamped so the mapping is canonical + queryable.
    _persist_hermes_run_mapping(
        run_id=run_id,
        task_id=task_id,
        status="running",
        routing={
            "client_source": source,
            "model_name": effective_model_name,
            "selected_executor": "hermes",
            "requested_executor": None,
            "selection_source": "default",
            "was_forced": resolved.was_forced,
            "reason": resolved.reason,
            "profile": resolved_profile,
        },
    )

    return CreateRunResponse(
        run_id=run_id,
        status=submit_result.status or "started",
        session_id=session_id,
        poll_url=f"/runs/{run_id}",
        requires_review=False,  # 2026-07-08: 鼎鼎指示 — 強制關閉，GPT 外部呼叫直接拿資訊；safety 仍記錄在 body.safety
        task_id=task_id,
        task_poll_url=f"/tasks/{task_id}",
        progress_pct=5,
        safety=decision.to_dict(),
        routing={
            "client_source": source,
            "model_name": effective_model_name,
            "was_forced": resolved.was_forced,
            "reason": resolved.reason,
            "caller_model": body.model_name,
            # Epic 9.4 §21.4: surface the resolved profile so the
            # caller can confirm which profile was active at dispatch
            # time. ``body.profile`` may be None; ``resolved_profile``
            # is always one of {full, mini, edge, developer}.
            "profile": resolved_profile,
            # TASK-20260719-0046 §4: surface the executor routing
            # decision as observable evidence. ``executor_decision``
            # is None when the caller passed no ``metadata`` (legacy
            # default-Hermes path); otherwise it carries the
            # requested and selected executor plus the selection
            # source. This lets the caller verify that an explicit
            # ``executor=claude_code`` request was honored (not
            # silently downgraded). See
            # ``aee.runtimes.executor_router.RoutingDecision.to_dict``
            # for the schema.
            "executor": (
                executor_decision.to_dict() if executor_decision is not None else None
            ),
        },
    )


# ---------------------------------------------------------------------------
# Final-mile executor endpoint: POST /runs/executor
# ---------------------------------------------------------------------------
# A dedicated, GPT-callable executor dispatch surface (work order
# TASK_AEE_CLAUDE_CODE_EXECUTOR_WIRING). Additive only: it does NOT
# touch ``create_run`` or the GPT -> MiniMax-M3 routing layer above.
# The endpoint never calls ``resolve_model_for_source``, so MiniMax-M3
# can never be forced here — ``routing.effective_executor`` always
# reflects the user-requested executor verbatim.
#
# Contract: validate config -> select executor -> launch -> track ->
# verify artifacts/evidence -> report. No second planner/orchestrator.
from aee.runtimes.executor_api import ExecutorRunRequest  # noqa: E402


def _attempt_telegram(subject: str, text: str) -> Dict[str, Any]:
    """Best-effort Telegram notification for an executor run.

    Uses bridge-env ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID`` only.
    Never raises; returns a truthful ``telegram_result`` dict:
    ``{success, message_id, recipient}`` on success, or
    ``{success: False, skipped: <reason>}`` when creds are absent /
    the send fails. The separate report-time Telegram send (§9) uses
    ``hermes send`` directly and is not this function's concern.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return {
            "success": False,
            "skipped": "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not configured in bridge env",
        }
    import json as _json
    import urllib.error as _urlerr
    import urllib.parse as _urlparse
    import urllib.request as _urlreq
    payload = _urlparse.urlencode({
        "chat_id": chat_id,
        "text": f"{subject}\n\n{text}",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = _urlreq.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        method="POST",
    )
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with _urlreq.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        if not data.get("ok"):
            return {"success": False, "skipped": f"telegram not ok: {data!r}"}
        msg_id = (data.get("result") or {}).get("message_id")
        return {"success": True, "message_id": msg_id, "recipient": chat_id}
    except (_urlerr.URLError, _urlerr.HTTPError, OSError, ValueError) as exc:
        return {"success": False, "skipped": f"{type(exc).__name__}: {exc}"}


@app.get("/executors")
async def list_executors(
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Read-only executor capability discovery (work-order Part A).

    Returns the executors this bridge currently supports, the configured
    default, and the alias map that canonicalises a requested name to its
    wire value. Pure read-only: no dispatch, no task creation, no executor
    launch, no runtime mutation, no side effects. Exists only so a GPT /
    operator can discover capabilities before calling
    ``POST /runs/executor``.
    """
    require_auth(authorization)
    from aee.runtimes.executor_config import load_executor_config, supported_executors

    cfg = load_executor_config()
    supported = supported_executors(cfg)
    aliases_raw = cfg.get("executor_aliases") or {}
    # Surface only the non-identity aliases (the example in the work order
    # excludes the ``claude-code-cli -> claude-code-cli`` self-map). Keys
    # and values are kept verbatim from config; unknown shapes are skipped.
    aliases: Dict[str, str] = {}
    if isinstance(aliases_raw, dict):
        for k, v in aliases_raw.items():
            if isinstance(k, str) and isinstance(v, str) and k != v:
                aliases[k] = v
    return {
        "supported_executors": supported,
        "default_executor": cfg.get("default_executor"),
        "aliases": aliases,
    }


async def _reconcile_hermes_run_once(
    run_id: str,
    persisted: Dict[str, Any],
) -> Dict[str, Any]:
    """Bounded reconciliation: poll upstream Hermes once for a non-terminal run.

    Shared core used by BOTH the GET-triggered reconciliation path
    (``_maybe_reconcile_hermes_run``) and the background
    ``ExecutorRunWatcher`` (TASK-AEE-P2-BRIDGE-HERMES-COMPLETION-SYNC).
    Only Hermes-dispatched runs (``selected_executor == "hermes"``)
    that are still in a non-terminal state
    (``queued``/``running``/``started``) are reconciled.  The
    function awaits ``adapter.poll(external_run_id)`` exactly once
    and, if the upstream reports a terminal state, updates the
    durable ``executor_runs`` row in-place with the final fields.
    Any error (adapter unavailable, upstream 404, transient HTTP
    error, non-terminal upstream report) is swallowed — the stale
    in-flight envelope is returned unchanged so callers can keep
    polling.  Idempotent: a row that is already terminal is returned
    as-is without any upstream call.

    Returns the (possibly updated) envelope dict.  Never raises.

    Safety:
      * No executor launch — only a read-only GET on Hermes 8642.
      * No mutation of unrelated rows.
      * Bounded — exactly one upstream call per invocation, and only
        when the row is non-terminal + Hermes-dispatched.
    """
    _TERMINAL = {"completed", "failed", "timeout", "cancelled"}
    try:
        if persisted.get("selected_executor") != "hermes":
            return persisted
        if persisted.get("status") in _TERMINAL:
            return persisted
    except Exception:  # pragma: no cover - defensive
        return persisted

    # Tests inject a stub adapter into ``adapter_registry``; the
    # production path uses ``HermesAdapter`` (registered by
    # ``bootstrap_defaults``).  When the registry does not have
    # one we fall back to building a default adapter from the env.
    from aee.adapters.base import (
        RuntimePollResult,
        UnknownExternalRunError,
        RuntimeError as AdapterRuntimeError,
    )
    from aee.core.registry import adapter_registry

    adapter = None
    try:
        adapter = adapter_registry.get("hermes")
    except Exception:  # pragma: no cover - registry miss falls through
        adapter = None
    if adapter is None:
        try:
            from aee.adapters.hermes_adapter import build_default as _build_hermes
            adapter = _build_hermes()
        except Exception:  # pragma: no cover - env not configured
            return persisted

    poll_result: Optional[RuntimePollResult] = None
    try:
        poll_result = await adapter.poll(run_id)
    except UnknownExternalRunError:
        # Upstream no longer tracks the run.  Persist a ``timeout``
        # state so callers see a deterministic terminal envelope
        # instead of an endless in-flight row.  This mirrors the
        # watcher's handling of UnknownExternalRunError.
        return _persist_terminal_reconciliation(
            run_id, persisted,
            status="timeout",
            error=f"upstream Hermes no longer tracks run_id={run_id!r}",
        )
    except AdapterRuntimeError:
        # Transient upstream error — leave the row in-flight.
        return persisted
    except Exception:  # pragma: no cover - defensive
        return persisted

    if poll_result is None:
        return persisted
    if not poll_result.is_terminal:
        return persisted

    # Translate the poll result into the persisted envelope.  Hermes'
    # output/error/usage become the new stdout_summary/error/usage.
    raw = dict(poll_result.raw) if isinstance(poll_result.raw, dict) else None
    out_text = poll_result.output
    err_text = poll_result.error or (raw.get("error") if raw else None) or ""
    new_status = (poll_result.status or "").lower() or "completed"
    if new_status not in _TERMINAL:
        new_status = "completed" if new_status in {"completed", "succeeded", "success"} else "failed"

    return _persist_terminal_reconciliation(
        run_id, persisted,
        status=new_status,
        stdout_summary=_truncate_for_envelope(out_text),
        error=str(err_text)[:2000] if err_text else None,
    )


async def _maybe_reconcile_hermes_run(
    run_id: str,
    persisted: Dict[str, Any],
) -> Dict[str, Any]:
    """GET-triggered bounded reconciliation wrapper.

    Thin wrapper around :func:`_reconcile_hermes_run_once` preserving
    the exact GET /runs/{run_id} contract established by commit
    ``5eb83f6``. The background ``ExecutorRunWatcher`` (P2.1) calls
    the shared core directly; this wrapper exists so the GET path's
    call site and the existing ``tests/test_completion_sync.py``
    suite continue to work byte-for-byte unchanged.
    """
    return await _reconcile_hermes_run_once(run_id, persisted)


def _truncate_for_envelope(text: Any, cap: int = 2000) -> str:
    if not text:
        return ""
    s = str(text)
    if len(s) <= cap:
        return s
    return s[:cap] + "...[truncated]"


def _utc_now_iso() -> str:
    """ISO-8601 UTC timestamp — shared helper for the P1.1 write path.

    Centralised so the terminal reconciliation + the initial dispatch
    use byte-identical timestamp formats.
    """
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _persist_terminal_reconciliation(
    run_id: str,
    persisted: Dict[str, Any],
    *,
    status: str,
    stdout_summary: Optional[str] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """Update the durable ``executor_runs`` row with the final state.

    Idempotent: ``upsert_run`` does ``INSERT OR REPLACE`` keyed by
    ``run_id``.  ``created_at`` is preserved by ``upsert_run``; the
    new ``completed_at`` is stamped only if the row was not already
    terminal.  The envelope returned by ``upsert_run`` is the same
    shape as ``get_run``, so callers can return it directly.

    P1.1 write-side activation (TASK-AEE-RUN-OBSERVABILITY-WRITE-ACTIVATION):
    terminal reconciliation stamps the terminal phase + the truthful
    final heartbeat + the lifecycle step matching the new status. The
    step is validated by ``upsert_run``'s call site against
    ``LIFECYCLE_STEPS``. The persisted row is now the single source
    of truth for the observability read path.
    """
    try:
        from dispatcher.db import get_conn
        from dispatcher.executor_runs import upsert_run
        envelope = dict(persisted)
        envelope["status"] = status
        envelope["progress"] = 1.0
        if stdout_summary is not None:
            envelope["stdout_summary"] = stdout_summary
        if error is not None:
            envelope["error"] = error
        # Preserve the existing routing/runtime_identity/etc. fields
        # by passing them through verbatim — upsert_run will re-encode
        # them.  ``completed_at`` is stamped by upsert_run when the
        # status is terminal.
        # P1.1: terminal transition persists phase=terminal, the
        # truthful final heartbeat (now), and the lifecycle step
        # matching the new status (work-order §5). The step is in
        # ``LIFECYCLE_STEPS`` (the terminal subset) by construction
        # because ``status`` is one of {completed, failed, timeout,
        # cancelled} which maps 1:1 to the terminal-step vocabulary.
        conn = get_conn()
        return upsert_run(
            conn,
            run_id=run_id,
            requested_executor=envelope.get("requested_executor"),
            selected_executor=envelope.get("selected_executor", "hermes"),
            task_id=envelope.get("task_id"),
            status=status,
            progress=1.0,
            exit_code=envelope.get("exit_code"),
            timeout_state=envelope.get("timeout_state"),
            cancel_state=envelope.get("cancel_state"),
            stdout_summary=envelope.get("stdout_summary", "") or "",
            stderr_summary=envelope.get("stderr_summary", "") or "",
            artifact_paths=envelope.get("artifact_paths") or [],
            artifact_verification=envelope.get("artifact_verification") or [],
            git_evidence=envelope.get("git_evidence"),
            telegram_result=envelope.get("telegram_result") or {},
            runtime_identity=envelope.get("runtime_identity"),
            routing=envelope.get("routing") or {},
            error=error if error is not None else envelope.get("error"),
            # P1.1: terminal heartbeat + phase + step. The status
            # itself is the truthful step (one of the canonical
            # terminal steps); ``last_heartbeat_at`` is now because
            # this is the terminal write; ``phase`` is ``"terminal"``.
            last_heartbeat_at=_utc_now_iso(),
            current_step=status,
            phase="terminal",
        )
    except Exception as exc:  # pragma: no cover - defensive
        import sys
        print(
            f"[reconcile] persistence failed for run_id={run_id!r}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        # Fall back to the in-memory envelope with the new state so
        # the GET response still reflects the terminal state even if
        # the durable write failed (the next GET will retry the write
        # via upsert_run idempotently).
        out = dict(persisted)
        out["status"] = status
        out["progress"] = 1.0
        if stdout_summary is not None:
            out["stdout_summary"] = stdout_summary
        if error is not None:
            out["error"] = error
        return out


def _persist_hermes_run_mapping(
    *,
    run_id: str,
    task_id: str,
    status: str = "running",
    routing: Optional[Dict[str, Any]] = None,
) -> None:
    """Persist a ``executor_runs`` mapping row for a Hermes-dispatched run.

    This closes the two-table split root cause (C1/C3) for ``POST /runs``:
    Hermes-adapter runs were only written to the ``tasks`` table, leaving
    ``GET /runs`` (list) and the summary endpoint unable to find them in
    the canonical ``executor_runs`` store. This helper writes a minimal
    mapping row with ``selected_executor='hermes'`` and the dispatcher's
    ``task_id`` so list/get/summary all resolve via the same store.

    Best-effort: a persistence failure MUST NOT break the dispatch
    response. Errors are logged to stderr and swallowed (mirroring
    ``_persist_executor_run``). ``upsert_run`` is idempotent on
    ``run_id``, so subsequent lifecycle updates (complete/fail) can
    re-call this with the same ``run_id`` to advance the row.
    """
    try:
        from dispatcher.db import get_conn
        from dispatcher.executor_runs import upsert_run

        _status = status
        _terminal = _status in {"completed", "failed", "timeout", "cancelled"}
        _phase = "terminal" if _terminal else (
            "queued" if _status in {"queued", "pending"} else "running"
        )
        _step = _status if _terminal else (
            "queued" if _status in {"queued", "pending"} else "running"
        )
        conn = get_conn()
        upsert_run(
            conn,
            run_id=run_id,
            requested_executor=None,
            selected_executor="hermes",
            task_id=task_id,
            status=_status,
            progress=1.0 if _terminal else 0.0,
            routing=routing or {
                "selected_executor": "hermes",
                "selection_source": "default",
            },
            last_heartbeat_at=_utc_now_iso(),
            current_step=_step,
            phase=_phase,
        )
    except Exception as exc:  # pragma: no cover - defensive
        import sys
        print(
            f"[hermes_run_mapping] persistence failed for run_id="
            f"{run_id!r}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def _persist_executor_run(envelope: Dict[str, Any]) -> None:
    """Persist a POST /runs/executor response envelope to executor_runs.

    Best-effort: a persistence failure MUST NOT break the dispatch
    response. The work-order requires the run to be pollable later,
    but if the DB is unavailable the response still carries the full
    evidence envelope; the caller can re-dispatch if needed. Errors
    are logged to stderr and swallowed.

    P1.1 write-side activation (TASK-AEE-RUN-OBSERVABILITY-WRITE-ACTIVATION):
    derives the truthful ``phase`` and ``current_step`` from the
    envelope's ``status`` so the persisted row carries the canonical
    observability fields from the very first write. Terminal
    statuses stamp ``phase="terminal"`` + ``current_step=<status>``;
    non-terminal statuses stamp ``phase="queued"`` / ``"running"``
    (mirroring ``dispatcher.observability.derive_phase``) +
    ``current_step="queued"`` / ``"running"``. The Claude CLI executor's
    live poll loop (``ClaudeCodeCliRunner.run``) writes subsequent
    heartbeats via ``update_heartbeat``; this initial persist is the
    row-creation write.
    """
    try:
        from dispatcher.db import get_conn
        from dispatcher.executor_runs import upsert_run
        # P1.1: derive the truthful phase + step from the envelope's
        # status so the persisted row is a complete observability
        # source from the first write. The terminal step matches the
        # status 1:1 (work-order §6).
        _status = envelope["status"]
        _terminal = _status in {"completed", "failed", "timeout", "cancelled"}
        _phase = "terminal" if _terminal else (
            "queued" if _status in {"queued", "pending"} else "running"
        )
        _step = _status if _terminal else (
            "queued" if _status in {"queued", "pending"} else "running"
        )
        conn = get_conn()
        upsert_run(
            conn,
            run_id=envelope["run_id"],
            requested_executor=envelope.get("requested_executor"),
            selected_executor=envelope["selected_executor"],
            task_id=envelope.get("task_id"),
            status=envelope["status"],
            progress=envelope.get("progress", 0.0),
            exit_code=envelope.get("exit_code"),
            timeout_state=envelope.get("timeout_state"),
            cancel_state=envelope.get("cancel_state"),
            stdout_summary=envelope.get("stdout_summary", ""),
            stderr_summary=envelope.get("stderr_summary", ""),
            artifact_paths=envelope.get("artifact_paths"),
            artifact_verification=envelope.get("artifact_verification"),
            git_evidence=envelope.get("git_evidence"),
            telegram_result=envelope.get("telegram_result"),
            runtime_identity=envelope.get("runtime_identity"),
            routing=envelope.get("routing"),
            error=envelope.get("error"),
            # P1.1: persist the canonical observability fields from
            # the very first write so a GET immediately after the
            # dispatch returns a complete envelope. The terminal
            # heartbeat stamp is truthful: for terminal dispatches
            # it is now; for non-terminal it is now (the live poll
            # loop will advance it on subsequent iterations).
            last_heartbeat_at=_utc_now_iso(),
            current_step=_step,
            phase=_phase,
        )
    except Exception as exc:  # pragma: no cover - defensive
        import sys
        print(
            f"[executor_runs] persistence failed for run_id="
            f"{envelope.get('run_id')!r}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


@app.post("/runs/executor")
async def create_executor_run(
    body: ExecutorRunRequest,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Dispatch to an explicit executor and return the full evidence envelope.

    Supported executors (config-driven, see ``config/executor.json``):
    ``claude-code-cli`` (direct Claude Code CLI; aliases ``claude_code``,
    ``claude-code``) and ``hermes`` (legacy Hermes provider). The
    response carries ``selected_executor`` / ``requested_executor`` /
    ``routing`` plus the full evidence envelope (artifact_paths,
    stdout_summary, stderr_summary, exit_code, timeout_state,
    cancel_state, git_evidence, artifact_verification,
    telegram_result, runtime_identity). No silent fallback; unsupported
    executors return a deterministic 400 ``unsupported_executor``.
    """
    source = require_auth(authorization)
    from aee.runtimes.executor_config import (
        canonical_executor,
        load_executor_config,
        supported_executors,
    )
    from aee.runtimes.executor_api import build_routing, build_executor_response
    from aee.runtimes.executor_envelope import (
        collect_git_evidence,
        truncate_summary,
        verify_artifacts,
    )
    from aee.runtimes.runtime_identity import collect_runtime_identity

    cfg = load_executor_config()
    requested = body.executor
    defaulted = requested is None
    effective_request = requested if requested is not None else cfg.get("default_executor")
    selected = canonical_executor(effective_request, cfg)
    if selected is None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "unsupported_executor",
                "message": (
                    f"executor {requested!r} is not supported; "
                    f"accepted aliases canonicalise to 'claude-code-cli' or 'hermes'"
                ),
                "supported_executors": supported_executors(cfg),
            },
        )

    # repo_path: default to the Abacus repo (a real git worktree) so
    # git_evidence is meaningful even when the caller omits it. Enforce
    # the configured allow-list; reject escapes.
    repo_path = body.repo_path or "/home/ubuntu/Abacus"
    allowlist = [p for p in (cfg.get("repo_allowlist") or []) if isinstance(p, str)]
    if not any(
        repo_path == p or repo_path.startswith(p.rstrip("/") + "/")
        for p in allowlist
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "repo_path_not_allowed",
                "message": (
                    f"repo_path {repo_path!r} is outside the configured allow-list"
                ),
                "allowed": allowlist,
            },
        )

    timeout = int(body.timeout_sec or cfg.get("default_timeout_sec", 120))
    timeout = min(timeout, int(cfg.get("max_timeout_sec", 7200)))

    # Truthful routing: was_forced is always False on this endpoint —
    # the GPT -> MiniMax-M3 layer is never consulted. effective_executor
    # echoes the user's choice verbatim (canonicalised).
    routing = build_routing(
        requested=requested,
        selected=selected,
        selection_source=("default" if defaulted else "explicit"),
        effective_executor=selected,
        effective_model=None,
        was_forced=False,
        reason=("default" if defaulted else "explicit_executor_opt_in"),
    )

    # ------------------------------------------------------------------
    # Task-Mapping work-order: create a dispatcher ``tasks`` row for
    # every executor run so that ``executor_runs.task_id`` is non-NULL
    # for newly created runs (root cause C5 of the investigation
    # report at /home/ubuntu/Abacus/AEE_RUNTIME_PERSISTENCE_TELEGRAM_INVESTIGATION.md).
    # The row is created in ``queued`` state; the executor path does
    # not call ``manager.start()`` (no upstream Hermes run to track),
    # and the watcher's completion gate is responsible for advancing
    # the row to a terminal status when the executor finishes. The
    # ``executor_session_id`` field records the caller's session id so
    # the read-side identity validator can attribute the run back to
    # the orchestrator that issued it. Best-effort: a create failure
    # is logged and swallowed so the dispatch still returns the
    # executor's evidence envelope with ``task_id=None`` (matching the
    # pre-fix contract for failure paths) rather than 500'ing.
    executor_task_id: Optional[str] = None
    try:
        from dispatcher.manager import TaskManager as _TaskManager
        _etask = _TaskManager().create(
            title=f"executor-run:{selected}",
            type="ops",
            input_text=body.prompt,
            session_id=None,
            mode="normal",
            owner="m2",
            model_name=None,
            workdir=None,
            initial_status="queued",
            required_capabilities=None,
            repo_root=repo_path,
            executor_session_id=source,
            profile=None,
        )
        executor_task_id = _etask.task_id
    except Exception as exc:  # pragma: no cover - defensive
        import sys as _sys
        print(
            f"[executor_run_mapping] tasks.create failed for "
            f"selected={selected!r}: {type(exc).__name__}: {exc}",
            file=_sys.stderr,
        )

    if selected == "claude-code-cli":
        from aee.runtimes.executor_cli import ClaudeCodeCliRunner

        if body.max_turns is not None:
            runner = ClaudeCodeCliRunner(
                binary=str(cfg.get("claude_cli_binary") or "/home/ubuntu/.local/bin/claude"),
                max_turns=int(body.max_turns),
                output_format=str(cfg.get("output_format") or "text"),
                bare=bool(cfg.get("bare", False)),
                extra_cli_args=[str(a) for a in (cfg.get("extra_cli_args") or [])] or None,
            )
        else:
            runner = ClaudeCodeCliRunner.from_config(cfg)
        result = await runner.run(
            prompt=body.prompt,
            cwd=repo_path,
            timeout_sec=timeout,
            expected_artifacts=body.expected_artifacts,
        )
        artifact_verification = verify_artifacts(
            body.expected_artifacts,
            compute_sha256=bool(cfg.get("artifact_sha256", True)),
        )
        git_evidence = collect_git_evidence(repo_path)
        telegram_result = _attempt_telegram(
            f"AEE executor run {result.run_id}: {result.status}",
            f"executor={selected}\nstatus={result.status}\nexit_code={result.exit_code}",
        )
        progress = 1.0 if result.status in ("completed", "failed", "timeout", "cancelled") else 0.0
        runtime_identity = collect_runtime_identity(
            selected_executor=selected, cfg=cfg
        )
        envelope = build_executor_response(
            requested_executor=requested,
            selected_executor=selected,
            run_id=result.run_id,
            status=result.status,
            routing=routing,
            task_id=executor_task_id,
            progress=progress,
            artifact_paths=result.artifact_paths,
            stdout_summary=truncate_summary(
                result.stdout, int(cfg.get("stdout_summary_cap", 2000))
            ),
            stderr_summary=truncate_summary(
                result.stderr, int(cfg.get("stderr_summary_cap", 1000))
            ),
            exit_code=result.exit_code,
            timeout_state=result.timeout_state,
            cancel_state=result.cancel_state,
            git_evidence=git_evidence,
            artifact_verification=artifact_verification,
            telegram_result=telegram_result,
            runtime_identity=runtime_identity,
            error=result.error,
        )
        _persist_executor_run(envelope)
        return envelope

    # selected == "hermes" — delegate to the existing Hermes adapter
    # (registered in ``adapter_registry``). Tests stub the adapter; in
    # production this submits to Hermes 8642. Hermes is async, so the
    # envelope returns a queued state with the upstream run_id; the
    # per-run evidence fields are null/skipped (Hermes does not produce
    # local artifacts / git evidence / a per-run Telegram on submit).
    from aee.adapters.base import RuntimeError as AdapterRuntimeError  # noqa: F811
    from aee.core.job_models import Job as AEEJob
    from aee.core.registry import adapter_registry

    job = AEEJob(
        title="executor-run",
        type="ops",
        mode="normal",
        input=body.prompt,
        client_source=source,
        adapter_name="hermes",
        runtime_type="hermes",
        expected_artifacts=body.expected_artifacts or [],
    )
    try:
        adapter = adapter_registry.get("hermes")
        submit_result = await adapter.submit(job)
    except AdapterRuntimeError as exc:
        runtime_identity = collect_runtime_identity(
            selected_executor=selected, cfg=cfg
        )
        envelope = build_executor_response(
            requested_executor=requested,
            selected_executor=selected,
            run_id="hermes-submit-failed",
            status="failed",
            routing=routing,
            task_id=executor_task_id,
            error=f"hermes submit error: {exc}",
            telegram_result={
                "success": False,
                "skipped": "hermes submit failed; no notification sent",
            },
            runtime_identity=runtime_identity,
        )
        _persist_executor_run(envelope)
        return envelope
    runtime_identity = collect_runtime_identity(
        selected_executor=selected, cfg=cfg
    )
    envelope = build_executor_response(
        requested_executor=requested,
        selected_executor=selected,
        run_id=submit_result.external_run_id,
        status=submit_result.status or "queued",
        routing=routing,
        task_id=executor_task_id,
        progress=0.0,
        artifact_paths=[],
        git_evidence=None,
        telegram_result={
            "success": False,
            "skipped": "hermes is async; per-run telegram not sent on submit",
        },
        runtime_identity=runtime_identity,
    )
    _persist_executor_run(envelope)
    return envelope


import re

# Run-id validation: work-order §3.1 supports Hermes async run IDs
# such as ``run_5f346ad4dd7c4f27beaefccec65c5175`` and Claude Code
# run IDs such as ``claude-cli-2322a3f2af5e``. The bridge also still
# accepts the legacy dispatcher task ids (e.g. ``TASK-20260722-0001``).
# The validation is intentionally permissive but rejects obviously
# malformed inputs: empty, whitespace, control chars, path
# separators, > 200 chars, or any char outside the union of
# ``[A-Za-z0-9_-]`` plus the literal ``run_`` prefix and the
# ``TASK-`` / ``claude-cli-`` shapes. We do NOT enforce a specific
# prefix — that would break Hermes' opaque run_ids — only that the
# id is a non-empty token of printable, non-slash chars.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,199}\Z")


def _malformed_run_id(run_id: str) -> bool:
    """Return True if ``run_id`` cannot be a valid run identifier."""
    if not isinstance(run_id, str):
        return True
    if not run_id or len(run_id) > 200:
        return True
    if "/" in run_id or "\\" in run_id or " " in run_id:
        return True
    if any(ord(c) < 32 for c in run_id):
        return True
    return not _RUN_ID_RE.match(run_id) is not None


# Work-order WO-FIX-API-SERIALIZATION-MERGE-HERMES-EVIDENCE:
# A Hermes lifecycle-sync ``executor_runs`` row is persisted by
# ``manager.complete()/fail()/timeout()/cancel()`` (Fix D) with
# only the terminal ``status`` and ``task_id`` populated — the
# ``stdout_summary``, ``artifact_paths``, ``artifact_verification``,
# ``git_evidence`` and ``telegram_result`` fields are left empty
# because the lifecycle hook does not have the executor transcript
# (those live in the dispatcher ``tasks``/``task_outputs``/
# ``artifacts`` tables). When the executor-side row has empty
# evidence AND a linked ``task_id`` whose task-side evidence is
# non-empty, merge the task-side evidence into the response so the
# GET /runs/{run_id} and summary endpoints surface the real
# artifacts/output the run produced, instead of an empty stub that
# looks like the run produced nothing.
#
# Detection is evidence-based and conservative:
#   * ``persisted.task_id`` is non-empty (there is a linked task)
#   * the executor-side evidence fields are all empty
#     (``stdout_summary`` empty, ``artifact_paths`` empty,
#      ``artifact_verification`` empty, ``git_evidence`` falsy)
#   * the task-side has at least one non-empty evidence field
#     (``output_text``, ``delivery_json`` artifacts, or
#      ``artifacts`` table rows)
# Only when all three hold is the merge applied. Fully populated
# claude-code-cli executor_runs rows (which carry their own
# artifact/git/stdout evidence) are returned unchanged — the
# detection short-circuits on the first non-empty executor field.
#
# Authoritative lifecycle fields (``status``, ``exit_code``,
# timestamps, ``phase``/``current_step``) are ALWAYS preserved
# from ``executor_runs``; only evidence fields are merged in. The
# merged envelope is tagged ``source="executor_runs+tasks_merge"``
# so callers can distinguish a merged response from a pure
# executor_runs response.


def _executor_evidence_is_empty(persisted: Dict[str, Any]) -> bool:
    """Return True if the executor_runs row carries no evidence."""
    if (persisted.get("stdout_summary") or "").strip():
        return False
    if persisted.get("artifact_paths"):
        return False
    if persisted.get("artifact_verification"):
        return False
    if persisted.get("git_evidence"):
        return False
    return True


def _telegram_result_is_confirmed(value: Any) -> bool:
    """Return True iff ``value`` represents a confirmed Telegram delivery.

    A confirmed delivery is a dict where EITHER ``success`` OR ``sent``
    is True AND ``message_id`` is a non-None value. This is the merge
    gate used by ``_merge_task_evidence_into_envelope`` so the Hermes
    async submit placeholder
    (``{"success": False, "skipped": "hermes is async; ..."}``) — which
    is a truthy dict but NOT a confirmed delivery — is treated as
    empty and overwritten by the task-side ``telegram_result``
    (built from ``task_outputs.notification_json``, carrying
    ``sent: True`` + ``message_id`` from the Hermes Telegram Gateway).

    WO-FIX-TELEGRAM-RESULT-SYNC.
    """
    if not isinstance(value, dict):
        return False
    success = bool(value.get("success", value.get("sent", False)))
    message_id = value.get("message_id")
    return success and message_id is not None


def _collect_task_evidence(task_id: str) -> Optional[Dict[str, Any]]:
    """Read task-side evidence for ``task_id`` from the dispatcher DB.

    Returns ``None`` when the task is unknown or carries no evidence.
    Never raises — a read failure degrades to ``None`` so the caller
    falls back to the original executor_runs envelope.

    Evidence collected:
      * ``output_text``     — from ``task_outputs.output_text``
      * ``artifact_paths``  — union of ``delivery_json`` paths and
                               ``artifacts`` table paths
      * ``artifact_verification`` — from the ``artifacts`` table rows
        (path / exists / size / mtime / sha256), mirroring the shape
        ``verify_artifacts()`` produces for the claude-code-cli path.
      * ``delivery_json``   — raw delivery blob (list of paths or
                               dict with ``artifacts`` key) so callers
                               can inspect the original structure.
      * ``notification_json`` — raw Telegram gate blob (kept verbatim
                                 so the merged envelope can surface
                                 ``telegram_result`` when present).
    """
    try:
        from dispatcher.db import get_conn
        conn = get_conn()
    except Exception:  # pragma: no cover - defensive
        return None

    try:
        out_row = conn.execute(
            "SELECT output_text, delivery_json, notification_json "
            "FROM task_outputs WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        output_text = out_row["output_text"] if out_row else None
        delivery_raw = out_row["delivery_json"] if out_row else None
        notification_raw = out_row["notification_json"] if out_row else None
    except Exception:  # pragma: no cover - defensive
        return None

    artifact_paths: List[str] = []
    artifact_verification: List[Dict[str, Any]] = []
    try:
        art_rows = conn.execute(
            "SELECT path, sha256, size, mtime, file_exists "
            "FROM artifacts WHERE task_id = ? "
            "ORDER BY collected_at DESC",
            (task_id,),
        ).fetchall()
        seen: set = set()
        for r in art_rows:
            p = r["path"]
            if not p or p in seen:
                continue
            seen.add(p)
            artifact_paths.append(p)
            try:
                size = int(r["size"]) if r["size"] is not None else None
            except (TypeError, ValueError):
                size = None
            artifact_verification.append({
                "path": p,
                "exists": bool(r["file_exists"]),
                "size": size,
                "mtime": r["mtime"],
                "sha256": r["sha256"],
            })
    except Exception:  # pragma: no cover - defensive
        pass

    # Augment artifact_paths from delivery_json when the artifacts
    # table was not populated (e.g. pre-AEE-6 tasks). The delivery
    # blob is either a list of path strings, a list of dicts with a
    # ``path`` key, or a dict with an ``artifacts`` key holding one
    # of those list shapes.
    delivery_parsed: Any = None
    if delivery_raw:
        try:
            delivery_parsed = json.loads(delivery_raw)
        except (ValueError, TypeError):
            delivery_parsed = None
    delivery_paths: List[str] = []
    if isinstance(delivery_parsed, list):
        for item in delivery_parsed:
            if isinstance(item, str):
                delivery_paths.append(item)
            elif isinstance(item, dict) and isinstance(item.get("path"), str):
                delivery_paths.append(item["path"])
    elif isinstance(delivery_parsed, dict):
        inner = delivery_parsed.get("artifacts")
        if isinstance(inner, list):
            for item in inner:
                if isinstance(item, str):
                    delivery_paths.append(item)
                elif isinstance(item, dict) and isinstance(item.get("path"), str):
                    delivery_paths.append(item["path"])
    for p in delivery_paths:
        if p and p not in artifact_paths:
            artifact_paths.append(p)

    # Telegram notification blob -> telegram_result shape that the
    # executor_runs envelope carries for the claude-code-cli path.
    telegram_result: Optional[Dict[str, Any]] = None
    if notification_raw:
        try:
            nblob = json.loads(notification_raw)
            if isinstance(nblob, dict):
                telegram_result = {
                    "sent": bool(nblob.get("sent")),
                    "method": nblob.get("method"),
                    "recipient": nblob.get("recipient"),
                    "message_id": nblob.get("message_id"),
                    "ts_utc": nblob.get("ts_utc"),
                    "ts_taipei": nblob.get("ts_taipei"),
                }
        except (ValueError, TypeError):
            telegram_result = None

    has_any = bool(
        (output_text and output_text.strip())
        or artifact_paths
        or artifact_verification
        or telegram_result
    )
    if not has_any:
        return None

    return {
        "output_text": output_text or "",
        "artifact_paths": artifact_paths,
        "artifact_verification": artifact_verification,
        "telegram_result": telegram_result,
        "delivery_json_raw": delivery_raw,
    }


def _merge_task_evidence_into_envelope(
    envelope: Dict[str, Any],
) -> Dict[str, Any]:
    """Merge task-side evidence into an executor_runs envelope.

    Detection (work-order §"REQUIRED BEHAVIOR"):
      * ``envelope.task_id`` is non-empty
      * ``_executor_evidence_is_empty(envelope)`` is True
      * ``_collect_task_evidence(task_id)`` returns non-None

    When the merge fires, the returned envelope keeps the
    authoritative lifecycle fields from ``executor_runs`` and
    populates the evidence fields from the task side. The
    ``source`` marker is set to ``"executor_runs+tasks_merge"``.

    When any detection predicate fails, the envelope is returned
    unchanged (so fully populated claude-code-cli rows and rows
    with no linked task are byte-for-byte identical to the
    pre-merge behaviour).
    """
    task_id = envelope.get("task_id")
    if not task_id:
        return envelope
    if not _executor_evidence_is_empty(envelope):
        # WO-FIX-TELEGRAM-RESULT-SYNC: even when the executor-side
        # envelope is NOT evidence-empty (e.g. reconciliation already
        # wrote stdout_summary), a non-confirmed ``telegram_result``
        # (the Hermes async submit placeholder) must still be replaced
        # by the task-side ``notification_json`` outcome when the
        # task-side has a confirmed delivery. Without this, stdout
        # shows a successful Telegram send with message_id but the
        # structured envelope returns ``telegram_result.success ==
        # False`` because the placeholder dict is truthy and the
        # early-return above skips the merge entirely.
        task_evidence = _collect_task_evidence(task_id)
        if task_evidence is not None and task_evidence.get("telegram_result"):
            if not _telegram_result_is_confirmed(envelope.get("telegram_result")):
                merged = dict(envelope)
                merged["telegram_result"] = dict(task_evidence["telegram_result"])
                return merged
        return envelope
    evidence = _collect_task_evidence(task_id)
    if evidence is None:
        return envelope

    # The merge fires when the task side carries ANY real evidence —
    # either a non-empty ``output_text`` OR at least one artifact
    # (work-order WO-FIX-OUTPUT-ONLY-HERMES-EVIDENCE-003 §6 "REQUIRED
    # BEHAVIOR": merge when ``task_outputs.output_text`` is non-empty
    # OR artifacts exist). A lifecycle-sync stub whose task has NEITHER
    # output_text NOR artifacts is the legitimate empty-task contract
    # (``_collect_task_evidence`` returns ``None`` for it, so the
    # ``evidence is None`` guard above already returns the envelope
    # unchanged); this second gate only rules out the residual case
    # where ``_collect_task_evidence`` returned non-None on the
    # strength of a ``telegram_result`` alone (no output, no
    # artifacts) — a notification row is not task evidence and must
    # not flip ``source`` to the merge marker.
    has_output = bool((evidence.get("output_text") or "").strip())
    has_artifacts = bool(evidence.get("artifact_paths"))
    if not (has_output or has_artifacts):
        return envelope

    merged = dict(envelope)
    # Evidence fields — populated from the task side only when the
    # executor-side field is empty (defensive: re-check each field
    # so a future caller that pre-populates one of them still wins).
    if not (merged.get("stdout_summary") or "").strip() and evidence.get("output_text"):
        merged["stdout_summary"] = _truncate_for_envelope(evidence["output_text"])
    if not merged.get("artifact_paths") and evidence.get("artifact_paths"):
        merged["artifact_paths"] = list(evidence["artifact_paths"])
    if not merged.get("artifact_verification") and evidence.get("artifact_verification"):
        merged["artifact_verification"] = list(evidence["artifact_verification"])
    # WO-FIX-TELEGRAM-RESULT-SYNC: the merge guard for telegram_result
    # must NOT fire on mere dict truthiness. The Hermes async submit
    # path (app.py:2027) persists a placeholder
    # ``{"success": False, "skipped": "hermes is async; ..."}`` into
    # executor_runs. That dict is truthy, so the previous
    # ``not merged.get("telegram_result")`` guard never let the
    # task-side ``telegram_result`` (built from
    # ``task_outputs.notification_json`` by ``_collect_task_evidence``,
    # carrying ``sent: True`` + ``message_id`` from the Hermes
    # Telegram Gateway) override it. The result: stdout showed a
    # successful Telegram send with message_id, but the structured
    # envelope returned ``telegram_result.success == False``.
    #
    # Fix: treat the executor-side telegram_result as "empty" for
    # merge purposes when it does NOT represent a confirmed delivery
    # — i.e. neither ``success`` nor ``sent`` is True with a
    # non-None ``message_id``. The task-side telegram_result then
    # overrides the placeholder, preserving the actual send outcome.
    if not _telegram_result_is_confirmed(merged.get("telegram_result")) and evidence.get("telegram_result"):
        merged["telegram_result"] = dict(evidence["telegram_result"])
    # git_evidence is NOT synthesized from the task side — the
    # dispatcher does not record git state for the task, and
    # fabricating one would violate the "no fabricated evidence"
    # contract. It stays whatever the executor_runs row had (None
    # for lifecycle-sync stubs).
    merged["source"] = "executor_runs+tasks_merge"
    return merged


@app.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Read-only poll of a run's current and final state.

    Work-order TASK-AEE-RUN-TRACKING-RESTORE: this endpoint returns a
    canonical JSON envelope for any run dispatched via
    ``POST /runs/executor`` (the executor_runs store) OR any
    dispatcher-tracked Hermes run (the ``tasks`` table). It does NOT
    launch a new executor, mutate run state, or scan the repo.

    Lookup order:
      1. ``executor_runs`` table (populated by POST /runs/executor
         for both claude-code-cli and hermes executors).
      2. ``tasks`` table via ``find_by_hermes_run_id`` (legacy
         dispatcher-backed runs that pre-date the executor store).
      3. Deterministic JSON 404 envelope when neither source has
         the run_id.

    Malformed run_id (empty, contains slashes, control chars,
    exceeds 200 chars) returns a deterministic JSON 400.
    """
    require_auth(authorization)

    # Malformed run_id — deterministic 400. We still go through
    # FastAPI's HTTPException so the response shape matches the
    # rest of the API (``{"detail": ...}``).
    if _malformed_run_id(run_id):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "malformed_run_id",
                "message": (
                    f"run_id {run_id!r} is not a valid run identifier; "
                    f"expected a non-empty token of printable, non-slash chars"
                ),
            },
        )

    from dispatcher.db import get_conn
    from dispatcher.executor_runs import get_run as _get_executor_run

    # 1) executor_runs table — the canonical source for any
    # POST /runs/executor dispatch (claude-code-cli or hermes).
    try:
        conn = get_conn()
        persisted = _get_executor_run(conn, run_id)
    except Exception:  # pragma: no cover - defensive
        persisted = None
    if persisted is not None:
        # Work-order TASK-AEE-HERMES-COMPLETION-SYNC: when the
        # persisted row was dispatched via the ``hermes`` executor
        # and is still in a non-terminal state (queued/running),
        # attempt ONE bounded reconciliation poll against the
        # upstream Hermes 8642. If Hermes reports a terminal state,
        # the row is updated in-place with the final fields and the
        # freshly-stamped envelope is returned. If Hermes is
        # unreachable, reports a non-terminal state, or the run is
        # not found upstream, the stale in-flight envelope is
        # returned unchanged (callers continue polling). The
        # reconciliation is idempotent: a terminal row is never
        # re-polled (terminal rows are returned as-is), and an
        # already-reconciled row carries the final state so a
        # duplicate GET does not re-launch any upstream call.
        persisted = await _maybe_reconcile_hermes_run(run_id, persisted)
        # The persisted envelope already matches the canonical
        # shape; add the source tag so callers can tell which
        # store served the response.
        envelope = dict(persisted)
        envelope["source"] = "executor_runs"
        envelope["is_terminal"] = envelope.get("status") in {
            "completed", "failed", "timeout", "cancelled",
        }
        # WO-FIX-API-SERIALIZATION-MERGE-HERMES-EVIDENCE: when the
        # persisted row is a Hermes lifecycle-sync stub (terminal
        # status, empty evidence) with a linked ``task_id``, merge
        # the task-side evidence (output / artifacts / telegram)
        # into the envelope. Fully populated rows short-circuit
        # inside the helper and are returned byte-for-byte unchanged.
        envelope = _merge_task_evidence_into_envelope(envelope)
        # P1 observability: derive the canonical observability
        # envelope from the **persisted post-reconciliation** row
        # (work-order §4). The reconciliation above may have updated
        # the row in-place with the final state; observability fields
        # are computed from whatever the persisted row now says, so
        # they reflect the post-reconciliation truth — never a
        # pre-reconciliation guess.
        from dispatcher.observability import derive_observability
        envelope.update(derive_observability(envelope))
        return envelope

    # 2) Dispatcher task table (legacy POST /runs runs that did not
    # go through POST /runs/executor). Preserve the pre-rewrite
    # behaviour for these: return the merged dispatcher view.
    manager = TaskManager()
    task = manager.find_by_hermes_run_id(run_id)
    if task is not None:
        out = manager.get_output(task.task_id) or {}
        envelope = {
            "run_id": run_id,
            "task_id": task.task_id,
            "status": task.status,
            "progress_pct": task.progress_pct,
            "progress_step": task.progress_step,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
            "duration_sec": task.duration_sec,
            "error": task.error_message,
            "warning_count": task.warning_count,
            "output": out.get("output_text"),
            "usage": out.get("usage"),
            "source": "dispatcher_tasks",
            "is_terminal": task.status in {
                "completed", "failed", "cancelled",
            },
            # WO-COMPLETION-GATE-MVP: surface the declared
            # expected_artifacts contract + the Phase-4 delivery
            # verification results (auto-scan) so callers can
            # distinguish "completed with delivery warnings" from
            # "failed at the gate". `expected_artifacts` is the
            # explicit contract (empty list = no contract);
            # `delivery_verification` is the Phase-4 auto-scan
            # result (always present, observability-only).
            "expected_artifacts": list(task.expected_artifacts or []),
            "delivery_verification": out.get("delivery_json"),
            # WO-INCOMPLETE-DELIVERY-AUTORESCUE: surface the rescue
            # lifecycle counters so callers can distinguish
            # ``incomplete_delivery`` (non-terminal, auto-rescue
            # queued) from ``failed`` (terminal). ``rescue_count`` is
            # the number of rescue attempts already executed;
            # ``max_rescues`` is the configured cap (0 = rescue
            # disabled). ``incomplete_delivery`` is non-terminal —
            # excluded from the is_terminal set above.
            "rescue_count": task.rescue_count,
            "max_rescues": task.max_rescues,
        }
        # P1 observability for the dispatcher-tasks fallback. The
        # ``tasks`` table does not carry an explicit ``updated_at``
        # column; we fall back to the most recent timestamp we have
        # on the row — ``finished_at`` for terminal runs,
        # ``started_at`` for non-terminal runs — so the stall policy
        # can produce a deterministic, non-fabricated outcome. When
        # neither is present the policy returns ``missing_timestamp``
        # (stalled=False). ``heartbeat_at`` (AEE-1 column) maps to
        # ``last_heartbeat_at``. ``current_step`` maps to
        # ``progress_step``. ``stdout_tail`` is derived from the
        # persisted ``output_text`` (the dispatcher's record of the
        # agent's final message) — never by scanning the repo.
        from dispatcher.observability import derive_observability
        obs_source = {
            "status": task.status,
            "updated_at": task.finished_at or task.started_at,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
            "duration_sec": task.duration_sec,
            "last_heartbeat_at": task.heartbeat_at,
            "current_step": task.progress_step,
            "output_text": out.get("output_text"),
        }
        envelope.update(derive_observability(obs_source))
        return envelope

    # 3) No persisted record. Deterministic 404 — do NOT call the
    # upstream Hermes adapter (that would launch a network call
    # and could 502 on a stale run_id, which the work-order
    # explicitly forbids: "Must not launch a new executor, mutate
    # run state, or require repo scanning").
    raise HTTPException(
        status_code=404,
        detail={
            "code": "unknown_run_id",
            "message": (
                f"run_id {run_id!r} not found in executor_runs or tasks"
            ),
            "run_id": run_id,
        },
    )


@app.get("/runs/{run_id}/summary")
async def get_run_summary(
    run_id: str,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Bridge-curated summary, friendly to ChatGPT.

    Work-order TASK-AEE-P2-RUN-RETRIEVAL-API-RESTORE: this endpoint
    is a STRICT PURE READ. It MUST NOT launch a new executor run,
    MUST NOT call the upstream Hermes adapter merely to inspect an
    existing persisted run, and MUST NOT mutate any state.

    Lookup order (mirrors ``GET /runs/{run_id}`` so the two
    endpoints agree on what "an existing run" means):
      1. ``executor_runs`` table (populated by POST /runs/executor
         for both claude-code-cli and hermes executors) — the
         canonical store for any run dispatched via the executor
         surface. The persisted envelope already carries
         routing/runtime/artifact/git/telegram evidence.
      2. ``tasks`` table via ``find_by_hermes_run_id`` (legacy
         dispatcher-backed runs that pre-date the executor store).
      3. Deterministic JSON 404 envelope when neither source has
         the run_id. The previous implementation fell through to
         ``adapter.poll(run_id)`` here, which launched an upstream
         Hermes call on every unknown id — that was the
         regression this restore fixes (commit f85804e left the
         summary endpoint on the pre-rewrite fall-through path
         while the full ``GET /runs/{run_id}`` was rewritten to
         be pure-read).
    """
    require_auth(authorization)

    # Malformed run_id — deterministic 400 (same gate as the full
    # GET /runs/{run_id} route so both endpoints agree).
    if _malformed_run_id(run_id):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "malformed_run_id",
                "message": (
                    f"run_id {run_id!r} is not a valid run identifier; "
                    f"expected a non-empty token of printable, non-slash chars"
                ),
            },
        )

    from dispatcher.db import get_conn
    from dispatcher.executor_runs import get_run as _get_executor_run

    # 1) executor_runs table — the canonical source for any
    # POST /runs/executor dispatch (claude-code-cli or hermes).
    try:
        conn = get_conn()
        persisted = _get_executor_run(conn, run_id)
    except Exception:  # pragma: no cover - defensive
        persisted = None
    if persisted is not None:
        # WO-FIX-API-SERIALIZATION-MERGE-HERMES-EVIDENCE: merge
        # task-side evidence into a Hermes lifecycle-sync stub
        # before computing the summary view. Fully populated rows
        # are returned unchanged by the helper.
        persisted = _merge_task_evidence_into_envelope(persisted)
        status = persisted.get("status") or "unknown"
        # The persisted stdout_summary is the bounded executor
        # transcript; the tasks-table output_text is the
        # dispatcher's record of the agent's final message. Both
        # are persisted evidence — never re-derived by polling.
        output = persisted.get("stdout_summary") or ""
        if status in {"completed", "failed", "timeout", "cancelled"}:
            hint = "Task ended. Read `output` and decide next step."
        elif status == "running":
            hint = "Task is still running. Poll again in a few seconds."
        else:
            hint = f"Task is in state: {status}. Re-check shortly."
        preview = output
        if isinstance(output, str) and len(output) > 2000:
            preview = output[:2000] + f"... [truncated, full length={len(output)}]"
        return {
            "run_id": run_id,
            "task_id": persisted.get("task_id"),
            "requested_executor": persisted.get("requested_executor"),
            "selected_executor": persisted.get("selected_executor"),
            "status": status,
            "progress": persisted.get("progress"),
            "exit_code": persisted.get("exit_code"),
            "timeout_state": persisted.get("timeout_state"),
            "cancel_state": persisted.get("cancel_state"),
            "phase": persisted.get("phase"),
            "current_step": persisted.get("current_step"),
            "last_heartbeat_at": persisted.get("last_heartbeat_at"),
            "created_at": persisted.get("created_at"),
            "updated_at": persisted.get("updated_at"),
            "completed_at": persisted.get("completed_at"),
            "last_event": None,
            "output_preview": preview,
            "artifact_paths": persisted.get("artifact_paths") or [],
            "artifact_count": len(persisted.get("artifact_paths") or []),
            "error": persisted.get("error"),
            "is_terminal": status in {
                "completed", "failed", "timeout", "cancelled",
            },
            "source": persisted.get("source", "executor_runs"),
            "current_hint": hint,
        }

    # 2) Dispatcher task table (legacy POST /runs runs that did not
    # go through POST /runs/executor). Preserve the pre-rewrite
    # behaviour for these: return the dispatcher's curated view.
    manager = TaskManager()
    task = manager.find_by_hermes_run_id(run_id)
    if task is not None:
        out = manager.get_output(task.task_id) or {}
        status = task.status
        output = out.get("output_text")
        if status == "completed":
            hint = "Task ended. Read `output` and decide next step."
        elif status == "running":
            hint = "Task is still running. Poll /tasks/{task_id}/progress in a few seconds."
        elif status == "failed":
            hint = "Task failed. Read `error` and decide next step."
        elif status == "cancelled":
            hint = "Task was cancelled."
        else:
            hint = f"Task is in state: {status}."
        preview = output if not (isinstance(output, str) and len(output) > 2000) else output[:2000] + "...[truncated]"
        return {
            "run_id": run_id,
            "task_id": task.task_id,
            "status": status,
            "progress_pct": task.progress_pct,
            "progress_step": task.progress_step,
            "last_event": None,
            "output_preview": preview,
            "error": task.error_message,
            "is_terminal": task.status in {
                "completed", "failed", "cancelled",
            },
            "source": "dispatcher_tasks",
            "current_hint": hint,
            # WO-COMPLETION-GATE-MVP: surface the declared
            # expected_artifacts contract + delivery verification
            # results in the summary view too so the orchestrator
            # can pattern-match on `missing_expected_artifacts`
            # without parsing the error_message prose.
            "expected_artifacts": list(task.expected_artifacts or []),
            "delivery_verification": out.get("delivery_json"),
            # WO-INCOMPLETE-DELIVERY-AUTORESCUE: surface rescue
            # lifecycle counters in the summary view too so the
            # orchestrator can pattern-match on
            # ``incomplete_delivery`` without parsing prose.
            "rescue_count": task.rescue_count,
            "max_rescues": task.max_rescues,
        }

    # 3) No persisted record. Deterministic 404 — do NOT call the
    # upstream Hermes adapter (that would launch a network call
    # and could 502 on a stale run_id, which the work-order
    # explicitly forbids: "No new run creation, no agent
    # execution, no Telegram, no service-side mutation"). The
    # previous implementation fell through to ``adapter.poll``
    # here; this restore removes that fall-through so the summary
    # endpoint is pure-read for unknown ids, matching the full
    # GET /runs/{run_id} contract.
    raise HTTPException(
        status_code=404,
        detail={
            "code": "unknown_run_id",
            "message": (
                f"run_id {run_id!r} not found in executor_runs or tasks"
            ),
            "run_id": run_id,
        },
    )


def _resolve_stop_adapter(
    run_id: str,
) -> Tuple[Optional[Any], Optional[Dict[str, Any]], Optional[str], bool]:
    """Resolve the cancel target for a run without branching on caller.

    The orchestrator must not need to know which executor is behind a
    run after creation. This helper reads the persisted
    ``executor_runs`` row (or the ``tasks`` table fallback for
    legacy dispatcher-tracked runs) and returns the adapter to
    delegate ``cancel`` to. The decision is purely read-side.

    Returns ``(adapter, persisted_envelope, selected_executor, already_terminal)``:

    * ``adapter`` — the registry adapter to call ``adapter.cancel(run_id)`` on.
    * ``persisted_envelope`` — the ``executor_runs`` row dict, or ``None``
      when the run is unknown or only present in the dispatcher tasks
      table (legacy fallback). Used by the caller to surface the
      canonical envelope fields (``status``/``routing``/etc.) in the
      404 vs already-terminal vs cancel-ok branches.
    * ``selected_executor`` — the persisted executor name
      (``"claude-code-cli"`` / ``"hermes"``) or ``None`` when unknown.
    * ``already_terminal`` — ``True`` if the persisted row is in a
      terminal state (caller should short-circuit cancel and return a
      deterministic envelope instead of issuing an upstream cancel
      that would no-op anyway).

    Never raises; returns ``(None, None, None, False)`` when no
    persisted record can be found — the caller is responsible for
    the 404.
    """
    from dispatcher.db import get_conn
    from dispatcher.executor_runs import get_run as _get_executor_run

    # 1) executor_runs — the canonical source for any
    # POST /runs/executor dispatch (claude-code-cli or hermes).
    try:
        conn = get_conn()
        persisted = _get_executor_run(conn, run_id)
    except Exception:  # pragma: no cover - defensive
        persisted = None
    if persisted is not None:
        selected = persisted.get("selected_executor") or "hermes"
        status = persisted.get("status") or "unknown"
        terminal = status in {"completed", "failed", "timeout", "cancelled"}
        if terminal:
            return None, persisted, selected, True
        # Map the persisted executor to the registry adapter name.
        # ``executor_runs`` stores the canonical ``selected_executor``
        # (``"claude-code-cli"`` or ``"hermes"``); the registry uses
        # the same keys (see ``aee.adapters.bootstrap_defaults``).
        from aee.core.registry import adapter_registry

        adapter = None
        try:
            adapter = adapter_registry.get(selected)
        except Exception:
            adapter = None
        # Fallback: if the named adapter is not registered (e.g.
        # tests stub only Hermes), the caller will get a deterministic
        # 503 below. We never silently downgrade to Hermes.
        return adapter, persisted, selected, False

    # 2) Dispatcher tasks (legacy POST /runs runs). The task row
    # carries the original ``adapter_name``; fall back to ``"hermes"``
    # when the column is absent (pre-AEE-2 rows).
    manager = TaskManager()
    task = manager.find_by_hermes_run_id(run_id)
    if task is not None:
        status = task.status
        terminal = status in {"completed", "failed", "cancelled"}
        if terminal:
            return None, None, None, True
        # Read the adapter name from the task row when present;
        # default to ``"hermes"`` for pre-AEE-2 rows.
        selected = getattr(task, "adapter_name", None) or "hermes"
        from aee.core.registry import adapter_registry

        adapter = None
        try:
            adapter = adapter_registry.get(selected)
        except Exception:
            adapter = None
        return adapter, None, selected, False

    return None, None, None, False


async def _stop_run_executor_neutral(
    run_id: str,
) -> Dict[str, Any]:
    """Executor-neutral cancellation core.

    Routes the cancel call to the adapter named in the persisted
    ``selected_executor`` (Hermes or Claude Code CLI). The caller
    never has to branch on which executor dispatched the run.

    Semantics (work-order §3.2 / §E):
      * Unknown run            -> deterministic 404 JSON
      * Already-terminal run   -> 200 + ``cancelled: False`` envelope
                                   (no upstream call)
      * Hermes active run      -> delegate to Hermes adapter
      * Claude Code active run -> delegate to Claude Code adapter
      * Adapter missing        -> 503 ``executor_unavailable``
      * Adapter raises         -> 502 with the underlying message

    Also best-effort cancels the dispatcher task (legacy runs) so
    the watcher's reconciliation can short-circuit on the next poll.
    """
    from aee.adapters.base import RuntimeError as AdapterRuntimeError

    # Validate the run_id shape the same way the other /runs routes do
    # so unknown / malformed ids return a deterministic 400 instead
    # of falling through to a 404.
    if _malformed_run_id(run_id):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "malformed_run_id",
                "message": (
                    f"run_id {run_id!r} is not a valid run identifier; "
                    f"expected a non-empty token of printable, non-slash chars"
                ),
            },
        )

    adapter, persisted, selected_executor, already_terminal = _resolve_stop_adapter(run_id)

    if persisted is None and selected_executor is None:
        # Legacy dispatcher task — still mark the task row cancelled
        # best-effort so the watcher short-circuits.
        manager = TaskManager()
        task = manager.find_by_hermes_run_id(run_id)
        if task is not None and task.status in {"queued", "running", "waiting"}:
            try:
                manager.cancel(task.task_id)
            except IllegalTransition:
                pass
        # No persisted executor_runs row and no dispatcher task row
        # either — deterministic 404.
        if task is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "unknown_run_id",
                    "message": (
                        f"run_id {run_id!r} not found in executor_runs or tasks"
                    ),
                    "run_id": run_id,
                },
            )

    if already_terminal:
        return {
            "run_id": run_id,
            "cancelled": False,
            "status": (persisted or {}).get("status", "terminal"),
            "selected_executor": selected_executor,
            "source": (persisted or {}).get("source", "dispatcher_tasks"),
            "already_terminal": True,
        }

    if adapter is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "executor_unavailable",
                "message": (
                    f"no adapter registered for selected_executor={selected_executor!r}; "
                    f"the run was dispatched via this executor but the adapter "
                    f"is not currently loaded"
                ),
                "selected_executor": selected_executor,
            },
        )

    try:
        cancel_result = await adapter.cancel(run_id)
    except AdapterRuntimeError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Upstream {selected_executor} error: "
                f"{type(exc).__name__}: {exc}"
            ),
        ) from exc

    # Best-effort dispatcher task cancel for legacy /runs rows that
    # happened to land in the executor_runs store too.
    manager = TaskManager()
    task = manager.find_by_hermes_run_id(run_id)
    if task is not None and task.status in {"queued", "running", "waiting"}:
        try:
            manager.cancel(task.task_id)
        except IllegalTransition:
            pass

    return {
        "run_id": run_id,
        "cancelled": cancel_result.cancelled,
        "status": cancel_result.reason or "stop_requested",
        "selected_executor": selected_executor,
        "source": (persisted or {}).get("source", "executor_runs"),
    }


@app.post("/runs/{run_id}/stop")
async def stop_run(
    run_id: str,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Executor-neutral cancellation.

    Routes the cancel call to the adapter named in the persisted
    ``selected_executor`` (Hermes or Claude Code CLI). The caller
    never has to branch on which executor dispatched the run.

    See :func:`_stop_run_executor_neutral` for the full semantics
    matrix (unknown run, already-terminal, Hermes active, Claude
    Code active, adapter missing).
    """
    require_auth(authorization)
    return await _stop_run_executor_neutral(run_id)


@app.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Alias of ``POST /runs/{run_id}/stop``.

    The new contract surface prefers ``/cancel`` (semantic verb);
    ``/stop`` is preserved verbatim for backward compatibility with
    existing ChatGPT Action configurations and shell scripts.
    Both endpoints route through the same executor-neutral core.
    """
    require_auth(authorization)
    return await _stop_run_executor_neutral(run_id)


# ---------------------------------------------------------------------------
# Routes — /stats/usage (Phase 2 P4)
# ---------------------------------------------------------------------------


@app.get("/stats/usage")
async def stats_usage(
    authorization: Optional[str] = Header(None),
    period: str = Query("today", pattern="^(today|7d|30d|all)$"),
    task: Optional[str] = Query(None, description="Single task_id to roll up."),
) -> Dict[str, Any]:
    require_auth(authorization)
    from dispatcher.usage import aggregate
    return aggregate(period=period, task_id=task)


# ---------------------------------------------------------------------------
# Routes — /tasks (new in Phase 1)
# ---------------------------------------------------------------------------


@app.get("/tasks")
async def list_tasks(
    authorization: Optional[str] = Header(None),
    status: Optional[str] = Query(None, description="Filter by status"),
    type: Optional[str] = Query(None, description="Filter by type"),
    limit: int = Query(50, ge=1, le=500),
) -> Dict[str, Any]:
    require_auth(authorization)
    manager = TaskManager()
    tasks = manager.list(status=status, type=type, limit=limit)
    return {
        "count": len(tasks),
        "limit": limit,
        "filters": {"status": status, "type": type},
        "tasks": [t.to_dict() for t in tasks],
    }


@app.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    require_auth(authorization)
    manager = TaskManager()
    try:
        t = manager.get_or_raise(task_id)
    except TaskNotFound:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    return t.to_dict()


@app.get("/tasks/{task_id}/progress")
async def get_task_progress(
    task_id: str,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Lightweight endpoint for ChatGPT to poll mid-run (5/10/25/40/60/80/95/100)."""
    require_auth(authorization)
    manager = TaskManager()
    try:
        t = manager.get_or_raise(task_id)
    except TaskNotFound:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    # Last few events for context
    events = manager.events(task_id, limit=5)
    return {
        "task_id": task_id,
        "status": t.status,
        "progress_pct": t.progress_pct,
        "progress_step": t.progress_step,
        "started_at": t.started_at,
        "finished_at": t.finished_at,
        "duration_sec": t.duration_sec,
        "recent_events": [e.to_dict() for e in events],
        "poll_url": f"/tasks/{task_id}/progress",
    }


@app.get("/tasks/{task_id}/logs")
async def get_task_logs(
    task_id: str,
    authorization: Optional[str] = Header(None),
    tail: int = Query(200, ge=1, le=2000, description="Last N lines of the log file."),
) -> Dict[str, Any]:
    require_auth(authorization)
    manager = TaskManager()
    try:
        manager.get_or_raise(task_id)
    except TaskNotFound:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    from dispatcher.manager import _log_path
    p = _log_path(task_id)
    if not p.exists():
        return {"task_id": task_id, "lines": [], "log_path": str(p), "exists": False}
    # Cheap tail: read all then slice (logs are <1MB for 900s runs).
    try:
        all_lines = p.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read log: {exc}")
    lines = all_lines[-tail:] if len(all_lines) > tail else all_lines
    return {
        "task_id": task_id,
        "log_path": str(p),
        "exists": True,
        "total_lines": len(all_lines),
        "returned_lines": len(lines),
        "lines": lines,
    }


@app.get("/tasks/{task_id}/result")
async def get_task_result(
    task_id: str,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Get the final result metadata + output + report path."""
    require_auth(authorization)
    manager = TaskManager()
    try:
        t = manager.get_or_raise(task_id)
    except TaskNotFound:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    out = manager.get_output(task_id) or {}
    return {
        "task_id": task_id,
        "status": t.status,
        "result_path": t.result_path,
        "duration_sec": t.duration_sec,
        "started_at": t.started_at,
        "finished_at": t.finished_at,
        "error": t.error_message,
        "warning_count": t.warning_count,
        "retry_count": t.retry_count,
        "prompt_version": t.prompt_version,
        "model_name": t.model_name,
        "git_commit": t.git_commit,
        "git_branch": t.git_branch,
        "output_text": out.get("output_text"),
        "usage": out.get("usage"),
    }


@app.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    require_auth(authorization)
    manager = TaskManager()
    try:
        t = manager.get_or_raise(task_id)
    except TaskNotFound:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    # Also cancel upstream if we have an external run id. AEE-2:
    # resolve the adapter from `task.adapter_name` instead of
    # hardcoding the Hermes URL.
    external_id = t.external_run_id or t.hermes_run_id
    if external_id:
        try:
            from aee.core.registry import adapter_registry
            from aee.adapters.base import RuntimeError as AdapterRuntimeError
            adapter = adapter_registry.get(t.adapter_name or "hermes")
            await adapter.cancel(external_id)
        except Exception:  # noqa: BLE001
            pass  # best-effort
    try:
        t = manager.cancel(task_id)
    except IllegalTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return t.to_dict()


@app.post("/tasks/{task_id}/rerun")
async def rerun_task(
    task_id: str,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Create a new task cloned from `task_id` and dispatch it to Hermes.

    The original task is left in its current status (failed/cancelled); the
    new task inherits title, type, input, session, prompt_version, etc.
    """
    require_auth(authorization)
    manager = TaskManager()
    try:
        old = manager.get_or_raise(task_id)
    except TaskNotFound:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    if old.status not in {"failed", "cancelled", "completed"}:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot rerun task in status {old.status!r}; only failed/cancelled/completed.",
        )

    # Clone via manager.retry() so the new task gets retry_count bumped and
    # the audit trail links back to the original.
    new_task = manager.retry(task_id)
    # Now dispatch the new task via the runtime adapter (AEE-2
    # seam) — same as /runs. HermesAdapter is the default; the
    # task's adapter_name is honoured.
    body_input = old.input_text or ""
    session_id = old.session_id or DEFAULT_SESSION_ID
    check_session_allowed(session_id)
    from aee.core.job_models import Job as AEEJob
    from aee.core.registry import adapter_registry
    from aee.adapters.base import RuntimeError as AdapterRuntimeError
    job = AEEJob(
        title=new_task.title,
        type=new_task.type,
        mode=new_task.mode or "normal",
        priority=new_task.priority,
        input=body_input,
        session_id=session_id,
        client_source="rerun",
        model_name=new_task.model_name,
        runtime_type=new_task.runtime_type or "hermes",
        adapter_name=new_task.adapter_name or "hermes",
    )
    adapter = adapter_registry.get(job.adapter_name)
    try:
        submit_result = await adapter.submit(job)
    except AdapterRuntimeError as exc:
        manager.fail(new_task.task_id, f"rerun upstream error: {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=502,
            detail=f"Upstream {adapter.name} error: {type(exc).__name__}: {exc}",
        ) from exc
    run_id = submit_result.external_run_id
    if not run_id:
        manager.fail(new_task.task_id, f"rerun returned no run_id: {submit_result.to_dict()}")
        raise HTTPException(status_code=502, detail=f"Upstream returned no run_id")
    manager.start(new_task.task_id, run_id)
    # AEE-2: also stamp external_run_id + adapter metadata on the
    # new task so the AEE-1 lookup path works.
    from dispatcher import db as _db
    with _db.transaction() as conn2:
        conn2.execute(
            "UPDATE tasks SET external_run_id = ?, runtime_type = ?, "
            "adapter_name = ? WHERE task_id = ?",
            (run_id, job.runtime_type, job.adapter_name, new_task.task_id),
        )
    return {
        "original_task_id": task_id,
        "new_task_id": new_task.task_id,
        "new_run_id": run_id,
        "new_task": new_task.to_dict(),
        "poll_url": f"/tasks/{new_task.task_id}",
    }


# ---------------------------------------------------------------------------
# AEE-2: mount the AEE API router (`/jobs`, `/workers`). The legacy
# `/runs` and `/tasks` endpoints above are unchanged; AEE-2 adds the
# new routes alongside them. AEE-5 will fold `/runs` into a thin
# alias over `/jobs` (compatibility layer).
# ---------------------------------------------------------------------------

try:
    from aee.api import api_router as aee_api_router  # type: ignore
    app.include_router(aee_api_router)
except Exception as exc:  # noqa: BLE001
    # Don't fail app startup if AEE module is missing (e.g. during
    # AEE-0 only rollouts where aee/ doesn't exist yet).
    import sys
    print(f"[aee] router not mounted: {type(exc).__name__}: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# AEE-1: bootstrap the default adapter registry (HermesAdapter) so
# `/jobs/claim` and `adapter_registry.get(adapter_name)` work in
# the same process. Safe to call multiple times; idempotent.
# ---------------------------------------------------------------------------

try:
    from aee.core.registry import bootstrap_defaults  # type: ignore
    bootstrap_defaults(force=False)
except Exception as exc:  # noqa: BLE001
    import sys
    print(f"[aee] bootstrap_defaults skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
