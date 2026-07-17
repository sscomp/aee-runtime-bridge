"""AEE Epic 9.6 §21.6.E — Reference adapter: MacBook Pro laptop (B2).

Reference adapter for the ``laptop`` host class on a MacBook Pro.
Matches today's B2 deployment (Master Plan §21.6.F):

    * ``launchd`` (or ``tmux`` fallback) process supervisor
    * no tunnel by default
    * ``inbound_allowed: false``
    * ``profile=developer`` reference host

The adapter is **informational only** — it does not provision
resources (a laptop has no provisioning API; the operator runs the
commands). ``materialize()`` produces the declarative launchd unit
list the operator should install.
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


#: launchd plist labels the operator should install.
_MACBOOK_SUPERVISOR_UNITS: Tuple[str, ...] = (
    "com.biaobecue.aee.bridge",
)

#: Persistent paths on a MacBook (the operator's home directory).
_MACBOOK_PERSISTENT_PATHS: Tuple[str, ...] = (
    "/Users",
)


class MacBookAdapter:
    """Reference Platform Adapter for the MacBook Pro laptop (B2).

    Per §21.6.F this is the reference ``profile=developer`` host. The
    adapter is informational only — it does not provision resources
    (a laptop has no provisioning API).
    """

    name = "macbook"

    def detect(self) -> HostCapabilities:
        is_mac = sys.platform == "darwin"
        return HostCapabilities(
            name="b2-macbook" if is_mac else "laptop-detected",
            class_="laptop",
            os=sys.platform,
            arch=platform.machine() or "arm64",
            python=self._detect_python_version(),
            filesystem="posix",
            supervisor="launchd",
            network_egress="direct",
            tunnel_kind="none",
            inbound_allowed=False,
            db_path_writable=True,
            tempdir_writable=True,
            persistent_paths=_MACBOOK_PERSISTENT_PATHS,
            provider_hint="apple",
            runtime_profile_supported=("developer",),
            runtime_profile_default="developer",
            runtime_profile_resource_floor={
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
        if profile != "developer":
            # Per §21.6.F B2 is the reference ``profile=developer``
            # host. The MacBook adapter declines non-developer
            # profiles rather than pretending to provision a worker
            # footprint on a laptop.
            return MaterializationResult(
                adapter_name=self.name,
                profile=profile,
                declined=True,
                decline_reason=(
                    f"B2 is the reference profile=developer host; "
                    f"profile={profile!r} requires a footprint not "
                    f"provisioned on a laptop"
                ),
            )
        return MaterializationResult(
            adapter_name=self.name,
            profile=profile,
            processes=tuple(_MACBOOK_SUPERVISOR_UNITS),
            ports=(8787,),
            persistent_paths=_MACBOOK_PERSISTENT_PATHS,
            supervisor_units=_MACBOOK_SUPERVISOR_UNITS,
            notes=(
                "Reference MacBook Pro laptop deployment. launchd "
                "(or tmux fallback), no tunnel by default, "
                "inbound_allowed=false, profile=developer."
            ),
        )

    def health_check(self, profile: str) -> HealthStatus:
        return HealthStatus(
            state="UNKNOWN",
            adapter_name=self.name,
            profile=profile,
            detail="launchd liveness probe not implemented "
                   "(operator should check via launchctl)",
        )

    @staticmethod
    def _detect_python_version() -> str:
        v = sys.version_info
        return f">={v.major}.{v.minor}"


__all__ = ["MacBookAdapter"]