"""AEE-5 dispatch service — unit tests.

DB-free tests that use the in-memory Runtime
Repository to verify the dispatch flow:
- select_runtime_for_job writes a DispatchRecord
- mark_dispatched / mark_dispatch_failed update
  the record
- AEE_RUNTIME_NOT_FOUND is raised with structured
  details when no Runtime matches
"""
from __future__ import annotations

import unittest

from aee.dispatch.service import DispatchService
from aee.runtimes.errors import RuntimeNotFoundError
from aee.runtimes.models import (
    DispatchStatus,
    RuntimeCapabilities,
    RuntimeDescriptor,
    RuntimeHealth,
    RuntimeHealthStatus,
    RuntimeLimits,
    TaskRuntimeRequirements,
)
from aee.runtimes.registry import RuntimeRegistry
from aee.runtimes.repository import InMemoryRuntimeRepository


def _mk(
    runtime_id: str = "r1",
    *,
    runtime_type: str = "aee_lightweight",
    caps=("task.shell",),
    labels=None,
    health: str = "healthy",
) -> RuntimeDescriptor:
    return RuntimeDescriptor(
        runtime_id=runtime_id,
        runtime_type=runtime_type,
        display_name=runtime_id,
        capabilities=RuntimeCapabilities(list(caps)),
        labels=dict(labels or {}),
        limits=RuntimeLimits(),
        health=RuntimeHealth(status=health),
    )


class TestDispatchService(unittest.TestCase):
    def setUp(self):
        self.repo = InMemoryRuntimeRepository()
        self.reg = RuntimeRegistry(self.repo)
        self.svc = DispatchService(registry=self.reg)

    def test_no_requirements_picks_default(self):
        self.reg.register_runtime(_mk("aee-lightweight-local"))
        rec = self.svc.select_runtime_for_job(
            task_id="t1", run_id="r1", runtime_requirements=None
        )
        self.assertEqual(rec.selected_runtime_id, "aee-lightweight-local")
        self.assertEqual(rec.dispatch_status, DispatchStatus.SELECTED)

    def test_no_match_writes_failed_record_and_raises(self):
        self.reg.register_runtime(_mk("r1", caps=["task.shell"]))
        with self.assertRaises(RuntimeNotFoundError) as ctx:
            self.svc.select_runtime_for_job(
                task_id="t1",
                run_id="r1",
                runtime_requirements=TaskRuntimeRequirements(
                    required_capabilities=["task.browser"],
                ),
            )
        self.assertEqual(ctx.exception.task_id, "t1")
        self.assertEqual(ctx.exception.run_id, "r1")
        # A failed dispatch record was written.
        out = self.svc.list_dispatches(task_id="t1")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].dispatch_status, DispatchStatus.FAILED)
        self.assertEqual(out[0].failure_code, "AEE_RUNTIME_NOT_FOUND")

    def test_mark_dispatched(self):
        self.reg.register_runtime(_mk("r1"))
        rec = self.svc.select_runtime_for_job(task_id="t1", run_id="r1")
        self.assertTrue(self.svc.mark_dispatched(rec.dispatch_id))
        out = self.svc.list_dispatches(task_id="t1")
        self.assertEqual(out[0].dispatch_status, DispatchStatus.DISPATCHED)

    def test_mark_dispatch_failed(self):
        self.reg.register_runtime(_mk("r1"))
        rec = self.svc.select_runtime_for_job(task_id="t1", run_id="r1")
        self.assertTrue(
            self.svc.mark_dispatch_failed(
                rec.dispatch_id,
                failure_code="AEE_ADAPTER_ERROR",
                failure_message="upstream 502",
            )
        )
        out = self.svc.list_dispatches(task_id="t1")
        self.assertEqual(out[0].dispatch_status, DispatchStatus.FAILED)
        self.assertEqual(out[0].failure_code, "AEE_ADAPTER_ERROR")

    def test_empty_requirements_treated_as_no_requirements(self):
        self.reg.register_runtime(_mk("aee-lightweight-local"))
        rec = self.svc.select_runtime_for_job(
            task_id="t1",
            run_id="r1",
            runtime_requirements=TaskRuntimeRequirements(),  # all empty
        )
        self.assertEqual(rec.selected_runtime_id, "aee-lightweight-local")

    def test_required_capability_match(self):
        self.reg.register_runtime(_mk("r1", caps=["task.shell"]))
        self.reg.register_runtime(_mk("r2", caps=["task.shell", "task.git"]))
        rec = self.svc.select_runtime_for_job(
            task_id="t1",
            run_id="r1",
            runtime_requirements=TaskRuntimeRequirements(
                required_capabilities=["task.git"],
            ),
        )
        self.assertEqual(rec.selected_runtime_id, "r2")
        self.assertEqual(rec.candidate_count, 1)
        self.assertIn("r1", rec.rejected_reasons)
        # r2 was the only candidate; the rejected list
        # is the key signal here.
        self.assertNotIn("r2", rec.rejected_reasons)


if __name__ == "__main__":
    unittest.main()
