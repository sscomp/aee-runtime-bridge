"""AEE Phase 4C — ``aee update`` CLI entrypoint + run_update flow (§21.3 + W5).

This module is the bridge between the ``aee update`` argparse surface
defined in :mod:`aee.cli` and the §21.3 installer backend in
:mod:`aee.installer.backend`. Phase 4C (this module) introduces the
*update CLI entrypoint* and the :func:`run_update` flow that the CLI
delegates to.

Design contract (per Master Plan §21.3 + Phase 4C brief):

* **Approved flags only.** The brief authorizes exactly seven flags:
  ``--channel``, ``--ref``, ``--yes``, ``--offline-bundle``,
  ``--log-format``, ``--execute``, ``--json``. No other flags are
  added.
* **Dry-run is the default.** Without ``--execute``, :func:`run_update`
  performs drift detection (projected, read-only) + pre-flight only.
  ``--execute`` requests the shell-level execution path, which is
  **still gated** by the §21.3 :class:`ExecuteNotAuthorizedError` —
  Phase 4C wires the flag plumbing, not the shell side-effects. An
  explicit ``--execute`` therefore yields a structured
  :class:`UpdateCliResult` with ``executed=False`` and
  ``execute_requested=True`` plus an ``execute_not_authorized`` note.
* **Drift detection is projected in dry-run.** The function compares
  the recorded pin (``commit_sha`` + ``requirements_lock_sha256``)
  against the on-disk HEAD (read-only ``git rev-parse`` is allowed;
  ``git fetch`` is not). The result carries a ``drift`` field
  (:class:`DriftResult`) with ``would_drift`` boolean. No network
  calls, no ``subprocess`` spawns.
* **Channel switching reuses the profile-switch path.** ``--channel``
  selects the release channel (``stable`` / ``rc`` / ``dev``). The
  backend's existing profile-switch rejection applies when an
  existing install's profile would change.
* **``--ref <ref>`` is audit-only.** Records the operator-supplied git
  ref; no git operations are performed.
* **``--yes`` is audit-only.** Records that non-interactive
  confirmation was requested; the flag does not bypass the
  ``--execute`` guard.
* **``--offline-bundle <path>`` is audit-only.** Records the path to an
  offline bundle; no filesystem reads beyond ``os.path.exists`` for
  projection.
* **``--log-format <format>`` is audit-only.** Records the requested
  log format; no log configuration is performed in this slice.
* **No ``subprocess`` import.** Same invariant as the §21.3 backend
  and Phase 4B ``cli_install``.

Exit code mapping (composed with the backend's exit codes):

* :data:`EXIT_OK` (0) — drift detection + pre-flight succeeded
  (dry-run, or ``--execute`` received but gated by §21.3).
* :data:`EXIT_PROFILE_INVALID` (3) — unknown profile.
* :data:`EXIT_PRE_FLIGHT_FAILED` (4) — pre-flight failed.
* :data:`EXIT_PROFILE_SWITCH_REJECTED` (5) — profile switch rejected.
* :data:`EXIT_EXECUTE_NOT_AUTHORIZED` (6) — ``--execute`` requested
  but the shell-level execution path raised
  :class:`ExecuteNotAuthorizedError`. Distinct from exit 0 so an
  operator can tell "I asked for execute and it was refused" apart
  from "I didn't ask for execute".
* :data:`EXIT_DRIFT_DETECTED` (9) — drift detected between the
  recorded pin and the on-disk HEAD. Returned only in dry-run when
  drift is detected AND ``--execute`` was NOT requested; when
  ``--execute`` is requested, exit 6 takes precedence (the shell
  path is the one that would re-pin, and it is gated).

Run: ``PYTHONPATH=. python3 -m unittest aee.tests.test_aee_phase4c_update_cli -v``
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from aee.installer.backend import (
    EXIT_EXECUTE_NOT_AUTHORIZED,
    EXIT_OK,
    EXIT_PRE_FLIGHT_FAILED,
    EXIT_PROFILE_INVALID,
    EXIT_PROFILE_SWITCH_REJECTED,
    ExecuteNotAuthorizedError,
    InstallPlan,
    InstallResult,
    InstallerBackend,
    PreFlightResult,
)
from aee.installer.lifecycle import EXIT_DRIFT_DETECTED
from aee.profiles.descriptor import (
    DEFAULT_PROFILE,
    KNOWN_PROFILES,
    UnknownProfileError,
    parse_profile,
)


# ---------------------------------------------------------------------------#
# Release channel vocabulary
# ---------------------------------------------------------------------------#

#: The canonical release channels (§21.3 / W9). The channel selects
#: which release stream ``aee update`` advances to. The default
#: channel is ``stable``.
KNOWN_CHANNELS: Tuple[str, ...] = ("stable", "rc", "dev")

#: The default release channel.
DEFAULT_CHANNEL: str = "stable"


# ---------------------------------------------------------------------------#
# Drift detection result
# ---------------------------------------------------------------------------#


@dataclass(frozen=True)
class DriftResult:
    """Projected drift-detection result (read-only).

    ``would_drift`` is True when the on-disk HEAD differs from the
    recorded pin. In dry-run, this is a *projected* check — no
    ``git fetch`` is performed; the comparison is between the
    recorded pin (read from the marker) and the on-disk HEAD
    (read-only ``git rev-parse`` if available, else ``None``).

    Fields:

    * ``would_drift`` — True when the recorded pin differs from
      the on-disk HEAD.
    * ``recorded_commit_sha`` — the commit SHA recorded in the
      pin marker, or ``None`` when no marker exists.
    * ``on_disk_commit_sha`` — the on-disk HEAD SHA (read-only),
      or ``None`` when git is unavailable or the repo root is not
      a git repo.
    * ``recorded_lock_sha256`` — the requirements.lock sha256
      recorded in the pin marker, or ``None``.
    * ``on_disk_lock_sha256`` — the on-disk requirements.lock
      sha256 (read via ``hashlib``, no subprocess), or ``None``
      when the lock file is absent.
    * ``reason`` — human-readable detail when ``would_drift`` is
      True (empty string otherwise).
    """

    would_drift: bool
    recorded_commit_sha: Optional[str]
    on_disk_commit_sha: Optional[str]
    recorded_lock_sha256: Optional[str]
    on_disk_lock_sha256: Optional[str]
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "would_drift": self.would_drift,
            "recorded_commit_sha": self.recorded_commit_sha,
            "on_disk_commit_sha": self.on_disk_commit_sha,
            "recorded_lock_sha256": self.recorded_lock_sha256,
            "on_disk_lock_sha256": self.on_disk_lock_sha256,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------#
# Options dataclass — the canonical input shape for run_update
# ---------------------------------------------------------------------------#


@dataclass(frozen=True)
class UpdateCliOptions:
    """Operator-facing options for ``aee update`` (Phase 4C).

    Constructed by the CLI layer from the argparse namespace. All
    approved flags are represented here so the dispatch contract is
    explicit and testable independent of argparse.

    Fields:

    * ``profile`` — the resolved canonical profile (one of
      :data:`KNOWN_PROFILES`). Default :data:`DEFAULT_PROFILE`.
    * ``channel`` — the release channel (one of
      :data:`KNOWN_CHANNELS`). Default :data:`DEFAULT_CHANNEL`.
    * ``ref`` — the git ref supplied via ``--ref <ref>``, or ``None``.
    * ``yes`` — whether ``--yes`` was passed. Default ``False``.
    * ``offline_bundle`` — the path supplied via
      ``--offline-bundle <path>``, or ``None``.
    * ``log_format`` — the log format supplied via
      ``--log-format <format>``, or ``None``.
    * ``execute`` — whether ``--execute`` was passed. Default ``False``.
    * ``repo_root`` — the repo root path used to construct the
      :class:`InstallerBackend`. Defaults to the current working
      directory when ``None``.
    """

    profile: str = DEFAULT_PROFILE
    channel: str = DEFAULT_CHANNEL
    ref: Optional[str] = None
    yes: bool = False
    offline_bundle: Optional[str] = None
    log_format: Optional[str] = None
    execute: bool = False
    repo_root: Optional[Path] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile": self.profile,
            "channel": self.channel,
            "ref": self.ref,
            "yes": self.yes,
            "offline_bundle": self.offline_bundle,
            "log_format": self.log_format,
            "execute": self.execute,
            "repo_root": str(self.repo_root) if self.repo_root else None,
        }


# ---------------------------------------------------------------------------#
# Result dataclass — the canonical output shape for run_update
# ---------------------------------------------------------------------------#


@dataclass(frozen=True)
class UpdateCliResult:
    """Result of :func:`run_update` (Phase 4C).

    Folds the §21.3 backend's :class:`InstallResult` together with
    the Phase 4C flag metadata + drift result so an operator / test /
    future shell layer gets a single structured object.

    Fields:

    * ``exit_code`` — the process exit code (0/3/4/5/6/9).
    * ``profile`` — the canonical profile that was resolved.
    * ``channel`` — the release channel that was resolved.
    * ``ref`` — the operator-supplied git ref (``--ref``), or ``None``.
    * ``yes`` — whether ``--yes`` was passed.
    * ``offline_bundle`` — the offline-bundle path, or ``None``.
    * ``log_format`` — the log-format string, or ``None``.
    * ``execute_requested`` — whether ``--execute`` was passed.
    * ``plan`` — the :class:`InstallPlan` (``None`` only on profile
      validation failure).
    * ``preflight`` — the :class:`PreFlightResult` (``None`` only on
      profile validation failure).
    * ``drift`` — the :class:`DriftResult` (always present, even on
      profile failure — the drift check is independent of the
      backend's profile validation; on profile failure the drift
      fields are ``None``/``False``).
    * ``executed`` — whether side effects were performed (always
      ``False`` in this slice; the §21.3 shell path is separately
      authorizable).
    * ``error`` — a non-empty string when an error was encountered.
    * ``notes`` — human-readable notes.
    """

    exit_code: int
    profile: str
    channel: str
    ref: Optional[str]
    yes: bool
    offline_bundle: Optional[str]
    log_format: Optional[str]
    execute_requested: bool
    plan: Optional[InstallPlan]
    preflight: Optional[PreFlightResult]
    drift: DriftResult
    executed: bool = False
    error: str = ""
    notes: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "profile": self.profile,
            "channel": self.channel,
            "ref": self.ref,
            "yes": self.yes,
            "offline_bundle": self.offline_bundle,
            "log_format": self.log_format,
            "execute_requested": self.execute_requested,
            "plan": self.plan.to_dict() if self.plan is not None else None,
            "preflight": (
                self.preflight.to_dict()
                if self.preflight is not None
                else None
            ),
            "drift": self.drift.to_dict(),
            "executed": self.executed,
            "error": self.error,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------#
# Drift detection helpers (read-only, no subprocess)
# ---------------------------------------------------------------------------#


_PIN_MARKER_FILENAME = ".aee-pin"
_PIN_COMMIT_PREFIX = "commit_sha="
_PIN_LOCK_PREFIX = "requirements_lock_sha256="


def _read_recorded_pin(repo_root: Path) -> Tuple[Optional[str], Optional[str]]:
    """Read the recorded pin marker at ``repo_root`` (read-only).

    Returns ``(commit_sha, lock_sha256)``. Both are ``None`` when no
    marker file exists. The marker is a simple key=value text file;
    unknown keys are ignored.
    """
    marker = repo_root / _PIN_MARKER_FILENAME
    if not marker.exists():
        return None, None
    try:
        content = marker.read_text(encoding="utf-8")
    except OSError:
        return None, None
    commit_sha: Optional[str] = None
    lock_sha: Optional[str] = None
    for line in content.splitlines():
        line = line.strip()
        if line.startswith(_PIN_COMMIT_PREFIX):
            commit_sha = line[len(_PIN_COMMIT_PREFIX):].strip() or None
        elif line.startswith(_PIN_LOCK_PREFIX):
            lock_sha = line[len(_PIN_LOCK_PREFIX):].strip() or None
    return commit_sha, lock_sha


def _read_on_disk_head(repo_root: Path) -> Optional[str]:
    """Read the on-disk HEAD commit SHA (read-only).

    Uses the ``.git/HEAD`` + ``.git/<ref>`` files directly — no
    ``subprocess`` / ``git`` invocation. Returns ``None`` when the
    repo root is not a git repo or the HEAD cannot be resolved.
    """
    git_dir = repo_root / ".git"
    if not git_dir.exists():
        return None
    head_file = git_dir / "HEAD"
    if not head_file.exists():
        return None
    try:
        head_content = head_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    # ``ref: refs/heads/main`` → resolve the ref file.
    if head_content.startswith("ref: "):
        ref_path = head_content[5:].strip()
        ref_file = git_dir / ref_path
        if ref_file.exists():
            try:
                sha = ref_file.read_text(encoding="utf-8").strip()
                if sha:
                    return sha
            except OSError:
                return None
        # Packed-refs fallback.
        packed = git_dir / "packed-refs"
        if packed.exists():
            try:
                for line in packed.read_text(encoding="utf-8").splitlines():
                    if line.endswith(" " + ref_path):
                        sha = line.split(" ", 1)[0]
                        if sha:
                            return sha
            except OSError:
                pass
        return None
    # Detached HEAD: the HEAD file contains the SHA directly.
    if head_content:
        return head_content
    return None


def _read_on_disk_lock_sha(repo_root: Path) -> Optional[str]:
    """Read the on-disk ``requirements.lock`` sha256 (read-only).

    Returns ``None`` when the lock file is absent. Uses ``hashlib``
    — no ``subprocess``.
    """
    import hashlib
    lock_file = repo_root / "requirements.lock"
    if not lock_file.exists():
        return None
    try:
        h = hashlib.sha256()
        with open(lock_file, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def detect_drift(repo_root: Path) -> DriftResult:
    """Project drift between the recorded pin and the on-disk HEAD.

    Read-only: no ``git fetch``, no ``subprocess``, no network. The
    comparison is between the recorded pin (``.aee-pin`` marker) and
    the on-disk HEAD (``.git/HEAD`` + ref resolution) +
    ``requirements.lock`` sha256.
    """
    recorded_commit, recorded_lock = _read_recorded_pin(repo_root)
    on_disk_commit = _read_on_disk_head(repo_root)
    on_disk_lock = _read_on_disk_lock_sha(repo_root)

    # No recorded pin → nothing to drift from (fresh install).
    if recorded_commit is None and recorded_lock is None:
        return DriftResult(
            would_drift=False,
            recorded_commit_sha=None,
            on_disk_commit_sha=on_disk_commit,
            recorded_lock_sha256=None,
            on_disk_lock_sha256=on_disk_lock,
            reason="",
        )

    reasons: list = []
    if recorded_commit is not None and on_disk_commit is not None:
        if recorded_commit != on_disk_commit:
            reasons.append(
                "commit_sha mismatch (recorded={r}, on_disk={o})".format(
                    r=recorded_commit[:12], o=on_disk_commit[:12],
                )
            )
    elif recorded_commit is not None and on_disk_commit is None:
        reasons.append(
            "commit_sha recorded but on-disk HEAD unavailable "
            "(recorded={r})".format(r=recorded_commit[:12])
        )
    if recorded_lock is not None and on_disk_lock is not None:
        if recorded_lock != on_disk_lock:
            reasons.append(
                "requirements_lock_sha256 mismatch "
                "(recorded={r}, on_disk={o})".format(
                    r=recorded_lock[:12], o=on_disk_lock[:12],
                )
            )
    elif recorded_lock is not None and on_disk_lock is None:
        reasons.append(
            "requirements_lock_sha256 recorded but lock file absent "
            "(recorded={r})".format(r=recorded_lock[:12])
        )

    would_drift = len(reasons) > 0
    return DriftResult(
        would_drift=would_drift,
        recorded_commit_sha=recorded_commit,
        on_disk_commit_sha=on_disk_commit,
        recorded_lock_sha256=recorded_lock,
        on_disk_lock_sha256=on_disk_lock,
        reason="; ".join(reasons),
    )


# ---------------------------------------------------------------------------#
# Channel validation
# ---------------------------------------------------------------------------#


def validate_channel(channel: str) -> str:
    """Validate ``channel`` against :data:`KNOWN_CHANNELS`.

    Returns the canonical channel name. Raises ``ValueError`` on an
    unknown channel (the CLI layer's argparse ``choices`` already
    rejects this, but a programmatic caller may bypass argparse).
    """
    if channel not in KNOWN_CHANNELS:
        raise ValueError(
            "unknown channel: {c!r} (known: {k})".format(
                c=channel, k=", ".join(KNOWN_CHANNELS),
            )
        )
    return channel


# ---------------------------------------------------------------------------#
# run_update — the canonical entrypoint
# ---------------------------------------------------------------------------#


def run_update(options: UpdateCliOptions) -> UpdateCliResult:
    """Run the ``aee update`` flow (Phase 4C).

    Composes the §21.3 backend's ``plan`` + ``execute(dry_run=True)``
    with the Phase 4C flag metadata + projected drift detection.
    Returns an :class:`UpdateCliResult`; never raises for
    application-level errors (profile invalid, pre-flight fail,
    execute refused, drift detected) — those are encoded in the
    result.

    Side effect safety: this function performs **no** subprocess
    spawns, **no** network calls, and **no** filesystem writes. It
    only constructs an :class:`InstallerBackend` (which reads the
    ``.aee-profile`` marker if present), runs read-only drift
    detection (``.git/HEAD`` + ``requirements.lock`` sha256), and
    calls ``plan`` + ``execute(dry_run=True)``. ``--execute`` is
    recorded but gated by :class:`ExecuteNotAuthorizedError`.
    """
    # 1. Validate the profile against the canonical source.
    try:
        canonical = parse_profile(options.profile)
    except UnknownProfileError as exc:
        return UpdateCliResult(
            exit_code=EXIT_PROFILE_INVALID,
            profile=options.profile,
            channel=options.channel,
            ref=options.ref,
            yes=options.yes,
            offline_bundle=options.offline_bundle,
            log_format=options.log_format,
            execute_requested=options.execute,
            plan=None,
            preflight=None,
            drift=DriftResult(
                would_drift=False,
                recorded_commit_sha=None,
                on_disk_commit_sha=None,
                recorded_lock_sha256=None,
                on_disk_lock_sha256=None,
                reason="",
            ),
            executed=False,
            error="unknown profile: {e}".format(e=exc),
            notes=(
                "Phase 4C: profile validation failed before backend "
                "construction; no I/O performed.",
            ),
        )

    # 2. Validate the channel.
    try:
        channel = validate_channel(options.channel)
    except ValueError as exc:
        return UpdateCliResult(
            exit_code=EXIT_PROFILE_INVALID,
            profile=canonical,
            channel=options.channel,
            ref=options.ref,
            yes=options.yes,
            offline_bundle=options.offline_bundle,
            log_format=options.log_format,
            execute_requested=options.execute,
            plan=None,
            preflight=None,
            drift=DriftResult(
                would_drift=False,
                recorded_commit_sha=None,
                on_disk_commit_sha=None,
                recorded_lock_sha256=None,
                on_disk_lock_sha256=None,
                reason="",
            ),
            executed=False,
            error="unknown channel: {e}".format(e=exc),
            notes=(
                "Phase 4C: channel validation failed before backend "
                "construction; no I/O performed.",
            ),
        )

    # 3. Run drift detection (read-only, projected).
    repo_root = options.repo_root if options.repo_root is not None else Path.cwd()
    drift = detect_drift(Path(repo_root))

    # 4. Construct the backend with dry_run=True (the §21.3 invariant).
    backend = InstallerBackend(
        repo_root=options.repo_root,
        dry_run=True,
    )

    plan: Optional[InstallPlan]
    preflight: Optional[PreFlightResult]
    notes: list = []

    # 5. Plan + pre-flight via the backend.
    try:
        result = backend.execute(canonical, dry_run=True)
    except ExecuteNotAuthorizedError:
        return UpdateCliResult(
            exit_code=EXIT_EXECUTE_NOT_AUTHORIZED,
            profile=canonical,
            channel=channel,
            ref=options.ref,
            yes=options.yes,
            offline_bundle=options.offline_bundle,
            log_format=options.log_format,
            execute_requested=True,
            plan=None,
            preflight=None,
            drift=drift,
            executed=False,
            error="execute not authorized (defence-in-depth branch)",
            notes=(
                "Phase 4C: backend.execute raised "
                "ExecuteNotAuthorizedError even though dry_run=True "
                "was passed; this is a defence-in-depth branch.",
            ),
        )
    except UnknownProfileError as exc:
        return UpdateCliResult(
            exit_code=EXIT_PROFILE_INVALID,
            profile=canonical,
            channel=channel,
            ref=options.ref,
            yes=options.yes,
            offline_bundle=options.offline_bundle,
            log_format=options.log_format,
            execute_requested=options.execute,
            plan=None,
            preflight=None,
            drift=drift,
            executed=False,
            error="unknown profile: {e}".format(e=exc),
        )

    plan = result.plan
    preflight = result.preflight

    # 6. Map the pre-flight result to an exit code.
    if not preflight.ok:
        if (
            preflight.existing_profile is not None
            and preflight.existing_profile != canonical
        ):
            exit_code = EXIT_PROFILE_SWITCH_REJECTED
            error_msg = preflight.reason or "profile switch rejected"
        else:
            exit_code = EXIT_PRE_FLIGHT_FAILED
            error_msg = preflight.reason or "pre-flight failed"
        return UpdateCliResult(
            exit_code=exit_code,
            profile=canonical,
            channel=channel,
            ref=options.ref,
            yes=options.yes,
            offline_bundle=options.offline_bundle,
            log_format=options.log_format,
            execute_requested=options.execute,
            plan=plan,
            preflight=preflight,
            drift=drift,
            executed=False,
            error=error_msg,
            notes=tuple(notes),
        )

    # 7. Pre-flight OK. If ``--execute`` was requested, record it and
    #    surface the §21.3 execute-refused note. The exit code is
    #    ``EXIT_EXECUTE_NOT_AUTHORIZED`` (6) so an operator can
    #    distinguish "I asked for execute and it was refused" from
    #    "I didn't ask for execute" (which is exit 0 or 9).
    if options.execute:
        execute_note = (
            "Phase 4C: --execute received but the §21.3 shell-level "
            "execution path is not authorized in this slice; drift "
            "detection (projected) + read-only pre-flight only. Use "
            "the future update.sh shell wrapper to actually perform "
            "the update."
        )
        if options.ref is not None:
            execute_note += (
                " --ref {ref} recorded; no git operations performed.".format(
                    ref=options.ref
                )
            )
        if options.yes:
            execute_note += (
                " --yes recorded; non-interactive confirmation "
                "requested (does not bypass the --execute guard)."
            )
        if options.offline_bundle is not None:
            execute_note += (
                " --offline-bundle {p} recorded; no filesystem reads "
                "beyond existence projection.".format(
                    p=options.offline_bundle
                )
            )
        if options.log_format is not None:
            execute_note += (
                " --log-format {f} recorded; no log configuration "
                "performed in this slice.".format(f=options.log_format)
            )
        return UpdateCliResult(
            exit_code=EXIT_EXECUTE_NOT_AUTHORIZED,
            profile=canonical,
            channel=channel,
            ref=options.ref,
            yes=options.yes,
            offline_bundle=options.offline_bundle,
            log_format=options.log_format,
            execute_requested=True,
            plan=plan,
            preflight=preflight,
            drift=drift,
            executed=False,
            error="",
            notes=(execute_note,),
        )

    # 8. Dry-run path. If drift is detected, surface it with exit 9
    #    (the §10.4 proposed code, introduced in Phase 4A). When
    #    drift is not detected, exit 0. Audit-only notes for the
    #    remaining flags so an operator can see they were received.
    audit_notes: list = []
    if options.ref is not None:
        audit_notes.append(
            "Phase 4C: --ref {ref} recorded; no git operations "
            "performed.".format(ref=options.ref)
        )
    if options.yes:
        audit_notes.append(
            "Phase 4C: --yes recorded; non-interactive confirmation "
            "requested (does not bypass the --execute guard)."
        )
    if options.offline_bundle is not None:
        audit_notes.append(
            "Phase 4C: --offline-bundle {p} recorded; no filesystem "
            "reads beyond existence projection.".format(
                p=options.offline_bundle
            )
        )
    if options.log_format is not None:
        audit_notes.append(
            "Phase 4C: --log-format {f} recorded; no log "
            "configuration performed in this slice.".format(
                f=options.log_format
            )
        )
    if drift.would_drift:
        audit_notes.append(
            "Phase 4C: drift detected (projected); {r}.".format(
                r=drift.reason or "pin mismatch"
            )
        )
    else:
        audit_notes.append(
            "Phase 4C: no drift detected (projected); on-disk HEAD "
            "matches recorded pin (or no pin exists)."
        )
    audit_notes.append(
        "Phase 4C: channel={c} (default={d}); advance on the current "
        "channel in dry-run.".format(c=channel, d=DEFAULT_CHANNEL)
    )

    exit_code = EXIT_DRIFT_DETECTED if drift.would_drift else EXIT_OK
    return UpdateCliResult(
        exit_code=exit_code,
        profile=canonical,
        channel=channel,
        ref=options.ref,
        yes=options.yes,
        offline_bundle=options.offline_bundle,
        log_format=options.log_format,
        execute_requested=False,
        plan=plan,
        preflight=preflight,
        drift=drift,
        executed=False,
        error="" if not drift.would_drift else drift.reason,
        notes=tuple(audit_notes),
    )


__all__ = [
    "KNOWN_CHANNELS",
    "DEFAULT_CHANNEL",
    "DriftResult",
    "UpdateCliOptions",
    "UpdateCliResult",
    "detect_drift",
    "validate_channel",
    "run_update",
]