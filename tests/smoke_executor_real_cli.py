"""Acceptance D — REAL Claude Code CLI smoke (gated).

This test invokes the real ``/home/ubuntu/.local/bin/claude`` binary
end-to-end through ``POST /runs/executor`` and asserts real evidence:
real ``exit_code``, non-empty ``stdout_summary``, a real artifact with
non-zero size, ``status=completed``, ``selected_executor=claude-code-cli``,
and no MiniMax in the routing block.

It is gated behind ``AEE_REAL_CLI_SMOKE=1`` so the unit suite never
spends real CLI tokens. Run it explicitly:

    AEE_REAL_CLI_SMOKE=1 .venv/bin/python -m pytest \\
        tests/smoke_executor_real_cli.py -s -q

A fake-binary run is NOT acceptable for acceptance D; this test is the
proof.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("AEE_REAL_CLI_SMOKE", "") != "1",
    reason="set AEE_REAL_CLI_SMOKE=1 to run the real CLI smoke (costs tokens)",
)

_SMOKE_ARTIFACT = "/tmp/aee_executor_smoke.md"


@pytest.fixture
def env(monkeypatch, tmp_path):
    # Do NOT override AEE_CLAUDE_CLI_BINARY — use the real configured binary.
    # Grant a SCOPED Write permission (the standard non-interactive Claude
    # Code pattern) so the worker can create the declared /tmp artifact.
    # This is NOT --dangerously-skip-permissions; it is a single-tool,
    # explicit grant scoped to this smoke run.
    monkeypatch.setenv("AEE_CLAUDE_EXTRA_ARGS", "--allowedTools=Write")
    from tests._executor_test_helpers import make_client
    client, app_module, key = make_client(monkeypatch, tmp_path)
    # Clean any leftover artifact from a prior run so `exists` is honest.
    try:
        os.unlink(_SMOKE_ARTIFACT)
    except OSError:
        pass
    return client, key


def test_real_cli_smoke(env):
    client, key = env
    resp = client.post(
        "/runs/executor",
        json={
            "executor": "claude-code-cli",
            "prompt": (
                "Use the write tool to create /tmp/aee_executor_smoke.md "
                "with content: hello from claude-code-cli"
            ),
            "expected_artifacts": [_SMOKE_ARTIFACT],
            "timeout_sec": 180,
            "max_turns": 10,
        },
        headers={"Authorization": f"Bearer {key}"},
        timeout=240,
    )
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    data = resp.json()
    # Print full envelope for the report sidecar / evidence capture.
    print("REAL_CLI_SMOKE_ENVELOPE=" + json.dumps(data))
    # Acceptance D: real evidence
    assert data["selected_executor"] == "claude-code-cli"
    assert data["status"] == "completed", f"status={data['status']!r} error={data.get('error')!r}"
    assert data["exit_code"] == 0, f"exit_code={data['exit_code']!r}"
    assert data["stdout_summary"], "stdout_summary empty"
    assert _SMOKE_ARTIFACT in data["artifact_paths"]
    av = {v["path"]: v for v in data["artifact_verification"]}
    assert av[_SMOKE_ARTIFACT]["exists"] is True
    assert av[_SMOKE_ARTIFACT]["size"] > 0
    # Acceptance C/I: no MiniMax
    assert "MiniMax" not in json.dumps(data["routing"])
    assert data["routing"]["effective_executor"] == "claude-code-cli"
    # The real artifact content must be present on disk.
    assert Path(_SMOKE_ARTIFACT).read_text().strip() == "hello from claude-code-cli"