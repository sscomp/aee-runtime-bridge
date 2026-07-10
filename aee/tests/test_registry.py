"""AEE-5 Runtime Registry service — unit tests.

DB-free tests using `InMemoryRuntimeRepository`.
Cover the AEE-5 task spec §4.2 contract.
"""
from __future__ import annotations

import unittest

from aee.runtimes.errors import (
    RuntimeNotFoundError,
    RuntimeRegistryError,
    RuntimeValidationError,
)
from aee.runtimes.models import (
    RuntimeCapabilities,
    RuntimeDescriptor,
    RuntimeHealth,
    RuntimeHealthStatus,
    RuntimeLimits,
)
from aee.runtimes.registry import (
    RuntimeRegistry,
    bootstrap_default_runtimes,
)
from aee.runtimes.repository import InMemoryRuntimeRepository


def _mk(runtime_id: str = "r1", **kwargs) -> RuntimeDescriptor:
    # Translate `caps=...` shorthand to the dataclass's
    # `capabilities=RuntimeCapabilities(...)` field.
    if "caps" in kwargs:
        caps = kwargs.pop("caps")
        kwargs.setdefault(
            "capabilities", RuntimeCapabilities(list(caps))
        )
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


class TestRegister(unittest.TestCase):
    def setUp(self):
        self.repo = InMemoryRuntimeRepository()
        self.reg = RuntimeRegistry(self.repo)

    def test_register_success(self):
        d = _mk("r1")
        out = self.reg.register_runtime(d)
        self.assertEqual(out.runtime_id, "r1")
        self.assertEqual(len(self.reg.list_runtimes()), 1)

    def test_duplicate_id_rejected_by_default(self):
        d = _mk("r1")
        self.reg.register_runtime(d)
        with self.assertRaises(RuntimeRegistryError):
            self.reg.register_runtime(_mk("r1"))

    def test_replace_overrides(self):
        d = _mk("r1", display_name="first")
        self.reg.register_runtime(d)
        d2 = _mk("r1", display_name="second")
        self.reg.register_runtime(d2, replace=True)
        out = self.reg.get_runtime("r1")
        self.assertEqual(out.display_name, "second")

    def test_missing_runtime_id_raises_validation(self):
        d = _mk("")
        with self.assertRaises(RuntimeValidationError):
            self.reg.register_runtime(d)

    def test_missing_runtime_type_raises_validation(self):
        d = _mk(runtime_type="")
        with self.assertRaises(RuntimeValidationError):
            self.reg.register_runtime(d)

    def test_invalid_health_status_raises(self):
        d = _mk("r1")
        d.health.status = "BOGUS"
        with self.assertRaises(RuntimeValidationError):
            self.reg.register_runtime(d)

    def test_invalid_max_concurrency_raises(self):
        d = _mk("r1")
        d.limits.max_concurrency = 0
        with self.assertRaises(RuntimeValidationError):
            self.reg.register_runtime(d)


class TestGet(unittest.TestCase):
    def setUp(self):
        self.reg = RuntimeRegistry(InMemoryRuntimeRepository())
        self.reg.register_runtime(_mk("r1"))

    def test_get_existing(self):
        d = self.reg.get_runtime("r1")
        self.assertEqual(d.runtime_id, "r1")

    def test_get_missing_raises_not_found(self):
        with self.assertRaises(RuntimeNotFoundError):
            self.reg.get_runtime("missing")


class TestList(unittest.TestCase):
    def setUp(self):
        self.reg = RuntimeRegistry(InMemoryRuntimeRepository())
        self.reg.register_runtime(_mk("r1", runtime_type="aee_lightweight"))
        self.reg.register_runtime(_mk("r2", runtime_type="shell"))
        self.reg.register_runtime(_mk("r3", runtime_type="shell", enabled=False))

    def test_list_all(self):
        self.assertEqual(len(self.reg.list_runtimes()), 3)

    def test_list_enabled(self):
        out = self.reg.list_runtimes(enabled=True)
        self.assertEqual({r.runtime_id for r in out}, {"r1", "r2"})

    def test_list_by_type(self):
        out = self.reg.list_runtimes(runtime_type="shell")
        self.assertEqual({r.runtime_id for r in out}, {"r2", "r3"})


class TestEnableDisable(unittest.TestCase):
    def setUp(self):
        self.reg = RuntimeRegistry(InMemoryRuntimeRepository())
        self.reg.register_runtime(_mk("r1"))

    def test_disable_then_enable(self):
        self.reg.set_runtime_enabled("r1", False)
        self.assertFalse(self.reg.get_runtime("r1").enabled)
        self.reg.set_runtime_enabled("r1", True)
        self.assertTrue(self.reg.get_runtime("r1").enabled)

    def test_set_enabled_missing_raises(self):
        with self.assertRaises(RuntimeNotFoundError):
            self.reg.set_runtime_enabled("missing", False)


class TestUpdate(unittest.TestCase):
    def setUp(self):
        self.reg = RuntimeRegistry(InMemoryRuntimeRepository())
        self.reg.register_runtime(_mk("r1", display_name="first"))

    def test_update_label(self):
        out = self.reg.update_runtime("r1", {"labels": {"env": "local"}})
        self.assertEqual(out.labels, {"env": "local"})
        # Original display_name preserved.
        self.assertEqual(out.display_name, "first")

    def test_update_capabilities(self):
        out = self.reg.update_runtime(
            "r1", {"capabilities": ["task.shell", "task.git"]}
        )
        self.assertEqual(
            sorted(out.capabilities.to_list()),
            ["task.git", "task.shell"],
        )

    def test_update_missing_raises(self):
        with self.assertRaises(RuntimeNotFoundError):
            self.reg.update_runtime("missing", {"display_name": "x"})


class TestUnregister(unittest.TestCase):
    def setUp(self):
        self.reg = RuntimeRegistry(InMemoryRuntimeRepository())
        self.reg.register_runtime(_mk("r1"))

    def test_unregister_existing(self):
        self.assertTrue(self.reg.unregister_runtime("r1"))
        with self.assertRaises(RuntimeNotFoundError):
            self.reg.get_runtime("r1")

    def test_unregister_missing(self):
        self.assertFalse(self.reg.unregister_runtime("missing"))


class TestFindByCapability(unittest.TestCase):
    def setUp(self):
        self.reg = RuntimeRegistry(InMemoryRuntimeRepository())
        self.reg.register_runtime(_mk("r1", caps=["task.shell"]))
        self.reg.register_runtime(_mk("r2", caps=["task.shell", "task.git"]))
        self.reg.register_runtime(_mk("r3", caps=["task.python"]))

    def test_find_capability_match(self):
        out = self.reg.find_runtimes_by_capability("task.shell")
        self.assertEqual({r.runtime_id for r in out}, {"r1", "r2"})

    def test_find_capability_case_insensitive(self):
        out = self.reg.find_runtimes_by_capability("TASK.SHELL")
        self.assertEqual(len(out), 2)

    def test_find_capability_no_match(self):
        out = self.reg.find_runtimes_by_capability("task.browser")
        self.assertEqual(out, [])

    def test_find_capability_empty(self):
        out = self.reg.find_runtimes_by_capability("")
        self.assertEqual(out, [])


class TestFindByLabels(unittest.TestCase):
    def setUp(self):
        self.reg = RuntimeRegistry(InMemoryRuntimeRepository())
        self.reg.register_runtime(_mk("r1", labels={"env": "local", "trust": "internal"}))
        self.reg.register_runtime(_mk("r2", labels={"env": "sandbox"}))
        self.reg.register_runtime(_mk("r3", labels={"env": "local"}))

    def test_find_label_subset(self):
        out = self.reg.find_runtimes_by_labels({"env": "local"})
        self.assertEqual({r.runtime_id for r in out}, {"r1", "r3"})

    def test_find_label_exact(self):
        out = self.reg.find_runtimes_by_labels(
            {"env": "local", "trust": "internal"}
        )
        self.assertEqual({r.runtime_id for r in out}, {"r1"})

    def test_find_label_no_match(self):
        out = self.reg.find_runtimes_by_labels({"env": "nowhere"})
        self.assertEqual(out, [])

    def test_find_label_empty_returns_all(self):
        out = self.reg.find_runtimes_by_labels({})
        self.assertEqual(len(out), 3)


class TestHealth(unittest.TestCase):
    def setUp(self):
        self.reg = RuntimeRegistry(InMemoryRuntimeRepository())
        self.reg.register_runtime(_mk("r1"))

    def test_update_health_valid(self):
        out = self.reg.update_runtime_health("r1", "healthy", message="ok")
        self.assertEqual(out.health.status, "healthy")
        self.assertEqual(out.health.message, "ok")
        self.assertIsNotNone(out.health.last_checked_at)

    def test_update_health_invalid_status(self):
        with self.assertRaises(RuntimeValidationError):
            self.reg.update_runtime_health("r1", "BOGUS")

    def test_check_health(self):
        h = self.reg.check_runtime_health("r1")
        self.assertIn("status", h)

    def test_list_healthy_dispatchable(self):
        self.reg.update_runtime_health("r1", "healthy")
        out = self.reg.list_healthy_runtimes()
        self.assertEqual({r.runtime_id for r in out}, {"r1"})

    def test_list_healthy_excludes_offline(self):
        self.reg.update_runtime_health("r1", "offline")
        out = self.reg.list_healthy_runtimes()
        self.assertEqual(out, [])


class TestDispatchRecords(unittest.TestCase):
    def setUp(self):
        self.reg = RuntimeRegistry(InMemoryRuntimeRepository())
        self.reg.register_runtime(_mk("r1"))

    def test_record_dispatch_then_update(self):
        from aee.runtimes.models import (
            DispatchRecord,
            DispatchStatus,
        )
        rec = DispatchRecord(
            dispatch_id="d1",
            task_id="t1",
            run_id="r1",
            selected_runtime_id="r1",
            selected_runtime_type="aee_lightweight",
        )
        self.reg.record_dispatch(rec)
        self.reg.update_dispatch_status(
            "d1", DispatchStatus.DISPATCHED
        )
        out = self.reg.list_dispatches(task_id="t1")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].dispatch_status, DispatchStatus.DISPATCHED)

    def test_count_dispatches(self):
        from aee.runtimes.models import DispatchRecord
        for i in range(3):
            self.reg.record_dispatch(DispatchRecord(
                dispatch_id=f"d{i}",
                task_id=f"t{i}",
                run_id=f"r{i}",
                selected_runtime_id="r1",
                selected_runtime_type="aee_lightweight",
            ))
        self.assertEqual(self.reg.list_dispatches(runtime_id="r1").__len__(), 3)


class TestBootstrapDefaultRuntimes(unittest.TestCase):
    def setUp(self):
        # Reset the module-level singleton's repo to a
        # fresh in-memory one so the bootstrap is observable.
        from aee.runtimes import registry as _reg_mod
        self._orig_registry = _reg_mod.runtime_registry
        _reg_mod.runtime_registry = RuntimeRegistry(
            InMemoryRuntimeRepository()
        )

    def tearDown(self):
        from aee.runtimes import registry as _reg_mod
        _reg_mod.runtime_registry = self._orig_registry

    def test_bootstrap_registers_builtin(self):
        from aee.runtimes import registry as _reg_mod
        bootstrap_default_runtimes(force=False)
        out = _reg_mod.runtime_registry.list_runtimes()
        ids = {r.runtime_id for r in out}
        self.assertIn("aee-lightweight-local", ids)
        d = _reg_mod.runtime_registry.get_runtime("aee-lightweight-local")
        self.assertEqual(d.runtime_type, "aee_lightweight")
        self.assertIn("task.shell", d.capabilities.to_list())

    def test_bootstrap_idempotent(self):
        from aee.runtimes import registry as _reg_mod
        bootstrap_default_runtimes(force=False)
        before = len(_reg_mod.runtime_registry.list_runtimes())
        bootstrap_default_runtimes(force=False)
        after = len(_reg_mod.runtime_registry.list_runtimes())
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
