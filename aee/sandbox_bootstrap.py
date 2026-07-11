"""AEE-7.6 sandbox bootstrap — runs in the child process BEFORE uvicorn.

The bridge's ``app.py`` reads ``dispatcher.db.DB_PATH`` at import
time as a module-level constant. To make the sandbox hermetic
(separate DB from the live bridge) we must mutate that constant
in the child process before any module reads it.

The bootstrap is intentionally minimal:

1. Read the sandbox env vars set by
   ``aee/runtime_bridge_sandbox.py::_build_sandbox_env``.
2. Patch ``dispatcher.db.DB_PATH`` (and friends) to point at
   the sandbox paths.
3. Hand off to ``uvicorn`` (via ``python -m uvicorn app:app``).

This module is imported by the child process; it is NOT used
by the live bridge. The live bridge keeps its existing
``data/dispatcher.db`` because this module is never on its
``PYTHONPATH``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _patch_module_db_path() -> None:
    """Mutate ``dispatcher.db.DB_PATH`` to the sandbox value.

    Imports the ``dispatcher.db`` module first to make sure the
    parent ``dispatcher`` package is initialized, then replaces
    the module-level constant. ``dispatcher.db.get_conn()``
    reads ``DB_PATH`` lazily on first call, so this is enough
    to redirect all subsequent DB I/O to the sandbox.
    """
    sandbox_db = os.environ.get("AEE_BRIDGE_DB_PATH")
    if not sandbox_db:
        return
    from dispatcher import db as dispatcher_db
    new_path = Path(sandbox_db)
    new_dir = new_path.parent
    new_dir.mkdir(parents=True, exist_ok=True)
    dispatcher_db.DB_DIR = new_dir
    dispatcher_db.DB_PATH = new_path
    # Reset the module-level connection cache so the next
    # get_conn() re-initializes with the new path. Without
    # this, the first connection would be opened against
    # the OLD (live) DB path.
    dispatcher_db._local.conn = None  # type: ignore[attr-defined]
    dispatcher_db._initialized = False  # type: ignore[attr-defined]


def _patch_reports_dir() -> None:
    """Ensure the sandbox reports dir exists.

    The bridge doesn't read REPORTS_DIR from env at import time
    (it's only used by the AEE reporting CLI), but we create
    it for the sandbox anyway so the AEE-7.5 G2 test fixture
    can drop a ``task.json`` in a known location.
    """
    sandbox_reports = os.environ.get("AEE_BRIDGE_REPORTS_DIR")
    if not sandbox_reports:
        return
    p = Path(sandbox_reports)
    p.mkdir(parents=True, exist_ok=True)


_patch_module_db_path()
_patch_reports_dir()
