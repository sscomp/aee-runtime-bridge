"""AEE-7.8 K2.5 — Targeted tests for the opt-in planner wire-up.

Coverage surface (one ``TestCase`` per concern):

* :class:`TestApplyWithPlanByteIdentity` — when ``manifest_path``
  is ``None`` (the default), the wrapper returns a
  byte-for-byte identical :class:`ApplySidecarsResult` to
  :func:`apply_sidecars` (same ``to_dict()``, same iteration
  order, same on-disk sidecar SHA-256s, same
  ``by_decision`` / ``by_record_kind`` counts). The
  ``plan_input_summary`` field is ``None``.
* :class:`TestApplyWithPlanAdditiveSummary` — when a real
  ``AEE_7_7d_7e_MANIFEST.json`` is supplied, the wrapper
  attaches an :class:`ApplyWithPlanSummary` whose
  ``plan_input_count`` matches the real manifest, and the
  underlying apply pass still runs (same outcomes as the
  no-manifest path).
* :class:`TestApplyWithPlanBadManifestPropagates` — when the
  caller supplies a missing / non-JSON ``manifest_path``, the
  transport-level error is raised (NOT silently swallowed)
  because the path is an explicit user-supplied input.
* :class:`TestApplyWithPlanValidationFailureIsAdditive` —
  when the manifest loads but fails validation, the
  :class:`ApplyWithPlanSummary` reports
  ``projection_passed=False`` and the apply pass still runs
  (no gating).
* :class:`TestApplyWithPlanNoDispatcherImport` — the wrapper
  module (``aee.audit.apply_sidecars``) does not introduce
  ``dispatcher.*`` into ``sys.modules`` when imported. K1
  import-isolation contract preserved (the lazy import of
  ``aee.audit.manifest`` inside the wrapper does not pull
  dispatcher transitively).
* :class:`TestApplyWithPlanReExports` — the new symbols
  (``apply_sidecars_with_plan``, ``ApplyWithPlanSummary``,
  ``PLAN_APPLY_SCHEMA_VERSION``) are re-exported from
  ``aee.audit`` and in the package ``__all__``.

Run:
    PYTHONPATH=/home/ubuntu/hermes-runtime-bridge /usr/bin/python3 -m unittest aee.tests.test_aee78_plan_apply -v
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path


# Make ``aee`` importable when running via ``python -m unittest
# aee.tests.test_aee78_plan_apply`` from outside the repo root.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


from aee.audit import (  # noqa: E402
    APPLY_SCHEMA_VERSION,
    PLAN_APPLY_SCHEMA_VERSION,
    ApplySidecarsResult,
    ApplyWithPlanSummary,
    AuditSummary,
    PerTaskSidecarOutcome,
    PerTaskVerdict,
    SidecarDecision,
    apply_sidecars,
    apply_sidecars_with_plan,
)
from aee.audit.manifest import (  # noqa: E402
    ManifestError,
    load_manifest,
    manifest_to_plan_inputs,
)
from aee.reporting.identity import (  # noqa: E402
    RecordKind,
    _file_sha256,
    read_identity_sidecar,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

#: Path to the real K1 reader input. Lives at the repo root and
#: is the artifact the K2.5 wire-up is expected to consume in the
#: happy-path test. Computed from this file's location rather than
#: hard-coded so the test passes from any cwd.
_REAL_MANIFEST = (
    Path(__file__).resolve().parent.parent.parent
    / "AEE_7_7d_7e_MANIFEST.json"
)


#: Canonical UTC stamp for deterministic apply output. Mirrors
#: the AEE-7.7b test pattern.
_FIXTURE_UTC_STAMP = "2026-07-12T13:00:00Z"


#: A RUNTIME record (the canonical real-executor shape).
_RUNTIME_RECORD = {
    "task_id": "TASK-20260712-K25-001",
    "title": "K2.5 RUNTIME smoke",
    "type": "coding",
    "priority": 50,
    "owner": "m2",
    "status": "completed",
    "progress_pct": 100,
    "progress_step": "done",
    "created_at": "2026-07-12T10:00:00.000Z",
    "started_at": "2026-07-12T10:00:01.000Z",
    "finished_at": "2026-07-12T10:05:00.000Z",
    "duration_sec": 299.0,
    "input_text": "marker_runtime_input_k25",
    "hermes_run_id": "run_0123456789abcdef0123456789abcdef",
    "executor_session_id": "AEE-7.8-K25-RUNTIME-20260712",
    "runtime_run_id": "run_0123456789abcdef0123456789abcdef",
    "repo_root": "/home/ubuntu/hermes-runtime-bridge",
}

#: A FIXTURE record. ``hermes_run_id`` is in the default
#: sentinel set so the classifier marks it as FIXTURE.
_FIXTURE_RECORD = {
    "task_id": "TASK-20260712-K25-002",
    "title": "aee6-traversal",
    "type": "coding",
    "priority": 50,
    "owner": "m2",
    "status": "running",
    "progress_pct": 5,
    "input_text": "marker_fixture_input_k25",
    "hermes_run_id": "hr-1",
    "executor_session_id": "AEE-7.8-K25-FIXTURE-20260712",
    "runtime_run_id": "run-aae-k25-fixture",
    "repo_root": "/home/ubuntu/hermes-runtime-bridge",
}


def _write_task_json(root: Path, task_id: str, payload: dict) -> Path:
    """Write a single ``task.json`` under ``root/<task_id>/``."""
    d = root / task_id
    d.mkdir(parents=True, exist_ok=True)
    p = d / "task.json"
    p.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return p


def _make_verdict(
    *,
    task_id: str,
    record_kind: str,
    is_consistent: bool,
    source_sha: str,
    is_fixture: bool = False,
    fixture_markers: tuple = (),
) -> PerTaskVerdict:
    """Build a single :class:`PerTaskVerdict` for the K2.5 tests.

    Mirrors the AEE-7.7b helper but is duplicated here so this
    test file is self-contained (no cross-test helper
    dependency).
    """
    findings: list = []
    return PerTaskVerdict(
        task_id=task_id,
        record_kind=record_kind,
        is_consistent=is_consistent,
        is_fixture=is_fixture,
        fixture_markers=fixture_markers,
        source_task_json_sha256=source_sha,
        findings=tuple(findings),
        classified_at_utc=_FIXTURE_UTC_STAMP,
    )


def _make_summary(verdicts: list) -> AuditSummary:
    """Build a minimal :class:`AuditSummary` for the K2.5 tests.

    Uses the actual :class:`AuditSummary` field set
    (``reports_root``, ``audited_at_utc``, ``schema_version``,
    ``verdicts``, ``by_record_kind``, ``by_consistency``,
    ``finding_code_counts``, ``fixture_inconsistent_count``)
    — the helper preserves the dataclass contract, not the
    K2.5 caller's expectations. Aggregates are derived from
    ``verdicts`` directly so the helper is correct by
    construction.
    """
    consistent = sum(1 for v in verdicts if v.is_consistent)
    inconsistent = sum(1 for v in verdicts if not v.is_consistent)
    by_record_kind: dict = {}
    for v in verdicts:
        by_record_kind[v.record_kind] = by_record_kind.get(v.record_kind, 0) + 1
    return AuditSummary(
        reports_root="",
        audited_at_utc=_FIXTURE_UTC_STAMP,
        schema_version="1.0.0",
        verdicts=list(verdicts),
        by_record_kind=by_record_kind,
        by_consistency={"consistent": consistent, "inconsistent": inconsistent},
        finding_code_counts={},
        fixture_inconsistent_count=0,
    )


def _build_corpus(tmp: Path):
    """Build the canonical K2.5 corpus (RUNTIME + FIXTURE).

    Returns ``(reports_root, summary)`` ready to be fed to
    :func:`apply_sidecars_with_plan` (or
    :func:`apply_sidecars`). The two fixtures are written to
    disk so the apply pass can write sidecars next to them.
    """
    reports_root = tmp / "reports"
    reports_root.mkdir(exist_ok=True)
    rt_path = _write_task_json(reports_root, _RUNTIME_RECORD["task_id"], _RUNTIME_RECORD)
    fx_path = _write_task_json(reports_root, _FIXTURE_RECORD["task_id"], _FIXTURE_RECORD)
    verdicts = [
        _make_verdict(
            task_id=_RUNTIME_RECORD["task_id"],
            record_kind=RecordKind.RUNTIME.value,
            is_consistent=True,
            source_sha=_file_sha256(rt_path),
        ),
        _make_verdict(
            task_id=_FIXTURE_RECORD["task_id"],
            record_kind=RecordKind.FIXTURE.value,
            is_consistent=True,
            is_fixture=True,
            fixture_markers=("sentinel_hermes_run_id:'hr-1'",),
            source_sha=_file_sha256(fx_path),
        ),
    ]
    return reports_root, _make_summary(verdicts)


def _all_sidecar_sha256s(reports_root: Path) -> dict:
    """Return ``{task_id: sidecar_sha256}`` for every task.json
    under ``reports_root``. Used by the byte-identity test to
    confirm the on-disk sidecar SHA-256s match across both call
    sites.
    """
    out = {}
    for d in sorted(reports_root.iterdir()):
        sidecar = d / "identity.json"
        if sidecar.exists():
            out[d.name] = _file_sha256(sidecar)
    return out


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestApplyWithPlanByteIdentity(unittest.TestCase):
    """When ``manifest_path is None`` (the default), the wrapper
    returns a byte-for-byte identical
    :class:`ApplySidecarsResult` to :func:`apply_sidecars`.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee78k25-byteid-"))
        self.reports_root, self.summary = _build_corpus(self._tmp)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_manifest_path_none_returns_pass_through(self) -> None:
        """The wrapper with ``manifest_path=None`` returns a
        result with ``plan_input_summary is None``."""
        result = apply_sidecars_with_plan(
            self.reports_root,
            self.summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        self.assertIsNone(result.plan_input_summary)

    def test_to_dict_is_byte_identical_to_apply_sidecars(self) -> None:
        """``to_dict()`` of the wrapper's result is byte-for-byte
        identical to ``to_dict()`` of :func:`apply_sidecars`'s
        result. This is the core byte-identity invariant — the
        default-path call site must be indistinguishable from
        a direct :func:`apply_sidecars` call.

        Both runs use the SAME ``reports_root`` (under the
        same ``_tmp``) so the ``reports_root`` field in the
        resulting ``to_dict()`` is identical. We re-build
        the corpus between runs to clear sidecars from the
        first run.
        """
        # First pass: wrapper with manifest_path=None.
        wrapper_result = apply_sidecars_with_plan(
            self.reports_root,
            self.summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        wrapper_dict = wrapper_result.to_dict()

        # Strip sidecars (but keep the same tmpdir) and re-run
        # the baseline. We do NOT recreate the tmpdir because
        # the apply result's reports_root is path-resolved and
        # would otherwise differ.
        import shutil
        for d in self.reports_root.iterdir():
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
        self.reports_root, self.summary = _build_corpus(self._tmp)

        baseline_result = apply_sidecars(
            self.reports_root,
            self.summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        baseline_dict = baseline_result.to_dict()

        self.assertEqual(wrapper_dict, baseline_dict)

    def test_on_disk_sidecar_sha256s_match_apply_sidecars(self) -> None:
        """The on-disk sidecar SHA-256s are identical between
        the wrapper and a direct :func:`apply_sidecars` call.
        The wrapper must NOT change the sidecar writer
        behavior.

        Same ``reports_root`` for both runs (so the apply
        result's path resolution is stable).
        """
        # First pass: wrapper.
        apply_sidecars_with_plan(
            self.reports_root,
            self.summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        wrapper_shas = _all_sidecar_sha256s(self.reports_root)

        # Strip sidecars (keep same tmpdir) and re-run baseline.
        import shutil
        for d in self.reports_root.iterdir():
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
        self.reports_root, self.summary = _build_corpus(self._tmp)

        apply_sidecars(
            self.reports_root,
            self.summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        baseline_shas = _all_sidecar_sha256s(self.reports_root)

        self.assertEqual(wrapper_shas, baseline_shas)

    def test_by_decision_and_by_record_kind_match(self) -> None:
        """The aggregate counts on the wrapper's result match
        the baseline exactly. (The byte-identity test above is
        stronger, but this is a fast smoke for the most
        important aggregate fields.)"""
        wrapper_result = apply_sidecars_with_plan(
            self.reports_root,
            self.summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)
        self._tmp = Path(tempfile.mkdtemp(prefix="aee78k25-byteid-"))
        self.reports_root, self.summary = _build_corpus(self._tmp)

        baseline_result = apply_sidecars(
            self.reports_root,
            self.summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )

        self.assertEqual(
            wrapper_result.by_decision,
            baseline_result.by_decision,
        )
        self.assertEqual(
            wrapper_result.by_record_kind,
            baseline_result.by_record_kind,
        )
        self.assertEqual(
            wrapper_result.sidecars_written,
            baseline_result.sidecars_written,
        )
        self.assertEqual(
            wrapper_result.anchor_warning_count,
            baseline_result.anchor_warning_count,
        )
        self.assertEqual(
            len(wrapper_result.outcomes),
            len(baseline_result.outcomes),
        )

    def test_repeated_passes_with_manifest_path_none_remain_idempotent(self) -> None:
        """Two back-to-back wrapper calls with
        ``manifest_path=None`` are idempotent (second pass
        reports ``UNCHANGED`` for every record, same as
        :func:`apply_sidecars`).
        """
        apply_sidecars_with_plan(
            self.reports_root,
            self.summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        second = apply_sidecars_with_plan(
            self.reports_root,
            self.summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        for outcome in second.outcomes:
            self.assertEqual(
                outcome.decision,
                SidecarDecision.UNCHANGED,
                f"task {outcome.task_id} should be UNCHANGED on second pass; "
                f"got {outcome.decision.value}",
            )
        self.assertIsNone(second.plan_input_summary)


class TestApplyWithPlanAdditiveSummary(unittest.TestCase):
    """When a real ``AEE_7_7d_7e_MANIFEST.json`` is supplied, the
    wrapper attaches an :class:`ApplyWithPlanSummary` whose
    ``plan_input_count`` matches the real manifest, and the
    underlying apply pass still runs (same outcomes as the
    no-manifest path).
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee78k25-additive-"))
        self.reports_root, self.summary = _build_corpus(self._tmp)
        if not _REAL_MANIFEST.exists():
            self.skipTest(f"real manifest not found: {_REAL_MANIFEST}")

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_real_manifest_produces_additive_summary(self) -> None:
        """A real K1 manifest at the repo root produces a
        :class:`ApplyWithPlanSummary` with the same
        ``plan_input_count`` as a direct call to
        :func:`manifest_to_plan_inputs` on the same doc.
        """
        result = apply_sidecars_with_plan(
            self.reports_root,
            self.summary,
            manifest_path=_REAL_MANIFEST,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        self.assertIsNotNone(result.plan_input_summary)

        # Cross-verify against a direct call to the adapter.
        doc = load_manifest(_REAL_MANIFEST)
        projection = manifest_to_plan_inputs(doc)

        self.assertEqual(
            result.plan_input_summary.plan_input_count,
            len(projection.plan_inputs),
        )
        self.assertTrue(result.plan_input_summary.projection_passed)
        # The real manifest should be warning-free (the loader
        # is forgiving and the validator is strict-but-advisory).
        # At minimum, projection_warning_count should equal the
        # direct call's warning count.
        self.assertEqual(
            result.plan_input_summary.projection_warning_count,
            len(projection.warnings),
        )

    def test_real_manifest_summary_records_manifest_fingerprint(self) -> None:
        """The additive summary records the manifest's on-disk
        SHA-256 + size + source path. These match a direct
        call to :func:`load_manifest` on the same path.
        """
        result = apply_sidecars_with_plan(
            self.reports_root,
            self.summary,
            manifest_path=_REAL_MANIFEST,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        doc = load_manifest(_REAL_MANIFEST)
        self.assertEqual(
            result.plan_input_summary.manifest_source_path,
            doc.source_path,
        )
        self.assertEqual(
            result.plan_input_summary.manifest_on_disk_sha256,
            doc.on_disk_sha256,
        )
        self.assertEqual(
            result.plan_input_summary.manifest_on_disk_size,
            doc.on_disk_size,
        )
        self.assertEqual(
            result.plan_input_summary.schema_version,
            PLAN_APPLY_SCHEMA_VERSION,
        )

    def test_apply_pass_still_runs_with_manifest(self) -> None:
        """Supplying a manifest does NOT short-circuit the
        apply pass — the sidecar writers still write
        identity.json next to every classified task.json, and
        the by_decision / by_record_kind counts are
        non-zero.
        """
        result = apply_sidecars_with_plan(
            self.reports_root,
            self.summary,
            manifest_path=_REAL_MANIFEST,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        # At least one sidecar must have been written
        # (both RUNTIME and FIXTURE records are consistent
        # and allow_runtime=True).
        self.assertGreaterEqual(result.sidecars_written, 1)
        # WROTE and/or UNCHANGED should be present in
        # by_decision. We don't assert the exact value
        # because the second run (here) reports UNCHANGED
        # for everything since sidecars from the test
        # fixture's previous run would be... wait, we just
        # built a fresh tmpdir, so first-run should be
        # WROTE.
        self.assertIn(
            SidecarDecision.WROTE.value,
            result.by_decision,
        )
        # plan_input_summary is NOT None.
        self.assertIsNotNone(result.plan_input_summary)


class TestApplyWithPlanBadManifestPropagates(unittest.TestCase):
    """When the caller supplies a missing / non-JSON
    ``manifest_path``, the transport-level error is raised
    (NOT silently swallowed) because the path is an explicit
    user-supplied input.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee78k25-badmanifest-"))
        self.reports_root, self.summary = _build_corpus(self._tmp)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_missing_manifest_path_raises_manifest_error(self) -> None:
        """A non-existent ``manifest_path`` raises
        :class:`ManifestError` (the loader's transport-level
        failure). It is NOT silently swallowed — the path
        is an explicit user-supplied input.

        Resolves ``ManifestError`` fresh from
        ``aee.audit.manifest`` so the assertion is robust
        against ``sys.modules`` cache invalidation done by
        other test modules (e.g.
        ``test_aee77_apply_sidecars.TestApplySidecarsNoDispatcherImport``
        which ``del sys.modules``-clears the audit package
        to take a clean ``sys.modules`` snapshot).
        """
        from aee.audit import manifest as _manifest_mod
        with self.assertRaises(_manifest_mod.ManifestError):
            apply_sidecars_with_plan(
                self.reports_root,
                self.summary,
                manifest_path=self._tmp / "does-not-exist.json",
                utc_stamp=_FIXTURE_UTC_STAMP,
                classified_at_override=_FIXTURE_UTC_STAMP,
                allow_runtime=True,
            )

    def test_non_json_manifest_path_raises_manifest_error(self) -> None:
        """A file that exists but is not valid JSON raises
        :class:`ManifestError` (same transport-level
        contract).

        See the sys.modules cache-invalidation note on
        :meth:`test_missing_manifest_path_raises_manifest_error`.
        """
        from aee.audit import manifest as _manifest_mod
        bad = self._tmp / "bad.json"
        bad.write_text("this is not json {{{", encoding="utf-8")
        with self.assertRaises(_manifest_mod.ManifestError):
            apply_sidecars_with_plan(
                self.reports_root,
                self.summary,
                manifest_path=bad,
                utc_stamp=_FIXTURE_UTC_STAMP,
                classified_at_override=_FIXTURE_UTC_STAMP,
                allow_runtime=True,
            )


class TestApplyWithPlanValidationFailureIsAdditive(unittest.TestCase):
    """When the manifest loads but fails validation, the
    :class:`ApplyWithPlanSummary` reports
    ``projection_passed=False`` and the apply pass still runs
    (no gating).
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee78k25-valfail-"))
        self.reports_root, self.summary = _build_corpus(self._tmp)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_validation_failure_records_projection_passed_false(self) -> None:
        """A manifest that loads but fails validation
        (missing required top-level keys) produces an
        :class:`ApplyWithPlanSummary` with
        ``projection_passed=False`` and
        ``projection_warning_count > 0``. The apply pass
        STILL RUNS (no gating)."""
        # Build a manifest that loads but fails validation:
        # missing the required top-level "groups" key.
        bad_manifest = self._tmp / "bad.json"
        bad_manifest.write_text(
            json.dumps({"generated_utc": "2026-07-12T18:00:00Z"}),
            encoding="utf-8",
        )

        result = apply_sidecars_with_plan(
            self.reports_root,
            self.summary,
            manifest_path=bad_manifest,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        self.assertIsNotNone(result.plan_input_summary)
        self.assertFalse(result.plan_input_summary.projection_passed)
        self.assertEqual(result.plan_input_summary.plan_input_count, 0)
        self.assertGreater(
            result.plan_input_summary.projection_warning_count,
            0,
            "validation failure should surface at least one warning",
        )
        # The apply pass still ran.
        self.assertGreaterEqual(result.sidecars_written, 1)


class TestApplyWithPlanNoDispatcherImport(unittest.TestCase):
    """The wrapper module does not introduce ``dispatcher.*``
    into ``sys.modules`` when imported (the K1
    import-isolation contract is preserved).
    """

    def setUp(self) -> None:
        # Snapshot the pre-import module set so the test does
        # not depend on test execution order.
        self._pre = set(sys.modules.keys())

    def test_no_dispatcher_import_in_apply_sidecars_module(self) -> None:
        # Force a fresh import to make sure the wrapper's
        # module is loaded (the lazy import inside
        # apply_sidecars_with_plan only triggers on a
        # manifest_path != None call, so importing the
        # module does not pull the manifest module either).
        import aee.audit.apply_sidecars  # noqa: F401
        dispatcher_like = [
            name
            for name in sys.modules.keys()
            if name == "dispatcher" or name.startswith("dispatcher.")
        ]
        # If dispatcher was already loaded by some earlier
        # test in the suite, that's NOT a regression — we
        # only assert it was not introduced BY THIS IMPORT.
        new_dispatcher_like = [
            name for name in dispatcher_like if name not in self._pre
        ]
        self.assertEqual(
            new_dispatcher_like,
            [],
            f"aee.audit.apply_sidecars import introduced "
            f"dispatcher modules: {new_dispatcher_like}",
        )

    def test_no_dispatcher_import_after_opt_in_call(self) -> None:
        """An opt-in call (with manifest_path set) also does
        NOT pull ``dispatcher.*`` into ``sys.modules`` — the
        lazy import of ``aee.audit.manifest`` is
        dispatcher-free, so the K1 isolation contract is
        preserved through the wire-up too.
        """
        pre = set(sys.modules.keys())
        if not _REAL_MANIFEST.exists():
            self.skipTest(f"real manifest not found: {_REAL_MANIFEST}")
        tmp = Path(tempfile.mkdtemp(prefix="aee78k25-noimp-"))
        try:
            reports_root, summary = _build_corpus(tmp)
            apply_sidecars_with_plan(
                reports_root,
                summary,
                manifest_path=_REAL_MANIFEST,
                utc_stamp=_FIXTURE_UTC_STAMP,
                classified_at_override=_FIXTURE_UTC_STAMP,
                allow_runtime=True,
            )
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

        new_dispatcher_like = [
            name
            for name in sys.modules.keys()
            if (name == "dispatcher" or name.startswith("dispatcher."))
            and name not in pre
        ]
        self.assertEqual(
            new_dispatcher_like,
            [],
            f"apply_sidecars_with_plan opt-in call introduced "
            f"dispatcher modules: {new_dispatcher_like}",
        )


class TestApplyWithPlanReExports(unittest.TestCase):
    """The new symbols are re-exported from ``aee.audit`` and
    in the package ``__all__``.
    """

    def test_reexports_resolve_to_apply_sidecars_module(self) -> None:
        import aee.audit as audit_pkg
        # Use assertEqual (not assertIs) on the module string —
        # assertIs on a string compares object identity, which
        # is not guaranteed even for equal interned strings.
        self.assertEqual(
            audit_pkg.apply_sidecars_with_plan.__module__,
            "aee.audit.apply_sidecars",
        )
        self.assertEqual(
            audit_pkg.ApplyWithPlanSummary.__module__,
            "aee.audit.apply_sidecars",
        )
        # Object identity IS safe to compare for the re-export
        # itself (Python preserves the same function object
        # through ``from X import Y``).
        from aee.audit.apply_sidecars import apply_sidecars_with_plan as _orig
        self.assertIs(
            _orig,
            audit_pkg.apply_sidecars_with_plan,
        )
        # The constant lives in the same module.
        self.assertEqual(
            audit_pkg.PLAN_APPLY_SCHEMA_VERSION,
            "1.0.0",
        )

    def test_new_symbols_in_audit_all(self) -> None:
        import aee.audit as audit_pkg
        for sym in (
            "apply_sidecars_with_plan",
            "ApplyWithPlanSummary",
            "PLAN_APPLY_SCHEMA_VERSION",
        ):
            self.assertIn(
                sym,
                audit_pkg.__all__,
                f"new symbol {sym!r} missing from aee.audit.__all__",
            )

    def test_apply_with_plan_summary_dto_shape(self) -> None:
        """The :class:`ApplyWithPlanSummary` DTO exposes the
        K2.5 wire-up's contract fields. Locked here so
        future K3+ slices don't drift the field set.
        """
        from dataclasses import fields
        names = {f.name for f in fields(ApplyWithPlanSummary)}
        self.assertEqual(
            names,
            {
                "schema_version",
                "manifest_source_path",
                "manifest_on_disk_sha256",
                "manifest_on_disk_size",
                "plan_input_count",
                "projection_passed",
                "projection_warning_count",
            },
        )

    def test_apply_with_plan_summary_to_dict_keys(self) -> None:
        s = ApplyWithPlanSummary(
            schema_version="1.0.0",
            manifest_source_path="/tmp/x.json",
            manifest_on_disk_sha256="0" * 64,
            manifest_on_disk_size=42,
            plan_input_count=3,
            projection_passed=True,
            projection_warning_count=0,
        )
        d = s.to_dict()
        self.assertEqual(set(d.keys()), {
            "schema_version",
            "manifest_source_path",
            "manifest_on_disk_sha256",
            "manifest_on_disk_size",
            "plan_input_count",
            "projection_passed",
            "projection_warning_count",
        })
        self.assertEqual(d["plan_input_count"], 3)
        self.assertTrue(d["projection_passed"])


if __name__ == "__main__":
    unittest.main()
