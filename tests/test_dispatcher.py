"""Tests for the Task Dispatcher.

Run:
    cd ~/hermes-runtime-bridge
    .venv/bin/python -m pytest tests/test_dispatcher.py -v
or:
    .venv/bin/python tests/test_dispatcher.py

The test suite uses a *fresh* in-memory-style DB built in a
tempdir (idempotent), so it never touches the production
``data/dispatcher.db`` (the live bridge is running and holds
the file open; touching the production path is unsafe —
see AEE_MASTER_PLAN §A.7.15).
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

# Make the bridge root importable.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# AEE-7.6: use the live-DB safety guard so the test never touches
# the production ``data/dispatcher.db``. The helper yields a
# tempdir copy of the production schema (no row data) and rebinds
# ``dispatcher.db`` + ``dispatcher.manager`` to point at it. The
# canonical unlink at module load is now against the tempdir
# copy, not the live DB.
import importlib.util
import tempfile
_TMPDIR = tempfile.mkdtemp(prefix="aee76-dispatcher-test-")
_guard_spec = importlib.util.spec_from_file_location(
    "_aee76_live_db_guard", _ROOT / "tests" / "_live_db_guard.py"
)
_guard_mod = importlib.util.module_from_spec(_guard_spec)
_guard_spec.loader.exec_module(_guard_mod)

# Enter the point_module_to_temp_db context. The rebinding must
# persist for the entire lifetime of the test module (we are
# ABOUT to import dispatcher.db and dispatcher.manager, which
# bind module-level constants at import time). The exit from
# the with-block below would restore the production paths; we
# keep the context open for the rest of the module. The tempdir
# is removed on interpreter exit via the atexit hook below.
_TEMP_DB_PATH = Path(_TMPDIR) / "dispatcher.db"
_point_ctx = _guard_mod.point_module_to_temp_db(_TEMP_DB_PATH)
_db, _mgr = _point_ctx.__enter__()

# AEE-7.6: register a process-exit hook to (a) restore the
# production DB_PATH (so the in-process rebinding doesn't leak
# to a sibling test module loaded later in the same process)
# and (b) remove the tempdir. This is the only way to keep
# the tempdir out of /tmp after the test run.
import atexit as _atexit

def _aee76_cleanup_dispatcher_test() -> None:
    try:
        _point_ctx.__exit__(None, None, None)
    except Exception:
        pass
    import shutil as _sh
    with __import__("contextlib").suppress(OSError):
        if Path(_TMPDIR).exists():
            _sh.rmtree(_TMPDIR, ignore_errors=True)

_atexit.register(_aee76_cleanup_dispatcher_test)

# Reset logs / reports directories for a clean slate. We sweep
# the tempdir's logs/reports rather than the bridge root's, so
# the test never touches production logs.
for d in (Path(_TMPDIR) / "logs", Path(_TMPDIR) / "reports"):
    if d.exists():
        for child in d.iterdir():
            if child.is_file():
                child.unlink()

from dispatcher.ids import next_task_id  # noqa: E402
from dispatcher.manager import (  # noqa: E402
    IllegalTransition,
    TaskManager,
    TaskNotFound,
)
from dispatcher.models import is_legal_transition, is_legal_progress, LEGAL_PROGRESS_PCTS  # noqa: E402
from dispatcher.progress import next_pct_hint, validate_progress, monotonic  # noqa: E402

import dispatcher.manager as _mgr  # noqa: E402

# Default LOGS_DIR / REPORTS_DIR (based on the tempdir, not the
# bridge root). The conftest in tests/ will clean up the tempdir
# on session teardown.
_DEFAULT_LOGS_DIR = Path(_TMPDIR) / "logs"
_DEFAULT_REPORTS_DIR = Path(_TMPDIR) / "reports"


def _reset_manager_paths() -> None:
    _mgr.LOGS_DIR = _DEFAULT_LOGS_DIR
    _mgr.REPORTS_DIR = _DEFAULT_REPORTS_DIR


# Run the reset eagerly at module load as well, in case a previous
# test module's import-time mutation had already landed.
_reset_manager_paths()


class TestIdGenerator(unittest.TestCase):
    def test_consecutive_ids_after_insert_are_unique(self):
        # First create a real row so next_task_id sees a non-empty table.
        m = TaskManager()
        t1 = m.create(title="x", type="normal", input_text="a")
        t2 = m.create(title="y", type="normal", input_text="b")
        self.assertNotEqual(t1.task_id, t2.task_id)
        # The second must be +1 over the first.
        n1 = int(t1.task_id.rsplit("-", 1)[1])
        n2 = int(t2.task_id.rsplit("-", 1)[1])
        self.assertEqual(n2, n1 + 1)

    def test_format(self):
        m = TaskManager()
        t = m.create(title="fmt", type="normal", input_text="z")
        self.assertRegex(t.task_id, r"^TASK-\d{8}-\d{4}$")


class TestStateMachine(unittest.TestCase):
    def test_legal_transitions(self):
        self.assertTrue(is_legal_transition("pending", "queued"))
        self.assertTrue(is_legal_transition("queued", "running"))
        self.assertTrue(is_legal_transition("running", "completed"))
        self.assertTrue(is_legal_transition("running", "failed"))
        self.assertTrue(is_legal_transition("running", "cancelled"))
        self.assertTrue(is_legal_transition("failed", "queued"))  # retry
        # Illegal
        self.assertFalse(is_legal_transition("completed", "running"))
        self.assertFalse(is_legal_transition("cancelled", "running"))
        self.assertFalse(is_legal_transition("running", "pending"))


class TestProgress(unittest.TestCase):
    def test_legal_pcts(self):
        for p in (0, 5, 10, 25, 40, 60, 80, 95, 100):
            self.assertTrue(is_legal_progress(p))
        for p in (1, 7, 15, 33, 50, 70, 99):
            self.assertFalse(is_legal_progress(p))

    def test_monotonic_helper(self):
        self.assertIsNone(monotonic(40, 60))
        self.assertIsNotNone(monotonic(60, 25))
        self.assertIsNone(monotonic(60, 60))

    def test_validate_raises(self):
        with self.assertRaises(ValueError):
            validate_progress(7)
        validate_progress(80)  # no raise

    def test_hint_progresses(self):
        # 5% of budget: suggest 5 (or 10 with output)
        self.assertEqual(next_pct_hint(0, 30, 900, has_output=False), 5)
        self.assertEqual(next_pct_hint(0, 30, 900, has_output=True), 10)
        # 60% of budget: hits the >= 0.60 branch -> 60 (or 80 with output)
        self.assertEqual(next_pct_hint(0, 540, 900, has_output=False), 60)
        self.assertEqual(next_pct_hint(0, 540, 900, has_output=True), 80)
        # 40% of budget: hits the >= 0.40 branch -> 40
        self.assertEqual(next_pct_hint(0, 360, 900, has_output=False), 40)


class TestTaskLifecycle(unittest.TestCase):
    def setUp(self):
        # Make sure the per-task log path is the canonical one,
        # even if some other test module mutated ``mgr.LOGS_DIR``
        # at import time. The mutation is module-level and
        # order-dependent; restoring it here is the cheapest way
        # to keep the lifecycle tests order-independent.
        _reset_manager_paths()
        self.m = TaskManager()

    def test_create_returns_task(self):
        t = self.m.create(title="t", type="research", input_text="hi")
        self.assertEqual(t.status, "queued")
        self.assertEqual(t.progress_pct, 5)
        self.assertIsNone(t.started_at)

    def test_start_progress_complete(self):
        t = self.m.create(title="t", type="research", input_text="hi")
        self.m.start(t.task_id, "hr-1")
        self.m.progress(t.task_id, 25, "a")
        self.m.progress(t.task_id, 100, "done")
        self.m.complete(t.task_id, output_text="ok", usage={"p": 1}, raw={"r": "hr-1"})
        g = self.m.get_or_raise(t.task_id)
        self.assertEqual(g.status, "completed")
        self.assertEqual(g.progress_pct, 100)
        self.assertIsNotNone(g.finished_at)
        self.assertIsNotNone(g.duration_sec)
        out = self.m.get_output(t.task_id)
        self.assertEqual(out["output_text"], "ok")
        self.assertEqual(out["usage"]["p"], 1)

    def test_illegal_double_start(self):
        t = self.m.create(title="t", type="normal", input_text="x")
        self.m.start(t.task_id, "hr")
        with self.assertRaises(IllegalTransition):
            self.m.start(t.task_id, "hr2")

    def test_progress_regression(self):
        t = self.m.create(title="t", type="normal", input_text="x")
        self.m.start(t.task_id, "hr")
        self.m.progress(t.task_id, 60, "mid")
        with self.assertRaises(ValueError):
            self.m.progress(t.task_id, 25, "back")

    def test_retry_creates_new_task(self):
        t = self.m.create(title="t", type="normal", input_text="x")
        self.m.start(t.task_id, "hr")
        self.m.fail(t.task_id, "boom")
        retried = self.m.retry(t.task_id)
        self.assertNotEqual(retried.task_id, t.task_id)
        self.assertEqual(retried.retry_count, 1)
        self.assertIn("[retry]", retried.title)

    def test_find_by_hermes_run_id(self):
        t = self.m.create(title="t", type="normal", input_text="x")
        self.m.start(t.task_id, "hr-xyz")
        found = self.m.find_by_hermes_run_id("hr-xyz")
        self.assertIsNotNone(found)
        self.assertEqual(found.task_id, t.task_id)

    def test_list_with_filter(self):
        self.m.create(title="a", type="research", input_text="1")
        self.m.create(title="b", type="ops", input_text="2")
        only_research = self.m.list(type="research", limit=10)
        self.assertTrue(all(t.type == "research" for t in only_research))
        self.assertGreaterEqual(len(only_research), 1)

    def test_events_recorded(self):
        t = self.m.create(title="t", type="normal", input_text="x")
        self.m.start(t.task_id, "hr")
        self.m.complete(t.task_id, output_text="done")
        events = self.m.events(t.task_id)
        kinds = [e.kind for e in events]
        for needed in ("created", "queued", "started", "completed"):
            self.assertIn(needed, kinds, f"missing event: {needed}")

    def test_log_file_written(self):
        # Per the module-level fixture above and the
        # ``TestTaskLifecycle.setUp`` reset, ``mgr.LOGS_DIR`` is
        # guaranteed to point at ``_DEFAULT_LOGS_DIR`` (the tempdir
        # ``logs``) for this test — even if a previous test module
        # mutated it at import time. The contract: a freshly-created
        # task has a non-empty per-task log file under
        # ``_DEFAULT_LOGS_DIR / {task_id}.log``.
        t = self.m.create(title="t", type="normal", input_text="x")
        log_path = _DEFAULT_LOGS_DIR / f"{t.task_id}.log"
        self.assertTrue(log_path.exists(), f"log not written: {log_path}")
        self.assertGreater(log_path.stat().st_size, 0)

    def test_task_json_written_on_complete(self):
        t = self.m.create(title="t", type="research", input_text="x")
        self.m.start(t.task_id, "hr")
        self.m.complete(t.task_id, output_text="result", raw={"k": "v"})
        report_dir = _DEFAULT_REPORTS_DIR / t.task_id
        self.assertTrue(report_dir.exists())
        self.assertTrue((report_dir / "task.json").exists())


class TestManagerPathsOrderIndependence(unittest.TestCase):
    """Regression test for the test_log_file_written baseline.

    Why this test exists
    --------------------
    ``dispatcher.manager.LOGS_DIR`` and ``REPORTS_DIR`` are
    module-level mutable attributes. ``test_jobs_api`` (and
    ``test_workers_api``) re-assign them to per-test tmpdirs
    at *import* time to isolate their own logs. If that
    mutation leaks into ``TestTaskLifecycle.test_log_file_written``,
    the per-task log file lands in a tmpdir while the assertion
    expects ``bridge_root/logs/{task_id}.log``, and the test
    fails.

    The fix lives in ``tests/test_dispatcher.py``: a module-level
    ``_reset_manager_paths()`` and a ``setUp`` call restore the
    canonical paths. This test simulates the import-order race by
    mutating the module attributes from inside the test, then
    constructs a ``TaskManager`` (matching the lifecycle setUp
    path) and verifies the per-task log file lands at the
    canonical path.
    """

    def setUp(self):
        # Make sure the canonical paths are in effect *before*
        # we measure — covers the case where another test module
        # mutated the attribute and we're the first to run.
        _reset_manager_paths()

    def test_log_file_lands_in_canonical_logs_dir_after_external_mutation(self):
        # Simulate the leak: another test module mutated the
        # module attribute at import time.
        bogus = _DEFAULT_LOGS_DIR / "should_not_be_used_by_this_test"
        _mgr.LOGS_DIR = bogus
        _mgr.REPORTS_DIR = _DEFAULT_REPORTS_DIR / "should_not_be_used_by_this_test"
        try:
            # Mimic the setUp reset.
            _reset_manager_paths()
            mgr = TaskManager()
            t = mgr.create(title="t", type="normal", input_text="x")
            log_path = _DEFAULT_LOGS_DIR / f"{t.task_id}.log"
            self.assertTrue(
                log_path.exists(),
                f"log not written to canonical path: {log_path}",
            )
            self.assertGreater(log_path.stat().st_size, 0)
            # The bogus dir must NOT have been created.
            self.assertFalse(
                bogus.exists(),
                f"log leaked into bogus path: {bogus}",
            )
        finally:
            # Restore the canonical paths for any subsequent test
            # in this process.
            _reset_manager_paths()


class TestResearchModules(unittest.TestCase):
    def test_prompts_load(self):
        from research.prompts import list_prompts, load, latest_version
        all_p = list_prompts()
        self.assertGreaterEqual(len(all_p), 1)
        body = load("macro")
        self.assertGreater(len(body), 50)
        self.assertIsNotNone(latest_version("macro"))

    def test_config_loads(self):
        from config import ensure_defaults, load
        ensure_defaults()
        for name in ("scheduler", "report", "research", "model"):
            data = load(name)
            self.assertIsInstance(data, dict)
            self.assertGreater(len(data), 0)

    def test_repo_scan(self):
        from research.repo import scan
        data = scan(str(_ROOT))
        self.assertIn("file_count", data)
        self.assertIn("by_class", data)
        self.assertGreater(data["file_count"], 0)

    def test_report_render(self):
        from research.report import render, template_sections, SECTIONS
        secs = template_sections()
        secs["Executive Summary"] = "Test summary."
        path = render("TASK-UNIT-TEST", "Unit Test", "research", secs)
        self.assertTrue(path.exists())
        text = path.read_text(encoding="utf-8")
        for s in SECTIONS:
            self.assertIn(s, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
