"""AEE-5 Runtime models — unit tests.

Pure domain tests. No DB, no FastAPI. Verify that
RuntimeDescriptor, TaskRuntimeRequirements,
RuntimeSelectionResult, DispatchRecord, and the
related dataclasses round-trip through `to_dict()` /
`from_dict()` and respect the AEE-5 task spec fields.
"""
from __future__ import annotations

import unittest

from aee.runtimes.models import (
    DispatchRecord,
    DispatchStatus,
    RuntimeCapabilities,
    RuntimeDescriptor,
    RuntimeHealth,
    RuntimeHealthStatus,
    RuntimeLimits,
    RuntimeSelectionResult,
    TaskRuntimeRequirements,
)


class TestRuntimeCapabilities(unittest.TestCase):
    def test_normalization_lowercase_trim_dedupe_sort(self):
        c = RuntimeCapabilities(["  Task.Shell ", "TASK.shell", "task.python", ""])
        self.assertEqual(c.normalized(), ["task.python", "task.shell"])

    def test_to_list_is_canonical(self):
        c = RuntimeCapabilities(["b", "a", "b"])
        self.assertEqual(c.to_list(), ["a", "b"])

    def test_empty(self):
        self.assertEqual(RuntimeCapabilities().to_list(), [])
        self.assertEqual(RuntimeCapabilities(None).to_list(), [])  # type: ignore[arg-type]


class TestRuntimeLimits(unittest.TestCase):
    def test_defaults(self):
        lim = RuntimeLimits()
        self.assertEqual(lim.max_concurrency, 1)
        self.assertEqual(lim.timeout_seconds, 1800)

    def test_from_dict_missing(self):
        lim = RuntimeLimits.from_dict(None)
        self.assertEqual(lim.max_concurrency, 1)
        self.assertEqual(lim.timeout_seconds, 1800)

    def test_from_dict_partial(self):
        lim = RuntimeLimits.from_dict({"max_concurrency": 5})
        self.assertEqual(lim.max_concurrency, 5)
        self.assertEqual(lim.timeout_seconds, 1800)

    def test_to_dict_round_trip(self):
        lim = RuntimeLimits(max_concurrency=4, timeout_seconds=600)
        d = lim.to_dict()
        self.assertEqual(d, {"max_concurrency": 4, "timeout_seconds": 600})


class TestRuntimeHealth(unittest.TestCase):
    def test_default_unknown(self):
        h = RuntimeHealth()
        self.assertEqual(h.status, RuntimeHealthStatus.UNKNOWN)
        self.assertIsNone(h.last_checked_at)

    def test_from_dict_invalid_status_falls_back_to_unknown(self):
        h = RuntimeHealth.from_dict({"status": "BOGUS"})
        self.assertEqual(h.status, RuntimeHealthStatus.UNKNOWN)

    def test_from_dict_valid(self):
        h = RuntimeHealth.from_dict({
            "status": "healthy",
            "last_checked_at": "2026-07-10T00:00:00Z",
            "message": "ok",
        })
        self.assertEqual(h.status, "healthy")
        self.assertEqual(h.message, "ok")


class TestRuntimeDescriptor(unittest.TestCase):
    def test_to_dict_includes_all_required_fields(self):
        d = RuntimeDescriptor(
            runtime_id="r1",
            runtime_type="aee_lightweight",
            display_name="Test",
            capabilities=RuntimeCapabilities(["task.shell"]),
            labels={"environment": "local"},
        )
        out = d.to_dict()
        for k in (
            "runtime_id",
            "runtime_type",
            "display_name",
            "version",
            "enabled",
            "endpoint",
            "capabilities",
            "labels",
            "limits",
            "health",
            "registered_at",
            "updated_at",
        ):
            self.assertIn(k, out, f"missing {k}")
        self.assertEqual(out["capabilities"], ["task.shell"])
        self.assertEqual(out["labels"], {"environment": "local"})

    def test_from_dict_round_trip(self):
        original = RuntimeDescriptor(
            runtime_id="r1",
            runtime_type="aee_lightweight",
            display_name="Test",
            capabilities=RuntimeCapabilities(["task.shell", "task.python"]),
            labels={"env": "local", "trust": "internal"},
            limits=RuntimeLimits(max_concurrency=3, timeout_seconds=300),
            health=RuntimeHealth(status="healthy", last_checked_at="2026-07-10T00:00:00Z"),
            version="1.2.3",
            endpoint="local",
        )
        d = original.to_dict()
        rt = RuntimeDescriptor.from_dict(d)
        self.assertEqual(rt.runtime_id, original.runtime_id)
        self.assertEqual(rt.runtime_type, original.runtime_type)
        self.assertEqual(rt.capabilities.to_list(), original.capabilities.to_list())
        self.assertEqual(rt.labels, original.labels)
        self.assertEqual(rt.limits.max_concurrency, 3)
        self.assertEqual(rt.limits.timeout_seconds, 300)
        self.assertEqual(rt.health.status, "healthy")
        self.assertEqual(rt.version, "1.2.3")

    def test_from_dict_non_dict_raises(self):
        with self.assertRaises(ValueError):
            RuntimeDescriptor.from_dict([])  # type: ignore[arg-type]

    def test_label_coercion_to_strings(self):
        d = RuntimeDescriptor.from_dict({
            "runtime_id": "r1",
            "runtime_type": "x",
            "labels": {"a": 1, "b": 2.5},
        })
        self.assertEqual(d.labels, {"a": "1", "b": "2.5"})


class TestTaskRuntimeRequirements(unittest.TestCase):
    def test_is_empty_when_all_unset(self):
        self.assertTrue(TaskRuntimeRequirements().is_empty())
        self.assertTrue(TaskRuntimeRequirements(
            runtime_type="",
            required_capabilities=[],
            preferred_capabilities=[],
            required_labels={},
            preferred_runtime_ids=[],
            excluded_runtime_ids=[],
        ).is_empty())

    def test_not_empty_when_set(self):
        self.assertFalse(TaskRuntimeRequirements(runtime_type="aee_lightweight").is_empty())
        self.assertFalse(TaskRuntimeRequirements(required_capabilities=["task.shell"]).is_empty())
        self.assertFalse(TaskRuntimeRequirements(required_labels={"env": "local"}).is_empty())
        self.assertFalse(TaskRuntimeRequirements(excluded_runtime_ids=["r1"]).is_empty())

    def test_from_dict_round_trip(self):
        original = TaskRuntimeRequirements(
            runtime_type="aee_lightweight",
            required_capabilities=["task.shell"],
            preferred_capabilities=["task.git"],
            required_labels={"environment": "local"},
            preferred_runtime_ids=["r1"],
            excluded_runtime_ids=["r2"],
        )
        d = original.to_dict()
        rt = TaskRuntimeRequirements.from_dict(d)
        self.assertEqual(rt.runtime_type, "aee_lightweight")
        self.assertEqual(rt.required_capabilities, ["task.shell"])
        self.assertEqual(rt.required_labels, {"environment": "local"})
        self.assertEqual(rt.preferred_runtime_ids, ["r1"])
        self.assertEqual(rt.excluded_runtime_ids, ["r2"])

    def test_from_dict_handles_none_and_invalid(self):
        self.assertEqual(TaskRuntimeRequirements.from_dict(None).to_dict(),
                         TaskRuntimeRequirements().to_dict())
        self.assertEqual(TaskRuntimeRequirements.from_dict("bogus").to_dict(),
                         TaskRuntimeRequirements().to_dict())


class TestRuntimeSelectionResult(unittest.TestCase):
    def test_to_dict_shape(self):
        r = RuntimeSelectionResult(
            selected_runtime_id="r1",
            selection_reason="matched",
            candidate_count=2,
            evaluated_runtime_ids=["r1", "r2"],
            rejected_reasons={"r2": ["missing capability: task.shell"]},
        )
        d = r.to_dict()
        self.assertEqual(d["selected_runtime_id"], "r1")
        self.assertEqual(d["candidate_count"], 2)
        self.assertEqual(d["rejected_reasons"], {"r2": ["missing capability: task.shell"]})

    def test_is_matched(self):
        self.assertTrue(RuntimeSelectionResult(selected_runtime_id="r1").is_matched)
        self.assertFalse(RuntimeSelectionResult(selected_runtime_id=None).is_matched)


class TestDispatchRecord(unittest.TestCase):
    def test_to_dict_has_required_aee5_fields(self):
        rec = DispatchRecord(
            dispatch_id="d1",
            task_id="t1",
            run_id="r1",
            selected_runtime_id="rt1",
            selected_runtime_type="aee_lightweight",
            selection_reason="matched",
            candidate_count=1,
            attempt_number=1,
            dispatched_at="2026-07-10T00:00:00Z",
            dispatch_status=DispatchStatus.SELECTED,
        )
        d = rec.to_dict()
        for k in (
            "dispatch_id", "task_id", "run_id",
            "selected_runtime_id", "selected_runtime_type",
            "selection_reason", "candidate_count",
            "attempt_number", "dispatched_at", "dispatch_status",
            "failure_code", "failure_message",
            "rejected_reasons",
        ):
            self.assertIn(k, d, f"missing {k}")

    def test_from_row_string_json(self):
        row = {
            "dispatch_id": "d1",
            "task_id": "t1",
            "run_id": "r1",
            "selected_runtime_id": "rt1",
            "selected_runtime_type": "aee_lightweight",
            "selection_reason": "ok",
            "candidate_count": 2,
            "attempt_number": 1,
            "dispatched_at": "2026-07-10T00:00:00Z",
            "dispatch_status": "selected",
            "failure_code": None,
            "failure_message": None,
            "rejected_reasons_json": '{"rt2": ["missing cap"]}',
        }
        rec = DispatchRecord.from_row(row)
        self.assertEqual(rec.dispatch_id, "d1")
        self.assertEqual(rec.rejected_reasons, {"rt2": ["missing cap"]})

    def test_from_row_dict_form(self):
        row = {
            "dispatch_id": "d1",
            "task_id": "t1",
            "run_id": "r1",
            "selected_runtime_id": "rt1",
            "selected_runtime_type": "aee_lightweight",
            "selection_reason": "ok",
            "candidate_count": 1,
            "attempt_number": 1,
            "dispatched_at": "2026-07-10T00:00:00Z",
            "dispatch_status": "dispatched",
            "rejected_reasons": {"rt2": ["missing"]},
        }
        rec = DispatchRecord.from_row(row)
        self.assertEqual(rec.dispatch_status, "dispatched")
        self.assertEqual(rec.rejected_reasons, {"rt2": ["missing"]})


if __name__ == "__main__":
    unittest.main()
