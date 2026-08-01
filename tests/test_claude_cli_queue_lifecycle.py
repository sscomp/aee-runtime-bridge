"""Targeted regression tests for the Claude Code CLI queue lifecycle fix.

Verifies that ``POST /runs/executor`` with
``executor == "claude-code-cli"`` transitions the dispatcher ``tasks``
row through ``queued → running → completed`` (success path) and
``queued → running → failed`` (failure path) in sync with the CLI
execution, instead of leaving the row orphaned in ``queued`` state for
the reaper to time out at ``stale_queued_sec=300``.

Root cause + fix reference:
``reports/claude_cli_queue_diagnosis.md`` §7 Fix 1.

The tests use the shared hermetic fixtures in ``_executor_test_helpers``
(temp dispatcher DB, fake claude binary, no real Telegram credentials)
so they run in-process under ``fastapi.testclient.TestClient`` without
touching the production ``data/dispatcher.db``.
"""
from __future__ import annotations

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
    set_fake_binary,
    write_fake_claude,
)


def _db_path(monkeypatch) -> Path:
    from dispatcher import db as ddb
    return ddb.DB_PATH


def _task_row(db_path: Path, task_id: str) -> dict | None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT task_id, status, started_at, finished_at, "
            "duration_sec, error_message, hermes_run_id "
            "FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _task_events(db_path: Path, task_id: str) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT kind, payload_json FROM task_events "
            "WHERE task_id = ? ORDER BY id ASC",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Success path: queued → running → completed
# ---------------------------------------------------------------------------

class TestQueueRunningCompleted:
    """claude-code-cli success transitions the dispatcher task to completed."""

    def test_success_path_task_reaches_completed(self, monkeypatch, tmp_path):
        artifact = str(tmp_path / "lifecycle_ok.md")
        binary = write_fake_claude(
            tmp_path,
            artifact=artifact,
            artifact_content="lifecycle ok",
            stdout="ok",
            name="fake-claude-lifecycle-ok",
        )
        set_fake_binary(monkeypatch, binary)
        client, app_module, key = make_client(monkeypatch, tmp_path)

        resp = post_executor(client, key, {
            "executor": "claude-code-cli",
            "prompt": "write the artifact",
            "expected_artifacts": [artifact],
            "timeout_sec": 30,
            "repo_path": str(tmp_path),
        })
        assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["status"] == "completed"
        assert data["exit_code"] == 0
        task_id = data.get("task_id")
        assert task_id, "task_id must be non-NULL (Fix B prerequisite)"

        row = _task_row(_db_path(monkeypatch), task_id)
        assert row is not None, "tasks row missing after POST /runs/executor"
        assert row["status"] == "completed", (
            f"expected status='completed', got {row['status']!r} "
            f"(Fix 1: queued → running → completed lifecycle must fire)"
        )
        assert row["started_at"] is not None, (
            "started_at must be set by manager.start() (queued → running)"
        )
        assert row["finished_at"] is not None, (
            "finished_at must be set by manager.complete() (running → completed)"
        )
        assert row["duration_sec"] is not None and row["duration_sec"] >= 0, (
            f"duration_sec must be non-negative, got {row['duration_sec']!r}"
        )
        assert row["hermes_run_id"] is not None, (
            "hermes_run_id must be stamped by manager.start() "
            "(even a placeholder is acceptable — proves start() ran)"
        )

    def test_success_path_emits_started_and_completed_events(
        self, monkeypatch, tmp_path
    ):
        artifact = str(tmp_path / "lifecycle_events.md")
        binary = write_fake_claude(
            tmp_path,
            artifact=artifact,
            artifact_content="events ok",
            stdout="ok",
            name="fake-claude-lifecycle-events",
        )
        set_fake_binary(monkeypatch, binary)
        client, app_module, key = make_client(monkeypatch, tmp_path)

        resp = post_executor(client, key, {
            "executor": "claude-code-cli",
            "prompt": "write the artifact",
            "expected_artifacts": [artifact],
            "timeout_sec": 30,
            "repo_path": str(tmp_path),
        })
        assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
        task_id = resp.json()["task_id"]

        events = _task_events(_db_path(monkeypatch), task_id)
        kinds = [e["kind"] for e in events]
        assert "started" in kinds, (
            f"task_events missing 'started' kind; got {kinds} "
            f"(Fix 1: manager.start() must emit STARTED event)"
        )
        assert "completed" in kinds, (
            f"task_events missing 'completed' kind; got {kinds} "
            f"(Fix 1: manager.complete() must emit COMPLETED event)"
        )

    def test_success_path_does_not_leave_queued(self, monkeypatch, tmp_path):
        """Regression guard: the task must NOT remain in queued state."""
        binary = write_fake_claude(
            tmp_path, stdout="ok", name="fake-claude-no-queued",
        )
        set_fake_binary(monkeypatch, binary)
        client, app_module, key = make_client(monkeypatch, tmp_path)

        resp = post_executor(client, key, {
            "executor": "claude-code-cli",
            "prompt": "no artifact",
            "timeout_sec": 30,
            "repo_path": str(tmp_path),
        })
        assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
        task_id = resp.json()["task_id"]
        row = _task_row(_db_path(monkeypatch), task_id)
        assert row is not None
        assert row["status"] != "queued", (
            f"task stuck in 'queued' after POST returns — this is the "
            f"original bug from claude_cli_queue_diagnosis.md. "
            f"status={row['status']!r}"
        )
        assert row["status"] == "completed", (
            f"expected 'completed', got {row['status']!r}"
        )


# ---------------------------------------------------------------------------
# Failure path: queued → running → failed
# ---------------------------------------------------------------------------

class TestQueueRunningFailed:
    """claude-code-cli non-zero exit transitions the dispatcher task to failed."""

    def test_failure_path_task_reaches_failed(self, monkeypatch, tmp_path):
        binary = write_fake_claude(
            tmp_path,
            artifact=None,
            exit_code=3,
            stdout="boom",
            name="fake-claude-lifecycle-fail",
        )
        set_fake_binary(monkeypatch, binary)
        client, app_module, key = make_client(monkeypatch, tmp_path)

        resp = post_executor(client, key, {
            "executor": "claude-code-cli",
            "prompt": "do something that fails",
            "timeout_sec": 30,
            "repo_path": str(tmp_path),
        })
        assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["status"] == "failed"
        assert data["exit_code"] == 3
        task_id = data.get("task_id")
        assert task_id, "task_id must be non-NULL even on failure"

        row = _task_row(_db_path(monkeypatch), task_id)
        assert row is not None, "tasks row missing after failed executor run"
        assert row["status"] == "failed", (
            f"expected status='failed', got {row['status']!r} "
            f"(Fix 1: queued → running → failed lifecycle must fire on "
            f"non-zero exit)"
        )
        assert row["started_at"] is not None, (
            "started_at must be set by manager.start() even on failure path"
        )
        assert row["finished_at"] is not None, (
            "finished_at must be set by manager.fail() (running → failed)"
        )
        assert row["error_message"] is not None, (
            "error_message must be populated by manager.fail()"
        )
        assert "claude-code-cli" in row["error_message"], (
            f"error_message should reference claude-code-cli; "
            f"got {row['error_message']!r}"
        )

    def test_failure_path_emits_started_and_failed_events(
        self, monkeypatch, tmp_path
    ):
        binary = write_fake_claude(
            tmp_path,
            artifact=None,
            exit_code=2,
            stdout="fail",
            name="fake-claude-lifecycle-fail-events",
        )
        set_fake_binary(monkeypatch, binary)
        client, app_module, key = make_client(monkeypatch, tmp_path)

        resp = post_executor(client, key, {
            "executor": "claude-code-cli",
            "prompt": "fail with events",
            "timeout_sec": 30,
            "repo_path": str(tmp_path),
        })
        assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
        task_id = resp.json()["task_id"]

        events = _task_events(_db_path(monkeypatch), task_id)
        kinds = [e["kind"] for e in events]
        assert "started" in kinds, (
            f"task_events missing 'started' kind on failure path; "
            f"got {kinds}"
        )
        assert "failed" in kinds, (
            f"task_events missing 'failed' kind; got {kinds} "
            f"(Fix 1: manager.fail() must emit FAILED event)"
        )


# ---------------------------------------------------------------------------
# Lifecycle ordering: running must precede terminal
# ---------------------------------------------------------------------------

class TestLifecycleOrdering:
    """The queued → running transition must happen BEFORE the CLI runs,
    and the terminal transition must happen AFTER the CLI result is known.
    This is enforced structurally by the code path (start before run,
    complete/fail after run) — we verify the observable consequence: the
    task reaches running at some point and then reaches a terminal state,
    never skipping running."""

    def test_task_visits_running_before_terminal(self, monkeypatch, tmp_path):
        binary = write_fake_claude(
            tmp_path, stdout="ok", name="fake-claude-ordering",
        )
        set_fake_binary(monkeypatch, binary)
        client, app_module, key = make_client(monkeypatch, tmp_path)

        resp = post_executor(client, key, {
            "executor": "claude-code-cli",
            "prompt": "ordering test",
            "timeout_sec": 30,
            "repo_path": str(tmp_path),
        })
        assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
        task_id = resp.json()["task_id"]

        events = _task_events(_db_path(monkeypatch), task_id)
        kinds = [e["kind"] for e in events]
        # The STARTED event must appear before the COMPLETED event.
        try:
            started_idx = kinds.index("started")
        except ValueError:
            started_idx = -1
        try:
            completed_idx = kinds.index("completed")
        except ValueError:
            completed_idx = -1
        assert started_idx >= 0 and completed_idx >= 0, (
            f"expected both 'started' and 'completed' events; got {kinds}"
        )
        assert started_idx < completed_idx, (
            f"'started' event must precede 'completed' event; "
            f"got order started={started_idx} completed={completed_idx} "
            f"in {kinds}"
        )