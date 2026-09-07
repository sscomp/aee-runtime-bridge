"""Targeted tests: Hermes async executor lifecycle / reaper false-timeout fix.

Work order (2026-09-08) — root cause (audit run_92c2b8f6dd7548898d4d412714c45868):

  * ``POST /runs/executor``'s hermes branch created the dispatcher
    ``tasks`` row but never advanced it (no ``manager.start()``), so the
    row sat in ``queued`` until the reaper's ``stale_queued_sec=300``
    false-timed it out.
  * ``ExecutorRunWatcher`` / ``_persist_terminal_reconciliation`` only
    updated ``executor_runs`` and never touched ``dispatcher.tasks``.
  * The reaper's ``_executor_owns_queued_task`` guard only covered
    non-Hermes executors.
  * ``TaskManager._sync_executor_runs_status`` resolved the run_id only
    from ``tasks.hermes_run_id``/``external_run_id``; a NULL at terminal
    time skipped the sync, leaving tasks=timeout / executor_runs=running.

Covers (work order Testing Contract):

  T1. Hermes ``POST /runs/executor`` no longer leaves the task queued
      (manager.start mirrors the claude-code-cli / dsh-headless branches).
  T2. An active Hermes run past ``stale_queued_sec`` with a fresh
      executor_runs heartbeat is NOT reaped (guard generalised); a
      stale/NULL heartbeat still is (legacy fallback preserved).
  T3. Hermes terminal completion converges BOTH tables (task-side
      terminal mirror in ``_persist_terminal_reconciliation``).
  T4. Timeout/failure sync falls back to ``executor_runs.task_id``
      when ``hermes_run_id`` is NULL (``_sync_executor_runs_status``).
  T5. Hermes submit-failure path converges the task to ``failed``
      (no queued row left behind for the reaper).
  T6. Non-Hermes executor reaper semantics unchanged (regression).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests._executor_test_helpers import (
    make_client,
    post_executor,
    setup_temp_db,
)


def _get_run(client, key: str, run_id: str):
    return client.get(
        f"/runs/{run_id}",
        headers={"Authorization": f"Bearer {key}"},
    )


def _install_hermes_stub(monkeypatch, *, submit_status: str = "queued",
                         poll_status: str | None = None):
    """Install a stub Hermes adapter (same pattern as test_completion_sync).

    ``poll_status=None`` keeps the run non-terminal; a terminal value
    makes the next GET-driven reconcile converge the run.
    """
    from aee.adapters.base import (
        RuntimePollResult,
        RuntimeSubmitResult,
    )
    from aee.core.registry import adapter_registry

    calls: list[str] = []

    class _StubHermes:
        name = "hermes"
        runtime_type = "hermes"

        async def submit(self, job):
            calls.append("submit")
            return RuntimeSubmitResult(
                external_run_id="run_hermes_reaper_fix_1",
                status=submit_status,
            )

        async def poll(self, external_run_id):
            calls.append(f"poll:{external_run_id}")
            if poll_status is None:
                return RuntimePollResult(
                    external_run_id=external_run_id,
                    status="running",
                    is_terminal=False,
                )
            return RuntimePollResult(
                external_run_id=external_run_id,
                status=poll_status,
                is_terminal=True,
                output="hermes terminal output" if poll_status == "completed" else None,
                error=None if poll_status == "completed" else "simulated hermes failure",
                raw={"status": poll_status},
            )

        async def cancel(self, external_run_id):
            from aee.adapters.base import RuntimeCancelResult
            return RuntimeCancelResult(external_run_id=external_run_id, cancelled=True)

    saved = dict(adapter_registry._adapters)
    stub = _StubHermes()
    monkeypatch.setattr(adapter_registry, "_adapters", saved, raising=False)
    adapter_registry._adapters["hermes"] = stub
    return stub, calls


# ---------------------------------------------------------------------------
# T1 — hermes /runs/executor no longer leaves the task queued
# ---------------------------------------------------------------------------
class TestHermesExecutorTaskStarted:
    def test_task_transitions_queued_to_running_after_submit(
        self, monkeypatch, tmp_path
    ):
        from dispatcher.manager import TaskManager
        from dispatcher.db import get_conn

        _stub, _calls = _install_hermes_stub(monkeypatch, submit_status="queued")
        client, _app, key = make_client(monkeypatch, tmp_path)

        resp = post_executor(client, key, {
            "executor": "hermes",
            "prompt": "work order probe",
            "timeout_sec": 30,
        })
        assert resp.status_code == 200, resp.text
        env = resp.json()
        run_id = env["run_id"]
        task_id = env.get("task_id")
        assert task_id, "hermes executor response must carry task_id"

        # ROOT CAUSE ASSERTION: the task row must have been advanced out
        # of queued by manager.start() (stamped with the real run_id).
        tm = TaskManager()
        task = tm.get(task_id)
        assert task is not None
        assert task.status == "running", (
            f"hermes executor task must be running after submit, "
            f"got status={task.status!r} (reaper false-timeout root cause)"
        )
        assert task.hermes_run_id == run_id
        assert task.runtime_run_id == run_id

        # The upstream run id is stamped on both the task and the
        # executor_runs row so the watcher / reaper can correlate.
        conn = get_conn()
        er = conn.execute(
            "SELECT status, task_id FROM executor_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        assert er is not None
        assert er["task_id"] == task_id
        assert er["status"] in {"queued", "running"}


# ---------------------------------------------------------------------------
# T2 — reaper must not reap an active hermes run past stale_queued_sec
# ---------------------------------------------------------------------------
class TestReaperSparesActiveHermesQueuedTask:
    def _make_stale_queued_task(self, task_id: str, age_sec: int):
        from dispatcher.db import get_conn
        old = (datetime.now(timezone.utc) - timedelta(seconds=age_sec)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        conn = get_conn()
        conn.execute(
            "INSERT INTO tasks (task_id, title, type, priority, owner, "
            "status, progress_pct, created_at, input_text) "
            "VALUES (?, ?, 'ops', 50, 'm2', 'queued', 0, ?, 'x')",
            (task_id, "executor-run:hermes", old),
        )
        conn.commit()

    def test_active_hermes_run_not_reaped_despite_stale_queued(
        self, monkeypatch, tmp_path
    ):
        """tasks=queued age>300s but executor_runs heartbeat fresh → skip."""
        from dispatcher.db import get_conn
        from dispatcher.executor_runs import upsert_run
        from dispatcher.reaper import ReaperConfig, reap_once
        from dispatcher.manager import TaskManager

        setup_temp_db(monkeypatch, tmp_path)
        get_conn()  # init schema
        tm = TaskManager()

        task_id = "TASK-20260908-9001"
        self._make_stale_queued_task(task_id, age_sec=400)

        # Non-terminal hermes executor_runs row with a FRESH heartbeat.
        fresh_hb = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = get_conn()
        upsert_run(
            conn,
            run_id="run_hermes_reaper_fix_active",
            requested_executor="hermes",
            selected_executor="hermes",
            task_id=task_id,
            status="running",
            progress=0.0,
            last_heartbeat_at=fresh_hb,
            current_step="running",
            phase="running",
        )
        conn.commit()

        cfg = ReaperConfig(enabled=True, stale_queued_sec=300)
        result = reap_once(tm, cfg)
        assert task_id not in result.reaped, (
            f"active hermes run with fresh heartbeat must NOT be reaped; "
            f"reaped={result.reaped} skipped={result.skipped}"
        )
        assert tm.get(task_id).status == "queued", (
            "the task row must remain queued (untouched) after the reap scan"
        )

    def test_stale_heartbeat_hermes_queued_task_is_still_reaped(
        self, monkeypatch, tmp_path
    ):
        """Legacy fallback preserved: stale heartbeat → queued-age reap."""
        from dispatcher.db import get_conn
        from dispatcher.executor_runs import upsert_run
        from dispatcher.reaper import ReaperConfig, reap_once
        from dispatcher.manager import TaskManager

        setup_temp_db(monkeypatch, tmp_path)
        get_conn()
        tm = TaskManager()

        task_id = "TASK-20260908-9002"
        self._make_stale_queued_task(task_id, age_sec=400)

        stale_hb = (
            datetime.now(timezone.utc) - timedelta(seconds=1200)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = get_conn()
        upsert_run(
            conn,
            run_id="run_hermes_reaper_fix_stale",
            requested_executor="hermes",
            selected_executor="hermes",
            task_id=task_id,
            status="running",
            progress=0.0,
            last_heartbeat_at=stale_hb,
        )
        conn.commit()

        cfg = ReaperConfig(enabled=True, stale_queued_sec=300)
        result = reap_once(tm, cfg)
        assert task_id in result.reaped, (
            f"a hermes queued task with a STALE executor heartbeat must "
            f"still be reaped (genuinely dead upstream); reaped={result.reaped}"
        )

    def test_null_heartbeat_hermes_queued_task_is_still_reaped(
        self, monkeypatch, tmp_path
    ):
        """NULL heartbeat (legacy row) keeps the legacy queued-age reap."""
        from dispatcher.db import get_conn
        from dispatcher.executor_runs import upsert_run
        from dispatcher.reaper import ReaperConfig, reap_once
        from dispatcher.manager import TaskManager

        setup_temp_db(monkeypatch, tmp_path)
        get_conn()
        tm = TaskManager()

        task_id = "TASK-20260908-9003"
        self._make_stale_queued_task(task_id, age_sec=400)
        conn = get_conn()
        upsert_run(
            conn,
            run_id="run_hermes_reaper_fix_nullhb",
            requested_executor="hermes",
            selected_executor="hermes",
            task_id=task_id,
            status="running",
            progress=0.0,
            last_heartbeat_at=None,
        )
        conn.commit()

        cfg = ReaperConfig(enabled=True, stale_queued_sec=300)
        result = reap_once(tm, cfg)
        assert task_id in result.reaped, (
            f"NULL heartbeat must fail-safe to the legacy reap; "
            f"reaped={result.reaped}"
        )

    def test_non_hermes_executor_owned_queued_task_still_skipped(
        self, monkeypatch, tmp_path
    ):
        """Regression: the claude-code-cli unconditional skip is intact."""
        from dispatcher.db import get_conn
        from dispatcher.executor_runs import upsert_run
        from dispatcher.reaper import ReaperConfig, reap_once
        from dispatcher.manager import TaskManager

        setup_temp_db(monkeypatch, tmp_path)
        get_conn()
        tm = TaskManager()

        task_id = "TASK-20260908-9004"
        self._make_stale_queued_task(task_id, age_sec=400)
        conn = get_conn()
        upsert_run(
            conn,
            run_id="claude-cli-reaper-fix-1",
            requested_executor="claude-code-cli",
            selected_executor="claude-code-cli",
            task_id=task_id,
            status="running",
            progress=0.0,
            # deliberately NO heartbeat — non-Hermes skip is unconditional
        )
        conn.commit()

        cfg = ReaperConfig(enabled=True, stale_queued_sec=300)
        result = reap_once(tm, cfg)
        assert task_id not in result.reaped, (
            f"claude-code-cli owned queued task must keep the existing "
            f"unconditional skip; reaped={result.reaped}"
        )


# ---------------------------------------------------------------------------
# T3 — hermes terminal completed converges tasks + executor_runs
# ---------------------------------------------------------------------------
class TestHermesTerminalConvergence:
    def test_watcher_reconcile_completes_both_tables(
        self, monkeypatch, tmp_path
    ):
        from dispatcher.manager import TaskManager
        from dispatcher.db import get_conn

        _stub, _calls = _install_hermes_stub(
            monkeypatch, submit_status="queued", poll_status="completed"
        )
        client, _app, key = make_client(monkeypatch, tmp_path)

        resp = post_executor(client, key, {
            "executor": "hermes",
            "prompt": "convergence probe",
            "timeout_sec": 30,
        })
        assert resp.status_code == 200, resp.text
        env = resp.json()
        run_id, task_id = env["run_id"], env["task_id"]

        # The GET-driven reconcile polls upstream (terminal completed),
        # persists the executor_runs row AND mirrors the terminal verdict
        # into the dispatcher task.
        get1 = _get_run(client, key, run_id)
        assert get1.status_code == 200, get1.text
        assert get1.json()["status"] == "completed"

        tm = TaskManager()
        assert tm.get(task_id).status == "completed", (
            "dispatcher task must converge to completed when the hermes "
            "run reaches terminal completion"
        )

    def test_watcher_reconcile_fails_both_tables(
        self, monkeypatch, tmp_path
    ):
        from dispatcher.manager import TaskManager

        _stub, _calls = _install_hermes_stub(
            monkeypatch, submit_status="queued", poll_status="failed"
        )
        client, _app, key = make_client(monkeypatch, tmp_path)

        resp = post_executor(client, key, {
            "executor": "hermes",
            "prompt": "failure convergence probe",
            "timeout_sec": 30,
        })
        assert resp.status_code == 200, resp.text
        run_id, task_id = resp.json()["run_id"], resp.json()["task_id"]

        get1 = _get_run(client, key, run_id)
        assert get1.status_code == 200, get1.text
        assert get1.json()["status"] == "failed"

        tm = TaskManager()
        task = tm.get(task_id)
        assert task.status == "failed", (
            f"dispatcher task must converge to failed, got {task.status!r}"
        )
        assert task.hermes_run_id == run_id, (
            "reconcile must stamp the real run_id for find_by_hermes_run_id"
        )


# ---------------------------------------------------------------------------
# T4 — timeout/failure sync when hermes_run_id is NULL but task_id maps
# ---------------------------------------------------------------------------
class TestTimeoutSyncTaskIdFallback:
    def test_timeout_syncs_executor_runs_via_task_id_fallback(
        self, monkeypatch, tmp_path
    ):
        from dispatcher.db import get_conn, transaction
        from dispatcher.executor_runs import upsert_run
        from dispatcher.manager import TaskManager

        setup_temp_db(monkeypatch, tmp_path)
        get_conn()
        tm = TaskManager()
        conn = get_conn()

        t = tm.create(
            title="executor-run:hermes", type="ops", input_text="x",
            owner="m2", initial_status="queued",
        )
        task_id = t.task_id
        # Advance to running via the manager (the lifecycle mirror path).
        tm.start(task_id, "run_sync_fallback_probe")

        upsert_run(
            conn,
            run_id="run_sync_fallback_probe",
            requested_executor="hermes",
            selected_executor="hermes",
            task_id=task_id,
            status="running",
            progress=0.0,
        )
        conn.commit()

        # Force the exact divergence from the audit: hermes_run_id NULL
        # at terminal time.
        with transaction() as c2:
            c2.execute(
                "UPDATE tasks SET hermes_run_id=NULL, external_run_id=NULL "
                "WHERE task_id=?",
                (task_id,),
            )

        # The reaper timeout path: with the fix, the terminal status must
        # be mirrored onto the executor_runs row via the task_id fallback.
        tm.timeout(task_id, "reaper: queued 400s exceeds stale_queued_sec=300")

        row = conn.execute(
            "SELECT status FROM executor_runs WHERE run_id=?",
            ("run_sync_fallback_probe",),
        ).fetchone()
        assert row is not None
        assert row["status"] == "timeout", (
            f"timeout must sync into executor_runs via the task_id "
            f"fallback even with NULL hermes_run_id; got {row['status']!r}"
        )

    def test_failed_syncs_executor_runs_via_task_id_fallback(
        self, monkeypatch, tmp_path
    ):
        from dispatcher.db import get_conn, transaction
        from dispatcher.executor_runs import upsert_run
        from dispatcher.manager import TaskManager

        setup_temp_db(monkeypatch, tmp_path)
        get_conn()
        tm = TaskManager()
        conn = get_conn()

        t = tm.create(
            title="executor-run:hermes", type="ops", input_text="x",
            owner="m2", initial_status="queued",
        )
        task_id = t.task_id
        tm.start(task_id, "run_fail_fallback_probe")

        upsert_run(
            conn,
            run_id="run_fail_fallback_probe",
            requested_executor="hermes",
            selected_executor="hermes",
            task_id=task_id,
            status="running",
            progress=0.0,
        )
        conn.commit()

        with transaction() as c2:
            c2.execute(
                "UPDATE tasks SET hermes_run_id=NULL, external_run_id=NULL "
                "WHERE task_id=?",
                (task_id,),
            )

        tm.fail(task_id, "simulated hermes failure")

        row = conn.execute(
            "SELECT status FROM executor_runs WHERE run_id=?",
            ("run_fail_fallback_probe",),
        ).fetchone()
        assert row is not None
        assert row["status"] == "failed", (
            f"failed must sync into executor_runs via the task_id "
            f"fallback; got {row['status']!r}"
        )

    def test_task_without_any_executor_runs_row_still_noop(
        self, monkeypatch, tmp_path
    ):
        """No executor_runs row → nothing to sync (bounded, no crash)."""
        from dispatcher.db import get_conn
        from dispatcher.manager import TaskManager

        setup_temp_db(monkeypatch, tmp_path)
        get_conn()
        tm = TaskManager()

        t = tm.create(
            title="never-dispatched", type="ops", input_text="x",
            owner="m2", initial_status="queued",
        )
        # terminal transition on a task with no run_id anywhere
        tm.timeout(t.task_id, "probe")
        assert tm.get(t.task_id).status == "timeout"


# ---------------------------------------------------------------------------
# T5 — hermes submit failure converges the task to failed
# ---------------------------------------------------------------------------
class TestHermesSubmitFailureConvergence:
    def test_submit_error_marks_task_failed(self, monkeypatch, tmp_path):
        from aee.adapters.base import (
            RuntimePollResult,
            RuntimeSubmitResult,
            RuntimeError as AdapterRuntimeError,
        )
        from aee.core.registry import adapter_registry
        from dispatcher.manager import TaskManager

        class _FailingStub:
            name = "hermes"
            runtime_type = "hermes"

            async def submit(self, job):
                raise AdapterRuntimeError("simulated hermes outage")

            async def poll(self, external_run_id):
                raise AdapterRuntimeError("simulated hermes outage")

            async def cancel(self, external_run_id):
                from aee.adapters.base import RuntimeCancelResult
                return RuntimeCancelResult(external_run_id=external_run_id, cancelled=True)

        saved = dict(adapter_registry._adapters)
        monkeypatch.setattr(adapter_registry, "_adapters", saved, raising=False)
        adapter_registry._adapters["hermes"] = _FailingStub()

        client, _app, key = make_client(monkeypatch, tmp_path)
        resp = post_executor(client, key, {
            "executor": "hermes",
            "prompt": "submit failure probe",
            "timeout_sec": 30,
        })
        assert resp.status_code == 200, resp.text
        env = resp.json()
        assert env["status"] == "failed"
        task_id = env.get("task_id")
        assert task_id

        tm = TaskManager()
        assert tm.get(task_id).status == "failed", (
            "submit failure must converge the dispatcher task to failed "
            "(no queued row left for the reaper)"
        )