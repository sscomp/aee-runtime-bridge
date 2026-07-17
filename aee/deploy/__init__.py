"""AEE Epic 9.6 §21.6 — Provider-Neutral Deployment Profiles.

This package implements the Provider-Neutral Deployment Contract
defined in Master Plan §21.6. The contract decomposes the deployment
surface into four explicitly separated layers so that a change of host
does not ripple into the runtime:

    1. AEE Core            — provider-agnostic runtime (no cloud SDK)
    2. Runtime Profile     — full / mini / edge / developer (§21.1)
    3. Deployment Contract — Host Capability Document + Adapter Manifest
    4. Platform Adapter    — pluggable edge component

Adding a new deployment host requires writing (or reusing) a Platform
Adapter and emitting a Host Capability Document — **not** modifying
AEE Core, the Runtime Profile, or the Docker image.

Public surface (re-exported):

* :class:`HostCapabilities`      — canonical capability document model
* :class:`ResourceFloor`         — profile-to-resource minimum mapping
* :class:`MaterializationResult` — adapter materialize() return type
* :class:`HealthStatus`         — adapter health_check() return type
* :class:`PlatformAdapter`      — adapter Protocol (§21.6.D)
* :class:`ContractError`        — base error
* :class:`ContractValidationError` — validation failure
* :func:`validate_capabilities`  — validate a HostCapabilities instance
* :func:`select_adapter`         — adapter selection by host class
* :func:`register_adapter`      — register a Platform Adapter
* :data:`RESOURCE_FLOOR_BY_PROFILE` — the §21.6.C resource mapping table
* :data:`KNOWN_HOST_CLASSES`    — supported host class values
* :data:`REFERENCE_ADAPTERS`    — canonical reference adapter names

Design invariants (§21.6 contract):

1. **No cloud SDK imports.** This package imports only stdlib and the
   canonical AEE profile descriptor. It does **not** import ``boto3``,
   ``google-cloud-*``, ``azure-*``, ``terraform``, or any IaC tool.
2. **``provider_hint`` is informational only.** No function in this
   package branches on ``provider_hint``. Selection uses ``class``.
3. **Adapter isolation.** Each Platform Adapter lives in
   ``aee/deploy/adapters/`` and is the only place host-specific
   knowledge lives. AEE Core is adapter-agnostic.
4. **Terraform is optional.** The ``terraform-aws`` adapter is a
   placeholder that documents the optional path; it is never a
   runtime dependency of AEE Core.
5. **No auto-provisioning.** Adapters for hosts without a
   programmatic API (B2 launchd, generic Docker host) document
   this and decline ``materialize()`` rather than fabricating
   resources they cannot actually create.

See Master Plan §21.6.A–§21.6.G for the authoritative contract.
"""
from __future__ import annotations

from aee.deploy.contract import (
    ContractError,
    ContractValidationError,
    HealthStatus,
    HostCapabilities,
    MaterializationResult,
    ResourceFloor,
    RESOURCE_FLOOR_BY_PROFILE,
    KNOWN_HOST_CLASSES,
    REFERENCE_ADAPTERS,
    validate_capabilities,
    validate_resource_floor,
)
from aee.deploy.adapters.base import PlatformAdapter
from aee.deploy.registry import (
    AdapterRegistry,
    AdapterNotFoundError,
    select_adapter,
    register_adapter,
    get_registry,
)

__all__ = [
    "HostCapabilities",
    "ResourceFloor",
    "MaterializationResult",
    "HealthStatus",
    "PlatformAdapter",
    "ContractError",
    "ContractValidationError",
    "validate_capabilities",
    "select_adapter",
    "register_adapter",
    "AdapterRegistry",
    "AdapterNotFoundError",
    "get_registry",
    "RESOURCE_FLOOR_BY_PROFILE",
    "KNOWN_HOST_CLASSES",
    "REFERENCE_ADAPTERS",
]