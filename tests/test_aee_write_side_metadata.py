"""AEE-7.5 — Dispatcher write-side metadata integration tests.

Closes the §20.9.10 deferred limitation by verifying that
``TaskManager.create()`` accepts and stamps the
``executor_session_id`` kwarg, and that ``TaskManager.start()``
stamps the ``runtime_run_id`` alongside the existing
``hermes_run_id``. These are end-to-end tests against a
tempdir copy of the production schema (NOT the live
``data/dispatcher.db`` — the live bridge is running and
holding the file open; touching the production path is
unsafe, see AEE_MASTER_PLAN §A.7.15).

Why here, not in ``aee/tests/``?
--------------------------------
``aee/tests/`` modules are loaded by ``unittest discover`` at
test time and can interleave with sibling AEE test files that
share module-level singletons (``db.DB_PATH``,
``db._local.conn``, ``manager.LOGS_DIR``,
``manager.REPORTS_DIR``). Running a TaskManager against a
private tempdir from ``aee/tests/`` has proven to leak state
into sibling AEE test modules. The convention in this repo
is: AEE-domain tests live in ``aee/tests/``; dispatcher
hot-path integration tests live in ``tests/`` and use a
tempdir copy of the production schema. The new tests here
follow the latter convention.

See also: ``tests/test_dispatcher.py`` for the canonical
``TaskManager`` lifecycle test pattern.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# AEE-7.6: load the live-DB safety guard by file path (avoids
# the ``tests`` namespace collision with hermes-agent/tests).
_guard_spec = importlib.util.spec_from_file_location(
    "_aee76_live_db_guard", _ROOT / "tests" / "_live_db_guard.py"
)
_guard_mod = importlib.util.module_from_spec(_guard_spec)
_guard_spec.loader.exec_module(_guard_mod)

# Build a fresh tempdir copy of the production schema, then
# permanently rebind ``dispatcher.db`` + ``dispatcher.manager``
# to the tempdir copy for the rest of this module's lifetime.
_TMPDIR = tempfile.mkdtemp(prefix="aee76-write-side-metadata-test-")
_TEMP_DB_PATH = Path(_TMPDIR) / "dispatcher.db"
_point_ctx = _guard_mod.point_module_to_temp_db(_TEMP_DB_PATH)
_db, _mgr = _point_ctx.__enter__()

from dispatcher.db import get_conn  # noqa: E402
from dispatcher.manager import TaskManager  # noqa: E402

# AEE-7.6: register a process-exit hook to (a) restore the
# production DB_PATH (so the in-process rebinding doesn't leak
# to a sibling test module loaded later in the same process)
# and (b) remove the tempdir. This is the only way to keep
# the tempdir out of /tmp after the test run.
import atexit as _atexit

def _aee76_cleanup_write_side_test() -> None:
    try:
        _point_ctx.__exit__(None, None, None)
    except Exception:
        pass
    import shutil as _sh
    with __import__("contextlib").suppress(OSError):
        if Path(_TMPDIR).exists():
            _sh.rmtree(_TMPDIR, ignore_errors=True)

_atexit.register(_aee76_cleanup_write_side_test)


class TestCreateStampsExecutorSessionId(unittest.TestCase):
    """``create()`` accepts an ``executor_session_id`` kwarg,
    stores it on the row, and emits it in the event payload —
    but only when it's non-empty (compact log policy).
    """

    def setUp(self) -> None:
        self.m = TaskManager()

    def test_create_stores_executor_session_id(self) -> None:
        t = self.m.create(
            title="aee75-store",
            type="normal",
            input_text="hello",
            executor_session_id="caller-xyz",
        )
        self.assertEqual(t.executor_session_id, "caller-xyz")
        row = get_conn().execute(
            "SELECT executor_session_id FROM tasks WHERE task_id = ?",
            (t.task_id,),
        ).fetchone()
        self.assertEqual(row["executor_session_id"], "caller-xyz")

    def test_create_strips_whitespace_to_none(self) -> None:
        t = self.m.create(
            title="aee75-ws",
            type="normal",
            input_text="hello",
            executor_session_id="   ",
        )
        self.assertIsNone(t.executor_session_id)
        row = get_conn().execute(
            "SELECT executor_session_id FROM tasks WHERE task_id = ?",
            (t.task_id,),
        ).fetchone()
        self.assertIsNone(row["executor_session_id"])

    def test_create_empty_string_becomes_none(self) -> None:
        t = self.m.create(
            title="aee75-empty",
            type="normal",
            input_text="hello",
            executor_session_id="",
        )
        self.assertIsNone(t.executor_session_id)

    def test_create_default_kwarg_is_none(self) -> None:
        t = self.m.create(
            title="aee75-default",
            type="normal",
            input_text="hello",
        )
        self.assertIsNone(t.executor_session_id)


class TestStartStampsRuntimeRunId(unittest.TestCase):
    """``start()`` stamps ``runtime_run_id`` alongside the
    existing ``hermes_run_id`` (same value, runtime-neutral
    alias), and includes it in the started event payload.
    """

    def setUp(self) -> None:
        self.m = TaskManager()

    def test_start_stamps_runtime_run_id(self) -> None:
        t = self.m.create(
            title="aee75-start",
            type="normal",
            input_text="hello",
        )
        self.m.start(t.task_id, hermes_run_id="run-abc")
        row = get_conn().execute(
            "SELECT status, hermes_run_id, runtime_run_id "
            "FROM tasks WHERE task_id = ?",
            (t.task_id,),
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["hermes_run_id"], "run-abc")
        self.assertEqual(row["runtime_run_id"], "run-abc")

    def test_start_emits_runtime_run_id_in_event(self) -> None:
        t = self.m.create(
            title="aee75-event",
            type="normal",
            input_text="hello",
        )
        self.m.start(t.task_id, hermes_run_id="run-xyz")
        ev = get_conn().execute(
            "SELECT payload_json FROM task_events "
            "WHERE task_id = ? AND kind = 'started'",
            (t.task_id,),
        ).fetchone()
        self.assertIsNotNone(ev)
        payload = json.loads(ev["payload_json"])
        self.assertEqual(payload.get("runtime_run_id"), "run-xyz")
        self.assertEqual(payload.get("hermes_run_id"), "run-xyz")


if __name__ == "__main__":
    unittest.main()
