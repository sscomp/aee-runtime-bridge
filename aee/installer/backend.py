"""AEE Epic 9.3 — Installer Backend implementation (§21.3).

This module implements the profile-aware installer backend described
in Master Plan §21.3. See :mod:`aee.installer` package docstring for
the full design contract.

Key types:

* :class:`InstallerBackend` — the backend object. Constructed with a
  ``repo_root`` (used to locate the ``.aee-profile`` marker) and a
  ``dry_run`` flag (default ``True``).
* :class:`InstallPlan` — the structured plan for a single profile
  install. Returned by :meth:`InstallerBackend.plan` and
  :func:`plan_install`.
* :class:`InstallPlanStep` — one step in the plan (e.g. ``preflight``,
  ``system_user``, ``env_file``, ``supervisor_conf``, ``smoke_test``).
* :class:`PreFlightResult` — the result of read-only pre-flight.
* :class:`InstallResult` — the result of :meth:`InstallerBackend.execute`.

Exit code semantics (the CLI layer maps these to process exit codes):

* :data:`EXIT_OK` (0) — success.
* :data:`EXIT_PROFILE_INVALID` (3) — unknown profile (defence in
  depth; argparse ``choices`` already rejects this at the CLI layer).
* :data:`EXIT_PRE_FLIGHT_FAILED` (4) — pre-flight checks failed.
* :data:`EXIT_PROFILE_SWITCH_REJECTED` (5) — an existing install with
  a different profile was detected (§21.3 "profile switch requires
  uninstall + reinstall").
* :data:`EXIT_EXECUTE_NOT_AUTHORIZED` (6) — ``execute(dry_run=False)``
  was called but the shell-level execution path is not yet
  authorized in this slice.

All exit codes are distinct from the CLI's argparse exit code 2
(argument parsing failure) so the two layers compose without
ambiguity.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import FrozenSet, List, Optional, Tuple

# Canonical source of truth — NO parallel hard-coded matrix.
from aee.profiles.descriptor import (
    DEFAULT_PROFILE,
    KNOWN_PROFILES,
    ProfileDescriptor,
    UnknownProfileError,
    get_descriptor,
    parse_profile,
)

# Phase 4A — import the proposed bootstrap v1 exit-code constants (§10.4)
# from the lifecycle module so the backend's exception classes can map to
# them without duplicating the canonical definitions.  The lifecycle module
# owns these constants (W1 skeleton); the backend owns the exception
# hierarchy.  No circular import: lifecycle imports only platform.current.
from aee.installer.lifecycle import (
    EXIT_STAGE_FAILED_RETRYABLE,
    EXIT_STAGE_FAILED_PERMANENT,
    EXIT_DRIFT_DETECTED,
    EXIT_NETWORK_ERROR,
    EXIT_SECRET_MISSING,
    EXIT_DEPENDENCY_FLOOR_NOT_MET,
)


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_PROFILE_INVALID = 3
EXIT_PRE_FLIGHT_FAILED = 4
EXIT_PROFILE_SWITCH_REJECTED = 5
EXIT_EXECUTE_NOT_AUTHORIZED = 6


# ---------------------------------------------------------------------------
# Plan step identifiers (stable vocabulary; the shell layer will map
# these to concrete shell operations in a follow-up slice)
# ---------------------------------------------------------------------------

# Universal steps: every profile gets these.
UNIVERSAL_STEPS: Tuple[str, ...] = (
    "preflight",          # read-only pre-flight checks
    "venv",               # ensure virtualenv exists at repo_root/.venv
    "supervisor_conf",    # install supervisord program for this profile
    "health_check",       # wait for /health to return 200
)

# Mini hardening steps: only the ``mini`` profile gets these (per
# §21.3: "--profile mini absorbs AEE-MINI's hardening: idempotent
# pre-flight, aee system user, 0600 env file, smoke test").
MINI_HARDENING_STEPS: Tuple[str, ...] = (
    "system_user",        # ensure the ``aee`` system user exists
    "env_file_0600",      # install .env with 0600 permissions
    "smoke_test",         # run a bounded smoke test post-install
)

# Edge-specific: PRAGMA query_only=1 wrapping (per §21.4, the installer
# must lay down the env var that the runtime reads). Listed here for
# completeness; the actual enforcement is §21.4 runtime selection.
EDGE_STEPS: Tuple[str, ...] = (
    "edge_readonly_env",  # set AEE_DB_READ_ONLY=1 in .env
)

# Developer-specific: tempdir DB + sandbox (per §21.5 developer row).
DEVELOPER_STEPS: Tuple[str, ...] = (
    "developer_tempdir_db",  # point AEE_DB_PATH at a tempdir SQLite
    "developer_smoke",       # interactive sandbox smoke
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InstallerError(Exception):
    """Base class for installer backend errors."""

    exit_code: int = EXIT_OK  # overridden by subclasses


class ProfileSwitchRejectedError(InstallerError):
    """An existing install with a different profile was detected.

    Per §21.3: "Profile switch on existing install is rejected
    (profile change requires uninstall + reinstall)."
    """

    exit_code = EXIT_PROFILE_SWITCH_REJECTED

    def __init__(self, existing: str, requested: str) -> None:
        super().__init__(
            "profile switch rejected: existing install is '{existing}' "
            "but '{requested}' was requested (per §21.3, profile change "
            "requires uninstall + reinstall)".format(
                existing=existing, requested=requested,
            )
        )
        self.existing = existing
        self.requested = requested


class PreFlightFailedError(InstallerError):
    """Pre-flight checks failed."""

    exit_code = EXIT_PRE_FLIGHT_FAILED

    def __init__(self, reason: str) -> None:
        super().__init__("pre-flight failed: {reason}".format(reason=reason))
        self.reason = reason


class ExecuteNotAuthorizedError(InstallerError):
    """``execute(dry_run=False)`` was called but not authorized.

    The shell-level execution path (subprocess / system user creation
    / env file writes / supervisord reload / smoke test) is a
    separately authorizable follow-up to this slice. The plan + the
    dry-run pre-flight are the deliverable here.
    """

    exit_code = EXIT_EXECUTE_NOT_AUTHORIZED

    def __init__(self) -> None:
        super().__init__(
            "execute(dry_run=False) is not authorized in this slice; "
            "the §21.3 shell-level install path is a separately "
            "authorizable follow-up (use plan() + preflight() only)"
        )


# ---------------------------------------------------------------------------
# Phase 4A — bootstrap v1 exit-code exception hierarchy (§10.4 proposed)
# ---------------------------------------------------------------------------
#
# Each new exception maps 1:1 to a §10.4 proposed exit-code constant
# imported from ``aee.installer.lifecycle`` (W1 skeleton owns the
# constants; the backend owns the exception hierarchy).  These are
# *introduced* in Phase 4A as named, raisable exception classes so the
# future W4/W5 CLI layers (and the shell trampolines W6/W7) can map a
# failure mode to the correct exit code via ``except StageFailedRetryableError``
# rather than re-deriving the integer.  The constants themselves are
# NOT renumbered — they are the same values already pinned by
# ``aee/tests/test_installer_lifecycle.py::TestExitConstants``.


class StageFailedRetryableError(InstallerError):
    """A bootstrap stage failed but is retryable (§10.4 code 7).

    Re-run with ``--resume``.  Raised by the future execute path when
    a stage reports a transient failure (e.g. temporary network
    blip, lock contention).  Phase 4A introduces the class; the
    shell layer (W6/W7) will raise it.
    """

    exit_code = EXIT_STAGE_FAILED_RETRYABLE

    def __init__(self, stage: str, reason: str = "") -> None:
        msg = "stage '{s}' failed (retryable)".format(s=stage)
        if reason:
            msg += ": {r}".format(r=reason)
        super().__init__(msg)
        self.stage = stage
        self.reason = reason


class StageFailedPermanentError(InstallerError):
    """A bootstrap stage failed permanently (§10.4 code 8).

    Max retries exceeded; requires ``--force-retry`` or operator
    intervention.  Raised by the future execute path when a stage
    has exhausted its ``MAX_RETRY`` budget.
    """

    exit_code = EXIT_STAGE_FAILED_PERMANENT

    def __init__(self, stage: str, reason: str = "") -> None:
        msg = "stage '{s}' failed (permanent)".format(s=stage)
        if reason:
            msg += ": {r}".format(r=reason)
        super().__init__(msg)
        self.stage = stage
        self.reason = reason


class DriftDetectedError(InstallerError):
    """On-disk state drifted from the recorded pin (§10.4 code 9).

    ``commit_sha`` or ``requirements_lock_sha256`` mismatch detected
    by ``aee doctor`` or ``aee update``.  Read-only detection — the
    caller decides whether to re-pin or re-install.
    """

    exit_code = EXIT_DRIFT_DETECTED

    def __init__(self, field: str, expected: str, actual: str) -> None:
        super().__init__(
            "drift detected: {f} expected={e} actual={a}".format(
                f=field, e=expected, a=actual,
            )
        )
        self.field = field
        self.expected = expected
        self.actual = actual


class NetworkError(InstallerError):
    """Network / git error (§10.4 code 10).

    Clone, fetch, or package-mirror unreachable.  Raised by the
    future execute path (W4/W5) when a network-dependent step fails
    for reasons distinct from a stage retryable failure.
    """

    exit_code = EXIT_NETWORK_ERROR

    def __init__(self, operation: str, reason: str = "") -> None:
        msg = "network error during '{op}'".format(op=operation)
        if reason:
            msg += ": {r}".format(r=reason)
        super().__init__(msg)
        self.operation = operation
        self.reason = reason


class SecretMissingError(InstallerError):
    """A required secret is missing or invalid (§10.4 code 11).

    Raised by the future execute path (W4/W5) or ``aee doctor``
    when a required secret (API key, token, password) is absent or
    fails validation.  Never include the secret value in the
    message — only the secret *name*.
    """

    exit_code = EXIT_SECRET_MISSING

    def __init__(self, secret_name: str) -> None:
        super().__init__(
            "required secret missing or invalid: '{n}'".format(n=secret_name)
        )
        self.secret_name = secret_name


class DependencyFloorNotMetError(InstallerError):
    """A hard dependency floor is not met (§10.4 code 12).

    git, python, or node version below the required floor and cannot
    be auto-installed.  Raised by the future execute path (W4/W5)
    during the ``01_deps`` stage.
    """

    exit_code = EXIT_DEPENDENCY_FLOOR_NOT_MET

    def __init__(self, dependency: str, required: str, found: str) -> None:
        super().__init__(
            "dependency floor not met for '{d}': required>={r} found={f}".format(
                d=dependency, r=required, f=found,
            )
        )
        self.dependency = dependency
        self.required = required
        self.found = found


# ---------------------------------------------------------------------------
# Plan dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstallPlanStep:
    """One step in an :class:`InstallPlan`.

    ``step_id`` is one of the stable vocabulary identifiers above
    (``preflight``, ``system_user``, etc.). ``description`` is a
    human-readable summary. ``side_effect`` documents whether the
    step **would** perform a side effect when executed (it does not
    mean the step is executed in this slice — dry-run is the default).
    """

    step_id: str
    description: str
    side_effect: bool = False

    def to_dict(self) -> dict:
        return {
            "step_id": self.step_id,
            "description": self.description,
            "side_effect": self.side_effect,
        }


@dataclass(frozen=True)
class InstallPlan:
    """Structured plan for installing one profile.

    The plan is pure data — it describes *what* would happen, not
    *how*. The shell layer (a follow-up slice) will map each
    :class:`InstallPlanStep` to concrete shell operations.
    """

    profile: str
    descriptor: ProfileDescriptor
    steps: Tuple[InstallPlanStep, ...]
    dry_run: bool = True
    notes: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "profile": self.profile,
            "descriptor": self.descriptor.to_dict(),
            "steps": [s.to_dict() for s in self.steps],
            "dry_run": self.dry_run,
            "notes": list(self.notes),
            "step_count": len(self.steps),
        }


@dataclass(frozen=True)
class PreFlightResult:
    """Read-only pre-flight result.

    ``ok`` is True when pre-flight passed. ``existing_profile`` is
    the profile detected from the ``.aee-profile`` marker file at
    ``repo_root``, or ``None`` when no marker exists (fresh install).
    """

    ok: bool
    existing_profile: Optional[str]
    checks: Tuple[Tuple[str, bool, str], ...]  # (name, ok, detail)
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "existing_profile": self.existing_profile,
            "checks": [
                {"name": n, "ok": o, "detail": d}
                for n, o, d in self.checks
            ],
            "reason": self.reason,
        }


@dataclass(frozen=True)
class InstallResult:
    """Result of :meth:`InstallerBackend.execute`.

    In this slice, ``executed`` is always ``False`` (dry-run only);
    ``plan`` carries the structured plan; ``preflight`` carries the
    pre-flight result. When the shell layer lands, ``executed=True``
    will indicate that side effects were actually performed.
    """

    executed: bool
    plan: InstallPlan
    preflight: PreFlightResult

    def to_dict(self) -> dict:
        return {
            "executed": self.executed,
            "plan": self.plan.to_dict(),
            "preflight": self.preflight.to_dict(),
        }


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class InstallerBackend:
    """Profile-aware installer backend (§21.3).

    Construction is cheap; no I/O is performed until
    :meth:`preflight` or :meth:`execute` is called.
    """

    MARKER_FILENAME = ".aee-profile"

    def __init__(
        self,
        repo_root: Optional[Path] = None,
        *,
        dry_run: bool = True,
    ) -> None:
        self.repo_root = Path(repo_root) if repo_root is not None else Path.cwd()
        self.dry_run = dry_run

    # -- plan -----------------------------------------------------------

    def plan(self, profile: str) -> InstallPlan:
        """Build an :class:`InstallPlan` for ``profile``.

        Validates ``profile`` against the canonical
        :data:`KNOWN_PROFILES`. Raises :class:`UnknownProfileError`
        (from the descriptor module) on an unknown profile — this is
        the defence-in-depth path; the CLI layer's argparse
        ``choices`` is the primary validation.
        """
        canonical = parse_profile(profile)
        descriptor = get_descriptor(canonical)
        steps = self._build_steps(canonical)
        notes: List[str] = []
        if canonical == "mini":
            notes.append(
                "mini profile absorbs AEE-MINI hardening (idempotent "
                "pre-flight, aee system user, 0600 env file, smoke "
                "test) per §21.3."
            )
        if canonical == "edge":
            notes.append(
                "edge profile sets AEE_DB_READ_ONLY=1; runtime "
                "enforcement (PRAGMA query_only=1) is §21.4."
            )
        if canonical == "developer":
            notes.append(
                "developer profile uses tempdir DB + sandbox; no "
                "production DB access."
            )
        notes.append(
            "AEE-MINI installer continues to work during the §21.10 "
            "deprecation window; this backend does not migrate or "
            "remove it."
        )
        return InstallPlan(
            profile=canonical,
            descriptor=descriptor,
            steps=tuple(steps),
            dry_run=self.dry_run,
            notes=tuple(notes),
        )

    def _build_steps(self, profile: str) -> List[InstallPlanStep]:
        """Assemble the step list for ``profile``.

        Order: universal steps first, then profile-specific hardening,
        then a final ``verify`` step. The step list is data only —
        no side effects happen here.
        """
        steps: List[InstallPlanStep] = []
        for sid in UNIVERSAL_STEPS:
            steps.append(InstallPlanStep(
                step_id=sid,
                description=self._describe(sid, profile),
                side_effect=sid in ("venv", "supervisor_conf"),
            ))
        if profile == "mini":
            for sid in MINI_HARDENING_STEPS:
                steps.append(InstallPlanStep(
                    step_id=sid,
                    description=self._describe(sid, profile),
                    side_effect=True,
                ))
        elif profile == "edge":
            for sid in EDGE_STEPS:
                steps.append(InstallPlanStep(
                    step_id=sid,
                    description=self._describe(sid, profile),
                    side_effect=True,
                ))
        elif profile == "developer":
            for sid in DEVELOPER_STEPS:
                steps.append(InstallPlanStep(
                    step_id=sid,
                    description=self._describe(sid, profile),
                    side_effect=True,
                ))
        steps.append(InstallPlanStep(
            step_id="verify",
            description="Verify install: health check + profile marker.",
            side_effect=False,
        ))
        return steps

    @staticmethod
    def _describe(step_id: str, profile: str) -> str:
        if step_id == "preflight":
            return "Read-only pre-flight: check repo, venv, existing profile marker."
        if step_id == "venv":
            return "Ensure virtualenv exists at {root}/.venv.".format(
                root="<repo_root>"
            )
        if step_id == "supervisor_conf":
            return "Install supervisord program for profile '{p}'.".format(
                p=profile
            )
        if step_id == "health_check":
            return "Wait for /health to return 200 (bounded retry)."
        if step_id == "system_user":
            return "Ensure the 'aee' system user exists (idempotent)."
        if step_id == "env_file_0600":
            return "Install .env with 0600 permissions."
        if step_id == "smoke_test":
            return "Run a bounded smoke test post-install."
        if step_id == "edge_readonly_env":
            return "Set AEE_DB_READ_ONLY=1 in .env (edge read-only mode)."
        if step_id == "developer_tempdir_db":
            return "Point AEE_DB_PATH at a tempdir SQLite (developer sandbox)."
        if step_id == "developer_smoke":
            return "Interactive sandbox smoke test."
        if step_id == "verify":
            return "Verify install: health check + profile marker."
        return step_id

    # -- pre-flight -----------------------------------------------------

    def detect_existing_profile(self) -> Optional[str]:
        """Read the ``.aee-profile`` marker at ``repo_root``.

        Returns the profile name, or ``None`` when no marker exists.
        Reads only — never writes. Validates the marker content
        against :data:`KNOWN_PROFILES`; a corrupted marker is treated
        as "no existing install" (returns ``None``).
        """
        marker = self.repo_root / self.MARKER_FILENAME
        if not marker.exists():
            return None
        try:
            content = marker.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if content in KNOWN_PROFILES:
            return content
        return None

    def preflight(self, profile: str) -> PreFlightResult:
        """Run read-only pre-flight for ``profile``.

        Pre-flight is safe to re-run (idempotent, per §21.3). It
        checks:

        1. ``profile`` is a known profile.
        2. The repo root exists.
        3. No conflicting existing install (profile switch rejected
           per §21.3).

        Returns a :class:`PreFlightResult`. Raises no exceptions —
        failures are encoded in the result. The caller (CLI layer)
        maps ``ok=False`` to :data:`EXIT_PRE_FLIGHT_FAILED` or
        :data:`EXIT_PROFILE_SWITCH_REJECTED` depending on the reason.
        """
        checks: List[Tuple[str, bool, str]] = []
        # Check 1: profile known.
        try:
            canonical = parse_profile(profile)
            checks.append(("profile_known", True, canonical))
        except UnknownProfileError as exc:
            checks.append(("profile_known", False, str(exc)))
            return PreFlightResult(
                ok=False,
                existing_profile=None,
                checks=tuple(checks),
                reason="unknown profile: {p}".format(p=profile),
            )
        # Check 2: repo root exists.
        root_ok = self.repo_root.exists() and self.repo_root.is_dir()
        checks.append((
            "repo_root_exists",
            root_ok,
            str(self.repo_root),
        ))
        if not root_ok:
            return PreFlightResult(
                ok=False,
                existing_profile=None,
                checks=tuple(checks),
                reason="repo_root does not exist: {p}".format(
                    p=str(self.repo_root)
                ),
            )
        # Check 3: profile switch.
        existing = self.detect_existing_profile()
        if existing is not None and existing != canonical:
            checks.append((
                "no_profile_switch",
                False,
                "existing={e} requested={r}".format(
                    e=existing, r=canonical
                ),
            ))
            return PreFlightResult(
                ok=False,
                existing_profile=existing,
                checks=tuple(checks),
                reason=(
                    "profile switch rejected (existing={e}, "
                    "requested={r}); per §21.3 uninstall + reinstall "
                    "required".format(e=existing, r=canonical)
                ),
            )
        checks.append((
            "no_profile_switch",
            True,
            "existing={e}".format(e=existing),
        ))
        return PreFlightResult(
            ok=True,
            existing_profile=existing,
            checks=tuple(checks),
            reason="",
        )

    # -- execute --------------------------------------------------------

    def execute(
        self,
        profile: str,
        *,
        dry_run: Optional[bool] = None,
    ) -> InstallResult:
        """Plan + pre-flight + (optionally) execute.

        In this slice, ``dry_run`` defaults to ``True`` (matching the
        backend's construction default). When ``dry_run=True`` (or
        when the backend was constructed with ``dry_run=True`` and
        ``dry_run`` is not overridden here), only planning + read-only
        pre-flight happen — no side effects.

        When ``dry_run=False`` is explicitly requested,
        :class:`ExecuteNotAuthorizedError` is raised: the shell-level
        execution path (system user creation, env file writes,
        supervisord reload, smoke test invocation) is a separately
        authorizable follow-up to this slice.
        """
        effective_dry_run = self.dry_run if dry_run is None else dry_run
        plan = self.plan(profile)
        pre = self.preflight(profile)
        if not pre.ok:
            # Encode the failure reason into the result; the CLI
            # layer inspects pre.reason to decide the exit code.
            return InstallResult(
                executed=False,
                plan=plan,
                preflight=pre,
            )
        if not effective_dry_run:
            raise ExecuteNotAuthorizedError()
        return InstallResult(
            executed=False,
            plan=plan,
            preflight=pre,
        )


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------


def plan_install(
    profile: str,
    *,
    repo_root: Optional[Path] = None,
    dry_run: bool = True,
) -> InstallPlan:
    """Build an :class:`InstallPlan` without constructing a backend.

    Convenience wrapper around :meth:`InstallerBackend.plan` for
    callers that only need the plan (no pre-flight, no execute).
    """
    backend = InstallerBackend(repo_root=repo_root, dry_run=dry_run)
    return backend.plan(profile)