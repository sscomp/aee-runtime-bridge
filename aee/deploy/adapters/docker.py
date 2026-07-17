"""AEE Epic 9.6 §21.6.E — Reference adapter: generic Docker host.

Reference adapter for the ``docker-host`` host class. Matches the
generic Docker host described in Master Plan §21.6.F:

    * docker-compose with the single image from §21.5
    * resource limits from §21.6.C
    * used for CI (§21.7), sandbox, and any operator who has Docker
      but not a specific cloud account

The adapter is **informational only** in this slice — it does not
provision resources (the operator runs ``docker-compose up``). It
produces the declarative ``docker-compose`` snippet the host should
use; the operator is responsible for actually running it.
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


#: The docker-compose service names the adapter materializes.
_DOCKER_SERVICES: Tuple[str, ...] = (
    "aee-bridge",
)


class DockerAdapter:
    """Reference Platform Adapter for a generic Docker host.

    Per §21.6.F a generic Docker host is used for CI (§21.7), sandbox,
    and any operator who has Docker but not a specific cloud account.
    The adapter is informational only — it produces the declarative
    ``docker-compose`` snippet the host should use.
    """

    name = "docker"

    def detect(self) -> HostCapabilities:
        return HostCapabilities(
            name="docker-host-detected",
            class_="docker-host",
            os=sys.platform,
            arch=platform.machine() or "x86_64",
            python=self._detect_python_version(),
            filesystem="posix",
            supervisor="docker",
            network_egress="direct",
            tunnel_kind="none",
            inbound_allowed=False,
            db_path_writable=True,
            tempdir_writable=True,
            persistent_paths=(),
            provider_hint="docker",
            runtime_profile_supported=("full", "mini", "edge", "developer"),
            runtime_profile_default="mini",
            runtime_profile_resource_floor={
                "full": {"cpu": 2, "mem_mb": 4096, "disk_mb": 2048},
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
        floor = RESOURCE_FLOOR_BY_PROFILE[profile]
        return MaterializationResult(
            adapter_name=self.name,
            profile=profile,
            processes=tuple(_DOCKER_SERVICES),
            ports=(8787,),
            persistent_paths=("/var/lib/docker/volumes/aee-data",),
            supervisor_units=_DOCKER_SERVICES,
            notes=(
                f"Reference generic Docker host deployment. "
                f"docker-compose with the single image from §21.5; "
                f"resource limits from §21.6.C "
                f"(cpu_floor={floor.cpu_floor}, "
                f"mem_mb_floor={floor.mem_mb_floor}, "
                f"disk_mb_floor={floor.disk_mb_floor})."
            ),
        )

    def health_check(self, profile: str) -> HealthStatus:
        return HealthStatus(
            state="UNKNOWN",
            adapter_name=self.name,
            profile=profile,
            detail="docker liveness probe not implemented "
                   "(operator should check via docker ps)",
        )

    @staticmethod
    def _detect_python_version() -> str:
        v = sys.version_info
        return f">={v.major}.{v.minor}"


__all__ = ["DockerAdapter"]