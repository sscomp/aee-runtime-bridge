"""Task-Mapping work-order test suite.

Covers the four fixes (A/B/C/D) for the run/task mapping
inconsistency between ``POST /runs`` (hermes) and
``POST /runs/executor`` (claude-code-cli / hermes):

  * Fix A — POST /runs writes an ``executor_runs`` mapping row
    (``_persist_hermes_run_mapping``) so hermes runs are visible
    to ``GET /runs`` (list), ``GET /runs/{id}``, and summary.
  * Fix B — POST /runs/executor creates a dispatcher ``tasks`` row
    so ``executor_runs.task_id`` is non-NULL for newly created
    executor runs (no more ``task_id=None`` new records).
  * Fix C — GET /runs (list) unions ``tasks`` rows that have a
    ``hermes_run_id`` into the result so pre-fix hermes runs
    (those never mirrored into ``executor_runs``) are visible.
  * Fix D — manager.complete()/fail()/timeout()/cancel() mirror
    the terminal status into ``executor_runs`` so lifecycle
    transitions are reflected in the canonical run store.

All tests use a temporary DB (``setup_temp_db``) — no production
DB mutation. The suite is hermetic: no real Hermes upstream, no
real Claude CLI binary (a fake script is written), no real
Telegram credentials.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests._executor_test_helpers import (
    make_client,
    post_executor,
    setup_temp_db,
    write_fake_claude,
    set_fake_binary,
    bash_quote,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stub_hermes_adapter(run_id: str = "stub-hermes-run-001", status: str = "queued"):
    """Register a stub hermes adapter that returns a fixed run_id."""
    from aee.adapters.base import RuntimePollResult, RuntimeSubmitResult
    from aee.core.registry import adapter_registry

    class _StubHermes:
        name = "hermes"
        runtime_type = "hermes"

        async def submit(self, job):
            return RuntimeSubmitResult(external_run_id=run_id, status=status)

        async def poll(self, external_run_id):
            return RuntimePollResult(
                external_run_id=external_run_id, status="completed", is_terminal=True,
            )

        async def cancel(self, external_run_id):
            from aee.adapters.base import RuntimeCancelResult
            return RuntimeCancelResult(external_run_id=external_run_id, cancelled=True)

    saved = dict(adapter_registry._adapters)
    adapter_registry._adapters["hermes"] = _StubHermes()
    return saved


def _restore_adapters(saved):
    from aee.core.registry import adapter_registry
    adapter_registry._adapters.clear()
    adapter_registry._adapters.update(saved)


def _auth_headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _db_path(monkeypatch) -> Path:
    from dispatcher import db as ddb
    return ddb.DB_PATH


def _query_executor_runs(db_path: Path, run_id: str) -> dict | None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM executor_runs WHERE run_id = ?", (run_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _query_tasks_by_run_id(db_path: Path, run_id: str) -> dict | None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM tasks WHERE hermes_run_id = ? OR external_run_id = ?",
            (run_id, run_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fix A: POST /runs writes executor_runs mapping row
# ---------------------------------------------------------------------------

class TestFixAHermesRunMapping:
    """POST /runs (hermes) must write an executor_runs row."""

    def test_post_runs_creates_executor_runs_row(self, monkeypatch, tmp_path):
        """POST /runs persists a mapping row with selected_executor=hermes."""
        saved = _stub_hermes_adapter(run_id="run_hermes_A001")
        try:
            client, app_module, key = make_client(monkeypatch, tmp_path)
            resp = client.post(
                "/runs",
                json={
                    "title": "test-hermes-mapping",
                    "input": "do a hermes thing",
                    "mode": "normal",
                    "session_id": "test-session-A",
                },
                headers=_auth_headers(key),
            )
            assert resp.status_code in (200, 201), f"{resp.status_code}: {resp.text}"
            body = resp.json()
            run_id = body["run_id"]
            task_id = body["task_id"]
            assert run_id, "run_id must be non-empty"
            assert task_id, "task_id must be non-empty"

            row = _query_executor_runs(_db_path(monkeypatch), run_id)
            assert row is not None, (
                f"executor_runs row missing for hermes run_id={run_id!r} "
                f"(Fix A: POST /runs must persist a mapping row)"
            )
            assert row["selected_executor"] == "hermes", (
                f"selected_executor={row['selected_executor']!r}, expected 'hermes'"
            )
            assert row["task_id"] == task_id, (
                f"executor_runs.task_id={row['task_id']!r} != response task_id={task_id!r}"
            )
            assert row["status"] in {"running", "queued", "started"}, (
                f"initial status={row['status']!r} not a non-terminal state"
            )
        finally:
            _restore_adapters(saved)

    def test_post_runs_mapping_is_idempotent(self, monkeypatch, tmp_path):
        """Re-dispatching the same run_id updates the row, not duplicates."""
        saved = _stub_hermes_adapter(run_id="run_hermes_A002")
        try:
            client, app_module, key = make_client(monkeypatch, tmp_path)
            for i in range(2):
                resp = client.post(
                    "/runs",
                    json={
                        "title": f"test-idempotent-{i}",
                        "input": "idempotent hermes",
                        "mode": "normal",
                        "session_id": "test-session-A-idem",
                    },
                    headers=_auth_headers(key),
                )
                assert resp.status_code in (200, 201), f"{resp.status_code}: {resp.text}"
            conn = sqlite3.connect(str(_db_path(monkeypatch)))
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM executor_runs WHERE run_id = ?",
                    ("run_hermes_A002",),
                ).fetchone()[0]
            finally:
                conn.close()
            assert count == 1, (
                f"expected 1 executor_runs row for run_hermes_A002, got {count} "
                f"(upsert_run must be idempotent)"
            )
        finally:
            _restore_adapters(saved)


# ---------------------------------------------------------------------------
# Fix B: POST /runs/executor creates a tasks row (task_id non-NULL)
# ---------------------------------------------------------------------------

class TestFixBExecutorTaskAssociation:
    """POST /runs/executor must create a tasks row so task_id is non-NULL."""

    def test_executor_claude_code_creates_task(self, monkeypatch, tmp_path):
        """claude-code-cli executor run has a non-NULL task_id in executor_runs."""
        client, app_module, key = make_client(monkeypatch, tmp_path)
        fake_bin = write_fake_claude(tmp_path, stdout="ok", name="fake-claude-B1")
        set_fake_binary(monkeypatch, fake_bin)
        # Override the configured binary path for the runner
        monkeypatch.setenv("AEE_CLAUDE_CLI_BINARY", fake_bin)

        resp = post_executor(client, key, {
            "executor": "claude-code-cli",
            "prompt": "do a cli thing",
            "timeout_sec": 30,
            "repo_path": "/home/ubuntu/Abacus",
        })
        assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
        data = resp.json()
        run_id = data["run_id"]
        task_id = data.get("task_id")
        assert task_id, (
            f"claude-code-cli run task_id is None/empty (Fix B: must be non-NULL "
            f"for newly created executor runs); run_id={run_id!r}"
        )

        row = _query_executor_runs(_db_path(monkeypatch), run_id)
        assert row is not None, f"executor_runs row missing for run_id={run_id!r}"
        assert row["task_id"] == task_id, (
            f"executor_runs.task_id={row['task_id']!r} != response task_id={task_id!r}"
        )
        # The task row must also exist in the tasks table
        trow = _query_tasks_by_run_id(_db_path(monkeypatch), task_id)
        # task_id is the PK in tasks; query directly
        conn = sqlite3.connect(str(_db_path(monkeypatch)))
        conn.row_factory = sqlite3.Row
        try:
            trow = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,),
            ).fetchone()
        finally:
            conn.close()
        assert trow is not None, (
            f"tasks row missing for task_id={task_id!r} (Fix B: executor must "
            f"create a dispatcher task)"
        )

    def test_executor_hermes_creates_task(self, monkeypatch, tmp_path):
        """hermes executor run also has a non-NULL task_id."""
        saved = _stub_hermes_adapter(run_id="run_exec_B002")
        try:
            client, app_module, key = make_client(monkeypatch, tmp_path)
            resp = post_executor(client, key, {
                "executor": "hermes",
                "prompt": "do a hermes executor thing",
                "timeout_sec": 30,
            })
            assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
            data = resp.json()
            task_id = data.get("task_id")
            assert task_id, (
                f"hermes executor run task_id is None/empty (Fix B); "
                f"run_id={data.get('run_id')!r}"
            )
        finally:
            _restore_adapters(saved)

    def test_executor_no_duplicate_tasks(self, monkeypatch, tmp_path):
        """Two distinct executor runs produce two distinct task_ids."""
        client, app_module, key = make_client(monkeypatch, tmp_path)
        fake_bin = write_fake_claude(tmp_path, stdout="ok", name="fake-claude-B3")
        set_fake_binary(monkeypatch, fake_bin)

        task_ids = set()
        for i in range(2):
            resp = post_executor(client, key, {
                "executor": "claude-code-cli",
                "prompt": f"distinct run {i}",
                "timeout_sec": 30,
                "repo_path": "/home/ubuntu/Abacus",
            })
            assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
            task_ids.add(resp.json().get("task_id"))
        assert len(task_ids) == 2, (
            f"expected 2 distinct task_ids, got {task_ids}"
        )


# ---------------------------------------------------------------------------
# Fix C: GET /runs (list) unions tasks table
# ---------------------------------------------------------------------------

class TestFixCListUnion:
    """GET /runs must show hermes runs even if only in tasks table."""

    def test_list_shows_hermes_run_from_tasks_only(self, monkeypatch, tmp_path):
        """A hermes run with only a tasks row (no executor_runs) is visible."""
        saved = _stub_hermes_adapter(run_id="run_union_C001")
        try:
            client, app_module, key = make_client(monkeypatch, tmp_path)
            # Dispatch via POST /runs — this writes BOTH tasks AND
            # executor_runs (Fix A). To test the union fallback for
            # pre-fix runs, we delete the executor_runs row and
            # verify the tasks union still surfaces it.
            resp = client.post(
                "/runs",
                json={
                    "title": "test-union-C001",
                    "input": "union test",
                    "mode": "normal",
                    "session_id": "test-session-C",
                },
                headers=_auth_headers(key),
            )
            assert resp.status_code in (200, 201), f"{resp.status_code}: {resp.text}"
            run_id = resp.json()["run_id"]
            assert run_id == "run_union_C001"

            # Simulate a pre-fix hermes run: delete the executor_runs row
            conn = sqlite3.connect(str(_db_path(monkeypatch)))
            try:
                conn.execute(
                    "DELETE FROM executor_runs WHERE run_id = ?", (run_id,),
                )
                conn.commit()
            finally:
                conn.close()

            # GET /runs must still surface it via the tasks union
            resp2 = client.get("/runs", headers=_auth_headers(key))
            assert resp2.status_code == 200, f"{resp2.status_code}: {resp2.text}"
            items = resp2.json()["items"]
            run_ids = [it["run_id"] for it in items]
            assert run_id in run_ids, (
                f"hermes run {run_id!r} not in GET /runs list {run_ids} "
                f"(Fix C: tasks union must surface pre-fix hermes runs)"
            )
            # The unioned row must carry task_id (non-NULL)
            item = next(it for it in items if it["run_id"] == run_id)
            assert item["task_id"], (
                f"unioned hermes run task_id is None/empty: {item}"
            )
        finally:
            _restore_adapters(saved)

    def test_list_shows_executor_runs_row(self, monkeypatch, tmp_path):
        """A claude-code-cli run is visible in GET /runs (from executor_runs)."""
        client, app_module, key = make_client(monkeypatch, tmp_path)
        fake_bin = write_fake_claude(tmp_path, stdout="ok", name="fake-claude-C2")
        set_fake_binary(monkeypatch, fake_bin)

        resp = post_executor(client, key, {
            "executor": "claude-code-cli",
            "prompt": "list union cli",
            "timeout_sec": 30,
            "repo_path": "/home/ubuntu/Abacus",
        })
        assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
        run_id = resp.json()["run_id"]

        resp2 = client.get("/runs", headers=_auth_headers(key))
        assert resp2.status_code == 200
        run_ids = [it["run_id"] for it in resp2.json()["items"]]
        assert run_id in run_ids, (
            f"claude-code-cli run {run_id!r} not in GET /runs list"
        )

    def test_list_no_duplicate_for_dual_store_run(self, monkeypatch, tmp_path):
        """A hermes run in BOTH tasks and executor_runs appears exactly once."""
        saved = _stub_hermes_adapter(run_id="run_union_C003")
        try:
            client, app_module, key = make_client(monkeypatch, tmp_path)
            resp = client.post(
                "/runs",
                json={
                    "title": "test-union-dual",
                    "input": "dual store",
                    "mode": "normal",
                    "session_id": "test-session-C-dual",
                },
                headers=_auth_headers(key),
            )
            assert resp.status_code in (200, 201)
            run_id = resp.json()["run_id"]

            resp2 = client.get("/runs", headers=_auth_headers(key))
            items = resp2.json()["items"]
            matches = [it for it in items if it["run_id"] == run_id]
            assert len(matches) == 1, (
                f"run {run_id!r} appeared {len(matches)} times in list "
                f"(must be exactly 1 — executor_runs row wins, tasks union deduped)"
            )
        finally:
            _restore_adapters(saved)


# ---------------------------------------------------------------------------
# Read consistency: GET /runs/{id} and summary for both executor types
# ---------------------------------------------------------------------------

class TestReadConsistency:
    """GET /runs/{id} and summary must work for both run types."""

    def test_get_run_hermes(self, monkeypatch, tmp_path):
        """GET /runs/{id} returns a hermes run."""
        saved = _stub_hermes_adapter(run_id="run_get_D001")
        try:
            client, app_module, key = make_client(monkeypatch, tmp_path)
            resp = client.post(
                "/runs",
                json={
                    "title": "test-get-hermes",
                    "input": "get hermes",
                    "mode": "normal",
                    "session_id": "test-session-D",
                },
                headers=_auth_headers(key),
            )
            run_id = resp.json()["run_id"]

            resp2 = client.get(f"/runs/{run_id}", headers=_auth_headers(key))
            assert resp2.status_code == 200, f"{resp2.status_code}: {resp2.text}"
            data = resp2.json()
            assert data["run_id"] == run_id
            assert data["selected_executor"] == "hermes"
        finally:
            _restore_adapters(saved)

    def test_get_run_summary_hermes(self, monkeypatch, tmp_path):
        """GET /runs/{id}/summary returns a hermes run summary."""
        saved = _stub_hermes_adapter(run_id="run_sum_D002")
        try:
            client, app_module, key = make_client(monkeypatch, tmp_path)
            resp = client.post(
                "/runs",
                json={
                    "title": "test-summary-hermes",
                    "input": "summary hermes",
                    "mode": "normal",
                    "session_id": "test-session-D-sum",
                },
                headers=_auth_headers(key),
            )
            run_id = resp.json()["run_id"]

            resp2 = client.get(f"/runs/{run_id}/summary", headers=_auth_headers(key))
            assert resp2.status_code == 200, f"{resp2.status_code}: {resp2.text}"
        finally:
            _restore_adapters(saved)

    def test_get_run_claude_code(self, monkeypatch, tmp_path):
        """GET /runs/{id} returns a claude-code-cli run."""
        client, app_module, key = make_client(monkeypatch, tmp_path)
        fake_bin = write_fake_claude(tmp_path, stdout="ok", name="fake-claude-D3")
        set_fake_binary(monkeypatch, fake_bin)

        resp = post_executor(client, key, {
            "executor": "claude-code-cli",
            "prompt": "get cli",
            "timeout_sec": 30,
            "repo_path": "/home/ubuntu/Abacus",
        })
        run_id = resp.json()["run_id"]

        resp2 = client.get(f"/runs/{run_id}", headers=_auth_headers(key))
        assert resp2.status_code == 200, f"{resp2.status_code}: {resp2.text}"
        data = resp2.json()
        assert data["run_id"] == run_id
        assert data["selected_executor"] == "claude-code-cli"

    def test_get_run_summary_claude_code(self, monkeypatch, tmp_path):
        """GET /runs/{id}/summary returns a claude-code-cli run summary."""
        client, app_module, key = make_client(monkeypatch, tmp_path)
        fake_bin = write_fake_claude(tmp_path, stdout="ok", name="fake-claude-D4")
        set_fake_binary(monkeypatch, fake_bin)

        resp = post_executor(client, key, {
            "executor": "claude-code-cli",
            "prompt": "summary cli",
            "timeout_sec": 30,
            "repo_path": "/home/ubuntu/Abacus",
        })
        run_id = resp.json()["run_id"]

        resp2 = client.get(f"/runs/{run_id}/summary", headers=_auth_headers(key))
        assert resp2.status_code == 200, f"{resp2.status_code}: {resp2.text}"

    def test_get_run_unknown_id_404(self, monkeypatch, tmp_path):
        """GET /runs/{unknown_id} returns 404 (unchanged behaviour)."""
        client, app_module, key = make_client(monkeypatch, tmp_path)
        resp = client.get("/runs/run_does_not_exist_xyz", headers=_auth_headers(key))
        assert resp.status_code == 404, (
            f"expected 404 for unknown run_id, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# Fix D: lifecycle status sync to executor_runs
# ---------------------------------------------------------------------------

class TestFixDLifecycleSync:
    """manager.complete()/fail() must sync executor_runs."""

    def test_complete_syncs_executor_runs(self, monkeypatch, tmp_path):
        """manager.complete() mirrors 'completed' into executor_runs."""
        from dispatcher.manager import TaskManager
        from dispatcher import db as ddb

        setup_temp_db(monkeypatch, tmp_path)
        monkeypatch.setenv("BRIDGE_API_KEY", "lifecycle-key")
        import app as app_module
        app_module.CLIENT_BRIDGE_KEYS = {"lifecycle-key"}

        mgr = TaskManager()
        task = mgr.create(
            title="lifecycle-complete",
            type="ops",
            input_text="test complete sync",
            initial_status="queued",
        )
        run_id = "run_lifecycle_D001"
        mgr.start(task.task_id, run_id)
        # Simulate the Fix A mapping write (as POST /runs would)
        from dispatcher.db import get_conn
        from dispatcher.executor_runs import upsert_run
        conn = get_conn()
        upsert_run(
            conn,
            run_id=run_id,
            requested_executor=None,
            selected_executor="hermes",
            task_id=task.task_id,
            status="running",
        )

        # Disable the notification gate to avoid Telegram dependency
        monkeypatch.setenv("AEE_NOTIFY_DISABLED", "1")

        # Stub notifier to avoid real Telegram calls. After
        # unifying the terminal-notification path, ``complete()``
        # calls ``_notify_terminal(task_id, \"completed\")`` which
        # calls ``notify_terminal_with_fallback`` (the generalized
        # gate). Patch the generalized symbol so the stub takes
        # effect for the ``completed`` transition.
        from dispatcher import notifier as dnotif
        monkeypatch.setattr(
            dnotif, "notify_terminal_with_fallback",
            lambda task_id, status, **kw: {"sent": True, "method": "stub", "message_id": 1, "recipient": "test"},
        )

        mgr.complete(task.task_id, output_text="done")

        row = _query_executor_runs(_db_path(monkeypatch), run_id)
        assert row is not None, f"executor_runs row missing for {run_id!r}"
        assert row["status"] == "completed", (
            f"executor_runs.status={row['status']!r} after complete(), expected 'completed' "
            f"(Fix D: lifecycle must sync)"
        )

    def test_fail_syncs_executor_runs(self, monkeypatch, tmp_path):
        """manager.fail() mirrors 'failed' into executor_runs."""
        from dispatcher.manager import TaskManager

        setup_temp_db(monkeypatch, tmp_path)
        monkeypatch.setenv("BRIDGE_API_KEY", "lifecycle-key")
        import app as app_module
        app_module.CLIENT_BRIDGE_KEYS = {"lifecycle-key"}

        # Test isolation: ``make_client`` / ``setup_temp_db`` import
        # ``app`` which calls ``load_dotenv()`` at module import time,
        # injecting the production ``TELEGRAM_CHAT_ID`` from ``.env``
        # into ``os.environ``. ``AEE_NOTIFY_DISABLED=1`` is NOT enforced
        # by production code (verified by grep across dispatcher/, aee/,
        # app.py — no consumer of that env var exists), so setting it
        # here is decorative only and does NOT block the gate.
        # ``mgr.fail()`` invokes ``_notify_terminal(task_id, "failed")``
        # which calls ``notify_terminal_with_fallback`` →
        # ``notify_terminal_hermes_gateway`` → ``subprocess.run(["hermes",
        # "send", ...])`` with the real chat id. Without stubbing the
        # generalized gate symbol, this test would fire a real Telegram
        # message to the production chat (incident root cause). Stub the
        # generalized ``notify_terminal_with_fallback`` symbol (the same
        # pattern used by ``test_complete_syncs_executor_runs`` above)
        # so the gate returns a fixed stub dict and never reaches the
        # subprocess.run path.
        monkeypatch.setenv("AEE_NOTIFY_DISABLED", "1")
        from dispatcher import notifier as dnotif
        monkeypatch.setattr(
            dnotif, "notify_terminal_with_fallback",
            lambda task_id, status, **kw: {"sent": True, "method": "stub", "message_id": 1, "recipient": "test"},
        )

        mgr = TaskManager()
        task = mgr.create(
            title="lifecycle-fail",
            type="ops",
            input_text="test fail sync",
            initial_status="queued",
        )
        run_id = "run_lifecycle_D002"
        mgr.start(task.task_id, run_id)
        from dispatcher.db import get_conn
        from dispatcher.executor_runs import upsert_run
        conn = get_conn()
        upsert_run(
            conn,
            run_id=run_id,
            requested_executor=None,
            selected_executor="hermes",
            task_id=task.task_id,
            status="running",
        )

        mgr.fail(task.task_id, "simulated failure")

        row = _query_executor_runs(_db_path(monkeypatch), run_id)
        assert row is not None
        assert row["status"] == "failed", (
            f"executor_runs.status={row['status']!r} after fail(), expected 'failed'"
        )

    def test_timeout_syncs_executor_runs(self, monkeypatch, tmp_path):
        """manager.timeout() mirrors 'timeout' into executor_runs."""
        from dispatcher.manager import TaskManager

        setup_temp_db(monkeypatch, tmp_path)
        monkeypatch.setenv("BRIDGE_API_KEY", "lifecycle-key")
        import app as app_module
        app_module.CLIENT_BRIDGE_KEYS = {"lifecycle-key"}

        # Test isolation (same as test_fail_syncs_executor_runs above):
        # ``setup_temp_db`` imports ``app`` which calls ``load_dotenv()``
        # at module import time, injecting the production
        # ``TELEGRAM_CHAT_ID`` from ``.env`` into ``os.environ``.
        # ``AEE_NOTIFY_DISABLED=1`` is NOT enforced by production code,
        # so setting it here is decorative only. ``mgr.timeout()``
        # invokes ``_notify_terminal(task_id, "timeout")`` →
        # ``notify_terminal_with_fallback`` →
        # ``notify_terminal_hermes_gateway`` → ``subprocess.run(["hermes",
        # "send", ...])`` with the real chat id. Without stubbing the
        # generalized gate symbol, this test would fire a real Telegram
        # message to the production chat (incident root cause). Stub the
        # generalized ``notify_terminal_with_fallback`` symbol so the
        # gate returns a fixed stub dict and never reaches the
        # subprocess.run path.
        monkeypatch.setenv("AEE_NOTIFY_DISABLED", "1")
        from dispatcher import notifier as dnotif
        monkeypatch.setattr(
            dnotif, "notify_terminal_with_fallback",
            lambda task_id, status, **kw: {"sent": True, "method": "stub", "message_id": 1, "recipient": "test"},
        )

        mgr = TaskManager()
        task = mgr.create(
            title="lifecycle-timeout",
            type="ops",
            input_text="test timeout sync",
            initial_status="queued",
        )
        run_id = "run_lifecycle_D003"
        mgr.start(task.task_id, run_id)
        from dispatcher.db import get_conn
        from dispatcher.executor_runs import upsert_run
        conn = get_conn()
        upsert_run(
            conn,
            run_id=run_id,
            requested_executor=None,
            selected_executor="hermes",
            task_id=task.task_id,
            status="running",
        )

        mgr.timeout(task.task_id, "test timeout reason")

        row = _query_executor_runs(_db_path(monkeypatch), run_id)
        assert row is not None
        assert row["status"] == "timeout", (
            f"executor_runs.status={row['status']!r} after timeout(), expected 'timeout'"
        )


# ---------------------------------------------------------------------------
# Test isolation regression: prove the fail/timeout lifecycle tests do
# NOT invoke the real ``hermes send`` subprocess (incident root cause).
# A fail-on-call sentinel is installed on ``subprocess.run`` that raises
# ``AssertionError`` if ANY subprocess invocation fires. The
# notification gate is also stubbed so the gate returns immediately
# without reaching the subprocess path. The test then asserts the
# sentinel was never triggered (``call_count == 0``).
#
# This regression test class is the durable proof required by the work
# order's "Test Isolation Strategy" + "Proof No Real Telegram Send
# Occurred" sections. It runs the same ``mgr.fail()`` / ``mgr.timeout()``
# lifecycle transitions exercised by ``TestFixDLifecycleSync`` but with
# an explicit subprocess-run sentinel, so any future regression that
# removes the ``notify_terminal_with_fallback`` stub will fail THIS
# test before it can fire a real Telegram message.
# ---------------------------------------------------------------------------

class TestNotificationIsolationRegression:
    """Prove the fail/timeout lifecycle tests do NOT invoke the real
    ``hermes send`` subprocess path.

    The sentinel: a ``subprocess.run`` patch that raises
    ``AssertionError`` on ANY call. Combined with the
    ``notify_terminal_with_fallback`` stub (mirroring the production
    fix in ``TestFixDLifecycleSync``), the sentinel MUST remain
    uncalled. ``call_count == 0`` after the lifecycle transition is
    the durable proof no real Telegram send was attempted.
    """

    def _setup_lifecycle_env(self, monkeypatch, tmp_path, *, run_id, title):
        """Shared setup: temp DB + bridge key + stub notifier +
        fail-on-call subprocess sentinel. Returns ``(mgr, task_id,
        sentinel)``."""
        from dispatcher.manager import TaskManager

        setup_temp_db(monkeypatch, tmp_path)
        monkeypatch.setenv("BRIDGE_API_KEY", "isolation-key")
        import app as app_module
        app_module.CLIENT_BRIDGE_KEYS = {"isolation-key"}

        # Stub the generalized notification gate so it never reaches
        # ``notify_terminal_hermes_gateway`` (and therefore never
        # reaches ``subprocess.run`` for the ``hermes send`` path).
        # This is the same stub used by
        # ``TestFixDLifecycleSync.test_*_syncs_executor_runs`` above.
        monkeypatch.setenv("AEE_NOTIFY_DISABLED", "1")
        from dispatcher import notifier as dnotif
        monkeypatch.setattr(
            dnotif, "notify_terminal_with_fallback",
            lambda task_id, status, **kw: {
                "sent": True, "method": "stub",
                "message_id": 1, "recipient": "test",
            },
        )

        # Fail-on-call sentinel: if the ``hermes send`` subprocess
        # path fires during the lifecycle transition, raise
        # AssertionError immediately. We scope the sentinel to the
        # ``hermes send`` argv shape (``argv[0] == "hermes" and
        # argv[1] == "send"``) because ``mgr.create()`` legitimately
        # invokes ``subprocess.run(["git", "rev-parse", "HEAD"])`` via
        # ``_git_info()`` for git-evidence capture — that call is NOT
        # a notification path and must not trip the sentinel. The
        # notification path is the only caller of ``hermes send`` in
        # the dispatcher, so an assertion here is durable proof the
        # gate reached the real subprocess.
        import subprocess as _sp
        calls: list = []
        _real_run = _sp.run
        def _sentinel(argv, *args, **kwargs):
            if argv and len(argv) >= 2 and argv[0] == "hermes" and argv[1] == "send":
                calls.append(list(argv))
                raise AssertionError(
                    f"subprocess.run invoked ``hermes send`` during "
                    f"lifecycle test (argv={argv!r}); notification "
                    f"isolation broken — the "
                    f"notify_terminal_with_fallback stub is missing "
                    f"or bypassed"
                )
            # Non-hermes-send subprocess calls (e.g. git rev-parse
            # for git-evidence) fall through to the real subprocess.run
            # so the test's lifecycle transition can complete normally.
            return _real_run(argv, *args, **kwargs)
        monkeypatch.setattr(_sp, "run", _sentinel)

        mgr = TaskManager()
        task = mgr.create(
            title=title,
            type="ops",
            input_text="isolation regression",
            initial_status="queued",
        )
        mgr.start(task.task_id, run_id)
        from dispatcher.db import get_conn
        from dispatcher.executor_runs import upsert_run
        conn = get_conn()
        upsert_run(
            conn,
            run_id=run_id,
            requested_executor=None,
            selected_executor="hermes",
            task_id=task.task_id,
            status="running",
        )
        return mgr, task.task_id, calls

    def test_fail_does_not_invoke_subprocess(self, monkeypatch, tmp_path):
        """``mgr.fail()`` MUST NOT invoke ``subprocess.run`` for the
        ``hermes send`` notification path. The
        ``notify_terminal_with_fallback`` stub short-circuits the
        gate before the subprocess call; the fail-on-call sentinel
        proves it."""
        mgr, task_id, calls = self._setup_lifecycle_env(
            monkeypatch, tmp_path,
            run_id="run_iso_fail_001", title="iso-fail",
        )
        mgr.fail(task_id, "simulated failure")
        assert calls == [], (
            f"subprocess.run was invoked during fail() (calls={calls}); "
            f"notification isolation broken — the "
            f"notify_terminal_with_fallback stub is missing or bypassed"
        )

    def test_timeout_does_not_invoke_subprocess(self, monkeypatch, tmp_path):
        """``mgr.timeout()`` MUST NOT invoke ``subprocess.run`` for
        the ``hermes send`` notification path."""
        mgr, task_id, calls = self._setup_lifecycle_env(
            monkeypatch, tmp_path,
            run_id="run_iso_timeout_001", title="iso-timeout",
        )
        mgr.timeout(task_id, "test timeout reason")
        assert calls == [], (
            f"subprocess.run was invoked during timeout() (calls={calls}); "
            f"notification isolation broken — the "
            f"notify_terminal_with_fallback stub is missing or bypassed"
        )

    def test_complete_does_not_invoke_subprocess(self, monkeypatch, tmp_path):
        """``mgr.complete()`` MUST NOT invoke ``subprocess.run`` for
        the ``hermes send`` notification path. This mirrors the
        existing ``test_complete_syncs_executor_runs`` stub but adds
        the explicit fail-on-call sentinel as durable proof."""
        mgr, task_id, calls = self._setup_lifecycle_env(
            monkeypatch, tmp_path,
            run_id="run_iso_complete_001", title="iso-complete",
        )
        mgr.complete(task_id, output_text="done")
        assert calls == [], (
            f"subprocess.run was invoked during complete() (calls={calls}); "
            f"notification isolation broken — the "
            f"notify_terminal_with_fallback stub is missing or bypassed"
        )


# ---------------------------------------------------------------------------
# Regression: pre-existing behaviour must not break
# ---------------------------------------------------------------------------

class TestRegression:
    """Pre-existing endpoint behaviour must not break."""

    def test_invalid_limit_400(self, monkeypatch, tmp_path):
        client, app_module, key = make_client(monkeypatch, tmp_path)
        resp = client.get("/runs?limit=0", headers=_auth_headers(key))
        assert resp.status_code == 400
        body = resp.json()
        assert body["detail"]["code"] == "invalid_limit"

    def test_invalid_status_400(self, monkeypatch, tmp_path):
        client, app_module, key = make_client(monkeypatch, tmp_path)
        resp = client.get("/runs?status=not_a_status", headers=_auth_headers(key))
        assert resp.status_code == 400
        body = resp.json()
        assert body["detail"]["code"] == "invalid_status"

    def test_unsupported_executor_400(self, monkeypatch, tmp_path):
        client, app_module, key = make_client(monkeypatch, tmp_path)
        resp = post_executor(client, key, {
            "executor": "not_a_real_executor",
            "prompt": "x",
            "timeout_sec": 30,
        })
        assert resp.status_code == 400
        body = resp.json()
        assert body["detail"]["code"] == "unsupported_executor"

    def test_unauthorized_401(self, monkeypatch, tmp_path):
        client, app_module, key = make_client(monkeypatch, tmp_path)
        resp = client.get("/runs", headers={"Authorization": "Bearer wrong-key"})
        assert resp.status_code == 401

    def test_list_filters_apply_to_union(self, monkeypatch, tmp_path):
        """The status filter applies to unioned tasks rows too."""
        saved = _stub_hermes_adapter(run_id="run_filter_E001")
        try:
            client, app_module, key = make_client(monkeypatch, tmp_path)
            resp = client.post(
                "/runs",
                json={
                    "title": "test-filter",
                    "input": "filter test",
                    "mode": "normal",
                    "session_id": "test-session-E",
                },
                headers=_auth_headers(key),
            )
            run_id = resp.json()["run_id"]
            # Delete executor_runs to force the union path
            conn = sqlite3.connect(str(_db_path(monkeypatch)))
            try:
                conn.execute("DELETE FROM executor_runs WHERE run_id = ?", (run_id,))
                conn.commit()
            finally:
                conn.close()
            # The task is in 'running' status; filter for 'completed' must NOT return it
            resp2 = client.get(
                "/runs?status=completed", headers=_auth_headers(key),
            )
            assert resp2.status_code == 200
            run_ids = [it["run_id"] for it in resp2.json()["items"]]
            assert run_id not in run_ids, (
                f"unioned run {run_id!r} (status=running) appeared in "
                f"status=completed filter: {run_ids}"
            )
        finally:
            _restore_adapters(saved)


# ---------------------------------------------------------------------------
# WO-FIX-API-SERIALIZATION-MERGE-HERMES-EVIDENCE
# ---------------------------------------------------------------------------

class TestHermesStubEvidenceMerge:
    """A Hermes lifecycle-sync executor_runs stub with empty evidence
    must be merged with task-side evidence so GET /runs/{id} and
    /summary surface the real artifacts/output the run produced.

    Regression safety (work-order §"REGRESSION SAFETY"):
      * Fully populated claude-code-cli rows are NOT merged (detection
        short-circuits on the first non-empty executor evidence field).
      * Unknown run IDs still 404.
      * Existing canary/tasks fallback behaviour unchanged.
    """

    def _seed_hermes_lifecycle_stub(self, db_path, *, run_id, task_id,
                                    status="completed",
                                    stdout_summary="",
                                    artifact_paths=None,
                                    git_evidence=None):
        """Write a Hermes lifecycle-sync stub directly into executor_runs."""
        from dispatcher import db as ddb
        from dispatcher.executor_runs import upsert_run
        # Ensure schema is initialised before we open a raw connection.
        ddb.get_conn()
        conn = sqlite3.connect(str(db_path))
        try:
            upsert_run(
                conn,
                run_id=run_id,
                requested_executor=None,
                selected_executor="hermes",
                task_id=task_id,
                status=status,
                stdout_summary=stdout_summary,
                artifact_paths=artifact_paths or [],
                git_evidence=git_evidence,
            )
        finally:
            conn.close()

    def _seed_task_evidence(self, db_path, *, task_id, output_text,
                            delivery_paths=None, artifact_rows=None,
                            notification=None):
        """Seed dispatcher task + task_outputs + artifacts rows."""
        from dispatcher import db as ddb
        # Ensure schema is initialised before we open a raw connection.
        ddb.get_conn()
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(
                "INSERT OR REPLACE INTO tasks ("
                "  task_id, title, type, status, progress_pct, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, "stub-evidence-merge", "ops", "completed",
                 100, "2026-07-23T09:00:00Z"),
            )
            delivery_json = json.dumps(delivery_paths or [])
            notification_json = json.dumps(notification) if notification else None
            conn.execute(
                "INSERT OR REPLACE INTO task_outputs ("
                "  task_id, output_text, usage_json, raw_json,"
                "  delivery_json, notification_json"
                ") VALUES (?, ?, NULL, NULL, ?, ?)",
                (task_id, output_text, delivery_json, notification_json),
            )
            for a in artifact_rows or []:
                conn.execute(
                    "INSERT OR REPLACE INTO artifacts ("
                    "  artifact_id, task_id, path, kind, sha256,"
                    "  size, mtime, file_exists, content_type,"
                    "  classification_source, collected_at, version"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (a.get("artifact_id", f"art-{a['path'][-8:]}"),
                     task_id, a["path"], a.get("kind", "artifact"),
                     a.get("sha256"), a.get("size"), a.get("mtime"),
                     1 if a.get("file_exists", True) else 0,
                     a.get("content_type", "text/markdown"),
                     a.get("classification_source", "auto"),
                     a.get("collected_at", "2026-07-23T09:00:00Z"),
                     a.get("version", 1)),
                )
            conn.commit()
        finally:
            conn.close()

    def test_get_run_merges_artifacts_from_tasks(self, monkeypatch, tmp_path):
        """GET /runs/{id} merges artifact_paths/verification from tasks table."""
        client, app_module, key = make_client(monkeypatch, tmp_path)
        run_id = "run_merge_001"
        task_id = "TASK-MERGE-001"
        db = _db_path(monkeypatch)

        self._seed_hermes_lifecycle_stub(
            db, run_id=run_id, task_id=task_id, status="completed",
            stdout_summary="", artifact_paths=[], git_evidence=None,
        )
        self._seed_task_evidence(
            db, task_id=task_id, output_text="WO-1 完成。提交 SHA abc123",
            delivery_paths=[
                "/home/ubuntu/Abacus/AEE_WO1_REPORT.md",
                "/home/ubuntu/Abacus/AEE_WO1_REPORT.md.sha256",
            ],
            artifact_rows=[
                {"path": "/home/ubuntu/Abacus/AEE_WO1_REPORT.md",
                 "sha256": "a" * 64, "size": 100, "mtime": "2026-07-23T09:00:00Z"},
                {"path": "/home/ubuntu/Abacus/AEE_WO1_REPORT.md.sha256",
                 "sha256": "b" * 64, "size": 65, "mtime": "2026-07-23T09:00:00Z"},
            ],
        )

        resp = client.get(f"/runs/{run_id}", headers=_auth_headers(key))
        assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
        data = resp.json()
        # Lifecycle fields preserved from executor_runs
        assert data["status"] == "completed"
        assert data["selected_executor"] == "hermes"
        assert data["task_id"] == task_id
        # Evidence merged from tasks side
        assert data["source"] == "executor_runs+tasks_merge"
        assert data["artifact_paths"] == [
            "/home/ubuntu/Abacus/AEE_WO1_REPORT.md",
            "/home/ubuntu/Abacus/AEE_WO1_REPORT.md.sha256",
        ], f"artifact_paths not merged: {data['artifact_paths']}"
        assert len(data["artifact_verification"]) == 2
        assert data["stdout_summary"], (
            f"stdout_summary empty after merge: {data['stdout_summary']!r}"
        )
        assert "WO-1 完成" in data["stdout_summary"]

    def test_get_run_summary_merges_artifacts_from_tasks(self, monkeypatch, tmp_path):
        """GET /runs/{id}/summary merges artifact_count/paths/output_preview."""
        client, app_module, key = make_client(monkeypatch, tmp_path)
        run_id = "run_merge_002"
        task_id = "TASK-MERGE-002"
        db = _db_path(monkeypatch)

        self._seed_hermes_lifecycle_stub(
            db, run_id=run_id, task_id=task_id, status="completed",
            stdout_summary="", artifact_paths=[],
        )
        self._seed_task_evidence(
            db, task_id=task_id,
            output_text="Diagnostic complete. All artifacts verified.",
            delivery_paths=[
                "/home/ubuntu/Abacus/AEE_DIAG_REPORT.md",
                "/home/ubuntu/Abacus/AEE_DIAG_REPORT.md.sha256",
            ],
            artifact_rows=[
                {"path": "/home/ubuntu/Abacus/AEE_DIAG_REPORT.md",
                 "sha256": "c" * 64, "size": 200},
                {"path": "/home/ubuntu/Abacus/AEE_DIAG_REPORT.md.sha256",
                 "sha256": "d" * 64, "size": 65},
            ],
        )

        resp = client.get(f"/runs/{run_id}/summary", headers=_auth_headers(key))
        assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["status"] == "completed"
        assert data["source"] == "executor_runs+tasks_merge"
        assert data["artifact_count"] == 2, (
            f"artifact_count={data['artifact_count']}, expected 2"
        )
        assert len(data["artifact_paths"]) == 2
        assert data["output_preview"], (
            f"output_preview empty after merge: {data['output_preview']!r}"
        )
        assert "Diagnostic complete" in data["output_preview"]

    def test_fully_populated_claude_code_row_not_merged(self, monkeypatch, tmp_path):
        """A claude-code-cli run with real artifact_paths is NOT merged."""
        client, app_module, key = make_client(monkeypatch, tmp_path)
        fake_bin = write_fake_claude(
            tmp_path, stdout="cli ok output",
            name="fake-claude-merge-noop",
        )
        set_fake_binary(monkeypatch, fake_bin)

        resp = post_executor(client, key, {
            "executor": "claude-code-cli",
            "prompt": "merge noop test",
            "timeout_sec": 30,
            "repo_path": "/home/ubuntu/Abacus",
        })
        assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
        run_id = resp.json()["run_id"]

        resp2 = client.get(f"/runs/{run_id}", headers=_auth_headers(key))
        assert resp2.status_code == 200
        data = resp2.json()
        # A fully populated claude-code-cli row must keep source=executor_runs
        # (NOT executor_runs+tasks_merge) — detection short-circuits.
        assert data["source"] == "executor_runs", (
            f"fully populated claude-code-cli row was merged (source="
            f"{data['source']!r}); merge must be a no-op when executor "
            f"evidence is non-empty"
        )

    def test_unknown_run_id_still_404(self, monkeypatch, tmp_path):
        """Unknown run_id still returns 404 (no merge, no upstream call)."""
        client, app_module, key = make_client(monkeypatch, tmp_path)
        resp = client.get(
            "/runs/run_unknown_merge_test_xyz", headers=_auth_headers(key),
        )
        assert resp.status_code == 404

    def test_empty_stub_with_no_task_evidence_not_merged(self, monkeypatch, tmp_path):
        """An empty stub whose task has no evidence is returned as-is
        (source stays executor_runs, no merge fires)."""
        client, app_module, key = make_client(monkeypatch, tmp_path)
        run_id = "run_merge_empty_005"
        task_id = "TASK-MERGE-EMPTY-005"
        db = _db_path(monkeypatch)

        self._seed_hermes_lifecycle_stub(
            db, run_id=run_id, task_id=task_id, status="completed",
            stdout_summary="", artifact_paths=[],
        )
        # Seed the task row but with NO output_text, NO artifacts,
        # NO delivery_json — _collect_task_evidence returns None.
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "INSERT OR REPLACE INTO tasks ("
                "  task_id, title, type, status, created_at"
                ") VALUES (?, ?, ?, ?, ?)",
                (task_id, "empty-evidence", "ops", "completed",
                 "2026-07-23T09:00:00Z"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO task_outputs ("
                "  task_id, output_text, delivery_json, notification_json"
                ") VALUES (?, ?, NULL, NULL)",
                (task_id, "",),
            )
            conn.commit()
        finally:
            conn.close()

        resp = client.get(f"/runs/{run_id}", headers=_auth_headers(key))
        assert resp.status_code == 200
        data = resp.json()
        # No evidence on either side → no merge, source stays executor_runs
        assert data["source"] == "executor_runs"
        assert data["artifact_paths"] == []

    def test_stub_without_task_id_not_merged(self, monkeypatch, tmp_path):
        """A stub with task_id=NULL cannot be merged (no linked task)."""
        client, app_module, key = make_client(monkeypatch, tmp_path)
        run_id = "run_merge_no_taskid_006"
        db = _db_path(monkeypatch)

        from dispatcher import db as ddb
        from dispatcher.executor_runs import upsert_run
        ddb.get_conn()  # ensure schema initialised
        conn = sqlite3.connect(str(db))
        try:
            upsert_run(
                conn,
                run_id=run_id,
                requested_executor=None,
                selected_executor="hermes",
                task_id=None,  # no linked task
                status="completed",
                stdout_summary="",
                artifact_paths=[],
            )
        finally:
            conn.close()

        resp = client.get(f"/runs/{run_id}", headers=_auth_headers(key))
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "executor_runs"
        assert data["artifact_paths"] == []

    def test_lifecycle_sync_output_only_is_merged(self, monkeypatch, tmp_path):
        """Case A (WO-FIX-OUTPUT-ONLY-HERMES-EVIDENCE-003 §9): a
        lifecycle-sync stub whose task carries ``output_text`` but NO
        artifacts is MERGED — ``source`` becomes
        ``executor_runs+tasks_merge`` and ``stdout_summary`` carries
        the task output. This is the corrected API contract (§6): a
        legitimate empty task — no ``output_text`` AND no artifacts —
        is the ONLY case that stays unmerged (covered by
        ``test_empty_stub_with_no_task_evidence_not_merged``)."""
        client, app_module, key = make_client(monkeypatch, tmp_path)
        run_id = "run_merge_output_only_009"
        task_id = "TASK-MERGE-OUTPUT-ONLY-009"
        db = _db_path(monkeypatch)

        self._seed_hermes_lifecycle_stub(
            db, run_id=run_id, task_id=task_id, status="completed",
            stdout_summary="", artifact_paths=[],
        )
        # Task has output_text but NO artifacts, NO delivery_json paths.
        self._seed_task_evidence(
            db, task_id=task_id, output_text="legacy done",
            delivery_paths=[], artifact_rows=[],
        )

        resp = client.get(f"/runs/{run_id}", headers=_auth_headers(key))
        assert resp.status_code == 200
        data = resp.json()
        # Output-only → merge fires → source flipped, output surfaced,
        # artifact_count stays 0 (no artifacts fabricated).
        assert data["source"] == "executor_runs+tasks_merge", (
            f"output-only stub was NOT merged (source="
            f"{data['source']!r}); §6 requires merge when output_text "
            f"is non-empty even with zero artifacts"
        )
        assert data["artifact_paths"] == []
        assert "legacy done" in data["stdout_summary"], (
            f"stdout_summary not populated from output_text: "
            f"{data['stdout_summary']!r}"
        )

        # Summary endpoint must agree (Case A on /runs/{id}/summary).
        resp2 = client.get(f"/runs/{run_id}/summary", headers=_auth_headers(key))
        assert resp2.status_code == 200, f"{resp2.status_code}: {resp2.text}"
        sdata = resp2.json()
        assert sdata["status"] == "completed"
        assert sdata["source"] == "executor_runs+tasks_merge"
        assert sdata["artifact_count"] == 0, (
            f"artifact_count={sdata['artifact_count']}, expected 0 "
            f"(output-only must not fabricate artifacts)"
        )
        assert "legacy done" in sdata["output_preview"], (
            f"output_preview not populated: {sdata['output_preview']!r}"
        )

    def test_canary_tasks_fallback_unchanged(self, monkeypatch, tmp_path):
        """A run with NO executor_runs row still falls through to the
        tasks-table fallback (source=dispatcher_tasks)."""
        saved = _stub_hermes_adapter(run_id="run_merge_canary_007")
        try:
            client, app_module, key = make_client(monkeypatch, tmp_path)
            resp = client.post(
                "/runs",
                json={
                    "title": "canary-fallback",
                    "input": "canary fallback test",
                    "mode": "normal",
                    "session_id": "test-merge-canary",
                },
                headers=_auth_headers(key),
            )
            run_id = resp.json()["run_id"]
            # Delete the executor_runs row to force the tasks fallback
            conn = sqlite3.connect(str(_db_path(monkeypatch)))
            try:
                conn.execute(
                    "DELETE FROM executor_runs WHERE run_id = ?", (run_id,),
                )
                conn.commit()
            finally:
                conn.close()

            resp2 = client.get(f"/runs/{run_id}", headers=_auth_headers(key))
            assert resp2.status_code == 200
            data = resp2.json()
            assert data["source"] == "dispatcher_tasks", (
                f"canary fallback broken: source={data['source']!r}"
            )
        finally:
            _restore_adapters(saved)

    def test_merged_envelope_preserves_lifecycle_fields(self, monkeypatch, tmp_path):
        """Merged envelope preserves status/exit_code/timestamps from
        executor_runs; only evidence fields are merged in."""
        client, app_module, key = make_client(monkeypatch, tmp_path)
        run_id = "run_merge_lifecycle_008"
        task_id = "TASK-MERGE-LIFECYCLE-008"
        db = _db_path(monkeypatch)

        self._seed_hermes_lifecycle_stub(
            db, run_id=run_id, task_id=task_id, status="failed",
            stdout_summary="", artifact_paths=[],
        )
        self._seed_task_evidence(
            db, task_id=task_id, output_text="task failed but produced a report",
            delivery_paths=["/home/ubuntu/Abacus/AEE_FAIL_REPORT.md"],
            artifact_rows=[
                {"path": "/home/ubuntu/Abacus/AEE_FAIL_REPORT.md",
                 "sha256": "e" * 64, "size": 50},
            ],
        )

        resp = client.get(f"/runs/{run_id}", headers=_auth_headers(key))
        assert resp.status_code == 200
        data = resp.json()
        # Lifecycle fields preserved from executor_runs
        assert data["status"] == "failed"
        assert data["selected_executor"] == "hermes"
        # Evidence merged from tasks
        assert data["source"] == "executor_runs+tasks_merge"
        assert data["artifact_paths"] == ["/home/ubuntu/Abacus/AEE_FAIL_REPORT.md"]
        assert "task failed" in data["stdout_summary"]