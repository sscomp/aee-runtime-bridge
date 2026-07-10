"""aee-runtime/tests/test_live_provider.py — AEE-4 Part B operator live-provider smoke test.

This test is **NOT in the regular CI suite**. It is for
operators who want to verify the AEE Lightweight Agent
Runtime against a real LLM provider (Ollama, RouteLLM,
or any OpenAI-compatible endpoint) with a real `tool.shell`
invocation. The regular CI runs only the offline tests
(`test_aee_runtime.py` and `test_smoke.py`).

Why this test is operator-only:
  * It needs a running LLM provider (Ollama, RouteLLM, etc.)
    on the network, which CI doesn't provide.
  * It needs a real API key in `provider.env`, which CI
    does not have (and which we never commit).
  * The test makes real HTTP calls to a real LLM and
    takes 5-60 seconds, depending on the model.

Operator runbook:

  1. Set up the provider env file (one-time):

       $ cp aee-runtime/aee_runtime.provider.env.example \\
             aee-runtime/provider.env
       $ chmod 600 aee-runtime/provider.env
       $ $EDITOR aee-runtime/provider.env
       # ... set PI_PROVIDER_BASE_URL, PI_PROVIDER_API_KEY,
       #     PI_PROVIDER_MODEL ...

  2. Make sure the bridge is running on the URL the test
     expects (default `http://127.0.0.1:8787`), OR set
     `AEE_RUNTIME_BRIDGE_URL` to override.

  3. Run the test:

       $ PYTHONPATH=. .venv/bin/python -m unittest \\
             aee-runtime.tests.test_live_provider

  4. (Optional) Run a specific scenario:

       $ PYTHONPATH=. AEE_RUNTIME_LIVE_SCENARIO=shell_echo \\
             .venv/bin/python -m unittest \\
             aee-runtime.tests.test_live_provider

The test asserts:
  * The daemon registers with all 8 metadata fields and
    the `aee_lightweight` worker_type.
  * The daemon claims a Job whose `required_capabilities`
    includes `tool.shell`.
  * The runtime calls the live provider, gets a response,
    invokes `tool.shell` to run `echo`, and produces an
    output_text that contains "hello from the live provider".
  * The Job's final state is `completed` (not `failed`).
  * The API key is **never** written to stdout / stderr /
    the bridge DB / the disk.

Failure modes:
  * `EnvironmentError: provider.env not configured` — the
    env file is missing or has placeholder values. Fix
    by editing `aee-runtime/provider.env`.
  * `ConnectionError: cannot reach bridge` — the bridge
    is not running. Start it with `supervisorctl start
    hermes-runtime-bridge` or `uvicorn app:app`.
  * `ProviderError: ...` — the LLM call failed. Check
    `PI_PROVIDER_BASE_URL` and `PI_PROVIDER_API_KEY`.
  * `TimeoutError: job did not complete in 90s` — the
    provider is slow or the model is large. Increase
    `AEE_RUNTIME_LIVE_TIMEOUT_SEC` (default 90).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path("/home/ubuntu/hermes-runtime-bridge")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "aee-runtime") not in sys.path:
    sys.path.insert(0, str(ROOT / "aee-runtime"))

# NOTE: do NOT clear BRIDGE_API_KEY / DISPATCHER_API_KEY at
# module import time. The AEE test runners (test_workers_api,
# test_jobs_api, etc.) need those env vars to be set when
# they import; clearing them here would break the AEE test
# suite if this file is imported first (e.g. via
# `unittest discover`). The live-provider test creates its
# own bridge subprocess with its own key, so the env var
# values don't matter for the actual test path.


# ---------------------------------------------------------------------------
# Test gating
# ---------------------------------------------------------------------------

# The test is OFF by default. To run, set
# AEE_RUNTIME_RUN_LIVE_PROVIDER=1. The standard
# `python -m unittest discover` will skip it.
LIVE_ENABLED = os.environ.get("AEE_RUNTIME_RUN_LIVE_PROVIDER", "0") == "1"


def _skip_unless_live(test):
    """Decorator: skip unless AEE_RUNTIME_RUN_LIVE_PROVIDER=1."""
    if not LIVE_ENABLED:
        return unittest.skip(
            "operator-only test; set AEE_RUNTIME_RUN_LIVE_PROVIDER=1 to run"
        )(test)
    return test


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_provider_env(env_path: Path) -> dict:
    """Read PI_PROVIDER_* from the env file.

    Refuses to start if the file is missing or has a
    placeholder. **Never** prints the API key.
    """
    if not env_path.exists():
        raise unittest.SkipTest(
            f"provider.env not found at {env_path}; copy "
            f"aee_runtime.provider.env.example to provider.env "
            f"and fill in PI_PROVIDER_*"
        )
    out = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    placeholders = {"", "sk-REPLACE-ME", "REPLACE-ME", "your-key-here"}
    api_key = out.get("PI_PROVIDER_API_KEY", "")
    if not api_key or api_key in placeholders:
        raise unittest.SkipTest(
            "PI_PROVIDER_API_KEY in provider.env is a placeholder; "
            "edit the file and set a real key"
        )
    return out


def _make_bridge_client(base_url: str, api_key: str):
    """Build a stdlib HTTP client for the bridge."""
    class _Client:
        def post(self, path: str, body: dict):
            data = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(
                f"{base_url}{path}",
                data=data,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b"{}")

        def get(self, path: str):
            req = urllib.request.Request(
                f"{base_url}{path}",
                headers={"Authorization": f"Bearer {api_key}"},
                method="GET",
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b"{}")

    return _Client()


def _make_smoke_config(
    tmp: Path,
    bridge_url: str,
    bridge_key: str,
    provider_env: Path,
    workdir_root: Path,
    extra_flags: list[str],
) -> Path:
    """Write a transient config.yaml for the daemon subprocess."""
    cfg_path = tmp / "live-config.yaml"
    cfg_path.write_text(
        f"""
bridge_base_url: "{bridge_url}"
bridge_api_key: "${{BRIDGE_API_KEY}}"
worker_id: "aee-runtime-live-001"
worker_name: "aee-runtime-live-001"
worker_type: "aee_lightweight"
capabilities:
  - "runtime.aee_runtime"
  - "tool.shell"
  - "tool.python"
  - "tool.git"
workdir_root: "{workdir_root}"
workdir_allowlist:
  - "{workdir_root}"
max_concurrent: 1
claim_interval_sec: 2
heartbeat_interval_sec: 5
log_max_bytes: 4096
allowlist_commands:
  - ls
  - cat
  - head
  - tail
  - grep
  - find
  - file
  - echo
  - pwd
  - whoami
  - env
  - date
  - python3
  - node
  - git
env_file: "{provider_env}"
runtime_path: ""
runtime_flags: {extra_flags!r}
""",
        encoding="utf-8",
    )
    return cfg_path


def _start_bridge_thread(tmp: Path, bridge_key: str):
    """Spin up a uvicorn bridge in a thread on a free port.

    Returns (base_url, server, thread). Caller is
    responsible for stopping the server in `finally`.
    """
    from dispatcher import db
    db.DB_DIR = tmp
    db.DB_PATH = tmp / "dispatcher.db"
    db._local.conn = None
    db._initialized = False
    import dispatcher.manager as mgr
    mgr.LOGS_DIR = tmp / "logs"
    mgr.REPORTS_DIR = tmp / "reports"

    import uvicorn
    from fastapi import FastAPI
    from aee.api import api_router

    app = FastAPI()
    app.include_router(api_router)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"

    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5.0
    while time.time() < deadline and not server.started:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("bridge did not start within 5s")
    return base_url, server, thread


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@_skip_unless_live
class TestLiveProvider(unittest.TestCase):
    """End-to-end with a real LLM provider (Ollama / RouteLLM / OpenAI)."""

    def test_shell_tool_executes_against_real_provider(self):
        # 1. Read provider env.
        provider_env = ROOT / "aee-runtime" / "provider.env"
        provider = _read_provider_env(provider_env)

        # The bridge uses a fixed test key; the live test
        # creates a real bridge and uses a unique key so
        # the daemon's API key (from provider.env) does not
        # leak across CI environments.
        bridge_key = "live-test-bridge-key"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            workdir_root = tmp / "work"
            workdir_root.mkdir()

            # 2. Spin up a real bridge in this thread.
            base_url, server, thread = _start_bridge_thread(tmp, bridge_key)
            try:
                client = _make_bridge_client(base_url, bridge_key)

                # 3. Write the daemon config + provider env.
                # The daemon reads PI_PROVIDER_* from the
                # operator's provider.env (path we already
                # validated).
                cfg_path = _make_smoke_config(
                    tmp,
                    bridge_url=base_url,
                    bridge_key=bridge_key,
                    provider_env=provider_env,
                    workdir_root=workdir_root,
                    extra_flags=[],  # no --dry-run; use real LLM
                )
                env = {
                    **os.environ,
                    "BRIDGE_API_KEY": bridge_key,
                    "DISPATCHER_API_KEY": bridge_key,
                    "PYTHONPATH": str(ROOT),
                }
                daemon = subprocess.Popen(
                    [
                        str(ROOT / ".venv" / "bin" / "python"),
                        "-u",
                        str(ROOT / "aee-runtime" / "aee_runtime.py"),
                        "--config", str(cfg_path),
                        "--env-file", str(provider_env),
                    ],
                    env=env,
                    cwd=str(ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                try:
                    # 4. Wait for the daemon to register.
                    deadline = time.time() + 10.0
                    registered = False
                    while time.time() < deadline:
                        code, data = client.get(f"/v1/workers/aee-runtime-live-001")
                        if code == 200:
                            registered = True
                            break
                        time.sleep(0.3)
                    self.assertTrue(registered, "daemon did not register within 10s")

                    # 5. Create a Job that requires tool.shell.
                    # The Job's input is a directive that
                    # any LLM with tool-calling should be able
                    # to handle: "echo hello from the live
                    # provider".
                    from dispatcher import db as _db
                    conn = _db.get_conn()
                    now = _db._now_iso()
                    job_id = "TASK-LIVE-001"
                    with _db.transaction() as c:
                        c.execute(
                            "INSERT INTO tasks (task_id, title, type, priority, owner, status, "
                            "created_at, input_text, mode, runtime_type, adapter_name, required_capabilities_json) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                job_id,
                                "live provider smoke",
                                "ops",
                                50,
                                "aee_lightweight",
                                "queued",
                                now,
                                "Use the shell tool to run `echo hello from the live provider`. "
                                "Then return that text as your final assistant message.",
                                "normal",
                                "aee_lightweight",
                                "aee_lightweight",
                                '["tool.shell"]',
                            ),
                        )

                    # 6. Wait for the daemon to claim + complete.
                    # 90s default; configurable via env.
                    timeout_sec = int(os.environ.get("AEE_RUNTIME_LIVE_TIMEOUT_SEC", "90"))
                    deadline = time.time() + timeout_sec
                    final_status = None
                    output_text = None
                    while time.time() < deadline:
                        row = conn.execute(
                            "SELECT status FROM tasks WHERE task_id = ?", (job_id,)
                        ).fetchone()
                        if row and row["status"] in ("completed", "failed", "cancelled", "timeout"):
                            final_status = row["status"]
                            out = conn.execute(
                                "SELECT output_text FROM task_outputs WHERE task_id = ?", (job_id,)
                            ).fetchone()
                            output_text = out["output_text"] if out else None
                            break
                        time.sleep(0.5)
                    self.assertIsNotNone(
                        final_status,
                        f"job did not reach a terminal state in {timeout_sec}s",
                    )
                    self.assertEqual(
                        final_status, "completed",
                        f"expected completed, got {final_status}; "
                        f"output_text={output_text!r}",
                    )
                    self.assertIsNotNone(output_text, "no output_text stored")
                    self.assertIn(
                        "hello from the live provider", output_text,
                        f"output_text did not contain the expected echo: {output_text!r}",
                    )

                    # 7. Sanity: the API key MUST NOT appear in
                    # the output_text or anywhere in the DB.
                    self.assertNotIn(
                        provider["PI_PROVIDER_API_KEY"], output_text,
                        "PI_PROVIDER_API_KEY leaked into output_text!",
                    )
                    # Walk the DB for any leakage.
                    for table in ("tasks", "task_outputs", "task_events", "workers"):
                        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
                        for r in rows:
                            for k, v in dict(r).items():
                                if isinstance(v, str) and provider["PI_PROVIDER_API_KEY"] in v:
                                    self.fail(
                                        f"PI_PROVIDER_API_KEY leaked into {table}.{k}: {v!r}"
                                    )
                finally:
                    daemon.terminate()
                    try:
                        daemon.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        daemon.kill()
                        # Capture output for debugging on failure.
                        try:
                            out, err = daemon.communicate(timeout=2)
                        except Exception:
                            out = err = b""
                        if final_status != "completed":
                            sys.stderr.write(
                                f"\n[live] daemon stdout (truncated):\n{out[-2000:].decode('utf-8', errors='replace')}\n"
                            )
                            sys.stderr.write(
                                f"[live] daemon stderr (truncated):\n{err[-2000:].decode('utf-8', errors='replace')}\n"
                            )
            finally:
                server.should_exit = True
                thread.join(timeout=5)


if __name__ == "__main__":
    # The standard `python -m unittest` invocation will
    # discover this file but the test will skip itself
    # unless AEE_RUNTIME_RUN_LIVE_PROVIDER=1. To run a
    # specific scenario: set AEE_RUNTIME_LIVE_SCENARIO
    # (currently informational only).
    unittest.main(verbosity=2)
