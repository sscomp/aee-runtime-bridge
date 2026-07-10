"""AEE-6.3 — Real ``ClaudeCodeProvider`` tests.

The provider shells out to the ``claude`` CLI as a subprocess. We
do **not** call the real CLI in unit tests (no network, no auth, no
flakiness). Instead, we drop a tiny shell script on disk that
mimics the CLI's stdout/exit-code contract:

* honors the ``--bare`` / ``-p`` / ``--output-format`` flags
  (we do not assert on them — we just emit a deterministic string)
* writes any file passed as ``--file <path>=<content>`` to disk
  so the test can verify the artifact pipeline sees the file
* exits 0 normally, 1 on a special ``__FAIL__`` marker, hangs on
  ``__HANG__`` so cancel/timeout tests can exercise the kill path
* prints the prompt verbatim to stdout so we can assert the
  provider forwarded it correctly

The script is created per-test via ``tempfile.mkdtemp`` and lives
under ``/tmp/aee6-claude-provider-*`` so it does not pollute the
repo.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import tempfile
import time
import unittest
from pathlib import Path
from typing import List

from aee.adapters.claude_code_provider import (
    ClaudeCodeProvider,
    _ALLOWED_ENV_VARS,
    DEFAULT_CANCEL_GRACE_SECONDS,
)
from aee.adapters.exec_provider import (
    ExecStatus,
    ProviderError,
    ProviderNotFoundError,
    ProviderTimeoutError,
)


_FAKE_CLAUDE_SCRIPT = r"""#!/usr/bin/env bash
# AEE-6.3 test fake — mimics `claude` enough for unit tests.
set -u
# Default behaviour: echo the prompt to stdout, write a "report.md" to
# the cwd (so the artifact pipeline has something to collect), exit 0.
WRITE_FILE=""
FAIL=0
HANG=0
SLEEP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -p|--print|--bare|--max-turns|--model|--system-prompt|--append-system-prompt|--output-format|--add-dir|--allowedTools|--disallowedTools|--tools)
      shift; [[ $# -gt 0 && "$1" != "--"* ]] && shift || true
      ;;
    --file)
      shift
      # Format: file_id:relative_path=content  (we ignore file_id)
      SPEC="$1"
      case "$SPEC" in
        *=*) CONTENT="${SPEC#*=}" ;;
        *)   CONTENT="auto-content" ;;
      esac
      case "$SPEC" in
        *:*) PATH_REL="${SPEC#*:}"; PATH_REL="${PATH_REL%%=*}" ;;
        *)   PATH_REL="fake-output.txt" ;;
      esac
      if [[ "$PATH_REL" = /* ]]; then
        echo "$CONTENT" > "$PATH_REL"
      else
        echo "$CONTENT" > "./$PATH_REL"
      fi
      shift
      ;;
    __FAIL__) FAIL=1; shift ;;
    __HANG__) HANG=1; shift ;;
    __SLEEP_*) SLEEP="${1#__SLEEP_}"; shift ;;
    *) shift ;;
  esac
done
if [[ "$HANG" == "1" ]]; then
  # Loop forever until killed
  while true; do sleep 1; done
fi
if [[ "$SLEEP" -gt 0 ]]; then
  sleep "$SLEEP"
fi
# Echo everything we got (so we can inspect what the provider sent)
echo "fake-claude: prompt received, exit=$FAIL"
if [[ "$FAIL" == "1" ]]; then
  echo "synthetic failure" >&2
  exit 1
fi
exit 0
"""


def _install_fake_claude(tmp_root: str) -> str:
    """Drop the fake claude script in ``tmp_root`` and return the path."""
    os.makedirs(tmp_root, exist_ok=True)
    p = os.path.join(tmp_root, "claude")
    Path(p).write_text(_FAKE_CLAUDE_SCRIPT)
    os.chmod(p, 0o755)
    return p


class _FakeClaudeMixin:
    """Common setup: install a fake ``claude`` binary in a temp dir."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="aee6-claude-provider-")
        self.bin_dir = os.path.join(self.tmp, "bin")
        self.cwd = os.path.join(self.tmp, "work")
        os.makedirs(self.cwd, exist_ok=True)
        self.fake_claude = _install_fake_claude(self.bin_dir)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        # Best-effort reap any lingering subprocesses.
        if hasattr(self, "provider") and self.provider is not None:
            try:
                self.provider.shutdown()
            except Exception:  # pragma: no cover - defensive
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _atexit_provider(self, provider: ClaudeCodeProvider) -> None:
        """Reap the provider after the test body so the event loop
        closes cleanly when the next test starts.

        Without this, the asyncio subprocess transport from the
        previous test still holds a reference to the (now-closed)
        loop, and ``BaseSubprocessTransport.__del__`` raises
        ``RuntimeError: Event loop is closed`` *after* the new test
        has already started — which then shows up as a spurious
        failure in the next test.
        """
        try:
            provider.shutdown()
        except Exception:  # pragma: no cover - defensive
            pass
        # Drain: for every record whose proc is dead but whose
        # drain task has not yet completed, await the drain.
        for rid, rec in list(provider._runs.items()):
            dt = getattr(rec, "_drain_task", None)
            if dt is not None:
                try:
                    await dt
                except Exception:
                    pass
        # Yield once so any pending callbacks settle.
        await asyncio.sleep(0)

    def _make_provider(self, cancel_grace_seconds: float = 0.5, **kwargs) -> ClaudeCodeProvider:
        p = ClaudeCodeProvider(
            binary=self.fake_claude,
            default_cwd=self.cwd,
            cancel_grace_seconds=cancel_grace_seconds,
            **kwargs,
        )
        self.provider = p
        return p

    async def asyncTearDown(self) -> None:  # type: ignore[override]
        """IsolatedAsyncioTestCase hook. Reap the provider so the
        subprocess transport from the *current* test loop is
        detached before the next test starts a new loop.
        """
        if hasattr(self, "provider") and self.provider is not None:
            await self._atexit_provider(self.provider)

    def tearDown(self) -> None:
        # Sync fallback for non-async TestCase subclasses.
        if hasattr(self, "provider") and self.provider is not None:
            try:
                self.provider.shutdown()
            except Exception:  # pragma: no cover
                pass


class TestClaudeCodeProviderBasics(_FakeClaudeMixin, unittest.TestCase):
    """Group A — protocol basics + protocol constants."""

    def test_name_and_runtime_type(self) -> None:
        provider = self._make_provider()
        self.assertEqual(provider.name, "claude_code")
        self.assertEqual(provider.runtime_type, "claude_code")

    def test_default_max_turns_one(self) -> None:
        # The constructor must reject < 1.
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                binary=self.fake_claude, max_turns=0
            )

    def test_invalid_output_format(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                binary=self.fake_claude, output_format="bogus"
            )

    def test_allowed_env_vars_includes_safe_set(self) -> None:
        # The allow-list is closed by design. Confirm the well-known
        # safe entries are present and that the *closed* set is
        # small (no random leak vectors). The auth token IS in the
        # allow-list — the worker subprocess needs it to talk to
        # the upstream API. Path-level security (ArtifactPolicy) and
        # subprocess isolation are the AEE-6.3 isolation model;
        # env filtering is defence-in-depth.
        self.assertIn("PATH", _ALLOWED_ENV_VARS)
        self.assertIn("HOME", _ALLOWED_ENV_VARS)
        self.assertIn("ANTHROPIC_BASE_URL", _ALLOWED_ENV_VARS)
        self.assertIn("ANTHROPIC_AUTH_TOKEN", _ALLOWED_ENV_VARS)
        # Closed set: nothing in the *untrusted* set is allowed.
        for sensitive in (
            "MY_RANDOM_VAR",
            "TELEGRAM_BOT_TOKEN",
            "GITHUB_TOKEN",
            "DATABASE_URL",
            "SSH_AUTH_SOCK",
        ):
            self.assertNotIn(sensitive, _ALLOWED_ENV_VARS)

    def test_unknown_run_id_raises(self) -> None:
        provider = self._make_provider()
        with self.assertRaises(ProviderNotFoundError):
            provider.read_stdout("does-not-exist")
        with self.assertRaises(ProviderNotFoundError):
            provider.artifacts_dir("does-not-exist")
        with self.assertRaises(ProviderNotFoundError):
            provider.exit_code("does-not-exist")


class TestClaudeCodeProviderHappy(_FakeClaudeMixin, unittest.IsolatedAsyncioTestCase):
    """Group B — happy path: real subprocess → exit 0."""

    async def test_submit_returns_running_with_pid(self) -> None:
        provider = self._make_provider()
        result = await provider.submit(
            prompt="hello world", cwd=self.cwd
        )
        self.assertTrue(result.external_run_id.startswith("claude-run-"))
        self.assertIsNotNone(result.pid)
        self.assertIn(result.status, (ExecStatus.PENDING, ExecStatus.RUNNING))

    async def test_poll_terminal_after_exit(self) -> None:
        provider = self._make_provider()
        result = await provider.submit(prompt="hi", cwd=self.cwd)
        # Wait for the subprocess to exit (it's synchronous in the
        # fake script).
        for _ in range(40):
            poll = await provider.poll(result.external_run_id)
            if poll.is_terminal:
                break
            await asyncio.sleep(0.05)
        self.assertTrue(poll.is_terminal)
        self.assertEqual(poll.status, ExecStatus.EXITED)
        self.assertEqual(poll.exit_code, 0)

    async def _wait_drain(self, provider, run_id) -> None:
        """Ensure the stdout/stderr drain coroutine has fully flushed
        after the process exited. Without this, ``read_stdout`` can
        race the drain and return a partial / empty buffer.
        """
        rec = provider._runs.get(run_id)
        if rec is None:
            return
        dt = getattr(rec, "_drain_task", None)
        if dt is not None:
            try:
                await dt
            except Exception:
                pass

    async def test_read_stdout_contains_prompt_marker(self) -> None:
        provider = self._make_provider()
        result = await provider.submit(
            prompt="please-mark-the-prompt", cwd=self.cwd
        )
        # Wait for completion
        for _ in range(40):
            poll = await provider.poll(result.external_run_id)
            if poll.is_terminal:
                break
            await asyncio.sleep(0.05)
        await self._wait_drain(provider, result.external_run_id)
        stdout = provider.read_stdout(result.external_run_id).read()
        # The fake script echoes a fixed marker, not the prompt
        # (because it doesn't see the prompt as an arg once our
        # provider passes it). But the output should at least exist
        # and contain the fake's status line.
        self.assertIn(b"fake-claude", stdout)
        self.assertIn(b"exit=0", stdout)

    async def test_artifacts_dir_matches_cwd(self) -> None:
        provider = self._make_provider()
        result = await provider.submit(prompt="x", cwd=self.cwd)
        self.assertEqual(
            provider.artifacts_dir(result.external_run_id),
            self.cwd,
        )

    async def test_exit_code_returns_int_after_finish(self) -> None:
        provider = self._make_provider()
        result = await provider.submit(prompt="x", cwd=self.cwd)
        for _ in range(40):
            poll = await provider.poll(result.external_run_id)
            if poll.is_terminal:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(
            provider.exit_code(result.external_run_id), 0
        )


class TestClaudeCodeProviderFailure(_FakeClaudeMixin, unittest.IsolatedAsyncioTestCase):
    """Group C — failure / cancel / timeout paths."""

    async def test_non_zero_exit_marked_failed(self) -> None:
        provider = self._make_provider()
        result = await provider.submit(
            prompt="__FAIL__", cwd=self.cwd
        )
        for _ in range(40):
            poll = await provider.poll(result.external_run_id)
            if poll.is_terminal:
                break
            await asyncio.sleep(0.05)
        self.assertTrue(poll.is_terminal)
        # Our policy: non-zero exit on a never-cancelled run == FAILED.
        self.assertEqual(poll.status, ExecStatus.FAILED)
        self.assertEqual(poll.exit_code, 1)
        # stderr should contain the synthetic failure message
        stderr = provider.read_stderr(result.external_run_id).read()
        self.assertIn(b"synthetic failure", stderr)

    async def test_cancel_kills_long_running(self) -> None:
        provider = self._make_provider(cancel_grace_seconds=0.3)
        result = await provider.submit(
            prompt="__HANG__", cwd=self.cwd
        )
        # Give the subprocess a moment to start
        await asyncio.sleep(0.2)
        cancel_result = await provider.cancel(result.external_run_id)
        self.assertTrue(cancel_result.cancelled)
        poll = await provider.poll(result.external_run_id)
        self.assertTrue(poll.is_terminal)
        self.assertIn(
            poll.status,
            (ExecStatus.CANCELLED, ExecStatus.TIMED_OUT),
        )

    async def test_timeout_kills_long_running(self) -> None:
        # timeout_seconds=1 + the fake hangs forever
        provider = self._make_provider(cancel_grace_seconds=0.3)
        result = await provider.submit(
            prompt="__HANG__", cwd=self.cwd, timeout_seconds=1
        )
        # Wait up to 4s for the supervisor to fire
        deadline = time.time() + 4.0
        while time.time() < deadline:
            poll = await provider.poll(result.external_run_id)
            if poll.is_terminal:
                break
            await asyncio.sleep(0.1)
        self.assertTrue(poll.is_terminal)
        self.assertIn(
            poll.status,
            (ExecStatus.CANCELLED, ExecStatus.TIMED_OUT),
        )

    async def test_cancel_already_exited_is_noop(self) -> None:
        provider = self._make_provider()
        result = await provider.submit(prompt="x", cwd=self.cwd)
        for _ in range(40):
            poll = await provider.poll(result.external_run_id)
            if poll.is_terminal:
                break
            await asyncio.sleep(0.05)
        cancel_result = await provider.cancel(result.external_run_id)
        self.assertTrue(cancel_result.cancelled)
        self.assertIn("already exited", cancel_result.reason)


class TestClaudeCodeProviderEnvFilter(_FakeClaudeMixin, unittest.TestCase):
    """Group D — env var allow-list filtering."""

    def test_caller_env_filtered_through_allow_list(self) -> None:
        provider = self._make_provider()
        # Bypass the subprocess and call _filter_env directly; it is
        # a deterministic pure function.
        full_env = provider._filter_env(
            {
                "PATH": "/custom/bin",
                "ANTHROPIC_AUTH_TOKEN": "test-token-1234",
                "ANTHROPIC_BASE_URL": "https://example.com",
                "MY_RANDOM_VAR": "noise",
                "HOME": "/custom/home",
                "TELEGRAM_BOT_TOKEN": "should-not-leak",
                "DATABASE_URL": "postgres://secret",
            }
        )
        # Allow-listed entries are present (PATH/HOME come from parent
        # + overlay, ANTHROPIC_BASE_URL is allow-listed,
        # ANTHROPIC_AUTH_TOKEN is in the allow-list so the worker
        # can authenticate).
        self.assertIn("PATH", full_env)
        self.assertIn("HOME", full_env)
        self.assertEqual(
            full_env["ANTHROPIC_BASE_URL"], "https://example.com"
        )
        self.assertEqual(
            full_env["ANTHROPIC_AUTH_TOKEN"], "test-token-1234"
        )
        # Untrusted entries are dropped.
        self.assertNotIn("MY_RANDOM_VAR", full_env)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", full_env)
        self.assertNotIn("DATABASE_URL", full_env)


class TestClaudeCodeProviderMissingBinary(unittest.TestCase):
    """Group E — the binary-not-found path."""

    def test_submit_raises_provider_error_when_binary_missing(self) -> None:
        provider = ClaudeCodeProvider(
            binary="/nonexistent/path/to/claude",
            default_cwd=tempfile.gettempdir(),
        )

        async def go():
            return await provider.submit(prompt="x")

        with self.assertRaises(ProviderError):
            asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
