"""AEE-5 Job lifecycle integration test.

End-to-end test that drives a Job through create,
select Runtime (via the dispatch service), and
verify the dispatch record landed. Uses the real
`dispatcher.db` connection (with a clean DB).
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DISPATCHER_API_KEY", "test-key")
os.environ.setdefault("BRIDGE_API_KEY", "test-key")

from dispatcher import db as dispatcher_db  # noqa: E402
from aee.dispatch.service import DispatchService  # noqa: E402
from aee.runtimes.models import (  # noqa: E402
    RuntimeCapabilities,
    RuntimeDescriptor,
    RuntimeHealth,
    RuntimeHealthStatus,
    RuntimeLimits,
    TaskRuntimeRequirements,
)
from aee.runtimes.registry import RuntimeRegistry  # noqa: E402
from aee.runtimes.repository import SqliteRuntimeRepository  # noqa: E402


def _mk(runtime_id: str = "r1", **kwargs) -> RuntimeDescriptor:
    if "caps" in kwargs:
        caps = kwargs.pop("caps")
        kwargs.setdefault("capabilities", RuntimeCapabilities(list(caps)))
    base = dict(
        runtime_id=runtime_id,
        runtime_type="aee_lightweight",
        display_name=runtime_id,
        capabilities=RuntimeCapabilities(["task.shell"]),
        limits=RuntimeLimits(),
        health=RuntimeHealth(),
    )
    base.update(kwargs)
    return RuntimeDescriptor(**base)


def _reset_db() -> None:
    p = dispatcher_db.DB_PATH
    for ext in ("", "-wal", "-shm"):
        f = p.with_name(p.name + ext)
        if f.exists():
            try:
                f.unlink()
            except Exception:
                pass
    dispatcher_db._local.conn = None  # type: ignore[attr-defined]
    dispatcher_db._initialized = False  # type: ignore[attr-defined]


class TestAee5JobLifecycle(unittest.TestCase):
    """End-to-end AEE-5 dispatch flow tests.

    These mirror the AEE-5 task spec §8.2
    integration tests.
    """

    def setUp(self):
        _reset_db()
        from aee.runtimes import registry as _reg_mod
        _reg_mod.runtime_registry = RuntimeRegistry(
            SqliteRuntimeRepository()
        )
        self.reg = _reg_mod.runtime_registry
        self.svc = DispatchService(registry=self.reg)

    # --- Spec §8.2 #1: AEE startup, auto-register built-in
    def test_aee_startup_registers_builtin(self):
        from aee.runtimes.registry import bootstrap_default_runtimes
        bootstrap_default_runtimes(force=False)
        ids = {r.runtime_id for r in self.reg.list_runtimes()}
        self.assertIn("aee-lightweight-local", ids)
        d = self.reg.get_runtime("aee-lightweight-local")
        self.assertEqual(d.runtime_type, "aee_lightweight")

    # --- Spec §8.2 #2: task without runtime_requirements dispatches to default
    def test_task_without_requirements_dispatches_to_default(self):
        self.reg.register_runtime(_mk("aee-lightweight-local"))
        rec = self.svc.select_runtime_for_job(
            task_id="t1", run_id="r1", runtime_requirements=None
        )
        self.assertEqual(rec.selected_runtime_id, "aee-lightweight-local")

    # --- Spec §8.2 #3: task with required_capability dispatches
    def test_task_with_capability_requirement_dispatches(self):
        self.reg.register_runtime(_mk("r-shell", caps=["task.shell", "task.git"]))
        rec = self.svc.select_runtime_for_job(
            task_id="t1",
            run_id="r1",
            runtime_requirements=TaskRuntimeRequirements(
                required_capabilities=["task.git"],
            ),
        )
        self.assertEqual(rec.selected_runtime_id, "r-shell")

    # --- Spec §8.2 #4: task with non-existent capability fails
    def test_task_with_nonexistent_capability_fails(self):
        from aee.runtimes.errors import RuntimeNotFoundError
        self.reg.register_runtime(_mk("r1", caps=["task.shell"]))
        with self.assertRaises(RuntimeNotFoundError):
            self.svc.select_runtime_for_job(
                task_id="t1",
                run_id="r1",
                runtime_requirements=TaskRuntimeRequirements(
                    required_capabilities=["task.browser"],
                ),
            )

    # --- Spec §8.2 #5: disable default, no longer selected
    def test_disabled_default_not_selected(self):
        self.reg.register_runtime(_mk("aee-lightweight-local"))
        self.reg.register_runtime(_mk("r-alt", caps=["task.shell"]))
        self.reg.set_runtime_enabled("aee-lightweight-local", False)
        rec = self.svc.select_runtime_for_job(
            task_id="t1", run_id="r1", runtime_requirements=None
        )
        self.assertEqual(rec.selected_runtime_id, "r-alt")

    # --- Spec §8.2 #6: re-enable restores dispatch
    def test_re_enable_restores_dispatch(self):
        self.reg.register_runtime(_mk("aee-lightweight-local"))
        self.reg.register_runtime(_mk("r-alt", caps=["task.shell"]))
        self.reg.set_runtime_enabled("aee-lightweight-local", False)
        self.reg.set_runtime_enabled("aee-lightweight-local", True)
        rec = self.svc.select_runtime_for_job(
            task_id="t1", run_id="r1", runtime_requirements=None
        )
        self.assertEqual(rec.selected_runtime_id, "aee-lightweight-local")

    # --- Spec §8.2 #7: dispatch record correctly written
    def test_dispatch_record_written(self):
        self.reg.register_runtime(_mk("aee-lightweight-local"))
        rec = self.svc.select_runtime_for_job(
            task_id="t1", run_id="r1", runtime_requirements=None
        )
        out = self.svc.list_dispatches(task_id="t1")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].dispatch_id, rec.dispatch_id)
        # Audit table also has the row.
        row = dispatcher_db.get_conn().execute(
            "SELECT dispatch_status, selected_runtime_id, "
            "selected_runtime_type, candidate_count FROM "
            "dispatch_records WHERE dispatch_id = ?",
            (rec.dispatch_id,),
        ).fetchone()
        self.assertEqual(row["dispatch_status"], "selected")
        self.assertEqual(row["selected_runtime_id"], "aee-lightweight-local")
        self.assertEqual(row["selected_runtime_type"], "aee_lightweight")
        self.assertEqual(row["candidate_count"], 1)

    # --- AEE-4 backward compat: task without runtime_requirements still works
    def test_backward_compat_no_requirements_uses_default(self):
        self.reg.register_runtime(_mk("aee-lightweight-local"))
        rec = self.svc.select_runtime_for_job(
            task_id="t1", run_id="r1", runtime_requirements=None
        )
        # Selection did not raise; the record was
        # written; the chosen Runtime is the default.
        self.assertIsNotNone(rec.selected_runtime_id)
        self.assertEqual(rec.selected_runtime_id, "aee-lightweight-local")

    def test_backward_compat_existing_api_still_works(self):
        """The pre-AEE-5 `POST /jobs` and `POST /runs`
        API must continue to work unchanged. We
        exercise the `_validate_create_job` and the
        existing `manager.create` flow."""
        from aee.api.jobs import _validate_create_job
        from dispatcher.manager import TaskManager
        m = TaskManager()
        body = {
            "title": "test",
            "input": "hello",
            "runtime_type": "hermes",
        }
        out = _validate_create_job(body)
        self.assertEqual(out["title"], "test")
        self.assertEqual(out["runtime_type"], "hermes")
        # manager.create() still works.
        t = m.create(
            title="t", type="ops", input_text="x",
            required_capabilities=[],
        )
        self.assertIsNotNone(t.task_id)

    def test_dispatch_record_after_failed_match_records_reasons(self):
        """Spec §4.5: a no-match scenario must persist
        the rejected reasons for auditability."""
        from aee.runtimes.errors import RuntimeNotFoundError
        self.reg.register_runtime(_mk("r1", caps=["task.x"]))
        with self.assertRaises(RuntimeNotFoundError):
            self.svc.select_runtime_for_job(
                task_id="t1", run_id="r1",
                runtime_requirements=TaskRuntimeRequirements(
                    required_capabilities=["task.browser"],
                ),
            )
        out = self.svc.list_dispatches(task_id="t1")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].dispatch_status, "failed")
        self.assertEqual(out[0].failure_code, "AEE_RUNTIME_NOT_FOUND")
        # The rejected reasons are persisted as JSON.
        self.assertIn("r1", out[0].rejected_reasons)
        self.assertTrue(
            any("missing" in r.lower() for r in out[0].rejected_reasons["r1"])
        )


if __name__ == "__main__":
    unittest.main()
