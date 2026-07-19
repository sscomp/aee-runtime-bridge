"""TASK-20260719-0046 — Executor Routing Evidence tests.

These tests verify that the ``POST /runs`` endpoint surfaces the
executor routing decision as observable evidence in the response's
``routing`` block (requirement §4 of the brief). They complement
``tests/test_executor_router.py`` (which covers the router pure
function and the 503 path) by asserting the *positive* path: when
the caller passes ``metadata.executor`` the response carries both
``requested_executor`` and ``selected_executor`` so the caller can
verify that an explicit request was honored (not silently
overridden).

Coverage:
    1. ``executor=claude_code`` -> response.routing.executor has
       ``selected_executor='claude_code'``.
    2. ``executor=hermes`` (explicit) -> response.routing.executor
       has ``selected_executor='hermes'`` and
       ``selection_source='explicit_hermes'``.
    3. No ``metadata`` (legacy default) ->
       response.routing.executor is None (observable "no override"
       sentinel). The default Hermes path is preserved (§3 of the
       brief).
    4. Unsupported executor value (e.g. ``gemini``) -> 400 with
       ``detail.code='unknown_executor'`` (§2 of the brief). The
       response does NOT silently fall back (§5 of the brief).

These tests use a stub Hermes adapter to avoid hitting any real
upstream. They follow the fixture pattern established in
``tests/test_executor_router.py::test_app_create_run_returns_503_executor_unavailable_when_claude_missing``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _setup_test_db(monkeypatch, tmp_path: Path):
    """Point the dispatcher at a temp DB / log dir so the test
    never touches production state. Returns the tmp_path so the
    caller can reference it.
    """
    from dispatcher import db as dispatcher_db
    from dispatcher import manager as dispatcher_manager

    monkeypatch.setattr(dispatcher_db, "DB_DIR", tmp_path)
    monkeypatch.setattr(dispatcher_db, "DB_PATH", tmp_path / "dispatcher.db")
    monkeypatch.setattr(dispatcher_manager, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(dispatcher_manager, "REPORTS_DIR", tmp_path / "reports")
    # Force a fresh DB connection so we don't reuse a cached
    # connection from a prior test that pointed elsewhere.
    monkeypatch.setattr(dispatcher_db, "_initialized", False)
    if hasattr(dispatcher_db._local, "conn"):
        dispatcher_db._local.conn = None
    return tmp_path


def _make_client(monkeypatch, test_key: str):
    """Build a FastAPI TestClient with a stub Hermes adapter so the
    endpoint can complete the submit path without calling a real
    upstream. Returns ``(client, app_module)``.
    """
    from fastapi.testclient import TestClient

    import app as app_module
    from aee.adapters.base import (
        RuntimePollResult,
        RuntimeSubmitResult,
    )
    from aee.core.registry import adapter_registry

    # Set the bridge key so require_auth accepts the request.
    monkeypatch.setenv("BRIDGE_API_KEY", test_key)
    try:
        app_module.CLIENT_BRIDGE_KEYS = app_module._collect_client_keys()
    except Exception:
        app_module.CLIENT_BRIDGE_KEYS = {test_key}

    # Register a stub "hermes" adapter if not already present so
    # the default path can complete. We do NOT touch the
    # ``claude_code`` entry — the explicit-routing tests below
    # rely on the registry's real claude_code adapter (or a stub
    # we register) being present.
    class _StubAdapter:
        name = "hermes"
        runtime_type = "hermes"

        async def submit(self, job):
            return RuntimeSubmitResult(
                external_run_id="stub-run-id",
                status="started",
            )

        async def poll(self, external_run_id):
            return RuntimePollResult(
                external_run_id=external_run_id,
                status="completed",
                is_terminal=True,
                output="stub",
            )

        async def cancel(self, external_run_id):
            from aee.adapters.base import RuntimeCancelResult
            return RuntimeCancelResult(
                external_run_id=external_run_id, cancelled=True
            )

    # Save and restore the registry around this test.
    saved = dict(adapter_registry._adapters)
    adapter_registry._adapters["hermes"] = _StubAdapter()
    try:
        client = TestClient(app_module.app)
        yield client, app_module
    finally:
        adapter_registry._adapters.clear()
        adapter_registry._adapters.update(saved)


@pytest.fixture
def client_factory(monkeypatch, tmp_path):
    """Fixture returning a function that builds a TestClient."""
    _setup_test_db(monkeypatch, tmp_path)
    test_key = "m3-fixture-test-key"
    gen = _make_client(monkeypatch, test_key)
    client, app_module = next(gen)
    yield client, app_module, test_key
    # Drain the generator's finally block.
    try:
        next(gen)
    except StopIteration:
        pass


def _post(client, test_key, body):
    return client.post(
        "/runs",
        json=body,
        headers={"Authorization": f"Bearer {test_key}"},
    )


# --- Tests ---------------------------------------------------------------


def test_executor_claude_code_surfaces_routing_evidence(client_factory):
    """§4: when ``metadata.executor='claude_code'`` and the adapter
    is available, the response's ``routing.executor`` block must
    carry ``selected_executor='claude_code'`` and
    ``requested_executor='claude_code'``. This is the observable
    evidence that the explicit request was honored (not silently
    overridden).
    """
    client, app_module, test_key = client_factory
    # Register a stub claude_code adapter so the router finds it
    # in the registry without needing the real Claude Code runner.
    from aee.adapters.base import RuntimePollResult, RuntimeSubmitResult
    from aee.core.registry import adapter_registry

    class _StubClaude:
        name = "claude_code"
        runtime_type = "claude_code"

        async def submit(self, job):
            return RuntimeSubmitResult(
                external_run_id="stub-claude-run", status="started"
            )

        async def poll(self, external_run_id):
            return RuntimePollResult(
                external_run_id=external_run_id,
                status="completed",
                is_terminal=True,
                output="stub",
            )

        async def cancel(self, external_run_id):
            from aee.adapters.base import RuntimeCancelResult
            return RuntimeCancelResult(
                external_run_id=external_run_id, cancelled=True
            )

    saved = dict(adapter_registry._adapters)
    adapter_registry._adapters["claude_code"] = _StubClaude()
    try:
        body = {
            "input": "test",
            "mode": "normal",
            "metadata": {
                "executor": "claude_code",
                "repo_path": str(Path("/home/ubuntu/Abacus")),
            },
        }
        resp = _post(client, test_key, body)
        assert resp.status_code == 200, (
            f"expected 200, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert "routing" in data, "response missing 'routing' block"
        executor_block = data["routing"].get("executor")
        assert executor_block is not None, (
            "routing.executor is None — explicit executor request was "
            "not surfaced as observable evidence (TASK-20260719-0046 §4)"
        )
        assert executor_block.get("selected_executor") == "claude_code", (
            f"selected_executor != 'claude_code': {executor_block!r}"
        )
        assert executor_block.get("requested_executor") == "claude_code", (
            f"requested_executor != 'claude_code': {executor_block!r}"
        )
        assert executor_block.get("fallback_applied") is False, (
            f"fallback_applied should be False: {executor_block!r}"
        )
    finally:
        adapter_registry._adapters.clear()
        adapter_registry._adapters.update(saved)


def test_executor_hermes_explicit_surfaces_routing_evidence(client_factory):
    """§4: when ``metadata.executor='hermes'`` (explicit), the
    response's ``routing.executor`` carries
    ``selected_executor='hermes'`` and
    ``selection_source='explicit_hermes'``.
    """
    client, _, test_key = client_factory
    body = {
        "input": "test",
        "mode": "normal",
        "metadata": {"executor": "hermes"},
    }
    resp = _post(client, test_key, body)
    assert resp.status_code == 200, (
        f"expected 200, got {resp.status_code}: {resp.text}"
    )
    data = resp.json()
    executor_block = data["routing"].get("executor")
    assert executor_block is not None, (
        "routing.executor is None for explicit executor='hermes'"
    )
    assert executor_block.get("selected_executor") == "hermes"
    assert executor_block.get("requested_executor") == "hermes"
    assert executor_block.get("selection_source") == "explicit_hermes"


def test_no_metadata_surfaces_null_executor_in_routing(client_factory):
    """§3: when ``metadata`` is omitted entirely (legacy default),
    the default Hermes path is preserved AND the response is
    observable: ``routing.executor`` is ``None`` so the caller can
    distinguish "no executor request" from "explicit Hermes".
    """
    client, _, test_key = client_factory
    body = {
        "input": "test",
        "mode": "normal",
        # No metadata key at all.
    }
    resp = _post(client, test_key, body)
    assert resp.status_code == 200, (
        f"expected 200, got {resp.status_code}: {resp.text}"
    )
    data = resp.json()
    assert "routing" in data
    # executor block must be None (no override applied).
    assert data["routing"].get("executor") is None, (
        f"routing.executor should be None for legacy default path, "
        f"got {data['routing'].get('executor')!r}"
    )


def test_unsupported_executor_rejected_with_stable_code(client_factory):
    """§2 + §5: an unsupported executor value (e.g. ``gemini``)
    is rejected with HTTP 400 and ``detail.code='unknown_executor'``.
    The runtime does NOT silently fall back to Hermes (§5).
    """
    client, _, test_key = client_factory
    body = {
        "input": "test",
        "mode": "normal",
        "metadata": {"executor": "gemini"},
    }
    resp = _post(client, test_key, body)
    assert resp.status_code == 400, (
        f"expected 400 for unknown executor, got {resp.status_code}: {resp.text}"
    )
    detail = resp.json().get("detail") or {}
    assert detail.get("code") == "unknown_executor", (
        f"detail.code should be 'unknown_executor', got {detail!r}"
    )