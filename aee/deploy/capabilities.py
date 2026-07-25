"""AEE Runtime + Platform Adapter — Phase 1: PlatformCapabilities facade.

This module is the primary deliverable of Phase 1 of the platform adapter
architecture (see
``reports/aee_platform_adapter_architecture_plan.md`` §6.1 + §14).

It defines a frozen, read-only :class:`PlatformCapabilities` dataclass that
is the runtime read-API over a loaded
:class:`aee.deploy.contract.HostCapabilities` document. The facade is
consumed by AEE Core / dispatcher in place of hardcoded paths and platform
checks.

Design invariants (plan §6.1 + §9):

* **Frozen and cached for the process lifetime.** Core code reads it; it
  never mutates.
* **Constructed from a** :class:`HostCapabilities` **document** (loaded
  from ``host.capabilities.<name>.yaml`` or returned by
  ``adapter.detect()``). The facade does not probe the host itself.
* **Default fallback = current M2 behavior.** When no document is loaded,
  :data:`LinuxDefaults` yields ``host_root='/home/ubuntu'``,
  ``supervisor_kind='supervisord'`` — byte-identical to the existing
  AbacusAdapter reference so Phase 2 can swap dispatcher reads to the
  facade with zero behavioral change (plan §9.2).
* **No circular imports.** This module imports only
  :mod:`aee.deploy.contract` (already a leaf module).

This module is **additive** (Phase 1). No production caller consumes it
yet (plan §10 Phase 1: "no caller depends on it"). It is the safe first
step — a new module that compiles, imports, and is verified.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from aee.deploy.contract import HostCapabilities


# ---------------------------------------------------------------------------
# PlatformCapabilities facade (plan §6.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlatformCapabilities:
    """Read-only runtime view over a HostCapabilities document + adapter.

    Consumed by dispatcher/aee core in place of hardcoded paths and
    platform checks. All fields are derived from the loaded
    :class:`HostCapabilities` (declared) or the adapter's ``detect()``
    (probed). The facade is frozen and cached for the process lifetime;
    Core code reads it, never mutates it.

    Field semantics:

    * ``host_root`` — the canonical persistent root for this host
      (e.g. ``"/home/ubuntu"`` on M2/Abacus, ``"/Users"`` on B2/MacBook).
      Derived from the first entry of
      :attr:`HostCapabilities.persistent_paths` when available.
    * ``persistent_paths`` — paths that survive a host reset
      (Abacus resets ``/tmp``, not ``/home``).
    * ``supervisor_kind`` — ``"supervisord"`` | ``"launchd"`` |
      ``"docker"`` | ``"none"``.
    * ``network_egress_kind`` — ``"tunnel"`` | ``"direct"`` | ``"none"``.
    * ``os_name`` — ``"linux"`` | ``"darwin"`` | ``"unknown"`` | ...
    * ``arch`` — ``"x86_64"`` | ``"arm64"`` | ``"aarch64"`` | ...
    * ``profile_supported`` — tuple of Runtime Profile names this host
      can run (subset of ``full`` / ``mini`` / ``edge`` / ``developer``).
    * ``profile_default`` — the default Runtime Profile for this host.
    * ``inbound_allowed`` — ``True`` only if the host accepts inbound
      connections (default egress-only per plan §4.3).
    * ``db_path_writable`` / ``tempdir_writable`` — read-only-resource
      constraints (§21.6 acceptance gate item 1).
    """

    host_root: str
    persistent_paths: Tuple[str, ...]
    supervisor_kind: str
    network_egress_kind: str
    os_name: str
    arch: str
    profile_supported: Tuple[str, ...]
    profile_default: str
    inbound_allowed: bool
    db_path_writable: bool
    tempdir_writable: bool
    # Informational only (plan §2.5 / §4.3): no function branches on this.
    host_class: str = "unknown"
    host_name: str = "unknown"
    provider_hint: str = ""
    tunnel_kind: str = "none"
    upstream_llm_reachable: bool = False
    upstream_llm_endpoint_kind: str = "openai-compatible"
    # Provenance: "declared" (YAML) | "detected" (adapter.detect()) |
    # "default" (fallback singleton).
    source: str = "declared"

    # ----- Predicates ---------------------------------------------------

    @property
    def is_linux(self) -> bool:
        """``True`` iff :attr:`os_name` is ``"linux"``."""
        return self.os_name == "linux"

    @property
    def is_macos(self) -> bool:
        """``True`` iff :attr:`os_name` is ``"darwin"``."""
        return self.os_name == "darwin"

    # ----- Path resolution ---------------------------------------------

    def resolve_path(self, p: str) -> str:
        """Resolve a ``~/path`` or ``{host_root}/path`` to an absolute path.

        Semantics:

        * ``None`` or empty string → returns ``host_root``.
        * Starts with ``"~"`` → replaced with :attr:`host_root`.
        * Starts with ``"{host_root}"`` → substituted with
          :attr:`host_root` (template form, useful in YAML).
        * Already absolute (starts with ``"/"``) → returned unchanged.
        * Relative path without ``"~"`` → joined under
          :attr:`host_root` (``host_root + "/" + p``).
        """
        if not p:
            return self.host_root
        if p.startswith("~"):
            rest = p[1:].lstrip("/")
            return f"{self.host_root}/{rest}" if rest else self.host_root
        if p.startswith("{host_root}"):
            rest = p[len("{host_root}"):].lstrip("/")
            return (
                f"{self.host_root}/{rest}" if rest else self.host_root
            )
        if p.startswith("/"):
            return p
        return f"{self.host_root}/{p}"

    # ----- Serialization ------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable dict view of the facade."""
        return {
            "host": {
                "name": self.host_name,
                "class": self.host_class,
                "os": self.os_name,
                "arch": self.arch,
                "supervisor": self.supervisor_kind,
                "network_egress": self.network_egress_kind,
                "tunnel_kind": self.tunnel_kind,
                "inbound_allowed": self.inbound_allowed,
                "provider_hint": self.provider_hint,
            },
            "paths": {
                "host_root": self.host_root,
                "persistent_paths": list(self.persistent_paths),
                "db_path_writable": self.db_path_writable,
                "tempdir_writable": self.tempdir_writable,
            },
            "runtime_profile": {
                "supported": list(self.profile_supported),
                "default": self.profile_default,
            },
            "upstream_llm": {
                "reachable": self.upstream_llm_reachable,
                "endpoint_kind": self.upstream_llm_endpoint_kind,
            },
            "provenance": {
                "source": self.source,
            },
        }


# ---------------------------------------------------------------------------
# Constructor: from a HostCapabilities document
# ---------------------------------------------------------------------------


def from_capabilities(cap: HostCapabilities) -> PlatformCapabilities:
    """Build a :class:`PlatformCapabilities` from a
    :class:`~aee.deploy.contract.HostCapabilities` document.

    The mapping is field-for-field from the contract. ``host_root`` is
    derived from the first entry of ``cap.persistent_paths`` when
    available, falling back to ``"/"`` if the document lists no
    persistent paths.
    """
    host_root = cap.persistent_paths[0] if cap.persistent_paths else "/"
    return PlatformCapabilities(
        host_root=host_root,
        persistent_paths=tuple(cap.persistent_paths),
        supervisor_kind=cap.supervisor,
        network_egress_kind=cap.network_egress,
        os_name=cap.os,
        arch=cap.arch,
        profile_supported=tuple(cap.runtime_profile_supported),
        profile_default=cap.runtime_profile_default,
        inbound_allowed=cap.inbound_allowed,
        db_path_writable=cap.db_path_writable,
        tempdir_writable=cap.tempdir_writable,
        host_class=cap.class_,
        host_name=cap.name,
        provider_hint=cap.provider_hint,
        tunnel_kind=cap.tunnel_kind,
        upstream_llm_reachable=cap.upstream_llm_reachable,
        upstream_llm_endpoint_kind=cap.upstream_llm_endpoint_kind,
        source=cap.source,
    )


# ---------------------------------------------------------------------------
# Fallback singletons (plan §9.2 — default = current M2 behavior)
# ---------------------------------------------------------------------------


#: Linux/Abacus (M2) default. Matches ``AbacusAdapter().detect()`` on the
#: M2 host byte-for-byte so Phase 2 can swap dispatcher reads to the
#: facade with zero behavioral change (plan §9.2).
LinuxDefaults: PlatformCapabilities = PlatformCapabilities(
    host_root="/home/ubuntu",
    persistent_paths=("/home/ubuntu",),
    supervisor_kind="supervisord",
    network_egress_kind="tunnel",
    os_name="linux",
    arch="x86_64",
    profile_supported=("full", "mini", "edge", "developer"),
    profile_default="full",
    inbound_allowed=False,
    db_path_writable=True,
    tempdir_writable=True,
    host_class="container",
    host_name="m2-abacus",
    provider_hint="abacus",
    tunnel_kind="cloudflared",
    upstream_llm_reachable=True,
    upstream_llm_endpoint_kind="openai-compatible",
    source="default",
)


#: macOS/MacBook (B2) default. Matches ``host.capabilities.b2-macbook.yaml``
#: and the MacBookAdapter's declared capabilities (``launchd``,
#: ``profile=developer`` only). This is a static declaration — no live
#: macOS validation is performed (plan §12.2).
MacOSDefaults: PlatformCapabilities = PlatformCapabilities(
    host_root="/Users",
    persistent_paths=("/Users",),
    supervisor_kind="launchd",
    network_egress_kind="direct",
    os_name="darwin",
    arch="arm64",
    profile_supported=("developer",),
    profile_default="developer",
    inbound_allowed=False,
    db_path_writable=True,
    tempdir_writable=True,
    host_class="laptop",
    host_name="b2-macbook",
    provider_hint="apple",
    tunnel_kind="none",
    upstream_llm_reachable=True,
    upstream_llm_endpoint_kind="openai-compatible",
    source="default",
)


#: Unknown-platform fallback. A frozen, explicitly limited capability set
#: (plan §4.3 + acceptance criterion "fail safely or return an explicitly
#: limited capability set"). Callers receiving this should refuse work
#: rather than guessing.
UnknownDefaults: PlatformCapabilities = PlatformCapabilities(
    host_root="/",
    persistent_paths=(),
    supervisor_kind="none",
    network_egress_kind="none",
    os_name="unknown",
    arch="unknown",
    profile_supported=(),
    profile_default="",
    inbound_allowed=False,
    db_path_writable=False,
    tempdir_writable=False,
    host_class="unknown",
    host_name="unknown",
    provider_hint="",
    tunnel_kind="none",
    upstream_llm_reachable=False,
    upstream_llm_endpoint_kind="none",
    source="default",
)


__all__ = [
    "PlatformCapabilities",
    "from_capabilities",
    "LinuxDefaults",
    "MacOSDefaults",
    "UnknownDefaults",
]