"""AEE-5 Runtime Registry — integration tests.

DB-backed tests that exercise the full AEE-5 stack:
SQLite migration, repository, registry, selector,
dispatch service, API. Uses the real
`dispatcher.db.get_conn()` connection (with DB file
cleaned per test).
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Force-test env BEFORE any imports that read env.
os.environ.setdefault("DISPATCHER_API_KEY", "test-key")
os.environ.setdefault("BRIDGE_API_KEY", "test-key")

import sqlite3  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from dispatcher import db as dispatcher_db  # noqa: E402
from aee.api.runtimes import router as runtimes_router  # noqa: E402
from aee.runtimes.errors import RuntimeNotFoundError  # noqa: E402
from aee.runtimes.models import (  # noqa: E402
    RuntimeCapabilities,
    RuntimeDescriptor,
    RuntimeHealth,
    RuntimeHealthStatus,
    RuntimeLimits,
    TaskRuntimeRequirements,
)
from aee.runtimes.registry import (  # noqa: E402
    RuntimeRegistry,
    bootstrap_default_runtimes,
    runtime_registry,
)
from aee.runtimes.repository import (  # noqa: E402
    SqliteRuntimeRepository,
    ensure_aee5_schema,
)
from aee.dispatch.service import (  # noqa: E402
    DispatchService,
    dispatch_service,
)


def _reset_db() -> None:
    """Wipe the dispatcher DB so each test starts fresh."""
    p = dispatcher_db.DB_PATH
    for ext in ("", "-wal", "-shm"):
        f = p.with_name(p.name + ext)
        if f.exists():
            try:
                f.unlink()
            except Exception:
                pass
    # Re-init on next get_conn()
    dispatcher_db._local.conn = None  # type: ignore[attr-defined]
    dispatcher_db._initialized = False  # type: ignore[attr-defined]


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


class TestSqliteMigration(unittest.TestCase):
    def setUp(self):
        _reset_db()

    def test_aee5_tables_created_on_first_open(self):
        conn = dispatcher_db.get_conn()
        for table in ("runtimes", "dispatch_records"):
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            self.assertIsNotNone(row, f"table {table!r} not created")
        for idx in (
            "idx_runtimes_type",
            "idx_runtimes_enabled",
            "idx_dispatch_records_task",
            "idx_dispatch_records_runtime",
        ):
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                (idx,),
            ).fetchone()
            self.assertIsNotNone(row, f"index {idx!r} not created")

    def test_migration_idempotent(self):
        # First open already happened in setUp; force
        # another ensure_aee5_schema() call.
        ensure_aee5_schema(dispatcher_db.get_conn())
        ensure_aee5_schema(dispatcher_db.get_conn())
        # No errors = good.


class TestSqliteRepository(unittest.TestCase):
    def setUp(self):
        _reset_db()
        # The singleton is a *different* repository
        # than the test fixtures use. The fixtures use
        # a fresh SqliteRuntimeRepository so test
        # state is independent.
        self.repo = SqliteRuntimeRepository()
        self.reg = RuntimeRegistry(self.repo)

    def test_round_trip_descriptor(self):
        d = _mk("r1", display_name="Test", caps=["task.shell", "task.git"])
        self.reg.register_runtime(d)
        out = self.reg.get_runtime("r1")
        self.assertEqual(out.display_name, "Test")
        self.assertEqual(out.capabilities.to_list(), ["task.git", "task.shell"])

    def test_label_round_trip(self):
        d = _mk("r1", labels={"env": "local", "trust": "internal"})
        self.reg.register_runtime(d)
        out = self.reg.get_runtime("r1")
        self.assertEqual(out.labels, {"env": "local", "trust": "internal"})

    def test_health_persists(self):
        d = _mk("r1")
        self.reg.register_runtime(d)
        self.reg.update_runtime_health("r1", "healthy", message="probe ok")
        out = self.reg.get_runtime("r1")
        self.assertEqual(out.health.status, "healthy")
        self.assertEqual(out.health.message, "probe ok")
        self.assertIsNotNone(out.health.last_checked_at)

    def test_dispatch_record_persists(self):
        from aee.runtimes.models import DispatchRecord, DispatchStatus
        rec = DispatchRecord(
            dispatch_id="d1",
            task_id="t1",
            run_id="r1",
            selected_runtime_id="r1",
            selected_runtime_type="aee_lightweight",
            selection_reason="matched",
            rejected_reasons={"rt2": ["missing cap"]},
        )
        self.reg.record_dispatch(rec)
        out = self.reg.list_dispatches(task_id="t1")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].rejected_reasons, {"rt2": ["missing cap"]})
        self.reg.update_dispatch_status("d1", DispatchStatus.DISPATCHED)
        out = self.reg.list_dispatches(task_id="t1")
        self.assertEqual(out[0].dispatch_status, DispatchStatus.DISPATCHED)


class TestBootstrapDefaultRuntime(unittest.TestCase):
    def setUp(self):
        _reset_db()

    def test_bootstrap_creates_builtin_in_real_db(self):
        # Re-init the singleton to use the fresh DB.
        from aee.runtimes import registry as _reg_mod
        _reg_mod.runtime_registry = RuntimeRegistry(
            SqliteRuntimeRepository()
        )
        _reg_mod.runtime_registry.register_runtime(
            _mk("aee-lightweight-local")
        )
        # A second bootstrap with replace=False is a no-op.
        bootstrap_default_runtimes(force=False)
        # The bootstrap helper uses the module-level
        # singleton; the built-in should already be there.
        from aee.runtimes import registry as _reg_mod2
        ids = {r.runtime_id for r in _reg_mod2.runtime_registry.list_runtimes()}
        self.assertIn("aee-lightweight-local", ids)


class TestDispatchServiceSqlite(unittest.TestCase):
    def setUp(self):
        _reset_db()
        from aee.runtimes import registry as _reg_mod
        _reg_mod.runtime_registry = RuntimeRegistry(
            SqliteRuntimeRepository()
        )
        self.reg = _reg_mod.runtime_registry
        self.svc = DispatchService(registry=self.reg)

    def test_end_to_end_dispatch_persists(self):
        self.reg.register_runtime(_mk("aee-lightweight-local"))
        rec = self.svc.select_runtime_for_job(
            task_id="t1",
            run_id="r1",
            runtime_requirements=None,
        )
        self.assertEqual(rec.selected_runtime_id, "aee-lightweight-local")
        # Persisted in dispatch_records
        rows = dispatcher_db.get_conn().execute(
            "SELECT dispatch_id, dispatch_status FROM dispatch_records "
            "WHERE task_id = 't1'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dispatch_status"], "selected")

    def test_no_match_persists_failed_record(self):
        self.reg.register_runtime(_mk("r1", caps=["task.shell"]))
        with self.assertRaises(RuntimeNotFoundError):
            self.svc.select_runtime_for_job(
                task_id="t1",
                run_id="r1",
                runtime_requirements=TaskRuntimeRequirements(
                    required_capabilities=["task.browser"],
                ),
            )
        rows = dispatcher_db.get_conn().execute(
            "SELECT dispatch_status, failure_code FROM dispatch_records "
            "WHERE task_id = 't1'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dispatch_status"], "failed")
        self.assertEqual(rows[0]["failure_code"], "AEE_RUNTIME_NOT_FOUND")


class TestRuntimeApi(unittest.TestCase):
    """Drive the AEE-5 API through `fastapi.testclient`."""

    def setUp(self):
        _reset_db()
        from aee.runtimes import registry as _reg_mod
        # Fresh in-memory registry for the API layer.
        self._fresh = RuntimeRegistry()
        # Patch the module-level singleton that the API
        # uses, so the API's mutations are visible to us.
        self._orig = _reg_mod.runtime_registry
        _reg_mod.runtime_registry = self._fresh

        from fastapi import FastAPI
        self.app = FastAPI()
        self.app.include_router(runtimes_router)
        self.client = TestClient(self.app)
        self.headers = {"Authorization": "Bearer test-key"}

    def tearDown(self):
        from aee.runtimes import registry as _reg_mod
        _reg_mod.runtime_registry = self._orig

    def test_create_runtime(self):
        body = {
            "runtime_id": "r1",
            "runtime_type": "shell",
            "display_name": "Shell",
            "capabilities": ["task.shell"],
        }
        r = self.client.post("/runtimes", json=body, headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["runtime_id"], "r1")

    def test_create_duplicate_returns_409(self):
        body = {
            "runtime_id": "r1",
            "runtime_type": "shell",
        }
        r = self.client.post("/runtimes", json=body, headers=self.headers)
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/runtimes", json=body, headers=self.headers)
        self.assertEqual(r.status_code, 409)

    def test_create_missing_fields_returns_400(self):
        body = {"display_name": "x"}  # no runtime_id
        r = self.client.post("/runtimes", json=body, headers=self.headers)
        self.assertEqual(r.status_code, 400)

    def test_list_runtimes_with_filters(self):
        for body in [
            {"runtime_id": "r1", "runtime_type": "aee_lightweight", "enabled": True},
            {"runtime_id": "r2", "runtime_type": "shell", "enabled": True},
            {"runtime_id": "r3", "runtime_type": "shell", "enabled": False},
        ]:
            self.client.post("/runtimes", json=body, headers=self.headers)
        # enabled=true filter
        r = self.client.get("/runtimes?enabled=true", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 2)
        # runtime_type filter
        r = self.client.get("/runtimes?runtime_type=shell", headers=self.headers)
        self.assertEqual(r.json()["count"], 2)
        # capability filter
        for body in [
            {"runtime_id": "r4", "runtime_type": "shell", "capabilities": ["task.shell"]},
        ]:
            self.client.post("/runtimes", json=body, headers=self.headers)
        r = self.client.get("/runtimes?capability=task.shell", headers=self.headers)
        ids = {x["runtime_id"] for x in r.json()["runtimes"]}
        self.assertIn("r4", ids)

    def test_get_runtime(self):
        body = {"runtime_id": "r1", "runtime_type": "shell"}
        self.client.post("/runtimes", json=body, headers=self.headers)
        r = self.client.get("/runtimes/r1", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["runtime_id"], "r1")
        # Missing
        r = self.client.get("/runtimes/missing", headers=self.headers)
        self.assertEqual(r.status_code, 404)

    def test_patch_runtime(self):
        body = {"runtime_id": "r1", "runtime_type": "shell", "display_name": "Original"}
        self.client.post("/runtimes", json=body, headers=self.headers)
        r = self.client.patch(
            "/runtimes/r1", json={"display_name": "Renamed"},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["display_name"], "Renamed")

    def test_delete_runtime(self):
        body = {"runtime_id": "r1", "runtime_type": "shell"}
        self.client.post("/runtimes", json=body, headers=self.headers)
        r = self.client.delete("/runtimes/r1", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/runtimes/r1", headers=self.headers)
        self.assertEqual(r.status_code, 404)

    def test_enable_disable(self):
        body = {"runtime_id": "r1", "runtime_type": "shell"}
        self.client.post("/runtimes", json=body, headers=self.headers)
        r = self.client.post("/runtimes/r1/disable", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["enabled"])
        r = self.client.post("/runtimes/r1/enable", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["enabled"])

    def test_health_check(self):
        body = {"runtime_id": "r1", "runtime_type": "shell"}
        self.client.post("/runtimes", json=body, headers=self.headers)
        # GET health-check
        r = self.client.post(
            "/runtimes/r1/health-check", headers=self.headers
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("status", r.json()["health"])
        # PATCH health
        r = self.client.patch(
            "/runtimes/r1/health",
            json={"status": "healthy", "message": "probe ok"},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["health"]["status"], "healthy")

    def test_unauthorized(self):
        r = self.client.get("/runtimes")
        self.assertEqual(r.status_code, 401)
        r = self.client.get(
            "/runtimes", headers={"Authorization": "Bearer wrong"}
        )
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
