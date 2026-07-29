"""AEE Epic 9.6 §21.6.E — Reference adapter: Windows (skeleton).

The ``windows`` adapter is the **skeleton** deliverable for work order
W1 (Bootstrap v1 spec §16). Per §17.3 Phase C:

    "After Phase C, Windows runs in UNKNOWN capability mode; first-class
    support waits on the Windows adapter (W1 already shipped in Phase A
    as a skeleton)."

This adapter is therefore **informational only** and follows the same
"decline to materialize" posture as the optional
:class:`~aee.deploy.adapters.terraform_aws.TerraformAwsAdapter`:

* ``detect()`` returns an honest :class:`HostCapabilities` document
  describing a Windows host. The ``class`` field is ``"laptop"`` (a
  Windows workstation is a single-operator machine, the same shape as
  the MacBook reference host) so the document passes
  :func:`~aee.deploy.contract.validate_capabilities` without requiring
  a new entry in :data:`~aee.deploy.contract.KNOWN_HOST_CLASSES` (a
  contract change is out of scope for W1).
* ``materialize()`` returns a :class:`MaterializationResult` with
  ``declined=True`` — the skeleton does not provision resources. Per
  §13.4 Windows is experimental in v1; the decline reason points the
  operator at the §13.4 documentation.
* ``health_check()`` returns ``UNKNOWN`` — no live probe is implemented
  by the skeleton.

Design rules (§21.6.D):

* Does not import any Windows-specific SDK. ``detect()`` uses
  :mod:`platform` and :mod:`sys` only (the same stdlib surface as the
  other reference adapters).
* Does not branch on ``provider_hint``.
* Does not assume inbound connectivity (``inbound_allowed=False``).
* Does not mutate AEE Core, the Docker image, or the profile
  descriptor.

The adapter is registered by name (``windows``) so operators can pass
``--adapter windows`` explicitly. It is **not** the default for any
:class:`~aee.platform.current.PlatformIdentity` — per
:data:`~aee.platform.current._DEFAULT_ADAPTER_BY_IDENTITY` the
``WINDOWS`` identity maps to ``None``, so the resolver returns
:data:`~aee.deploy.capabilities.UnknownDefaults` unless the operator
opts in.
"""
from __future__ import annotations

import platform
import sys

from aee.deploy.contract import (
    HealthStatus,
    HostCapabilities,
    MaterializationResult,
    RESOURCE_FLOOR_BY_PROFILE,
)


#: Persistent paths on a Windows host (user profile survives resets).
_WINDOWS_PERSISTENT_PATHS = (
    "%LOCALAPPDATA%",
    "%USERPROFILE%",
)


class WindowsAdapter:
    """Skeleton Platform Adapter for Windows hosts (W1, §16 + §17.3).

    Per §17.3 Phase C this adapter is a **skeleton** — Windows is
    experimental in v1 (§13.4). The skeleton documents the Windows
    path; it does not provision resources and does not implement a
    live health probe. First-class Windows support is a future work
    order (tracked in §17.2 open question 4 "WSL support").
    """

    name = "windows"

    def detect(self) -> HostCapabilities:
        # The skeleton returns an honest Windows document. ``class`` is
        # ``"laptop"`` (a Windows workstation is a single-operator
        # machine — the same shape as the MacBook reference host) so
        # the document passes validate_capabilities() without a new
        # KNOWN_HOST_CLASSES entry (a contract change is out of scope
        # for W1). ``detected=False`` because the skeleton does not
        # probe the host; ``source="declared"`` because the document is
        # declarative.
        return HostCapabilities(
            name="windows-host",
            class_="laptop",
            os="windows",
            arch=platform.machine() or "x86_64",
            python=self._detect_python_version(),
            filesystem="windows-wsl",
            supervisor="none",
            network_egress="direct",
            tunnel_kind="none",
            inbound_allowed=False,
            db_path_writable=True,
            tempdir_writable=True,
            persistent_paths=_WINDOWS_PERSISTENT_PATHS,
            provider_hint="microsoft",
            runtime_profile_supported=("developer",),
            runtime_profile_default="developer",
            runtime_profile_resource_floor={
                "developer": {"cpu": 1, "mem_mb": 1024, "disk_mb": 512},
            },
            upstream_llm_reachable=True,
            upstream_llm_endpoint_kind="openai-compatible",
            detected=False,
            source="declared",
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
        # The skeleton declines: per §17.3 Phase C "Windows runs in
        # UNKNOWN capability mode; first-class support waits on the
        # Windows adapter". A real implementation would emit a
        # PowerShell-based supervisor unit (e.g. a Windows Service or
        # a Scheduled Task); the skeleton documents this without
        # provisioning anything.
        return MaterializationResult(
            adapter_name=self.name,
            profile=profile,
            declined=True,
            decline_reason=(
                "windows adapter is a skeleton (W1, §16 + §17.3 "
                "Phase C); Windows is experimental in v1 (§13.4). "
                "First-class support waits on a future work order. "
                "The operator should use install.ps1 (W7) for the "
                "Windows trampoline and treat the host as UNKNOWN "
                "capability until the full adapter lands."
            ),
            notes=(
                "Skeleton placeholder. A real implementation would "
                "emit a Windows Service / Scheduled Task definition "
                "with a `profile` variable and a PowerShell bootstrap; "
                "the canonical deployment path today is install.ps1 "
                "(W7) + UNKNOWN capability mode."
            ),
        )

    def health_check(self, profile: str) -> HealthStatus:
        return HealthStatus(
            state="UNKNOWN",
            adapter_name=self.name,
            profile=profile,
            detail="windows skeleton; no live probe (W1)",
        )

    @staticmethod
    def _detect_python_version() -> str:
        v = sys.version_info
        return f">={v.major}.{v.minor}"


__all__ = ["WindowsAdapter"]