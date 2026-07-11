"""AEE-7.4 — Observability event taxonomy (single source of truth).

This package is the canonical home for the event-name vocabulary
used across the bridge, the dispatcher, and the AEE-7
orchestrator.  The motivation is the same class of bug that
motivated the AEE-7.3 ``FailureCode`` SOT rescue: event kinds
were scattered as string literals across
``dispatcher/manager.py``, ``aee/orchestrator/orchestrator.py``,
``aee/artifacts/collect.py``, and the tests.  Two consecutive
G2 (GPT-orchestrated) task failures (TASK-20260708-0017 and
TASK-20260708-0018) were caused in part by the orchestrator
having to read the agent's prose to know the failure shape; a
typed event vocabulary is the structural fix.

Public surface (intentionally narrow, mirroring
``aee/orchestrator/__init__.py``):

* :class:`EventCategory` — the 4 buckets every event falls
  into.  Used to filter an event stream by intent.
* :class:`EventSeverity` — the 3 severities the bridge emits
  today.  An event's severity is the orchestrator's hint about
  whether to retry / escalate / observe-only.
* :class:`EventKind` — the 23 canonical event-name literals
  (14 LIFECYCLE + 1 DELIVERY + 1 INTENT + 2 POLICY + 5 ORCHESTRATOR;
  the finalization round added ``CLAIMED`` for the worker claim
  race winner).  This is the single source of truth.  Anything
  written to ``task_events.kind`` (in production) or asserted
  in tests MUST resolve to a member of this class.
* :func:`category_for` — map an event name to its category.
* :func:`severity_for` — map an event name to its severity.
* :func:`is_known` — quick membership check (used by the
  tripwire regression test).
* :func:`events_by_category` — group all known events by
  category, for dashboards.

Why a separate package
----------------------
The vocabulary is consumed by (at least) three call sites:

1. ``dispatcher/manager.py:_emit_event`` — the production
   writer for ``task_events.kind``.  Every event written
   here MUST be a member of :class:`EventKind`.
2. ``aee/orchestrator/orchestrator.py`` — emits
   ``delivery_unverified``-style events in the post-poll
   path.  The orchestrator is currently a pure-domain
   module (no I/O); in AEE-7.4+ the event-emission side
   may be added.
3. ``aee/tests/test_*.py`` — assertions on event names.

A separate package is the cleanest way to (a) be the import
target for all three, (b) be the only place where the
literal strings live, and (c) host the tripwire regression
test that pins the vocabulary in CI.

Anti-patterns (do NOT do)
-------------------------
* Do NOT add a new event kind by writing a literal at a call
  site.  Add it to ``EventKind`` and (if it deserves a new
  category) ``EventCategory``.  The tripwire test will fail
  if you do.
* Do NOT rename an existing event kind without a one-version
  compatibility shim — the orchestrator (GPT) reads
  ``kind = "intent_mismatch"`` as a string contract.
* Do NOT couple this package to FastAPI / Starlette / any
  HTTP framework.  It is a pure-domain vocabulary package
  and may be embedded in any caller.
"""
from __future__ import annotations

from .categories import EventCategory, category_for
from .events import EventKind, events_by_category, is_known
from .severity import EventSeverity, severity_for

__all__ = [
    # Categories
    "EventCategory",
    "category_for",
    # Severities
    "EventSeverity",
    "severity_for",
    # Event vocabulary (the SOT)
    "EventKind",
    "events_by_category",
    "is_known",
]
