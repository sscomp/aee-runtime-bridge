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
* :data:`EXIT_CAPABILITIES_INVALID` (13) — WO-3 (§21.6.G item 3): the
  Host Capability Document supplied via ``--capabilities`` is
  missing, unreadable, malformed, or fails the §21.6.B contract /
  §21.6.C resource-floor validation. Distinct from 3-6 so an operator
  can tell "the capabilities contract was rejected" apart from "the
  install plan / pre-flight failed". When ``--capabilities`` is
  omitted, this exit code is never produced (backward compat).

Run: ``PYTHONPATH=. python3 -m unittest aee.tests.test_aee_phase4b_install_cli -v``
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from aee.installer.backend import (
    EXIT_CAPABILITIES_INVALID,
    EXIT_EXECUTE_NOT_AUTHORIZED,
    EXIT_OK,
    EXIT_PRE_FLIGHT_FAILED,
    EXIT_PROFILE_INVALID,
    EXIT_PROFILE_SWITCH_REJECTED,
    CapabilitiesValidationResult,
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
    * ``capabilities`` — the path to a Host Capability Document
      YAML supplied via ``--capabilities <path>``, or ``None`` when
      the flag was omitted. WO-2 (this field) is **plumbing-only**:
      the path is recorded in the result and an audit note is
      emitted; the backend contract binding (loading + validating
      the document and refusing the install when it is invalid)
      is WO-3 and is NOT performed here. A light read-only
      ``os.path.exists`` check surfaces whether the file is
      present, but does not change the exit code.
    """

    profile: str = DEFAULT_PROFILE
    execute: bool = False
    resume: bool = False
    from_ref: Optional[str] = None
    rollback_to: Optional[str] = None
    repo_root: Optional[Path] = None
    capabilities: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile": self.profile,
            "execute": self.execute,
            "resume": self.resume,
            "from_ref": self.from_ref,
            "rollback_to": self.rollback_to,
            "repo_root": str(self.repo_root) if self.repo_root else None,
            "capabilities": self.capabilities,
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
    * ``capabilities`` — the path to the Host Capability Document
      YAML supplied via ``--capabilities <path>``, or ``None`` when
      the flag was omitted. WO-2 plumbing-only — recorded for the
      future WO-3 backend binding; not enforced here.
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
    capabilities: Optional[str]
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
            "capabilities": self.capabilities,
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
# ---------------------------------------------------------------------------#
# WO-3 — capabilities validation-success audit note
# ---------------------------------------------------------------------------#


def _capabilities_validated_note(
    capabilities_path: str,
    cap_result: CapabilitiesValidationResult,
) -> str:
    """Build the audit note for a successfully validated ``--capabilities``
    document (WO-3).

    WO-3 binds the installer backend to the authoritative §21.6.B /
    §21.6.C contract: the document is loaded via the canonical loader
    and validated via :func:`validate_capabilities` +
    :func:`validate_resource_floor` BEFORE any plan/preflight/execute
    action. This note is emitted only when validation succeeded
    (``cap_result.ok is True``); failure notes are emitted by the
    ``run_install`` WO-3 guard above.

    The note surfaces the validated host name + class so an operator
    can see what was accepted, and records that the install proceeded
    past the contract gate.
    """
    cap = cap_result.capabilities
    host_name = cap.name if cap is not None else "(unknown)"
    host_class = cap.class_ if cap is not None else "(unknown)"
    return (
        " WO-3: --capabilities {p} validated via canonical §21.6.B / "
        "§21.6.C contract (host={n}, class={c}); install proceeded "
        "past the contract gate."
    ).format(
        p=capabilities_path,
        n=host_name,
        c=host_class,
    )


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
            capabilities=options.capabilities,
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
    #    WO-3 (§21.6.G item 3): when ``--capabilities <path>`` is
    #    supplied, the ``cap_path`` is threaded into the backend so
    #    :meth:`validate_capabilities_document` can load + validate it
    #    via the canonical §21.6.B / §21.6.C contract BEFORE any
    #    plan/preflight/execute action.
    backend = InstallerBackend(
        repo_root=options.repo_root,
        dry_run=True,
        cap_path=options.capabilities,
    )

    # 2.5 WO-3: validate the supplied Host Capability Document (if any)
    #     BEFORE any plan/preflight/execute action. When
    #     ``--capabilities`` is omitted, ``options.capabilities`` is
    #     ``None`` and the backend's ``validate_capabilities_document``
    #     returns ``ok=True`` with ``capabilities=None`` (the
    #     backward-compat path — no extra I/O, no validation). When
    #     supplied, a failure is surfaced as a deterministic,
    #     user-visible :class:`CapabilitiesValidationResult` with a
    #     stable ``reason_kind``; the CLI maps it to
    #     :data:`EXIT_CAPABILITIES_INVALID` (13) and does NOT proceed
    #     to plan/preflight.
    cap_result: CapabilitiesValidationResult = (
        backend.validate_capabilities_document(profile=canonical)
    )
    if not cap_result.ok:
        cap_note = (
            "WO-3: --capabilities {p} rejected (reason_kind={k}); "
            "install refused before plan/preflight. "
            "reason: {r}"
        ).format(
            p=cap_result.cap_path or options.capabilities or "",
            k=cap_result.reason_kind,
            r=cap_result.reason,
        )
        if cap_result.field:
            cap_note += " (field={f})".format(f=cap_result.field)
        return InstallCliResult(
            exit_code=EXIT_CAPABILITIES_INVALID,
            profile=canonical,
            execute_requested=options.execute,
            resume=options.resume,
            from_ref=options.from_ref,
            rollback_to=options.rollback_to,
            capabilities=options.capabilities,
            plan=None,
            preflight=None,
            executed=False,
            error=cap_result.reason,
            notes=(cap_note,),
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
            capabilities=options.capabilities,
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
            capabilities=options.capabilities,
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
            capabilities=options.capabilities,
            plan=plan,
            preflight=preflight,
            executed=False,
            error=error_msg,
            notes=notes,
        )

    # 5. Pre-flight OK. If ``--execute`` was requested, drive the
    #    BootstrapRunner (stages 02-07) for real. The runner creates the
    #    venv, installs locked deps, runs the doctor health check, runs
    #    the smoke test, and writes the AGENT_READY marker. No credential
    #    provisioning happens here — the runner threads os.environ to
    #    stages; secrets the operator already provisioned are read at
    #    runtime. When ``--execute`` was NOT requested, exit 0 (dry-run).
    if options.execute:
        from aee.installer.runner import BootstrapRunner

        repo_root = (
            Path(options.repo_root) if options.repo_root is not None
            else Path.cwd()
        )
        runner = BootstrapRunner(
            repo_root=repo_root,
            profile=canonical,
            dry_run=False,
        )
        run_result = runner.run()

        if run_result.ok and run_result.agent_ready:
            execute_notes = [
                "Bootstrap hardening: --execute drove stages 02-07; "
                "AGENT_READY marker written. run_id={rid}, duration={d:.1f}s.".format(
                    rid=run_result.run_id, d=run_result.duration_seconds
                )
            ]
            for s in run_result.stages:
                execute_notes.append(
                    "  {stage}: {outcome} — {msg}".format(
                        stage=s.stage.value,
                        outcome=s.outcome.value,
                        msg=s.message,
                    )
                )
            return InstallCliResult(
                exit_code=EXIT_OK,
                profile=canonical,
                execute_requested=True,
                resume=options.resume,
                from_ref=options.from_ref,
                rollback_to=options.rollback_to,
                capabilities=options.capabilities,
                plan=plan,
                preflight=preflight,
                executed=True,
                error="",
                notes=tuple(execute_notes),
            )
        else:
            failing = (
                run_result.failing_stage.value
                if run_result.failing_stage
                else "unknown"
            )
            execute_note = (
                "Bootstrap hardening: --execute drove stages 02-07 but "
                "stage {f} failed. run_id={rid}, duration={d:.1f}s.".format(
                    f=failing,
                    rid=run_result.run_id,
                    d=run_result.duration_seconds,
                )
            )
            return InstallCliResult(
                exit_code=EXIT_PRE_FLIGHT_FAILED,
                profile=canonical,
                execute_requested=True,
                resume=options.resume,
                from_ref=options.from_ref,
                rollback_to=options.rollback_to,
                capabilities=options.capabilities,
                plan=plan,
                preflight=preflight,
                executed=False,
                error="bootstrap stage {f} failed".format(f=failing),
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
    if options.capabilities is not None:
        audit_notes.append(
            _capabilities_validated_note(options.capabilities, cap_result)
        )
    return InstallCliResult(
        exit_code=EXIT_OK,
        profile=canonical,
        execute_requested=False,
        resume=options.resume,
        from_ref=options.from_ref,
        rollback_to=options.rollback_to,
        capabilities=options.capabilities,
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