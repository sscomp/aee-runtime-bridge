"""Tests for ``aee.runtimes.executor_router``.

The router is a pure function. Tests cover the 8 cases listed in
TASK-M2 §Test Requirements.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aee.runtimes import executor_router as er  # noqa: E402


# --- Constants for tests ---------------------------------------------

AVAILABLE = ["hermes", "claude_code"]


# --- select_executor -------------------------------------------------


def test_metadata_claude_code_routes_to_claude_code():
    d = er.select_executor(
        {"executor": "claude_code"},
        available_adapters=AVAILABLE,
    )
    assert d.selected_executor == "claude_code"
    assert d.requested_executor == "claude_code"
    assert d.selection_source == "metadata"
    assert d.fallback_applied is False


def test_metadata_hermes_routes_to_hermes():
    d = er.select_executor(
        {"executor": "hermes"},
        available_adapters=AVAILABLE,
    )
    assert d.selected_executor == "hermes"
    assert d.selection_source == "explicit_hermes"


def test_metadata_missing_uses_default_hermes():
    d = er.select_executor(None, available_adapters=AVAILABLE)
    assert d.selected_executor == "hermes"
    assert d.selection_source == "default"
    assert d.requested_executor is None


def test_unknown_executor_rejected():
    with pytest.raises(er.ExecutorValidationError) as excinfo:
        er.select_executor(
            {"executor": "gemini"},
            available_adapters=AVAILABLE,
        )
    assert excinfo.value.code == "unknown_executor"


def test_no_silent_fallback_when_claude_code_unavailable():
    with pytest.raises(er.ExecutorUnavailable):
        er.select_executor(
            {"executor": "claude_code"},
            available_adapters=["hermes"],  # claude_code not present
        )


# --- validate_metadata -----------------------------------------------


def test_allow_commit_without_human_approval_rejected():
    with pytest.raises(er.ExecutorValidationError) as excinfo:
        er.validate_metadata({
            "executor": "claude_code",
            "allow_commit": True,
            "human_approved": False,
        })
    assert excinfo.value.code == "allow_commit_requires_human_approved"


def test_path_outside_allowlist_rejected(tmp_path):
    with pytest.raises(er.ExecutorValidationError) as excinfo:
        er.validate_metadata({
            "executor": "claude_code",
            "repo_path": str(tmp_path),
        })
    assert excinfo.value.code == "repo_path_outside_allowlist"


def test_valid_repo_path_accepted(tmp_path):
    # tmp_path is by default /tmp/pytest-... — not in allow-list. Use
    # an Abacus subdir that exists.
    abacus = Path("/home/ubuntu/Abacus")
    if not abacus.exists():
        pytest.skip("/home/ubuntu/Abacus not present")
    er.validate_metadata({
        "executor": "claude_code",
        "repo_path": str(abacus),
    })


def test_validate_metadata_none_is_noop():
    er.validate_metadata(None)


def test_validate_metadata_required_artifact_absolute_rejected():
    with pytest.raises(er.ExecutorValidationError) as excinfo:
        er.validate_metadata({
            "executor": "claude_code",
            "repo_path": str(Path("/home/ubuntu/Abacus")),
            "required_artifacts": ["/etc/passwd"],
        })
    assert excinfo.value.code == "required_artifact_absolute"


def test_validate_metadata_required_artifact_traversal_rejected():
    with pytest.raises(er.ExecutorValidationError) as excinfo:
        er.validate_metadata({
            "executor": "claude_code",
            "repo_path": str(Path("/home/ubuntu/Abacus")),
            "required_artifacts": ["../escape.txt"],
        })
    assert excinfo.value.code == "required_artifact_traversal"


def test_validate_metadata_test_command_rejected():
    with pytest.raises(er.ExecutorValidationError) as excinfo:
        er.validate_metadata({
            "executor": "claude_code",
            "repo_path": str(Path("/home/ubuntu/Abacus")),
            "test_command": "pytest; rm -rf /",
        })
    assert excinfo.value.code == "test_command_shell_metachar"


# --- TASK-M3 FIX-8: registry fail-closed regression ------------------
#
# The registry must not silently fall back to a legacy
# ``ClaudeCodeRuntimeAdapter`` shim. The only registered adapter
# for ``claude_code`` is the verified
# ``ClaudeCodeExecutorAdapter``. If it is not available, the
# router must raise ``ExecutorUnavailable`` and the API layer
# must surface a 503 ``executor_unavailable`` error code (no
# legacy execution).


def test_registry_does_not_register_claude_code_when_adapter_missing(monkeypatch):
    """If ``ClaudeCodeExecutorAdapter`` cannot be imported, the
    registry must NOT register ``claude_code`` at all. The legacy
    shim fallback is forbidden.

    We simulate "adapter missing" two ways and assert both:

    1. The internal ``_register_aee7_defaults`` raises
       ``ImportError`` (propagated, not silently swallowed) when
       the verified adapter import fails.
    2. After the failure, ``claude_code`` is NOT in
       ``adapter_registry.names()``.
    """
    import sys
    from aee.core import registry as reg

    # Save and restore the real registry state.
    saved = dict(reg.adapter_registry._adapters)
    # The "fail" is forced by patching the module's source to
    # raise. We rewrite ``aee.adapters.claude_code_executor`` to
    # point at a tiny stub that raises on attribute access. The
    # import path stays the same so the import statement in
    # ``_register_aee7_defaults`` succeeds; the failure happens
    # at the *class lookup* (``ClaudeCodeExecutorAdapter``).
    saved_module = sys.modules.get("aee.adapters.claude_code_executor")
    try:
        import types
        broken = types.ModuleType("aee.adapters.claude_code_executor")

        def _boom(*args, **kwargs):
            raise ImportError(
                "m3-fixture: simulated import failure for "
                "ClaudeCodeExecutorAdapter"
            )

        broken.ClaudeCodeExecutorAdapter = property(
            lambda self: _boom()
        )  # type: ignore[attr-defined]
        # Make ``from aee.adapters.claude_code_executor import
        # ClaudeCodeExecutorAdapter`` raise at the attribute lookup
        # step. We do this by replacing the module entry with a
        # module whose ``__getattr__`` raises for the target name.
        class _BrokenModule(types.ModuleType):
            def __getattr__(self, name):
                if name == "ClaudeCodeExecutorAdapter":
                    raise ImportError(
                        "m3-fixture: simulated import failure for "
                        "ClaudeCodeExecutorAdapter"
                    )
                raise AttributeError(name)

        broken = _BrokenModule("aee.adapters.claude_code_executor")
        sys.modules["aee.adapters.claude_code_executor"] = broken

        # Reset registry state for this test.
        reg.adapter_registry._adapters.clear()
        reg.adapter_registry._adapters.update(saved)
        # Make sure ``claude_code`` is not present before the call.
        reg.adapter_registry._adapters.pop("claude_code", None)

        # Call the internal function directly; it must raise
        # ImportError (the verified adapter is unavailable) and
        # NOT fall back to a legacy shim.
        with pytest.raises(ImportError) as excinfo:
            reg._register_aee7_defaults()
        # The "claude_code" name must NOT be registered.
        assert "claude_code" not in reg.adapter_registry.names(), (
            f"registry contains 'claude_code' despite import failure: "
            f"{reg.adapter_registry.names()}"
        )
        # Confirm we got the simulated error.
        assert "m3-fixture" in str(excinfo.value)
    finally:
        # Restore the real module.
        if saved_module is not None:
            sys.modules["aee.adapters.claude_code_executor"] = saved_module
        else:
            sys.modules.pop("aee.adapters.claude_code_executor", None)
        reg.adapter_registry._adapters.clear()
        reg.adapter_registry._adapters.update(saved)


def test_router_raises_unavailable_when_claude_code_not_registered():
    """When ``claude_code`` is not in the registry, the router
    must raise ``ExecutorUnavailable`` for an explicit request
    (no silent Hermes fallback)."""
    with pytest.raises(er.ExecutorUnavailable) as excinfo:
        er.select_executor(
            {"executor": "claude_code"},
            available_adapters=["hermes"],  # claude_code absent
        )
    # The error message should mention the requested executor.
    assert "claude_code" in str(excinfo.value)


def test_app_create_run_returns_503_executor_unavailable_when_claude_missing(
    monkeypatch,
):
    """End-to-end: the ``POST /runs`` endpoint must return a 503
    ``executor_unavailable`` error when the caller asks for
    ``metadata.executor='claude_code'`` and the verified adapter
    is not in the registry. No legacy execution. No silent
    Hermes fallback.
    """
    from aee.core import registry as reg
    from dispatcher import db as dispatcher_db
    from dispatcher import manager as dispatcher_manager
    import tempfile

    # Set a known bridge key so ``require_auth`` accepts the request.
    test_key = "m3-fixture-test-key"
    monkeypatch.setenv("BRIDGE_API_KEY", test_key)

    # Use a temp dir for the dispatcher DB so this test does not
    # touch the production database.
    tmp = Path(tempfile.mkdtemp(prefix="m3-fixture-503-"))
    saved_db_dir = dispatcher_db.DB_DIR
    saved_db_path = dispatcher_db.DB_PATH
    saved_logs_dir = dispatcher_manager.LOGS_DIR
    saved_reports_dir = dispatcher_manager.REPORTS_DIR
    # Force a fresh DB connection on next get_conn() so the test
    # is not contaminated by a previous test's cached state.
    saved_initialized = dispatcher_db._initialized
    saved_local_conn = getattr(dispatcher_db._local, "conn", None)
    dispatcher_db._initialized = False
    dispatcher_db._local.conn = None
    try:
        # Point the dispatcher DB at a temp dir BEFORE we import
        # the app, so the app's startup uses the temp DB.
        dispatcher_db.DB_DIR = tmp
        dispatcher_db.DB_PATH = tmp / "dispatcher.db"
        # Reset module-level paths in the manager (some tests
        # mutate them, which would otherwise leak into this one).
        dispatcher_manager.LOGS_DIR = tmp / "logs"
        dispatcher_manager.REPORTS_DIR = tmp / "reports"

        # Build a TestClient for the main app (app.py owns
        # ``/runs``).
        try:
            from fastapi.testclient import TestClient
            from app import app  # type: ignore[import-not-found]
        except Exception as exc:
            pytest.skip(
                f"FastAPI TestClient / app import not available: {exc}"
            )

        # Refresh the auth key map so our test key is accepted.
        import app as _app_module
        try:
            _app_module.CLIENT_BRIDGE_KEYS = _app_module._collect_client_keys()  # type: ignore[attr-defined]
        except Exception:
            _app_module.CLIENT_BRIDGE_KEYS = {test_key}

        # Save and restore state.
        saved = dict(reg.adapter_registry._adapters)
        try:
            # 1. Remove claude_code from the registry AFTER app
            #    startup. ``bootstrap_defaults`` re-registers it
            #    on app import, so we unregister it now to
            #    simulate "verified adapter unavailable".
            reg.adapter_registry._adapters.pop("claude_code", None)
            assert "claude_code" not in reg.adapter_registry.names()

            client = TestClient(app)
            # Build a valid-looking request body.
            body = {
                "input": "test",
                "mode": "normal",
                "metadata": {
                    "executor": "claude_code",
                    "repo_path": str(Path("/home/ubuntu/Abacus")),
                },
            }
            resp = client.post(
                "/runs",
                json=body,
                headers={"Authorization": f"Bearer {test_key}"},
            )
            assert resp.status_code == 503, (
                f"expected 503, got {resp.status_code}: {resp.text}"
            )
            # The error body must carry the stable code
            # ``executor_unavailable``.
            detail = resp.json().get("detail") or {}
            assert detail.get("code") == "executor_unavailable", (
                f"expected code=executor_unavailable, got {detail}"
            )
        finally:
            # Restore the registry.
            reg.adapter_registry._adapters.clear()
            reg.adapter_registry._adapters.update(saved)
    finally:
        dispatcher_db.DB_DIR = saved_db_dir
        dispatcher_db.DB_PATH = saved_db_path
        dispatcher_manager.LOGS_DIR = saved_logs_dir
        dispatcher_manager.REPORTS_DIR = saved_reports_dir
        dispatcher_db._initialized = saved_initialized
        dispatcher_db._local.conn = saved_local_conn
