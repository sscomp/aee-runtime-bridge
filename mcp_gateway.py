#!/usr/bin/env python3
"""AEE v2 MCP gateway: restricted five-tool surface and bounded Codex jobs.

Production uses an approved deployment manifest, dedicated inference broker,
immutable workspace and kernel-enforced resource/sandbox policy. No deployment
approval or real inference is implied by the offline fixture test path.
"""

from __future__ import annotations

import asyncio
import grp
import hashlib
import hmac
import json
import logging
import os
import platform
import pwd
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
from typing import Any, Dict, List, Literal, Optional

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

RESTRICTED_TOOLS = frozenset({"aee_status", "aee_agents", "aee_dispatch", "aee_job_cancel",
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
    for p in os.getenv("A3_DISPATCH_ALLOWED_ROOTS", "").split(",")
    if p.strip()
]

# Codex job hard limits.
A3_CODEX_BIN = os.getenv("A3_CODEX_BIN", "codex")
A3_CODEX_TIMEOUT_SEC = int(os.getenv("A3_CODEX_TIMEOUT_SEC", "900"))     # 15 min
A3_CODEX_MAX_SUMMARY = int(os.getenv("A3_CODEX_MAX_SUMMARY", "4000"))    # chars
A3_CODEX_MAX_LOG = int(os.getenv("A3_CODEX_MAX_LOG", "2000"))           # chars
A3_CODEX_MAX_OUTPUT_FILE = int(os.getenv("A3_CODEX_MAX_OUTPUT_FILE", "65536"))

# Dispatch job contract (A2 write-capable dispatch recovery): read_only is
# the always-available default; write is an explicitly authorized mode with
# per-job approval, operator-scoped write roots and bounded resources.
# autonomous adds independent OS-issued capabilities and native process authority.
# Agents come from the generic adapter registry (multi-agent recovery work
# order: codex is an adapter, not the mandatory sole backend).
from aee.mcp_runtime.store import AGENT_NAMES as A3_ALLOWED_AGENTS, ACTIVE as ACTIVE_STATES
A3_ALLOWED_MODES = {"read_only", "write", "autonomous"}
from aee.mcp_runtime.authorization import Authority, authenticated_transport, digest as auth_digest

# Write-mode authorization (work order AEE_A2_WRITE_CAPABLE_DISPATCH_CONTRACT_RECOVERY):
# * AEE_WRITE_ALLOWED_ROOTS: comma-separated realpath roots a write-mode job's
#   working_directory MUST be inside. Unset/empty fails write dispatch closed
#   (WRITE_SCOPE_NOT_CONFIGURED) — write mode is never silently blanket-enabled.
# * AEE_WRITE_APPROVAL_TOKEN: operator-secret (0600 env file) every write job
#   must present via metadata {"approval": ...}. Unset fails write dispatch
#   closed (WRITE_MODE_NOT_CONFIGURED).
_WRITE_ROOTS_ENV = os.getenv("AEE_WRITE_ALLOWED_ROOTS", "")
WRITE_ALLOWED_ROOTS: List[str] = [
    p for p in (Path(r.strip()).expanduser() for r in _WRITE_ROOTS_ENV.split(",") if r.strip())
]
_WRITE_TOKEN_ENV = os.getenv("AEE_WRITE_APPROVAL_TOKEN", "").encode()
WRITE_APPROVAL_TOKEN = _WRITE_TOKEN_ENV or None

# A2 dispatch hard-denylist: never admit write-mode jobs whose working
# directory sits inside an AEE-owned operational region (source releases,
# job store, shadow runtime, gateway/tunnel credentials can never be the
# working directory of a dispatched agent).
_WRITE_FORBIDDEN_ROOTS = [
    Path.home() / "aee" / "releases",
    Path.home() / "aee" / "state",
    Path.home() / "aee" / "shadow",
    Path.home() / ".config" / "aee",
    Path.home() / ".config" / "aee-a2-tunnel",
    Path.home() / ".local" / "state" / "aee",
    Path.home() / ".local" / "state" / "aee-p2b-jobs",
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("aee-mcp-gateway")

_START_TIME = time.time()

# ---------------------------------------------------------------------------
# Security helpers — redaction
# ---------------------------------------------------------------------------

# Credential-shaped values are redacted from every tool output.
_SECRET_VALUE_RE = re.compile(
    r"("
    r"aee-aut1\.[A-Za-z0-9_-]{43}|"
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


def _os_platform() -> Dict[str, Any]:
    """OS-native platform identity (Linux + macOS; no hardcoded hosts)."""
    info: Dict[str, Any] = {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
    }
    distro: Optional[str] = None
    if info["system"] == "Darwin":
        mac = platform.mac_ver()[0]
        distro = f"macOS {mac}" if mac else "macOS"
    else:
        try:  # Linux: /etc/os-release (freedesktop API), absent on macOS
            distro = platform.freedesktop_os_release().get("PRETTY_NAME")
        except (OSError, AttributeError, ValueError):
            pass
    info["distro"] = distro
    return info


def _execution_identity() -> Dict[str, Any]:
    """OS-native execution identity of this gateway process (uid/gid)."""
    uid, euid = os.getuid(), os.geteuid()
    gid, egid = os.getgid(), os.getegid()
    username: Optional[str] = None
    try:
        username = pwd.getpwuid(euid).pw_name
    except (KeyError, OSError):
        pass
    groups: List[str] = []
    try:
        gids = os.getgroups()
    except (OSError, KeyError):
        gids = [egid]
    for _gid in gids:
        try:
            groups.append(grp.getgrgid(_gid).gr_name)
        except (KeyError, OSError):
            groups.append(str(_gid))
    return {
        "username": username, "uid": uid, "gid": gid,
        "effective_uid": euid, "effective_gid": egid, "groups": sorted(groups),
    }


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
    "pi": [
        "pi",
        str(Path.home() / ".local/bin/pi"),
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
    """Read-only discovery for the aee_agents surface (multi-agent recovery).

    Every adapter-registry agent is listed — installed binaries with
    path/version, absent ones with installed=False — plus the adapter's
    declared read_only enforcement semantics, so a caller can see WHY an
    agent cannot be dispatched (ADAPTER_UNSUPPORTED shows openly).
    The listing follows the adapter registry, not a hardcoded roster:
    agents registered (or removed) in job_adapters appear/disappear here
    automatically.
    """
    agents = [discover_agent(n) for n in sorted(adapter_specs())]
    for entry in agents:
        spec = adapter_specs().get(entry.get("agent"))
        if spec is not None:
            entry["read_only_enforcement"] = spec.read_only_enforcement
            entry["enforcement_note"] = spec.enforcement_note
            capability = autonomous_capability(spec.name)
            entry.update(capability)
            entry["dispatchable"] = bool(entry.get('installed')) and spec.read_only_enforcement != "UNSUPPORTED"
            entry["dispatch_modes"] = (["read_only"] if entry["dispatchable"] else [])
            if entry.get('installed') and spec.write_enforcement != "UNSUPPORTED":
                entry["dispatch_modes"].append("write")
            if capability["autonomous_supported"]:
                entry["dispatch_modes"].append("autonomous")
            entry["dispatchable"] = bool(entry["dispatch_modes"])
            entry["autonomous_enabled"] = False
            try:
                policy = Authority.configured().policy()
                entry["autonomous_enabled"] = capability["autonomous_supported"] and spec.name in policy['agents']
            except _JobError:
                pass
    return agents


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


from aee.mcp_runtime.store import (JobError as _JobError, JobStore as _PersistentJobStore,
                                   compact_adapter_terminal)


def _validate_working_directory(working_directory: str, mode: str = "read_only") -> str:
    """Resolve + validate a dispatch working_directory (OS-identity model).

    Per the operator's A2 recovery permission model: the working directory
    is a job parameter, not an authorization boundary. The executor runs
    under the gateway process's own OS user (no sudo/setuid/escalation),
    so access is decided by that identity's actual OS permissions. Basic
    filesystem validation only: the path must exist, be a directory,
    resolve symlinks normally, and be enterable/readable as the executor
    user. An optional deployment-level confinement still applies when the
    operator configures A3_DISPATCH_ALLOWED_ROOTS; by default no path
    roots are imposed. No privilege path is ever created for the job.
    Returns the resolved absolute path or raises _JobError.
    """
    if not isinstance(working_directory, str):
        raise _JobError("INVALID_WORKING_DIRECTORY", "Expected a directory path string")
    raw = working_directory if mode == "autonomous" else (working_directory or "").strip()
    if mode == 'autonomous' and not os.path.isabs(raw):
        raise _JobError("INVALID_WORKING_DIRECTORY", "Autonomous cwd must be absolute")
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
    if mode != "autonomous" and A3_DISPATCH_ALLOWED_ROOTS:
        for root in A3_DISPATCH_ALLOWED_ROOTS:
            try:
                resolved.relative_to(Path(root).resolve())
                break
            except ValueError:
                continue
        else:
            raise _JobError(
                "INVALID_WORKING_DIRECTORY",
                "working_directory is outside the configured dispatch allowlist",
            )
    if not (os.access(resolved, os.R_OK) and os.access(resolved, os.X_OK)):
        raise _JobError(
            "INVALID_WORKING_DIRECTORY",
            "working_directory is not enterable/readable as the executor OS user",
        )
    return resolved_s


def _under_root(path: Path, root: Path) -> Optional[str]:
    """Return the root a path sits under, else None (symlink-resolved compare)."""
    try:
        path.relative_to(root.resolve())
        return str(root.resolve())
    except (ValueError, OSError):
        return None


def _validate_write_authorization(mode: str, metadata: object,
                                  resolved_workdir: str) -> Optional[Dict[str, Any]]:
    """Fail-closed write-mode gates; returns the per-job authorization proof.

    Requirements (work order AEE_A2_WRITE_CAPABLE_DISPATCH_CONTRACT_RECOVERY):
    operator approval secret presented per job, an operator-configured write
    root scope, and exclusion of every AEE-owned operational region. read_only
    jobs pass through untouched (returns None).
    """
    if mode != "write":
        return None
    resolved = Path(resolved_workdir)
    if WRITE_APPROVAL_TOKEN is None:
        raise _JobError(
            "WRITE_MODE_NOT_CONFIGURED",
            "mode 'write' requires the operator-configured AEE_WRITE_APPROVAL_TOKEN "
            "(0600 env); write dispatch is not enabled",
        )
    approval = ""
    if isinstance(metadata, dict):
        value = metadata.get("approval")
        approval = value if isinstance(value, str) else ""
    if not approval:
        raise _JobError(
            "WRITE_APPROVAL_INVALID",
            'mode \'write\' requires per-job operator approval: pass metadata '
            '{"approval": "<operator-issued token>"}',
        )
    if not hmac.compare_digest(approval.encode("utf-8"), WRITE_APPROVAL_TOKEN):
        raise _JobError(
            "WRITE_APPROVAL_INVALID",
            "the supplied metadata.approval does not match the operator-approved "
            "token; write dispatch refused",
        )
    if not WRITE_ALLOWED_ROOTS:
        raise _JobError(
            "WRITE_SCOPE_NOT_CONFIGURED",
            "mode 'write' requires operator-configured AEE_WRITE_ALLOWED_ROOTS; "
            "no write scope is enabled on this host",
        )
    write_root = None
    for root in WRITE_ALLOWED_ROOTS:
        write_root = _under_root(resolved, root)
        if write_root:
            break
    if not write_root:
        raise _JobError(
            "INVALID_WRITE_SCOPE",
            f"write-mode working_directory '{redact(resolved_workdir[:200])}' is "
            f"outside the operator-configured write roots {sorted(map(str, WRITE_ALLOWED_ROOTS))}",
        )
    for forbidden in _WRITE_FORBIDDEN_ROOTS:
        if _under_root(resolved, forbidden) is not None:
            raise _JobError(
                "INVALID_WRITE_SCOPE",
                "write-mode working_directory may not sit inside an AEE-owned "
                f"operational region ({forbidden})",
            )
    return {
        "mode": "write",
        "approval_fingerprint": hashlib.sha256(approval.encode("utf-8")).hexdigest()[:16],
        "write_root": write_root,
        "provenance": redact_obj({
            key: value for key, value in (metadata.items() if isinstance(metadata, dict) else [])
            if key != "approval" and isinstance(key, str)
        }),
    }


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
from aee.mcp_runtime.budget import ExecutionBudget, BudgetPolicy
from aee.mcp_runtime.spool import SpoolStore
from aee.mcp_runtime.profiles import (
    ExecutorIdentity, execution_metadata, select_profile,
)
from aee.mcp_runtime.runtime import resolve_executor as resolve_codex, deployment_policy, require_deployment_ready, deployment_gate_approved
from aee.mcp_runtime.job_adapters import (
    adapter_execution_metadata, adapter_identity, adapter_specs, refuse_unsupported,
    resolve_adapter, execute_adapter_job, autonomous_capability,
)
from aee.mcp_runtime.broker_client import control as broker_control
from aee.mcp_runtime.sandbox import read_manifest

A3_EXECUTION_PROFILE = os.getenv("A3_EXECUTION_PROFILE", "codex-readonly-high")
A3_WORKSPACE_MANIFEST = os.getenv("A3_WORKSPACE_MANIFEST")
A3_LIMITS = Limits(timeout=min(A3_CODEX_TIMEOUT_SEC, 900), summary_chars=A3_CODEX_MAX_SUMMARY,
                   log_chars=A3_CODEX_MAX_LOG, result_bytes=A3_CODEX_MAX_OUTPUT_FILE)
# Snapshot operator configuration once. Legacy values are migration fallbacks only
# when explicitly set; the old 900s default is not imposed on generic adapters.
_LEGACY_BUDGET = {}
for _env, _field, _value in (
    ('A3_CODEX_TIMEOUT_SEC', 'wall_timeout_seconds', A3_CODEX_TIMEOUT_SEC),
    ('A3_CODEX_MAX_LOG', 'log_excerpt_bytes', A3_CODEX_MAX_LOG),
    ('A3_CODEX_MAX_OUTPUT_FILE', 'result_inline_bytes', A3_CODEX_MAX_OUTPUT_FILE)):
    if _env in os.environ:
        _LEGACY_BUDGET[_field] = _value
EXECUTION_POLICY = BudgetPolicy(os.getenv('AEE_EXECUTION_POLICY'), legacy=_LEGACY_BUDGET)



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
        if isinstance(failure_execution, dict) and isinstance(error.context, dict):
            # R5-R2 additive output-limit boundary diagnostics; bounded
            # scalars only, and never present on completed records.
            failure_execution['output_limit_diagnostics'] = error.context
        fields = {'execution': failure_execution} if isinstance(failure_execution, dict) else {}
        JOB_STORE.update(job_id, status=status, finished_at=_now_iso(), exit_code=error.exit_code,
                         error_code=error.code, error=redact(error.message), **fields)
    except Exception as exc:
        # D1 minimum exception instrumentation: journal-only bounded class name
        # and sanitized errno. Never logs str(exc), repr(exc), or any payload.
        safe_type = type(exc).__name__[:64]
        if not (safe_type.isascii() and safe_type.isidentifier()):
            safe_type = "Unknown"
        try:
            errno_value = getattr(exc, "errno", None)
        except Exception:
            errno_value = None  # diagnostics must not raise; keep fallback fixed
        safe_errno = errno_value if (
            type(errno_value) is int and 0 <= errno_value <= 65535
        ) else "none"
        log.warning(
            "event=job_failed job_id=%s error_code=EXECUTION_FAILED exc_type=%s"
            " exc_errno=%s", job_id, safe_type, safe_errno,
        )
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
        _cancel_events.pop(job_id, None)


def run_agent_job(job_id: str, task: str, working_directory: str, *, cancel_event=None) -> None:
    """Execute one adapter-routed job (multi-agent recovery contract).

    Same worker lifecycle as run_codex_job — persist running, bounded
    execution, terminal update with adapter proof, redacted bounded
    failure mapping — but the executor comes from the job_adapters
    registry instead of the approved codex/broker contract.
    """
    worker_started = time.monotonic()
    execution = None
    spool_store = spool = None
    try:
        record = JOB_STORE.get(job_id)
        execution = record.get("execution")
        if not isinstance(execution, dict) or not isinstance(execution.get("adapter"), dict):
            raise _JobError("EXECUTION_FAILED", "Job record lacks adapter execution proof")
        agent_name = execution["adapter"].get("adapter") or record.get("agent")
        mode = record.get("mode") or "read_only"
        spec, executable = resolve_adapter(agent_name)
        if mode == "autonomous":
            Authority.configured().check_start(execution.get('authorization'), spec.name, task, working_directory)
        elif mode == "write":
            if spec.write_enforcement == "UNSUPPORTED":
                raise _JobError("ADAPTER_UNSUPPORTED",
                                f"agent '{spec.name}' cannot enforce write mode on this host")
        elif spec.read_only_enforcement == "UNSUPPORTED":
            raise _JobError("ADAPTER_UNSUPPORTED",
                            f"agent '{spec.name}' cannot enforce read_only on this host")
        snapshot = execution.get('budget') or {
            'configured': EXECUTION_POLICY.for_agent(spec.name).record(), 'revision': EXECUTION_POLICY.revision}
        budget = ExecutionBudget(**snapshot['configured'])
        spool_store = SpoolStore(JOB_STORE._root / 'spool')
        spool_store.reconcile(budget.retention_seconds)
        spool = spool_store.create(job_id, budget, snapshot['revision'])
        execution['streaming'] = spool.metadata('running')
        JOB_STORE.update(job_id, status="running", started_at=_now_iso(), execution=execution)
        log.info("event=job_started job_id=%s adapter=%s", job_id, spec.name)
        outcome = execute_adapter_job(spec, executable, execution, task, working_directory,
                                      limits=A3_LIMITS, cancel_event=cancel_event,
                                      budget=budget, spool=spool, redact=redact, mode=mode)
        execution["streaming"] = outcome["streaming"]
        execution["adapter_proof"] = {
            "adapter": spec.name,
            "mode": mode,
            "read_only_enforcement": spec.read_only_enforcement,
            "enforcement_note": spec.enforcement_note,
            "write_enforcement": spec.write_enforcement,
            "write_note": spec.write_note,
            "authorization": execution.get("authorization"),
            "exit_code": outcome["exit_code"],
            "turn_completed": True,
            "working_directory": working_directory,
            "observed": {"version": execution["adapter"].get("version"),
                         "executable_sha256": execution["adapter"].get("executable_sha256")},
        }
        # Mirror the codex contract's truncation: clip the final summary to
        # the configured cap and record truncation when it was exceeded.
        summary = outcome["summary"]
        clipped = len(summary) > A3_LIMITS.summary_chars
        if clipped:
            summary = summary[:A3_LIMITS.summary_chars]
        fields = compact_adapter_terminal(record, {
            'status': 'completed', 'finished_at': _now_iso(), 'exit_code': outcome['exit_code'],
            'summary': summary, 'log_excerpt': outcome['log_excerpt'], 'artifacts': [],
            'truncated': clipped, 'execution': execution})
        JOB_STORE.update(job_id, **fields)
        log.info("event=job_completed job_id=%s duration_ms=%d", job_id,
                 int((time.monotonic() - worker_started) * 1000))
    except _JobError as error:
        log.warning("event=job_failed job_id=%s error_code=%s duration_ms=%d", job_id, error.code,
                    int((time.monotonic() - worker_started) * 1000))
        status = {'EXECUTION_TIMEOUT': 'timed_out', 'EXECUTION_IDLE_TIMEOUT': 'timed_out', 'EXECUTION_CANCELLED': 'cancelled'}.get(error.code, 'failed')
        if isinstance(execution, dict) and isinstance(error.context, dict):
            # R5-R2 additive output-limit boundary diagnostics, mirroring the
            # codex worker path: bounded scalars only, never on completed
            # records, retrievable through aee_job_result's execution field.
            execution['output_limit_diagnostics'] = error.context
        if spool is not None and getattr(error, 'streaming', None) is None:
            try:
                error.streaming = spool.finish(code=error.code, exit_code=error.exit_code, redact=redact)
            except (OSError, _JobError):
                error.streaming = spool.metadata('failed', code='SPOOL_IO_ERROR', redact=redact)
        if isinstance(execution, dict) and getattr(error, 'streaming', None) is not None:
            execution['streaming'] = error.streaming
        diagnostic_tail = (execution or {}).get('streaming', {}).get('streams', {}).get('stderr', {}).get('tail', '')
        fields = {'execution': execution, 'log_excerpt': diagnostic_tail} if isinstance(execution, dict) else {}
        fields.update(status=status, finished_at=_now_iso(), exit_code=error.exit_code,
                      error_code=error.code, error=redact(error.message))
        JOB_STORE.update(job_id, **compact_adapter_terminal(record, fields))
    except Exception as exc:
        safe_type = type(exc).__name__[:64]
        if not (safe_type.isascii() and safe_type.isidentifier()):
            safe_type = "Unknown"
        log.warning(
            "event=job_failed job_id=%s error_code=EXECUTION_FAILED exc_type=%s",
            job_id, safe_type,
        )
        if spool is not None and isinstance(execution, dict):
            try:
                execution['streaming'] = spool.finish(code='EXECUTION_FAILED', redact=redact)
            except (OSError, _JobError):
                execution['streaming'] = spool.metadata('failed', code='SPOOL_IO_ERROR', redact=redact)
        fields = compact_adapter_terminal(record, {
            'status': 'failed', 'finished_at': _now_iso(), 'error_code': 'EXECUTION_FAILED',
            'error': 'Isolated executor failed', 'execution': execution})
        JOB_STORE.update(job_id, **fields)
    finally:
        if spool is not None:
            spool.close()
        if spool_store is not None:
            spool_store.close()
        JOB_STORE.release(job_id)
        _dispatch_pool.pop(job_id, None)
        _cancel_events.pop(job_id, None)


_dispatch_pool: Dict[str, threading.Thread] = {}
_cancel_events: Dict[str, threading.Event] = {}


def _start_job_worker(job_id: str, task: str, working_directory: str, *,
                      production_codex: bool = False) -> None:
    """Spawn the executor thread; single-flight by design (bootstrap)."""
    target = run_codex_job if production_codex else run_agent_job
    cancel_event = threading.Event()
    _cancel_events[job_id] = cancel_event
    t = threading.Thread(
        target=target,
        args=(job_id, task, working_directory),
        kwargs={"cancel_event": cancel_event},
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
    reasoning_effort: Optional[str] = None, metadata: Optional[dict] = None,
) -> Dict[str, Any]:
    """Dispatch entrypoint — validate, persist, spawn, return immediately.

    Routing: when the deployment manifest is present AND approved, a codex
    job keeps the production broker-mandated contract (read-only; unchanged).
    Every other admitted agent — or codex when Stage-2 approval is absent —
    runs through the generic adapter registry with per-adapter semantics.
    ``write`` mode adds fail-closed gates on every branch: per-job operator
    approval (metadata {"approval": ...}), an operator-configured write-root
    scope, exclusion of AEE-owned operational regions, and an auditable
    authorization proof recorded in the job record. Authentication and
    working-directory validation are unchanged.
    """
    agent_clean = (agent or "").strip().lower()
    if agent_clean not in A3_ALLOWED_AGENTS:
        raise _JobError(
            "INVALID_AGENT",
            f"agent '{redact(agent_clean[:60])}' is not supported; "
            "supported agents: " + ", ".join(sorted(A3_ALLOWED_AGENTS)),
        )
    mode_clean = (mode or "").strip().lower()
    if mode_clean not in A3_ALLOWED_MODES:
        raise _JobError(
            "INVALID_MODE",
            f"mode '{redact(mode_clean[:60])}' is not supported on this runtime; "
            "expected 'read_only', 'write' or 'autonomous'",
        )
    refuse_unsupported(agent_clean, mode_clean)
    if not task or not task.strip():
        raise _JobError("INVALID_TASK", "task must not be empty")
    if metadata is not None and not isinstance(metadata, dict):
        raise _JobError("INVALID_METADATA", "metadata must be an object when provided")
    workdir = _validate_working_directory(working_directory, mode_clean)
    authorization = _validate_write_authorization(mode_clean, metadata, workdir)
    if mode_clean == 'autonomous':
        if execution_profile is not None or model is not None or reasoning_effort is not None:
            raise _JobError('INVALID_PROFILE', 'Autonomous uses os-native profile; omit selector fields')
        if not AEE_MCP_REQUIRE_AUTH or AEE_MCP_HOST != '127.0.0.1':
            raise _JobError('AUTONOMOUS_CALLER_UNVERIFIED', 'Authenticated loopback control plane required')
        Authority.configured().consume((metadata or {}).get('autonomous_approval'),
                                       agent_clean, task, workdir, consume=False)

    policy = deployment_policy()
    approved = policy is not None and deployment_gate_approved(policy)
    # The legacy broker-mandated codex contract runs ONLY on a host whose
    # manifest is present AND approved. Policy-absent hosts run codex
    # through the generic adapter path (native sandbox); a
    # present-but-unapproved manifest keeps codex fail-closed before
    # admission rather than silently downgrading its contract. The broker
    # contract executes a reviewed read-only workspace snapshot, so write
    # mode is refused there rather than silently discarded.
    production_codex = agent_clean == "codex" and approved and mode_clean != "autonomous"
    if production_codex and mode_clean == "write":
        raise _JobError(
            "INVALID_MODE",
            "mode 'write' is not supported by the approved codex broker "
            "contract (it executes a read-only workspace snapshot); "
            "dispatch write jobs to the adapter registry instead",
        )
    if production_codex:
        profile = select_profile(A3_EXECUTION_PROFILE if execution_profile is None else execution_profile,
                                 model, reasoning_effort)
        read_manifest(A3_WORKSPACE_MANIFEST)
        require_deployment_ready(policy)
        identity = resolve_codex(A3_CODEX_BIN)
        execution = execution_metadata(profile, identity, requested_profile=execution_profile,
                                       requested_model=model, requested_reasoning=reasoning_effort)
    elif agent_clean == "codex" and policy is not None and mode_clean != "autonomous":
        raise _JobError("DEPLOYMENT_GATE_CLOSED",
                        "Codex dispatch requires the approved deployment gate on a "
                        "manifest-managed host; use an approved manifest or another adapter")
    else:
        production_codex = False
        if execution_profile is not None or model is not None or reasoning_effort is not None:
            raise _JobError("INVALID_PROFILE",
                            "execution_profile/model/reasoning_effort selectors apply only to the "
                            "codex production contract")
        # Adapter admissions resolve the installed binary now so an absent
        # CLI fails before admission instead of inside the worker.
        execution = adapter_execution_metadata(agent_clean, mode_clean) if mode_clean == "autonomous" else adapter_execution_metadata(agent_clean)
        execution['budget'] = {'configured': EXECUTION_POLICY.for_agent(agent_clean).record(),
                               'revision': EXECUTION_POLICY.revision}
    if mode_clean == "autonomous":
        if not AEE_MCP_REQUIRE_AUTH or AEE_MCP_HOST != '127.0.0.1':
            raise _JobError('AUTONOMOUS_CALLER_UNVERIFIED', 'Authenticated loopback control plane required')
        token = (metadata or {}).get('autonomous_approval')
        authorization = Authority.configured().consume(token, agent_clean, task, workdir)
        execution['native_profile'] = 'os-native'
    if authorization is not None:
        execution['authorization'] = authorization
    data = JOB_STORE.create(agent_clean, task, workdir, mode_clean, execution=execution)
    try:
        _start_job_worker(data["job_id"], task, workdir, production_codex=production_codex)
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
    "aee-mcp-gateway",
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
        "gateway": "aee-mcp-gateway",
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
        "gateway": "aee-mcp-gateway",
        "version": AEE_GATEWAY_VERSION,
        "aee_product_version": AEE_PRODUCT_VERSION,
        "hostname": socket.gethostname(),
        "bind": f"{AEE_MCP_HOST}:{AEE_MCP_PORT}",
        "gateway_uptime_seconds": round(time.time() - _START_TIME, 1),
        "host_uptime_seconds": _uptime_seconds(),
        "auth_required": AEE_MCP_REQUIRE_AUTH,
        "allowed_roots": AEE_MCP_ALLOWED_ROOTS,
        "dispatch_modes": sorted(A3_ALLOWED_MODES),
        "autonomous": {"profile": "os-native", "authorization": "single-job-capability",
                       "mandatory_directory_allowlist": False,
                       "policy_configured": bool(os.getenv('AEE_AUTONOMOUS_POLICY'))},
        "write_scope": {
            "configured": bool(WRITE_ALLOWED_ROOTS),
            "approval_configured": WRITE_APPROVAL_TOKEN is not None,
            "roots": sorted(str(root.resolve()) for root in WRITE_ALLOWED_ROOTS),
        },
    }
    return JSONResponse(payload)


# --- tools ------------------------------------------------------------------


@mcp.tool()
async def aee_status() -> str:
    """Get the current live system status of this AEE runtime node.

    Returns hostname, OS platform, kernel, uptime, CPU load, memory,
    disk, AEE gateway/product version info, and the runtime execution
    identity (OS uid/gid of this gateway process), all read live from
    this host via OS-native APIs. Read-only.
    """
    payload = {
        "aee_gateway_version": AEE_GATEWAY_VERSION,
        "aee_product_version": AEE_PRODUCT_VERSION,
        "node": {
            "hostname": socket.gethostname(),
            "os": platform.platform(),
            "platform": _os_platform(),
            "kernel": platform.release(),
            "boot_time": _host_boot_time(),
            "uptime_seconds": _uptime_seconds(),
            "cpu": _cpu_load(),
            "memory": _mem(),
            "disks": _disks(),
            "execution_identity": _execution_identity(),
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
    """List executor agent adapters registered in this gateway's
    Agent Registry.

    Each registered adapter is reported with installed / executable
    path / version / available, plus its declared read-only enforcement
    semantics. The listing follows the adapter registry — adapters
    added to or removed from it appear/disappear here automatically.
    Read-only discovery — this tool never modifies agent configuration.
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
    mode: Literal["read_only", "write", "autonomous"] = "read_only",
    prompt: Optional[str] = None,
    metadata: Optional[dict] = None,
    execution_profile: Optional[str] = None,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
) -> str:
    """Dispatch a bounded job to a registered agent adapter on this node.

    Supported agents run local CLIs under the OS-identity execution
    model: the Codex adapter runs the Codex CLI and the Claude adapter
    runs the Claude Code CLI, both as this gateway's own OS user — no
    sudo/setuid/credential escalation. agent must be a registered
    adapter (call aee_agents for the live registry set).

    mode decides the agent's permission boundary:
    * 'read_only' (default): the agent runs inside its native read-only
      sandbox (codex --sandbox read-only; claude --permission-mode plan)
      and cannot modify the filesystem.
    * 'write': an explicitly authorized, scoped write job. Requires per-job
      operator approval — pass metadata {"approval": "<operator-issued
      token>"} — and an operator-configured write-root scope: the job's
      working_directory must be inside that scope. Writes are executed by
      the agent's native write sandbox (codex --sandbox workspace-write;
      claude --permission-mode acceptEdits). Unauthorized writes, missing
      approval or out-of-scope working directories are denied (fail
      closed). The approval proof is fingerprinted into the job record's
      audit trail; the secret itself is never stored or logged.

    * 'autonomous': OS-native authority under the service account, without
      per-tool approvals. Requires authenticated transport identity and a
      short-lived single-job operator capability in metadata.autonomous_approval.
      Bound to host, OS identity, agent, exact task and cwd; no mandatory root
      allowlist. The native profile is os-native (omit selector fields).
      AEE does not add OS privileges. Call aee_agents for actual CLI capabilities.

    working_directory is a job parameter: access is decided by the
    executor identity's real OS permissions on that directory, not by a
    mandatory allowlist (operator-configured root lists still confine
    read-only dispatch and confine write scope when set). Every job runs
    with bounded resources (timeout, output caps, durable spool) and a
    full audit trail. Returns immediately with a job_id; track the job
    lifecycle through aee_job_status / aee_job_result and cancel active
    jobs through aee_job_cancel.
    """
    # Back-compat: A1 callers passed `prompt` + optional `metadata`.
    task = task or prompt or ""
    try:
        data = await asyncio.to_thread(
            dispatch_job, agent, task, working_directory, mode, execution_profile, model, reasoning_effort,
            metadata
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
        log.warning("event=dispatch_denied mode=%s agent=%s code=%s",
                    mode if mode in A3_ALLOWED_MODES else 'invalid',
                    agent if agent in A3_ALLOWED_AGENTS else 'invalid', exc.code)
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


@mcp.tool()
async def aee_job_cancel(job_id: str) -> str:
    """Request cancellation of an active dispatched AEE job.

    Cooperative-cancel: the worker thread observes the cancel flag inside
    its bounded run loop, raises EXECUTION_CANCELLED, and the executor's
    whole process group is killed by the sandbox teardown. Terminal state
    (cancelled) is persisted by the worker; poll aee_job_status.
    """
    try:
        data = await asyncio.to_thread(JOB_STORE.get, job_id)
    except _JobError as exc:
        return json.dumps(job_error_payload(exc), ensure_ascii=False)
    if data["status"] not in ACTIVE_STATES:
        return json.dumps({
            "ok": False,
            "job_id": job_id,
            "error_code": "JOB_NOT_ACTIVE",
            "error": f"job is {data['status']}; cancel is only valid while queued/running",
        }, ensure_ascii=False)
    event = _cancel_events.get(job_id)
    if event is None:
        # Worker already finished or release raced the store read.
        return json.dumps({
            "ok": False,
            "job_id": job_id,
            "error_code": "JOB_NOT_ACTIVE",
            "error": "job is no longer owned by a running worker",
        }, ensure_ascii=False)
    event.set()
    return json.dumps({"ok": True, "job_id": job_id, "status": data["status"],
                       "cancel": "requested"}, ensure_ascii=False)


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
        if any(hmac.compare_digest(token.encode(), item.encode()) for item in allowed):
            context_token = authenticated_transport.set(auth_digest(token))
            try:
                await app(scope, receive, send)
            finally:
                authenticated_transport.reset(context_token)
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


def startup_gate_mode(policy) -> str:
    """Startup/deployment policy decision for the gateway process.

    Returns ``APPROVED`` when the manifest passed the reviewed gate (the
    production codex/broker contract is then also required on startup),
    ``OPEN_GENERIC`` when a well-formed manifest exists but Stage-2 is
    not approved (gateway starts; generic adapter dispatch remains under
    auth + per-job safety), and raises for a structurally invalid
    manifest (deployment_policy already fail-closes those).
    """
    if policy is None:
        return "OPEN_GENERIC"  # policy-absent isolated/bootstrap mode
    return "APPROVED" if deployment_gate_approved(policy) else "OPEN_GENERIC"


def main() -> None:
    # Multi-agent recovery: basic gateway startup and generic adapter
    # dispatch are no longer gated on Stage-2 deployment certification.
    # ONLY the approved production codex/broker contract checks the gate;
    # auth and per-job safety are enforced on every branch. An INVALID
    # manifest (unparseable/structurally rejected) remains fail-closed:
    # the host policy cannot be trusted for dispatch.
    policy = deployment_policy()
    mode = startup_gate_mode(policy)
    if mode == "APPROVED":
        require_deployment_ready(policy)
        resolve_codex(A3_CODEX_BIN)
        read_manifest(A3_WORKSPACE_MANIFEST)
        if not os.getenv("AEE_BROKER_CONTROL") or AEE_MCP_HOST != "127.0.0.1":
            raise _JobError("RUNTIME_POLICY_INVALID", "Production broker and loopback binding are required")
    elif mode != "OPEN_GENERIC":
        raise _JobError("RUNTIME_POLICY_INVALID", "Deployment policy mode is invalid")
    else:
        log.warning(
            "event=startup_deployment_gate_open "
            "mode=generic-adapter-dispatch note=stage2-not-approved; "
            "auth and per-job safety remain enforced")
    if AEE_MCP_REQUIRE_AUTH and not AEE_MCP_API_KEY:
        print(
            "FATAL: MCP_BRIDGE_API_KEY is not configured; refusing to start "
            "without auth. Set it in the service Environment file, or set "
            "AEE_MCP_REQUIRE_AUTH=false explicitly for local testing.",
            file=sys.stderr,
        )
        sys.exit(2)
    log.info(
        "AEE MCP Gateway starting on %s:%s (auth=%s, allowed_roots=%s)",
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
        "AEE MCP Gateway binding: http://%s:%s/mcp (health: /health, status: /status)",
        AEE_MCP_HOST,
        AEE_MCP_PORT,
    )
    server.run()


if __name__ == "__main__":
    main()
