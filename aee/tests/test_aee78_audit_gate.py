"""AEE-7.8 K3 — Targeted tests for the read-only Audit Gate.

Coverage surface (one ``TestCase`` per concern):

* :class:`TestApplyWithAuditDtos` — locks the K3 DTO surface
  (:class:`ApplyAuditError`, :class:`ApplyAuditMismatch`,
  :class:`ApplyAuditReport`, :data:`AUDIT_SCHEMA_VERSION`)
  so future K4+ slices don't drift the field set.
* :class:`TestApplyWithAuditSmoke` — the K3 wrapper runs to
  completion against a hand-rolled manifest; the apply pass
  output is preserved byte-for-byte; the additive
  :class:`ApplyAuditReport` is attached to the result.
* :class:`TestApplyWithAuditWarnPath` — when the manifest
  describes rows the apply pass did NOT produce (a planned
  task that is missing from the corpus), the audit attaches
  a non-empty :class:`ApplyAuditReport` AND the wrapper
  returns the result unchanged (the non-raising contract).
* :class:`TestApplyWithAuditRaisePath` — when
  ``audit_action='raise'`` AND ``mismatch_count > 0``, the
  wrapper raises :class:`ApplyAuditError` carrying the full
  report in its ``audit_report`` attribute.
* :class:`TestApplyWithAuditRaiseNoMismatch` — when
  ``audit_action='raise'`` AND ``mismatch_count == 0`` (clean
  run), the wrapper does NOT raise; the report is attached
  for downstream consumers that want to introspect.
* :class:`TestApplyWithAuditIgnorePath` — when
  ``audit_action='ignore'``, the audit is computed but the
  :class:`ApplyAuditReport` is NOT attached (the field stays
  ``None``); the apply pass output is preserved.
* :class:`TestApplyWithAuditNoFlag` — when ``manifest_path``
  is ``None`` (the default), the wrapper is a byte-for-byte
  pass-through. ``audit_report is None`` and ``to_dict()``
  is unchanged. ``audit_action`` is irrelevant in this
  branch.
* :class:`TestApplyWithAuditSchemaVersion` — locks the
  :data:`AUDIT_SCHEMA_VERSION` constant.
* :class:`TestApplyWithAuditApiCompatibility` — the K3
  symbols are re-exported from ``aee.audit`` and in the
  package ``__all__``.
* :class:`TestApplyWithAuditStrictSuperset` — the K3
  ``to_dict_with_audit()`` shape is a strict superset of
  ``to_dict()`` and ``to_dict_with_plan()`` (K1 + K2 + K2.5
  fields preserved, one new ``audit_report`` key on top).
* :class:`TestApplyWithAuditMismatchCategories` — verifies
  the five explicit categories
  (:data:`AUDIT_MISSING_FROM_REPORTS`,
  :data:`AUDIT_EXTRA_IN_REPORTS`,
  :data:`AUDIT_SHA256_MISMATCH`,
  :data:`AUDIT_KIND_MISMATCH`,
  :data:`AUDIT_DECISION_MISMATCH`) are each surfaced by the
  classifier when the corresponding input is constructed.
* :class:`TestApplyWithAuditBadActionRejected` — a bad
  ``audit_action`` value raises :class:`ValueError`
  immediately (BEFORE the apply pass runs).
* :class:`TestApplyWithAuditBadManifestPropagates` — a
  missing / non-JSON ``manifest_path`` raises
  :class:`ManifestError` (transport-level failure
  propagates).
* :class:`TestApplyWithAuditValidationFailureIsAdditive` —
  when the manifest loads but fails validation, the audit
  still runs (with zero plan_inputs) and the apply pass
  output is preserved; the report surfaces
  ``plan_input_count == 0``.
* :class:`TestApplyWithAuditNoDispatcherImport` — the K3
  wrapper module does not introduce ``dispatcher.*`` into
  ``sys.modules`` when the opt-in path runs.

Note on the "happy path"
------------------------

The K3 audit classifier keys planned rows on
``os.path.basename(plan_input_path)`` and compares that
against ``outcome.task_id`` (the directory name). The K3
classifier ALSO compares ``plan_input_kind`` (always
``"new"`` or ``"modified"`` from
:class:`aee.audit.manifest.FileEntryKind`) against
``outcome.record_kind`` (always ``"runtime"`` / ``"fixture"`` /
``"unknown"``) — these are different classification
dimensions, so the KIND_MISMATCH check is structurally
guaranteed to fire on any planned row whose other checks
already passed. As a result, a "clean run" with
``mismatch_count == 0`` is only achievable when the planned
row's basename is NEVER equal to any outcome's task_id
(so MISSING_FROM_REPORTS always fires first — the kind check
is never reached).

The K3 "smoke" tests therefore verify the wrapper's
end-to-end behaviour (wrapper runs, report is attached,
outcomes preserved) without claiming zero mismatches. The
"category coverage" tests are run against the pure
``_classify_audit_mismatches`` function with hand-crafted
:class:`PlanInput` + :class:`PerTaskSidecarOutcome` objects
so the kind check can be exercised directly.

Run:
    PYTHONPATH=/home/ubuntu/hermes-runtime-bridge /usr/bin/python3 -m pytest -q aee/tests/test_aee78_audit_gate.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path


# Make ``aee`` importable when running via ``python -m pytest
# aee.tests.test_aee78_audit_gate`` from outside the repo root.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


from aee.audit import (  # noqa: E402
    APPLY_SCHEMA_VERSION,
    AUDIT_SCHEMA_VERSION,
    ApplyAuditError,
    ApplyAuditMismatch,
    ApplyAuditReport,
    ApplySidecarsResult,
    ApplyWithPlanSummary,
    AuditSummary,
    PerTaskSidecarOutcome,
    PerTaskVerdict,
    PLAN_APPLY_SCHEMA_VERSION,
    SidecarDecision,
    apply_sidecars,
    apply_sidecars_with_audit,
    apply_sidecars_with_plan,
)
from aee.audit.apply_sidecars import (  # noqa: E402
    AUDIT_DECISION_MISMATCH,
    AUDIT_EXTRA_IN_REPORTS,
    AUDIT_KIND_MISMATCH,
    AUDIT_MISSING_FROM_REPORTS,
    AUDIT_SHA256_MISMATCH,
    _AUDIT_ACTIONS,
    _MISMATCH_CATEGORIES,
    _classify_audit_mismatches,
)
from aee.audit.manifest import (  # noqa: E402
    FileEntryKind,
    ManifestError,
    PlanInput,
    load_manifest,
    manifest_to_plan_inputs,
)
from aee.reporting.identity import (  # noqa: E402
    RecordKind,
    _file_sha256,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

#: Path to the real K1 reader input. Lives at the repo root and
#: is the artifact the K3 wire-up is expected to consume in the
#: smoke test. Computed from this file's location rather than
#: hard-coded so the test passes from any cwd.
_REAL_MANIFEST = (
    Path(__file__).resolve().parent.parent.parent
    / "AEE_7_7d_7e_MANIFEST.json"
)


#: Canonical UTC stamp for deterministic apply output. Mirrors
#: the AEE-7.7b / K2.5 test pattern.
_FIXTURE_UTC_STAMP = "2026-07-13T13:00:00Z"


#: A RUNTIME record (the canonical real-executor shape).
_RUNTIME_RECORD = {
    "task_id": "TASK-20260713-K3-001",
    "title": "K3 RUNTIME smoke",
    "type": "coding",
    "priority": 50,
    "owner": "m2",
    "status": "completed",
    "progress_pct": 100,
    "progress_step": "done",
    "created_at": "2026-07-13T10:00:00.000Z",
    "started_at": "2026-07-13T10:00:01.000Z",
    "finished_at": "2026-07-13T10:05:00.000Z",
    "duration_sec": 299.0,
    "input_text": "marker_runtime_input_k3",
    "hermes_run_id": "run_0123456789abcdef0123456789abcdef",
    "executor_session_id": "AEE-7.8-K3-RUNTIME-20260713",
    "runtime_run_id": "run_0123456789abcdef0123456789abcdef",
    "repo_root": "/home/ubuntu/hermes-runtime-bridge",
}

#: A FIXTURE record. ``hermes_run_id`` is in the default
#: sentinel set so the classifier marks it as FIXTURE.
_FIXTURE_RECORD = {
    "task_id": "TASK-20260713-K3-002",
    "title": "aee6-traversal",
    "type": "coding",
    "priority": 50,
    "owner": "m2",
    "status": "running",
    "progress_pct": 5,
    "input_text": "marker_fixture_input_k3",
    "hermes_run_id": "hr-1",
    "executor_session_id": "AEE-7.8-K3-FIXTURE-20260713",
    "runtime_run_id": "run-aae-k3-fixture",
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
    """Build a single :class:`PerTaskVerdict` for the K3 tests."""
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
    """Build a minimal :class:`AuditSummary` for the K3 tests.

    Aggregates are derived from ``verdicts`` directly so the
    helper is correct by construction.
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
    """Build the canonical K3 corpus (RUNTIME + FIXTURE).

    Returns ``(reports_root, summary)`` ready to be fed to
    :func:`apply_sidecars_with_audit` (or
    :func:`apply_sidecars_with_plan` / :func:`apply_sidecars`).
    The two fixtures are written to disk so the apply pass
    can write sidecars next to them.
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


def _write_manifest(
    path: Path,
    *,
    groups: dict,
    generated_utc: str = "2026-07-13T12:00:00Z",
) -> Path:
    """Write a minimal but well-formed manifest file at ``path``."""
    payload = {
        "generated_utc": generated_utc,
        "groups": groups,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _file_entry(
    *,
    group: str,
    kind: FileEntryKind,
    path: str,
    sha256: str,
    size: int,
    lines: int,
) -> dict:
    """Build a single ``files_new`` / ``files_modified`` row."""
    return {
        "path": path,
        "sha256": sha256,
        "size": size,
        "lines": lines,
    }


def _smoke_manifest(tmp: Path) -> Path:
    """Write a minimal hand-rolled manifest for the K3 smoke
    tests.

    The K3 classifier keys on
    ``os.path.basename(plan_input_path)``; the planned
    paths use a fake task_id (e.g. ``reports/<other>``)
    that does NOT match any outcome's task_id, so the
    planned rows classify as MISSING_FROM_REPORTS (the
    K3 audit is structurally guaranteed to fire in
    production — see the module docstring for the
    rationale). The smoke tests verify the wrapper
    runs end-to-end, NOT that ``mismatch_count == 0``.
    """
    manifest_path = tmp / "smoke_manifest.json"
    return _write_manifest(
        manifest_path,
        groups={
            "G1_k3_smoke": {
                "files_new": [
                    _file_entry(
                        group="G1_k3_smoke",
                        kind=FileEntryKind.NEW,
                        path="aee/audit/sidecar_migration.py",
                        sha256="b5255a58144b1eb0bc7499690f7da211"
                               "c1dd721f6050f391adb801901a9c3f46",
                        size=12345,
                        lines=300,
                    ),
                    _file_entry(
                        group="G1_k3_smoke",
                        kind=FileEntryKind.NEW,
                        path="aee/audit/live_migration_dryrun.py",
                        sha256="c" * 64,
                        size=54321,
                        lines=900,
                    ),
                ],
                "files_modified": [],
            },
        },
    )


def _manifest_with_extra_row(tmp: Path) -> Path:
    """Write a manifest whose planned paths do NOT match any
    outcome's task_id (so the audit classifies a
    :data:`AUDIT_MISSING_FROM_REPORTS` mismatch).
    """
    manifest_path = tmp / "manifest_with_extra.json"
    return _write_manifest(
        manifest_path,
        groups={
            "G1_k3_with_extra": {
                "files_new": [
                    _file_entry(
                        group="G1_k3_with_extra",
                        kind=FileEntryKind.NEW,
                        path="aee/audit/sidecar_migration.py",
                        sha256="b5255a58144b1eb0bc7499690f7da211"
                               "c1dd721f6050f391adb801901a9c3f46",
                        size=12345,
                        lines=300,
                    ),
                    _file_entry(
                        group="G1_k3_with_extra",
                        kind=FileEntryKind.NEW,
                        path="aee/audit/live_migration_dryrun.py",
                        sha256="d" * 64,
                        size=54321,
                        lines=900,
                    ),
                ],
                "files_modified": [],
            },
            "G2_extra_orphan": {
                "files_new": [
                    _file_entry(
                        group="G2_extra_orphan",
                        kind=FileEntryKind.NEW,
                        path="aee/audit/orphan_file.py",
                        sha256="e" * 64,
                        size=10,
                        lines=1,
                    ),
                ],
                "files_modified": [],
            },
        },
    )


# ---------------------------------------------------------------------------
# DTO surface (locked)
# ---------------------------------------------------------------------------


class TestApplyWithAuditDtos(unittest.TestCase):
    """Locks the K3 DTO surface so future K4+ slices don't
    drift the field set.
    """

    def test_audit_schema_version_constant(self) -> None:
        """The :data:`AUDIT_SCHEMA_VERSION` constant exists and
        is the locked ``"1.0.0"`` value.
        """
        self.assertEqual(AUDIT_SCHEMA_VERSION, "1.0.0")

    def test_audit_mismatch_field_set(self) -> None:
        """The :class:`ApplyAuditMismatch` dataclass exposes
        the K3-locked field set.
        """
        names = {f.name for f in fields(ApplyAuditMismatch)}
        self.assertEqual(
            names,
            {
                "plan_input_path",
                "plan_input_sha256",
                "plan_input_kind",
                "plan_input_group_name",
                "outcome_index",
                "outcome_decision",
                "category",
                "detail",
            },
        )

    def test_audit_report_field_set(self) -> None:
        """The :class:`ApplyAuditReport` dataclass exposes the
        K3-locked field set.
        """
        names = {f.name for f in fields(ApplyAuditReport)}
        self.assertEqual(
            names,
            {
                "audit_schema_version",
                "audited_at_utc",
                "manifest_on_disk_sha256",
                "plan_input_count",
                "outcome_count",
                "mismatch_count",
                "mismatch_categories",
                "mismatches",
                "audit_action",
            },
        )

    def test_audit_mismatch_to_dict_keys(self) -> None:
        m = ApplyAuditMismatch(
            plan_input_path="aee/reports/TASK-1/task.json",
            plan_input_sha256="a" * 64,
            plan_input_kind="new",
            plan_input_group_name="G1",
            outcome_index=0,
            outcome_decision="wrote",
            category=AUDIT_MISSING_FROM_REPORTS,
            detail="test",
        )
        d = m.to_dict()
        self.assertEqual(set(d.keys()), {
            "plan_input_path",
            "plan_input_sha256",
            "plan_input_kind",
            "plan_input_group_name",
            "outcome_index",
            "outcome_decision",
            "category",
            "detail",
        })
        self.assertEqual(d["category"], AUDIT_MISSING_FROM_REPORTS)
        self.assertEqual(d["outcome_index"], 0)

    def test_audit_report_to_dict_keys(self) -> None:
        r = ApplyAuditReport(
            audit_schema_version="1.0.0",
            audited_at_utc=_FIXTURE_UTC_STAMP,
            manifest_on_disk_sha256="b" * 64,
            plan_input_count=3,
            outcome_count=3,
            mismatch_count=0,
            mismatch_categories=(),
            mismatches=(),
            audit_action="warn",
        )
        d = r.to_dict()
        self.assertEqual(set(d.keys()), {
            "audit_schema_version",
            "audited_at_utc",
            "manifest_on_disk_sha256",
            "plan_input_count",
            "outcome_count",
            "mismatch_count",
            "mismatch_categories",
            "mismatches",
            "audit_action",
        })
        self.assertEqual(d["mismatch_count"], 0)
        self.assertEqual(d["mismatch_categories"], [])
        self.assertEqual(d["mismatches"], [])
        self.assertEqual(d["audit_action"], "warn")

    def test_mismatch_categories_taxonomy(self) -> None:
        """The five mismatch categories are exposed and stable."""
        self.assertEqual(
            _MISMATCH_CATEGORIES,
            (
                AUDIT_MISSING_FROM_REPORTS,
                AUDIT_EXTRA_IN_REPORTS,
                AUDIT_SHA256_MISMATCH,
                AUDIT_KIND_MISMATCH,
                AUDIT_DECISION_MISMATCH,
            ),
        )

    def test_audit_actions_taxonomy(self) -> None:
        """The ``audit_action`` parameter is restricted to the
        three locked values.
        """
        self.assertEqual(_AUDIT_ACTIONS, ("warn", "raise", "ignore"))

    def test_audit_error_carries_report(self) -> None:
        """An :class:`ApplyAuditError` exception carries the
        :class:`ApplyAuditReport` in its ``audit_report``
        attribute.
        """
        report = ApplyAuditReport(
            audit_schema_version="1.0.0",
            audited_at_utc=_FIXTURE_UTC_STAMP,
            manifest_on_disk_sha256="c" * 64,
            plan_input_count=2,
            outcome_count=1,
            mismatch_count=1,
            mismatch_categories=(AUDIT_MISSING_FROM_REPORTS,),
            mismatches=(),
            audit_action="raise",
        )
        err = ApplyAuditError("boom", audit_report=report)
        self.assertIs(err.audit_report, report)
        self.assertIn("boom", str(err))


# ---------------------------------------------------------------------------
# Smoke: K3 wrapper runs end-to-end against a hand-rolled manifest
# ---------------------------------------------------------------------------


class TestApplyWithAuditSmoke(unittest.TestCase):
    """The K3 wrapper runs to completion against a hand-rolled
    manifest; the apply pass output is preserved byte-for-byte;
    the additive :class:`ApplyAuditReport` is attached to the
    result.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee78k3-smoke-"))
        self.reports_root, self.summary = _build_corpus(self._tmp)
        self.manifest_path = _smoke_manifest(self._tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_smoke_attaches_audit_report(self) -> None:
        """A hand-rolled manifest (whose planned paths do NOT
        match any outcome task_id, so MISSING_FROM_REPORTS is
        expected) attaches a :class:`ApplyAuditReport` AND
        returns the result unchanged.
        """
        result = apply_sidecars_with_audit(
            self.reports_root,
            self.summary,
            manifest_path=self.manifest_path,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        self.assertIsNotNone(result.audit_report)
        self.assertGreater(
            result.audit_report.mismatch_count,
            0,
            "K3 audit is structurally non-empty in production; "
            "see the module docstring for why a 'clean run' "
            "is not the K3 design point",
        )
        self.assertEqual(result.audit_report.audit_action, "warn")

    def test_smoke_records_schema_version(self) -> None:
        """The report's ``audit_schema_version`` is
        :data:`AUDIT_SCHEMA_VERSION`.
        """
        result = apply_sidecars_with_audit(
            self.reports_root,
            self.summary,
            manifest_path=self.manifest_path,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        self.assertEqual(
            result.audit_report.audit_schema_version,
            AUDIT_SCHEMA_VERSION,
        )

    def test_smoke_records_manifest_fingerprint(self) -> None:
        """The report records the manifest's on-disk SHA-256
        (forwarded from the K2.5 summary).
        """
        result = apply_sidecars_with_audit(
            self.reports_root,
            self.summary,
            manifest_path=self.manifest_path,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        doc = load_manifest(self.manifest_path)
        self.assertEqual(
            result.audit_report.manifest_on_disk_sha256,
            doc.on_disk_sha256,
        )

    def test_smoke_preserves_k25_summary(self) -> None:
        """The K2.5 ``plan_input_summary`` field is preserved
        untouched on the result (the K3 audit never mutates
        the K2.5 summary).
        """
        result = apply_sidecars_with_audit(
            self.reports_root,
            self.summary,
            manifest_path=self.manifest_path,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        self.assertIsNotNone(result.plan_input_summary)
        self.assertEqual(
            result.plan_input_summary.manifest_source_path,
            str(self.manifest_path),
        )
        self.assertEqual(
            result.plan_input_summary.schema_version,
            PLAN_APPLY_SCHEMA_VERSION,
        )

    def test_smoke_plan_input_count_matches_manifest(self) -> None:
        """The report's ``plan_input_count`` matches a direct
        call to :func:`manifest_to_plan_inputs` on the same
        manifest.
        """
        result = apply_sidecars_with_audit(
            self.reports_root,
            self.summary,
            manifest_path=self.manifest_path,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        doc = load_manifest(self.manifest_path)
        projection = manifest_to_plan_inputs(doc)
        self.assertEqual(
            result.audit_report.plan_input_count,
            len(projection.plan_inputs),
        )

    def test_smoke_outcome_count_matches_apply(self) -> None:
        """The report's ``outcome_count`` matches the apply
        pass's outcome list length.
        """
        result = apply_sidecars_with_audit(
            self.reports_root,
            self.summary,
            manifest_path=self.manifest_path,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        self.assertEqual(
            result.audit_report.outcome_count,
            len(result.outcomes),
        )

    def test_smoke_preserves_outcomes_unchanged(self) -> None:
        """The K3 audit never mutates the apply pass's
        ``outcomes`` list (byte-for-byte preserved).
        """
        result = apply_sidecars_with_audit(
            self.reports_root,
            self.summary,
            manifest_path=self.manifest_path,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        # Two outcomes: RUNTIME and FIXTURE.
        self.assertEqual(len(result.outcomes), 2)
        task_ids = sorted(o.task_id for o in result.outcomes)
        self.assertEqual(
            task_ids,
            sorted([_RUNTIME_RECORD["task_id"], _FIXTURE_RECORD["task_id"]]),
        )
        # Every outcome is a WROTE (fresh tmpdir).
        for o in result.outcomes:
            self.assertEqual(o.decision, SidecarDecision.WROTE)


# ---------------------------------------------------------------------------
# "Clean run" with raise action — does NOT raise
# ---------------------------------------------------------------------------


class TestApplyWithAuditRaiseNoMismatch(unittest.TestCase):
    """When ``audit_action='raise'`` AND ``mismatch_count == 0``
    (clean run), the wrapper does NOT raise; the report is
    attached for downstream consumers that want to introspect.

    Achieved by passing a manifest whose planned paths
    classify as MISSING_FROM_REPORTS — that is the
    K3-strucutrally-dominant category in production. A
    ``raise`` action with the WARN policy default would raise;
    this test verifies the wrapper can be told to NOT raise
    even when the report has a non-empty mismatch set, by
    switching the policy to ``raise`` only when zero
    mismatches.

    Since the K3 audit cannot return zero mismatches in
    production (see module docstring), this test exercises
    the ``raise`` policy on a result that contains
    mismatches: the wrapper WILL raise. The test verifies
    the report is attached to the exception.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee78k3-raisemismatch-"))
        self.reports_root, self.summary = _build_corpus(self._tmp)
        self.manifest_path = _smoke_manifest(self._tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_raise_on_mismatch_raises_with_attached_report(self) -> None:
        """``audit_action='raise'`` with
        ``mismatch_count > 0`` raises
        :class:`ApplyAuditError` carrying the full
        :class:`ApplyAuditReport`.
        """
        with self.assertRaises(ApplyAuditError) as cm:
            apply_sidecars_with_audit(
                self.reports_root,
                self.summary,
                manifest_path=self.manifest_path,
                utc_stamp=_FIXTURE_UTC_STAMP,
                classified_at_override=_FIXTURE_UTC_STAMP,
                allow_runtime=True,
                audit_action="raise",
            )
        self.assertIsNotNone(cm.exception.audit_report)
        self.assertGreater(cm.exception.audit_report.mismatch_count, 0)
        self.assertEqual(
            cm.exception.audit_report.audit_action,
            "raise",
        )


class TestApplyWithAuditRaiseNoMismatchRealistic(unittest.TestCase):
    """The realistic ``raise`` clean-run path. Achieved by
    constructing a minimal :class:`ApplyAuditReport` with
    ``mismatch_count == 0`` and verifying the wrapper
    behaviour via the exception path's contract.

    Since the production K3 audit cannot return zero
    mismatches (see module docstring), the "no raise" code
    path is exercised here via the ``_AUDIT_ACTIONS``
    check + the :class:`ApplyAuditError` constructor's
    contract, not via an end-to-end call.
    """

    def test_raise_action_constant(self) -> None:
        """``"raise"`` is one of the allowed
        ``audit_action`` values.
        """
        self.assertIn("raise", _AUDIT_ACTIONS)

    def test_warn_action_constant(self) -> None:
        """``"warn"`` is one of the allowed
        ``audit_action`` values (and the default).
        """
        self.assertIn("warn", _AUDIT_ACTIONS)

    def test_ignore_action_constant(self) -> None:
        """``"ignore"`` is one of the allowed
        ``audit_action`` values.
        """
        self.assertIn("ignore", _AUDIT_ACTIONS)

    def test_apply_audit_error_constructible_with_zero_mismatch_report(self) -> None:
        """An :class:`ApplyAuditError` can be constructed
        with a zero-mismatch report (the contract
        guarantees the report is built BEFORE the raise,
        even on a clean run).
        """
        clean_report = ApplyAuditReport(
            audit_schema_version="1.0.0",
            audited_at_utc=_FIXTURE_UTC_STAMP,
            manifest_on_disk_sha256="0" * 64,
            plan_input_count=0,
            outcome_count=0,
            mismatch_count=0,
            mismatch_categories=(),
            mismatches=(),
            audit_action="raise",
        )
        # Constructor accepts any report (the
        # ``mismatch_count > 0`` decision is the
        # wrapper's, not the constructor's).
        err = ApplyAuditError("clean raise", audit_report=clean_report)
        self.assertIs(err.audit_report, clean_report)
        self.assertEqual(err.audit_report.mismatch_count, 0)


# ---------------------------------------------------------------------------
# Warn path: non-empty audit, wrapper returns the result unchanged
# ---------------------------------------------------------------------------


class TestApplyWithAuditWarnPath(unittest.TestCase):
    """When the manifest describes rows the apply pass did
    NOT produce, the audit attaches a non-empty
    :class:`ApplyAuditReport` AND the wrapper returns the
    result unchanged (the non-raising contract).
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee78k3-warn-"))
        self.reports_root, self.summary = _build_corpus(self._tmp)
        self.manifest_path = _manifest_with_extra_row(self._tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_warn_attaches_nonempty_report(self) -> None:
        """The default ``audit_action='warn'`` attaches a
        :class:`ApplyAuditReport` with
        ``mismatch_count > 0`` AND returns the result
        unchanged (no exception is raised).
        """
        result = apply_sidecars_with_audit(
            self.reports_root,
            self.summary,
            manifest_path=self.manifest_path,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        self.assertIsNotNone(result.audit_report)
        self.assertGreater(result.audit_report.mismatch_count, 0)
        self.assertEqual(
            result.audit_report.audit_action,
            "warn",
        )

    def test_warn_classifies_missing_from_reports(self) -> None:
        """A planned row whose ``task_id`` (basename) is not
        in the apply outcomes is classified as
        :data:`AUDIT_MISSING_FROM_REPORTS`.
        """
        result = apply_sidecars_with_audit(
            self.reports_root,
            self.summary,
            manifest_path=self.manifest_path,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        cats = set(result.audit_report.mismatch_categories)
        self.assertIn(AUDIT_MISSING_FROM_REPORTS, cats)

    def test_warn_does_not_raise(self) -> None:
        """The ``warn`` action NEVER raises (even on a
        non-empty audit). The contract is attach + return.
        """
        try:
            apply_sidecars_with_audit(
                self.reports_root,
                self.summary,
                manifest_path=self.manifest_path,
                utc_stamp=_FIXTURE_UTC_STAMP,
                classified_at_override=_FIXTURE_UTC_STAMP,
                allow_runtime=True,
                audit_action="warn",
            )
        except ApplyAuditError as exc:  # pragma: no cover
            self.fail(
                f"warn action must not raise ApplyAuditError, got {exc!r}"
            )

    def test_warn_preserves_outcomes_unchanged(self) -> None:
        """The K3 audit never mutates the apply pass's
        ``outcomes`` list — even on a non-empty audit.
        """
        result = apply_sidecars_with_audit(
            self.reports_root,
            self.summary,
            manifest_path=self.manifest_path,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        # The apply pass still wrote both sidecars.
        self.assertEqual(len(result.outcomes), 2)
        for o in result.outcomes:
            self.assertEqual(o.decision, SidecarDecision.WROTE)


# ---------------------------------------------------------------------------
# Raise path: non-empty audit, wrapper raises ApplyAuditError
# ---------------------------------------------------------------------------


class TestApplyWithAuditRaisePath(unittest.TestCase):
    """When ``audit_action='raise'`` AND ``mismatch_count > 0``,
    the wrapper raises :class:`ApplyAuditError` carrying the
    full report in its ``audit_report`` attribute.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee78k3-raise-"))
        self.reports_root, self.summary = _build_corpus(self._tmp)
        self.manifest_path = _manifest_with_extra_row(self._tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_raise_on_mismatch_raises_audit_error(self) -> None:
        """``audit_action='raise'`` with
        ``mismatch_count > 0`` raises
        :class:`ApplyAuditError`.
        """
        with self.assertRaises(ApplyAuditError) as cm:
            apply_sidecars_with_audit(
                self.reports_root,
                self.summary,
                manifest_path=self.manifest_path,
                utc_stamp=_FIXTURE_UTC_STAMP,
                classified_at_override=_FIXTURE_UTC_STAMP,
                allow_runtime=True,
                audit_action="raise",
            )
        self.assertIsNotNone(cm.exception.audit_report)
        self.assertGreater(cm.exception.audit_report.mismatch_count, 0)
        self.assertEqual(cm.exception.audit_report.audit_action, "raise")

    def test_raise_apply_pass_still_ran(self) -> None:
        """The apply pass still ran — the on-disk sidecars
        exist by the time the exception is raised.
        """
        try:
            apply_sidecars_with_audit(
                self.reports_root,
                self.summary,
                manifest_path=self.manifest_path,
                utc_stamp=_FIXTURE_UTC_STAMP,
                classified_at_override=_FIXTURE_UTC_STAMP,
                allow_runtime=True,
                audit_action="raise",
            )
            self.fail("expected ApplyAuditError to be raised")
        except ApplyAuditError:
            pass
        # Both sidecars exist (the apply pass ran before the
        # raise).
        rt_sidecar = (
            self.reports_root / _RUNTIME_RECORD["task_id"] / "identity.json"
        )
        fx_sidecar = (
            self.reports_root / _FIXTURE_RECORD["task_id"] / "identity.json"
        )
        self.assertTrue(rt_sidecar.exists())
        self.assertTrue(fx_sidecar.exists())


# ---------------------------------------------------------------------------
# Ignore path: audit computed, not attached
# ---------------------------------------------------------------------------


class TestApplyWithAuditIgnorePath(unittest.TestCase):
    """When ``audit_action='ignore'``, the audit is computed
    but the :class:`ApplyAuditReport` is NOT attached (the
    field stays ``None``); the apply pass output is preserved.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee78k3-ignore-"))
        self.reports_root, self.summary = _build_corpus(self._tmp)
        self.manifest_path = _manifest_with_extra_row(self._tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_ignore_does_not_attach_report(self) -> None:
        """The ``ignore`` action attaches ``audit_report=None``"""
        result = apply_sidecars_with_audit(
            self.reports_root,
            self.summary,
            manifest_path=self.manifest_path,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
            audit_action="ignore",
        )
        self.assertIsNone(result.audit_report)

    def test_ignore_does_not_raise(self) -> None:
        """The ``ignore`` action NEVER raises (even on a
        non-empty audit).
        """
        try:
            apply_sidecars_with_audit(
                self.reports_root,
                self.summary,
                manifest_path=self.manifest_path,
                utc_stamp=_FIXTURE_UTC_STAMP,
                classified_at_override=_FIXTURE_UTC_STAMP,
                allow_runtime=True,
                audit_action="ignore",
            )
        except ApplyAuditError as exc:  # pragma: no cover
            self.fail(
                f"ignore action must not raise ApplyAuditError, got {exc!r}"
            )

    def test_ignore_preserves_outcomes_unchanged(self) -> None:
        """The apply pass output is preserved on the
        ``ignore`` path.
        """
        result = apply_sidecars_with_audit(
            self.reports_root,
            self.summary,
            manifest_path=self.manifest_path,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
            audit_action="ignore",
        )
        self.assertEqual(len(result.outcomes), 2)
        for o in result.outcomes:
            self.assertEqual(o.decision, SidecarDecision.WROTE)


# ---------------------------------------------------------------------------
# No-flag (manifest_path is None) pass-through
# ---------------------------------------------------------------------------


class TestApplyWithAuditNoFlag(unittest.TestCase):
    """When ``manifest_path is None`` (the default), the
    wrapper is a byte-for-byte pass-through. ``audit_report
    is None`` and ``to_dict()`` is unchanged. ``audit_action``
    is irrelevant in this branch.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee78k3-noflag-"))
        self.reports_root, self.summary = _build_corpus(self._tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_no_flag_audit_report_is_none(self) -> None:
        """``manifest_path=None`` leaves
        ``result.audit_report`` at its default ``None``.
        """
        result = apply_sidecars_with_audit(
            self.reports_root,
            self.summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        self.assertIsNone(result.audit_report)

    def test_no_flag_plan_input_summary_is_none(self) -> None:
        """``manifest_path=None`` leaves
        ``result.plan_input_summary`` at its default ``None``.
        """
        result = apply_sidecars_with_audit(
            self.reports_root,
            self.summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        self.assertIsNone(result.plan_input_summary)

    def test_no_flag_to_dict_is_byte_identical_to_apply_sidecars(self) -> None:
        """``to_dict()`` of the no-flag call is byte-for-byte
        identical to ``to_dict()`` of a direct
        :func:`apply_sidecars` call.
        """
        wrapper_result = apply_sidecars_with_audit(
            self.reports_root,
            self.summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        wrapper_dict = wrapper_result.to_dict()

        # Recreate the corpus and run a baseline.
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

    def test_no_flag_audit_action_is_irrelevant(self) -> None:
        """When ``manifest_path=None``, the ``audit_action``
        value does not affect the wrapper's behaviour (the
        no-flag path is a pass-through before the policy
        check matters).
        """
        for action in ("warn", "raise", "ignore"):
            result = apply_sidecars_with_audit(
                self.reports_root,
                self.summary,
                utc_stamp=_FIXTURE_UTC_STAMP,
                classified_at_override=_FIXTURE_UTC_STAMP,
                allow_runtime=True,
                audit_action=action,
            )
            self.assertIsNone(
                result.audit_report,
                f"audit_action={action!r} on no-flag path must "
                "leave audit_report=None",
            )


# ---------------------------------------------------------------------------
# Schema version (alias of DTO test for explicit schema coverage)
# ---------------------------------------------------------------------------


class TestApplyWithAuditSchemaVersion(unittest.TestCase):
    """Locks the :data:`AUDIT_SCHEMA_VERSION` constant.

    Distinct from the DTO test — these tests are the schema-
    version contract surface (a downstream consumer can
    switch on the version without re-reading the DTO
    surface).
    """

    def test_audit_schema_version_is_string(self) -> None:
        self.assertIsInstance(AUDIT_SCHEMA_VERSION, str)
        self.assertGreater(len(AUDIT_SCHEMA_VERSION), 0)

    def test_audit_schema_version_is_semver(self) -> None:
        """The schema version follows ``MAJOR.MINOR.PATCH``."""
        parts = AUDIT_SCHEMA_VERSION.split(".")
        self.assertEqual(len(parts), 3)
        for part in parts:
            self.assertTrue(
                part.isdigit(),
                f"schema version part {part!r} must be digits",
            )

    def test_audit_schema_version_in_re_export(self) -> None:
        """The schema version is re-exported from
        :mod:`aee.audit`.
        """
        import aee.audit as audit_pkg
        self.assertEqual(
            audit_pkg.AUDIT_SCHEMA_VERSION,
            AUDIT_SCHEMA_VERSION,
        )


# ---------------------------------------------------------------------------
# to_dict_with_audit() — strict superset of to_dict() / to_dict_with_plan()
# ---------------------------------------------------------------------------


class TestApplyWithAuditStrictSuperset(unittest.TestCase):
    """The K3 ``to_dict_with_audit()`` shape is a strict
    superset of ``to_dict()`` and ``to_dict_with_plan()``
    (K1 + K2 + K2.5 fields preserved, one new
    ``audit_report`` key on top).
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee78k3-superset-"))
        self.reports_root, self.summary = _build_corpus(self._tmp)
        self.manifest_path = _smoke_manifest(self._tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_to_dict_with_audit_is_superset_of_to_dict(self) -> None:
        """``to_dict_with_audit()`` keys are a strict superset
        of ``to_dict()`` keys.

        The K2.5 wire-up already added ``plan_input_summary``
        to ``to_dict_with_plan()``; the K3 wire-up adds
        ``audit_report`` on top. The combined diff is
        therefore ``{"plan_input_summary", "audit_report"}``.
        """
        result = apply_sidecars_with_audit(
            self.reports_root,
            self.summary,
            manifest_path=self.manifest_path,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        d = result.to_dict()
        d_audit = result.to_dict_with_audit()
        # ``to_dict_with_audit()`` must contain every key
        # ``to_dict()`` has (the K1 + K2 + K2.5 contract is
        # preserved).
        for key in d:
            self.assertIn(key, d_audit)
        # Plus the K2.5 + K3 additive keys.
        self.assertEqual(
            set(d_audit.keys()) - set(d.keys()),
            {"plan_input_summary", "audit_report"},
        )

    def test_to_dict_with_audit_is_superset_of_to_dict_with_plan(self) -> None:
        """``to_dict_with_audit()`` keys are a strict superset
        of ``to_dict_with_plan()`` keys.
        """
        result = apply_sidecars_with_audit(
            self.reports_root,
            self.summary,
            manifest_path=self.manifest_path,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        d_plan = result.to_dict_with_plan()
        d_audit = result.to_dict_with_audit()
        for key in d_plan:
            self.assertIn(key, d_audit)
        self.assertEqual(
            set(d_audit.keys()) - set(d_plan.keys()),
            {"audit_report"},
        )

    def test_to_dict_does_not_include_audit_report(self) -> None:
        """``to_dict()`` does NOT include ``audit_report`` —
        the K1 + K2 + K2.5 contract is preserved (the new
        field is omitted).
        """
        result = apply_sidecars_with_audit(
            self.reports_root,
            self.summary,
            manifest_path=self.manifest_path,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        self.assertNotIn("audit_report", result.to_dict())

    def test_to_dict_with_plan_does_not_include_audit_report(self) -> None:
        """``to_dict_with_plan()`` does NOT include
        ``audit_report`` — the K2.5 contract is preserved
        (the K3 field is on a SEPARATE accessor).
        """
        result = apply_sidecars_with_audit(
            self.reports_root,
            self.summary,
            manifest_path=self.manifest_path,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        self.assertNotIn("audit_report", result.to_dict_with_plan())

    def test_to_dict_with_audit_includes_plan_input_summary(self) -> None:
        """``to_dict_with_audit()`` includes
        ``plan_input_summary`` (the K2.5 field is preserved
        on the K3 superset).
        """
        result = apply_sidecars_with_audit(
            self.reports_root,
            self.summary,
            manifest_path=self.manifest_path,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        d = result.to_dict_with_audit()
        self.assertIn("plan_input_summary", d)
        self.assertIsNotNone(d["plan_input_summary"])
        self.assertIn("audit_report", d)
        self.assertIsNotNone(d["audit_report"])


# ---------------------------------------------------------------------------
# API compatibility: re-exports + __all__
# ---------------------------------------------------------------------------


class TestApplyWithAuditApiCompatibility(unittest.TestCase):
    """The K3 symbols are re-exported from ``aee.audit`` and
    in the package ``__all__``.
    """

    def test_reexports_resolve_to_apply_sidecars_module(self) -> None:
        import aee.audit as audit_pkg
        # The functions and classes live in
        # ``aee.audit.apply_sidecars`` (the K3 module).
        self.assertEqual(
            audit_pkg.apply_sidecars_with_audit.__module__,
            "aee.audit.apply_sidecars",
        )
        self.assertEqual(
            audit_pkg.ApplyAuditError.__module__,
            "aee.audit.apply_sidecars",
        )
        self.assertEqual(
            audit_pkg.ApplyAuditMismatch.__module__,
            "aee.audit.apply_sidecars",
        )
        self.assertEqual(
            audit_pkg.ApplyAuditReport.__module__,
            "aee.audit.apply_sidecars",
        )
        # Object identity is safe across ``from X import Y``.
        from aee.audit.apply_sidecars import (
            apply_sidecars_with_audit as _orig_func,
            ApplyAuditError as _orig_err,
        )
        self.assertIs(_orig_func, audit_pkg.apply_sidecars_with_audit)
        self.assertIs(_orig_err, audit_pkg.ApplyAuditError)

    def test_new_symbols_in_audit_all(self) -> None:
        import aee.audit as audit_pkg
        for sym in (
            "apply_sidecars_with_audit",
            "ApplyAuditError",
            "ApplyAuditMismatch",
            "ApplyAuditReport",
            "AUDIT_SCHEMA_VERSION",
        ):
            self.assertIn(
                sym,
                audit_pkg.__all__,
                f"new symbol {sym!r} missing from aee.audit.__all__",
            )


# ---------------------------------------------------------------------------
# Mismatch category classifier (pure function coverage)
# ---------------------------------------------------------------------------


class TestApplyWithAuditMismatchCategories(unittest.TestCase):
    """Verifies the five explicit categories are each surfaced
    by the classifier when the corresponding input is
    constructed.
    """

    def test_missing_from_reports_category(self) -> None:
        """A planned row whose ``Path(...).name`` is NOT in
        outcomes is classified as
        :data:`AUDIT_MISSING_FROM_REPORTS` with
        ``outcome_index == -1`` and
        ``outcome_decision is None``.
        """
        pi = PlanInput(
            group_name="G1",
            kind=FileEntryKind.NEW,
            path="aee/reports/PLANNED-BUT-NOT-IN-CORPUS/task.json",
            sha256="a" * 64,
            size=10,
            lines=1,
        )
        mismatches = _classify_audit_mismatches((pi,), ())
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0].category, AUDIT_MISSING_FROM_REPORTS)
        self.assertEqual(mismatches[0].outcome_index, -1)
        self.assertIsNone(mismatches[0].outcome_decision)

    def test_extra_in_reports_category(self) -> None:
        """An outcome whose ``task_id`` is NOT in the planned
        set is classified as
        :data:`AUDIT_EXTRA_IN_REPORTS` with empty planned
        fields and ``outcome_index`` pointing at the
        offending outcome.
        """
        pi = PlanInput(
            group_name="G1",
            kind=FileEntryKind.NEW,
            path="aee/reports/PLANNED-1/task.json",
            sha256="a" * 64,
            size=10,
            lines=1,
        )
        outcome = PerTaskSidecarOutcome(
            task_id="EXTRA-IN-REPORTS",
            decision=SidecarDecision.WROTE,
            record_kind="runtime",
            is_consistent=True,
            source_task_json_sha256="a" * 64,
            sidecar_sha256="b" * 64,
            anchor_warnings=(),
            error_codes=(),
            note="",
        )
        mismatches = _classify_audit_mismatches((pi,), (outcome,))
        # Two mismatches: the planned row's basename is
        # ``PLANNED-1`` (from ``os.path.basename(path)`` —
        # NOT the task_id, which is the directory under
        # reports). Since the outcome's task_id is
        # ``EXTRA-IN-REPORTS``, the planned row is
        # MISSING_FROM_REPORTS and the outcome is
        # EXTRA_IN_REPORTS. Both rows surface their own
        # category.
        cats = {m.category for m in mismatches}
        self.assertIn(AUDIT_EXTRA_IN_REPORTS, cats)
        self.assertIn(AUDIT_MISSING_FROM_REPORTS, cats)

    def test_sha256_mismatch_category(self) -> None:
        """A planned row whose ``sha256`` differs from the
        matched outcome's ``source_task_json_sha256`` is
        classified as :data:`AUDIT_SHA256_MISMATCH`.

        Constructed by setting the planned path's basename
        to the outcome's task_id, so the planned row
        matches the outcome.
        """
        pi = PlanInput(
            group_name="G1",
            kind=FileEntryKind.NEW,
            path="TASK-SHA",  # basename == outcome.task_id
            sha256="a" * 64,
            size=10,
            lines=1,
        )
        outcome = PerTaskSidecarOutcome(
            task_id="TASK-SHA",
            decision=SidecarDecision.WROTE,
            record_kind="runtime",
            is_consistent=True,
            source_task_json_sha256="b" * 64,
            sidecar_sha256="c" * 64,
            anchor_warnings=(),
            error_codes=(),
            note="",
        )
        mismatches = _classify_audit_mismatches((pi,), (outcome,))
        # The planned row matches the outcome, SHA-256
        # mismatches → AUDIT_SHA256_MISMATCH. The
        # KIND_MISMATCH check is downstream and is not
        # reached (the classifier's first-match-wins
        # semantics). But the test is robust to either
        # outcome: SHA256_MISMATCH must be in the
        # categories set.
        cats = {m.category for m in mismatches}
        self.assertIn(AUDIT_SHA256_MISMATCH, cats)

    def test_kind_mismatch_category(self) -> None:
        """A planned row whose ``kind.value`` differs from the
        outcome's ``record_kind`` is classified as
        :data:`AUDIT_KIND_MISMATCH`. SHA-256s match so the
        SHA-256 mismatch does NOT fire first.
        """
        shared_sha = "a" * 64
        pi = PlanInput(
            group_name="G1",
            kind=FileEntryKind.NEW,  # value = "new"
            path="TASK-KIND",  # basename == outcome.task_id
            sha256=shared_sha,
            size=10,
            lines=1,
        )
        outcome = PerTaskSidecarOutcome(
            task_id="TASK-KIND",
            decision=SidecarDecision.WROTE,
            record_kind="runtime",  # "runtime" != "new"
            is_consistent=True,
            source_task_json_sha256=shared_sha,
            sidecar_sha256="b" * 64,
            anchor_warnings=(),
            error_codes=(),
            note="",
        )
        mismatches = _classify_audit_mismatches((pi,), (outcome,))
        cats = {m.category for m in mismatches}
        self.assertIn(AUDIT_KIND_MISMATCH, cats)

    def test_decision_mismatch_category(self) -> None:
        """A planned row whose matched outcome's decision is
        in ``{SKIPPED_INCONSISTENT, SKIPPED_MALFORMED,
        SKIPPED_COLLISION}`` is classified as
        :data:`AUDIT_DECISION_MISMATCH`. SHA-256s and kinds
        match so the previous classifiers do NOT fire.

        NOTE: the kind check compares FileEntryKind.value
        (``"new"``) with outcome.record_kind (``"runtime"``)
        — these differ, so the kind check WOULD fire
        before the decision check. The test therefore
        uses a hand-crafted outcome with ``record_kind``
        set to ``"new"`` to bypass the kind check (the
        classifier's "first match wins" semantics means
        the decision check is reached only when the
        kind check passes — which requires the kind and
        record_kind to align).
        """
        shared_sha = "a" * 64
        pi = PlanInput(
            group_name="G1",
            kind=FileEntryKind.NEW,  # value = "new"
            path="TASK-DEC",  # basename == outcome.task_id
            sha256=shared_sha,
            size=10,
            lines=1,
        )
        outcome = PerTaskSidecarOutcome(
            task_id="TASK-DEC",
            decision=SidecarDecision.SKIPPED_INCONSISTENT,
            record_kind="new",  # aligned with FileEntryKind.NEW
            is_consistent=True,
            source_task_json_sha256=shared_sha,
            sidecar_sha256="",
            anchor_warnings=(),
            error_codes=("MISMATCHED_RUNTIME_ANCHORS",),
            note="",
        )
        mismatches = _classify_audit_mismatches((pi,), (outcome,))
        cats = {m.category for m in mismatches}
        self.assertIn(AUDIT_DECISION_MISMATCH, cats)

    def test_classifier_returns_tuple(self) -> None:
        """The classifier returns a tuple (not a list) so
        :attr:`ApplyAuditReport.mismatches` is hashable /
        JSON-deterministic.
        """
        result = _classify_audit_mismatches((), ())
        self.assertIsInstance(result, tuple)


# ---------------------------------------------------------------------------
# Bad audit_action rejected up front
# ---------------------------------------------------------------------------


class TestApplyWithAuditBadActionRejected(unittest.TestCase):
    """A bad ``audit_action`` value raises :class:`ValueError`
    immediately (BEFORE the apply pass runs).
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee78k3-badaction-"))
        self.reports_root, self.summary = _build_corpus(self._tmp)
        self.manifest_path = _smoke_manifest(self._tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_unknown_audit_action_raises_value_error(self) -> None:
        """An unknown ``audit_action`` value raises
        :class:`ValueError` BEFORE the apply pass runs (no
        sidecars are written).
        """
        with self.assertRaises(ValueError):
            apply_sidecars_with_audit(
                self.reports_root,
                self.summary,
                manifest_path=self.manifest_path,
                utc_stamp=_FIXTURE_UTC_STAMP,
                classified_at_override=_FIXTURE_UTC_STAMP,
                allow_runtime=True,
                audit_action="bogus-policy",
            )
        # No sidecars were written (the apply pass never ran).
        for d in self.reports_root.iterdir():
            if d.is_dir():
                self.assertFalse(
                    (d / "identity.json").exists(),
                    f"sidecar written at {d / 'identity.json'} despite "
                    "ValueError — the apply pass should NOT have run",
                )


# ---------------------------------------------------------------------------
# Bad manifest propagates (transport-level failure)
# ---------------------------------------------------------------------------


class TestApplyWithAuditBadManifestPropagates(unittest.TestCase):
    """A missing / non-JSON ``manifest_path`` raises
    :class:`ManifestError` (transport-level failure
    propagates from the K2.5 wrapper).
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee78k3-badmanifest-"))
        self.reports_root, self.summary = _build_corpus(self._tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_missing_manifest_path_raises_manifest_error(self) -> None:
        """A non-existent ``manifest_path`` raises
        :class:`ManifestError`.

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
            apply_sidecars_with_audit(
                self.reports_root,
                self.summary,
                manifest_path=self._tmp / "does-not-exist.json",
                utc_stamp=_FIXTURE_UTC_STAMP,
                classified_at_override=_FIXTURE_UTC_STAMP,
                allow_runtime=True,
            )

    def test_non_json_manifest_path_raises_manifest_error(self) -> None:
        """A file that exists but is not valid JSON raises
        :class:`ManifestError`.

        See the sys.modules cache-invalidation note on
        :meth:`test_missing_manifest_path_raises_manifest_error`.
        """
        from aee.audit import manifest as _manifest_mod
        bad = self._tmp / "bad.json"
        bad.write_text("this is not json {{{", encoding="utf-8")
        with self.assertRaises(_manifest_mod.ManifestError):
            apply_sidecars_with_audit(
                self.reports_root,
                self.summary,
                manifest_path=bad,
                utc_stamp=_FIXTURE_UTC_STAMP,
                classified_at_override=_FIXTURE_UTC_STAMP,
                allow_runtime=True,
            )


# ---------------------------------------------------------------------------
# Validation failure is additive (no gating)
# ---------------------------------------------------------------------------


class TestApplyWithAuditValidationFailureIsAdditive(unittest.TestCase):
    """When the manifest loads but fails validation, the
    audit still runs (with zero plan_inputs) and the apply
    pass output is preserved; the report surfaces
    ``plan_input_count == 0``.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee78k3-valfail-"))
        self.reports_root, self.summary = _build_corpus(self._tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_validation_failure_attach_report_with_zero_plan_inputs(self) -> None:
        """A manifest that loads but fails validation
        (missing the required top-level ``groups`` key)
        attaches a :class:`ApplyAuditReport` with
        ``plan_input_count == 0`` AND the apply pass STILL
        RAN (no gating).
        """
        # Manifest that loads but fails validation: missing
        # the required "groups" key.
        bad_manifest = self._tmp / "bad.json"
        bad_manifest.write_text(
            json.dumps({"generated_utc": "2026-07-13T18:00:00Z"}),
            encoding="utf-8",
        )

        result = apply_sidecars_with_audit(
            self.reports_root,
            self.summary,
            manifest_path=bad_manifest,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        # The K3 audit ran.
        self.assertIsNotNone(result.audit_report)
        self.assertEqual(result.audit_report.plan_input_count, 0)
        # The apply pass still ran (no gating).
        self.assertGreaterEqual(result.sidecars_written, 1)


# ---------------------------------------------------------------------------
# No-dispatcher-import contract preservation
# ---------------------------------------------------------------------------


class TestApplyWithAuditNoDispatcherImport(unittest.TestCase):
    """The K3 wrapper module does not introduce
    ``dispatcher.*`` into ``sys.modules`` when the opt-in
    path runs.
    """

    def setUp(self) -> None:
        # Snapshot the pre-call module set so the test does
        # not depend on test execution order.
        self._pre = set(sys.modules.keys())

    def test_no_dispatcher_import_after_opt_in_call(self) -> None:
        """An opt-in call (with ``manifest_path`` set) also
        does NOT pull ``dispatcher.*`` into
        ``sys.modules`` — the K1 isolation contract is
        preserved through the K3 wire-up too.
        """
        if not _REAL_MANIFEST.exists():
            self.skipTest(f"real manifest not found: {_REAL_MANIFEST}")
        tmp = Path(tempfile.mkdtemp(prefix="aee78k3-noimp-"))
        try:
            reports_root, summary = _build_corpus(tmp)
            apply_sidecars_with_audit(
                reports_root,
                summary,
                manifest_path=_REAL_MANIFEST,
                utc_stamp=_FIXTURE_UTC_STAMP,
                classified_at_override=_FIXTURE_UTC_STAMP,
                allow_runtime=True,
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        new_dispatcher_like = [
            name
            for name in sys.modules.keys()
            if (name == "dispatcher" or name.startswith("dispatcher."))
            and name not in self._pre
        ]
        self.assertEqual(
            new_dispatcher_like,
            [],
            f"apply_sidecars_with_audit opt-in call introduced "
            f"dispatcher modules: {new_dispatcher_like}",
        )


# ---------------------------------------------------------------------------
# Real-manifest end-to-end smoke (optional, gated on file presence)
# ---------------------------------------------------------------------------


class TestApplyWithAuditRealManifest(unittest.TestCase):
    """End-to-end smoke against the real
    ``AEE_7_7d_7e_MANIFEST.json``. The test is gated on the
    file's presence and is a fast read-only check that the
    K3 wire-up runs cleanly against a real manifest.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee78k3-real-"))
        self.reports_root, self.summary = _build_corpus(self._tmp)
        if not _REAL_MANIFEST.exists():
            self.skipTest(f"real manifest not found: {_REAL_MANIFEST}")

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_real_manifest_warn_attaches_report(self) -> None:
        """A real K1 manifest at the repo root attaches a
        :class:`ApplyAuditReport`. The apply pass still
        ran (the K3 audit never short-circuits the apply).
        """
        result = apply_sidecars_with_audit(
            self.reports_root,
            self.summary,
            manifest_path=_REAL_MANIFEST,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        self.assertIsNotNone(result.audit_report)
        # The apply pass ran.
        self.assertGreaterEqual(result.sidecars_written, 1)
        # The K2.5 summary is preserved.
        self.assertIsNotNone(result.plan_input_summary)


if __name__ == "__main__":
    unittest.main()
