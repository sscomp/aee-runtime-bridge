"""AEE Bootstrap v1 — W2 Ubuntu/Debian bootstrap detection + dependency plan.

This module is the **Python-side testable core** for the W2 Ubuntu/Debian
bootstrap flow (spec §6, §13.1, §13.2). It complements the shell layer
(``bootstrap/lib/detect.sh`` + ``bootstrap/lib/deps.sh``) by providing a
pure-Python, side-effect-free planning surface that unit tests can
exercise without a real apt / sudo / network.

W2 scope: **Ubuntu/Debian ONLY**. macOS (brew) and Windows (winget) are
out of scope (spec §13.3 / §13.4). The module explicitly refuses to
plan for non-Linux or non-apt platforms.

Design contract (W2):

1. **No subprocess.** This module performs no process spawns. It reads
   the manifest file and produces a :class:`DependencyPlan` data
   structure. The shell layer (``deps.sh``) is responsible for the
   actual apt invocation.
2. **No filesystem writes.** The manifest is read (read-only); no
   files are created or modified.
3. **No network.** No apt update, no PPA fetch, no pip install.
4. **Honest scope.** :func:`plan_apt_dependencies` raises
   :class:`UnsupportedDistroError` for non-ubuntu/debian distros. No
   silent fallback to a "generic Linux" path.
5. **Profile-aware.** Filters the manifest per spec §6.2
   (supervisor → mini+full; docker.io → full+edge).
6. **Idempotent planning.** Same ``(distro, version_id, profile)``
   tuple always yields the same :class:`DependencyPlan`.

Reference: ``reports/aee_bootstrap_v1_spec.md`` §6 (dependency strategy),
§13.1 (Ubuntu), §13.2 (Debian), §4 stage 00_detect + 01_deps.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Tuple

# Re-use the verified exit constants from the installer backend (0,3,4,5,6)
# and the W1 proposed bootstrap constants (7,10,12) from lifecycle.
# EXIT_OK=0 lives in aee.installer.backend (verified);
# EXIT_PARSE_ERROR=2 is the argparse exit code (canonical, defined locally);
# the proposed bootstrap codes live in aee.installer.lifecycle (W1).
from aee.installer.backend import EXIT_OK
from aee.installer.lifecycle import (
    EXIT_DEPENDENCY_FLOOR_NOT_MET,
    EXIT_NETWORK_ERROR,
    EXIT_STAGE_FAILED_RETRYABLE,
)

#: Argument parsing failure (argparse / shell usage). Matches the
#: verified value in ``aee/cli.py`` (`EXIT_PARSE_ERROR = 2`).
EXIT_PARSE_ERROR = 2


# ---------------------------------------------------------------------------#
# Distro vocabulary
# ---------------------------------------------------------------------------#

#: Distros supported by W2. macOS and Windows are out of scope.
SUPPORTED_DISTROS: FrozenSet[str] = frozenset({"ubuntu", "debian"})

#: Ubuntu version IDs that require the deadsnakes PPA for python3.11.
#: Ubuntu 24.04 ships python3.12 natively (no PPA needed).
DEADSNAKES_UBUNTU_VERSIONS: FrozenSet[str] = frozenset({"22.04"})

#: Debian version IDs supported by W2 (spec §1.4 floor: 12 bookworm).
SUPPORTED_DEBIAN_VERSIONS: FrozenSet[str] = frozenset({"12"})


# ---------------------------------------------------------------------------#
# Errors
# ---------------------------------------------------------------------------#


class UnsupportedDistroError(Exception):
    """Raised when the detected distro is not Ubuntu or Debian.

    W2 scope is Ubuntu/Debian ONLY (spec §13.1/§13.2). macOS and
    Windows are separate work orders.
    """

    def __init__(self, distro: str, message: str = "") -> None:
        self.distro = distro
        super().__init__(message or f"unsupported distro for W2: {distro!r}")


class UnsupportedProfileError(Exception):
    """Raised when the profile is not in KNOWN_PROFILES."""

    def __init__(self, profile: str) -> None:
        self.profile = profile
        super().__init__(f"unsupported profile: {profile!r}")


class ManifestNotFoundError(FileNotFoundError):
    """Raised when the apt.deps.txt manifest is not found at the expected path."""


# ---------------------------------------------------------------------------#
# Data structures
# ---------------------------------------------------------------------------#


@dataclass(frozen=True)
class DistroInfo:
    """Detected Linux distribution info (from /etc/os-release)."""

    distro: str  # `ubuntu`, `debian`, or `unknown`
    version_id: str  # `22.04`, `12`, or `unknown`

    @property
    def is_supported(self) -> bool:
        return self.distro in SUPPORTED_DISTROS

    @property
    def needs_deadsnakes(self) -> bool:
        """True if Ubuntu 22.04 (needs deadsnakes PPA for python3.11)."""
        return (
            self.distro == "ubuntu"
            and self.version_id in DEADSNAKES_UBUNTU_VERSIONS
        )


@dataclass(frozen=True)
class DependencyPlan:
    """Planned apt dependency installation for a (distro, profile) pair.

    Produced by :func:`plan_apt_dependencies`. The shell layer
    (``deps.sh``) consumes this to drive the actual apt invocation.
    """

    distro_info: DistroInfo
    profile: str
    packages: Tuple[str, ...]
    needs_deadsnakes: bool
    needs_uv_pip_install: bool
    dry_run: bool = True

    @property
    def apt_command(self) -> str:
        """The apt-get install command the shell layer would run."""
        pkgs = " ".join(self.packages)
        prefix = "[dry-run] " if self.dry_run else ""
        sudo = "sudo " if not self.dry_run else ""
        return f"{prefix}{sudo}apt-get install --no-install-recommends -y {pkgs}"

    @property
    def package_count(self) -> int:
        return len(self.packages)


# ---------------------------------------------------------------------------#
# Profile → package gating (spec §6.2)
# ---------------------------------------------------------------------------#

#: Packages that are always installed (hard deps, all profiles).
ALWAYS_INSTALL: FrozenSet[str] = frozenset(
    {
        "git",
        "python3",
        "python3-pip",
        "python3-venv",
        "curl",
        "ca-certificates",
        "gnupg",
        "python3.11",
        "python3.11-venv",
    }
)

#: Profile-gated packages (spec §6.2).
#: supervisor → mini + full
#: docker.io  → full + edge
PROFILE_GATED: Dict[str, FrozenSet[str]] = {
    "supervisor": frozenset({"mini", "full"}),
    "docker.io": frozenset({"full", "edge"}),
}


def _profile_allows(profile: str, package: str) -> bool:
    """Return True if the profile gates this package in."""
    if package in ALWAYS_INSTALL:
        return True
    allowed_profiles = PROFILE_GATED.get(package)
    if allowed_profiles is None:
        # Unknown package in manifest — default to always install
        # (the manifest is the source of truth; unknown = core).
        return True
    return profile in allowed_profiles


# ---------------------------------------------------------------------------#
# Manifest parsing
# ---------------------------------------------------------------------------#


def parse_manifest(manifest_path: str) -> List[str]:
    """Read the apt.deps.txt manifest and return a list of package names.

    Skips blank lines and lines starting with ``#``. Preserves order.
    Raises :class:`ManifestNotFoundError` if the file does not exist.
    """
    if not os.path.isfile(manifest_path):
        raise ManifestNotFoundError(manifest_path)
    packages: List[str] = []
    with open(manifest_path, "r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            packages.append(stripped)
    return packages


def filter_by_profile(packages: List[str], profile: str) -> List[str]:
    """Filter the raw package list by the requested profile (spec §6.2).

    Preserves order; does not deduplicate (the manifest is the source of
    truth and is expected to be duplicate-free).
    """
    if profile not in {"full", "mini", "edge", "developer"}:
        raise UnsupportedProfileError(profile)
    return [pkg for pkg in packages if _profile_allows(profile, pkg)]


# ---------------------------------------------------------------------------#
# Distro detection (Python-side, testable without /etc/os-release)
# ---------------------------------------------------------------------------#


def parse_os_release(os_release_content: str) -> DistroInfo:
    """Parse /etc/os-release content and return :class:`DistroInfo`.

    Accepts the raw file content (so tests can inject synthetic content
    without touching the filesystem). Returns
    ``DistroInfo(distro="unknown", version_id="unknown")`` when ID or
    VERSION_ID are absent.
    """
    fields: Dict[str, str] = {}
    for line in os_release_content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        # Strip surrounding quotes
        value = value.strip().strip('"').strip("'")
        fields[key.strip()] = value
    distro = fields.get("ID", "unknown")
    version_id = fields.get("VERSION_ID", "unknown")
    if distro not in SUPPORTED_DISTROS:
        distro = "unknown"
    return DistroInfo(distro=distro, version_id=version_id)


def detect_distro(os_release_path: str = "/etc/os-release") -> DistroInfo:
    """Read /etc/os-release from the filesystem and return DistroInfo.

    Returns ``DistroInfo(distro="unknown", version_id="unknown")`` if
    the file is absent. This is the Python-side equivalent of the
    shell ``detect_linux_distro`` + ``detect_linux_version_id`` helpers.
    """
    if not os.path.isfile(os_release_path):
        return DistroInfo(distro="unknown", version_id="unknown")
    with open(os_release_path, "r", encoding="utf-8") as fh:
        return parse_os_release(fh.read())


# ---------------------------------------------------------------------------#
# Planning
# ---------------------------------------------------------------------------#


def plan_apt_dependencies(
    distro_info: DistroInfo,
    profile: str,
    manifest_path: str,
    dry_run: bool = True,
) -> DependencyPlan:
    """Produce a :class:`DependencyPlan` for the given (distro, profile).

    Raises:
        UnsupportedDistroError: if distro is not ubuntu/debian.
        UnsupportedProfileError: if profile is not in KNOWN_PROFILES.
        ManifestNotFoundError: if the manifest file is absent.
    """
    if not distro_info.is_supported:
        raise UnsupportedDistroError(distro_info.distro)
    if profile not in {"full", "mini", "edge", "developer"}:
        raise UnsupportedProfileError(profile)
    raw = parse_manifest(manifest_path)
    filtered = filter_by_profile(raw, profile)
    return DependencyPlan(
        distro_info=distro_info,
        profile=profile,
        packages=tuple(filtered),
        needs_deadsnakes=distro_info.needs_deadsnakes,
        needs_uv_pip_install=True,  # uv always installed via pip post-apt
        dry_run=dry_run,
    )


def plan_for_current_host(
    profile: str,
    repo_root: str,
    dry_run: bool = True,
) -> DependencyPlan:
    """Convenience: detect the current host's distro and plan deps.

    Reads /etc/os-release from the filesystem. Raises
    :class:`UnsupportedDistroError` if the host is not Ubuntu/Debian.
    """
    distro_info = detect_distro()
    manifest_path = os.path.join(repo_root, "bootstrap", "manifests", "apt.deps.txt")
    return plan_apt_dependencies(
        distro_info=distro_info,
        profile=profile,
        manifest_path=manifest_path,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------#
# Exit code re-exports (so callers import from a single surface)
# ---------------------------------------------------------------------------#

__all__ = [
    # Exit codes (re-exported from W1 lifecycle)
    "EXIT_OK",
    "EXIT_PARSE_ERROR",
    "EXIT_STAGE_FAILED_RETRYABLE",
    "EXIT_NETWORK_ERROR",
    "EXIT_DEPENDENCY_FLOOR_NOT_MET",
    # Distro vocabulary
    "SUPPORTED_DISTROS",
    "DEADSNAKES_UBUNTU_VERSIONS",
    "SUPPORTED_DEBIAN_VERSIONS",
    # Errors
    "UnsupportedDistroError",
    "UnsupportedProfileError",
    "ManifestNotFoundError",
    # Data structures
    "DistroInfo",
    "DependencyPlan",
    # Profile gating
    "ALWAYS_INSTALL",
    "PROFILE_GATED",
    # Functions
    "parse_manifest",
    "filter_by_profile",
    "parse_os_release",
    "detect_distro",
    "plan_apt_dependencies",
    "plan_for_current_host",
]