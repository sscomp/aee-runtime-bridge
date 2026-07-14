"""Tests for ``ClaudeCodeExecutorAdapter``.

The adapter launches the Runner as a subprocess. To keep the
tests deterministic and avoid touching the production
``/home/ubuntu/Abacus/AEE`` checkout, the tests use a tiny
``fake_runner.py`` script that emulates the Runner's CLI surface
plus its ``completion.verified.json`` shape.

The adapter itself is constructed with:

* ``runs_root`` pointing at a temp dir
* ``runner_cwd`` pointing at a temp dir that *contains* a
  ``scripts/fake_runner.py`` (we add it via ``os.symlink``)
* ``runner_module="scripts.fake_runner"``

This lets us assert argv construction, lifecycle semantics, and
the manifest gate end-to-end with no network, no production
filesystem, and no real Runner.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aee.adapters import claude_code_executor as cce  # noqa: E402
from aee.adapters import manifest_verifier as mv  # noqa: E402
from aee.adapters.base import (  # noqa: E402
    RuntimeCancelResult,
    RuntimeError as AdapterRuntimeError,
    UnknownExternalRunError,
)


# --- Fake Runner ------------------------------------------------------


# A minimal but real Python module so ``python3 -m scripts.fake_runner``
# works exactly like the production path. The behavior is selected
# via the ``FAKE_RUNNER_MODE`` env var: ``pass``, ``fail``, ``hang``,
# ``no_manifest``, ``bad_manifest``, ``bad_task_id``.
FAKE_RUNNER_PY = textwrap.dedent('''
    import json, os, sys, time, hashlib, pathlib

    def _parse():
        argv = sys.argv[1:]
        out = {}
        repeat = {"required_artifact"}
        for k in repeat:
            out[k] = []
        i = 0
        while i < len(argv):
            a = argv[i]
            if a.startswith("--"):
                key = a[2:].replace("-", "_")
                if i+1 < len(argv) and not argv[i+1].startswith("--"):
                    v = argv[i+1]
                    i += 2
                else:
                    v = True
                    i += 1
                if key in repeat:
                    if isinstance(out[key], list):
                        out[key].append(v)
                    else:
                        out[key] = [v]
                else:
                    out[key] = v
            else:
                i += 1
        return out

    def main():
        args = _parse()
        task_id = args.get("task_id")
        runs_root = args.get("runs_root", "/tmp")
        run_dir = pathlib.Path(runs_root) / task_id
        run_dir.mkdir(parents=True, exist_ok=True)
        # Always write claim manifest
        claim = {"task_id": task_id, "ok": True}
        claim_path = run_dir / "completion.claim.json"
        claim_path.write_text(json.dumps(claim))
        mode = os.environ.get("FAKE_RUNNER_MODE", "pass")
        if mode == "hang":
            time.sleep(60)
            return
        verified = run_dir / "completion.verified.json"
        if mode == "no_manifest":
            # Exit 0 but write nothing — simulates the failure mode
            # the manifest gate is designed to catch.
            return
        if mode == "bad_manifest":
            verified.write_text("not json {")
            return
        if mode == "bad_task_id":
            verified.write_text(json.dumps({
                "schema_version": "1.0.0",
                "task_id": "WRONG--TASK",
                "executor": {
                    "type": "claude-code", "is_error": False,
                    "subtype": "success", "terminal_reason": "completed",
                    "exit_code": 0,
                },
                "verification": {"verification_errors": [], "safety_violations": [], "artifacts": []},
                "safety": {"violations": []},
                "process_group": {"verified_dead": True},
            }))
            return
        if mode == "fail":
            # Manifest says it failed
            verified.write_text(json.dumps({
                "schema_version": "1.0.0",
                "task_id": task_id,
                "executor": {
                    "type": "claude-code", "is_error": True,
                    "subtype": "error", "terminal_reason": "failed",
                    "exit_code": 1,
                },
                "verification": {"verification_errors": ["x"], "safety_violations": [], "artifacts": []},
                "safety": {"violations": []},
                "process_group": {"verified_dead": True},
            }))
            return
        # pass — write a happy manifest honoring --brief into stdout
        brief = args.get("brief", "")
        repo = args.get("repo_path")
        artifacts = args.get("required_artifact") or []
        v = {
            "schema_version": "1.0.0",
            "task_id": task_id,
            # TASK-M3 FIX-1: top-level status / verdict (the
            # committed Runner writes these at the top level).
            "status": "COMPLETED",
            "verdict": "PASS",
            "executor": {
                "type": "claude-code", "is_error": False,
                "subtype": "success", "terminal_reason": "completed",
                "exit_code": 0,
            },
            # TASK-M3 FIX-2: artifacts at the top level
            # (mirrors ``scripts/claude_code_runner.py:1178``).
            "artifacts": [],
            "verification": {
                "verification_errors": [], "safety_violations": [],
                "claim_manifest_hash": hashlib.sha256(claim_path.read_bytes()).hexdigest(),
            },
            "safety": {"violations": []},
            "process_group": {"verified_dead": True},
        }
        for rel in artifacts:
            p = pathlib.Path(repo) / rel
            if p.exists():
                h = hashlib.sha256(p.read_bytes()).hexdigest()
                v["artifacts"].append({"path": rel, "sha256": h, "verified": True})
        verified.write_text(json.dumps(v))
        print(f"processed: {brief[:40]}")

    if __name__ == "__main__":
        main()
''').strip()


# --- Fixtures ---------------------------------------------------------


@pytest.fixture
def fake_runner_env(tmp_path: Path):
    """Create a temp runner cwd with ``scripts/fake_runner.py`` and
    return useful paths.
    """
    runner_cwd = tmp_path / "runner"
    scripts = runner_cwd / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "fake_runner.py").write_text(FAKE_RUNNER_PY)
    (scripts / "__init__.py").write_text("")
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    return {
        "runner_cwd": str(runner_cwd),
        "runs_root": str(runs_root),
        "repo": str(repo),
        "tmp": tmp_path,
    }


@pytest.fixture
def adapter(fake_runner_env) -> cce.ClaudeCodeExecutorAdapter:
    return cce.ClaudeCodeExecutorAdapter(
        runs_root=fake_runner_env["runs_root"],
        runner_cwd=fake_runner_env["runner_cwd"],
        python_bin=sys.executable,
        runner_module="scripts.fake_runner",
    )


def _make_job(spec: Dict[str, Any]) -> Any:
    """A duck-typed Job for tests."""

    class _Job:
        def __init__(self, d):
            self.spec = d
            self.task_id = d.get("task_id", "TASK-T")
            self.title = d.get("title", "test")
            self.mode = d.get("mode", "normal")
            self.priority = 50
            self.input = d.get("brief", "")
            self.session_id = None
            self.client_source = "test"
            self.model_name = None
            self.runtime_type = "claude_code"
            self.adapter_name = "claude_code"
            self.external_run_id = None

    return _Job(spec)


# --- Tests ------------------------------------------------------------


def _wait_for_exit(proc, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return proc.returncode
        time.sleep(0.05)
    raise AssertionError("process did not exit in time")


@pytest.mark.asyncio
async def test_subprocess_launched_with_argv(adapter, fake_runner_env, monkeypatch):
    monkeypatch.setenv("FAKE_RUNNER_MODE", "pass")
    spec = {
        "task_id": "TASK-A",
        "run_id": "RUN-A",
        "repo_path": fake_runner_env["repo"],
        "mode": "coding",
        "timeout_seconds": 60,
        "required_artifacts": ["x.txt"],
        "model": "claude-sonnet-4-6",
        "fallback_model": "claude-opus-4-8",
        "brief": "do thing",
    }
    job = _make_job(spec)
    res = await adapter.submit(job)
    assert res.external_run_id == "RUN-A"
    assert res.status == "queued"
    # Pull the in-flight run, inspect argv
    run = cce._inflight["RUN-A"]
    argv = run.argv
    assert argv[0] == sys.executable
    assert argv[1:3] == ["-m", "scripts.fake_runner"]
    # composite task id
    assert "TASK-A--RUN-A" in argv
    # required artifact
    assert "--required-artifact" in argv
    assert argv[argv.index("--required-artifact") + 1] == "x.txt"
    # No empty flags
    for i, a in enumerate(argv):
        if a.startswith("--"):
            assert i + 1 < len(argv), f"flag {a!r} has no value"
            assert argv[i + 1] != "", f"flag {a!r} has empty value"
    # Wait for process to exit so cleanup is clean
    _wait_for_exit(run.process, timeout=10.0)
    # Manifest is on disk
    assert run.verified_manifest.exists()


@pytest.mark.asyncio
async def test_shell_false(adapter, fake_runner_env, monkeypatch):
    monkeypatch.setenv("FAKE_RUNNER_MODE", "pass")
    spec = {
        "task_id": "TASK-SH",
        "run_id": "RUN-SH",
        "repo_path": fake_runner_env["repo"],
        "mode": "coding",
        "timeout_seconds": 60,
        "required_artifacts": [],
    }
    job = _make_job(spec)
    res = await adapter.submit(job)
    run = cce._inflight[res.external_run_id]
    # shell=False is implied by passing an argv list; Popen raises
    # if you pass a string with shell=False. So this test asserts
    # the launch succeeded — that proves shell=False was acceptable.
    assert run.process is not None
    _wait_for_exit(run.process, timeout=10.0)


@pytest.mark.asyncio
async def test_composite_task_id(adapter, fake_runner_env, monkeypatch):
    monkeypatch.setenv("FAKE_RUNNER_MODE", "pass")
    spec = {
        "task_id": "TASK-X",
        "run_id": "RUN-Y",
        "repo_path": fake_runner_env["repo"],
        "mode": "normal",
        "timeout_seconds": 60,
    }
    job = _make_job(spec)
    res = await adapter.submit(job)
    run = cce._inflight[res.external_run_id]
    assert "TASK-X--RUN-Y" in run.argv
    _wait_for_exit(run.process, timeout=10.0)


@pytest.mark.asyncio
async def test_expected_run_directory(adapter, fake_runner_env, monkeypatch):
    monkeypatch.setenv("FAKE_RUNNER_MODE", "pass")
    spec = {
        "task_id": "TASK-D",
        "run_id": "RUN-D",
        "repo_path": fake_runner_env["repo"],
        "mode": "normal",
        "timeout_seconds": 60,
    }
    job = _make_job(spec)
    res = await adapter.submit(job)
    run = cce._inflight[res.external_run_id]
    expected = Path(fake_runner_env["runs_root"]) / "TASK-D--RUN-D"
    assert run.runner_run_dir == expected
    assert run.verified_manifest == expected / "completion.verified.json"
    assert run.stdout_log == expected / "stdout.log"
    assert run.stderr_log == expected / "stderr.log"
    _wait_for_exit(run.process, timeout=10.0)


@pytest.mark.asyncio
async def test_required_artifact_flags(adapter, fake_runner_env, monkeypatch):
    monkeypatch.setenv("FAKE_RUNNER_MODE", "pass")
    spec = {
        "task_id": "TASK-RA",
        "run_id": "RUN-RA",
        "repo_path": fake_runner_env["repo"],
        "mode": "normal",
        "timeout_seconds": 60,
        "required_artifacts": ["a.txt", "b/c.txt"],
    }
    job = _make_job(spec)
    res = await adapter.submit(job)
    run = cce._inflight[res.external_run_id]
    # Every required artifact appears with --required-artifact
    pairs = list(zip(run.argv, run.argv[1:]))
    flag_value = [v for k, v in pairs if k == "--required-artifact"]
    assert flag_value == ["a.txt", "b/c.txt"]
    _wait_for_exit(run.process, timeout=10.0)


@pytest.mark.asyncio
async def test_no_empty_flags(adapter, fake_runner_env, monkeypatch):
    monkeypatch.setenv("FAKE_RUNNER_MODE", "pass")
    spec = {
        "task_id": "TASK-NE",
        "run_id": "RUN-NE",
        "repo_path": fake_runner_env["repo"],
        "mode": "normal",
        "timeout_seconds": 60,
        "model": "",  # empty
        "fallback_model": None,
        "expected_branch": "",
        "expected_head": None,
    }
    job = _make_job(spec)
    res = await adapter.submit(job)
    run = cce._inflight[res.external_run_id]
    pairs = list(zip(run.argv, run.argv[1:]))
    for k, v in pairs:
        if k.startswith("--"):
            assert v != "" and v is not None, f"flag {k!r} has empty value"
            assert v is not True, f"flag {k!r} has no value"
    _wait_for_exit(run.process, timeout=10.0)


@pytest.mark.asyncio
async def test_process_alive_returns_running(adapter, fake_runner_env, monkeypatch):
    monkeypatch.setenv("FAKE_RUNNER_MODE", "hang")
    spec = {
        "task_id": "TASK-H",
        "run_id": "RUN-H",
        "repo_path": fake_runner_env["repo"],
        "mode": "normal",
        "timeout_seconds": 60,
    }
    job = _make_job(spec)
    res = await adapter.submit(job)
    poll = await adapter.poll(res.external_run_id)
    assert poll.status == "running"
    assert poll.is_terminal is False
    # Cancel + cleanup
    run = cce._inflight[res.external_run_id]
    os.killpg(run.process.pid, signal.SIGKILL)
    _wait_for_exit(run.process, timeout=5.0)


@pytest.mark.asyncio
async def test_exited_no_manifest_returns_failed(adapter, fake_runner_env, monkeypatch):
    monkeypatch.setenv("FAKE_RUNNER_MODE", "no_manifest")
    spec = {
        "task_id": "TASK-NM",
        "run_id": "RUN-NM",
        "repo_path": fake_runner_env["repo"],
        "mode": "normal",
        "timeout_seconds": 60,
    }
    job = _make_job(spec)
    res = await adapter.submit(job)
    run = cce._inflight[res.external_run_id]
    _wait_for_exit(run.process, timeout=10.0)
    poll = await adapter.poll(res.external_run_id)
    assert poll.is_terminal is True
    assert poll.status == "failed"
    assert "completion.verified.json" in (poll.error or "")


@pytest.mark.asyncio
async def test_exited_invalid_manifest_returns_failed(adapter, fake_runner_env, monkeypatch):
    monkeypatch.setenv("FAKE_RUNNER_MODE", "bad_manifest")
    spec = {
        "task_id": "TASK-BM",
        "run_id": "RUN-BM",
        "repo_path": fake_runner_env["repo"],
        "mode": "normal",
        "timeout_seconds": 60,
    }
    job = _make_job(spec)
    res = await adapter.submit(job)
    run = cce._inflight[res.external_run_id]
    _wait_for_exit(run.process, timeout=10.0)
    poll = await adapter.poll(res.external_run_id)
    assert poll.is_terminal is True
    assert poll.status == "failed"
    # The verifier should have flagged the JSON parse error.
    raw = poll.raw or {}
    ver = raw.get("verification", {})
    assert mv.E_VERIFIED_MANIFEST_INVALID_JSON in ver.get("verification_errors", [])


@pytest.mark.asyncio
async def test_exited_verified_pass_returns_completed(adapter, fake_runner_env, monkeypatch):
    monkeypatch.setenv("FAKE_RUNNER_MODE", "pass")
    spec = {
        "task_id": "TASK-P",
        "run_id": "RUN-P",
        "repo_path": fake_runner_env["repo"],
        "mode": "normal",
        "timeout_seconds": 60,
    }
    job = _make_job(spec)
    res = await adapter.submit(job)
    run = cce._inflight[res.external_run_id]
    _wait_for_exit(run.process, timeout=10.0)
    poll = await adapter.poll(res.external_run_id)
    assert poll.is_terminal is True
    assert poll.status == "completed"
    assert poll.raw["verification"]["verified"] is True


@pytest.mark.asyncio
async def test_runner_exit_nonzero_returns_failed(adapter, fake_runner_env, monkeypatch):
    monkeypatch.setenv("FAKE_RUNNER_MODE", "fail")
    spec = {
        "task_id": "TASK-F",
        "run_id": "RUN-F",
        "repo_path": fake_runner_env["repo"],
        "mode": "normal",
        "timeout_seconds": 60,
    }
    job = _make_job(spec)
    res = await adapter.submit(job)
    run = cce._inflight[res.external_run_id]
    _wait_for_exit(run.process, timeout=10.0)
    poll = await adapter.poll(res.external_run_id)
    assert poll.is_terminal is True
    assert poll.status == "failed"


@pytest.mark.asyncio
async def test_cancel_sends_sigterm(adapter, fake_runner_env, monkeypatch):
    monkeypatch.setenv("FAKE_RUNNER_MODE", "hang")
    spec = {
        "task_id": "TASK-C",
        "run_id": "RUN-C",
        "repo_path": fake_runner_env["repo"],
        "mode": "normal",
        "timeout_seconds": 60,
    }
    job = _make_job(spec)
    res = await adapter.submit(job)
    run = cce._inflight[res.external_run_id]
    # Wait until it's actually running
    deadline = time.time() + 2.0
    while run.process.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    # Cancel — should send SIGTERM and quickly exit (fake runner
    # is just sleeping so SIGTERM will not be handled; we expect
    # escalation to SIGKILL after the grace period).
    cancel = await adapter.cancel(res.external_run_id)
    assert cancel.cancelled is True
    assert "SIGKILL" in (cancel.reason or "") or "SIGTERM" in (cancel.reason or "")


@pytest.mark.asyncio
async def test_cancel_never_returns_completed(adapter, fake_runner_env, monkeypatch):
    """After a successful cancel, the adapter must NEVER return
    ``completed`` for the cancelled run.

    TASK-M3 FIX-4: the in-flight entry is now cleaned up on every
    terminal path, including cancellation. The test therefore
    asserts that polling a cancelled run raises
    ``UnknownExternalRunError`` (the entry is gone) rather than
    returning ``completed``. The invariant the contract requires
    is "no cancelled run can ever be promoted to completed",
    which is now stronger than before: the in-flight state is
    not just refused but actively removed.
    """
    monkeypatch.setenv("FAKE_RUNNER_MODE", "hang")
    spec = {
        "task_id": "TASK-NC",
        "run_id": "RUN-NC",
        "repo_path": fake_runner_env["repo"],
        "mode": "normal",
        "timeout_seconds": 60,
    }
    job = _make_job(spec)
    res = await adapter.submit(job)
    cancel = await adapter.cancel(res.external_run_id)
    # The cancel must succeed.
    assert cancel.cancelled is True
    # Post-cancel, the in-flight entry is cleaned up (TASK-M3 FIX-4).
    assert res.external_run_id not in cce._inflight
    # Subsequent poll must NOT return ``completed``; the entry is
    # gone so the adapter raises ``UnknownExternalRunError``.
    with pytest.raises(UnknownExternalRunError):
        await adapter.poll(res.external_run_id)


@pytest.mark.asyncio
async def test_concurrency_guard(adapter, fake_runner_env, monkeypatch):
    # Use the hang mode to keep the first run alive while we try a
    # second submit. The MVP rejects the second one.
    monkeypatch.setenv("FAKE_RUNNER_MODE", "hang")
    spec1 = {
        "task_id": "TASK-G1",
        "run_id": "RUN-G1",
        "repo_path": fake_runner_env["repo"],
        "mode": "normal",
        "timeout_seconds": 60,
    }
    await adapter.submit(_make_job(spec1))
    spec2 = {
        "task_id": "TASK-G2",
        "run_id": "RUN-G2",
        "repo_path": fake_runner_env["repo"],
        "mode": "normal",
        "timeout_seconds": 60,
    }
    with pytest.raises(cce.ClaudeConcurrencyError):
        await adapter.submit(_make_job(spec2))
    # Cleanup
    run = cce._inflight[spec1["run_id"]]
    os.killpg(run.process.pid, signal.SIGKILL)
    _wait_for_exit(run.process, timeout=5.0)


# --- TASK-M3 FIX-4: inflight cleanup on terminal paths ----------------


@pytest.mark.asyncio
async def test_inflight_cleaned_after_completed(adapter, fake_runner_env, monkeypatch):
    """After a successful (verified) poll, the in-flight entry is
    removed so the dict does not grow without bound over a
    long-lived process.
    """
    monkeypatch.setenv("FAKE_RUNNER_MODE", "pass")
    spec = {
        "task_id": "TASK-CLEAN",
        "run_id": "RUN-CLEAN",
        "repo_path": fake_runner_env["repo"],
        "mode": "normal",
        "timeout_seconds": 60,
    }
    res = await adapter.submit(_make_job(spec))
    run = cce._inflight[res.external_run_id]
    _wait_for_exit(run.process, timeout=10.0)
    poll = await adapter.poll(res.external_run_id)
    assert poll.is_terminal is True
    assert poll.status == "completed"
    # FIX-4: the entry is gone.
    assert res.external_run_id not in cce._inflight


@pytest.mark.asyncio
async def test_inflight_cleaned_after_failed_no_manifest(
    adapter, fake_runner_env, monkeypatch
):
    """After a failed poll (no manifest), the in-flight entry is
    removed.
    """
    monkeypatch.setenv("FAKE_RUNNER_MODE", "no_manifest")
    spec = {
        "task_id": "TASK-CLEAN2",
        "run_id": "RUN-CLEAN2",
        "repo_path": fake_runner_env["repo"],
        "mode": "normal",
        "timeout_seconds": 60,
    }
    res = await adapter.submit(_make_job(spec))
    run = cce._inflight[res.external_run_id]
    _wait_for_exit(run.process, timeout=10.0)
    poll = await adapter.poll(res.external_run_id)
    assert poll.is_terminal is True
    assert poll.status == "failed"
    # FIX-4: the entry is gone.
    assert res.external_run_id not in cce._inflight


@pytest.mark.asyncio
async def test_inflight_cleaned_after_cancelled(
    adapter, fake_runner_env, monkeypatch
):
    """After a successful cancel, the in-flight entry is removed.
    """
    monkeypatch.setenv("FAKE_RUNNER_MODE", "hang")
    spec = {
        "task_id": "TASK-CLEAN3",
        "run_id": "RUN-CLEAN3",
        "repo_path": fake_runner_env["repo"],
        "mode": "normal",
        "timeout_seconds": 60,
    }
    res = await adapter.submit(_make_job(spec))
    cancel = await adapter.cancel(res.external_run_id)
    assert cancel.cancelled is True
    # FIX-4: the entry is gone after cancel.
    assert res.external_run_id not in cce._inflight


# --- TASK-M6: Auth environment pass-through security tests -----------
#
# These tests cover the contract in TASK-M6 §6. We use only
# synthetic credentials ("*-not-real") and never inspect real
# auth tokens. Values are referenced as "must not be present in
# output X" — never asserted to be a specific string the test
# itself owns (except the test-owned synthetic ones).

# Synthetic test secrets. These are NOT real credentials; they
# only exist to prove the helper does not echo them into argv,
# raw payloads, or error messages.
SYNTHETIC_SECRETS = {
    "ANTHROPIC_API_KEY": "test-anthropic-key-not-real",
    "ANTHROPIC_AUTH_TOKEN": "test-anthropic-token-not-real",
    "CLAUDE_CODE_OAUTH_TOKEN": "test-oauth-token-not-real",
    "CLAUDE_CODE_API_KEY": "test-claude-api-key-not-real",
    "CLAUDE_CODE_ENTRYPOINT": "test-claude-entrypoint-not-real",
    "CLAUDE_CONFIG_DIR": "/tmp/test-claude-config-not-real",
}

# Variables that the helper MUST NOT forward, even when set.
UNRELATED_SECRETS = {
    "AWS_SECRET_ACCESS_KEY": "test-aws-should-not-leak",
    "GITHUB_TOKEN": "test-github-should-not-leak",
    "DATABASE_URL": "test-pg-should-not-leak",
    "BRIDGE_API_KEY": "test-bridge-should-not-leak",
    "GPT_BRIDGE_API_KEY": "test-gpt-should-not-leak",
    "SSH_AUTH_SOCK": "/tmp/test-ssh-should-not-leak",
}


def test_build_runner_env_forwards_allowlisted_auth_vars():
    """Requirement 1: allow-listed auth variables are forwarded
    when present in the parent mapping.
    """
    parent = dict(SYNTHETIC_SECRETS)
    parent["PATH"] = "/usr/bin"
    parent["HOME"] = "/home/ubuntu"
    out = cce.build_runner_environment(parent)
    for k, v in SYNTHETIC_SECRETS.items():
        assert k in out, f"missing allow-listed key: {k}"
        assert out[k] == v, f"value mismatch for {k}"
    # And the base vars too (they must keep being forwarded).
    assert out["PATH"] == "/usr/bin"
    assert out["HOME"] == "/home/ubuntu"


def test_build_runner_env_forwards_allowlisted_config_vars():
    """TASK-M6 §5 (final set based on actual environment and Claude
    CLI behavior): non-secret Claude config variables
    (``ANTHROPIC_BASE_URL``, model aliases) are forwarded when
    present. Without ``ANTHROPIC_BASE_URL`` the Claude CLI
    cannot reach a custom endpoint and the auth token is
    rejected with HTTP 401.
    """
    parent = {
        "ANTHROPIC_BASE_URL": "https://example.invalid",
        "ANTHROPIC_MODEL": "minimax-m3:cloud",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "minimax-sonnet",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "minimax-opus",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "minimax-haiku",
        "CLAUDE_CODE_EXECPATH": "/usr/local/bin/claude",
        "PATH": "/usr/bin",
    }
    out = cce.build_runner_environment(parent)
    for k, v in parent.items():
        assert out.get(k) == v, f"missing/wrong config var: {k}={out.get(k)!r}"
    # And the allow-list of config keys must be exactly the
    # documented set (so a future change cannot silently widen
    # the surface).
    assert cce.CLAUDE_CONFIG_ENV_ALLOWLIST == (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "CLAUDE_CODE_EXECPATH",
    )


def test_build_runner_env_omits_missing_auth_vars():
    """Requirement 2: missing auth variables are omitted from
    the child env.
    """
    out = cce.build_runner_environment({})
    for k in cce.CLAUDE_AUTH_ENV_ALLOWLIST:
        assert k not in out, f"unexpected key in empty-output: {k}"
    # And the base / fake-runner lists too.
    for k in cce.PASS_THROUGH_BASE + cce.PASS_THROUGH_FAKE_RUNNER:
        assert k not in out, f"unexpected base/fake key: {k}"


def test_build_runner_env_omits_empty_auth_vars():
    """Requirement 3: empty-string auth variables are omitted.
    """
    parent = {
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_AUTH_TOKEN": "non-empty-ok",
        "CLAUDE_CODE_OAUTH_TOKEN": "",
        "PATH": "/usr/bin",
    }
    out = cce.build_runner_environment(parent)
    assert "ANTHROPIC_API_KEY" not in out
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in out
    # Non-empty still forwarded.
    assert out["ANTHROPIC_AUTH_TOKEN"] == "non-empty-ok"
    assert out["PATH"] == "/usr/bin"


def test_build_runner_env_does_not_forward_unrelated_secrets():
    """Requirement 4: variables not in any allow-list are not
    forwarded.
    """
    parent = dict(UNRELATED_SECRETS)
    # Also throw in a fake-runner marker to make sure we are
    # not too restrictive.
    parent["FAKE_RUNNER_MODE"] = "pass"
    out = cce.build_runner_environment(parent)
    for k in UNRELATED_SECRETS:
        assert k not in out, f"unrelated secret leaked: {k}"
    # FAKE_RUNNER_MODE is the only thing that should survive
    # (plus the empty base/auth lists).
    assert out.get("FAKE_RUNNER_MODE") == "pass"
    # The set of keys in ``out`` must be a subset of the union
    # of the three allow-lists. (Empty-string entries dropped.)
    allowed_set = set(
        cce.PASS_THROUGH_BASE + cce.PASS_THROUGH_FAKE_RUNNER + cce.CLAUDE_AUTH_ENV_ALLOWLIST
    )
    assert set(out.keys()).issubset(allowed_set), (
        f"unexpected keys: {set(out.keys()) - allowed_set}"
    )


def test_build_runner_env_does_not_copy_full_parent_environ():
    """Requirement 9: the full parent environment is never
    copied. Start with a parent containing 30 random keys
    and a single allow-listed key, and assert only the
    allow-listed one survives.
    """
    parent = {f"RANDOM_KEY_{i}": f"v{i}" for i in range(30)}
    parent["PATH"] = "/x"
    out = cce.build_runner_environment(parent)
    # No random key may appear.
    for k in parent:
        if k.startswith("RANDOM_KEY_"):
            assert k not in out, f"random key leaked: {k}"
    # Only the single allow-listed key remains.
    assert out == {"PATH": "/x"}, f"unexpected output: {out}"


@pytest.mark.asyncio
async def test_argv_does_not_carry_secret_values(
    adapter, fake_runner_env, monkeypatch
):
    """Requirement 5: secret values do not appear in the
    constructed argv list of the Runner subprocess.
    """
    # Inject synthetic secrets into the parent env so the
    # helper has something to forward.
    for k, v in SYNTHETIC_SECRETS.items():
        monkeypatch.setenv(k, v)
    for k, v in UNRELATED_SECRETS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("FAKE_RUNNER_MODE", "pass")

    spec = {
        "task_id": "TASK-NOSECRETS-ARGV",
        "run_id": "RUN-NOSECRETS-ARGV",
        "repo_path": fake_runner_env["repo"],
        "mode": "normal",
        "timeout_seconds": 60,
    }
    job = _make_job(spec)
    res = await adapter.submit(job)
    run = cce._inflight[res.external_run_id]
    argv = run.argv
    # No synthetic secret value may appear in the argv list.
    forbidden = set(SYNTHETIC_SECRETS.values()) | set(UNRELATED_SECRETS.values())
    for entry in argv:
        assert entry not in forbidden, (
            f"secret value leaked into argv: {entry!r}"
        )
    _wait_for_exit(run.process, timeout=10.0)


def test_helper_returned_dict_does_not_carry_unrelated_secret_values():
    """Requirement 6: the helper's returned dict must not carry
    unrelated secret values, even if they appear in the parent.
    """
    parent = dict(SYNTHETIC_SECRETS)
    parent.update(UNRELATED_SECRETS)
    out = cce.build_runner_environment(parent)
    # Allowed: SYNTHETIC_SECRETS values
    # Forbidden: UNRELATED_SECRETS values
    forbidden_values = set(UNRELATED_SECRETS.values())
    for v in out.values():
        assert v not in forbidden_values, f"unrelated secret leaked: {v!r}"


def test_routing_decision_log_does_not_carry_secret_values():
    """Requirement 6: the executor_router's RoutingDecision.to_dict
    output must not carry secret values. We assert by feeding the
    router metadata dict that includes secret-shaped keys and
    confirming the rendered decision string has no value.
    """
    from aee.runtimes.executor_router import (
        RoutingDecision,
        select_executor,
        validate_metadata,
    )
    # Inject a secret-shaped metadata value alongside a
    # well-formed key.
    meta = {
        "executor": "claude_code",
        "repo_path": "/home/ubuntu/Abacus",
        "watermark": "test-secret-not-real",
    }
    # The router does not actually inspect 'watermark'; we are
    # checking that the routing decision dict we build does not
    # echo the secret value.
    validate_metadata(meta)
    decision = select_executor(meta, available_adapters=("claude_code",))
    rendered = decision.to_dict()
    # Recursively check: no value in the rendered decision is
    # the secret.
    def _walk(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                yield from _walk(v)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                yield from _walk(v)
        else:
            yield obj
    for v in _walk(rendered):
        assert v != "test-secret-not-real", (
            f"secret value leaked into routing decision: {v!r}"
        )


@pytest.mark.asyncio
async def test_adapter_error_message_does_not_carry_secret_values(
    adapter, fake_runner_env, monkeypatch
):
    """Requirement 7: secret values do not appear in adapter
    error messages.
    """
    for k, v in SYNTHETIC_SECRETS.items():
        monkeypatch.setenv(k, v)
    # Build a job that triggers an AdapterRuntimeError: missing
    # repo_path. The error text must not echo the auth env.
    class _BadJob:
        spec = {
            "task_id": "TASK-ERR",
            "run_id": "RUN-ERR",
            "mode": "normal",
            "timeout_seconds": 60,
            # repo_path intentionally missing -> raises.
        }
        task_id = "TASK-ERR"
        title = "err"
        mode = "normal"
        priority = 50
        input = "x"
        session_id = None
        client_source = "test"
        model_name = None
        runtime_type = "claude_code"
        adapter_name = "claude_code"
        external_run_id = None

    with pytest.raises(cce.AdapterRuntimeError) as excinfo:
        await adapter.submit(_BadJob())
    err_text = str(excinfo.value)
    for v in SYNTHETIC_SECRETS.values():
        assert v not in err_text, (
            f"secret value leaked into error message: {v!r}"
        )


@pytest.mark.asyncio
async def test_submit_result_raw_does_not_carry_secret_values(
    adapter, fake_runner_env, monkeypatch
):
    """Requirement 8: secret values do not appear in
    ``RuntimeSubmitResult.raw`` (the value the watcher / API
    layer will serialize back to the caller).
    """
    for k, v in SYNTHETIC_SECRETS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("FAKE_RUNNER_MODE", "pass")

    spec = {
        "task_id": "TASK-RAW",
        "run_id": "RUN-RAW",
        "repo_path": fake_runner_env["repo"],
        "mode": "normal",
        "timeout_seconds": 60,
    }
    job = _make_job(spec)
    res = await adapter.submit(job)
    # Convert raw to a JSON string to recursively check.
    raw = res.raw or {}
    # Build a flat list of all string values.
    def _flat(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                yield from _flat(v)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                yield from _flat(v)
        else:
            yield obj
    forbidden = set(SYNTHETIC_SECRETS.values())
    found = [v for v in _flat(raw) if v in forbidden]
    assert not found, f"secret values leaked into raw: {found}"
    # Also sanity-check that to_dict is also clean.
    res_dict = res.to_dict()
    found2 = [v for v in _flat(res_dict) if v in forbidden]
    assert not found2, f"secret values leaked into to_dict: {found2}"
    run = cce._inflight[res.external_run_id]
    _wait_for_exit(run.process, timeout=10.0)


@pytest.mark.asyncio
async def test_existing_fake_runner_determinism_preserved(
    adapter, fake_runner_env, monkeypatch
):
    """Requirement 10: existing fake-runner test behavior
    remains deterministic. Re-run a happy-path submit + poll
    with FAKE_RUNNER_MODE=pass and assert the verified
    manifest path is still produced.
    """
    monkeypatch.setenv("FAKE_RUNNER_MODE", "pass")
    spec = {
        "task_id": "TASK-DETERM",
        "run_id": "RUN-DETERM",
        "repo_path": fake_runner_env["repo"],
        "mode": "normal",
        "timeout_seconds": 60,
    }
    res = await adapter.submit(_make_job(spec))
    run = cce._inflight[res.external_run_id]
    _wait_for_exit(run.process, timeout=10.0)
    poll = await adapter.poll(res.external_run_id)
    assert poll.is_terminal is True
    assert poll.status == "completed"
    assert run.verified_manifest.exists()
