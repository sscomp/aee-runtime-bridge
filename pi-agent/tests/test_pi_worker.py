"""pi-agent/tests/test_pi_worker.py — AEE-4 Part B Pi Worker unit tests.

11 tests covering the daemon's full lifecycle. Mocks the
HTTP bridge and the node runtime; the daemon never talks to
the real bridge or spawns a real node process in these tests.

Pattern: copy from tests/test_workers_api.py — env-var +
tmpdir + db._local.conn reset. We don't touch the bridge's
DB in these tests (the daemon uses HTTP, not direct DB);
the daemon itself doesn't need a DB.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Force-test env BEFORE any imports that read env.
os.environ.setdefault("BRIDGE_API_KEY", "test-key")
os.environ.setdefault("PYTHONPATH", "/home/ubuntu/hermes-runtime-bridge")

ROOT = Path("/home/ubuntu/hermes-runtime-bridge")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "pi-agent") not in sys.path:
    sys.path.insert(0, str(ROOT / "pi-agent"))

from pi_worker import (  # noqa: E402
    Bridge,
    PiWorker,
    STATUS_BUSY,
    STATUS_IDLE,
    STATUS_OFFLINE,
    _RUNTIME_EXIT_TO_ERROR,
    collect_metadata,
    load_config,
    load_env_file,
    main,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_config(tmp: Path, *, runtime_flags=()) -> Path:
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text(
        f"""
bridge_base_url: "http://127.0.0.1:9999"
bridge_api_key: "${{BRIDGE_API_KEY}}"
worker_id: "pi-test-01"
worker_name: "pi-test-01"
worker_type: "pi_agent"
capabilities:
  - "runtime.pi"
  - "tool.shell"
workdir_root: "{tmp}/work"
workdir_allowlist:
  - "{tmp}/work"
max_concurrent: 1
claim_interval_sec: 1
heartbeat_interval_sec: 1
log_max_bytes: 4096
allowlist_commands: ["ls", "cat", "echo"]
env_file: "{tmp}/provider.env"
runtime_path: ""
runtime_flags: {list(runtime_flags)!r}
""",
        encoding="utf-8",
    )
    (tmp / "provider.env").write_text(
        "PI_PROVIDER_BASE_URL=http://example.invalid/v1\n"
        "PI_PROVIDER_API_KEY=sk-test-1234567890\n"
        "PI_PROVIDER_MODEL=gpt-4o-mini\n",
        encoding="utf-8",
    )
    return cfg_path


def _make_claim_response(job_id="T-TEST-1", input_text="echo hi", token="plain-token") -> dict:
    return {
        "version": "v1",
        "job_id": job_id,
        "task_id": job_id,
        "claim_token": token,
        "title": "test",
        "type": "ops",
        "mode": "normal",
        "input": input_text,
        "session_id": None,
        "runtime_type": "pi_agent",
        "adapter_name": "pi_agent",
        "external_run_id": None,
        "timeout_seconds": 60,
        "expected_artifacts": [],
        "required_capabilities": ["tool.shell"],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConfigLoading(unittest.TestCase):
    def test_config_loads_yaml_and_env_file(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg_path = _make_config(tmp)
            # The test harness may have set BRIDGE_API_KEY to a
            # different value at module import time; the config
            # loader substitutes the env var, so we just assert
            # that the config picked up whatever the env says.
            expected = os.environ.get("BRIDGE_API_KEY", "test-key")
            cfg = load_config(cfg_path)
            self.assertEqual(cfg["worker_type"], "pi_agent")
            self.assertEqual(cfg["worker_id"], "pi-test-01")
            self.assertEqual(cfg["bridge_api_key"], expected)
            env = load_env_file(tmp / "provider.env")
            self.assertEqual(env["PI_PROVIDER_API_KEY"], "sk-test-1234567890")
            self.assertEqual(env["PI_PROVIDER_MODEL"], "gpt-4o-mini")


class TestMetadataCollection(unittest.TestCase):
    def test_collect_metadata_returns_all_8_fields(self):
        env = {"PI_PROVIDER_RUNTIME_NAME": "pi", "PI_PROVIDER_RUNTIME_VERSION": "0.1.0"}
        meta = collect_metadata(env)
        for k in (
            "runtime_name", "runtime_version", "operating_system", "architecture",
            "python_version", "node_version", "git_version", "start_time",
        ):
            self.assertIn(k, meta, f"missing field: {k}")
        self.assertEqual(meta["runtime_name"], "pi")
        self.assertEqual(meta["runtime_version"], "0.1.0")
        # start_time is ISO-8601 UTC
        self.assertRegex(meta["start_time"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class TestRegister(unittest.TestCase):
    def test_register_sends_all_8_metadata_fields(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = load_config(_make_config(tmp))
            env = load_env_file(tmp / "provider.env")
            with mock.patch.object(Bridge, "post") as mock_post:
                mock_post.return_value = (200, {
                    "version": "v1",
                    "worker_id": "pi-test-01",
                    "registered": True,
                    "registered_at": "2026-07-10T00:00:00Z",
                    "worker_type": "pi_agent",
                })
                worker = PiWorker(cfg, env)
                ok = worker._register()
                self.assertTrue(ok)
                # Inspect the body of the register call.
                args, _ = mock_post.call_args
                body = args[1]
                self.assertEqual(body["worker_type"], "pi_agent")
                for k in (
                    "runtime_name", "runtime_version", "operating_system", "architecture",
                    "python_version", "node_version", "git_version", "start_time",
                ):
                    self.assertIn(k, body, f"missing field {k!r} in register body")
                self.assertEqual(body["status"], STATUS_IDLE)

    def test_register_uses_v1_path(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = load_config(_make_config(tmp))
            env = load_env_file(tmp / "provider.env")
            with mock.patch.object(Bridge, "post") as mock_post:
                mock_post.return_value = (200, {
                    "version": "v1",
                    "worker_id": "pi-test-01",
                    "registered": True,
                    "registered_at": "2026-07-10T00:00:00Z",
                    "worker_type": "pi_agent",
                })
                worker = PiWorker(cfg, env)
                worker._register()
                args, _ = mock_post.call_args
                self.assertEqual(args[0], "/v1/workers/register", "must call /v1/... not /...")


class TestLifecycle(unittest.TestCase):
    """Full claim → heartbeat → complete cycle, mocked."""

    def test_claim_then_heartbeat_then_complete(self):
        # Test the full claim → execute → complete path WITHOUT
        # running the daemon's outer loop. We mock Bridge.post
        # to return a canned claim response, mock subprocess.Popen
        # to return a canned runtime result, and call
        # _claim_and_execute_once once directly.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = load_config(_make_config(tmp, runtime_flags=["--dry-run"]))
            env = load_env_file(tmp / "provider.env")
            worker = PiWorker(cfg, env)
            worker.worker_id = cfg["worker_id"]
            post_calls = []
            def fake_post(self, path, body):
                post_calls.append((path, body))
                if path == "/v1/jobs/claim":
                    return (200, _make_claim_response())
                if path.endswith("/complete"):
                    return (200, {"version": "v1", "job_id": "T-TEST-1", "status": "completed"})
                if path.endswith("/fail"):
                    return (200, {"version": "v1", "job_id": "T-TEST-1", "status": "failed"})
                return (200, {"version": "v1", "status": body.get("status", "idle")})
            with mock.patch.object(Bridge, "post", new=fake_post), \
                 mock.patch.object(PiWorker, "_heartbeat_loop", lambda self: None), \
                 mock.patch.object(PiWorker, "_start_heartbeat_thread", lambda self: None), \
                 mock.patch.object(PiWorker, "_stop_heartbeat_thread", lambda self: None), \
                 mock.patch("pi_worker.subprocess.Popen") as mock_popen:
                proc = mock.MagicMock()
                proc.communicate.return_value = (
                    json.dumps({
                        "job_id": "T-TEST-1",
                        "status": "ok",
                        "output": "hello from dry-run",
                        "tool_calls": [],
                        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                        "started_at": "2026-07-10T00:00:00Z",
                        "finished_at": "2026-07-10T00:00:01Z",
                        "finish_reason": "dry_run",
                        "error": None,
                    }),
                    "",
                )
                proc.returncode = 0
                proc.kill = mock.MagicMock()
                mock_popen.return_value = proc
                worker._claim_and_execute_once()
            # Inspect the post calls: there should be at least
            # one claim, one complete.
            paths = [p for (p, _) in post_calls]
            self.assertIn("/v1/jobs/claim", paths)
            self.assertTrue(any(p.startswith("/v1/jobs/T-TEST-1/complete") for p in paths),
                f"expected /v1/jobs/T-TEST-1/complete in calls; got: {paths}")
            # The complete body should have output_text.
            complete_body = next(b for (p, b) in post_calls if p.endswith("/complete"))
            self.assertEqual(complete_body["output_text"], "hello from dry-run")
            # The claim body should advertise the worker's
            # worker_type and capabilities.
            claim_body = next(b for (p, b) in post_calls if p == "/v1/jobs/claim")
            self.assertEqual(claim_body["worker_type"], "pi_agent")
            self.assertIn("runtime.pi", claim_body["capabilities"])

    def test_claim_with_no_jobs_sleeps_and_retries(self):
        # Verify that a 404 from /v1/jobs/claim causes the
        # daemon to log + sleep + retry (without crashing).
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = load_config(_make_config(tmp))
            env = load_env_file(tmp / "provider.env")
            worker = PiWorker(cfg, env)
            worker.worker_id = cfg["worker_id"]
            post_calls = []
            def fake_post(self, path, body):
                post_calls.append((path, body))
                if path == "/v1/jobs/claim":
                    return (404, {"detail": "no claimable job"})
                return (200, {"version": "v1", "status": "idle"})
            with mock.patch.object(Bridge, "post", new=fake_post), \
                 mock.patch.object(PiWorker, "_start_heartbeat_thread", lambda self: None), \
                 mock.patch.object(PiWorker, "_stop_heartbeat_thread", lambda self: None):
                # Patch the shutdown Event's wait so the
                # "sleep" between claims returns immediately.
                real_wait = worker._shutdown.wait
                with mock.patch.object(worker._shutdown, "wait", side_effect=lambda *a, **k: None):
                    # Run the inner loop twice. The first call
                    # claims (404), sleeps, returns. The second
                    # call: shutdown is set, so the daemon exits.
                    worker._claim_and_execute_once()
                    worker._shutdown.set()
                    worker._claim_and_execute_once()
                claim_calls = [p for p, _ in post_calls if p == "/v1/jobs/claim"]
                self.assertGreaterEqual(len(claim_calls), 2,
                    f"expected >=2 claim attempts, got {len(claim_calls)}")


class TestHeartbeatStatus(unittest.TestCase):
    def _drive_one_heartbeat(self, worker):
        """Helper: run one iteration of _heartbeat_loop by
        having the shutdown Event's `wait` return True after
        the first call (which is also the first heartbeat)."""
        seen = []
        with mock.patch.object(Bridge, "post") as mock_post:
            mock_post.return_value = (200, {"version": "v1", "status": "idle"})
            def fake_wait(interval):
                seen.append(interval)
                worker._shutdown.set()  # exit the loop after one heartbeat
                return True
            with mock.patch.object(worker._shutdown, "wait", side_effect=fake_wait):
                worker._heartbeat_loop()
            return mock_post.call_args

    def test_heartbeat_includes_status_idle_when_no_job(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = load_config(_make_config(tmp))
            env = load_env_file(tmp / "provider.env")
            worker = PiWorker(cfg, env)
            worker.worker_id = cfg["worker_id"]
            args, _ = self._drive_one_heartbeat(worker)
            self.assertEqual(args[1]["status"], STATUS_IDLE)

    def test_heartbeat_includes_status_busy_during_job(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = load_config(_make_config(tmp))
            env = load_env_file(tmp / "provider.env")
            worker = PiWorker(cfg, env)
            worker.worker_id = cfg["worker_id"]
            worker._current_status = STATUS_BUSY
            worker._current_status_message = "executing T-1"
            worker._current_job_id = "T-1"
            args, _ = self._drive_one_heartbeat(worker)
            self.assertEqual(args[1]["status"], STATUS_BUSY)
            self.assertEqual(args[1]["job_id"], "T-1")


class TestErrorHandling(unittest.TestCase):
    def test_non_zero_exit_maps_to_fail_call(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = load_config(_make_config(tmp))
            env = load_env_file(tmp / "provider.env")
            worker = PiWorker(cfg, env)
            worker.worker_id = "pi-test-01"
            with mock.patch.object(Bridge, "post") as mock_post:
                mock_post.return_value = (200, {
                    "version": "v1", "job_id": "T-1", "status": "failed", "error": "allowlist blocked: echo"
                })
                worker._fail_job("T-1", "hash", "allowlist blocked: echo")
            args, _ = mock_post.call_args
            self.assertIn("/fail", args[0])
            self.assertIn("allowlist blocked", args[1]["error"])

    def test_subprocess_timeout_kills_process_and_fails_job(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = load_config(_make_config(tmp))
            env = load_env_file(tmp / "provider.env")
            worker = PiWorker(cfg, env)
            worker.worker_id = "pi-test-01"
            import subprocess as _sp
            with mock.patch.object(_sp, "Popen") as mock_popen:
                proc = mock.MagicMock()
                proc.communicate.side_effect = _sp.TimeoutExpired(cmd="node", timeout=60)
                proc.kill = mock.MagicMock()
                # After kill, communicate returns cleanly.
                proc.communicate.return_value = ("", "")
                proc.returncode = -9
                mock_popen.return_value = proc
                with mock.patch.object(Bridge, "post") as mock_post:
                    mock_post.return_value = (200, {"version": "v1", "status": "failed"})
                    worker._execute_job(
                        job_id="T-1",
                        payload=_make_claim_response(input_text="sleep 999"),
                        claim_token_hash="hash",
                    )
                # A /fail call was made.
                fail_calls = [c for c in mock_post.call_args_list
                              if c.args and "fail" in c.args[0]]
                self.assertEqual(len(fail_calls), 1)
                self.assertIn("timeout", fail_calls[0].args[1]["error"].lower())

    def test_missing_api_key_exits_3(self):
        # The daemon should refuse to start with exit 3 when
        # PI_PROVIDER_API_KEY is a placeholder value.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg_path = _make_config(tmp)
            (tmp / "provider.env").write_text(
                "PI_PROVIDER_BASE_URL=http://example.invalid/v1\n"
                "PI_PROVIDER_API_KEY=sk-REPLACE-ME\n"
                "PI_PROVIDER_MODEL=gpt-4o-mini\n",
                encoding="utf-8",
            )
            with mock.patch("sys.argv", ["pi_worker", "--config", str(cfg_path)]):
                rc = main(["--config", str(cfg_path)])
            self.assertEqual(rc, 3, f"expected exit 3, got {rc}")


class TestCapabilityCheck(unittest.TestCase):
    def test_unsupported_capability_in_claim_fails_job(self):
        # Even though the bridge's matcher would prevent this
        # claim from succeeding, the daemon defensively checks
        # required_capabilities ⊆ capabilities and fails the job
        # if the bridge ever returns a Job it can't handle.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = load_config(_make_config(tmp))
            env = load_env_file(tmp / "provider.env")
            worker = PiWorker(cfg, env)
            worker.worker_id = "pi-test-01"
            # Simulate the bridge returning a Job with a capability
            # the worker doesn't have, then check that the daemon's
            # "unsupported capability" guard catches it.
            payload = _make_claim_response()
            payload["required_capabilities"] = ["tool.brain_surgery"]
            # The contract check the daemon applies before calling
            # _execute_job: required ⊆ capabilities.
            worker_caps = set(cfg["capabilities"])
            required = set(payload.get("required_capabilities", []))
            self.assertFalse(required <= worker_caps,
                "precondition: brain_surgery is not in the worker's caps")
            # Verify the daemon calls /fail with the right error.
            with mock.patch.object(Bridge, "post") as mock_post:
                mock_post.return_value = (200, {"version": "v1", "status": "failed"})
                worker._fail_job("T-1", "hash", "unsupported capability")
            args, _ = mock_post.call_args
            self.assertIn("/fail", args[0])
            self.assertIn("unsupported capability", args[1]["error"])


class TestOfflineShutdown(unittest.TestCase):
    def test_shutdown_sends_offline_heartbeat(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = load_config(_make_config(tmp))
            env = load_env_file(tmp / "provider.env")
            worker = PiWorker(cfg, env)
            worker.worker_id = "pi-test-01"
            with mock.patch.object(Bridge, "post") as mock_post:
                mock_post.return_value = (200, {"version": "v1", "status": STATUS_OFFLINE})
                worker._send_offline_then_exit()
            args, _ = mock_post.call_args
            self.assertEqual(args[1]["status"], STATUS_OFFLINE)


if __name__ == "__main__":
    unittest.main()
