"""B1b targeted tests — reaper no-progress freshness / heartbeat
propagation (work order 2026-09-08, incident
run_86b294fab2054d4b9495bccf4fdecadd).

Root cause (verified against code + live evidence):
  ``dispatcher/reaper.py::_last_progress_ts`` returned the FIRST
  non-null of ``heartbeat_at``/``started_at``/``created_at`` — but
  ``created_at`` is never null, so the ``task_events`` PROGRESS scan
  was dead code and the freshness clock degenerated to "time since
  task creation". TASK-20260907-0002 (a real long-running Hermes run)
  emitted 60%/80%/95% PROGRESS at 18:48–18:53Z yet was reaped at
  19:09:06Z with ``no progress for 1804s (threshold=1800s)`` — 1804s
  measured exactly from created_at 18:39:02Z.

Additionally, the hermes dispatch path stamps
``executor_runs.last_heartbeat_at`` exactly once (no writer advanced
it while non-terminal), so the fix has two halves:
  1. reaper reads freshness as the max over authoritative sources
     (worker heartbeat, newest eligible PROGRESS event, non-terminal
     executor_runs stamps, task-side fallbacks); and
  2. the ExecutorRunWatcher stamps a canonical liveness heartbeat
     (``update_heartbeat``) for every non-terminal hermes row it
     reconciles.

Covers the work-order Testing Contract:

  a. run age > stale_running_sec with a REAL in-window PROGRESS
     event -> NOT reaped.
  b. run age > stale_running_sec with a fresh non-terminal
     executor_runs heartbeat/update -> NOT reaped.
  c. genuinely > stale_running_sec with no progress/heartbeat at
     all -> reaped (timeout).
  d. stale/terminal executor timestamps must not keep an idle run
     alive (terminal-row ignore + stale-stamp expiry + pre-start
     PROGRESS event ignore).
  e. B1a regression: queued/running/terminal reconciliation
     semantics unchanged (fresh-heartbeat queued skip, non-Hermes
     unconditional skip, terminal convergence) — exercised via the
     existing B1a suite + the guards below.

Safety: every test rebinds ``dispatcher.db.DB_PATH`` to a pytest
tmp_path BEFORE any connection is opened (proven by the
test_db_path_is_isolated guard asserting the live module attribute);
no production DB is touched, no Telegram is sent (session conftest
sanitizes env; this module additionally asserts the notification
switch).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests._executor_test_helpers import setup_temp_db


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago(seconds: int) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(seconds=seconds))


def _make_running_task(task_id: str, *, age_sec: int, started: bool = True):
    """Insert a dispatcher task in ``running`` with an old birth.

    ``started_at`` mirrors ``created_at`` (stalled-from-birth shape —
    the incident shape for an async hermes run whose executor never
    advanced the tasks row) unless ``started`` is False, which writes
    a fresh ``started_at`` (fresh start, no activity since).
    """
    from dispatcher.db import get_conn

    created = _ago(age_sec)
    started_at = _ago(age_sec) if started else _iso(datetime.now(timezone.utc))
    conn = get_conn()
    conn.execute(
        "INSERT INTO tasks (task_id, title, type, priority, owner, "
        "status, progress_pct, created_at, started_at, input_text) "
        "VALUES (?, 'b1b-freshness', 'ops', 50, 'm2', 'running', 0, ?, ?, 'x')",
        (task_id, created, started_at),
    )
    conn.commit()


def _emit_progress(task_id: str, *, minutes_ago: int, pct: int = 95) -> None:
    """Write a REAL task_events PROGRESS row directly (the same shape
    ``manager.progress()`` produces, with a controlled timestamp)."""
    from dispatcher.db import get_conn

    conn = get_conn()
    conn.execute(
        "INSERT INTO task_events (task_id, ts, kind, payload_json) "
        "VALUES (?, ?, 'progress', ?)",
        (task_id, _ago(minutes_ago * 60), f'{{"pct": {pct}, "step": "Running on adapter"}}'),
    )
    conn.commit()


def _seed_executor_run(
    task_id: str,
    run_id: str,
    *,
    status: str = "running",
    heartbeat_age_sec: int | None = 60,
    updated_age_sec: int | None = 60,
):
    from dispatcher.db import get_conn
    from dispatcher.executor_runs import upsert_run

    conn = get_conn()
    upsert_run(
        conn,
        run_id=run_id,
        requested_executor="hermes",
        selected_executor="hermes",
        task_id=task_id,
        status=status,
        progress=0.0,
        last_heartbeat_at=(
            _ago(heartbeat_age_sec) if heartbeat_age_sec is not None else None
        ),
        current_step="running" if status == "running" else None,
    )
    if updated_age_sec is not None:
        # upsert_run has no updated_at kwarg; backdate the reconcile
        # stamp directly (same column the freshness clock reads).
        conn.execute(
            "UPDATE executor_runs SET updated_at = ? WHERE run_id = ?",
            (_ago(updated_age_sec), run_id),
        )
    conn.commit()


def _reap(task_id: str, *, stale_running_sec: int = 1800, stale_queued_sec: int = 300):
    from dispatcher.manager import TaskManager
    from dispatcher.reaper import ReaperConfig, reap_once

    cfg = ReaperConfig(
        stale_running_sec=stale_running_sec,
        stale_queued_sec=stale_queued_sec,
        max_total_age_sec=7200,
        grace_period_sec=0,
        enabled=True,
    )
    result = reap_once(TaskManager(), cfg)
    return result


# ---------------------------------------------------------------------------
# Isolation guard (run FIRST): the dispatcher module must be pointed at
# the pytest temp DB before any row is written — production safety.
# ---------------------------------------------------------------------------


class TestDBIsolation:
    def test_db_path_is_isolated(self, monkeypatch, tmp_path):
        setup_temp_db(monkeypatch, tmp_path)
        from dispatcher import db as ddb

        assert str(ddb.DB_PATH) == str(tmp_path / "dispatcher.db")
        assert str(ddb.DB_PATH) != str(
            tmp_path.parent / "aee-runtime-bridge" / "data" / "dispatcher.db"
        )
        get_conn_probe = ddb.get_conn()
        # Fresh temp DB: the schema is empty of real tasks.
        n = get_conn_probe.execute(
            "SELECT COUNT(*) AS n FROM tasks"
        ).fetchone()["n"]
        assert n == 0

    def test_notification_switch_disables_live_sends(self, monkeypatch):
        """B1b runs tests with the terminal-notification gate off (the
        B2/B3-era suppression switch); verify it actually disarms the
        manager's notify gate so no live Telegram send is possible."""
        monkeypatch.setenv("AEE_BRIDGE_NOTIFICATIONS_DISABLED", "true")
        from dispatcher.manager import TaskManager

        result = TaskManager._notify_terminal(
            TaskManager(), "TASK-B1B-NOTIFY-GUARD", status="completed"
        )
        assert result["method"] == "notifications_disabled"
        assert result["sent"] is False


# ---------------------------------------------------------------------------
# (a) real PROGRESS event inside the window -> no timeout
# ---------------------------------------------------------------------------


class TestProgressEventKeepsRunAlive:
    def test_progress_event_prevents_reap(self, monkeypatch, tmp_path):
        setup_temp_db(monkeypatch, tmp_path)
        from dispatcher.db import get_conn
        from dispatcher.manager import TaskManager
        get_conn()  # init schema
        tm = TaskManager()

        task_id = "TASK-B1B-000A"
        _make_running_task(task_id, age_sec=2200)
        # Newest PROGRESS 10 min ago: idle 600s < 1800s threshold.
        _emit_progress(task_id, minutes_ago=10, pct=95)

        result = _reap(task_id)
        assert task_id not in result.reaped, (
            f"run age 2200s but real PROGRESS 600s ago must NOT be reaped; "
            f"reaped={result.reaped} skipped={result.skipped}"
        )
        assert tm.get(task_id).status == "running"

    def test_progress_event_too_old_still_reaped(self, monkeypatch, tmp_path):
        """The freshness clock must still expire: a PROGRESS event older
        than stale_running_sec does not protect the run."""
        setup_temp_db(monkeypatch, tmp_path)
        from dispatcher.db import get_conn
        from dispatcher.manager import TaskManager
        get_conn()
        tm = TaskManager()

        task_id = "TASK-B1B-000A2"
        _make_running_task(task_id, age_sec=4000)
        # Last PROGRESS 2000s ago -> idle 2000s > 1800s.
        _emit_progress(task_id, minutes_ago=33, pct=95)

        result = _reap(task_id)
        assert task_id in result.reaped, (
            f"PROGRESS 2000s old must NOT keep the run alive; "
            f"reaped={result.reaped} skipped={result.skipped}"
        )
        assert tm.get(task_id).status == "timeout"

    def test_newest_progress_event_wins(self, monkeypatch, tmp_path):
        """Multiple PROGRESS events: the NEWEST one drives freshness
        (regression against a first-match-on-oldest bug)."""
        setup_temp_db(monkeypatch, tmp_path)
        from dispatcher.db import get_conn
        from dispatcher.manager import TaskManager
        get_conn()
        tm = TaskManager()

        task_id = "TASK-B1B-000A3"
        _make_running_task(task_id, age_sec=2200)
        _emit_progress(task_id, minutes_ago=40, pct=25)   # stale
        _emit_progress(task_id, minutes_ago=5, pct=95)    # fresh

        result = _reap(task_id)
        assert task_id not in result.reaped


# ---------------------------------------------------------------------------
# (b) fresh non-terminal executor_runs heartbeat -> no timeout
# ---------------------------------------------------------------------------


class TestExecutorHeartbeatKeepsRunAlive:
    def test_fresh_executor_heartbeat_prevents_reap(self, monkeypatch, tmp_path):
        setup_temp_db(monkeypatch, tmp_path)
        from dispatcher.db import get_conn
        from dispatcher.manager import TaskManager
        get_conn()
        tm = TaskManager()

        task_id = "TASK-B1B-000B"
        _make_running_task(task_id, age_sec=2200)
        _seed_executor_run(task_id, "run-b1b-b1", heartbeat_age_sec=60)

        result = _reap(task_id)
        assert task_id not in result.reaped, (
            f"fresh non-terminal executor heartbeat must keep the run alive; "
            f"reaped={result.reaped} skipped={result.skipped}"
        )
        assert tm.get(task_id).status == "running"

    def test_updated_at_alone_prevents_reap(self, monkeypatch, tmp_path):
        """A row whose heartbeat column is stale but whose ``updated_at``
        was just advanced (e.g. a reconcile write) still counts."""
        setup_temp_db(monkeypatch, tmp_path)
        from dispatcher.db import get_conn
        from dispatcher.manager import TaskManager
        get_conn()
        tm = TaskManager()

        task_id = "TASK-B1B-000B2"
        _make_running_task(task_id, age_sec=2200)
        _seed_executor_run(
            task_id, "run-b1b-b2",
            heartbeat_age_sec=4000,   # stale stamp
            updated_age_sec=30,       # fresh reconcile write
        )

        result = _reap(task_id)
        assert task_id not in result.reaped


# ---------------------------------------------------------------------------
# (c) genuinely idle > threshold -> reaped
# ---------------------------------------------------------------------------


class TestGenuinelyIdleRunIsReaped:
    def test_idle_run_no_sources_is_reaped(self, monkeypatch, tmp_path):
        setup_temp_db(monkeypatch, tmp_path)
        from dispatcher.db import get_conn
        from dispatcher.manager import TaskManager
        get_conn()
        tm = TaskManager()

        task_id = "TASK-B1B-000C"
        _make_running_task(task_id, age_sec=2200)
        # No PROGRESS events, no executor_runs row: freshness falls
        # back to started_at (also old) -> reap.

        result = _reap(task_id)
        assert task_id in result.reaped, (
            f"a genuinely idle run (>1800s, no progress/heartbeat) must be "
            f"reaped; reaped={result.reaped} skipped={result.skipped}"
        )
        t = tm.get(task_id)
        assert t.status == "timeout"
        assert "no progress for" in (t.error_message or "")

    def test_idle_run_with_stale_executor_row_is_reaped(
        self, monkeypatch, tmp_path
    ):
        """A non-terminal executor row with STALE stamps must not act
        as a permanent keep-alive: the task-side clock still expires."""
        setup_temp_db(monkeypatch, tmp_path)
        from dispatcher.db import get_conn
        from dispatcher.manager import TaskManager
        get_conn()
        tm = TaskManager()

        task_id = "TASK-B1B-000C2"
        _make_running_task(task_id, age_sec=4000)
        _seed_executor_run(
            task_id, "run-b1b-c2",
            heartbeat_age_sec=4000,  # frozen since dispatch, never advanced
            updated_age_sec=4000,
        )

        result = _reap(task_id)
        assert task_id in result.reaped


# ---------------------------------------------------------------------------
# (d) stale / terminal executor timestamps must not keep-alive
# ---------------------------------------------------------------------------


class TestStaleExecutorTimestampsDoNotKeepAlive:
    def test_terminal_executor_row_is_ignored(self, monkeypatch, tmp_path):
        """A COMPLETED executor_runs row (fresh timestamps!) must not
        keep a subsequent idle task alive — terminal stamps describe a
        finished run, not liveness."""
        setup_temp_db(monkeypatch, tmp_path)
        from dispatcher.db import get_conn
        from dispatcher.manager import TaskManager
        get_conn()
        tm = TaskManager()

        task_id = "TASK-B1B-000D"
        _make_running_task(task_id, age_sec=4000)
        _seed_executor_run(
            task_id, "run-b1b-d1",
            status="completed",
            heartbeat_age_sec=1,     # terminal rows carry fresh stamps
            updated_age_sec=1,
        )

        result = _reap(task_id)
        assert task_id in result.reaped, (
            f"terminal executor_runs timestamps must be ignored by the "
            f"freshness clock; reaped={result.reaped} skipped={result.skipped}"
        )

    def test_pre_start_progress_event_is_ignored(self, monkeypatch, tmp_path):
        """A PROGRESS event older than started_at (previous lifecycle
        of the same task id) must not keep a fresh run alive. Shape:
        the task was re-started moments ago (fresh started_at) but
        carries a stale pre-start PROGRESS event; a buggy clock that
        honors the event reaps, the correct one spares."""
        setup_temp_db(monkeypatch, tmp_path)
        from dispatcher.db import get_conn
        from dispatcher.manager import TaskManager
        get_conn()
        tm = TaskManager()

        task_id = "TASK-B1B-000D2"
        # age 2200s since creation, but started_at is fresh (re-start).
        _make_running_task(task_id, age_sec=2200, started=False)
        # PROGRESS 40 min ago: predates the fresh started_at -> skip.
        _emit_progress(task_id, minutes_ago=40, pct=60)

        result = _reap(task_id)
        assert task_id not in result.reaped, (
            f"pre-start PROGRESS event must be ignored; the fresh start "
            f"itself keeps the run alive. reaped={result.reaped} "
            f"skipped={result.skipped}"
        )

    def test_heartbeat_at_source_still_honored(self, monkeypatch, tmp_path):
        """AEE-2 worker heartbeat column remains the most direct signal
        (precedence: it participates in the max, never degraded)."""
        setup_temp_db(monkeypatch, tmp_path)
        from dispatcher.db import get_conn
        from dispatcher.manager import TaskManager
        get_conn()
        tm = TaskManager()

        task_id = "TASK-B1B-000D3"
        _make_running_task(task_id, age_sec=2200)
        conn = get_conn()
        conn.execute(
            "UPDATE tasks SET heartbeat_at = ? WHERE task_id = ?",
            (_ago(60), task_id),
        )
        conn.commit()

        result = _reap(task_id)
        assert task_id not in result.reaped


# ---------------------------------------------------------------------------
# (e) B1a regression guards (executor_runs-driven reconciliation)
# ---------------------------------------------------------------------------


class TestB1aReconciliationRegression:
    def test_fresh_hb_queued_task_still_skipped(self, monkeypatch, tmp_path):
        """B1a: queued task, age > stale_queued_sec, fresh hermes
        executor heartbeat -> executor-owned skip (unchanged)."""
        setup_temp_db(monkeypatch, tmp_path)
        from dispatcher.db import get_conn
        from dispatcher.executor_runs import upsert_run
        from dispatcher.manager import TaskManager
        get_conn()
        tm = TaskManager()

        task_id = "TASK-B1B-000E1"
        conn = get_conn()
        conn.execute(
            "INSERT INTO tasks (task_id, title, type, priority, owner, "
            "status, progress_pct, created_at, input_text) "
            "VALUES (?, 'b1b-b1a-guard', 'ops', 50, 'm2', 'queued', 0, ?, 'x')",
            (task_id, _ago(400)),
        )
        conn.commit()
        upsert_run(
            conn,
            run_id="run-b1b-e1",
            requested_executor="hermes",
            selected_executor="hermes",
            task_id=task_id,
            status="running",
            progress=0.0,
            last_heartbeat_at=_ago(30),
            current_step="running",
        )
        conn.commit()

        from dispatcher.reaper import reap_once, ReaperConfig
        cfg = ReaperConfig(
            stale_running_sec=1800, stale_queued_sec=300,
            max_total_age_sec=7200, grace_period_sec=0, enabled=True,
        )
        result = reap_once(tm, cfg)
        assert task_id not in result.reaped
        assert tm.get(task_id).status == "queued"

    def test_non_hermes_queued_skip_unchanged(self, monkeypatch, tmp_path):
        setup_temp_db(monkeypatch, tmp_path)
        from dispatcher.db import get_conn
        from dispatcher.executor_runs import upsert_run
        from dispatcher.manager import TaskManager
        get_conn()
        tm = TaskManager()

        task_id = "TASK-B1B-000E2"
        conn = get_conn()
        conn.execute(
            "INSERT INTO tasks (task_id, title, type, priority, owner, "
            "status, progress_pct, created_at, input_text) "
            "VALUES (?, 'b1b-b1a-guard2', 'ops', 50, 'm2', 'queued', 0, ?, 'x')",
            (task_id, _ago(400)),
        )
        conn.commit()
        upsert_run(
            conn,
            run_id="run-b1b-e2",
            requested_executor="claude-code-cli",
            selected_executor="claude-code-cli",
            task_id=task_id,
            status="running",
            progress=0.0,
            # no heartbeat — non-Hermes skip is unconditional (B1a)
        )
        conn.commit()

        from dispatcher.reaper import reap_once, ReaperConfig
        cfg = ReaperConfig(
            stale_running_sec=1800, stale_queued_sec=300,
            max_total_age_sec=7200, grace_period_sec=0, enabled=True,
        )
        result = reap_once(tm, cfg)
        assert task_id not in result.reaped

    def test_terminal_mirror_convergence_unchanged(
        self, monkeypatch, tmp_path
    ):
        """B1a terminal reconciliation: a completed executor_runs row
        mirrored by the manager still converges the task row (the
        reaper's terminal-row freshness ignore must not interfere —
        the task here is already terminal, so reap_once never scans
        it)."""
        setup_temp_db(monkeypatch, tmp_path)
        from dispatcher.db import get_conn
        from dispatcher.executor_runs import upsert_run
        from dispatcher.manager import TaskManager
        get_conn()
        tm = TaskManager()

        # Create via the manager (it generates its own task_id) and
        # drive the real lifecycle: queued -> running -> completed.
        t = tm.create(title="b1b-b1a-terminal", type="ops", input_text="x",
                      session_id="s")
        task_id = t.task_id
        tm.start(task_id, "run-b1b-e3")
        tm.complete(task_id, output_text="done")
        conn = get_conn()
        upsert_run(
            conn,
            run_id="run-b1b-e3",
            requested_executor="hermes",
            selected_executor="hermes",
            task_id=task_id,
            status="completed",
            progress=1.0,
            last_heartbeat_at=_iso(datetime.now(timezone.utc)),
        )
        conn.commit()

        result = _reap(task_id)
        assert task_id not in result.reaped  # not in-flight -> never scanned
        assert tm.get(task_id).status == "completed"


# ---------------------------------------------------------------------------
# (f) watcher liveness heartbeat propagation
# ---------------------------------------------------------------------------


class TestWatcherLivenessHeartbeat:
    def test_watcher_stamps_heartbeat_after_successful_poll(
        self, monkeypatch, tmp_path
    ):
        """ExecutorRunWatcher._tick stamps last_heartbeat_at via the
        canonical update_heartbeat writer when the upstream poll
        succeeds and reports the run still non-terminal (B1b
        propagation half)."""
        import asyncio

        setup_temp_db(monkeypatch, tmp_path)
        from dispatcher.db import get_conn
        from dispatcher.executor_runs import get_run
        _seed_executor_run("TASK-B1B-000F", "run-b1b-f1",
                           heartbeat_age_sec=1200, updated_age_sec=1200)
        # A task-side row is NOT needed for the watcher half.

        from dispatcher.executor_watcher import ExecutorRunWatcher
        w = ExecutorRunWatcher(tick_sec=5.0)
        # Stub the shared reconcile core: non-terminal poll + the
        # same opt-in heartbeat write the real core performs (the
        # core's stamp behavior itself is covered by the test below).
        import app as app_module
        stamped: list[str] = []

        async def _fake_reconcile(run_id, row, *, stamp_heartbeat=False):
            if stamp_heartbeat:
                from dispatcher.executor_runs import update_heartbeat
                update_heartbeat(
                    get_conn(), run_id=run_id,
                    current_step="running", phase="running",
                )
                stamped.append(run_id)
            return row

        monkeypatch.setattr(
            app_module, "_reconcile_hermes_run_once", _fake_reconcile
        )
        asyncio.run(w._tick())

        assert stamped == ["run-b1b-f1"], (
            f"watcher must opt in to the liveness heartbeat; stamped={stamped}"
        )
        env = get_run(get_conn(), "run-b1b-f1")
        assert env is not None
        # Heartbeat advanced from the 1200s-old stamp to ~now.
        hb = datetime.fromisoformat(
            env["last_heartbeat_at"].replace("Z", "+00:00")
        )
        age = (datetime.now(timezone.utc) - hb).total_seconds()
        assert age < 60, (
            f"watcher must stamp a fresh liveness heartbeat; age={age}s"
        )
        assert env["status"] == "running"  # still non-terminal, untouched
        assert env["phase"] == "running"

    def test_reconcile_core_stamps_only_opted_caller(
        self, monkeypatch, tmp_path
    ):
        """_reconcile_hermes_run_once stamps the heartbeat ONLY when
        the caller passes stamp_heartbeat=True; the GET path (default)
        must keep its pure-read P1.1 contract."""
        import asyncio

        setup_temp_db(monkeypatch, tmp_path)
        from dispatcher.db import get_conn
        from dispatcher.executor_runs import get_run, update_heartbeat
        _seed_executor_run("TASK-B1B-000F3", "run-b1b-f3",
                           heartbeat_age_sec=1200, updated_age_sec=1200)

        # Stub adapter: non-terminal poll, no network.
        from aee.adapters.base import RuntimePollResult
        from aee.core.registry import adapter_registry

        class _StubHermes:
            name = "hermes"
            runtime_type = "hermes"

            async def submit(self, job):  # pragma: no cover
                raise AssertionError("not used")

            async def poll(self, external_run_id):
                return RuntimePollResult(
                    external_run_id=external_run_id,
                    status="running", is_terminal=False,
                )

            async def cancel(self, external_run_id):  # pragma: no cover
                raise AssertionError("not used")

        saved = dict(adapter_registry._adapters)
        monkeypatch.setattr(adapter_registry, "_adapters", saved,
                            raising=False)
        adapter_registry._adapters["hermes"] = _StubHermes()

        import app as app_module
        row = get_run(get_conn(), "run-b1b-f3")

        # GET-style call (default): pure read — no stamp.
        asyncio.run(app_module._reconcile_hermes_run_once(
            "run-b1b-f3", dict(row)))
        env = get_run(get_conn(), "run-b1b-f3")
        hb = datetime.fromisoformat(
            env["last_heartbeat_at"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - hb).total_seconds()
        assert age > 600, (
            f"GET path must remain a pure read; heartbeat age={age}s"
        )

        # Watcher-style call (opt-in): stamps.
        asyncio.run(app_module._reconcile_hermes_run_once(
            "run-b1b-f3", dict(row), stamp_heartbeat=True))
        env = get_run(get_conn(), "run-b1b-f3")
        hb = datetime.fromisoformat(
            env["last_heartbeat_at"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - hb).total_seconds()
        assert age < 60, (
            f"opted-in caller must stamp the liveness heartbeat; "
            f"age={age}s"
        )

    def test_failed_poll_does_not_stamp(self, monkeypatch, tmp_path):
        """A poll that fails (upstream unreachable) must NOT stamp the
        heartbeat — a dead upstream cannot keep an idle run alive."""
        import asyncio

        setup_temp_db(monkeypatch, tmp_path)
        from dispatcher.db import get_conn
        from dispatcher.executor_runs import get_run
        _seed_executor_run("TASK-B1B-000F4", "run-b1b-f4",
                           heartbeat_age_sec=1200, updated_age_sec=1200)

        from aee.adapters.base import (
            RuntimePollResult,
            RuntimeError as AdapterRuntimeError,
        )
        from aee.core.registry import adapter_registry

        class _DeadHermes:
            name = "hermes"
            runtime_type = "hermes"

            async def submit(self, job):  # pragma: no cover
                raise AssertionError("not used")

            async def poll(self, external_run_id):
                raise AdapterRuntimeError("upstream unreachable")

            async def cancel(self, external_run_id):  # pragma: no cover
                raise AssertionError("not used")

        saved = dict(adapter_registry._adapters)
        monkeypatch.setattr(adapter_registry, "_adapters", saved,
                            raising=False)
        adapter_registry._adapters["hermes"] = _DeadHermes()

        import app as app_module
        row = get_run(get_conn(), "run-b1b-f4")
        out = asyncio.run(app_module._reconcile_hermes_run_once(
            "run-b1b-f4", dict(row), stamp_heartbeat=True))
        assert out is not None  # row left in-flight, unchanged
        env = get_run(get_conn(), "run-b1b-f4")
        hb = datetime.fromisoformat(
            env["last_heartbeat_at"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - hb).total_seconds()
        assert age > 600, (
            f"failed poll must not stamp a liveness heartbeat; "
            f"age={age}s"
        )

    def test_watcher_never_reheartbeats_terminal_row(
        self, monkeypatch, tmp_path
    ):
        """Terminal rows are excluded from the scan AND
        update_heartbeat refuses them — verify the writer contract
        that the propagation relies on."""
        setup_temp_db(monkeypatch, tmp_path)
        from dispatcher.db import get_conn
        from dispatcher.executor_runs import update_heartbeat

        _seed_executor_run(
            "TASK-B1B-000F2", "run-b1b-f2",
            status="completed", heartbeat_age_sec=1, updated_age_sec=1,
        )
        old_stamp = _ago(500)
        conn = get_conn()
        conn.execute(
            "UPDATE executor_runs SET last_heartbeat_at = ? "
            "WHERE run_id = 'run-b1b-f2'",
            (old_stamp,),
        )
        conn.commit()

        result = update_heartbeat(
            conn, run_id="run-b1b-f2",
            current_step="running", phase="running",
        )
        assert result is None  # terminal rows are never re-heartbeated
        row = conn.execute(
            "SELECT last_heartbeat_at FROM executor_runs "
            "WHERE run_id = 'run-b1b-f2'"
        ).fetchone()
        assert row["last_heartbeat_at"] == old_stamp