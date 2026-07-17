"""AEE Epic 9.7 §21.7 — CI/CD Matrix.

This package implements the CI/CD Matrix described in Master Plan
§21.7. The matrix is a single CI/CD pipeline that tests all four
profiles (``full``, ``mini``, ``edge``, ``developer``) so a regression
in ``mini`` is caught before merge, not after a B2 deployment breaks.

Per Master Plan §21.7:

    One CI workflow with matrix job ``profile: [full, mini, edge,
    developer]``. Each job runs ``install.sh --profile ${{ matrix.profile }}``
    → smoke test → targeted tests → regression suite. ``full`` runs
    the complete suite; ``mini``/``edge``/``developer`` run subset
    suites. All 4 jobs must pass for merge to ``master``. Each job
    runs in its own container with its own tempdir DB.

Design contract (per workorder §5 — provider-neutral, dry-run-first,
safe defaults):

* **No production deploy.** The matrix spec describes what each job
  runs; it does NOT push to any registry, does NOT modify repository
  secrets, does NOT enable a formal release, and does NOT touch a
  production database.
* **Provider-neutral.** The CI host is referenced by container image
  only (``python:3.11-slim``); the matrix spec carries no AWS / GCP /
  Azure / Terraform / IaC references. The GitHub Actions workflow
  file at ``.github/workflows/ci-matrix.yml`` is the orchestration
  surface; ``aee.ci.matrix`` is its declarative source of truth.
* **Dry-run-first install.** Every matrix job invokes
  ``install.sh --profile <p> --dry-run`` (per the §21.3 installer
  safety contract). The ``--execute`` path is out of scope for
  §21.7 and remains separately authorizable per §21.3.
* **Subset suites per profile.** ``full`` runs the complete AEE
  suite; ``mini``/``edge``/``developer`` run the subset suites
  defined here (per §21.7 line 7790). The subset selection is
  data-driven from :data:`SUBSET_SUITES_BY_PROFILE`, not hardcoded
  in the workflow YAML, so adding a test module does not require
  editing the workflow.
* **Per-job isolation.** Each matrix job runs in its own container
  with its own tempdir DB (per §21.7 line 7790). The matrix spec
  exposes ``container_image`` and ``db_isolation`` fields so the
  workflow can materialize isolation; the Python module does not
  spawn containers.
* **Single source of truth.** The four profile names, the default
  profile, and the resource floor come from
  :mod:`aee.profiles.descriptor`. The matrix module does NOT
  maintain a parallel profile matrix.

Public surface (re-exported):

* :data:`CI_MATRIX_JOBS`        — tuple of :class:`CIJobSpec` (one per profile)
* :class:`CIJobSpec`            — declarative spec for one matrix job
* :data:`SUBSET_SUITES_BY_PROFILE` — profile → subset test-suite mapping
* :data:`FULL_SUITE_PROFILE`    — the profile that runs the complete suite
* :data:`CONTAINER_IMAGE`       — the canonical CI container image
* :func:`get_job_spec`          — look up a job spec by profile
* :func:`validate_matrix`       — validate the matrix against §21.7 acceptance
* :class:`MatrixSpecError`      — base error
* :class:`UnknownProfileError`  — re-exported from the descriptor module

Invariants enforced by :func:`validate_matrix`:

1. Exactly 4 jobs, one per canonical profile (no extra, no missing).
2. ``full`` runs the complete suite (``suite_kind == "full"``).
3. ``mini``/``edge``/``developer`` run subset suites (``suite_kind
   == "subset"``).
4. Every job has ``dry_run_install == True`` (no production install
   in CI).
5. Every job has ``db_isolation == True`` (per §21.7 line 7790).
6. Every job uses the same ``container_image`` (single image, per
   §21.7 / §21.5 single-image principle).
7. No job references a registry push, a production deploy, a secret
   mutation, or a release-enablement step.

See Master Plan §21.7 and §21.A acceptance item 7 for the
authoritative contract.
"""
from __future__ import annotations

from aee.ci.matrix import (
    CI_MATRIX_JOBS,
    CIJobSpec,
    CONTAINER_IMAGE,
    FULL_SUITE_PROFILE,
    SUBSET_SUITES_BY_PROFILE,
    MatrixSpecError,
    UnknownProfileError,
    get_job_spec,
    validate_matrix,
)

__all__ = [
    "CI_MATRIX_JOBS",
    "CIJobSpec",
    "CONTAINER_IMAGE",
    "FULL_SUITE_PROFILE",
    "SUBSET_SUITES_BY_PROFILE",
    "MatrixSpecError",
    "UnknownProfileError",
    "get_job_spec",
    "validate_matrix",
]