"""Shared helpers for the POST /runs/executor test suite.

Builds a FastAPI TestClient with bridge auth + a temp dispatcher DB,
and mints executable fake ``claude`` binaries so the claude-code-cli
path can be exercised end-to-end without invoking the real CLI (the
real CLI smoke is acceptance D, run separately).
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def setup_temp_db(monkeypatch, tmp_path: Path) -> None:
    """Point the dispatcher at a temp DB / log / report dir."""
    from dispatcher import db as ddb
    from dispatcher import manager as dmgr

    monkeypatch.setattr(ddb, "DB_DIR", tmp_path)
    monkeypatch.setattr(ddb, "DB_PATH", tmp_path / "dispatcher.db")
    monkeypatch.setattr(dmgr, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(dmgr, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(ddb, "_initialized", False)
    if hasattr(ddb._local, "conn"):
        ddb._local.conn = None


def make_client(monkeypatch, tmp_path: Path, test_key: str = "exec-fixture-key"):
    """Return ``(TestClient, app_module, test_key)`` with auth + temp DB."""
    from fastapi.testclient import TestClient

    import app as app_module

    setup_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv("BRIDGE_API_KEY", test_key)
    app_module.CLIENT_BRIDGE_KEYS = {test_key}
    return TestClient(app_module.app), app_module, test_key


def write_fake_claude(
    tmp_path: Path,
    *,
    artifact: str | None = None,
    artifact_content: str = "hello from claude-code-cli",
    sleep: float = 0.0,
    exit_code: int = 0,
    stdout: str = "fake claude stdout ok",
    stderr: str = "",
    name: str = "fake-claude",
) -> str:
    """Write an executable fake claude binary; return its absolute path.

    The script ignores all argv (the real provider passes
    ``-p [--bare] --max-turns N --output-format text <prompt>``), so we
    can simulate success / artifact write / sleep / non-zero exit without
    depending on any env var being forwarded through the provider's
    allow-list filter.
    """
    lines = ["#!/usr/bin/env bash"]
    if stderr:
        lines.append(f"echo {bash_quote(stderr)} 1>&2")
    if artifact:
        lines.append(f'mkdir -p "$(dirname {bash_quote(artifact)})"')
        lines.append(f'echo {bash_quote(artifact_content)} > {bash_quote(artifact)}')
    if sleep and float(sleep) > 0:
        lines.append(f"sleep {float(sleep)}")
    lines.append(f"echo {bash_quote(stdout)}")
    lines.append(f"exit {int(exit_code)}")
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def bash_quote(s: str) -> str:
    """Single-quote a string for safe bash embedding."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


def set_fake_binary(monkeypatch, binary_path: str) -> None:
    """Override the configured claude CLI binary for the duration of a test."""
    monkeypatch.setenv("AEE_CLAUDE_CLI_BINARY", binary_path)


def post_executor(client, test_key: str, body: dict) -> "object":
    return client.post(
        "/runs/executor",
        json=body,
        headers={"Authorization": f"Bearer {test_key}"},
    )