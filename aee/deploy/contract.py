"""AEE Epic 9.6 §21.6 — Provider-Neutral Deployment Contract.

Canonical data models and validators for the Provider-Neutral
Deployment Contract (§21.6.A–§21.6.C). This module is **contract-only**;
Platform Adapter implementations live in ``aee/deploy/adapters/`` and
the registry / selection logic lives in ``aee/deploy/registry.py``.

Design contract (per Master Plan §21.6):

    * The Deployment Contract is the set of declarations a host must
      satisfy for a given Runtime Profile to be runnable; it is **not**
      a Terraform plan and is **not** a cloud API.
    * The contract is expressed as a declarative **Host Capability
      Document** plus an **Adapter Manifest**.
    * The ``provider_hint`` field is **informational only**. AEE Core
      and the installer MUST NOT branch on it. Selection uses
      ``class``.
    * The Profile-to-Resource mapping (§21.6.C) is defined once here
      and consumed by every Platform Adapter, so that
      "is this host big enough for ``profile=mini``?" has the same
      answer everywhere.
    * Adding a new host class requires writing (or reusing) a
      Platform Adapter and emitting a Host Capability Document —
      **not** modifying AEE Core or the Docker image.

Invariants (this module):

1. No cloud SDK imports. Only stdlib + the canonical AEE profile
   descriptor (``aee.profiles.descriptor``).
2. No mutation. The validators are pure functions over the data model.
3. ``provider_hint`` is never branched on by any code path.
4. The resource floor table is the single source of truth for
   "is this host big enough?" — adapters do not re-encode it.

See ``AEE_MASTER_PLAN.md`` §21.6.A–§21.6.G for the authoritative
contract wording.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

# Canonical AEE profile descriptor (the §21.1 source of truth for
# profile names + defaults). Imported read-only; this module does not
# mutate the descriptor.
from aee.profiles.descriptor import KNOWN_PROFILES, DEFAULT_PROFILE


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class ContractError(ValueError):
    """Base class for Deployment Contract violations (§21.6.A).

    All contract errors are subclasses of :class:`ValueError` so the
    installer (§21.3) can catch them uniformly without importing
    adapter-specific exception types.
    """


class ContractValidationError(ContractError):
    """Raised by :func:`validate_capabilities` when a Host Capability
    Document violates the §21.6.B schema or the §21.6.C resource
    floor.

    The ``field`` attribute carries the dotted field path that failed
    (e.g. ``"host.class"``, ``"runtime_profile.supported"``). The
    ``reason`` attribute carries the human-readable diagnostic. Both
    are also embedded in the str representation.
    """

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"[{field}] {reason}")


# ---------------------------------------------------------------------------
# Canonical host class values (§21.6.B)
# ---------------------------------------------------------------------------

#: The host classes the contract recognises. Drives adapter selection
#: via :func:`aee.deploy.registry.select_adapter`, **not**
#: ``provider_hint``. Adding a new class is a contract change (new
#: adapter + new capability document), not a runtime change.
KNOWN_HOST_CLASSES: Tuple[str, ...] = (
    "container",
    "laptop",
    "docker-host",
    "cloud-vm",
    "cloud-container",
)

#: The supervisor primitives the contract recognises. Each adapter
#: maps this to its host-native equivalent.
KNOWN_SUPERVISORS: Tuple[str, ...] = (
    "supervisord",
    "launchd",
    "systemd",
    "zo-native",
    "docker",
    "none",
)

#: The network egress modes the contract recognises.
KNOWN_EGRESS: Tuple[str, ...] = (
    "direct",
    "tunnel",
    "both",
    "none",
)

#: The filesystem modes the contract recognises.
KNOWN_FILESYSTEM: Tuple[str, ...] = (
    "posix",
    "windows-wsl",
)


# ---------------------------------------------------------------------------
# Profile-to-Resource mapping (§21.6.C — single source of truth)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceFloor:
    """Resource floor / ceiling pair for a single Runtime Profile.

    Per §21.6.C the floor is the **minimum** the host must provide;
    the ceiling is the **maximum recommended**. The installer
    (§21.3) validates the host against the floor for the requested
    profile and refuses to install with a clear error if the floor
    is not met — the same refusal path on every host, whether cloud
    or laptop.

    Fields mirror §21.6.C table columns. ``inbound_required`` is a
    boolean: ``True`` if the profile needs inbound (none of the four
    canonical profiles do, but the field is preserved for future
    cloud profiles that may).
    """

    profile: str
    cpu_floor: int            # vCPU count
    mem_mb_floor: int         # megabytes
    disk_mb_floor: int        # megabytes
    inbound_required: bool = False
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile": self.profile,
            "cpu_floor": self.cpu_floor,
            "mem_mb_floor": self.mem_mb_floor,
            "disk_mb_floor": self.disk_mb_floor,
            "inbound_required": self.inbound_required,
            "notes": self.notes,
        }


#: The §21.6.C resource floor table. Defined once here and consumed by
#: every Platform Adapter, so that "is this host big enough for
#: ``profile=mini``?" has the same answer everywhere.
#:
#: Values match the table in §21.6.C verbatim.
RESOURCE_FLOOR_BY_PROFILE: Dict[str, ResourceFloor] = {
    "full": ResourceFloor(
        profile="full",
        cpu_floor=2,
        mem_mb_floor=4096,
        disk_mb_floor=2048,
        inbound_required=False,
        notes="Worker + dispatcher DB + bridge + tunnel",
    ),
    "mini": ResourceFloor(
        profile="mini",
        cpu_floor=1,
        mem_mb_floor=1024,
        disk_mb_floor=1024,
        inbound_required=False,
        notes="Bridge + dispatcher DB + tunnel; no worker",
    ),
    "edge": ResourceFloor(
        profile="edge",
        cpu_floor=1,
        mem_mb_floor=1024,
        disk_mb_floor=512,
        inbound_required=False,
        notes="Bridge + read-only DB replica + tunnel",
    ),
    "developer": ResourceFloor(
        profile="developer",
        cpu_floor=1,
        mem_mb_floor=1024,
        disk_mb_floor=512,
        inbound_required=False,
        notes="tempdir DB; sandboxed; no production DB",
    ),
}


# ---------------------------------------------------------------------------
# Host Capability Document (§21.6.B)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HostCapabilities:
    """Canonical Host Capability Document (§21.6.B).

    The declaration is the source of truth for what the host can run;
    the adapter does not guess. Required capability fields are minimal
    and extensible: future fields are appended with defaults so that
    existing documents remain valid.

    Design notes:

    * ``provider_hint`` is **informational only**. No function in this
      package branches on it. It exists purely for operator
      observability and audit logs.
    * ``class`` drives adapter selection (§21.6.B last paragraph +
      §21.6.E): ``container`` → ``abacus`` or ``zo``; ``laptop`` →
      ``macbook``; ``docker-host`` → ``docker``; ``cloud-vm`` /
      ``cloud-container`` → ``terraform-aws`` (optional).
    * ``persistent_paths`` documents which paths survive a host reset
      (Abacus resets ``/tmp``, not ``/home``; the installer honors
      this when picking the DB path).
    * ``db_path_writable`` and ``tempdir_writable`` are the
      read-only-resource constraints that the §21.6 acceptance gate
      item 1 ("edge read-only resource constraints") exercises.
    """

    # --- §21.6.B required fields ---
    name: str                      # operator-chosen identifier; not a provider slug
    class_: str                    # one of KNOWN_HOST_CLASSES
    os: str                        # linux | darwin | windows
    arch: str                      # x86_64 | arm64 | aarch64
    python: str                    # e.g. ">=3.11"
    filesystem: str = "posix"
    supervisor: str = "none"
    network_egress: str = "none"
    tunnel_kind: str = "none"
    inbound_allowed: bool = False
    db_path_writable: bool = True
    tempdir_writable: bool = True
    persistent_paths: Tuple[str, ...] = field(default_factory=tuple)

    # --- §21.6.B provider_hint (informational only) ---
    provider_hint: str = ""

    # --- §21.6.B runtime_profile block ---
    runtime_profile_supported: Tuple[str, ...] = KNOWN_PROFILES
    runtime_profile_default: str = DEFAULT_PROFILE
    runtime_profile_resource_floor: Dict[str, Dict[str, int]] = field(
        default_factory=dict
    )

    # --- §21.6.B upstream_llm block ---
    upstream_llm_reachable: bool = False
    upstream_llm_endpoint_kind: str = "openai-compatible"

    # --- Detection provenance (informational) ---
    detected: bool = False       # True if produced by adapter.detect()
    source: str = "declared"     # "declared" | "detected"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "host": {
                "name": self.name,
                "class": self.class_,
                "provider_hint": self.provider_hint,
                "os": self.os,
                "arch": self.arch,
                "python": self.python,
                "filesystem": self.filesystem,
                "supervisor": self.supervisor,
                "network_egress": self.network_egress,
                "tunnel_kind": self.tunnel_kind,
                "inbound_allowed": self.inbound_allowed,
                "db_path_writable": self.db_path_writable,
                "tempdir_writable": self.tempdir_writable,
                "persistent_paths": list(self.persistent_paths),
            },
            "runtime_profile": {
                "supported": list(self.runtime_profile_supported),
                "default": self.runtime_profile_default,
                "resource_floor": dict(self.runtime_profile_resource_floor),
            },
            "upstream_llm": {
                "reachable": self.upstream_llm_reachable,
                "endpoint_kind": self.upstream_llm_endpoint_kind,
            },
            "provenance": {
                "detected": self.detected,
                "source": self.source,
            },
        }


# ---------------------------------------------------------------------------
# Adapter result types (§21.6.D)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaterializationResult:
    """Result of :meth:`PlatformAdapter.materialize` (§21.6.D).

    Carries the host-native primitives the adapter produced: the set
    of processes, ports, persistent paths, and the supervisor unit(s)
    the host should keep alive. The result is declarative; the adapter
    does not start the processes (that is the host supervisor's job).
    """

    adapter_name: str
    profile: str
    processes: Tuple[str, ...] = field(default_factory=tuple)
    ports: Tuple[int, ...] = field(default_factory=tuple)
    persistent_paths: Tuple[str, ...] = field(default_factory=tuple)
    supervisor_units: Tuple[str, ...] = field(default_factory=tuple)
    notes: str = ""
    declined: bool = False        # True if the adapter cannot materialize on this host
    decline_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "adapter_name": self.adapter_name,
            "profile": self.profile,
            "processes": list(self.processes),
            "ports": list(self.ports),
            "persistent_paths": list(self.persistent_paths),
            "supervisor_units": list(self.supervisor_units),
            "notes": self.notes,
            "declined": self.declined,
            "decline_reason": self.decline_reason,
        }


@dataclass(frozen=True)
class HealthStatus:
    """Result of :meth:`PlatformAdapter.health_check` (§21.6.D).

    One of ``ONLINE`` / ``STARTING`` / ``OFFLINE`` / ``UNKNOWN`` /
    ``ERROR``. Used by the keepalive cron pattern (see
    external-service-keepalive skill).
    """

    state: str
    adapter_name: str = ""
    profile: str = ""
    checked_at: str = ""         # ISO-8601 UTC, informational
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "adapter_name": self.adapter_name,
            "profile": self.profile,
            "checked_at": self.checked_at,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Reference adapter names (§21.6.E)
# ---------------------------------------------------------------------------

#: Canonical reference adapter names (§21.6.E table). The
#: ``terraform-aws`` adapter is **optional** — it is the right tool
#: when an operator has chosen a cloud provider and wants declarative
#: provisioning; it is **not** the canonical deployment path.
REFERENCE_ADAPTERS: Tuple[str, ...] = (
    "abacus",
    "zo",
    "macbook",
    "docker",
    "terraform-aws",
)


# ---------------------------------------------------------------------------
# Validators (§21.6.B + §21.6.C)
# ---------------------------------------------------------------------------


def _validate_host_block(cap: HostCapabilities, errors: List[ContractValidationError]) -> None:
    """Validate the §21.6.B ``host:`` block fields."""
    if not cap.name:
        errors.append(ContractValidationError(
            "host.name",
            "host.name must be a non-empty operator-chosen identifier",
        ))
    if cap.class_ not in KNOWN_HOST_CLASSES:
        errors.append(ContractValidationError(
            "host.class",
            f"host.class {cap.class_!r} not in {list(KNOWN_HOST_CLASSES)}",
        ))
    if not cap.os:
        errors.append(ContractValidationError(
            "host.os",
            "host.os must be a non-empty string (linux | darwin | windows)",
        ))
    if not cap.arch:
        errors.append(ContractValidationError(
            "host.arch",
            "host.arch must be a non-empty string (x86_64 | arm64 | aarch64)",
        ))
    if not cap.python:
        errors.append(ContractValidationError(
            "host.python",
            "host.python must declare a minimum Python version (e.g. '>=3.11')",
        ))
    if cap.filesystem not in KNOWN_FILESYSTEM:
        errors.append(ContractValidationError(
            "host.filesystem",
            f"host.filesystem {cap.filesystem!r} not in {list(KNOWN_FILESYSTEM)}",
        ))
    if cap.supervisor not in KNOWN_SUPERVISORS:
        errors.append(ContractValidationError(
            "host.supervisor",
            f"host.supervisor {cap.supervisor!r} not in {list(KNOWN_SUPERVISORS)}",
        ))
    if cap.network_egress not in KNOWN_EGRESS:
        errors.append(ContractValidationError(
            "host.network_egress",
            f"host.network_egress {cap.network_egress!r} not in {list(KNOWN_EGRESS)}",
        ))
    # tunnel_kind is informational; we accept any non-empty string but
    # special-case "none" / "cloudflared" / "ngrok" / "ssh-reverse" in
    # the §21.6.B example. We do not enforce against a closed set so
    # operators can declare new tunnel kinds without a contract change.


def _validate_runtime_profile_block(
    cap: HostCapabilities, errors: List[ContractValidationError]
) -> None:
    """Validate the §21.6.B ``runtime_profile:`` block fields."""
    supported = cap.runtime_profile_supported
    if not supported:
        errors.append(ContractValidationError(
            "runtime_profile.supported",
            "runtime_profile.supported must list at least one profile",
        ))
    for p in supported:
        if p not in KNOWN_PROFILES:
            errors.append(ContractValidationError(
                "runtime_profile.supported",
                f"unknown profile {p!r} in runtime_profile.supported "
                f"(expected one of {list(KNOWN_PROFILES)})",
            ))
    if cap.runtime_profile_default not in KNOWN_PROFILES:
        errors.append(ContractValidationError(
            "runtime_profile.default",
            f"runtime_profile.default {cap.runtime_profile_default!r} not in "
            f"{list(KNOWN_PROFILES)}",
        ))
    if (
        cap.runtime_profile_default not in supported
        and len(supported) > 0
    ):
        errors.append(ContractValidationError(
            "runtime_profile.default",
            f"runtime_profile.default {cap.runtime_profile_default!r} "
            f"not in supported list {list(supported)}",
        ))


def _validate_upstream_llm_block(
    cap: HostCapabilities, errors: List[ContractValidationError]
) -> None:
    """Validate the §21.6.B ``upstream_llm:`` block fields."""
    if not isinstance(cap.upstream_llm_reachable, bool):
        errors.append(ContractValidationError(
            "upstream_llm.reachable",
            "upstream_llm.reachable must be a boolean",
        ))
    if not cap.upstream_llm_endpoint_kind:
        errors.append(ContractValidationError(
            "upstream_llm.endpoint_kind",
            "upstream_llm.endpoint_kind must be a non-empty string",
        ))


def validate_capabilities(cap: HostCapabilities) -> None:
    """Validate a :class:`HostCapabilities` document against §21.6.B.

    Raises :class:`ContractValidationError` on the first violation
    found. The validation is purely structural — it does not probe the
    host (that is :meth:`PlatformAdapter.detect`'s job). It does not
    branch on ``provider_hint`` (per §21.6.B last paragraph).

    Resource floor checks (§21.6.C) are **not** performed here; use
    :func:`validate_resource_floor` to check the host against the
    §21.6.C table for a specific profile.
    """
    errors: List[ContractValidationError] = []
    _validate_host_block(cap, errors)
    _validate_runtime_profile_block(cap, errors)
    _validate_upstream_llm_block(cap, errors)
    if errors:
        # Surface the first error; the rest are visible via the
        # errors list if a caller wants to aggregate.
        raise errors[0]


def validate_resource_floor(
    cap: HostCapabilities, profile: str
) -> ResourceFloor:
    """Validate the host's declared resources against the §21.6.C
    resource floor for the given Runtime Profile.

    Per §21.6.C the installer validates the host against the floor for
    the requested profile and refuses to install with a clear error
    if the floor is not met — the same refusal path on every host,
    whether cloud or laptop.

    The host's declared resources are read from
    :attr:`HostCapabilities.runtime_profile_resource_floor` (the
    ``resource_floor`` sub-block of the §21.6.B document). The
    adapter does not re-encode the floor table; it consumes
    :data:`RESOURCE_FLOOR_BY_PROFILE`.

    Returns the :class:`ResourceFloor` for the profile on success.
    Raises :class:`ContractValidationError` if the profile is unknown
    or the host's declared resources fall below the floor.
    """
    if profile not in RESOURCE_FLOOR_BY_PROFILE:
        raise ContractValidationError(
            "runtime_profile",
            f"unknown profile {profile!r}; expected one of "
            f"{list(RESOURCE_FLOOR_BY_PROFILE)}",
        )
    floor = RESOURCE_FLOOR_BY_PROFILE[profile]
    # The §21.6.B resource_floor block is keyed by profile name; the
    # host declares the resources it has for each profile it supports.
    declared_per_profile: Dict[str, Dict[str, int]] = (
        cap.runtime_profile_resource_floor or {}
    )
    declared = declared_per_profile.get(profile, {})
    host_cpu = int(declared.get("cpu", 0))
    host_mem = int(declared.get("mem_mb", 0))
    host_disk = int(declared.get("disk_mb", 0))
    if host_cpu < floor.cpu_floor:
        raise ContractValidationError(
            f"runtime_profile.resource_floor.cpu",
            f"profile {profile!r} requires cpu_floor={floor.cpu_floor} "
            f"but host declares cpu={host_cpu}",
        )
    if host_mem < floor.mem_mb_floor:
        raise ContractValidationError(
            f"runtime_profile.resource_floor.mem_mb",
            f"profile {profile!r} requires mem_mb_floor={floor.mem_mb_floor} "
            f"but host declares mem_mb={host_mem}",
        )
    if host_disk < floor.disk_mb_floor:
        raise ContractValidationError(
            f"runtime_profile.resource_floor.disk_mb",
            f"profile {profile!r} requires disk_mb_floor={floor.disk_mb_floor} "
            f"but host declares disk_mb={host_disk}",
        )
    if floor.inbound_required and not cap.inbound_allowed:
        raise ContractValidationError(
            "host.inbound_allowed",
            f"profile {profile!r} requires inbound but host declares "
            f"inbound_allowed=False",
        )
    return floor


__all__ = [
    "ContractError",
    "ContractValidationError",
    "ResourceFloor",
    "HostCapabilities",
    "MaterializationResult",
    "HealthStatus",
    "RESOURCE_FLOOR_BY_PROFILE",
    "KNOWN_HOST_CLASSES",
    "KNOWN_SUPERVISORS",
    "KNOWN_EGRESS",
    "KNOWN_FILESYSTEM",
    "REFERENCE_ADAPTERS",
    "validate_capabilities",
    "validate_resource_floor",
]