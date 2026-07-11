"""AEE-7.1 provider factory + runtime-typed registry.

Maps a ``RuntimeDescriptor`` (selected by the existing AEE-5
``RuntimeSelector``) to a concrete :class:`Provider`
implementation.

The registry is *runtime-typed* (keyed by ``runtime_type``) and
*lazy* — providers are only imported when the dispatcher actually
needs them, so a host without the ``claude`` CLI does not pay
the import cost.

Built-in registrations (always present):

* ``runtime_type="aee_lightweight"`` and ``"hermes"`` →
  :class:`aee.orchestrator.hermes_provider.HermesRuntimeAdapterProvider`
  (wraps the AEE-2 ``HermesAdapter``; works for both names because
  the legacy dispatcher creates tasks with
  ``runtime_type="hermes"`` by default).
* ``runtime_type="claude_code"`` →
  :class:`aee.orchestrator.claude_code_provider_shim.ClaudeCodeExecProvider`
  (wraps the AEE-6 ``ClaudeCodeProvider``). The import is
  guarded: if the AEE-6 module is not present (e.g. a future
  slim install), the provider is **not** registered, and a
  later ``provider_for_descriptor("claude_code", ...)`` call
  raises :class:`ProviderNotFoundError` instead of crashing
  the import.
* ``runtime_type="fake"`` →
  :class:`aee.orchestrator.fake_provider.FakeProvider`.
  Always registered; the test suite uses it to inject
  pluggable ``behavior`` (success / failing / hanging) without
  any subprocess or HTTP traffic.
"""
from __future__ import annotations

import threading
from typing import Callable, Dict, List, Optional, TYPE_CHECKING

from aee.runtimes.models import RuntimeDescriptor

from .provider import Provider, ProviderNotFoundError

if TYPE_CHECKING:  # pragma: no cover
    from aee.runtimes.repository import RuntimeRepository


# Type alias for the factory callable. Each factory receives a
# RuntimeDescriptor (the *selected* one) and returns a Provider
# implementation. This indirection lets tests and ops replace
# the live provider with a fake one without touching the
# orchestrator.
ProviderFactory = Callable[[RuntimeDescriptor], "Provider"]


# ---------------------------------------------------------------------------
# Module-level registry (thread-safe)
# ---------------------------------------------------------------------------


class _ProviderRegistry:
    """Thread-safe map of ``runtime_type`` → :class:`ProviderFactory`."""

    def __init__(self) -> None:
        self._factories: Dict[str, ProviderFactory] = {}
        self._lock = threading.Lock()

    def register(self, runtime_type: str, factory: ProviderFactory, *, replace: bool = False) -> None:
        rt = (runtime_type or "").strip()
        if not rt:
            raise ValueError("runtime_type must be a non-empty string")
        with self._lock:
            existing = self._factories.get(rt)
            if existing is not None and not replace:
                raise ValueError(
                    f"provider for runtime_type={rt!r} already registered; "
                    "pass replace=True to override"
                )
            self._factories[rt] = factory

    def unregister(self, runtime_type: str) -> None:
        with self._lock:
            self._factories.pop(runtime_type, None)

    def get(self, runtime_type: str) -> ProviderFactory:
        with self._lock:
            factory = self._factories.get(runtime_type)
            if factory is None:
                raise ProviderNotFoundError(
                    f"no provider factory registered for runtime_type={runtime_type!r}; "
                    f"known={sorted(self._factories)}"
                )
            return factory

    def types(self) -> List[str]:
        with self._lock:
            return sorted(self._factories)


_registry = _ProviderRegistry()


def register_provider(
    runtime_type: str, factory: ProviderFactory, *, replace: bool = False
) -> None:
    """Public API: register a provider factory for a runtime_type."""
    _registry.register(runtime_type, factory, replace=replace)


def unregister_provider(runtime_type: str) -> None:
    """Public API: remove a provider factory registration."""
    _registry.unregister(runtime_type)


def registered_providers() -> List[str]:
    """Public API: list the runtime_types currently registered."""
    return _registry.types()


# ---------------------------------------------------------------------------
# Default registrations
# ---------------------------------------------------------------------------


def _build_hermes_provider(descriptor: RuntimeDescriptor) -> "Provider":
    """Factory for the Hermes HTTP provider (AEE-2 wrap)."""
    # Late import: keeps the orchestrator package importable even
    # if the AEE-2 hermes_adapter module is unavailable.
    from .hermes_provider import HermesRuntimeAdapterProvider

    return HermesRuntimeAdapterProvider(descriptor=descriptor)


def _build_claude_code_provider(descriptor: RuntimeDescriptor) -> "Provider":
    """Factory for the Claude Code subprocess provider (AEE-6 wrap)."""
    # Late import: AEE-6 may be optional in slim installs. The
    # guard at register time is below.
    from .claude_code_provider_shim import ClaudeCodeExecProvider

    return ClaudeCodeExecProvider(descriptor=descriptor)


def _build_fake_provider(descriptor: RuntimeDescriptor) -> "Provider":
    """Factory for the in-memory test provider."""
    from .fake_provider import FakeProvider

    return FakeProvider(descriptor=descriptor)


# Register defaults. The hermes + fake entries are always present.
# claude_code is best-effort: if the AEE-6 import fails, the
# orchestrator still loads and a later dispatch to claude_code
# raises ProviderNotFoundError at the call site, not at module
# import.
try:
    register_provider("aee_lightweight", _build_hermes_provider)
    register_provider("hermes", _build_hermes_provider)
except Exception:  # pragma: no cover - defensive
    pass

try:
    register_provider("claude_code", _build_claude_code_provider)
except Exception:  # pragma: no cover - import guard
    # The AEE-6 module is unavailable on this host (e.g. slim
    # install). Future dispatch to runtime_type=claude_code will
    # raise ProviderNotFoundError; that's the contract.
    pass

try:
    register_provider("fake", _build_fake_provider)
except Exception:  # pragma: no cover - defensive
    pass


# ---------------------------------------------------------------------------
# Public factory entry point
# ---------------------------------------------------------------------------


def provider_for_descriptor(
    descriptor: RuntimeDescriptor,
    *,
    repository: Optional["RuntimeRepository"] = None,
) -> "Provider":
    """Return a concrete :class:`Provider` for the given descriptor.

    The descriptor's ``runtime_type`` is the lookup key. The
    ``repository`` argument is currently unused at this layer (the
    Provider receives the repository through the orchestrator's
    ``submit()`` call), but is kept in the signature so AEE-7.2
    can pass an explicit repository without a signature change.

    Raises
    ------
    ProviderNotFoundError
        No factory is registered for ``descriptor.runtime_type``.
    """
    factory = _registry.get(descriptor.runtime_type)
    return factory(descriptor)


__all__ = [
    "ProviderFactory",
    "register_provider",
    "unregister_provider",
    "registered_providers",
    "provider_for_descriptor",
]
