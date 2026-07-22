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

        # Stub notifier to avoid real Telegram calls. Use raising=False
        # so the test is self-contained on a clean HEAD: the
        # ``notify_completed_with_fallback`` function is added by the
        # pre-existing notifier.py dirty change, NOT by this patch, and
        # is absent on a clean HEAD where ``manager.complete()`` does
        # not call any notifier at all. On a dirty tree (where the
        # notification gate is present) the stub is applied normally;
        # on a clean HEAD the setattr is a silent no-op.
        from dispatcher import notifier as dnotif
        monkeypatch.setattr(
            dnotif, "notify_completed_with_fallback",
            lambda task_id: {"sent": True, "method": "stub", "message_id": 1, "recipient": "test"},
            raising=False,
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