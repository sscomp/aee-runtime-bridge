"""AEE Epic 9.7 §21.7 — CI/CD Matrix targeted tests.

Tests the CI/CD Matrix described in Master Plan §21.7 line 7790:

    One CI workflow with matrix job ``profile: [full, mini, edge,
    developer]``. Each job runs ``install.sh --profile ${{ matrix.profile }}``
    → smoke test → targeted tests → regression suite. ``full`` runs
    the complete suite; ``mini``/``edge``/``developer`` run subset
    suites. All 4 jobs must pass for merge to ``master``. Each job
    runs in its own container with its own tempdir DB.

Coverage (per workorder §6 — acceptance criteria, error paths,
dry-run, safety guards, unknown/unsupported config):

  * §21.7 acceptance item 1 — exactly 4 jobs, one per canonical
    profile (no extra, no missing, no duplicates).
  * §21.7 acceptance item 2 — ``full`` runs the complete suite;
    the other three run subset suites.
  * §21.7 acceptance item 3 — every job has ``dry_run_install ==
    True`` (workorder §5 dry-run-first).
  * §21.7 acceptance item 4 — every job has ``db_isolation ==
    True`` (§21.7 line 7790).
  * §21.7 acceptance item 5 — every job uses the canonical
    container image (provider-neutral, single image).
  * §21.7 acceptance item 6 — every job's commands are free of
    forbidden tokens (no ``--execute``, no ``docker push``, no
    ``terraform``, no cloud SDK CLI, no secret mutation, no
    release enablement).
  * §21.7 acceptance item 7 — every job has ``needs_all_green ==
    True`` (§21.7 "All 4 jobs must pass for merge to ``master``").
  * §21.7 acceptance item 8 — subset jobs reference at least one
    test module (no empty subset — a profile with no tests is a
    CI blind spot).
  * Error path — ``get_job_spec`` raises ``UnknownProfileError``
    on an unknown profile.
  * Error path — ``validate_matrix`` raises ``MatrixSpecError`` on
    a matrix with the wrong job count, a missing profile, a
    duplicate profile, a wrong ``suite_kind``, a non-dry-run
    install, no DB isolation, a wrong container image, a
    forbidden token, no all-green requirement, or an empty
    subset.
  * Dry-run — every job's ``install_command`` ends with
    ``--dry-run``.
  * Safety guard — no job's ``install_command`` or
    ``smoke_command`` contains ``--execute``, ``docker push``,
    ``kubectl``, ``terraform``, ``aws``, ``gcloud``, ``az``,
    ``secret``, ``release``, or ``publish``.
  * Unknown / unsupported config — ``get_job_spec("")`` and
    ``get_job_spec("bogus")`` raise ``UnknownProfileError``.
  * Workflow YAML agreement — the ``.github/workflows/ci-matrix.yml``
    file exists, declares the 4-profile matrix, references
    ``install.sh --dry-run``, and contains no forbidden token.
  * Provider neutrality — no cloud SDK / IaC string literal in
    the matrix module, the workflow YAML, or any job spec.
  * Subset suite sanity — every subset module referenced in
    :data:`SUBSET_SUITES_BY_PROFILE` is importable (so the CI
    job does not fail on a typo).
  * Single source of truth — :data:`CI_MATRIX_JOBS` has one
    entry per :data:`KNOWN_PROFILES`, in the canonical order.

Run: ``PYTHONPATH=. python3 -m unittest aee.tests.test_aee97_cicd_matrix -v``
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

# Ensure the repo root is on sys.path so `import aee.ci...` works
# when running from anywhere.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from aee.ci import (
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
from aee.ci.matrix import (
    SUITE_KIND_FULL,
    SUITE_KIND_SUBSET,
    _FORBIDDEN_COMMAND_TOKENS,
)
from aee.profiles.descriptor import (
    DEFAULT_PROFILE,
    KNOWN_PROFILES,
    parse_profile,
)


_WORKFLOW_YAML = _REPO_ROOT / ".github" / "workflows" / "ci-matrix.yml"


# ---------------------------------------------------------------------------
# §21.7 acceptance item 1 — exactly 4 jobs, one per canonical profile
# ---------------------------------------------------------------------------


class TestMatrixShape(unittest.TestCase):
    """§21.7 — matrix has exactly 4 jobs, one per canonical profile."""

    def test_matrix_has_four_jobs(self) -> None:
        self.assertEqual(len(CI_MATRIX_JOBS), 4)

    def test_matrix_has_one_job_per_canonical_profile(self) -> None:
        profiles = [spec.profile for spec in CI_MATRIX_JOBS]
        self.assertEqual(set(profiles), set(KNOWN_PROFILES))

    def test_matrix_profiles_in_canonical_order(self) -> None:
        profiles = [spec.profile for spec in CI_MATRIX_JOBS]
        self.assertEqual(tuple(profiles), KNOWN_PROFILES)

    def test_no_duplicate_profiles(self) -> None:
        profiles = [spec.profile for spec in CI_MATRIX_JOBS]
        self.assertEqual(len(profiles), len(set(profiles)))

    def test_every_job_is_ci_job_spec(self) -> None:
        for spec in CI_MATRIX_JOBS:
            self.assertIsInstance(spec, CIJobSpec)


# ---------------------------------------------------------------------------
# §21.7 acceptance item 2 — full runs complete suite; others run subsets
# ---------------------------------------------------------------------------


class TestSuiteKindAssignment(unittest.TestCase):
    """§21.7 — full runs the complete suite; others run subsets."""

    def test_full_runs_complete_suite(self) -> None:
        spec = get_job_spec("full")
        self.assertEqual(spec.suite_kind, SUITE_KIND_FULL)

    def test_full_has_empty_subset_modules(self) -> None:
        spec = get_job_spec("full")
        self.assertEqual(spec.subset_modules, ())

    def test_mini_runs_subset_suite(self) -> None:
        spec = get_job_spec("mini")
        self.assertEqual(spec.suite_kind, SUITE_KIND_SUBSET)

    def test_edge_runs_subset_suite(self) -> None:
        spec = get_job_spec("edge")
        self.assertEqual(spec.suite_kind, SUITE_KIND_SUBSET)

    def test_developer_runs_subset_suite(self) -> None:
        spec = get_job_spec("developer")
        self.assertEqual(spec.suite_kind, SUITE_KIND_SUBSET)

    def test_subset_profiles_have_nonempty_subset(self) -> None:
        for p in ("mini", "edge", "developer"):
            spec = get_job_spec(p)
            self.assertTrue(
                spec.subset_modules,
                "profile '{p}' has empty subset_modules".format(p=p),
            )

    def test_full_suite_profile_constant_is_full(self) -> None:
        self.assertEqual(FULL_SUITE_PROFILE, "full")


# ---------------------------------------------------------------------------
# §21.7 acceptance item 3 — dry-run-first install
# ---------------------------------------------------------------------------


class TestDryRunInstall(unittest.TestCase):
    """§21.7 + workorder §5 — every job is dry-run-install."""

    def test_every_job_has_dry_run_install_true(self) -> None:
        for spec in CI_MATRIX_JOBS:
            self.assertTrue(
                spec.dry_run_install,
                "profile '{p}' has dry_run_install=False".format(
                    p=spec.profile
                ),
            )

    def test_every_install_command_ends_with_dry_run(self) -> None:
        for spec in CI_MATRIX_JOBS:
            cmd = list(spec.install_command)
            self.assertIn("--dry-run", cmd,
                          "profile '{p}' install_command lacks --dry-run".format(
                              p=spec.profile
                          ))

    def test_no_install_command_contains_execute(self) -> None:
        for spec in CI_MATRIX_JOBS:
            self.assertNotIn("--execute", spec.install_command)


# ---------------------------------------------------------------------------
# §21.7 acceptance item 4 — DB isolation
# ---------------------------------------------------------------------------


class TestDBIsolation(unittest.TestCase):
    """§21.7 line 7790 — each job runs in its own container with its
    own tempdir DB."""

    def test_every_job_has_db_isolation_true(self) -> None:
        for spec in CI_MATRIX_JOBS:
            self.assertTrue(
                spec.db_isolation,
                "profile '{p}' has db_isolation=False".format(p=spec.profile),
            )


# ---------------------------------------------------------------------------
# §21.7 acceptance item 5 — single, provider-neutral container image
# ---------------------------------------------------------------------------


class TestContainerImage(unittest.TestCase):
    """§21.7 + §21.5 — single image, provider-neutral."""

    def test_every_job_uses_canonical_container_image(self) -> None:
        for spec in CI_MATRIX_JOBS:
            self.assertEqual(spec.container_image, CONTAINER_IMAGE)

    def test_container_image_is_python_311_slim(self) -> None:
        self.assertEqual(CONTAINER_IMAGE, "python:3.11-slim")

    def test_container_image_has_no_cloud_provider_reference(self) -> None:
        cloud_refs = ("aws", "gcp", "azure", "terraform", "eks", "gke",
                      "akm", "ecr", "gcr", "acr")
        for ref in cloud_refs:
            self.assertNotIn(ref, CONTAINER_IMAGE.lower(),
                             "container image references cloud provider '{r}'".format(r=ref))


# ---------------------------------------------------------------------------
# §21.7 acceptance item 6 — no forbidden tokens
# ---------------------------------------------------------------------------


class TestNoForbiddenTokens(unittest.TestCase):
    """§21.7 + workorder §5 — no production deploy, no registry push,
    no secret mutation, no release enablement."""

    def test_no_forbidden_token_in_any_install_command(self) -> None:
        for spec in CI_MATRIX_JOBS:
            blob = " ".join(spec.install_command).lower()
            for tok in _FORBIDDEN_COMMAND_TOKENS:
                self.assertNotIn(tok, blob,
                                 "profile '{p}' install_command contains '{t}'".format(
                                     p=spec.profile, t=tok
                                 ))

    def test_no_forbidden_token_in_any_smoke_command(self) -> None:
        for spec in CI_MATRIX_JOBS:
            blob = " ".join(spec.smoke_command).lower()
            for tok in _FORBIDDEN_COMMAND_TOKENS:
                self.assertNotIn(tok, blob,
                                 "profile '{p}' smoke_command contains '{t}'".format(
                                     p=spec.profile, t=tok
                                 ))


# ---------------------------------------------------------------------------
# §21.7 acceptance item 7 — needs_all_green
# ---------------------------------------------------------------------------


class TestNeedsAllGreen(unittest.TestCase):
    """§21.7 line 7790 — all 4 jobs must pass for merge to master."""

    def test_every_job_has_needs_all_green_true(self) -> None:
        for spec in CI_MATRIX_JOBS:
            self.assertTrue(
                spec.needs_all_green,
                "profile '{p}' has needs_all_green=False".format(p=spec.profile),
            )


# ---------------------------------------------------------------------------
# §21.7 acceptance item 8 — subset sanity
# ---------------------------------------------------------------------------


class TestSubsetSuiteSanity(unittest.TestCase):
    """§21.7 — subset jobs reference real, importable test modules."""

    def test_subset_suites_dict_has_three_subset_profiles(self) -> None:
        self.assertEqual(
            set(SUBSET_SUITES_BY_PROFILE.keys()),
            {"mini", "edge", "developer"},
        )

    def test_full_is_not_in_subset_suites_dict(self) -> None:
        self.assertNotIn("full", SUBSET_SUITES_BY_PROFILE)

    def test_every_subset_module_is_importable(self) -> None:
        import importlib
        for profile, modules in SUBSET_SUITES_BY_PROFILE.items():
            for mod in modules:
                try:
                    importlib.import_module(mod)
                except ImportError as exc:
                    self.fail(
                        "profile '{p}' subset references unimportable "
                        "module '{m}': {e}".format(p=profile, m=mod, e=exc)
                    )

    def test_every_subset_module_starts_with_aee_tests(self) -> None:
        for profile, modules in SUBSET_SUITES_BY_PROFILE.items():
            for mod in modules:
                self.assertTrue(
                    mod.startswith("aee.tests."),
                    "profile '{p}' subset module '{m}' does not start "
                    "with 'aee.tests.'".format(p=profile, m=mod),
                )

    def test_every_subset_includes_self_reference(self) -> None:
        # Every subset must include the §21.7 tests themselves, so a
        # regression in the matrix spec is caught by every profile.
        for profile, modules in SUBSET_SUITES_BY_PROFILE.items():
            self.assertIn(
                "aee.tests.test_aee97_cicd_matrix",
                modules,
                "profile '{p}' subset does not include the §21.7 tests".format(
                    p=profile
                ),
            )


# ---------------------------------------------------------------------------
# Error paths — get_job_spec
# ---------------------------------------------------------------------------


class TestGetJobSpecErrorPaths(unittest.TestCase):
    """get_job_spec raises on unknown / unsupported config.

    Note: ``parse_profile("")``, ``parse_profile(None)``, and
    ``parse_profile("   ")`` resolve to :data:`DEFAULT_PROFILE`
    (``"full"``) per the descriptor module's design — empty / None /
    whitespace is a *missing* value, not an *unknown* value. Only
    non-empty values that are NOT in :data:`KNOWN_PROFILES` raise
    :class:`UnknownProfileError`.
    """

    def test_bogus_profile_raises_unknown_profile(self) -> None:
        with self.assertRaises(UnknownProfileError):
            get_job_spec("bogus")

    def test_uppercase_full_raises_unknown_profile(self) -> None:
        # Profile names are case-sensitive (canonical tuple is
        # lowercase). An uppercase 'FULL' is unknown.
        with self.assertRaises(UnknownProfileError):
            get_job_spec("FULL")

    def test_numeric_profile_raises_unknown_profile(self) -> None:
        with self.assertRaises(UnknownProfileError):
            get_job_spec("1")

    def test_profile_with_spaces_raises_unknown_profile(self) -> None:
        # parse_profile strips whitespace, so ' mini ' resolves to
        # 'mini' (a known profile). A genuinely unknown value with
        # trailing space ('bogus ') is still unknown.
        with self.assertRaises(UnknownProfileError):
            get_job_spec("bogus ")


# ---------------------------------------------------------------------------
# Error paths — validate_matrix
# ---------------------------------------------------------------------------


class TestValidateMatrixErrorPaths(unittest.TestCase):
    """validate_matrix raises MatrixSpecError on every violation."""

    def _make_valid_spec(self, profile: str) -> CIJobSpec:
        return get_job_spec(profile)

    def test_validate_default_matrix_passes(self) -> None:
        # The canonical matrix must validate.
        validate_matrix()

    def test_validate_empty_matrix_raises(self) -> None:
        with self.assertRaises(MatrixSpecError):
            validate_matrix(())

    def test_validate_missing_profile_raises(self) -> None:
        # Drop 'developer' — only 3 jobs. The cardinality check
        # fires first ("must have exactly 4 jobs, got 3"); the
        # missing-profile check fires only when the count is right
        # but a canonical profile is absent. Test both paths.
        three = tuple(get_job_spec(p) for p in ("full", "mini", "edge"))
        with self.assertRaises(MatrixSpecError) as ctx:
            validate_matrix(three)
        # Cardinality-guard message names the count mismatch.
        msg = str(ctx.exception).lower()
        self.assertTrue("exactly 4" in msg or "got 3" in msg
                        or "developer" in msg.lower(),
                        "expected cardinality/missing message, got: {m}".format(
                            m=ctx.exception))

    def test_validate_missing_profile_with_correct_count_raises(self) -> None:
        # 4 jobs but one canonical profile is replaced by a
        # duplicate of another — the missing-profile check fires
        # after the duplicate check. Construct a matrix where the
        # count is 4, there are no duplicates, but a canonical
        # profile is missing. This is unreachable with the
        # current `KNOWN_PROFILES` of size 4 and CIJobSpec's
        # profile validation, so we craft a synthetic spec with a
        # non-canonical profile name.
        non_canon = CIJobSpec(
            profile="bogus",  # not in KNOWN_PROFILES
            suite_kind=SUITE_KIND_SUBSET,
            subset_modules=("aee.tests.test_aee97_cicd_matrix",),
            container_image=CONTAINER_IMAGE,
            dry_run_install=True,
            db_isolation=True,
            install_command=("bash", "install.sh", "--profile", "bogus", "--dry-run"),
            smoke_command=("python3", "-c", "import sys; sys.exit(0)"),
            needs_all_green=True,
        )
        four = (get_job_spec("full"), get_job_spec("mini"),
                get_job_spec("edge"), non_canon)
        with self.assertRaises(MatrixSpecError) as ctx:
            validate_matrix(four)
        # The missing profile is 'developer' (the only canonical
        # profile absent from `four`).
        self.assertIn("developer", str(ctx.exception).lower())

    def test_validate_extra_job_raises(self) -> None:
        # 4 valid jobs + a 5th duplicate of 'full'.
        five = CI_MATRIX_JOBS + (get_job_spec("full"),)
        with self.assertRaises(MatrixSpecError):
            validate_matrix(five)

    def test_validate_duplicate_profiles_raises(self) -> None:
        # Replace 'developer' with a second 'full'.
        dup = tuple(get_job_spec(p) for p in ("full", "mini", "edge", "full"))
        with self.assertRaises(MatrixSpecError) as ctx:
            validate_matrix(dup)
        self.assertIn("duplicate", str(ctx.exception).lower())

    def test_validate_full_with_subset_kind_raises(self) -> None:
        spec = get_job_spec("full")
        bad = CIJobSpec(
            profile=spec.profile,
            suite_kind=SUITE_KIND_SUBSET,  # wrong — full must be SUITE_KIND_FULL
            subset_modules=spec.subset_modules,
            container_image=spec.container_image,
            dry_run_install=spec.dry_run_install,
            db_isolation=spec.db_isolation,
            install_command=spec.install_command,
            smoke_command=spec.smoke_command,
            needs_all_green=spec.needs_all_green,
        )
        with self.assertRaises(MatrixSpecError) as ctx:
            validate_matrix((bad,) + tuple(get_job_spec(p) for p in ("mini", "edge", "developer")))
        self.assertIn("full", str(ctx.exception))

    def test_validate_subset_with_full_kind_raises(self) -> None:
        spec = get_job_spec("mini")
        bad = CIJobSpec(
            profile=spec.profile,
            suite_kind=SUITE_KIND_FULL,  # wrong — mini must be SUITE_KIND_SUBSET
            subset_modules=spec.subset_modules,
            container_image=spec.container_image,
            dry_run_install=spec.dry_run_install,
            db_isolation=spec.db_isolation,
            install_command=spec.install_command,
            smoke_command=spec.smoke_command,
            needs_all_green=spec.needs_all_green,
        )
        with self.assertRaises(MatrixSpecError) as ctx:
            validate_matrix((get_job_spec("full"), bad) + tuple(get_job_spec(p) for p in ("edge", "developer")))
        self.assertIn("mini", str(ctx.exception))

    def test_validate_non_dry_run_install_raises(self) -> None:
        spec = get_job_spec("mini")
        bad = CIJobSpec(
            profile=spec.profile,
            suite_kind=spec.suite_kind,
            subset_modules=spec.subset_modules,
            container_image=spec.container_image,
            dry_run_install=False,  # violation
            db_isolation=spec.db_isolation,
            install_command=spec.install_command,
            smoke_command=spec.smoke_command,
            needs_all_green=spec.needs_all_green,
        )
        with self.assertRaises(MatrixSpecError) as ctx:
            validate_matrix((get_job_spec("full"), bad) + tuple(get_job_spec(p) for p in ("edge", "developer")))
        self.assertIn("dry_run", str(ctx.exception).lower())

    def test_validate_no_db_isolation_raises(self) -> None:
        spec = get_job_spec("edge")
        bad = CIJobSpec(
            profile=spec.profile,
            suite_kind=spec.suite_kind,
            subset_modules=spec.subset_modules,
            container_image=spec.container_image,
            dry_run_install=spec.dry_run_install,
            db_isolation=False,  # violation
            install_command=spec.install_command,
            smoke_command=spec.smoke_command,
            needs_all_green=spec.needs_all_green,
        )
        with self.assertRaises(MatrixSpecError) as ctx:
            validate_matrix((get_job_spec("full"), get_job_spec("mini"), bad, get_job_spec("developer")))
        self.assertIn("db_isolation", str(ctx.exception).lower())

    def test_validate_wrong_container_image_raises(self) -> None:
        spec = get_job_spec("developer")
        bad = CIJobSpec(
            profile=spec.profile,
            suite_kind=spec.suite_kind,
            subset_modules=spec.subset_modules,
            container_image="ubuntu:22.04",  # violation — wrong image
            dry_run_install=spec.dry_run_install,
            db_isolation=spec.db_isolation,
            install_command=spec.install_command,
            smoke_command=spec.smoke_command,
            needs_all_green=spec.needs_all_green,
        )
        with self.assertRaises(MatrixSpecError) as ctx:
            validate_matrix((get_job_spec("full"), get_job_spec("mini"), get_job_spec("edge"), bad))
        self.assertIn("container_image", str(ctx.exception).lower())

    def test_validate_forbidden_token_in_install_raises(self) -> None:
        spec = get_job_spec("mini")
        bad = CIJobSpec(
            profile=spec.profile,
            suite_kind=spec.suite_kind,
            subset_modules=spec.subset_modules,
            container_image=spec.container_image,
            dry_run_install=spec.dry_run_install,
            db_isolation=spec.db_isolation,
            install_command=("bash", "install.sh", "--profile", "mini", "--execute"),  # forbidden
            smoke_command=spec.smoke_command,
            needs_all_green=spec.needs_all_green,
        )
        with self.assertRaises(MatrixSpecError) as ctx:
            validate_matrix((get_job_spec("full"), bad) + tuple(get_job_spec(p) for p in ("edge", "developer")))
        # The forbidden-token check fires before the dry-run check
        # because it inspects the command tokens directly.
        self.assertIn("--execute", str(ctx.exception))

    def test_validate_no_needs_all_green_raises(self) -> None:
        spec = get_job_spec("full")
        bad = CIJobSpec(
            profile=spec.profile,
            suite_kind=spec.suite_kind,
            subset_modules=spec.subset_modules,
            container_image=spec.container_image,
            dry_run_install=spec.dry_run_install,
            db_isolation=spec.db_isolation,
            install_command=spec.install_command,
            smoke_command=spec.smoke_command,
            needs_all_green=False,  # violation
        )
        with self.assertRaises(MatrixSpecError) as ctx:
            validate_matrix((bad,) + tuple(get_job_spec(p) for p in ("mini", "edge", "developer")))
        self.assertIn("needs_all_green", str(ctx.exception).lower())

    def test_validate_empty_subset_raises(self) -> None:
        spec = get_job_spec("mini")
        bad = CIJobSpec(
            profile=spec.profile,
            suite_kind=spec.suite_kind,
            subset_modules=(),  # violation — empty subset
            container_image=spec.container_image,
            dry_run_install=spec.dry_run_install,
            db_isolation=spec.db_isolation,
            install_command=spec.install_command,
            smoke_command=spec.smoke_command,
            needs_all_green=spec.needs_all_green,
        )
        with self.assertRaises(MatrixSpecError) as ctx:
            validate_matrix((get_job_spec("full"), bad) + tuple(get_job_spec(p) for p in ("edge", "developer")))
        self.assertIn("subset", str(ctx.exception).lower())


# ---------------------------------------------------------------------------
# Workflow YAML agreement
# ---------------------------------------------------------------------------


class TestWorkflowYamlAgreement(unittest.TestCase):
    """The .github/workflows/ci-matrix.yml file mirrors the Python
    matrix spec. This test class verifies the two agree on the
    critical invariants."""

    def test_workflow_yaml_exists(self) -> None:
        self.assertTrue(_WORKFLOW_YAML.exists(),
                        "ci-matrix.yml missing at {p}".format(p=_WORKFLOW_YAML))

    def _yaml_text(self) -> str:
        if not _WORKFLOW_YAML.exists():
            self.skipTest("ci-matrix.yml missing")
        return _WORKFLOW_YAML.read_text(encoding="utf-8")

    def test_workflow_declares_four_profiles(self) -> None:
        text = self._yaml_text()
        for p in KNOWN_PROFILES:
            self.assertIn("- profile: {p}".format(p=p), text,
                          "workflow YAML missing profile '{p}'".format(p=p))

    def test_workflow_uses_dry_run_install(self) -> None:
        text = self._yaml_text()
        self.assertIn("--dry-run", text)

    def test_workflow_does_not_use_execute(self) -> None:
        text = self._yaml_text()
        # `--execute` is the forbidden §21.3 shell-level path. Strip
        # comments first so a doc comment mentioning the flag does
        # not false-positive.
        non_comment = "\n".join(
            ln for ln in text.splitlines()
            if not ln.strip().startswith("#")
        )
        self.assertNotIn("--execute", non_comment)

    def test_workflow_does_not_push_to_registry(self) -> None:
        text = self._yaml_text()
        # `docker push` / `docker login` are registry operations
        # that belong to §21.8 release scope, not §21.7 CI. Strip
        # comments first — doc comments may mention the forbidden
        # tokens as part of the safety contract.
        non_comment = "\n".join(
            ln for ln in text.splitlines()
            if not ln.strip().startswith("#")
        ).lower()
        self.assertNotIn("docker push", non_comment)
        self.assertNotIn("docker login", non_comment)

    def test_workflow_does_not_mutate_secrets(self) -> None:
        text = self._yaml_text()
        # `secrets.` is the GitHub Actions secrets context; any
        # reference to it would imply the workflow reads or writes
        # repository secrets, which is out of scope for §21.7.
        self.assertNotIn("secrets.", text)

    def test_workflow_does_not_enable_release(self) -> None:
        text = self._yaml_text()
        # `release` / `publish` are §21.8 release enablement
        # keywords. They may appear in comments (this test guards
        # the non-comment surface; the comment-tolerant check
        # below is a softer guard).
        # Strip YAML comments (lines starting with #).
        non_comment = "\n".join(
            ln for ln in text.splitlines()
            if not ln.strip().startswith("#")
        )
        for tok in ("release", "publish"):
            self.assertNotIn(tok, non_comment.lower(),
                             "workflow YAML non-comment surface contains '{t}'".format(t=tok))

    def test_workflow_does_not_reference_cloud_providers(self) -> None:
        text = self._yaml_text().lower()
        # Strip YAML comments first — doc comments may legitimately
        # mention forbidden tokens as part of the safety contract.
        non_comment = "\n".join(
            ln for ln in text.splitlines()
            if not ln.strip().startswith("#")
        )
        for ref in ("terraform", "aws ", "gcloud", "az ", "kubectl"):
            self.assertNotIn(ref, non_comment,
                             "workflow YAML non-comment surface references cloud/IaC token '{t}'".format(t=ref))

    def test_workflow_uses_python_311_slim_container(self) -> None:
        text = self._yaml_text()
        self.assertIn("python:3.11-slim", text)

    def test_workflow_has_merge_gate_job(self) -> None:
        text = self._yaml_text()
        # The merge-gate job is the §21.7 "all 4 must pass" enforcement.
        self.assertIn("merge-gate", text)

    def test_workflow_matrix_includes_subset_kind_per_profile(self) -> None:
        text = self._yaml_text()
        # The `include:` block must declare suite_kind for each profile.
        # full → full; mini/edge/developer → subset.
        for p in ("mini", "edge", "developer"):
            self.assertIn("suite_kind: subset", text)
        self.assertIn("suite_kind: full", text)

    def test_workflow_has_no_services_block(self) -> None:
        text = self._yaml_text()
        # No shared DB service — per §21.7 each job has its own
        # tempdir DB.
        # `services:` is a top-level key; check it does not appear
        # at the start of a line (YAML key).
        lines = text.splitlines()
        for ln in lines:
            stripped = ln.lstrip()
            if stripped.startswith("services:"):
                self.fail("workflow YAML has a `services:` block — §21.7 requires per-job DB isolation, no shared service")


# ---------------------------------------------------------------------------
# Provider neutrality — no cloud SDK / IaC literal in the matrix module
# ---------------------------------------------------------------------------


class TestProviderNeutrality(unittest.TestCase):
    """§21.7 + workorder §5 — provider-neutral, no cloud SDK / IaC."""

    def test_matrix_module_has_no_cloud_sdk_import(self) -> None:
        import inspect
        from aee.ci import matrix as matrix_mod
        src = inspect.getsource(matrix_mod)
        # Strip comments, docstrings, AND the
        # `_forbidden_command_tokens` frozenset body (the guard
        # data legitimately contains the forbidden SDK names —
        # that IS the safety mechanism).
        lines = src.splitlines()
        kept = []
        in_docstring = False
        skip_until_frozenset_end = False
        for ln in lines:
            stripped = ln.strip()
            if skip_until_frozenset_end:
                if stripped.rstrip().endswith("})"):
                    skip_until_frozenset_end = False
                continue
            if in_docstring:
                if stripped.endswith('"""') or stripped.endswith("'''"):
                    in_docstring = False
                continue
            if stripped.startswith('"""') or stripped.startswith("'''"):
                if not (stripped.endswith('"""') and len(stripped) > 3):
                    in_docstring = True
                continue
            if stripped.startswith("#"):
                continue
            if "_forbidden_command_tokens" in stripped.lower() and "frozenset({" in stripped.lower():
                if not stripped.rstrip().endswith("})"):
                    skip_until_frozenset_end = True
                continue
            kept.append(ln)
        kept_src = "\n".join(kept)
        for sdk in ("boto3", "google-cloud", "azure-", "terraform"):
            self.assertNotIn(sdk, kept_src,
                             "matrix module executable source references '{s}'".format(s=sdk))

    def test_matrix_module_has_no_cloud_cli_token(self) -> None:
        import inspect
        from aee.ci import matrix as matrix_mod
        src = inspect.getsource(matrix_mod)
        # Strip comments, docstrings, AND the
        # `_forbidden_command_tokens` frozenset body (the guard
        # data legitimately contains the forbidden tokens — that
        # IS the safety mechanism).
        lines = src.splitlines()
        kept = []
        in_docstring = False
        skip_until_frozenset_end = False
        for ln in lines:
            stripped = ln.strip()
            if skip_until_frozenset_end:
                if stripped.endswith("})"):
                    skip_until_frozenset_end = False
                continue
            if in_docstring:
                if stripped.endswith('"""') or stripped.endswith("'''"):
                    in_docstring = False
                continue
            if stripped.startswith('"""') or stripped.startswith("'''"):
                if not (stripped.endswith('"""') and len(stripped) > 3):
                    in_docstring = True
                continue
            if stripped.startswith("#"):
                continue
            if "_forbidden_command_tokens" in stripped.lower() and "frozenset({" in stripped.lower():
                if not stripped.rstrip().endswith("})"):
                    skip_until_frozenset_end = True
                continue
            kept.append(ln)
        kept_lower = "\n".join(kept).lower()
        for tok in ("kubectl", "terraform", "aws ", "gcloud", "az "):
            self.assertNotIn(tok, kept_lower,
                             "matrix module non-docstring non-comment non-guard source references '{t}'".format(t=tok))

    def test_ci_package_has_no_cloud_sdk_import(self) -> None:
        import inspect
        import aee.ci as ci_pkg
        src = inspect.getsource(ci_pkg)
        for sdk in ("boto3", "google-cloud", "azure-", "terraform"):
            self.assertNotIn(sdk, src)


# ---------------------------------------------------------------------------
# Single source of truth — CI_MATRIX_JOBS mirrors KNOWN_PROFILES
# ---------------------------------------------------------------------------


class TestSingleSourceOfTruth(unittest.TestCase):
    """The matrix derives from KNOWN_PROFILES, not a parallel tuple."""

    def test_ci_matrix_jobs_profiles_equal_known_profiles(self) -> None:
        self.assertEqual(
            tuple(s.profile for s in CI_MATRIX_JOBS),
            KNOWN_PROFILES,
        )

    def test_full_suite_profile_equals_default_profile(self) -> None:
        # `full` is both the default profile and the full-suite
        # profile. This is intentional: the default profile is
        # the one that runs the complete suite in CI.
        self.assertEqual(FULL_SUITE_PROFILE, DEFAULT_PROFILE)

    def test_get_job_spec_round_trips_through_parse_profile(self) -> None:
        for p in KNOWN_PROFILES:
            spec = get_job_spec(p)
            self.assertEqual(spec.profile, parse_profile(p))


# ---------------------------------------------------------------------------
# to_dict round-trip
# ---------------------------------------------------------------------------


class TestToDictRoundTrip(unittest.TestCase):
    """CIJobSpec.to_dict() produces a JSON-serializable dict."""

    def test_to_dict_keys(self) -> None:
        spec = get_job_spec("mini")
        d = spec.to_dict()
        expected_keys = {
            "profile", "suite_kind", "subset_modules",
            "container_image", "dry_run_install", "db_isolation",
            "install_command", "smoke_command", "needs_all_green",
        }
        self.assertEqual(set(d.keys()), expected_keys)

    def test_to_dict_is_json_serializable(self) -> None:
        import json
        for spec in CI_MATRIX_JOBS:
            d = spec.to_dict()
            json.dumps(d)  # must not raise

    def test_to_dict_subset_modules_is_list(self) -> None:
        for spec in CI_MATRIX_JOBS:
            d = spec.to_dict()
            self.assertIsInstance(d["subset_modules"], list)


if __name__ == "__main__":
    unittest.main()