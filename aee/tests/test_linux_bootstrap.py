"""Targeted unit tests for AEE Bootstrap v1 W2 — Ubuntu/Debian bootstrap.

Covers `aee.installer.linux_bootstrap` (the Python-side testable core
for W2). stdlib `unittest` only — no pytest, no subprocess, no network.

W2 scope: Ubuntu/Debian ONLY. Tests assert the honest-scope contract
(UnsupportedDistroError for non-ubuntu/debian) and the profile-gating
rules from spec §6.2.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from typing import List

from aee.installer.linux_bootstrap import (
    ALWAYS_INSTALL,
    DEADSNAKES_UBUNTU_VERSIONS,
    DependencyPlan,
    DistroInfo,
    EXIT_DEPENDENCY_FLOOR_NOT_MET,
    EXIT_NETWORK_ERROR,
    EXIT_OK,
    EXIT_PARSE_ERROR,
    EXIT_STAGE_FAILED_RETRYABLE,
    ManifestNotFoundError,
    PROFILE_GATED,
    SUPPORTED_DEBIAN_VERSIONS,
    SUPPORTED_DISTROS,
    UnsupportedDistroError,
    UnsupportedProfileError,
    detect_distro,
    filter_by_profile,
    parse_manifest,
    parse_os_release,
    plan_apt_dependencies,
    plan_for_current_host,
)


# Path to the real manifest in the repo (for integration-ish tests).
_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
_REAL_MANIFEST = os.path.join(_REPO_ROOT, "bootstrap", "manifests", "apt.deps.txt")


def _write_manifest(path: str, packages: List[str]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for pkg in packages:
            fh.write(pkg + "\n")


class TestExitConstants(unittest.TestCase):
    """Verify exit codes are the documented values and do not collide."""

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
        # Verified backend codes occupy {0, 2, 3, 4, 5, 6}.
        verified = {0, 2, 3, 4, 5, 6}
        w2_codes = {EXIT_OK, EXIT_PARSE_ERROR, EXIT_STAGE_FAILED_RETRYABLE,
                    EXIT_NETWORK_ERROR, EXIT_DEPENDENCY_FLOOR_NOT_MET}
        # EXIT_OK=0 and EXIT_PARSE_ERROR=2 overlap intentionally (same
        # verified values). The proposed new codes {7, 10, 12} must not.
        proposed = w2_codes - verified
        self.assertEqual(proposed, {7, 10, 12})


class TestDistroVocabulary(unittest.TestCase):
    """SUPPORTED_DISTROS, DEADSNAKES_UBUNTU_VERSIONS, SUPPORTED_DEBIAN_VERSIONS."""

    def test_supported_distros_ubuntu_debian_only(self) -> None:
        self.assertEqual(SUPPORTED_DISTROS, frozenset({"ubuntu", "debian"}))

    def test_macos_not_in_supported(self) -> None:
        self.assertNotIn("macos", SUPPORTED_DISTROS)

    def test_windows_not_in_supported(self) -> None:
        self.assertNotIn("windows", SUPPORTED_DISTROS)

    def test_deadsnakes_ubuntu_versions(self) -> None:
        self.assertIn("22.04", DEADSNAKES_UBUNTU_VERSIONS)
        self.assertNotIn("24.04", DEADSNAKES_UBUNTU_VERSIONS)

    def test_supported_debian_versions(self) -> None:
        self.assertIn("12", SUPPORTED_DEBIAN_VERSIONS)


class TestDistroInfo(unittest.TestCase):

    def test_ubuntu_2204_is_supported(self) -> None:
        di = DistroInfo(distro="ubuntu", version_id="22.04")
        self.assertTrue(di.is_supported)
        self.assertTrue(di.needs_deadsnakes)

    def test_ubuntu_2404_no_deadsnakes(self) -> None:
        di = DistroInfo(distro="ubuntu", version_id="24.04")
        self.assertTrue(di.is_supported)
        self.assertFalse(di.needs_deadsnakes)

    def test_debian_12_no_deadsnakes(self) -> None:
        di = DistroInfo(distro="debian", version_id="12")
        self.assertTrue(di.is_supported)
        self.assertFalse(di.needs_deadsnakes)

    def test_unknown_distro_not_supported(self) -> None:
        di = DistroInfo(distro="unknown", version_id="x")
        self.assertFalse(di.is_supported)

    def test_frozen(self) -> None:
        di = DistroInfo(distro="ubuntu", version_id="22.04")
        with self.assertRaises((AttributeError, Exception)):
            di.distro = "debian"  # type: ignore[misc]


class TestParseOsRelease(unittest.TestCase):

    def test_ubuntu(self) -> None:
        content = 'ID=ubuntu\nVERSION_ID="22.04"\nNAME="Ubuntu"\n'
        di = parse_os_release(content)
        self.assertEqual(di.distro, "ubuntu")
        self.assertEqual(di.version_id, "22.04")

    def test_debian(self) -> None:
        content = 'ID=debian\nVERSION_ID=12\nVERSION="12 (bookworm)"\n'
        di = parse_os_release(content)
        self.assertEqual(di.distro, "debian")
        self.assertEqual(di.version_id, "12")

    def test_fedora_maps_to_unknown(self) -> None:
        content = 'ID=fedora\nVERSION_ID=39\n'
        di = parse_os_release(content)
        self.assertEqual(di.distro, "unknown")
        # version_id preserved even when distro is unknown
        self.assertEqual(di.version_id, "39")

    def test_empty_content(self) -> None:
        di = parse_os_release("")
        self.assertEqual(di.distro, "unknown")
        self.assertEqual(di.version_id, "unknown")

    def test_comments_and_blanks_skipped(self) -> None:
        content = '# comment\n\nID=ubuntu\n\n# another\nVERSION_ID="24.04"\n'
        di = parse_os_release(content)
        self.assertEqual(di.distro, "ubuntu")
        self.assertEqual(di.version_id, "24.04")

    def test_quotes_stripped(self) -> None:
        content = "ID='debian'\nVERSION_ID=\"12\"\n"
        di = parse_os_release(content)
        self.assertEqual(di.distro, "debian")
        self.assertEqual(di.version_id, "12")

    def test_missing_id(self) -> None:
        content = 'VERSION_ID="12"\n'
        di = parse_os_release(content)
        self.assertEqual(di.distro, "unknown")
        self.assertEqual(di.version_id, "12")


class TestParseManifest(unittest.TestCase):

    def test_reads_real_manifest(self) -> None:
        if not os.path.isfile(_REAL_MANIFEST):
            self.skipTest("real manifest not present")
        pkgs = parse_manifest(_REAL_MANIFEST)
        self.assertIn("git", pkgs)
        self.assertIn("python3", pkgs)
        self.assertIn("python3.11", pkgs)
        self.assertIn("supervisor", pkgs)
        self.assertIn("docker.io", pkgs)

    def test_skips_comments_and_blanks(self) -> None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as fh:
            fh.write("# header\n\ngit\npython3\n# trailing comment\n\n")
            path = fh.name
        try:
            pkgs = parse_manifest(path)
            self.assertEqual(pkgs, ["git", "python3"])
        finally:
            os.unlink(path)

    def test_preserves_order(self) -> None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as fh:
            fh.write("zzz\naaa\nmmm\n")
            path = fh.name
        try:
            pkgs = parse_manifest(path)
            self.assertEqual(pkgs, ["zzz", "aaa", "mmm"])
        finally:
            os.unlink(path)

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(ManifestNotFoundError):
            parse_manifest("/nonexistent/path/apt.deps.txt")


class TestFilterByProfile(unittest.TestCase):

    def test_mini_excludes_docker(self) -> None:
        pkgs = ["git", "python3", "supervisor", "docker.io"]
        filtered = filter_by_profile(pkgs, "mini")
        self.assertIn("supervisor", filtered)
        self.assertNotIn("docker.io", filtered)

    def test_full_includes_supervisor_and_docker(self) -> None:
        pkgs = ["git", "supervisor", "docker.io"]
        filtered = filter_by_profile(pkgs, "full")
        self.assertIn("supervisor", filtered)
        self.assertIn("docker.io", filtered)

    def test_edge_includes_docker_excludes_supervisor(self) -> None:
        pkgs = ["git", "supervisor", "docker.io"]
        filtered = filter_by_profile(pkgs, "edge")
        self.assertNotIn("supervisor", filtered)
        self.assertIn("docker.io", filtered)

    def test_developer_excludes_both(self) -> None:
        pkgs = ["git", "supervisor", "docker.io"]
        filtered = filter_by_profile(pkgs, "developer")
        self.assertNotIn("supervisor", filtered)
        self.assertNotIn("docker.io", filtered)
        self.assertIn("git", filtered)

    def test_always_install_included_for_all_profiles(self) -> None:
        for profile in ("full", "mini", "edge", "developer"):
            for pkg in ("git", "python3", "curl", "ca-certificates"):
                self.assertIn(
                    pkg,
                    filter_by_profile([pkg], profile),
                    f"{pkg} missing for profile={profile}",
                )

    def test_invalid_profile_raises(self) -> None:
        with self.assertRaises(UnsupportedProfileError):
            filter_by_profile(["git"], "bogus")


class TestProfileGatedTable(unittest.TestCase):

    def test_supervisor_mini_full(self) -> None:
        self.assertEqual(PROFILE_GATED["supervisor"], frozenset({"mini", "full"}))

    def test_docker_full_edge(self) -> None:
        self.assertEqual(PROFILE_GATED["docker.io"], frozenset({"full", "edge"}))


class TestPlanAptDependencies(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.manifest = os.path.join(self.tmp, "apt.deps.txt")
        _write_manifest(
            self.manifest,
            [
                "git",
                "python3",
                "python3-pip",
                "python3-venv",
                "curl",
                "ca-certificates",
                "gnupg",
                "python3.11",
                "python3.11-venv",
                "supervisor",
                "docker.io",
            ],
        )

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ubuntu_2204_mini_plan(self) -> None:
        di = DistroInfo(distro="ubuntu", version_id="22.04")
        plan = plan_apt_dependencies(di, "mini", self.manifest)
        self.assertTrue(plan.distro_info.is_supported)
        self.assertTrue(plan.needs_deadsnakes)
        self.assertIn("supervisor", plan.packages)
        self.assertNotIn("docker.io", plan.packages)
        self.assertIn("git", plan.packages)
        self.assertEqual(plan.profile, "mini")
        self.assertTrue(plan.needs_uv_pip_install)
        self.assertTrue(plan.dry_run)

    def test_ubuntu_2404_full_plan_no_deadsnakes(self) -> None:
        di = DistroInfo(distro="ubuntu", version_id="24.04")
        plan = plan_apt_dependencies(di, "full", self.manifest)
        self.assertFalse(plan.needs_deadsnakes)
        self.assertIn("docker.io", plan.packages)
        self.assertIn("supervisor", plan.packages)

    def test_debian_12_edge_plan(self) -> None:
        di = DistroInfo(distro="debian", version_id="12")
        plan = plan_apt_dependencies(di, "edge", self.manifest)
        self.assertFalse(plan.needs_deadsnakes)
        self.assertIn("docker.io", plan.packages)
        self.assertNotIn("supervisor", plan.packages)

    def test_developer_excludes_gated(self) -> None:
        di = DistroInfo(distro="debian", version_id="12")
        plan = plan_apt_dependencies(di, "developer", self.manifest)
        self.assertNotIn("supervisor", plan.packages)
        self.assertNotIn("docker.io", plan.packages)

    def test_unknown_distro_raises(self) -> None:
        di = DistroInfo(distro="unknown", version_id="x")
        with self.assertRaises(UnsupportedDistroError):
            plan_apt_dependencies(di, "mini", self.manifest)

    def test_fedora_raises(self) -> None:
        di = DistroInfo(distro="fedora", version_id="39")
        # parse_os_release maps fedora→unknown, but DistroInfo can be
        # constructed directly. plan_apt_dependencies checks is_supported.
        di_unknown = DistroInfo(distro="unknown", version_id="39")
        with self.assertRaises(UnsupportedDistroError):
            plan_apt_dependencies(di_unknown, "mini", self.manifest)

    def test_invalid_profile_raises(self) -> None:
        di = DistroInfo(distro="ubuntu", version_id="22.04")
        with self.assertRaises(UnsupportedProfileError):
            plan_apt_dependencies(di, "bogus", self.manifest)

    def test_missing_manifest_raises(self) -> None:
        di = DistroInfo(distro="ubuntu", version_id="22.04")
        with self.assertRaises(ManifestNotFoundError):
            plan_apt_dependencies(di, "mini", "/nonexistent/manifest.txt")

    def test_dry_run_false_sets_flag(self) -> None:
        di = DistroInfo(distro="debian", version_id="12")
        plan = plan_apt_dependencies(di, "full", self.manifest, dry_run=False)
        self.assertFalse(plan.dry_run)

    def test_apt_command_format_dry_run(self) -> None:
        di = DistroInfo(distro="debian", version_id="12")
        plan = plan_apt_dependencies(di, "mini", self.manifest, dry_run=True)
        cmd = plan.apt_command
        self.assertIn("[dry-run]", cmd)
        self.assertIn("apt-get install", cmd)
        self.assertIn("--no-install-recommends", cmd)

    def test_apt_command_format_execute(self) -> None:
        di = DistroInfo(distro="debian", version_id="12")
        plan = plan_apt_dependencies(di, "mini", self.manifest, dry_run=False)
        cmd = plan.apt_command
        self.assertNotIn("[dry-run]", cmd)
        self.assertIn("sudo apt-get install", cmd)

    def test_package_count(self) -> None:
        di = DistroInfo(distro="debian", version_id="12")
        plan = plan_apt_dependencies(di, "developer", self.manifest)
        # developer: 9 core deps, no supervisor, no docker.io
        self.assertEqual(plan.package_count, 9)

    def test_idempotent_planning(self) -> None:
        di = DistroInfo(distro="ubuntu", version_id="22.04")
        p1 = plan_apt_dependencies(di, "mini", self.manifest)
        p2 = plan_apt_dependencies(di, "mini", self.manifest)
        self.assertEqual(p1, p2)

    def test_plan_is_frozen(self) -> None:
        di = DistroInfo(distro="ubuntu", version_id="22.04")
        plan = plan_apt_dependencies(di, "mini", self.manifest)
        with self.assertRaises((AttributeError, Exception)):
            plan.profile = "full"  # type: ignore[misc]


class TestPlanForCurrentHost(unittest.TestCase):

    def test_returns_plan_on_debian_or_raises_on_other(self) -> None:
        # This host is Debian 12 per /etc/os-release.
        try:
            plan = plan_for_current_host("mini", _REPO_ROOT)
            self.assertIn(plan.distro_info.distro, SUPPORTED_DISTROS)
        except UnsupportedDistroError:
            # Acceptable if the host is not Ubuntu/Debian.
            pass

    def test_invalid_profile_raises(self) -> None:
        with self.assertRaises(UnsupportedProfileError):
            plan_for_current_host("bogus", _REPO_ROOT)


class TestDetectDistro(unittest.TestCase):

    def test_detect_distro_returns_distro_info(self) -> None:
        di = detect_distro()
        self.assertIsInstance(di, DistroInfo)

    def test_detect_distro_custom_path(self) -> None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".release", delete=False, encoding="utf-8"
        ) as fh:
            fh.write('ID=ubuntu\nVERSION_ID="22.04"\n')
            path = fh.name
        try:
            di = detect_distro(path)
            self.assertEqual(di.distro, "ubuntu")
            self.assertEqual(di.version_id, "22.04")
        finally:
            os.unlink(path)

    def test_detect_distro_missing_file(self) -> None:
        di = detect_distro("/nonexistent/os-release")
        self.assertEqual(di.distro, "unknown")
        self.assertEqual(di.version_id, "unknown")


class TestAlwaysInstallSet(unittest.TestCase):

    def test_core_deps_present(self) -> None:
        for pkg in ("git", "python3", "python3-pip", "python3-venv",
                    "curl", "ca-certificates", "gnupg",
                    "python3.11", "python3.11-venv"):
            self.assertIn(pkg, ALWAYS_INSTALL, f"missing core dep: {pkg}")

    def test_gated_deps_not_in_always(self) -> None:
        self.assertNotIn("supervisor", ALWAYS_INSTALL)
        self.assertNotIn("docker.io", ALWAYS_INSTALL)


class TestW2ScopeContract(unittest.TestCase):
    """W2 is Ubuntu/Debian ONLY — assert no silent macOS/Windows fallback."""

    def test_macos_distro_not_supported(self) -> None:
        # parse_os_release maps anything not in SUPPORTED_DISTROS to unknown
        content = 'ID=macos\nVERSION_ID="14"\n'
        di = parse_os_release(content)
        self.assertEqual(di.distro, "unknown")
        self.assertFalse(di.is_supported)

    def test_windows_id_not_supported(self) -> None:
        # /etc/os-release doesn't exist on Windows, but if someone
        # synthesizes one with ID=windows, it should map to unknown.
        content = 'ID=windows\nVERSION_ID="11"\n'
        di = parse_os_release(content)
        self.assertEqual(di.distro, "unknown")

    def test_arch_linux_not_supported(self) -> None:
        content = 'ID=arch\nVERSION_ID=\n'
        di = parse_os_release(content)
        self.assertEqual(di.distro, "unknown")


if __name__ == "__main__":
    unittest.main()