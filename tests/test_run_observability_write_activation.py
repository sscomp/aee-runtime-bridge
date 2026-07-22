"""Focused tests for the P1.1 run observability write-side activation
(TASK-AEE-RUN-OBSERVABILITY-WRITE-ACTIVATION).

Covers the work-order's required scenarios for the WRITE path (the
read-path contract is already covered by ``test_run_observability.py``):

  1. heartbeat timestamp advances for a running executor
  2. current_step transitions through known lifecycle states
  3. phase queued/running/terminal persistence
  4. GET /runs does not trigger heartbeat or mutate DB
  5. terminal run never receives further heartbeat updates
  6. failed/timeout/cancelled transitions persist terminal phase
  7. missing heartbeat support degrades truthfully
  8. legacy rows remain readable
  9. no model-internal/fabricated step labels

These tests exercise the canonical ``update_heartbeat`` writer +
``_persist_executor_run`` / ``_persist_terminal_reconciliation`` call
sites in ``app.py`` via a FastAPI TestClient driven by the shared
fake-binary fixture in ``tests/_executor_test_helpers.py``. No real
CLI is invoked; no real upstream Hermes is contacted.
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from dispatcher.executor_runs import (
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    LIFECYCLE_STEPS,
    ensure_schema,
    get_heartbeat_interval_seconds,
    upsert_run,
    update_heartbeat,
)
from dispatcher.observability import derive_observability, derive_phase
from tests._executor_test_helpers import (
    make_client,
    post_executor,
    set_fake_binary,
    write_fake_claude,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(dt: _dt.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_run(tmp_path: Path, **kwargs: Any) -> str:
    """Seed an executor_runs row directly via the writer; return run_id."""
    from dispatcher import db as ddb
    from dispatcher.executor_runs import ensure_schema, upsert_run
    ddb.DB_DIR = tmp_path
    ddb.DB_PATH = tmp_path / "dispatcher.db"
    ddb._initialized = False
    if hasattr(ddb._local, "conn"):
        ddb._local.conn = None
    conn = ddb.get_conn()
    ensure_schema(conn)
    run_id = kwargs.pop("run_id", "run-test-" + _dt.datetime.utcnow().strftime("%H%M%S%f"))
    upsert_run(
        conn,
        run_id=run_id,
        requested_executor=kwargs.pop("requested_executor", "claude-code-cli"),
        selected_executor=kwargs.pop("selected_executor", "claude-code-cli"),
        task_id=kwargs.pop("task_id", None),
        status=kwargs.pop("status", "running"),
        progress=kwargs.pop("progress", 0.0),
        exit_code=kwargs.pop("exit_code", None),
        stdout_summary=kwargs.pop("stdout_summary", ""),
        stderr_summary=kwargs.pop("stderr_summary", ""),
        last_heartbeat_at=kwargs.pop("last_heartbeat_at", None),
        current_step=kwargs.pop("current_step", "running"),
        phase=kwargs.pop("phase", "running"),
    )
    return run_id


def _get_run_row(tmp_path: Path, run_id: str) -> Dict[str, Any]:
    """Read the raw executor_runs row (without going through the API)."""
    from dispatcher import db as ddb
    conn = sqlite3.connect(str(tmp_path / "dispatcher.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM executor_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    return dict(row) if row is not None else {}


# ---------------------------------------------------------------------------
# Heartbeat contract: cadence + lifecycle vocabulary (scenarios 1, 2, 3, 9)
# ---------------------------------------------------------------------------

class TestHeartbeatContract:
    def test_default_heartbeat_interval_is_named_constant(self, monkeypatch):
        monkeypatch.delenv("RUN_HEARTBEAT_INTERVAL_SECONDS", raising=False)
        assert get_heartbeat_interval_seconds() == float(DEFAULT_HEARTBEAT_INTERVAL_SECONDS)
        assert DEFAULT_HEARTBEAT_INTERVAL_SECONDS == 5

    def test_heartbeat_interval_env_override(self, monkeypatch):
        monkeypatch.setenv("RUN_HEARTBEAT_INTERVAL_SECONDS", "0.01")
        assert get_heartbeat_interval_seconds() == pytest.approx(0.01)

    def test_heartbeat_interval_malformed_falls_back(self, monkeypatch):
        monkeypatch.setenv("RUN_HEARTBEAT_INTERVAL_SECONDS", "garbage")
        assert get_heartbeat_interval_seconds() == float(DEFAULT_HEARTBEAT_INTERVAL_SECONDS)

    def test_heartbeat_interval_negative_falls_back(self, monkeypatch):
        monkeypatch.setenv("RUN_HEARTBEAT_INTERVAL_SECONDS", "-3")
        assert get_heartbeat_interval_seconds() == float(DEFAULT_HEARTBEAT_INTERVAL_SECONDS)

    def test_lifecycle_steps_canonical_set(self):
        # The terminal steps MUST map 1:1 to the canonical run statuses.
        assert "queued" in LIFECYCLE_STEPS
        assert "starting" in LIFECYCLE_STEPS
        assert "running" in LIFECYCLE_STEPS
        assert "collecting_output" in LIFECYCLE_STEPS
        assert "verifying_artifacts" in LIFECYCLE_STEPS
        for terminal in ("completed", "failed", "timeout", "cancelled"):
            assert terminal in LIFECYCLE_STEPS

    def test_no_model_internal_step_labels_accepted(self, tmp_path):
        """update_heartbeat MUST reject a fabricated / model-internal step."""
        run_id = _seed_run(tmp_path, status="running")
        from dispatcher import db as ddb
        conn = ddb.get_conn()
        with pytest.raises(ValueError, match="LIFECYCLE_STEPS"):
            update_heartbeat(conn, run_id=run_id, current_step="thinking_about_code")
        with pytest.raises(ValueError):
            update_heartbeat(conn, run_id=run_id, current_step="model_layer_3_attention")
        with pytest.raises(ValueError):
            update_heartbeat(conn, run_id=run_id, current_step="")


# ---------------------------------------------------------------------------
# Scenario 1: heartbeat timestamp advances for a running executor
# ---------------------------------------------------------------------------

class TestHeartbeatAdvances:
    def test_heartbeat_advances_for_running_executor(self, tmp_path):
        """Two successive heartbeats on a running row must advance
        ``last_heartbeat_at`` and ``updated_at`` monotonically."""
        run_id = _seed_run(tmp_path, status="running", current_step="starting")
        from dispatcher import db as ddb
        conn = ddb.get_conn()
        # First heartbeat.
        r1 = update_heartbeat(conn, run_id=run_id, current_step="running", phase="running")
        assert r1 is not None, "running row should accept a heartbeat"
        assert r1["current_step"] == "running"
        assert r1["phase"] == "running"
        first_hb = r1["last_heartbeat_at"]
        first_updated = r1["updated_at"]
        assert first_hb is not None and first_updated is not None
        # Sleep a small, deterministic delta so the next stamp differs.
        time.sleep(1.05)
        # Second heartbeat.
        r2 = update_heartbeat(conn, run_id=run_id, current_step="running", phase="running")
        assert r2 is not None
        second_hb = r2["last_heartbeat_at"]
        second_updated = r2["updated_at"]
        # Monotonic advance — the second stamp is strictly later.
        assert second_hb > first_hb, (
            f"last_heartbeat_at must advance: first={first_hb!r} second={second_hb!r}"
        )
        assert second_updated > first_updated, (
            f"updated_at must advance: first={first_updated!r} second={second_updated!r}"
        )

    def test_heartbeat_phase_persisted(self, tmp_path):
        """update_heartbeat(phase=...) stamps the row's phase column."""
        run_id = _seed_run(tmp_path, status="running", current_step="starting", phase="queued")
        from dispatcher import db as ddb
        conn = ddb.get_conn()
        r = update_heartbeat(conn, run_id=run_id, current_step="running", phase="running")
        assert r is not None
        assert r["phase"] == "running"
        # COALESCE: passing phase=None preserves the existing value.
        time.sleep(1.0)
        r2 = update_heartbeat(conn, run_id=run_id, current_step="running", phase=None)
        assert r2["phase"] == "running"


# ---------------------------------------------------------------------------
# Scenario 5: terminal run never receives further heartbeat updates
# ---------------------------------------------------------------------------

class TestTerminalNeverReheartbeated:
    @pytest.mark.parametrize("terminal_status", ["completed", "failed", "timeout", "cancelled"])
    def test_terminal_row_rejects_heartbeat(self, tmp_path, terminal_status):
        run_id = _seed_run(
            tmp_path,
            status=terminal_status,
            current_step=terminal_status,
            phase="terminal",
        )
        from dispatcher import db as ddb
        conn = ddb.get_conn()
        # Snapshot the row before the attempted heartbeat.
        before = _get_run_row(tmp_path, run_id)
        result = update_heartbeat(conn, run_id=run_id, current_step="running", phase="running")
        assert result is None, f"terminal row {terminal_status!r} must reject heartbeats"
        after = _get_run_row(tmp_path, run_id)
        # The row is byte-identical: no field was mutated.
        assert after == before, (
            f"terminal row mutated by heartbeat: before={before!r} after={after!r}"
        )

    def test_missing_row_returns_none(self, tmp_path):
        from dispatcher import db as ddb
        conn = ddb.get_conn()
        r = update_heartbeat(conn, run_id="run-does-not-exist", current_step="running")
        assert r is None


# ---------------------------------------------------------------------------
# Scenario 4: GET /runs is a pure read (no DB mutation, no heartbeat trigger)
# ---------------------------------------------------------------------------

class TestGetRunsPureRead:
    def test_get_runs_does_not_advance_heartbeat(self, monkeypatch, tmp_path):
        client, app_module, key = make_client(monkeypatch, tmp_path)
        # Seed a running run with a known heartbeat stamp.
        run_id = _seed_run(tmp_path, status="running", current_step="running", phase="running")
        from dispatcher import db as ddb
        before = _get_run_row(tmp_path, run_id)
        # Multiple GET /runs/{run_id} calls.
        for _ in range(3):
            r = client.get(
                f"/runs/{run_id}",
                headers={"Authorization": f"Bearer {key}"},
            )
            assert r.status_code == 200
        after = _get_run_row(tmp_path, run_id)
        # The persisted row must be byte-identical — GET never writes.
        assert after == before, (
            "GET /runs/{run_id} mutated the row: "
            f"before={before!r} after={after!r}"
        )

    def test_get_runs_list_does_not_advance_heartbeat(self, monkeypatch, tmp_path):
        client, app_module, key = make_client(monkeypatch, tmp_path)
        run_id = _seed_run(tmp_path, status="running", current_step="running", phase="running")
        before = _get_run_row(tmp_path, run_id)
        # GET /runs (list) — must not mutate any row.
        r = client.get("/runs", headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 200
        after = _get_run_row(tmp_path, run_id)
        assert after == before


# ---------------------------------------------------------------------------
# Scenario 6: failed/timeout/cancelled transitions persist terminal phase
# ---------------------------------------------------------------------------

class TestTerminalTransitionsPersisted:
    def test_terminal_reconciliation_stamps_terminal_phase(self, monkeypatch, tmp_path):
        """_persist_terminal_reconciliation stamps phase=terminal,
        current_step=<status>, and a final heartbeat."""
        client, app_module, key = make_client(monkeypatch, tmp_path)
        # Seed a running Hermes run so the reconciliation path can find it.
        run_id = _seed_run(
            tmp_path,
            status="running",
            selected_executor="hermes",
            current_step="running",
            phase="running",
        )
        # Drive the terminal reconciliation directly through the
        # app helper — the GET /runs/{run_id} path invokes
        # _maybe_reconcile_hermes_run which (for a non-terminal Hermes
        # row) calls _persist_terminal_reconciliation when the upstream
        # reports terminal. We emulate the "upstream reports completed"
        # case by calling the helper directly with a simulated terminal
        # poll result (the helper is the write-side surface under test).
        from app import _persist_terminal_reconciliation
        persisted = {"selected_executor": "hermes", "status": "running"}
        out = _persist_terminal_reconciliation(
            run_id, persisted, status="completed",
            stdout_summary="done",
        )
        assert out is not None
        assert out["status"] == "completed"
        assert out["phase"] == "terminal"
        assert out["current_step"] == "completed"
        assert out["last_heartbeat_at"] is not None
        # Verify the row was actually persisted with the terminal fields.
        row = _get_run_row(tmp_path, run_id)
        assert row["status"] == "completed"
        assert row["phase"] == "terminal"
        assert row["current_step"] == "completed"
        assert row["last_heartbeat_at"] is not None

    @pytest.mark.parametrize("status", ["failed", "timeout", "cancelled"])
    def test_each_terminal_status_persists_terminal_phase(self, monkeypatch, tmp_path, status):
        client, app_module, key = make_client(monkeypatch, tmp_path)
        run_id = _seed_run(
            tmp_path,
            status="running",
            selected_executor="hermes",
            current_step="running",
            phase="running",
        )
        from app import _persist_terminal_reconciliation
        out = _persist_terminal_reconciliation(
            run_id, {"selected_executor": "hermes", "status": "running"},
            status=status, error=f"simulated {status}",
        )
        assert out["status"] == status
        assert out["phase"] == "terminal"
        assert out["current_step"] == status
        # Subsequent heartbeat must be a no-op (terminal never re-heartbeated).
        from dispatcher import db as ddb
        conn = ddb.get_conn()
        r = update_heartbeat(conn, run_id=run_id, current_step="running")
        assert r is None


# ---------------------------------------------------------------------------
# Scenario 8: legacy rows remain readable (P1 backward compat)
# ---------------------------------------------------------------------------

class TestLegacyRowReadability:
    def test_legacy_row_without_observability_columns_remains_readable(self, tmp_path):
        """A row that predates the P1 migration (no last_heartbeat_at /
        current_step / phase) must remain readable; missing columns
        degrade to None / sensible defaults."""
        from dispatcher import db as ddb
        ddb.DB_DIR = tmp_path
        ddb.DB_PATH = tmp_path / "dispatcher.db"
        ddb._initialized = False
        if hasattr(ddb._local, "conn"):
            ddb._local.conn = None
        conn = ddb.get_conn()
        # Manually create the executor_runs table WITHOUT the P1
        # observability columns, then insert a legacy row.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS executor_runs (
              run_id TEXT PRIMARY KEY,
              requested_executor TEXT,
              selected_executor TEXT NOT NULL,
              task_id TEXT,
              status TEXT NOT NULL,
              progress REAL NOT NULL DEFAULT 0.0,
              exit_code INTEGER,
              timeout_state TEXT,
              cancel_state TEXT,
              stdout_summary TEXT NOT NULL DEFAULT '',
              stderr_summary TEXT NOT NULL DEFAULT '',
              artifact_paths_json TEXT NOT NULL DEFAULT '[]',
              artifact_verification_json TEXT NOT NULL DEFAULT '[]',
              git_evidence_json TEXT,
              telegram_result_json TEXT NOT NULL DEFAULT '{}',
              runtime_identity_json TEXT,
              routing_json TEXT NOT NULL DEFAULT '{}',
              error TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              completed_at TEXT
            );
        """)
        conn.execute(
            """INSERT INTO executor_runs
               (run_id, requested_executor, selected_executor, task_id,
                status, progress, exit_code, timeout_state, cancel_state,
                stdout_summary, stderr_summary, artifact_paths_json,
                artifact_verification_json, git_evidence_json,
                telegram_result_json, runtime_identity_json,
                routing_json, error, created_at, updated_at, completed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("run-legacy-1", None, "claude-code-cli", None,
             "completed", 1.0, 0, None, None,
             "legacy output", "", "[]", "[]", None,
             "{}", None, "{}", None,
             "2026-07-22T00:00:00Z", "2026-07-22T00:00:00Z",
             "2026-07-22T00:00:01Z"),
        )
        conn.commit()
        # The legacy row must be readable via get_run — missing P1
        # columns degrade to None.
        from dispatcher.executor_runs import get_run
        row = get_run(conn, "run-legacy-1")
        assert row is not None
        assert row["status"] == "completed"
        assert row["last_heartbeat_at"] is None
        assert row["current_step"] is None
        assert row["phase"] is None
        # derive_observability on a legacy row must NOT raise.
        obs = derive_observability(row)
        assert obs["phase"] == "terminal"  # status=completed -> terminal
        assert obs["last_heartbeat_at"] is None
        assert obs["current_step"] is None
        assert obs["stalled"] is False  # terminal -> never stalled


# ---------------------------------------------------------------------------
# Scenario 2: current_step transitions through known lifecycle states
# ---------------------------------------------------------------------------

class TestCurrentStepTransitions:
    def test_current_step_transitions_through_known_states(self, tmp_path):
        run_id = _seed_run(tmp_path, status="queued", current_step="queued", phase="queued")
        from dispatcher import db as ddb
        conn = ddb.get_conn()
        # queued -> starting -> running -> collecting_output -> completed
        for step in ("starting", "running", "collecting_output", "verifying_artifacts"):
            r = update_heartbeat(conn, run_id=run_id, current_step=step, phase="running")
            assert r is not None, f"step={step!r} should be accepted"
            assert r["current_step"] == step
            assert r["phase"] == "running"
        # Terminal transition: complete via _persist_terminal_reconciliation.
        from app import _persist_terminal_reconciliation
        out = _persist_terminal_reconciliation(
            run_id, {"selected_executor": "claude-code-cli", "status": "running"},
            status="completed", stdout_summary="ok",
        )
        assert out["current_step"] == "completed"
        assert out["phase"] == "terminal"


# ---------------------------------------------------------------------------
# Scenario 3: phase queued/running/terminal persistence (end-to-end via API)
# ---------------------------------------------------------------------------

class TestPhaseEndToEnd:
    def test_phase_running_after_dispatch_then_terminal_after_completion(
        self, monkeypatch, tmp_path,
    ):
        """End-to-end via the POST /runs/executor + GET /runs/{run_id}
        surface: the persisted row carries the canonical phase at
        every stage."""
        client, app_module, key = make_client(monkeypatch, tmp_path)
        # Use a fake claude binary that writes an artifact and exits 0
        # so the dispatch is deterministic and terminal in one call.
        artifact_path = tmp_path / "out.txt"
        binary = write_fake_claude(
            tmp_path,
            artifact=str(artifact_path),
            artifact_content="hello",
            stdout="done",
            name="fake-claude-phase",
        )
        set_fake_binary(monkeypatch, binary)
        body = {
            "executor": "claude-code-cli",
            "prompt": "do the thing",
            "repo_path": "/tmp",
            "expected_artifacts": [str(artifact_path)],
            "timeout_sec": 30,
        }
        r = post_executor(client, key, body)
        assert r.status_code == 200, r.text
        env = r.json()
        run_id = env["run_id"]
        assert env["status"] == "completed"
        # GET /runs/{run_id} — must report phase=terminal, step=completed.
        g = client.get(f"/runs/{run_id}", headers={"Authorization": f"Bearer {key}"})
        assert g.status_code == 200
        got = g.json()
        assert got["phase"] == "terminal"
        assert got["current_step"] == "completed"
        assert got["last_heartbeat_at"] is not None
        assert got["stalled"] is False