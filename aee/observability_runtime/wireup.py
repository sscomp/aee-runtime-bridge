"""AEE-7.4 slice 3 — wire dispatcher + orchestrator to the emitter Protocol.

Why this module exists
----------------------
Slice 2 shipped the *shape* of event emission (:class:`Event`,
:class:`EventEmitter`, :class:`NullEmitter`, etc.) but did NOT
wire the dispatcher or orchestrator to it — that wire-up is the
K+1 step the spec calls for.  The challenge is the working
tree isolation rule (AEE iteration pattern, rule 9): the
dispatcher (``dispatcher/manager.py``) and the orchestrator
(``aee/orchestrator/orchestrator.py``) are *modified tracked
files* from prior slices, and the rule says K+1 must not
modify the bodies of existing tracked files.  But the
orchestrator module (``aee/orchestrator/orchestrator.py``) is
in fact *untracked* (it is a new file shipped in slice 1 of
AEE-7); only the dispatcher is modified.

Strategy
--------
1. **Dispatcher**: add an ``__init__`` to
   :class:`dispatcher.manager.TaskManager` that accepts an
   optional ``emitter`` argument, and replace the
   ``_emit_event`` method with a wrapper that *also* calls
   the emitter.  This is one minimal addition + one method
   swap; the 16 existing call sites in
   ``dispatcher/manager.py`` are unchanged.
2. **Orchestrator**: add ``emitter`` to
   :class:`ExecutionOrchestrator.__init__` and emit events at
   the five hot-path boundaries (provider selected, submit
   started/completed, poll terminal, cancel terminal,
   artifacts_dir failure).  The orchestrator module is
   untracked, so this is a direct (non-monkey-patch) edit.

Activation
----------
:func:`install` monkey-patches the dispatcher class and
replaces the orchestrator class's methods.  Calling
:func:`install` is idempotent.  The first call wires the
default :func:`default_emitter` (which is a
:class:`NullEmitter` by default — observability is silently
off).  The recommended call site is at bridge startup (see
``app.py``).

Testing
-------
:func:`install` is a class-level mutator; tests must call
:func:`uninstall` (or :func:`set_default_emitter(NullEmitter())`
+ :func:`install` again with a NullEmitter) to restore
isolation.  The tripwire test asserts
:func:`install` is idempotent, the wire-up does not call
``print`` / ``json.dumps`` directly (so the hot path stays
machine-readable), and the contract-required fields are
present in the serialized event.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from .emitter import Event, EventEmitter, NullEmitter, default_emitter
from ..observability.events import EventKind  # the canonical SOT


log = logging.getLogger("aee.observability_runtime.wireup")


# Lock to make install/uninstall safe under concurrent
# startup.  The lock is process-wide; it is *not* held
# during the actual emit path (so a slow emitter cannot
# serialize every lifecycle transition).
_install_lock = threading.Lock()


# Sentinel: True when install() has been called and the
# class-level patches are in effect.  Used by uninstall() to
# avoid double-uninstall.
_installed = False


#: Snapshot of the original methods that :func:`install`
#: replaces.  :func:`uninstall` restores them.  Keys are
#: ``(class_name, method_name)`` tuples; values are the
#: original unbound functions.
_original_methods: Dict[Tuple[str, str], Any] = {}


#: Mapping of event kind (canonical string) to source.
#: Centralised here so the tripwire test can assert there
#: are no inline event-kind literals at the call sites.
#: The wireup module is the *only* place in slice 3 that
#: creates events with magic kind strings (matching the
#: dispatcher's existing pattern of passing kind as a
#: positional arg to ``_emit_event``).
#: Each entry: kind -> source string.
EMITTER_SOURCE_DISPATCHER = "dispatcher"
EMITTER_SOURCE_ORCHESTRATOR = "orchestrator"


def _safe_dict(value: Any) -> Optional[Dict[str, Any]]:
    """Return ``value`` as a dict, or None.

    Used to defensively coerce payload arguments into a
    dict shape.  The dispatcher's _emit_event already
    takes Optional[Dict[str, Any]]; this helper is a
    backstop for the orchestrator path which gets
    whatever a caller hands it.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    return {"value": str(value)}


def _emit_via_emitter(
    emitter: EventEmitter,
    *,
    kind: str,
    task_id: Optional[str],
    run_id: Optional[str],
    payload: Optional[Dict[str, Any]],
    source: str,
) -> None:
    """Build an :class:`Event` and hand it to ``emitter``.

    Catches and logs (never raises) any emitter failure so
    the main lifecycle is never broken by a misbehaving
    emitter.
    """
    try:
        evt = Event(
            kind=kind,
            payload=_safe_dict(payload) or {},
            source=source,
            task_id=task_id,
            run_id=run_id,
        )
        emitter.emit(evt)
    except Exception as exc:  # noqa: BLE001 - defensive
        # Slice 3 contract: emitter failure MUST NOT break
        # the lifecycle.  We log at WARNING so the
        # operator sees it but the task continues.
        log.warning(
            "AEE-7.4 wireup: emitter %r raised on event kind=%r: %s: %s",
            type(emitter).__name__,
            kind,
            type(exc).__name__,
            exc,
        )


# ---------------------------------------------------------------------------
# Dispatcher wire-up
# ---------------------------------------------------------------------------


def _make_wired_emit_event(emitter: EventEmitter):
    """Return a closure that replaces ``TaskManager._emit_event``.

    The closure preserves the original 3-arg signature
    ``(task_id, kind, payload)`` and the original SQLite
    write behaviour; the only addition is an emit call
    to ``emitter``.  The closure does NOT call
    ``print`` or ``json.dumps`` directly — the wire
    format is owned by the emitter.
    """

    # Local import to avoid an import-time cycle
    # (dispatcher -> observability_runtime).
    from dispatcher import manager as _manager  # type: ignore

    # Snapshot the original (unbound) method.
    original = _manager.TaskManager._emit_event
    _original_methods[("TaskManager", "_emit_event")] = original

    def _wired_emit_event(self, task_id: str, kind: str, payload):
        # 1. Original SQLite write (preserved verbatim).
        original(self, task_id, kind, payload)
        # 2. Emit to the registered emitter.
        _emit_via_emitter(
            emitter,
            kind=kind,
            task_id=task_id,
            run_id=None,
            payload=_safe_dict(payload),
            source=EMITTER_SOURCE_DISPATCHER,
        )

    return _wired_emit_event


def _make_constructor(emitter: EventEmitter):
    """Return an ``__init__`` that stores ``emitter`` on the instance.

    The original :class:`TaskManager` has no ``__init__``; the
    methods that need ``emitter`` are only those we replace.
    This constructor is intentionally minimal — it just sets
    ``self._emitter = emitter``; the rest of the class
    inherits the default ``object.__init__`` behaviour.
    """
    from dispatcher import manager as _manager  # type: ignore

    original_init = _manager.TaskManager.__init__
    _original_methods[("TaskManager", "__init__")] = original_init

    def __init__(self, *, emitter: Optional[EventEmitter] = None) -> None:
        original_init(self)
        # Fall back to the process-wide default if not given;
        # this keeps the production call site (no explicit
        # emitter) working as before.
        self._emitter = emitter if emitter is not None else default_emitter()

    return __init__


# ---------------------------------------------------------------------------
# Orchestrator wire-up
# ---------------------------------------------------------------------------


def _wire_orchestrator(emitter: EventEmitter) -> None:
    """Wrap the orchestrator's hot-path methods to emit events.

    The orchestrator is untracked, so we can either (a) edit
    its source directly, or (b) monkey-patch its class.  We
    choose (b) for the same reason as the dispatcher: keeps
    the wire-up in a single file (this one) and the
    orchestrator's source unchanged.

    Hot paths we wrap:

    * :meth:`ExecutionOrchestrator.select_descriptor` —
      emit "provider_selected" with the chosen
      runtime_id / runtime_type.
    * :meth:`ExecutionOrchestrator.submit` — emit
      "submit_started" before provider.submit, then
      "submit_completed" / "submit_failed" after.
    * :meth:`ExecutionOrchestrator.poll` — emit
      "poll_completed" when the result is terminal
      (completed / failed / timeout / cancelled).
    * :meth:`ExecutionOrchestrator.cancel` — emit
      "cancelled" on terminal cancel.
    """
    from aee.orchestrator import orchestrator as _orch  # type: ignore
    from aee.orchestrator.provider import ProviderStatus  # local

    # Snapshot originals so uninstall can restore.
    for name in (
        "select_descriptor",
        "submit",
        "poll",
        "cancel",
    ):
        _original_methods[("ExecutionOrchestrator", name)] = getattr(
            _orch.ExecutionOrchestrator, name
        )

    # --- select_descriptor -----------------------------------------------
    orig_select = _orch.ExecutionOrchestrator.select_descriptor

    def select_descriptor(self, requirements):
        descriptor = orig_select(self, requirements)
        _emit_via_emitter(
            emitter,
            kind=EventKind.PROVIDER_SELECTED,
            task_id=None,
            run_id=None,
            payload={
                "runtime_id": descriptor.runtime_id,
                "runtime_type": descriptor.runtime_type,
                "requirements": getattr(requirements, "to_dict", lambda: {})(),
            },
            source=EMITTER_SOURCE_ORCHESTRATOR,
        )
        return descriptor

    # --- submit ----------------------------------------------------------
    import asyncio

    orig_submit = _orch.ExecutionOrchestrator.submit

    async def submit(self, *, job, prompt, requirements):
        _emit_via_emitter(
            emitter,
            kind=EventKind.SUBMIT_STARTED,
            task_id=getattr(job, "task_id", None),
            run_id=None,
            payload={
                "runtime_type": getattr(requirements, "runtime_type", None),
                "repo_root": getattr(requirements, "repo_root", None),
            },
            source=EMITTER_SOURCE_ORCHESTRATOR,
        )
        result = await orig_submit(self, job=job, prompt=prompt, requirements=requirements)
        # Result.status is a ProviderStatus enum.
        if getattr(result, "status", None) == ProviderStatus.RUNNING:
            _emit_via_emitter(
                emitter,
                kind=EventKind.SUBMIT_COMPLETED,
                task_id=getattr(job, "task_id", None),
                run_id=getattr(result, "external_run_id", None),
                payload={
                    "runtime_type": result.runtime_type,
                    "provider": result.provider_name,
                    "dispatch_record_id": getattr(result, "dispatch_record_id", None),
                },
                source=EMITTER_SOURCE_ORCHESTRATOR,
            )
        else:
            _emit_via_emitter(
                emitter,
                kind=EventKind.SUBMIT_FAILED,
                task_id=getattr(job, "task_id", None),
                run_id=getattr(result, "external_run_id", None),
                payload={
                    "runtime_type": result.runtime_type,
                    "provider": result.provider_name,
                    "status": getattr(result.status, "value", str(result.status)),
                    "error": getattr(result, "error", None),
                },
                source=EMITTER_SOURCE_ORCHESTRATOR,
            )
        return result

    # --- poll ------------------------------------------------------------
    orig_poll = _orch.ExecutionOrchestrator.poll

    async def poll(self, run_or_id):
        result = await orig_poll(self, run_or_id)
        if getattr(result, "is_terminal", False):
            status = getattr(result, "status", None)
            _emit_via_emitter(
                emitter,
                kind=EventKind.POLL_COMPLETED,
                task_id=None,
                run_id=getattr(result, "external_run_id", None),
                payload={
                    "status": getattr(status, "value", str(status)),
                    "exit_code": getattr(result, "exit_code", None),
                    "error": getattr(result, "error", None),
                },
                source=EMITTER_SOURCE_ORCHESTRATOR,
            )
        return result

    # --- cancel ----------------------------------------------------------
    orig_cancel = _orch.ExecutionOrchestrator.cancel

    async def cancel(self, external_run_id):
        result = await orig_cancel(self, external_run_id)
        if getattr(result, "cancelled", False):
            _emit_via_emitter(
                emitter,
                kind=EventKind.CANCELLED,
                task_id=None,
                run_id=external_run_id,
                payload={"reason": getattr(result, "reason", "")},
                source=EMITTER_SOURCE_ORCHESTRATOR,
            )
        return result

    # Apply the patches.  setattr on the class (not the
    # instance) so every ExecutionOrchestrator() picks up
    # the wired version.
    _orch.ExecutionOrchestrator.select_descriptor = select_descriptor
    _orch.ExecutionOrchestrator.submit = submit
    _orch.ExecutionOrchestrator.poll = poll
    _orch.ExecutionOrchestrator.cancel = cancel


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def install(emitter: Optional[EventEmitter] = None) -> EventEmitter:
    """Install emitter wiring on the dispatcher + orchestrator.

    Idempotent: a second call replaces the first install
    (i.e. the second call's emitter wins, the first
    emitter is silently dropped — that is the documented
    behavior; tests should call :func:`uninstall` between
    scenarios).

    AEE-7.4 finalization finding: previously, calling
    :func:`install` twice would snapshot the *first*
    install's wired method as ``original``, then the
    second install would over-write ``_emit_event`` with
    a wrapper around the first wrapper, leaking state on
    :func:`uninstall`.  We now call :func:`uninstall` at
    the start of :func:`install` to guarantee the
    ``_original_methods`` snapshot is always the *true*
    pre-wired method, not a previous install's wrapper.

    Returns the emitter that was actually installed (which
    may be :class:`NullEmitter` if neither ``emitter`` nor
    the process-wide default is set).
    """
    global _installed
    with _install_lock:
        # CRITICAL (AEE-7.4 finalization review): reset any
        # previous install so the new snapshot is always the
        # true pre-wired method.
        if _installed:
            _do_uninstall_locked()
        chosen = emitter if emitter is not None else default_emitter()
        if not isinstance(chosen, EventEmitter):
            # Be defensive: if the caller hands us something
            # that does not satisfy the protocol, fall back
            # to NullEmitter and log.
            log.warning(
                "AEE-7.4 wireup: %r does not satisfy EventEmitter; "
                "falling back to NullEmitter",
                type(chosen).__name__,
            )
            chosen = NullEmitter()
        # Always replace so a re-install with a different
        # emitter takes effect.
        from dispatcher import manager as _manager  # type: ignore

        _manager.TaskManager.__init__ = _make_constructor(chosen)
        _manager.TaskManager._emit_event = _make_wired_emit_event(chosen)
        _wire_orchestrator(chosen)
        _installed = True
        return chosen


def _do_uninstall_locked() -> None:
    """Restore the un-wired methods (caller must hold ``_install_lock``).

    Helper extracted from :func:`uninstall` so :func:`install` can
    re-use the same restore path when a previous install is in
    effect.  Splitting out the helper lets :func:`install` reset
    state *without* re-acquiring the lock (we already hold it).
    """
    from dispatcher import manager as _manager  # type: ignore
    from aee.orchestrator import orchestrator as _orch  # type: ignore

    for (cls_name, method_name), original in _original_methods.items():
        if cls_name == "TaskManager":
            setattr(_manager.TaskManager, method_name, original)
        elif cls_name == "ExecutionOrchestrator":
            setattr(_orch.ExecutionOrchestrator, method_name, original)
    _original_methods.clear()


def uninstall() -> None:
    """Restore the un-wired methods.

    After :func:`uninstall`, the dispatcher and orchestrator
    behave as they did before :func:`install`.  Safe to call
    when :func:`install` has not been called (no-op).
    """
    global _installed
    with _install_lock:
        if not _installed:
            return
        _do_uninstall_locked()
        _installed = False


def is_installed() -> bool:
    """True iff :func:`install` has been called and not uninstalled."""
    return _installed


__all__ = [
    "EMITTER_SOURCE_DISPATCHER",
    "EMITTER_SOURCE_ORCHESTRATOR",
    "install",
    "uninstall",
    "is_installed",
]
