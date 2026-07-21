"""Acceptance F — full response envelope matches work-order §2.4 schema.

Every required field is present with the correct type:
selected_executor, requested_executor, run_id, task_id, status, progress,
routing, artifact_paths, stdout_summary, stderr_summary, exit_code,
timeout_state, cancel_state, git_evidence, artifact_verification,
telegram_result.
"""
from __future__ import annotations

import pytest

from tests._executor_test_helpers import (
    make_client,
    post_executor,
    set_fake_binary,
    write_fake_claude,
)


_REQUIRED_FIELDS = [
    "selected_executor", "requested_executor", "run_id", "task_id",
    "status", "progress", "routing", "artifact_paths",
    "stdout_summary", "stderr_summary", "exit_code",
    "timeout_state", "cancel_state", "git_evidence",
    "artifact_verification", "telegram_result",
]


@pytest.fixture
def env(monkeypatch, tmp_path):
    artifact = str(tmp_path / "contract.md")
    binary = write_fake_claude(tmp_path, artifact=artifact)
    set_fake_binary(monkeypatch, binary)
    client, app_module, key = make_client(monkeypatch, tmp_path)
    return client, key, artifact


def test_full_envelope_contract(env):
    client, key, artifact = env
    resp = post_executor(client, key, {
        "executor": "claude-code-cli",
        "prompt": "create the artifact",
        "expected_artifacts": [artifact],
        "timeout_sec": 30,
        "repo_path": "/home/ubuntu/Abacus",
    })
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    data = resp.json()
    for field in _REQUIRED_FIELDS:
        assert field in data, f"envelope missing required field: {field!r}"

    # Type assertions per §2.4
    assert isinstance(data["selected_executor"], str)
    assert data["requested_executor"] is None or isinstance(data["requested_executor"], str)
    assert isinstance(data["run_id"], str) and data["run_id"]
    assert data["task_id"] is None or isinstance(data["task_id"], str)
    assert data["status"] in {"queued", "running", "completed", "failed", "timeout", "cancelled"}
    assert isinstance(data["progress"], (int, float))
    assert 0.0 <= float(data["progress"]) <= 1.0
    assert isinstance(data["routing"], dict)
    assert isinstance(data["artifact_paths"], list)
    assert isinstance(data["stdout_summary"], str)
    assert isinstance(data["stderr_summary"], str)
    assert data["exit_code"] is None or isinstance(data["exit_code"], int)
    assert data["timeout_state"] is None or isinstance(data["timeout_state"], str)
    assert data["cancel_state"] is None or isinstance(data["cancel_state"], str)
    assert data["git_evidence"] is None or isinstance(data["git_evidence"], dict)
    assert isinstance(data["artifact_verification"], list)
    assert isinstance(data["telegram_result"], dict)


def test_hermes_envelope_contract(monkeypatch, tmp_path):
    """executor=hermes also returns the full envelope (queued shape)."""
    from aee.adapters.base import RuntimePollResult, RuntimeSubmitResult
    from aee.core.registry import adapter_registry

    class _StubHermes:
        name = "hermes"
        runtime_type = "hermes"

        async def submit(self, job):
            return RuntimeSubmitResult(external_run_id="stub-hermes-run", status="queued")

        async def poll(self, external_run_id):
            return RuntimePollResult(external_run_id=external_run_id, status="completed", is_terminal=True)

        async def cancel(self, external_run_id):
            from aee.adapters.base import RuntimeCancelResult
            return RuntimeCancelResult(external_run_id=external_run_id, cancelled=True)

    saved = dict(adapter_registry._adapters)
    adapter_registry._adapters["hermes"] = _StubHermes()
    try:
        client, app_module, key = make_client(monkeypatch, tmp_path)
        resp = post_executor(client, key, {
            "executor": "hermes",
            "prompt": "do a thing",
            "timeout_sec": 30,
        })
        assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
        data = resp.json()
        for field in _REQUIRED_FIELDS:
            assert field in data, f"hermes envelope missing: {field!r}"
        assert data["selected_executor"] == "hermes"
        assert data["routing"]["effective_executor"] == "hermes"
        assert data["status"] in {"queued", "running", "completed"}
        assert isinstance(data["telegram_result"], dict)
    finally:
        adapter_registry._adapters.clear()
        adapter_registry._adapters.update(saved)