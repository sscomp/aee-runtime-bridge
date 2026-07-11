"""AEE-7.4 — event severity vocabulary.

The severity is the bridge's hint to the orchestrator about
*what to do* with an event.  It is orthogonal to the
category: an event can be POLICY + INFO (``traversal`` audit
row, observe-only) or DELIVERY + HIGH (the LLM's prose
declares intent to write but the file is missing —
escalate).

Severity ladder
---------------

* ``INFO`` — ordinary event, no action required.  The
  default for LIFECYCLE events.
* ``WARN`` — something is off, but the task itself is not
  necessarily broken.  Most DELIVERY events land here.
* ``HIGH`` — a known-stuck pattern; the orchestrator
  should *not* retry the same prompt.  ``intent_mismatch``
  is the only HIGH event today, and the only one where
  the docs recommend a specific ``recommended_action``
  string.

The mapping is owned by :mod:`aee.observability.events`
(the SOT).  This module exposes the enum + the lookup
function.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional


class EventSeverity(str, Enum):
    """Canonical event severities.

    Members are strings (not ints) so they round-trip
    cleanly through SQLite TEXT columns and JSON payloads
    without a custom (de)serializer.
    """

    INFO = "info"
    WARN = "warn"
    HIGH = "high"


_VALID_SEVERITIES: frozenset = frozenset(s.value for s in EventSeverity)


def severity_for(kind: str) -> Optional[EventSeverity]:
    """Return the :class:`EventSeverity` for the given event
    kind name, or ``None`` if the kind is not in the
    vocabulary.

    Unknown kinds return ``None`` so the caller can decide
    whether to fall back to ``INFO`` (the safe default) or
    raise.  The tripwire regression test in
    ``aee/tests/test_aee74_observability.py`` asserts that
    every known event kind has a severity.
    """
    # Local import — see the note in categories.py about
    # the one-way dependency.
    from .events import _SEVERITY_FOR_KIND  # noqa: WPS433 (intentional lazy import)

    if not kind:
        return None
    sev = _SEVERITY_FOR_KIND.get(kind)
    if sev is None:
        return None
    return EventSeverity(sev)


def is_valid_severity(name: str) -> bool:
    """``True`` iff ``name`` is one of the 3 known
    :class:`EventSeverity` values."""
    return name in _VALID_SEVERITIES
