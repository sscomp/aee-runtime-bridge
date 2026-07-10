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


# ---------------------------------------------------------------------------
# AEE-4 Worker Runtime Contract tests
# ---------------------------------------------------------------------------
#
# These tests pin the AEE-4 contract surface:
#   * 8 new metadata fields on POST /workers/register
#   * 5-status vocabulary on POST /workers/.../heartbeat
#   * /v1/... aliases
# They live in a separate class so a future AEE-5 work can
# opt-in / opt-out without touching the AEE-2 baseline tests.
# ---------------------------------------------------------------------------


class TestWorkersAPIAEE4(unittest.TestCase):
    """AEE-4 Worker Runtime Contract — metadata, status, /v1/ aliases."""

    def setUp(self) -> None:
        db._local.conn = None
        db._initialized = False
        if db.DB_PATH.exists():
            try:
                db.DB_PATH.unlink()
            except OSError:
                pass
        self.client = _build_client()
        self.headers = {"Authorization": "Bearer test-key"}

    def test_register_accepts_runtime_metadata(self):
        """The 8 new metadata fields round-trip through register + GET."""
        body = {
            "worker_name": "pi-agent-aee4-01",
            "worker_type": "pi_agent",
            "capabilities": ["runtime.pi", "tool.shell"],
            "workdir_allowlist": ["/tmp"],
            "runtime_name": "pi",
            "runtime_version": "0.1.0",
            "operating_system": "linux",
            "architecture": "x86_64",
            "python_version": "3.11.2",
            "node_version": "v22.22.2",
            "git_version": "2.34.1",
            "start_time": "2026-07-10T00:00:00Z",
        }
        r = self.client.post("/workers/register", json=body, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["version"], "v1")
        wid = data["worker_id"]
        # Read back; all 8 fields are exposed.
        g = self.client.get(f"/workers/{wid}", headers=self.headers)
        self.assertEqual(g.status_code, 200)
        meta = g.json()
        self.assertEqual(meta["runtime_name"], "pi")
        self.assertEqual(meta["runtime_version"], "0.1.0")
        self.assertEqual(meta["operating_system"], "linux")
        self.assertEqual(meta["architecture"], "x86_64")
        self.assertEqual(meta["python_version"], "3.11.2")
        self.assertEqual(meta["node_version"], "v22.22.2")
        self.assertEqual(meta["git_version"], "2.34.1")
        self.assertEqual(meta["start_time"], "2026-07-10T00:00:00Z")

    def test_register_omits_metadata_yields_nulls(self):
        """Backward compat: AEE-2 / AEE-3 clients that don't send the
        new fields still register cleanly. NULLs surface in the GET
        response, status defaults to 'unknown'."""
        body = {
            "worker_name": "aee2-only-worker",
            "worker_type": "pi_agent",
            "capabilities": ["shell"],
        }
        r = self.client.post("/workers/register", json=body, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        wid = r.json()["worker_id"]
        g = self.client.get(f"/workers/{wid}", headers=self.headers)
        meta = g.json()
        for k in (
            "runtime_name", "runtime_version", "operating_system",
            "architecture", "python_version", "node_version",
            "git_version", "start_time", "status_message",
            "last_status_change_at",
        ):
            self.assertIsNone(meta[k], f"expected {k} to be None, got {meta[k]!r}")
        self.assertEqual(meta["status"], "unknown")

    def test_heartbeat_updates_worker_status(self):
        """Heartbeat with `status` and `status_message` updates the
        row and stamps `last_status_change_at`."""
        body = {
            "worker_name": "aee4-hb-01",
            "worker_type": "pi_agent",
            "capabilities": ["shell"],
        }
        reg = self.client.post("/workers/register", json=body, headers=self.headers).json()
        wid = reg["worker_id"]
        # Idle.
        r = self.client.post(
            f"/workers/{wid}/heartbeat",
            json={"status": "idle", "status_message": "between jobs"},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "idle")
        self.assertEqual(r.json()["status_message"], "between jobs")
        self.assertIsNotNone(r.json()["last_status_change_at"])
        # Busy.
        r2 = self.client.post(
            f"/workers/{wid}/heartbeat",
            json={"status": "busy", "status_message": "executing TASK-X"},
            headers=self.headers,
        )
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["status"], "busy")
        # Same-status heartbeat does NOT bump last_status_change_at.
        first_change = r2.json()["last_status_change_at"]
        r3 = self.client.post(
            f"/workers/{wid}/heartbeat",
            json={"status": "busy"},
            headers=self.headers,
        )
        self.assertEqual(r3.json()["last_status_change_at"], first_change)

    def test_heartbeat_rejects_invalid_status(self):
        """Contract: unknown status is HTTP 400, not silently coerced."""
        body = {
            "worker_name": "aee4-hb-bad",
            "worker_type": "pi_agent",
            "capabilities": ["shell"],
        }
        reg = self.client.post("/workers/register", json=body, headers=self.headers).json()
        wid = reg["worker_id"]
        r = self.client.post(
            f"/workers/{wid}/heartbeat",
            json={"status": "BOGUS"},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("status must be one of", r.json()["detail"])

    def test_v1_aliases_work(self):
        """ADR-007: /v1/... paths share the same handlers as the
        legacy /jobs/... and /workers/... paths."""
        body = {
            "worker_name": "aee4-v1-01",
            "worker_type": "pi_agent",
            "capabilities": ["shell"],
            "runtime_name": "pi",
            "runtime_version": "0.1.0",
        }
        # Register via /v1/...
        r = self.client.post("/v1/workers/register", json=body, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["version"], "v1")
        wid = data["worker_id"]
        # Heartbeat via /v1/...
        r2 = self.client.post(
            f"/v1/workers/{wid}/heartbeat",
            json={"status": "busy"},
            headers=self.headers,
        )
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["status"], "busy")
        # GET via /v1/... — should match what /workers/... returns.
        g1 = self.client.get(f"/v1/workers/{wid}", headers=self.headers)
        g2 = self.client.get(f"/workers/{wid}", headers=self.headers)
        self.assertEqual(g1.status_code, 200)
        self.assertEqual(g2.status_code, 200)
        # Both surfaces expose the same worker record.
        for k in ("worker_id", "runtime_name", "status", "registered_at"):
            self.assertEqual(g1.json()[k], g2.json()[k])


if __name__ == "__main__":
    unittest.main()
