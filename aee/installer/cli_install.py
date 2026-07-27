"""AEE Phase 4B — ``aee install`` CLI entrypoint + run_install flow (§21.3).

This module is the bridge between the ``aee install`` argparse surface
defined in :mod:`aee.cli` and the §21.3 installer backend in
:mod:`aee.installer.backend`. Phase 9.2 (Epic 9.2) wired the argparse
skeleton with a dry-run-only dispatch contract; Phase 4B (this module)
introduces the *installer CLI entrypoint* and the :func:`run_install`
flow that the CLI delegates to.

Design contract (per Master Plan §21.3 + Phase 4B brief):

* **Approved flags only.** The brief authorizes exactly four flags:
  ``--execute``, ``--resume``, ``--from <ref>``, ``--rollback-to <ref>``.
  No other flags are added. The ``update`` CLI is explicitly OUT of
  scope for this slice.
* **Dry-run is the default.** Without ``--execute``, :func:`run_install`
  performs plan + read-only pre-flight only (the same behaviour as the
  Phase 9.2 ``_install_dispatch``). ``--execute`` requests the
  shell-level execution path, which is **still gated** by the
  §21.3 :class:`ExecuteNotAuthorizedError` — Phase 4B wires the flag
  plumbing, not the shell side-effects. An explicit ``--execute``
  therefore yields a structured ``InstallCliResult`` with
  ``executed=False`` and ``execute_requested=True`` plus an
  ``execute_not_authorized`` note, so an operator / test can observe
  that the flag was *received* without any side effect occurring.
* **``--resume`` is audit-only in this slice.** The flag is parsed,
  recorded in :attr:`InstallCliOptions.resume`, and surfaced in the
  result. It does NOT replay stage markers (the shell trampolines W6/W7
  are the separately authorizable follow-up that will consume
  :class:`BootstrapState`). Recording the flag now lets CI / tests
  assert that the CLI surface accepts it.
* **``--from <ref>`` / ``--rollback-to <ref>`` are audit-only.** Both
  record the operator-supplied git ref in the result so a future shell
  layer can act on them. They are validated as non-empty strings only;
  no git operations are performed (no ``subprocess``, no ``os.system``,
  no filesystem writes outside the result object).
* **No ``subprocess`` import.** Same invariant as the §21.3 backend.
* **Composes with the existing backend.** :func:`run_install` calls
  :meth:`InstallerBackend.plan` and :meth:`InstallerBackend.execute`
  (with ``dry_run=True``) and folds the result into an
  :class:`InstallCliResult`. The existing ``_install_dispatch`` in
  :mod:`aee.cli` is preserved for backward compat — Phase 4B adds a
  *new* dispatch path, it does not delete the old one.

Exit code mapping (composed with the backend's exit codes):

* :data:`EXIT_OK` (0) — plan + pre-flight succeeded (dry-run, or
  ``--execute`` received but gated by §21.3).
* :data:`EXIT_PROFILE_INVALID` (3) — unknown profile.
* :data:`EXIT_PRE_FLIGHT_FAILED` (4) — pre-flight failed.
* :data:`EXIT_PROFILE_SWITCH_REJECTED` (5) — profile switch rejected.
* :data:`EXIT_EXECUTE_NOT_AUTHORIZED` (6) — ``--execute`` requested
  but the shell-level execution path raised
  :class:`ExecuteNotAuthorizedError`. Distinct from exit 0 so an
  operator can tell "I asked for execute and it was refused" apart
  from "I didn't ask for execute".

Run: ``PYTHONPATH=. python3 -m unittest aee.tests.test_aee_phase4b_install_cli -v``
"""
from __future__ import annotations

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
from aee.profiles.descriptor import (
    DEFAULT_PROFILE,
    KNOWN_PROFILES,
    UnknownProfileError,
    parse_profile,
)


# ---------------------------------------------------------------------------
# Options dataclass — the canonical input shape for run_install
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstallCliOptions:
    """Operator-facing options for ``aee install`` (Phase 4B).

    Constructed by the CLI layer from the argparse namespace. All
    approved flags are represented here so the dispatch contract is
    explicit and testable independent of argparse.

    Fields:

    * ``profile`` — the resolved canonical profile (one of
      :data:`KNOWN_PROFILES`). Default :data:`DEFAULT_PROFILE`.
    * ``execute`` — whether ``--execute`` was passed. Default ``False``.
    * ``resume`` — whether ``--resume`` was passed. Default ``False``.
    * ``from_ref`` — the git ref supplied via ``--from <ref>``, or
      ``None`` when the flag was omitted.
    * ``rollback_to`` — the git ref supplied via ``--rollback-to <ref>``,
      or ``None`` when the flag was omitted.
    * ``repo_root`` — the repo root path used to construct the
      :class:`InstallerBackend`. Defaults to the current working
      directory when ``None`` (the backend's own default).
    """

    profile: str = DEFAULT_PROFILE
    execute: bool = False
    resume: bool = False
    from_ref: Optional[str] = None
    rollback_to: Optional[str] = None
    repo_root: Optional[Path] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile": self.profile,
            "execute": self.execute,
            "resume": self.resume,
            "from_ref": self.from_ref,
            "rollback_to": self.rollback_to,
            "repo_root": str(self.repo_root) if self.repo_root else None,
        }


# ---------------------------------------------------------------------------
# Result dataclass — the canonical output shape for run_install
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstallCliResult:
    """Result of :func:`run_install` (Phase 4B).

    Folds the §21.3 backend's :class:`InstallResult` together with the
    Phase 4B flag metadata so an operator / test / future shell layer
    gets a single structured object.

    Fields:

    * ``exit_code`` — the process exit code (0/3/4/5/6).
    * ``profile`` — the canonical profile that was resolved.
    * ``execute_requested`` — whether ``--execute`` was passed.
    * ``resume`` — whether ``--resume`` was passed.
    * ``from_ref`` / ``rollback_to`` — the operator-supplied refs.
    * ``plan`` — the :class:`InstallPlan` (``None`` only on profile
      validation failure).
    * ``preflight`` — the :class:`PreFlightResult` (``None`` only on
      profile validation failure).
    * ``executed`` — whether side effects were performed (always
      ``False`` in this slice; the §21.3 shell path is separately
      authorizable).
    * ``error`` — a non-empty string when an error was encountered
      (profile invalid, pre-flight failed, execute refused).
    * ``notes`` — human-readable notes (e.g. the execute-refused
      reason).
    """

    exit_code: int
    profile: str
    execute_requested: bool
    resume: bool
    from_ref: Optional[str]
    rollback_to: Optional[str]
    plan: Optional[InstallPlan]
    preflight: Optional[PreFlightResult]
    executed: bool = False
    error: str = ""
    notes: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "profile": self.profile,
            "execute_requested": self.execute_requested,
            "resume": self.resume,
            "from_ref": self.from_ref,
            "rollback_to": self.rollback_to,
            "plan": self.plan.to_dict() if self.plan is not None else None,
            "preflight": (
                self.preflight.to_dict()
                if self.preflight is not None
                else None
            ),
            "executed": self.executed,
            "error": self.error,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# run_install — the canonical entrypoint
# ---------------------------------------------------------------------------


def run_install(options: InstallCliOptions) -> InstallCliResult:
    """Run the ``aee install`` flow (Phase 4B).

    Composes the §21.3 backend's ``plan`` + ``execute(dry_run=True)``
    with the Phase 4B flag metadata. Returns an
    :class:`InstallCliResult`; never raises for application-level
    errors (profile invalid, pre-flight fail, execute refused) —
    those are encoded in the result.

    Side effect safety: this function performs **no** subprocess
    spawns, **no** filesystem writes, and **no** network calls. It
    only constructs an :class:`InstallerBackend` (which reads the
    ``.aee-profile`` marker if present) and calls ``plan`` +
    ``execute(dry_run=True)``. ``--execute`` is recorded but gated
    by :class:`ExecuteNotAuthorizedError`.
    """
    # 1. Validate the profile against the canonical source. We do this
    #    before constructing the backend so a bogus profile yields a
    #    clean result with ``exit_code=EXIT_PROFILE_INVALID`` and no
    #    backend I/O. Defence in depth: the CLI layer's argparse
    #    ``choices`` already rejects unknown profiles, but a
    #    programmatic caller (e.g. a test) may bypass argparse.
    try:
        canonical = parse_profile(options.profile)
    except UnknownProfileError as exc:
        return InstallCliResult(
            exit_code=EXIT_PROFILE_INVALID,
            profile=options.profile,
            execute_requested=options.execute,
            resume=options.resume,
            from_ref=options.from_ref,
            rollback_to=options.rollback_to,
            plan=None,
            preflight=None,
            executed=False,
            error="unknown profile: {e}".format(e=exc),
            notes=(
                "Phase 4B: profile validation failed before backend "
                "construction; no I/O performed.",
            ),
        )

    # 2. Construct the backend with dry_run=True (the §21.3 invariant).
    #    Even when ``--execute`` is requested, the backend is
    #    constructed with ``dry_run=True`` — the execute-refused path
    #    below encodes the §21.3 guard explicitly.
    backend = InstallerBackend(
        repo_root=options.repo_root,
        dry_run=True,
    )

    plan: Optional[InstallPlan]
    preflight: Optional[PreFlightResult]
    notes: Tuple[str, ...] = ()

    # 3. Plan + pre-flight via the backend. The backend's ``execute``
    #    method performs plan + pre-flight and returns an
    #    ``InstallResult`` whose ``preflight`` field carries the
    #    PreFlightResult. When pre-flight fails, the result still
    #    carries the plan + preflight so the caller can inspect the
    #    failure reason.
    try:
        result = backend.execute(canonical, dry_run=True)
    except ExecuteNotAuthorizedError:
        # Should not happen here because we pass dry_run=True, but
        # defend in depth — if a future code path flips the default,
        # this branch keeps the function side-effect-free.
        return InstallCliResult(
            exit_code=EXIT_EXECUTE_NOT_AUTHORIZED,
            profile=canonical,
            execute_requested=options.execute,
            resume=options.resume,
            from_ref=options.from_ref,
            rollback_to=options.rollback_to,
            plan=None,
            preflight=None,
            executed=False,
            error="execute not authorized (defence-in-depth branch)",
            notes=(
                "Phase 4B: backend.execute raised "
                "ExecuteNotAuthorizedError even though dry_run=True "
                "was passed; this is a defence-in-depth branch.",
            ),
        )
    except UnknownProfileError as exc:
        return InstallCliResult(
            exit_code=EXIT_PROFILE_INVALID,
            profile=canonical,
            execute_requested=options.execute,
            resume=options.resume,
            from_ref=options.from_ref,
            rollback_to=options.rollback_to,
            plan=None,
            preflight=None,
            executed=False,
            error="unknown profile: {e}".format(e=exc),
        )

    plan = result.plan
    preflight = result.preflight

    # 4. Map the pre-flight result to an exit code.
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
        return InstallCliResult(
            exit_code=exit_code,
            profile=canonical,
            execute_requested=options.execute,
            resume=options.resume,
            from_ref=options.from_ref,
            rollback_to=options.rollback_to,
            plan=plan,
            preflight=preflight,
            executed=False,
            error=error_msg,
            notes=notes,
        )

    # 5. Pre-flight OK. If ``--execute`` was requested, record it and
    #    surface the §21.3 execute-refused note. The exit code is
    #    ``EXIT_EXECUTE_NOT_AUTHORIZED`` (6) so an operator can
    #    distinguish "I asked for execute and it was refused" from
    #    "I didn't ask for execute" (which is exit 0). When
    #    ``--execute`` was NOT requested, exit 0.
    if options.execute:
        execute_note = (
            "Phase 4B: --execute received but the §21.3 shell-level "
            "execution path is not authorized in this slice; plan + "
            "read-only pre-flight only. Use the future install.sh "
            "shell wrapper (W6/W7) to actually perform the install."
        )
        if options.resume:
            execute_note += (
                " --resume also received; recorded for the future "
                "shell layer (no stage-marker replay in this slice)."
            )
        if options.from_ref is not None:
            execute_note += (
                " --from {ref} recorded; no git operations performed.".format(
                    ref=options.from_ref
                )
            )
        if options.rollback_to is not None:
            execute_note += (
                " --rollback-to {ref} recorded; no git operations "
                "performed.".format(ref=options.rollback_to)
            )
        return InstallCliResult(
            exit_code=EXIT_EXECUTE_NOT_AUTHORIZED,
            profile=canonical,
            execute_requested=True,
            resume=options.resume,
            from_ref=options.from_ref,
            rollback_to=options.rollback_to,
            plan=plan,
            preflight=preflight,
            executed=False,
            error="",
            notes=(execute_note,),
        )

    # 6. Dry-run success. Audit-only notes for --resume / --from /
    #    --rollback-to so an operator can see the flags were received.
    audit_notes: list = []
    if options.resume:
        audit_notes.append(
            "Phase 4B: --resume received; recorded for the future "
            "shell layer (no stage-marker replay in this slice)."
        )
    if options.from_ref is not None:
        audit_notes.append(
            "Phase 4B: --from {ref} recorded; no git operations "
            "performed.".format(ref=options.from_ref)
        )
    if options.rollback_to is not None:
        audit_notes.append(
            "Phase 4B: --rollback-to {ref} recorded; no git "
            "operations performed.".format(ref=options.rollback_to)
        )
    return InstallCliResult(
        exit_code=EXIT_OK,
        profile=canonical,
        execute_requested=False,
        resume=options.resume,
        from_ref=options.from_ref,
        rollback_to=options.rollback_to,
        plan=plan,
        preflight=preflight,
        executed=False,
        error="",
        notes=tuple(audit_notes),
    )


__all__ = [
    "InstallCliOptions",
    "InstallCliResult",
    "run_install",
]