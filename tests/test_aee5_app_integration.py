"""AEE-5 — app.py integration test.

Drives the AEE-5 Runtime Registry endpoints
through the full FastAPI app (i.e. the same
mounting used by production), verifying that
the AEE-4 alias pattern is intact: the new
`/v1/runtimes` routes are reachable AND the
legacy `/runtimes` paths are reachable.
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

from fastapi.testclient import TestClient  # noqa: E402

from dispatcher import db as dispatcher_db  # noqa: E402


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


class TestAppMounting(unittest.TestCase):
    """Verify the AEE-5 routes are mounted on the
    production app (under both `/runtimes` and
    `/v1/runtimes`)."""

    @classmethod
    def setUpClass(cls):
        _reset_db()
        # Use a fresh in-memory registry to avoid
        # bleeding state between tests.
        from aee.runtimes import registry as _reg_mod
        from aee.runtimes.repository import InMemoryRuntimeRepository
        from aee.runtimes.registry import RuntimeRegistry
        _reg_mod.runtime_registry = RuntimeRegistry(
            InMemoryRuntimeRepository()
        )

        # Build a minimal FastAPI app mirroring app.py's
        # mounting. We don't import app.py directly because
        # it has unrelated side-effects (FastAPI lifespan
        # starts a watcher thread). We only need the
        # routers.
        from fastapi import FastAPI
        from aee.api import api_router, v1_router
        cls.app = FastAPI()
        cls.app.include_router(api_router)
        cls.app.include_router(v1_router)
        cls.client = TestClient(cls.app)
        cls.headers = {"Authorization": "Bearer test-key"}

    def test_create_via_legacy_path(self):
        body = {"runtime_id": "r1", "runtime_type": "shell"}
        r = self.client.post("/runtimes", json=body, headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["runtime_id"], "r1")

    def test_create_via_v1_alias(self):
        body = {"runtime_id": "r2", "runtime_type": "shell"}
        r = self.client.post("/v1/runtimes", json=body, headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["runtime_id"], "r2")

    def test_list_via_legacy_path(self):
        r = self.client.get("/runtimes", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertIn("runtimes", r.json())

    def test_list_via_v1_alias(self):
        r = self.client.get("/v1/runtimes", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertIn("runtimes", r.json())

    def test_get_via_v1(self):
        r = self.client.get("/v1/runtimes/r1", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["runtime_id"], "r1")

    def test_get_via_legacy(self):
        r = self.client.get("/runtimes/r2", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["runtime_id"], "r2")

    def test_search_via_v1(self):
        for body in [
            {"runtime_id": "r-search-local", "runtime_type": "shell",
             "labels": {"environment": "local"}},
            {"runtime_id": "r-search-sandbox", "runtime_type": "shell",
             "labels": {"environment": "sandbox"}},
        ]:
            r = self.client.post(
                "/v1/runtimes", json=body, headers=self.headers
            )
            self.assertEqual(r.status_code, 200)
        r = self.client.get(
            "/v1/runtimes/search?label.environment=sandbox",
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200)
        ids = {x["runtime_id"] for x in r.json()["runtimes"]}
        self.assertIn("r-search-sandbox", ids)
        self.assertNotIn("r-search-local", ids)


if __name__ == "__main__":
    unittest.main()
