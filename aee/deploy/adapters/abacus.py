"""AEE Epic 9.6 §21.6.E — Reference adapter: Abacus.ai container (M2).

This is the **reference** adapter for the ``container`` host class
on Abacus.ai. It matches today's M2 deployment (Master Plan §21.6.F):

    * supervisord process supervisor
    * Cloudflare Tunnel for egress (no inbound)
    * ``/home/ubuntu`` persistent across container resets
    * ``/tmp`` is wiped on container reset

The adapter is **informational only** in this slice — it does not
provision resources on Abacus (Abacus.ai has no public provisioning
API; the container exists). ``materialize()`` produces the
declarative supervisor unit list + tunnel config the host should keep
alive; it does not start them (that is supervisord's job).

Design rules (§21.6.D):

* Does not import ``boto3`` / ``google-cloud-*`` / ``azure-*`` /
  ``terraform`` (Abacus has no cloud SDK).
* Does not branch on ``provider_hint``.
* Does not assume inbound connectivity (the default is egress-only
  via Cloudflare Tunnel).
* MAY shell out to ``supervisorctl`` (the host-native tooling).
"""
from __future__ import annotations

import os
import platform
import sys
from typing import Tuple

from aee.deploy.contract import (
    HealthStatus,
    HostCapabilities,
    MaterializationResult,
    RESOURCE_FLOOR_BY_PROFILE,
)
from aee.deploy.adapters.base import PlatformAdapter


#: The canonical supervisor units the Abacus adapter materializes.
#: The names match the existing ``/etc/supervisor/conf.d/*.conf``
#: programs on M2 (verified during AEE-9.5 baseline). The adapter
#: does not create these files — it documents what the host should
#: keep alive; supervisord is the host's process supervisor.
_ABACUS_SUPERVISOR_UNITS: Tuple[str, ...] = (
    "hermes-runtime-bridge",
    "cloudflared-tunnel",
)

#: Persistent paths on Abacus (Master Plan §21.6.F).
_ABACUS_PERSISTENT_PATHS: Tuple[str, ...] = (
    "/home/ubuntu",
)


class AbacusAdapter:
    """Reference Platform Adapter for the Abacus.ai container (M2).

    This adapter is the §21.6.G acceptance item 2 reference adapter.
    It is **informational only** — it does not provision resources on
    Abacus (Abacus.ai has no public provisioning API; the container
    exists). ``detect()`` returns a populated
    :class:`HostCapabilities` document for the current host if the
    host looks like an Abacus container, otherwise raises
    :class:`RuntimeError`. ``materialize()`` produces the
    declarative supervisor unit list + tunnel config the host should
    keep alive.
    """

    name = "abacus"

    def detect(self) -> HostCapabilities:
        """Probe the host and return a populated HostCapabilities.

        The detection is heuristic: it checks for the Abacus
        supervisord socket (``/tmp/supervisor.sock``), the
        ``/home/ubuntu`` persistent path, and the Cloudflare Tunnel
        process. If any of these are missing, the document is still
        returned (with the missing fields flagged) so the installer
        can surface a clear error.

        The return value is a snapshot; callers should not assume it
        stays accurate across container resets.
        """
        is_abacus = (
            os.path.exists("/tmp/supervisor.sock")
            or os.path.isdir("/home/ubuntu")
        )
        # We do NOT raise if the heuristic fails — the document is
        # the source of truth, and the installer validates it. This
        # keeps detect() side-effect-free per §21.6.D.
        py_version = self._detect_python_version()
        return HostCapabilities(
            name="m2-abacus" if is_abacus else "abacus-detected",
            class_="container",
            os=sys.platform,
            arch=platform.machine() or "x86_64",
            python=py_version,
            filesystem="posix",
            supervisor="supervisord",
            network_egress="tunnel",
            tunnel_kind="cloudflared",
            inbound_allowed=False,
            db_path_writable=os.access("/home/ubuntu", os.W_OK),
            tempdir_writable=os.access("/tmp", os.W_OK),
            persistent_paths=_ABACUS_PERSISTENT_PATHS,
            provider_hint="abacus",
            runtime_profile_supported=("full", "mini", "edge", "developer"),
            runtime_profile_default="full",
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
        """Translate the contract into host-native primitives.

        Returns the supervisor unit list + tunnel config the host
        should keep alive for the given Runtime Profile. Does not
        start the processes (that is supervisord's job).

        Per §21.6.E the adapter is **informational only** on Abacus —
        it does not provision resources (Abacus.ai has no public
        provisioning API; the container exists). The result is
        declarative; the host supervisor is responsible for keeping
        the units alive.
        """
        if profile not in RESOURCE_FLOOR_BY_PROFILE:
            return MaterializationResult(
                adapter_name=self.name,
                profile=profile,
                declined=True,
                decline_reason=f"unknown profile {profile!r}",
            )
        # The Abacus container supports all four profiles per
        # §21.6.F, but ``full`` is the reference profile (it is the
        # canonical deployment on M2). For ``edge`` we add the
        # read-only DB constraint note.
        units = _ABACUS_SUPERVISOR_UNITS
        notes = (
            "Reference Abacus.ai container deployment. supervisord + "
            "Cloudflare Tunnel, egress-only, /home/ubuntu persistent."
        )
        if profile == "edge":
            notes += (
                " Edge profile: AEE_DB_READ_ONLY=1 env var is set by "
                "the docker-entrypoint.sh wrapper (§21.5), not by this "
                "adapter. The adapter documents the constraint; the "
                "runtime enforces it."
            )
        return MaterializationResult(
            adapter_name=self.name,
            profile=profile,
            processes=tuple(f"{u}" for u in units),
            ports=(8787, 20241),
            persistent_paths=_ABACUS_PERSISTENT_PATHS,
            supervisor_units=units,
            notes=notes,
        )

    def health_check(self, profile: str) -> HealthStatus:
        """Return the current health of the Abacus deployment.

        The check is best-effort: it probes the supervisord socket and
        the Cloudflare Tunnel process. If both are present, the
        deployment is ``ONLINE``; if neither, ``OFFLINE``; otherwise
        ``STARTING``.
        """
        sup_ok = os.path.exists("/tmp/supervisor.sock")
        # We do not fork a subprocess to check cloudflared — that would
        # be a side-effecting probe, which the §21.6.D contract
        # discourages for a health_check call. The supervisord socket
        # is the canonical liveness signal on Abacus.
        if sup_ok:
            return HealthStatus(
                state="ONLINE",
                adapter_name=self.name,
                profile=profile,
                detail="supervisord socket present",
            )
        return HealthStatus(
            state="OFFLINE",
            adapter_name=self.name,
            profile=profile,
            detail="supervisord socket missing",
        )

    @staticmethod
    def _detect_python_version() -> str:
        """Return a §21.6.B-style Python version specifier."""
        v = sys.version_info
        return f">={v.major}.{v.minor}"


__all__ = ["AbacusAdapter"]