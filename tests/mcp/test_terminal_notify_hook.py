"""Synthetic sender and worker tests for the legacy terminal notification hook."""
from contextlib import ExitStack
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aee.mcp_runtime.profiles import ExecutorIdentity, execution_metadata, select_profile
from aee.mcp_runtime.result_contract import validate_native_receipt
from completed_result_fixtures import successful_fields

ROOT = Path(__file__).resolve().parents[2]
NOTIFY_ENV = "AEE_TELEGRAM_TERMINAL_NOTIFY"
TARGET_ENV = "AEE_TERMINAL_NOTIFY_TARGET"
COMMAND_ENV = "AEE_TERMINAL_NOTIFY_COMMAND"
PROFILE_ENV = "AEE_TERMINAL_NOTIFY_HERMES_HOME"
JOB_ID = "A3JOB-20261126-" + "1" * 32
TARGET = "telegram:42"


class TerminalNotifyHook(unittest.TestCase):
    def _module(self, **env):
        store = tempfile.TemporaryDirectory(prefix="aee-terminal-notify-")
        self.addCleanup(store.cleanup)
        self.workdir = store.name
        spec = importlib.util.spec_from_file_location(
            f"gw_terminal_notify_{Path(store.name).name}", ROOT / "mcp_gateway.py")
        gateway = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = gateway
        self.addCleanup(sys.modules.pop, spec.name)
        with patch.dict(os.environ, {"A3_JOB_STORE_DIR": store.name, **env}, clear=True):
            spec.loader.exec_module(gateway)
        self.addCleanup(gateway.JOB_STORE.close)
        return gateway

    def _enabled(self, **env):
        return self._module(**{NOTIFY_ENV: "1", TARGET_ENV: TARGET, **env})

    def setUp(self):
        guard = patch("aee._notification_guard.notifications_disabled", return_value=False)
        self.suppressed = guard.start()
        self.addCleanup(guard.stop)

    def test_default_off_is_a_no_op(self):
        gw = self._module()
        self.assertFalse(gw.AEE_TELEGRAM_TERMINAL_NOTIFY)
        self.assertEqual(gw.AEE_TERMINAL_NOTIFY_COMMAND, "hermes")
        self.assertEqual(gw.AEE_TELEGRAM_NOTIFY_TARGET, "")
        with patch.object(gw.subprocess, "run") as sender:
            gw._notify_terminal(JOB_ID, "completed", 0)
        sender.assert_not_called()

    def test_truthy_variants_enable_the_hook(self):
        for value in ("1", "true", "yes", "on", "YES"):
            with self.subTest(value=value):
                self.assertTrue(self._module(**{NOTIFY_ENV: value}).AEE_TELEGRAM_TERMINAL_NOTIFY)

    def test_missing_target_skips_the_sender(self):
        gw = self._module(**{NOTIFY_ENV: "1"})
        with patch.object(gw.subprocess, "run") as sender, self.assertLogs(gw.log) as logs:
            gw._notify_terminal(JOB_ID, "failed", None, error_code="EXECUTION_FAILED")
        sender.assert_not_called()
        self.assertIn("reason=no_target", "\n".join(logs.output))

    def test_sender_contract_and_environment(self):
        profile = "/fixture/notification-profile"
        gw = self._enabled(**{PROFILE_ENV: profile})
        base_env = {"HOME": "/fixture/home", "PATH": "/fixture/bin", "LC_ALL": "C"}
        seeds = {"OPENROUTER_API_KEY": "provider-canary", "AEE_BROKER_CONTROL": "broker-canary",
                 "AEE_WRITE_APPROVAL_TOKEN": "approval-canary", "TELEGRAM_BOT_TOKEN": "bot-canary",
                 "HERMES_HOME": "/fixture/gateway-profile"}
        with patch.dict(os.environ, {**base_env, **seeds}, clear=True), \
                patch.object(gw.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as sender:
            gw._notify_terminal(JOB_ID, "completed", 0, agent="codex")
        sender.assert_called_once_with(
            ["hermes", "send", "-t", TARGET, "-s", "AEE job terminal"],
            input=f"AEE job {JOB_ID}: completed (exit_code=0, agent=codex)",
            text=True, timeout=30, env={**base_env, "HERMES_HOME": profile},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        gw = self._enabled(**{PROFILE_ENV: "relative"})
        with patch.object(gw.subprocess, "run") as sender, self.assertLogs(gw.log) as logs:
            gw._notify_terminal(JOB_ID, "completed", 0)
        sender.assert_not_called()
        self.assertIn("reason=invalid_sender_home", "\n".join(logs.output))

    def test_sender_exit_and_exception_logs_are_fixed(self):
        gw = self._enabled()
        canary = "private-sender-output"
        cases = [
            (SimpleNamespace(returncode=0, stdout=canary, stderr=canary), None,
             "terminal_notify_sender_succeeded"),
            (SimpleNamespace(returncode=1), None, "reason=sender_exit returncode=1"),
            (SimpleNamespace(returncode=-15), None, "reason=sender_exit returncode=-15"),
            (None, subprocess.TimeoutExpired([TARGET], 30, output=canary), "reason=timeout"),
            (None, FileNotFoundError(canary), "reason=sender_os_error"),
            (None, RuntimeError(canary), "reason=sender_exception"),
        ]
        for result, error, event in cases:
            with self.subTest(event=event):
                with patch.object(gw.subprocess, "run", return_value=result, side_effect=error), \
                        self.assertLogs(gw.log) as logs:
                    gw._notify_terminal(JOB_ID, "failed", 1, error_code="REQUIRED_TOOL_FAILED")
                logged = "\n".join(logs.output)
                self.assertIn(event, logged)
                self.assertEqual("terminal_notify_sender_succeeded" in logged, event.endswith("succeeded"))
                for private in (TARGET, canary, "terminal_notify_sent"):
                    self.assertNotIn(private, logged)

    def test_metadata_uses_job_store_identity_and_fixed_fields(self):
        gw = self._enabled()
        with patch.object(gw.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as sender, \
                self.assertLogs(gw.log) as logs:
            gw._notify_terminal("private\njob", "failed", 0)
            gw._notify_terminal(JOB_ID, {"private": "status"}, 0)
            gw._notify_terminal(JOB_ID, "failed", True, error_code="PRIVATE_UPPERCASE", agent=[])
        sender.assert_called_once()
        body = sender.call_args.kwargs["input"]
        self.assertIn("exit_code=none, agent=unknown", body)
        self.assertIn("error_code=EXECUTION_FAILED", body)
        self.assertLessEqual(len(body), 400)
        self.assertNotIn("PRIVATE", body)
        self.assertNotIn("private", "\n".join(logs.output))
        self.assertIn("reason=invalid_metadata", "\n".join(logs.output))

    def test_durable_owner_and_verification_suppress_legacy_sender(self):
        gw = self._enabled()
        with patch.object(gw.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as sender:
            with patch.object(gw, "_DURABLE_JOB_NOTIFIER_ACTIVE", True):
                gw._notify_terminal(JOB_ID, "completed", 0)
            self.suppressed.return_value = True
            gw._notify_terminal(JOB_ID, "completed", 0)
            sender.assert_not_called()
            self.suppressed.return_value = False
            gw._notify_terminal(JOB_ID, "completed", 0)
            sender.assert_called_once()
            self.assertIn(JOB_ID, sender.call_args.kwargs["input"])

    def test_all_worker_outcomes_notify_only_after_sealed_terminal_write(self):
        gw = self._enabled()
        shaped = "sk" + "-" + "b" * 28
        private = "x" * 390 + shaped + "\nDEMO_PASSWORD=canary-fixture\nunlabelled canary"
        task = "synthetic private worker task"
        fixture = successful_fields(summary=private)["execution"]
        proof = fixture["native_tool_evidence"]
        outcome = fixture["result_contract"]
        brokered = SimpleNamespace(
            snapshot={}, evidence=validate_native_receipt(
                proof["receipt"], outcome["thread"], proof["broker_receipt"], accepted=True),
            outcome=outcome, summary=private, log_excerpt=private, exit_code=0, truncated=False,
        )
        generic = {"summary": private, "log_excerpt": private, "exit_code": 0, "streaming": {}}
        for agent, execute, worker, success, execution in (
            ("codex", "execute_codex", gw.run_codex_job, brokered,
             execution_metadata(select_profile(), ExecutorIdentity("/fixture/codex", "1" * 64, "fixture"))),
            ("claude", "execute_adapter_job", gw.run_agent_job, generic,
             {"adapter": {"adapter": "claude", "version": "fixture", "executable_sha256": "2" * 64}}),
        ):
            for status, error in (
                ("completed", None),
                ("failed", gw._JobError("REQUIRED_TOOL_FAILED", private, exit_code=7)),
                ("failed", RuntimeError(private)),
            ):
                with self.subTest(agent=agent, status=status, error_type=type(error).__name__):
                    job = gw.JOB_STORE.create(agent, task, self.workdir, "read_only", execution=execution)
                    job_id = job["job_id"]

                    sealed_at_send = []

                    def fake_send(*args, **kwargs):
                        sealed_at_send.append(gw.JOB_STORE.get(job_id))
                        raise RuntimeError("sender-exception-canary")

                    spec = SimpleNamespace(name=agent, read_only_enforcement="FIXTURE",
                                           write_enforcement="FIXTURE", enforcement_note="", write_note="")
                    with ExitStack() as stack:
                        stack.enter_context(patch.object(gw, "resolve_adapter", return_value=(spec, "/fixture/tool")))
                        stack.enter_context(patch.object(gw, "check_execution"))
                        stack.enter_context(patch.object(gw, execute, return_value=success, side_effect=error))
                        sender = stack.enter_context(patch.object(gw.subprocess, "run", side_effect=fake_send))
                        logs = stack.enter_context(self.assertLogs(gw.log))
                        worker(job_id, task, self.workdir)
                    sender.assert_called_once()
                    self.assertEqual(len(sealed_at_send), 1)
                    record = sealed_at_send[0]
                    self.assertEqual(record["status"], status)
                    self.assertIn("integrity", record)
                    self.assertEqual(gw.JOB_STORE.get(job_id), record)
                    if status == "completed":
                        self.assertIn("canary-fixture", record["summary"])
                    elif isinstance(error, gw._JobError):
                        self.assertIn("canary-fixture", record["error"])
                    sent = sender.call_args.kwargs["input"]
                    self.assertIn(f"{job_id}: {status}", sent)
                    self.assertIn(f"agent={agent}", sent)
                    if isinstance(error, gw._JobError):
                        self.assertIn("error_code=REQUIRED_TOOL_FAILED", sent)
                    observed = repr(sender.call_args) + "\n".join(logs.output)
                    for payload in (shaped, "DEMO_PASSWORD", "canary-fixture", "unlabelled canary",
                                    task, "sender-exception-canary", "Isolated executor failed"):
                        self.assertNotIn(payload, observed)


if __name__ == "__main__":
    unittest.main()
