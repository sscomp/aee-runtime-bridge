"""AEE Phase 3 — Installer Workflow (§21.3 + §21.x bootstrap).

This module is the **end-to-end installer orchestrator** introduced by
Phase 3. It composes the existing building blocks into a single,
deterministic, dry-run-by-default workflow that prepares a new
machine to run AEE with minimal manual steps:

    +-------------------+        +------------------+        +----------------+
    |  Phase 2 doctor   | ----> |  §21.3 installer | ----> |  bootstrap     |
    |  (readiness probe)|       |  backend (plan)  |        |  detect/deps   |
    +-------------------+        +------------------+        +----------------+
           |                              |                          |
           v                              v                          v
     PreFlightResult              InstallPlan                  DependencyPlan
           |                              |                          |
           +----------------------------------------------------------+
                                        |
                                        v
                          DirectoryInitPlan + ConfigBootstrapPlan
                                        |
                                        v
                          PostInstallVerification
                                        |
                                        v
                          InstallWorkflowResult

Design contract (Phase 3):

1. **No side effects by default.** ``run_workflow`` defaults to
   ``dry_run=True``. In dry-run, every stage produces a plan/result
   data structure; no filesystem writes, no subprocess, no network
   with side effects. ``--execute`` is refused in this slice via
   :class:`ExecuteNotAuthorizedError` (matching §21.3 guard).

2. **Composes, does not duplicate.** The workflow imports the
   existing :class:`DoctorRunner` (Phase 2), :class:`InstallerBackend`
   (§21.3), :func:`detect_distro` / :func:`detect_macos_host`
   (W2/W3 bootstrap). It does NOT re-implement any of their logic.

3. **Profile-aware throughout.** Profile flows from the caller through
   every stage. The doctor validates it, the backend plans with it,
   the bootstrap filters deps by it.

4. **Directory initialization as plan data.** :class:`DirectoryInitPlan`
   describes the directories AEE expects (data, reports, logs,
   .aee-profile marker). Execution is a shell-layer follow-up; in
   dry-run we only describe.

5. **Configuration bootstrap as plan data.** :class:`ConfigBootstrapPlan`
   describes the env-file + supervisord-conf + profile-marker writes
   the shell layer would perform. In dry-run we only describe.

6. **Post-install verification.** :class:`PostInstallVerification`
   re-runs a subset of the doctor's checks (dependencies, directory
   perms, profile marker presence) as a post-install smoke. In
   dry-run this is a *projected* verification (what would be checked).

7. **Idempotent.** Re-running the workflow with the same arguments
   yields the same :class:`InstallWorkflowResult` (modulo
   timestamps in :class:`WorkflowSummary`, which are deterministic
   within a single run and absent from the dry-run plan).

8. **No new exit codes.** The workflow reuses the existing
   {0, 2, 3, 4, 5, 6, 7, 8, 12} vocabulary. No new constants.

Reference: ``reports/aee_phase3_installer_implementation.md``
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Tuple,
)

# Canonical profile source of truth.
from aee.profiles.descriptor import (
    DEFAULT_PROFILE,
    KNOWN_PROFILES,
    UnknownProfileError,
    get_descriptor,
    parse_profile,
)

# §21.3 installer backend (verified — do NOT reimplement).
from aee.installer.backend import (
    EXIT_EXECUTE_NOT_AUTHORIZED,
    EXIT_OK,
    EXIT_PRE_FLIGHT_FAILED,
    EXIT_PROFILE_SWITCH_REJECTED,
    ExecuteNotAuthorizedError,
    InstallPlan,
    InstallResult,
    InstallerBackend,
    PreFlightResult,
)

# Phase 2 doctor (verified — do NOT reimplement).
from aee.doctor import (
    DoctorReport,
    DoctorRunner,
    EXIT_DOCTOR_CAVEATS,
    EXIT_DOCTOR_FAILED,
    EXIT_DOCTOR_OK,
)

# W2/W3 bootstrap detection (verified — do NOT reimplement).
# We import lazily inside _run_platform_bootstrap so that environments
# without the bootstrap manifests (or on an unsupported platform) do
# not break workflow import. The functions raise on unsupported
# platforms, which the workflow captures into the plan.


# ---------------------------------------------------------------------------#
# Exit codes (reused — no new constants per design contract #8)
# ---------------------------------------------------------------------------#

#: Re-exported for caller convenience. Same value as
#: :data:`aee.installer.backend.EXIT_OK`.
EXIT_WORKFLOW_OK = EXIT_OK

#: Re-exported pre-flight failure exit code.
EXIT_WORKFLOW_PRE_FLIGHT_FAILED = EXIT_PRE_FLIGHT_FAILED

#: Re-exported profile-switch-rejected exit code.
EXIT_WORKFLOW_PROFILE_SWITCH_REJECTED = EXIT_PROFILE_SWITCH_REJECTED

#: Re-exported execute-not-authorized exit code.
EXIT_WORKFLOW_EXECUTE_NOT_AUTHORIZED = EXIT_EXECUTE_NOT_AUTHORIZED

#: Re-exported doctor caveats exit code (no required-failure).
EXIT_WORKFLOW_DOCTOR_CAVEATS = EXIT_DOCTOR_CAVEATS

#: Re-exported doctor failed exit code (a required check failed).
EXIT_WORKFLOW_DOCTOR_FAILED = EXIT_DOCTOR_FAILED


# ---------------------------------------------------------------------------#
# Plan dataclasses (pure data; execution is shell-layer)
# ---------------------------------------------------------------------------#


@dataclass(frozen=True)
class DirectoryInitPlan:
    """Plan for initializing the directories AEE expects.

    ``entries`` is a tuple of ``(relative_path, purpose, exists)``
    triples. ``exists`` reflects the *current* state read during
    planning (read-only). Execution would ``mkdir -p`` each missing
    entry; in dry-run we only describe.
    """

    entries: Tuple[Tuple[str, str, bool], ...]
    marker_would_write: bool

    def to_dict(self) -> dict:
        return {
            "entries": [
                {"path": p, "purpose": pu, "exists": e}
                for p, pu, e in self.entries
            ],
            "marker_would_write": self.marker_would_write,
            "missing_count": sum(1 for _, _, e in self.entries if not e),
        }


@dataclass(frozen=True)
class ConfigBootstrapPlan:
    """Plan for the configuration bootstrap (env file + supervisor conf).

    ``env_file_would_install`` is True when ``.env`` is missing and the
    plan would write it. ``supervisor_conf_would_install`` is True when
    no supervisord program for this profile is detected (read-only
    check; the actual install path is shell-layer). ``profile_marker``
    is the profile name that would be written to ``.aee-profile``.
    """

    profile: str
    env_file_present: bool
    env_file_would_install: bool
    supervisor_conf_present: bool
    supervisor_conf_would_install: bool
    profile_marker_would_write: bool

    def to_dict(self) -> dict:
        return {
            "profile": self.profile,
            "env_file_present": self.env_file_present,
            "env_file_would_install": self.env_file_would_install,
            "supervisor_conf_present": self.supervisor_conf_present,
            "supervisor_conf_would_install": self.supervisor_conf_would_install,
            "profile_marker_would_write": self.profile_marker_would_write,
        }


@dataclass(frozen=True)
class PlatformBootstrapPlan:
    """Plan for the platform-specific dependency installation.

    Captures the output of W2/W3 detection + planning. ``platform``
    is the resolved platform string. ``supported`` is False when the
    host platform is not yet supported by the bootstrap (e.g.
    Windows). ``dependency_plan`` is the W2/W3 plan dict when
    supported, else an empty dict. ``error`` is the error message when
    detection/planning raised, else empty string.
    """

    platform: str
    supported: bool
    profile_allowed: bool
    dependency_plan: Dict[str, Any]
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "supported": self.supported,
            "profile_allowed": self.profile_allowed,
            "dependency_plan": self.dependency_plan,
            "error": self.error,
        }


@dataclass(frozen=True)
class PostInstallVerification:
    """Projected post-install verification (dry-run).

    Each entry is ``(check_name, would_pass, detail)``. In dry-run,
    ``would_pass`` reflects the *current* state (a check that would
    pass after install but currently fails is reported as
    ``would_pass=False`` with a note that the install would fix it).
    """

    checks: Tuple[Tuple[str, bool, str], ...]
    notes: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "checks": [
                {"name": n, "would_pass": p, "detail": d}
                for n, p, d in self.checks
            ],
            "notes": list(self.notes),
            "would_pass_all": all(p for _, p, _ in self.checks) if self.checks else True,
        }


@dataclass(frozen=True)
class WorkflowSummary:
    """Top-level summary of the workflow run."""

    profile: str
    dry_run: bool
    doctor_verdict: str
    doctor_exit_code: int
    install_exit_code: int
    overall_exit_code: int
    overall_verdict: str  # OK / CAVEAT / FAIL / BLOCKED
    timestamp: str = ""  # set by caller; not auto-populated to keep frozen

    def to_dict(self) -> dict:
        return {
            "profile": self.profile,
            "dry_run": self.dry_run,
            "doctor_verdict": self.doctor_verdict,
            "doctor_exit_code": self.doctor_exit_code,
            "install_exit_code": self.install_exit_code,
            "overall_exit_code": self.overall_exit_code,
            "overall_verdict": self.overall_verdict,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class InstallWorkflowResult:
    """Full result of :func:`run_workflow`.

    Composes every stage's output. ``executed`` is False in this slice
    (dry-run only). ``summary`` carries the top-level verdict + exit
    codes. ``error`` is non-empty only when the workflow raised
    internally (and was caught); in that case the summary's
    ``overall_verdict`` is ``"BLOCKED"``.
    """

    summary: WorkflowSummary
    doctor_report: DoctorReport
    install_plan: Optional[InstallPlan]
    install_preflight: Optional[PreFlightResult]
    platform_bootstrap: PlatformBootstrapPlan
    directory_init: DirectoryInitPlan
    config_bootstrap: ConfigBootstrapPlan
    post_install_verification: PostInstallVerification
    executed: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "summary": self.summary.to_dict(),
            "doctor_report": self.doctor_report.to_dict(),
            "install_plan": self.install_plan.to_dict() if self.install_plan else None,
            "install_preflight": (
                self.install_preflight.to_dict() if self.install_preflight else None
            ),
            "platform_bootstrap": self.platform_bootstrap.to_dict(),
            "directory_init": self.directory_init.to_dict(),
            "config_bootstrap": self.config_bootstrap.to_dict(),
            "post_install_verification": self.post_install_verification.to_dict(),
            "executed": self.executed,
            "error": self.error,
        }


# ---------------------------------------------------------------------------#
# Internal stage functions (pure; no side effects in dry-run)
# ---------------------------------------------------------------------------#


def _resolve_exit_for_doctor(verdict: str) -> int:
    """Map the doctor's verdict to its exit code."""
    if verdict == "PASS":
        return EXIT_DOCTOR_OK
    if verdict == "CAVEAT":
        return EXIT_DOCTOR_CAVEATS
    return EXIT_DOCTOR_FAILED


def _resolve_overall(
    doctor_exit: int,
    install_exit: int,
    *,
    preflight_ok: bool,
) -> Tuple[int, str]:
    """Compute the overall exit code and verdict string.

    Precedence (worst wins):
      * profile switch / pre-flight fail (5/4) > doctor FAIL (8)
        > doctor CAVEAT (7) > OK (0).
    """
    if install_exit == EXIT_PROFILE_SWITCH_REJECTED:
        return EXIT_PROFILE_SWITCH_REJECTED, "BLOCKED"
    if install_exit == EXIT_PRE_FLIGHT_FAILED or not preflight_ok:
        return EXIT_PRE_FLIGHT_FAILED, "BLOCKED"
    if doctor_exit == EXIT_DOCTOR_FAILED:
        return EXIT_DOCTOR_FAILED, "FAIL"
    if doctor_exit == EXIT_DOCTOR_CAVEATS:
        return EXIT_DOCTOR_CAVEATS, "CAVEAT"
    return EXIT_OK, "OK"


_REQUIRED_DIRS: Tuple[str, ...] = ("data", "reports", "logs")


def _plan_directory_init(
    repo_root: Path,
    *,
    existing_profile: Optional[str],
) -> DirectoryInitPlan:
    """Build a :class:`DirectoryInitPlan` from the current filesystem state."""
    entries: List[Tuple[str, str, bool]] = []
    for name in _REQUIRED_DIRS:
        d = repo_root / name
        entries.append((name, "runtime directory", d.exists()))
    marker = repo_root / ".aee-profile"
    marker_exists = marker.exists()
    marker_would_write = existing_profile is None
    # Include the marker in the entries for visibility.
    entries.append((".aee-profile", "profile marker", marker_exists))
    return DirectoryInitPlan(
        entries=tuple(entries),
        marker_would_write=marker_would_write,
    )


def _plan_config_bootstrap(
    repo_root: Path,
    profile: str,
    *,
    existing_profile: Optional[str],
) -> ConfigBootstrapPlan:
    """Build a :class:`ConfigBootstrapPlan` from current state.

    Detects ``.env`` presence and a best-effort supervisord conf
    presence (looks under ``supervisor/`` and ``/etc/supervisor/conf.d/``
    for an AEE-named file; read-only). The profile marker write is
    planned when no existing marker is present.
    """
    env_path = repo_root / ".env"
    env_present = env_path.exists()

    # Best-effort supervisor conf detection. We do NOT spawn any
    # process; we look for known file paths. This is intentionally
    # conservative — a missing conf under supervisor/ does not imply
    # the system has no supervisord program (it could be installed
    # elsewhere). The plan records the detection, not a guarantee.
    sup_conf_candidates: List[Path] = [
        repo_root / "supervisor" / "aee-{p}.conf".format(p=profile),
        repo_root / "supervisor" / "aee.conf",
        Path("/etc/supervisor/conf.d/aee-{p}.conf".format(p=profile)),
        Path("/etc/supervisor/conf.d/aee.conf"),
    ]
    sup_present = any(p.exists() for p in sup_conf_candidates)

    marker_would_write = existing_profile is None

    return ConfigBootstrapPlan(
        profile=profile,
        env_file_present=env_present,
        env_file_would_install=not env_present,
        supervisor_conf_present=sup_present,
        supervisor_conf_would_install=not sup_present,
        profile_marker_would_write=marker_would_write,
    )


def _dependency_plan_to_dict(plan: object) -> dict:
    """Convert a W2/W3 dependency plan into a JSON-serializable dict.

    Both :class:`aee.installer.linux_bootstrap.DependencyPlan` and
    :class:`aee.installer.macos_bootstrap.BrewDependencyPlan` are
    frozen dataclasses without a ``to_dict`` method. We use
    :func:`dataclasses.asdict` plus a few explicit field conversions
    to produce a JSON-serializable dict for the workflow result.
    """
    import dataclasses as _dc
    if not _dc.is_dataclass(plan):
        return {}
    raw = _dc.asdict(plan)  # type: ignore[arg-type]
    # Convert non-JSON-native types to strings.
    out: Dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, (str, int, float, bool, type(None))):
            out[key] = value
        elif isinstance(value, (list, tuple)):
            out[key] = list(value)
        elif _dc.is_dataclass(value):
            out[key] = _dependency_plan_to_dict(value)
        else:
            out[key] = str(value)
    # Attach the command strings (computed properties) so the plan
    # dict carries the actionable install command.
    apt_cmd = getattr(plan, "apt_command", None)
    if apt_cmd is not None:
        out["apt_command"] = apt_cmd
    pkg_count = getattr(plan, "package_count", None)
    if pkg_count is not None:
        out["package_count"] = pkg_count
    brew_cmd = getattr(plan, "brew_command", None)
    if brew_cmd is not None:
        out["brew_command"] = brew_cmd
    formulae_count = getattr(plan, "formulae_count", None)
    if formulae_count is not None:
        out["formulae_count"] = formulae_count
    return out


def _run_platform_bootstrap(
    repo_root: Path,
    profile: str,
) -> PlatformBootstrapPlan:
    """Detect platform + build the dependency plan (W2/W3).

    Imports W2/W3 lazily so a missing optional dependency on an
    unsupported platform does not break workflow import. Captures
    any error into the plan's ``error`` field rather than raising.
    """
    # Lazy imports — these modules raise on unsupported platforms.
    try:
        from aee.installer.lifecycle import detect_platform  # noqa: F401
        from aee.platform.current import PlatformIdentity, resolve_platform_identity
    except Exception as exc:  # pragma: no cover — defensive
        return PlatformBootstrapPlan(
            platform="unknown",
            supported=False,
            profile_allowed=False,
            dependency_plan={},
            error="platform resolver unavailable: {e}".format(e=exc),
        )

    try:
        ident = resolve_platform_identity()
    except Exception as exc:  # pragma: no cover
        return PlatformBootstrapPlan(
            platform="unknown",
            supported=False,
            profile_allowed=False,
            dependency_plan={},
            error="resolve_platform_identity failed: {e}".format(e=exc),
        )

    platform_name = str(ident)

    # W2: Linux apt path (Ubuntu/Debian; the linux_bootstrap layer
    # detects distro via /etc/os-release and raises UnsupportedDistroError
    # for non-apt distros, so we route all LINUX hosts through it and
    # capture the error).
    if ident is PlatformIdentity.LINUX:
        try:
            from aee.installer.linux_bootstrap import (
                UnsupportedDistroError as _UDE,
                UnsupportedProfileError as _UPE,
            )
            from aee.installer.linux_bootstrap import plan_for_current_host
        except Exception as exc:  # pragma: no cover
            return PlatformBootstrapPlan(
                platform=platform_name,
                supported=False,
                profile_allowed=False,
                dependency_plan={},
                error="linux_bootstrap unavailable: {e}".format(e=exc),
            )
        try:
            plan = plan_for_current_host(
                repo_root=str(repo_root),
                profile=profile,
            )
            return PlatformBootstrapPlan(
                platform=platform_name,
                supported=True,
                profile_allowed=True,
                dependency_plan=_dependency_plan_to_dict(plan),
            )
        except _UDE as exc:
            return PlatformBootstrapPlan(
                platform=platform_name,
                supported=False,
                profile_allowed=False,
                dependency_plan={},
                error="distro not supported by apt bootstrap: {e}".format(e=exc),
            )
        except _UPE as exc:
            return PlatformBootstrapPlan(
                platform=platform_name,
                supported=True,
                profile_allowed=False,
                dependency_plan={},
                error="profile not allowed on this platform: {e}".format(e=exc),
            )
        except Exception as exc:
            return PlatformBootstrapPlan(
                platform=platform_name,
                supported=False,
                profile_allowed=False,
                dependency_plan={},
                error="linux_bootstrap plan failed: {e}".format(e=exc),
            )

    # W3: macOS brew path.
    if ident is PlatformIdentity.MACOS:
        try:
            from aee.installer.macos_bootstrap import (
                UnsupportedProfileError as _UPE_M,
            )
            from aee.installer.macos_bootstrap import plan_for_current_macos_host
        except Exception as exc:  # pragma: no cover
            return PlatformBootstrapPlan(
                platform=platform_name,
                supported=False,
                profile_allowed=False,
                dependency_plan={},
                error="macos_bootstrap unavailable: {e}".format(e=exc),
            )
        try:
            plan = plan_for_current_macos_host(
                repo_root=str(repo_root),
                profile=profile,
            )
            return PlatformBootstrapPlan(
                platform=platform_name,
                supported=True,
                profile_allowed=True,
                dependency_plan=_dependency_plan_to_dict(plan),
            )
        except _UPE_M as exc:
            return PlatformBootstrapPlan(
                platform=platform_name,
                supported=True,
                profile_allowed=False,
                dependency_plan={},
                error="profile not allowed on macOS v1: {e}".format(e=exc),
            )
        except Exception as exc:
            return PlatformBootstrapPlan(
                platform=platform_name,
                supported=False,
                profile_allowed=False,
                dependency_plan={},
                error="macos_bootstrap plan failed: {e}".format(e=exc),
            )

    # Unsupported platform (e.g. Windows resolves to UNKNOWN).
    return PlatformBootstrapPlan(
        platform=platform_name,
        supported=False,
        profile_allowed=False,
        dependency_plan={},
        error="platform {p} not yet supported by the bootstrap".format(
            p=platform_name
        ),
    )


def _project_post_install_verification(
    repo_root: Path,
    profile: str,
    *,
    directory_init: DirectoryInitPlan,
    config_bootstrap: ConfigBootstrapPlan,
    platform_bootstrap: PlatformBootstrapPlan,
) -> PostInstallVerification:
    """Project the post-install verification checks (dry-run).

    Each check is reported as ``would_pass`` reflecting the state
    *after* a successful install would complete. We project this from
    the current state + the install plan: a check that currently fails
    but would be fixed by the install is ``would_pass=True`` with a
    note. A check that currently fails AND the install does not
    address is ``would_pass=False``.
    """
    checks: List[Tuple[str, bool, str]] = []
    notes: List[str] = []

    # 1. Profile marker would be present after install (if plan writes it).
    if config_bootstrap.profile_marker_would_write:
        checks.append((
            "profile_marker",
            True,
            "marker would be written to .aee-profile by the install",
        ))
    else:
        marker = repo_root / ".aee-profile"
        if marker.exists():
            checks.append((
                "profile_marker",
                True,
                "marker already present at .aee-profile",
            ))
        else:
            checks.append((
                "profile_marker",
                False,
                "marker missing and install does not write it",
            ))

    # 2. Required directories would be present after install.
    missing = [p for p, _, e in directory_init.entries if not e and p != ".aee-profile"]
    if missing:
        checks.append((
            "required_directories",
            True,
            "install would create: {m}".format(m=", ".join(missing)),
        ))
    else:
        checks.append((
            "required_directories",
            True,
            "all required directories already present",
        ))

    # 3. Env file would be present after install (if plan writes it).
    if config_bootstrap.env_file_would_install:
        checks.append((
            "env_file",
            True,
            "install would write .env with 0600 (mini) or 0640 (others)",
        ))
    elif config_bootstrap.env_file_present:
        checks.append((
            "env_file",
            True,
            ".env already present",
        ))
    else:
        checks.append((
            "env_file",
            False,
            ".env missing and install does not write it",
        ))

    # 4. Dependencies would be importable after install (if currently missing).
    try:
        from aee.doctor import _check_dependencies  # type: ignore
        dep_check = _check_dependencies()
        if dep_check.status == "PASS":
            checks.append((
                "required_dependencies",
                True,
                "all required deps already importable",
            ))
        else:
            # Install would run pip install -r requirements.lock.
            checks.append((
                "required_dependencies",
                True,
                "install would run pip install -r requirements.lock "
                "(currently: {d})".format(d=dep_check.detail),
            ))
    except Exception as exc:  # pragma: no cover
        checks.append((
            "required_dependencies",
            False,
            "cannot project dependency check: {e}".format(e=exc),
        ))

    # 5. Platform bootstrap would succeed (if supported + profile allowed).
    if platform_bootstrap.supported and platform_bootstrap.profile_allowed:
        checks.append((
            "platform_bootstrap",
            True,
            "platform {p} supported for profile {pr}".format(
                p=platform_bootstrap.platform, pr=profile
            ),
        ))
    elif platform_bootstrap.supported and not platform_bootstrap.profile_allowed:
        checks.append((
            "platform_bootstrap",
            False,
            "platform supported but profile not allowed: {e}".format(
                e=platform_bootstrap.error
            ),
        ))
        notes.append(
            "Profile {p} is not supported on platform {pl}; the install "
            "would fail at the bootstrap stage. Choose a supported "
            "profile for this platform.".format(p=profile, pl=platform_bootstrap.platform)
        )
    else:
        checks.append((
            "platform_bootstrap",
            False,
            "platform not supported by the bootstrap: {e}".format(
                e=platform_bootstrap.error
            ),
        ))
        notes.append(
            "Platform {p} is not yet supported by the AEE bootstrap; "
            "manual dependency installation is required.".format(
                p=platform_bootstrap.platform
            )
        )

    return PostInstallVerification(
        checks=tuple(checks),
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------#
# Public entrypoint
# ---------------------------------------------------------------------------#


def run_workflow(
    *,
    profile: str = DEFAULT_PROFILE,
    repo_root: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
    network: bool = True,
    dry_run: bool = True,
    connect_timeout: float = 2.0,
) -> InstallWorkflowResult:
    """Run the Phase 3 installer workflow.

    Returns an :class:`InstallWorkflowResult`. In dry-run (the
    default), no side effects are performed. When ``dry_run=False``
    is explicitly requested, :class:`ExecuteNotAuthorizedError` is
    raised (the shell-level execution path is a separately
    authorizable follow-up, matching the §21.3 guard).
    """
    root = Path(repo_root) if repo_root is not None else Path.cwd()
    env = environ if environ is not None else os.environ

    # Validate profile up front (defence in depth — the doctor also
    # validates, but we want a clean error before any stage runs).
    try:
        canonical = parse_profile(profile)
    except UnknownProfileError as exc:
        # Build a minimal BLOCKED result so the caller gets a structured
        # error rather than an exception.
        doctor = DoctorRunner(
            repo_root=root, environ=env, profile=profile, network=False,
        ).run()
        summary = WorkflowSummary(
            profile=profile,
            dry_run=dry_run,
            doctor_verdict=doctor.verdict,
            doctor_exit_code=_resolve_exit_for_doctor(doctor.verdict),
            install_exit_code=EXIT_PROFILE_SWITCH_REJECTED,
            overall_exit_code=EXIT_PRE_FLIGHT_FAILED,
            overall_verdict="BLOCKED",
        )
        return InstallWorkflowResult(
            summary=summary,
            doctor_report=doctor,
            install_plan=None,
            install_preflight=None,
            platform_bootstrap=PlatformBootstrapPlan(
                platform="unknown",
                supported=False,
                profile_allowed=False,
                dependency_plan={},
                error="unknown profile: {e}".format(e=exc),
            ),
            directory_init=DirectoryInitPlan(entries=(), marker_would_write=False),
            config_bootstrap=ConfigBootstrapPlan(
                profile=profile,
                env_file_present=False,
                env_file_would_install=False,
                supervisor_conf_present=False,
                supervisor_conf_would_install=False,
                profile_marker_would_write=False,
            ),
            post_install_verification=PostInstallVerification(checks=()),
            executed=False,
            error="unknown profile: {e}".format(e=exc),
        )

    # Refuse --execute (shell path is a separately authorizable follow-up).
    if not dry_run:
        raise ExecuteNotAuthorizedError()

    # --- Stage 1: Phase 2 doctor (readiness probe) ----------------------
    doctor_runner = DoctorRunner(
        repo_root=root,
        environ=env,
        profile=canonical,
        network=network,
        connect_timeout=connect_timeout,
    )
    doctor_report = doctor_runner.run()
    doctor_exit = _resolve_exit_for_doctor(doctor_report.verdict)

    # --- Stage 2: §21.3 installer backend (plan + pre-flight) ------------
    backend = InstallerBackend(repo_root=root, dry_run=True)
    install_plan = backend.plan(canonical)
    install_preflight = backend.preflight(canonical)
    install_result = backend.execute(canonical, dry_run=True)
    # Determine install exit code from the pre-flight result.
    if not install_preflight.ok:
        if (
            install_preflight.existing_profile is not None
            and install_preflight.existing_profile != canonical
        ):
            install_exit = EXIT_PROFILE_SWITCH_REJECTED
        else:
            install_exit = EXIT_PRE_FLIGHT_FAILED
    else:
        install_exit = EXIT_OK

    # --- Stage 3: platform bootstrap (W2/W3 detection + dep plan) ------
    platform_bootstrap = _run_platform_bootstrap(root, canonical)

    # --- Stage 4: directory initialization plan -------------------------
    directory_init = _plan_directory_init(
        root, existing_profile=install_preflight.existing_profile,
    )

    # --- Stage 5: configuration bootstrap plan ---------------------------
    config_bootstrap = _plan_config_bootstrap(
        root, canonical, existing_profile=install_preflight.existing_profile,
    )

    # --- Stage 6: post-install verification (projected, dry-run) -------
    post_install = _project_post_install_verification(
        root,
        canonical,
        directory_init=directory_init,
        config_bootstrap=config_bootstrap,
        platform_bootstrap=platform_bootstrap,
    )

    # --- Compose the summary ---------------------------------------------
    overall_exit, overall_verdict = _resolve_overall(
        doctor_exit, install_exit, preflight_ok=install_preflight.ok,
    )
    summary = WorkflowSummary(
        profile=canonical,
        dry_run=True,
        doctor_verdict=doctor_report.verdict,
        doctor_exit_code=doctor_exit,
        install_exit_code=install_exit,
        overall_exit_code=overall_exit,
        overall_verdict=overall_verdict,
    )

    return InstallWorkflowResult(
        summary=summary,
        doctor_report=doctor_report,
        install_plan=install_plan,
        install_preflight=install_preflight,
        platform_bootstrap=platform_bootstrap,
        directory_init=directory_init,
        config_bootstrap=config_bootstrap,
        post_install_verification=post_install,
        executed=False,
        error="",
    )


__all__ = [
    "EXIT_WORKFLOW_OK",
    "EXIT_WORKFLOW_PRE_FLIGHT_FAILED",
    "EXIT_WORKFLOW_PROFILE_SWITCH_REJECTED",
    "EXIT_WORKFLOW_EXECUTE_NOT_AUTHORIZED",
    "EXIT_WORKFLOW_DOCTOR_CAVEATS",
    "EXIT_WORKFLOW_DOCTOR_FAILED",
    "DirectoryInitPlan",
    "ConfigBootstrapPlan",
    "PlatformBootstrapPlan",
    "PostInstallVerification",
    "WorkflowSummary",
    "InstallWorkflowResult",
    "run_workflow",
]