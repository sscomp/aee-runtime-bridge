"""Focused tests for GET /runs (TASK-AEE-RUN-LIST-ENDPOINT).

Covers the work-order's 10 required scenarios:

  1. default recent list
  2. newest-first deterministic ordering (with tie-breaker on run_id)
  3. limit bounds (min/max + default)
  4. status filter
  5. executor filter
  6. since filter
  7. empty result
  8. malformed inputs (invalid status, invalid since, out-of-range limit)
  9. auth required
 10. no executor/upstream side effects

These tests use the same fixtures as the run-tracking suite
(``tests/_executor_test_helpers.py``) so they run against a temp
dispatcher DB — no real CLI, no real upstream Hermes, no network.
"""
from __future__ import annotations

from typing import Any, Dict, List

from tests._executor_test_helpers import make_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _list_runs(
    client,
    key: str,
    *,
    limit: int | None = None,
    status: str | None = None,
    executor: str | None = None,
    since: str | None = None,
):
    params: Dict[str, Any] = {}
    if limit is not None:
        params["limit"] = limit
    if status is not None:
        params["status"] = status
    if executor is not None:
        params["executor"] = executor
    if since is not None:
        params["since"] = since
    return client.get(
        "/runs",
        params=params,
        headers={"Authorization": f"Bearer {key}"},
    )


def _seed_run(
    tmp_path,
    *,
    run_id: str,
    selected_executor: str = "claude-code-cli",
    requested_executor: str | None = "claude-code-cli",
    status: str = "completed",
    progress: float = 1.0,
    created_at: str = "2026-07-22T10:00:00Z",
    artifact_paths: List[str] | None = None,
    error: str | None = None,
) -> Dict[str, Any]:
    """Insert a run row directly into the temp executor_runs table.

    Bypasses POST /runs/executor so the test controls created_at
    exactly (the POST path stamps ``now`` which is non-deterministic).
    """
    import sqlite3

    from dispatcher import db as ddb

    db_path = ddb.DB_PATH
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        from dispatcher.executor_runs import ensure_schema, upsert_run

        ensure_schema(conn)
        env = upsert_run(
            conn,
            run_id=run_id,
            requested_executor=requested_executor,
            selected_executor=selected_executor,
            status=status,
            progress=progress,
            artifact_paths=artifact_paths or [],
            error=error,
        )
        # Override created_at to the deterministic test value so
        # ordering assertions are stable. upsert_run stamps ``now``,
        # so we patch the row after the upsert.
        conn.execute(
            "UPDATE executor_runs SET created_at = ? WHERE run_id = ?",
            (created_at, run_id),
        )
        conn.commit()
        env["created_at"] = created_at
        return env
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Required envelope fields for each list item (work-order §5)
# ---------------------------------------------------------------------------
_REQUIRED_ITEM_FIELDS = [
    "run_id",
    "requested_executor",
    "selected_executor",
    "status",
    "progress",
    "created_at",
    "updated_at",
    "completed_at",
    "is_terminal",
    "source",
    "artifact_paths",
    "error",
]


# ---------------------------------------------------------------------------
# 1. default recent list
# ---------------------------------------------------------------------------

def test_default_recent_list(monkeypatch, tmp_path):
    """GET /runs with no params returns up to 20 runs, newest first."""
    client, _app, key = make_client(monkeypatch, tmp_path)
    for i in range(3):
        _seed_run(
            tmp_path,
            run_id=f"run-default-{i}",
            created_at=f"2026-07-22T10:00:0{i}Z",
        )
    resp = _list_runs(client, key)
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["count"] == 3
    assert body["limit"] == 20
    assert body["filters"] == {"status": None, "executor": None, "since": None}
    items = body["items"]
    assert len(items) == 3
    # Each item carries the required fields
    for item in items:
        for field in _REQUIRED_ITEM_FIELDS:
            assert field in item, f"item missing required field: {field!r}"
    # Newest first
    assert items[0]["created_at"] >= items[1]["created_at"]
    assert items[1]["created_at"] >= items[2]["created_at"]


# ---------------------------------------------------------------------------
# 2. newest-first deterministic ordering (tie-breaker on run_id)
# ---------------------------------------------------------------------------

def test_deterministic_ordering_tie_breaker(monkeypatch, tmp_path):
    """Two runs sharing created_at are ordered by run_id DESC."""
    client, _app, key = make_client(monkeypatch, tmp_path)
    # Three runs: same created_at, different run_ids
    _seed_run(tmp_path, run_id="run-alpha", created_at="2026-07-22T10:00:00Z")
    _seed_run(tmp_path, run_id="run-zeta", created_at="2026-07-22T10:00:00Z")
    _seed_run(tmp_path, run_id="run-mid", created_at="2026-07-22T10:00:00Z")
    resp = _list_runs(client, key)
    assert resp.status_code == 200
    items = resp.json()["items"]
    run_ids = [item["run_id"] for item in items]
    # DESC tie-breaker: zeta > mid > alpha
    assert run_ids == ["run-zeta", "run-mid", "run-alpha"]


def test_newest_first_across_different_timestamps(monkeypatch, tmp_path):
    """Runs with distinct created_at are ordered newest-first."""
    client, _app, key = make_client(monkeypatch, tmp_path)
    _seed_run(tmp_path, run_id="run-old", created_at="2026-07-22T08:00:00Z")
    _seed_run(tmp_path, run_id="run-new", created_at="2026-07-22T12:00:00Z")
    _seed_run(tmp_path, run_id="run-mid", created_at="2026-07-22T10:00:00Z")
    resp = _list_runs(client, key)
    items = resp.json()["items"]
    run_ids = [item["run_id"] for item in items]
    assert run_ids == ["run-new", "run-mid", "run-old"]


# ---------------------------------------------------------------------------
# 3. limit bounds
# ---------------------------------------------------------------------------

def test_limit_bounds_default(monkeypatch, tmp_path):
    """Default limit is 20."""
    client, _app, key = make_client(monkeypatch, tmp_path)
    for i in range(25):
        _seed_run(
            tmp_path,
            run_id=f"run-limit-d-{i}",
            created_at=f"2026-07-22T10:00:{i:02d}Z",
        )
    resp = _list_runs(client, key)
    body = resp.json()
    assert body["count"] == 20
    assert body["limit"] == 20


def test_limit_clamped_to_max(monkeypatch, tmp_path):
    """limit=200 (above max=100) is rejected by FastAPI validation (422)."""
    client, _app, key = make_client(monkeypatch, tmp_path)
    resp = _list_runs(client, key, limit=200)
    assert resp.status_code == 422


def test_limit_zero_rejected(monkeypatch, tmp_path):
    """limit=0 (below min=1) is rejected by FastAPI validation (422)."""
    client, _app, key = make_client(monkeypatch, tmp_path)
    resp = _list_runs(client, key, limit=0)
    assert resp.status_code == 422


def test_limit_one(monkeypatch, tmp_path):
    """limit=1 returns exactly one item (the newest)."""
    client, _app, key = make_client(monkeypatch, tmp_path)
    _seed_run(tmp_path, run_id="run-a", created_at="2026-07-22T08:00:00Z")
    _seed_run(tmp_path, run_id="run-b", created_at="2026-07-22T12:00:00Z")
    resp = _list_runs(client, key, limit=1)
    body = resp.json()
    assert body["count"] == 1
    assert body["limit"] == 1
    assert body["items"][0]["run_id"] == "run-b"


# ---------------------------------------------------------------------------
# 4. status filter
# ---------------------------------------------------------------------------

def test_status_filter(monkeypatch, tmp_path):
    """status=completed returns only completed runs."""
    client, _app, key = make_client(monkeypatch, tmp_path)
    _seed_run(tmp_path, run_id="run-c1", status="completed", created_at="2026-07-22T10:00:00Z")
    _seed_run(tmp_path, run_id="run-c2", status="completed", created_at="2026-07-22T11:00:00Z")
    _seed_run(tmp_path, run_id="run-f1", status="failed", created_at="2026-07-22T12:00:00Z", error="boom")
    resp = _list_runs(client, key, status="completed")
    body = resp.json()
    assert body["count"] == 2
    for item in body["items"]:
        assert item["status"] == "completed"
    assert body["filters"]["status"] == "completed"


def test_status_filter_no_match(monkeypatch, tmp_path):
    """status=cancelled with no cancelled runs returns empty list."""
    client, _app, key = make_client(monkeypatch, tmp_path)
    _seed_run(tmp_path, run_id="run-x", status="completed", created_at="2026-07-22T10:00:00Z")
    resp = _list_runs(client, key, status="cancelled")
    body = resp.json()
    assert body["count"] == 0
    assert body["items"] == []


# ---------------------------------------------------------------------------
# 5. executor filter
# ---------------------------------------------------------------------------

def test_executor_filter(monkeypatch, tmp_path):
    """executor=hermes returns only hermes runs."""
    client, _app, key = make_client(monkeypatch, tmp_path)
    _seed_run(
        tmp_path,
        run_id="run-h1",
        selected_executor="hermes",
        requested_executor="hermes",
        status="queued",
        progress=0.0,
        created_at="2026-07-22T10:00:00Z",
    )
    _seed_run(
        tmp_path,
        run_id="run-c1",
        selected_executor="claude-code-cli",
        requested_executor="claude-code-cli",
        status="completed",
        created_at="2026-07-22T11:00:00Z",
    )
    resp = _list_runs(client, key, executor="hermes")
    body = resp.json()
    assert body["count"] == 1
    assert body["items"][0]["selected_executor"] == "hermes"
    assert body["filters"]["executor"] == "hermes"


# ---------------------------------------------------------------------------
# 6. since filter
# ---------------------------------------------------------------------------

def test_since_filter(monkeypatch, tmp_path):
    """since=2026-07-22T10:30:00Z returns only runs at or after that time."""
    client, _app, key = make_client(monkeypatch, tmp_path)
    _seed_run(tmp_path, run_id="run-old", created_at="2026-07-22T08:00:00Z")
    _seed_run(tmp_path, run_id="run-edge", created_at="2026-07-22T10:30:00Z")
    _seed_run(tmp_path, run_id="run-new", created_at="2026-07-22T12:00:00Z")
    resp = _list_runs(client, key, since="2026-07-22T10:30:00Z")
    body = resp.json()
    assert body["count"] == 2
    run_ids = [item["run_id"] for item in body["items"]]
    assert "run-old" not in run_ids
    assert "run-edge" in run_ids
    assert "run-new" in run_ids
    # Filters echo back the normalized timestamp
    assert body["filters"]["since"] == "2026-07-22T10:30:00Z"


def test_since_filter_with_z_suffix(monkeypatch, tmp_path):
    """since with trailing Z is accepted and normalised."""
    client, _app, key = make_client(monkeypatch, tmp_path)
    _seed_run(tmp_path, run_id="run-a", created_at="2026-07-22T08:00:00Z")
    _seed_run(tmp_path, run_id="run-b", created_at="2026-07-22T12:00:00Z")
    resp = _list_runs(client, key, since="2026-07-22T10:00:00Z")
    body = resp.json()
    assert body["count"] == 1
    assert body["items"][0]["run_id"] == "run-b"


def test_since_filter_future_returns_empty(monkeypatch, tmp_path):
    """since in the future returns empty list."""
    client, _app, key = make_client(monkeypatch, tmp_path)
    _seed_run(tmp_path, run_id="run-a", created_at="2026-07-22T10:00:00Z")
    resp = _list_runs(client, key, since="2099-01-01T00:00:00Z")
    body = resp.json()
    assert body["count"] == 0
    assert body["items"] == []


# ---------------------------------------------------------------------------
# 7. empty result (already covered above, explicit case here)
# ---------------------------------------------------------------------------

def test_empty_result(monkeypatch, tmp_path):
    """GET /runs on an empty store returns count=0, items=[]."""
    client, _app, key = make_client(monkeypatch, tmp_path)
    resp = _list_runs(client, key)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0
    assert body["items"] == []
    assert body["limit"] == 20


# ---------------------------------------------------------------------------
# 8. malformed inputs
# ---------------------------------------------------------------------------

def test_invalid_status_returns_400(monkeypatch, tmp_path):
    """An unknown status value returns a deterministic 400."""
    client, _app, key = make_client(monkeypatch, tmp_path)
    resp = _list_runs(client, key, status="bogus")
    assert resp.status_code == 400
    detail = resp.json().get("detail", {})
    assert isinstance(detail, dict)
    assert detail.get("code") == "invalid_status"
    assert "valid_statuses" in detail
    assert "bogus" in detail.get("message", "")


def test_invalid_since_returns_400(monkeypatch, tmp_path):
    """A malformed since timestamp returns a deterministic 400."""
    client, _app, key = make_client(monkeypatch, tmp_path)
    resp = _list_runs(client, key, since="not-a-date")
    assert resp.status_code == 400
    detail = resp.json().get("detail", {})
    assert isinstance(detail, dict)
    assert detail.get("code") == "invalid_since"


def test_invalid_since_garbage_iso(monkeypatch, tmp_path):
    """A partially malformed ISO-8601 returns 400."""
    client, _app, key = make_client(monkeypatch, tmp_path)
    resp = _list_runs(client, key, since="2026-13-45T99:99:99Z")
    assert resp.status_code == 400
    detail = resp.json().get("detail", {})
    assert detail.get("code") == "invalid_since"


def test_limit_above_max_rejected(monkeypatch, tmp_path):
    """limit=101 (above max=100) is rejected by FastAPI (422)."""
    client, _app, key = make_client(monkeypatch, tmp_path)
    resp = _list_runs(client, key, limit=101)
    assert resp.status_code == 422


def test_limit_at_max_accepted(monkeypatch, tmp_path):
    """limit=100 (the max) is accepted."""
    client, _app, key = make_client(monkeypatch, tmp_path)
    for i in range(105):
        _seed_run(
            tmp_path,
            run_id=f"run-max-{i}",
            created_at=f"2026-07-22T10:00:{i % 60:02d}Z",
        )
    resp = _list_runs(client, key, limit=100)
    body = resp.json()
    assert body["count"] == 100
    assert body["limit"] == 100


# ---------------------------------------------------------------------------
# 9. auth required
# ---------------------------------------------------------------------------

def test_auth_required_missing_token(monkeypatch, tmp_path):
    """GET /runs without a bearer token returns 401."""
    client, _app, key = make_client(monkeypatch, tmp_path)
    resp = client.get("/runs")  # no Authorization header
    assert resp.status_code == 401


def test_auth_required_invalid_token(monkeypatch, tmp_path):
    """GET /runs with an invalid bearer token returns 401."""
    client, _app, key = make_client(monkeypatch, tmp_path)
    resp = client.get(
        "/runs",
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 10. no executor / upstream side effects
# ---------------------------------------------------------------------------

def test_no_side_effects_no_dispatch(monkeypatch, tmp_path):
    """GET /runs must not dispatch, poll upstream, or mutate state.

    We seed runs, then GET /runs twice and assert:
      a) both responses are identical (no mutation between reads)
      b) the persisted rows are unchanged across reads
    """
    client, _app, key = make_client(monkeypatch, tmp_path)
    _seed_run(tmp_path, run_id="run-side-1", created_at="2026-07-22T10:00:00Z")
    _seed_run(tmp_path, run_id="run-side-2", created_at="2026-07-22T11:00:00Z")

    import sqlite3
    from dispatcher import db as ddb

    def _count_rows():
        conn = sqlite3.connect(str(ddb.DB_PATH))
        try:
            row = conn.execute("SELECT COUNT(*) FROM executor_runs").fetchone()
            return row[0]
        finally:
            conn.close()

    before = _count_rows()
    resp1 = _list_runs(client, key)
    assert resp1.status_code == 200
    body1 = resp1.json()

    resp2 = _list_runs(client, key)
    assert resp2.status_code == 200
    body2 = resp2.json()

    after = _count_rows()

    # No new rows created, no rows mutated
    assert before == after == 2
    # Both reads return the same envelope
    assert body1 == body2


def test_no_upstream_call_on_list(monkeypatch, tmp_path):
    """GET /runs must not call the Hermes adapter even when the
    executor_runs store has rows. We instrument the adapter registry
    to fail the test if poll() is invoked."""
    from aee.adapters.base import RuntimePollResult
    from aee.core.registry import adapter_registry

    class _TripwireHermes:
        name = "hermes"
        runtime_type = "hermes"
        poll_called = False

        async def submit(self, job):
            from aee.adapters.base import RuntimeSubmitResult
            return RuntimeSubmitResult(external_run_id="tripwire", status="queued")

        async def poll(self, external_run_id):
            _TripwireHermes.poll_called = True
            return RuntimePollResult(
                external_run_id=external_run_id,
                status="completed",
                is_terminal=True,
            )

        async def cancel(self, external_run_id):
            from aee.adapters.base import RuntimeCancelResult
            return RuntimeCancelResult(external_run_id=external_run_id, cancelled=True)

    saved = dict(adapter_registry._adapters)
    adapter_registry._adapters["hermes"] = _TripwireHermes()
    try:
        client, _app, key = make_client(monkeypatch, tmp_path)
        _seed_run(
            tmp_path,
            run_id="run-no-call",
            selected_executor="hermes",
            requested_executor="hermes",
            status="running",
            created_at="2026-07-22T10:00:00Z",
        )
        resp = _list_runs(client, key)
        assert resp.status_code == 200
        assert _TripwireHermes.poll_called is False, (
            "GET /runs must not call adapter.poll()"
        )
    finally:
        adapter_registry._adapters.clear()
        adapter_registry._adapters.update(saved)


# ---------------------------------------------------------------------------
# Combined filters
# ---------------------------------------------------------------------------

def test_combined_status_and_executor_filter(monkeypatch, tmp_path):
    """status=completed + executor=hermes returns only matching runs."""
    client, _app, key = make_client(monkeypatch, tmp_path)
    _seed_run(
        tmp_path,
        run_id="run-h-c",
        selected_executor="hermes",
        requested_executor="hermes",
        status="completed",
        created_at="2026-07-22T10:00:00Z",
    )
    _seed_run(
        tmp_path,
        run_id="run-h-q",
        selected_executor="hermes",
        requested_executor="hermes",
        status="queued",
        progress=0.0,
        created_at="2026-07-22T11:00:00Z",
    )
    _seed_run(
        tmp_path,
        run_id="run-c-c",
        selected_executor="claude-code-cli",
        requested_executor="claude-code-cli",
        status="completed",
        created_at="2026-07-22T12:00:00Z",
    )
    resp = _list_runs(client, key, status="completed", executor="hermes")
    body = resp.json()
    assert body["count"] == 1
    assert body["items"][0]["run_id"] == "run-h-c"
    assert body["filters"]["status"] == "completed"
    assert body["filters"]["executor"] == "hermes"