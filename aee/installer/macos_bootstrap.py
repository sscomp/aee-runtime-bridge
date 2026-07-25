"""AEE Bootstrap v1 — W3 macOS bootstrap detection + dependency plan.

This module is the **Python-side testable core** for the W3 macOS
bootstrap flow (spec §6, §6.3, §13.3). It complements the shell layer
(``bootstrap/lib/macos_deps.sh``) by providing a pure-Python,
side-effect-free planning surface that unit tests can exercise without
a real Homebrew / brew / network.

W3 scope: **macOS ONLY**. Ubuntu/Debian (apt) and Windows (winget) are
out of scope (spec §13.1 / §13.2 / §13.4). The module explicitly refuses
to plan for non-darwin platforms.

Design contract (W3):

1. **No subprocess.** This module performs no process spawns. It reads
   the manifest file and produces a :class:`BrewDependencyPlan` data
   structure. The shell layer (``macos_deps.sh``) is responsible for
   the actual brew invocation.
2. **No filesystem writes.** The manifest is read (read-only); no
   files are created or modified.
3. **No network.** No brew update, no Homebrew install, no pip install.
4. **Honest scope.** :func:`plan_brew_dependencies` raises
   :class:`UnsupportedPlatformError` for non-darwin platforms. No
   silent fallback to a "generic POSIX" path.
5. **Profile-aware.** Filters the manifest per spec §6.2
   (supervisor → mini+full; docker → full+edge). macOS v1 supports
   only the ``developer`` profile (spec §13.3,
   :data:`~aee.deploy.capabilities.MacOSDefaults`), so the profile
   gate plus the macOS profile whitelist together mean the supervisor
   and docker formulae are never installed on macOS v1.
6. **Idempotent planning.** Same ``(platform, profile)`` tuple always
   yields the same :class:`BrewDependencyPlan`.

Reference: ``reports/aee_bootstrap_v1_spec.md`` §6 (dependency strategy),
§6.3 (Homebrew), §13.3 (macOS), §4 stage 00_detect + 01_deps.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Tuple

# Re-use the verified exit constants from the installer backend (0,3,4,5,6)
# and the W1 proposed bootstrap constants (7,10,12) from lifecycle.
from aee.installer.backend import EXIT_OK
from aee.installer.lifecycle import (
    EXIT_DEPENDENCY_FLOOR_NOT_MET,
    EXIT_NETWORK_ERROR,
    EXIT_STAGE_FAILED_RETRYABLE,
)

#: Argument parsing failure (argparse / shell usage). Matches the
#: verified value in ``aee/cli.py`` (``EXIT_PARSE_ERROR = 2``).
EXIT_PARSE_ERROR = 2


# ---------------------------------------------------------------------------#
# Platform vocabulary
# ---------------------------------------------------------------------------#

#: Platforms supported by W3. Linux (apt) and Windows (winget) are out
#: of scope. The value ``"darwin"`` matches
#: :data:`~aee.platform.current.PlatformIdentity.MACOS.value` and the
#: shell ``uname -s`` output on macOS.
SUPPORTED_PLATFORM: str = "darwin"

#: Profiles supported on macOS v1 (spec §13.3,
#: :data:`~aee.deploy.capabilities.MacOSDefaults.profile_supported`).
#: The W3 planner refuses profiles outside this set with
#: :class:`UnsupportedProfileError`, providing defence in depth on top
#: of the profile gate in :func:`filter_by_profile`.
MACOS_SUPPORTED_PROFILES: FrozenSet[str] = frozenset({"developer"})


# ---------------------------------------------------------------------------#
# Data structures
# ---------------------------------------------------------------------------#


@dataclass(frozen=True)
class MacOSHostInfo:
    """Detected macOS host info (from ``uname -s`` + ``brew --prefix``).

    The shell layer (``macos_deps.sh``) probes these via native commands;
    the Python planner consumes them as plain data so tests can inject
    synthetic values without a real macOS host.
    """

    kernel: str  # `uname -s` → `Darwin` on macOS
    brew_prefix: str  # `brew --prefix` → `/opt/homebrew` (Apple Silicon) or `/usr/local` (Intel)

    @property
    def is_supported(self) -> bool:
        """True if the kernel is Darwin (macOS)."""
        return self.kernel == "Darwin"

    @property
    def is_apple_silicon(self) -> bool:
        """True if the Homebrew prefix is the Apple Silicon default."""
        return self.brew_prefix == "/opt/homebrew"


@dataclass(frozen=True)
class BrewDependencyPlan:
    """Planned Homebrew dependency installation for a macOS host.

    Produced by :func:`plan_brew_dependencies`. The shell layer
    (``macos_deps.sh``) consumes this to drive the actual brew
    invocation.
    """

    host_info: MacOSHostInfo
    profile: str
    formulae: Tuple[str, ...]
    needs_homebrew_install: bool  # True if brew is missing and --no-brew is not set
    needs_uv_pip_install: bool  # uv always installed via pip post-brew
    no_brew: bool = False  # True if operator passed --no-brew
    dry_run: bool = True

    @property
    def brew_command(self) -> str:
        """The `brew install` command the shell layer would run.

        Mirrors :attr:`~aee.installer.linux_bootstrap.DependencyPlan.apt_command`
        in shape: a single ``brew install <formulae...>`` invocation.
        Uses ``brew install`` (idempotent — brew short-circuits already
        installed formulae per spec §5.1). ``--quiet`` is added per
        spec §6.3 for reproducibility.
        """
        formulae = " ".join(self.formulae)
        prefix = "[dry-run] " if self.dry_run else ""
        return f"{prefix}brew install --quiet {formulae}"

    @property
    def formulae_count(self) -> int:
        return len(self.formulae)


# ---------------------------------------------------------------------------#
# Profile → formula gating (spec §6.2)
# ---------------------------------------------------------------------------#

#: Formulae that are always installed (hard deps, all profiles).
#: Mirrors the apt.deps.txt hard set with macOS-native formula names:
#:   git, python@3.11, curl, ca-certificates.
ALWAYS_INSTALL: FrozenSet[str] = frozenset(
    {
        "git",
        "python@3.11",
        "curl",
        "ca-certificates",
    }
)

#: Profile-gated formulae (spec §6.2).
#:   supervisor → mini + full
#:   docker     → full + edge
#: On macOS v1 only the ``developer`` profile is supported, so neither
#: of these is ever installed by W3; the gate is kept for parity with
#: the apt manifest and for future profile expansion.
PROFILE_GATED: Dict[str, FrozenSet[str]] = {
    "supervisor": frozenset({"mini", "full"}),
    "docker": frozenset({"full", "edge"}),
}


def _profile_allows(profile: str, formula: str) -> bool:
    """Return True if the profile gates this formula in."""
    if formula in ALWAYS_INSTALL:
        return True
    allowed_profiles = PROFILE_GATED.get(formula)
    if allowed_profiles is None:
        # Unknown formula in manifest — default to always install
        # (the manifest is the source of truth; unknown = core).
        return True
    return profile in allowed_profiles


# ---------------------------------------------------------------------------#
# Manifest parsing
# ---------------------------------------------------------------------------#


def parse_manifest(manifest_path: str) -> List[str]:
    """Read the brew.deps.txt manifest and return a list of formula names.

    Skips blank lines and lines starting with ``#``. Preserves order.
    Raises :class:`ManifestNotFoundError` if the file does not exist.
    """
    if not os.path.isfile(manifest_path):
        raise ManifestNotFoundError(manifest_path)
    formulae: List[str] = []
    with open(manifest_path, "r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            formulae.append(stripped)
    return formulae


def filter_by_profile(formulae: List[str], profile: str) -> List[str]:
    """Filter the raw formula list by the requested profile (spec §6.2).

    Preserves order; does not deduplicate (the manifest is the source of
    truth and is expected to be duplicate-free).
    """
    if profile not in {"full", "mini", "edge", "developer"}:
        raise UnsupportedProfileError(profile)
    return [f for f in formulae if _profile_allows(profile, f)]


# ---------------------------------------------------------------------------#
# Host detection (Python-side, testable without a real macOS host)
# ---------------------------------------------------------------------------#


def parse_uname_kernel(uname_s_output: str) -> str:
    """Parse the ``uname -s`` output and return the kernel name.

    Returns the trimmed kernel name (e.g. ``Darwin``, ``Linux``) or
    ``"unknown"`` if the input is empty/whitespace.
    """
    kernel = uname_s_output.strip()
    return kernel if kernel else "unknown"


def detect_macos_host(
    uname_s: str = "Darwin",
    brew_prefix: str = "/opt/homebrew",
) -> MacOSHostInfo:
    """Build a :class:`MacOSHostInfo` from probed values.

    This is the Python-side equivalent of the shell ``detect_macos``
    helper. Tests inject synthetic ``uname -s`` / ``brew --prefix``
    outputs without a real macOS host. Production callers obtain these
    via subprocess (owned by the shell layer, not this module).
    """
    kernel = parse_uname_kernel(uname_s)
    return MacOSHostInfo(kernel=kernel, brew_prefix=brew_prefix)


# ---------------------------------------------------------------------------#
# Planning
# ---------------------------------------------------------------------------#


def plan_brew_dependencies(
    host_info: MacOSHostInfo,
    profile: str,
    manifest_path: str,
    *,
    homebrew_available: bool = True,
    no_brew: bool = False,
    dry_run: bool = True,
) -> BrewDependencyPlan:
    """Produce a :class:`BrewDependencyPlan` for the given (host, profile).

    Args:
        host_info: Detected macOS host info (kernel + brew prefix).
        profile: Requested profile (full/mini/edge/developer).
        manifest_path: Path to ``brew.deps.txt``.
        homebrew_available: Whether ``brew`` is on PATH (probe result
            from the shell layer). When False and ``no_brew`` is False,
            the plan records ``needs_homebrew_install=True``.
        no_brew: True if the operator passed ``--no-brew`` (skip
            Homebrew install; spec §13.3 limitation).
        dry_run: True for plan-only; False for real install.

    Raises:
        UnsupportedPlatformError: if host_info kernel is not Darwin.
        UnsupportedProfileError: if profile is not in the macOS v1
            supported set (``developer``) — defence in depth on top of
            the manifest profile gate.
        ManifestNotFoundError: if the manifest file is absent.
    """
    if not host_info.is_supported:
        raise UnsupportedPlatformError(host_info.kernel)
    if profile not in MACOS_SUPPORTED_PROFILES:
        raise UnsupportedProfileError(profile)
    raw = parse_manifest(manifest_path)
    filtered = filter_by_profile(raw, profile)
    needs_brew_install = (not homebrew_available) and (not no_brew)
    return BrewDependencyPlan(
        host_info=host_info,
        profile=profile,
        formulae=tuple(filtered),
        needs_homebrew_install=needs_brew_install,
        needs_uv_pip_install=True,  # uv always installed via pip post-brew
        no_brew=no_brew,
        dry_run=dry_run,
    )


def plan_for_current_macos_host(
    profile: str,
    repo_root: str,
    *,
    homebrew_available: bool = True,
    no_brew: bool = False,
    dry_run: bool = True,
) -> BrewDependencyPlan:
    """Convenience: detect the current macOS host and plan deps.

    Raises :class:`UnsupportedPlatformError` if the host is not macOS.
    """
    host_info = detect_macos_host()
    manifest_path = os.path.join(
        repo_root, "bootstrap", "manifests", "brew.deps.txt"
    )
    return plan_brew_dependencies(
        host_info=host_info,
        profile=profile,
        manifest_path=manifest_path,
        homebrew_available=homebrew_available,
        no_brew=no_brew,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------#
# Errors
# ---------------------------------------------------------------------------#


class UnsupportedPlatformError(Exception):
    """Raised when the host platform is not macOS (not Darwin)."""

    def __init__(self, kernel: str, message: str = "") -> None:
        self.kernel = kernel
        super().__init__(
            message
            or f"unsupported platform for W3 macOS bootstrap: kernel={kernel!r} "
            f"(expected Darwin); Ubuntu/Debian (apt) and Windows (winget) "
            f"are separate work orders"
        )


class UnsupportedProfileError(Exception):
    """Raised when the profile is not in the macOS v1 supported set."""

    def __init__(self, profile: str) -> None:
        self.profile = profile
        super().__init__(
            f"unsupported profile for macOS v1: {profile!r} "
            f"(macOS v1 supports only 'developer'; spec §13.3)"
        )


class ManifestNotFoundError(FileNotFoundError):
    """Raised when the brew.deps.txt manifest is absent."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"brew manifest not found: {path}")


# ---------------------------------------------------------------------------#
# Exit code re-exports (so callers import from a single surface)
# ---------------------------------------------------------------------------#

__all__ = [
    # Exit codes (re-exported from W1 lifecycle + backend)
    "EXIT_OK",
    "EXIT_PARSE_ERROR",
    "EXIT_STAGE_FAILED_RETRYABLE",
    "EXIT_NETWORK_ERROR",
    "EXIT_DEPENDENCY_FLOOR_NOT_MET",
    # Platform vocabulary
    "SUPPORTED_PLATFORM",
    "MACOS_SUPPORTED_PROFILES",
    # Errors
    "UnsupportedPlatformError",
    "UnsupportedProfileError",
    "ManifestNotFoundError",
    # Data structures
    "MacOSHostInfo",
    "BrewDependencyPlan",
    # Profile gating
    "ALWAYS_INSTALL",
    "PROFILE_GATED",
    # Functions
    "parse_manifest",
    "filter_by_profile",
    "parse_uname_kernel",
    "detect_macos_host",
    "plan_brew_dependencies",
    "plan_for_current_macos_host",
]