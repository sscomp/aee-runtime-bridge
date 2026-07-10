"""AEE-6.2 ExecProvider lifecycle tests (AEE Provider 抽象).

Scope
-----
Verifies the `ExecProvider` Protocol skeleton + the
`FakeClaudeCodeProvider` test stub cover the lifecycle semantics
that a real `ClaudeCodeProvider` (next slice, AEE-6.3) will need:

1. submit() returns a run_id + pid + RUNNING status.
2. poll() returns `is_terminal=True` with exit_code=0 after a
   happy-path run completes.
3. poll() returns `is_terminal=True` with exit_code=1 + error
   for a failing run.
4. cancel() transitions a running process to CANCELLED, sets
   exit_code=-1, emits stderr, subsequent poll() reports the
   terminal state.
5. read_stdout() / read_stderr() / exit_code() / artifacts_dir()
   all return the expected values for a completed run.
6. Timeout works for a hanging run (poll after timeout_seconds
   shows TIMED_OUT + is_terminal).
7. artifacts_dir() returns a fresh per-run directory; tests can
   write real artifacts and feed them to AEE-6 ArtifactPipeline
   (the next slice's full integration).
8. Calling poll() on an unknown run_id raises ProviderError.

These tests are stdlib `unittest` only, no pytest, no network.

Run with:
    .venv/bin/python -m unittest aee.tests.test_exec_provider -v
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import unittest

from aee.adapters.exec_provider import (
    ExecStatus,
    ProviderError,
)
from aee.adapters.fake_claude_adapter import (
    FakeClaudeCodeProvider,
    _BehaviorResult,
    _failing_behavior,
    _hanging_behavior,
    _happy_behavior,
)


def _run(coro):
    """Sync helper around an async coroutine for unittest.

    Uses ``asyncio.new_event_loop()`` + ``set_event_loop()`` so it
    works regardless of whether a previous test left a current
    event loop behind. (The original implementation used the
    deprecated ``asyncio.get_event_loop()``, which raises
    ``RuntimeError: There is no current event loop`` on
    Python 3.10+ when the loop has been closed by a prior
    ``IsolatedAsyncioTestCase``.)
    """
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.close()
        finally:
            asyncio.set_event_loop(None)


class TestExecProviderHappyPath(unittest.TestCase):
    """Happy path: submit → poll (terminal) → read outputs."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="aee6-prov-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.provider = FakeClaudeCodeProvider(
            behavior=_happy_behavior,
            default_artifacts_root=self.tmpdir,
        )

    def test_submit_returns_run_id_and_pid(self):
        sub = _run(
            self.provider.submit(prompt="write a hello", cwd=self.tmpdir)
        )
        self.assertTrue(sub.external_run_id.startswith("claude-run-"))
        self.assertIsNotNone(sub.pid)
        self.assertGreater(sub.pid, 0)
        self.assertEqual(sub.status, ExecStatus.RUNNING)

    def test_poll_terminal_with_exit_zero(self):
        sub = _run(self.provider.submit(prompt="hi", cwd=self.tmpdir))
        poll = _run(self.provider.poll(sub.external_run_id))
        self.assertTrue(poll.is_terminal)
        self.assertEqual(poll.exit_code, 0)
        self.assertEqual(poll.status, ExecStatus.EXITED)
        self.assertIn("processed: 'hi'", poll.output or "")

    def test_read_stdout_returns_full_output(self):
        sub = _run(self.provider.submit(prompt="hi", cwd=self.tmpdir))
        # Wait briefly for the fake's synchronous finalize.
        for _ in range(10):
            poll = _run(self.provider.poll(sub.external_run_id))
            if poll.is_terminal:
                break
            time.sleep(0.01)
        buf = self.provider.read_stdout(sub.external_run_id)
        content = buf.read().decode("utf-8")
        self.assertIn("processed: 'hi'", content)
        # read_stdout returns a fresh BytesIO each call.
        buf2 = self.provider.read_stdout(sub.external_run_id)
        self.assertEqual(buf.read(), b"")  # exhausted
        self.assertNotEqual(buf2.read(), b"")  # still has content

    def test_artifacts_dir_is_per_run(self):
        sub1 = _run(self.provider.submit(prompt="a", cwd=self.tmpdir))
        sub2 = _run(self.provider.submit(prompt="b", cwd=self.tmpdir))
        d1 = self.provider.artifacts_dir(sub1.external_run_id)
        d2 = self.provider.artifacts_dir(sub2.external_run_id)
        self.assertNotEqual(d1, d2)
        self.assertTrue(os.path.isdir(d1))
        self.assertTrue(os.path.isdir(d2))
        # Both are under the provider's default root.
        self.assertTrue(d1.startswith(self.tmpdir))
        self.assertTrue(d2.startswith(self.tmpdir))

    def test_exit_code_returns_zero_after_completion(self):
        sub = _run(self.provider.submit(prompt="x", cwd=self.tmpdir))
        for _ in range(10):
            if self.provider.exit_code(sub.external_run_id) is not None:
                break
            time.sleep(0.01)
        self.assertEqual(self.provider.exit_code(sub.external_run_id), 0)


class TestExecProviderFailure(unittest.TestCase):
    """Failure path: non-zero exit + stderr."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="aee6-prov-fail-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.provider = FakeClaudeCodeProvider(
            behavior=_failing_behavior,
            default_artifacts_root=self.tmpdir,
        )

    def test_poll_terminal_with_nonzero_exit(self):
        sub = _run(self.provider.submit(prompt="bad", cwd=self.tmpdir))
        poll = _run(self.provider.poll(sub.external_run_id))
        self.assertTrue(poll.is_terminal)
        self.assertEqual(poll.exit_code, 1)
        self.assertEqual(poll.status, ExecStatus.FAILED)
        self.assertIn("TypeError", poll.error or "")

    def test_stderr_captured(self):
        sub = _run(self.provider.submit(prompt="bad", cwd=self.tmpdir))
        for _ in range(10):
            poll = _run(self.provider.poll(sub.external_run_id))
            if poll.is_terminal:
                break
            time.sleep(0.01)
        stderr = self.provider.read_stderr(sub.external_run_id).read().decode("utf-8")
        self.assertIn("TypeError", stderr)


class TestExecProviderCancel(unittest.TestCase):
    """Cancel path: caller kills the subprocess mid-flight."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="aee6-prov-cancel-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        # Hang so the process is still running when we cancel.
        self.provider = FakeClaudeCodeProvider(
            behavior=_hanging_behavior,
            default_artifacts_root=self.tmpdir,
        )

    def test_cancel_running_run_transitions_to_cancelled(self):
        sub = _run(self.provider.submit(prompt="long", cwd=self.tmpdir))
        # Should still be RUNNING (hang).
        poll = _run(self.provider.poll(sub.external_run_id))
        self.assertFalse(poll.is_terminal)
        # Cancel.
        cres = _run(self.provider.cancel(sub.external_run_id))
        self.assertTrue(cres.cancelled)
        # Subsequent poll shows CANCELLED.
        poll2 = _run(self.provider.poll(sub.external_run_id))
        self.assertTrue(poll2.is_terminal)
        self.assertEqual(poll2.status, ExecStatus.CANCELLED)
        self.assertEqual(poll2.exit_code, -1)

    def test_cancel_already_terminal_is_noop(self):
        # First make the run terminate via happy_behavior.
        self.provider.set_behavior(_happy_behavior)
        sub = _run(self.provider.submit(prompt="hi", cwd=self.tmpdir))
        for _ in range(10):
            poll = _run(self.provider.poll(sub.external_run_id))
            if poll.is_terminal:
                break
            time.sleep(0.01)
        cres = _run(self.provider.cancel(sub.external_run_id))
        self.assertFalse(cres.cancelled)
        self.assertIn("already", cres.reason)


class TestExecProviderTimeout(unittest.TestCase):
    """Timeout path: a hanging run killed by the supervisor."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="aee6-prov-tmo-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.provider = FakeClaudeCodeProvider(
            behavior=_hanging_behavior,
            default_artifacts_root=self.tmpdir,
        )

    def test_timeout_kills_hanging_run(self):
        sub = _run(
            self.provider.submit(
                prompt="endless",
                cwd=self.tmpdir,
                timeout_seconds=1,
            )
        )
        # Wait up to 3s for the timeout thread to fire.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            poll = _run(self.provider.poll(sub.external_run_id))
            if poll.is_terminal:
                self.assertEqual(poll.status, ExecStatus.TIMED_OUT)
                self.assertEqual(poll.exit_code, -1)
                stderr = self.provider.read_stderr(sub.external_run_id).read().decode("utf-8")
                self.assertIn("timeout", stderr)
                return
            time.sleep(0.05)
        self.fail("hanging run did not transition to TIMED_OUT within 3s")


class TestExecProviderErrors(unittest.TestCase):
    """Error path: unknown run_id and other contract violations."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="aee6-prov-err-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.provider = FakeClaudeCodeProvider(
            default_artifacts_root=self.tmpdir,
        )

    def test_poll_unknown_run_id_raises(self):
        with self.assertRaises(ProviderError):
            _run(self.provider.poll("nonexistent-run"))

    def test_cancel_unknown_run_id_raises(self):
        with self.assertRaises(ProviderError):
            _run(self.provider.cancel("nonexistent-run"))

    def test_read_stdout_unknown_run_id_raises(self):
        with self.assertRaises(ProviderError):
            self.provider.read_stdout("nonexistent-run")

    def test_satisfies_protocol(self):
        """The fake provider must satisfy the ExecProvider Protocol
        structurally (runtime_checkable allows isinstance)."""
        from aee.adapters.exec_provider import ExecProvider
        self.assertIsInstance(self.provider, ExecProvider)


class TestArtifactPipelineIntegrationWithProvider(unittest.TestCase):
    """End-to-end: ExecProvider + ArtifactPipeline.

    The provider writes a report.md into its artifacts_dir; the
    AEE-6 ArtifactPipeline picks it up, hashes it, and persists
    to the dispatcher DB.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="aee6-prov-int-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.provider = FakeClaudeCodeProvider(
            default_artifacts_root=self.tmpdir,
        )
        # Force dispatcher DB init.
        from dispatcher.db import get_conn
        _ = get_conn()

    def test_provider_artifacts_dir_feeds_pipeline(self):
        sub = _run(
            self.provider.submit(
                prompt="write report.md",
                cwd=self.tmpdir,
            )
        )
        art_dir = self.provider.artifacts_dir(sub.external_run_id)
        # Simulate the worker writing artifacts.
        report = os.path.join(art_dir, "report.md")
        with open(report, "w", encoding="utf-8") as f:
            f.write("# done\n")
        # Run the AEE-6 pipeline on the artifacts_dir.
        from aee.artifacts import (
            ArtifactPipeline,
            SqliteArtifactRepository,
        )
        from dispatcher.db import get_conn
        repo = SqliteArtifactRepository(get_conn())
        pipeline = ArtifactPipeline(repo=repo)
        results = pipeline.collect(
            task_id="TASK-PROV-INT",
            paths=[report],
        )
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].exists)
        self.assertEqual(results[0].kind, "report")
        self.assertEqual(len(results[0].sha256), 64)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
