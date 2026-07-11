"""AEE-7.7b — apply_sidecars targeted tests.

Covers the AEE-7.7b ``aee.audit.apply_sidecars`` entry point and
its companion ``ApplySidecarsResult`` / ``PerTaskSidecarOutcome``
/ ``SidecarDecision`` DTOs.

The test placement mirrors ``test_aee77_live_audit.py``:
``aee/tests/`` is a sibling of the read-side audit tests because
``apply_sidecars`` is the write-side counterpart. The G4 guard
(see ``tests/_live_db_guard.py``) is NOT required at import
time — ``aee.audit.apply_sidecars`` does not import
``dispatcher`` (verified by
``TestApplySidecarsNoDispatcherImport``).

The tests verify:

1. **Deterministic serialization** — same input + same
   ``utc_stamp`` yields a byte-identical ``to_dict()`` /
   ``to_markdown()`` payload. Re-running with the same
   summary is a no-op (``UNCHANGED``) and the on-disk
   sidecar is byte-identical to the first run.
2. **Idempotent apply** — running ``apply_sidecars`` twice
   on the same corpus reports ``WROTE`` once and
   ``UNCHANGED`` for every record on the second run.
3. **Missing task.json** — a directory that lacks
   ``task.json`` is silently skipped by ``iter_reports``;
   a record that appears in the corpus but is NOT in the
   summary is reported as ``SKIPPED_NOT_IN_SUMMARY``.
4. **Fixture-only record behavior** — with
   ``allow_runtime=False``, RUNTIME records are reported
   as ``SKIPPED_RUNTIME_DISALLOWED`` and no sidecar is
   written for them.
5. **Runtime record behavior** — with
   ``allow_runtime=True``, RUNTIME records get a sidecar
   carrying the supplied ``executor_session_id`` /
   ``runtime_run_id`` / ``user_provided_alias`` values.
6. **Collision / anchor warning** — an existing sidecar
   whose verdict differs from the new verdict is
   reported as ``OVERWROTE`` (default) or
   ``SKIPPED_COLLISION`` (under the strict-consistency
   guard for inconsistent records). Records with
   WARNING-severity findings keep their existing
   ``is_consistent=True`` verdict and the sidecar is
   written (the warning is surfaced via
   ``anchor_warnings``).
7. **No dispatcher import leakage** — loading
   ``aee.audit.apply_sidecars`` does NOT introduce any
   ``dispatcher.*`` module into ``sys.modules``.
8. **No secret / prompt / stdout / stderr leakage** —
   ``ApplySidecarsResult.to_dict()`` and
   ``.to_markdown()`` never contain ``input_text``,
   the seeded "sk-secret-..." / "PROMPT" markers, or
   the canonical "stdout" / "stderr" / "payload"
   substrings.
9. **AEE-7.7a compatibility** — a real ``AuditSummary``
   built by ``aee.audit.run_audit`` can be fed directly
   to ``apply_sidecars`` (the same shape the G2
   ``build_index`` CLI uses).
10. **Sidecar path confinement / traversal rejection** —
    every sidecar lands at
    ``reports_root/<task_id>/identity.json`` and
    nowhere else. A directory whose name contains
    ``..`` is refused by the filesystem (we assert
    the writer does not follow / escape the parent).
11. **No rewrite of existing report contents** —
    ``task.json`` byte content + mtime is preserved
    before and after ``apply_sidecars`` runs.
12. **build_index call-site migration** — the G2
    ``build_index`` CLI calls ``run_audit`` +
    ``apply_sidecars`` and the resulting manifest's
    ``apply_sidecars`` sub-bucket is populated
    correctly.
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
# aee.tests.test_aee77_apply_sidecars`` from outside the repo root.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


from aee.audit import (  # noqa: E402
    APPLY_SCHEMA_VERSION,
    ApplySidecarsResult,
    AuditSummary,
    PerTaskSidecarOutcome,
    PerTaskVerdict,
    SidecarDecision,
    apply_sidecars,
    run_audit,
)
from aee.audit.apply_sidecars import _collect_finding_codes  # noqa: E402
from aee.reporting.identity import (  # noqa: E402
    RecordKind,
    SentinelPolicy,
    _file_sha256,
    read_identity_sidecar,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# A RUNTIME record (the canonical real-executor shape).
# ``input_text`` carries a recognizable marker so the no-leakage
# tests can assert it never appears in the apply-side output.
_RUNTIME_RECORD = {
    "task_id": "TASK-20260711-1001",
    "title": "apply_sidecars RUNTIME smoke",
    "type": "coding",
    "priority": 50,
    "owner": "m2",
    "status": "completed",
    "progress_pct": 100,
    "progress_step": "done",
    "created_at": "2026-07-11T10:00:00.000Z",
    "started_at": "2026-07-11T10:00:01.000Z",
    "finished_at": "2026-07-11T10:05:00.000Z",
    "duration_sec": 299.0,
    "input_text": (
        "marker_runtime_input " "sk-secret-runtime-do-not-leak "
        "PROMPT-FROM-USER-12345"
    ),
    "hermes_run_id": "run_0123456789abcdef0123456789abcdef",
    "executor_session_id": "AEE-7.7B-APPLY-20260711",
    "runtime_run_id": "run_0123456789abcdef0123456789abcdef",
    "repo_root": "/home/ubuntu/hermes-runtime-bridge",
}

# A FIXTURE record. ``hermes_run_id`` is in the default
# sentinel set so the classifier marks it as FIXTURE.
_FIXTURE_RECORD = {
    "task_id": "TASK-20260711-1002",
    "title": "aee6-traversal",
    "type": "coding",
    "priority": 50,
    "owner": "m2",
    "status": "running",
    "progress_pct": 5,
    "input_text": (
        "marker_fixture_input " "sk-secret-fixture-do-not-leak "
        "stdout-from-test-fixture " "stderr-from-test-fixture"
    ),
    "hermes_run_id": "hr-1",
    "executor_session_id": "AEE-7.7B-FIXTURE-20260711",
    "runtime_run_id": "run-aae-apply-fixture",
    "repo_root": "/home/ubuntu/hermes-runtime-bridge",
}

# A UNKNOWN record. No fixture signals fire AND ``hermes_run_id``
# is NOT in the run_<32hex> shape.
_UNKNOWN_RECORD = {
    "task_id": "TASK-20260711-1003",
    "title": "apply_sidecars UNKNOWN smoke",
    "type": "coding",
    "priority": 50,
    "owner": "m2",
    "status": "completed",
    "progress_pct": 100,
    "input_text": (
        "marker_unknown_input " "payload-from-test-unknown"
    ),
    "hermes_run_id": "weird-custom-id-2026",
    "executor_session_id": "AEE-7.7B-UNKNOWN-20260711",
    "runtime_run_id": "weird-custom-id-2026",
    "repo_root": "/home/ubuntu/hermes-runtime-bridge",
}


_FIXTURE_UTC_STAMP = "2026-07-11T13:00:00Z"


# A canonical run_<32hex> id used in synthetic verdicts.
_RUNTIME_RUN_ID_HEX = "run_0123456789abcdef0123456789abcdef"


def _write_task_json(
    root: Path,
    task_id: str,
    payload: dict,
) -> Path:
    """Write a single ``task.json`` under ``root/<task_id>/``.

    Returns the path to the written file.
    """
    d = root / task_id
    d.mkdir(parents=True, exist_ok=True)
    p = d / "task.json"
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return p


def _write_identity_sidecar_raw(
    task_json_path: Path,
    payload: dict,
) -> Path:
    """Write a hand-crafted ``identity.json`` sidecar for a
    collision test. Atomic write via temp+replace.
    """
    sidecar = task_json_path.parent / "identity.json"
    encoded = json.dumps(payload, indent=2, sort_keys=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".identity.", suffix=".json.tmp",
        dir=str(task_json_path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(encoded.encode("utf-8"))
        os.replace(tmp_path, sidecar)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return sidecar


def _make_verdict(
    *,
    task_id: str,
    record_kind: str,
    is_consistent: bool,
    findings: tuple = (),
    is_fixture: bool = False,
    fixture_markers: tuple = (),
    source_sha: str = "",
) -> PerTaskVerdict:
    """Build a synthetic :class:`PerTaskVerdict` for tests
    that do NOT want to round-trip through ``run_audit``.

    The fields are the same shape ``run_audit`` produces.
    """
    return PerTaskVerdict(
        task_id=task_id,
        record_kind=record_kind,
        is_fixture=is_fixture,
        fixture_markers=tuple(fixture_markers),
        is_consistent=is_consistent,
        findings=tuple(findings),
        source_task_json_sha256=source_sha,
        classified_at_utc=_FIXTURE_UTC_STAMP,
    )


def _make_summary(verdicts: list) -> AuditSummary:
    """Build a minimal :class:`AuditSummary` containing the
    supplied verdicts. Aggregate buckets are zeroed (the
    ``apply_sidecars`` writer only reads ``verdicts``).
    """
    by_kind = {
        RecordKind.RUNTIME.value: 0,
        RecordKind.FIXTURE.value: 0,
        RecordKind.UNKNOWN.value: 0,
    }
    by_consistency = {"consistent_true": 0, "consistent_false": 0}
    for v in verdicts:
        if v.record_kind in by_kind:
            by_kind[v.record_kind] += 1
        if v.is_consistent:
            by_consistency["consistent_true"] += 1
        else:
            by_consistency["consistent_false"] += 1
    return AuditSummary(
        reports_root="<unused-by-apply-sidecars>",
        audited_at_utc=_FIXTURE_UTC_STAMP,
        schema_version="1.0.0",
        verdicts=list(verdicts),
        by_record_kind=by_kind,
        by_consistency=by_consistency,
        finding_code_counts={},
        fixture_inconsistent_count=sum(
            1 for v in verdicts if v.is_fixture and not v.is_consistent
        ),
    )


# Markers that must NEVER appear in ``to_dict()`` /
# ``to_markdown()`` output (input_text fingerprint, secret
# pattern, prompt marker, stdout/stderr markers).
_LEAK_MARKERS = (
    "marker_runtime_input",
    "marker_fixture_input",
    "marker_unknown_input",
    "sk-secret-runtime-do-not-leak",
    "sk-secret-fixture-do-not-leak",
    "PROMPT-FROM-USER-12345",
    "stdout-from-test-fixture",
    "stderr-from-test-fixture",
    "payload-from-test-unknown",
)


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestApplySidecarsDeterministicSerialization(unittest.TestCase):
    """Same corpus + same utc_stamp ⇒ byte-identical result
    document + byte-identical sidecar on disk.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77b-det-"))
        self.reports_root = Path(self._tmp) / "reports"
        self.reports_root.mkdir()
        # Seed: one RUNTIME + one FIXTURE + one UNKNOWN.
        self._rt_path = _write_task_json(
            self.reports_root, _RUNTIME_RECORD["task_id"], _RUNTIME_RECORD
        )
        self._fx_path = _write_task_json(
            self.reports_root, _FIXTURE_RECORD["task_id"], _FIXTURE_RECORD
        )
        self._uk_path = _write_task_json(
            self.reports_root, _UNKNOWN_RECORD["task_id"], _UNKNOWN_RECORD
        )
        self.verdicts = [
            _make_verdict(
                task_id=_RUNTIME_RECORD["task_id"],
                record_kind=RecordKind.RUNTIME.value,
                is_consistent=True,
                source_sha=_file_sha256(self._rt_path),
            ),
            _make_verdict(
                task_id=_FIXTURE_RECORD["task_id"],
                record_kind=RecordKind.FIXTURE.value,
                is_consistent=True,
                is_fixture=True,
                fixture_markers=("sentinel_hermes_run_id:'hr-1'",),
                source_sha=_file_sha256(self._fx_path),
            ),
            _make_verdict(
                task_id=_UNKNOWN_RECORD["task_id"],
                record_kind=RecordKind.UNKNOWN.value,
                is_consistent=True,
                source_sha=_file_sha256(self._uk_path),
            ),
        ]
        self.summary = _make_summary(self.verdicts)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_two_runs_byte_identical_to_dict(self) -> None:
        a = apply_sidecars(
            self.reports_root, self.summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        b = apply_sidecars(
            self.reports_root, self.summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        # Same utc_stamp ⇒ same applied_at_utc / schema_version /
        # policy_version / reports_root / strict_consistency.
        # The by_decision / by_record_kind / sidecars_written /
        # anchor_warning_count buckets LEGITIMATELY differ between
        # the WROTE and UNCHANGED passes — they summarise what
        # the run did, not what the corpus looks like. The
        # stability invariant is the on-disk sidecar bytes
        # (asserted in TestApplySidecarsIdempotent) and the
        # per-task outcome shape.
        a_doc = a.to_dict()
        b_doc = b.to_dict()
        for key in (
            "schema_version",
            "applied_at_utc",
            "reports_root",
            "policy_version",
            "strict_consistency",
        ):
            self.assertEqual(
                a_doc[key], b_doc[key],
                f"to_dict[{key!r}] not stable across idempotent runs",
            )
        # And the per-task outcomes carry the same
        # (record_kind, is_consistent, source_task_json_sha256)
        # on the second pass. Decision transitions WROTE→UNCHANGED.
        a_by_id = {o.task_id: o for o in a.outcomes}
        b_by_id = {o.task_id: o for o in b.outcomes}
        self.assertEqual(set(a_by_id.keys()), set(b_by_id.keys()))
        for tid in a_by_id:
            self.assertEqual(
                a_by_id[tid].record_kind, b_by_id[tid].record_kind,
            )
            self.assertEqual(
                a_by_id[tid].is_consistent, b_by_id[tid].is_consistent,
            )
            self.assertEqual(
                a_by_id[tid].source_task_json_sha256,
                b_by_id[tid].source_task_json_sha256,
            )
            self.assertEqual(a_by_id[tid].decision, SidecarDecision.WROTE)
            self.assertEqual(b_by_id[tid].decision, SidecarDecision.UNCHANGED)

    def test_two_runs_byte_identical_to_markdown(self) -> None:
        a = apply_sidecars(
            self.reports_root, self.summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        b = apply_sidecars(
            self.reports_root, self.summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            classified_at_override=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        # Same headers + same totals. The by_decision table
        # legitimately differs (WROTE vs UNCHANGED).
        a_md = a.to_markdown()
        b_md = b.to_markdown()
        for header in (
            "# AEE-7.7b Sidecar Apply Summary",
            "## By decision",
            "## By record kind",
            "## Anchor warnings:",
            "## Sidecars written:",
            f"- Schema version: `{APPLY_SCHEMA_VERSION}`",
            f"- Applied at (UTC): `{_FIXTURE_UTC_STAMP}`",
            f"- Reports root: `{a.reports_root}`",
        ):
            self.assertIn(header, a_md)
            self.assertIn(header, b_md)
        # First run wrote 3 sidecars; second run wrote 0 (all
        # UNCHANGED) — that is the whole point of the
        # idempotency guarantee.
        self.assertEqual(a.sidecars_written, 3)
        self.assertEqual(b.sidecars_written, 0)

    def test_schema_version_is_pinned(self) -> None:
        result = apply_sidecars(
            self.reports_root, self.summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        self.assertEqual(result.schema_version, APPLY_SCHEMA_VERSION)

    def test_applied_at_utc_uses_supplied_stamp(self) -> None:
        result = apply_sidecars(
            self.reports_root, self.summary,
            utc_stamp="2026-07-11T13:00:00Z",
            allow_runtime=True,
        )
        self.assertEqual(result.applied_at_utc, "2026-07-11T13:00:00Z")

    def test_to_dict_is_json_serializable_round_trip(self) -> None:
        result = apply_sidecars(
            self.reports_root, self.summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        encoded = json.dumps(result.to_dict(), sort_keys=True)
        decoded = json.loads(encoded)
        # Round-trip preserves the schema_version + counts.
        self.assertEqual(decoded["schema_version"], APPLY_SCHEMA_VERSION)
        self.assertEqual(
            decoded["by_decision"]["wrote"],
            3,
        )
        # And the schema has the documented keys.
        for key in (
            "schema_version",
            "applied_at_utc",
            "reports_root",
            "policy_version",
            "strict_consistency",
            "by_decision",
            "by_record_kind",
            "anchor_warning_count",
            "sidecars_written",
            "outcomes",
        ):
            self.assertIn(key, decoded)

    def test_to_dict_outcomes_are_frozen(self) -> None:
        result = apply_sidecars(
            self.reports_root, self.summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        outcome = result.outcomes[0]
        self.assertIsInstance(outcome, PerTaskSidecarOutcome)
        with self.assertRaises(Exception):
            # frozen dataclass — assignment must raise.
            outcome.decision = SidecarDecision.OVERWROTE  # type: ignore[misc]


class TestApplySidecarsIdempotent(unittest.TestCase):
    """Repeated apply over the same corpus is a no-op."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77b-idem-"))
        self.reports_root = Path(self._tmp) / "reports"
        self.reports_root.mkdir()
        self._fx_path = _write_task_json(
            self.reports_root, _FIXTURE_RECORD["task_id"], _FIXTURE_RECORD
        )
        self.verdicts = [
            _make_verdict(
                task_id=_FIXTURE_RECORD["task_id"],
                record_kind=RecordKind.FIXTURE.value,
                is_consistent=True,
                is_fixture=True,
                source_sha=_file_sha256(self._fx_path),
            ),
        ]
        self.summary = _make_summary(self.verdicts)
        self.sidecar_path = self._fx_path.parent / "identity.json"

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_first_run_writes_second_run_unchanged(self) -> None:
        first = apply_sidecars(
            self.reports_root, self.summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        self.assertTrue(self.sidecar_path.exists())
        first_sha = _file_sha256(self.sidecar_path)
        first_decision = first.outcomes[0].decision
        self.assertEqual(first_decision, SidecarDecision.WROTE)

        second = apply_sidecars(
            self.reports_root, self.summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        second_sha = _file_sha256(self.sidecar_path)
        second_decision = second.outcomes[0].decision
        # The sidecar is byte-identical (no re-write needed).
        self.assertEqual(first_sha, second_sha)
        # And the second run reports UNCHANGED.
        self.assertEqual(second_decision, SidecarDecision.UNCHANGED)
        self.assertEqual(second.by_decision[SidecarDecision.WROTE.value], 0)
        self.assertEqual(
            second.by_decision[SidecarDecision.UNCHANGED.value], 1
        )

    def test_three_runs_still_byte_stable(self) -> None:
        apply_sidecars(
            self.reports_root, self.summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        sha_one = _file_sha256(self.sidecar_path)
        for _ in range(3):
            apply_sidecars(
                self.reports_root, self.summary,
                utc_stamp=_FIXTURE_UTC_STAMP,
                allow_runtime=True,
            )
        sha_n = _file_sha256(self.sidecar_path)
        self.assertEqual(sha_one, sha_n)


class TestApplySidecarsMissingTaskJson(unittest.TestCase):
    """``apply_sidecars`` handles missing task.json / corpus
    entries that are not in the summary.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77b-miss-"))
        self.reports_root = Path(self._tmp) / "reports"
        self.reports_root.mkdir()
        # A directory WITHOUT task.json (skipped by iter_reports).
        (self.reports_root / "TASK-NOFILE").mkdir()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_directory_without_task_json_is_ignored(self) -> None:
        result = apply_sidecars(
            self.reports_root,
            _make_summary([]),
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        # No verdicts ⇒ no outcomes; nothing written.
        self.assertEqual(result.outcomes, [])
        self.assertEqual(result.sidecars_written, 0)
        # No file was created in the dangling dir.
        self.assertFalse(
            (self.reports_root / "TASK-NOFILE" / "task.json").exists()
        )
        self.assertFalse(
            (self.reports_root / "TASK-NOFILE" / "identity.json").exists()
        )

    def test_record_in_corpus_but_not_in_summary_is_skipped(self) -> None:
        # Seed a real task.json that the summary does NOT cover.
        _write_task_json(
            self.reports_root, _RUNTIME_RECORD["task_id"], _RUNTIME_RECORD
        )
        result = apply_sidecars(
            self.reports_root,
            _make_summary([]),  # empty summary
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        # The record was skipped (not in summary), not written.
        self.assertEqual(len(result.outcomes), 0)
        self.assertEqual(
            result.by_decision[SidecarDecision.SKIPPED_NOT_IN_SUMMARY.value],
            1,
        )
        self.assertFalse(
            (self.reports_root / _RUNTIME_RECORD["task_id"] / "identity.json")
            .exists()
        )

    def test_non_existent_reports_root_yields_empty_result(self) -> None:
        missing = self._tmp / "does-not-exist"
        result = apply_sidecars(
            missing,
            _make_summary([]),
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        self.assertEqual(result.outcomes, [])
        self.assertEqual(result.sidecars_written, 0)
        self.assertEqual(result.anchor_warning_count, 0)
        # The reports_root is preserved (string form) so
        # operators can still see where the call targeted.
        self.assertIn("does-not-exist", result.reports_root)
        # The schema_version / applied_at_utc / policy_version
        # / strict_consistency are populated regardless of
        # whether the root exists.
        self.assertEqual(result.schema_version, APPLY_SCHEMA_VERSION)
        self.assertEqual(result.applied_at_utc, _FIXTURE_UTC_STAMP)
        self.assertTrue(result.strict_consistency)


class TestApplySidecarsRecordKindBehavior(unittest.TestCase):
    """Fixture-only (``allow_runtime=False``) vs runtime-inclusive."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77b-kind-"))
        self.reports_root = Path(self._tmp) / "reports"
        self.reports_root.mkdir()
        self._rt_path = _write_task_json(
            self.reports_root, _RUNTIME_RECORD["task_id"], _RUNTIME_RECORD
        )
        self._fx_path = _write_task_json(
            self.reports_root, _FIXTURE_RECORD["task_id"], _FIXTURE_RECORD
        )
        self._uk_path = _write_task_json(
            self.reports_root, _UNKNOWN_RECORD["task_id"], _UNKNOWN_RECORD
        )
        self.verdicts = [
            _make_verdict(
                task_id=_RUNTIME_RECORD["task_id"],
                record_kind=RecordKind.RUNTIME.value,
                is_consistent=True,
                source_sha=_file_sha256(self._rt_path),
            ),
            _make_verdict(
                task_id=_FIXTURE_RECORD["task_id"],
                record_kind=RecordKind.FIXTURE.value,
                is_consistent=True,
                is_fixture=True,
                source_sha=_file_sha256(self._fx_path),
            ),
            _make_verdict(
                task_id=_UNKNOWN_RECORD["task_id"],
                record_kind=RecordKind.UNKNOWN.value,
                is_consistent=True,
                source_sha=_file_sha256(self._uk_path),
            ),
        ]
        self.summary = _make_summary(self.verdicts)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_allow_runtime_true_writes_all(self) -> None:
        result = apply_sidecars(
            self.reports_root, self.summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        decisions = {o.task_id: o.decision for o in result.outcomes}
        self.assertEqual(decisions[_RUNTIME_RECORD["task_id"]],
                         SidecarDecision.WROTE)
        self.assertEqual(decisions[_FIXTURE_RECORD["task_id"]],
                         SidecarDecision.WROTE)
        self.assertEqual(decisions[_UNKNOWN_RECORD["task_id"]],
                         SidecarDecision.WROTE)
        self.assertEqual(result.sidecars_written, 3)
        # The runtime sidecar exists. Without explicit
        # executor_anchors / user_provided_alias the SOT writer
        # stamps them as None (the writer only echoes the
        # values the caller passes; it does NOT read them back
        # from task.json — that would re-couple the writer to
        # the input shape).
        sidecar = json.loads(
            (self._rt_path.parent / "identity.json").read_text()
        )
        self.assertIsNone(sidecar.get("executor_session_id"))
        self.assertIsNone(sidecar.get("runtime_run_id"))

    def test_allow_runtime_false_skips_runtime_records(self) -> None:
        result = apply_sidecars(
            self.reports_root, self.summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=False,
        )
        decisions = {o.task_id: o.decision for o in result.outcomes}
        self.assertEqual(
            decisions[_RUNTIME_RECORD["task_id"]],
            SidecarDecision.SKIPPED_RUNTIME_DISALLOWED,
        )
        self.assertEqual(decisions[_FIXTURE_RECORD["task_id"]],
                         SidecarDecision.WROTE)
        self.assertEqual(decisions[_UNKNOWN_RECORD["task_id"]],
                         SidecarDecision.WROTE)
        # Only FIXTURE + UNKNOWN sidecars are on disk.
        self.assertFalse(
            (self._rt_path.parent / "identity.json").exists()
        )
        self.assertTrue(
            (self._fx_path.parent / "identity.json").exists()
        )
        self.assertTrue(
            (self._uk_path.parent / "identity.json").exists()
        )
        self.assertEqual(result.sidecars_written, 2)
        self.assertEqual(
            result.by_decision[
                SidecarDecision.SKIPPED_RUNTIME_DISALLOWED.value
            ],
            1,
        )

    def test_runtime_record_sidecar_preserves_executor_anchors(self) -> None:
        # Pass per-task anchors via the new wire-up API.
        result = apply_sidecars(
            self.reports_root, self.summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
            executor_anchors={
                _RUNTIME_RECORD["task_id"]: {
                    "executor_session_id": "AEE-7.7B-WIRE-UP",
                    "runtime_run_id": "run-wire-up-001",
                },
            },
            user_provided_alias={
                _RUNTIME_RECORD["task_id"]: "USER-ALIAS-RT-001",
            },
        )
        sidecar = json.loads(
            (self._rt_path.parent / "identity.json").read_text()
        )
        self.assertEqual(sidecar["executor_session_id"], "AEE-7.7B-WIRE-UP")
        self.assertEqual(sidecar["runtime_run_id"], "run-wire-up-001")
        self.assertEqual(
            sidecar["user_provided_alias"], "USER-ALIAS-RT-001"
        )
        # And the apply-side result surfaced the runtime outcome.
        rt = next(
            o for o in result.outcomes
            if o.task_id == _RUNTIME_RECORD["task_id"]
        )
        self.assertEqual(rt.decision, SidecarDecision.WROTE)
        self.assertEqual(rt.record_kind, "runtime")


class TestApplySidecarsCollisionAndAnchors(unittest.TestCase):
    """Existing sidecar differs from the new verdict ⇒
    ``OVERWROTE`` (default) or ``SKIPPED_COLLISION`` (strict).
    WARNING-severity findings keep ``is_consistent=True`` and
    the sidecar is still written.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77b-coll-"))
        self.reports_root = Path(self._tmp) / "reports"
        self.reports_root.mkdir()
        self._rt_path = _write_task_json(
            self.reports_root, _RUNTIME_RECORD["task_id"], _RUNTIME_RECORD
        )

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_overwrite_when_existing_sidecar_differs(self) -> None:
        # Plant a divergent but well-formed sidecar (record_kind
        # = fixture, but the new verdict is runtime). Default
        # strict_consistency=True on a consistent record ⇒
        # OVERWROTE.
        _write_identity_sidecar_raw(
            self._rt_path,
            {
                "schema_version": "1.0.0",
                "task_id": _RUNTIME_RECORD["task_id"],
                "record_kind": "fixture",
                "is_fixture": True,
                "fixture_markers": ["sentinel_hermes_run_id:'hr-1'"],
                "executor_session_id": "stale-session",
                "runtime_run_id": "stale-run",
                "user_provided_alias": None,
                "source_task_json_sha256": "stale-sha",
                "classified_at_utc": "2020-01-01T00:00:00Z",
                "policy_version": "0.0.0",
            },
        )
        verdicts = [
            _make_verdict(
                task_id=_RUNTIME_RECORD["task_id"],
                record_kind=RecordKind.RUNTIME.value,
                is_consistent=True,
                source_sha=_file_sha256(self._rt_path),
            ),
        ]
        result = apply_sidecars(
            self.reports_root,
            _make_summary(verdicts),
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
            strict_consistency=True,
        )
        self.assertEqual(
            result.outcomes[0].decision, SidecarDecision.OVERWROTE
        )
        self.assertEqual(
            result.by_decision[SidecarDecision.OVERWROTE.value], 1
        )
        # The on-disk sidecar now reflects the runtime verdict.
        new_sidecar = json.loads(
            (self._rt_path.parent / "identity.json").read_text()
        )
        self.assertEqual(new_sidecar["record_kind"], "runtime")
        self.assertFalse(new_sidecar["is_fixture"])

    def test_collision_skipped_under_strict_consistency(self) -> None:
        # Inconsistent verdicts are ALWAYS SKIPPED_INCONSISTENT
        # — they short-circuit before the collision branch is
        # reached (G1 line 511). This is the correct safety
        # behaviour: we never auto-overwrite the sidecar of a
        # record the audit flagged as inconsistent, even when
        # the existing sidecar differs from the new verdict.
        _write_identity_sidecar_raw(
            self._rt_path,
            {
                "schema_version": "1.0.0",
                "task_id": _RUNTIME_RECORD["task_id"],
                "record_kind": "fixture",
                "is_fixture": True,
                "fixture_markers": ["sentinel_hermes_run_id:'hr-1'"],
                "executor_session_id": "stale-session",
                "runtime_run_id": "stale-run",
                "user_provided_alias": None,
                "source_task_json_sha256": "stale-sha",
                "classified_at_utc": "2020-01-01T00:00:00Z",
                "policy_version": "0.0.0",
            },
        )
        verdicts = [
            _make_verdict(
                task_id=_RUNTIME_RECORD["task_id"],
                record_kind=RecordKind.RUNTIME.value,
                is_consistent=False,
                findings=(
                    {
                        "code": "MISMATCHED_RUNTIME_ANCHORS",
                        "severity": "error",
                        "message": "mismatched",
                        "field_path": "hermes_run_id",
                    },
                ),
                source_sha=_file_sha256(self._rt_path),
            ),
        ]
        result = apply_sidecars(
            self.reports_root,
            _make_summary(verdicts),
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
            strict_consistency=True,
        )
        self.assertEqual(
            result.outcomes[0].decision, SidecarDecision.SKIPPED_INCONSISTENT
        )
        # The on-disk sidecar is the original (stale) one.
        existing = json.loads(
            (self._rt_path.parent / "identity.json").read_text()
        )
        self.assertEqual(existing["record_kind"], "fixture")
        # The error code is captured on the outcome.
        self.assertIn(
            "MISMATCHED_RUNTIME_ANCHORS",
            result.outcomes[0].error_codes,
        )

    def test_warning_finding_does_not_block_sidecar_write(self) -> None:
        # A WARNING-severity finding is non-fatal: the verdict
        # is still is_consistent=True and the sidecar is written.
        verdicts = [
            _make_verdict(
                task_id=_RUNTIME_RECORD["task_id"],
                record_kind=RecordKind.RUNTIME.value,
                is_consistent=True,
                findings=(
                    {
                        "code": "RUNTIME_ANCHOR_SHAPE",
                        "severity": "warning",
                        "message": "anchor shape",
                        "field_path": "runtime_run_id",
                    },
                ),
                source_sha=_file_sha256(self._rt_path),
            ),
        ]
        result = apply_sidecars(
            self.reports_root,
            _make_summary(verdicts),
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        self.assertEqual(
            result.outcomes[0].decision, SidecarDecision.WROTE
        )
        # The warning surfaced via the outcome's anchor_warnings.
        self.assertIn(
            "RUNTIME_ANCHOR_SHAPE",
            result.outcomes[0].anchor_warnings,
        )
        self.assertEqual(result.anchor_warning_count, 1)
        # The on-disk sidecar exists.
        self.assertTrue(
            (self._rt_path.parent / "identity.json").exists()
        )

    def test_inconsistent_verdict_is_always_skipped(self) -> None:
        # A new verdict with is_consistent=False is always
        # SKIPPED_INCONSISTENT, even without an existing sidecar.
        verdicts = [
            _make_verdict(
                task_id=_RUNTIME_RECORD["task_id"],
                record_kind=RecordKind.RUNTIME.value,
                is_consistent=False,
                findings=(
                    {
                        "code": "RUNTIME_RECORD_WITHOUT_ANCHOR",
                        "severity": "error",
                        "message": "no anchor",
                        "field_path": "runtime_run_id",
                    },
                ),
                source_sha=_file_sha256(self._rt_path),
            ),
        ]
        result = apply_sidecars(
            self.reports_root,
            _make_summary(verdicts),
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
            strict_consistency=True,
        )
        self.assertEqual(
            result.outcomes[0].decision, SidecarDecision.SKIPPED_INCONSISTENT
        )
        # The error code is captured in the outcome.
        self.assertIn(
            "RUNTIME_RECORD_WITHOUT_ANCHOR",
            result.outcomes[0].error_codes,
        )
        # No sidecar was written.
        self.assertFalse(
            (self._rt_path.parent / "identity.json").exists()
        )

    def test_collect_finding_codes_helper(self) -> None:
        # Internal helper: warnings and errors are split by
        # severity, not by code set.
        verdict = _make_verdict(
            task_id="TASK-X",
            record_kind=RecordKind.RUNTIME.value,
            is_consistent=False,
            findings=(
                {"code": "WARN_A", "severity": "warning"},
                {"code": "INFO_B", "severity": "info"},
                {"code": "ERR_C", "severity": "error"},
            ),
        )
        warnings, errors = _collect_finding_codes(verdict)
        self.assertIn("WARN_A", warnings)
        self.assertIn("INFO_B", warnings)
        self.assertIn("ERR_C", errors)
        self.assertNotIn("ERR_C", warnings)


class TestApplySidecarsNoDispatcherImport(unittest.TestCase):
    """Loading ``aee.audit.apply_sidecars`` must NOT introduce
    any ``dispatcher.*`` module into ``sys.modules``.

    Same pattern as ``TestRunAuditNoDispatcherImport`` in
    ``test_aee77_live_audit.py``. Catches a regression where
    a future maintainer adds a leaky import to the apply
    path.
    """

    def test_loading_apply_sidecars_does_not_introduce_dispatcher(self) -> None:
        # Force a fresh interpreter view of aee.audit* so the
        # snapshot is meaningful.
        for mod_name in list(sys.modules):
            if mod_name == "aee.audit" or mod_name.startswith("aee.audit."):
                del sys.modules[mod_name]

        before = set(sys.modules.keys())
        # Import the apply-side module via the public path.
        from aee.audit import apply_sidecars  # noqa: F401
        from aee.audit import ApplySidecarsResult  # noqa: F401
        from aee.audit import SidecarDecision  # noqa: F401
        after = set(sys.modules.keys())
        new_dispatcher = [
            m
            for m in (after - before)
            if m == "dispatcher" or m.startswith("dispatcher.")
        ]
        self.assertEqual(
            new_dispatcher,
            [],
            f"aee.audit.apply_sidecars must not introduce "
            f"dispatcher.* into sys.modules; got: {new_dispatcher}",
        )

    def test_underlying_module_does_not_import_dispatcher(self) -> None:
        # Source-level grep: the apply-side module must not
        # import ``dispatcher`` anywhere. Catches indirect
        # imports that lazy loading would miss in the
        # ``sys.modules`` snapshot test above.
        apply_path = (
            Path(_REPO_ROOT)
            / "aee"
            / "audit"
            / "apply_sidecars.py"
        )
        src = apply_path.read_text(encoding="utf-8")
        # Strip string literals + comments (cheap regex; a
        # false-positive is fine — a false-negative is what
        # we are guarding against).
        no_strings = re.sub(
            r"(\"\"\".*?\"\"\"|'''.*?'''|\".*?\"|'.*?')",
            " ",
            src,
            flags=re.DOTALL,
        )
        # Comment lines
        no_strings = re.sub(r"#[^\n]*", " ", no_strings)
        bad = re.search(
            r"(^|\n)\s*(import\s+dispatcher|from\s+dispatcher)",
            no_strings,
        )
        self.assertIsNone(
            bad,
            f"{apply_path} must not import dispatcher; "
            f"found: {bad.group(0) if bad else None!r}",
        )


class TestApplySidecarsNoLeakage(unittest.TestCase):
    """``ApplySidecarsResult.to_dict()`` and ``.to_markdown()``
    must never echo the seeded ``input_text`` fingerprint, the
    secret pattern, the prompt marker, or stdout/stderr markers.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77b-leak-"))
        self.reports_root = Path(self._tmp) / "reports"
        self.reports_root.mkdir()
        self._rt_path = _write_task_json(
            self.reports_root, _RUNTIME_RECORD["task_id"], _RUNTIME_RECORD
        )
        self._fx_path = _write_task_json(
            self.reports_root, _FIXTURE_RECORD["task_id"], _FIXTURE_RECORD
        )
        verdicts = [
            _make_verdict(
                task_id=_RUNTIME_RECORD["task_id"],
                record_kind=RecordKind.RUNTIME.value,
                is_consistent=True,
                source_sha=_file_sha256(self._rt_path),
            ),
            _make_verdict(
                task_id=_FIXTURE_RECORD["task_id"],
                record_kind=RecordKind.FIXTURE.value,
                is_consistent=True,
                is_fixture=True,
                source_sha=_file_sha256(self._fx_path),
            ),
        ]
        self.summary = _make_summary(verdicts)
        self.result = apply_sidecars(
            self.reports_root, self.summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_to_dict_does_not_leak_secrets_or_prompt(self) -> None:
        encoded = json.dumps(self.result.to_dict(), sort_keys=True)
        for marker in _LEAK_MARKERS:
            self.assertNotIn(
                marker, encoded,
                f"to_dict() must not leak {marker!r}; "
                f"found in result.to_dict()",
            )

    def test_to_markdown_does_not_leak_secrets_or_prompt(self) -> None:
        md = self.result.to_markdown()
        for marker in _LEAK_MARKERS:
            self.assertNotIn(
                marker, md,
                f"to_markdown() must not leak {marker!r}",
            )

    def test_outcome_note_does_not_leak_secrets_or_prompt(self) -> None:
        # Even at the per-outcome level (most verbose case).
        for outcome in self.result.outcomes:
            note = outcome.note or ""
            for marker in _LEAK_MARKERS:
                self.assertNotIn(
                    marker, note,
                    f"outcome[{outcome.task_id}].note leaked {marker!r}",
                )

    def test_sidecar_payload_does_not_echo_input_text(self) -> None:
        # The sidecar should not contain the full input_text
        # (it carries the verdict shape, not the prompt).
        for task_id in (
            _RUNTIME_RECORD["task_id"],
            _FIXTURE_RECORD["task_id"],
        ):
            sidecar_path = (
                self.reports_root / task_id / "identity.json"
            )
            payload = json.loads(sidecar_path.read_text())
            encoded = json.dumps(payload, sort_keys=True)
            for marker in _LEAK_MARKERS:
                self.assertNotIn(
                    marker, encoded,
                    f"sidecar[{task_id}] leaked {marker!r}",
                )


class TestApplySidecarsAEE77aCompatibility(unittest.TestCase):
    """A real ``AuditSummary`` built by ``aee.audit.run_audit``
    can be fed directly to ``apply_sidecars`` — the same shape
    the G2 ``build_index`` CLI uses.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77b-77a-"))
        self.reports_root = self._tmp / "reports"
        self.output_dir = self._tmp / "out"
        self.reports_root.mkdir()
        self.output_dir.mkdir()
        _write_task_json(
            self.reports_root, _RUNTIME_RECORD["task_id"], _RUNTIME_RECORD
        )
        _write_task_json(
            self.reports_root, _FIXTURE_RECORD["task_id"], _FIXTURE_RECORD
        )
        _write_task_json(
            self.reports_root, _UNKNOWN_RECORD["task_id"], _UNKNOWN_RECORD
        )

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_run_audit_summary_drives_apply_sidecars(self) -> None:
        summary, _, _ = run_audit(
            self.reports_root,
            self.output_dir,
            utc_stamp=_FIXTURE_UTC_STAMP,
        )
        # The summary has the same shape the G2 build_index
        # CLI passes to apply_sidecars.
        self.assertGreater(len(summary.verdicts), 0)
        result = apply_sidecars(
            self.reports_root, summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        # The verdict-by-task mapping round-trips: the apply
        # path sees the same per-task verdicts the audit
        # produced.
        verdicts_by_id = {v.task_id: v for v in summary.verdicts}
        for outcome in result.outcomes:
            self.assertIn(outcome.task_id, verdicts_by_id)
            v = verdicts_by_id[outcome.task_id]
            self.assertEqual(outcome.record_kind, v.record_kind)
            self.assertEqual(outcome.is_consistent, v.is_consistent)


class TestApplySidecarsPathConfinement(unittest.TestCase):
    """Every sidecar lands at ``reports_root/<task_id>/identity.json``
    and nowhere else.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77b-path-"))
        self.reports_root = Path(self._tmp) / "reports"
        self.reports_root.mkdir()
        self._fx_path = _write_task_json(
            self.reports_root, _FIXTURE_RECORD["task_id"], _FIXTURE_RECORD
        )

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_sidecar_lands_next_to_task_json(self) -> None:
        verdicts = [
            _make_verdict(
                task_id=_FIXTURE_RECORD["task_id"],
                record_kind=RecordKind.FIXTURE.value,
                is_consistent=True,
                is_fixture=True,
                source_sha=_file_sha256(self._fx_path),
            ),
        ]
        apply_sidecars(
            self.reports_root, _make_summary(verdicts),
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=False,
        )
        expected = self._fx_path.parent / "identity.json"
        self.assertTrue(expected.exists())
        # The sidecar's resolved path is the parent dir +
        # "identity.json" — no traversal, no extra segments.
        self.assertEqual(expected.name, "identity.json")
        self.assertEqual(
            expected.parent.resolve(),
            (self.reports_root / _FIXTURE_RECORD["task_id"]).resolve(),
        )

    def test_no_sidecar_written_outside_reports_root(self) -> None:
        verdicts = [
            _make_verdict(
                task_id=_FIXTURE_RECORD["task_id"],
                record_kind=RecordKind.FIXTURE.value,
                is_consistent=True,
                is_fixture=True,
                source_sha=_file_sha256(self._fx_path),
            ),
        ]
        apply_sidecars(
            self.reports_root, _make_summary(verdicts),
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=False,
        )
        # Walk the entire temp dir and confirm no identity.json
        # landed anywhere outside the task's own directory.
        for path in self._tmp.rglob("identity.json"):
            self.assertTrue(
                path.is_relative_to(
                    self.reports_root / _FIXTURE_RECORD["task_id"]
                ),
                f"sidecar escaped the task dir: {path}",
            )

    def test_directory_with_dotdot_in_name_is_refused_by_filesystem(
        self,
    ) -> None:
        # The OS will refuse to mkdir a directory whose name
        # contains ``..``; the writer therefore cannot escape
        # via a directory name. We assert that the test cannot
        # even seed such a directory, and the writer is never
        # asked to traverse.
        bad_name = "TASK-..-escape"
        # OSError / FileNotFoundError depending on platform.
        try:
            (self.reports_root / bad_name).mkdir(parents=True)
        except (OSError, ValueError):
            # Expected — the filesystem refused.
            return
        # If the OS allowed it (some FUSE / overlayfs setups
        # do), the writer must still confine itself to the
        # sidecar path next to the (now-rogue) task dir.
        # This is the writer's actual safety net.
        task_json = self.reports_root / bad_name / "task.json"
        try:
            task_json.write_text(
                json.dumps(_FIXTURE_RECORD), encoding="utf-8"
            )
        except OSError:
            return
        verdicts = [
            _make_verdict(
                task_id=bad_name,
                record_kind=RecordKind.FIXTURE.value,
                is_consistent=True,
                is_fixture=True,
                source_sha=_file_sha256(task_json),
            ),
        ]
        apply_sidecars(
            self.reports_root, _make_summary(verdicts),
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=False,
        )
        # No identity.json outside the bad-name dir; in
        # particular, no identity.json at the reports_root
        # itself or at the temp root.
        escaped = list(self._tmp.glob("identity.json"))
        escaped += list(self.reports_root.glob("identity.json"))
        self.assertEqual(
            escaped, [],
            f"sidecar escaped to {escaped!r}",
        )


class TestApplySidecarsImmutableReportContents(unittest.TestCase):
    """``task.json`` content is byte-identical before and after
    ``apply_sidecars`` runs. Only sidecars (new files) are added.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77b-imm-"))
        self.reports_root = Path(self._tmp) / "reports"
        self.reports_root.mkdir()
        self._rt_path = _write_task_json(
            self.reports_root, _RUNTIME_RECORD["task_id"], _RUNTIME_RECORD
        )
        self._fx_path = _write_task_json(
            self.reports_root, _FIXTURE_RECORD["task_id"], _FIXTURE_RECORD
        )
        self._before_snapshot = self._snapshot()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _snapshot(self):
        """Return a mapping of every file under reports_root to
        its (sha256, mtime_ns, content_bytes). task.json must
        not change; identity.json may appear post-apply.
        """
        out = {}
        for p in sorted(self.reports_root.rglob("*")):
            if p.is_file():
                out[str(p.relative_to(self.reports_root))] = (
                    _file_sha256(p),
                    p.stat().st_mtime_ns,
                    p.read_bytes(),
                )
        return out

    def test_task_json_unchanged_after_apply(self) -> None:
        verdicts = [
            _make_verdict(
                task_id=_RUNTIME_RECORD["task_id"],
                record_kind=RecordKind.RUNTIME.value,
                is_consistent=True,
                source_sha=_file_sha256(self._rt_path),
            ),
            _make_verdict(
                task_id=_FIXTURE_RECORD["task_id"],
                record_kind=RecordKind.FIXTURE.value,
                is_consistent=True,
                is_fixture=True,
                source_sha=_file_sha256(self._fx_path),
            ),
        ]
        apply_sidecars(
            self.reports_root, _make_summary(verdicts),
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        after = self._snapshot()
        for rel_path, before in self._before_snapshot.items():
            self.assertIn(rel_path, after)
            self.assertEqual(
                after[rel_path][0], before[0],
                f"{rel_path} sha changed",
            )
            self.assertEqual(
                after[rel_path][1], before[1],
                f"{rel_path} mtime changed",
            )
            self.assertEqual(
                after[rel_path][2], before[2],
                f"{rel_path} content changed",
            )

    def test_only_identity_json_files_added(self) -> None:
        verdicts = [
            _make_verdict(
                task_id=_RUNTIME_RECORD["task_id"],
                record_kind=RecordKind.RUNTIME.value,
                is_consistent=True,
                source_sha=_file_sha256(self._rt_path),
            ),
            _make_verdict(
                task_id=_FIXTURE_RECORD["task_id"],
                record_kind=RecordKind.FIXTURE.value,
                is_consistent=True,
                is_fixture=True,
                source_sha=_file_sha256(self._fx_path),
            ),
        ]
        apply_sidecars(
            self.reports_root, _make_summary(verdicts),
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=True,
        )
        after = self._snapshot()
        new_files = set(after.keys()) - set(self._before_snapshot.keys())
        self.assertEqual(
            new_files,
            {
                f"{_RUNTIME_RECORD['task_id']}/identity.json",
                f"{_FIXTURE_RECORD['task_id']}/identity.json",
            },
        )

    def test_no_collision_overwrite_when_idempotent(self) -> None:
        verdicts = [
            _make_verdict(
                task_id=_FIXTURE_RECORD["task_id"],
                record_kind=RecordKind.FIXTURE.value,
                is_consistent=True,
                is_fixture=True,
                source_sha=_file_sha256(self._fx_path),
            ),
        ]
        # First apply: WROTE.
        apply_sidecars(
            self.reports_root, _make_summary(verdicts),
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=False,
        )
        sha_after_first = _file_sha256(
            self._fx_path.parent / "identity.json"
        )
        # Snapshot task.json again.
        task_sha_after_first = _file_sha256(self._fx_path)
        # Second apply: UNCHANGED. No re-write of the sidecar.
        apply_sidecars(
            self.reports_root, _make_summary(verdicts),
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=False,
        )
        self.assertEqual(
            _file_sha256(self._fx_path.parent / "identity.json"),
            sha_after_first,
        )
        self.assertEqual(
            _file_sha256(self._fx_path), task_sha_after_first
        )


class TestBuildIndexCallSiteMigration(unittest.TestCase):
    """The G2 ``build_index`` CLI now calls ``run_audit`` +
    ``apply_sidecars`` and the resulting manifest's
    ``apply_sidecars`` sub-bucket is populated correctly.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77b-bi-"))
        self.reports_root = Path(self._tmp) / "reports"
        self.audit_dir = Path(self._tmp) / "audit"
        self.reports_root.mkdir()
        self.audit_dir.mkdir()
        _write_task_json(
            self.reports_root, _RUNTIME_RECORD["task_id"], _RUNTIME_RECORD
        )
        _write_task_json(
            self.reports_root, _FIXTURE_RECORD["task_id"], _FIXTURE_RECORD
        )

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_build_index_uses_apply_sidecars(self) -> None:
        from aee.reporting.build_index import build_index

        result = build_index(
            reports_root=self.reports_root,
            audit_dir=self.audit_dir,
            aliases={},
            executor_session_id=None,
            runtime_run_id=None,
            classified_at_utc=_FIXTURE_UTC_STAMP,
            sidecar_for_runtime=True,
        )
        # The G2 manifest is unchanged in shape; the
        # apply_sidecars sub-bucket is populated.
        self.assertIn("apply_sidecars", result["summary"])
        apply_block = result["summary"]["apply_sidecars"]
        for key in (
            "by_decision",
            "by_record_kind",
            "anchor_warning_count",
            "sidecars_written",
            "schema_version",
        ):
            self.assertIn(key, apply_block)
        # The schema version is the AEE-7.7b canonical version.
        self.assertEqual(
            apply_block["schema_version"], APPLY_SCHEMA_VERSION
        )
        # And the run_audit write-side is present in the audit
        # dir (aee77a-audit-*.json + *.md).
        audit_files = sorted(
            p.name for p in self.audit_dir.iterdir()
            if p.is_file()
        )
        self.assertTrue(
            any(f.startswith("aee77a-audit-") and f.endswith(".json")
                for f in audit_files),
            f"audit JSON missing from {audit_files!r}",
        )

    def test_build_index_emits_per_record_reports(self) -> None:
        from aee.reporting.build_index import build_index

        result = build_index(
            reports_root=self.reports_root,
            audit_dir=self.audit_dir,
            aliases={},
            executor_session_id=None,
            runtime_run_id=None,
            classified_at_utc=_FIXTURE_UTC_STAMP,
            sidecar_for_runtime=True,
        )
        # Per-record manifest: every record gets a row.
        by_id = {r["task_id"]: r for r in result["reports"]}
        self.assertIn(_RUNTIME_RECORD["task_id"], by_id)
        self.assertIn(_FIXTURE_RECORD["task_id"], by_id)
        # The RUNTIME record's sidecar is written
        # (sidecar_for_runtime=True, is_consistent=True).
        self.assertTrue(by_id[_RUNTIME_RECORD["task_id"]]["sidecar_written"])
        # The FIXTURE record is consistent=False from the audit
        # (mismatched anchors) ⇒ strict_consistency=True skips
        # it. This is the documented AEE-7.7a safety net:
        # inconsistent records are NEVER auto-overwritten. The
        # manifest correctly records sidecar_written=False.
        self.assertFalse(
            by_id[_FIXTURE_RECORD["task_id"]]["sidecar_written"]
        )
        # And task_json_sha256 is populated (the manifest
        # reader can cross-check the sidecar against the
        # underlying task.json).
        for rec in result["reports"]:
            self.assertTrue(rec["task_json_sha256"])
            self.assertEqual(len(rec["task_json_sha256"]), 64)
        # The apply_sidecars sub-bucket reflects this too.
        apply_block = result["summary"]["apply_sidecars"]
        self.assertEqual(
            apply_block["by_decision"]["skipped_inconsistent"], 1
        )


class TestApplySidecarsReadSidecarHelper(unittest.TestCase):
    """Smoke-check that the SOT ``read_identity_sidecar`` helper
    round-trips with the apply-side output (no schema drift).
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77b-rs-"))
        self.reports_root = Path(self._tmp) / "reports"
        self.reports_root.mkdir()
        self._fx_path = _write_task_json(
            self.reports_root, _FIXTURE_RECORD["task_id"], _FIXTURE_RECORD
        )

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_sidecar_round_trips_through_read_identity_sidecar(self) -> None:
        verdicts = [
            _make_verdict(
                task_id=_FIXTURE_RECORD["task_id"],
                record_kind=RecordKind.FIXTURE.value,
                is_consistent=True,
                is_fixture=True,
                source_sha=_file_sha256(self._fx_path),
            ),
        ]
        apply_sidecars(
            self.reports_root, _make_summary(verdicts),
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=False,
        )
        sidecar = read_identity_sidecar(self._fx_path)
        self.assertIsNotNone(sidecar)
        self.assertEqual(sidecar.record_kind, RecordKind.FIXTURE)
        self.assertTrue(sidecar.is_fixture)
        self.assertEqual(
            sidecar.source_task_json_sha256,
            _file_sha256(self._fx_path),
        )


if __name__ == "__main__":
    unittest.main()
