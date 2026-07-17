"""AEE Epic 9.6 §21.6.E — Optional adapter: Terraform on AWS (placeholder).

The ``terraform-aws`` adapter is **explicitly optional** (Master Plan
§21.6.E). It is the right tool when an operator has chosen a cloud
provider and wants declarative provisioning; it is **not** the
canonical deployment path.

This adapter is a **placeholder** in this slice. Per §21.6.E a new
cloud provider is supported by writing a new ``terraform-gcp`` /
``terraform-azure`` adapter. Adding such an adapter is **out of
scope** for Epic 9 and requires a separate user decision per §21.0
(each sub-section independently authorizable). No provider-specific
adapter is pre-committed by this sub-section.

Design rules (§21.6.D):

* Does not import ``boto3`` / ``google-cloud-*`` / ``azure-*`` /
  ``terraform``. The placeholder documents the optional path; it
  does not become a runtime dependency of AEE Core.
* ``materialize()`` returns a :class:`MaterializationResult` with
  ``declined=True`` and a ``decline_reason`` explaining that the
  adapter is optional and out of scope for Epic 9.
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


class TerraformAwsAdapter:
    """Optional Platform Adapter for Terraform on AWS (placeholder).

    Per §21.6.E the ``terraform-aws`` adapter is **optional** — used
    only when the operator explicitly chooses AWS. It is **not**
    required for any of the acceptance items 1–5 in §21.6.G (those
    depend on the reference adapters ``abacus`` / ``zo`` /
    ``macbook`` / ``docker``).

    This placeholder documents the optional path; it does not
    provision resources and does not import the Terraform CLI.
    """

    name = "terraform-aws"

    def detect(self) -> HostCapabilities:
        # The placeholder returns a generic cloud-vm document. A real
        # implementation would probe the AWS metadata endpoint
        # (169.254.169.254) — but doing so without explicit operator
        # opt-in would violate §21.6.D ("MUST NOT assume inbound
        # connectivity is available"). The placeholder is declarative
        # only.
        return HostCapabilities(
            name="cloud-vm-detected",
            class_="cloud-vm",
            os=sys.platform,
            arch=platform.machine() or "x86_64",
            python=self._detect_python_version(),
            filesystem="posix",
            supervisor="systemd",
            network_egress="direct",
            tunnel_kind="none",
            inbound_allowed=True,
            db_path_writable=True,
            tempdir_writable=True,
            persistent_paths=("/var/lib/aee",),
            provider_hint="aws",
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
        # The placeholder declines: per §21.6.E the terraform-aws
        # adapter is optional and out of scope for Epic 9. A real
        # implementation would emit a Terraform module with a
        # ``profile`` variable; the placeholder documents this
        # without provisioning anything.
        return MaterializationResult(
            adapter_name=self.name,
            profile=profile,
            declined=True,
            decline_reason=(
                "terraform-aws is an optional adapter (§21.6.E); "
                "implementation out of scope for Epic 9. The operator "
                "must explicitly choose AWS and provide a Terraform "
                "module. AEE Core does not depend on Terraform."
            ),
            notes=(
                "Optional placeholder. A real implementation would "
                "emit a Terraform module with a `profile` variable; "
                "the canonical deployment path is the Host Capability "
                "Document + the appropriate reference adapter, not "
                "Terraform."
            ),
        )

    def health_check(self, profile: str) -> HealthStatus:
        return HealthStatus(
            state="UNKNOWN",
            adapter_name=self.name,
            profile=profile,
            detail="terraform-aws placeholder; no live probe",
        )

    @staticmethod
    def _detect_python_version() -> str:
        v = sys.version_info
        return f">={v.major}.{v.minor}"


__all__ = ["TerraformAwsAdapter"]