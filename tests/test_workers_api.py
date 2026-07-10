"""AEE-2 /workers API tests.

Covers the 11 acceptance scenarios from
`Abacus/AEE2_Worker_Claim_Protocol_Task.md` §8 that map to
the workers surface. Job-side scenarios live in
`tests/test_jobs_api.py`; the manager-level row→Task coverage
lives in `tests/test_manager_aee1.py`.

Each test uses an isolated tmpdir-backed DB so we don't pollute
the production `data/dispatcher.db`.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Force-test env BEFORE any imports that read BRIDGE_API_KEY.
os.environ.setdefault("BRIDGE_API_KEY", "test-key")
os.environ.setdefault("DISPATCHER_API_KEY", "test-key")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Use a temp DB so this test never touches production data.
_TMPDIR = tempfile.mkdtemp(prefix="aee2-workers-test-")
os.environ["DISPATCHER_DB_DIR"] = _TMPDIR

from fastapi.testclient import TestClient  # noqa: E402

from aee.api import api_router  # noqa: E402
from dispatcher import db  # noqa: E402


# Point the production DB module at the tmp dir for the duration of
# these tests. The module-level constants are imported at import
# time, so we have to override them.
db.DB_DIR = Path(_TMPDIR)
db.DB_PATH = db.DB_DIR / "dispatcher.db"
db._local.conn = None
db._initialized = False
# Same for the manager module if it has its own constants.
import dispatcher.manager as mgr  # noqa: E402
mgr.LOGS_DIR = Path(_TMPDIR) / "logs"
mgr.REPORTS_DIR = Path(_TMPDIR) / "reports"


app = None  # built in setUpModule


def _build_client() -> TestClient:
    from fastapi import FastAPI
    application = FastAPI()
    application.include_router(api_router)
    return TestClient(application)


class TestWorkersAPI(unittest.TestCase):
    """§8.1: worker register idempotent / repeat safe
       §8.2: worker heartbeat updates last_heartbeat_at."""

    def setUp(self) -> None:
        # Force a fresh DB connection per test, and drop the tmp DB
        # file so `_init_schema` rebuilds schema cleanly.
        db._local.conn = None
        db._initialized = False
        if db.DB_PATH.exists():
            try:
                db.DB_PATH.unlink()
            except OSError:
                pass
        self.client = _build_client()
        self.headers = {"Authorization": "Bearer test-key"}

    def test_register_then_repeat_is_idempotent(self):
        body = {
            "worker_name": "pi-agent-m2-test",
            "worker_type": "pi_agent",
            "hostname": "m2",
            "capabilities": ["shell", "filesystem", "python"],
            "workdir_allowlist": ["/home/ubuntu/hermes-runtime-bridge"],
            "max_concurrent": 1,
        }
        r1 = self.client.post("/workers/register", json=body, headers=self.headers)
        self.assertEqual(r1.status_code, 200, r1.text)
        data1 = r1.json()
        self.assertTrue(data1["registered"])
        # registered_at should be stable across re-registers.
        first_reg_at = data1["registered_at"]
        # Re-register with a different capability list — allowed.
        body2 = dict(body)
        body2["capabilities"] = ["shell", "filesystem"]
        r2 = self.client.post("/workers/register", json=body2, headers=self.headers)
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()["registered_at"], first_reg_at)
        # And the new capabilities are persisted.
        rid = data1["worker_id"]
        r3 = self.client.get(f"/workers/{rid}", headers=self.headers)
        self.assertEqual(r3.status_code, 200)
        caps = r3.json()["capabilities"]
        self.assertIn("shell", caps)
        self.assertIn("filesystem", caps)
        self.assertNotIn("python", caps)

    def test_heartbeat_updates_last_heartbeat_at(self):
        body = {
            "worker_name": "pi-agent-b2-01",
            "worker_type": "pi_agent",
            "capabilities": ["shell"],
            "workdir_allowlist": ["/tmp"],
            "max_concurrent": 1,
        }
        reg = self.client.post("/workers/register", json=body, headers=self.headers).json()
        wid = reg["worker_id"]
        # heartbeat with no job_id
        r = self.client.post(f"/workers/{wid}/heartbeat", json={}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNotNone(r.json()["last_heartbeat_at"])
        # heartbeat with a job_id
        r2 = self.client.post(f"/workers/{wid}/heartbeat", json={"job_id": "T-FAKE"}, headers=self.headers)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["last_job_id"], "T-FAKE")

    def test_heartbeat_unknown_worker_returns_404(self):
        r = self.client.post(
            "/workers/nonexistent-worker/heartbeat", json={}, headers=self.headers
        )
        self.assertEqual(r.status_code, 404)

    def test_register_requires_bearer(self):
        body = {
            "worker_name": "noauth",
            "worker_type": "pi_agent",
            "capabilities": [],
        }
        r = self.client.post("/workers/register", json=body)
        self.assertEqual(r.status_code, 401)

    def test_register_validates_worker_type(self):
        body = {"worker_name": "bad", "worker_type": ""}
        r = self.client.post("/workers/register", json=body, headers=self.headers)
        self.assertEqual(r.status_code, 400)

    def test_worker_id_charset_is_validated(self):
        body = {
            "worker_id": "bad/id",  # slash is not allowed
            "worker_name": "x",
            "worker_type": "pi_agent",
        }
        r = self.client.post("/workers/register", json=body, headers=self.headers)
        self.assertEqual(r.status_code, 400)

    def test_list_workers(self):
        for nm in ("a", "b", "c"):
            self.client.post(
                "/workers/register",
                json={"worker_name": nm, "worker_type": "pi_agent"},
                headers=self.headers,
            )
        r = self.client.get("/workers", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["count"], 3)


if __name__ == "__main__":
    unittest.main()
