"""AEE Epic 9.3 — Installer Backend (§21.3).

This package holds the profile-aware installer backend introduced by
Epic 9.3 (Master Plan §21.3 — Installer Profile). It is the Python
backend that the ``aee install`` CLI subcommand (§21.2) delegates to,
and that a future ``install.sh`` (§21.3 single-shell-installer goal)
will orchestrate.

Design contract (per Master Plan §21.3):

* **Single installer surface** accepting
  ``--profile {full,mini,edge,developer}``. There is **no** parallel
  hard-coded profile matrix — the backend imports the canonical
  :data:`aee.profiles.descriptor.KNOWN_PROFILES` and validates every
  input against it.
* **Idempotent across all profiles.** Planning the same profile twice
  yields the same :class:`InstallPlan`; pre-flight is safe to re-run.
* **Profile switch on existing install is rejected.** If the backend
  detects an existing installed profile that differs from the
  requested one, :class:`ProfileSwitchRejectedError` is raised and
  the caller is told to uninstall + reinstall (per §21.3).
* **``--profile mini`` absorbs AEE-MINI hardening.** The mini plan
  includes idempotent pre-flight, ``aee`` system user provisioning,
  ``0600`` env file, and a smoke test step (modeled as plan steps,
  not executed in this slice).
* **Default is dry-run.** :class:`InstallerBackend` is constructed
  with ``dry_run=True`` by default. ``execute()`` only performs
  planning + read-only pre-flight in this mode. The actual side
  effects (system user creation, env file writes, supervisord conf
  install, smoke test invocation) require ``dry_run=False`` AND are
  **not yet authorized in this slice** — ``execute(dry_run=False)``
  raises :class:`ExecuteNotAuthorizedError` signaling that the
  shell-level install path is a separately authorizable follow-up
  (the §21.3 ``install.sh`` shell wrapper).

Invariants (Epic 9.3 contract):

1. **No ``subprocess`` import.** The backend performs no process
   spawns. The actual install steps are described in the plan as
   data; execution is delegated to a future shell layer.
2. **No filesystem writes.** Pre-flight reads existing state via
   :func:`InstallerBackend.detect_existing_profile` (which looks for
   a ``.aee-profile`` marker file at the repo root); it never
   writes. ``execute(dry_run=True)`` is side-effect-free.
3. **No ``os.system`` / ``os.popen``.** Same rationale as #1.
4. **Canonical source of truth.** Profile validation goes through
   :func:`aee.profiles.descriptor.parse_profile`; the backend does
   not maintain its own profile list.
5. **AEE-MINI installer compatibility.** This backend does **not**
   migrate or remove the existing AEE-MINI
   ``deploy/scripts/install.sh``. Per §21.10 the AEE-MINI installer
   continues to work during the deprecation window; the §21.3
   backend is the *new* canonical path, not a replacement that
   breaks the old one.

Run: ``PYTHONPATH=. python3 -m unittest aee.tests.test_aee93_installer_backend -v``
"""
from __future__ import annotations

from aee.installer.backend import (
    EXIT_OK,
    EXIT_PRE_FLIGHT_FAILED,
    EXIT_PROFILE_SWITCH_REJECTED,
    EXIT_EXECUTE_NOT_AUTHORIZED,
    EXIT_PROFILE_INVALID,
    InstallPlan,
    InstallPlanStep,
    PreFlightResult,
    InstallResult,
    InstallerBackend,
    InstallerError,
    ProfileSwitchRejectedError,
    PreFlightFailedError,
    ExecuteNotAuthorizedError,
    # Phase 7 / W9 — release channel + ref pinning + drift detection (§9)
    KNOWN_CHANNELS,
    DEFAULT_CHANNEL,
    UnknownChannelError,
    validate_channel,
    ReleasePin,
    DriftReport,
    # Phase 4A — bootstrap v1 exit-code exception hierarchy (§10.4)
    StageFailedRetryableError,
    StageFailedPermanentError,
    DriftDetectedError,
    NetworkError,
    SecretMissingError,
    DependencyFloorNotMetError,
    MINI_HARDENING_STEPS,
    UNIVERSAL_STEPS,
    plan_install,
)
# Bootstrap v1 — W1 shared bootstrap core skeleton (§4 + §5 + §10.4).
# Lifecycle / stage vocabulary / marker store / detection framework hooks.
# Imported here so future CLI layers (W3/W4/W5) consume a single canonical
# ``aee.installer`` surface.
from aee.installer.lifecycle import (
    EXIT_DEPENDENCY_FLOOR_NOT_MET,
    EXIT_DRIFT_DETECTED,
    EXIT_NETWORK_ERROR,
    EXIT_SECRET_MISSING,
    EXIT_STAGE_FAILED_PERMANENT,
    EXIT_STAGE_FAILED_RETRYABLE,
    MAX_RETRY,
    PYTHON_STAGES,
    RETRY_BACKOFF_SECONDS,
    SHELL_STAGES,
    BootstrapLifecycle,
    BootstrapState,
    InMemoryMarkerStore,
    MarkerStore,
    StageMarker,
    StageName,
    StageState,
    default_profile_for,
    detect_platform,
)

__all__ = [
    "EXIT_OK",
    "EXIT_PRE_FLIGHT_FAILED",
    "EXIT_PROFILE_SWITCH_REJECTED",
    "EXIT_EXECUTE_NOT_AUTHORIZED",
    "EXIT_PROFILE_INVALID",
    "InstallPlan",
    "InstallPlanStep",
    "PreFlightResult",
    "InstallResult",
    "InstallerBackend",
    "InstallerError",
    "ProfileSwitchRejectedError",
    "PreFlightFailedError",
    "ExecuteNotAuthorizedError",
    # Phase 4A — bootstrap v1 exit-code exception hierarchy (§10.4)
    "StageFailedRetryableError",
    "StageFailedPermanentError",
    "DriftDetectedError",
    "NetworkError",
    "SecretMissingError",
    "DependencyFloorNotMetError",
    "MINI_HARDENING_STEPS",
    "UNIVERSAL_STEPS",
    "plan_install",
    # Phase 7 / W9 — release channel + ref pinning + drift detection
    "KNOWN_CHANNELS",
    "DEFAULT_CHANNEL",
    "UnknownChannelError",
    "validate_channel",
    "ReleasePin",
    "DriftReport",
    # Bootstrap v1 — W1 shared bootstrap core skeleton
    "EXIT_STAGE_FAILED_RETRYABLE",
    "EXIT_STAGE_FAILED_PERMANENT",
    "EXIT_DRIFT_DETECTED",
    "EXIT_NETWORK_ERROR",
    "EXIT_SECRET_MISSING",
    "EXIT_DEPENDENCY_FLOOR_NOT_MET",
    "MAX_RETRY",
    "RETRY_BACKOFF_SECONDS",
    "SHELL_STAGES",
    "PYTHON_STAGES",
    "StageName",
    "StageState",
    "StageMarker",
    "BootstrapState",
    "MarkerStore",
    "InMemoryMarkerStore",
    "BootstrapLifecycle",
    "detect_platform",
    "default_profile_for",
]