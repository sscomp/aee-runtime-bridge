"""Focused regression for the Claude Code executor auth-bridge fix.

TASK-AEE-CLAUDE-CODE-EXECUTOR-RECOVERY (2026-07-22): The executor path
``ClaudeCodeCliRunner.run()`` previously called
``ClaudeCodeProvider.submit()`` without ``env=``, so the worker
subprocess only received ``_ALLOWED_ENV_VARS ∩ os.environ``. On hosts
that carry the Ollama-Cloud bearer under ``ANTHROPIC_AUTH_TOKEN``
(and do NOT set ``ANTHROPIC_API_KEY``), the worker exited with
``Not logged in · Please run /login`` and exit_code 1.

The fix mirrors ``ANTHROPIC_AUTH_TOKEN`` -> ``ANTHROPIC_API_KEY`` in
the runner before submit (the same pattern proven by
``aee/orchestrator/claude_code_provider_shim.py`` for the orchestrator
path). These tests pin the fix without invoking the real CLI: a fake
binary inspects its own env and fails when the mirror is missing.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from tests._executor_test_helpers import make_client, post_executor


def _write_env_inspecting_fake_claude(
    tmp_path: Path,
    *,
    marker_path: str,
    name: str = "fake-claude-env-inspect",
) -> str:
    """Write a fake claude binary that:
      1. For ``--version`` probes (from ``collect_runtime_identity``):
         print a static version string and exit 0 WITHOUT touching the
         marker. The version probe is a separate, harmless subprocess
         invocation; it must not pollute the run-evidence marker.
      2. For prompt runs (any other argv): write ANTHROPIC_API_KEY
         presence (1/0) to ``marker_path``, then exit 0 if the API key
         is present, else exit 1 with the same "Not logged in" message
         the real CLI prints.

    This reproduces the production failure mode in a hermetic test
    while coexisting with the bridge's separate version-probe call.
    """
    lines = [
        "#!/usr/bin/env bash",
        # The bridge's collect_runtime_identity() invokes
        # ``<binary> --version`` to capture the executor version. That
        # is a separate, harmless subprocess; it must not write the
        # marker (it would clobber the run evidence).
        'if [ "$1" = "--version" ]; then',
        '  echo "fake-claude 0.0.0-test"',
        "  exit 0",
        "fi",
        # Prompt-run path: capture whether ANTHROPIC_API_KEY is set.
        'if [ -n "${ANTHROPIC_API_KEY:-}" ]; then',
        f'  echo "1" > {marker_path!r}',
        'else',
        f'  echo "0" > {marker_path!r}',
        '  echo "Not logged in · Please run /login"',
        "  exit 1",
        "fi",
        'echo "fake claude ok"',
        "exit 0",
    ]
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Configure a fake claude that inspects its own ANTHROPIC_API_KEY.

    The parent env is set up to mimic the production failure host:
    ``ANTHROPIC_AUTH_TOKEN`` set, ``ANTHROPIC_API_KEY`` unset. The fix
    mirrors the token into the API key before spawning the worker; the
    fake binary therefore sees the API key and exits 0. Without the
    fix, the fake binary sees no API key and exits 1 with the real
    "Not logged in" message.
    """
    marker = str(tmp_path / "env_marker.txt")
    binary = _write_env_inspecting_fake_claude(tmp_path, marker_path=marker)
    monkeypatch.setenv("AEE_CLAUDE_CLI_BINARY", binary)
    # Mimic the production host: AUTH_TOKEN present, API_KEY absent.
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-bearer-token")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client, app_module, key = make_client(monkeypatch, tmp_path)
    # Clean any leftover marker so `exists` is honest.
    try:
        os.unlink(marker)
    except OSError:
        pass
    return client, key, marker


def test_env_mirror_propagates_api_key_to_worker(env):
    """The runner mirrors AUTH_TOKEN -> API_KEY; the worker sees the key."""
    client, key, marker = env
    resp = post_executor(client, key, {
        "executor": "claude-code-cli",
        "prompt": "noop — fake binary inspects env only",
        "timeout_sec": 30,
    })
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["selected_executor"] == "claude-code-cli"
    # The fix: worker sees ANTHROPIC_API_KEY -> exits 0 -> completed.
    assert data["status"] == "completed", (
        f"status={data['status']!r} error={data.get('error')!r} "
        f"stdout={data.get('stdout_summary')!r}"
    )
    assert data["exit_code"] == 0
    # Marker proves the env-mirror reached the subprocess.
    assert os.path.exists(marker), "env marker file was not written"
    assert Path(marker).read_text().strip() == "1", (
        "ANTHROPIC_API_KEY was NOT present in the worker env; mirror failed"
    )


def test_env_mirror_skipped_when_api_key_already_set(monkeypatch, tmp_path):
    """When ANTHROPIC_API_KEY is already set, the mirror is a no-op.

    The mirror helper must NOT overwrite an existing API_KEY. This pins
    the "only when unset" contract from the shim.
    """
    from aee.runtimes.executor_cli import _build_claude_env_mirror

    # API_KEY already set -> mirror is identity.
    out = _build_claude_env_mirror({
        "ANTHROPIC_API_KEY": "explicit-key",
        "ANTHROPIC_AUTH_TOKEN": "ignored-token",
    })
    assert out["ANTHROPIC_API_KEY"] == "explicit-key"

    # API_KEY absent, AUTH_TOKEN present -> mirror copies the token.
    out = _build_claude_env_mirror({"ANTHROPIC_AUTH_TOKEN": "the-token"})
    assert out["ANTHROPIC_API_KEY"] == "the-token"
    assert out["ANTHROPIC_AUTH_TOKEN"] == "the-token"

    # Both absent -> no key added (subprocess will fail; that's the
    # caller's signal that no auth is available).
    out = _build_claude_env_mirror({"PATH": "/usr/bin"})
    assert "ANTHROPIC_API_KEY" not in out

    # Purity: input mapping is never mutated.
    src = {"ANTHROPIC_AUTH_TOKEN": "tok"}
    _build_claude_env_mirror(src)
    assert src == {"ANTHROPIC_AUTH_TOKEN": "tok"}, "mirror mutated its input"


def test_failure_path_without_mirror_yields_not_logged_in(monkeypatch, tmp_path):
    """Regression: without the mirror, the worker exits "Not logged in".

    This test simulates the pre-fix behaviour by clearing AUTH_TOKEN
    entirely, so the mirror has nothing to copy. The fake binary then
    reports the absence of API_KEY and the run fails with the
    canonical "Not logged in" stdout. It pins the failure mode the
    recovery was commissioned to fix.
    """
    marker = str(tmp_path / "env_marker_fail.txt")
    binary = _write_env_inspecting_fake_claude(
        tmp_path, marker_path=marker, name="fake-claude-fail-env"
    )
    monkeypatch.setenv("AEE_CLAUDE_CLI_BINARY", binary)
    # Both auth vars absent -> mirror has nothing to copy -> worker fails.
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client, _app, key = make_client(monkeypatch, tmp_path)
    try:
        os.unlink(marker)
    except OSError:
        pass

    resp = post_executor(client, key, {
        "executor": "claude-code-cli",
        "prompt": "noop — fake binary inspects env only",
        "timeout_sec": 30,
    })
    assert resp.status_code == 200
    data = resp.json()
    # Pre-fix failure mode reproduced: status=failed, exit=1, marker=0.
    assert data["status"] == "failed", (
        f"expected failed when no auth is available; got {data['status']!r}"
    )
    assert data["exit_code"] == 1
    assert "Not logged in" in (data.get("stdout_summary") or "")
    # Marker proves the env really was missing the API key.
    assert os.path.exists(marker)
    assert Path(marker).read_text().strip() == "0"