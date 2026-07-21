"""Focused tests for TASK-AEE-HERMES-COMPLETION-SYNC.

Covers the work-order's required scenarios for terminal-state
synchronization of Hermes async runs into the durable executor_runs
store:

  1. queued Hermes run becomes completed (reconciliation poll
     reports terminal completed → row updated → GET returns fresh
     terminal evidence).
  2. failed Hermes run becomes failed.
  3. idempotent duplicate completion/update (a second GET on the
     same now-terminal row does NOT re-launch any upstream call).
  4. unknown run update rejected (404 unchanged; no upstream call).
  5. unauthenticated internal callback rejected — N/A for the
     polling design (no internal callback endpoint); we verify
     the equivalent invariant: a forged completion cannot be
     injected via GET because the GET handler only writes a
     terminal row when the upstream adapter confirms it.
  6. GET returns updated terminal evidence (stdout_summary, error,
     status, progress, is_terminal flag).
  7. no executor launch on status query (GET is read-only; the
     reconciliation poll is a read-only GET on Hermes 8642, never
     a POST /v1/runs dispatch).

These tests reuse the fixtures from ``tests/_executor_test_helpers.py``
so they run against a temp dispatcher DB and a stubbed Hermes adapter
— no real upstream, no network.
"""
from __future__ import annotations

import pytest

from tests._executor_test_helpers import (
    make_client,
    post_executor,
)


# ---------------------------------------------------------------------------
# Required envelope field set (work-order §2.2)
# ---------------------------------------------------------------------------
_REQUIRED_ENVELOPE_FIELDS = [
    "run_id",
    "requested_executor",
    "selected_executor",
    "status",
    "progress",
    "exit_code",
    "stdout_summary",
    "stderr_summary",
    "artifact_paths",
    "artifact_verification",
    "git_evidence",
    "telegram_result",
    "runtime_identity",
    "error",
]


def _get_run(client, key: str, run_id: str):
    return client.get(
        f"/runs/{run_id}",
        headers={"Authorization": f"Bearer {key}"},
    )


def _install_hermes_stub(monkeypatch, *, mode: str):
    """Install a stub Hermes adapter whose poll() reports the given mode.

    ``mode`` is one of:
      - "running"        → poll returns non-terminal running (no update)
      - "completed"     → poll returns terminal completed with output
      - "failed"        → poll returns terminal failed with error
      - "unknown"       → poll raises UnknownExternalRunError
      - "transient_err" → poll raises AdapterRuntimeError
    """
    from aee.adapters.base import (
        RuntimePollResult,
        UnknownExternalRunError,
        RuntimeError as AdapterRuntimeError,
        RuntimeSubmitResult,
    )
    from aee.core.registry import adapter_registry

    poll_calls: list[str] = []

    class _StubHermes:
        name = "hermes"
        runtime_type = "hermes"

        async def submit(self, job):
            return RuntimeSubmitResult(
                external_run_id="run_sync_test_abc123",
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
                    output="hello from hermes upstream",
                    raw={"status": "completed", "output": "hello from hermes upstream"},
                )
            if mode == "failed":
                return RuntimePollResult(
                    external_run_id=external_run_id,
                    status="failed",
                    is_terminal=True,
                    error="hermes upstream failed: simulated error",
                    raw={"status": "failed", "error": "simulated error"},
                )
            if mode == "unknown":
                raise UnknownExternalRunError(f"hermes run {external_run_id!r} not found")
            if mode == "transient_err":
                raise AdapterRuntimeError("simulated hermes outage")
            raise AssertionError(f"unknown mode {mode!r}")

        async def cancel(self, external_run_id):
            from aee.adapters.base import RuntimeCancelResult
            return RuntimeCancelResult(external_run_id=external_run_id, cancelled=True)

    saved = dict(adapter_registry._adapters)
    stub = _StubHermes()
    adapter_registry._adapters["hermes"] = stub
    # Restore the original registry on teardown (not immediately).
    monkeypatch.setattr(
        adapter_registry, "_adapters", saved, raising=False
    )
    # Re-install the stub AFTER monkeypatch registers the restore —
    # monkeypatch.setattr schedules the restore for teardown, but it
    # also sets the value to ``saved`` immediately.  We re-inject the
    # stub so the test sees it, and the teardown restore puts back the
    # original dict.
    adapter_registry._adapters["hermes"] = stub
    return stub, poll_calls


# ---------------------------------------------------------------------------
# 1. queued Hermes run becomes completed
# ---------------------------------------------------------------------------
def test_queued_hermes_run_becomes_completed(monkeypatch, tmp_path):
    """A queued Hermes run is reconciled to completed via the upstream poll."""
    stub, poll_calls = _install_hermes_stub(monkeypatch, mode="completed")
    client, _app, key = make_client(monkeypatch, tmp_path)

    # POST a hermes run — persists a queued row.
    resp = post_executor(client, key, {
        "executor": "hermes",
        "prompt": "do work",
        "timeout_sec": 30,
    })
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]
    assert resp.json()["status"] in {"queued", "running"}

    # GET — reconciliation polls upstream, sees completed, persists final.
    get1 = _get_run(client, key, run_id)
    assert get1.status_code == 200, f"{get1.status_code}: {get1.text}"
    env1 = get1.json()
    assert env1["run_id"] == run_id
    assert env1["status"] == "completed"
    assert env1["is_terminal"] is True
    assert env1["progress"] == 1.0
    assert "hello from hermes upstream" in env1.get("stdout_summary", "")
    assert env1["source"] == "executor_runs"
    # Work-order §2.2 envelope fields present.
    for field in _REQUIRED_ENVELOPE_FIELDS:
        assert field in env1, f"GET envelope missing required field: {field!r}"

    # Reconciliation polled exactly once.
    assert len(poll_calls) == 1, f"expected 1 poll, got {len(poll_calls)}"


# ---------------------------------------------------------------------------
# 2. failed Hermes run becomes failed
# ---------------------------------------------------------------------------
def test_failed_hermes_run_becomes_failed(monkeypatch, tmp_path):
    """A queued Hermes run is reconciled to failed with the upstream error."""
    stub, poll_calls = _install_hermes_stub(monkeypatch, mode="failed")
    client, _app, key = make_client(monkeypatch, tmp_path)

    resp = post_executor(client, key, {
        "executor": "hermes",
        "prompt": "do work that fails",
        "timeout_sec": 30,
    })
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    get1 = _get_run(client, key, run_id)
    assert get1.status_code == 200, f"{get1.status_code}: {get1.text}"
    env1 = get1.json()
    assert env1["run_id"] == run_id
    assert env1["status"] == "failed"
    assert env1["is_terminal"] is True
    assert env1["error"] is not None
    assert "simulated error" in env1["error"] or "hermes upstream failed" in env1["error"]
    assert env1["source"] == "executor_runs"


# ---------------------------------------------------------------------------
# 3. idempotent duplicate completion/update
# ---------------------------------------------------------------------------
def test_idempotent_duplicate_completion(monkeypatch, tmp_path):
    """A second GET on the now-terminal row does NOT re-launch any upstream call."""
    stub, poll_calls = _install_hermes_stub(monkeypatch, mode="completed")
    client, _app, key = make_client(monkeypatch, tmp_path)

    resp = post_executor(client, key, {
        "executor": "hermes",
        "prompt": "do work",
        "timeout_sec": 30,
    })
    run_id = resp.json()["run_id"]

    # First GET reconciles (1 poll).
    get1 = _get_run(client, key, run_id)
    assert get1.status_code == 200
    env1 = get1.json()
    assert env1["status"] == "completed"
    assert len(poll_calls) == 1

    # Second GET — row is terminal, no upstream call, same envelope.
    get2 = _get_run(client, key, run_id)
    assert get2.status_code == 200
    env2 = get2.json()
    assert env2["status"] == "completed"
    assert env2 == env1  # byte-for-byte identical envelope
    assert len(poll_calls) == 1, (
        "Second GET on a terminal row must not re-launch any upstream poll; "
        f"poll_calls={poll_calls}"
    )


# ---------------------------------------------------------------------------
# 4. unknown run update rejected (404, no upstream call)
# ---------------------------------------------------------------------------
def test_unknown_run_rejected_no_upstream(monkeypatch, tmp_path):
    """GET on an unknown run_id returns 404 without launching an upstream call."""
    stub, poll_calls = _install_hermes_stub(monkeypatch, mode="completed")
    client, _app, key = make_client(monkeypatch, tmp_path)

    resp = _get_run(client, key, "run_never_seen_xyz999")
    assert resp.status_code == 404
    detail = resp.json().get("detail", resp.json())
    assert isinstance(detail, dict)
    assert detail.get("code") == "unknown_run_id"
    # No upstream poll should have been attempted.
    assert len(poll_calls) == 0, (
        f"unknown run GET must not call adapter.poll; poll_calls={poll_calls}"
    )


# ---------------------------------------------------------------------------
# 5. forged completion cannot be injected via GET
# ---------------------------------------------------------------------------
def test_forged_completion_cannot_be_injected(monkeypatch, tmp_path):
    """The GET handler only writes a terminal row when the upstream adapter
    confirms the terminal state. A non-terminal upstream report leaves the
    row in-flight — a caller cannot forge completion by merely issuing a GET.
    """
    stub, poll_calls = _install_hermes_stub(monkeypatch, mode="running")
    client, _app, key = make_client(monkeypatch, tmp_path)

    resp = post_executor(client, key, {
        "executor": "hermes",
        "prompt": "do work",
        "timeout_sec": 30,
    })
    run_id = resp.json()["run_id"]
    assert resp.json()["status"] in {"queued", "running"}

    # GET — upstream reports running; row stays non-terminal.
    get1 = _get_run(client, key, run_id)
    assert get1.status_code == 200
    env1 = get1.json()
    assert env1["status"] in {"queued", "running"}
    assert env1["is_terminal"] is False
    assert len(poll_calls) == 1

    # Second GET — upstream still reports running; row stays non-terminal.
    # A second poll IS allowed here because the row is still non-terminal.
    get2 = _get_run(client, key, run_id)
    assert get2.status_code == 200
    env2 = get2.json()
    assert env2["status"] in {"queued", "running"}
    assert env2["is_terminal"] is False
    assert len(poll_calls) == 2
    # No completion was forged — the row is still in-flight.


# ---------------------------------------------------------------------------
# 6. GET returns updated terminal evidence
# ---------------------------------------------------------------------------
def test_get_returns_updated_terminal_evidence(monkeypatch, tmp_path):
    """The terminal GET response carries the final stdout_summary + error."""
    stub, poll_calls = _install_hermes_stub(monkeypatch, mode="completed")
    client, _app, key = make_client(monkeypatch, tmp_path)

    resp = post_executor(client, key, {
        "executor": "hermes",
        "prompt": "do work",
        "timeout_sec": 30,
    })
    run_id = resp.json()["run_id"]
    pre_env = resp.json()
    # Pre-reconciliation envelope has empty stdout_summary (queued).
    assert pre_env["stdout_summary"] == "" or pre_env["stdout_summary"] is None

    get1 = _get_run(client, key, run_id)
    env = get1.json()
    assert env["status"] == "completed"
    assert env["progress"] == 1.0
    assert env["stdout_summary"]  # non-empty after reconciliation
    assert "hello from hermes upstream" in env["stdout_summary"]
    # completed_at is stamped.
    assert env.get("completed_at") is not None
    # created_at preserved (not overwritten by the reconciliation upsert).
    assert env.get("created_at") is not None


# ---------------------------------------------------------------------------
# 7. no executor launch on status query
# ---------------------------------------------------------------------------
def test_no_executor_launch_on_status_query(monkeypatch, tmp_path):
    """GET never invokes adapter.submit; only adapter.poll (read-only GET on 8642)."""
    stub, poll_calls = _install_hermes_stub(monkeypatch, mode="completed")
    submit_calls: list = []

    orig_submit = stub.submit

    async def _tracked_submit(job):
        submit_calls.append(job)
        return await orig_submit(job)

    monkeypatch.setattr(stub, "submit", _tracked_submit, raising=False)

    client, _app, key = make_client(monkeypatch, tmp_path)

    # POST one hermes run (this legitimately calls submit once).
    resp = post_executor(client, key, {
        "executor": "hermes",
        "prompt": "do work",
        "timeout_sec": 30,
    })
    run_id = resp.json()["run_id"]
    assert len(submit_calls) == 1, "POST must call submit exactly once"

    # GET the run — must NOT call submit (no executor launch).
    get1 = _get_run(client, key, run_id)
    assert get1.status_code == 200
    assert len(submit_calls) == 1, (
        f"GET must not launch a new executor (submit must not be called); "
        f"submit_calls={len(submit_calls)}"
    )

    # GET an unknown id — also must NOT call submit.
    get2 = _get_run(client, key, "run_unknown_999")
    assert get2.status_code == 404
    assert len(submit_calls) == 1


# ---------------------------------------------------------------------------
# 8. upstream-unknown run transitions to timeout
# ---------------------------------------------------------------------------
def test_upstream_unknown_run_transitions_to_timeout(monkeypatch, tmp_path):
    """When the upstream no longer tracks the run, the row is reconciled
    to a deterministic ``timeout`` terminal state (mirrors the watcher's
    handling of UnknownExternalRunError)."""
    stub, poll_calls = _install_hermes_stub(monkeypatch, mode="unknown")
    client, _app, key = make_client(monkeypatch, tmp_path)

    resp = post_executor(client, key, {
        "executor": "hermes",
        "prompt": "do work",
        "timeout_sec": 30,
    })
    run_id = resp.json()["run_id"]

    get1 = _get_run(client, key, run_id)
    assert get1.status_code == 200
    env = get1.json()
    assert env["status"] == "timeout"
    assert env["is_terminal"] is True
    assert env["error"] is not None
    assert "no longer tracks" in env["error"]


# ---------------------------------------------------------------------------
# 9. transient upstream error leaves the row in-flight
# ---------------------------------------------------------------------------
def test_transient_upstream_error_leaves_in_flight(monkeypatch, tmp_path):
    """A transient upstream error does NOT mutate the row — callers can
    continue polling and the next GET may succeed when the upstream recovers."""
    stub, poll_calls = _install_hermes_stub(monkeypatch, mode="transient_err")
    client, _app, key = make_client(monkeypatch, tmp_path)

    resp = post_executor(client, key, {
        "executor": "hermes",
        "prompt": "do work",
        "timeout_sec": 30,
    })
    run_id = resp.json()["run_id"]
    pre_status = resp.json()["status"]
    assert pre_status in {"queued", "running"}

    get1 = _get_run(client, key, run_id)
    assert get1.status_code == 200
    env = get1.json()
    # Row stays in-flight — not forged to a terminal state by the error.
    assert env["status"] in {"queued", "running"}
    assert env["is_terminal"] is False


# ---------------------------------------------------------------------------
# 10. claude-code-cli runs are NOT reconciled (already terminal)
# ---------------------------------------------------------------------------
def test_claude_code_cli_run_not_reconciled(monkeypatch, tmp_path):
    """A claude-code-cli run is already terminal after POST; GET does not
    call the hermes adapter at all (different executor, no reconciliation)."""
    from tests._executor_test_helpers import (
        set_fake_binary,
        write_fake_claude,
    )
    stub, poll_calls = _install_hermes_stub(monkeypatch, mode="completed")
    artifact = str(tmp_path / "cc.md")
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
    run_id = resp.json()["run_id"]
    assert resp.json()["status"] == "completed"

    get1 = _get_run(client, key, run_id)
    assert get1.status_code == 200
    env = get1.json()
    assert env["status"] == "completed"
    assert env["is_terminal"] is True
    # The hermes adapter was never polled for a claude-code-cli run.
    assert len(poll_calls) == 0, (
        f"claude-code-cli runs must not trigger hermes reconciliation; "
        f"poll_calls={poll_calls}"
    )