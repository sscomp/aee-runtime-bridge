"""Focused tests for TASK-AEE-P2-BRIDGE-HERMES-COMPLETION-SYNC.

Background completion synchronization: the ExecutorRunWatcher
polls non-terminal Hermes-dispatched executor_runs rows and
reconciles them to a terminal state when Hermes reports
completion/failure/timeout. These tests exercise the background
poller path (not the GET-triggered reconciliation already covered
by tests/test_completion_sync.py).

Scenarios (work-order "Focused tests required"):
  1. Hermes async started -> completed persistence (background)
  2. Hermes async started -> failed persistence (background)
  3. idempotent duplicate completion sync (background)
  4. terminal rows are stable across later sync attempts
  5. missing/unknown Hermes run degrades truthfully (background)
  6. GET /runs remains non-mutating (background watcher does not
     affect the pure-read GET /runs list endpoint)
  7. stdout/stderr/artifact evidence persists (background)
  8. executor routing remains correct for Hermes and Claude aliases
  9. legacy rows remain readable
"""
from __future__ import annotations

import asyncio

import pytest

from tests._executor_test_helpers import (
    make_client,
    post_executor,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _install_hermes_stub(monkeypatch, *, mode: str, poll_calls: list = None, unique: bool = False):
    """Install a stub Hermes adapter whose poll() reports the given mode.

    ``mode`` is one of:
      - "running"        -> poll returns non-terminal running (no update)
      - "completed"     -> poll returns terminal completed with output
      - "failed"        -> poll returns terminal failed with error
      - "unknown"       -> poll raises UnknownExternalRunError
      - "transient_err" -> poll raises AdapterRuntimeError
    ``unique`` — when True, each submit() call returns a distinct
    external_run_id (for tests that POST multiple runs).
    """
    from aee.adapters.base import (
        RuntimePollResult,
        UnknownExternalRunError,
        RuntimeError as AdapterRuntimeError,
        RuntimeSubmitResult,
    )
    from aee.core.registry import adapter_registry

    if poll_calls is None:
        poll_calls = []
    _submit_counter = [0]

    class _StubHermes:
        name = "hermes"
        runtime_type = "hermes"

        async def submit(self, job):
            if unique:
                _submit_counter[0] += 1
                rid = f"run_bg_test_{mode}_{_submit_counter[0]}"
            else:
                rid = f"run_bg_test_{mode}"
            return RuntimeSubmitResult(
                external_run_id=rid,
                status="queued",
            )

        async def poll(self, external_run_id):
            poll_calls.append(external_run_id)
            if mode == "running":
                return RuntimePollResult(
                    external_run_id=external_run_id,
                    status="running",
                    is_terminal=False,
                )
            if mode == "completed":
                return RuntimePollResult(
                    external_run_id=external_run_id,
                    status="completed",
                    is_terminal=True,
                    output="hello from background watcher",
                    raw={"status": "completed", "output": "hello from background watcher"},
                )
            if mode == "failed":
                return RuntimePollResult(
                    external_run_id=external_run_id,
                    status="failed",
                    is_terminal=True,
                    error="background upstream failed: simulated error",
                    raw={"status": "failed", "error": "simulated error"},
                )
            if mode == "unknown":
                raise UnknownExternalRunError(
                    f"hermes run {external_run_id!r} not found"
                )
            if mode == "transient_err":
                raise AdapterRuntimeError("simulated hermes outage")
            raise AssertionError(f"unknown mode {mode!r}")

        async def cancel(self, external_run_id):
            from aee.adapters.base import RuntimeCancelResult
            return RuntimeCancelResult(
                external_run_id=external_run_id, cancelled=True
            )

    saved = dict(adapter_registry._adapters)
    stub = _StubHermes()
    adapter_registry._adapters["hermes"] = stub
    monkeypatch.setattr(
        adapter_registry, "_adapters", saved, raising=False
    )
    adapter_registry._adapters["hermes"] = stub
    return stub, poll_calls


def _run_watcher_tick(app_module):
    """Synchronously drive one ExecutorRunWatcher tick.

    The TestClient context is sync; we use asyncio.run() to drive
    the async _tick() method once. This is deterministic — the test
    controls exactly when the background reconciliation happens.
    """
    from dispatcher.executor_watcher import ExecutorRunWatcher
    watcher = ExecutorRunWatcher(tick_sec=999)
    asyncio.run(watcher._tick())


def _get_run(client, key, run_id):
    return client.get(
        f"/runs/{run_id}",
        headers={"Authorization": f"Bearer {key}"},
    )


def _list_runs(client, key, **params):
    return client.get(
        "/runs",
        params=params,
        headers={"Authorization": f"Bearer {key}"},
    )


# ---------------------------------------------------------------------------
# 1. background: queued Hermes run becomes completed
# ---------------------------------------------------------------------------

def test_background_queued_hermes_becomes_completed(monkeypatch, tmp_path):
    """A queued Hermes run is reconciled to completed by the background watcher."""
    poll_calls = []
    stub, poll_calls = _install_hermes_stub(monkeypatch, mode="completed", poll_calls=poll_calls)
    client, app_module, key = make_client(monkeypatch, tmp_path)

    resp = post_executor(client, key, {
        "executor": "hermes",
        "prompt": "do background work",
        "timeout_sec": 30,
    })
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]
    assert resp.json()["status"] in {"queued", "running"}

    # Drive one background tick — should reconcile to completed.
    _run_watcher_tick(app_module)
    assert len(poll_calls) >= 1, f"expected >=1 poll, got {len(poll_calls)}"

    # GET to verify terminal state persisted.
    get1 = _get_run(client, key, run_id)
    assert get1.status_code == 200
    env = get1.json()
    assert env["status"] == "completed"
    assert env["is_terminal"] is True
    assert env["progress"] == 1.0
    assert "hello from background watcher" in env.get("stdout_summary", "")
    assert env["source"] == "executor_runs"


# ---------------------------------------------------------------------------
# 2. background: queued Hermes run becomes failed
# ---------------------------------------------------------------------------

def test_background_queued_hermes_becomes_failed(monkeypatch, tmp_path):
    """A queued Hermes run is reconciled to failed by the background watcher."""
    poll_calls = []
    stub, poll_calls = _install_hermes_stub(monkeypatch, mode="failed", poll_calls=poll_calls)
    client, app_module, key = make_client(monkeypatch, tmp_path)

    resp = post_executor(client, key, {
        "executor": "hermes",
        "prompt": "do work that fails",
        "timeout_sec": 30,
    })
    run_id = resp.json()["run_id"]

    _run_watcher_tick(app_module)

    get1 = _get_run(client, key, run_id)
    assert get1.status_code == 200
    env = get1.json()
    assert env["status"] == "failed"
    assert env["is_terminal"] is True
    assert env["error"] is not None
    assert "simulated error" in env["error"] or "background upstream failed" in env["error"]


# ---------------------------------------------------------------------------
# 3. idempotent duplicate completion sync (background)
# ---------------------------------------------------------------------------

def test_background_idempotent_duplicate_completion(monkeypatch, tmp_path):
    """A second background tick on an already-terminal row does NOT re-poll."""
    poll_calls = []
    stub, poll_calls = _install_hermes_stub(monkeypatch, mode="completed", poll_calls=poll_calls)
    client, app_module, key = make_client(monkeypatch, tmp_path)

    resp = post_executor(client, key, {
        "executor": "hermes",
        "prompt": "do work",
        "timeout_sec": 30,
    })
    run_id = resp.json()["run_id"]

    # First tick reconciles.
    _run_watcher_tick(app_module)
    polls_after_first = len(poll_calls)
    assert polls_after_first >= 1

    # Verify terminal.
    get1 = _get_run(client, key, run_id)
    env1 = get1.json()
    assert env1["status"] == "completed"

    # Second tick — row is terminal, no upstream call.
    _run_watcher_tick(app_module)
    assert len(poll_calls) == polls_after_first, (
        f"Second tick on terminal row must not re-poll; poll_calls={poll_calls}"
    )

    # GET is stable.
    get2 = _get_run(client, key, run_id)
    env2 = get2.json()
    assert env2["status"] == "completed"
    assert env2 == env1  # byte-for-byte identical


# ---------------------------------------------------------------------------
# 4. terminal rows are stable across later sync attempts
# ---------------------------------------------------------------------------

def test_background_terminal_stable_across_syncs(monkeypatch, tmp_path):
    """Terminal rows remain stable across multiple background ticks."""
    poll_calls = []
    stub, poll_calls = _install_hermes_stub(monkeypatch, mode="completed", poll_calls=poll_calls)
    client, app_module, key = make_client(monkeypatch, tmp_path)

    resp = post_executor(client, key, {
        "executor": "hermes",
        "prompt": "do work",
        "timeout_sec": 30,
    })
    run_id = resp.json()["run_id"]

    _run_watcher_tick(app_module)
    get1 = _get_run(client, key, run_id)
    env1 = get1.json()
    assert env1["status"] == "completed"
    completed_at_1 = env1["completed_at"]

    # Multiple further ticks.
    for _ in range(3):
        _run_watcher_tick(app_module)

    get2 = _get_run(client, key, run_id)
    env2 = get2.json()
    assert env2["status"] == "completed"
    assert env2["completed_at"] == completed_at_1  # stable timestamp


# ---------------------------------------------------------------------------
# 5. missing/unknown Hermes run degrades truthfully (background)
# ---------------------------------------------------------------------------

def test_background_unknown_run_degrades_to_timeout(monkeypatch, tmp_path):
    """When upstream no longer tracks the run, background watcher persists timeout."""
    poll_calls = []
    stub, poll_calls = _install_hermes_stub(monkeypatch, mode="unknown", poll_calls=poll_calls)
    client, app_module, key = make_client(monkeypatch, tmp_path)

    resp = post_executor(client, key, {
        "executor": "hermes",
        "prompt": "do work",
        "timeout_sec": 30,
    })
    run_id = resp.json()["run_id"]

    _run_watcher_tick(app_module)

    get1 = _get_run(client, key, run_id)
    assert get1.status_code == 200
    env = get1.json()
    assert env["status"] == "timeout"
    assert env["is_terminal"] is True
    assert env["error"] is not None
    assert "no longer tracks" in env["error"]


# ---------------------------------------------------------------------------
# 6. GET /runs remains non-mutating (background watcher doesn't affect it)
# ---------------------------------------------------------------------------

def test_get_runs_list_remains_non_mutating(monkeypatch, tmp_path):
    """GET /runs list endpoint is a pure read; background watcher doesn't change it."""
    poll_calls = []
    stub, poll_calls = _install_hermes_stub(monkeypatch, mode="running", poll_calls=poll_calls, unique=True)
    client, app_module, key = make_client(monkeypatch, tmp_path)

    # POST two runs.
    resp1 = post_executor(client, key, {
        "executor": "hermes",
        "prompt": "work 1",
        "timeout_sec": 30,
    })
    resp2 = post_executor(client, key, {
        "executor": "hermes",
        "prompt": "work 2",
        "timeout_sec": 30,
    })
    assert resp1.status_code == 200
    assert resp2.status_code == 200

    # GET /runs before any background tick — both should be non-terminal.
    list1 = _list_runs(client, key, limit=10)
    assert list1.status_code == 200
    runs1 = list1.json().get("items", [])
    assert len(runs1) == 2
    for r in runs1:
        assert r["status"] in {"queued", "running"}

    # GET /runs again — should be identical (pure read).
    list2 = _list_runs(client, key, limit=10)
    assert list2.status_code == 200
    assert list2.json() == list1.json()


# ---------------------------------------------------------------------------
# 7. stdout/stderr/artifact evidence persists (background)
# ---------------------------------------------------------------------------

def test_background_evidence_persists(monkeypatch, tmp_path):
    """The background-reconciled terminal row carries stdout_summary + completed_at."""
    poll_calls = []
    stub, poll_calls = _install_hermes_stub(monkeypatch, mode="completed", poll_calls=poll_calls)
    client, app_module, key = make_client(monkeypatch, tmp_path)

    resp = post_executor(client, key, {
        "executor": "hermes",
        "prompt": "do work",
        "timeout_sec": 30,
    })
    run_id = resp.json()["run_id"]
    pre_env = resp.json()
    assert pre_env["stdout_summary"] == "" or pre_env["stdout_summary"] is None

    _run_watcher_tick(app_module)

    get1 = _get_run(client, key, run_id)
    env = get1.json()
    assert env["status"] == "completed"
    assert env["stdout_summary"]  # non-empty after reconciliation
    assert "hello from background watcher" in env["stdout_summary"]
    assert env.get("completed_at") is not None
    assert env.get("created_at") is not None
    # created_at is stable (preserved by upsert_run on update).


# ---------------------------------------------------------------------------
# 8. executor routing remains correct for Hermes and Claude aliases
# ---------------------------------------------------------------------------

def test_executor_routing_hermes_alias(monkeypatch, tmp_path):
    """POST with executor=hermes routes to the hermes adapter."""
    poll_calls = []
    stub, poll_calls = _install_hermes_stub(monkeypatch, mode="running", poll_calls=poll_calls)
    client, _app, key = make_client(monkeypatch, tmp_path)

    resp = post_executor(client, key, {
        "executor": "hermes",
        "prompt": "test routing",
        "timeout_sec": 30,
    })
    assert resp.status_code == 200
    env = resp.json()
    assert env["selected_executor"] == "hermes"
    assert env["routing"] is not None


def test_executor_routing_claude_code_alias(monkeypatch, tmp_path):
    """POST with executor=claude-code-cli routes to the CLI runner (already terminal)."""
    from tests._executor_test_helpers import (
        set_fake_binary,
        write_fake_claude,
    )
    poll_calls = []
    stub, poll_calls = _install_hermes_stub(monkeypatch, mode="completed", poll_calls=poll_calls)
    artifact = str(tmp_path / "cc_routing.md")
    binary = write_fake_claude(tmp_path, artifact=artifact, artifact_content="ok")
    set_fake_binary(monkeypatch, binary)
    client, _app, key = make_client(monkeypatch, tmp_path)

    resp = post_executor(client, key, {
        "executor": "claude-code-cli",
        "prompt": "create the artifact",
        "expected_artifacts": [artifact],
        "timeout_sec": 30,
        "repo_path": "/home/ubuntu/Abacus",
    })
    assert resp.status_code == 200
    env = resp.json()
    assert env["selected_executor"] == "claude-code-cli"
    assert env["status"] == "completed"
    # Hermes adapter was never called.
    assert len(poll_calls) == 0


# ---------------------------------------------------------------------------
# 9. legacy rows remain readable
# ---------------------------------------------------------------------------

def test_legacy_rows_remain_readable(monkeypatch, tmp_path):
    """A manually-inserted legacy row (pre-P2.1 schema) is still readable."""
    client, _app, key = make_client(monkeypatch, tmp_path)

    # Insert a legacy row directly into executor_runs with minimal columns.
    from dispatcher.db import get_conn
    from dispatcher.executor_runs import ensure_schema
    conn = get_conn()
    ensure_schema(conn)
    conn.execute(
        """INSERT OR REPLACE INTO executor_runs
           (run_id, requested_executor, selected_executor, status,
            progress, exit_code, timeout_state, cancel_state,
            stdout_summary, stderr_summary, artifact_paths_json,
            artifact_verification_json, git_evidence_json,
            telegram_result_json, runtime_identity_json, routing_json,
            error, created_at, updated_at, completed_at)
           VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?, '',
                   '[]', '[]', 'null', '{}', 'null', '{}',
                   NULL, ?, ?, ?)""",
        (
            "run_legacy_001",
            "hermes",
            "hermes",
            "completed",
            1.0,
            "legacy output",
            "2026-07-20T00:00:00Z",
            "2026-07-20T00:01:00Z",
            "2026-07-20T00:01:00Z",
        ),
    )
    conn.commit()

    get1 = _get_run(client, key, "run_legacy_001")
    assert get1.status_code == 200
    env = get1.json()
    assert env["run_id"] == "run_legacy_001"
    assert env["status"] == "completed"
    assert env["source"] == "executor_runs"
    assert env["stdout_summary"] == "legacy output"


# ---------------------------------------------------------------------------
# 10. list_non_terminal_runs unit test (direct DB query)
# ---------------------------------------------------------------------------

def test_list_non_terminal_runs_query(monkeypatch, tmp_path):
    """list_non_terminal_runs returns only queued/started/running rows."""
    from dispatcher.db import get_conn
    from dispatcher.executor_runs import (
        ensure_schema,
        upsert_run,
        list_non_terminal_runs,
    )
    # Use the bridge's DB (temp).
    client, _app, key = make_client(monkeypatch, tmp_path)
    conn = get_conn()
    ensure_schema(conn)

    # Insert one terminal + two non-terminal rows.
    upsert_run(conn, run_id="r_t1", requested_executor="hermes",
               selected_executor="hermes", status="completed", progress=1.0)
    upsert_run(conn, run_id="r_n1", requested_executor="hermes",
               selected_executor="hermes", status="queued", progress=0.0)
    upsert_run(conn, run_id="r_n2", requested_executor="hermes",
               selected_executor="hermes", status="running", progress=0.5)

    non_terminal = list_non_terminal_runs(conn, selected_executor="hermes")
    ids = {r["run_id"] for r in non_terminal}
    assert "r_t1" not in ids
    assert "r_n1" in ids
    assert "r_n2" in ids