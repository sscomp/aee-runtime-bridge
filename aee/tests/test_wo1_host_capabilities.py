"""WO-1: Canonical `host.capabilities.yaml` for M2.

Targeted validation that the canonical Host Capability Document at the
repository root exists, loads via the canonical loader, validates
against the §21.6.B contract, and matches the live AbacusAdapter.detect()
on M2 for the shared declaration fields.

Scope: WO-1 only. Does NOT test installer CLI --capabilities (WO-2) or
installer backend binding (WO-3).

All tests are stdlib ``unittest`` — no pytest, no subprocess, no host
mutation. Read-only.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from aee.deploy.contract import (
    HostCapabilities,
    validate_capabilities,
    validate_resource_floor,
)
from aee.deploy.loader import load_host_capabilities


_REPO_ROOT = Path(__file__).resolve().parents[2]
_CANONICAL_PATH = _REPO_ROOT / "host.capabilities.yaml"


class TestCanonicalM2HostCapabilities(unittest.TestCase):
    """WO-1: the canonical host.capabilities.yaml exists and validates."""

    def test_canonical_file_exists(self) -> None:
        self.assertTrue(
            _CANONICAL_PATH.is_file(),
            f"canonical host.capabilities.yaml not found at {_CANONICAL_PATH}",
        )

    def test_canonical_file_loads(self) -> None:
        cap = load_host_capabilities(str(_CANONICAL_PATH))
        self.assertIsInstance(cap, HostCapabilities)
        self.assertEqual(cap.name, "m2-abacus")
        self.assertEqual(cap.class_, "container")

    def test_canonical_file_validates(self) -> None:
        cap = load_host_capabilities(str(_CANONICAL_PATH))
        validate_capabilities(cap)  # no raise

    def test_canonical_resource_floor_all_profiles(self) -> None:
        cap = load_host_capabilities(str(_CANONICAL_PATH))
        for p in cap.runtime_profile_supported:
            rf = validate_resource_floor(cap, p)
            self.assertEqual(rf.profile, p)

    def test_canonical_file_source_is_declared(self) -> None:
        cap = load_host_capabilities(str(_CANONICAL_PATH))
        self.assertFalse(cap.detected)
        self.assertEqual(cap.source, str(_CANONICAL_PATH))

    def test_canonical_matches_linux_defaults_facade(self) -> None:
        """The canonical file should produce the same facade fields as
        LinuxDefaults (the §9.2 fallback), except for `source`."""
        from aee.deploy.capabilities import LinuxDefaults, from_capabilities

        cap = load_host_capabilities(str(_CANONICAL_PATH))
        pc = from_capabilities(cap)
        for field_name in (
            "host_root", "persistent_paths", "supervisor_kind",
            "network_egress_kind", "os_name", "arch",
            "profile_supported", "profile_default", "inbound_allowed",
            "db_path_writable", "tempdir_writable",
            "host_class", "host_name", "provider_hint", "tunnel_kind",
            "upstream_llm_reachable", "upstream_llm_endpoint_kind",
        ):
            self.assertEqual(
                getattr(pc, field_name),
                getattr(LinuxDefaults, field_name),
                f"field mismatch on {field_name}",
            )
        # source differs: canonical file is "declared" path, LinuxDefaults is "default"
        self.assertEqual(LinuxDefaults.source, "default")

    def test_canonical_matches_abacus_detect_on_m2(self) -> None:
        """On M2, the canonical file should match AbacusAdapter.detect()
        for all shared declaration fields (the file is the declared
        counterpart of the detected document)."""
        from aee.deploy.adapters.abacus import AbacusAdapter

        cap = load_host_capabilities(str(_CANONICAL_PATH))
        detected = AbacusAdapter().detect()
        for f in (
            "name", "class_", "os", "arch", "supervisor",
            "network_egress", "tunnel_kind", "inbound_allowed",
            "persistent_paths", "runtime_profile_supported",
            "runtime_profile_default",
            "upstream_llm_reachable", "upstream_llm_endpoint_kind",
        ):
            self.assertEqual(
                getattr(cap, f), getattr(detected, f),
                f"field mismatch on {f}: canonical={getattr(cap, f)!r} detected={getattr(detected, f)!r}",
            )
        # detected=True from adapter; canonical file is declared (detected=False)
        self.assertTrue(detected.detected)
        self.assertFalse(cap.detected)


if __name__ == "__main__":
    unittest.main()