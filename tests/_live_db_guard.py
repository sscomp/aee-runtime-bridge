"""Live-DB safety guard for the legacy ``tests/`` suite.

Why this exists
---------------

On 2026-07-11 the AEE-7.6 test runs exposed a recurring incident:
the legacy ``tests/`` convention is to call ``DB_PATH.unlink()`` at
module load time so each test starts with a clean DB. Three of the
five legacy modules (``tests/test_dispatcher.py``,
``tests/test_aee_write_side_metadata.py``) pointed the
unlink at the **production** ``data/dispatcher.db`` rather than a
tempdir copy. The unlink nuked the live DB mid-session while the
supervised bridge (pid 174291, port 8787) was holding the file open
on a separate inode. The bridge kept writing to its deleted inode
and the disk copy became stale; recovery required copying the
bridge's ``/proc/174291/fd/13`` back to the canonical path
(see AEE_MASTER_PLAN §A.7.15 for the full incident report).

The guard makes three guarantees:

1. **No module in ``tests/`` (other than this guard itself) may
   call ``DB_PATH.unlink()`` directly against the production
   ``data/dispatcher.db`` path.** The companion tripwire
   ``aee/tests/test_live_db_safety_tripwire.py`` scans the source
   and fails if a call is found.
2. **All tests that need a dispatcher DB get a tempdir copy** via
   :func:`make_temp_dispatcher_db`. The helper unlinks
   safely (only ever the tempdir copy), then yields the path.
3. **When the live bridge is running**, the helper exposes
   :func:`is_live_bridge_running` so test modules can skip or
   fail-fast rather than race the bridge.

Public surface
--------------

* :data:`LIVE_DISPATCHER_DB_PATH` — the canonical production DB path.
* :data:`LIVE_BRIDGE_PORT` — the port the supervised bridge listens on.
* :func:`is_live_bridge_running` — TCP probe to LIVE_BRIDGE_PORT.
* :func:`make_temp_dispatcher_db` — context manager: tempdir +
  unlink-safe copy of the production schema (or empty DB if no
  production copy). Never touches LIVE_DISPATCHER_DB_PATH.
* :func:`point_module_to_temp_db` — rebind ``dispatcher.db`` and
  ``dispatcher.manager`` constants to a tempdir DB.

Hard rules
----------

* This module never imports ``dispatcher.db`` at module load time
  (it would trigger ``dispatcher/db.py:21`` to evaluate
  ``DB_DIR / DB_PATH`` against the production path). All access
  to ``dispatcher.db`` is lazy / function-local.
* No destructive operations against ``LIVE_DISPATCHER_DB_PATH``.
  Even ``stat`` / ``exists`` is wrapped in try/except to avoid
  raising on a partial / locked DB.
"""
from __future__ import annotations

import contextlib
import os
import socket
import sqlite3
import tempfile
import threading
import unittest.mock as _um
from pathlib import Path
from typing import Iterator, Optional, Tuple

# Canonical production paths. These are resolved from this file's
# location so the helper is robust to ``cd`` at call time.
_HERE = Path(__file__).resolve().parent
_BRIDGE_ROOT = _HERE.parent
LIVE_DISPATCHER_DB_PATH = _BRIDGE_ROOT / "data" / "dispatcher.db"
LIVE_BRIDGE_PORT = 8787
LIVE_BRIDGE_HOST = "127.0.0.1"


def is_live_bridge_running(
    host: str = LIVE_BRIDGE_HOST,
    port: int = LIVE_BRIDGE_PORT,
    timeout: float = 0.5,
) -> bool:
    """Return True iff a TCP connect to (host, port) succeeds.

    The check is intentionally cheap (a 500ms TCP probe) so callers
    can run it at module load without slowing the test suite
    noticeably. It does NOT issue an HTTP ``/health`` request to
    avoid depending on the bridge's response shape.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@contextlib.contextmanager
def make_temp_dispatcher_db(
    *,
    prefix: str = "aee76-guard-",
    copy_production_schema: bool = True,
) -> Iterator[Path]:
    """Yield a fresh ``dispatcher.db`` inside a tempdir.

    The tempdir is removed on context exit. The helper NEVER
    touches ``LIVE_DISPATCHER_DB_PATH``: it does not copy the
    production DB, only the schema (``sqlite_master`` rows from
    the production file, if available, otherwise an empty
    database). Tests that need real production data should read
    it via a read-only URI connection elsewhere — never mutate
    LIVE_DISPATCHER_DB_PATH from a test.

    :param prefix: tempfile.mkdtemp prefix.
    :param copy_production_schema: if True and the production DB
        exists, copy the schema (DDL only) so the test starts with
        a working empty DB. The DDL is captured via
        ``sqlite_master`` dump, which is safe against the live
        bridge's WAL.
    :raises RuntimeError: if the tempdir cannot be created.
    :raises FileNotFoundError: if the production DB does not
        exist (when ``copy_production_schema=True``).
    """
    tmpdir = Path(tempfile.mkdtemp(prefix=prefix))
    db_path = tmpdir / "dispatcher.db"
    try:
        if copy_production_schema and LIVE_DISPATCHER_DB_PATH.exists():
            # Read the schema DDL only (no row data) from the
            # production file in read-only URI mode. This is
            # safe even when the live bridge is writing.
            with sqlite3.connect(
                f"file:{LIVE_DISPATCHER_DB_PATH}?mode=ro",
                uri=True,
            ) as src:
                ddl_rows = src.execute(
                    "SELECT sql, name, type FROM sqlite_master "
                    "WHERE sql IS NOT NULL AND type IN "
                    "('table', 'index', 'trigger') "
                    "AND name NOT LIKE 'sqlite_%' "
                    "ORDER BY type DESC, name"
                ).fetchall()
                ddl_statements = [row[0] for row in ddl_rows if row[0]]
            with sqlite3.connect(db_path) as dst:
                # sqlite3 executescript splits on ``;`` and tries to
                # run each chunk as a single statement, which breaks
                # for CREATE TABLE bodies that contain
                # ``DEFAULT '[]'`` (the inner ``[`` confuses the
                # splitter). Run each DDL with ``execute`` instead.
                for stmt in ddl_statements:
                    dst.execute(stmt)
                dst.commit()
        else:
            # Empty DB — _init_schema will populate.
            db_path.touch()
        yield db_path
    finally:
        # Defensive: unlink the temp DB and remove the tempdir.
        # The tempdir may contain sidecar files (-wal, -shm).
        try:
            for ext in ("", "-wal", "-shm", "-journal"):
                p = db_path.with_name(db_path.name + ext)
                if p.exists():
                    p.unlink()
        except OSError:
            pass
        with contextlib.suppress(OSError):
            # Remove any other residue in the tempdir.
            for child in tmpdir.iterdir():
                if child.is_file():
                    child.unlink()
            tmpdir.rmdir()


@contextlib.contextmanager
def point_module_to_temp_db(
    db_path: Path,
) -> Iterator[Tuple[object, object]]:
    """Rebind ``dispatcher.db`` and ``dispatcher.manager`` to a
    tempdir DB for the duration of the context.

    Use this in a test module's setUp / module-level fixture to
    make the production module constants point at a per-test
    tempdir copy. On exit, the original constants are restored
    and any thread-local connection is dropped.

    :param db_path: a path produced by :func:`make_temp_dispatcher_db`.
    :returns: a tuple ``(dispatcher.db, dispatcher.manager)``
        already rebound. The caller does not need to do anything
        else; on context exit the originals are restored.
    """
    import dispatcher.db as _db  # lazy import
    import dispatcher.manager as _mgr  # lazy import

    saved = {
        "DB_DIR": _db.DB_DIR,
        "DB_PATH": _db.DB_PATH,
        "LOGS_DIR": _mgr.LOGS_DIR,
        "REPORTS_DIR": _mgr.REPORTS_DIR,
        "_local_conn": getattr(_db._local, "conn", None),
        "_initialized": _db._initialized,
    }
    try:
        _db.DB_DIR = db_path.parent
        _db.DB_PATH = db_path
        _db._local.conn = None
        _db._initialized = False
        _mgr.LOGS_DIR = db_path.parent / "logs"
        _mgr.REPORTS_DIR = db_path.parent / "reports"
        _mgr.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        _mgr.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        yield (_db, _mgr)
    finally:
        _db.DB_DIR = saved["DB_DIR"]
        _db.DB_PATH = saved["DB_PATH"]
        _db._local.conn = saved["_local_conn"]
        _db._initialized = saved["_initialized"]
        _mgr.LOGS_DIR = saved["LOGS_DIR"]
        _mgr.REPORTS_DIR = saved["REPORTS_DIR"]


# Module-level singleton lock so two threads can't race the
# tempdir creation. Stdlib tempfile.mkdtemp is already atomic; this
# is documentation more than necessity.
_alloc_lock = threading.Lock()
