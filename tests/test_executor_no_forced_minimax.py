"""Acceptance C/I — no forced MiniMax-M3 override on /runs/executor.

When ``executor=claude-code-cli`` is requested, ``routing.effective_executor``
must be ``claude-code-cli`` and the string ``MiniMax`` must not appear
anywhere in the routing block. The endpoint never calls the GPT->MiniMax
routing layer, so ``was_forced`` is always False.
"""
from __future__ import annotations

import json

import pytest

from tests._executor_test_helpers import (
    make_client,
    post_executor,
    set_fake_binary,
    write_fake_claude,
)


@pytest.fixture
def env(monkeypatch, tmp_path):
    artifact = str(tmp_path / "smoke.md")
    binary = write_fake_claude(tmp_path, artifact=artifact)
    set_fake_binary(monkeypatch, binary)
    client, app_module, key = make_client(monkeypatch, tmp_path)
    return client, key, artifact


def test_routing_block_has_no_minimax(env):
    client, key, artifact = env
    resp = post_executor(client, key, {
        "executor": "claude-code-cli",
        "prompt": "create the artifact",
        "expected_artifacts": [artifact],
        "timeout_sec": 30,
    })
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    data = resp.json()
    routing = data["routing"]
    assert routing["effective_executor"] == "claude-code-cli"
    assert routing["was_forced"] is False
    # The literal "MiniMax" must not appear anywhere in the routing block.
    blob = json.dumps(routing)
    assert "MiniMax" not in blob, f"MiniMax leaked into routing: {routing!r}"
    assert "minimax" not in blob.lower()


def test_effective_executor_not_minimax_model(env):
    """routing.effective_model is None (the CLI uses its own model), never MiniMax."""
    client, key, artifact = env
    resp = post_executor(client, key, {
        "executor": "claude-code-cli",
        "prompt": "create the artifact",
        "expected_artifacts": [artifact],
        "timeout_sec": 30,
    })
    assert resp.status_code == 200
    routing = resp.json()["routing"]
    assert routing["effective_executor"] != "MiniMaxAI/MiniMax-M3"
    assert routing["effective_model"] is None