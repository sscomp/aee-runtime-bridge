"""AEE-7.4 slice 2 — Event dataclass, EventEmitter Protocol, NullEmitter,
and the process-wide default-emitter registry.

Why Protocol
------------
The dispatcher / orchestrator call sites should not depend
on a concrete emitter class — they need to depend on a
*shape* so tests can swap in :class:`BufferingEmitter`
without touching production code.  ``typing.Protocol`` is
the structural answer: any class with ``emit(event)`` and
``close()`` is an ``EventEmitter`` even if it does not
inherit from this protocol.  Duck typing is the desired
behavior.

Why a frozen dataclass
----------------------
:class:`Event` is the unit of emission.  It needs to be
hashable (so tests can put it in a set), printable (so logs
are useful), and immutable (so an event cannot be mutated
after emission — that would invalidate any downstream
hash/audit trail).  ``@dataclass(frozen=True)`` gives all
three.  The default factory for ``payload`` is a fresh
empty dict so two events with no payload compare equal.

Why the registry is process-wide
--------------------------------
A long-lived daemon (the bridge) needs a single source of
truth for "which emitter am I using right now" so that
sibling modules do not have to thread a parameter through
every call site.  The registry starts at
:class:`NullEmitter` so a fresh process is observable-safe:
if no one configures an emitter, every ``emit`` is a no-op
and no event ever leaves the process.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional, Protocol, runtime_checkable
import uuid


def _default_event_id() -> str:
    """Return a unique 12-char hex id for an Event.

    Used as the default for :attr:`Event.event_id`.  Not a
    full UUID (12 hex chars == 48 bits, enough for in-process
    uniqueness over a single daemon lifetime) so the
    serialized event stays compact.
    """
    return uuid.uuid4().hex[:12]


def _now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Event:
    """An event ready to be emitted.

    Fields
    ------
    kind
        The event kind name (e.g. ``"completed"``,
        ``"intent_mismatch"``).  MUST be one of the
        :class:`aee.observability.EventKind` values.  The
        :func:`__post_init__` validator enforces this at
        construction time — passing a typo raises
        :class:`ValueError` immediately rather than letting
        the typo silently leak into the event stream.
    payload
        Arbitrary key/value context (e.g. ``{"duration_sec": 12.3}``).
        Wrapped in :class:`MappingProxyType` so the event
        cannot be mutated through the payload after
        construction.
    source
        Which subsystem emitted the event.  Examples:
        ``"dispatcher"``, ``"orchestrator"``,
        ``"artifacts_collect"``.  Lower-snake by
        convention; not validated (the SOT does not pin
        source names — new sources are added freely).
    task_id
        Optional task identifier.  ``None`` for events that
        are not bound to a single task.
    run_id
        Optional run identifier.  ``None`` for events that
        are not bound to a single run.
    severity
        Optional explicit severity override.  When ``None``,
        the emitter looks the severity up from the SOT via
        :func:`aee.observability.severity_for`.  Pass an
        explicit value only when the event is an unknown
        kind (and the validator was bypassed — see the
        ``bypass_sot_check`` flag below).
    bypass_sot_check
        When ``True``, skip the ``is_known(kind)`` check.
        Reserved for the *K+2 wire-up slice* (AEE-7.4+)
        that introduces new kinds *before* the SOT grows a
        new entry.  Production code today should never set
        this; tests may use it to assert the validator
        behavior.
    """

    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    source: str = "unknown"
    task_id: Optional[str] = None
    run_id: Optional[str] = None
    severity: Optional[str] = None
    bypass_sot_check: bool = False
    # AEE-7.4 slice 3: deterministic id + timestamp.
    # `event_id` defaults to a fresh 12-char hex; the
    # serialization layer emits it as the wire id.
    # `timestamp_iso` defaults to now() in ISO-8601; the
    # serialization layer uses it as the wire timestamp.
    event_id: str = field(default_factory=_default_event_id)
    timestamp_iso: Optional[str] = field(default_factory=_now_iso)

    def __post_init__(self) -> None:  # noqa: D401 — dataclass hook
        """Validate ``kind`` against the SOT, freeze the payload.

        Three steps (in order):

        1. **Empty-kind check** — if ``kind`` is falsy
           (empty string, ``None``), raise ``ValueError``
           *unconditionally*.  This check is NOT bypassed
           by ``bypass_sot_check`` because an empty kind
           is never a legitimate "future kind" — it is
           always a caller bug.
        2. **Freeze the payload** — wrap ``payload`` in
           :class:`MappingProxyType` so the event cannot
           be mutated through the payload after
           construction.  We do this via
           ``object.__setattr__`` because the dataclass
           is frozen.
        3. **SOT validation** — unless ``bypass_sot_check``
           is ``True``, call
           :func:`aee.observability.is_known` and raise
           ``ValueError`` for unknown kinds.
        """
        # 1. Empty-kind check (unconditional).
        if not self.kind:
            raise ValueError(
                "Event.kind is empty; this is always a caller bug. "
                "Even bypass_sot_check=True does not permit an empty kind."
            )
        # 2. Freeze the payload (frozen dataclass forbids normal
        # assignment, hence the ``object.__setattr__`` dance).
        if not isinstance(self.payload, MappingProxyType):
            object.__setattr__(
                self, "payload", MappingProxyType(dict(self.payload))
            )
        # 3. SOT validation (lazy import keeps the one-way
        # dependency: emitter -> observability SOT).
        if self.bypass_sot_check:
            return
        from aee.observability import is_known  # noqa: WPS433

        if not is_known(self.kind):
            raise ValueError(
                f"Event.kind={self.kind!r} is not in the AEE-7.4 SOT "
                "(see aee.observability.EventKind). "
                "Either use a known kind, or pass bypass_sot_check=True "
                "for K+2 wire-up work that introduces a new kind first."
            )

    @property
    def effective_severity(self) -> str:
        """Return the severity string the emitter should record.

        If ``severity`` is set explicitly, use it.  Otherwise
        look up the SOT default via
        :func:`aee.observability.severity_for`; if the lookup
        returns ``None`` (which should not happen for known
        kinds, but is defensive against SOT drift), fall
        back to ``"info"``.
        """
        if self.severity is not None:
            return self.severity
        from aee.observability import severity_for  # noqa: WPS433

        looked_up = severity_for(self.kind)
        if looked_up is None:
            return "info"
        return looked_up.value

    def to_dict(self) -> dict:
        """Plain-dict view, suitable for JSON serialization.

        ``payload`` is copied out of the
        :class:`MappingProxyType` so consumers get a
        mutable copy.  Used by :class:`StdoutJsonEmitter`
        and by tests.
        """
        return {
            "kind": self.kind,
            "payload": dict(self.payload),
            "source": self.source,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "severity": self.effective_severity,
            "event_id": self.event_id,
            "timestamp_iso": self.timestamp_iso,
        }

    def __hash__(self) -> int:
        """Hash on the primitive fields only.

        :class:`MappingProxyType` is unhashable, so the
        default dataclass-generated ``__hash__`` (which
        walks ``__dict__``) raises ``TypeError`` for any
        event with a non-empty payload.  We restrict the
        hash to the 7 primitive fields so events with
        payloads are still hashable.  Two events with
        the same primitive fields but different payloads hash
        equal — that is the documented trade-off (use
        :meth:`to_dict` for a full-fidelity comparison).
        """
        return hash(
            (
                self.kind,
                self.source,
                self.task_id,
                self.run_id,
                self.severity,
                self.event_id,
                self.timestamp_iso,
            )
        )


@runtime_checkable
class EventEmitter(Protocol):
    """Structural protocol for any sink that can receive an :class:`Event`.

    Two methods, no exceptions:

    * :meth:`emit` — receive one :class:`Event` and
      persist / forward it.  MUST NOT raise for ordinary
      input (malformed event is the caller's bug, fixed at
      construction time by the dataclass validator).  MAY
      raise for I/O failures (e.g. stdout closed); in that
      case the bridge's call site should log + swallow, not
      crash the task.
    * :meth:`close` — release any resources.  MUST be
      idempotent (callable multiple times without error).
      Implementations that hold no resources can be no-ops.
    """

    def emit(self, event: Event) -> None: ...

    def close(self) -> None: ...


class NullEmitter:
    """No-op emitter.

    Drop-in safe default.  Used in two places:

    1. As the initial value of the process-wide registry
       (``default_emitter()`` returns this until someone
       calls :func:`set_default_emitter`).
    2. As the emitter passed to code paths that have not
       decided on a real sink yet (e.g. a sub-system that
       wants to *log internally* but not *emit to the
       outside world*).
    """

    def emit(self, event: Event) -> None:  # noqa: D401 — protocol impl
        """Do nothing.  Argument is accepted and discarded."""
        return None

    def close(self) -> None:  # noqa: D401 — protocol impl
        """Idempotent no-op."""
        return None


# Process-wide registry.  Single module-level instance so
# any code path can grab it without dependency injection.
# ``_default_emitter`` is a single-element list so
# ``set_default_emitter`` can rebind it from a closure
# without ``global`` (and without leaking the binding
# across test boundaries — tests call
# :func:`set_default_emitter` in ``setUp`` and
# ``tearDown``).
_default_emitter: list = [NullEmitter()]


def default_emitter() -> EventEmitter:
    """Return the process-wide default emitter.

    Never returns ``None``.  Starts at :class:`NullEmitter`.
    """
    return _default_emitter[0]


def set_default_emitter(emitter: EventEmitter) -> None:
    """Install a new process-wide default emitter.

    Pass :class:`NullEmitter` to reset.  Callers are
    responsible for calling :meth:`close` on the
    *previous* emitter before replacing it (this function
    does not close for you, so a test that swaps
    emitters mid-run does not lose in-flight buffered
    events).
    """
    _default_emitter[0] = emitter
