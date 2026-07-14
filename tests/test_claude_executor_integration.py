"""End-to-end integration test for the Claude Code Executor + Manifest
Gate chain.

This test exercises the full path **without** the FastAPI layer:

    adapter.submit
        -> Popen(fake_runner, …)
        -> fake_runner writes claim + verified manifests
    adapter.poll (waits for exit)
        -> verifies manifest
        -> returns is_terminal=True, status="completed"
    watcher._poll_one (gate)
        -> confirms adapter's verification
        -> manager.complete is called
        -> manager.fail is NOT called

We use a tiny in-memory ``FakeManager`` to stand in for
``dispatcher.manager.TaskManager`` so we can assert exactly which
methods were called.

The fake Runner is the same one used in
``tests/test_claude_code_executor.py``. We do not touch
``/home/ubuntu/Abacus/AEE`` or ``/home/ubuntu/Abacus/AEE-RUNS``;
all artifacts live under ``/tmp``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aee.adapters import claude_code_executor as cce  # noqa: E402


# --- Fake Manager -----------------------------------------------------


class FakeManager:
    """In-memory substitute for ``dispatcher.manager.TaskManager``.

    We only need to count ``complete`` vs ``fail`` calls and
    capture the args, so this is intentionally tiny.
    """

    def __init__(self) -> None:
        self.complete_calls: List[Dict[str, Any]] = []
        self.fail_calls: List[Dict[str, Any]] = []
        self.cancel_calls: List[Dict[str, Any]] = []
        self.timeout_calls: List[Dict[str, Any]] = []
        self.warning_calls: List[Dict[str, Any]] = []
        self.log_calls: List[Dict[str, Any]] = []

    def complete(self, task_id, **kw):
        self.complete_calls.append({"task_id": task_id, **kw})
        return None

    def fail(self, task_id, error):
        self.fail_calls.append({"task_id": task_id, "error": error})
        return None

    def cancel(self, task_id):
        self.cancel_calls.append({"task_id": task_id})
        return None

    def timeout(self, task_id, reason):
        self.timeout_calls.append({"task_id": task_id, "reason": reason})
        return None

    def warning(self, task_id, message):
        self.warning_calls.append({"task_id": task_id, "message": message})
        return None

    def log(self, task_id, line):
        self.log_calls.append({"task_id": task_id, "line": line})
        return None


# --- Fake Task (duck-typed for watcher) -------------------------------


class FakeTask:
    def __init__(
        self,
        task_id: str,
        adapter_name: str,
        status: str = "running",
        external_run_id: str = "RUN-X",
    ) -> None:
        self.task_id = task_id
        self.adapter_name = adapter_name
        self.status = status
        self.progress_pct = 0
        self.external_run_id = external_run_id
        self.hermes_run_id = external_run_id
        self.title = "integration test"
        self.type = "ops"
        self.mode = "normal"
        self.priority = 50
        self.runtime_type = adapter_name
        self.input_text = ""


# --- Watcher import (use the real Watcher but with a tiny shim) ------


def _import_watcher():
    """Import ``dispatcher.watcher`` from the bridge root."""
    import importlib

    spec = importlib.util.spec_from_file_location(
        "_aee_dispatcher_watcher", _ROOT / "dispatcher" / "watcher.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- Fake runner (production-style) ----------------------------------


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
        claim = {"task_id": task_id, "ok": True}
        claim_path = run_dir / "completion.claim.json"
        claim_path.write_text(json.dumps(claim))
        repo = args.get("repo_path")
        artifacts = args.get("required_artifact") or []
        v = {
            "schema_version": "1.0.0",
            "task_id": task_id,
            # TASK-M3 FIX-1: top-level status / verdict.
            "status": "COMPLETED",
            "verdict": "PASS",
            "executor": {
                "type": "claude-code", "is_error": False,
                "subtype": "success", "terminal_reason": "completed",
                "exit_code": 0,
            },
            # TASK-M3 FIX-2: artifacts at the top level (mirrors
            # ``scripts/claude_code_runner.py:1178``).
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
            if p.exists() and p.is_file():
                h = hashlib.sha256(p.read_bytes()).hexdigest()
                v["artifacts"].append({"path": rel, "sha256": h, "verified": True})
        (run_dir / "completion.verified.json").write_text(json.dumps(v))
        print(f"done: {task_id}")

    if __name__ == "__main__":
        main()
''').strip()


# --- Fixtures ---------------------------------------------------------


@pytest.fixture
def integration_env(tmp_path: Path):
    runner_cwd = tmp_path / "runner"
    scripts = runner_cwd / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "fake_runner.py").write_text(FAKE_RUNNER_PY)
    (scripts / "__init__.py").write_text("")
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    artifact_rel = "out/result.txt"
    artifact_abs = repo / artifact_rel
    artifact_abs.parent.mkdir(parents=True)
    artifact_abs.write_bytes(b"integration-ok")
    return {
        "runner_cwd": str(runner_cwd),
        "runs_root": str(runs_root),
        "repo": str(repo),
        "artifact_rel": artifact_rel,
        "tmp": tmp_path,
    }


def _make_job(spec: Dict[str, Any]) -> Any:
    class _Job:
        def __init__(self, d):
            self.spec = d
            self.task_id = d.get("task_id", "TASK-T")
            self.title = "integration"
            self.mode = "normal"
            self.priority = 50
            self.input = d.get("brief", "")
            self.session_id = None
            self.client_source = "integration"
            self.model_name = None
            self.runtime_type = "claude_code"
            self.adapter_name = "claude_code"
            self.external_run_id = None
    return _Job(spec)


def _wait_for_exit(proc, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return proc.returncode
        time.sleep(0.05)
    raise AssertionError("process did not exit in time")


# --- Tests ------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_happy_path(integration_env):
    adapter = cce.ClaudeCodeExecutorAdapter(
        runs_root=integration_env["runs_root"],
        runner_cwd=integration_env["runner_cwd"],
        python_bin=sys.executable,
        runner_module="scripts.fake_runner",
    )
    spec = {
        "task_id": "TASK-INT",
        "repo_path": integration_env["repo"],
        "mode": "normal",
        "timeout_seconds": 60,
        "required_artifacts": [integration_env["artifact_rel"]],
        "brief": "integration smoke",
    }
    res = await adapter.submit(_make_job(spec))
    run = cce._inflight[res.external_run_id]
    _wait_for_exit(run.process, timeout=15.0)
    poll = await adapter.poll(res.external_run_id)
    # Adapter says completed + terminal
    assert poll.is_terminal is True
    assert poll.status == "completed"
    # Re-verify the manifest independently to prove the gate is
    # not just trusting the adapter's claim.
    from aee.adapters.manifest_verifier import verify_completion_manifest
    ver = verify_completion_manifest(
        verified_manifest_path=run.verified_manifest,
        expected_task_id=run.runner_task_id,
        expected_run_dir=run.runner_run_dir,
        repo_path=run.repo_path,
        required_artifacts=run.required_artifacts,
        subprocess_exit_code=0,
    )
    assert ver.verified is True
    # Drive the watcher gate
    watcher = _import_watcher()
    t = FakeTask(
        task_id="TASK-INT",
        adapter_name="claude_code",
        status="running",
        external_run_id=res.external_run_id,
    )
    fm = FakeManager()
    # Replicate the relevant branch of watcher._poll_one by
    # calling the gate helper directly + the manager shim.
    ok, err = watcher._claude_code_completion_gate(t, poll.raw)
    assert ok is True, f"gate rejected: {err}"
    fm.complete(t.task_id, output_text=poll.output, raw=poll.raw)
    assert len(fm.complete_calls) == 1
    assert len(fm.fail_calls) == 0


@pytest.mark.asyncio
async def test_e2e_missing_manifest_blocks_completion(integration_env):
    """The manifest gate must prevent ``completed`` when the
    adapter reports ``completed`` but the manifest is missing.

    We simulate this by feeding a synthetic raw payload to the
    watcher gate (mimicking what an adapter would send if it
    somehow bypassed its own verifier).
    """
    watcher = _import_watcher()
    t = FakeTask(
        task_id="TASK-BAD",
        adapter_name="claude_code",
        status="running",
    )
    # Synthetic raw: status=completed but no verification block.
    fm = FakeManager()
    ok, err = watcher._claude_code_completion_gate(t, raw=None)
    assert ok is False
    fm.fail(t.task_id, f"manifest_missing_or_subprocess_failed: {err}")
    # The actual watcher code path: with our gate, the manager
    # would receive ``fail`` (not ``complete``).
    assert len(fm.fail_calls) == 1
    assert len(fm.complete_calls) == 0


@pytest.mark.asyncio
async def test_e2e_verification_failed_blocks_completion(integration_env):
    watcher = _import_watcher()
    t = FakeTask(
        task_id="TASK-VFAIL",
        adapter_name="claude_code",
        status="running",
    )
    fm = FakeManager()
    # Raw indicates verification failed (e.g. exit code mismatch).
    raw = {
        "verification": {
            "verified": False,
            "verification_errors": ["runner_exit_code_mismatch"],
        },
        "verified_manifest": str(integration_env["runs_root"]) + "/x/y",
    }
    ok, err = watcher._claude_code_completion_gate(t, raw=raw)
    assert ok is False
    assert "runner_exit_code_mismatch" in err
    fm.fail(t.task_id, f"manifest_missing_or_subprocess_failed: {err}")
    assert len(fm.fail_calls) == 1
    assert len(fm.complete_calls) == 0


@pytest.mark.asyncio
async def test_e2e_watcher_poll_one_calls_complete_for_verified(
    integration_env,
):
    """Behavioral FIX-6: drive ``watcher._poll_one`` end-to-end.

    Replaces the prior ``inspect.getsource`` test. We construct a
    real :class:`dispatcher.watcher.Watcher`, swap its
    ``_manager`` for a :class:`FakeManager`, register a fake
    adapter that returns a verified-completed ``RuntimePollResult``,
    and assert that ``manager.complete`` is called (and
    ``manager.fail`` is NOT) when the watcher polls a Claude Code
    task.
    """
    from aee.core.registry import adapter_registry as _reg
    from aee.adapters.base import RuntimeAdapter, RuntimePollResult

    # Build a real Watcher, then swap its manager for a fake so we
    # can record calls without a database.
    watcher = _import_watcher().Watcher(tick_sec=0.01)
    fm = FakeManager()
    watcher._manager = fm  # type: ignore[assignment]

    # Register a fake adapter that returns "completed + verified"
    # raw. The watcher gates on ``adapter_name == "claude_code"``,
    # so we register the fake under exactly that name. We save and
    # restore the real registry afterwards so other tests are
    # unaffected.
    class _FakeAdapter(RuntimeAdapter):
        name = "claude_code"
        runtime_type = "claude_code"

        def __init__(self) -> None:
            self.poll_calls = 0

        async def submit(self, job):  # pragma: no cover - unused
            raise NotImplementedError

        async def poll(self, external_run_id):
            self.poll_calls += 1
            return RuntimePollResult(
                external_run_id=external_run_id,
                status="completed",
                is_terminal=True,
                output="fake output",
                raw={
                    "verification": {
                        "verified": True,
                        "verification_errors": [],
                    },
                    "verified_manifest": str(
                        Path(integration_env["runs_root"]) / "x" / "verified.json"
                    ),
                },
            )

        async def cancel(self, external_run_id):  # pragma: no cover - unused
            return None

    fake = _FakeAdapter()
    # Save and restore the real registry.
    saved = dict(_reg._adapters)
    try:
        _reg.register(fake, replace=True)
        t = FakeTask(
            task_id="TASK-WATCHER-OK",
            adapter_name="claude_code",
            status="running",
            external_run_id="RUN-WATCHER-OK",
        )
        await watcher._poll_one(t, t.external_run_id)
        assert fake.poll_calls == 1, "fake adapter.poll was not called"
        assert len(fm.complete_calls) == 1, (
            f"manager.complete was not called; got {fm.complete_calls}"
        )
        assert len(fm.fail_calls) == 0
        # The complete call should reference our task and raw.
        assert fm.complete_calls[0]["task_id"] == "TASK-WATCHER-OK"
    finally:
        # Restore the registry so other tests see the original set.
        _reg._adapters.clear()
        _reg._adapters.update(saved)


@pytest.mark.asyncio
async def test_e2e_watcher_poll_one_calls_fail_for_unverified(
    integration_env,
):
    """Behavioral FIX-6: ``_poll_one`` must call ``manager.fail``
    when the adapter's raw indicates the manifest gate failed
    (the second defense-in-depth line in the watcher)."""
    from aee.core.registry import adapter_registry as _reg
    from aee.adapters.base import RuntimeAdapter, RuntimePollResult

    watcher = _import_watcher().Watcher(tick_sec=0.01)
    fm = FakeManager()
    watcher._manager = fm  # type: ignore[assignment]

    class _BadAdapter(RuntimeAdapter):
        name = "claude_code"
        runtime_type = "claude_code"

        def __init__(self) -> None:
            self.poll_calls = 0

        async def submit(self, job):  # pragma: no cover - unused
            raise NotImplementedError

        async def poll(self, external_run_id):
            self.poll_calls += 1
            return RuntimePollResult(
                external_run_id=external_run_id,
                status="completed",
                is_terminal=True,
                raw={
                    "verification": {
                        "verified": False,
                        "verification_errors": [
                            "status_not_completed",
                            "verdict_not_pass",
                        ],
                    },
                    "verified_manifest": str(
                        Path(integration_env["runs_root"]) / "x" / "verified.json"
                    ),
                },
            )

        async def cancel(self, external_run_id):  # pragma: no cover - unused
            return None

    bad = _BadAdapter()
    saved = dict(_reg._adapters)
    try:
        _reg.register(bad, replace=True)
        t = FakeTask(
            task_id="TASK-WATCHER-BAD",
            adapter_name="claude_code",
            status="running",
            external_run_id="RUN-WATCHER-BAD",
        )
        await watcher._poll_one(t, t.external_run_id)
        assert bad.poll_calls == 1
        assert len(fm.fail_calls) == 1, (
            f"manager.fail was not called; got {fm.fail_calls}"
        )
        assert len(fm.complete_calls) == 0
        # The error message should mention the gate failure.
        assert "manifest" in (fm.fail_calls[0]["error"] or "").lower() or "gate" in (
            fm.fail_calls[0]["error"] or ""
        ).lower()
    finally:
        _reg._adapters.clear()
        _reg._adapters.update(saved)


@pytest.mark.asyncio
async def test_e2e_watcher_poll_one_bypasses_gate_for_hermes(
    integration_env,
):
    """Behavioral FIX-6: ``_poll_one`` must NOT invoke the manifest
    gate for non-claude_code tasks. We drive it with a fake Hermes
    adapter that returns ``completed`` with no verification block
    and assert that ``manager.complete`` is called (i.e. the gate
    was bypassed)."""
    from aee.core.registry import adapter_registry as _reg
    from aee.adapters.base import RuntimeAdapter, RuntimePollResult

    watcher = _import_watcher().Watcher(tick_sec=0.01)
    fm = FakeManager()
    watcher._manager = fm  # type: ignore[assignment]

    class _HermesAdapter(RuntimeAdapter):
        name = "fake_hermes"
        runtime_type = "hermes"

        def __init__(self) -> None:
            self.poll_calls = 0

        async def submit(self, job):  # pragma: no cover - unused
            raise NotImplementedError

        async def poll(self, external_run_id):
            self.poll_calls += 1
            # Hermes-style raw: no verification block, no
            # verified_manifest path. The watcher must accept it
            # because the gate is bypassed for non-claude_code
            # adapters.
            return RuntimePollResult(
                external_run_id=external_run_id,
                status="completed",
                is_terminal=True,
                output="hermes output",
                raw={"status": "completed"},
            )

        async def cancel(self, external_run_id):  # pragma: no cover - unused
            return None

    hermes = _HermesAdapter()
    saved = dict(_reg._adapters)
    try:
        _reg.register(hermes, replace=True)
        t = FakeTask(
            task_id="TASK-WATCHER-HERMES",
            adapter_name="fake_hermes",
            status="running",
            external_run_id="RUN-WATCHER-HERMES",
        )
        await watcher._poll_one(t, t.external_run_id)
        assert hermes.poll_calls == 1
        # Gate bypass: complete (not fail) must be called.
        assert len(fm.complete_calls) == 1, (
            f"manager.complete was not called for hermes; "
            f"complete={fm.complete_calls} fail={fm.fail_calls}"
        )
        assert len(fm.fail_calls) == 0
    finally:
        _reg._adapters.clear()
        _reg._adapters.update(saved)
