"""Focused tests for WO-FIX-TELEGRAM-RESULT-SYNC.

Reproduces and locks the fix for the mismatch where Hermes stdout
reports a successful Telegram send (with message_id) but the
structured ``telegram_result`` returned by GET /runs/{run_id} has
``sent: False`` / ``success: False``.

Root cause: the Hermes async submit path persists a placeholder
``telegram_result = {"success": False, "skipped": "hermes is async;
per-run telegram not sent on submit"}`` into the executor_runs row.
That dict is truthy, so the previous merge guard
``if not merged.get("telegram_result")`` never let the task-side
``telegram_result`` (built from ``task_outputs.notification_json``,
carrying ``sent: True`` + ``message_id``) override it. A second
variant of the same bug exists when reconciliation writes
``stdout_summary``: ``_executor_evidence_is_empty`` returns False,
the merge short-circuits, and the placeholder survives.

Fix: ``_telegram_result_is_confirmed`` is the new merge gate. The
task-side ``telegram_result`` overrides the executor-side value
when the executor-side does NOT represent a confirmed delivery
(success/sent True + non-None message_id).

Covers:
  1. Successful Telegram result propagation (empty envelope).
  2. Successful Telegram result propagation (non-empty envelope
     after reconciliation — the early-return path).
  3. Failed Telegram result propagation (task-side sent=False).
  4. Lifecycle/task merge behavior: already-confirmed executor-side
     is preserved (no override).
  5. No task_id -> no merge.
  6. Task-side has no telegram_result -> no override.
  7. Unit test for ``_telegram_result_is_confirmed``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# 7. Unit test for _telegram_result_is_confirmed
# ---------------------------------------------------------------------------

def test_telegram_result_is_confirmed_unit():
    """_telegram_result_is_confirmed gates on success/sent + message_id."""
    import app

    # Confirmed: success=True + message_id
    assert app._telegram_result_is_confirmed(
        {"success": True, "message_id": 123}
    ) is True
    # Confirmed: sent=True + message_id (task-side shape)
    assert app._telegram_result_is_confirmed(
        {"sent": True, "message_id": 456}
    ) is True
    # Not confirmed: success=True but message_id is None
    assert app._telegram_result_is_confirmed(
        {"success": True, "message_id": None}
    ) is False
    # Not confirmed: success=False (the Hermes placeholder)
    assert app._telegram_result_is_confirmed(
        {"success": False, "skipped": "hermes is async"}
    ) is False
    # Not confirmed: sent=False
    assert app._telegram_result_is_confirmed(
        {"sent": False, "last_error": "x"}
    ) is False
    # Not confirmed: not a dict
    assert app._telegram_result_is_confirmed(None) is False
    assert app._telegram_result_is_confirmed("{}") is False
    assert app._telegram_result_is_confirmed([]) is False
    # Not confirmed: empty dict
    assert app._telegram_result_is_confirmed({}) is False


# ---------------------------------------------------------------------------
# Helper: stub _collect_task_evidence
# ---------------------------------------------------------------------------

def _stub_collect(monkeypatch, evidence_or_factory):
    """Replace app._collect_task_evidence with a stub."""
    import app

    if callable(evidence_or_factory):
        monkeypatch.setattr(app, "_collect_task_evidence", evidence_or_factory)
    else:
        monkeypatch.setattr(
            app, "_collect_task_evidence", lambda tid: evidence_or_factory
        )


def _envelope(stdout_summary="", telegram_result=None, task_id="T-100"):
    """Build a minimal executor_runs envelope for merge tests."""
    return {
        "task_id": task_id,
        "stdout_summary": stdout_summary,
        "artifact_paths": [],
        "artifact_verification": [],
        "git_evidence": None,
        "telegram_result": telegram_result
        or {"success": False, "skipped": "hermes is async; per-run telegram not sent on submit"},
    }


# ---------------------------------------------------------------------------
# 1. Successful propagation — empty envelope
# ---------------------------------------------------------------------------

def test_successful_telegram_propagation_empty_envelope(monkeypatch):
    """A confirmed task-side telegram_result overrides the placeholder."""
    import app

    confirmed = {
        "sent": True,
        "message_id": 6807,
        "method": "hermes_send",
        "recipient": "5132341473",
        "ts_utc": "2026-07-23T17:43:29Z",
        "ts_taipei": "2026-07-24T01:43:29+08:00",
        "attempts": 1,
        "last_error": None,
    }
    _stub_collect(monkeypatch, {
        "output_text": "task output",
        "artifact_paths": [],
        "artifact_verification": [],
        "telegram_result": confirmed,
        "delivery_json_raw": None,
    })

    env = _envelope(stdout_summary="")
    result = app._merge_task_evidence_into_envelope(env)

    tg = result["telegram_result"]
    assert tg["sent"] is True
    assert tg["message_id"] == 6807
    assert tg["method"] == "hermes_send"
    # The merge marker is set because the full merge fired.
    assert result.get("source") == "executor_runs+tasks_merge"


# ---------------------------------------------------------------------------
# 2. Successful propagation — non-empty envelope (reconciliation wrote stdout)
# ---------------------------------------------------------------------------

def test_successful_telegram_propagation_non_empty_envelope(monkeypatch):
    """The fix must also work when reconciliation already wrote stdout_summary.

    This is the second variant of the bug: ``_executor_evidence_is_empty``
    returns False because stdout_summary is non-empty, so the merge
    short-circuits at the early return. The placeholder
    telegram_result survives and GET returns ``success: False``.
    """
    import app

    confirmed = {
        "sent": True,
        "message_id": 6901,
        "method": "hermes_send",
        "recipient": "5132341473",
    }
    _stub_collect(monkeypatch, {
        "output_text": "task output",
        "artifact_paths": [],
        "artifact_verification": [],
        "telegram_result": confirmed,
        "delivery_json_raw": None,
    })

    env = _envelope(stdout_summary="reconciled final output")
    result = app._merge_task_evidence_into_envelope(env)

    tg = result["telegram_result"]
    assert tg["sent"] is True
    assert tg["message_id"] == 6901
    # stdout_summary is preserved (not clobbered by the telegram-only merge).
    assert result["stdout_summary"] == "reconciled final output"
    # source is NOT set to the merge marker because the full merge
    # did not fire — only the telegram_result was patched.
    assert result.get("source") != "executor_runs+tasks_merge"


# ---------------------------------------------------------------------------
# 3. Failed Telegram result propagation
# ---------------------------------------------------------------------------

def test_failed_telegram_propagation(monkeypatch):
    """A failed task-side notification propagates as sent=False."""
    import app

    failed = {
        "sent": False,
        "method": "hermes_send",
        "recipient": "5132341473",
        "message_id": None,
        "last_error": "hermes send exit=1: connection refused",
    }
    _stub_collect(monkeypatch, {
        "output_text": "task output",
        "artifact_paths": [],
        "artifact_verification": [],
        "telegram_result": failed,
        "delivery_json_raw": None,
    })

    env = _envelope(stdout_summary="")
    result = app._merge_task_evidence_into_envelope(env)

    tg = result["telegram_result"]
    assert tg["sent"] is False
    assert tg["message_id"] is None
    assert "connection refused" in tg["last_error"]


# ---------------------------------------------------------------------------
# 4. Lifecycle/task merge — already-confirmed executor-side preserved
# ---------------------------------------------------------------------------

def test_already_confirmed_executor_side_preserved(monkeypatch):
    """When the executor-side telegram_result is already confirmed,
    the task-side does NOT override it."""
    import app

    executor_confirmed = {
        "success": True,
        "message_id": 9999,
        "recipient": "claude-code-cli-path",
    }
    task_side = {
        "sent": True,
        "message_id": 8888,
        "method": "hermes_send",
    }
    _stub_collect(monkeypatch, {
        "output_text": "task output",
        "artifact_paths": [],
        "artifact_verification": [],
        "telegram_result": task_side,
        "delivery_json_raw": None,
    })

    env = _envelope(stdout_summary="", telegram_result=executor_confirmed)
    result = app._merge_task_evidence_into_envelope(env)

    tg = result["telegram_result"]
    # Executor-side confirmed delivery is preserved.
    assert tg["message_id"] == 9999
    assert tg["success"] is True


# ---------------------------------------------------------------------------
# 5. No task_id -> no merge
# ---------------------------------------------------------------------------

def test_no_task_id_no_merge(monkeypatch):
    """Without a task_id the envelope is returned unchanged."""
    import app

    placeholder = {"success": False, "skipped": "no task"}
    _stub_collect(monkeypatch, {
        "output_text": "x",
        "artifact_paths": [],
        "artifact_verification": [],
        "telegram_result": {"sent": True, "message_id": 1},
        "delivery_json_raw": None,
    })

    env = {
        "task_id": None,
        "stdout_summary": "",
        "telegram_result": placeholder,
    }
    result = app._merge_task_evidence_into_envelope(env)
    assert result["telegram_result"] is placeholder


# ---------------------------------------------------------------------------
# 6. Task-side has no telegram_result -> no override
# ---------------------------------------------------------------------------

def test_no_task_side_telegram_no_override(monkeypatch):
    """When the task-side has no telegram_result, the executor-side
    placeholder survives (there is nothing to merge)."""
    import app

    placeholder = {"success": False, "skipped": "hermes is async"}
    _stub_collect(monkeypatch, {
        "output_text": "output",
        "artifact_paths": [],
        "artifact_verification": [],
        "telegram_result": None,
        "delivery_json_raw": None,
    })

    env = _envelope(stdout_summary="has output", telegram_result=placeholder)
    result = app._merge_task_evidence_into_envelope(env)
    assert result["telegram_result"] == placeholder


# ---------------------------------------------------------------------------
# Regression: GET /runs/{run_id} end-to-end with stubbed adapter
# ---------------------------------------------------------------------------

def test_get_run_returns_confirmed_telegram_after_reconcile(monkeypatch, tmp_path):
    """End-to-end: a Hermes run that reconciles to completed with a
    task-side confirmed notification returns the confirmed
    telegram_result in the GET /runs/{run_id} envelope."""
    from tests._executor_test_helpers import make_client, post_executor

    client, app_module, key = make_client(monkeypatch, tmp_path)

    # Stub the Hermes adapter to report a terminal completed run.
    from aee.adapters.base import RuntimePollResult
    from aee.core.registry import adapter_registry

    class _StubHermesAdapter:
        async def submit(self, job):
            from aee.adapters.base import RuntimeSubmitResult
            return RuntimeSubmitResult(
                external_run_id="run_stub_tg_001",
                status="queued",
            )

        async def poll(self, external_run_id):
            return RuntimePollResult(
                external_run_id=external_run_id,
                status="completed",
                output="task completed output",
                error=None,
                is_terminal=True,
                raw={"status": "completed"},
            )

    # Replace the registered hermes adapter.
    original = adapter_registry._adapters.get("hermes") if hasattr(
        adapter_registry, "_adapters"
    ) else None
    adapter_registry._adapters["hermes"] = _StubHermesAdapter()  # type: ignore[assignment]
    try:
        # Submit a hermes executor run.
        resp = post_executor(client, key, {
            "prompt": "test telegram sync",
            "executor": "hermes",
            "timeout_sec": 30,
        })
        assert resp.status_code == 200
        env = resp.json()
        run_id = env["run_id"]
        assert env["selected_executor"] == "hermes"
        # The submit-time placeholder.
        assert env["telegram_result"]["success"] is False

        # Simulate the task completing + notification gate firing
        # by writing a task_outputs row with a confirmed
        # notification_json blob. First find the task_id linked to
        # this run.
        from dispatcher.db import get_conn
        conn = get_conn()
        row = conn.execute(
            "SELECT task_id FROM executor_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        task_id = row["task_id"] if row else None
        if task_id:
            import json as _json
            notif_blob = _json.dumps({
                "sent": True,
                "method": "hermes_send",
                "recipient": "5132341473",
                "message_id": 7777,
                "ts_utc": "2026-07-23T18:00:00Z",
                "ts_taipei": "2026-07-24T02:00:00+08:00",
                "attempts": 1,
                "last_error": None,
            })
            # Insert a task_outputs row with the notification blob
            # AND a non-empty output_text so _collect_task_evidence
            # returns non-None.
            conn.execute(
                "INSERT OR REPLACE INTO task_outputs "
                "(task_id, output_text, notification_json) "
                "VALUES (?, ?, ?)",
                (task_id, "task completed output", notif_blob),
            )
            conn.commit()

        # GET the run — this triggers reconciliation + merge.
        get_resp = client.get(
            f"/runs/{run_id}",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert get_resp.status_code == 200
        final_env = get_resp.json()
        tg = final_env["telegram_result"]
        assert tg.get("sent") is True or tg.get("success") is True, (
            f"telegram_result should show confirmed delivery, got: {tg}"
        )
        assert tg.get("message_id") == 7777, (
            f"message_id should be 7777, got: {tg}"
        )
    finally:
        if original is not None:
            adapter_registry._adapters["hermes"] = original