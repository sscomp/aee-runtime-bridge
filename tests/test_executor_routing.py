"""Acceptance A/B/C — executor routing honors the request; no MiniMax override.

Covers work-order §2.3 and acceptance A/B/C: when ``executor=claude-code-cli``
is requested the response's ``selected_executor`` and
``routing.effective_executor`` echo it verbatim; the alias ``claude_code``
canonicalises to ``claude-code-cli``; omitting ``executor`` applies the
documented default. Uses a fake claude binary so the run completes.
"""
from __future__ import annotations

from pathlib import Path

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


def test_executor_claude_code_cli_honored(env):
    """executor=claude-code-cli -> selected/effective echo it."""
    client, key, artifact = env
    resp = post_executor(client, key, {
        "executor": "claude-code-cli",
        "prompt": "create the artifact",
        "expected_artifacts": [artifact],
        "timeout_sec": 30,
    })
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["selected_executor"] == "claude-code-cli"
    assert data["requested_executor"] == "claude-code-cli"
    assert data["routing"]["effective_executor"] == "claude-code-cli"
    assert data["routing"]["selected_executor"] == "claude-code-cli"
    assert data["routing"]["selection_source"] == "explicit"


def test_executor_alias_claude_code_canonicalised(env):
    """alias claude_code -> canonical response value claude-code-cli."""
    client, key, artifact = env
    resp = post_executor(client, key, {
        "executor": "claude_code",
        "prompt": "create the artifact",
        "expected_artifacts": [artifact],
        "timeout_sec": 30,
    })
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["selected_executor"] == "claude-code-cli"
    assert data["requested_executor"] == "claude_code"
    assert data["routing"]["effective_executor"] == "claude-code-cli"


def test_executor_default_when_omitted(env):
    """omitted executor -> configured default (claude-code-cli), source=default."""
    client, key, artifact = env
    resp = post_executor(client, key, {
        "prompt": "create the artifact",
        "expected_artifacts": [artifact],
        "timeout_sec": 30,
    })
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["selected_executor"] == "claude-code-cli"
    assert data["requested_executor"] is None
    assert data["routing"]["selection_source"] == "default"
    assert data["routing"]["was_forced"] is False