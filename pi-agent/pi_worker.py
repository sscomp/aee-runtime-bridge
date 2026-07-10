#!/usr/bin/env python3
"""pi-agent/pi_worker.py — AEE-4 Part B Pi Worker daemon.

The first AEE-4 conformant Worker runtime. Speaks the
Worker Runtime Contract over HTTP to the bridge at
/v1/workers/register, /v1/workers/{id}/heartbeat,
/v1/jobs/claim, /v1/jobs/{id}/logs, /v1/jobs/{id}/complete,
/v1/jobs/{id}/fail. Spawns the Node.js pi-agent-runtime for
each claimed Job, streams its logs to the bridge, and reports
the final result.

The daemon is single-threaded, except for a background
heartbeat thread while a Job is running. The background
thread is the only reason for threading — the rest of the
daemon is a simple `while True` loop.

Run:

    python3 -u pi_worker.py --config /path/to/config.yaml

The daemon exits 3 if the provider env file is missing or has
placeholder values. All other failures are non-fatal: the
daemon logs and retries.

This is the **reference implementation** of the AEE-4
Worker Runtime Contract
(`docs/runtime/Worker_Runtime_Contract.md`). Future Runtimes
(Claude Code, GPT-direct, MCP) should follow the same shape
and pass the same conformance checklist (§9 of the contract).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml  # PyYAML >= 6.0; see pi-agent/requirements.txt
except ImportError:  # pragma: no cover
    print(
        "[pi_worker] PyYAML is required. Install with: "
        "pip install -r pi-agent/requirements.txt",
        file=sys.stderr,
    )
    sys.exit(2)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger("pi_worker")


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Status names from the AEE-4 Worker Runtime Contract §5.
STATUS_IDLE = "idle"
STATUS_BUSY = "busy"
STATUS_OFFLINE = "offline"
STATUS_DRAINING = "draining"
STATUS_ERROR = "error"

# Exit code from the node runtime that we map to /fail reasons
# (see runtime/pi-agent-runtime.js docstring). Codes 1-7 are
# the runtime's contract; 124 is the well-known SIGKILL exit.
_RUNTIME_EXIT_TO_ERROR = {
    2: "invalid job spec",
    3: "provider failure",
    4: "step timeout",
    5: "allowlist blocked",
    6: "workdir violation",
    7: "unknown tool",
    8: "internal error",
    9: "lease expired (killed)",
    124: "job exceeded timeout_seconds",
}


def load_config(path: Path) -> Dict[str, Any]:
    """Read the YAML config and return a dict.

    Required keys:
      bridge_base_url, bridge_api_key, worker_id, worker_name,
      worker_type, workdir_root, workdir_allowlist,
      max_concurrent, claim_interval_sec, heartbeat_interval_sec,
      allowlist_commands, log_max_bytes, runtime_path

    Optional keys:
      env_file, runtime_dir, runtime_flags
    """
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ValueError(f"config {path} is not a YAML mapping")
    # The bridge_api_key may be a literal "${BRIDGE_API_KEY}"
    # (config template convention) — substitute from the env if
    # so. We never write a real key into the config file; the
    # operator puts it in the env.
    api_key = cfg.get("bridge_api_key", "")
    if isinstance(api_key, str) and api_key.startswith("${") and api_key.endswith("}"):
        env_name = api_key[2:-1]
        api_key = os.getenv(env_name, "")
    cfg["bridge_api_key"] = api_key
    if not api_key:
        raise ValueError(
            "bridge_api_key is missing or unresolved. Set it in the env "
            "file (PI_PROVIDER_*_API_KEY) or directly via the env."
        )
    return cfg


def load_env_file(path: Optional[Path]) -> Dict[str, str]:
    """Read a simple KEY=VALUE env file (one per line, # comments).
    Used for the provider env file. Does NOT shell-expand.
    """
    if path is None:
        return {}
    if not path.exists():
        log.warning("env file not found: %s", path)
        return {}
    out: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


# ---------------------------------------------------------------------------
# Bridge HTTP client
# ---------------------------------------------------------------------------


class Bridge:
    """Tiny stdlib HTTP client for the AEE bridge. The daemon
    only needs POST + GET with bearer auth; we don't pull in
    httpx for a 200-line daemon.
    """

    def __init__(self, base_url: str, api_key: str, timeout_sec: int = 30):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_sec = timeout_sec

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            url, data=data, headers=headers, method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as e:
            raw = e.read() if e.fp else b""
            status = e.code
        except urllib.error.URLError as e:
            log.error("HTTP %s %s failed: %s", method, path, e)
            return 0, {"detail": str(e)}
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"detail": raw.decode("utf-8", errors="replace")}
        return status, payload

    def post(self, path: str, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        return self._request("POST", path, body)

    def get(self, path: str) -> Tuple[int, Dict[str, Any]]:
        return self._request("GET", path)


# ---------------------------------------------------------------------------
# Metadata (Worker Runtime Contract §4)
# ---------------------------------------------------------------------------


def collect_metadata(env: Dict[str, str]) -> Dict[str, Any]:
    """Build the 8 metadata fields for register.

    Sourced from:
      * env (PI_PROVIDER_* — runtime_name, runtime_version)
      * platform module (os, arch, python)
      * subprocess (node, git if on PATH)
      * datetime (start_time)
    """
    node_version = _safe_subprocess(["node", "--version"]) or ""
    git_version = _safe_subprocess(["git", "--version"]) or ""
    return {
        "runtime_name": env.get("PI_PROVIDER_RUNTIME_NAME", "pi"),
        "runtime_version": env.get("PI_PROVIDER_RUNTIME_VERSION", "0.1.0"),
        "operating_system": platform.system().lower() or "unknown",
        "architecture": platform.machine() or "unknown",
        "python_version": platform.python_version(),
        "node_version": node_version,
        "git_version": git_version,
        "start_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _safe_subprocess(cmd: List[str]) -> Optional[str]:
    """Run a command, capture stdout, swallow errors. Used for
    `node --version` and `git --version` which may not be on PATH
    in every environment; we don't want metadata collection to
    fail the register call.
    """
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=2)
        return out.decode("utf-8", errors="replace").strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


# ---------------------------------------------------------------------------
# The daemon
# ---------------------------------------------------------------------------


class PiWorker:
    def __init__(self, cfg: Dict[str, Any], env: Dict[str, str]):
        self.cfg = cfg
        self.env = env
        self.bridge = Bridge(cfg["bridge_base_url"], cfg["bridge_api_key"])
        self.worker_id: Optional[str] = None
        self.worker_type = cfg["worker_type"]
        self.capabilities = cfg.get("capabilities", [
            "runtime.pi", "tool.shell", "tool.python", "tool.git", "tool.filesystem",
        ])
        self._shutdown = threading.Event()
        self._current_status: str = STATUS_IDLE
        self._current_status_message: str = ""
        self._current_job_id: Optional[str] = None
        self._heartbeat_thread: Optional[threading.Thread] = None

    # --- lifecycle ---

    def run(self) -> int:
        log.info("starting pi-worker pid=%d", os.getpid())
        # Verify provider env. We don't want to start the daemon
        # just to fail on the first /v1/jobs/claim, so check early.
        for k in ("PI_PROVIDER_BASE_URL", "PI_PROVIDER_API_KEY", "PI_PROVIDER_MODEL"):
            if not self.env.get(k):
                log.error("env var %s is missing; refusing to start", k)
                return 3
        # Validate api_key is not a placeholder.
        placeholder_values = {"", "sk-REPLACE-ME", "REPLACE-ME", "your-key-here"}
        if self.env["PI_PROVIDER_API_KEY"] in placeholder_values:
            log.error("PI_PROVIDER_API_KEY is a placeholder; refusing to start")
            return 3

        # Register.
        if not self._register():
            return 4
        # Loop until SIGINT / SIGTERM.
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)
        try:
            while not self._shutdown.is_set():
                self._claim_and_execute_once()
        finally:
            self._send_offline_then_exit()
        return 0

    def _on_signal(self, signum, frame) -> None:  # noqa: ARG002
        log.info("received signal %d; shutting down", signum)
        self._shutdown.set()

    # --- register ---

    def _register(self) -> bool:
        body = {
            "worker_name": self.cfg["worker_name"],
            "worker_type": self.worker_type,
            "hostname": platform.node(),
            "capabilities": self.capabilities,
            "workdir_allowlist": self.cfg["workdir_allowlist"],
            "max_concurrent": int(self.cfg["max_concurrent"]),
            **collect_metadata(self.env),
        }
        # AEE-4: an initial status. Send "idle" so the dispatcher
        # knows we are ready to claim; if anything is wrong, the
        # daemon would have already exited.
        body["status"] = STATUS_IDLE
        body["status_message"] = "registered"
        status, payload = self.bridge.post("/v1/workers/register", body)
        if status != 200:
            log.error("register failed: %s %s", status, payload)
            return False
        self.worker_id = payload["worker_id"]
        log.info(
            "registered worker_id=%s type=%s registered_at=%s",
            self.worker_id, payload["worker_type"], payload["registered_at"],
        )
        return True

    # --- heartbeat (background thread during a Job) ---

    def _start_heartbeat_thread(self) -> None:
        assert self.worker_id is not None
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="pi-worker-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _stop_heartbeat_thread(self) -> None:
        # The thread is daemon=True; it dies with the process. We
        # just wait briefly so the final heartbeat lands.
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2)
            self._heartbeat_thread = None

    def _heartbeat_loop(self) -> None:
        # Worker-level heartbeat (not the job-level one). The
        # bridge reaper watches the worker's last_heartbeat_at;
        # if it goes stale, the worker is considered offline and
        # the running Job is reaped to `timeout`.
        interval = int(self.cfg["heartbeat_interval_sec"])
        while not self._shutdown.is_set():
            body = {
                "status": self._current_status,
                "status_message": self._current_status_message,
            }
            if self._current_job_id:
                body["job_id"] = self._current_job_id
            status, payload = self.bridge.post(
                f"/v1/workers/{self.worker_id}/heartbeat", body
            )
            if status != 200:
                log.warning("heartbeat failed: %s %s", status, payload)
            # Use Event.wait so the loop exits promptly on shutdown.
            self._shutdown.wait(interval)

    def _heartbeat_job(self, job_id: str, claim_token_hash: str) -> None:
        # Job-level heartbeat. Same cadence. This is what the
        # bridge reaper checks for the `running` Job — if this
        # goes stale, the Job is reaped to `timeout`.
        body = {"claim_token_hash": claim_token_hash}
        status, payload = self.bridge.post(
            f"/v1/jobs/{job_id}/heartbeat", body
        )
        if status != 200:
            log.warning("job heartbeat failed: %s %s", status, payload)

    # --- claim / execute / complete ---

    def _claim_and_execute_once(self) -> None:
        # 1. Heartbeat idle.
        self._current_status = STATUS_IDLE
        self._current_status_message = "between jobs"
        body = {"status": self._current_status, "status_message": self._current_status_message}
        self.bridge.post(f"/v1/workers/{self.worker_id}/heartbeat", body)
        # 2. Claim.
        body = {
            "worker_id": self.worker_id,
            "worker_type": self.worker_type,
            "capabilities": self.capabilities,
        }
        status, payload = self.bridge.post("/v1/jobs/claim", body)
        if status == 404:
            # No claimable jobs. Sleep and retry.
            interval = int(self.cfg["claim_interval_sec"])
            self._shutdown.wait(interval)
            return
        if status != 200:
            log.warning("claim returned %s: %s", status, payload)
            self._shutdown.wait(5)
            return
        job_id = payload["job_id"]
        claim_token = payload["claim_token"]
        import hashlib as _h
        claim_token_hash = _h.sha256(claim_token.encode("utf-8")).hexdigest()
        log.info("claimed job_id=%s input=%r", job_id, payload.get("input", "")[:80])
        # 3. Set busy and execute.
        self._current_status = STATUS_BUSY
        self._current_status_message = f"executing {job_id}"
        self._current_job_id = job_id
        self._start_heartbeat_thread()
        try:
            self._execute_job(job_id=job_id, payload=payload, claim_token_hash=claim_token_hash)
        finally:
            self._current_job_id = None
            self._current_status = STATUS_IDLE
            self._current_status_message = "between jobs"
            self._stop_heartbeat_thread()

    def _execute_job(
        self,
        job_id: str,
        payload: Dict[str, Any],
        claim_token_hash: str,
    ) -> None:
        # Build the per-job workdir.
        workdir_root = Path(self.cfg["workdir_root"])
        workdir = workdir_root / "jobs" / job_id
        try:
            workdir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._fail_job(job_id, claim_token_hash, f"cannot create workdir: {e}")
            return
        # Write the spec file.
        runtime_timeout = int(payload.get("timeout_seconds") or 900)
        spec = {
            "job_id": job_id,
            "input": payload.get("input", ""),
            "tools": ["shell", "file_read", "file_write"],
            "max_steps": 20,
            "per_step_timeout_ms": 30000,
            "max_output_bytes": 204800,
            "workdir": str(workdir),
            "allowlist_cmds": self.cfg["allowlist_commands"],
            "approval_required": bool(payload.get("approval_required", False)),
        }
        spec_path = workdir / "spec.json"
        try:
            spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
        except OSError as e:
            self._fail_job(job_id, claim_token_hash, f"cannot write spec: {e}")
            return
        # Spawn the node runtime.
        runtime_path = self.cfg.get("runtime_path") or self._default_runtime_path()
        flags = self.cfg.get("runtime_flags", [])
        cmd = ["node", str(runtime_path), "--job-file", str(spec_path), *flags]
        log.info("spawning runtime: %s", " ".join(cmd))
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(workdir),
                env={**os.environ, **self.env},  # inherit env, layer PI_PROVIDER_*
                text=True,
            )
        except (OSError, FileNotFoundError) as e:
            self._fail_job(job_id, claim_token_hash, f"cannot spawn runtime: {e}")
            return
        # Stream stdout (single JSON line on success) and stderr
        # (logs) to the bridge. Wait with a hard timeout.
        try:
            stdout, stderr = proc.communicate(timeout=runtime_timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = "", ""
            timed_out = True
        if timed_out:
            err = _RUNTIME_EXIT_TO_ERROR.get(124, f"job exceeded {runtime_timeout}s")
            self._fail_job(job_id, claim_token_hash, err)
            return
        # Stream the stderr lines as logs.
        for line in (stderr or "").splitlines():
            if line.strip():
                self._log_line(job_id, claim_token_hash, line)
        # Parse the final stdout.
        if not stdout:
            self._fail_job(job_id, claim_token_hash, "runtime produced no output")
            return
        result_line = stdout.strip().splitlines()[-1]
        try:
            result = json.loads(result_line)
        except json.JSONDecodeError:
            self._fail_job(job_id, claim_token_hash, f"runtime output not JSON: {result_line[:200]}")
            return
        # Map runtime exit code → daemon decision.
        if proc.returncode == 0 and result.get("status") == "ok":
            self._complete_job(job_id, claim_token_hash, result)
            return
        # Non-zero. The runtime may have set a result.error or
        # we use the exit-code map.
        msg = result.get("error") or _RUNTIME_EXIT_TO_ERROR.get(proc.returncode, f"runtime exit {proc.returncode}")
        self._fail_job(job_id, claim_token_hash, msg)

    def _default_runtime_path(self) -> Path:
        # Default: <repo>/pi-agent/runtime/pi-agent-runtime.js
        return Path(__file__).resolve().parent / "runtime" / "pi-agent-runtime.js"

    def _log_line(self, job_id: str, claim_token_hash: str, line: str) -> None:
        max_bytes = int(self.cfg.get("log_max_bytes", 4096))
        line = line[:max_bytes]
        self.bridge.post(
            f"/v1/jobs/{job_id}/logs",
            {"claim_token_hash": claim_token_hash, "line": line},
        )

    def _complete_job(
        self,
        job_id: str,
        claim_token_hash: str,
        result: Dict[str, Any],
    ) -> None:
        body = {
            "claim_token_hash": claim_token_hash,
            "output_text": result.get("output") or "",
            "usage": result.get("usage") or {},
            "raw": {
                "tool_calls": result.get("tool_calls", []),
                "finish_reason": result.get("finish_reason"),
            },
        }
        status, payload = self.bridge.post(f"/v1/jobs/{job_id}/complete", body)
        if status == 200:
            log.info("job %s completed: %s", job_id, payload.get("status"))
        else:
            log.error("complete failed: %s %s", status, payload)

    def _fail_job(
        self,
        job_id: str,
        claim_token_hash: str,
        error: str,
    ) -> None:
        body = {"claim_token_hash": claim_token_hash, "error": error[:1000]}
        status, payload = self.bridge.post(f"/v1/jobs/{job_id}/fail", body)
        if status == 200:
            log.warning("job %s failed: %s", job_id, payload.get("status"))
        else:
            log.error("fail failed: %s %s", status, payload)

    # --- shutdown ---

    def _send_offline_then_exit(self) -> None:
        if self.worker_id is None:
            return
        try:
            self.bridge.post(
                f"/v1/workers/{self.worker_id}/heartbeat",
                {"status": STATUS_OFFLINE, "status_message": "shutting down"},
            )
        except Exception as e:  # noqa: BLE001
            log.warning("offline heartbeat failed: %s", e)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Pi Worker daemon (AEE-4 Part B)")
    parser.add_argument("--config", required=True, type=Path, help="path to config.yaml")
    parser.add_argument("--env-file", type=Path, default=None, help="path to provider env file (overrides config)")
    parser.add_argument("--verbose", "-v", action="store_true", help="DEBUG-level logging")
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        log.error("config error: %s", e)
        return 2
    env_path = args.env_file or (Path(cfg["env_file"]) if cfg.get("env_file") else None)
    env = load_env_file(env_path)
    # Layer in any PI_* env vars the operator exported in the
    # daemon's process env (so unit tests can inject them).
    for k, v in os.environ.items():
        if k.startswith("PI_") and k not in env:
            env[k] = v
    try:
        return PiWorker(cfg, env).run()
    except KeyboardInterrupt:
        log.info("interrupted; exiting")
        return 130


if __name__ == "__main__":
    sys.exit(main())
