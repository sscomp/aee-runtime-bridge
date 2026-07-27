"""AEE Epic 9.2 — Unified CLI UX (§21.2).

This module exposes the unified AEE command-line interface described
in Master Plan §21.2:

    aee --profile {full,mini,edge,developer} <subcommand>
    aee install --profile {full,mini,edge,developer}

Design contract (per Master Plan §21.2):
    * ``--profile`` accepts only the four canonical profile names from
      :data:`aee.profiles.descriptor.KNOWN_PROFILES`. There is **no**
      parallel hard-coded matrix — the CLI validation imports the
      canonical source of truth and uses it directly.
    * The default profile is :data:`aee.profiles.descriptor.DEFAULT_PROFILE`
      (``"full"``), matching the descriptor module.
    * An unknown profile is rejected with a non-zero exit code and a
      clear message (argparse ``choices`` produces exit code 2; the
      ``install`` subcommand additionally relies on the descriptor
      module's :class:`UnknownProfileError` for defence in depth).
    * ``--help`` lists the four profiles and the default behavior.
    * No installer backend is implemented in this slice. The
      ``install`` subcommand parses its arguments, validates the
      profile against the canonical source, and emits a dry-run /
      spec-level dispatch contract message. It performs **no** side
      effects (no ``subprocess``, no filesystem writes outside the
      ``--dry-run`` message on stdout, no service restarts).
    * Existing CLI surfaces (``python -m aee.reporting.build_index``)
      remain untouched and behave identically — backward compat is
      preserved byte-for-byte.

Run: ``PYTHONPATH=. python3 -m unittest aee.tests.test_aee92_unified_cli_ux -v``
"""
from __future__ import annotations

import argparse
import sys
from typing import List, Optional

# §21.8 Release Strategy — the canonical unified product version.
# ``aee.__version__`` is the single source of truth for the version
# string; ``aee --version`` and ``aee.release.changelog`` both read
# this attribute (no parallel hard-coded literal).
from aee import __version__ as _AEE_VERSION

# Canonical source of truth — NO parallel hard-coded matrix.
from aee.profiles.descriptor import (
    DEFAULT_PROFILE,
    KNOWN_PROFILES,
    UnknownProfileError,
    get_descriptor,
    parse_profile,
)

#: Program name used in argparse ``prog``.
PROG_NAME = "aee"

#: Exit code returned on success.
EXIT_OK = 0

#: Exit code returned on argument-parsing errors (argparse default).
EXIT_PARSE_ERROR = 2

#: Exit code returned on a profile-validation error that escapes
#: argparse (defence-in-depth; e.g. an empty / whitespace profile
#: that argparse accepts as a string but ``parse_profile`` rejects).
EXIT_PROFILE_ERROR = 3

#: Exit code returned when installer pre-flight fails (§21.3).
EXIT_PRE_FLIGHT_FAILED = 4

#: Exit code returned when an existing install with a different
#: profile is detected (§21.3 "profile switch requires uninstall +
#: reinstall").
EXIT_PROFILE_SWITCH_REJECTED = 5

#: Exit code returned by ``aee doctor`` when all required checks pass
#: but at least one optional check raised a caveat. Distinct from the
#: installer's exit codes (0/2/3/4/5/6) — see :mod:`aee.doctor`.
EXIT_DOCTOR_CAVEATS = 7

#: Exit code returned by ``aee doctor`` when at least one required
#: check failed.
EXIT_DOCTOR_FAILED = 8


def _profile_choices_help() -> str:
    """Build the ``--profile`` help string from the canonical tuple."""
    return (
        "Product profile to select for this invocation. "
        "One of: {profiles}. "
        "Default: {default} (matches aee.profiles.descriptor.DEFAULT_PROFILE)."
    ).format(
        profiles=", ".join(KNOWN_PROFILES),
        default=DEFAULT_PROFILE,
    )


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level AEE argparse parser.

    The parser exposes a single ``--profile`` global flag (parsed
    *before* the subcommand) and a set of subcommands. The only
    subcommand shipped in this slice is ``install``; the parser is
    structured so that future §21.x subcommands can be added without
    touching the profile-flag plumbing.
    """
    parser = argparse.ArgumentParser(
        prog=PROG_NAME,
        description=(
            "Agent Execution Engine (AEE) unified CLI. "
            "Master Plan §21.2 — Expose profile selection at the CLI "
            "so an operator can choose full / mini / edge / developer "
            "per invocation without editing config."
        ),
    )
    # §21.8 Release Strategy — ``aee --version`` returns the unified
    # product version (``2.0.0-rc1`` on first Epic 9 release, ``2.0.0``
    # GA when §21.10 completes). The version string is the canonical
    # ``aee.__version__`` attribute — argparse ``action="version"``
    # prints it to stdout and exits 0. There is no parallel hard-coded
    # version literal here; the canonical source is ``aee.__version__``.
    parser.add_argument(
        "--version",
        action="version",
        version="{prog} {ver}".format(prog=PROG_NAME, ver=_AEE_VERSION),
        help=(
            "Print the unified AEE product version (per §21.8) and "
            "exit. The version follows SemVer: MAJOR per Epic / MINOR "
            "per sub-section / PATCH per bugfix."
        ),
    )
    # Global --profile flag. ``choices`` is the canonical tuple; this
    # is the CLI-level validation that §21.2 calls for. argparse
    # rejects any value not in ``choices`` with exit code 2 and a
    # helpful message.
    parser.add_argument(
        "--profile",
        default=DEFAULT_PROFILE,
        choices=KNOWN_PROFILES,
        help=_profile_choices_help(),
    )
    subparsers = parser.add_subparsers(
        dest="subcommand",
        title="subcommands",
        metavar="<subcommand>",
    )

    # ``install`` subcommand (§21.3 installer backend is OUT of scope
    # for this slice — only the parsing/dispatch contract is wired).
    install_parser = subparsers.add_parser(
        "install",
        help=(
            "Install AEE for the selected profile. "
            "Backend is not implemented in this slice (§21.3 is a "
            "separately authorizable sub-section); the subcommand "
            "parses arguments, validates the profile against the "
            "canonical source, and emits a dry-run dispatch contract "
            "message."
        ),
    )
    # Per-subcommand ``--profile`` (brief canonical form:
    # ``aee install --profile <profile>``). When supplied, it takes
    # precedence over the global ``--profile`` (§21.2 form:
    # ``aee --profile <profile> install``). When omitted, the
    # global value (defaulting to DEFAULT_PROFILE) is used. The
    # ``choices`` constraint is the same canonical tuple, so an
    # invalid value is rejected at the argparse layer with exit
    # code 2 here too — no silent fallback to the global default.
    install_parser.add_argument(
        "--profile",
        default=None,
        choices=KNOWN_PROFILES,
        help=(
            "Profile to install for this invocation (overrides the "
            "global --profile). One of: {profiles}. "
            "Default: falls back to the global --profile (or "
            "{default} if the global flag is also omitted)."
        ).format(
            profiles=", ".join(KNOWN_PROFILES),
            default=DEFAULT_PROFILE,
        ),
    )
    install_parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print the resolved profile + dispatch contract and exit "
            "without performing any side effects (default behavior; "
            "this flag is accepted for forward-compat with §21.3)."
        ),
    )
    install_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the dispatch contract as a JSON object on stdout.",
    )

    # ``doctor`` subcommand (Phase 2 — AEE readiness health check).
    # The doctor is read-only and side-effect free; it never sends
    # credentials, mutates the dispatcher DB, or writes to disk. The
    # ``--no-network`` flag skips the upstream reachability probe for
    # air-gapped environments; ``--repo-root`` overrides the detected
    # repo root (defaults to cwd); ``--json`` emits a machine-readable
    # report on stdout.
    doctor_parser = subparsers.add_parser(
        "doctor",
        help=(
            "Run a comprehensive AEE readiness health check. Validates "
            "Python/runtime version, git availability, Hermes Runtime "
            "connectivity, required dependencies, configuration files, "
            "environment-variable presence, directory permissions, "
            "and optional Docker availability. Reports a PASS / "
            "PASS WITH CAVEATS / FAIL summary. Read-only; no side "
            "effects. Exit codes: 0 = OK, 7 = caveats, 8 = fail."
        ),
    )
    doctor_parser.add_argument(
        "--no-network",
        action="store_true",
        help=(
            "Skip the upstream Hermes Runtime reachability probe "
            "(use in air-gapped or offline environments)."
        ),
    )
    doctor_parser.add_argument(
        "--repo-root",
        default=None,
        help=(
            "Path to the AEE repository root (defaults to the current "
            "working directory)."
        ),
    )
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as a JSON object on stdout.",
    )
    return parser


def _resolve_profile(global_profile: str, sub_profile: Optional[str]) -> str:
    """Resolve the effective profile between the global and subcommand flags.

    Precedence: subcommand ``--profile`` (if not None) wins over the
    global ``--profile``. Both have already been validated by
    argparse ``choices`` against :data:`KNOWN_PROFILES`, so this
    function only picks the winner. Defence in depth: the result is
    still passed through :func:`parse_profile` in
    :func:`_install_dispatch` to catch any future code path that
    bypasses argparse.
    """
    return sub_profile if sub_profile is not None else global_profile


def _extract_global_profile(argv: Optional[List[str]]) -> str:
    """Recover the global ``--profile`` value from raw argv.

    argparse's subparser machinery overwrites ``args.profile`` with
    the subcommand's own ``--profile`` value, so the global value is
    lost after parsing. This tiny pre-pass parser only consumes the
    global ``--profile`` flag (and the global ``-h/--help``) and
    ignores everything else (including the subcommand and its
    arguments). It never raises ``SystemExit`` on its own — unknown
    tokens are passed through silently, since the real parser
    already validated them.

    Returns :data:`DEFAULT_PROFILE` when the global flag is absent.
    """
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument(
        "--profile",
        default=DEFAULT_PROFILE,
        choices=KNOWN_PROFILES,
    )
    # ``parse_known_args`` swallows everything we don't explicitly
    # declare here (subcommand + its flags), so this pre-pass only
    # reads the global ``--profile`` value.
    ns, _ = pre.parse_known_args(argv if argv is not None else sys.argv[1:])
    return ns.profile


def _install_dispatch(
    profile: str,
    *,
    json_output: bool = False,
) -> int:
    """Run the ``install`` dispatch contract (§21.2 CLI + §21.3 backend).

    This function delegates to the §21.3 :class:`InstallerBackend` to
    build an :class:`InstallPlan` and run read-only pre-flight. It
    performs **no** side effects: the backend is constructed with
    ``dry_run=True``, and ``execute(dry_run=False)`` is not called
    here (the shell-level execution path is a separately authorizable
    follow-up).

    Defence in depth: even though argparse ``choices`` already rejects
    unknown profiles, we call :func:`parse_profile` here so that any
    future code path that bypasses argparse (e.g. a programmatic
    caller) still gets canonical validation.

    Exit code mapping (composed with the backend's exit codes):
      * ``EXIT_OK`` (0) — plan + pre-flight succeeded.
      * ``EXIT_PROFILE_ERROR`` (3) — unknown profile.
      * ``EXIT_PRE_FLIGHT_FAILED`` (4) — pre-flight failed (e.g. repo
        root missing).
      * ``EXIT_PROFILE_SWITCH_REJECTED`` (5) — existing install with
        a different profile detected (§21.3 "profile switch requires
        uninstall + reinstall").
    """
    # Imported lazily to keep the §21.2 "no installer backend side
    # effects" invariant intact at module-import time — the backend
    # module is only loaded when ``install`` is actually dispatched.
    from aee.installer import (
        InstallerBackend,
        ProfileSwitchRejectedError,
        PreFlightFailedError,
    )

    try:
        canonical = parse_profile(profile)
    except UnknownProfileError as exc:
        msg = "error: {prog}: {err}\n".format(prog=PROG_NAME, err=exc)
        sys.stderr.write(msg)
        return EXIT_PROFILE_ERROR

    backend = InstallerBackend(dry_run=True)
    try:
        plan = backend.plan(canonical)
        result = backend.execute(canonical, dry_run=True)
    except UnknownProfileError as exc:
        msg = "error: {prog}: {err}\n".format(prog=PROG_NAME, err=exc)
        sys.stderr.write(msg)
        return EXIT_PROFILE_ERROR

    if not result.preflight.ok:
        # Distinguish profile-switch rejection from generic pre-flight
        # failure by inspecting the existing-profile field (§21.3).
        if (
            result.preflight.existing_profile is not None
            and result.preflight.existing_profile != canonical
        ):
            msg = "error: {prog}: {err}\n".format(
                prog=PROG_NAME, err=result.preflight.reason
            )
            sys.stderr.write(msg)
            return EXIT_PROFILE_SWITCH_REJECTED
        msg = "error: {prog}: pre-flight failed: {reason}\n".format(
            prog=PROG_NAME, reason=result.preflight.reason
        )
        sys.stderr.write(msg)
        return EXIT_PRE_FLIGHT_FAILED

    descriptor = plan.descriptor
    if json_output:
        import json
        payload = {
            "subcommand": "install",
            "profile": canonical,
            "default_profile": DEFAULT_PROFILE,
            "known_profiles": list(KNOWN_PROFILES),
            "descriptor": descriptor.to_dict(),
            "dry_run": True,
            "backend_implemented": True,
            "executed": False,
            "plan": plan.to_dict(),
            "preflight": result.preflight.to_dict(),
            "note": (
                "Epic 9.3 installer backend (§21.3) is wired; dry-run "
                "plan + read-only pre-flight only. The shell-level "
                "execution path (system user, env file, supervisord "
                "reload, smoke test) is a separately authorizable "
                "follow-up."
            ),
        }
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
        sys.stdout.write("\n")
        return EXIT_OK

    step_ids = [s.step_id for s in plan.steps]
    lines = [
        "aee install (dry-run / §21.3 installer backend)",
        "  profile (resolved)  : {p}".format(p=canonical),
        "  default profile     : {d}".format(d=DEFAULT_PROFILE),
        "  known profiles      : {k}".format(k=", ".join(KNOWN_PROFILES)),
        "  purpose             : {pur}".format(pur=descriptor.purpose),
        "  audience            : {aud}".format(aud=descriptor.audience),
        "  safety tier         : {st}".format(st=descriptor.safety_tier),
        "  toolset             : {ts}".format(ts=descriptor.toolset),
        "  can_dispatch        : {cd}".format(cd=descriptor.can_dispatch),
        "  can_create_cron     : {cc}".format(cc=descriptor.can_create_cron),
        "  is_read_only        : {ro}".format(ro=descriptor.is_read_only),
        "  backend_implemented : True (§21.3)",
        "  executed            : False (dry-run; shell path not authorized)",
        "  plan steps          : {n} ({ids})".format(
            n=len(plan.steps), ids=", ".join(step_ids)
        ),
        "  side effects        : none (dry-run; read-only pre-flight)",
    ]
    sys.stdout.write("\n".join(lines) + "\n")
    return EXIT_OK


def _doctor_dispatch(
    profile: str,
    *,
    no_network: bool = False,
    repo_root: Optional[str] = None,
    json_output: bool = False,
) -> int:
    """Run the ``aee doctor`` health check (Phase 2).

    Imports :mod:`aee.doctor` lazily so a missing optional dependency
    cannot break ``aee install``. Maps the doctor's verdict to the
    doctor-specific exit codes (:data:`EXIT_OK`,
    :data:`EXIT_DOCTOR_CAVEATS`, :data:`EXIT_DOCTOR_FAILED`).

    The doctor is read-only: it never sends credentials, mutates the
    dispatcher DB, or writes to disk. The ``--no-network`` flag skips
    the upstream reachability probe for air-gapped environments.
    """
    from aee.doctor import (
        DoctorRunner,
        EXIT_DOCTOR_CAVEATS,
        EXIT_DOCTOR_FAILED,
        EXIT_DOCTOR_OK,
    )
    from pathlib import Path

    runner = DoctorRunner(
        repo_root=Path(repo_root) if repo_root else None,
        profile=profile,
        network=not no_network,
    )
    report = runner.run()

    if json_output:
        import json
        sys.stdout.write(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        sys.stdout.write("\n")
    else:
        sys.stdout.write(report.to_text())

    if report.verdict == "PASS":
        return EXIT_DOCTOR_OK
    if report.verdict == "CAVEAT":
        return EXIT_DOCTOR_CAVEATS
    return EXIT_DOCTOR_FAILED


def main(argv: Optional[List[str]] = None) -> int:
    """Unified AEE CLI entrypoint.

    Returns the process exit code. Does not raise ``SystemExit`` for
    application-level errors — argparse raises ``SystemExit(2)`` for
    argument-parsing failures (the canonical argparse contract),
    which the caller may let propagate.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.subcommand is None:
        # No subcommand: print help and exit non-zero (matches the
        # standard ``aee`` with no args contract — operator must
        # pick a subcommand).
        parser.print_help()
        return EXIT_PARSE_ERROR
    if args.subcommand == "install":
        # When ``install`` is the active subcommand, argparse's
        # subparser overwrites ``args.profile`` with the subcommand's
        # own ``--profile`` value (default ``None``). The global
        # value is no longer recoverable from ``args`` alone. We
        # work around this by re-parsing the raw argv with a tiny
        # pre-pass parser that only consumes the global ``--profile``
        # flag. This keeps the canonical ``aee --profile X install``
        # and ``aee install --profile X`` forms both functional
        # and the brief's ``aee install --profile X`` form canonical.
        global_profile = _extract_global_profile(argv)
        sub_profile = args.profile  # None when subcommand flag omitted
        effective = _resolve_profile(global_profile, sub_profile)
        return _install_dispatch(
            effective,
            json_output=getattr(args, "json", False),
        )
    if args.subcommand == "doctor":
        # ``aee doctor`` — read-only readiness health check (Phase 2).
        # The doctor runs against the global ``--profile`` (recovered
        # via the same pre-pass as ``install`` because argparse's
        # subparser also overwrites ``args.profile`` here). The
        # ``--no-network`` flag and ``--repo-root`` flag are
        # subcommand-specific. The doctor module is imported lazily so
        # a missing optional dependency cannot break ``aee install``.
        global_profile = _extract_global_profile(argv)
        return _doctor_dispatch(
            profile=global_profile,
            no_network=getattr(args, "no_network", False),
            repo_root=getattr(args, "repo_root", None),
            json_output=getattr(args, "json", False),
        )
    # Future §21.x subcommands land here. Unknown subcommand is
    # impossible (argparse rejects it), but keep a defensive branch.
    parser.error("unknown subcommand: {sc!r}".format(sc=args.subcommand))
    return EXIT_PARSE_ERROR  # pragma: no cover


__all__ = [
    "PROG_NAME",
    "EXIT_OK",
    "EXIT_PARSE_ERROR",
    "EXIT_PROFILE_ERROR",
    "EXIT_PRE_FLIGHT_FAILED",
    "EXIT_PROFILE_SWITCH_REJECTED",
    "EXIT_DOCTOR_CAVEATS",
    "EXIT_DOCTOR_FAILED",
    "main",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())