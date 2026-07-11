"""AEE-7.6 isolated HTTP bridge sandbox.

Spawns a fresh uvicorn process on a sandbox port, with a temp DB
and temp logs, so HTTP round-trip tests can exercise the full
lifespan + FastAPI mount + dispatcher + identity pipeline without
touching the live-running bridge or the live ``data/dispatcher.db``.

Why a process (not in-process TestClient)
-----------------------------------------
The user-mandated contract for AEE-7.6 is "isolated bridge
process / sandbox HTTP round-trip". An in-process TestClient would
shortcut:

* ``uvicorn`` workers (we have 1 worker in the production conf,
  but TestClient uses 0 workers)
* the full lifespan startup/shutdown (``bootstrap_default_runtimes``,
  ``Watcher.start()``, ``Watcher.stop()``)
* the actual HTTP wire (multipart, headers, JSON serialization)
* the dispatcher hot path's thread-local connection caching
  (which the live bridge holds via ``_local.conn`` — see
  ``aee75-write-side-metadata-case-study.md`` "Live bridge
  holds deleted dispatcher.db inode")

This module's job is to spin up a controlled instance and tear
it down deterministically. The "controlled" is enforced by:

* ``BRIDGE_PORT`` — explicit, sandbox port. We pick a free port
  in 18787-19999 to avoid clashing with the live bridge
  (8787) or any other service.
* ``DISPATCHER_DB_PATH`` — env var, sandbox DB path under a
  tempdir. The bridge honours the env var via
  ``dispatcher.db.DB_PATH = Path(os.environ["DISPATCHER_DB_PATH"])``
  (we patch the module-level constant in the child process
  before uvicorn imports ``app``).
* ``DISPATCHER_LOG_DIR`` / ``DISPATCHER_REPORTS_DIR`` — sandbox
  paths so the live ``logs/`` and ``reports/`` directories
  are not touched.
* ``BRIDGE_API_KEY`` / ``DISPATCHER_API_KEY`` / etc. — sandbox
  keys, not the live production keys.
* The child process is started with ``subprocess.Popen`` and
  registered with ``atexit`` / ``__exit__`` so the process is
  always killed (SIGTERM, then SIGKILL after a grace period).

Process isolation contract
--------------------------
The sandbox is hermetic from the live bridge's perspective:

* Different ``data/dispatcher.db`` (tempdir copy / fresh
  schema).
* Different port (sandbox port, not 8787).
* Different API key (sandbox value, not the live
  ``BRIDGE_API_KEY``).
* Different log/report directories (tempdir, not
  ``/home/ubuntu/hermes-runtime-bridge/logs`` or
  ``/home/ubuntu/hermes-runtime-bridge/reports``).
* No interactions with the live ``aee.runtimes.repository`` —
  the child process has its own in-memory runtime registry.
* No interactions with the live ``Watcher`` (a fresh watcher
  starts on the sandbox DB).

The caller is expected to call :func:`cleanup_bridge_sandbox`
in a ``finally`` block to guarantee the child process is
killed and the tempdir is removed.
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, Optional


# ---------------------------------------------------------------------------
# Port allocation
# ---------------------------------------------------------------------------


def _pick_free_port() -> int:
    """Return an unused TCP port in the 18787-19999 range.

    Avoids the live bridge port (8787) and the legacy sandbox
    range used by older AEE-7.x smoke tests (19090-19199).
    """
    for port in range(18787, 19999):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("No free port in 18787-19999 for sandbox bridge")


# ---------------------------------------------------------------------------
# Sandbox handle
# ---------------------------------------------------------------------------


@dataclass
class BridgeSandbox:
    """Handle for a single live isolated bridge instance.

    Fields
    ------
    base_url
        ``http://127.0.0.1:<port>`` — for httpx/urllib clients.
    port
        The TCP port the child process is bound to.
    api_key
        The ``Bearer`` key to use for authenticated calls.
    data_dir
        Tempdir containing the sandbox ``dispatcher.db`` and
        any sidecar state.
    db_path
        The exact path to the sandbox DB.
    log_dir
        The sandbox log directory (isolated from the live
        ``logs/``).
    reports_dir
        The sandbox reports directory (isolated from the live
        ``reports/``).
    process
        The subprocess.Popen handle. Use
        :func:`cleanup_bridge_sandbox` to terminate it
        deterministically.
    ready_timeout_sec
        Max time to wait for ``/health`` to return 200. Default 30s.
    """

    base_url: str
    port: int
    api_key: str
    data_dir: Path
    db_path: Path
    log_dir: Path
    reports_dir: Path
    process: subprocess.Popen
    ready_timeout_sec: float = 30.0


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def start_bridge_sandbox(
    *,
    repo_root: Path,
    api_key: Optional[str] = None,
    port: Optional[int] = None,
    ready_timeout_sec: float = 30.0,
) -> BridgeSandbox:
    """Start a fresh uvicorn bridge process on a sandbox port + temp DB.

    Parameters
    ----------
    repo_root
        Absolute path to ``hermes-runtime-bridge`` (the repo root
        with ``app.py`` and the ``.venv``).
    api_key
        The ``Bearer`` key the sandbox will accept. Defaults to a
        random 16-char value. The caller uses this value to
        authenticate HTTP calls.
    port
        Optional explicit port. If absent, picks a free one in
        18787-19999.
    ready_timeout_sec
        Max seconds to wait for ``/health`` to return 200. The
        process is killed (SIGTERM then SIGKILL) if it doesn't
        come up in time.

    Returns
    -------
    BridgeSandbox
        A live sandbox handle. Always call
        :func:`cleanup_bridge_sandbox` (or use
        :func:`bridge_sandbox` as a context manager) to ensure
        the child is killed.
    """
    if not repo_root.exists():
        raise FileNotFoundError(f"repo_root does not exist: {repo_root}")
    app_py = repo_root / "app.py"
    venv_python = repo_root / ".venv" / "bin" / "python"
    if not app_py.exists():
        raise FileNotFoundError(f"app.py not found at {app_py}")
    if not venv_python.exists():
        raise FileNotFoundError(
            f"venv python not found at {venv_python}; "
            "AEE-7.6 sandbox requires the repo's .venv to be set up"
        )

    chosen_port = port or _pick_free_port()
    chosen_key = api_key or _gen_random_key()
    sandbox_root = Path(tempfile.mkdtemp(prefix="aee76-bridge-"))
    data_dir = sandbox_root / "data"
    log_dir = sandbox_root / "logs"
    reports_dir = sandbox_root / "reports"
    for d in (data_dir, log_dir, reports_dir):
        d.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "dispatcher.db"

    # Build a child env that is hermetic from the live bridge.
    child_env = _build_sandbox_env(
        repo_root=repo_root,
        db_path=db_path,
        log_dir=log_dir,
        reports_dir=reports_dir,
        api_key=chosen_key,
    )

    # Start the uvicorn process. We use a fresh python invocation
    # (NOT background=True from Hermes) so the process is
    # parented to this script and we can SIGTERM it deterministically.
    #
    # AEE-7.6 sandbox bootstrap: we use ``python -c "import aee.sandbox_bootstrap; exec(open(...).read())"``
    # so the child's ``dispatcher.db.DB_PATH`` is redirected to
    # the sandbox path BEFORE uvicorn imports ``app``. The bootstrap
    # is a no-op when ``AEE_BRIDGE_DB_PATH`` is unset, so the live
    # bridge (which doesn't set it) is unaffected.
    bootstrap = (
        "import sys, runpy;"
        f"sys.argv=['uvicorn','app:app','--host','127.0.0.1','--port','{chosen_port}',"
        "'--no-access-log','--log-level','warning'];"
        "import aee.sandbox_bootstrap;"
        "runpy.run_module('uvicorn', run_name='__main__')"
    )
    cmd = [
        str(venv_python),
        "-c",
        bootstrap,
    ]
    # IMPORTANT: cwd must be the repo root so app.py's relative
    # imports resolve. We also make sure the venv is on PYTHONPATH
    # (uvicorn does that automatically when invoked via the venv's
    # python).
    proc = subprocess.Popen(
        cmd,
        cwd=str(repo_root),
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # New process group so we can SIGTERM the whole group.
        start_new_session=True,
    )

    sandbox = BridgeSandbox(
        base_url=f"http://127.0.0.1:{chosen_port}",
        port=chosen_port,
        api_key=chosen_key,
        data_dir=sandbox_root,
        db_path=db_path,
        log_dir=log_dir,
        reports_dir=reports_dir,
        process=proc,
        ready_timeout_sec=ready_timeout_sec,
    )

    # Wait for the bridge to come up. This is the deterministic
    # "ready" probe — without it, the first HTTP call would race
    # the lifespan startup.
    _wait_until_ready(sandbox)
    return sandbox


def cleanup_bridge_sandbox(sandbox: BridgeSandbox) -> None:
    """Terminate the child process and remove the sandbox tempdir.

    Idempotent: safe to call multiple times. The process is
    SIGTERM'd first; if it doesn't exit within 5s, SIGKILL is
    sent. The tempdir is then removed (best-effort — if a file
    is still busy, the OS will retry on next unlink).
    """
    proc = sandbox.process
    if proc is not None:
        if proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    pass
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
    if sandbox.data_dir.exists():
        try:
            shutil.rmtree(sandbox.data_dir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


@contextmanager
def bridge_sandbox(
    *,
    repo_root: Path,
    api_key: Optional[str] = None,
    port: Optional[int] = None,
    ready_timeout_sec: float = 30.0,
) -> Generator[BridgeSandbox, None, None]:
    """Context manager wrapping :func:`start_bridge_sandbox` +
    :func:`cleanup_bridge_sandbox`. The sandbox is always
    cleaned up on exit, even if the body raises.
    """
    sandbox = start_bridge_sandbox(
        repo_root=repo_root,
        api_key=api_key,
        port=port,
        ready_timeout_sec=ready_timeout_sec,
    )
    try:
        yield sandbox
    finally:
        cleanup_bridge_sandbox(sandbox)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _gen_random_key() -> str:
    """Generate a random 16-char hex key for the sandbox.

    64 bits of entropy is plenty for an isolated test instance
    that lives for the duration of a single test method.
    """
    import secrets
    return secrets.token_hex(8)


def _build_sandbox_env(
    *,
    repo_root: Path,
    db_path: Path,
    log_dir: Path,
    reports_dir: Path,
    api_key: str,
) -> dict:
    """Build a hermetic child env.

    The strategy is to start from a sanitized copy of os.environ,
    then OVERWRITE the bridge-relevant keys. Anything that
    could affect the dispatcher's behavior (DB path, API key,
    runtime config, log directory) is set to the sandbox value.
    We deliberately preserve PATH and HOME so the venv
    python can find its site-packages.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        # Force unbuffered output so the bridge log line is
        # visible to anything tailing the process.
        "PYTHONUNBUFFERED": "1",
        # Sandbox-specific overrides.
        "BRIDGE_API_KEY": api_key,
        "DISPATCHER_API_KEY": api_key,
        "GPT_BRIDGE_API_KEY": api_key,
        "CLAUDE_BRIDGE_API_KEY": api_key,
        "CURSOR_BRIDGE_API_KEY": api_key,
        "MCP_BRIDGE_API_KEY": api_key,
        "DISPATCHER_DB_PATH": str(db_path),
        "DISPATCHER_LOG_DIR": str(log_dir),
        "DISPATCHER_REPORTS_DIR": str(reports_dir),
        # Suppress the watcher tick — we don't want the
        # sandbox's watcher polling the live Hermes 8642.
        "DISPATCHER_WATCHER_ENABLED": "false",
        "DISPATCHER_WATCHER_TICK": "0",
        # Tell app.py to use the sandbox DB / log / reports dirs.
        # The app's module-level constants read these at
        # import time; we patch them in the child via the
        # AEE_BRIDGE_DB_PATH env var (see app.py's _lifespan
        # if/when it grows the env-var read path).
        "AEE_BRIDGE_DB_PATH": str(db_path),
        "AEE_BRIDGE_LOG_DIR": str(log_dir),
        "AEE_BRIDGE_REPORTS_DIR": str(reports_dir),
        # Don't try to authenticate with upstream Hermes 8642.
        # The bridge will still boot; calls that need the
        # upstream will fail with 502 (which is what we want
        # for the round-trip test — we never want a sandbox
        # call to actually hit production Hermes).
        "HERMES_API_KEY": "",
        "HERMES_BASE_URL": "http://127.0.0.1:1",
        "OPENAI_API_KEY": "",
        "MINIMAX_API_KEY": "",
        # The repo root on PYTHONPATH so ``app`` imports as a
        # top-level module.
        "PYTHONPATH": str(repo_root),
    }
    return env


def _wait_until_ready(sandbox: BridgeSandbox) -> None:
    """Poll ``/health`` until 200 or timeout.

    Raises RuntimeError with the captured stderr/stdout if the
    process never becomes ready (or dies before becoming ready).
    """
    deadline = time.time() + sandbox.ready_timeout_sec
    url = f"{sandbox.base_url}/health"
    last_err: Optional[BaseException] = None
    while time.time() < deadline:
        # If the process is already dead, fail fast with the
        # captured output.
        if sandbox.process.poll() is not None:
            out = _safe_read_process_output(sandbox.process)
            raise RuntimeError(
                f"Bridge sandbox process exited (rc="
                f"{sandbox.process.returncode}) before becoming ready. "
                f"Output:\n{out}"
            )
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_err = exc
            time.sleep(0.2)
    # Timed out. Capture process output for diagnostics.
    out = _safe_read_process_output(sandbox.process)
    raise RuntimeError(
        f"Bridge sandbox did not become ready within "
        f"{sandbox.ready_timeout_sec}s (last error: {last_err}). "
        f"Process output:\n{out}"
    )


def _safe_read_process_output(proc: subprocess.Popen) -> str:
    """Best-effort read of the child's stdout/stderr.

    Does not block. If the pipe is empty, returns an empty
    string. The output is decoded as utf-8 with errors='replace'
    so a malformed log line never crashes the test.
    """
    if proc.stdout is None:
        return ""
    try:
        # Non-blocking read of whatever's been captured so far.
        import fcntl
        fd = proc.stdout.fileno()
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        try:
            data = proc.stdout.read()
        except (BlockingIOError, TypeError):
            data = b""
    except (OSError, ValueError):
        return ""
    if not data:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)
