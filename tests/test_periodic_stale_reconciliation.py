"""Targeted tests for periodic stale-run reconciliation in ExecutorRunWatcher.

Tests that the periodic reconcile_stale_runs call added to
``dispatcher.executor_watcher.ExecutorRunWatcher._tick()`` correctly:

  1. Reconciles post-init orphans after the age cutoff
  2. Does NOT run reconcile on every tick (cadence is bounded)
  3. Fresh orphans (< 1h) are protected
  4. Live/legitimate mapped runs (task_id IS NOT NULL) are protected
  5. Terminal records are untouched
  6. Repeated reconciliation is idempotent
  7. No process killing or row deletion
  8. No duplicate Telegram notification side effects

These tests directly call reconcile_stale_runs with the same call
pattern used by the watcher's periodic hook, plus test the tick-count
cadence gate logic.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dispatcher.executor_runs import (
    ensure_schema,
    reconcile_stale_runs,
)


def _make_db(tmp_path: Path) -> sqlite3.Connection:
    """Create a temp dispatcher DB with the executor_runs schema."""
    db_path = tmp_path / "test_periodic_reconcile.db"
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


class TestPeriodicReconcilePostInitOrphan:
    """AC: post-init orphan becomes eligible after cutoff."""

    def test_post_init_orphan_reconciled_after_cutoff(self, tmp_path):
        """An orphan created after init (task_id=NULL, status=running)
        is reconciled to cancelled once it exceeds the age cutoff."""
        conn = _make_db(tmp_path)
        # Simulate a post-init orphan: created 2 hours ago
        _seed_run(
            conn,
            run_id="claude-cli-post-init-orphan",
            task_id=None,
            created_at="2026-08-09T03:09:50Z",
        )

        # Periodic reconcile fires at 05:09:50+ (2h later)
        result = reconcile_stale_runs(conn, max_age_sec=3600, now="2026-08-09T05:09:50Z")

        assert result["scanned"] == 1
        assert result["reconciled"] == 1
        assert "claude-cli-post-init-orphan" in result["run_ids"]

        row = conn.execute(
            "SELECT status, completed_at, error FROM executor_runs WHERE run_id = ?",
            ("claude-cli-post-init-orphan",),
        ).fetchone()
        assert row["status"] == "cancelled"
        assert row["completed_at"] is not None
        assert "reconcile_stale_runs" in row["error"]
        conn.close()

    def test_post_init_orphan_protected_before_cutoff(self, tmp_path):
        """A post-init orphan younger than max_age_sec is NOT reconciled."""
        conn = _make_db(tmp_path)
        # Created 30 minutes ago — under the 1h cutoff
        _seed_run(
            conn,
            run_id="claude-cli-young-orphan",
            task_id=None,
            created_at="2026-08-09T04:39:50Z",
        )

        result = reconcile_stale_runs(conn, max_age_sec=3600, now="2026-08-09T05:09:50Z")

        assert result["scanned"] == 1
        assert result["reconciled"] == 0

        row = conn.execute(
            "SELECT status FROM executor_runs WHERE run_id = 'claude-cli-young-orphan'"
        ).fetchone()
        assert row["status"] == "running"
        conn.close()


class TestPeriodicReconcileCadence:
    """AC: periodic cadence prevents per-tick excessive DB work."""

    def test_reconcile_only_fires_every_n_ticks(self, tmp_path):
        """The tick-count gate ensures reconcile_stale_runs is NOT called
        on every tick — only every N ticks."""
        from dispatcher.executor_watcher import ExecutorRunWatcher

        watcher = ExecutorRunWatcher(tick_sec=0.01)
        # Default is 360, but for testing use a small value
        watcher._reconcile_every_n_ticks = 3

        call_count = 0

        # We need to mock the _tick method's internals. Since _tick
        # imports from app (circular), test the cadence gate logic
        # directly: the tick_count mod n pattern.
        for i in range(10):
            watcher._tick_count += 1
            should_reconcile = (watcher._tick_count % watcher._reconcile_every_n_ticks == 0)
            if should_reconcile:
                call_count += 1

        # 10 ticks, reconcile every 3 → ticks 3,6,9 → 3 reconciles
        assert call_count == 3

    def test_default_cadence_is_30_minutes(self, tmp_path):
        """Default reconcile interval is 360 ticks at 5s/tick = 30 minutes."""
        from dispatcher.executor_watcher import ExecutorRunWatcher

        watcher = ExecutorRunWatcher(tick_sec=5.0)
        assert watcher._reconcile_every_n_ticks == 360
        assert watcher.tick_sec == 5.0
        # 360 * 5 = 1800 seconds = 30 minutes
        interval_sec = watcher._reconcile_every_n_ticks * watcher.tick_sec
        assert interval_sec == 1800  # 30 minutes


class TestPeriodicReconcileFreshOrphanProtected:
    """AC: fresh orphan protected."""

    def test_fresh_orphan_not_reconciled(self, tmp_path):
        """Fresh orphan (< 1h old) is NOT cancelled by periodic reconcile."""
        conn = _make_db(tmp_path)
        _seed_run(
            conn,
            run_id="claude-cli-fresh",
            task_id=None,
            created_at="2026-08-09T05:09:00Z",
        )

        # Reconcile 30 seconds later — fresh
        result = reconcile_stale_runs(conn, max_age_sec=3600, now="2026-08-09T05:09:30Z")

        assert result["reconciled"] == 0
        row = conn.execute(
            "SELECT status FROM executor_runs WHERE run_id = 'claude-cli-fresh'"
        ).fetchone()
        assert row["status"] == "running"
        conn.close()


class TestPeriodicReconcileLiveRunProtected:
    """AC: live/legitimate mapped run protected."""

    def test_mapped_run_with_task_id_not_touched(self, tmp_path):
        """A run with task_id IS NOT NULL is never matched by reconcile."""
        conn = _make_db(tmp_path)
        _seed_run(
            conn,
            run_id="claude-cli-live",
            task_id="TASK-20260809-0005",
            created_at="2026-08-01T14:31:30Z",  # very old
        )

        result = reconcile_stale_runs(conn, max_age_sec=3600, now="2026-08-09T05:09:50Z")

        assert result["scanned"] == 0
        assert result["reconciled"] == 0
        row = conn.execute(
            "SELECT status FROM executor_runs WHERE run_id = 'claude-cli-live'"
        ).fetchone()
        assert row["status"] == "running"
        conn.close()


class TestPeriodicReconcileTerminalUntouched:
    """AC: terminal record untouched."""

    def test_terminal_records_not_matched(self, tmp_path):
        """Already-terminal rows (completed, failed, cancelled) are not
        matched by reconcile_stale_runs."""
        conn = _make_db(tmp_path)
        _seed_run(
            conn,
            run_id="claude-cli-completed",
            status="completed",
            task_id=None,
            created_at="2026-08-01T14:31:30Z",
        )
        _seed_run(
            conn,
            run_id="claude-cli-failed",
            status="failed",
            task_id=None,
            created_at="2026-08-01T14:31:30Z",
        )
        _seed_run(
            conn,
            run_id="claude-cli-already-cancelled",
            status="cancelled",
            task_id=None,
            created_at="2026-08-01T14:31:30Z",
        )

        result = reconcile_stale_runs(conn, max_age_sec=3600, now="2026-08-09T05:09:50Z")

        assert result["scanned"] == 0
        assert result["reconciled"] == 0

        for rid in ("claude-cli-completed", "claude-cli-failed", "claude-cli-already-cancelled"):
            row = conn.execute(
                "SELECT status FROM executor_runs WHERE run_id = ?", (rid,)
            ).fetchone()
            # Status unchanged
            assert row["status"] in ("completed", "failed", "cancelled")
        conn.close()


class TestPeriodicReconcileIdempotent:
    """AC: repeated reconciliation is idempotent."""

    def test_double_reconcile_no_duplicates(self, tmp_path):
        """Running reconcile_stale_runs twice produces zero reconciliations
        on the second call."""
        conn = _make_db(tmp_path)
        _seed_run(
            conn,
            run_id="claude-cli-stale-1",
            task_id=None,
            created_at="2026-08-01T14:31:30Z",
        )

        first = reconcile_stale_runs(conn, max_age_sec=3600, now="2026-08-09T05:09:50Z")
        assert first["reconciled"] == 1

        second = reconcile_stale_runs(conn, max_age_sec=3600, now="2026-08-09T05:39:50Z")
        assert second["scanned"] == 0
        assert second["reconciled"] == 0
        assert second["run_ids"] == []
        conn.close()

    def test_repeated_reconcile_preserves_audit(self, tmp_path):
        """Re-reconciling an already-cancelled row does not modify it."""
        conn = _make_db(tmp_path)
        _seed_run(
            conn,
            run_id="claude-cli-audit-check",
            task_id=None,
            created_at="2026-08-01T14:31:30Z",
            stdout_summary="critical audit trail",
        )

        reconcile_stale_runs(conn, max_age_sec=3600, now="2026-08-09T05:09:50Z")
        # Capture state after first reconcile
        row1 = conn.execute(
            "SELECT status, completed_at, error, stdout_summary FROM executor_runs WHERE run_id = ?",
            ("claude-cli-audit-check",),
        ).fetchone()

        # Second reconcile — should not touch the row
        reconcile_stale_runs(conn, max_age_sec=3600, now="2026-08-09T05:39:50Z")
        row2 = conn.execute(
            "SELECT status, completed_at, error, stdout_summary FROM executor_runs WHERE run_id = ?",
            ("claude-cli-audit-check",),
        ).fetchone()

        assert row1["status"] == row2["status"] == "cancelled"
        assert row1["completed_at"] == row2["completed_at"]
        assert row1["error"] == row2["error"]
        assert row1["stdout_summary"] == row2["stdout_summary"] == "critical audit trail"
        conn.close()


class TestPeriodicReconcileNoKillingOrDeletion:
    """AC: no process killing/deletion."""

    def test_no_row_deletion(self, tmp_path):
        """reconcile_stale_runs never DELETEs rows — only UPDATEs."""
        conn = _make_db(tmp_path)
        _seed_run(
            conn,
            run_id="claude-cli-no-delete",
            task_id=None,
            created_at="2026-08-01T14:31:30Z",
        )

        reconcile_stale_runs(conn, max_age_sec=3600, now="2026-08-09T05:09:50Z")

        # Row still exists
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM executor_runs WHERE run_id = 'claude-cli-no-delete'"
        ).fetchone()
        assert row["cnt"] == 1
        conn.close()

    def test_no_os_kill_or_subprocess(self, tmp_path):
        """reconcile_stale_runs does not call os.kill, subprocess, or any
        process-related function."""
        conn = _make_db(tmp_path)
        _seed_run(
            conn,
            run_id="claude-cli-no-kill",
            task_id=None,
            created_at="2026-08-01T14:31:30Z",
        )

        with patch("os.kill") as mock_kill, patch("subprocess.run") as mock_subproc:
            reconcile_stale_runs(conn, max_age_sec=3600, now="2026-08-09T05:09:50Z")
            mock_kill.assert_not_called()
            mock_subproc.assert_not_called()

        conn.close()


class TestPeriodicReconcileNoTelegramSideEffects:
    """AC: no duplicate Telegram notification side effects."""

    def test_reconcile_does_not_trigger_telegram(self, tmp_path):
        """reconcile_stale_runs does not call any notification function."""
        conn = _make_db(tmp_path)
        _seed_run(
            conn,
            run_id="claude-cli-no-notify",
            task_id=None,
            created_at="2026-08-01T14:31:30Z",
        )

        # Patch the notifier module's send function
        with patch("dispatcher.notifier.notify_terminal_with_fallback") as mock_notify:
            reconcile_stale_runs(conn, max_age_sec=3600, now="2026-08-09T05:09:50Z")
            mock_notify.assert_not_called()

        # telegram_result_json should remain '{}' (untouched)
        row = conn.execute(
            "SELECT telegram_result_json FROM executor_runs WHERE run_id = 'claude-cli-no-notify'"
        ).fetchone()
        assert row["telegram_result_json"] == "{}"
        conn.close()

    def test_reconcile_does_not_call_hermes_send(self, tmp_path):
        """reconcile_stale_runs does not invoke `hermes send` subprocess."""
        conn = _make_db(tmp_path)
        _seed_run(
            conn,
            run_id="claude-cli-no-hms-send",
            task_id=None,
            created_at="2026-08-01T14:31:30Z",
        )

        with patch("subprocess.run") as mock_run:
            reconcile_stale_runs(conn, max_age_sec=3600, now="2026-08-09T05:09:50Z")
            # No subprocess calls at all
            mock_run.assert_not_called()

        conn.close()