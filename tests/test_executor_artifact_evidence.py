"""Acceptance F — artifact / git / telegram evidence fields shaped correctly.

Verifies ``artifact_paths``, ``artifact_verification`` (exists/size/mtime/sha256),
``git_evidence`` (head_sha/branch/dirty/staged_file_count), and
``telegram_result`` are present and correctly shaped in the response.
"""
from __future__ import annotations

import re

import pytest

from tests._executor_test_helpers import (
    make_client,
    post_executor,
    set_fake_binary,
    write_fake_claude,
)


_HEX40 = re.compile(r"^[0-9a-f]{40}$")


@pytest.fixture
def env(monkeypatch, tmp_path):
    artifact = str(tmp_path / "evidence.md")
    binary = write_fake_claude(tmp_path, artifact=artifact)
    set_fake_binary(monkeypatch, binary)
    client, app_module, key = make_client(monkeypatch, tmp_path)
    return client, key, artifact


def test_artifact_verification_shaped(env):
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
    assert isinstance(data["artifact_paths"], list)
    assert artifact in data["artifact_paths"]
    av = data["artifact_verification"]
    assert isinstance(av, list) and len(av) == 1
    entry = av[0]
    assert entry["path"] == artifact
    assert entry["exists"] is True
    assert isinstance(entry["size"], int) and entry["size"] > 0
    assert isinstance(entry["mtime"], int)
    # sha256 optional but present when artifact_sha256 config is true
    assert entry.get("sha256") is None or re.match(r"^[0-9a-f]{64}$", entry["sha256"])


def test_git_evidence_shaped(env):
    client, key, artifact = env
    resp = post_executor(client, key, {
        "executor": "claude-code-cli",
        "prompt": "create the artifact",
        "expected_artifacts": [artifact],
        "timeout_sec": 30,
        "repo_path": "/home/ubuntu/Abacus",
    })
    assert resp.status_code == 200
    ge = resp.json()["git_evidence"]
    assert ge is not None
    assert _HEX40.match(ge["head_sha"]), f"head_sha not a 40-hex sha: {ge['head_sha']!r}"
    assert isinstance(ge["branch"], str)
    assert isinstance(ge["dirty"], bool)
    assert isinstance(ge["staged_file_count"], int)
    assert ge["repo_path"] == "/home/ubuntu/Abacus"


def test_telegram_result_shaped(env):
    """telegram_result is always a dict (success or skipped-with-reason)."""
    client, key, artifact = env
    resp = post_executor(client, key, {
        "executor": "claude-code-cli",
        "prompt": "create the artifact",
        "expected_artifacts": [artifact],
        "timeout_sec": 30,
    })
    assert resp.status_code == 200
    tr = resp.json()["telegram_result"]
    assert isinstance(tr, dict)
    # Either a real success (success=True, message_id int) or a truthful skip.
    if tr.get("success") is True:
        assert "message_id" in tr
    else:
        assert "skipped" in tr and tr["skipped"]