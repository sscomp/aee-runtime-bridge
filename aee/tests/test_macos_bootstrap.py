"""Targeted unit tests for AEE Bootstrap v1 W3 — macOS bootstrap.

Covers `aee.installer.macos_bootstrap` (the Python-side testable core
for W3). stdlib `unittest` only — no pytest, no subprocess, no network,
no real macOS host.

W3 scope: macOS ONLY. Tests assert the honest-scope contract
(UnsupportedPlatformError for non-Darwin kernels) and the profile-gating
rules from spec §6.2 + §13.3 (macOS v1 supports only `developer`).
"""
from __future__ import annotations

import os
import tempfile
import unittest
from typing import List

from aee.installer.macos_bootstrap import (
    ALWAYS_INSTALL,
    EXIT_DEPENDENCY_FLOOR_NOT_MET,
    EXIT_NETWORK_ERROR,
    EXIT_OK,
    EXIT_PARSE_ERROR,
    EXIT_STAGE_FAILED_RETRYABLE,
    MACOS_SUPPORTED_PROFILES,
    PROFILE_GATED,
    SUPPORTED_PLATFORM,
    BrewDependencyPlan,
    MacOSHostInfo,
    ManifestNotFoundError,
    UnsupportedPlatformError,
    UnsupportedProfileError,
    detect_macos_host,
    filter_by_profile,
    parse_manifest,
    parse_uname_kernel,
    plan_brew_dependencies,
    plan_for_current_macos_host,
)


# Path to the real manifest in the repo (for integration-ish tests).
_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
_REAL_MANIFEST = os.path.join(_REPO_ROOT, "bootstrap", "manifests", "brew.deps.txt")


def _write_manifest(path: str, formulae: List[str]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for f in formulae:
            fh.write(f + "\n")


class TestExitConstants(unittest.TestCase):
    """Pin the exit code values (must not collide with verified backend)."""

    def test_exit_ok_is_zero(self) -> None:
        self.assertEqual(EXIT_OK, 0)

    def test_exit_parse_error_is_two(self) -> None:
        self.assertEqual(EXIT_PARSE_ERROR, 2)

    def test_exit_stage_failed_retryable_is_seven(self) -> None:
        self.assertEqual(EXIT_STAGE_FAILED_RETRYABLE, 7)

    def test_exit_network_error_is_ten(self) -> None:
        self.assertEqual(EXIT_NETWORK_ERROR, 10)

    def test_exit_dependency_floor_not_met_is_twelve(self) -> None:
        self.assertEqual(EXIT_DEPENDENCY_FLOOR_NOT_MET, 12)

    def test_no_collision_with_verified_backend_codes(self) -> None:
        # The verified backend codes occupy {0, 2, 3, 4, 5, 6}; the W3
        # codes reuse the W1 proposed constants {7, 10, 12} and the
        # canonical argparse 2. None of these collide with 3, 4, 5, 6.
        verified = {3, 4, 5, 6}
        w3 = {EXIT_OK, EXIT_PARSE_ERROR, EXIT_STAGE_FAILED_RETRYABLE,
              EXIT_NETWORK_ERROR, EXIT_DEPENDENCY_FLOOR_NOT_MET}
        self.assertTrue(verified.isdisjoint(w3 - {EXIT_OK, EXIT_PARSE_ERROR}))


class TestPlatformVocabulary(unittest.TestCase):
    """W3 supports macOS (Darwin) only."""

    def test_supported_platform_is_darwin(self) -> None:
        self.assertEqual(SUPPORTED_PLATFORM, "darwin")

    def test_macos_supported_profiles_developer_only(self) -> None:
        # Spec §13.3: macOS v1 supports only `developer`.
        self.assertEqual(MACOS_SUPPORTED_PROFILES, frozenset({"developer"}))

    def test_linux_not_in_macos_supported(self) -> None:
        self.assertNotIn("linux", MACOS_SUPPORTED_PROFILES)

    def test_mini_not_in_macos_supported(self) -> None:
        self.assertNotIn("mini", MACOS_SUPPORTED_PROFILES)

    def test_full_not_in_macos_supported(self) -> None:
        self.assertNotIn("full", MACOS_SUPPORTED_PROFILES)

    def test_edge_not_in_macos_supported(self) -> None:
        self.assertNotIn("edge", MACOS_SUPPORTED_PROFILES)


class TestMacOSHostInfo(unittest.TestCase):
    """MacOSHostInfo correctly identifies Darwin + Apple Silicon."""

    def test_darwin_apple_silicon_is_supported(self) -> None:
        info = MacOSHostInfo(kernel="Darwin", brew_prefix="/opt/homebrew")
        self.assertTrue(info.is_supported)
        self.assertTrue(info.is_apple_silicon)

    def test_darwin_intel_is_supported_not_apple_silicon(self) -> None:
        info = MacOSHostInfo(kernel="Darwin", brew_prefix="/usr/local")
        self.assertTrue(info.is_supported)
        self.assertFalse(info.is_apple_silicon)

    def test_linux_not_supported(self) -> None:
        info = MacOSHostInfo(kernel="Linux", brew_prefix="/usr/local")
        self.assertFalse(info.is_supported)

    def test_unknown_not_supported(self) -> None:
        info = MacOSHostInfo(kernel="unknown", brew_prefix="unknown")
        self.assertFalse(info.is_supported)

    def test_frozen(self) -> None:
        info = MacOSHostInfo(kernel="Darwin", brew_prefix="/opt/homebrew")
        with self.assertRaises(Exception):
            # frozen dataclass — attribute assignment raises
            info.kernel = "Linux"  # type: ignore[misc]


class TestParseUnameKernel(unittest.TestCase):
    """parse_uname_kernel trims and handles empty input."""

    def test_darwin(self) -> None:
        self.assertEqual(parse_uname_kernel("Darwin"), "Darwin")

    def test_linux(self) -> None:
        self.assertEqual(parse_uname_kernel("Linux"), "Linux")

    def test_trims_whitespace(self) -> None:
        self.assertEqual(parse_uname_kernel("  Darwin\n"), "Darwin")

    def test_empty_returns_unknown(self) -> None:
        self.assertEqual(parse_uname_kernel(""), "unknown")

    def test_whitespace_only_returns_unknown(self) -> None:
        self.assertEqual(parse_uname_kernel("   \n  "), "unknown")


class TestDetectMacOSHost(unittest.TestCase):
    """detect_macos_host builds MacOSHostInfo from probed values."""

    def test_default_is_darwin_apple_silicon(self) -> None:
        # Defaults reflect Apple Silicon (the current B2 reference host).
        info = detect_macos_host()
        self.assertEqual(info.kernel, "Darwin")
        self.assertEqual(info.brew_prefix, "/opt/homebrew")
        self.assertTrue(info.is_supported)
        self.assertTrue(info.is_apple_silicon)

    def test_intel_host(self) -> None:
        info = detect_macos_host(uname_s="Darwin", brew_prefix="/usr/local")
        self.assertTrue(info.is_supported)
        self.assertFalse(info.is_apple_silicon)


class TestParseManifest(unittest.TestCase):
    """parse_manifest reads brew.deps.txt and strips comments/blanks."""

    def test_reads_real_manifest(self) -> None:
        # The real manifest must exist and include git + python@3.11.
        self.assertTrue(
            os.path.isfile(_REAL_MANIFEST),
            f"brew.deps.txt not found at {_REAL_MANIFEST}",
        )
        formulae = parse_manifest(_REAL_MANIFEST)
        self.assertIn("git", formulae)
        self.assertIn("python@3.11", formulae)
        self.assertIn("curl", formulae)
        self.assertIn("ca-certificates", formulae)

    def test_skips_comments_and_blanks(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "brew.deps.txt")
            _write_manifest(path, ["# comment", "", "git", "  ", "python@3.11"])
            formulae = parse_manifest(path)
            self.assertEqual(formulae, ["git", "python@3.11"])

    def test_preserves_order(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "brew.deps.txt")
            _write_manifest(path, ["git", "python@3.11", "curl", "ca-certificates"])
            formulae = parse_manifest(path)
            self.assertEqual(
                formulae,
                ["git", "python@3.11", "curl", "ca-certificates"],
            )

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(ManifestNotFoundError):
            parse_manifest("/nonexistent/brew.deps.txt")


class TestFilterByProfile(unittest.TestCase):
    """filter_by_profile applies §6.2 gating + macOS v1 whitelist."""

    def test_developer_excludes_supervisor_and_docker(self) -> None:
        # On macOS v1 only developer is supported; supervisor (mini+full)
        # and docker (full+edge) are gated out.
        formulae = ["git", "python@3.11", "curl", "ca-certificates",
                    "supervisor", "docker"]
        filtered = filter_by_profile(formulae, "developer")
        self.assertIn("git", filtered)
        self.assertIn("python@3.11", filtered)
        self.assertIn("curl", filtered)
        self.assertIn("ca-certificates", filtered)
        self.assertNotIn("supervisor", filtered)
        self.assertNotIn("docker", filtered)

    def test_mini_includes_supervisor_excludes_docker(self) -> None:
        # mini allows supervisor (mini+full) but not docker (full+edge).
        formulae = ["git", "supervisor", "docker"]
        filtered = filter_by_profile(formulae, "mini")
        self.assertIn("supervisor", filtered)
        self.assertNotIn("docker", filtered)

    def test_full_includes_supervisor_and_docker(self) -> None:
        formulae = ["git", "supervisor", "docker"]
        filtered = filter_by_profile(formulae, "full")
        self.assertIn("supervisor", filtered)
        self.assertIn("docker", filtered)

    def test_edge_includes_docker_excludes_supervisor(self) -> None:
        formulae = ["git", "supervisor", "docker"]
        filtered = filter_by_profile(formulae, "edge")
        self.assertNotIn("supervisor", filtered)
        self.assertIn("docker", filtered)

    def test_always_install_included_for_all_profiles(self) -> None:
        for profile in ("full", "mini", "edge", "developer"):
            filtered = filter_by_profile(list(ALWAYS_INSTALL), profile)
            for f in ALWAYS_INSTALL:
                self.assertIn(f, filtered,
                               f"{f} missing for profile {profile}")

    def test_invalid_profile_raises(self) -> None:
        with self.assertRaises(UnsupportedProfileError):
            filter_by_profile(["git"], "bogus")


class TestProfileGatedTable(unittest.TestCase):
    """Pin the PROFILE_GATED gating sets (spec §6.2)."""

    def test_supervisor_mini_full(self) -> None:
        self.assertEqual(PROFILE_GATED["supervisor"], frozenset({"mini", "full"}))

    def test_docker_full_edge(self) -> None:
        self.assertEqual(PROFILE_GATED["docker"], frozenset({"full", "edge"}))

    def test_no_unknown_formulae_in_gated_table(self) -> None:
        # Only supervisor and docker are profile-gated; everything else
        # in the manifest is a core hard dep.
        self.assertEqual(set(PROFILE_GATED.keys()), {"supervisor", "docker"})


class TestAlwaysInstall(unittest.TestCase):
    """Pin the ALWAYS_INSTALL hard-dep set (spec §6.1, macOS formula names)."""

    def test_includes_git(self) -> None:
        self.assertIn("git", ALWAYS_INSTALL)

    def test_includes_python_3_11(self) -> None:
        self.assertIn("python@3.11", ALWAYS_INSTALL)

    def test_includes_curl(self) -> None:
        self.assertIn("curl", ALWAYS_INSTALL)

    def test_includes_ca_certificates(self) -> None:
        self.assertIn("ca-certificates", ALWAYS_INSTALL)

    def test_excludes_supervisor(self) -> None:
        self.assertNotIn("supervisor", ALWAYS_INSTALL)

    def test_excludes_docker(self) -> None:
        self.assertNotIn("docker", ALWAYS_INSTALL)

    def test_frozen(self) -> None:
        self.assertIsInstance(ALWAYS_INSTALL, frozenset)


class TestPlanBrewDependencies(unittest.TestCase):
    """plan_brew_dependencies produces a BrewDependencyPlan for macOS."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._manifest = os.path.join(self._tmp, "brew.deps.txt")
        _write_manifest(
            self._manifest,
            ["git", "python@3.11", "curl", "ca-certificates",
             "supervisor", "docker"],
        )

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _darwin_host(self) -> MacOSHostInfo:
        return MacOSHostInfo(kernel="Darwin", brew_prefix="/opt/homebrew")

    def test_darwin_developer_plan(self) -> None:
        plan = plan_brew_dependencies(
            host_info=self._darwin_host(),
            profile="developer",
            manifest_path=self._manifest,
            homebrew_available=True,
            dry_run=True,
        )
        self.assertTrue(plan.host_info.is_supported)
        self.assertEqual(plan.profile, "developer")
        # supervisor + docker gated out for developer
        self.assertNotIn("supervisor", plan.formulae)
        self.assertNotIn("docker", plan.formulae)
        # core hard deps included
        self.assertIn("git", plan.formulae)
        self.assertIn("python@3.11", plan.formulae)
        self.assertIn("curl", plan.formulae)
        self.assertIn("ca-certificates", plan.formulae)
        self.assertTrue(plan.needs_uv_pip_install)
        self.assertFalse(plan.needs_homebrew_install)
        self.assertTrue(plan.dry_run)
        self.assertEqual(plan.formulae_count, 4)

    def test_darwin_developer_brew_command_has_dry_run_prefix(self) -> None:
        plan = plan_brew_dependencies(
            host_info=self._darwin_host(),
            profile="developer",
            manifest_path=self._manifest,
            dry_run=True,
        )
        self.assertIn("[dry-run]", plan.brew_command)
        self.assertIn("brew install --quiet", plan.brew_command)

    def test_darwin_developer_execute_no_dry_run_prefix(self) -> None:
        plan = plan_brew_dependencies(
            host_info=self._darwin_host(),
            profile="developer",
            manifest_path=self._manifest,
            dry_run=False,
        )
        self.assertNotIn("[dry-run]", plan.brew_command)
        self.assertTrue(plan.brew_command.startswith("brew install --quiet"))

    def test_linux_host_raises_unsupported_platform(self) -> None:
        host = MacOSHostInfo(kernel="Linux", brew_prefix="/usr/local")
        with self.assertRaises(UnsupportedPlatformError):
            plan_brew_dependencies(
                host_info=host,
                profile="developer",
                manifest_path=self._manifest,
            )

    def test_unknown_host_raises_unsupported_platform(self) -> None:
        host = MacOSHostInfo(kernel="unknown", brew_prefix="unknown")
        with self.assertRaises(UnsupportedPlatformError):
            plan_brew_dependencies(
                host_info=host,
                profile="developer",
                manifest_path=self._manifest,
            )

    def test_mini_profile_raises_unsupported_profile(self) -> None:
        # macOS v1 supports only developer; mini is rejected by the
        # planner's defence-in-depth profile whitelist.
        with self.assertRaises(UnsupportedProfileError):
            plan_brew_dependencies(
                host_info=self._darwin_host(),
                profile="mini",
                manifest_path=self._manifest,
            )

    def test_full_profile_raises_unsupported_profile(self) -> None:
        with self.assertRaises(UnsupportedProfileError):
            plan_brew_dependencies(
                host_info=self._darwin_host(),
                profile="full",
                manifest_path=self._manifest,
            )

    def test_edge_profile_raises_unsupported_profile(self) -> None:
        with self.assertRaises(UnsupportedProfileError):
            plan_brew_dependencies(
                host_info=self._darwin_host(),
                profile="edge",
                manifest_path=self._manifest,
            )

    def test_bogus_profile_raises_unsupported_profile(self) -> None:
        with self.assertRaises(UnsupportedProfileError):
            plan_brew_dependencies(
                host_info=self._darwin_host(),
                profile="bogus",
                manifest_path=self._manifest,
            )

    def test_missing_manifest_raises(self) -> None:
        with self.assertRaises(ManifestNotFoundError):
            plan_brew_dependencies(
                host_info=self._darwin_host(),
                profile="developer",
                manifest_path="/nonexistent/brew.deps.txt",
            )

    def test_brew_missing_no_no_brew_needs_homebrew_install(self) -> None:
        plan = plan_brew_dependencies(
            host_info=self._darwin_host(),
            profile="developer",
            manifest_path=self._manifest,
            homebrew_available=False,
            no_brew=False,
            dry_run=True,
        )
        self.assertTrue(plan.needs_homebrew_install)
        self.assertFalse(plan.no_brew)

    def test_brew_missing_with_no_brew_no_homebrew_install(self) -> None:
        plan = plan_brew_dependencies(
            host_info=self._darwin_host(),
            profile="developer",
            manifest_path=self._manifest,
            homebrew_available=False,
            no_brew=True,
            dry_run=True,
        )
        self.assertFalse(plan.needs_homebrew_install)
        self.assertTrue(plan.no_brew)

    def test_brew_present_no_homebrew_install(self) -> None:
        plan = plan_brew_dependencies(
            host_info=self._darwin_host(),
            profile="developer",
            manifest_path=self._manifest,
            homebrew_available=True,
            no_brew=False,
            dry_run=True,
        )
        self.assertFalse(plan.needs_homebrew_install)

    def test_idempotent_planning(self) -> None:
        # Same (host, profile, manifest) always yields the same plan.
        plan1 = plan_brew_dependencies(
            host_info=self._darwin_host(),
            profile="developer",
            manifest_path=self._manifest,
        )
        plan2 = plan_brew_dependencies(
            host_info=self._darwin_host(),
            profile="developer",
            manifest_path=self._manifest,
        )
        self.assertEqual(plan1, plan2)

    def test_apple_silicon_vs_intel_produces_equivalent_plans(self) -> None:
        # The plan only differs in host_info; formulae are the same
        # because the manifest does not branch on brew prefix.
        apple = plan_brew_dependencies(
            host_info=MacOSHostInfo(kernel="Darwin", brew_prefix="/opt/homebrew"),
            profile="developer",
            manifest_path=self._manifest,
        )
        intel = plan_brew_dependencies(
            host_info=MacOSHostInfo(kernel="Darwin", brew_prefix="/usr/local"),
            profile="developer",
            manifest_path=self._manifest,
        )
        self.assertEqual(apple.formulae, intel.formulae)
        self.assertNotEqual(apple.host_info.brew_prefix, intel.host_info.brew_prefix)


class TestPlanForCurrentMacOSHost(unittest.TestCase):
    """plan_for_current_macos_host uses detect_macos_host defaults.

    On a non-Mac CI host, detect_macos_host defaults to Darwin (the
    function does NOT probe the live host — that is the shell layer's
    job). The planner therefore succeeds in unit tests even on Linux,
    which is the W2 pattern (linux_bootstrap.plan_for_current_host also
    works on non-Debian hosts in unit tests via parse_os_release with
    injected content).
    """

    def test_plan_for_default_host_developer(self) -> None:
        plan = plan_for_current_macos_host(
            profile="developer",
            repo_root=_REPO_ROOT,
            homebrew_available=True,
            dry_run=True,
        )
        self.assertEqual(plan.profile, "developer")
        self.assertTrue(plan.host_info.is_supported)
        # The real manifest is loaded
        self.assertIn("git", plan.formulae)
        self.assertIn("python@3.11", plan.formulae)

    def test_plan_for_default_host_no_brew(self) -> None:
        plan = plan_for_current_macos_host(
            profile="developer",
            repo_root=_REPO_ROOT,
            homebrew_available=False,
            no_brew=True,
            dry_run=True,
        )
        self.assertTrue(plan.no_brew)
        self.assertFalse(plan.needs_homebrew_install)


class TestBrewDependencyPlanProperties(unittest.TestCase):
    """BrewDependencyPlan properties return correct values."""

    def test_formulae_count(self) -> None:
        plan = BrewDependencyPlan(
            host_info=MacOSHostInfo(kernel="Darwin", brew_prefix="/opt/homebrew"),
            profile="developer",
            formulae=("git", "python@3.11", "curl", "ca-certificates"),
            needs_homebrew_install=False,
            needs_uv_pip_install=True,
            dry_run=True,
        )
        self.assertEqual(plan.formulae_count, 4)

    def test_brew_command_dry_run(self) -> None:
        plan = BrewDependencyPlan(
            host_info=MacOSHostInfo(kernel="Darwin", brew_prefix="/opt/homebrew"),
            profile="developer",
            formulae=("git", "python@3.11"),
            needs_homebrew_install=False,
            needs_uv_pip_install=True,
            dry_run=True,
        )
        self.assertEqual(
            plan.brew_command,
            "[dry-run] brew install --quiet git python@3.11",
        )

    def test_brew_command_execute(self) -> None:
        plan = BrewDependencyPlan(
            host_info=MacOSHostInfo(kernel="Darwin", brew_prefix="/opt/homebrew"),
            profile="developer",
            formulae=("git", "python@3.11"),
            needs_homebrew_install=False,
            needs_uv_pip_install=True,
            dry_run=False,
        )
        self.assertEqual(
            plan.brew_command,
            "brew install --quiet git python@3.11",
        )

    def test_frozen(self) -> None:
        plan = BrewDependencyPlan(
            host_info=MacOSHostInfo(kernel="Darwin", brew_prefix="/opt/homebrew"),
            profile="developer",
            formulae=("git",),
            needs_homebrew_install=False,
            needs_uv_pip_install=True,
        )
        with self.assertRaises(Exception):
            plan.profile = "mini"  # type: ignore[misc]


class TestRealManifestContent(unittest.TestCase):
    """Integration-ish: the real brew.deps.txt has the expected content."""

    def test_real_manifest_has_core_hard_deps(self) -> None:
        formulae = parse_manifest(_REAL_MANIFEST)
        for f in ALWAYS_INSTALL:
            self.assertIn(f, formulae,
                          f"core hard dep {f} missing from brew.deps.txt")

    def test_real_manifest_lists_supervisor_and_docker_for_parity(self) -> None:
        # supervisor + docker are in the manifest (profile-gated, never
        # installed on macOS v1 developer profile, but present for
        # parity + future profile expansion).
        formulae = parse_manifest(_REAL_MANIFEST)
        self.assertIn("supervisor", formulae)
        self.assertIn("docker", formulae)

    def test_real_manifest_no_duplicates(self) -> None:
        formulae = parse_manifest(_REAL_MANIFEST)
        self.assertEqual(len(formulae), len(set(formulae)))

    def test_real_manifest_does_not_list_python_unversioned(self) -> None:
        # The unversioned `python` formula is NOT in the manifest;
        # `python@3.11` is the canonical pinned path (spec §6.3).
        formulae = parse_manifest(_REAL_MANIFEST)
        self.assertNotIn("python", formulae)


class TestHonestScopeContract(unittest.TestCase):
    """W3 honest-scope contract: refuse non-Darwin, refuse non-developer.

    This is the W3 counterpart to the W2 honest-scope contract
    (UnsupportedDistroError for non-ubuntu/debian). W3 refuses:
      * non-Darwin kernels (UnsupportedPlatformError)
      * non-developer profiles (UnsupportedProfileError, defence in depth
        on top of the macOS v1 profile whitelist)
    """

    def test_linux_kernel_refused(self) -> None:
        host = MacOSHostInfo(kernel="Linux", brew_prefix="/usr/local")
        with self.assertRaises(UnsupportedPlatformError) as ctx:
            plan_brew_dependencies(
                host_info=host,
                profile="developer",
                manifest_path=_REAL_MANIFEST,
            )
        self.assertIn("Linux", str(ctx.exception))

    def test_windows_kernel_refused(self) -> None:
        # Windows would report `WindowsNT` or similar from uname -s;
        # the planner refuses it just like Linux.
        host = MacOSHostInfo(kernel="WindowsNT", brew_prefix="unknown")
        with self.assertRaises(UnsupportedPlatformError):
            plan_brew_dependencies(
                host_info=host,
                profile="developer",
                manifest_path=_REAL_MANIFEST,
            )

    def test_mini_refused_on_macos_v1(self) -> None:
        host = MacOSHostInfo(kernel="Darwin", brew_prefix="/opt/homebrew")
        with self.assertRaises(UnsupportedProfileError) as ctx:
            plan_brew_dependencies(
                host_info=host,
                profile="mini",
                manifest_path=_REAL_MANIFEST,
            )
        self.assertIn("mini", str(ctx.exception))
        self.assertIn("developer", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()