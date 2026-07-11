"""AEE-7.4 — event category vocabulary.

Every event the bridge emits falls into exactly one of the
4 categories below.  The category is a hint to the
orchestrator (GPT) about the event's semantic shape and the
default reaction:

* ``LIFECYCLE`` — the ordinary task lifecycle
  (``created`` / ``queued`` / ``started`` / ``completed`` /
  ``failed`` / ...).  These are the "narration" of what
  happened to a task.  No action required beyond logging.
* ``DELIVERY`` — the bridge noticed that a declared
  artifact was not actually written.  A *delivery
  verification* gap.  Default action: orchestrator inspects
  the file system and decides whether to retry.
* ``INTENT`` — the bridge's higher-signal "the LLM got
  stuck on intent" pattern (Phase 4.1).  Default action:
  ``escalate_to_human_review`` or rewrite the prompt;
  retrying with the same prompt will fail the same way.
* ``POLICY`` — a security / artifact-policy decision
  (``traversal`` secondary row emitted by
  ``aee/artifacts/collect.py`` when a path contains ``..``).
  These are audit breadcrumb rows; default action:
  observe-only.
* ``ORCHESTRATOR`` — AEE-7.4 slice 3 — the
  ``aee/orchestrator/`` hot path emitted events
  (``provider_selected`` / ``submit_started`` /
  ``submit_completed`` / ``submit_failed`` /
  ``poll_completed``).  Default action: trace the
  end-to-end pipeline; the dispatcher is the source
  of truth for the *task* lifecycle, the orchestrator
  is the source of truth for the *provider*
  lifecycle.

Why a category column at all
----------------------------
The orchestrator currently filters events by ``kind``
string equality (e.g. ``any(e["kind"] == "intent_mismatch"
for e in events)``).  That works for a handful of named
events but does not scale: a new event kind has to be
documented in two places (the emitter and the consumer)
and the consumer code has to grow a new branch.

A category column is the structural answer: the orchestrator
asks "did this task emit any INTENT event?" and gets a
boolean.  Adding a new INTENT event does not require touching
the consumer code.
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, Optional


class EventCategory(str, Enum):
    """Canonical event categories.  Stored as TEXT in
    ``task_events.kind`` would be the kind column; this enum
    is the contract the orchestrator's filter code uses.

    Members are strings (not ints) so they round-trip
    cleanly through SQLite TEXT columns and JSON payloads
    without a custom (de)serializer.  Use the
    ``.value`` string when persisting.
    """

    LIFECYCLE = "lifecycle"
    DELIVERY = "delivery"
    INTENT = "intent"
    POLICY = "policy"
    # AEE-7.4 slice 3 — orchestrator hot-path events.
    # See the comment above for the source-of-truth split.
    ORCHESTRATOR = "orchestrator"


# ``LIFECYCLE`` is the implicit default; every event the
# dispatcher emits today belongs to one of the four buckets.
_VALID_CATEGORIES: frozenset = frozenset(c.value for c in EventCategory)


def category_for(kind: str) -> Optional[EventCategory]:
    """Return the :class:`EventCategory` for the given event
    kind name, or ``None`` if the kind is not in the
    vocabulary.

    The mapping is owned by :mod:`aee.observability.events`
    (the SOT).  This module imports it lazily to keep the
    dependency one-way (categories do not import kinds;
    kinds do not import categories).
    """
    # Local import — the SOT lives in events.py; importing
    # at module top would create a cycle because events.py
    # itself imports this module for the tripwire.
    from .events import _CATEGORY_FOR_KIND  # noqa: WPS433 (intentional lazy import)

    if not kind:
        return None
    cat = _CATEGORY_FOR_KIND.get(kind)
    if cat is None:
        return None
    # The SOT stores the string value; we return the
    # enum member for type-narrowing at call sites.
    return EventCategory(cat)


def is_valid_category(name: str) -> bool:
    """``True`` iff ``name`` is one of the 4 known
    :class:`EventCategory` values.  Used by the tripwire
    regression test to validate that the
    ``_CATEGORY_FOR_KIND`` mapping in ``events.py`` does
    not contain a typo."""
    return name in _VALID_CATEGORIES
