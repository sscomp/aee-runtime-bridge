"""AEE Epic 9.7 §21.7 — CI/CD Matrix declarative spec.

This module is the single Python source of truth for the CI/CD
matrix described in Master Plan §21.7. The matrix is consumed by:

* ``.github/workflows/ci-matrix.yml`` — the GitHub Actions workflow
  that orchestrates the 4 profile jobs.
* :func:`aee.ci.validate_matrix` — the programmatic gate used by
  the §21.7 targeted tests to verify the matrix still matches the
  §21.7 acceptance criteria.

Per Master Plan §21.7 line 7790:

    One CI workflow with matrix job ``profile: [full, mini, edge,
    developer]``. Each job runs ``install.sh --profile ${{ matrix.profile }}``
    → smoke test → targeted tests → regression suite. ``full`` runs
    the complete suite; ``mini``/``edge``/``developer`` run subset
    suites. All 4 jobs must pass for merge to ``master``. Each job
    runs in its own container with its own tempdir DB.

Design contract (workorder §5 — provider-neutral, dry-run-first,
safe defaults):

* No cloud SDK, no IaC tool, no provider-specific reference. The
  CI host is ``python:3.11-slim`` (matches the §21.5 Dockerfile
  base); no AWS / GCP / Azure / Terraform string literal appears
  in this module.
* No subprocess import. The matrix spec is pure data; the workflow
  file is the orchestration surface.
* No filesystem writes. Module import has no side effects.
* The four profile names come from
  :data:`aee.profiles.descriptor.KNOWN_PROFILES`. There is no
  parallel hard-coded profile tuple in this module.

Acceptance gate (Master Plan §21.A item 7):

    §21.7 — CI/CD matrix runs 4 profile jobs; all 4 green on
    ``master``.

This module is the declarative half of that acceptance. The
workflow YAML is the orchestration half; the targeted tests
(``aee.tests.test_aee97_cicd_matrix``) verify both halves agree.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Optional, Tuple

# Canonical source of truth — NO parallel hard-coded matrix.
from aee.profiles.descriptor import (
    DEFAULT_PROFILE,
    KNOWN_PROFILES,
    UnknownProfileError,
    parse_profile,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The canonical CI container image. Matches the §21.5 Dockerfile
#: ``FROM python:3.11-slim`` base so the CI host and the production
#: image share a Python version. Provider-neutral: no AWS / GCP /
#: Azure / Terraform reference.
CONTAINER_IMAGE: str = "python:3.11-slim"

#: The profile that runs the complete AEE test suite in CI. Per
#: §21.7 line 7790: "``full`` runs the complete suite". The other
#: three profiles run subset suites (see :data:`SUBSET_SUITES_BY_PROFILE`).
FULL_SUITE_PROFILE: str = "full"

#: Suite kind vocabulary. ``"full"`` means the complete AEE test
#: suite; ``"subset"`` means the per-profile subset defined in
#: :data:`SUBSET_SUITES_BY_PROFILE`. The workflow uses this field
#: to decide whether to pass ``--profile <p>`` to the full-suite
#: discovery or to the subset list.
SUITE_KIND_FULL: str = "full"
SUITE_KIND_SUBSET: str = "subset"


# ---------------------------------------------------------------------------
# Subset suites per profile (§21.7 line 7790)
# ---------------------------------------------------------------------------

#: Per-profile subset test suites. ``full`` is intentionally absent
#: — its job runs the complete suite (``suite_kind == "full"``), not
#: a subset. The subsets are the **minimum** set of test modules
#: each profile must pass in CI; they are not the complete suite.
#:
#: Selection rationale (per §21.7 line 7790 + §21.1 capability
#: matrix):
#:
#: * ``mini`` — the dispatch / installer / CLI surface that ``mini``
#:   actually exercises. ``mini`` cannot create cron or delegate
#:   subagents (per §21.1), so cron / subagent tests are excluded.
#: * ``edge`` — the read-only enforcement surface. ``edge`` cannot
#:   perform DB writes (per §21.1), so write-side and dispatch-
#:   write tests are excluded. The read-only PRAGMA tests are the
#:   core of the edge subset.
#: * ``developer`` — the sandbox surface. ``developer`` cannot
#:   touch production DB (per §21.1), so production-DB tests are
#:   excluded. The sandbox / tempdir-DB tests are the core.
#:
#: The subset is a tuple of test module names (without the
#: ``aee.tests.`` prefix); the workflow discovers them via
#: ``python3 -m unittest <module>``.
SUBSET_SUITES_BY_PROFILE: Dict[str, Tuple[str, ...]] = {
    "mini": (
        "aee.tests.test_aee91_canonical_profile_matrix",
        "aee.tests.test_aee92_unified_cli_ux",
        "aee.tests.test_aee93_installer_backend",
        "aee.tests.test_aee96_provider_neutral_deployment",
        "aee.tests.test_aee97_cicd_matrix",
    ),
    "edge": (
        "aee.tests.test_aee91_canonical_profile_matrix",
        "aee.tests.test_aee93_installer_backend",
        "aee.tests.test_aee94_runtime_profile_selection",
        "aee.tests.test_aee95_docker_profiles",
        "aee.tests.test_aee96_provider_neutral_deployment",
        "aee.tests.test_aee97_cicd_matrix",
    ),
    "developer": (
        "aee.tests.test_aee91_canonical_profile_matrix",
        "aee.tests.test_aee92_unified_cli_ux",
        "aee.tests.test_aee93_installer_backend",
        "aee.tests.test_aee95_docker_profiles",
        "aee.tests.test_aee96_provider_neutral_deployment",
        "aee.tests.test_aee97_cicd_matrix",
    ),
}


# ---------------------------------------------------------------------------
# CIJobSpec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CIJobSpec:
    """Declarative spec for one CI/CD matrix job (§21.7).

    A :class:`CIJobSpec` is pure data — it describes *what* the job
    runs, not *how* the workflow orchestrates it. The workflow YAML
    at ``.github/workflows/ci-matrix.yml`` is the orchestration
    surface; this dataclass is its source of truth.

    Fields:

    * ``profile`` — one of :data:`KNOWN_PROFILES`. The matrix has
      exactly one job per profile.
    * ``suite_kind`` — ``"full"`` (complete AEE suite) or
      ``"subset"`` (per-profile subset from
      :data:`SUBSET_SUITES_BY_PROFILE`). Per §21.7 line 7790, only
      ``full`` runs the complete suite.
    * ``subset_modules`` — when ``suite_kind == "subset"``, the
      tuple of test module names the job runs. Empty for
      ``suite_kind == "full"``.
    * ``container_image`` — the CI container image. Provider-neutral;
      all 4 jobs use the same image (per §21.5 single-image
      principle and §21.7 line 7790 "each job runs in its own
      container").
    * ``dry_run_install`` — always ``True`` in this slice (per
      workorder §5: dry-run-first, safe defaults). The
      ``install.sh --execute`` path is out of scope for §21.7 and
      remains separately authorizable per §21.3.
    * ``db_isolation`` — always ``True`` (per §21.7 line 7790:
      "Each job runs in its own container with its own tempdir DB").
    * ``install_command`` — the canonical install command the job
      runs. Always ``install.sh --profile <p> --dry-run`` in this
      slice.
    * ``smoke_command`` — the smoke step the job runs after install.
      The §21.7 proposal lists "smoke test" as the second step; in
      this slice the smoke is a no-op exit-0 placeholder, because
      the §21.3 shell-level execution path is not authorized. The
      workflow runs ``python3 -c "import sys; sys.exit(0)"`` as the
      smoke placeholder; no production service is started.
    * ``needs_all_green`` — always ``True`` (per §21.7 line 7790:
      "All 4 jobs must pass for merge to ``master``").
    """

    profile: str
    suite_kind: str
    subset_modules: Tuple[str, ...]
    container_image: str
    dry_run_install: bool
    db_isolation: bool
    install_command: Tuple[str, ...]
    smoke_command: Tuple[str, ...]
    needs_all_green: bool

    def to_dict(self) -> dict:
        return {
            "profile": self.profile,
            "suite_kind": self.suite_kind,
            "subset_modules": list(self.subset_modules),
            "container_image": self.container_image,
            "dry_run_install": self.dry_run_install,
            "db_isolation": self.db_isolation,
            "install_command": list(self.install_command),
            "smoke_command": list(self.smoke_command),
            "needs_all_green": self.needs_all_green,
        }


# ---------------------------------------------------------------------------
# Matrix construction
# ---------------------------------------------------------------------------


def _build_install_command(profile: str) -> Tuple[str, ...]:
    """Build the canonical install command for ``profile``.

    The command is ``bash install.sh --profile <p> --dry-run``. The
    ``--dry-run`` flag is the workorder §5 safe-default; the
    ``--execute`` path is out of scope for §21.7.
    """
    return ("bash", "install.sh", "--profile", profile, "--dry-run")


def _build_smoke_command() -> Tuple[str, ...]:
    """Build the smoke step command.

    The §21.7 proposal lists "smoke test" as the second step. In
    this slice, the smoke is a no-op exit-0 placeholder, because
    the §21.3 shell-level execution path is not authorized (the
    installer backend's ``execute(dry_run=False)`` raises
    :class:`ExecuteNotAuthorizedError`). No production service is
    started. The placeholder is explicit so the workflow YAML can
    be audited for the absence of a real smoke invocation.
    """
    return ("python3", "-c", "import sys; sys.exit(0)")


def _build_job(profile: str) -> CIJobSpec:
    """Build the :class:`CIJobSpec` for one profile."""
    canonical = parse_profile(profile)  # defence in depth
    if canonical == FULL_SUITE_PROFILE:
        suite_kind = SUITE_KIND_FULL
        subset: Tuple[str, ...] = ()
    else:
        suite_kind = SUITE_KIND_SUBSET
        subset = SUBSET_SUITES_BY_PROFILE[canonical]
    return CIJobSpec(
        profile=canonical,
        suite_kind=suite_kind,
        subset_modules=subset,
        container_image=CONTAINER_IMAGE,
        dry_run_install=True,
        db_isolation=True,
        install_command=_build_install_command(canonical),
        smoke_command=_build_smoke_command(),
        needs_all_green=True,
    )


#: The canonical CI/CD matrix — one job per profile, in the
#: canonical order from :data:`KNOWN_PROFILES`. This tuple is the
#: single source of truth consumed by the workflow YAML and by
#: :func:`validate_matrix`.
CI_MATRIX_JOBS: Tuple[CIJobSpec, ...] = tuple(
    _build_job(p) for p in KNOWN_PROFILES
)


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def get_job_spec(profile: str) -> CIJobSpec:
    """Look up the :class:`CIJobSpec` for ``profile``.

    Raises :class:`UnknownProfileError` (from the descriptor module)
    when ``profile`` is not one of :data:`KNOWN_PROFILES`. This is
    the defence-in-depth path — the workflow YAML uses the matrix
    ``include`` to enumerate the four profiles, so an unknown
    profile cannot reach this function via the workflow; the guard
    exists for programmatic callers.
    """
    canonical = parse_profile(profile)
    for spec in CI_MATRIX_JOBS:
        if spec.profile == canonical:
            return spec
    # Unreachable: parse_profile rejects unknown profiles, and
    # CI_MATRIX_JOBS has one entry per KNOWN_PROFILES.
    raise MatrixSpecError(
        "matrix has no job for profile '{p}' (KNOWN_PROFILES={k})".format(
            p=canonical, k=list(KNOWN_PROFILES),
        )
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class MatrixSpecError(Exception):
    """Raised when the CI matrix does not match the §21.7 acceptance
    criteria. The message describes the specific mismatch."""


# Forbidden substrings — none of these may appear in any job's
# install/smoke command or in the container image. They guard
# against scope creep into §21.8 (release), production deploy,
# registry push, or secret mutation.
_FORBIDDEN_COMMAND_TOKENS: FrozenSet[str] = frozenset({
    "--execute",          # §21.3 shell-level execution path (out of scope)
    "docker", "push",     # registry push (§21.8 release scope)
    "kubectl",            # production deploy
    "terraform",          # IaC tool (provider-neutrality)
    "aws", "gcloud", "az",  # cloud SDK CLI
    "secret", "secrets",  # repository secret mutation
    "release", "publish",  # §21.8 release enablement
})


def _check_no_forbidden_tokens(spec: CIJobSpec) -> None:
    """Ensure no forbidden token appears in the job's commands."""
    blob = " ".join(spec.install_command) + " " + " ".join(spec.smoke_command)
    blob_lower = blob.lower()
    for tok in _FORBIDDEN_COMMAND_TOKENS:
        if tok in blob_lower:
            raise MatrixSpecError(
                "job for profile '{p}' contains forbidden token "
                "'{tok}' in command: {cmd}. Per workorder §5: no "
                "production deploy, no registry push, no secret "
                "mutation, no release enablement.".format(
                    p=spec.profile, tok=tok, cmd=spec.install_command,
                )
            )


def validate_matrix(jobs: Optional[Tuple[CIJobSpec, ...]] = None) -> None:
    """Validate the CI/CD matrix against the §21.7 acceptance criteria.

    Raises :class:`MatrixSpecError` on the first violation. Returns
    ``None`` on success. The checks encode §21.7 line 7790 + §21.A
    acceptance item 7 + the workorder §5 safety guards.

    Checks:

    1. Exactly 4 jobs, one per canonical profile (no extra, no
       missing, no duplicates).
    2. ``full`` runs the complete suite (``suite_kind == "full"``);
       the other three run subset suites (``suite_kind == "subset"``).
    3. Every job has ``dry_run_install == True`` (no production
       install in CI — workorder §5).
    4. Every job has ``db_isolation == True`` (§21.7 line 7790).
    5. Every job uses :data:`CONTAINER_IMAGE` (single image, §21.5
       single-image principle; provider-neutral).
    6. Every job's commands are free of forbidden tokens (no
       ``--execute``, no ``docker push``, no ``terraform``, no
       cloud SDK CLI, no secret mutation, no release enablement).
    7. Every job has ``needs_all_green == True`` (§21.7 line 7790:
       "All 4 jobs must pass for merge to ``master``").
    8. Subset jobs reference at least one test module (no empty
       subset — a profile with no tests is a CI blind spot).
    """
    matrix = jobs if jobs is not None else CI_MATRIX_JOBS
    # Check 1a: exactly 4 jobs (cardinality). Distinguished from
    # the missing/duplicate checks below so the error message names
    # the actual defect.
    if len(matrix) != len(KNOWN_PROFILES):
        raise MatrixSpecError(
            "matrix must have exactly {n} jobs (one per profile), "
            "got {m}".format(n=len(KNOWN_PROFILES), m=len(matrix))
        )
    seen_profiles = [spec.profile for spec in matrix]
    # Check 1b: duplicates (checked before missing so a duplicate
    # matrix reports 'duplicate', not 'missing' — the duplicate
    # could fill the missing slot).
    if len(set(seen_profiles)) != len(seen_profiles):
        dup = [p for p in seen_profiles if seen_profiles.count(p) > 1]
        raise MatrixSpecError(
            "matrix has duplicate profile jobs: {p}".format(
                p=sorted(set(dup)))
        )
    # Check 1c: missing profiles (after cardinality + duplicate
    # checks pass, so the only remaining failure is a missing
    # canonical profile).
    for p in KNOWN_PROFILES:
        if p not in seen_profiles:
            raise MatrixSpecError(
                "matrix is missing job for profile '{p}'".format(p=p)
            )
    # Check 2: full runs the complete suite; the others run subsets.
    for spec in matrix:
        if spec.profile == FULL_SUITE_PROFILE:
            if spec.suite_kind != SUITE_KIND_FULL:
                raise MatrixSpecError(
                    "profile 'full' must run suite_kind='full' "
                    "(got '{k}')".format(k=spec.suite_kind)
                )
            if spec.subset_modules:
                raise MatrixSpecError(
                    "profile 'full' must have empty subset_modules "
                    "(full runs the complete suite)"
                )
        else:
            if spec.suite_kind != SUITE_KIND_SUBSET:
                raise MatrixSpecError(
                    "profile '{p}' must run suite_kind='subset' "
                    "(got '{k}')".format(p=spec.profile, k=spec.suite_kind)
                )
    # Check 3: every job is dry-run-install.
    for spec in matrix:
        if not spec.dry_run_install:
            raise MatrixSpecError(
                "job for profile '{p}' has dry_run_install=False; "
                "CI install must be dry-run (workorder §5)".format(
                    p=spec.profile
                )
            )
    # Check 4: every job has db_isolation.
    for spec in matrix:
        if not spec.db_isolation:
            raise MatrixSpecError(
                "job for profile '{p}' has db_isolation=False; "
                "§21.7 requires each job to run in its own container "
                "with its own tempdir DB".format(p=spec.profile)
            )
    # Check 5: every job uses the canonical container image.
    for spec in matrix:
        if spec.container_image != CONTAINER_IMAGE:
            raise MatrixSpecError(
                "job for profile '{p}' uses container_image='{c}'; "
                "expected '{e}' (single image, provider-neutral)".format(
                    p=spec.profile, c=spec.container_image, e=CONTAINER_IMAGE,
                )
            )
    # Check 6: no forbidden tokens in any job's commands.
    for spec in matrix:
        _check_no_forbidden_tokens(spec)
    # Check 7: every job requires all-green to merge.
    for spec in matrix:
        if not spec.needs_all_green:
            raise MatrixSpecError(
                "job for profile '{p}' has needs_all_green=False; "
                "§21.7 requires all 4 jobs to pass for merge to "
                "master".format(p=spec.profile)
            )
    # Check 8: subset jobs reference at least one test module.
    for spec in matrix:
        if spec.suite_kind == SUITE_KIND_SUBSET:
            if not spec.subset_modules:
                raise MatrixSpecError(
                    "profile '{p}' has suite_kind='subset' but "
                    "subset_modules is empty — a profile with no "
                    "tests is a CI blind spot".format(p=spec.profile)
                )


__all__ = [
    "CI_MATRIX_JOBS",
    "CIJobSpec",
    "CONTAINER_IMAGE",
    "FULL_SUITE_PROFILE",
    "SUBSET_SUITES_BY_PROFILE",
    "SUITE_KIND_FULL",
    "SUITE_KIND_SUBSET",
    "MatrixSpecError",
    "UnknownProfileError",
    "get_job_spec",
    "validate_matrix",
]