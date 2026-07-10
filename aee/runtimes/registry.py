"""AEE-5 Runtime Registry — service layer.

The public API the rest of AEE uses. Sits on top of a
`RuntimeRepository`; tests can pass
`InMemoryRuntimeRepository` to keep unit tests DB-free.

Concurrency
-----------
All write operations take a process-wide
`threading.Lock`. Reads (`get`, `list`, `find_*`) lock
briefly for consistent iteration; the underlying SQLite
read is the actual atomic operation. This is sufficient
for the AEE-5 single-process FastAPI bridge; a multi-
process deployment would need a real lock manager.

Persistence
-----------
The default singleton uses `SqliteRuntimeRepository` so
Runtime metadata survives restarts. Built-in Runtimes
are auto-registered at app startup via
`bootstrap_default_runtimes()` (called from
`aee/__init__.py` when the AEE package is first
imported, mirroring the existing
`bootstrap_defaults()` pattern for adapter_registry).
"""
from __future__ import annotations

import threading
from typing import Dict, Iterable, List, Optional

from .errors import (
    RuntimeNotFoundError,
    RuntimeRegistryError,
    RuntimeValidationError,
)
from .health import is_health_status
from .models import (
    DispatchRecord,
    DispatchStatus,
    RuntimeDescriptor,
    RuntimeHealthStatus,
    TaskRuntimeRequirements,
)
from .repository import (
    InMemoryRuntimeRepository,
    RuntimeRepository,
    SqliteRuntimeRepository,
)


def _validate_descriptor(descriptor: RuntimeDescriptor) -> None:
    """Lightweight validation: required fields, value ranges.

    Raises `RuntimeValidationError` with a human-readable
    message; the API layer maps this to a 400 response.
    """
    if not descriptor.runtime_id or not descriptor.runtime_id.strip():
        raise RuntimeValidationError(
            "runtime_id is required and must be a non-empty string"
        )
    if not descriptor.runtime_type or not descriptor.runtime_type.strip():
        raise RuntimeValidationError(
            "runtime_type is required and must be a non-empty string"
        )
    if not isinstance(descriptor.capabilities, type(descriptor.capabilities)):
        # Defensive: a malformed dataclass replaced the field
        raise RuntimeValidationError("capabilities must be a list of strings")
    if not isinstance(descriptor.labels, dict):
        raise RuntimeValidationError("labels must be a dict of string->string")
    if not is_health_status(descriptor.health.status):
        raise RuntimeValidationError(
            f"health.status must be one of "
            f"{RuntimeHealthStatus.ALL}, got {descriptor.health.status!r}"
        )
    if descriptor.limits.max_concurrency < 1:
        raise RuntimeValidationError("limits.max_concurrency must be >= 1")
    if descriptor.limits.timeout_seconds < 1:
        raise RuntimeValidationError("limits.timeout_seconds must be >= 1")


class RuntimeRegistry:
    """Service layer over a `RuntimeRepository`.

    Methods
    -------
    * `register_runtime` — register a new Runtime (idempotency
      controlled by `replace`).
    * `unregister_runtime` — remove by id.
    * `get_runtime` — fetch by id.
    * `list_runtimes` — list with optional filters.
    * `update_runtime` — replace the descriptor.
    * `set_runtime_enabled` — flip the `enabled` flag.
    * `find_runtimes_by_capability` — Runtimes that have the
      given capability.
    * `find_runtimes_by_labels` — Runtimes whose labels are
      a *superset* of the given labels.
    * `update_runtime_health` — set the health status.
    * `check_runtime_health` — read the current health.
    * `list_healthy_runtimes` — dispatchable (per the policy).
    """

    def __init__(self, repository: Optional[RuntimeRepository] = None) -> None:
        self._repo: RuntimeRepository = repository or SqliteRuntimeRepository()
        self._lock = threading.Lock()
        # Cache: name -> set of normalised caps. Rebuilt
        # on first read; populated on every write. The
        # cache is just a performance optimization —
        # the source of truth is the repository.
        #
        # We DO NOT call `_rebuild_caches()` here. The
        # dispatcher.db.get_conn() chain can be
        # transitively reached (e.g. when this module
        # is imported as part of the dispatcher's
        # `_init_schema` migration), and triggering a
        # `get_conn` call from inside another `get_conn`
        # call (e.g. while holding `_init_lock`) would
        # deadlock. The cache is built lazily on first
        # read, and rebuilt on every write.
        self._cap_cache: Dict[str, set] = {}
        self._label_cache: Dict[str, Dict[str, str]] = {}
        self._caches_built = False

    # ---- Public API ----------------------------------------------------

    def register_runtime(
        self, descriptor: RuntimeDescriptor, *, replace: bool = False
    ) -> RuntimeDescriptor:
        with self._lock:
            _validate_descriptor(descriptor)
            existing = self._repo.get(descriptor.runtime_id)
            if existing is not None and not replace:
                raise RuntimeRegistryError(
                    f"runtime_id {descriptor.runtime_id!r} already "
                    f"registered; pass replace=True to override"
                )
            descriptor.capabilities = type(descriptor.capabilities)(
                descriptor.capabilities.normalized()
            )
            if existing is None:
                self._repo.insert(descriptor)
            else:
                self._repo.update(descriptor)
            self._refresh_cache_for(descriptor)
        return descriptor

    def unregister_runtime(self, runtime_id: str) -> bool:
        with self._lock:
            ok = self._repo.delete(runtime_id)
            if ok:
                self._cap_cache.pop(runtime_id, None)
                self._label_cache.pop(runtime_id, None)
            return ok

    def get_runtime(self, runtime_id: str) -> RuntimeDescriptor:
        with self._lock:
            r = self._repo.get(runtime_id)
            if r is None:
                raise RuntimeNotFoundError(
                    f"runtime_id {runtime_id!r} not found",
                    evaluated_runtimes=[
                        {"runtime_id": runtime_id, "rejected_reasons": ["not registered"]}
                    ],
                )
            return r

    def list_runtimes(
        self,
        *,
        enabled: Optional[bool] = None,
        runtime_type: Optional[str] = None,
    ) -> List[RuntimeDescriptor]:
        with self._lock:
            return self._repo.list_all(
                enabled=enabled, runtime_type=runtime_type
            )

    def update_runtime(
        self, runtime_id: str, updates: Dict
    ) -> RuntimeDescriptor:
        with self._lock:
            existing = self._repo.get(runtime_id)
            if existing is None:
                raise RuntimeNotFoundError(
                    f"runtime_id {runtime_id!r} not found",
                    evaluated_runtimes=[
                        {"runtime_id": runtime_id, "rejected_reasons": ["not registered"]}
                    ],
                )
            payload = existing.to_dict()
            for k, v in updates.items():
                if v is None:
                    continue
                if k in ("capabilities", "labels", "limits", "health"):
                    # Nested objects are replaced wholesale; the
                    # caller is expected to pass a fully-formed
                    # sub-dict (e.g. `{"capabilities": [...]}`).
                    payload[k] = v
                else:
                    payload[k] = v
            new_descriptor = RuntimeDescriptor.from_dict(payload)
            new_descriptor.runtime_id = existing.runtime_id  # immutable
            new_descriptor.registered_at = existing.registered_at
            new_descriptor.updated_at = None
            self._repo.update(new_descriptor)
            self._refresh_cache_for(new_descriptor)
        return new_descriptor

    def set_runtime_enabled(
        self, runtime_id: str, enabled: bool
    ) -> RuntimeDescriptor:
        with self._lock:
            existing = self._repo.get(runtime_id)
            if existing is None:
                raise RuntimeNotFoundError(
                    f"runtime_id {runtime_id!r} not found",
                    evaluated_runtimes=[
                        {"runtime_id": runtime_id, "rejected_reasons": ["not registered"]}
                    ],
                )
            self._repo.set_enabled(runtime_id, bool(enabled))
            existing.enabled = bool(enabled)
            existing.updated_at = None
        return existing

    def find_runtimes_by_capability(
        self, capability: str
    ) -> List[RuntimeDescriptor]:
        with self._lock:
            self._ensure_caches()
            target = (capability or "").strip().lower()
            if not target:
                return []
            all_rts = self._repo.list_all()
            return [r for r in all_rts if target in self._cap_cache.get(r.runtime_id, set())]

    def find_runtimes_by_labels(
        self, labels: Dict[str, str]
    ) -> List[RuntimeDescriptor]:
        with self._lock:
            self._ensure_caches()
            if not labels:
                return self._repo.list_all()
            wanted = {
                str(k).strip().lower(): str(v).strip().lower()
                for k, v in labels.items()
            }
            out: List[RuntimeDescriptor] = []
            for r in self._repo.list_all():
                rt_labels = self._label_cache.get(r.runtime_id, {})
                norm = {k.strip().lower(): str(v).strip().lower() for k, v in rt_labels.items()}
                if all(norm.get(k) == v for k, v in wanted.items()):
                    out.append(r)
            return out

    def update_runtime_health(
        self,
        runtime_id: str,
        status: str,
        *,
        message: Optional[str] = None,
    ) -> RuntimeDescriptor:
        if not is_health_status(status):
            raise RuntimeValidationError(
                f"health.status must be one of {RuntimeHealthStatus.ALL}, "
                f"got {status!r}"
            )
        with self._lock:
            existing = self._repo.get(runtime_id)
            if existing is None:
                raise RuntimeNotFoundError(
                    f"runtime_id {runtime_id!r} not found",
                    evaluated_runtimes=[
                        {"runtime_id": runtime_id, "rejected_reasons": ["not registered"]}
                    ],
                )
            self._repo.update_health(runtime_id, status, message=message)
            existing.health.status = status
            existing.health.message = message
            from .repository import _now_iso
            existing.health.last_checked_at = _now_iso()
        return existing

    def check_runtime_health(self, runtime_id: str) -> Dict:
        with self._lock:
            r = self._repo.get(runtime_id)
            if r is None:
                raise RuntimeNotFoundError(
                    f"runtime_id {runtime_id!r} not found",
                    evaluated_runtimes=[
                        {"runtime_id": runtime_id, "rejected_reasons": ["not registered"]}
                    ],
                )
            return r.health.to_dict()

    def list_healthy_runtimes(
        self, *, allow_unknown_health: bool = True
    ) -> List[RuntimeDescriptor]:
        with self._lock:
            from .health import is_dispatchable
            return [
                r
                for r in self._repo.list_all(enabled=True)
                if is_dispatchable(r.health.status, allow_unknown_health=allow_unknown_health)
            ]

    # ---- Dispatch record helpers (thin wrappers over the repo) -------

    def record_dispatch(self, record: DispatchRecord) -> None:
        with self._lock:
            self._repo.insert_dispatch_record(record)

    def update_dispatch_status(
        self,
        dispatch_id: str,
        status: str,
        *,
        failure_code: Optional[str] = None,
        failure_message: Optional[str] = None,
    ) -> bool:
        with self._lock:
            return self._repo.update_dispatch_status(
                dispatch_id,
                status,
                failure_code=failure_code,
                failure_message=failure_message,
            )

    def list_dispatches(
        self,
        *,
        task_id: Optional[str] = None,
        runtime_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[DispatchRecord]:
        with self._lock:
            return self._repo.list_dispatch_records(
                task_id=task_id, runtime_id=runtime_id, limit=limit
            )

    # ---- Internals ----------------------------------------------------

    def _ensure_caches(self) -> None:
        """Build the capability / label caches on first read.

        We do this lazily (not in `__init__`) to avoid a
        `get_conn()` call at module import time. The
        dispatcher.db migration path can reach this
        module's `__init__.py`, which constructs
        `RuntimeRegistry()`; if the constructor calls
        `get_conn` we'd deadlock against the dispatcher's
        own `_init_lock`.
        """
        if self._caches_built:
            return
        self._cap_cache.clear()
        self._label_cache.clear()
        for r in self._repo.list_all():
            self._refresh_cache_for(r)
        self._caches_built = True

    def _rebuild_caches(self) -> None:
        self._caches_built = False
        self._ensure_caches()

    def _refresh_cache_for(self, r: RuntimeDescriptor) -> None:
        self._cap_cache[r.runtime_id] = set(r.capabilities.normalized())
        self._label_cache[r.runtime_id] = {str(k): str(v) for k, v in r.labels.items()}


# Module-level singleton used by the rest of AEE. Mirrors
# the `adapter_registry` pattern in `aee.core.registry`.
runtime_registry = RuntimeRegistry()


def bootstrap_default_runtimes(
    force: bool = False,
    *,
    default_runtime_id: str = "aee-lightweight-local",
) -> None:
    """Register the built-in `aee-lightweight-local` Runtime.

    Called once at app startup; idempotent. If
    `force=True`, re-registers even when the descriptor
    already exists (used by the config-loader for an
    explicit settings file).

    The built-in descriptor mirrors the existing AEE-4
    runtime contract: `runtime_type="aee_lightweight"`,
    capabilities `runtime.aee_runtime` + the four tool
    capabilities the AEE-4 worker reports to the
    bridge (`tool.shell`, `tool.python`, `tool.git`,
    `tool.filesystem`).
    """
    from .builtins.aee_lightweight import build_default_descriptor

    descriptor = build_default_descriptor(default_runtime_id=default_runtime_id)
    existing_ids = {r.runtime_id for r in runtime_registry.list_runtimes()}
    if descriptor.runtime_id in existing_ids and not force:
        return
    runtime_registry.register_runtime(descriptor, replace=force)


__all__ = [
    "RuntimeRegistry",
    "runtime_registry",
    "bootstrap_default_runtimes",
]
