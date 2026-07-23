"""Executor-neutral control-plane stop semantics (work-order §3.2 / §E).

Validates that ``POST /runs/{run_id}/stop`` and its ``/cancel`` alias
route through the persisted ``selected_executor`` instead of being
hard-coded to Hermes, and that ``GET /runs``,
``GET /runs/{run_id}`` and ``GET /runs/{run_id}/summary`` remain
pure-reads (the work-order explicitly forbids query-time run
creation).

Uses a temp dispatcher DB (see ``_executor_test_helpers.setup_temp_db``)
so the live supervised bridge is not touched. Stubs both the Hermes
and the Claude Code adapter to record which adapter received the
``cancel(run_id)`` call and to surface deterministic
``RuntimeCancelResult`` values.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# Stub adapters — record cancel invocations
# ---------------------------------------------------------------------------


class _RecordingAdapter:
    """Minimal adapter that records cancel(run_id) calls.

    The bridge resolves adapters by their registry key
    (``"hermes"`` or ``"claude-code-cli"``). Each call to
    ``cancel(external_run_id)`` appends to ``self.cancel_calls``;
    the canned ``cancel_result`` is returned verbatim.
    """

    def __init__(
        self,
        name: str,
        runtime_type: str,
        cancel_result: Any,
    ) -> None:
        self.name = name
        self.runtime_type = runtime_type
        self._cancel_result = cancel_result
        self.cancel_calls: List[str] = []

    async def submit(self, job):  # pragma: no cover - unused here
        from aee.adapters.base import RuntimeSubmitResult
        return RuntimeSubmitResult(external_run_id="unused", status="queued")

    async def poll(self, external_run_id):  # pragma: no cover - unused
        from aee.adapters.base import RuntimePollResult
        return RuntimePollResult(
            external_run_id=external_run_id, status="completed", is_terminal=True
        )

    async def cancel(self, external_run_id: str):
        self.cancel_calls.append(external_run_id)
        return self._cancel_result


def _install_stubs(monkeypatch, *, hermes_cancel, claude_cancel):
    """Install recording stub adapters for both executors.

    Returns ``(hermes_stub, claude_stub)`` so the test can assert
    which adapter received the cancel call.
    """
    from aee.core.registry import adapter_registry

    hermes_stub = _RecordingAdapter("hermes", "hermes", hermes_cancel)
    claude_stub = _RecordingAdapter("claude-code-cli", "claude-code-cli", claude_cancel)
    saved = dict(adapter_registry._adapters)
    adapter_registry._adapters["hermes"] = hermes_stub
    adapter_registry._adapters["claude-code-cli"] = claude_stub
    monkeypatch.setattr(adapter_registry, "_adapters", adapter_registry._adapters)

    def _restore():
        adapter_registry._adapters.clear()
        adapter_registry._adapters.update(saved)

    monkeypatch.setattr(adapter_registry, "_adapters", adapter_registry._adapters)
    return hermes_stub, claude_stub, _restore


def _seed_executor_run(
    tmp_path: Path,
    *,
    run_id: str,
    selected_executor: str,
    status: str = "running",
) -> None:
    """Insert a row directly into the temp ``executor_runs`` table.

    Uses the bridge's own ``upsert_run`` so the row matches the
    production schema 1:1 (including the P1 observability columns
    that ``get_run`` reads back).
    """
    from dispatcher.db import get_conn
    from dispatcher.executor_runs import upsert_run

    conn = get_conn()
    upsert_run(
        conn,
        run_id=run_id,
        requested_executor=selected_executor,
        selected_executor=selected_executor,
        status=status,
        stdout_summary="seeded",
        artifact_paths=[],
        routing={"selected_executor": selected_executor},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch, tmp_path):
    from tests._executor_test_helpers import setup_temp_db

    setup_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv("BRIDGE_API_KEY", "ctrl-plane-key")
    from fastapi.testclient import TestClient
    import app as app_module

    app_module.CLIENT_BRIDGE_KEYS = {"ctrl-plane-key"}
    return TestClient(app_module.app), tmp_path


def test_stop_routes_to_hermes_for_hermes_run(monkeypatch, env):
    """Persisted selected_executor=hermes -> Hermes adapter.cancel is called."""
    client, tmp_path = env
    from aee.adapters.base import RuntimeCancelResult
    hermes_stub, claude_stub, _restore = _install_stubs(
        monkeypatch,
        hermes_cancel=RuntimeCancelResult(
            external_run_id="h-1", cancelled=True, reason="stopped"
        ),
        claude_cancel=RuntimeCancelResult(
            external_run_id="c-1", cancelled=True, reason="stopped"
        ),
    )
    _seed_executor_run(tmp_path, run_id="h-run-1", selected_executor="hermes", status="running")
    try:
        resp = client.post(
            "/runs/h-run-1/stop",
            headers={"Authorization": "Bearer ctrl-plane-key"},
        )
        assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["run_id"] == "h-run-1"
        assert body["cancelled"] is True
        assert body["selected_executor"] == "hermes"
        assert hermes_stub.cancel_calls == ["h-run-1"]
        assert claude_stub.cancel_calls == [], "claude adapter must NOT be called"
    finally:
        _restore()


def test_stop_routes_to_claude_code_for_claude_run(monkeypatch, env):
    """Persisted selected_executor=claude-code-cli -> Claude Code adapter.cancel is called."""
    client, tmp_path = env
    from aee.adapters.base import RuntimeCancelResult
    hermes_stub, claude_stub, _restore = _install_stubs(
        monkeypatch,
        hermes_cancel=RuntimeCancelResult(
            external_run_id="h-1", cancelled=True, reason="stopped"
        ),
        claude_cancel=RuntimeCancelResult(
            external_run_id="c-1", cancelled=False, reason="process exited"
        ),
    )
    _seed_executor_run(
        tmp_path, run_id="c-run-1",
        selected_executor="claude-code-cli", status="running"
    )
    try:
        resp = client.post(
            "/runs/c-run-1/stop",
            headers={"Authorization": "Bearer ctrl-plane-key"},
        )
        assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["run_id"] == "c-run-1"
        assert body["selected_executor"] == "claude-code-cli"
        assert claude_stub.cancel_calls == ["c-run-1"]
        assert hermes_stub.cancel_calls == [], "hermes adapter must NOT be called"
    finally:
        _restore()


def test_cancel_alias_calls_same_core(monkeypatch, env):
    """POST /runs/{id}/cancel and /stop both invoke the same executor-neutral core."""
    client, tmp_path = env
    from aee.adapters.base import RuntimeCancelResult
    hermes_stub, claude_stub, _restore = _install_stubs(
        monkeypatch,
        hermes_cancel=RuntimeCancelResult(
            external_run_id="h-2", cancelled=True, reason="ok"
        ),
        claude_cancel=RuntimeCancelResult(
            external_run_id="c-2", cancelled=True, reason="ok"
        ),
    )
    _seed_executor_run(
        tmp_path, run_id="claude-alias",
        selected_executor="claude-code-cli", status="running"
    )
    try:
        resp = client.post(
            "/runs/claude-alias/cancel",
            headers={"Authorization": "Bearer ctrl-plane-key"},
        )
        assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
        assert resp.json()["selected_executor"] == "claude-code-cli"
        assert claude_stub.cancel_calls == ["claude-alias"]
        assert hermes_stub.cancel_calls == []
    finally:
        _restore()


def test_stop_unknown_run_returns_404(monkeypatch, env):
    """Unknown run_id -> deterministic 404 (no adapter call)."""
    client, tmp_path = env
    from aee.adapters.base import RuntimeCancelResult
    hermes_stub, claude_stub, _restore = _install_stubs(
        monkeypatch,
        hermes_cancel=RuntimeCancelResult(external_run_id="x", cancelled=True, reason="x"),
        claude_cancel=RuntimeCancelResult(external_run_id="x", cancelled=True, reason="x"),
    )
    try:
        resp = client.post(
            "/runs/does-not-exist-xyz/stop",
            headers={"Authorization": "Bearer ctrl-plane-key"},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "unknown_run_id"
        assert hermes_stub.cancel_calls == []
        assert claude_stub.cancel_calls == []
    finally:
        _restore()


def test_stop_already_terminal_run_is_noop(monkeypatch, env):
    """Terminal run_id -> 200 + cancelled=False, no upstream adapter call."""
    client, tmp_path = env
    from aee.adapters.base import RuntimeCancelResult
    hermes_stub, claude_stub, _restore = _install_stubs(
        monkeypatch,
        hermes_cancel=RuntimeCancelResult(external_run_id="x", cancelled=True, reason="x"),
        claude_cancel=RuntimeCancelResult(external_run_id="x", cancelled=True, reason="x"),
    )
    _seed_executor_run(
        tmp_path, run_id="done-run",
        selected_executor="claude-code-cli", status="completed"
    )
    try:
        resp = client.post(
            "/runs/done-run/stop",
            headers={"Authorization": "Bearer ctrl-plane-key"},
        )
        assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["cancelled"] is False
        assert body["already_terminal"] is True
        assert body["status"] == "completed"
        assert hermes_stub.cancel_calls == []
        assert claude_stub.cancel_calls == []
    finally:
        _restore()


def test_stop_malformed_run_id_returns_400(monkeypatch, env):
    """Malformed run_id (with slashes / spaces) -> deterministic 400, no upstream call."""
    client, tmp_path = env
    from aee.adapters.base import RuntimeCancelResult
    hermes_stub, claude_stub, _restore = _install_stubs(
        monkeypatch,
        hermes_cancel=RuntimeCancelResult(external_run_id="x", cancelled=True, reason="x"),
        claude_cancel=RuntimeCancelResult(external_run_id="x", cancelled=True, reason="x"),
    )
    try:
        resp = client.post(
            "/runs/has%20space/stop",  # %20 is space; malformed
            headers={"Authorization": "Bearer ctrl-plane-key"},
        )
        # FastAPI's path param will accept it but our validator catches
        # whitespace via _malformed_run_id; check both 400 and 422
        # (framework may reject before the handler).
        assert resp.status_code in (400, 422)
        assert hermes_stub.cancel_calls == []
        assert claude_stub.cancel_calls == []
    finally:
        _restore()


def test_get_run_does_not_create_run(monkeypatch, env):
    """GET /runs/{id} is pure-read: it must not insert a new executor_runs row."""
    client, tmp_path = env
    from dispatcher.db import get_conn
    from dispatcher.executor_runs import upsert_run

    conn = get_conn()
    upsert_run(
        conn,
        run_id="pristine",
        requested_executor="hermes",
        selected_executor="hermes",
        status="completed",
        stdout_summary="ok",
    )

    db_path = tmp_path / "dispatcher.db"
    before = sqlite3.connect(str(db_path)).execute(
        "SELECT COUNT(*) FROM executor_runs"
    ).fetchone()[0]
    resp = client.get(
        "/runs/pristine",
        headers={"Authorization": "Bearer ctrl-plane-key"},
    )
    assert resp.status_code == 200
    after = sqlite3.connect(str(db_path)).execute(
        "SELECT COUNT(*) FROM executor_runs"
    ).fetchone()[0]
    assert before == after, (
        f"GET /runs/{{id}} must be a pure read; "
        f"row count changed from {before} to {after}"
    )


def test_get_run_summary_does_not_create_run(monkeypatch, env):
    """GET /runs/{id}/summary is pure-read: it must not insert a new executor_runs row."""
    client, tmp_path = env
    from dispatcher.db import get_conn
    from dispatcher.executor_runs import upsert_run

    conn = get_conn()
    upsert_run(
        conn,
        run_id="pristine-sum",
        requested_executor="hermes",
        selected_executor="hermes",
        status="completed",
        stdout_summary="ok",
    )

    db_path = tmp_path / "dispatcher.db"
    before = sqlite3.connect(str(db_path)).execute(
        "SELECT COUNT(*) FROM executor_runs"
    ).fetchone()[0]
    resp = client.get(
        "/runs/pristine-sum/summary",
        headers={"Authorization": "Bearer ctrl-plane-key"},
    )
    assert resp.status_code == 200
    after = sqlite3.connect(str(db_path)).execute(
        "SELECT COUNT(*) FROM executor_runs"
    ).fetchone()[0]
    assert before == after, (
        f"GET /runs/{{id}}/summary must be a pure read; "
        f"row count changed from {before} to {after}"
    )


def test_list_runs_does_not_create_run(monkeypatch, env):
    """GET /runs is pure-read: it must not insert a new executor_runs row."""
    client, tmp_path = env
    from dispatcher.db import get_conn
    from dispatcher.executor_runs import upsert_run

    conn = get_conn()
    upsert_run(
        conn,
        run_id="pristine-list",
        requested_executor="hermes",
        selected_executor="hermes",
        status="completed",
    )

    db_path = tmp_path / "dispatcher.db"
    before = sqlite3.connect(str(db_path)).execute(
        "SELECT COUNT(*) FROM executor_runs"
    ).fetchone()[0]
    resp = client.get(
        "/runs?limit=5",
        headers={"Authorization": "Bearer ctrl-plane-key"},
    )
    assert resp.status_code == 200
    after = sqlite3.connect(str(db_path)).execute(
        "SELECT COUNT(*) FROM executor_runs"
    ).fetchone()[0]
    assert before == after, (
        f"GET /runs must be a pure read; "
        f"row count changed from {before} to {after}"
    )
