"""AEE-7.4 — event-name vocabulary (the single source of truth).

This module is the **only** place in the bridge codebase
where the literal event-name strings (``"delivery_unverified"``,
``"intent_mismatch"``, ``"traversal"``, the 13 lifecycle
events, ...) may appear.  Any other occurrence is a
regression and will be caught by the tripwire test in
``aee/tests/test_aee74_observability.py``.

Why this design
---------------
Pre-AEE-7.4, the event-name strings lived as inline
literals at the call sites in
``dispatcher/manager.py:_emit_event``,
``aee/orchestrator/orchestrator.py`` (planned), and
``aee/artifacts/collect.py:record_traversal_event``.
The same anti-pattern motivated the AEE-7.3 ``FailureCode``
SOT rescue: a literal added to one site and forgotten in
the other produces two events with the same shape but
different spellings, which the orchestrator's filter
``any(e["kind"] == "intent_mismatch" for e in events)``
silently misroutes.

The fix is the same one AEE-7.3 used: a class of named
constants, a single mapping per axis (kind → category,
kind → severity), and a tripwire regression test that
fails the build if the literals leak.

Inventory of the 26 event kinds (canonical)
-------------------------------------------

LIFECYCLE (17 — the ordinary task lifecycle narration):

* ``created``      — task created (POST /runs succeeded)
* ``queued``       — task queued for dispatch
* ``status``       — state-machine transition
* ``started``      — worker claimed + invoked
* ``progress``     — worker reported progress (pct + step)
* ``log``          — worker log line (truncated to 500 chars)
* ``warning``      — non-fatal warning
* ``completed``    — task completed
* ``failed``       — task failed (terminal error)
* ``timeout``      — task timed out
* ``cancelled``    — task cancelled
* ``retry_of``     — a new task created as a retry of an
  earlier one
* ``openai_run_attached`` — a GPT Custom GPT attached an
  OpenAI run id to the task
* ``claimed``      — worker claim race winner (AEE-7.4
  finalization)
* ``notification_pending`` — AEE v3 Telegram Completion
  Enforcement Gate: the gate fired but the send has not been
  confirmed (no ``message_id`` yet, e.g. queued but not
  delivered, or the legacy fallback returned ``sent=True``
  without a message id). LIFECYCLE WARN.
* ``notification_completed`` — AEE v3 gate: the
  Hermes-Telegram-Gateway (or the legacy notifier fallback)
  returned a confirmed ``message_id``. LIFECYCLE INFO.
* ``notification_failed`` — AEE v3 gate: both the gateway path
  and the legacy fallback failed to send. LIFECYCLE WARN. The
  task is still ``status='completed'`` for backward compat —
  the gate is observability-only in this iteration.

DELIVERY (1 — Phase 4 delivery verification):

* ``delivery_unverified`` — expected artifact was not
  written

INTENT (1 — Phase 4.1 intent-mismatch detection):

* ``intent_mismatch`` — output prose says "let me write"
  (or 11 other patterns) AND expected paths are missing

POLICY (2 — AEE-6 / AEE-7 artifact policy audit):

* ``traversal``    — secondary audit row when the path
  contained literal ``..`` segments
  (``code='traversal'`` in
  ``aee/artifacts/policy.py`` and
  ``aee/artifacts/collect.py``)
* ``policy_event`` — generic policy audit event
  (reserved; emitted by :class:`ArtifactPolicy.to_event`
  for non-traversal cases)

ORCHESTRATOR (5 — AEE-7.4 slice 3 — the
``aee/orchestrator/`` hot path):

* ``provider_selected`` — ``select_descriptor`` resolved
  a ``RuntimeDescriptor`` for the given
  ``TaskRuntimeRequirements``.
* ``submit_started``    — the orchestrator entered the
  provider's submit path; emit BEFORE the awaited
  ``provider.submit`` returns.
* ``submit_completed``  — provider accepted the run;
  ``ProviderRun.status == RUNNING``.
* ``submit_failed``     — provider rejected the submit;
  ``ProviderRun.status`` is not ``RUNNING`` (e.g.
  validation error, queue full).
* ``poll_completed``    — the orchestrator's poll cycle
  reached a terminal status (``completed`` /
  ``failed`` / ``timeout`` / ``cancelled``).

Why ``traversal`` is BOTH an EventKind AND an artifact
policy ``code``
------------------------------------------------------
They happen to share the literal string ``"traversal"``,
but they are emitted by different code paths and read by
different consumers.  The artifact policy code is a
column value in the ``artifact_policy_events`` SQLite
table; the event kind is a column value in
``task_events.kind``.  This module pins both spellings
in one place so a typo fix updates both consistently.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, List

from .categories import EventCategory
from .severity import EventSeverity


class EventKind:
    """Canonical event-name literals for the bridge.

    These are the strings persisted to ``task_events.kind``
    in production, asserted against in tests, and read by
    the orchestrator (GPT) when filtering event streams.

    Adding a new event kind: extend this class + the
    :data:`_CATEGORY_FOR_KIND` and
    :data:`_SEVERITY_FOR_KIND` mappings below.  Never
    inline a new literal at a call site — the tripwire
    regression test will fail.

    Members are *class-level string constants*, not
    ``Enum`` members, because the on-disk shape is
    ``TEXT`` (not INT) and the orchestrator's filter
    compares against the string value.  Using ``str``
    constants keeps the round-trip trivial.
    """

    # --- LIFECYCLE (14) ---------------------------------------------------
    CREATED = "created"
    QUEUED = "queued"
    STATUS = "status"
    STARTED = "started"
    PROGRESS = "progress"
    LOG = "log"
    WARNING = "warning"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    RETRY_OF = "retry_of"
    OPENAI_RUN_ATTACHED = "openai_run_attached"
    # AEE-7.4 finalization — a worker claimed a job (race winner
    # in ``aee/api/jobs.py`` claim flow).  LIFECYCLE INFO.
    CLAIMED = "claimed"
    # AEE v3 Telegram Completion Enforcement Gate — narrate the
    # notification outcome. All three are LIFECYCLE (the gate is
    # part of the ordinary task narration, not a new category):
    #   * PENDING   — gate fired, no confirmed message_id yet (WARN)
    #   * COMPLETED — gate returned a confirmed message_id (INFO)
    #   * FAILED    — both gateway + legacy fallback failed (WARN)
    # The gate is observability-only in this iteration; the task's
    # ``status='completed'`` SQL UPDATE is unchanged. See
    # ``dispatcher/notification_state.py`` for the 4-stage model
    # and ``dispatcher/notifier.py:notify_completed_with_fallback``
    # for the gate implementation.
    NOTIFICATION_PENDING = "notification_pending"
    NOTIFICATION_COMPLETED = "notification_completed"
    NOTIFICATION_FAILED = "notification_failed"

    # --- DELIVERY (1) -----------------------------------------------------
    DELIVERY_UNVERIFIED = "delivery_unverified"

    # --- INTENT (1) -------------------------------------------------------
    INTENT_MISMATCH = "intent_mismatch"

    # --- POLICY (2) ------------------------------------------------------
    TRAVERSAL = "traversal"
    POLICY_EVENT = "policy_event"

    # --- ORCHESTRATOR (5) --- AEE-7.4 slice 3 -----------------------------
    PROVIDER_SELECTED = "provider_selected"
    SUBMIT_STARTED = "submit_started"
    SUBMIT_COMPLETED = "submit_completed"
    SUBMIT_FAILED = "submit_failed"
    POLL_COMPLETED = "poll_completed"

    @classmethod
    def all(cls) -> FrozenSet[str]:
        """Return a frozenset of every known event kind
        string.  Used by the tripwire regression test to
        validate that ``_CATEGORY_FOR_KIND`` and
        ``_SEVERITY_FOR_KIND`` are in lock-step with
        :class:`EventKind`."""
        return frozenset(
            {
                cls.CREATED,
                cls.QUEUED,
                cls.STATUS,
                cls.STARTED,
                cls.PROGRESS,
                cls.LOG,
                cls.WARNING,
                cls.COMPLETED,
                cls.FAILED,
                cls.TIMEOUT,
                cls.CANCELLED,
                cls.RETRY_OF,
                cls.OPENAI_RUN_ATTACHED,
                cls.CLAIMED,
                cls.NOTIFICATION_PENDING,
                cls.NOTIFICATION_COMPLETED,
                cls.NOTIFICATION_FAILED,
                cls.DELIVERY_UNVERIFIED,
                cls.INTENT_MISMATCH,
                cls.TRAVERSAL,
                cls.POLICY_EVENT,
                cls.PROVIDER_SELECTED,
                cls.SUBMIT_STARTED,
                cls.SUBMIT_COMPLETED,
                cls.SUBMIT_FAILED,
                cls.POLL_COMPLETED,
            }
        )


# Category mapping — every known event kind MUST have a
# category.  Tripwire test asserts
# ``set(_CATEGORY_FOR_KIND) == EventKind.all()``.
_CATEGORY_FOR_KIND: Dict[str, str] = {
    # LIFECYCLE
    EventKind.CREATED: EventCategory.LIFECYCLE.value,
    EventKind.QUEUED: EventCategory.LIFECYCLE.value,
    EventKind.STATUS: EventCategory.LIFECYCLE.value,
    EventKind.STARTED: EventCategory.LIFECYCLE.value,
    EventKind.PROGRESS: EventCategory.LIFECYCLE.value,
    EventKind.LOG: EventCategory.LIFECYCLE.value,
    EventKind.WARNING: EventCategory.LIFECYCLE.value,
    EventKind.COMPLETED: EventCategory.LIFECYCLE.value,
    EventKind.FAILED: EventCategory.LIFECYCLE.value,
    EventKind.TIMEOUT: EventCategory.LIFECYCLE.value,
    EventKind.CANCELLED: EventCategory.LIFECYCLE.value,
    EventKind.RETRY_OF: EventCategory.LIFECYCLE.value,
    EventKind.OPENAI_RUN_ATTACHED: EventCategory.LIFECYCLE.value,
    EventKind.CLAIMED: EventCategory.LIFECYCLE.value,
    # AEE v3 Telegram Completion Enforcement Gate — narrated as
    # LIFECYCLE (the gate is part of the ordinary task narration,
    # not a new category; minimal blast radius per the v3 design).
    EventKind.NOTIFICATION_PENDING: EventCategory.LIFECYCLE.value,
    EventKind.NOTIFICATION_COMPLETED: EventCategory.LIFECYCLE.value,
    EventKind.NOTIFICATION_FAILED: EventCategory.LIFECYCLE.value,
    # DELIVERY
    EventKind.DELIVERY_UNVERIFIED: EventCategory.DELIVERY.value,
    # INTENT
    EventKind.INTENT_MISMATCH: EventCategory.INTENT.value,
    # POLICY
    EventKind.TRAVERSAL: EventCategory.POLICY.value,
    EventKind.POLICY_EVENT: EventCategory.POLICY.value,
    # AEE-7.4 slice 3 — orchestrator hot-path events.
    EventKind.PROVIDER_SELECTED: EventCategory.ORCHESTRATOR.value,
    EventKind.SUBMIT_STARTED: EventCategory.ORCHESTRATOR.value,
    EventKind.SUBMIT_COMPLETED: EventCategory.ORCHESTRATOR.value,
    EventKind.SUBMIT_FAILED: EventCategory.ORCHESTRATOR.value,
    EventKind.POLL_COMPLETED: EventCategory.ORCHESTRATOR.value,
}


# Severity mapping — every known event kind MUST have a
# severity.  Tripwire test asserts
# ``set(_SEVERITY_FOR_KIND) == EventKind.all()``.
_SEVERITY_FOR_KIND: Dict[str, str] = {
    # LIFECYCLE: all INFO except WARNING (which is WARN).
    EventKind.CREATED: EventSeverity.INFO.value,
    EventKind.QUEUED: EventSeverity.INFO.value,
    EventKind.STATUS: EventSeverity.INFO.value,
    EventKind.STARTED: EventSeverity.INFO.value,
    EventKind.PROGRESS: EventSeverity.INFO.value,
    EventKind.LOG: EventSeverity.INFO.value,
    EventKind.WARNING: EventSeverity.WARN.value,
    EventKind.COMPLETED: EventSeverity.INFO.value,
    EventKind.FAILED: EventSeverity.WARN.value,
    EventKind.TIMEOUT: EventSeverity.WARN.value,
    EventKind.CANCELLED: EventSeverity.WARN.value,
    EventKind.RETRY_OF: EventSeverity.INFO.value,
    EventKind.OPENAI_RUN_ATTACHED: EventSeverity.INFO.value,
    EventKind.CLAIMED: EventSeverity.INFO.value,
    # AEE v3 Telegram Completion Enforcement Gate.
    #   * PENDING   — WARN: the send is not yet confirmed (queued
    #     or fell back to the legacy notifier which returns no
    #     message_id).
    #   * COMPLETED — INFO: the gate confirmed delivery (message_id
    #     present).
    #   * FAILED    — WARN: both the gateway + the legacy fallback
    #     failed; the operator should inspect.
    EventKind.NOTIFICATION_PENDING: EventSeverity.WARN.value,
    EventKind.NOTIFICATION_COMPLETED: EventSeverity.INFO.value,
    EventKind.NOTIFICATION_FAILED: EventSeverity.WARN.value,
    # DELIVERY: WARN — the task completed but the artifact is
    # missing.  Default action: orchestrator inspects and
    # decides.
    EventKind.DELIVERY_UNVERIFIED: EventSeverity.WARN.value,
    # INTENT: HIGH — the LLM is stuck on intent; retrying with
    # the same prompt will fail the same way.
    EventKind.INTENT_MISMATCH: EventSeverity.HIGH.value,
    # POLICY: INFO — audit breadcrumb; observe-only.
    EventKind.TRAVERSAL: EventSeverity.INFO.value,
    EventKind.POLICY_EVENT: EventSeverity.INFO.value,
    # AEE-7.4 slice 3 — orchestrator hot-path events.
    # All INFO by default; the orchestrator's wire-up
    # can override ``severity`` on a per-event basis if
    # it needs a WARN/HIGH label (it does not today).
    EventKind.PROVIDER_SELECTED: EventSeverity.INFO.value,
    EventKind.SUBMIT_STARTED: EventSeverity.INFO.value,
    EventKind.SUBMIT_COMPLETED: EventSeverity.INFO.value,
    EventKind.SUBMIT_FAILED: EventSeverity.WARN.value,
    EventKind.POLL_COMPLETED: EventSeverity.INFO.value,
}


def is_known(kind: str) -> bool:
    """``True`` iff ``kind`` is one of the canonical
    :class:`EventKind` values.  Used by the orchestrator's
    filter code and by the tripwire test."""
    return kind in _CATEGORY_FOR_KIND


def events_by_category() -> Dict[str, List[str]]:
    """Return a dict of ``{category: [kind, ...]}`` for
    every known event.  Convenience for dashboards /
    docs renderers; not used on the dispatch hot path."""
    out: Dict[str, List[str]] = {}
    for kind, cat in _CATEGORY_FOR_KIND.items():
        out.setdefault(cat, []).append(kind)
    # Sort for deterministic output (helps the tripwire
    # test catch accidental ordering changes).
    for cat in out:
        out[cat].sort()
    return out
