"""
Hermes Runtime Bridge — Phase 1 (Dispatcher-enabled)
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
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

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
    try:
        yield
    finally:
        await watcher.stop()


app = FastAPI(
    title="Hermes Runtime Bridge",
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


@app.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Poll a run's current state + final output (if completed).

    If a dispatcher task is bound to this run_id, return the merged view
    (dispatcher progress + upstream raw). Otherwise pass through to Hermes.
    """
    require_auth(authorization)

    # Try the dispatcher first.
    manager = TaskManager()
    task = manager.find_by_hermes_run_id(run_id)
    if task is not None:
        out = manager.get_output(task.task_id) or {}
        return {
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
        }

    # No dispatcher record — fall back to adapter-driven pass-through.
    # AEE-2: use the adapter registry instead of hardcoded
    # `httpx.AsyncClient.get(... /v1/runs/{id} ...)`. The legacy
    # HermesAdapter implements the same endpoint; the response
    # shape is whatever Hermes itself returns.
    from aee.core.registry import adapter_registry
    from aee.adapters.base import RuntimeError as AdapterRuntimeError
    try:
        adapter = adapter_registry.get("hermes")
        poll_result = await adapter.poll(run_id)
    except AdapterRuntimeError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Upstream Hermes error: {type(exc).__name__}: {exc}",
        ) from exc
    return {
        "run_id": poll_result.external_run_id,
        "status": poll_result.status,
        "is_terminal": poll_result.is_terminal,
        "output": poll_result.output,
        "error": poll_result.error,
        "raw": dict(poll_result.raw) if poll_result.raw is not None else None,
    }


@app.get("/runs/{run_id}/summary")
async def get_run_summary(
    run_id: str,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Bridge-curated summary, friendly to ChatGPT."""
    require_auth(authorization)

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
            "current_hint": hint,
        }

    # Fall through to upstream via the runtime adapter.
    from aee.core.registry import adapter_registry
    from aee.adapters.base import RuntimeError as AdapterRuntimeError
    try:
        adapter = adapter_registry.get("hermes")
        poll_result = await adapter.poll(run_id)
    except AdapterRuntimeError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Upstream Hermes error: {type(exc).__name__}: {exc}",
        ) from exc
    status = poll_result.status or "unknown"
    output = poll_result.output
    last_event = (poll_result.raw or {}).get("last_event") if isinstance(poll_result.raw, dict) else None
    if status in {"completed", "failed", "cancelled"}:
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
        "status": status,
        "last_event": last_event,
        "output_preview": preview,
        "current_hint": hint,
    }


@app.post("/runs/{run_id}/stop")
async def stop_run(
    run_id: str,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    require_auth(authorization)
    # Mark dispatcher task cancelled (best-effort).
    manager = TaskManager()
    task = manager.find_by_hermes_run_id(run_id)
    if task is not None and task.status in {"queued", "running", "waiting"}:
        try:
            manager.cancel(task.task_id)
        except IllegalTransition:
            pass
    from aee.core.registry import adapter_registry
    from aee.adapters.base import RuntimeError as AdapterRuntimeError
    try:
        adapter = adapter_registry.get("hermes")
        cancel_result = await adapter.cancel(run_id)
    except AdapterRuntimeError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Upstream Hermes error: {type(exc).__name__}: {exc}",
        ) from exc
    return {
        "run_id": run_id,
        "cancelled": cancel_result.cancelled,
        "status": cancel_result.reason or "stop_requested",
    }


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
