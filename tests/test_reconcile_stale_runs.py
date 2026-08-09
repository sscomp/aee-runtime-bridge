"""Targeted tests for ``reconcile_stale_runs`` (production-readiness minimal finalization).

Tests the stale-run reconciliation function added to
``dispatcher/executor_runs.py``. This function transitions orphaned
``executor_runs`` rows (``status='running'``, ``task_id IS NULL``, age > 1h)
to ``cancelled``, preserving audit history (no DELETE).

Covers:
  * Basic reconciliation of stale orphaned rows
  * Idempotency (second run finds zero candidates)
  * Age filter (fresh orphans are NOT reconciled)
  * Rows with ``task_id`` are NOT touched (live tasks protected)
  * Rows already in terminal status are NOT touched
  * Audit history preserved (created_at, stdout_summary, etc.)
  * Summary return value correctness
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dispatcher.executor_runs import (
    ensure_schema,
    upsert_run,
    reconcile_stale_runs,
    init_executor_runs,
)


def _make_db(tmp_path: Path) -> sqlite3.Connection:
    """Create a temp dispatcher DB with the executor_runs schema."""
    db_path = tmp_path / "test_dispatcher.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _seed_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    status: str = "running",
    task_id: str | None = None,
    created_at: str = "2026-08-01T14:31:30Z",
    selected_executor: str = "claude-code-cli",
    stdout_summary: str = "some stdout",
) -> None:
    """Seed an executor_runs row with explicit created_at."""
    conn.execute(
        """
        INSERT INTO executor_runs (
          run_id, requested_executor, selected_executor, task_id, status,
          progress, exit_code, timeout_state, cancel_state,
          stdout_summary, stderr_summary,
          artifact_paths_json, artifact_verification_json,
          git_evidence_json, telegram_result_json,
          runtime_identity_json, routing_json, error,
          created_at, updated_at, completed_at,
          last_heartbeat_at, current_step, phase
        ) VALUES (
          ?, NULL, ?, ?, ?, 0.0, NULL, NULL, NULL,
        ?, '', '[]', '[]', NULL, '{}', NULL, '{}', NULL,
        ?, ?, NULL, NULL, 'starting', 'running'
        )
        """,
        (run_id, selected_executor, task_id, status, stdout_summary, created_at, created_at),
    )
    conn.commit()


class TestReconcileStaleRuns:
    """Tests for ``reconcile_stale_runs``."""

    def test_reconciles_orphaned_running_rows(self, tmp_path):
        """Stale ``running`` rows with ``task_id IS NULL`` are cancelled."""
        conn = _make_db(tmp_path)
        _seed_run(conn, run_id="claude-cli-stale-1", task_id=None, created_at="2026-08-01T14:31:30Z")
        _seed_run(conn, run_id="claude-cli-stale-2", task_id=None, created_at="2026-08-01T14:31:32Z")

        result = reconcile_stale_runs(conn, max_age_sec=3600, now="2026-08-09T05:00:00Z")

        assert result["scanned"] == 2
        assert result["reconciled"] == 2
        assert set(result["run_ids"]) == {"claude-cli-stale-1", "claude-cli-stale-2"}

        # Verify status changed to cancelled
        for rid in result["run_ids"]:
            row = conn.execute(
                "SELECT status, completed_at, error FROM executor_runs WHERE run_id = ?",
                (rid,),
            ).fetchone()
            assert row["status"] == "cancelled"
            assert row["completed_at"] is not None
            assert "reconcile_stale_runs" in row["error"]

        conn.close()

    def test_idempotent_second_run(self, tmp_path):
        """Second reconciliation finds zero candidates."""
        conn = _make_db(tmp_path)
        _seed_run(conn, run_id="claude-cli-stale-1", task_id=None, created_at="2026-08-01T14:31:30Z")

        first = reconcile_stale_runs(conn, max_age_sec=3600, now="2026-08-09T05:00:00Z")
        assert first["reconciled"] == 1

        second = reconcile_stale_runs(conn, max_age_sec=3600, now="2026-08-09T06:00:00Z")
        assert second["scanned"] == 0
        assert second["reconciled"] == 0
        assert second["run_ids"] == []

        conn.close()

    def test_age_filter_excludes_fresh_orphans(self, tmp_path):
        """Fresh orphaned rows (< max_age_sec) are NOT reconciled."""
        conn = _make_db(tmp_path)
        _seed_run(conn, run_id="claude-cli-fresh", task_id=None, created_at="2026-08-09T04:59:00Z")

        result = reconcile_stale_runs(conn, max_age_sec=3600, now="2026-08-09T05:00:00Z")

        assert result["scanned"] == 1
        assert result["reconciled"] == 0

        # Row is still running
        row = conn.execute("SELECT status FROM executor_runs WHERE run_id = 'claude-cli-fresh'").fetchone()
        assert row["status"] == "running"

        conn.close()

    def test_preserves_rows_with_task_id(self, tmp_path):
        """Rows with a task_id are NOT orphaned and must NOT be cancelled."""
        conn = _make_db(tmp_path)
        _seed_run(conn, run_id="claude-cli-live", task_id="TASK-20260809-0005", created_at="2026-08-01T14:31:30Z")

        result = reconcile_stale_runs(conn, max_age_sec=3600, now="2026-08-09T05:00:00Z")

        assert result["scanned"] == 0  # not matched (task_id IS NOT NULL)
        assert result["reconciled"] == 0

        row = conn.execute("SELECT status FROM executor_runs WHERE run_id = 'claude-cli-live'").fetchone()
        assert row["status"] == "running"  # unchanged

        conn.close()

    def test_preserves_terminal_rows(self, tmp_path):
        """Rows already in terminal status are NOT matched."""
        conn = _make_db(tmp_path)
        _seed_run(conn, run_id="claude-cli-done", status="completed", task_id=None, created_at="2026-08-01T14:31:30Z")
        _seed_run(conn, run_id="claude-cli-failed", status="failed", task_id=None, created_at="2026-08-01T14:31:30Z")

        result = reconcile_stale_runs(conn, max_age_sec=3600, now="2026-08-09T05:00:00Z")

        assert result["scanned"] == 0
        assert result["reconciled"] == 0

        conn.close()

    def test_audit_history_preserved(self, tmp_path):
        """Reconciliation preserves created_at and stdout_summary."""
        conn = _make_db(tmp_path)
        _seed_run(conn, run_id="claude-cli-audit", task_id=None, created_at="2026-08-01T14:31:30Z", stdout_summary="important audit data")

        reconcile_stale_runs(conn, max_age_sec=3600, now="2026-08-09T05:00:00Z")

        row = conn.execute("SELECT created_at, stdout_summary, status FROM executor_runs WHERE run_id = 'claude-cli-audit'").fetchone()
        assert row["created_at"] == "2026-08-01T14:31:30Z"  # preserved
        assert row["stdout_summary"] == "important audit data"  # preserved
        assert row["status"] == "cancelled"

        conn.close()

    def test_completed_at_equals_created_at(self, tmp_path):
        """``completed_at`` is set to ``created_at`` (not to the reconciliation time)."""
        conn = _make_db(tmp_path)
        _seed_run(conn, run_id="claude-cli-timestamp", task_id=None, created_at="2026-08-01T14:31:30Z")

        reconcile_stale_runs(conn, max_age_sec=3600, now="2026-08-09T05:00:00Z")

        row = conn.execute("SELECT created_at, completed_at FROM executor_runs WHERE run_id = 'claude-cli-timestamp'").fetchone()
        assert row["completed_at"] == row["created_at"]

        conn.close()

    def test_queued_status_also_reconciled(self, tmp_path):
        """Queued orphaned rows are also reconciled (not just running)."""
        conn = _make_db(tmp_path)
        _seed_run(conn, run_id="claude-cli-queued", status="queued", task_id=None, created_at="2026-08-01T14:31:30Z")

        result = reconcile_stale_runs(conn, max_age_sec=3600, now="2026-08-09T05:00:00Z")

        assert result["reconciled"] == 1
        row = conn.execute("SELECT status FROM executor_runs WHERE run_id = 'claude-cli-queued'").fetchone()
        assert row["status"] == "cancelled"

        conn.close()

    def test_no_rows_returns_empty_summary(self, tmp_path):
        """Empty DB returns zero counts."""
        conn = _make_db(tmp_path)
        result = reconcile_stale_runs(conn, max_age_sec=3600, now="2026-08-09T05:00:00Z")
        assert result == {"scanned": 0, "reconciled": 0, "run_ids": []}
        conn.close()

    def test_init_executor_runs_calls_reconcile(self, tmp_path, monkeypatch):
        """``init_executor_runs`` calls ``reconcile_stale_runs`` on first init."""
        import dispatcher.executor_runs as er

        db_path = tmp_path / "test_init.db"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Seed a stale row BEFORE calling init
        ensure_schema(conn)
        _seed_run(conn, run_id="claude-cli-init-test", task_id=None, created_at="2026-08-01T14:31:30Z")

        # Reset _initialized so init runs fresh
        monkeypatch.setattr(er, "_initialized", False)
        er.init_executor_runs(conn)

        # The stale row should now be cancelled
        row = conn.execute("SELECT status FROM executor_runs WHERE run_id = 'claude-cli-init-test'").fetchone()
        assert row["status"] == "cancelled"

        conn.close()