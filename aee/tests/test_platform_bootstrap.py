"""AEE Runtime + Platform Adapter — Phase 1: bootstrap tests.

Targets the bootstrap in :mod:`aee.platform.current`. All tests are
stdlib ``unittest`` — no pytest, no Mac host, no live ``launchctl`` /
``supervisorctl`` invocation.

Coverage (plan §11.1):

* :class:`TestPlatformIdentity` — deterministic
  :func:`resolve_platform_identity` mapping.
* :class:`TestResolveCapabilities` — explicit resolver for LINUX /
  MACOS / UNKNOWN; explicit ``adapter_name`` override; explicit
  ``cap_path`` load; ``cap_override`` injection (no host probing).
* :class:`TestGetCapabilitiesCache` — process-wide cache: second call
  returns the same instance; ``force_refresh`` re-resolves;
  ``_reset_cache`` clears it.
* :class:`TestLinuxBehaviorPreserved` — on Linux the resolver produces
  a facade byte-identical (modulo ``source``) to
  :data:`~aee.deploy.capabilities.LinuxDefaults`.
* :class:`TestMacOSContractViaInjection` — macOS skeleton selected via
  injected :data:`PlatformIdentity.MACOS` with a synthetic
  ``HostCapabilities(os='darwin')``; no host command executed; the
  facade reports ``is_macos == True`` and ``profile_supported ==
  ('developer',)``.
* :class:`TestUnknownFallback` — unknown platform returns
  :data:`UnknownDefaults` without raising.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from aee.deploy.capabilities import (
    LinuxDefaults,
    MacOSDefaults,
    UnknownDefaults,
)
from aee.deploy.contract import HostCapabilities
from aee.platform.current import (
    PlatformIdentity,
    _reset_cache,
    get_capabilities,
    resolve_capabilities,
    resolve_platform_identity,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _darwin_cap() -> HostCapabilities:
    """Synthetic darwin HostCapabilities — used to test the macOS
    skeleton without invoking ``MacBookAdapter.detect()`` on a Linux
    host (which would honestly return ``os='linux'``).
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


def _linux_cap() -> HostCapabilities:
    return HostCapabilities(
        name="m2-abacus",
        class_="container",
        os="linux",
        arch="x86_64",
        python=">=3.11",
        supervisor="supervisord",
        network_egress="tunnel",
        tunnel_kind="cloudflared",
        persistent_paths=("/home/ubuntu",),
        runtime_profile_supported=("full", "mini", "edge", "developer"),
        runtime_profile_default="full",
        upstream_llm_reachable=True,
        detected=True,
        source="detected",
    )


# ---------------------------------------------------------------------------
# resolve_platform_identity
# ---------------------------------------------------------------------------


class TestPlatformIdentity(unittest.TestCase):
    def test_linux(self) -> None:
        self.assertEqual(resolve_platform_identity("linux"), PlatformIdentity.LINUX)
        self.assertEqual(resolve_platform_identity("linux2"), PlatformIdentity.LINUX)

    def test_darwin(self) -> None:
        self.assertEqual(resolve_platform_identity("darwin"), PlatformIdentity.MACOS)

    def test_unknown(self) -> None:
        self.assertEqual(resolve_platform_identity(""), PlatformIdentity.UNKNOWN)
        self.assertEqual(resolve_platform_identity("win32"), PlatformIdentity.UNKNOWN)
        self.assertEqual(
            resolve_platform_identity("freebsd"), PlatformIdentity.UNKNOWN
        )

    def test_reads_sys_platform_when_none(self) -> None:
        with patch("aee.platform.current.sys") as mock_sys:
            mock_sys.platform = "linux"
            self.assertEqual(resolve_platform_identity(), PlatformIdentity.LINUX)
        with patch("aee.platform.current.sys") as mock_sys:
            mock_sys.platform = "darwin"
            self.assertEqual(resolve_platform_identity(), PlatformIdentity.MACOS)
        with patch("aee.platform.current.sys") as mock_sys:
            mock_sys.platform = "haiku"
            self.assertEqual(resolve_platform_identity(), PlatformIdentity.UNKNOWN)


# ---------------------------------------------------------------------------
# resolve_capabilities
# ---------------------------------------------------------------------------


class TestResolveCapabilities(unittest.TestCase):
    def test_linux_default_selects_abacus(self) -> None:
        cap = resolve_capabilities(platform_id=PlatformIdentity.LINUX)
        self.assertEqual(cap.host_root, "/home/ubuntu")
        self.assertTrue(cap.is_linux)
        self.assertEqual(cap.supervisor_kind, "supervisord")
        # detect() on the actual Linux host returns source='detected'
        self.assertEqual(cap.source, "detected")

    def test_macos_with_cap_override(self) -> None:
        cap = resolve_capabilities(
            platform_id=PlatformIdentity.MACOS,
            cap_override=_darwin_cap(),
        )
        self.assertTrue(cap.is_macos)
        self.assertEqual(cap.host_root, "/Users")
        self.assertEqual(cap.supervisor_kind, "launchd")
        self.assertEqual(cap.profile_supported, ("developer",))

    def test_unknown_returns_unknown_defaults(self) -> None:
        cap = resolve_capabilities(platform_id=PlatformIdentity.UNKNOWN)
        self.assertEqual(cap.os_name, "unknown")
        self.assertEqual(cap.profile_supported, ())
        self.assertFalse(cap.db_path_writable)
        # UnknownDefaults is the canonical instance.
        self.assertEqual(cap, UnknownDefaults)

    def test_explicit_adapter_name_override(self) -> None:
        # 'zo' is registered by name; selecting it should produce the
        # Zo adapter's detect() result (a container-class host).
        cap = resolve_capabilities(
            platform_id=PlatformIdentity.LINUX,
            adapter_name="zo",
        )
        # Zo adapter's detect() returns a container-class HostCapabilities;
        # the facade should reflect that regardless of platform_id.
        self.assertEqual(cap.host_class, "container")

    def test_cap_path_loads_yaml(self) -> None:
        cap = resolve_capabilities(
            platform_id=PlatformIdentity.LINUX,
            cap_path="aee/deploy/samples/host.capabilities.m2-abacus.yaml",
        )
        self.assertEqual(cap.host_root, "/home/ubuntu")
        self.assertEqual(cap.os_name, "linux")
        self.assertEqual(cap.supervisor_kind, "supervisord")
        self.assertEqual(cap.profile_default, "full")
        # loader sets source = the file path
        self.assertIn("m2-abacus", cap.source)

    def test_cap_override_takes_precedence_over_path(self) -> None:
        cap = resolve_capabilities(
            platform_id=PlatformIdentity.LINUX,
            cap_path="aee/deploy/samples/host.capabilities.m2-abacus.yaml",
            cap_override=_darwin_cap(),
        )
        # cap_override wins
        self.assertTrue(cap.is_macos)
        self.assertEqual(cap.host_root, "/Users")


# ---------------------------------------------------------------------------
# get_capabilities cache
# ---------------------------------------------------------------------------


class TestGetCapabilitiesCache(unittest.TestCase):
    def setUp(self) -> None:
        _reset_cache()

    def tearDown(self) -> None:
        _reset_cache()

    def test_cache_returns_same_instance(self) -> None:
        a = get_capabilities()
        b = get_capabilities()
        self.assertIs(a, b)

    def test_force_refresh_returns_new_instance(self) -> None:
        a = get_capabilities()
        b = get_capabilities(force_refresh=True)
        # New instance on refresh (resolve_capabilities rebuilds).
        self.assertIsNot(a, b)
        # But same field values on Linux.
        self.assertEqual(a.host_root, b.host_root)
        self.assertEqual(a.os_name, b.os_name)

    def test_first_call_resolver_kwargs_applied(self) -> None:
        # Inject cap_override to force a known macOS result on first call.
        cap = get_capabilities(
            platform_id=PlatformIdentity.MACOS,
            cap_override=_darwin_cap(),
        )
        self.assertTrue(cap.is_macos)
        # Subsequent call returns the cached macOS instance even with
        # different kwargs (kwargs ignored on cached call).
        cap2 = get_capabilities()
        self.assertIs(cap, cap2)
        self.assertTrue(cap2.is_macos)


# ---------------------------------------------------------------------------
# Linux behavior preserved (byte-identical modulo source)
# ---------------------------------------------------------------------------


class TestLinuxBehaviorPreserved(unittest.TestCase):
    def test_linux_resolved_matches_linuxdefaults_fields(self) -> None:
        cap = resolve_capabilities(platform_id=PlatformIdentity.LINUX)
        for field_name in (
            "host_root", "persistent_paths", "supervisor_kind",
            "network_egress_kind", "os_name", "arch",
            "profile_supported", "profile_default", "inbound_allowed",
            "host_class", "provider_hint", "tunnel_kind",
        ):
            self.assertEqual(
                getattr(LinuxDefaults, field_name),
                getattr(cap, field_name),
                f"field mismatch on {field_name}",
            )
        # Writable flags on the actual host should match the defaults
        # (M2 has both writable).
        self.assertTrue(cap.db_path_writable)
        self.assertTrue(cap.tempdir_writable)


# ---------------------------------------------------------------------------
# macOS contract via injection (no host command)
# ---------------------------------------------------------------------------


class TestMacOSContractViaInjection(unittest.TestCase):
    def test_macos_skeleton_honest_capabilities(self) -> None:
        cap = resolve_capabilities(
            platform_id=PlatformIdentity.MACOS,
            cap_override=_darwin_cap(),
        )
        self.assertTrue(cap.is_macos)
        self.assertEqual(cap.os_name, "darwin")
        self.assertEqual(cap.host_root, "/Users")
        self.assertEqual(cap.supervisor_kind, "launchd")
        self.assertEqual(cap.network_egress_kind, "direct")
        self.assertEqual(cap.profile_supported, ("developer",))
        self.assertEqual(cap.profile_default, "developer")
        self.assertFalse(cap.inbound_allowed)
        self.assertEqual(cap.host_class, "laptop")

    def test_macos_defaults_match_darwin_injection_fields(self) -> None:
        cap = resolve_capabilities(
            platform_id=PlatformIdentity.MACOS,
            cap_override=_darwin_cap(),
        )
        for field_name in (
            "host_root", "persistent_paths", "supervisor_kind",
            "network_egress_kind", "os_name", "profile_supported",
            "profile_default", "inbound_allowed", "host_class",
            "tunnel_kind",
        ):
            self.assertEqual(
                getattr(MacOSDefaults, field_name),
                getattr(cap, field_name),
                f"field mismatch on {field_name}",
            )

    def test_no_host_command_invoked(self) -> None:
        # The macOS path with cap_override does not call detect() and
        # therefore does not invoke any subprocess. Verify by patching
        # subprocess.run to raise if called.
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = AssertionError(
                "subprocess.run must not be called for macOS skeleton"
            )
            cap = resolve_capabilities(
                platform_id=PlatformIdentity.MACOS,
                cap_override=_darwin_cap(),
            )
            self.assertTrue(cap.is_macos)
            mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Unknown fallback
# ---------------------------------------------------------------------------


class TestUnknownFallback(unittest.TestCase):
    def test_unknown_does_not_raise(self) -> None:
        cap = resolve_capabilities(platform_id=PlatformIdentity.UNKNOWN)
        self.assertEqual(cap.os_name, "unknown")

    def test_unknown_with_no_adapter_name(self) -> None:
        # Even with UNKNOWN, no adapter_name → UnknownDefaults.
        cap = resolve_capabilities(
            platform_id=PlatformIdentity.UNKNOWN,
            adapter_name=None,
        )
        self.assertEqual(cap, UnknownDefaults)

    def test_unknown_with_cap_override_still_uses_override(self) -> None:
        # cap_override always wins, even for unknown platform.
        cap = resolve_capabilities(
            platform_id=PlatformIdentity.UNKNOWN,
            cap_override=_linux_cap(),
        )
        self.assertEqual(cap.os_name, "linux")

    def test_unknown_via_sys_platform_mock(self) -> None:
        with patch("aee.platform.current.sys") as mock_sys:
            mock_sys.platform = "solaris"
            cap = resolve_capabilities()
            self.assertEqual(cap.os_name, "unknown")
            self.assertEqual(cap, UnknownDefaults)


if __name__ == "__main__":
    unittest.main()