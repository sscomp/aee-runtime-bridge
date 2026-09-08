#!/usr/bin/env python3
"""Verification-mode DB safety guard for the AEE runtime bridge (B4).

Closes the 2026-09-07 dispatcher-DB shell incident class: a pytest /
verification / smoke process whose ``dispatcher.db.DB_PATH`` still
pointed at the production ``<bridge_root>/data/dispatcher.db`` unlinked
the live production DB; the next ``get_conn()`` recreated an empty
schema shell at the same path while the bridge kept writing to the
(now deleted) old inode.

The guard is FAIL-CLOSED for test / verification / smoke contexts and
inert (by design) for the production service:

* ``production_db_identity()`` resolves the canonical production DB
  identity (realpath, device, inode) — never trusting a filename
  string.
* ``is_verification_context()`` detects pytest / verification-mode /
  test-runner environments (env markers, argv, ``sys.modules``).
* ``assert_not_production_db(path, ...)`` raises
  :class:`ProductionDBWriteAttemptError` when a candidate path resolves
  to the production DB identity, before any open/unlink can happen.
* ``rebind_to_temp_db()`` / ``apply_default_test_db_override()`` give
  test and verification processes a unique tempdir DB automatically —
  before imports and module-level ``_reset_db()`` code run.

Production keeps its normal behavior: nothing in this module is
consulted by the live bridge (it never imports this module), and a
process that explicitly calls ``enter_verification_mode()`` /
``exit_verification_mode()`` pairs with the notification contract
(``aee/_notification_guard.py``).
"""
from __future__ import annotations

import os
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

# ---------------------------------------------------------------------------
# Production identity
# ---------------------------------------------------------------------------

_BRIDGE_ROOT = Path(__file__).resolve().parent.parent
#: Canonical production DB path (resolved once, at import time, from
#: this file's location — the same anchor dispatcher/db.py uses).
PRODUCTION_DB_PATH: Path = _BRIDGE_ROOT / "data" / "dispatcher.db"

_lock = threading.RLock()
_identity_cache: Optional[dict] = None
# Test hooks may force a fake identity (requirement 6f): keyed by
# ``os.getpid()`` so parallel test processes never share the override.
_identity_override: dict = {}


def production_db_identity() -> dict:
    """Resolve the production DB's canonical identity.

    Returns a dict with:

    * ``realpath`` — fully resolved absolute path (symlinks, ``..``,
      and relative aliases collapse to this);
    * ``device`` / ``inode`` — filesystem identity when the file
      currently exists (an identity comparison on these catches even a
      same-content copy hard- or bind-mounted at another path).

    The identity is computed ONCE per process and cached: the
    production DB must not move under us mid-run, and ``os.stat`` on a
    live WAL-backed DB is not free. Call :func:`reset_identity_cache`
    in tests that need to re-resolve.
    """
    global _identity_cache
    with _lock:
        if _identity_override:
            return dict(_identity_override)
        if _identity_cache is not None:
            return dict(_identity_cache)
    real = os.path.realpath(str(PRODUCTION_DB_PATH))
    ident = {"realpath": real, "device": None, "inode": None}
    try:
        st = os.stat(real)
        ident["device"] = st.st_dev
        ident["inode"] = st.st_ino
    except OSError:
        # The production DB may legitimately not exist yet (fresh
        # host). The canonical path still protects it: any path that
        # resolves to the same realpath is rejected.
        pass
    with _lock:
        _identity_cache = ident
        return dict(_identity_cache)


def reset_identity_cache() -> None:
    """Drop the cached production identity (re-resolve on next use)."""
    global _identity_cache
    with _lock:
        _identity_cache = None


def _set_identity_override_for_testing(ident: Optional[dict]) -> None:
    """Install a per-process identity override (requirement 6f tests).

    Lets a test simulate "production lives elsewhere" without touching
    the real production DB. ``None`` clears the override.
    """
    global _identity_override
    with _lock:
        _identity_override = dict(ident) if ident else {}
        _identity_cache = None


def canonicalize(path) -> str:
    """Canonical string form of a candidate path.

    ``os.path.realpath`` collapses symlinks, relative segments and
    ``..`` traversal; a missing leaf is resolved against its nearest
    existing parent (so ``<prod_dir>/dispatcher.db`` where the leaf was
    already unlinked still canonicalizes to the production path).
    """
    return os.path.realpath(str(path))


# ---------------------------------------------------------------------------
# Verification-context detection
# ---------------------------------------------------------------------------

#: Env markers that mean "this process must never touch the production
#: DB directly". Extensible: any future test/verification runner adds
#: its own name here.
VERIFICATION_ENV_MARKERS = (
    "PYTEST_CURRENT_TEST",       # set by pytest for the duration of a test
    "PYTEST_VERSION",            # set by pytest at session start
    "AEE_BRIDGE_VERIFICATION",   # scripts/a2_runner.py verification mode
    "AEE_BRIDGE_TEST_MODE",      # explicit opt-in marker
)

#: argv fragments of known test/verification runners.
_VERIFICATION_ARGV_MARKERS = (
    "pytest",
    "unittest",
    "a2_runner.py",
    "a2_smoke.py",
)


def is_verification_context() -> bool:
    """True iff this process looks like a test/verification context.

    Detection layers (any hit = verification):

    1. env markers: ``PYTEST_*`` (pytest sets these itself — a spawned
       child pytest inherits nothing, but its own session sets them),
       ``AEE_BRIDGE_VERIFICATION`` (armed by :func:`enter_verification_mode`
       and by ``scripts/a2_runner.py`` / ``scripts/a2_smoke.py``),
       ``AEE_BRIDGE_TEST_MODE`` (explicit);
    2. process argv containing a known runner name (pytest / unittest /
       the a2 verification wrappers) — catches wrapper scripts that
       forget to export the env var;
    3. ``pytest`` / ``_pytest`` importable in ``sys.modules`` — catches
       in-process library-style pytest use.

    Never raises; unknown shapes fail OPEN here and are closed by the
    :func:`assert_not_production_db` identity check, which applies in
    every context for unlink-class operations.
    """
    try:
        for name in VERIFICATION_ENV_MARKERS:
            if os.environ.get(name):
                return True
        argv0 = " ".join(sys.argv[:3]).lower() if sys.argv else ""
        for marker in _VERIFICATION_ARGV_MARKERS:
            if marker in argv0:
                return True
        if "pytest" in sys.modules or "_pytest" in sys.modules:
            return True
    except Exception:  # noqa: BLE001 — detection must never raise
        return False
    return False


# ---------------------------------------------------------------------------
# The fail-closed rejection
# ---------------------------------------------------------------------------


class ProductionDBWriteAttemptError(RuntimeError):
    """Raised when an operation would resolve to the production DB."""


def _identity_matches(candidate_real: str) -> bool:
    ident = production_db_identity()
    if candidate_real == ident["realpath"]:
        return True
    # Same-file check via device/inode only when the candidate exists
    # AND we have a stored inode: a production path that does not
    # currently exist cannot be "the same file" as an existing
    # candidate — the candidate may legitimately be a fresh temp DB
    # whose basename merely matches.
    if ident["inode"] is not None and os.path.exists(candidate_real):
        try:
            st = os.stat(candidate_real)
            if st.st_dev == ident["device"] and st.st_ino == ident["inode"]:
                return True
        except OSError:
            pass
    return False


def assert_not_production_db(path, *, operation: str = "use") -> str:
    """Fail closed if ``path`` resolves to the production dispatcher DB.

    Compares the CANDIDATE's canonical form (symlink / relative /
    ``..``-traversal collapsed) against the production DB's canonical
    identity (realpath + device/inode when both exist). Never compares
    filename strings.

    Raises :class:`ProductionDBWriteAttemptError` BEFORE any open /
    unlink / recreate can happen. Returns the canonical candidate path
    on success (production-context callers use the return value to
    proceed).
    """
    candidate_real = canonicalize(path)
    if _identity_matches(candidate_real):
        raise ProductionDBWriteAttemptError(
            f"REFUSED: {operation} on {candidate_real!r} resolves to the "
            f"production dispatcher DB. Test/verification contexts must "
            f"use a temp/sandbox DB (see aee/_db_guard.py:"
            f"apply_default_test_db_override). If this is genuinely the "
            f"production service, route the operation through the "
            f"dispatcher API / an explicit production opt-in — never by "
            f"rebinding DB_PATH in a test process."
        )
    return candidate_real


# ---------------------------------------------------------------------------
# Temp DB allocation (the automatic test isolation)
# ---------------------------------------------------------------------------

_alloc_lock = threading.Lock()
_allocated_temp_dbs: list = []


def allocate_temp_db_path(prefix: str = "aee-b4-guard-") -> Path:
    """Create a unique tempdir and return its ``dispatcher.db`` path.

    The directory persists for the process lifetime (the DB may be
    opened lazily much later); cleanup happens in
    :func:`cleanup_temp_dbs` at session end. Every call gets a fresh
    unique directory — never a shared or predictable path.
    """
    import tempfile

    with _alloc_lock:
        tmpdir = Path(tempfile.mkdtemp(prefix=prefix))
        path = tmpdir / "dispatcher.db"
        _allocated_temp_dbs.append(tmpdir)
    return path


def cleanup_temp_dbs() -> None:
    """Best-effort removal of temp DB dirs allocated by this process."""
    import shutil

    with _alloc_lock:
        dirs, _allocated_temp_dbs[:] = list(_allocated_temp_dbs), []
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)


@contextmanager
def rebind_dispatcher_db(db_path: Optional[Path] = None) -> Iterator[Path]:
    """Rebind ``dispatcher.db`` (+ manager log/report dirs) to a temp DB.

    This is the shared, guarded rebind entry point. With ``db_path``
    omitted, a unique tempdir DB is allocated. The rebind is asserted
    fail-closed BEFORE any module constant changes (requirement 4b:
    pointing the test DB at the production canonical path raises
    here, not at first connect).

    Restores the previous constants and drops the cached thread-local
    connection on exit.
    """
    import dispatcher.db as ddb

    resolved = db_path if db_path is not None else allocate_temp_db_path()
    assert_not_production_db(resolved, operation="rebind dispatcher.db.DB_PATH")

    saved = {
        "DB_DIR": ddb.DB_DIR,
        "DB_PATH": ddb.DB_PATH,
        "_local_conn": getattr(ddb._local, "conn", None),
        "_initialized": ddb._initialized,
    }
    try:
        import dispatcher.manager as dmm

        saved["LOGS_DIR"] = dmm.LOGS_DIR
        saved["REPORTS_DIR"] = dmm.REPORTS_DIR
        dmm.LOGS_DIR = resolved.parent / "logs"
        dmm.REPORTS_DIR = resolved.parent / "reports"
        dmm.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        dmm.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001 — manager may be absent in some contexts
        pass
    try:
        ddb.DB_DIR = resolved.parent
        ddb.DB_PATH = resolved
        ddb._local.conn = None
        ddb._initialized = False
        yield resolved
    finally:
        try:
            ddb.DB_DIR = saved["DB_DIR"]
            ddb.DB_PATH = saved["DB_PATH"]
            ddb._local.conn = saved["_local_conn"]
            ddb._initialized = saved["_initialized"]
            if "LOGS_DIR" in saved:
                import dispatcher.manager as dmm

                dmm.LOGS_DIR = saved["LOGS_DIR"]
                dmm.REPORTS_DIR = saved["REPORTS_DIR"]
        except Exception:  # noqa: BLE001 — interpreter-shutdown imports fail
            pass


def apply_default_test_db_override() -> Path:
    """Fail-closed default DB override for test/verification processes.

    If ``dispatcher.db.DB_PATH`` currently resolves to the production
    DB, rebind it (plus manager dirs) to a fresh unique temp DB and
    return the new path. Call as EARLY as possible (session start /
    module import), BEFORE any test module's module-level ``_reset_db()``
    runs. Idempotent: a second call with a non-production DB_PATH is a
    no-op returning the current path.

    In a production-context process (no verification markers), this is
    a no-op that returns the existing DB_PATH — production behavior is
    unchanged.
    """
    import dispatcher.db as ddb

    try:
        current = canonicalize(ddb.DB_PATH)
    except Exception:  # noqa: BLE001
        current = None
    if current is None or not _identity_matches(current):
        return Path(str(ddb.DB_PATH))
    # The module is still production-bound in a verification context:
    # rebind permanently (no restore) — the test/verification session
    # owns the dispatcher module from here on. Unique tempdir per
    # process, so parallel sessions never collide.
    temp = allocate_temp_db_path(prefix="aee-b4-auto-")
    ddb.DB_DIR = temp.parent
    ddb.DB_PATH = temp
    ddb._local.conn = None
    ddb._initialized = False
    try:
        import dispatcher.manager as dmm

        dmm.LOGS_DIR = temp.parent / "logs"
        dmm.REPORTS_DIR = temp.parent / "reports"
        dmm.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        dmm.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return temp


# ---------------------------------------------------------------------------
# Hardened unlink (requirement 3)
# ---------------------------------------------------------------------------


def safe_unlink_dispatcher_db(path) -> None:
    """Unlink a dispatcher DB file + WAL/SHM sidecars, fail-closed.

    Refuses (raising :class:`ProductionDBWriteAttemptError`) when
    ``path`` resolves to the production DB in ANY context — this check
    is unconditional for unlink-class operations, not just verification
    contexts. Only after the guard passes are the sidecar files
    (``-wal`` / ``-shm``) unlinked alongside the main file.
    """
    assert_not_production_db(path, operation="unlink")
    p = Path(str(path))
    for ext in ("", "-wal", "-shm", "-journal"):
        f = p.with_name(p.name + ext)
        try:
            if f.exists():
                f.unlink()
        except FileNotFoundError:
            pass


def child_db_sentinel() -> Optional[Path]:
    """The temp DB path a SPAWNED child must inherit, or ``None``.

    Returns this process's isolated dispatcher DB binding when one is
    active (i.e. ``dispatcher.db.DB_PATH`` is NOT production): the
    value for the ``AEE_BRIDGE_DB_PATH`` sentinel that makes a spawned
    sandbox/bridge child rebind to the parent's temp DB via
    ``aee.sandbox_bootstrap`` instead of falling back to the production
    path. In a production-context process this returns ``None`` — no
    sentinel is injected, production behavior is unchanged.
    """
    try:
        import dispatcher.db as ddb

        current = canonicalize(ddb.DB_PATH)
        if not _identity_matches(current):
            return Path(str(ddb.DB_PATH))
    except Exception:  # noqa: BLE001 — a sentinel must never break the caller
        pass
    return None


# ---------------------------------------------------------------------------
# Process-level unlink guard (defense in depth)
# ---------------------------------------------------------------------------
#
# The per-call guards above only protect callers that route through
# ``safe_unlink_dispatcher_db`` / ``rebind_dispatcher_db``. A legacy
# test module may call ``Path.unlink()`` / ``os.unlink`` /
# ``os.remove`` directly. This wrapper makes ANY unlink against a path
# resolving to the production dispatcher DB identity raise
# :class:`ProductionDBWriteAttemptError` in every context — production
# service processes are protected too (the wrapper is inert for every
# other path; the bridge never unlinks the dispatcher DB at runtime).

_UNLINK_GUARD_STATE = {"installed": False, "originals": {}}


def install_unlink_guard() -> None:
    """Wrap ``os.unlink`` / ``os.remove`` AND ``pathlib.Path.unlink``.

    ANY unlink against a path resolving to the production dispatcher
    DB identity raises :class:`ProductionDBWriteAttemptError` before
    the syscall — in every context (test or production service). This
    is the last-ditch layer that closes the module-level
    ``_reset_db()`` shape even in a process that never routed through
    the guarded helpers.
    """
    import os as _os
    import pathlib as _pathlib

    with _lock:
        if _UNLINK_GUARD_STATE["installed"]:
            return
        originals = {
            "os.unlink": _os.unlink,
            "os.remove": _os.remove,
            "Path.unlink": _pathlib.Path.unlink,
        }
        _UNLINK_GUARD_STATE["originals"] = originals

        def _guarded_os_unlink(path, *a, **k):
            try:
                assert_not_production_db(path, operation="unlink")
            except ProductionDBWriteAttemptError:
                raise
            except Exception:  # noqa: BLE001
                pass
            return originals["os.unlink"](path, *a, **k)

        def _guarded_os_remove(path, *a, **k):
            try:
                assert_not_production_db(path, operation="os.remove")
            except ProductionDBWriteAttemptError:
                raise
            except Exception:  # noqa: BLE001
                pass
            return originals["os.remove"](path, *a, **k)

        _real_path_unlink = originals["Path.unlink"]

        def _guarded_path_unlink(self, missing_ok=False):
            try:
                assert_not_production_db(self, operation="Path.unlink")
            except ProductionDBWriteAttemptError:
                raise
            except Exception:  # noqa: BLE001
                pass
            return _real_path_unlink(self, missing_ok=missing_ok)

        _os.unlink = _guarded_os_unlink
        _os.remove = _guarded_os_remove
        _pathlib.Path.unlink = _guarded_path_unlink
        _UNLINK_GUARD_STATE["installed"] = True


def dispatcher_db_is_production_bound() -> bool:
    """True iff ``dispatcher.db.DB_PATH`` currently resolves to the
    production DB identity. The pytest fail-closed gate asserts this is
    False at collection boundaries."""
    try:
        import dispatcher.db as ddb

        return _identity_matches(canonicalize(ddb.DB_PATH))
    except Exception:  # noqa: BLE001 — import-time failure is not a hazard
        return False


# ---------------------------------------------------------------------------
# Verification-mode wiring (notification contract integration)
# ---------------------------------------------------------------------------


def enter_verification_mode() -> dict:
    """Arm the DB guard for this process (verification-mode contract).

    Marks the process as a verification context (so every guarded
    operation fails closed against the production path) and rebinds an
    untouched production DB_PATH to a unique temp DB. Intended to be
    called by ``scripts/a2_runner.py`` / ``scripts/a2_smoke.py`` and by
    the pytest session BEFORE test modules import.
    """
    os.environ.setdefault("AEE_BRIDGE_VERIFICATION", "1")
    try:
        path = apply_default_test_db_override()
    except Exception:  # noqa: BLE001 — the marker itself must land
        path = None
    return {"verification_env": True, "temp_db": str(path) if path else None}


def exit_verification_mode() -> None:
    """No-op: verification mode has no persistent undo for DB paths.

    Kept for contract symmetry with
    ``aee._notification_guard.exit_verification_mode`` — a verification
    process terminates after its run; the allocated tempdirs are
    removed by :func:`cleanup_temp_dbs`.
    """
    return None


@contextmanager
def verification_mode() -> Iterator[dict]:
    """Context-manager form of :func:`enter_verification_mode`."""
    snap = enter_verification_mode()
    try:
        yield snap
    finally:
        exit_verification_mode()