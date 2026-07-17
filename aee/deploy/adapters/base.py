"""AEE Epic 9.6 §21.6.D — Platform Adapter Interface.

A Platform Adapter is a Python class implementing the minimal
interface below. The interface is the **only** coupling between AEE
Core and the host; everything else is contract data.

Per §21.6.D a Platform Adapter MUST NOT:

* import cloud SDKs into AEE Core (it lives in
  ``aee/deploy/adapters/``, not in core)
* mutate AEE Core source, the Docker image, or the profile descriptor
* branch on ``provider_hint``
* assume inbound connectivity is available (the default is egress-only)

A Platform Adapter MAY:

* shell out to host-native tooling (``supervisorctl``, ``launchctl``,
  ``docker``, ``terraform``)
* write a host-specific supervisor unit file
* write a host-specific tunnel config

This module only defines the :class:`PlatformAdapter` Protocol. The
contract data models (``HostCapabilities``, ``MaterializationResult``,
``HealthStatus``) live in :mod:`aee.deploy.contract` so that adapter
implementations and the registry can import them without circular
dependencies.

The Protocol is ``runtime_checkable`` so the registry can verify
adapters structurally without inheriting from a base class.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from aee.deploy.contract import (
    HealthStatus,
    HostCapabilities,
    MaterializationResult,
)


@runtime_checkable
class PlatformAdapter(Protocol):
    """Minimal Platform Adapter interface (§21.6.D).

    Each adapter declares:

    * ``name`` — the adapter identifier (e.g. ``"abacus"``,
      ``"zo"``, ``"macbook"``, ``"docker"``, ``"terraform-aws"``).
    * ``detect()`` — probe the host and return a populated
      :class:`HostCapabilities` document. Called by ``install.sh``
      when no ``host.capabilities.yaml`` is supplied.
    * ``materialize(profile, cap)`` — translate the contract into
      host-native primitives for the given Runtime Profile. Returns
      a :class:`MaterializationResult` describing the processes,
      ports, persistent paths, and supervisor units the host should
      keep alive. MUST NOT modify AEE Core.
    * ``health_check(profile)`` — return ``ONLINE`` / ``STARTING`` /
      ``OFFLINE`` / ``UNKNOWN`` for the running deployment. Used by
      the keepalive cron pattern (see external-service-keepalive
      skill).

    Implementations are free to add host-specific helper methods; the
    Protocol only constrains the four members above.
    """

    name: str

    def detect(self) -> HostCapabilities:
        """Probe the host and return a populated HostCapabilities.

        Implementations SHOULD set ``detected=True`` and
        ``source="detected"`` on the returned document. They MUST NOT
        mutate AEE Core, write to the DB, or start processes. The
        return value is a snapshot; callers should not assume it
        stays accurate across host resets.
        """
        ...

    def materialize(
        self, profile: str, cap: HostCapabilities
    ) -> MaterializationResult:
        """Translate the contract into host-native primitives.

        The result is declarative: it describes what the host should
        keep alive, not what the adapter started. The adapter MUST
        NOT modify AEE Core source, the Docker image, or the profile
        descriptor. If the host cannot satisfy the profile (e.g. B2
        cannot run ``profile=full`` per §21.6.C resource floor), the
        adapter returns a :class:`MaterializationResult` with
        ``declined=True`` and a ``decline_reason`` rather than
        raising.
        """
        ...

    def health_check(self, profile: str) -> HealthStatus:
        """Return the current health of the running deployment."""
        ...


__all__ = ["PlatformAdapter"]