"""Focused tests for the P1 run observability contract
(TASK-AEE-RUN-OBSERVABILITY-P1).

Covers the work-order's 9 required scenarios:

  1. running fresh run not stalled
  2. stale non-terminal run stalled
  3. terminal run never stalled
  4. missing heartbeat/update timestamps
  5. stdout/stderr tail truncation
  6. GET /runs pure-read behavior (no side effects across reads)
  7. GET /runs/{run_id} envelope compatibility
  8. legacy rows remain readable
  9. stall threshold configurable via RUN_STALL_THRESHOLD_SECONDS

The pure-function tests (1-5, 9) exercise
``dispatcher.observability.derive_observability`` directly. The
endpoint tests (6-8) build a FastAPI TestClient via the shared
fixture in ``tests/_executor_test_helpers.py`` so they run against
a temp dispatcher DB — no real CLI, no real upstream Hermes, no
network.
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict

import pytest

from dispatcher.observability import (
    DEFAULT_RUN_STALL_THRESHOLD_SECONDS,
    OBSERVABILITY_FIELDS,
    TAIL_MAX_BYTES,
    derive_observability,
    derive_phase,
    evaluate_stall,
    get_stall_threshold_seconds,
)
from tests._executor_test_helpers import make_client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
#
# All pure-function tests use a frozen ``now`` so the stall decision is
# deterministic regardless of when the test runs.

_NOW = _dt.datetime(2026, 7, 22, 12, 0, 0, tzinfo=_dt.timezone.utc)


def _iso(dt: _dt.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 1. running fresh run not stalled
# ---------------------------------------------------------------------------

def test_running_fresh_run_not_stalled():
    """A non-terminal run whose ``updated_at`` is within the stall
    threshold is NOT stalled, and the observability envelope exposes
    the persisted heartbeat / step / phase.
    """
    row = {
        "status": "running",
        "updated_at": _iso(_NOW - _dt.timedelta(seconds=60)),
        "started_at": _iso(_NOW - _dt.timedelta(minutes=10)),
        "last_heartbeat_at": _iso(_NOW - _dt.timedelta(seconds=30)),
        "current_step": "running tests",
        "stdout_summary": "stdout so far",
        "stderr_summary": "",
    }
    env = derive_observability(row, now=_NOW)

    assert env["stalled"] is False
    assert env["stalled_reason"] == "recent_update"
    assert env["phase"] == "running"
    assert env["current_step"] == "running tests"
    assert env["last_heartbeat_at"] == _iso(_NOW - _dt.timedelta(seconds=30))
    assert env["stdout_tail"] == "stdout so far"
    assert env["stderr_tail"] == ""
    assert env["duration_seconds"] is None  # not finished yet
    assert env["seconds_since_update"] == 60


# ---------------------------------------------------------------------------
# 2. stale non-terminal run stalled
# ---------------------------------------------------------------------------

def test_stale_non_terminal_run_stalled():
    """A non-terminal run whose ``updated_at`` is older than the
    threshold IS stalled, with reason ``no_update``.
    """
    stale_seconds = DEFAULT_RUN_STALL_THRESHOLD_SECONDS + 60
    row = {
        "status": "running",
        "updated_at": _iso(_NOW - _dt.timedelta(seconds=stale_seconds)),
        "started_at": _iso(_NOW - _dt.timedelta(seconds=stale_seconds + 60)),
    }
    env = derive_observability(row, now=_NOW)

    assert env["stalled"] is True
    assert env["stalled_reason"] == "no_update"
    assert env["phase"] == "running"
    assert env["seconds_since_update"] == stale_seconds


def test_stale_threshold_boundary_not_stalled():
    """A run whose age equals exactly the threshold is NOT stalled
    (the policy is strict greater-than)."""
    threshold = DEFAULT_RUN_STALL_THRESHOLD_SECONDS
    row = {
        "status": "running",
        "updated_at": _iso(_NOW - _dt.timedelta(seconds=threshold)),
    }
    env = derive_observability(row, now=_NOW)
    assert env["stalled"] is False
    assert env["stalled_reason"] == "recent_update"


def test_stale_threshold_boundary_plus_one_stalled():
    """A run whose age is one second past the threshold IS stalled."""
    threshold = DEFAULT_RUN_STALL_THRESHOLD_SECONDS
    row = {
        "status": "running",
        "updated_at": _iso(_NOW - _dt.timedelta(seconds=threshold + 1)),
    }
    env = derive_observability(row, now=_NOW)
    assert env["stalled"] is True
    assert env["stalled_reason"] == "no_update"


# ---------------------------------------------------------------------------
# 3. terminal run never stalled
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["completed", "failed", "timeout", "cancelled"])
def test_terminal_run_never_stalled(status):
    """Every terminal status is never stalled, regardless of how old
    ``updated_at`` is. The phase is ``terminal``.
    """
    stale_seconds = DEFAULT_RUN_STALL_THRESHOLD_SECONDS * 10
    row = {
        "status": status,
        "updated_at": _iso(_NOW - _dt.timedelta(seconds=stale_seconds)),
        "started_at": _iso(_NOW - _dt.timedelta(seconds=stale_seconds + 100)),
        "finished_at": _iso(_NOW - _dt.timedelta(seconds=stale_seconds)),
    }
    env = derive_observability(row, now=_NOW)

    assert env["stalled"] is False
    assert env["stalled_reason"] == "terminal"
    assert env["phase"] == "terminal"
    # duration_seconds is computed from started_at→finished_at when both present
    assert env["duration_seconds"] == 100.0


def test_terminal_run_with_missing_finished_at():
    """A terminal run with no finished_at falls back to no
    duration (None) rather than fabricating one.
    """
    row = {"status": "completed", "updated_at": _iso(_NOW)}
    env = derive_observability(row, now=_NOW)
    assert env["stalled"] is False
    assert env["duration_seconds"] is None


# ---------------------------------------------------------------------------
# 4. missing heartbeat/update timestamps
# ---------------------------------------------------------------------------

def test_missing_updated_at_non_terminal():
    """A non-terminal run with no ``updated_at`` is NOT stalled and
    the reason is ``missing_timestamp`` — we do not fabricate a
    duration or pretend to know how stale the run is.
    """
    row = {"status": "running"}
    env = derive_observability(row, now=_NOW)
    assert env["stalled"] is False
    assert env["stalled_reason"] == "missing_timestamp"
    assert env["seconds_since_update"] is None
    assert env["updated_at"] is None


def test_missing_heartbeat_nullable():
    """``last_heartbeat_at`` is nullable — a row that never
    heartbeated exposes ``None``, not a fabricated timestamp.
    """
    row = {"status": "running", "updated_at": _iso(_NOW)}
    env = derive_observability(row, now=_NOW)
    assert env["last_heartbeat_at"] is None


def test_missing_stdout_stderr_nullable():
    """Missing stdout/stderr summaries produce ``None`` tails, not
    empty strings, so callers can distinguish "no output captured"
    from "empty output".
    """
    row = {"status": "running", "updated_at": _iso(_NOW)}
    env = derive_observability(row, now=_NOW)
    assert env["stdout_tail"] is None
    assert env["stderr_tail"] is None


# ---------------------------------------------------------------------------
# 5. stdout/stderr tail truncation
# ---------------------------------------------------------------------------

def test_stdout_tail_truncation():
    """A stdout_summary longer than ``TAIL_MAX_BYTES`` is truncated
    to the last ``TAIL_MAX_BYTES`` bytes (tail slice — preserves the
    root cause at the end of a stack trace).
    """
    big = "x" * (TAIL_MAX_BYTES + 5000)
    row = {
        "status": "completed",
        "updated_at": _iso(_NOW),
        "stdout_summary": big,
    }
    env = derive_observability(row, now=_NOW)
    assert env["stdout_tail"] is not None
    assert len(env["stdout_tail"].encode("utf-8")) <= TAIL_MAX_BYTES
    # Tail slice — the last bytes are preserved
    assert env["stdout_tail"].endswith("x" * 100)


def test_stderr_tail_truncation():
    big = "e" * (TAIL_MAX_BYTES + 1000)
    row = {
        "status": "failed",
        "updated_at": _iso(_NOW),
        "stderr_summary": big,
    }
    env = derive_observability(row, now=_NOW)
    assert env["stderr_tail"] is not None
    assert len(env["stderr_tail"].encode("utf-8")) <= TAIL_MAX_BYTES


def test_tail_multibyte_safe():
    """The tail slice is taken on UTF-8 bytes and re-decoded with
    errors=replace so a multi-byte char is never split.
    """
    # A string with multi-byte chars near the boundary
    body = "中" * 2000 + "tail_marker"
    row = {
        "status": "completed",
        "updated_at": _iso(_NOW),
        "stdout_summary": body,
    }
    env = derive_observability(row, now=_NOW)
    assert env["stdout_tail"] is not None
    # Must end with the ASCII marker (preserved by tail slice)
    assert env["stdout_tail"].endswith("tail_marker")
    # Must be decodable (no surrogate halves from a split char)
    env["stdout_tail"].encode("utf-8")  # round-trip OK


def test_tail_empty_string_preserved():
    """An empty stdout_summary produces an empty string tail, not None —
    so callers can distinguish "empty output" from "no output captured".
    """
    row = {
        "status": "completed",
        "updated_at": _iso(_NOW),
        "stdout_summary": "",
    }
    env = derive_observability(row, now=_NOW)
    assert env["stdout_tail"] == ""


# ---------------------------------------------------------------------------
# 6. GET /runs pure-read behavior (no side effects across reads)
# ---------------------------------------------------------------------------

def _seed_run_for_list(tmp_path: Path, **kwargs):
    """Seed a single executor_runs row directly into the temp DB."""
    from dispatcher import db as ddb
    from dispatcher.executor_runs import ensure_schema, upsert_run

    db_path = tmp_path / "dispatcher.db"
    conn = sqlite3.connect(db_path)
    ddb._init_schema(conn)
    ensure_schema(conn)
    upsert_run(conn, **kwargs)
    conn.close()
    return db_path


def test_get_runs_pure_read_no_side_effects(monkeypatch, tmp_path):
    """Two consecutive GET /runs calls return byte-for-byte identical
    envelopes. The endpoint does not poll executors, launch work,
    mutate state, or scan the repo (work-order §3).
    """
    client, app_module, key = make_client(monkeypatch, tmp_path)

    # Seed a row directly into the temp DB so the list endpoint has
    # something to return without invoking POST /runs/executor.
    from dispatcher import db as ddb
    from dispatcher.executor_runs import ensure_schema, upsert_run

    monkeypatch.setattr(ddb, "_initialized", False)
    if hasattr(ddb._local, "conn"):
        ddb._local.conn = None

    conn = ddb.get_conn()
    ensure_schema(conn)
    upsert_run(
        conn,
        run_id="run-p1-fresh",
        requested_executor="claude-code-cli",
        selected_executor="claude-code-cli",
        status="running",
        progress=0.5,
        stdout_summary="working",
        stderr_summary="",
        last_heartbeat_at=_iso(_NOW - _dt.timedelta(seconds=30)),
        current_step="running tests",
    )
    conn.commit()

    resp1 = client.get("/runs", headers={"Authorization": f"Bearer {key}"})
    assert resp1.status_code == 200
    body1 = resp1.json()

    resp2 = client.get("/runs", headers={"Authorization": f"Bearer {key}"})
    assert resp2.status_code == 200
    body2 = resp2.json()

    # Pure read — byte-for-byte identical across consecutive calls
    assert body1 == body2

    # The observability fields are present in each item
    item = body1["items"][0]
    for field in OBSERVABILITY_FIELDS:
        assert field in item, f"missing observability field: {field}"
    assert item["stalled"] in (True, False)
    assert item["phase"] == "running"


def test_get_runs_does_not_mutate_db(monkeypatch, tmp_path):
    """GET /runs must not create or mutate any executor_runs row."""
    client, app_module, key = make_client(monkeypatch, tmp_path)

    from dispatcher import db as ddb
    from dispatcher.executor_runs import ensure_schema, upsert_run

    monkeypatch.setattr(ddb, "_initialized", False)
    if hasattr(ddb._local, "conn"):
        ddb._local.conn = None

    conn = ddb.get_conn()
    ensure_schema(conn)
    upsert_run(
        conn,
        run_id="run-p1-stable",
        requested_executor="claude-code-cli",
        selected_executor="claude-code-cli",
        status="completed",
        progress=1.0,
        stdout_summary="done",
        stderr_summary="",
    )
    conn.commit()

    db_path = ddb.DB_PATH

    def _snapshot():
        # Open a fresh independent sqlite3 connection (NOT the
        # dispatcher's thread-local handle, which the test client
        # may close between calls) to read the row verbatim.
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT * FROM executor_runs WHERE run_id = ?", ("run-p1-stable",)
        ).fetchone()
        d = dict(row)
        c.close()
        return d

    before = _snapshot()

    # Two reads
    for _ in range(2):
        client.get("/runs", headers={"Authorization": f"Bearer {key}"})

    after = _snapshot()

    # No mutation — every column byte-identical
    assert before == after


# ---------------------------------------------------------------------------
# 7. GET /runs/{run_id} envelope compatibility
# ---------------------------------------------------------------------------

def test_get_run_envelope_has_observability_fields(monkeypatch, tmp_path):
    """GET /runs/{run_id} returns the canonical run envelope PLUS the
    P1 observability fields. The canonical fields (run_id, status,
    progress, source, is_terminal) are preserved; observability
    fields are added alongside (work-order §8).
    """
    client, app_module, key = make_client(monkeypatch, tmp_path)

    from dispatcher import db as ddb
    from dispatcher.executor_runs import ensure_schema, upsert_run

    monkeypatch.setattr(ddb, "_initialized", False)
    if hasattr(ddb._local, "conn"):
        ddb._local.conn = None

    conn = ddb.get_conn()
    ensure_schema(conn)
    upsert_run(
        conn,
        run_id="run-p1-get",
        requested_executor="claude-code-cli",
        selected_executor="claude-code-cli",
        status="running",
        progress=0.25,
        stdout_summary="in progress",
        stderr_summary="",
        last_heartbeat_at=_iso(_NOW - _dt.timedelta(seconds=10)),
        current_step="writing tests",
    )
    conn.commit()

    resp = client.get("/runs/run-p1-get", headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 200
    env = resp.json()

    # Canonical fields preserved
    assert env["run_id"] == "run-p1-get"
    assert env["status"] == "running"
    assert env["progress"] == 0.25
    assert env["source"] == "executor_runs"
    assert env["is_terminal"] is False

    # Observability fields present
    for field in OBSERVABILITY_FIELDS:
        assert field in env, f"missing observability field: {field}"

    assert env["phase"] == "running"
    assert env["current_step"] == "writing tests"
    assert env["stalled"] is False  # fresh run, just heartbeated


def test_get_run_terminal_canonical_fields_preserved(monkeypatch, tmp_path):
    """A terminal run's canonical fields (exit_code, completed_at,
    error) are preserved alongside the observability additions.
    """
    client, app_module, key = make_client(monkeypatch, tmp_path)

    from dispatcher import db as ddb
    from dispatcher.executor_runs import ensure_schema, upsert_run

    monkeypatch.setattr(ddb, "_initialized", False)
    if hasattr(ddb._local, "conn"):
        ddb._local.conn = None

    conn = ddb.get_conn()
    ensure_schema(conn)
    upsert_run(
        conn,
        run_id="run-p1-done",
        requested_executor="claude-code-cli",
        selected_executor="claude-code-cli",
        status="completed",
        progress=1.0,
        exit_code=0,
        stdout_summary="all good",
        stderr_summary="",
    )
    conn.commit()

    resp = client.get("/runs/run-p1-done", headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 200
    env = resp.json()

    assert env["status"] == "completed"
    assert env["is_terminal"] is True
    assert env["exit_code"] == 0
    assert env["phase"] == "terminal"
    assert env["stalled"] is False
    assert env["stalled_reason"] == "terminal"


# ---------------------------------------------------------------------------
# 8. legacy rows remain readable
# ---------------------------------------------------------------------------

def test_legacy_row_without_observability_columns(monkeypatch, tmp_path):
    """A row written by a pre-P1 dispatch (no ``last_heartbeat_at``,
    ``current_step``, ``phase`` columns) is still readable and the
    observability envelope degrades gracefully (NULL → None, missing
    timestamp → ``missing_timestamp`` reason).
    """
    client, app_module, key = make_client(monkeypatch, tmp_path)

    from dispatcher import db as ddb
    from dispatcher.executor_runs import ensure_schema

    monkeypatch.setattr(ddb, "_initialized", False)
    if hasattr(ddb._local, "conn"):
        ddb._local.conn = None

    conn = ddb.get_conn()
    ensure_schema(conn)
    # Insert a legacy-shape row directly, WITHOUT the new columns,
    # by dropping them from the INSERT. We use raw SQL to simulate
    # a pre-migration write.
    conn.execute(
        """
        INSERT INTO executor_runs (
          run_id, requested_executor, selected_executor, task_id, status,
          progress, exit_code, timeout_state, cancel_state,
          stdout_summary, stderr_summary,
          artifact_paths_json, artifact_verification_json,
          git_evidence_json, telegram_result_json,
          runtime_identity_json, routing_json, error,
          created_at, updated_at, completed_at
        ) VALUES (?, NULL, ?, NULL, ?, 0.0, 0, NULL, NULL, ?, '', '[]', '[]', NULL, '{}', NULL, '{}', NULL, ?, ?, ?)
        """,
        (
            "run-p1-legacy",
            "claude-code-cli",
            "completed",
            "legacy stdout",
            _iso(_NOW - _dt.timedelta(hours=1)),
            _iso(_NOW - _dt.timedelta(hours=1)),
            _iso(_NOW - _dt.timedelta(hours=1)),
        ),
    )
    conn.commit()

    resp = client.get("/runs/run-p1-legacy", headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 200
    env = resp.json()

    # Canonical fields readable
    assert env["run_id"] == "run-p1-legacy"
    assert env["status"] == "completed"
    # Observability: missing columns → None / missing_timestamp policy
    assert env["last_heartbeat_at"] is None
    assert env["current_step"] is None
    # phase derives from status when the persisted phase column is NULL
    assert env["phase"] == "terminal"
    # Terminal runs are never stalled
    assert env["stalled"] is False
    assert env["stalled_reason"] == "terminal"


def test_legacy_running_row_missing_timestamps(monkeypatch, tmp_path):
    """A legacy non-terminal row whose ``updated_at`` is unparseable
    is not stalled and the reason is ``missing_timestamp`` (we do
    not fabricate a duration from a bad timestamp). The
    ``executor_runs.updated_at`` column is NOT NULL, but a
    pre-migration write may have stored a non-ISO-8601 string;
    ``_parse_iso`` returns None for unparseable input, so the
    stall policy produces a deterministic non-fabricated outcome.
    """
    from dispatcher import db as ddb
    from dispatcher.executor_runs import ensure_schema

    monkeypatch.setattr(ddb, "_initialized", False)
    if hasattr(ddb._local, "conn"):
        ddb._local.conn = None

    client, app_module, key = make_client(monkeypatch, tmp_path)
    conn = ddb.get_conn()
    ensure_schema(conn)
    # Insert a legacy row with a non-ISO updated_at (simulating a
    # pre-P1 bug or a row written with a non-canonical timestamp).
    conn.execute(
        """
        INSERT INTO executor_runs (
          run_id, requested_executor, selected_executor, task_id, status,
          progress, exit_code, timeout_state, cancel_state,
          stdout_summary, stderr_summary,
          artifact_paths_json, artifact_verification_json,
          git_evidence_json, telegram_result_json,
          runtime_identity_json, routing_json, error,
          created_at, updated_at, completed_at
        ) VALUES (?, NULL, ?, NULL, ?, 0.0, NULL, NULL, NULL, '', '', '[]', '[]', NULL, '{}', NULL, '{}', NULL, ?, ?, NULL)
        """,
        (
            "run-p1-legacy-running",
            "claude-code-cli",
            "running",
            _iso(_NOW),
            "not-an-iso-timestamp",  # unparseable → missing_timestamp
        ),
    )
    conn.commit()

    resp = client.get(
        "/runs/run-p1-legacy-running",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    env = resp.json()
    assert env["status"] == "running"
    assert env["stalled"] is False
    assert env["stalled_reason"] == "missing_timestamp"
    assert env["seconds_since_update"] is None


# ---------------------------------------------------------------------------
# 9. stall threshold configurable via RUN_STALL_THRESHOLD_SECONDS
# ---------------------------------------------------------------------------

def test_stall_threshold_env_override(monkeypatch):
    """``RUN_STALL_THRESHOLD_SECONDS`` overrides the default. A run
    that is fresh under the default but stale under a tiny override
    is correctly flagged as stalled.
    """
    monkeypatch.setenv("RUN_STALL_THRESHOLD_SECONDS", "30")
    assert get_stall_threshold_seconds() == 30

    row = {
        "status": "running",
        "updated_at": _iso(_NOW - _dt.timedelta(seconds=60)),
    }
    env = derive_observability(row, now=_NOW)
    assert env["stalled"] is True
    assert env["stalled_reason"] == "no_update"


def test_stall_threshold_default(monkeypatch):
    monkeypatch.delenv("RUN_STALL_THRESHOLD_SECONDS", raising=False)
    assert get_stall_threshold_seconds() == DEFAULT_RUN_STALL_THRESHOLD_SECONDS


def test_stall_threshold_malformed_falls_back(monkeypatch):
    """A malformed env value falls back to the default rather than
    raising — the read path must never fail on config.
    """
    monkeypatch.setenv("RUN_STALL_THRESHOLD_SECONDS", "not-an-int")
    assert get_stall_threshold_seconds() == DEFAULT_RUN_STALL_THRESHOLD_SECONDS


def test_stall_threshold_negative_falls_back(monkeypatch):
    """A non-positive threshold is nonsensical; fall back to default."""
    monkeypatch.setenv("RUN_STALL_THRESHOLD_SECONDS", "-5")
    assert get_stall_threshold_seconds() == DEFAULT_RUN_STALL_THRESHOLD_SECONDS


def test_stall_threshold_zero_falls_back(monkeypatch):
    monkeypatch.setenv("RUN_STALL_THRESHOLD_SECONDS", "0")
    assert get_stall_threshold_seconds() == DEFAULT_RUN_STALL_THRESHOLD_SECONDS


# ---------------------------------------------------------------------------
# Bonus: phase derivation coverage
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status,expected",
    [
        ("queued", "queued"),
        ("pending", "queued"),
        ("started", "queued"),
        ("running", "running"),
        ("waiting", "running"),
        ("completed", "terminal"),
        ("failed", "terminal"),
        ("timeout", "terminal"),
        ("cancelled", "terminal"),
        (None, "unknown"),
        ("bogus", "unknown"),
    ],
)
def test_derive_phase(status, expected):
    assert derive_phase(status) == expected


# ---------------------------------------------------------------------------
# Bonus: evaluate_stall direct coverage
# ---------------------------------------------------------------------------

def test_evaluate_stall_terminal_never_stalled():
    res = evaluate_stall(status="completed", updated_at=None, now=_NOW)
    assert res == {"stalled": False, "stalled_reason": "terminal"}


def test_evaluate_stall_missing_timestamp_non_terminal():
    res = evaluate_stall(status="running", updated_at=None, now=_NOW)
    assert res == {"stalled": False, "stalled_reason": "missing_timestamp"}


def test_evaluate_stall_unknown_status_treated_as_non_terminal():
    """An unknown status is NOT trusted as terminal — it goes
    through the non-terminal branch (missing_timestamp when no
    updated_at, recent_update / no_update when there is one).
    """
    res = evaluate_stall(status="weird", updated_at=None, now=_NOW)
    assert res["stalled"] is False
    assert res["stalled_reason"] == "missing_timestamp"


# ---------------------------------------------------------------------------
# Bonus: OBSERVABILITY_FIELDS contract stability
# ---------------------------------------------------------------------------

def test_observability_fields_canonical_set():
    """The field set is the exact canonical contract — adding or
    removing a field is a breaking change and must be intentional.
    """
    assert OBSERVABILITY_FIELDS == (
        "updated_at",
        "last_heartbeat_at",
        "current_step",
        "phase",
        "duration_seconds",
        "seconds_since_update",
        "stdout_tail",
        "stderr_tail",
        "stalled",
        "stalled_reason",
    )


def test_no_eta_field_present():
    """The contract deliberately omits ETA — no evidence-backed
    estimator exists, so we expose ``duration_seconds`` and
    ``seconds_since_update`` only. There must be no ``eta_seconds``
    or ``estimated_completion`` field.
    """
    env = derive_observability({"status": "running"}, now=_NOW)
    assert "eta_seconds" not in env
    assert "estimated_completion" not in env
    assert "eta" not in env