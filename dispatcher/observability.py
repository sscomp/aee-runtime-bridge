"""Persisted run observability contract (TASK-AEE-RUN-OBSERVABILITY-P1).

This module owns the **canonical observability field contract** for
``GET /runs`` and ``GET /runs/{run_id}`` response envelopes. It is the
single source of truth for:

1. The field set every run envelope MUST expose.
2. The stall policy (when is a non-terminal run considered stalled?).
3. The bounded tail derivation for ``stdout_tail`` / ``stderr_tail``.
4. ETA policy: this implementation deliberately does NOT fabricate an
   ETA. There is no evidence-backed estimator in this iteration, so
   the canonical ``eta_seconds`` field is omitted. ``duration_seconds``
   and ``seconds_since_update`` are the only time-based fields, both
   derived from persisted timestamps — never extrapolated.

Design rules (work-order §1–§7)
--------------------------------
- Every field is derived from **persisted evidence** (the
  ``executor_runs`` row or the ``tasks`` row). No field is computed by
  polling an executor at read time, scanning the repo, or guessing.
- The stall policy is **deterministic** and configurable via the
  ``RUN_STALL_THRESHOLD_SECONDS`` environment variable (with a named
  constant fallback). Missing timestamps always produce a
  non-fabricated outcome: ``stalled=False`` for terminal runs and
  ``stalled=False`` with ``stalled_reason="missing_timestamp"`` for
  non-terminal runs (so a stalled-by-clock run is distinguishable from
  a stalled-by-missing-evidence run).
- ``stdout_tail`` / ``stderr_tail`` are bounded tail slices of the
  persisted ``stdout_summary`` / ``stderr_summary`` (executor_runs) or
  the dispatcher ``output_text`` (tasks fallback). They are **never**
  derived from scanning arbitrary repo files.
- Terminal runs are **never** stalled. The terminal status set is
  ``{completed, failed, timeout, cancelled}``.
- The contract is **backward-compatible**: a row that predates the
  observability migration (no ``last_heartbeat_at`` / ``current_step``
  / ``phase`` columns) is still readable; missing columns degrade to
  ``None`` / sensible defaults and ``stalled=False``.

Non-goals
---------
- No ETA estimation. A future P2 work order can add a deterministic
  rolling-average estimator backed by historical durations; until
  then, ETA is omitted rather than fabricated.
- No live executor polling at read time. ``GET /runs`` remains a pure
  read; ``GET /runs/{run_id}`` may keep its existing bounded Hermes
  reconciliation (work-order §4), and the observability fields are
  computed from the persisted post-reconciliation row.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional


# ---------------------------------------------------------------------------
# Canonical field set (work-order §1)
# ---------------------------------------------------------------------------
#
# These are the ONLY observability fields the GET /runs family exposes.
# Adding a field requires updating this list, the docstring, and the
# ``_derive_observability`` mapping logic. The fields are intentionally
# flat so a GPT caller can rely on a stable schema.

OBSERVABILITY_FIELDS = (
    "updated_at",            # ISO-8601 last row-mutation timestamp (persisted)
    "last_heartbeat_at",     # ISO-8601 last executor heartbeat (persisted, nullable)
    "current_step",          # short human-readable step label (persisted, nullable)
    "phase",                # coarse phase: queued|running|terminal|unknown (derived from status)
    "duration_seconds",     # wall-clock seconds from started_at→finished_at (persisted, nullable)
    "seconds_since_update",  # seconds between now and updated_at (derived, nullable when updated_at missing)
    "stdout_tail",           # bounded tail of persisted stdout summary (derived)
    "stderr_tail",           # bounded tail of persisted stderr summary (derived)
    "stalled",               # boolean (derived, deterministic)
    "stalled_reason",        # nullable string explaining why stalled (or why not)
)

# Coarse phase mapping. ``phase`` is a derived field so a GPT caller
# can bucket runs without re-implementing the terminal-status check.
# ``unknown`` is the only non-fabricated answer when ``status`` is
# missing or not in the vocabulary.
PHASE_QUEUED = "queued"
PHASE_RUNNING = "running"
PHASE_TERMINAL = "terminal"
PHASE_UNKNOWN = "unknown"

_TERMINAL_STATUSES = frozenset({"completed", "failed", "timeout", "cancelled"})
_QUEUED_STATUSES = frozenset({"queued", "pending", "started"})
_RUNNING_STATUSES = frozenset({"running", "waiting"})


# ---------------------------------------------------------------------------
# Stall policy (work-order §6)
# ---------------------------------------------------------------------------
#
# Non-terminal runs whose ``updated_at`` is older than the threshold
# are considered stalled. The threshold is configurable via the
# ``RUN_STALL_THRESHOLD_SECONDS`` environment variable (parsed as an
# integer; a malformed value falls back to the named constant with a
# warning printed to stderr — never an exception, never silent).
#
# Determinism contract:
#   - terminal runs are NEVER stalled (``stalled=False``, reason ``"terminal"``)
#   - non-terminal runs with no ``updated_at`` are NOT stalled
#     (``stalled=False``, reason ``"missing_timestamp"``) — we do not
#     fabricate a timestamp or a duration
#   - non-terminal runs with ``updated_at`` older than the threshold are
#     stalled (``stalled=True``, reason ``"no_update"``)
#   - non-terminal runs with ``updated_at`` newer than the threshold are
#     not stalled (``stalled=False``, reason ``"recent_update"``)
#
# The threshold is read **once per call** (not cached at import time)
# so a runtime env change (without restart) is honoured — useful for
# operators tuning the threshold in a running container.

DEFAULT_RUN_STALL_THRESHOLD_SECONDS = 600  # 10 minutes


def get_stall_threshold_seconds() -> int:
    """Return the configured stall threshold in seconds.

    Reads ``RUN_STALL_THRESHOLD_SECONDS`` from the environment on every
    call. A missing or malformed value falls back to
    ``DEFAULT_RUN_STALL_THRESHOLD_SECONDS`` (10 minutes). A malformed
    value also prints a warning to stderr — this is the only side
    effect, and it is intentional: operators should know their env var
    was ignored, but the read path must never raise.
    """
    raw = os.environ.get("RUN_STALL_THRESHOLD_SECONDS")
    if raw is None:
        return DEFAULT_RUN_STALL_THRESHOLD_SECONDS
    try:
        val = int(raw)
    except (ValueError, TypeError):
        # Malformed — fall back to default. Print to stderr so the
        # operator can see the env var was ignored; do NOT raise.
        import sys
        print(
            f"[observability] RUN_STALL_THRESHOLD_SECONDS={raw!r} is not an "
            f"integer; falling back to "
            f"{DEFAULT_RUN_STALL_THRESHOLD_SECONDS}s",
            file=sys.stderr,
        )
        return DEFAULT_RUN_STALL_THRESHOLD_SECONDS
    # Negative or zero thresholds are nonsensical; clamp to the
    # default rather than fabricating a 0-second "everything is
    # stalled" policy.
    if val <= 0:
        import sys
        print(
            f"[observability] RUN_STALL_THRESHOLD_SECONDS={val} is non-positive; "
            f"falling back to {DEFAULT_RUN_STALL_THRESHOLD_SECONDS}s",
            file=sys.stderr,
        )
        return DEFAULT_RUN_STALL_THRESHOLD_SECONDS
    return val


# ---------------------------------------------------------------------------
# Tail truncation (work-order §7)
# ---------------------------------------------------------------------------
#
# ``stdout_tail`` / ``stderr_tail`` are bounded tail slices of the
# persisted summaries. The bound is a named constant (not an env var)
# so a GPT caller can rely on a stable maximum length. The default
# (4096 bytes) is large enough to capture a stack trace while small
# enough to keep the envelope cheap.

TAIL_MAX_BYTES = 4096


def _bounded_tail(text: Optional[str], max_bytes: int = TAIL_MAX_BYTES) -> Optional[str]:
    """Return a bounded UTF-8 tail slice of ``text``.

    Returns ``None`` when ``text`` is ``None`` or empty — a caller can
    distinguish "no stdout captured" (``None``) from "empty stdout"
    (``""``). The slice is taken on UTF-8 bytes and re-decoded with
    ``errors="replace"`` so a multi-byte char is never split.
    """
    if text is None:
        return None
    if text == "":
        return ""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    # Tail slice — keep the LAST max_bytes bytes so a stack trace's
    # root cause is preserved.
    sliced = encoded[-max_bytes:]
    return sliced.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Phase derivation (pure)
# ---------------------------------------------------------------------------

def derive_phase(status: Optional[str]) -> str:
    """Derive the coarse phase from a canonical run status.

    Returns one of ``PHASE_QUEUED`` / ``PHASE_RUNNING`` /
    ``PHASE_TERMINAL`` / ``PHASE_UNKNOWN``. ``None`` or an unknown
    status yields ``PHASE_UNKNOWN`` — never an exception, never a
    fabricated bucket.
    """
    if status is None:
        return PHASE_UNKNOWN
    if status in _TERMINAL_STATUSES:
        return PHASE_TERMINAL
    if status in _QUEUED_STATUSES:
        return PHASE_QUEUED
    if status in _RUNNING_STATUSES:
        return PHASE_RUNNING
    return PHASE_UNKNOWN


# ---------------------------------------------------------------------------
# Time helpers (pure)
# ---------------------------------------------------------------------------

def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 string into a tz-aware UTC ``datetime``.

    Returns ``None`` for ``None`` / empty / unparseable input — the
    caller is responsible for the missing-timestamp policy.
    """
    if value is None or value == "":
        return None
    try:
        # ``fromisoformat`` in 3.11 accepts the trailing ``Z``.
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _seconds_between(later: datetime, earlier: datetime) -> Optional[float]:
    """Return ``later - earlier`` in seconds (float), or ``None`` if either is None."""
    if later is None or earlier is None:
        return None
    return (later - earlier).total_seconds()


# ---------------------------------------------------------------------------
# Stall decision (pure)
# ---------------------------------------------------------------------------

def evaluate_stall(
    *,
    status: Optional[str],
    updated_at: Optional[str],
    now: Optional[datetime] = None,
    threshold_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """Evaluate the stall policy for a single run row.

    Returns ``{"stalled": bool, "stalled_reason": Optional[str]}``.

    Determinism contract (see module docstring §6):

      * terminal runs: ``stalled=False``, reason ``"terminal"``
      * non-terminal + missing ``updated_at``: ``stalled=False``,
        reason ``"missing_timestamp"`` (we do not fabricate a duration)
      * non-terminal + ``updated_at`` older than threshold:
        ``stalled=True``, reason ``"no_update"``
      * non-terminal + ``updated_at`` newer than threshold:
        ``stalled=False``, reason ``"recent_update"``
      * unknown status is treated as non-terminal for the stall
        decision (a status we don't recognise should not be silently
        trusted as terminal).
    """
    if threshold_seconds is None:
        threshold_seconds = get_stall_threshold_seconds()

    # Terminal runs are never stalled.
    if status in _TERMINAL_STATUSES:
        return {"stalled": False, "stalled_reason": "terminal"}

    updated_dt = _parse_iso(updated_at)
    if updated_dt is None:
        # Non-terminal + missing timestamp: deterministic non-fabricated
        # outcome. We do NOT pretend to know how stale the run is.
        return {"stalled": False, "stalled_reason": "missing_timestamp"}

    now_dt = now if now is not None else _now_utc()
    age_seconds = _seconds_between(now_dt, updated_dt)
    if age_seconds is None:
        return {"stalled": False, "stalled_reason": "missing_timestamp"}

    if age_seconds > float(threshold_seconds):
        return {"stalled": True, "stalled_reason": "no_update"}
    return {"stalled": False, "stalled_reason": "recent_update"}


# ---------------------------------------------------------------------------
# Envelope derivation (the single entry point for GET /runs)
# ---------------------------------------------------------------------------

def derive_observability(
    row: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
    threshold_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """Derive the canonical observability envelope from a run row.

    ``row`` is a mapping with at least ``status``. The following keys
    are consumed if present (all optional — backward-compatibility):

      * ``status``              — canonical run status (REQUIRED)
      * ``updated_at``          — ISO-8601 last mutation (persisted)
      * ``last_heartbeat_at``    — ISO-8601 last executor heartbeat (persisted)
      * ``current_step``         — short step label (persisted)
      * ``started_at``           — ISO-8601 start time (persisted, for duration)
      * ``finished_at``          — ISO-8601 finish time (persisted, for duration)
      * ``stdout_summary``       — persisted stdout blob (executor_runs)
      * ``stderr_summary``       — persisted stderr blob (executor_runs)
      * ``stdout_tail``          — if the caller already pre-computed a tail
        (executor_runs can store one directly; this takes precedence over
        ``stdout_summary``)
      * ``stderr_tail``          — see ``stdout_tail``
      * ``output_text``          — persisted dispatcher output (tasks fallback)

    Returns a flat dict with EXACTLY the keys in ``OBSERVABILITY_FIELDS``.
    A missing source key degrades to ``None`` (or the documented
    default) — never an exception.
    """
    status = row.get("status")

    # ----- duration_seconds -----
    started_dt = _parse_iso(row.get("started_at"))
    finished_dt = _parse_iso(row.get("finished_at"))
    duration_seconds = _seconds_between(finished_dt, started_dt) if (started_dt and finished_dt) else None
    # Some persisted rows carry a precomputed duration_sec / duration_seconds
    # (the tasks table stores ``duration_sec`` as a float). Prefer the
    # persisted value when the timestamps are unavailable — but only if
    # it is non-None and non-negative (a negative persisted duration is
    # a data bug, not evidence; do not propagate it).
    if duration_seconds is None:
        persisted_duration = row.get("duration_sec")
        if persisted_duration is None:
            persisted_duration = row.get("duration_seconds")
        if persisted_duration is not None:
            try:
                d = float(persisted_duration)
                if d >= 0:
                    duration_seconds = d
            except (ValueError, TypeError):
                pass

    # ----- seconds_since_update -----
    # Quantised to integer seconds so two reads issued within the
    # same second return byte-for-byte identical values. This
    # preserves the "GET /runs is a pure read — same input, same
    # output" contract (work-order §3) while still giving operators
    # a useful "how stale is this row?" signal. A sub-second drift
    # is not actionable for a human operator; the integer floor is
    # the right precision for a staleness metric.
    updated_dt = _parse_iso(row.get("updated_at"))
    now_dt = now if now is not None else _now_utc()
    seconds_since_update: Optional[int] = None
    if updated_dt is not None:
        delta = _seconds_between(now_dt, updated_dt)
        if delta is not None:
            # Floor to non-negative int. A negative delta (clock
            # skew — updated_at in the future) floors to 0 rather
            # than fabricating a negative age; operators see "just
            # updated" which is the safest non-fabricated answer.
            seconds_since_update = max(0, int(delta))

    # ----- stdout_tail / stderr_tail -----
    # Precedence: explicit ``stdout_tail`` field (if the row already
    # stores one) → ``stdout_summary`` → ``output_text`` (tasks
    # fallback) → None.
    stdout_tail = row.get("stdout_tail")
    if stdout_tail is None:
        stdout_tail = row.get("stdout_summary")
    if stdout_tail is None:
        # tasks-table fallback: dispatcher stores the agent's final
        # text in ``output_text``. Use it as the stdout source only
        # when the row did not carry a dedicated stdout field.
        stdout_tail = row.get("output_text")
    stdout_tail = _bounded_tail(stdout_tail) if stdout_tail is not None else None

    stderr_tail = row.get("stderr_tail")
    if stderr_tail is None:
        stderr_tail = row.get("stderr_summary")
    stderr_tail = _bounded_tail(stderr_tail) if stderr_tail is not None else None

    # ----- phase -----
    phase = derive_phase(status)

    # ----- stalled -----
    stall = evaluate_stall(
        status=status,
        updated_at=row.get("updated_at"),
        now=now_dt,
        threshold_seconds=threshold_seconds,
    )

    return {
        "updated_at": row.get("updated_at"),
        "last_heartbeat_at": row.get("last_heartbeat_at"),
        "current_step": row.get("current_step"),
        "phase": phase,
        "duration_seconds": duration_seconds,
        "seconds_since_update": seconds_since_update,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "stalled": stall["stalled"],
        "stalled_reason": stall["stalled_reason"],
    }


__all__ = [
    "OBSERVABILITY_FIELDS",
    "PHASE_QUEUED",
    "PHASE_RUNNING",
    "PHASE_TERMINAL",
    "PHASE_UNKNOWN",
    "DEFAULT_RUN_STALL_THRESHOLD_SECONDS",
    "TAIL_MAX_BYTES",
    "get_stall_threshold_seconds",
    "derive_phase",
    "evaluate_stall",
    "derive_observability",
]