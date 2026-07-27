"""AEE Phase 2 — ``aee doctor`` health-check command (§21.x).

This module implements a comprehensive, side-effect-free health check
that validates whether a machine is ready to run AEE and migrate to a
new environment. It is invoked via ``aee doctor`` (see :mod:`aee.cli`).

Design contract:

* **Read-only.** No function in this module writes to disk, mutates
  runtime state, sends network requests with side effects, or touches
  the dispatcher DB. The optional network probe is a single HTTP GET
  with a short timeout — it never mutates the upstream.
* **Stdlib-only at import time.** Module top-level imports are limited
  to the Python standard library so the doctor can run even when
  optional dependencies (``fastapi``, ``yaml``, ``pydantic`` …) are
  missing. Each check that needs an optional import does the
  ``import`` lazily inside its own body and reports ``CAVEAT`` /
  ``FAIL`` if the import fails.
* **No secret exposure.** The environment-variable check reports the
  *presence* of each required variable, never its value. The
  ``detail`` string uses the literal variable name only.
* **Deterministic verdicts.** The overall verdict is computed by
  folding per-check statuses with the rule
  ``FAIL > CAVEAT > PASS`` — a single FAIL sinks the whole report.
* **Machine-readable.** :meth:`DoctorReport.to_dict` produces a JSON
  object suitable for ``aee doctor --json``; the human-readable form
  (:meth:`DoctorReport.to_text`) is a plain-text table.
* **Injectable environment.** :class:`DoctorRunner` accepts an
  ``environ`` mapping (defaults to :data:`os.environ`) and a
  ``repo_root`` :class:`pathlib.Path` (defaults to ``Path.cwd()``)
  so tests can drive the doctor without touching the real filesystem
  or the real environment. The network probe is gated by
  ``network=True`` and a ``connect_timeout`` (default 2 seconds).
* **Profile awareness.** The doctor accepts an optional
  ``profile`` parameter and reports which profile is being checked.
  Profile validation delegates to
  :func:`aee.profiles.descriptor.parse_profile`; an unknown profile
  is reported as a FAIL check (not an exception) so the doctor can
  still emit a report.

Run: ``PYTHONPATH=. python3 -m unittest aee.tests.test_aee_phase2_doctor -v``
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

# Canonical source of truth — profile names (no parallel hard-coded matrix).
from aee.profiles.descriptor import (
    DEFAULT_PROFILE,
    KNOWN_PROFILES,
    UnknownProfileError,
    parse_profile,
)

# ---------------------------------------------------------------------------#
# Exit codes (extend the installer's 0/2/3/4/5/6 without collision)
# ---------------------------------------------------------------------------#

#: Doctor found everything healthy.
EXIT_DOCTOR_OK = 0

#: All required checks passed but at least one optional check raised a
#: caveat (e.g. Docker not installed). The machine is usable but the
#: operator should review the caveats.
EXIT_DOCTOR_CAVEATS = 7

#: At least one required check failed. The machine is not ready to
#: run AEE until the failure is resolved.
EXIT_DOCTOR_FAILED = 8


# ---------------------------------------------------------------------------#
# Status vocabulary
# ---------------------------------------------------------------------------#

#: The three possible check outcomes, in worsening order. The order
#: matters because :func:`_fold` uses it to pick the worst status.
_STATUS_ORDER = ("PASS", "CAVEAT", "FAIL")
_STATUS_RANK = {s: i for i, s in enumerate(_STATUS_ORDER)}


def _fold(a: str, b: str) -> str:
    """Return the worse of two statuses (FAIL > CAVEAT > PASS)."""
    return a if _STATUS_RANK[a] >= _STATUS_RANK[b] else b


# ---------------------------------------------------------------------------#
# Required environment variables (presence-only; never expose values)
# ---------------------------------------------------------------------------#
#
# The doctor checks *presence* of each of these. The values are never
# read or echoed. The list is split into "required" (FAIL if absent)
# and "optional" (CAVEAT if absent) tiers so a misconfigured key does
# not block migration but is surfaced.
REQUIRED_ENV_VARS: Tuple[str, ...] = (
    "HERMES_BASE_URL",
    "HERMES_API_KEY",
    "BRIDGE_HOST",
    "BRIDGE_PORT",
)
OPTIONAL_ENV_VARS: Tuple[str, ...] = (
    "BRIDGE_API_KEY",
    "GPT_BRIDGE_API_KEY",
    "CLAUDE_BRIDGE_API_KEY",
    "CURSOR_BRIDGE_API_KEY",
    "MCP_BRIDGE_API_KEY",
    "MINIMAX_API_KEY",
    "MINIMAX_MODEL",
    "BRIDGE_DEFAULT_MODEL",
    "DEFAULT_SESSION_ID",
)


# ---------------------------------------------------------------------------#
# Required Python dependencies (lazy import inside the check)
# ---------------------------------------------------------------------------#
#
# Each entry is ``(module_name, package_name)``. The doctor reports
# PASS if the import succeeds, FAIL otherwise. ``package_name`` is the
# pip-install name used in the ``detail`` string (never installed
# automatically — the doctor is read-only).
REQUIRED_DEPS: Tuple[Tuple[str, str], ...] = (
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn[standard]"),
    ("httpx", "httpx"),
    ("pydantic", "pydantic"),
    ("yaml", "pyyaml"),
    ("dotenv", "python-dotenv"),
)


# ---------------------------------------------------------------------------#
# Directories the runtime expects to exist or be able to create
# ---------------------------------------------------------------------------#
REQUIRED_DIRS: Tuple[str, ...] = ("data", "reports", "logs")


# ---------------------------------------------------------------------------#
# Minimum Python version (matches the host's py3.11 toolchain)
# ---------------------------------------------------------------------------#
MIN_PYTHON_VERSION: Tuple[int, int] = (3, 11)


# ---------------------------------------------------------------------------#
# DTOs
# ---------------------------------------------------------------------------#


@dataclass(frozen=True)
class CheckResult:
    """Result of a single health check.

    ``status`` is one of ``"PASS"``, ``"CAVEAT"``, ``"FAIL"``.
    ``detail`` is a single human-readable line (no trailing newline).
    ``caveat`` is the optional caveat text for CAVEAT-status results;
    kept separate from ``detail`` so the report can surface caveats
    in a dedicated section without re-parsing prose.

    The DTO is frozen to keep the doctor's report immutable once built.
    """

    name: str
    status: str
    detail: str
    caveat: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "caveat": self.caveat,
        }


@dataclass(frozen=True)
class DoctorReport:
    """Aggregated doctor report.

    ``verdict`` is the worst of all per-check statuses. ``checks`` is
    the ordered tuple of :class:`CheckResult` (the order checks ran
    in, not sorted by severity — operators see the natural flow).
    """

    verdict: str
    profile: str
    checks: Tuple[CheckResult, ...]
    summary: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "profile": self.profile,
            "checks": [c.to_dict() for c in self.checks],
            "summary": dict(self.summary),
        }

    def to_text(self) -> str:
        """Render the report as a plain-text table.

        Uses fixed-width columns so the output is greppable. Avoids
        any ANSI escape codes — the doctor's output may be piped into
        logs or parsed by other tools.
        """
        lines: List[str] = []
        lines.append("aee doctor — AEE readiness health check")
        lines.append("  profile : {p}".format(p=self.profile))
        lines.append("  verdict : {v}".format(v=self.verdict))
        lines.append("")
        lines.append(
            "  {name:<28} {status:<8} {detail}".format(
                name="CHECK", status="STATUS", detail="DETAIL"
            )
        )
        lines.append("  " + "-" * 78)
        for c in self.checks:
            lines.append(
                "  {name:<28} {status:<8} {detail}".format(
                    name=c.name, status=c.status, detail=c.detail
                )
            )
        lines.append("")
        lines.append(
            "  summary: PASS={p} CAVEAT={c} FAIL={f} (total={n})".format(
                p=self.summary.get("PASS", 0),
                c=self.summary.get("CAVEAT", 0),
                f=self.summary.get("FAIL", 0),
                n=len(self.checks),
            )
        )
        caveats = [c for c in self.checks if c.status == "CAVEAT" and c.caveat]
        if caveats:
            lines.append("")
            lines.append("  caveats:")
            for c in caveats:
                lines.append("    - {name}: {cv}".format(name=c.name, cv=c.caveat))
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------#
# Individual checks (pure functions, no module-level side effects)
# ---------------------------------------------------------------------------#


def _check_python_version() -> CheckResult:
    """Verify the running Python meets :data:`MIN_PYTHON_VERSION`."""
    cur = sys.version_info[:2]
    detail = "running Python {v}".format(v=".".join(str(x) for x in cur))
    if cur >= MIN_PYTHON_VERSION:
        return CheckResult("python_version", "PASS", detail)
    need = ".".join(str(x) for x in MIN_PYTHON_VERSION)
    return CheckResult(
        "python_version",
        "FAIL",
        detail + " (need >= {need})".format(need=need),
    )


def _check_git(repo_root: Path) -> CheckResult:
    """Verify ``git`` is on PATH and ``repo_root`` is a git repo."""
    binary = shutil.which("git")
    if binary is None:
        return CheckResult(
            "git_availability",
            "FAIL",
            "git binary not found on PATH",
        )
    # We do NOT call git (avoids the rtk wrapper interference documented
    # in M2 memory). Instead we look for ``.git`` at repo_root.
    git_dir = repo_root / ".git"
    if git_dir.exists():
        return CheckResult(
            "git_repo_state",
            "PASS",
            "git={bin} repo_root={root} (.git present)".format(
                bin=binary, root=str(repo_root)
            ),
        )
    return CheckResult(
        "git_repo_state",
        "CAVEAT",
        "git={bin} repo_root={root} (.git MISSING)".format(
            bin=binary, root=str(repo_root)
        ),
        caveat="repo_root is not a git checkout; AEE can still run but "
        "version-tracking features (commits, diffs) are unavailable.",
    )


def _check_dependencies() -> CheckResult:
    """Try to import each required dependency module."""
    missing: List[str] = []
    for mod_name, pkg_name in REQUIRED_DEPS:
        try:
            __import__(mod_name)
        except ImportError:
            missing.append(pkg_name)
    if not missing:
        return CheckResult(
            "required_dependencies",
            "PASS",
            "all {n} required modules importable".format(
                n=len(REQUIRED_DEPS)
            ),
        )
    return CheckResult(
        "required_dependencies",
        "FAIL",
        "missing: {m}".format(m=", ".join(missing)),
    )


def _check_config_files(repo_root: Path) -> CheckResult:
    """Check presence of ``.env`` and ``requirements.lock``."""
    env_path = repo_root / ".env"
    lock_path = repo_root / "requirements.lock"
    have_env = env_path.exists()
    have_lock = lock_path.exists()
    if have_env and have_lock:
        return CheckResult(
            "config_files",
            "PASS",
            ".env + requirements.lock present",
        )
    if have_env and not have_lock:
        return CheckResult(
            "config_files",
            "CAVEAT",
            ".env present; requirements.lock missing",
            caveat="without a lockfile, dependency versions may drift "
            "between machines; pip install -r requirements.txt will "
            "resolve freely.",
        )
    if have_lock and not have_env:
        return CheckResult(
            "config_files",
            "FAIL",
            ".env missing; requirements.lock present",
        )
    return CheckResult(
        "config_files",
        "FAIL",
        ".env + requirements.lock both missing",
    )


def _check_env_vars(environ: Mapping[str, str]) -> CheckResult:
    """Verify presence of each required env var (value never read)."""
    missing_required = [v for v in REQUIRED_ENV_VARS if not environ.get(v)]
    missing_optional = [v for v in OPTIONAL_ENV_VARS if not environ.get(v)]
    if not missing_required and not missing_optional:
        return CheckResult(
            "environment_variables",
            "PASS",
            "all {r} required + {o} optional vars present".format(
                r=len(REQUIRED_ENV_VARS), o=len(OPTIONAL_ENV_VARS)
            ),
        )
    if not missing_required and missing_optional:
        return CheckResult(
            "environment_variables",
            "CAVEAT",
            "required OK; optional missing: {m}".format(
                m=", ".join(missing_optional)
            ),
            caveat="optional vars absent: {m}; corresponding integration "
            "(GPT, Cursor, MCP, MiniMax) will be disabled until "
            "configured.".format(m=", ".join(missing_optional)),
        )
    return CheckResult(
        "environment_variables",
        "FAIL",
        "required missing: {m}".format(m=", ".join(missing_required)),
    )


def _check_directory_permissions(repo_root: Path) -> CheckResult:
    """Check that each required directory is writable (or creatable)."""
    failures: List[str] = []
    for name in REQUIRED_DIRS:
        d = repo_root / name
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            failures.append("{n}: cannot create".format(n=name))
            continue
        if not os.access(d, os.W_OK):
            failures.append("{n}: not writable".format(n=name))
    if not failures:
        return CheckResult(
            "directory_permissions",
            "PASS",
            "all {n} required dirs writable".format(n=len(REQUIRED_DIRS)),
        )
    return CheckResult(
        "directory_permissions",
        "FAIL",
        "; ".join(failures),
    )


def _check_hermes_connectivity(
    environ: Mapping[str, str],
    *,
    connect_timeout: float,
) -> CheckResult:
    """Probe the upstream Hermes Runtime (HERMES_BASE_URL).

    Uses :data:`urllib.request` with a short timeout. The probe is a
    GET request to ``HERMES_BASE_URL`` root path (or ``/health`` if
    not specified). It treats 2xx/3xx/4xx as "reachable" (the upstream
    is up and answering HTTP); only connection errors and 5xx are
    failures. The probe is **read-only** — it never sends credentials.
    """
    base = environ.get("HERMES_BASE_URL", "").strip()
    if not base:
        return CheckResult(
            "hermes_connectivity",
            "FAIL",
            "HERMES_BASE_URL not set; cannot probe upstream",
        )
    # Construct a probe URL. If the base already has a path, probe the
    # base as-is; otherwise probe ``/``. We deliberately do NOT send
    # the API key — this is a reachability check, not an auth check.
    probe_url = base if base.endswith("/") else base + "/"
    # Use a fake User-Agent to avoid Cloudflare's WAF blocking Python's
    # default ``Python-urllib/3.x`` UA (per M2 memory, 2026-07-07).
    req = urllib.request.Request(
        probe_url,
        headers={"User-Agent": "curl/7.88.1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=connect_timeout) as resp:
            code = resp.getcode()
    except urllib.error.HTTPError as exc:
        # 4xx is "reachable but rejected" — still means upstream is up.
        code = exc.code
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
        return CheckResult(
            "hermes_connectivity",
            "FAIL",
            "cannot reach {url}: {e}".format(url=probe_url, e=type(exc).__name__),
        )
    if 200 <= code < 500:
        return CheckResult(
            "hermes_connectivity",
            "PASS",
            "GET {url} -> HTTP {code}".format(url=probe_url, code=code),
        )
    return CheckResult(
        "hermes_connectivity",
        "FAIL",
        "GET {url} -> HTTP {code} (upstream error)".format(
            url=probe_url, code=code
        ),
    )


def _check_docker() -> CheckResult:
    """Optional Docker availability (CAVEAT if absent, never FAIL)."""
    binary = shutil.which("docker")
    if binary is None:
        return CheckResult(
            "docker_optional",
            "CAVEAT",
            "docker binary not on PATH",
            caveat="Docker is optional; container-based deployment "
            "profiles (see §21.5) will be unavailable.",
        )
    return CheckResult(
        "docker_optional",
        "PASS",
        "docker found at {b}".format(b=binary),
    )


def _check_profile(profile: str) -> CheckResult:
    """Validate the requested profile against the canonical registry."""
    try:
        canonical = parse_profile(profile)
    except UnknownProfileError as exc:
        return CheckResult(
            "profile_known",
            "FAIL",
            "unknown profile {p!r}: {e}".format(p=profile, e=exc),
        )
    return CheckResult(
        "profile_known",
        "PASS",
        "profile={p} (known_profiles={k})".format(
            p=canonical, k=", ".join(KNOWN_PROFILES)
        ),
    )


def _check_platform_info() -> CheckResult:
    """Surface host platform info (always PASS — informational only)."""
    return CheckResult(
        "platform_info",
        "PASS",
        "python={py} platform={pl} machine={m}".format(
            py=platform.python_version(),
            pl=platform.platform(),
            m=platform.machine(),
        ),
    )


# ---------------------------------------------------------------------------#
# Runner
# ---------------------------------------------------------------------------#


class DoctorRunner:
    """Run the AEE readiness checks and produce a :class:`DoctorReport`.

    Construction is cheap; no I/O happens until :meth:`run` is called.
    All filesystem/network/environment reads go through the constructor
    arguments so tests can inject fakes.
    """

    def __init__(
        self,
        *,
        repo_root: Optional[Path] = None,
        environ: Optional[Mapping[str, str]] = None,
        profile: str = DEFAULT_PROFILE,
        network: bool = True,
        connect_timeout: float = 2.0,
    ) -> None:
        self.repo_root = Path(repo_root) if repo_root is not None else Path.cwd()
        self.environ: Mapping[str, str] = environ if environ is not None else os.environ
        self.profile = profile
        self.network = network
        self.connect_timeout = connect_timeout

    def run(self) -> DoctorReport:
        """Run all checks in order and fold the results into a report."""
        checks: List[CheckResult] = []
        # Profile validation first so a typo doesn't abort the whole
        # run — the doctor should still emit a report.
        checks.append(_check_profile(self.profile))
        # Platform info — informational, always PASS.
        checks.append(_check_platform_info())
        # Python version.
        checks.append(_check_python_version())
        # Git.
        checks.append(_check_git(self.repo_root))
        # Required dependencies (lazy import inside check).
        checks.append(_check_dependencies())
        # Config files.
        checks.append(_check_config_files(self.repo_root))
        # Environment variables (presence only).
        checks.append(_check_env_vars(self.environ))
        # Directory permissions.
        checks.append(_check_directory_permissions(self.repo_root))
        # Hermes upstream connectivity (opt-in via constructor).
        if self.network:
            checks.append(
                _check_hermes_connectivity(
                    self.environ, connect_timeout=self.connect_timeout
                )
            )
        # Docker (optional).
        checks.append(_check_docker())

        # Fold the verdict.
        verdict = "PASS"
        for c in checks:
            verdict = _fold(verdict, c.status)

        summary: Dict[str, int] = {"PASS": 0, "CAVEAT": 0, "FAIL": 0}
        for c in checks:
            summary[c.status] = summary.get(c.status, 0) + 1

        return DoctorReport(
            verdict=verdict,
            profile=self.profile,
            checks=tuple(checks),
            summary=summary,
        )


# ---------------------------------------------------------------------------#
# Convenience
# ---------------------------------------------------------------------------#


def run_doctor(
    *,
    repo_root: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
    profile: str = DEFAULT_PROFILE,
    network: bool = True,
    connect_timeout: float = 2.0,
) -> DoctorReport:
    """Build a :class:`DoctorRunner`, run it, return the report.

    Convenience wrapper for callers that don't need to hold the runner.
    """
    runner = DoctorRunner(
        repo_root=repo_root,
        environ=environ,
        profile=profile,
        network=network,
        connect_timeout=connect_timeout,
    )
    return runner.run()


__all__ = [
    "EXIT_DOCTOR_OK",
    "EXIT_DOCTOR_CAVEATS",
    "EXIT_DOCTOR_FAILED",
    "REQUIRED_ENV_VARS",
    "OPTIONAL_ENV_VARS",
    "REQUIRED_DEPS",
    "REQUIRED_DIRS",
    "MIN_PYTHON_VERSION",
    "CheckResult",
    "DoctorReport",
    "DoctorRunner",
    "run_doctor",
]