"""AEE Runtime + Platform Adapter — Phase 1: bootstrap package.

This package is a thin bootstrap (plan §5): it resolves the
:class:`~aee.deploy.capabilities.PlatformCapabilities` for the current
process once at start and caches it. It does **not** duplicate the
adapter registry; it imports
:func:`aee.deploy.registry.get_registry` and
:func:`aee.deploy.loader.load_host_capabilities`. This keeps §21.6 as
the only adapter registry and avoids a second source of truth.

Public surface (re-exported):

* :func:`get_capabilities`         — process-wide cached facade.
* :func:`resolve_capabilities`     — explicit resolver (tests / injection).
* :class:`PlatformIdentity`        — LINUX / MACOS / UNKNOWN enum.
* :func:`resolve_platform_identity` — deterministic platform detection.
"""
from __future__ import annotations

from aee.platform.current import (
    PlatformIdentity,
    get_capabilities,
    resolve_capabilities,
    resolve_platform_identity,
)

__all__ = [
    "PlatformIdentity",
    "get_capabilities",
    "resolve_capabilities",
    "resolve_platform_identity",
]