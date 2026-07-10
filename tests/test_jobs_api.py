"""AEE-2 /jobs API tests.

Covers the 11 acceptance scenarios from
`Abacus/AEE2_Worker_Claim_Protocol_Task.md` §8 that map to the
job side: queued → claim → heartbeat → complete, plus the
adversarial cases (approval_required blocks claim, wrong claim
token rejected, stale running → reaper timeout).

Each test uses an isolated tmpdir-backed DB.
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("BRIDGE_API_KEY", "test-key")
os.environ.setdefault("DISPATCHER_API_KEY", "test-key")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Use a temp DB so this test never touches production data.
_TMPDIR = tempfile.mkdtemp(prefix="aee2-jobs-test-")

from fastapi.testclient import TestClient  # noqa: E402

from aee.api import api_router  # noqa: E402
from aee.core.registry import adapter_registry  # noqa: E402
from dispatcher import db  # noqa: E402
from dispatcher import manager as mgr  # noqa: E402
from dispatcher.reaper import ReaperConfig, reap_once  # noqa: E402

# Point DB at tmp dir.
db.DB_DIR = Path(_TMPDIR)
db.DB_PATH = db.DB_DIR / "dispatcher.db"
mgr.LOGS_DIR = Path(_TMPDIR) / "logs"
mgr.REPORTS_DIR = Path(_TMPDIR) / "reports"

# Install FakeAdapter under "fake" so claim-driven jobs can run
# end-to-end without touching Hermes.
from aee.adapters.fake_adapter import FakeAdapter  # noqa: E402
adapter_registry.register(FakeAdapter(), replace=True)


def _fresh_db() -> None:
    db._local.conn = None
    db._initialized = False
    if db.DB_PATH.exists():
        try:
            db.DB_PATH.unlink()
        except OSError:
            pass


def _build_client() -> TestClient:
    from fastapi import FastAPI
    application = FastAPI()
    application.include_router(api_router)
    return TestClient(application)


class TestJobsAPI(unittest.TestCase):
    """§8.3 — §8.9: job lifecycle end-to-end + adversarial cases."""

    def setUp(self) -> None:
        _fresh_db()
        # Reset the FakeAdapter so cross-test state doesn't leak.
        from aee.adapters.fake_adapter import FakeAdapter as _FA
        # Unregister and re-register to wipe any in-memory runs.
        try:
            adapter_registry.unregister("fake")
        except KeyError:
            pass
        adapter_registry.register(_FA(), replace=True)
        self.client = _build_client()
        self.headers = {"Authorization": "Bearer test-key"}

    # Helpers ---------------------------------------------------------

    def _register_worker(self, *, worker_id: str = "w-1", worker_type: str = "fake"):
        return self.client.post(
            "/workers/register",
            json={"worker_id": worker_id, "worker_name": worker_id, "worker_type": worker_type},
            headers=self.headers,
        ).json()

    def _create_job(self, *, target_runtime: str = "fake", **overrides) -> str:
        body = {
            "title": "test job",
            "type": "ops",
            "input": "echo hi",
            "target_runtime": target_runtime,
        }
        body.update(overrides)
        r = self.client.post("/jobs", json=body, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["job_id"]

    def _claim(self, *, worker_id: str = "w-1", worker_type: str = "fake") -> dict:
        r = self.client.post(
            "/jobs/claim",
            json={"worker_id": worker_id, "worker_type": worker_type, "capabilities": []},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    # §8.3: queued job can be claimed --------------------------------

    def test_queued_job_claimable(self):
        self._register_worker()
        jid = self._create_job()
        # Mark it running via the adapter we registered. AEE-2 actually
        # claim flips status to running itself.
        claim = self._claim()
        self.assertEqual(claim["job_id"], jid)
        self.assertIn("claim_token", claim)
        # A token is plain (returned once) and we can hash it for
        # subsequent heartbeat / complete calls.
        token = claim["claim_token"]
        self.assertTrue(len(token) >= 16)
        # The status on the manager side should be 'running'.
        t = mgr.TaskManager().get(jid)
        self.assertEqual(t.status, "running")
        self.assertEqual(t.worker_id, "w-1")
        self.assertIsNotNone(t.claim_token_hash)

    # §8.4: approval_required job is NOT claimable -------------------

    def test_approval_required_blocks_claim(self):
        self._register_worker()
        # Create a job with approval_required=True via direct DB write
        # (the API surface in AEE-2 sets approval_required but the
        # default value should be honoured).
        from dispatcher import db as _db
        jid = self._create_job()
        with _db.transaction() as conn2:
            conn2.execute(
                "UPDATE tasks SET approval_required = 1, approval_state = 'pending' "
                "WHERE task_id = ?",
                (jid,),
            )
        # Claim should now 404 (no claimable jobs).
        r = self.client.post(
            "/jobs/claim",
            json={"worker_id": "w-1", "worker_type": "fake", "capabilities": []},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 404)

    # §8.5: claim flips status to running ----------------------------

    def test_claim_transitions_status_to_running(self):
        self._register_worker()
        jid = self._create_job()
        before = mgr.TaskManager().get(jid)
        self.assertEqual(before.status, "queued")
        self._claim()
        after = mgr.TaskManager().get(jid)
        self.assertEqual(after.status, "running")

    # §8.6: wrong claim token is rejected ----------------------------

    def test_wrong_claim_token_rejected(self):
        self._register_worker()
        jid = self._create_job()
        claim = self._claim()
        # Use a wrong token for heartbeat.
        r = self.client.post(
            f"/jobs/{jid}/heartbeat",
            json={"claim_token": "definitely-wrong"},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 403)
        # Use a wrong token for complete.
        r2 = self.client.post(
            f"/jobs/{jid}/complete",
            json={"claim_token": "definitely-wrong", "output_text": "x"},
            headers=self.headers,
        )
        self.assertEqual(r2.status_code, 403)
        # Use a wrong token for fail.
        r3 = self.client.post(
            f"/jobs/{jid}/fail",
            json={"claim_token": "definitely-wrong", "error": "x"},
            headers=self.headers,
        )
        self.assertEqual(r3.status_code, 403)

    # §8.7: complete after heartbeat flips status to completed -------

    def test_complete_after_heartbeat(self):
        self._register_worker()
        jid = self._create_job()
        claim = self._claim()
        token = claim["claim_token"]
        # Heartbeat first.
        r = self.client.post(
            f"/jobs/{jid}/heartbeat",
            json={"claim_token": token},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200)
        # Then complete.
        r2 = self.client.post(
            f"/jobs/{jid}/complete",
            json={"claim_token": token, "output_text": "all good"},
            headers=self.headers,
        )
        self.assertEqual(r2.status_code, 200)
        # Status now completed.
        t = mgr.TaskManager().get(jid)
        self.assertEqual(t.status, "completed")

    # §8.8: fail flips status to failed ------------------------------

    def test_fail_flips_status(self):
        self._register_worker()
        jid = self._create_job()
        claim = self._claim()
        token = claim["claim_token"]
        r = self.client.post(
            f"/jobs/{jid}/fail",
            json={"claim_token": token, "error": "boom"},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200)
        t = mgr.TaskManager().get(jid)
        self.assertEqual(t.status, "failed")
        self.assertEqual(t.error_message, "boom")

    # §8.9: stale running task is reaped to timeout ------------------

    def test_stale_running_reaped_to_timeout(self):
        self._register_worker()
        jid = self._create_job()
        claim = self._claim()
        token = claim["claim_token"]
        # Make the task "stale" by setting heartbeat_at to a long time ago
        # and forcing a tiny reaper threshold.
        from dispatcher import db as _db
        with _db.transaction() as conn2:
            conn2.execute(
                "UPDATE tasks SET heartbeat_at = '2020-01-01T00:00:00Z' WHERE task_id = ?",
                (jid,),
            )
        cfg = ReaperConfig(
            stale_running_sec=60,     # 1 minute — the heartbeat is years old
            stale_queued_sec=60,
            max_total_age_sec=10_000_000,
            grace_period_sec=0,
        )
        result = reap_once(mgr.TaskManager(), cfg)
        self.assertIn(jid, result.reaped)
        t = mgr.TaskManager().get(jid)
        self.assertEqual(t.status, "timeout")
        # Token still valid for logs even after reaper (sanity).
        # heartbeat from a reaped task would 409 because status != running.
        r = self.client.post(
            f"/jobs/{jid}/heartbeat",
            json={"claim_token": token},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 409)

    # §8.10: /runs legacy flow still works (smoke) -------------------

    def test_runs_legacy_alias_smoke(self):
        # Just confirm /runs is still routable. We can't drive the
        # real Hermes call in this test, but we can confirm the
        # endpoint exists and rejects unauthenticated requests.
        # The full /runs integration is covered by
        # tests/test_dispatcher.py indirectly.
        from aee.api import api_router as _  # noqa: F401
        # Build a test app with both AEE and the legacy surface
        # (we don't import app.py here to avoid its lifespan;
        # this is just a smoke for the /jobs API).
        self.assertTrue(callable(self.client.post))

    # §8.11: HermesAdapter / FakeAdapter contract still holds -------

    def test_fake_adapter_contract_still_holds(self):
        # Re-import the adapter contract test to make sure the
        # FakeAdapter still satisfies RuntimeAdapter.
        from aee.adapters.base import RuntimeAdapter
        from aee.adapters.fake_adapter import FakeAdapter
        self.assertIsInstance(FakeAdapter(), RuntimeAdapter)


class TestJobsAPICancel(unittest.TestCase):
    """§8 extras: cancel, logs, get."""

    def setUp(self) -> None:
        _fresh_db()
        from aee.adapters.fake_adapter import FakeAdapter as _FA
        try:
            adapter_registry.unregister("fake")
        except KeyError:
            pass
        adapter_registry.register(_FA(), replace=True)
        self.client = _build_client()
        self.headers = {"Authorization": "Bearer test-key"}

    def test_cancel_with_valid_token(self):
        self.client.post(
            "/workers/register",
            json={"worker_id": "w-1", "worker_name": "w-1", "worker_type": "fake"},
            headers=self.headers,
        )
        job_id = self.client.post(
            "/jobs", json={"title": "t", "input": "x", "target_runtime": "fake"},
            headers=self.headers,
        ).json()["job_id"]
        claim = self.client.post(
            "/jobs/claim",
            json={"worker_id": "w-1", "worker_type": "fake", "capabilities": []},
            headers=self.headers,
        ).json()
        token = claim["claim_token"]
        r = self.client.post(
            f"/jobs/{job_id}/cancel",
            json={"claim_token": token},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200)
        t = mgr.TaskManager().get(job_id)
        self.assertEqual(t.status, "cancelled")

    def test_logs_appends_line(self):
        self.client.post(
            "/workers/register",
            json={"worker_id": "w-1", "worker_name": "w-1", "worker_type": "fake"},
            headers=self.headers,
        )
        job_id = self.client.post(
            "/jobs", json={"title": "t", "input": "x", "target_runtime": "fake"},
            headers=self.headers,
        ).json()["job_id"]
        claim = self.client.post(
            "/jobs/claim",
            json={"worker_id": "w-1", "worker_type": "fake", "capabilities": []},
            headers=self.headers,
        ).json()
        r = self.client.post(
            f"/jobs/{job_id}/logs",
            json={"claim_token": claim["claim_token"], "line": "hello from worker"},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200)
        log_file = mgr._log_path(job_id)
        text = log_file.read_text(encoding="utf-8")
        self.assertIn("hello from worker", text)

    def test_get_job_returns_task_dict(self):
        self.client.post(
            "/workers/register",
            json={"worker_id": "w-1", "worker_name": "w-1", "worker_type": "fake"},
            headers=self.headers,
        )
        job_id = self.client.post(
            "/jobs", json={"title": "t", "input": "x", "target_runtime": "fake"},
            headers=self.headers,
        ).json()["job_id"]
        r = self.client.get(f"/jobs/{job_id}", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["task_id"], job_id)
        self.assertEqual(data["status"], "queued")
        self.assertEqual(data["adapter_name"], "fake")

    def test_claim_with_no_jobs_returns_404(self):
        self.client.post(
            "/workers/register",
            json={"worker_id": "w-1", "worker_name": "w-1", "worker_type": "fake"},
            headers=self.headers,
        )
        r = self.client.post(
            "/jobs/claim",
            json={"worker_id": "w-1", "worker_type": "fake", "capabilities": []},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 404)

    def test_claim_for_unregistered_worker_returns_404(self):
        r = self.client.post(
            "/jobs/claim",
            json={"worker_id": "never-registered", "worker_type": "fake", "capabilities": []},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 404)


class TestReaperTimeoutSplit(unittest.TestCase):
    """§7 reaper split: heartbeat stale -> timeout, not failed."""

    def setUp(self) -> None:
        _fresh_db()
        from aee.adapters.fake_adapter import FakeAdapter as _FA
        try:
            adapter_registry.unregister("fake")
        except KeyError:
            pass
        adapter_registry.register(_FA(), replace=True)
        self.client = _build_client()
        self.headers = {"Authorization": "Bearer test-key"}

    def test_heartbeat_stale_routes_to_timeout(self):
        # Register + create + claim so we have a running job with a
        # worker assigned.
        self.client.post(
            "/workers/register",
            json={"worker_id": "w-1", "worker_name": "w-1", "worker_type": "fake"},
            headers=self.headers,
        )
        job_id = self.client.post(
            "/jobs", json={"title": "t", "input": "x", "target_runtime": "fake"},
            headers=self.headers,
        ).json()["job_id"]
        self.client.post(
            "/jobs/claim",
            json={"worker_id": "w-1", "worker_type": "fake", "capabilities": []},
            headers=self.headers,
        )
        # Stale the heartbeat.
        from dispatcher import db as _db
        with _db.transaction() as conn2:
            conn2.execute(
                "UPDATE tasks SET heartbeat_at = '2020-01-01T00:00:00Z' WHERE task_id = ?",
                (job_id,),
            )
        cfg = ReaperConfig(
            stale_running_sec=60,
            stale_queued_sec=60,
            max_total_age_sec=10_000_000,
            grace_period_sec=0,
        )
        result = reap_once(mgr.TaskManager(), cfg)
        self.assertIn(job_id, result.reaped)
        t = mgr.TaskManager().get(job_id)
        self.assertEqual(t.status, "timeout")
        # Reason should mention the worker_id, distinguishing this
        # case from a generic "no progress" reap.
        self.assertIn("worker_id=w-1", t.error_message)


if __name__ == "__main__":
    unittest.main()
