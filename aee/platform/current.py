"""AEE Runtime + Platform Adapter — Phase 1: cached bootstrap resolver.

This module is the bootstrap (plan §5 + §7 + §8). On process start it
resolves the :class:`~aee.deploy.capabilities.PlatformCapabilities` for
the current host via the §21.6 adapter registry + loader, then caches it
as a module-level singleton. Core code reads the cached facade; it never
mutates.

Design invariants:

* **Deterministic platform identity.**
  :func:`resolve_platform_identity` is the *only* place ``sys.platform``
  is read. Everything else consumes the :class:`PlatformIdentity` enum
  or the facade. Tests inject a fake ``sys.platform`` (via
  ``unittest.mock.patch``) or pass ``platform_id`` explicitly to
  :func:`resolve_capabilities`.
* **No second source of truth.** The resolver imports
  :func:`aee.deploy.registry.get_registry` and
  :func:`aee.deploy.loader.load_host_capabilities`. It does not
  re-implement adapter selection.
* **Linux preserves current behavior.** On Linux the resolver selects
  the ``abacus`` adapter, calls ``detect()``, and builds a facade that
  is byte-identical to :data:`~aee.deploy.capabilities.LinuxDefaults`
  (plan §7 + §9.2).
* **macOS skeleton is honest.** On macOS the resolver selects the
  ``macbook`` adapter and calls ``detect()``. On a real Mac
  ``sys.platform == "darwin"`` and the adapter returns
  ``os='darwin'`` naturally. On a Linux host the adapter returns
  ``os='linux'`` — the macOS contract tests therefore inject a
  synthetic :class:`~aee.deploy.contract.HostCapabilities` with
  ``os='darwin'`` via a stubbed adapter rather than calling ``detect()``
  on Linux. No host-specific command is executed (no ``launchctl`` /
  ``sw_vers``); the adapter is information-only per §21.6.E.
* **Unknown platforms fail safely.** ``PlatformIdentity.UNKNOWN``
  resolves to :data:`~aee.deploy.capabilities.UnknownDefaults` — a
  frozen, explicitly limited capability set. The resolver raises no
  exception; callers decide whether to refuse work.
* **No circular imports.** This module imports
  :mod:`aee.deploy.capabilities`, :mod:`aee.deploy.registry`, and
  :mod:`aee.deploy.loader` — all import-clean at baseline.
"""
from __future__ import annotations

import enum
import sys
from typing import Optional

from aee.deploy.capabilities import (
    LinuxDefaults,
    MacOSDefaults,
    PlatformCapabilities,
    UnknownDefaults,
    from_capabilities,
)
from aee.deploy.contract import HostCapabilities
from aee.deploy.loader import load_host_capabilities
from aee.deploy.registry import AdapterNotFoundError, get_registry


# ---------------------------------------------------------------------------
# Platform identity (deterministic detection)
# ---------------------------------------------------------------------------


class PlatformIdentity(enum.Enum):
    """Deterministic platform identity for the current process.

    The only place ``sys.platform`` is read is
    :func:`resolve_platform_identity`. Everything else consumes this
    enum or the :class:`PlatformCapabilities` facade.
    """

    LINUX = "linux"
    MACOS = "darwin"
    WINDOWS = "windows"
    UNKNOWN = "unknown"


def resolve_platform_identity(platform: Optional[str] = None) -> PlatformIdentity:
    """Return the :class:`PlatformIdentity` for ``platform``.

    If ``platform`` is ``None``, reads ``sys.platform``. This is the
    *only* ``sys.platform`` read site in the bootstrap. Tests inject a
    fake ``sys.platform`` (via ``unittest.mock.patch``) or pass
    ``platform`` explicitly.

    Mapping:

    * ``"linux"`` / ``"linux2"`` → :data:`PlatformIdentity.LINUX`
    * ``"darwin"``              → :data:`PlatformIdentity.MACOS`
    * ``"win32"`` / ``"cygwin"`` / ``"msys"`` → :data:`PlatformIdentity.WINDOWS`
    * anything else             → :data:`PlatformIdentity.UNKNOWN`
    """
    p = platform if platform is not None else sys.platform
    if p in ("linux", "linux2"):
        return PlatformIdentity.LINUX
    if p == "darwin":
        return PlatformIdentity.MACOS
    if p in ("win32", "cygwin", "msys"):
        return PlatformIdentity.WINDOWS
    return PlatformIdentity.UNKNOWN


# ---------------------------------------------------------------------------
# Adapter name resolution by platform identity
# ---------------------------------------------------------------------------


#: Maps :class:`PlatformIdentity` → default adapter name in the §21.6
#: registry. ``UNKNOWN`` and ``WINDOWS`` map to ``None``: ``UNKNOWN``
#: because there is no adapter; ``WINDOWS`` because the Windows adapter
#: is a **skeleton** (W1, §16) — per §17.3 Phase C "Windows runs in
#: UNKNOWN capability mode; first-class support waits on the Windows
#: adapter". Operators who want the skeleton pass ``--adapter windows``
#: explicitly; the resolver otherwise returns
#: :data:`UnknownDefaults` for ``PlatformIdentity.WINDOWS`` so the
#: installer surfaces a clear "Windows is experimental" message rather
#: than pretending to provision.
_DEFAULT_ADAPTER_BY_IDENTITY = {
    PlatformIdentity.LINUX: "abacus",
    PlatformIdentity.MACOS: "macbook",
    PlatformIdentity.WINDOWS: None,
    PlatformIdentity.UNKNOWN: None,
}


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def resolve_capabilities(
    *,
    platform_id: Optional[PlatformIdentity] = None,
    platform: Optional[str] = None,
    adapter_name: Optional[str] = None,
    cap_path: Optional[str] = None,
    cap_override: Optional[HostCapabilities] = None,
) -> PlatformCapabilities:
    """Resolve a :class:`PlatformCapabilities` for the given inputs.

    This is the explicit resolver used by tests and by
    :func:`get_capabilities`. It does not mutate the process-wide cache.

    Resolution order:

    1. Determine the platform identity. If ``platform_id`` is supplied,
       use it. Otherwise call
       :func:`resolve_platform_identity(platform)` (which reads
       ``sys.platform`` when ``platform`` is ``None``).
    2. Determine the adapter name. If ``adapter_name`` is supplied, use
       it. Otherwise use
       ``_DEFAULT_ADAPTER_BY_IDENTITY[platform_id]``.
    3. Determine the :class:`HostCapabilities`:

       * If ``cap_override`` is supplied, use it (tests inject a
         synthetic document — no host probing).
       * Else if ``cap_path`` is supplied, load it via
         :func:`aee.deploy.loader.load_host_capabilities`.
       * Else if an adapter name was resolved, call
         ``adapter.detect()``.
       * Else (unknown platform, no override, no path, no adapter)
         return :data:`UnknownDefaults`.

    4. Build the facade via
       :func:`aee.deploy.capabilities.from_capabilities`.

    On unknown platforms the resolver returns
    :data:`UnknownDefaults` **without raising**. Callers decide whether
    to refuse work (acceptance criterion "fail safely or return an
    explicitly limited capability set").

    Raises :class:`aee.deploy.registry.AdapterNotFoundError` if an
    explicit ``adapter_name`` is supplied that is not registered.
    """
    if platform_id is None:
        platform_id = resolve_platform_identity(platform)

    if adapter_name is None:
        adapter_name = _DEFAULT_ADAPTER_BY_IDENTITY.get(platform_id)

    if cap_override is not None:
        return from_capabilities(cap_override)

    if cap_path is not None:
        cap = load_host_capabilities(cap_path)
        return from_capabilities(cap)

    if adapter_name is None:
        # Unknown platform with no override and no path → safe fallback.
        return UnknownDefaults

    registry = get_registry()
    try:
        adapter = registry.select(adapter_name=adapter_name)
    except AdapterNotFoundError:
        # Re-raise for an explicit adapter_name that is not registered;
        # for the default mapping (None → UnknownDefaults already
        # returned above) this branch is unreachable.
        raise
    cap = adapter.detect()
    return from_capabilities(cap)


# ---------------------------------------------------------------------------
# Process-wide cached singleton
# ---------------------------------------------------------------------------


_cached: Optional[PlatformCapabilities] = None


def get_capabilities(
    *, force_refresh: bool = False, **resolver_kwargs
) -> PlatformCapabilities:
    """Return the process-wide cached :class:`PlatformCapabilities`.

    On the first call the facade is resolved via
    :func:`resolve_capabilities` and cached. Subsequent calls return the
    cached instance. Pass ``force_refresh=True`` to re-resolve (tests
    only; production never refreshes — plan §6.1 "cached for the process
    lifetime").

    Extra ``resolver_kwargs`` are forwarded to
    :func:`resolve_capabilities` on the first call (or when
    ``force_refresh`` is set). They are ignored on subsequent cached
    calls.
    """
    global _cached
    if _cached is None or force_refresh:
        _cached = resolve_capabilities(**resolver_kwargs)
    return _cached


def _reset_cache() -> None:
    """Clear the process-wide cache (test helper; not public API)."""
    global _cached
    _cached = None


__all__ = [
    "PlatformIdentity",
    "resolve_platform_identity",
    "resolve_capabilities",
    "get_capabilities",
]