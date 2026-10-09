#!/usr/bin/env python3
"""AEE v2 MCP gateway: restricted five-tool surface and bounded Codex jobs.

Production uses an approved deployment manifest, dedicated inference broker,
immutable workspace and kernel-enforced resource/sandbox policy. No deployment
approval or real inference is implied by the offline fixture test path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Config (env-driven; no hardcoded secrets)
# ---------------------------------------------------------------------------

AEE_MCP_HOST = os.getenv("AEE_MCP_HOST", "127.0.0.1")
AEE_MCP_PORT = int(os.getenv("AEE_MCP_PORT", "8790"))
AEE_MCP_API_KEY = os.getenv("MCP_BRIDGE_API_KEY", "").strip()

# Bounded-exec hard limits.
AEE_EXEC_TIMEOUT_SEC = int(os.getenv("AEE_EXEC_TIMEOUT_SEC", "15"))
AEE_EXEC_MAX_OUTPUT = int(os.getenv("AEE_EXEC_MAX_OUTPUT", "16000"))
AEE_MCP_ALLOWED_ROOTS: List[str] = [
    p.strip()
    for p in os.getenv("AEE_MCP_ALLOWED_ROOTS", f"{Path.home()},/tmp").split(",")
    if p.strip()
]

# Bearer key required unless AEE_MCP_REQUIRE_AUTH is explicitly false.
AEE_MCP_REQUIRE_AUTH = os.getenv("AEE_MCP_REQUIRE_AUTH", "true").strip().lower() not in {
    "0", "false", "no", "off",
}

# Version reported by the gateway itself.
AEE_GATEWAY_VERSION = "0.2.0-p2c-candidate"
# Product version from the canonical package (aee/__init__.py).
try:
    from aee import __version__ as AEE_PRODUCT_VERSION  # type: ignore
except Exception:
    AEE_PRODUCT_VERSION = "unknown"

RESTRICTED_TOOLS = frozenset({"aee_status", "aee_agents", "aee_dispatch",
                              "aee_job_status", "aee_job_result"})
AEE_MCP_SURFACE = os.getenv("AEE_MCP_SURFACE", "restricted" if AEE_MCP_PORT == 8791 else "local")
if AEE_MCP_SURFACE not in {"local", "restricted"}:
    raise ValueError("Invalid MCP surface configuration")
if AEE_MCP_PORT == 8791 and AEE_MCP_SURFACE != "restricted":
    raise ValueError("Port 8791 requires the restricted MCP surface")
AEE_MCP_EXPOSED_TOOLS = [t.strip() for t in os.getenv("AEE_MCP_EXPOSED_TOOLS", "").split(",") if t.strip()]
if AEE_MCP_SURFACE == "restricted":
    configured_tools = os.getenv("AEE_MCP_EXPOSED_TOOLS")
    if configured_tools is not None and set(AEE_MCP_EXPOSED_TOOLS) != RESTRICTED_TOOLS:
        raise ValueError("Restricted tool allowlist must equal the five-tool contract")
    if not AEE_MCP_REQUIRE_AUTH:
        raise ValueError("Restricted MCP requires bearer authentication")

# ---------------------------------------------------------------------------
# A3 bootstrap-dispatch configuration
# ---------------------------------------------------------------------------

# Job store directory (filesystem persistence; created with 0700).
# Kept OUTSIDE the canonical repo (state must not pollute git status and
# the live A3 store must never be opened by P2B fixtures).
A3_JOB_STORE_DIR = Path(os.getenv(
    "A3_JOB_STORE_DIR", str(Path.home() / ".local/state/aee-p2b-jobs")
))

# Dispatch working-directory allowlist. Bootstrap: the canonical AEE repo
# ONLY (work order A3 §Working Directory Security). Comma-separated roots.
A3_DISPATCH_ALLOWED_ROOTS: List[str] = [
    p.strip()
    for p in os.getenv(
        "A3_DISPATCH_ALLOWED_ROOTS", str(Path(__file__).resolve().parent)
    ).split(",")
    if p.strip()
]

# Directories that are NEVER valid as (or inside) a working directory,
# even if they live under an allowed root.
A3_DISPATCH_FORBIDDEN_PATH_RE = re.compile(
    r"(?i)(/\.git(/|$)|/\.ssh(/|$)|/\.hermes(/|$)|/\.aws(/|$)|/\.gnupg(/|$)"
    r"|/\.cache/claude|/\.codex/sessions|/\.config/chromium|credential)"
)

# Codex job hard limits.
A3_CODEX_BIN = os.getenv("A3_CODEX_BIN", "codex")
A3_CODEX_TIMEOUT_SEC = int(os.getenv("A3_CODEX_TIMEOUT_SEC", "900"))     # 15 min
A3_CODEX_MAX_SUMMARY = int(os.getenv("A3_CODEX_MAX_SUMMARY", "4000"))    # chars
A3_CODEX_MAX_LOG = int(os.getenv("A3_CODEX_MAX_LOG", "2000"))           # chars
A3_CODEX_MAX_OUTPUT_FILE = int(os.getenv("A3_CODEX_MAX_OUTPUT_FILE", "65536"))

# Bootstrap job contract: read-only Codex only.
A3_ALLOWED_AGENTS = {"codex"}
A3_ALLOWED_MODES = {"read_only"}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("aee-a3-mcp-gateway")

_START_TIME = time.time()

# ---------------------------------------------------------------------------
# Security helpers — redaction
# ---------------------------------------------------------------------------

# Credential-shaped values are redacted from every tool output.
_SECRET_VALUE_RE = re.compile(
    r"("
    r"sk-[A-Za-z0-9_\-]{8,}"                       # OpenAI-style keys
    r"|gh[pousr]_[A-Za-z0-9]{20,}"                 # GitHub tokens
    r"|xox[baprs]-[A-Za-z0-9\-]+"                  # Slack tokens
    r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"  # JWTs
    r")"
)

_SECRET_ENV_NAMES = re.compile(
    r"(?i)^(?:.*API_KEY|.*TOKEN|.*SECRET|.*PASSWORD|.*CREDENTIAL.*|"
    r"API_SERVER_KEY|ANTHROPIC_AUTH_TOKEN)$"
)


def redact(text: str) -> str:
    """Best-effort redaction of credential-shaped values."""
    if not text:
        return text
    return _SECRET_VALUE_RE.sub("[REDACTED]", text)


def redact_obj(obj: Any) -> Any:
    """Recursively redact strings inside dicts/lists; redact by key name."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and _SECRET_ENV_NAMES.match(k):
                out[k] = "[REDACTED]"
            else:
                out[k] = redact_obj(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_obj(i) for i in obj]
    return obj


def path_allowed(path: str) -> bool:
    """Check an absolute path against the allowed roots."""
    if not path:
        return True
    p = Path(os.path.normpath(path))
    for root in AEE_MCP_ALLOWED_ROOTS:
        try:
            p.relative_to(Path(root))
            return True
        except ValueError:
            continue
    return False


# ---------------------------------------------------------------------------
# System status helpers (read-only, /proc + coreutils)
# ---------------------------------------------------------------------------


def _read_proc(path: str) -> Optional[str]:
    try:
        return Path(path).read_text().strip()
    except Exception:
        return None


def _cpu_load() -> Dict[str, Any]:
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        return {
            "load_1m": float(parts[0]),
            "load_5m": float(parts[1]),
            "load_15m": float(parts[2]),
        }
    except Exception:
        return {}


def _mem() -> Dict[str, Any]:
    try:
        info: Dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, val = line.partition(":")
            info[key] = int(val.strip().split()[0]) * 1024  # bytes
        total = info.get("MemTotal", 0)
        available = info.get("MemAvailable", 0)
        return {
            "total_bytes": total,
            "available_bytes": available,
            "used_bytes": total - available,
            "percent_used": round(100 * (total - available) / total, 1) if total else None,
        }
    except Exception:
        return {}


def _disks() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        df = subprocess.run(
            ["df", "-B1", "--output=source,size,used,avail,pcent,target", "/", "/home"],
            capture_output=True, text=True, timeout=5,
        )
        seen = set()
        for line in df.stdout.strip().splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 6:
                src, size, used, avail, pcent, target = parts
                if src in seen:
                    continue
                seen.add(src)
                out.append({
                    "filesystem": src,
                    "mount": target,
                    "total_bytes": int(size),
                    "used_bytes": int(used),
                    "available_bytes": int(avail),
                    "percent_used": pcent.rstrip("%"),
                })
    except Exception:
        pass
    return out


def _uptime_seconds() -> Optional[float]:
    s = _read_proc("/proc/uptime")
    if s:
        try:
            return float(s.split()[0])
        except Exception:
            return None
    return None


def _host_boot_time() -> Optional[str]:
    up = _uptime_seconds()
    if up is None:
        return None
    return datetime.fromtimestamp(time.time() - up, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Agent discovery (read-only; never mutates agent config)
# ---------------------------------------------------------------------------

# Executable search order per agent: PATH first, then known mise/pipx layout.
_AGENT_COMMANDS: Dict[str, List[str]] = {
    "hermes": [
        "hermes",
        str(Path.home() / ".local/share/mise/installs/pipx-hermes-agent/latest/bin/hermes"),
    ],
    "claude": [
        "claude",
        str(Path.home() / ".local/bin/claude"),
    ],
}


def discover_agent(name: str) -> Dict[str, Any]:
    """Discover one agent CLI. Read-only: which + `--version` only."""
    if name == "codex":
        try:
            identity = resolve_codex(A3_CODEX_BIN)
            return {"agent": name, "installed": True, "available": True,
                    "executable": identity.executable, "version": identity.version,
                    "executable_sha256": identity.sha256}
        except _JobError as error:
            return {"agent": name, "installed": False, "available": False,
                    "executable": None, "version": None, "error_code": error.code}
    candidates = _AGENT_COMMANDS.get(name, [])
    exe: Optional[str] = None
    for cand in candidates:
        if os.sep not in cand:
            found = shutil.which(cand)
            if found:
                exe = found
                break
        else:
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                exe = cand
                break
    if exe is None:
        return {
            "agent": name,
            "installed": False,
            "executable": None,
            "version": None,
            "available": False,
        }
    version: Optional[str] = None
    try:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True, text=True, timeout=20,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": os.environ.get("HOME", str(Path.home())),
            },
        )
        raw = (proc.stdout or proc.stderr or "").strip()
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        version = lines[0] if lines else None
        version = version[:200] if version else None
    except Exception as exc:
        version = f"error: {type(exc).__name__}"
    return {
        "agent": name,
        "installed": True,
        "executable": exe,
        "version": redact(version or ""),
        "available": True,
    }


def discover_agents() -> List[Dict[str, Any]]:
    return [discover_agent(n) for n in ("hermes", "codex", "claude")]


# ---------------------------------------------------------------------------
# Bounded execution (argv allowlist, no shell)
# ---------------------------------------------------------------------------

# First-token allowlist for aee_exec. Intentionally minimal for Phase A1 —
# read-only system & repo inspection commands only (work order §4.3).
EXEC_ALLOWLIST: set = {
    "pwd", "hostname", "uptime", "free", "df", "git", "ps", "ls", "cat",
    "head", "tail", "wc", "grep", "rg", "find", "file", "echo", "date",
    "whoami", "uname", "ip", "systemctl", "journalctl", "printenv",
    "env", "python3", "node", "npm", "uv", "docker", "jq",
    "stat", "id", "du", "sort", "diff", "which", "readlink",
}

# Arguments that are outright blocked even for allowlisted binaries
# (credential/secret file shapes).
EXEC_ARG_BLOCKLIST = re.compile(
    r"(\.hermes/\.env)|(\.ssh/)|(\.pem$)|(\.key$)|(^/etc/shadow$)|(credential)",
    re.IGNORECASE,
)


def bounded_exec(argv: List[str], cwd: Optional[str] = None) -> Dict[str, Any]:
    """Run an allowlisted argv (never via shell). Bounded by timeout, output
    cap, allowed roots, and secret redaction. Returns a structured result."""
    t0 = time.monotonic()
    if not argv:
        return {"ok": False, "error": "argv is empty"}
    binary = argv[0]
    if binary not in EXEC_ALLOWLIST:
        return {
            "ok": False,
            "error": f"binary '{binary}' is not allowlisted",
            "allowlist": sorted(EXEC_ALLOWLIST),
        }
    # Path safety: absolute-path arguments must be under an allowed root.
    for arg in argv[1:]:
        if arg.startswith("/") and not path_allowed(arg):
            return {
                "ok": False,
                "error": f"path '{arg}' is outside allowed roots {AEE_MCP_ALLOWED_ROOTS}",
            }
    # Credential-shaped arguments are blocked outright.
    for arg in argv[1:]:
        if EXEC_ARG_BLOCKLIST.search(arg):
            return {
                "ok": False,
                "error": "argument blocked (credential/secret path pattern)",
            }
    try:
        proc = subprocess.run(
            argv,
            capture_output=True, text=True, timeout=AEE_EXEC_TIMEOUT_SEC,
            cwd=cwd or None,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"timeout after {AEE_EXEC_TIMEOUT_SEC}s",
            "argv": argv,
        }
    except FileNotFoundError:
        return {"ok": False, "error": "binary not found", "argv": argv}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "argv": argv}
    stdout = redact(proc.stdout or "")[:AEE_EXEC_MAX_OUTPUT]
    stderr = redact(proc.stderr or "")[:2000]
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "argv": argv,
        "stdout": stdout,
        "stderr": stderr,
        "duration_ms": int((time.monotonic() - t0) * 1000),
    }


# ---------------------------------------------------------------------------
# A3 job store — filesystem persistence (atomic writes, lock-protected)
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


from aee.mcp_runtime.store import JobError as _JobError, JobStore as _PersistentJobStore


def _validate_working_directory(working_directory: str) -> str:
    """Resolve + validate a dispatch working_directory (A3 security gate).

    MUST be an existing directory, under an A3_DISPATCH_ALLOWED_ROOTS root,
    not inside a forbidden (.git/.ssh/credential...) path, checked on the
    REAL resolved path (symlinks resolved, traversal collapsed).
    Returns the resolved absolute path or raises _JobError.
    """
    raw = (working_directory or "").strip()
    if not raw:
        raise _JobError("INVALID_WORKING_DIRECTORY", "working_directory is required")
    if "\x00" in raw:
        raise _JobError("INVALID_WORKING_DIRECTORY", "working_directory contains NUL")
    try:
        resolved = Path(os.path.realpath(raw))
    except Exception as exc:
        raise _JobError("INVALID_WORKING_DIRECTORY", f"unresolvable path: {type(exc).__name__}")
    if not resolved.is_dir():
        raise _JobError(
            "INVALID_WORKING_DIRECTORY",
            "working_directory is not an existing directory",
        )
    resolved_s = str(resolved)
    if A3_DISPATCH_FORBIDDEN_PATH_RE.search(resolved_s):
        raise _JobError(
            "INVALID_WORKING_DIRECTORY",
            "working_directory resolves into a forbidden path "
            "(.git/.ssh/credential/config area)",
        )
    for root in A3_DISPATCH_ALLOWED_ROOTS:
        try:
            resolved.relative_to(Path(root).resolve())
            break
        except ValueError:
            continue
    else:
        raise _JobError(
            "INVALID_WORKING_DIRECTORY",
            "working_directory is outside the dispatch allowlist",
        )
    return resolved_s


class JobStore(_PersistentJobStore):
    def __init__(self, root: Path):
        super().__init__(root, redact=redact)


JOB_STORE = JobStore(A3_JOB_STORE_DIR)


# ---------------------------------------------------------------------------
# Codex executor adapter (A3.2) — argv subprocess, runtime-enforced
# read-only sandbox, minimal env, timeout + output caps
# ---------------------------------------------------------------------------


from aee.mcp_runtime.result_contract import validate_success
from aee.mcp_runtime.executor import execute_codex
from aee.mcp_runtime.process import Limits
from aee.mcp_runtime.profiles import (
    ExecutorIdentity, execution_metadata, select_profile,
)
from aee.mcp_runtime.runtime import resolve_executor as resolve_codex, deployment_policy, require_deployment_ready
from aee.mcp_runtime.broker_client import control as broker_control
from aee.mcp_runtime.sandbox import read_manifest

A3_EXECUTION_PROFILE = os.getenv("A3_EXECUTION_PROFILE", "codex-readonly-high")
A3_WORKSPACE_MANIFEST = os.getenv("A3_WORKSPACE_MANIFEST")
A3_LIMITS = Limits(timeout=A3_CODEX_TIMEOUT_SEC, summary_chars=A3_CODEX_MAX_SUMMARY,
                   log_chars=A3_CODEX_MAX_LOG, result_bytes=A3_CODEX_MAX_OUTPUT_FILE)


def _codex_executable() -> str:
    return resolve_codex(A3_CODEX_BIN).executable


def run_codex_job(job_id: str, task: str, working_directory: str, *, cancel_event=None) -> None:
    """Execute one lease-owned job. No host HOME/auth or task-in-argv fallback."""
    broker_socket = None
    broker_path = os.getenv("AEE_BROKER_CONTROL")
    worker_started = time.monotonic()
    try:
        record = JOB_STORE.get(job_id)
        execution = record.get("execution")
        if execution is None:  # internal compatibility for store-created test jobs
            profile = select_profile(A3_EXECUTION_PROFILE)
            execution = execution_metadata(profile, resolve_codex(A3_CODEX_BIN))
        configured = execution["configured"]
        profile = select_profile(configured["execution_profile"], configured["model"],
                                 configured["reasoning_effort"])
        identity = ExecutorIdentity(configured["executable"], configured["executable_sha256"],
                                    execution["observed"]["agent_version_probe"])
        policy = deployment_policy()
        if policy:
            require_deployment_ready(policy)
            observed = resolve_codex(A3_CODEX_BIN)
            if observed != identity:
                raise _JobError("EXECUTOR_IDENTITY_MISMATCH", "Executor identity changed since admission")
            if not broker_path:
                raise _JobError("BROKER_UNAVAILABLE", "Production execution requires the approved broker")
        if broker_path:
            broker_socket = broker_control(broker_path, "lease", job_id)["socket"]
            execution["isolation"]["network"] = "private-loopback-to-job-uds"
            execution["isolation"]["egress"] = "fixed-openai-responses-broker"
        JOB_STORE.update(job_id, status="running", started_at=_now_iso(), execution=execution)
        log.info("event=job_started job_id=%s profile=%s", job_id, profile.name)
        result = execute_codex(identity, profile, task, working_directory, A3_WORKSPACE_MANIFEST,
                               limits=A3_LIMITS, redact=redact, broker_socket=broker_socket,
                               broker_receipt=(lambda: broker_control(broker_path, 'receipt', job_id))
                               if policy else None, cancel_event=cancel_event)
        execution["isolation"]["snapshot"] = result.snapshot
        if result.evidence is not None:
            execution['native_tool_evidence'] = result.evidence
        if result.outcome is not None:
            execution['result_contract'] = result.outcome
        JOB_STORE.update(job_id, status="completed", finished_at=_now_iso(), exit_code=result.exit_code,
                         summary=result.summary, log_excerpt=result.log_excerpt, artifacts=[],
                         truncated=result.truncated, execution=execution)
        log.info("event=job_completed job_id=%s duration_ms=%d", job_id,
                 int((time.monotonic()-worker_started)*1000))
    except _JobError as error:
        log.warning("event=job_failed job_id=%s error_code=%s duration_ms=%d", job_id, error.code,
                    int((time.monotonic()-worker_started)*1000))
        status = {'EXECUTION_TIMEOUT': 'timed_out', 'EXECUTION_CANCELLED': 'cancelled'}.get(error.code, 'failed')
        failure_execution = locals().get('execution')
        if isinstance(failure_execution, dict) and error.failure is not None:
            failure_execution['native_tool_failure'] = error.failure
        fields = {'execution': failure_execution} if isinstance(failure_execution, dict) else {}
        JOB_STORE.update(job_id, status=status, finished_at=_now_iso(), exit_code=error.exit_code,
                         error_code=error.code, error=redact(error.message), **fields)
    except Exception:
        log.warning("event=job_failed job_id=%s error_code=EXECUTION_FAILED", job_id)
        JOB_STORE.update(job_id, status="failed", finished_at=_now_iso(),
                         error_code="EXECUTION_FAILED", error="Isolated executor failed")
    finally:
        if broker_socket is not None:
            try:
                broker_control(broker_path, "revoke", job_id)
            except _JobError:
                log.error("event=broker_revoke_failed job_id=%s", job_id)
        JOB_STORE.release(job_id)
        _dispatch_pool.pop(job_id, None)


_dispatch_pool: Dict[str, threading.Thread] = {}


def _start_job_worker(job_id: str, task: str, working_directory: str) -> None:
    """Spawn the executor thread; single-flight by design (bootstrap)."""
    t = threading.Thread(
        target=run_codex_job,
        args=(job_id, task, working_directory),
        name=f"aee-job-{job_id}",
        daemon=True,
    )
    _dispatch_pool[job_id] = t
    try:
        t.start()
    except Exception:
        _dispatch_pool.pop(job_id, None)
        raise


def dispatch_job(
    agent: str, task: str, working_directory: str, mode: str,
    execution_profile: Optional[str] = None, model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
) -> Dict[str, Any]:
    """A3 dispatch entrypoint — validate, persist, spawn, return immediately."""
    agent_clean = (agent or "").strip().lower()
    if agent_clean not in A3_ALLOWED_AGENTS:
        raise _JobError(
            "INVALID_AGENT",
            f"agent '{redact(agent_clean[:60])}' is not supported in the A3 "
            "bootstrap contract; expected 'codex'",
        )
    mode_clean = (mode or "").strip().lower()
    if mode_clean not in A3_ALLOWED_MODES:
        raise _JobError(
            "INVALID_MODE",
            f"mode '{redact(mode_clean[:60])}' is not supported in the A3 "
            "bootstrap contract; expected 'read_only'",
        )
    if not task or not task.strip():
        raise _JobError("INVALID_TASK", "task must not be empty")
    workdir = _validate_working_directory(working_directory)
    profile = select_profile(A3_EXECUTION_PROFILE if execution_profile is None else execution_profile,
                             model, reasoning_effort)
    read_manifest(A3_WORKSPACE_MANIFEST)
    policy = deployment_policy()
    if policy:
        require_deployment_ready(policy)
    identity = resolve_codex(A3_CODEX_BIN)
    execution = execution_metadata(profile, identity, requested_profile=execution_profile,
                                   requested_model=model, requested_reasoning=reasoning_effort)
    data = JOB_STORE.create(agent_clean, task, workdir, mode_clean, execution=execution)
    try:
        _start_job_worker(data["job_id"], task, workdir)
    except Exception:
        JOB_STORE.update(data["job_id"], status="failed", finished_at=_now_iso(),
                         error_code="EXECUTION_FAILED", error="Worker could not start")
        raise _JobError("EXECUTION_FAILED", "Worker could not start") from None
    return data


def job_error_payload(exc: _JobError) -> Dict[str, Any]:
    return {
        "ok": False,
        "error_code": exc.code,
        "error": exc.message,
    }

# ---------------------------------------------------------------------------
# MCP server (FastMCP — streamable HTTP transport)
# ---------------------------------------------------------------------------

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "aee-a3-gateway",
    host=AEE_MCP_HOST,
    port=AEE_MCP_PORT,
    streamable_http_path="/mcp",
    # stateless_http keeps each request self-contained — friendlier for the
    # future ChatGPT remote connector (no sticky session affinity needed).
    stateless_http=True,
    json_response=True,
)


@mcp.custom_route("/health", methods=["GET"])
async def health_route(request):
    """Liveness probe — plain HTTP GET, no MCP session, no auth (facts only)."""
    from starlette.responses import JSONResponse

    payload = {
        "ok": True,
        "status": "healthy",
        "gateway": "aee-a3-mcp-gateway",
        "version": AEE_GATEWAY_VERSION,
        "aee_product_version": AEE_PRODUCT_VERSION,
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "time": _now_iso(),
    }
    policy = deployment_policy()
    if policy:
        payload["runtime"] = {"source_commit": policy["source_commit"],
                              "codex_version": policy["codex"]["version"]}
    return JSONResponse(payload)


@mcp.custom_route("/status", methods=["GET"])
async def status_route(request):
    """Status surface for systemd & monitoring (facts only, no secrets)."""
    from starlette.responses import JSONResponse

    payload = {
        "ok": True,
        "gateway": "aee-a3-mcp-gateway",
        "version": AEE_GATEWAY_VERSION,
        "aee_product_version": AEE_PRODUCT_VERSION,
        "hostname": socket.gethostname(),
        "bind": f"{AEE_MCP_HOST}:{AEE_MCP_PORT}",
        "gateway_uptime_seconds": round(time.time() - _START_TIME, 1),
        "host_uptime_seconds": _uptime_seconds(),
        "auth_required": AEE_MCP_REQUIRE_AUTH,
        "allowed_roots": AEE_MCP_ALLOWED_ROOTS,
    }
    return JSONResponse(payload)


# --- tools ------------------------------------------------------------------


@mcp.tool()
async def aee_status() -> str:
    """Get the current live system status of the Omarchy-A3 AEE runtime node.

    Returns hostname, kernel, uptime, CPU load, memory, disk, and AEE
    gateway/product version info, read live from the real Omarchy-A3 host.
    Read-only.
    """
    payload = {
        "aee_gateway_version": AEE_GATEWAY_VERSION,
        "aee_product_version": AEE_PRODUCT_VERSION,
        "node": {
            "hostname": socket.gethostname(),
            "os": "Omarchy (Arch-based)",
            "kernel": _read_proc("/proc/sys/kernel/osrelease"),
            "boot_time": _host_boot_time(),
            "uptime_seconds": _uptime_seconds(),
            "cpu": _cpu_load(),
            "memory": _mem(),
            "disks": _disks(),
        },
        "gateway": {
            "bind": f"{AEE_MCP_HOST}:{AEE_MCP_PORT}",
            "gateway_started_at": datetime.fromtimestamp(
                _START_TIME, tz=timezone.utc
            ).isoformat(),
            "gateway_uptime_seconds": round(time.time() - _START_TIME, 1),
            "localhost_only": AEE_MCP_HOST == "127.0.0.1",
            "auth_required": AEE_MCP_REQUIRE_AUTH,
        },
    }
    return json.dumps(redact_obj(payload), ensure_ascii=False)


@mcp.tool()
async def aee_agents() -> str:
    """List AI agent runtimes installed and available on Omarchy-A3.

    Reports hermes, codex, and claude with installed / executable path /
    version / available. Read-only discovery — this tool never modifies
    agent configuration.
    """
    agents = await asyncio.to_thread(discover_agents)
    return json.dumps({"agents": redact_obj(agents)}, ensure_ascii=False)


@mcp.tool()
async def aee_exec(command: str, cwd: Optional[str] = None) -> str:
    """Run a bounded, allowlisted command on this node (no shell).

    Only read-only inspection commands are allowed (pwd, hostname, uptime,
    free, df, git status/log, ps, ls, etc.). Absolute-path arguments must
    stay under the allowed roots (configured home and /tmp). Output is capped
    and secret-redacted. Arbitrary or root-shell execution is not provided.
    """
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return json.dumps({"ok": False, "error": f"unparseable command: {exc}"})
    if cwd and not path_allowed(cwd):
        return json.dumps({
            "ok": False,
            "error": f"cwd '{cwd}' is outside allowed roots {AEE_MCP_ALLOWED_ROOTS}",
        })
    result = await asyncio.to_thread(bounded_exec, argv, cwd)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def aee_dispatch(
    agent: str,
    working_directory: str,
    task: Optional[str] = None,
    mode: str = "read_only",
    prompt: Optional[str] = None,
    metadata: Optional[dict] = None,
    execution_profile: Optional[str] = None,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
) -> str:
    """Dispatch a controlled, read-only job to the Codex agent on this node.

    Bootstrap contract (A3): agent must be 'codex' and mode must be
    'read_only'; every other agent or mode is rejected. working_directory
    must be an existing directory under the dispatch allowlist. Execution
    reads a reviewed source snapshot; host source files cannot be modified.
    Model/reasoning selectors must match the named execution profile.
    Returns immediately with a job_id; poll aee_job_status / aee_job_result.
    """
    # Back-compat: A1 callers passed `prompt` + optional `metadata`.
    task = task or prompt or ""
    try:
        data = await asyncio.to_thread(
            dispatch_job, agent, task, working_directory, mode, execution_profile, model, reasoning_effort
        )
        return json.dumps(
            {
                "ok": True,
                "job_id": data["job_id"],
                "status": data["status"],
                "agent": data["agent"],
                "mode": data["mode"],
                "working_directory": data["working_directory"],
                "created_at": data["created_at"],
            },
            ensure_ascii=False,
        )
    except _JobError as exc:
        return json.dumps(job_error_payload(exc), ensure_ascii=False)


@mcp.tool()
async def aee_job_status(job_id: str) -> str:
    """Get the lifecycle status of a dispatched AEE job.

    Returns job_id, agent, status (queued/running/completed/failed/timed_out/cancelled),
    created_at, started_at, finished_at. Does not return job logs.
    """
    try:
        data = await asyncio.to_thread(JOB_STORE.get, job_id)
    except _JobError as exc:
        return json.dumps(job_error_payload(exc), ensure_ascii=False)
    return json.dumps(
        {
            "ok": True,
            "job_id": data["job_id"],
            "agent": data["agent"],
            "status": data["status"],
            "created_at": data["created_at"],
            "started_at": data["started_at"],
            "finished_at": data["finished_at"],
            "error_code": data.get("error_code"),
        },
        ensure_ascii=False,
    )


@mcp.tool()
async def aee_job_result(job_id: str) -> str:
    """Get the structured result of a COMPLETED dispatched AEE job.

    Returns agent, status, summary (bounded), exit_code, timestamps,
    artifacts, and a short redacted log excerpt. Full logs stay on the
    node. Returns JOB_NOT_COMPLETE while the job is queued/running.
    """
    try:
        data = await asyncio.to_thread(JOB_STORE.get, job_id)
    except _JobError as exc:
        return json.dumps(job_error_payload(exc), ensure_ascii=False)
    if data["status"] not in ("completed", "failed", "timed_out", "cancelled"):
        return json.dumps(
            {
                "ok": False,
                "error_code": "JOB_NOT_COMPLETE",
                "error": f"job is {data['status']}; poll aee_job_status",
                "job_id": data["job_id"],
                "status": data["status"],
            },
            ensure_ascii=False,
        )
    failure = None
    try:
        validate_success(data)
    except _JobError as exc:
        failure = job_error_payload(exc)
    if failure is not None and data['status'] == 'completed':
        failure.update(job_id=data['job_id'], status='completed')
        return json.dumps(redact_obj(failure), ensure_ascii=False)
    return json.dumps(
        redact_obj({
            "ok": failure is None,
            "job_id": data["job_id"],
            "agent": data["agent"],
            "status": data["status"],
            "summary": data.get("summary"),
            "exit_code": data.get("exit_code"),
            "error_code": failure["error_code"] if failure else None,
            "error": failure["error"] if failure else None,
            "started_at": data["started_at"],
            "finished_at": data["finished_at"],
            "artifacts": data.get("artifacts", []),
            "log_excerpt": data.get("log_excerpt"),
            "truncated": data.get("truncated", False),
            "execution": data.get("execution"),
        }),
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Bearer auth middleware (Starlette-level, applied to the MCP ASGI app)
# ---------------------------------------------------------------------------


def _build_auth_middleware(app):
    """Wrap the FastMCP ASGI app with a bearer-token check.

    Unauthenticated surfaces: GET /health and GET /status (facts only).
    Everything else (MCP session endpoints) requires
    `Authorization: Bearer <MCP_BRIDGE_API_KEY>`.
    """
    from starlette.responses import JSONResponse

    allowed = {AEE_MCP_API_KEY} if AEE_MCP_API_KEY else set()

    async def middleware(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        path = scope.get("path", "")
        if scope.get("method") == "GET" and (
            path in ("/health", "/status")
            # MCP DCR/PRMD convention: /.well-known/* metadata probes are
            # answered WITHOUT a token; a server that does not advertise
            # OAuth returns 404 there. Returning 401+JSON makes compliant
            # clients (e.g. tunnel-client doctor) misread it as invalid
            # metadata. GET-only, non-MCP surface: nothing is revealed.
            or path.startswith("/.well-known/")
        ):
            await app(scope, receive, send)
            return
        if not AEE_MCP_REQUIRE_AUTH:
            await app(scope, receive, send)
            return
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if token in allowed:
            await app(scope, receive, send)
            return
        resp = JSONResponse(
            {"ok": False, "error": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await resp(scope, receive, send)

    return middleware


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def _apply_tool_exposure_filter() -> None:
    """Filter both listing and the manager call path, including future tools."""
    from mcp.server.fastmcp.exceptions import ToolError

    allowed = RESTRICTED_TOOLS if AEE_MCP_SURFACE == "restricted" else frozenset(AEE_MCP_EXPOSED_TOOLS)
    if not allowed:
        return  # deliberately full/local only; restricted always has fixed five
    manager = mcp._tool_manager
    if getattr(manager, "_aee_surface_guard", False):
        return
    original_list = manager.list_tools
    original_call = manager.call_tool
    for tool in original_list():
        if tool.name not in allowed:
            manager.remove_tool(tool.name)

    def guarded_list():
        return [tool for tool in original_list() if tool.name in allowed]

    async def guarded_call(name, arguments, **kwargs):
        if name not in allowed:
            raise ToolError("Tool is not exposed in this gateway")
        return await original_call(name, arguments, **kwargs)

    manager.list_tools = guarded_list
    manager.call_tool = guarded_call
    manager._aee_surface_guard = True


def main() -> None:
    policy = deployment_policy()
    if policy:
        require_deployment_ready(policy)
        resolve_codex(A3_CODEX_BIN)
        read_manifest(A3_WORKSPACE_MANIFEST)
        if not os.getenv("AEE_BROKER_CONTROL") or AEE_MCP_HOST != "127.0.0.1":
            raise _JobError("RUNTIME_POLICY_INVALID", "Production broker and loopback binding are required")
    if AEE_MCP_REQUIRE_AUTH and not AEE_MCP_API_KEY:
        print(
            "FATAL: MCP_BRIDGE_API_KEY is not configured; refusing to start "
            "without auth. Set it in the service Environment file, or set "
            "AEE_MCP_REQUIRE_AUTH=false explicitly for local testing.",
            file=sys.stderr,
        )
        sys.exit(2)
    log.info(
        "AEE A3 MCP Gateway starting on %s:%s (auth=%s, allowed_roots=%s)",
        AEE_MCP_HOST,
        AEE_MCP_PORT,
        "required" if AEE_MCP_REQUIRE_AUTH else "disabled",
        AEE_MCP_ALLOWED_ROOTS,
    )
    import uvicorn

    _apply_tool_exposure_filter()

    # Build the streamable-HTTP ASGI app, wrap it with bearer auth, serve it.
    mcp_app = mcp.streamable_http_app()
    app = _build_auth_middleware(mcp_app)

    class _Server(uvicorn.Server):
        """Uvicorn server with graceful shutdown on SIGTERM/SIGINT."""

        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if self.started:
                from aee.mcp_runtime.readiness import notify
                notify("READY=1")

        def install_signal_handlers(self) -> None:
            import signal

            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    asyncio.get_event_loop().add_signal_handler(
                        sig, self.handle_exit, sig, None
                    )
                except NotImplementedError:
                    signal.signal(sig, self.handle_exit)

    config = uvicorn.Config(
        app,
        host=AEE_MCP_HOST,
        port=AEE_MCP_PORT,
        log_level="info",
        # Single worker; in-memory job registry is not multi-worker safe.
    )
    server = _Server(config)
    log.info(
        "AEE A3 MCP Gateway binding: http://%s:%s/mcp (health: /health, status: /status)",
        AEE_MCP_HOST,
        AEE_MCP_PORT,
    )
    server.run()


if __name__ == "__main__":
    main()
