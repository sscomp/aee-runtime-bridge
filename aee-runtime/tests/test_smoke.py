"""aee-runtime/tests/test_smoke.py — AEE-4 Part B closed-loop smoke test.

The end-to-end "register → claim → execute → complete" cycle,
in-process. We:

1. Spin up the bridge's FastAPI app on a TestClient.
2. Start the Pi Worker daemon in a subprocess (with --dry-run
   on the runtime, so no real LLM call).
3. Create a Job via the bridge with `target_runtime="aee_lightweight"`
   and `required_capabilities=["tool.shell"]`.
4. Wait up to 10s for the Job to be claimed and completed.
5. Assert the Job's `status` is `completed` and `output_text`
   matches the canned dry-run output.

This is the spec's "Closed Loop Validation" — it proves the
register / heartbeat / claim / logs / complete cycle works
end-to-end without Hermes, without a real LLM, and without
manual intervention.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Force-test env BEFORE any imports that read env.
# Use the same key as tests/test_workers_api.py so the
# process-global os.environ doesn't pollute the AEE test
# suite that may run in the same process.
os.environ.setdefault("BRIDGE_API_KEY", "test-key")
os.environ.setdefault("DISPATCHER_API_KEY", "test-key")
# Wipe the upstream env's HERMES_API_KEY so the bridge test
# process doesn't try to call out to Hermes 8642.
os.environ.pop("HERMES_API_KEY", None)

ROOT = Path("/home/ubuntu/hermes-runtime-bridge")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from aee.api import api_router  # noqa: E402
from dispatcher import db  # noqa: E402


def _build_bridge_client() -> TestClient:
    """Build a fresh FastAPI TestClient with the AEE router mounted.
    The bridge's own DB is in a tmpdir so this test does not touch
    production data.
    """
    app = FastAPI()
    app.include_router(api_router)
    return TestClient(app)


def _fresh_bridge_db(tmp: Path) -> None:
    db.DB_DIR = tmp
    db.DB_PATH = tmp / "dispatcher.db"
    db._local.conn = None
    db._initialized = False
    if db.DB_PATH.exists():
        try:
            db.DB_PATH.unlink()
        except OSError:
            pass
    import dispatcher.manager as mgr
    mgr.LOGS_DIR = tmp / "logs"
    mgr.REPORTS_DIR = tmp / "reports"


def _make_smoke_config(tmp: Path) -> Path:
    cfg_path = tmp / "smoke-config.yaml"
    cfg_path.write_text(
        f"""
bridge_base_url: "http://127.0.0.1:1"  # never actually called; we mock it via env
bridge_api_key: "test-key"
worker_id: "aee-smoke-01"
worker_name: "aee-smoke-01"
worker_type: "aee_lightweight"
capabilities:
  - "runtime.aee_runtime"
  - "tool.shell"
workdir_root: "{tmp}/work"
workdir_allowlist:
  - "{tmp}/work"
max_concurrent: 1
claim_interval_sec: 1
heartbeat_interval_sec: 1
log_max_bytes: 4096
allowlist_commands: ["ls", "cat", "echo"]
runtime_path: ""
runtime_flags: ["--dry-run"]
""",
        encoding="utf-8",
    )
    (tmp / "provider.env").write_text(
        "PI_PROVIDER_BASE_URL=http://example.invalid/v1\n"
        "PI_PROVIDER_API_KEY=sk-smoke-1234567890\n"
        "PI_PROVIDER_MODEL=gpt-4o-mini\n",
        encoding="utf-8",
    )
    return cfg_path


class TestClosedLoop(unittest.TestCase):
    """The spec's required 'Closed Loop Validation'."""

    def test_register_claim_execute_complete_via_test_client(self):
        # We DO NOT spin up the daemon in a subprocess for this
        # test; that would require the daemon to make real HTTP
        # calls. Instead, we exercise the bridge via TestClient
        # and assert that the bridge's claim/protocol works
        # end-to-end. The daemon's lifecycle is unit-tested
        # separately (test_pi_worker.py) with all calls mocked.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _fresh_bridge_db(tmp)
            client = _build_bridge_client()
            headers = {"Authorization": "Bearer test-key"}

            # 1. Register a worker.
            reg = client.post("/v1/workers/register", json={
                "worker_name": "aee-smoke-01",
                "worker_type": "aee_lightweight",
                "capabilities": ["runtime.aee_runtime", "tool.shell"],
                "workdir_allowlist": [str(tmp / "work")],
                "max_concurrent": 1,
                "runtime_name": "aee-runtime",
                "runtime_version": "0.1.0",
                "operating_system": "linux",
                "architecture": "x86_64",
                "python_version": "3.11.2",
                "node_version": "v22.22.2",
                "git_version": "2.34.1",
                "start_time": "2026-07-10T00:00:00Z",
                "status": "idle",
            }, headers=headers)
            self.assertEqual(reg.status_code, 200, reg.text)
            self.assertEqual(reg.json()["version"], "v1")
            wid = reg.json()["worker_id"]

            # 2. Heartbeat with status=idle.
            hb = client.post(f"/v1/workers/{wid}/heartbeat", json={
                "status": "idle",
            }, headers=headers)
            self.assertEqual(hb.status_code, 200)
            self.assertEqual(hb.json()["status"], "idle")

            # 3. Create a job.
            job = client.post("/v1/jobs", json={
                "title": "smoke test",
                "input": "echo hello from pi",
                "target_runtime": "aee_lightweight",
                "required_capabilities": ["tool.shell"],
            }, headers=headers)
            self.assertEqual(job.status_code, 200, job.text)
            job_id = job.json()["job_id"]
            self.assertEqual(job.json()["version"], "v1")

            # 4. Claim.
            claim = client.post("/v1/jobs/claim", json={
                "worker_id": wid,
                "worker_type": "aee_lightweight",
                "capabilities": ["runtime.aee_runtime", "tool.shell"],
            }, headers=headers)
            self.assertEqual(claim.status_code, 200, claim.text)
            claim_token = claim.json()["claim_token"]
            self.assertEqual(claim.json()["required_capabilities"], ["tool.shell"])

            # 5. Worker switches to busy + heartbeats the Job.
            hb2 = client.post(f"/v1/workers/{wid}/heartbeat", json={
                "status": "busy",
                "status_message": f"executing {job_id}",
                "job_id": job_id,
            }, headers=headers)
            self.assertEqual(hb2.status_code, 200)
            self.assertEqual(hb2.json()["status"], "busy")

            # 6. Append a log line.
            log_resp = client.post(f"/v1/jobs/{job_id}/logs", json={
                "claim_token": claim_token,
                "line": "shell: echo hello from pi",
            }, headers=headers)
            self.assertEqual(log_resp.status_code, 200)

            # 7. Complete the job.
            done = client.post(f"/v1/jobs/{job_id}/complete", json={
                "claim_token": claim_token,
                "output_text": "hello from pi\n",
                "usage": {"input_tokens": 50, "output_tokens": 12, "total_tokens": 62},
                "raw": {"finish_reason": "dry_run"},
            }, headers=headers)
            self.assertEqual(done.status_code, 200, done.text)
            self.assertEqual(done.json()["status"], "completed")

            # 8. Job is read back as completed.
            get = client.get(f"/v1/jobs/{job_id}", headers=headers)
            self.assertEqual(get.status_code, 200)
            self.assertEqual(get.json()["status"], "completed")

            # 9. Worker goes back to idle.
            hb3 = client.post(f"/v1/workers/{wid}/heartbeat", json={
                "status": "idle",
            }, headers=headers)
            self.assertEqual(hb3.status_code, 200)
            self.assertEqual(hb3.json()["status"], "idle")
            # last_status_change_at was set when we went busy, then
            # when we went back to idle.
            self.assertIsNotNone(hb3.json()["last_status_change_at"])

            # 10. Verify the worker record has all 8 metadata fields.
            w = client.get(f"/v1/workers/{wid}", headers=headers)
            self.assertEqual(w.status_code, 200)
            data = w.json()
            for k in (
                "runtime_name", "runtime_version", "operating_system", "architecture",
                "python_version", "node_version", "git_version", "start_time",
            ):
                self.assertIn(k, data, f"missing metadata field {k!r}")
                self.assertIsNotNone(data[k], f"metadata field {k!r} is None")
            self.assertEqual(data["status"], "idle")


class TestEndToEndDryRun(unittest.TestCase):
    """Spawn the actual node runtime in --dry-run mode through
    the daemon's claim path. The daemon runs as a subprocess
    talking to a FastAPI TestClient that serves as the bridge.

    This is the full closed-loop smoke test the spec asks for.
    """

    def test_daemon_claims_completes_a_job_end_to_end(self):
        """Spin up a real bridge on a random port, run the daemon
        against it, create a Job via direct SQL on the bridge's
        DB, and assert the daemon claims + completes the Job.

        This is the spec's required "Closed Loop Validation"
        (run end-to-end against a real bridge + a real daemon
        + a real node runtime in --dry-run mode).
        """
        import socket
        import threading
        import uvicorn

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _fresh_bridge_db(tmp)
            cfg_path = _make_smoke_config(tmp)

            # Find a free port.
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            base_url = f"http://127.0.0.1:{port}"

            # Build a uvicorn server in a thread, pointed at our tmp DB.
            app = FastAPI()
            app.include_router(api_router)

            config = uvicorn.Config(
                app=app,
                host="127.0.0.1",
                port=port,
                log_level="info",
                access_log=False,
            )
            server = uvicorn.Server(config)
            server_thread = threading.Thread(target=server.run, daemon=True)
            server_thread.start()
            # Wait for the server to be ready (uvicorn signals via
            # server.started; we poll the started flag).
            deadline = time.time() + 5.0
            while time.time() < deadline and not server.started:
                time.sleep(0.05)
            if not server.started:
                self.fail("uvicorn did not start within 5s")
            try:
                # Now launch the daemon against this bridge.
                env = {
                    **os.environ,
                    "BRIDGE_API_KEY": "test-key",
                    "DISPATCHER_API_KEY": "test-key",
                    # The daemon runs in its own process; point
                    # it at the same tmpdir DB the bridge uses.
                    "DISPATCHER_DB_DIR": str(tmp),
                    "PYTHONPATH": str(ROOT),
                    "PI_PROVIDER_BASE_URL": "http://example.invalid/v1",
                    "PI_PROVIDER_API_KEY": "sk-smoke-1234567890",
                    "PI_PROVIDER_MODEL": "gpt-4o-mini",
                }
                # Override the config's bridge_base_url at runtime
                # by writing a fresh config with the real port.
                cfg_text = cfg_path.read_text()
                cfg_text = cfg_text.replace(
                    'bridge_base_url: "http://127.0.0.1:1"',
                    f'bridge_base_url: "{base_url}"',
                )
                live_cfg = tmp / "live-config.yaml"
                live_cfg.write_text(cfg_text)

                daemon = subprocess.Popen(
                    [
                        str(ROOT / ".venv" / "bin" / "python"),
                        "-u",
                        str(ROOT / "aee-runtime" / "aee_runtime.py"),
                        "--config", str(live_cfg),
                        "--env-file", str(tmp / "provider.env"),
                    ],
                    env=env,
                    cwd=str(ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                try:
                    # Give the daemon a moment to register.
                    time.sleep(2.0)
                    # Create a job via direct SQL.
                    from dispatcher import db as _db
                    conn = _db.get_conn()
                    now = _db._now_iso()
                    with _db.transaction() as c:
                        c.execute(
                            "INSERT INTO tasks (task_id, title, type, priority, owner, status, "
                            "created_at, input_text, mode, runtime_type, adapter_name, required_capabilities_json) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                "TASK-SMOKE-001",
                                "smoke",
                                "ops",
                                50,
                                "aee_lightweight",
                                "queued",
                                now,
                                "echo hello from pi",
                                "normal",
                                "aee_lightweight",
                                "aee_lightweight",
                                '["tool.shell"]',
                            ),
                        )
                    # Wait up to 15s for the daemon to claim + complete.
                    deadline = time.time() + 15.0
                    final_status = None
                    output_text = None
                    while time.time() < deadline:
                        row = conn.execute(
                            "SELECT status FROM tasks WHERE task_id = ?",
                            ("TASK-SMOKE-001",),
                        ).fetchone()
                        if row and row["status"] in ("completed", "failed", "cancelled", "timeout"):
                            final_status = row["status"]
                            # Get the output_text.
                            out = conn.execute(
                                "SELECT output_text FROM task_outputs WHERE task_id = ?",
                                ("TASK-SMOKE-001",),
                            ).fetchone()
                            output_text = out["output_text"] if out else None
                            break
                        time.sleep(0.3)
                    self.assertIsNotNone(final_status, "job did not reach a terminal state in 15s")
                    self.assertEqual(final_status, "completed",
                        f"expected completed, got {final_status}")
                    self.assertIsNotNone(output_text, "no output_text stored")
                    self.assertIn("dry-run", output_text.lower(),
                        f"expected dry-run output, got: {output_text!r}")
                    # Verify the worker is registered in the bridge.
                    # (It may still be `busy` here; the daemon
                    # sends the final `idle` heartbeat on the
                    # next iteration of its loop, which may
                    # not have happened yet.)
                    workers = conn.execute(
                        "SELECT worker_id, runtime_name FROM workers"
                    ).fetchall()
                    self.assertEqual(len(workers), 1)
                    self.assertEqual(workers[0]["worker_id"], "aee-smoke-01")
                    self.assertEqual(workers[0]["runtime_name"], "aee-runtime")
                finally:
                    daemon.terminate()
                    try:
                        daemon.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        daemon.kill()
                    # Print stdout/stderr for debugging if the test fails.
                    try:
                        out, err = daemon.communicate(timeout=2)
                    except Exception:
                        out = err = b""
                    if final_status != "completed":
                        sys.stderr.write(f"\n[smoke] daemon stdout:\n{out.decode('utf-8', errors='replace')}\n")
                        sys.stderr.write(f"[smoke] daemon stderr:\n{err.decode('utf-8', errors='replace')}\n")
            finally:
                server.should_exit = True
                server_thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
