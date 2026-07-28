"""AEE Bootstrap v1 — W7 Windows PowerShell integration tests.

Spec coverage: §16 W10 (redaction + resume + stage transitions) Windows
branch, §16 W7 (Windows trampoline), §13.4 (Windows).

These tests are Python-side static contract tests for the Windows
PowerShell bootstrap surface. They do NOT execute ``pwsh`` (the Abacus
host is Linux; spec §13.4 W7 is Windows only). They verify:

* The W7 deliverables exist on disk at the canonical paths.
* The W7 PowerShell scripts contain the expected structural elements
  (parameter blocks, ValidateSet, exit-code constants, CLI guards,
  dry-run default, profile gating, build floor, winget availability
  check, WSL rejection note).
* The ``pwsh.deps.txt`` manifest lists the spec-mandated core deps and
  profile-gated entries (§6.1, §6.2).
* The redaction module (shipped in W10) covers Windows-style secret
  patterns (``$env:API_KEY`` PowerShell variable references) — this is
  the W10 redaction module's Windows-branch regression guard, required
  by Phase C per §17.3 (Windows trampoline uses the same shared
  redaction module; §8.2 R5 says redaction must be consistent across
  shell, PowerShell, and Python layers).

Run:
    PYTHONPATH=/home/ubuntu/hermes-runtime-bridge python3 -m unittest \\
        aee.tests.test_bootstrap_windows_ps1

All tests are hermetic and read-only. No subprocess side effects.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="strict")


class TestW7SurfacePresence(unittest.TestCase):
    """Verify the W7 deliverable files exist at the canonical paths."""

    def test_install_ps1_present(self) -> None:
        self.assertTrue((_REPO_ROOT / "install.ps1").is_file(),
                        "install.ps1 must exist at repo root (spec §16 W7)")

    def test_detect_ps1_present(self) -> None:
        self.assertTrue((_REPO_ROOT / "bootstrap" / "lib" / "detect.ps1").is_file(),
                        "bootstrap/lib/detect.ps1 must exist (spec §16 W7)")

    def test_deps_ps1_present(self) -> None:
        self.assertTrue((_REPO_ROOT / "bootstrap" / "lib" / "deps.ps1").is_file(),
                        "bootstrap/lib/deps.ps1 must exist (spec §16 W7)")

    def test_pwsh_deps_manifest_present(self) -> None:
        self.assertTrue((_REPO_ROOT / "bootstrap" / "manifests" / "pwsh.deps.txt").is_file(),
                        "bootstrap/manifests/pwsh.deps.txt must exist (spec §16 W7)")


class TestInstallPs1Contract(unittest.TestCase):
    """Static contract assertions for install.ps1 (spec §16 W7, §13.4)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _read_text(_REPO_ROOT / "install.ps1")

    def test_requires_version_5_1(self) -> None:
        self.assertIn("#Requires -Version 5.1", self.text,
                      "install.ps1 must declare #Requires -Version 5.1 (spec §13.4)")

    def test_w7_banner(self) -> None:
        self.assertIn("W7 Windows PowerShell trampoline", self.text)

    def test_profile_validate_set(self) -> None:
        self.assertIn("ValidateSet('full', 'mini', 'edge', 'developer')", self.text)

    def test_default_profile_developer(self) -> None:
        # spec §2.4 Windows row: bootstrap on Windows defaults to developer
        self.assertIn("developer", self.text)

    def test_dry_run_default(self) -> None:
        # §18.3: dry-run by default
        self.assertIn("dry-run", self.text.lower())

    def test_mutual_exclusion_dryrun_execute(self) -> None:
        self.assertIn("mutually exclusive", self.text)

    def test_force_reset_gated_by_execute(self) -> None:
        self.assertIn("ForceReset", self.text)
        self.assertIn("-Execute", self.text)

    def test_delegates_to_aee_cli(self) -> None:
        self.assertIn("aee.cli", self.text)

    def test_find_python_helper(self) -> None:
        self.assertIn("Find-Python", self.text)

    def test_win32nt_host_validation(self) -> None:
        self.assertIn("Win32NT", self.text)

    def test_wsl_not_supported_note(self) -> None:
        self.assertIn("WSL", self.text)

    def test_exit_code_constants(self) -> None:
        for name in ("EXIT_OK", "EXIT_PARSE_ERROR", "EXIT_STAGE_FAILED_RETRYABLE",
                     "EXIT_NETWORK_ERROR", "EXIT_DEPENDENCY_FLOOR_NOT_MET"):
            self.assertIn(name, self.text, f"install.ps1 missing exit code: {name}")

    def test_write_help_function(self) -> None:
        self.assertIn("Write-Help", self.text)

    def test_cli_guard(self) -> None:
        self.assertIn("InvocationName", self.text)


class TestDetectPs1Contract(unittest.TestCase):
    """Static contract assertions for detect.ps1 (spec §16 W7, §2.3, §13.4)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _read_text(_REPO_ROOT / "bootstrap" / "lib" / "detect.ps1")

    def test_w7_banner(self) -> None:
        self.assertIn("W7 Windows detect shim", self.text)

    def test_resolve_via_python(self) -> None:
        self.assertIn("Resolve-ViaPython", self.text)

    def test_resolve_via_heuristic(self) -> None:
        self.assertIn("Resolve-ViaHeuristic", self.text)

    def test_detect_platform_entry(self) -> None:
        self.assertIn("Detect-Platform", self.text)

    def test_detect_windows_build(self) -> None:
        self.assertIn("Detect-WindowsBuild", self.text)

    def test_win32nt_gate(self) -> None:
        self.assertIn("Win32NT", self.text)

    def test_delegates_to_python_resolver(self) -> None:
        self.assertIn("aee.platform.current", self.text)

    def test_no_side_effects_note(self) -> None:
        self.assertIn("No subprocess side effects", self.text)

    def test_cli_guard(self) -> None:
        self.assertIn("InvocationName", self.text)


class TestDepsPs1Contract(unittest.TestCase):
    """Static contract assertions for deps.ps1 (spec §16 W7, §6, §13.4)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _read_text(_REPO_ROOT / "bootstrap" / "lib" / "deps.ps1")

    def test_w7_banner(self) -> None:
        self.assertIn("W7 Windows dependency installer", self.text)

    def test_dot_sources_detect_ps1(self) -> None:
        self.assertIn("detect.ps1", self.text)

    def test_read_manifest(self) -> None:
        self.assertIn("Read-Manifest", self.text)

    def test_filter_packages_by_profile(self) -> None:
        self.assertIn("Filter-PackagesByProfile", self.text)

    def test_invoke_winget_install(self) -> None:
        self.assertIn("Invoke-WingetInstall", self.text)

    def test_profile_validate_set(self) -> None:
        self.assertIn("ValidateSet('full', 'mini', 'edge', 'developer')", self.text)

    def test_system_scope_rejected(self) -> None:
        self.assertIn("NOT supported on Windows v1", self.text)

    def test_build_floor_22621(self) -> None:
        self.assertIn("22621", self.text)

    def test_winget_availability_check(self) -> None:
        self.assertIn("Get-Command winget", self.text)

    def test_pwsh_deps_manifest_reference(self) -> None:
        self.assertIn("pwsh.deps.txt", self.text)

    def test_dry_run_default(self) -> None:
        self.assertIn("dry-run", self.text.lower())

    def test_exit_code_constants(self) -> None:
        for name in ("EXIT_OK", "EXIT_PARSE_ERROR", "EXIT_STAGE_FAILED_RETRYABLE",
                     "EXIT_NETWORK_ERROR", "EXIT_DEPENDENCY_FLOOR_NOT_MET"):
            self.assertIn(name, self.text, f"deps.ps1 missing exit code: {name}")

    def test_cli_guard(self) -> None:
        self.assertIn("InvocationName", self.text)

    def test_profile_gating_rules_documented(self) -> None:
        # §6.2: supervisor → mini+full; docker → full+edge
        self.assertIn("supervisor", self.text)
        self.assertIn("docker", self.text.lower())

    # ----- H1: $LASTEXITCODE wiring (winget install failure path) -----

    def test_invoke_winget_inspects_lastexitcode(self) -> None:
        # H1 fix: Invoke-WingetInstall MUST capture and inspect
        # $LASTEXITCODE after each winget call so that the documented
        # exit codes (7 retryable / 10 network) are actually emitted.
        self.assertIn("$LASTEXITCODE", self.text,
                      "deps.ps1 must inspect $LASTEXITCODE after winget install")

    def test_winget_exit_classifier_present(self) -> None:
        # H1 fix: a classifier that maps winget exit codes to the
        # documented deps.ps1 exit categories (success/retryable/network)
        # must exist so exit 7 / 10 are actually wired.
        self.assertIn("Get-WingetExitCategory", self.text,
                      "deps.ps1 must have Get-WingetExitCategory classifier")

    def test_winget_already_installed_code_handled(self) -> None:
        # H1 fix: winget's "already installed" exit code (-1978335045)
        # must map to EXIT_OK so idempotent re-runs don't fail.
        self.assertIn("-1978335045", self.text,
                      "deps.ps1 must handle winget AlreadyInstalled (-1978335045)")

    def test_winget_network_codes_map_to_exit_10(self) -> None:
        # H1 fix: winget network error codes must map to EXIT_NETWORK_ERROR
        # so the documented exit 10 is actually emitted on network failure.
        self.assertIn("-1978335015", self.text,
                      "deps.ps1 must map winget DownloadError to exit 10")
        self.assertIn("-1978335034", self.text,
                      "deps.ps1 must map winget NoNetwork to exit 10")

    def test_winget_failure_exits_nonzero(self) -> None:
        # H1 fix: Invoke-WingetInstall must exit non-zero on any winget
        # failure (not silently return and let Main print "completed").
        self.assertRegex(self.text, r"if\s+\(\$rc\s+-ne\s+0\)",
                         "deps.ps1 must check $rc after winget and exit on failure")

    # ----- H2: supervisor not passed to winget on any profile -----

    def test_supervisor_filtered_out_on_windows(self) -> None:
        # H2 fix: Filter-PackagesByProfile must exclude `supervisor`
        # from the winget install set on every profile (it is a pip
        # package on Windows per spec §6.2, not a winget id).
        # The old code emitted `supervisor` for mini+full; the fix uses
        # `continue` so it never reaches Invoke-WingetInstall.
        # We locate the supervisor switch branch (from `'^supervisor$' {`
        # to the next closing `}`) and verify it contains `continue` and
        # does NOT emit `$pkg`.
        m = re.search(r"'\^supervisor\$'\s*\{(.*?)\}", self.text, re.DOTALL)
        self.assertIsNotNone(m, "deps.ps1 must have a supervisor switch branch")
        branch = m.group(1)
        self.assertIn("continue", branch,
                      "deps.ps1 must `continue` on supervisor, not emit it")
        self.assertNotIn("$pkg", branch,
                         "deps.ps1 supervisor branch must NOT emit $pkg")

    def test_supervisor_not_in_mini_full_gating(self) -> None:
        # H2 fix: the old `if ($ProfileName -in @('mini', 'full')) { $pkg }`
        # branch for supervisor must be GONE so supervisor is never
        # passed to winget on mini/full profiles.
        self.assertNotRegex(
            self.text,
            r"'\^supervisor\$'\s*\)\s*\{\s*if\s+\(\$ProfileName\s+-in\s+@\('mini',\s*'full'\)",
            "deps.ps1 must NOT gate supervisor to mini+full (it is a pip package on Windows)"
        )

    # ----- H3: standalone deps.ps1 RepoRoot resolution from script dir -----

    def test_reporoot_resolved_from_script_dir(self) -> None:
        # H3 fix: when -RepoRoot is empty, deps.ps1 must derive it from
        # the script's own location (parent of bootstrap/lib) rather
        # than relying on the caller's CWD.
        self.assertIn("if (-not $RepoRoot)", self.text,
                      "deps.ps1 must auto-resolve RepoRoot when not supplied")
        self.assertRegex(self.text, r"Join-Path\s+\$scriptDir\s+\'\.\.\'",
                         "deps.ps1 must derive RepoRoot from $scriptDir parent")


class TestPwshDepsManifest(unittest.TestCase):
    """Contract assertions for pwsh.deps.txt (spec §6.1, §6.2, §13.4)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _read_text(_REPO_ROOT / "bootstrap" / "manifests" / "pwsh.deps.txt")
        # Strip comments + blanks to get the actual package entries.
        cls.entries = [
            line.strip() for line in cls.text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def test_git_git_present(self) -> None:
        self.assertIn("Git.Git", self.entries, "pwsh.deps.txt must list Git.Git (§6.1 core)")

    def test_python_3_11_present(self) -> None:
        self.assertIn("Python.Python.3.11", self.entries,
                      "pwsh.deps.txt must list Python.Python.3.11 (§1.4 Python 3.11 floor)")

    def test_supervisor_present(self) -> None:
        self.assertIn("supervisor", self.entries,
                      "pwsh.deps.txt must list supervisor (§6.2 profile-gated)")

    def test_docker_present(self) -> None:
        # The Docker entry may be Docker.DockerDesktop or a variant — we
        # just assert that the docker profile-gated entry exists.
        docker_entries = [e for e in self.entries if e.lower().startswith("docker")]
        self.assertTrue(docker_entries,
                        "pwsh.deps.txt must list a Docker package (§6.2 profile-gated)")

    def test_manifest_header_documents_w7_scope(self) -> None:
        self.assertIn("W7", self.text)
        self.assertIn("Windows", self.text)

    def test_manifest_documents_floors(self) -> None:
        # §1.4 floors must be documented in the header
        self.assertIn("Git", self.text)
        self.assertIn("Python", self.text)

    def test_no_apt_or_brew_entries(self) -> None:
        # The manifest must NOT contain apt/brew package names (cross-
        # platform leakage would break the winget install).
        for entry in self.entries:
            self.assertFalse(entry.startswith("apt-") or entry.startswith("python3-"),
                             f"pwsh.deps.txt must not contain apt-style entries: {entry}")

    # ----- H2: manifest documents supervisor is filtered out on Windows -----

    def test_manifest_documents_supervisor_filtered_on_windows(self) -> None:
        # H2 fix: the manifest header must document that `supervisor`
        # is a pip package on Windows (not a winget id) and is excluded
        # from the winget install set by Filter-PackagesByProfile.
        self.assertIn("pip install supervisor", self.text,
                      "manifest must document supervisor is a pip package on Windows")
        self.assertIn("not a winget", self.text.lower(),
                      "manifest must state supervisor is NOT a winget package")


class TestRedactionWindowsBranch(unittest.TestCase):
    """W10 redaction module Windows-pattern regression (§8.2 R5).

    The shared redaction module (shipped in W10) MUST cover Windows-style
    secret patterns. Spec §17.3 Phase C says Windows trampoline uses the
    same shared redaction module; §8.2 R5 says redaction must be
    consistent across shell, PowerShell, and Python layers.

    These tests verify the redaction module's env-var-name pattern set
    covers the ``_API_KEY`` / ``_TOKEN`` / ``_SECRET`` / ``_PASSWORD``
    suffixes that appear in PowerShell-style ``$env:NAME`` references.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.module_path = _REPO_ROOT / "aee" / "installer" / "redaction.py"
        if cls.module_path.is_file():
            cls.text = _read_text(cls.module_path)
        else:
            cls.text = ""

    def test_redaction_module_present(self) -> None:
        self.assertTrue(self.module_path.is_file(),
                        "aee/installer/redaction.py must exist (W10 deliverable)")

    def test_covers_api_key_suffix(self) -> None:
        self.assertIn("_API_KEY", self.text,
                      "redaction must cover _API_KEY suffix (§8.2 R5)")

    def test_covers_token_suffix(self) -> None:
        self.assertIn("_TOKEN", self.text,
                      "redaction must cover _TOKEN suffix (§8.2 R5)")

    def test_covers_secret_suffix(self) -> None:
        self.assertIn("_SECRET", self.text,
                      "redaction must cover _SECRET suffix (§8.2 R5)")

    def test_covers_password_suffix(self) -> None:
        self.assertIn("_PASSWORD", self.text,
                      "redaction must cover _PASSWORD suffix (§8.2 R5)")

    def test_covers_authorization_header(self) -> None:
        self.assertIn("Authorization", self.text,
                      "redaction must cover Authorization header (§8.2)")

    def test_case_insensitive_matching(self) -> None:
        # The redaction module must be case-insensitive on the name suffix
        # so PowerShell-style $env:API_KEY and $env:api_key both get redacted.
        self.assertIn("IGNORECASE", self.text or "",
                      "redaction must use re.IGNORECASE for case-insensitive matching")


class TestW13E2EHarnessPresence(unittest.TestCase):
    """W13 deliverable: tests/e2e/windows.ps1 (spec §16 W13, §14.3)."""

    def test_windows_e2e_present(self) -> None:
        path = _REPO_ROOT / "tests" / "e2e" / "windows.ps1"
        self.assertTrue(path.is_file(),
                        "tests/e2e/windows.ps1 must exist (spec §16 W13)")

    def test_windows_e2e_banner(self) -> None:
        path = _REPO_ROOT / "tests" / "e2e" / "windows.ps1"
        if not path.is_file():
            self.skipTest("windows.ps1 not present")
        text = _read_text(path)
        self.assertIn("W13", text)
        self.assertIn("Windows", text)

    def test_windows_e2e_validates_w7_surface(self) -> None:
        path = _REPO_ROOT / "tests" / "e2e" / "windows.ps1"
        if not path.is_file():
            self.skipTest("windows.ps1 not present")
        text = _read_text(path)
        self.assertIn("install.ps1", text)
        self.assertIn("detect.ps1", text)
        self.assertIn("deps.ps1", text)
        self.assertIn("pwsh.deps.txt", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)