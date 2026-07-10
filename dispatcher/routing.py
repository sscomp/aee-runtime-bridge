"""Client-source identification + GPT -> MiniMax-M3 routing policy.

Layer 1 (pure functions, no I/O, no global state):
  * identify_source(presented_key, sources) -> Optional[str]
  * resolve_model_for_source(source, caller_model, policy) -> ResolveResult

Why pure functions?
  * They are easy to unit-test (no env reads, no FastAPI, no DB).
  * The same logic is exercised by the bridge (`app.py`) AND by the
    dispatcher's safety path if we ever need to re-resolve a model mid-task.
  * A regression in routing is much easier to spot in a 30-line module
    than in a 700-line app.py.

Why a dedicated module instead of inlining into `app.py`?
  * `app.py` already mixes auth, safety, OpenAPI, upstream HTTPX calls,
    and task lifecycle. Adding a fourth concern (model routing) would
    make it impossible to write a focused unit test without spinning
    up FastAPI.
  * This file is loaded only when the bridge imports it; no I/O at
    import time, no module-level `os.getenv()` calls.

Design contract (what the rest of the bridge assumes):
  * `identify_source` MUST be a pure function of its arguments. It may
    not read env, log, raise HTTPException, or touch the dispatcher DB.
  * `resolve_model_for_source` MUST return a ResolveResult (not raise)
    even for unconfigured / missing-key scenarios — the caller is
    responsible for translating the result into an HTTPException or
    audit log entry. This keeps the policy testable in isolation.

The two functions together implement the "GPT request -> MiniMax-M3,
other sources -> caller's choice or default" rule requested in
`/home/ubuntu/Abacus/MiniMax-M3-routellm.md`. See
`/home/ubuntu/Abacus/Hermes_GPT_MiniMax_Routing.md` for the operator-
facing walkthrough.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolveResult:
    """Outcome of `resolve_model_for_source`.

    Fields:
      * model_id         — the model name to put on the upstream call
                           (always non-empty; callers must reject empty).
      * was_forced       — True iff we overrode the caller's `model_name`
                           because their source has a routing policy.
      * reason           — short human-readable reason, used for
                           dispatcher task_logs and OpenAPI responses.
                           NEVER contains secrets.
      * source           — echo of the input source label (e.g. "gpt",
                           "cli", "claude", "cursor", "mcp", "unknown").
      * key_present      — True iff the source's required env key
                           (e.g. MINIMAX_API_KEY) was set when this
                           function was called. Lets the caller
                           distinguish "policy says GPT -> MiniMax" from
                           "policy says GPT -> MiniMax, but no key set".
    """
    model_id: str
    was_forced: bool
    reason: str
    source: str
    key_present: bool


@dataclass(frozen=True)
class RoutingPolicy:
    """Configuration injected from app.py at startup.

    The bridge constructs this once and passes it to
    `resolve_model_for_source` on every request. Tests construct it
    directly with synthetic values.

    Fields:
      * default_model   — what non-GPT sources get when they pass
                          `caller_model=None`. CLI / CI default path.
      * gpt_model       — the MiniMax-M3 full model id (per docs:
                          `MiniMaxAI/MiniMax-M3` — short names are
                          rejected with HTTP 400 by the upstream).
      * allow_caller_override_for_gpt
                        — if True, a GPT caller's explicit
                          `caller_model` is honoured instead of
                          `gpt_model`. Default False: the whole point
                          of the routing rule is that GPT ALWAYS uses
                          MiniMax-M3.
      * minimax_key     — string value of the `MINIMAX_API_KEY` env
                          var at construction time. Used to set
                          `key_present` on the result so the caller
                          can surface "key not configured" as a 503.
                          MUST NOT be logged.
    """
    default_model: str
    gpt_model: str
    allow_caller_override_for_gpt: bool
    minimax_key: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_source_map(env: Dict[str, str]) -> Dict[str, str]:
    """Build a `key -> source` map from a flat env dict.

    The bridge stores five env vars (BRIDGE_API_KEY, GPT_BRIDGE_API_KEY,
    CLAUDE_BRIDGE_API_KEY, CURSOR_BRIDGE_API_KEY, MCP_BRIDGE_API_KEY).
    We invert the env into a lookup table so `identify_source` is O(1).

    The source label is *what the key authenticates as*, not *where the
    HTTP call came from* — but in practice we don't accept any other
    form of authentication on the bridge, so the two are equivalent.

    Empty env values are skipped (a key set to "" means "this channel
    is disabled", per `.env.example` and the multi-key auth design
    from 2026-07-07).

    Args:
      env: a dict like `os.environ` — must contain at least
           BRIDGE_API_KEY, GPT_BRIDGE_API_KEY, CLAUDE_BRIDGE_API_KEY,
           CURSOR_BRIDGE_API_KEY, MCP_BRIDGE_API_KEY (missing keys
           become empty string and are skipped).
    Returns:
      a `dict` of `presented_key -> source_label`. Multiple env vars
      set to the same value (a misconfiguration!) collapse to one
      entry whose label is the *first* in iteration order — but the
      caller can also detect this via `len(set(env_values)) !=
      len(non_empty)` before calling `build_source_map`.
    """
    pairs = [
        ("BRIDGE_API_KEY", "cli"),
        ("GPT_BRIDGE_API_KEY", "gpt"),
        ("CLAUDE_BRIDGE_API_KEY", "claude"),
        ("CURSOR_BRIDGE_API_KEY", "cursor"),
        ("MCP_BRIDGE_API_KEY", "mcp"),
    ]
    out: Dict[str, str] = {}
    for var, label in pairs:
        val = (env.get(var) or "").strip()
        if not val:
            continue
        out[val] = label
    return out


def identify_source(presented_key: str, sources: Dict[str, str]) -> str:
    """Return the source label for `presented_key`, or "unknown".

    Pure function: does not raise, does not read env. Tests can pass
    any dict they like.
    """
    if not presented_key:
        return "unknown"
    return sources.get(presented_key, "unknown")


def resolve_model_for_source(
    *,
    source: str,
    caller_model: Optional[str],
    policy: RoutingPolicy,
) -> ResolveResult:
    """Decide which model to put on the upstream call.

    Rules:
      * source == "gpt"  -> force `policy.gpt_model` (MiniMax-M3) unless
                            `policy.allow_caller_override_for_gpt` is
                            True AND the caller passed a non-empty
                            `caller_model`. In that case the caller's
                            choice wins, but `was_forced=False` so the
                            audit log shows the override.
      * source != "gpt"  -> use `caller_model` if set, else
                            `policy.default_model`. Never force.
      * source == "unknown" (key not in map) -> same as non-GPT
                            path, because `require_auth` should have
                            rejected the call already. We still
                            produce a sensible result here so the
                            function is total.

    `key_present` reflects whether `policy.minimax_key` was set when
    the policy was constructed. The value is independent of `source`
    — it always reports the policy state. The bridge SHOULD only
    consult `key_present` when `was_forced=True` (because that is the
    case where a missing key would cause a 401 from the upstream
    MiniMax-M3 service). For non-GPT sources the field is informational
    and the bridge should ignore it.

    This function NEVER raises. It always returns a `ResolveResult`
    with a non-empty `model_id`. The bridge is responsible for
    checking `was_forced and not key_present` and turning that into
    a 503 with a clear error message.
    """
    key_present = bool(policy.minimax_key and policy.minimax_key.strip())

    if source == "gpt":
        if caller_model and policy.allow_caller_override_for_gpt:
            return ResolveResult(
                model_id=caller_model,
                was_forced=False,
                reason=(
                    f"gpt source with caller override allowed "
                    f"(policy.allow_caller_override_for_gpt=True); "
                    f"using caller_model={caller_model!r}"
                ),
                source=source,
                key_present=key_present,
            )
        return ResolveResult(
            model_id=policy.gpt_model,
            was_forced=True,
            reason=(
                f"gpt source routed to MiniMax-M3 per policy "
                f"(caller_model={caller_model!r} ignored)"
            ),
            source=source,
            key_present=key_present,
        )

    # Non-GPT path
    chosen = caller_model or policy.default_model
    return ResolveResult(
        model_id=chosen,
        was_forced=False,
        reason=(
            f"source={source!r} uses caller's choice "
            f"(caller_model={caller_model!r}, "
            f"default_model={policy.default_model!r})"
        ),
        source=source,
        key_present=key_present,
    )
