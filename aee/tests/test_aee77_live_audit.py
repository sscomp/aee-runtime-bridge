"""AEE-7.7a — Live-Report Audit (targeted tests).

Covers the AEE-7.7a ``aee.audit.run_audit`` entry point + the
``AuditSummary`` / ``PerTaskVerdict`` dataclasses.

Test placement rationale
------------------------
The audit is read-only against ``reports/`` and never touches
``data/dispatcher.db``.  No G4 guard is required at module
import time (the audit does not import ``dispatcher.db``).
The tests run in their own tempdir under ``tmp_path`` (pytest)
or ``tempfile.mkdtemp`` (unittest fallback) — the legacy
``tests/_live_db_guard.py`` is not needed here.

The tests verify:

1. **Round-trip** — 3 hand-crafted ``task.json`` files
   (RUNTIME / FIXTURE / UNKNOWN) plus 1 malformed record
   yield the expected verdicts + the expected aggregate
   counts.
2. **Schema stability** — the JSON output has a stable shape
   (schema_version, audited_at_utc, reports_root, by_*,
   verdicts[]).  The MD output has the expected headers.
3. **Read-only contract** — running the audit does not create
   / modify / delete any file under the source ``reports/``
   dir; only the output dir gains new files.
4. **No live DB touch** — running the audit does not open
   ``data/dispatcher.db`` at all (the audit module does not
   import ``dispatcher``).
5. **Determinism** — same input + same utc_stamp yields
   byte-identical output (sorted verdicts, sorted finding
   code counts).
6. **Empty reports root** — a non-existent reports root
   yields an empty summary + a valid JSON/MD (no exceptions).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


# Make ``aee`` importable when running via ``python -m unittest
# aee.tests.test_aee77_live_audit`` from outside the repo root.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


from aee.audit import AuditSummary, PerTaskVerdict, run_audit  # noqa: E402
from aee.audit.live_audit import AUDIT_SCHEMA_VERSION  # noqa: E402


def _write_task_json(
    root: Path,
    task_id: str,
    payload: dict,
) -> Path:
    """Write a single ``task.json`` under ``root/<task_id>/``.

    Returns the path to the written file.  Used by the
    round-trip tests to seed the reports dir.
    """
    d = root / task_id
    d.mkdir(parents=True, exist_ok=True)
    p = d / "task.json"
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return p


# A canonical RUNTIME record.  hermes_run_id matches the
# ``run_<32hex>`` shape; no fixture signals fire.
_RUNTIME_RECORD = {
    "task_id": "TASK-20260711-9999",
    "title": "Real AEE-7.7a audit smoke",
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
    "input_text": "Run the AEE-7.7a live-report audit.",
    "hermes_run_id": "run_0123456789abcdef0123456789abcdef",
    "executor_session_id": "AEE-7.7A-AUDIT-20260711",
    "runtime_run_id": "run_0123456789abcdef0123456789abcdef",
    "repo_root": "/home/ubuntu/hermes-runtime-bridge",
}

# A canonical FIXTURE record.  hermes_run_id is in the
# default sentinel set ("hr-1").
_FIXTURE_RECORD = {
    "task_id": "TASK-20260711-9998",
    "title": "aee6-traversal",
    "type": "coding",
    "priority": 50,
    "owner": "m2",
    "status": "running",
    "progress_pct": 5,
    "input_text": "Read /tmp/../etc/whatever now",
    "hermes_run_id": "hr-1",
    "executor_session_id": "AEE-7.6-TRAVERSAL-20260711",
    "runtime_run_id": "run-audit-fixture",
    "repo_root": "/home/ubuntu/hermes-runtime-bridge",
}

# A canonical UNKNOWN record.  No fixture signals fire AND
# hermes_run_id is NOT in the run_<32hex> shape.
_UNKNOWN_RECORD = {
    "task_id": "TASK-20260711-9997",
    "title": "Mystery audit smoke",
    "type": "coding",
    "priority": 50,
    "owner": "m2",
    "status": "completed",
    "progress_pct": 100,
    "input_text": "A normal-looking record with a non-canonical run id",
    "hermes_run_id": "weird-custom-id-2026",
    "executor_session_id": "AEE-7.7A-AUDIT-UNKNOWN",
    "runtime_run_id": "weird-custom-id-2026",
    "repo_root": "/home/ubuntu/hermes-runtime-bridge",
}


class TestRunAuditRoundTrip(unittest.TestCase):
    """End-to-end test of run_audit with 4 hand-crafted records.

    Verifies the verdict shape, the aggregate counts, and the
    deterministic ordering.  The audit is run against a
    tempdir; the live ``data/dispatcher.db`` is never opened.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="aee77a-rt-")
        self.reports_root = Path(self._tmp) / "reports"
        self.output_dir = Path(self._tmp) / "out"
        self.reports_root.mkdir()
        self.output_dir.mkdir()

        # Seed: 3 well-formed + 1 malformed.
        _write_task_json(self.reports_root, "TASK-20260711-9999", _RUNTIME_RECORD)
        _write_task_json(self.reports_root, "TASK-20260711-9998", _FIXTURE_RECORD)
        _write_task_json(self.reports_root, "TASK-20260711-9997", _UNKNOWN_RECORD)
        # Malformed — write a non-dict JSON to test the
        # ``MALFORMED_TASK_JSON`` finding path.
        bad_dir = self.reports_root / "TASK-20260711-9996"
        bad_dir.mkdir(parents=True, exist_ok=True)
        with open(bad_dir / "task.json", "w", encoding="utf-8") as fh:
            fh.write("[1, 2, 3]")  # list, not dict

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_run_audit_returns_4_verdicts(self) -> None:
        summary, json_path, md_path = run_audit(
            self.reports_root,
            self.output_dir,
            utc_stamp="2026-07-11T12:00:00Z",
        )
        self.assertEqual(len(summary.verdicts), 4)

    def test_run_audit_record_kinds_classified(self) -> None:
        summary, _, _ = run_audit(
            self.reports_root,
            self.output_dir,
            utc_stamp="2026-07-11T12:00:00Z",
        )
        by_kind = {v.task_id: v.record_kind for v in summary.verdicts}
        self.assertEqual(by_kind["TASK-20260711-9999"], "runtime")
        self.assertEqual(by_kind["TASK-20260711-9998"], "fixture")
        self.assertEqual(by_kind["TASK-20260711-9997"], "unknown")
        # Malformed record → unknown per the audit's
        # never-drop policy.
        self.assertEqual(by_kind["TASK-20260711-9996"], "unknown")

    def test_run_audit_aggregate_counts(self) -> None:
        summary, _, _ = run_audit(
            self.reports_root,
            self.output_dir,
            utc_stamp="2026-07-11T12:00:00Z",
        )
        self.assertEqual(summary.by_record_kind["runtime"], 1)
        self.assertEqual(summary.by_record_kind["fixture"], 1)
        self.assertEqual(summary.by_record_kind["unknown"], 2)

    def test_run_audit_finds_malformed_record(self) -> None:
        summary, _, _ = run_audit(
            self.reports_root,
            self.output_dir,
            utc_stamp="2026-07-11T12:00:00Z",
        )
        malformed = [
            v for v in summary.verdicts
            if v.task_id == "TASK-20260711-9996"
        ]
        self.assertEqual(len(malformed), 1)
        self.assertFalse(malformed[0].is_consistent)
        codes = [f["code"] for f in malformed[0].findings]
        self.assertIn("MALFORMED_TASK_JSON", codes)

    def test_run_audit_fixture_inconsistent(self) -> None:
        """The FIXTURE record's hermes_run_id = 'hr-1' (a
        sentinel).  validate_task_identity should flag it
        because the runtime anchor shape is wrong.
        """
        summary, _, _ = run_audit(
            self.reports_root,
            self.output_dir,
            utc_stamp="2026-07-11T12:00:00Z",
        )
        fixture = [
            v for v in summary.verdicts
            if v.task_id == "TASK-20260711-9998"
        ][0]
        self.assertTrue(fixture.is_fixture)
        # The fixture record's runtime_run_id is a
        # non-canonical shape, so the consistency check
        # produces at least one WARNING finding.  But the
        # is_consistent flag is True iff no ERROR.  A
        # mismatched-anchor between hermes_run_id and
        # runtime_run_id IS an ERROR (per AEE-7.6 G3
        # semantics).  Verify the dangerous combination.
        self.assertTrue(
            summary.fixture_inconsistent_count >= 1,
            "fixture record should be inconsistent "
            "(mismatched anchors)",
        )


class TestRunAuditOutputFiles(unittest.TestCase):
    """Verify the JSON + Markdown output shape."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="aee77a-out-")
        self.reports_root = Path(self._tmp) / "reports"
        self.output_dir = Path(self._tmp) / "out"
        self.reports_root.mkdir()
        self.output_dir.mkdir()
        _write_task_json(self.reports_root, "TASK-20260711-9999", _RUNTIME_RECORD)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_json_has_expected_keys(self) -> None:
        _, json_path, _ = run_audit(
            self.reports_root,
            self.output_dir,
            utc_stamp="2026-07-11T12:00:00Z",
        )
        with open(json_path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        for key in (
            "schema_version",
            "audited_at_utc",
            "reports_root",
            "total_records",
            "by_record_kind",
            "by_consistency",
            "finding_code_counts",
            "fixture_inconsistent_count",
            "verdicts",
        ):
            self.assertIn(key, doc)
        self.assertEqual(doc["schema_version"], AUDIT_SCHEMA_VERSION)
        self.assertEqual(doc["audited_at_utc"], "2026-07-11T12:00:00Z")
        self.assertEqual(doc["total_records"], 1)

    def test_markdown_has_expected_headers(self) -> None:
        _, _, md_path = run_audit(
            self.reports_root,
            self.output_dir,
            utc_stamp="2026-07-11T12:00:00Z",
        )
        with open(md_path, "r", encoding="utf-8") as fh:
            md = fh.read()
        for header in (
            "# AEE-7.7a Live-Report Audit",
            "## By record kind",
            "## By consistency verdict",
            "## Finding code counts",
        ):
            self.assertIn(header, md)

    def test_output_filenames_use_utc_stamp(self) -> None:
        _, json_path, md_path = run_audit(
            self.reports_root,
            self.output_dir,
            utc_stamp="2026-07-11T12:00:00Z",
        )
        # ':' is replaced with '-' for portability.
        self.assertIn("aee77a-audit-2026-07-11T12-00-00Z.json", str(json_path))
        self.assertIn("aee77a-audit-2026-07-11T12-00-00Z.md", str(md_path))


class TestRunAuditReadOnlyContract(unittest.TestCase):
    """Verify the audit is read-only against ``reports/``."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="aee77a-ro-")
        self.reports_root = Path(self._tmp) / "reports"
        self.output_dir = Path(self._tmp) / "out"
        self.reports_root.mkdir()
        self.output_dir.mkdir()
        _write_task_json(self.reports_root, "TASK-20260711-9999", _RUNTIME_RECORD)
        # Snapshot the reports dir file list before the audit.
        self._before_files = sorted(
            str(p.relative_to(self.reports_root))
            for p in self.reports_root.rglob("*")
            if p.is_file()
        )

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_reports_dir_unchanged_after_audit(self) -> None:
        run_audit(
            self.reports_root,
            self.output_dir,
            utc_stamp="2026-07-11T12:00:00Z",
        )
        after_files = sorted(
            str(p.relative_to(self.reports_root))
            for p in self.reports_root.rglob("*")
            if p.is_file()
        )
        self.assertEqual(self._before_files, after_files)

    def test_output_dir_gains_exactly_2_files(self) -> None:
        before = sorted(os.listdir(self.output_dir))
        run_audit(
            self.reports_root,
            self.output_dir,
            utc_stamp="2026-07-11T12:00:00Z",
        )
        after = sorted(os.listdir(self.output_dir))
        # Before: empty.  After: exactly 2 files (json + md).
        self.assertEqual(before, [])
        self.assertEqual(len(after), 2)
        self.assertTrue(any(p.endswith(".json") for p in after))
        self.assertTrue(any(p.endswith(".md") for p in after))


class TestRunAuditEmptyAndEdgeCases(unittest.TestCase):
    """Edge cases: empty dir, non-existent root, empty dir with
    a subdir that has no task.json.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="aee77a-edge-")
        self.output_dir = Path(self._tmp) / "out"
        self.output_dir.mkdir()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_non_existent_reports_root_yields_empty_summary(self) -> None:
        missing = Path(self._tmp) / "does-not-exist"
        summary, json_path, md_path = run_audit(
            missing,
            self.output_dir,
            utc_stamp="2026-07-11T12:00:00Z",
        )
        self.assertEqual(len(summary.verdicts), 0)
        # Aggregate buckets are 0.
        self.assertEqual(summary.by_record_kind["runtime"], 0)
        self.assertEqual(summary.by_record_kind["fixture"], 0)
        self.assertEqual(summary.by_record_kind["unknown"], 0)
        self.assertEqual(summary.fixture_inconsistent_count, 0)
        # Output files exist + are valid JSON / MD.
        self.assertTrue(json_path.exists())
        self.assertTrue(md_path.exists())
        with open(json_path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["total_records"], 0)

    def test_subdir_without_task_json_is_skipped(self) -> None:
        reports = Path(self._tmp) / "reports"
        reports.mkdir()
        (reports / "TASK-EMPTY").mkdir()
        # No task.json inside.
        summary, _, _ = run_audit(
            reports,
            self.output_dir,
            utc_stamp="2026-07-11T12:00:00Z",
        )
        self.assertEqual(len(summary.verdicts), 0)


class TestRunAuditDeterminism(unittest.TestCase):
    """Same input + same utc_stamp ⇒ byte-identical output."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="aee77a-det-")
        self.reports_root = Path(self._tmp) / "reports"
        self.output_dir_a = Path(self._tmp) / "out-a"
        self.output_dir_b = Path(self._tmp) / "out-b"
        self.reports_root.mkdir()
        self.output_dir_a.mkdir()
        self.output_dir_b.mkdir()
        _write_task_json(self.reports_root, "TASK-20260711-9999", _RUNTIME_RECORD)
        _write_task_json(self.reports_root, "TASK-20260711-9998", _FIXTURE_RECORD)
        _write_task_json(self.reports_root, "TASK-20260711-9997", _UNKNOWN_RECORD)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_two_runs_byte_identical(self) -> None:
        _, ja, ma = run_audit(
            self.reports_root,
            self.output_dir_a,
            utc_stamp="2026-07-11T12:00:00Z",
        )
        _, jb, mb = run_audit(
            self.reports_root,
            self.output_dir_b,
            utc_stamp="2026-07-11T12:00:00Z",
        )
        with open(ja, "rb") as fa, open(jb, "rb") as fb:
            self.assertEqual(fa.read(), fb.read())
        with open(ma, "rb") as fa, open(mb, "rb") as fb:
            self.assertEqual(fa.read(), fb.read())


class TestRunAuditNoDispatcherImport(unittest.TestCase):
    """The audit module must NOT introduce dispatcher into sys.modules.

    The AEE-7.6 G4 guard's whole point is that the audit path should
    be safe even when the live bridge is running.  We assert the
    actual invariant — that loading aee.audit / aee.audit.live_audit
    does NOT add any ``dispatcher`` or ``dispatcher.*`` module to
    ``sys.modules`` — rather than the over-strict "sys.modules must
    not contain dispatcher at all" (which would fail in any test
    sweep that legitimately imports the bridge's app elsewhere).
    """

    def test_loading_aee_audit_does_not_introduce_dispatcher(self) -> None:
        # Force a fresh interpreter view: clear any cached aee.audit
        # imports so the snapshot is meaningful.
        for mod_name in list(sys.modules):
            if mod_name == "aee.audit" or mod_name.startswith("aee.audit."):
                del sys.modules[mod_name]

        before = set(sys.modules.keys())
        import aee.audit  # noqa: F401
        import aee.audit.live_audit  # noqa: F401
        after = set(sys.modules.keys())
        new_dispatcher = [
            m
            for m in (after - before)
            if m == "dispatcher" or m.startswith("dispatcher.")
        ]
        self.assertEqual(
            new_dispatcher,
            [],
            f"aee.audit must not introduce dispatcher.* into sys.modules; "
            f"got: {new_dispatcher}",
        )


if __name__ == "__main__":
    unittest.main()
