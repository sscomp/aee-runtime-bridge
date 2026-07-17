"""AEE Epic 9.6 §21.6.E — Reference adapter: Zo.computer container (N2).

Reference adapter for the ``container`` host class on Zo.computer.
Matches today's N2 deployment (Master Plan §21.6.F):

    * Zo-native process supervisor
    * egress-only (no inbound)
    * persistence model defined by the Zo.computer plan tier

The adapter is **informational only** — it does not provision
resources on Zo.computer (no public provisioning API assumed).
"""
from __future__ import annotations

import platform
import sys
from typing import Tuple

from aee.deploy.contract import (
    HealthStatus,
    HostCapabilities,
    MaterializationResult,
    RESOURCE_FLOOR_BY_PROFILE,
)


#: The canonical supervisor unit on Zo.computer. The exact name is
#: informational; the host's Zo-native supervisor is responsible for
#: keeping it alive.
_ZO_SUPERVISOR_UNITS: Tuple[str, ...] = (
    "aee-bridge",
)

#: Persistent paths on Zo.computer (Master Plan §21.6.F).
_ZO_PERSISTENT_PATHS: Tuple[str, ...] = (
    "/home/n2",
    "/home/user",
)


class ZoAdapter:
    """Reference Platform Adapter for the Zo.computer container (N2).

    Per §21.6.F this is the reference ``profile=mini`` host. The
    adapter is informational only — it does not provision resources
    on Zo.computer.
    """

    name = "zo"

    def detect(self) -> HostCapabilities:
        return HostCapabilities(
            name="n2-zo",
            class_="container",
            os=sys.platform,
            arch=platform.machine() or "x86_64",
            python=self._detect_python_version(),
            filesystem="posix",
            supervisor="zo-native",
            network_egress="tunnel",
            tunnel_kind="cloudflared",
            inbound_allowed=False,
            db_path_writable=True,
            tempdir_writable=True,
            persistent_paths=_ZO_PERSISTENT_PATHS,
            provider_hint="zo",
            runtime_profile_supported=("mini", "edge", "developer"),
            runtime_profile_default="mini",
            runtime_profile_resource_floor={
                "mini": {"cpu": 1, "mem_mb": 1024, "disk_mb": 1024},
                "edge": {"cpu": 1, "mem_mb": 1024, "disk_mb": 512},
                "developer": {"cpu": 1, "mem_mb": 1024, "disk_mb": 512},
            },
            upstream_llm_reachable=True,
            upstream_llm_endpoint_kind="openai-compatible",
            detected=True,
            source="detected",
        )

    def materialize(
        self, profile: str, cap: HostCapabilities
    ) -> MaterializationResult:
        if profile not in RESOURCE_FLOOR_BY_PROFILE:
            return MaterializationResult(
                adapter_name=self.name,
                profile=profile,
                declined=True,
                decline_reason=f"unknown profile {profile!r}",
            )
        if profile == "full":
            # Per §21.6.F N2 is a reference ``profile=mini`` host.
            # The Zo.computer container does not run ``profile=full``
            # (no worker footprint) — the adapter declines rather
            # than fabricating resources.
            return MaterializationResult(
                adapter_name=self.name,
                profile=profile,
                declined=True,
                decline_reason=(
                    "N2 is the reference profile=mini host; profile=full "
                    "requires a worker footprint not provisioned on "
                    "Zo.computer"
                ),
            )
        return MaterializationResult(
            adapter_name=self.name,
            profile=profile,
            processes=tuple(_ZO_SUPERVISOR_UNITS),
            ports=(8787,),
            persistent_paths=_ZO_PERSISTENT_PATHS,
            supervisor_units=_ZO_SUPERVISOR_UNITS,
            notes=(
                "Reference Zo.computer container deployment. "
                "Zo-native supervisor, egress-only."
            ),
        )

    def health_check(self, profile: str) -> HealthStatus:
        return HealthStatus(
            state="UNKNOWN",
            adapter_name=self.name,
            profile=profile,
            detail="Zo-native supervisor liveness probe not implemented "
                   "(host-specific; installer should override)",
        )

    @staticmethod
    def _detect_python_version() -> str:
        v = sys.version_info
        return f">={v.major}.{v.minor}"


__all__ = ["ZoAdapter"]