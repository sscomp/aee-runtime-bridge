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
