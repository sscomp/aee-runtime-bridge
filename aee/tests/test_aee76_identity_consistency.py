"""AEE-7.6 — task identity consistency validator tests.

Locks down the contract in
``aee/reporting/identity_consistency.py``:

* The validator is a pure function over a dict-shaped task
  row and produces a structured :class:`IdentityConsistencyReport`.
* Severity ladder is: ERROR (cite-killing) > WARNING
  (suspicious) > LEGACY_NULL_FIELD (informational).
* The tripwire form (:func:`tripwire_violations`) returns
  only ERROR-severity messages, so it can be wired into a
  CI / audit gate without noise.
* Backward-compat: pre-AEE-7.5 rows with both
  ``executor_session_id`` and ``runtime_run_id`` set to
  None pass (LEGACY_NULL_FIELD is not an ERROR).
"""
from __future__ import annotations

import unittest

from aee.reporting.identity_consistency import (
    ConsistencySeverity,
    IdentityConsistencyReport,
    tripwire_violations,
    validate_task_identity,
)


def _base_task(**overrides):
    """Build a minimal valid task dict.

    Defaults represent a healthy AEE-7.5+ task with both
    write-side metadata fields populated and a real
    ``run_<32hex>`` runtime anchor.
    """
    base = {
        "task_id": "TASK-20260711-0001",
        "status": "running",
        "executor_session_id": "agent:main:webchat",
        "runtime_run_id": "run_00112233445566778899aabbccddeeff",
        "hermes_run_id": "run_00112233445566778899aabbccddeeff",
        "record_kind": "runtime",
    }
    base.update(overrides)
    return base


class TestHealthyRuntimeTask(unittest.TestCase):
    """A healthy AEE-7.5+ runtime task passes with no findings."""

    def test_no_findings(self):
        report = validate_task_identity(_base_task())
        self.assertTrue(report.is_consistent)
        self.assertEqual(report.errors(), [])
        self.assertEqual(report.warnings(), [])

    def test_tripwire_passes(self):
        self.assertEqual(tripwire_violations(_base_task()), [])

    def test_task_id_in_report(self):
        report = validate_task_identity(_base_task())
        self.assertEqual(report.task_id, "TASK-20260711-0001")


class TestLegacyNullFields(unittest.TestCase):
    """Pre-AEE-7.5 rows have both write-side fields null.
    They must NOT trip the tripwire (legacy compat).
    """

    def test_legacy_null_both_fields_no_error(self):
        task = _base_task(
            executor_session_id=None,
            runtime_run_id=None,
        )
        report = validate_task_identity(task)
        self.assertTrue(report.is_consistent)
        self.assertEqual(report.errors(), [])

    def test_legacy_null_produces_legacy_finding(self):
        task = _base_task(
            executor_session_id=None,
            runtime_run_id=None,
        )
        report = validate_task_identity(task)
        legacy = report.legacy_hints()
        self.assertEqual(len(legacy), 1)
        self.assertEqual(
            legacy[0].code,
            "LEGACY_NULL_WRITE_SIDE_METADATA",
        )
        self.assertEqual(
            legacy[0].severity,
            ConsistencySeverity.LEGACY_NULL_FIELD,
        )

    def test_only_executor_session_id_populated_no_legacy(self):
        # Only one of the two write-side fields is null —
        # that's NOT a legacy row. The validator must NOT
        # raise a legacy hint, but the task is still
        # consistent.
        task = _base_task(
            executor_session_id="agent:main:webchat",
            runtime_run_id=None,
        )
        report = validate_task_identity(task)
        self.assertEqual(report.legacy_hints(), [])
        self.assertTrue(report.is_consistent)


class TestMismatchedRuntimeAnchors(unittest.TestCase):
    """The classic attribution-drift class: same record
    referenced under two different runtime ids.
    """

    def test_hermes_and_runtime_differ_is_error(self):
        task = _base_task(
            hermes_run_id="run_00112233445566778899aabbccddeeff",
            runtime_run_id="run_ffeeddccbbaa99887766554433221100",
        )
        report = validate_task_identity(task)
        self.assertFalse(report.is_consistent)
        errors = report.errors()
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].code, "MISMATCHED_RUNTIME_ANCHORS")
        self.assertEqual(errors[0].severity, ConsistencySeverity.ERROR)

    def test_hermes_and_runtime_match_is_clean(self):
        task = _base_task(
            hermes_run_id="run_00112233445566778899aabbccddeeff",
            runtime_run_id="run_00112233445566778899aabbccddeeff",
        )
        report = validate_task_identity(task)
        self.assertTrue(report.is_consistent)


class TestRuntimeRecordWithoutAnchor(unittest.TestCase):
    """A RUNTIME record without any runtime anchor is a
    cite-killer — there's no way to reference it as a
    real run.
    """

    def test_runtime_kind_no_anchor_is_error(self):
        task = _base_task(
            hermes_run_id=None,
            runtime_run_id=None,
            record_kind="runtime",
        )
        report = validate_task_identity(task)
        self.assertFalse(report.is_consistent)
        codes = [e.code for e in report.errors()]
        self.assertIn("RUNTIME_RECORD_WITHOUT_ANCHOR", codes)

    def test_legacy_null_runtime_record_also_running_no_anchor(self):
        # When status='running' AND no anchor AND record_kind=runtime
        # we get TWO errors (RUNTIME_RECORD_WITHOUT_ANCHOR +
        # RUNNING_TASK_WITHOUT_ANCHOR). This is intentional — the
        # auditor should see both signals.
        task = _base_task(
            hermes_run_id=None,
            runtime_run_id=None,
            executor_session_id=None,
            record_kind="runtime",
            status="running",
        )
        report = validate_task_identity(task)
        self.assertFalse(report.is_consistent)
        codes = [e.code for e in report.errors()]
        self.assertIn("RUNTIME_RECORD_WITHOUT_ANCHOR", codes)
        self.assertIn("RUNNING_TASK_WITHOUT_ANCHOR", codes)


class TestRunningTaskWithoutAnchor(unittest.TestCase):
    """status='running' with no runtime anchor is an
    attribution-drift signal even for non-runtime records.
    """

    def test_running_no_anchor_is_error(self):
        task = _base_task(
            status="running",
            hermes_run_id=None,
            runtime_run_id=None,
            record_kind="unknown",
        )
        report = validate_task_identity(task)
        self.assertFalse(report.is_consistent)
        codes = [e.code for e in report.errors()]
        self.assertIn("RUNNING_TASK_WITHOUT_ANCHOR", codes)


class TestFixtureRecordWithRealAnchor(unittest.TestCase):
    """A FIXTURE record with a real ``run_<32hex>`` anchor
    is suspicious — could be a fixture that was mis-classified
    or a real run that was mis-classified.
    """

    def test_fixture_with_real_hermes_anchor_is_warning(self):
        task = _base_task(
            record_kind="fixture",
            hermes_run_id="run_00112233445566778899aabbccddeeff",
        )
        report = validate_task_identity(task)
        self.assertTrue(report.is_consistent)
        warnings = report.warnings()
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].code, "FIXTURE_RECORD_WITH_REAL_ANCHOR")

    def test_fixture_with_sentinel_anchor_no_warning(self):
        # Sentinel ids (``hr-1``, ``run-traversal``) do NOT
        # match ``run_<32hex>``, so the validator does NOT
        # raise the warning. This is the expected pattern for
        # AEE-7.5 G1 fixture records.
        task = _base_task(
            record_kind="fixture",
            hermes_run_id="run-traversal",
            runtime_run_id=None,
        )
        report = validate_task_identity(task)
        self.assertTrue(report.is_consistent)
        codes = [w.code for w in report.warnings()]
        self.assertNotIn("FIXTURE_RECORD_WITH_REAL_ANCHOR", codes)


class TestEmptyTaskId(unittest.TestCase):
    """An empty task_id is a cite-killer."""

    def test_missing_task_id_is_error(self):
        task = _base_task()
        task["task_id"] = ""
        report = validate_task_identity(task)
        self.assertFalse(report.is_consistent)
        codes = [e.code for e in report.errors()]
        self.assertIn("EMPTY_TASK_ID", codes)


class TestRuntimeAnchorShape(unittest.TestCase):
    """``runtime_run_id`` should match the canonical
    ``run_<32hex>`` shape. A non-canonical value is a
    WARNING (suspicious) but not an error — the legacy
    sentinel ``hr-1`` is a valid runtime anchor in
    pre-write-side-metadata rows.
    """

    def test_canonical_shape_no_warning(self):
        task = _base_task()
        report = validate_task_identity(task)
        self.assertEqual(
            [w.code for w in report.warnings()],
            [],
        )

    def test_non_canonical_shape_is_warning(self):
        task = _base_task(
            runtime_run_id="not-a-run-id",
            hermes_run_id="not-a-run-id",
        )
        report = validate_task_identity(task)
        warnings = report.warnings()
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].code, "RUNTIME_ANCHOR_SHAPE")


class TestBackwardCompatibilityOldRows(unittest.TestCase):
    """Pre-AEE-7.5 rows from the production DB: every
    write-side field is null, hermes_run_id is a sentinel
    string like ``hr-1`` or ``run-traversal``, and
    record_kind is whatever the legacy classifier said.
    These rows MUST continue to validate cleanly
    (no ERROR-severity findings).
    """

    def test_aee6_traversal_sentinel_row(self):
        task = {
            "task_id": "TASK-20260711-0010",
            "status": "running",
            "executor_session_id": None,
            "runtime_run_id": None,
            "hermes_run_id": "run-traversal",
            "record_kind": "fixture",
        }
        report = validate_task_identity(task)
        self.assertTrue(report.is_consistent)
        # Legacy null fields produce a legacy hint, not an error.
        self.assertEqual(report.legacy_hints()[0].code, "LEGACY_NULL_WRITE_SIDE_METADATA")

    def test_aee5_legacy_row_with_real_run_id(self):
        task = {
            "task_id": "TASK-20260711-0005",
            "status": "completed",
            "executor_session_id": None,
            "runtime_run_id": None,
            "hermes_run_id": "run_00112233445566778899aabbccddeeff",
            "record_kind": "runtime",
        }
        report = validate_task_identity(task)
        self.assertTrue(report.is_consistent)
        self.assertEqual(report.legacy_hints()[0].code, "LEGACY_NULL_WRITE_SIDE_METADATA")


class TestReportSerialization(unittest.TestCase):
    """The report serializes to a JSON-safe dict."""

    def test_to_dict_shape(self):
        # Use two different real ``run_<32hex>`` ids that
        # disagree — that triggers MISMATCHED_RUNTIME_ANCHORS
        # (ERROR) but no RUNTIME_ANCHOR_SHAPE warning (both
        # match the canonical pattern).
        report = validate_task_identity(_base_task(
            hermes_run_id="run_00112233445566778899aabbccddeeff",
            runtime_run_id="run_ffeeddccbbaa99887766554433221100",
        ))
        d = report.to_dict()
        self.assertEqual(d["task_id"], "TASK-20260711-0001")
        self.assertFalse(d["is_consistent"])
        # Exactly one finding (the mismatched-anchors error).
        self.assertEqual(len(d["findings"]), 1)
        finding = d["findings"][0]
        self.assertEqual(finding["code"], "MISMATCHED_RUNTIME_ANCHORS")
        self.assertEqual(finding["severity"], "error")
        self.assertIn("message", finding)
        self.assertIn("field_path", finding)


class TestOptionalInputs(unittest.TestCase):
    """The validator tolerates missing optional fields."""

    def test_no_record_kind_defaults_to_unknown(self):
        task = {
            "task_id": "TASK-20260711-0001",
            "status": "queued",
            "executor_session_id": "x",
            "runtime_run_id": None,
            "hermes_run_id": None,
        }
        report = validate_task_identity(task)
        self.assertTrue(report.is_consistent)

    def test_record_kind_override(self):
        task = {
            "task_id": "TASK-20260711-0001",
            "status": "running",
            "executor_session_id": "x",
            "runtime_run_id": "run_00112233445566778899aabbccddeeff",
            "hermes_run_id": "run_00112233445566778899aabbccddeeff",
        }
        # No kind in the dict, but we override to RUNTIME.
        report = validate_task_identity(task, record_kind="runtime")
        self.assertTrue(report.is_consistent)

    def test_empty_string_normalized_to_none(self):
        # The dispatcher's wire-boundary normalization collapses
        # empty / whitespace strings to None. The validator
        # does the same so the audit pipeline never sees a
        # sentinel ``""`` value.
        task = {
            "task_id": "TASK-20260711-0001",
            "status": "running",
            "executor_session_id": "   ",
            "runtime_run_id": "",
            "hermes_run_id": "run_00112233445566778899aabbccddeeff",
            "record_kind": "runtime",
        }
        report = validate_task_identity(task)
        # Empty/whitespace -> None; both write-side fields are
        # effectively null, so LEGACY_NULL_FIELD fires.
        self.assertEqual(len(report.legacy_hints()), 1)


if __name__ == "__main__":
    unittest.main()
