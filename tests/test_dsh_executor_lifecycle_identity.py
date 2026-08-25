"""Regression tests for the DSH executor lifecycle identity fix
(TASK-20260825-0027).

The pre-fix code had three independent failure modes that
collaborated to surface the ``queued task reaped after 300s with
Hermes run: — and Executor: unknown`` symptom:

1. ``dispatcher.watcher.Watcher._tick`` special-cased the
   ``claude-cli-pending-*`` placeholder but did NOT skip
   ``dsh-headless-pending-*``. The watcher's poll path then polled
   the Hermes gateway for a DSH placeholder, got
   ``UnknownExternalRunError``, and marked the task ``timeout`` —
   racing the DSH bridge's own ``manager.complete()`` /
   ``manager.fail()`` call.

2. ``dispatcher.reaper.reap_once`` reaped any queued task older
   than ``stale_queued_sec`` (default 300s) regardless of whether
   an executor (``executor_runs`` row) had already taken ownership.
   A genuinely-healthy DSH run could be reaped just because the
   task-side ``status`` lagged the executor-side progress for
   > 300s (the DSH bridge path is synchronous-wait but downstream
   network latency to Ollama Cloud can push past that window).

3. ``TaskManager._sync_executor_runs_status`` used
   ``trow["adapter_name"] or "hermes"`` as the
   ``selected_executor`` fallback. Whenever the dispatch branch
   had not yet stamped ``adapter_name`` (a metadata-lag race in
   the placeholder path), the executor-runs row was downgraded
   to ``selected_executor="hermes"`` and the operator dashboard
   showed ``Executor: unknown``.

The fix introduces a centralised predicate module
(``dispatcher/executor_identity.py``) and uses it in all three
sites. These tests prove:

  a) The watcher skip predicate recognises DSH-pending placeholders
     via the centralised helper (not via a new inline ``startswith``
     check).
  b) The claude-cli-pending behaviour remains unchanged.
  c) A genuinely-stale non-executor queued task IS still reaped
     (the reaper's bounded semantics are preserved).
  d) A DSH executor identity survives the lifecycle sync / merge
     into ``executor_runs`` and does NOT degrade to ``"hermes"``
     or ``"unknown"`` just because ``adapter_name`` is None
     (the metadata-lag race).
  e) No duplicate terminal transition is produced for a DSH run
     already owned by ``executor_runs`` (the reaper skip does
     not let the manager re-fire a terminal status that the
     executor path has already set).

All tests use the hermetic fixtures in
``tests/_executor_test_helpers`` (temp dispatcher DB, no real
upstream) so they run in-process under ``pytest`` without
touching the production ``data/dispatcher.db``.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests._executor_test_helpers import setup_temp_db  # noqa: E402


# ---------------------------------------------------------------------------
# (a) Watcher skip — DSH placeholder must be skipped via the
#     centralised helper, not via a new inline `startswith` check.
# ---------------------------------------------------------------------------

class TestWatcherSkipDshPlaceholder:
    """The watcher's polling loop must skip any task whose
    ``hermes_run_id`` / ``external_run_id`` is a
    ``dsh-headless-pending-`` placeholder. Recognition is routed
    through ``dispatcher.executor_identity.is_executor_placeholder``
    so adding a new executor is a one-line edit in the helper
    module, not a new inline check in the watcher."""

    def test_centralised_helper_recognises_dsh_placeholder(self):
        from dispatcher.executor_identity import is_executor_placeholder

        assert is_executor_placeholder("dsh-headless-pending-TASK-20260825-0001"), (
            "is_executor_placeholder must recognise "
            "'dsh-headless-pending-TASK-...' as an executor placeholder"
        )
        # Also recognise the placeholder form that the bridge stamps
        # (it uses the literal task_id; we only check the prefix).
        assert is_executor_placeholder("dsh-headless-pending-anything"), (
            "is_executor_placeholder must recognise any id that starts "
            "with 'dsh-headless-pending-' as a placeholder"
        )

    def test_watcher_source_uses_centralised_helper(self):
        """Static check: ``dispatcher/watcher.py`` MUST route
        placeholder recognition through ``is_executor_placeholder``,
        not via a new inline ``startswith`` check. The whole point
        of the helper is to centralise the predicate so a future
        executor (e.g. a third CLI) is a one-line edit in the
        helper rather than scattered checks across the watcher,
        reaper, and manager."""
        watcher_src = Path("/workspace/aee-runtime-bridge/dispatcher/watcher.py").read_text()
        # The skip must import the helper
        assert "from dispatcher.executor_identity import is_executor_placeholder" in watcher_src, (
            "watcher.py must import is_executor_placeholder from "
            "dispatcher.executor_identity"
        )
        # The skip must use the helper (not a new inline startswith)
        assert "is_executor_placeholder(external_id)" in watcher_src, (
            "watcher._tick must call is_executor_placeholder(external_id) "
            "to decide whether to skip a running task's poll"
        )
        # Regression guard: ensure we did NOT regress and add a new
        # inline `startswith` for dsh-headless-pending in the watcher
        # (the whole point of the helper is to centralise these checks).
        assert 'startswith("dsh-headless-pending-")' not in watcher_src, (
            "watcher.py must NOT add a new inline startswith check for "
            "'dsh-headless-pending-'; the predicate is centralised in "
            "dispatcher.executor_identity"
        )


# ---------------------------------------------------------------------------
# (b) Claude-cli pending behaviour must remain unchanged.
# ---------------------------------------------------------------------------

class TestClaudeCliPendingUnchanged:
    """The pre-existing claude-cli-pending skip must keep working
    after the DSH fix. Adding a new placeholder prefix must not
    regress the claude-cli path."""

    def test_claude_placeholder_still_recognised(self):
        from dispatcher.executor_identity import is_executor_placeholder
        assert is_executor_placeholder("claude-cli-pending-TASK-20260824-0040"), (
            "claude-cli-pending placeholders must still be recognised "
            "after the DSH fix"
        )

    def test_hermes_real_run_id_not_a_placeholder(self):
        from dispatcher.executor_identity import is_executor_placeholder
        assert not is_executor_placeholder("hermes-run-abc123"), (
            "a real Hermes run id must NOT be recognised as a placeholder"
        )
        assert not is_executor_placeholder("dsh-headless-real-abc123"), (
            "a real DSH run id (NOT the -pending- form) must NOT be "
            "recognised as a placeholder"
        )

    def test_empty_and_none_inputs_return_false(self):
        from dispatcher.executor_identity import is_executor_placeholder
        assert not is_executor_placeholder(None)
        assert not is_executor_placeholder("")
        assert not is_executor_placeholder("dsh-headless")  # missing -pending- suffix
        assert not is_executor_placeholder("claude-cli")    # missing -pending- suffix


# ---------------------------------------------------------------------------
# (c) Genuinely-stale non-executor queued task must still be reaped.
# ---------------------------------------------------------------------------

class TestReaperStillReapsGenuineStaleQueued:
    """Bounded semantics: the reaper's queued-age check must keep
    reaping tasks that are NOT owned by an executor (i.e. a
    task with no ``executor_runs`` row, or an ``executor_runs``
    row owned by Hermes, must be reaped when it sits in ``queued``
    past ``stale_queued_sec``). The fix is scoped to executor-owned
    rows only — we must not mask genuinely stale tasks."""

    def test_queued_task_without_executor_runs_is_still_reaped(
        self, monkeypatch, tmp_path
    ):
        from dispatcher.db import get_conn
        from dispatcher.ids import next_task_id
        from dispatcher.manager import TaskManager
        from dispatcher.reaper import ReaperConfig, reap_once

        setup_temp_db(monkeypatch, tmp_path)
        get_conn()  # trigger schema init
        tm = TaskManager()

        # Create a queued task with created_at in the past (400s ago).
        old = (datetime.now(timezone.utc) - timedelta(seconds=400)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        task_id = next_task_id()
        conn = get_conn()
        conn.execute(
            "INSERT INTO tasks (task_id, title, type, priority, owner, "
            "status, progress_pct, created_at, input_text) "
            "VALUES (?, 'no-executor', 'research', 50, 'm2', 'queued', "
            "5, ?, 'x')",
            (task_id, old),
        )
        conn.commit()
        # No executor_runs row for this task — the legacy path.

        cfg = ReaperConfig(enabled=True, stale_queued_sec=300)
        result = reap_once(tm, cfg)
        assert task_id in result.reaped, (
            f"BUG: a queued task with NO executor_runs row should still "
            f"be reaped after stale_queued_sec; "
            f"reaped={result.reaped}"
        )

    def test_queued_task_with_hermes_executor_runs_is_still_reaped(
        self, monkeypatch, tmp_path
    ):
        """Hermes is the async polling path that the dispatcher
        watcher already drives. A queued task with a Hermes-owned
        ``executor_runs`` row is still the reaper's responsibility
        (the reaper is the fallback for "the watcher never started
        polling it"). The fix must not skip Hermes-owned rows."""
        from dispatcher.db import get_conn
        from dispatcher.ids import next_task_id
        from dispatcher.manager import TaskManager
        from dispatcher.executor_runs import upsert_run
        from dispatcher.reaper import ReaperConfig, reap_once

        setup_temp_db(monkeypatch, tmp_path)
        get_conn()
        tm = TaskManager()

        old = (datetime.now(timezone.utc) - timedelta(seconds=400)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        task_id = next_task_id()
        conn = get_conn()
        conn.execute(
            "INSERT INTO tasks (task_id, title, type, priority, owner, "
            "status, progress_pct, created_at, input_text) "
            "VALUES (?, 'hermes-stuck', 'research', 50, 'm2', 'queued', "
            "5, ?, 'x')",
            (task_id, old),
        )
        upsert_run(
            conn,
            run_id=f"hermes-{task_id}",
            requested_executor="hermes",
            selected_executor="hermes",  # Hermes IS the reaper's responsibility
            task_id=task_id,
            status="running",
            progress=0.0,
            routing={"selected_executor": "hermes"},
        )
        conn.commit()

        cfg = ReaperConfig(enabled=True, stale_queued_sec=300)
        result = reap_once(tm, cfg)
        assert task_id in result.reaped, (
            f"a queued task with a Hermes-owned executor_runs row must "
            f"still be reaped (the reaper is the fallback for 'watcher "
            f"never started polling it'); reaped={result.reaped}"
        )


# ---------------------------------------------------------------------------
# (d) DSH executor identity survives the lifecycle sync merge.
# ---------------------------------------------------------------------------

class TestDshIdentitySurvivesSync:
    """The pre-fix code downgraded a DSH-owned task to
    ``selected_executor="hermes"`` whenever ``adapter_name`` was
    NULL (a metadata-lag race). After the fix, the sync must
    fall back to ``runtime_type`` (also a task-side column) and
    preserve the DSH identity. This is the operator-visible
    symptom that produced ``Executor: unknown``."""

    def test_sync_preserves_dsh_when_adapter_name_is_none(
        self, monkeypatch, tmp_path
    ):
        """Simulate the exact TASK-20260825-0027 race: the task
        row was created but the dispatch branch had not yet
        stamped ``adapter_name`` (still NULL). The legacy
        ``trow["adapter_name"] or "hermes"`` fallback would have
        produced ``selected_executor="hermes"``; the fixed code
        must produce a DSH identity (preserved from
        ``runtime_type``) instead of degrading to ``"hermes"``."""
        from dispatcher.db import get_conn
        from dispatcher.ids import next_task_id
        from dispatcher.manager import TaskManager
        from dispatcher.executor_runs import get_run
        from dispatcher.executor_identity import is_dsh_executor

        setup_temp_db(monkeypatch, tmp_path)
        get_conn()
        tm = TaskManager()

        task_id = next_task_id()
        conn = get_conn()
        # Metadata-lag race: adapter_name is NULL, runtime_type is set.
        # hermes_run_id is the DSH placeholder the dispatch branch
        # stamped (proves the task was driven by DSH).
        conn.execute(
            "INSERT INTO tasks (task_id, title, type, priority, owner, "
            "status, progress_pct, created_at, input_text, "
            "hermes_run_id, runtime_type, adapter_name) "
            "VALUES (?, 'dsh-stuck', 'research', 50, 'm2', 'running', "
            "10, ?, 'x', ?, ?, NULL)",
            (
                task_id,
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                f"dsh-headless-pending-{task_id}",
                "dsh-headless",
            ),
        )
        conn.commit()

        # Trigger the lifecycle sync (the path the manager uses to
        # mirror terminal / progress into executor_runs).
        tm._sync_executor_runs_status(
            task_id, status="completed", exit_code=0,
        )

        row = get_run(conn, f"dsh-headless-pending-{task_id}")
        assert row is not None, "executor_runs row must exist after sync"
        # The fix preserves the DSH identity (selected_executor must
        # be a DSH executor). The legacy fallback would have produced
        # 'hermes' — we use the DSH predicate so the test stays
        # independent of the specific alias form ('dsh-headless' vs
        # 'dsh_headless' vs 'dsh').
        assert is_dsh_executor(row["selected_executor"]), (
            f"DSH identity must survive the sync even when "
            f"adapter_name is NULL; got selected_executor="
            f"{row['selected_executor']!r} (the legacy fallback would "
            f"have been 'hermes')"
        )

    def test_sync_preserves_dsh_when_adapter_name_set(
        self, monkeypatch, tmp_path
    ):
        """Sanity: when both ``adapter_name`` and ``runtime_type``
        are set, the sync must use ``adapter_name`` (the
        dispatch-stamped value, which is the authoritative
        identity)."""
        from dispatcher.db import get_conn
        from dispatcher.ids import next_task_id
        from dispatcher.manager import TaskManager
        from dispatcher.executor_runs import get_run

        setup_temp_db(monkeypatch, tmp_path)
        get_conn()
        tm = TaskManager()

        task_id = next_task_id()
        conn = get_conn()
        conn.execute(
            "INSERT INTO tasks (task_id, title, type, priority, owner, "
            "status, progress_pct, created_at, input_text, "
            "hermes_run_id, runtime_type, adapter_name) "
            "VALUES (?, 'dsh-ok', 'research', 50, 'm2', 'running', "
            "10, ?, 'x', ?, ?, ?)",
            (
                task_id,
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                f"dsh-headless-pending-{task_id}",
                "dsh_headless",
                "dsh-headless",
            ),
        )
        conn.commit()

        tm._sync_executor_runs_status(
            task_id, status="completed", exit_code=0,
        )

        row = get_run(conn, f"dsh-headless-pending-{task_id}")
        assert row["selected_executor"] == "dsh-headless"

    def test_sync_preserves_hermes_when_no_dsh_signal(
        self, monkeypatch, tmp_path
    ):
        """Sanity: a Hermes task with no DSH signal anywhere must
        keep its Hermes identity. The fix is scoped to DSH only —
        we must not falsely attribute a Hermes task to DSH just
        because ``adapter_name`` is NULL."""
        from dispatcher.db import get_conn
        from dispatcher.ids import next_task_id
        from dispatcher.manager import TaskManager
        from dispatcher.executor_runs import get_run

        setup_temp_db(monkeypatch, tmp_path)
        get_conn()
        tm = TaskManager()

        task_id = next_task_id()
        conn = get_conn()
        conn.execute(
            "INSERT INTO tasks (task_id, title, type, priority, owner, "
            "status, progress_pct, created_at, input_text, "
            "hermes_run_id, runtime_type, adapter_name) "
            "VALUES (?, 'hermes-orphan', 'research', 50, 'm2', 'running', "
            "10, ?, 'x', ?, 'hermes', NULL)",
            (
                task_id,
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                f"hermes-orphan-{task_id}",
            ),
        )
        conn.commit()

        tm._sync_executor_runs_status(
            task_id, status="completed", exit_code=0,
        )

        row = get_run(conn, f"hermes-orphan-{task_id}")
        assert row["selected_executor"] == "hermes", (
            f"a Hermes task with no DSH signal must keep its Hermes "
            f"identity; got {row['selected_executor']!r}"
        )


# ---------------------------------------------------------------------------
# (e) No duplicate terminal transition for a DSH run already owned
#     by executor_runs.
# ---------------------------------------------------------------------------

class TestNoDuplicateTerminalForDshRun:
    """The reaper skip must NOT cause the manager to fire a
    duplicate terminal transition for a DSH run already owned by
    ``executor_runs``. The skip is bounded to executor-owned
    rows; the executor path remains the sole authority for the
    terminal status."""

    def test_dsh_owned_queued_task_is_skipped_by_reaper(
        self, monkeypatch, tmp_path
    ):
        """A queued task with a DSH-owned ``executor_runs`` row
        must NOT be reaped (the executor is the sole authority
        for the terminal transition). The skipped entry must
        surface a recognisable reason so operators can audit."""
        from dispatcher.db import get_conn
        from dispatcher.ids import next_task_id
        from dispatcher.manager import TaskManager
        from dispatcher.executor_runs import upsert_run
        from dispatcher.reaper import ReaperConfig, reap_once

        setup_temp_db(monkeypatch, tmp_path)
        get_conn()
        tm = TaskManager()

        old = (datetime.now(timezone.utc) - timedelta(seconds=400)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        task_id = next_task_id()
        conn = get_conn()
        conn.execute(
            "INSERT INTO tasks (task_id, title, type, priority, owner, "
            "status, progress_pct, created_at, input_text) "
            "VALUES (?, 'dsh-healthy', 'research', 50, 'm2', 'queued', "
            "5, ?, 'x')",
            (task_id, old),
        )
        # DSH owns the executor_run — the executor is the sole
        # authority for the terminal transition.
        upsert_run(
            conn,
            run_id=f"dsh-headless-pending-{task_id}",
            requested_executor="dsh-headless",
            selected_executor="dsh-headless",
            task_id=task_id,
            status="running",
            progress=0.0,
            routing={"selected_executor": "dsh-headless"},
        )
        conn.commit()

        cfg = ReaperConfig(enabled=True, stale_queued_sec=300)
        result = reap_once(tm, cfg)
        assert task_id not in result.reaped, (
            f"a DSH-owned queued task must NOT be reaped; "
            f"reaped={result.reaped}"
        )
        # Verify the task row was untouched.
        row = tm.get(task_id)
        assert row is not None
        assert row.status == "queued", (
            f"DSH-owned queued task must remain in 'queued'; "
            f"got status={row.status!r}"
        )
        # Skipped entry should mention the executor-owned reason.
        skipped_reasons = [r for tid, r in result.skipped if tid == task_id]
        assert any("executor-owned" in r for r in skipped_reasons), (
            f"reaper should record an executor-owned skip reason; "
            f"got {skipped_reasons!r}"
        )

    def test_dsh_owned_terminal_run_is_not_double_reaped(
        self, monkeypatch, tmp_path
    ):
        """Sanity: if a DSH-owned ``executor_runs`` row has
        already reached a terminal status, the reaper must
        still apply the queued-age check (the divergence is the
        manager's reconcile path, not the reaper's)."""
        from dispatcher.db import get_conn
        from dispatcher.ids import next_task_id
        from dispatcher.manager import TaskManager
        from dispatcher.executor_runs import upsert_run
        from dispatcher.reaper import ReaperConfig, reap_once

        setup_temp_db(monkeypatch, tmp_path)
        get_conn()
        tm = TaskManager()

        old = (datetime.now(timezone.utc) - timedelta(seconds=400)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        task_id = next_task_id()
        conn = get_conn()
        conn.execute(
            "INSERT INTO tasks (task_id, title, type, priority, owner, "
            "status, progress_pct, created_at, input_text) "
            "VALUES (?, 'dsh-terminal', 'research', 50, 'm2', 'queued', "
            "5, ?, 'x')",
            (task_id, old),
        )
        # DSH executor_run already terminal — divergence case.
        upsert_run(
            conn,
            run_id=f"dsh-headless-{task_id}",
            requested_executor="dsh-headless",
            selected_executor="dsh-headless",
            task_id=task_id,
            status="completed",  # already terminal
            progress=1.0,
            routing={"selected_executor": "dsh-headless"},
        )
        conn.commit()

        cfg = ReaperConfig(enabled=True, stale_queued_sec=300)
        result = reap_once(tm, cfg)
        # The skip is bounded to NON-terminal executor_runs rows.
        # A terminal row paired with a queued task is a divergence
        # the manager's reconcile path owns — the reaper should
        # still reap so the divergence surfaces in observability.
        assert task_id in result.reaped, (
            f"a queued task with a TERMINAL DSH executor_runs row "
            f"is a divergence the reaper must still surface; "
            f"reaped={result.reaped}"
        )


# ---------------------------------------------------------------------------
# Centralised-helper surface: identity predicate coverage
# ---------------------------------------------------------------------------

class TestExecutorIdentityHelper:
    """The ``dispatcher.executor_identity`` module owns the bounded
    vocabulary of executor placeholder prefixes and identity aliases.
    These tests pin the public surface so a future edit cannot
    silently narrow the recognition set."""

    def test_is_dsh_executor_aliases(self):
        from dispatcher.executor_identity import is_dsh_executor
        # The aliases mirror config/executor.json::executor_aliases
        for alias in (
            "dsh-headless",
            "dsh_headless",
            "dsh",
            "deepseek-harness",
            "deepseek_harness",
        ):
            assert is_dsh_executor(alias), (
                f"is_dsh_executor must recognise canonical alias {alias!r}"
            )

    def test_is_dsh_executor_rejects_hermes_claude(self):
        from dispatcher.executor_identity import is_dsh_executor
        for non_dsh in (None, "", "hermes", "claude-code-cli", "claude_cli"):
            assert not is_dsh_executor(non_dsh), (
                f"is_dsh_executor must NOT recognise {non_dsh!r}"
            )

    def test_is_non_hermes_executor_includes_dsh_and_claude(self):
        from dispatcher.executor_identity import is_non_hermes_executor
        for non_hermes in (
            "dsh-headless", "dsh", "deepseek-harness",
            "claude-code-cli", "claude_cli", "claude_code",
        ):
            assert is_non_hermes_executor(non_hermes), (
                f"is_non_hermes_executor must recognise {non_hermes!r}"
            )
        for hermes_like in (None, "", "hermes"):
            assert not is_non_hermes_executor(hermes_like), (
                f"is_non_hermes_executor must NOT recognise {hermes_like!r}"
            )

    def test_helper_known_prefixes(self):
        """The bounded set of placeholder prefixes must be visible
        via the module so other tests / docs can introspect it
        without re-implementing the recognition logic."""
        from dispatcher.executor_identity import _EXECUTOR_PLACEHOLDER_PREFIXES

        assert "claude-cli-pending-" in _EXECUTOR_PLACEHOLDER_PREFIXES
        assert "dsh-headless-pending-" in _EXECUTOR_PLACEHOLDER_PREFIXES
