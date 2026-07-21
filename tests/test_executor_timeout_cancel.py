"""Acceptance — timeout and cancel states.

* timeout: a run that exceeds ``timeout_sec`` returns ``status=timeout``,
  ``timeout_state=exceeded``. Tested both through the HTTP endpoint
  (fake ``sleep`` binary, short timeout) and directly on the runner.
* cancel: a run cancelled mid-flight returns ``status=cancelled``,
  ``cancel_state=requested``. Tested directly on the runner (the
  synchronous endpoint has no external cancel channel; the runner's
  ``cancel_event`` is the seam).
"""
from __future__ import annotations

import asyncio

import pytest

from tests._executor_test_helpers import (
    make_client,
    post_executor,
    set_fake_binary,
    write_fake_claude,
)


def test_timeout_via_endpoint(monkeypatch, tmp_path):
    """A sleep binary + short timeout -> status=timeout, timeout_state=exceeded."""
    binary = write_fake_claude(tmp_path, sleep=10, stdout="slow")
    set_fake_binary(monkeypatch, binary)
    client, app_module, key = make_client(monkeypatch, tmp_path)
    resp = post_executor(client, key, {
        "executor": "claude-code-cli",
        "prompt": "sleep a long time",
        "timeout_sec": 2,
    })
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["status"] == "timeout"
    assert data["timeout_state"] == "exceeded"
    assert data["selected_executor"] == "claude-code-cli"


def test_timeout_via_runner(monkeypatch, tmp_path):
    from aee.runtimes.executor_cli import ClaudeCodeCliRunner

    binary = write_fake_claude(tmp_path, sleep=10, stdout="slow", name="fake-claude-to")
    runner = ClaudeCodeCliRunner(binary=binary, max_turns=1)

    async def _go():
        return await runner.run(
            prompt="sleep", cwd="/home/ubuntu/Abacus",
            timeout_sec=1, expected_artifacts=[],
        )

    result = asyncio.run(_go())
    assert result.status == "timeout"
    assert result.timeout_state == "exceeded"


def test_cancel_via_runner(monkeypatch, tmp_path):
    from aee.runtimes.executor_cli import ClaudeCodeCliRunner

    binary = write_fake_claude(tmp_path, sleep=10, stdout="slow", name="fake-claude-cancel")
    runner = ClaudeCodeCliRunner(binary=binary, max_turns=1)

    async def _go():
        event = asyncio.Event()

        async def _set_after():
            await asyncio.sleep(0.3)
            event.set()

        asyncio.create_task(_set_after())
        return await runner.run(
            prompt="sleep", cwd="/home/ubuntu/Abacus",
            timeout_sec=None, cancel_event=event,
        )

    result = asyncio.run(_go())
    assert result.status == "cancelled"
    assert result.cancel_state == "requested"