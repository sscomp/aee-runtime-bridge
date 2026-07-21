"""Acceptance B/D (unit) — selected_executor == claude-code-cli end-to-end.

Proves the adapter path: a fake claude binary writes the declared
artifact, the runner captures stdout/exit_code, and the endpoint
returns ``selected_executor == "claude-code-cli"`` with real evidence
(real stdout_summary, real exit_code, real non-zero artifact). The
real-CLI smoke (acceptance D with the actual binary) is run separately.
"""
from __future__ import annotations

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
    binary = write_fake_claude(
        tmp_path,
        artifact=artifact,
        artifact_content="hello from claude-code-cli",
        stdout="fake claude stdout ok",
    )
    set_fake_binary(monkeypatch, binary)
    client, app_module, key = make_client(monkeypatch, tmp_path)
    return client, key, artifact


def test_selected_executor_claude_code_cli_end_to_end(env):
    client, key, artifact = env
    resp = post_executor(client, key, {
        "executor": "claude-code-cli",
        "prompt": "Use the write tool to create the artifact.",
        "expected_artifacts": [artifact],
        "timeout_sec": 30,
    })
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    data = resp.json()
    # B: selected_executor echoed
    assert data["selected_executor"] == "claude-code-cli"
    # D (unit): real evidence shape — completed, real exit_code, real stdout
    assert data["status"] == "completed"
    assert data["exit_code"] == 0
    assert data["stdout_summary"]  # non-empty
    # real artifact path produced and verified
    assert artifact in data["artifact_paths"]
    av = {v["path"]: v for v in data["artifact_verification"]}
    assert av[artifact]["exists"] is True
    assert av[artifact]["size"] > 0
    assert av[artifact]["mtime"] is not None


def test_nonzero_exit_marked_failed(env, monkeypatch, tmp_path):
    """A non-zero exit from the CLI surfaces as status=failed with the code."""
    artifact = str(tmp_path / "fail.md")
    binary = write_fake_claude(
        tmp_path, artifact=None, exit_code=3, stdout="boom",
        name="fake-claude-fail",
    )
    set_fake_binary(monkeypatch, binary)
    client, app_module, key = make_client(monkeypatch, tmp_path)
    resp = post_executor(client, key, {
        "executor": "claude-code-cli",
        "prompt": "do something that fails",
        "expected_artifacts": [artifact],
        "timeout_sec": 30,
    })
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["status"] == "failed"
    assert data["exit_code"] == 3
    assert data["selected_executor"] == "claude-code-cli"