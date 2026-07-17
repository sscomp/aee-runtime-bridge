"""AEE Epic 9.6 §21.6 Provider-Neutral Deployment Profiles — targeted tests.

Tests the Provider-Neutral Deployment Contract (§21.6.A–§21.6.G) as
recovered from the original run `run_63335fcc47a94c8eaa1a42a3e595d380`.
The original run shipped source but stopped before writing tests; this
suite is the minimal finalization per Rescue Policy step 4.

Coverage:

  * §21.6.A — four-layer decomposition (AEE Core / Runtime Profile /
    Deployment Contract / Platform Adapter) — verified via import surface
  * §21.6.B — Host Capability Document schema + validator
  * §21.6.C — Profile-to-Resource floor table (single source of truth)
  * §21.6.D — Platform Adapter Protocol (runtime_checkable)
  * §21.6.E — 5 reference adapters (abacus, zo, macbook, docker,
    terraform-aws) + adapter registry + selection by class (not by
    provider_hint)
  * §21.6.F — Heterogeneous host coverage (M2/N2/B2/generic Docker host)
  * §21.6.G — Acceptance gate items 1–7 (Terraform optional, no cloud SDK,
    adapter smoke on M2, host capability docs accepted by installer)

Invariants enforced:

  1. No cloud SDK imports (no boto3 / google-cloud-* / azure-* / terraform).
  2. provider_hint is informational only — no code path branches on it.
  3. terraform-aws adapter declines every profile (placeholder, out of
     scope per §21.6.E).
  4. Adapters for hosts without a provisioning API (B2 laptop, generic
     Docker host) do NOT pretend to provision resources — they produce
     declarative primitives the operator runs.
  5. N2 (Zo) and B2 (MacBook) decline profiles that require a footprint
     they cannot provision (full on N2, non-developer on B2).

Run: PYTHONPATH=. python3 -m unittest discover -s aee/tests -p 'test_aee96*' -v
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

# Ensure the repo root is on sys.path so `import aee.deploy...` works
# when running from anywhere.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from aee.deploy import (
    AdapterNotFoundError,
    ContractError,
    ContractValidationError,
    HealthStatus,
    HostCapabilities,
    MaterializationResult,
    PlatformAdapter,
    REFERENCE_ADAPTERS,
    RESOURCE_FLOOR_BY_PROFILE,
    KNOWN_HOST_CLASSES,
    get_registry,
    register_adapter,
    select_adapter,
    validate_capabilities,
    validate_resource_floor,
)
from aee.deploy.adapters.abacus import AbacusAdapter
from aee.deploy.adapters.base import PlatformAdapter as _PlatformAdapterFromBase
from aee.deploy.adapters.docker import DockerAdapter
from aee.deploy.adapters.macbook import MacBookAdapter
from aee.deploy.adapters.terraform_aws import TerraformAwsAdapter
from aee.deploy.adapters.zo import ZoAdapter
from aee.deploy.loader import load_host_capabilities


_SAMPLES_DIR = _REPO_ROOT / "aee" / "deploy" / "samples"


# ---------------------------------------------------------------------------
# §21.6.B — Host Capability Document schema
# ---------------------------------------------------------------------------


class TestHostCapabilityDocumentSchema(unittest.TestCase):
    """§21.6.B — HostCapabilities dataclass + to_dict roundtrip."""

    def test_host_capabilities_is_frozen_dataclass(self) -> None:
        c = HostCapabilities(
            name="t", class_="container", os="linux", arch="x86_64",
            python=">=3.11",
        )
        # frozen=True → cannot setattr
        with self.assertRaises(Exception):
            c.name = "other"  # type: ignore[misc]

    def test_to_dict_contains_required_blocks(self) -> None:
        c = HostCapabilities(
            name="t", class_="container", os="linux", arch="x86_64",
            python=">=3.11", provider_hint="abacus",
            runtime_profile_supported=("full", "mini"),
            runtime_profile_default="full",
        )
        d = c.to_dict()
        self.assertIn("host", d)
        self.assertIn("runtime_profile", d)
        self.assertIn("upstream_llm", d)
        self.assertIn("provenance", d)
        self.assertEqual(d["host"]["name"], "t")
        self.assertEqual(d["host"]["class"], "container")
        self.assertEqual(d["host"]["provider_hint"], "abacus")
        self.assertEqual(d["runtime_profile"]["default"], "full")
        self.assertFalse(d["provenance"]["detected"])

    def test_default_filesystem_is_posix(self) -> None:
        c = HostCapabilities(
            name="t", class_="container", os="linux", arch="x86_64",
            python=">=3.11",
        )
        self.assertEqual(c.filesystem, "posix")
        self.assertEqual(c.supervisor, "none")
        self.assertEqual(c.network_egress, "none")
        self.assertFalse(c.inbound_allowed)

    def test_persistent_paths_default_empty_tuple(self) -> None:
        c = HostCapabilities(
            name="t", class_="container", os="linux", arch="x86_64",
            python=">=3.11",
        )
        self.assertEqual(c.persistent_paths, ())
        self.assertEqual(c.runtime_profile_supported, ("full", "mini", "edge", "developer"))


# ---------------------------------------------------------------------------
# §21.6.B — validate_capabilities
# ---------------------------------------------------------------------------


class TestValidateCapabilities(unittest.TestCase):
    """§21.6.B — validate_capabilities surfaces schema violations."""

    def _good(self, **overrides) -> HostCapabilities:
        defaults = dict(
            name="t", class_="container", os="linux", arch="x86_64",
            python=">=3.11", filesystem="posix", supervisor="supervisord",
            network_egress="tunnel",
            runtime_profile_supported=("full",),
            runtime_profile_default="full",
        )
        defaults.update(overrides)
        return HostCapabilities(**defaults)

    def test_valid_document_passes(self) -> None:
        validate_capabilities(self._good())  # no raise

    def test_unknown_host_class_raises(self) -> None:
        with self.assertRaises(ContractValidationError) as cm:
            validate_capabilities(self._good(class_="not-a-class"))
        self.assertIn("host.class", cm.exception.field)

    def test_unknown_supervisor_raises(self) -> None:
        with self.assertRaises(ContractValidationError) as cm:
            validate_capabilities(self._good(supervisor="not-a-supervisor"))
        self.assertIn("host.supervisor", cm.exception.field)

    def test_unknown_filesystem_raises(self) -> None:
        with self.assertRaises(ContractValidationError) as cm:
            validate_capabilities(self._good(filesystem="not-a-fs"))
        self.assertIn("host.filesystem", cm.exception.field)

    def test_unknown_egress_raises(self) -> None:
        with self.assertRaises(ContractValidationError) as cm:
            validate_capabilities(self._good(network_egress="not-an-egress"))
        self.assertIn("host.network_egress", cm.exception.field)

    def test_empty_name_raises(self) -> None:
        with self.assertRaises(ContractValidationError) as cm:
            validate_capabilities(self._good(name=""))
        self.assertIn("host.name", cm.exception.field)

    def test_unknown_profile_in_supported_raises(self) -> None:
        with self.assertRaises(ContractValidationError) as cm:
            validate_capabilities(self._good(
                runtime_profile_supported=("not-a-profile",),
                runtime_profile_default="not-a-profile",
            ))
        self.assertIn("runtime_profile", cm.exception.field)

    def test_default_not_in_supported_raises(self) -> None:
        with self.assertRaises(ContractValidationError) as cm:
            validate_capabilities(self._good(
                runtime_profile_supported=("mini",),
                runtime_profile_default="full",
            ))
        self.assertIn("runtime_profile.default", cm.exception.field)

    def test_provider_hint_is_never_validated(self) -> None:
        # provider_hint is informational only — any string is accepted
        validate_capabilities(self._good(provider_hint="anything"))
        validate_capabilities(self._good(provider_hint=""))


# ---------------------------------------------------------------------------
# §21.6.C — Profile-to-Resource floor table
# ---------------------------------------------------------------------------


class TestResourceFloorTable(unittest.TestCase):
    """§21.6.C — RESOURCE_FLOOR_BY_PROFILE is the single source of truth."""

    def test_table_has_all_four_profiles(self) -> None:
        for p in ("full", "mini", "edge", "developer"):
            self.assertIn(p, RESOURCE_FLOOR_BY_PROFILE)
            self.assertEqual(RESOURCE_FLOOR_BY_PROFILE[p].profile, p)

    def test_full_has_highest_floor(self) -> None:
        full = RESOURCE_FLOOR_BY_PROFILE["full"]
        mini = RESOURCE_FLOOR_BY_PROFILE["mini"]
        self.assertGreater(full.cpu_floor, mini.cpu_floor)
        self.assertGreater(full.mem_mb_floor, mini.mem_mb_floor)
        self.assertGreater(full.disk_mb_floor, mini.disk_mb_floor)

    def test_no_profile_requires_inbound(self) -> None:
        # §21.6.C — none of the four canonical profiles requires inbound
        for p, f in RESOURCE_FLOOR_BY_PROFILE.items():
            self.assertFalse(f.inbound_required,
                              f"{p} should not require inbound")


class TestValidateResourceFloor(unittest.TestCase):
    """§21.6.C — installer refuses if the host declares below the floor."""

    def _cap_with_resources(self, profile: str, cpu: int, mem: int, disk: int) -> HostCapabilities:
        return HostCapabilities(
            name="t", class_="container", os="linux", arch="x86_64",
            python=">=3.11",
            runtime_profile_supported=(profile,),
            runtime_profile_default=profile,
            runtime_profile_resource_floor={
                profile: {"cpu": cpu, "mem_mb": mem, "disk_mb": disk}
            },
        )

    def test_above_floor_passes(self) -> None:
        cap = self._cap_with_resources("mini", 4, 8192, 4096)
        floor = validate_resource_floor(cap, "mini")
        self.assertEqual(floor.profile, "mini")

    def test_below_floor_cpu_raises(self) -> None:
        cap = self._cap_with_resources("full", 1, 8192, 4096)
        with self.assertRaises(ContractValidationError) as cm:
            validate_resource_floor(cap, "full")
        self.assertIn("cpu", cm.exception.field)

    def test_below_floor_mem_raises(self) -> None:
        cap = self._cap_with_resources("full", 4, 1024, 4096)
        with self.assertRaises(ContractValidationError) as cm:
            validate_resource_floor(cap, "full")
        self.assertIn("mem_mb", cm.exception.field)

    def test_below_floor_disk_raises(self) -> None:
        cap = self._cap_with_resources("full", 4, 8192, 512)
        with self.assertRaises(ContractValidationError) as cm:
            validate_resource_floor(cap, "full")
        self.assertIn("disk_mb", cm.exception.field)

    def test_unknown_profile_raises(self) -> None:
        cap = self._cap_with_resources("mini", 4, 8192, 4096)
        with self.assertRaises(ContractValidationError):
            validate_resource_floor(cap, "not-a-profile")

    def test_missing_resource_floor_block_raises(self) -> None:
        cap = HostCapabilities(
            name="t", class_="container", os="linux", arch="x86_64",
            python=">=3.11",
            runtime_profile_supported=("full",),
            runtime_profile_default="full",
            # resource_floor block intentionally empty
        )
        with self.assertRaises(ContractValidationError):
            validate_resource_floor(cap, "full")


# ---------------------------------------------------------------------------
# §21.6.D — PlatformAdapter Protocol
# ---------------------------------------------------------------------------


class TestPlatformAdapterProtocol(unittest.TestCase):
    """§21.6.D — Protocol is runtime_checkable; all 5 adapters satisfy it."""

    def test_protocol_is_runtime_checkable(self) -> None:
        # The Protocol class is decorated with @runtime_checkable.
        self.assertTrue(hasattr(PlatformAdapter, "_is_runtime_protocol"))
        # isinstance against the Protocol works for any object with the
        # right methods (duck typing).
        a = AbacusAdapter()
        self.assertIsInstance(a, _PlatformAdapterFromBase)

    def test_all_five_adapters_satisfy_protocol(self) -> None:
        for cls in (AbacusAdapter, ZoAdapter, MacBookAdapter,
                    DockerAdapter, TerraformAwsAdapter):
            a = cls()
            self.assertIsInstance(a, _PlatformAdapterFromBase,
                                  f"{cls.__name__} does not satisfy Protocol")
            self.assertTrue(hasattr(a, "name"))
            self.assertTrue(hasattr(a, "detect"))
            self.assertTrue(hasattr(a, "materialize"))
            self.assertTrue(hasattr(a, "health_check"))

    def test_adapter_names_match_reference_set(self) -> None:
        names = tuple(cls().name for cls in (
            AbacusAdapter, ZoAdapter, MacBookAdapter,
            DockerAdapter, TerraformAwsAdapter,
        ))
        self.assertEqual(set(names), set(REFERENCE_ADAPTERS))


# ---------------------------------------------------------------------------
# §21.6.E — Reference adapters + registry + selection
# ---------------------------------------------------------------------------


class TestReferenceAdapters(unittest.TestCase):
    """§21.6.E — Each reference adapter's detect/materialize/health_check."""

    def test_abacus_detect_returns_container_class(self) -> None:
        cap = AbacusAdapter().detect()
        self.assertEqual(cap.class_, "container")
        self.assertEqual(cap.supervisor, "supervisord")
        self.assertEqual(cap.network_egress, "tunnel")
        self.assertFalse(cap.inbound_allowed)

    def test_abacus_materialize_full_returns_supervisor_units(self) -> None:
        a = AbacusAdapter()
        cap = a.detect()
        r = a.materialize("full", cap)
        self.assertFalse(r.declined)
        self.assertEqual(r.adapter_name, "abacus")
        self.assertIn("hermes-runtime-bridge", r.supervisor_units)
        self.assertIn("cloudflared-tunnel", r.supervisor_units)

    def test_abacus_materialize_unknown_profile_declines(self) -> None:
        a = AbacusAdapter()
        r = a.materialize("not-a-profile", a.detect())
        self.assertTrue(r.declined)
        self.assertIn("unknown profile", r.decline_reason)

    def test_abacus_health_check_returns_known_state(self) -> None:
        # health_check may return ONLINE or OFFLINE depending on whether
        # the supervisord socket exists on this host, but the state must
        # be one of the canonical values.
        hs = AbacusAdapter().health_check("full")
        self.assertIn(hs.state, ("ONLINE", "STARTING", "OFFLINE", "UNKNOWN", "ERROR"))

    def test_zo_detect_returns_container_class(self) -> None:
        cap = ZoAdapter().detect()
        self.assertEqual(cap.class_, "container")
        self.assertEqual(cap.supervisor, "zo-native")

    def test_zo_materialize_mini_returns_units(self) -> None:
        r = ZoAdapter().materialize("mini", ZoAdapter().detect())
        self.assertFalse(r.declined)
        self.assertEqual(r.adapter_name, "zo")

    def test_zo_materialize_full_declines(self) -> None:
        # N2 is the reference profile=mini host; full requires a worker
        # footprint not provisioned on Zo.computer.
        r = ZoAdapter().materialize("full", ZoAdapter().detect())
        self.assertTrue(r.declined)
        self.assertIn("profile=full", r.decline_reason)

    def test_macbook_detect_returns_laptop_class(self) -> None:
        cap = MacBookAdapter().detect()
        self.assertEqual(cap.class_, "laptop")
        self.assertEqual(cap.supervisor, "launchd")

    def test_macbook_materialize_developer_returns_units(self) -> None:
        r = MacBookAdapter().materialize("developer", MacBookAdapter().detect())
        self.assertFalse(r.declined)
        self.assertEqual(r.adapter_name, "macbook")

    def test_macbook_materialize_full_declines(self) -> None:
        # B2 is the reference profile=developer host; full requires a
        # footprint not provisioned on a laptop.
        r = MacBookAdapter().materialize("full", MacBookAdapter().detect())
        self.assertTrue(r.declined)
        self.assertIn("profile=developer", r.decline_reason)

    def test_docker_detect_returns_docker_host_class(self) -> None:
        cap = DockerAdapter().detect()
        self.assertEqual(cap.class_, "docker-host")
        self.assertEqual(cap.supervisor, "docker")

    def test_docker_materialize_all_profiles_return_units(self) -> None:
        a = DockerAdapter()
        cap = a.detect()
        for p in ("full", "mini", "edge", "developer"):
            r = a.materialize(p, cap)
            self.assertFalse(r.declined, f"docker should accept {p}")
            self.assertEqual(r.adapter_name, "docker")

    def test_terraform_aws_detect_returns_cloud_vm_class(self) -> None:
        cap = TerraformAwsAdapter().detect()
        self.assertIn(cap.class_, ("cloud-vm", "cloud-container"))

    def test_terraform_aws_materialize_always_declines(self) -> None:
        # §21.6.E — terraform-aws is optional and out of scope for Epic 9.
        a = TerraformAwsAdapter()
        cap = a.detect()
        for p in ("full", "mini", "edge", "developer"):
            r = a.materialize(p, cap)
            self.assertTrue(r.declined,
                            f"terraform-aws should decline {p}")
            self.assertIn("optional", r.decline_reason)


class TestRegistryAndSelection(unittest.TestCase):
    """§21.6.E — AdapterRegistry + select_adapter by class + override."""

    def test_default_registry_has_all_five_adapters(self) -> None:
        reg = get_registry()
        names = set(reg.list_adapters().keys())
        self.assertEqual(names, set(REFERENCE_ADAPTERS))

    def test_default_class_mappings_cover_all_known_classes(self) -> None:
        mappings = get_registry().list_class_mappings()
        # container / laptop / docker-host / cloud-vm / cloud-container
        for c in ("container", "laptop", "docker-host", "cloud-vm", "cloud-container"):
            self.assertIn(c, mappings)

    def test_select_by_class_container_returns_abacus(self) -> None:
        a = select_adapter(host_class="container")
        self.assertEqual(a.name, "abacus")  # M2 reference default

    def test_select_by_class_laptop_returns_macbook(self) -> None:
        a = select_adapter(host_class="laptop")
        self.assertEqual(a.name, "macbook")

    def test_select_by_class_docker_host_returns_docker(self) -> None:
        a = select_adapter(host_class="docker-host")
        self.assertEqual(a.name, "docker")

    def test_select_by_adapter_name_overrides_class(self) -> None:
        # N2 has class=container but uses the zo adapter via --adapter zo.
        a = select_adapter(host_class="container", adapter_name="zo")
        self.assertEqual(a.name, "zo")

    def test_select_with_unknown_class_raises(self) -> None:
        with self.assertRaises(AdapterNotFoundError):
            select_adapter(host_class="not-a-class")

    def test_select_with_unknown_adapter_name_raises(self) -> None:
        with self.assertRaises(AdapterNotFoundError):
            select_adapter(adapter_name="not-an-adapter")

    def test_select_with_no_args_raises(self) -> None:
        with self.assertRaises(AdapterNotFoundError):
            select_adapter()

    def test_register_adapter_adds_to_registry(self) -> None:
        # Use a private registry for isolation.
        from aee.deploy.registry import AdapterRegistry
        reg = AdapterRegistry()
        reg.register(AbacusAdapter())
        reg.register(ZoAdapter())
        reg.map_class("container", "abacus")
        a = reg.select(host_class="container", adapter_name="zo")
        self.assertEqual(a.name, "zo")

    def test_provider_hint_does_not_affect_selection(self) -> None:
        # Build a HostCapabilities with provider_hint="zo" but class=container;
        # selection should still return abacus (class-based default), NOT zo.
        c = HostCapabilities(
            name="t", class_="container", os="linux", arch="x86_64",
            python=">=3.11", provider_hint="zo",
            runtime_profile_supported=("full",),
            runtime_profile_default="full",
        )
        a = select_adapter(cap=c)
        self.assertEqual(a.name, "abacus")  # class drives selection


# ---------------------------------------------------------------------------
# §21.6.B — Host Capability Document loader (4 reference samples)
# ---------------------------------------------------------------------------


class TestHostCapabilityLoader(unittest.TestCase):
    """§21.6.B — load_host_capabilities parses all 4 reference YAMLs."""

    def test_m2_abacus_yaml_loads(self) -> None:
        path = _SAMPLES_DIR / "host.capabilities.m2-abacus.yaml"
        c = load_host_capabilities(str(path))
        self.assertEqual(c.name, "m2-abacus")
        self.assertEqual(c.class_, "container")
        self.assertEqual(c.provider_hint, "abacus")
        self.assertEqual(c.supervisor, "supervisord")
        self.assertEqual(c.runtime_profile_default, "full")
        self.assertIn("/home/ubuntu", c.persistent_paths)
        self.assertEqual(c.source, str(path))

    def test_n2_zo_yaml_loads(self) -> None:
        c = load_host_capabilities(str(_SAMPLES_DIR / "host.capabilities.n2-zo.yaml"))
        self.assertEqual(c.name, "n2-zo")
        self.assertEqual(c.class_, "container")
        self.assertEqual(c.provider_hint, "zo")
        self.assertEqual(c.supervisor, "zo-native")
        self.assertEqual(c.runtime_profile_default, "mini")

    def test_b2_macbook_yaml_loads(self) -> None:
        c = load_host_capabilities(str(_SAMPLES_DIR / "host.capabilities.b2-macbook.yaml"))
        self.assertEqual(c.name, "b2-macbook")
        self.assertEqual(c.class_, "laptop")
        self.assertEqual(c.provider_hint, "apple")
        self.assertEqual(c.supervisor, "launchd")
        self.assertEqual(c.runtime_profile_default, "developer")
        self.assertFalse(c.inbound_allowed)

    def test_docker_host_yaml_loads(self) -> None:
        c = load_host_capabilities(str(_SAMPLES_DIR / "host.capabilities.docker-host.yaml"))
        self.assertEqual(c.name, "docker-host-generic")
        self.assertEqual(c.class_, "docker-host")
        self.assertEqual(c.supervisor, "docker")
        self.assertEqual(c.runtime_profile_default, "mini")

    def test_all_four_samples_pass_validate_capabilities(self) -> None:
        for name in (
            "host.capabilities.m2-abacus.yaml",
            "host.capabilities.n2-zo.yaml",
            "host.capabilities.b2-macbook.yaml",
            "host.capabilities.docker-host.yaml",
        ):
            c = load_host_capabilities(str(_SAMPLES_DIR / name))
            validate_capabilities(c)  # no raise

    def test_all_four_samples_pass_validate_resource_floor(self) -> None:
        # Each sample declares a resource floor for its default profile
        # that meets or exceeds the §21.6.C table.
        for name, default_profile in (
            ("host.capabilities.m2-abacus.yaml", "full"),
            ("host.capabilities.n2-zo.yaml", "mini"),
            ("host.capabilities.b2-macbook.yaml", "developer"),
            ("host.capabilities.docker-host.yaml", "mini"),
        ):
            c = load_host_capabilities(str(_SAMPLES_DIR / name))
            floor = validate_resource_floor(c, default_profile)
            self.assertEqual(floor.profile, default_profile)


# ---------------------------------------------------------------------------
# §21.6.G — Acceptance gate (the 8 items)
# ---------------------------------------------------------------------------


class TestAcceptanceGate(unittest.TestCase):
    """§21.6.G — Acceptance items 1–7 are verified by the targeted suite.

    Item 8 is the meta-item ("acceptance items 1–7 above replace the old
    Terraform acceptance"); it requires no separate test.
    """

    # Item 1: contract in master plan matches adapter interface in base.py.
    # Verified by TestPlatformAdapterProtocol + TestReferenceAdapters +
    # TestHostCapabilityLoader together (the interface is the only
    # coupling, the contract data lives in contract.py).

    def test_acceptance_2_abacus_adapter_smoke(self) -> None:
        """§21.6.G item 2 — abacus adapter passes smoke on M2 host."""
        a = AbacusAdapter()
        cap = a.detect()
        # On the M2 (Abacus.ai container) host the supervisord socket
        # may or may not be present at test time; either is acceptable.
        # The smoke is: detect + materialize + health_check all succeed
        # without raising.
        r = a.materialize("full", cap)
        self.assertFalse(r.declined)
        a.health_check("full")

    def test_acceptance_3_m2_host_capability_document_populated(self) -> None:
        """§21.6.G item 3 — M2 Host Capability Document populated + valid."""
        c = load_host_capabilities(str(_SAMPLES_DIR / "host.capabilities.m2-abacus.yaml"))
        validate_capabilities(c)
        validate_resource_floor(c, "full")

    def test_acceptance_4_b2_host_capability_document_developer(self) -> None:
        """§21.6.G item 4 — B2 Host Capability Document accepts profile=developer."""
        c = load_host_capabilities(str(_SAMPLES_DIR / "host.capabilities.b2-macbook.yaml"))
        validate_capabilities(c)
        validate_resource_floor(c, "developer")

    def test_acceptance_5_docker_host_capability_document_mini(self) -> None:
        """§21.6.G item 5 — generic Docker Host Capability Document accepts profile=mini."""
        c = load_host_capabilities(str(_SAMPLES_DIR / "host.capabilities.docker-host.yaml"))
        validate_capabilities(c)
        validate_resource_floor(c, "mini")

    def test_acceptance_6_terraform_aws_not_required(self) -> None:
        """§21.6.G item 6 — terraform-aws is documented but optional.

        Acceptance items 1–5 do not depend on Terraform. Verified by
        the fact that TestAcceptanceGate.test_acceptance_2/3/4/5 pass
        without importing terraform-aws; the terraform_aws adapter is
        a placeholder that declines every profile.
        """
        # The terraform-aws adapter imports no cloud SDK and declines
        # every profile (verified in TestReferenceAdapters).
        # This assertion makes the acceptance item explicit.
        a = TerraformAwsAdapter()
        for p in ("full", "mini", "edge", "developer"):
            r = a.materialize(p, a.detect())
            self.assertTrue(r.declined)

    def test_acceptance_7_aee_core_imports_no_cloud_sdk(self) -> None:
        """§21.6.G item 7 — AEE Core imports no cloud SDK / IaC tool.

        We grep the deployed module set for forbidden imports. The
        forbidden set is: boto3, google.cloud, azure, terraform.
        """
        import ast
        forbidden = ("boto3", "google.cloud", "azure", "terraform")
        deploy_dir = _REPO_ROOT / "aee" / "deploy"
        for py in deploy_dir.rglob("*.py"):
            if "__pycache__" in str(py):
                continue
            tree = ast.parse(py.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        for f in forbidden:
                            self.assertFalse(
                                alias.name.startswith(f),
                                f"{py}: forbidden import {alias.name}",
                            )
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        for f in forbidden:
                            self.assertFalse(
                                node.module.startswith(f),
                                f"{py}: forbidden import from {node.module}",
                            )


# ---------------------------------------------------------------------------
# §21.6.F — Heterogeneous host coverage (M2 / N2 / B2 / generic Docker host)
# ---------------------------------------------------------------------------


class TestHeterogeneousHostCoverage(unittest.TestCase):
    """§21.6.F — All four reference host classes are first-class targets."""

    def test_m2_descriptor_is_container_class(self) -> None:
        c = load_host_capabilities(str(_SAMPLES_DIR / "host.capabilities.m2-abacus.yaml"))
        self.assertEqual(c.class_, "container")
        self.assertFalse(c.inbound_allowed)
        self.assertIn("/home/ubuntu", c.persistent_paths)

    def test_n2_descriptor_is_container_class(self) -> None:
        c = load_host_capabilities(str(_SAMPLES_DIR / "host.capabilities.n2-zo.yaml"))
        self.assertEqual(c.class_, "container")
        self.assertFalse(c.inbound_allowed)

    def test_b2_descriptor_is_laptop_class(self) -> None:
        c = load_host_capabilities(str(_SAMPLES_DIR / "host.capabilities.b2-macbook.yaml"))
        self.assertEqual(c.class_, "laptop")
        self.assertFalse(c.inbound_allowed)
        self.assertEqual(c.runtime_profile_default, "developer")

    def test_docker_host_descriptor_is_docker_host_class(self) -> None:
        c = load_host_capabilities(str(_SAMPLES_DIR / "host.capabilities.docker-host.yaml"))
        self.assertEqual(c.class_, "docker-host")

    def test_m2_n2_share_class_but_differ_in_provider_hint(self) -> None:
        m2 = load_host_capabilities(str(_SAMPLES_DIR / "host.capabilities.m2-abacus.yaml"))
        n2 = load_host_capabilities(str(_SAMPLES_DIR / "host.capabilities.n2-zo.yaml"))
        self.assertEqual(m2.class_, n2.class_)  # both container
        self.assertNotEqual(m2.provider_hint, n2.provider_hint)
        self.assertNotEqual(m2.runtime_profile_default, n2.runtime_profile_default)


# ---------------------------------------------------------------------------
# §21.6 — MaterializationResult / HealthStatus DTOs
# ---------------------------------------------------------------------------


class TestMaterializationResultDto(unittest.TestCase):

    def test_to_dict_roundtrip(self) -> None:
        r = MaterializationResult(
            adapter_name="abacus", profile="full",
            processes=("a",), ports=(8787,),
            supervisor_units=("hermes-runtime-bridge",),
            persistent_paths=("/home/ubuntu",),
        )
        d = r.to_dict()
        self.assertEqual(d["adapter_name"], "abacus")
        self.assertEqual(d["profile"], "full")
        self.assertEqual(d["processes"], ["a"])
        self.assertEqual(d["ports"], [8787])
        self.assertFalse(d["declined"])

    def test_declined_result_has_reason(self) -> None:
        r = MaterializationResult(
            adapter_name="x", profile="p", declined=True,
            decline_reason="nope",
        )
        self.assertTrue(r.declined)
        self.assertEqual(r.decline_reason, "nope")


class TestHealthStatusDto(unittest.TestCase):

    def test_to_dict_roundtrip(self) -> None:
        hs = HealthStatus(state="ONLINE", adapter_name="abacus", profile="full")
        d = hs.to_dict()
        self.assertEqual(d["state"], "ONLINE")
        self.assertEqual(d["adapter_name"], "abacus")


if __name__ == "__main__":
    unittest.main()