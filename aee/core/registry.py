"""AEE adapter + worker registries.

AdapterRegistry
---------------
A module-level singleton that maps `adapter_name` (e.g. "hermes",
"fake", "aee_lightweight" — the AEE Lightweight Agent Runtime) to a
`RuntimeAdapter` instance. Tests call `register()` to install a
FakeAdapter; production wires the real `HermesAdapter` at startup.

WorkerRegistry
--------------
AEE-2 will populate this from `POST /workers/register`; AEE-1 only
exposes the shape so the rest of the code can type-hint against it.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from aee.adapters.base import AdapterNotFoundError, RuntimeAdapter


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# AdapterRegistry
# ---------------------------------------------------------------------------


class AdapterRegistry:
    """Thread-safe registry of `RuntimeAdapter` instances.

    Default entries are populated by `bootstrap_defaults()`; tests
    typically replace them with `register()`.
    """

    def __init__(self) -> None:
        self._adapters: Dict[str, RuntimeAdapter] = {}
        self._lock = threading.Lock()

    def register(self, adapter: RuntimeAdapter, *, replace: bool = False) -> None:
        if not isinstance(adapter, RuntimeAdapter):
            raise TypeError(
                f"adapter {adapter!r} does not satisfy RuntimeAdapter protocol"
            )
        with self._lock:
            existing = self._adapters.get(adapter.name)
            if existing is not None and not replace:
                raise ValueError(
                    f"adapter {adapter.name!r} already registered; "
                    "pass replace=True to override"
                )
            self._adapters[adapter.name] = adapter

    def unregister(self, name: str) -> None:
        with self._lock:
            self._adapters.pop(name, None)

    def get(self, name: str) -> RuntimeAdapter:
        with self._lock:
            adapter = self._adapters.get(name)
            if adapter is None:
                raise AdapterNotFoundError(
                    f"no adapter registered under {name!r}; "
                    f"known={sorted(self._adapters)}"
                )
            return adapter

    def names(self) -> List[str]:
        with self._lock:
            return sorted(self._adapters)

    def all(self) -> List[RuntimeAdapter]:
        with self._lock:
            return list(self._adapters.values())


# Module-level singleton used by the rest of AEE.
adapter_registry = AdapterRegistry()


def bootstrap_defaults(force: bool = False) -> None:
    """Register the default set of adapters.

    In production this wires the real HermesAdapter; the function
    is safe to call more than once (subsequent calls are no-ops
    unless `force=True`).

    AEE-7.1: also registers the ``ClaudeCodeRuntimeAdapter`` shim
    so a task with ``adapter_name="claude_code"`` routes through
    the ``ExecutionOrchestrator`` instead of the legacy
    ``adapter_registry`` 404 path. The shim is best-effort —
    if the AEE-7 orchestrator cannot be imported, the
    hermes-only fallback is preserved.
    """
    from aee.adapters.hermes_adapter import HermesAdapter  # local import

    existing = "hermes" in adapter_registry.names()
    if existing and not force:
        # Even when we are not forcing, still make sure the
        # claude_code shim is registered (idempotent).
        _register_aee7_defaults()
        return
    adapter_registry.register(HermesAdapter(), replace=True)
    _register_aee7_defaults()


def _register_aee7_defaults() -> None:
    """AEE-7.1: register the claude_code AEE-2 adapter shim.

    Idempotent: if the shim is already registered, this is a
    no-op. If the import fails (e.g. slim install), the shim
    is silently skipped — legacy ``adapter_name="hermes"``
    tasks still work.
    """
    if "claude_code" in adapter_registry.names():
        return
    try:
        from aee.orchestrator.aee2_shim import ClaudeCodeRuntimeAdapter
    except Exception:  # noqa: BLE001 - defensive
        return
    try:
        adapter_registry.register(ClaudeCodeRuntimeAdapter(), replace=True)
    except Exception:  # noqa: BLE001 - defensive
        pass


# ---------------------------------------------------------------------------
# WorkerRegistry — AEE-2 placeholder
# ---------------------------------------------------------------------------


@dataclass
class WorkerRecord:
    """In-memory representation of a registered worker (AEE-2)."""

    worker_id: str
    worker_name: str
    worker_type: str
    hostname: Optional[str] = None
    capabilities: List[str] = field(default_factory=list)
    workdir_allowlist: List[str] = field(default_factory=list)
    max_concurrent: int = 1
    registered_at: str = field(default_factory=_utcnow_iso)
    last_heartbeat_at: Optional[str] = None
    last_job_id: Optional[str] = None


class WorkerRegistry:
    """In-memory worker registry used by AEE-2 `POST /jobs/claim`."""

    def __init__(self) -> None:
        self._workers: Dict[str, WorkerRecord] = {}
        self._lock = threading.Lock()

    def register(self, record: WorkerRecord) -> None:
        with self._lock:
            self._workers[record.worker_id] = record

    def heartbeat(self, worker_id: str, job_id: Optional[str] = None) -> WorkerRecord:
        with self._lock:
            record = self._workers.get(worker_id)
            if record is None:
                raise KeyError(f"worker {worker_id!r} not registered")
            record.last_heartbeat_at = _utcnow_iso()
            if job_id is not None:
                record.last_job_id = job_id
            return record

    def get(self, worker_id: str) -> WorkerRecord:
        with self._lock:
            record = self._workers.get(worker_id)
            if record is None:
                raise KeyError(f"worker {worker_id!r} not registered")
            return record

    def all(self) -> Iterable[WorkerRecord]:
        with self._lock:
            return list(self._workers.values())


# Module-level singleton (used by tests; production wires a
# persistence-backed one in AEE-2).
worker_registry = WorkerRegistry()


__all__ = [
    "AdapterRegistry",
    "adapter_registry",
    "bootstrap_defaults",
    "WorkerRecord",
    "WorkerRegistry",
    "worker_registry",
]
