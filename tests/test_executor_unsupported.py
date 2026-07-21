"""Acceptance E — unsupported executor returns a deterministic 400.

Unsupported values return HTTP 400 with ``detail.code == 'unsupported_executor'``
and a ``supported_executors`` list. No silent fallback to Hermes or MiniMax.
"""
from __future__ import annotations

import pytest

from tests._executor_test_helpers import make_client, post_executor


@pytest.fixture
def env(monkeypatch, tmp_path):
    client, app_module, key = make_client(monkeypatch, tmp_path)
    return client, key


def test_unsupported_executor_returns_400(env):
    client, key = env
    resp = post_executor(client, key, {
        "executor": "gemini",
        "prompt": "anything",
        "timeout_sec": 30,
    })
    assert resp.status_code == 400, f"expected 400, got {resp.status_code}: {resp.text}"
    detail = resp.json()["detail"]
    assert detail["code"] == "unsupported_executor"
    assert isinstance(detail["supported_executors"], list)
    assert "claude-code-cli" in detail["supported_executors"]
    assert "hermes" in detail["supported_executors"]


def test_unsupported_executor_no_silent_fallback(env):
    """An unsupported executor must NOT produce a 200 run (no fallback)."""
    client, key = env
    resp = post_executor(client, key, {
        "executor": "minimax-m3",
        "prompt": "anything",
        "timeout_sec": 30,
    })
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "unsupported_executor"