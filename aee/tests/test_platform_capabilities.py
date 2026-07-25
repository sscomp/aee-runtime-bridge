"""AEE Runtime + Platform Adapter — Phase 1: PlatformCapabilities tests.

Targets the facade defined in :mod:`aee.deploy.capabilities`. All tests
are stdlib ``unittest`` — no pytest, no Mac host, no subprocess.

Coverage (plan §11.1: "≥ 10 cases"):

* :class:`TestFromCapabilities` — field mapping from
  :class:`~aee.deploy.contract.HostCapabilities`.
* :class:`TestResolvePath` — ``~/x``, ``{host_root}/x``, absolute,
  relative, empty, host_root-only.
* :class:`TestPredicates` — ``is_linux`` / ``is_macos``.
* :class:`TestLinuxDefaults` — exact field values matching the M2
  reference (plan §9.2 byte-identical requirement).
* :class:`TestMacOSDefaults` — exact field values matching the B2
  reference (developer-only, launchd, /Users).
* :class:`TestUnknownDefaults` — explicitly limited capability set.
* :class:`TestFrozen` — frozen dataclass raises on mutation.
* :class:`TestSerialization` — ``to_dict()`` round-trip shape.
* :class:`TestEquality` — equality / inequality semantics.
"""
from __future__ import annotations

import unittest

from aee.deploy.capabilities import (
    LinuxDefaults,
    MacOSDefaults,
    PlatformCapabilities,
    UnknownDefaults,
    from_capabilities,
)
from aee.deploy.contract import HostCapabilities


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _linux_cap() -> HostCapabilities:
    """A HostCapabilities matching the M2 abacus reference."""
    return HostCapabilities(
        name="m2-abacus",
        class_="container",
        os="linux",
        arch="x86_64",
        python=">=3.11",
        filesystem="posix",
        supervisor="supervisord",
        network_egress="tunnel",
        tunnel_kind="cloudflared",
        inbound_allowed=False,
        db_path_writable=True,
        tempdir_writable=True,
        persistent_paths=("/home/ubuntu",),
        provider_hint="abacus",
        runtime_profile_supported=("full", "mini", "edge", "developer"),
        runtime_profile_default="full",
        runtime_profile_resource_floor={
            "full": {"cpu": 2, "mem_mb": 4096, "disk_mb": 2048},
        },
        upstream_llm_reachable=True,
        upstream_llm_endpoint_kind="openai-compatible",
        detected=True,
        source="detected",
    )


def _darwin_cap() -> HostCapabilities:
    """A synthetic HostCapabilities for macOS (darwin) — used in tests
    that need ``os='darwin'`` without invoking ``MacBookAdapter.detect()``
    on a Linux host (which would honestly return ``os='linux'``).
    """
    return HostCapabilities(
        name="b2-macbook",
        class_="laptop",
        os="darwin",
        arch="arm64",
        python=">=3.11",
        filesystem="posix",
        supervisor="launchd",
        network_egress="direct",
        tunnel_kind="none",
        inbound_allowed=False,
        db_path_writable=True,
        tempdir_writable=True,
        persistent_paths=("/Users",),
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


# ---------------------------------------------------------------------------
# from_capabilities() field mapping
# ---------------------------------------------------------------------------


class TestFromCapabilities(unittest.TestCase):
    def test_linux_field_mapping(self) -> None:
        cap = from_capabilities(_linux_cap())
        self.assertEqual(cap.host_root, "/home/ubuntu")
        self.assertEqual(cap.persistent_paths, ("/home/ubuntu",))
        self.assertEqual(cap.supervisor_kind, "supervisord")
        self.assertEqual(cap.network_egress_kind, "tunnel")
        self.assertEqual(cap.os_name, "linux")
        self.assertEqual(cap.arch, "x86_64")
        self.assertEqual(cap.profile_supported, ("full", "mini", "edge", "developer"))
        self.assertEqual(cap.profile_default, "full")
        self.assertFalse(cap.inbound_allowed)
        self.assertTrue(cap.db_path_writable)
        self.assertTrue(cap.tempdir_writable)
        self.assertEqual(cap.host_class, "container")
        self.assertEqual(cap.host_name, "m2-abacus")
        self.assertEqual(cap.provider_hint, "abacus")
        self.assertEqual(cap.tunnel_kind, "cloudflared")
        self.assertTrue(cap.upstream_llm_reachable)
        self.assertEqual(cap.source, "detected")

    def test_darwin_field_mapping(self) -> None:
        cap = from_capabilities(_darwin_cap())
        self.assertEqual(cap.host_root, "/Users")
        self.assertEqual(cap.os_name, "darwin")
        self.assertEqual(cap.supervisor_kind, "launchd")
        self.assertEqual(cap.profile_supported, ("developer",))
        self.assertEqual(cap.profile_default, "developer")
        self.assertEqual(cap.host_class, "laptop")

    def test_host_root_falls_back_when_no_persistent_paths(self) -> None:
        cap = HostCapabilities(
            name="empty", class_="container", os="linux", arch="x86_64",
            python=">=3.11", persistent_paths=(),
        )
        pc = from_capabilities(cap)
        self.assertEqual(pc.host_root, "/")
        self.assertEqual(pc.persistent_paths, ())


# ---------------------------------------------------------------------------
# resolve_path
# ---------------------------------------------------------------------------


class TestResolvePath(unittest.TestCase):
    def test_tilde_expands_to_host_root(self) -> None:
        self.assertEqual(LinuxDefaults.resolve_path("~/x"), "/home/ubuntu/x")
        self.assertEqual(MacOSDefaults.resolve_path("~/x"), "/Users/x")

    def test_tilde_without_slash(self) -> None:
        self.assertEqual(LinuxDefaults.resolve_path("~x"), "/home/ubuntu/x")

    def test_tilde_alone_returns_host_root(self) -> None:
        self.assertEqual(LinuxDefaults.resolve_path("~"), "/home/ubuntu")

    def test_host_root_template(self) -> None:
        self.assertEqual(
            LinuxDefaults.resolve_path("{host_root}/data/dispatcher.db"),
            "/home/ubuntu/data/dispatcher.db",
        )

    def test_absolute_passthrough(self) -> None:
        self.assertEqual(LinuxDefaults.resolve_path("/tmp/x"), "/tmp/x")
        self.assertEqual(LinuxDefaults.resolve_path("/opt/aee"), "/opt/aee")

    def test_relative_joined_under_host_root(self) -> None:
        self.assertEqual(LinuxDefaults.resolve_path("rel/path"), "/home/ubuntu/rel/path")

    def test_empty_returns_host_root(self) -> None:
        self.assertEqual(LinuxDefaults.resolve_path(""), "/home/ubuntu")
        self.assertEqual(LinuxDefaults.resolve_path(None or ""), "/home/ubuntu")


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


class TestPredicates(unittest.TestCase):
    def test_is_linux(self) -> None:
        self.assertTrue(LinuxDefaults.is_linux)
        self.assertFalse(MacOSDefaults.is_linux)
        self.assertFalse(UnknownDefaults.is_linux)

    def test_is_macos(self) -> None:
        self.assertTrue(MacOSDefaults.is_macos)
        self.assertFalse(LinuxDefaults.is_macos)
        self.assertFalse(UnknownDefaults.is_macos)


# ---------------------------------------------------------------------------
# LinuxDefaults (byte-identical to M2 reference — plan §9.2)
# ---------------------------------------------------------------------------


class TestLinuxDefaults(unittest.TestCase):
    def test_host_root(self) -> None:
        self.assertEqual(LinuxDefaults.host_root, "/home/ubuntu")

    def test_persistent_paths(self) -> None:
        self.assertEqual(LinuxDefaults.persistent_paths, ("/home/ubuntu",))

    def test_supervisor_kind(self) -> None:
        self.assertEqual(LinuxDefaults.supervisor_kind, "supervisord")

    def test_network_egress_kind(self) -> None:
        self.assertEqual(LinuxDefaults.network_egress_kind, "tunnel")

    def test_os_name(self) -> None:
        self.assertEqual(LinuxDefaults.os_name, "linux")

    def test_arch(self) -> None:
        self.assertEqual(LinuxDefaults.arch, "x86_64")

    def test_profile_supported(self) -> None:
        self.assertEqual(
            LinuxDefaults.profile_supported,
            ("full", "mini", "edge", "developer"),
        )

    def test_profile_default(self) -> None:
        self.assertEqual(LinuxDefaults.profile_default, "full")

    def test_writable_flags(self) -> None:
        self.assertTrue(LinuxDefaults.db_path_writable)
        self.assertTrue(LinuxDefaults.tempdir_writable)

    def test_inbound_allowed_false(self) -> None:
        self.assertFalse(LinuxDefaults.inbound_allowed)

    def test_source_default(self) -> None:
        self.assertEqual(LinuxDefaults.source, "default")


# ---------------------------------------------------------------------------
# MacOSDefaults (developer-only, launchd, /Users — plan §8)
# ---------------------------------------------------------------------------


class TestMacOSDefaults(unittest.TestCase):
    def test_host_root(self) -> None:
        self.assertEqual(MacOSDefaults.host_root, "/Users")

    def test_supervisor_kind_launchd(self) -> None:
        self.assertEqual(MacOSDefaults.supervisor_kind, "launchd")

    def test_profile_supported_developer_only(self) -> None:
        self.assertEqual(MacOSDefaults.profile_supported, ("developer",))

    def test_profile_default_developer(self) -> None:
        self.assertEqual(MacOSDefaults.profile_default, "developer")

    def test_os_name_darwin(self) -> None:
        self.assertEqual(MacOSDefaults.os_name, "darwin")

    def test_network_egress_direct(self) -> None:
        self.assertEqual(MacOSDefaults.network_egress_kind, "direct")


# ---------------------------------------------------------------------------
# UnknownDefaults (explicitly limited — acceptance criterion)
# ---------------------------------------------------------------------------


class TestUnknownDefaults(unittest.TestCase):
    def test_os_name_unknown(self) -> None:
        self.assertEqual(UnknownDefaults.os_name, "unknown")

    def test_no_profiles_supported(self) -> None:
        self.assertEqual(UnknownDefaults.profile_supported, ())
        self.assertEqual(UnknownDefaults.profile_default, "")

    def test_no_persistent_paths(self) -> None:
        self.assertEqual(UnknownDefaults.persistent_paths, ())

    def test_no_supervisor(self) -> None:
        self.assertEqual(UnknownDefaults.supervisor_kind, "none")

    def test_not_writable(self) -> None:
        self.assertFalse(UnknownDefaults.db_path_writable)
        self.assertFalse(UnknownDefaults.tempdir_writable)

    def test_no_inbound(self) -> None:
        self.assertFalse(UnknownDefaults.inbound_allowed)

    def test_predicates_false(self) -> None:
        self.assertFalse(UnknownDefaults.is_linux)
        self.assertFalse(UnknownDefaults.is_macos)


# ---------------------------------------------------------------------------
# Frozen dataclass
# ---------------------------------------------------------------------------


class TestFrozen(unittest.TestCase):
    def test_mutation_raises(self) -> None:
        with self.assertRaises(Exception):
            LinuxDefaults.host_root = "/other"  # type: ignore[misc]

    def test_mutation_os_name_raises(self) -> None:
        with self.assertRaises(Exception):
            MacOSDefaults.os_name = "linux"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


class TestSerialization(unittest.TestCase):
    def test_to_dict_shape(self) -> None:
        d = LinuxDefaults.to_dict()
        self.assertEqual(
            sorted(d.keys()),
            ["host", "paths", "provenance", "runtime_profile", "upstream_llm"],
        )
        self.assertEqual(d["host"]["os"], "linux")
        self.assertEqual(d["host"]["supervisor"], "supervisord")
        self.assertEqual(d["paths"]["host_root"], "/home/ubuntu")
        self.assertEqual(
            d["paths"]["persistent_paths"], ["/home/ubuntu"]
        )
        self.assertEqual(
            d["runtime_profile"]["supported"],
            ["full", "mini", "edge", "developer"],
        )
        self.assertEqual(d["runtime_profile"]["default"], "full")
        self.assertEqual(d["provenance"]["source"], "default")

    def test_to_dict_macos(self) -> None:
        d = MacOSDefaults.to_dict()
        self.assertEqual(d["host"]["os"], "darwin")
        self.assertEqual(d["host"]["supervisor"], "launchd")
        self.assertEqual(d["runtime_profile"]["supported"], ["developer"])


# ---------------------------------------------------------------------------
# Equality
# ---------------------------------------------------------------------------


class TestEquality(unittest.TestCase):
    def test_equal_when_identical(self) -> None:
        a = from_capabilities(_linux_cap())
        b = from_capabilities(_linux_cap())
        self.assertEqual(a, b)

    def test_unequal_when_os_differs(self) -> None:
        a = from_capabilities(_linux_cap())
        b = from_capabilities(_darwin_cap())
        self.assertNotEqual(a, b)

    def test_linuxdefaults_equal_to_from_capabilities(self) -> None:
        # The LinuxDefaults singleton should produce the same fields as
        # from_capabilities() on a matching HostCapabilities, *except*
        # for `source` (LinuxDefaults uses "default"; detect() uses
        # "detected"). Verify all other fields match.
        cap = from_capabilities(_linux_cap())
        for field_name in (
            "host_root", "persistent_paths", "supervisor_kind",
            "network_egress_kind", "os_name", "arch",
            "profile_supported", "profile_default", "inbound_allowed",
            "db_path_writable", "tempdir_writable",
            "host_class", "host_name", "provider_hint", "tunnel_kind",
            "upstream_llm_reachable", "upstream_llm_endpoint_kind",
        ):
            self.assertEqual(
                getattr(LinuxDefaults, field_name),
                getattr(cap, field_name),
                f"field mismatch on {field_name}",
            )
        self.assertEqual(LinuxDefaults.source, "default")
        self.assertEqual(cap.source, "detected")


if __name__ == "__main__":
    unittest.main()