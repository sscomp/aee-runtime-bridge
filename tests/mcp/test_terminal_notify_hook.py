"""Terminal notification hook contract for mcp_gateway workers.

The hook (``mcp_gateway._notify_terminal``) is operator-opt-in via
``AEE_TELEGRAM_TERMINAL_NOTIFY`` and must satisfy four guarantees:

1. Default-off: when the env sentinel is absent, nothing is executed --
   no subprocess, no log noise, no behaviour change (public-repo default).
2. Opt-in send path: when enabled with a target, the hook invokes
   ``<AEE_TERMINAL_NOTIFY_COMMAND> send -t <target> -s AEE job terminal``
   with the summary text on stdin (never as an argv positional, so task
   text can never split or inject extra argv items).
3. Fail-soft: any exception inside the send is swallowed and journal-
   logged with a bounded exception class name; the caller's terminal
   state machine (JOB_STORE.update) is unaffected either way.
4. Bounded text: the summary is whitespace-flattened and clipped so a
   hostile task summary cannot forge message structure or flood length.

The gateway module is loaded by file path exactly like the other
fixture-gateway tests, so each test gets a fresh import and can pin the
process-global config constants before exercising the hook.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]

NOTIFY_ENV = "AEE_TELEGRAM_TERMINAL_NOTIFY"
TARGET_ENV = "AEE_TERMINAL_NOTIFY_TARGET"
COMMAND_ENV = "AEE_TERMINAL_NOTIFY_COMMAND"
SUBJECT = "AEE job terminal"


def _load_gateway(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "mcp_gateway.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class TerminalNotifyHook(unittest.TestCase):
    def _module(self, name, **env):
        store = tempfile.TemporaryDirectory(prefix="aee-terminal-notify-")
        self.addCleanup(store.cleanup)
        env["A3_JOB_STORE_DIR"] = store.name
        with patch.dict("os.environ", env, clear=False):
            for key in (NOTIFY_ENV, TARGET_ENV, COMMAND_ENV):
                if key not in env:
                    import os
                    os.environ.pop(key, None)
            gateway = _load_gateway(name)
        self.addCleanup(gateway.JOB_STORE.close)
        return gateway

    def setUp(self):
        guard = patch("aee._notification_guard.notifications_disabled", return_value=False)
        self.suppressed = guard.start()
        self.addCleanup(guard.stop)

    def test_1_default_off_is_a_no_op(self):
        gw = self._module("gw_notify_off")
        with patch.object(gw.subprocess, "run") as probe:
            gw._notify_terminal("J1", "completed", 0, summary="hello")
            probe.assert_not_called()

    def test_2_enabled_send_builds_expected_argv_and_stdin(self):
        gw = self._module("gw_notify_on",
                          **{NOTIFY_ENV: "1", TARGET_ENV: "telegram:42"})
        with patch.object(gw.subprocess, "run") as probe:
            gw._notify_terminal("J2", "completed", 0,
                                summary="line one\nline two  tail",
                                error_code=None, agent="codex")
            probe.assert_called_once()
            args, kwargs = probe.call_args
            argv = args[0]
            self.assertEqual(argv[:3],
                             ["hermes", "send", "-t"])
            self.assertEqual(argv[3], "telegram:42")
            self.assertIn("-s", argv)
            self.assertEqual(argv[argv.index("-s") + 1], SUBJECT)
            self.assertNotIn("-f", argv)
            self.assertEqual(kwargs.get("timeout"), 30)
            text = kwargs.get("input") or ""
            self.assertIn("AEE job J2: completed", text)
            self.assertIn("agent=codex", text)
            # newline structure of the summary must be flattened
            self.assertNotIn("\n", text.split("summary: ")[-1])
            self.assertIn("line one line two tail", text)

    def test_3_enabled_without_target_logs_skip_and_never_executes(self):
        gw = self._module("gw_notify_notarget", **{NOTIFY_ENV: "1"})
        capture = _CaptureHandler()
        gw.log.addHandler(capture)
        self.addCleanup(gw.log.removeHandler, capture)
        with patch.object(gw.subprocess, "run") as probe:
            gw._notify_terminal("J3", "failed", None,
                                error_code="EXECUTION_FAILED",
                                summary="boom")
            probe.assert_not_called()
        self.assertTrue(any("terminal_notify_skipped" in line for line in capture.lines))

    def test_4_send_failure_is_swallowed_fail_soft(self):
        gw = self._module("gw_notify_fail",
                          **{NOTIFY_ENV: "1", TARGET_ENV: "telegram:0"})
        class _Boom(Exception):
            pass
        def raiser(*a, **k):
            raise _Boom("cli unavailable")
        with patch.object(gw.subprocess, "run", side_effect=raiser):
            # must NOT raise out of the hook
            gw._notify_terminal("J4", "failed", 1,
                                error_code="REQUIRED_TOOL_FAILED",
                                summary="s")
        # error path exercised; nothing propagated

    def test_5_summary_is_clipped_and_hostile_newlines_flattened(self):
        gw = self._module("gw_notify_clip",
                          **{NOTIFY_ENV: "1", TARGET_ENV: "telegram:1"})
        hostile = "x" + "\n" * 50 + "y " + "z" * 900
        with patch.object(gw.subprocess, "run") as probe:
            gw._notify_terminal("J5", "completed", 0, summary=hostile)
            text = probe.call_args[1]["input"]
            self.assertIn("summary: ", text)
            flat = text.split("summary: ", 1)[1]
            self.assertLessEqual(len(flat), 400)
            self.assertNotIn("\n", flat)

    def test_6_config_flags_default_values(self):
        gw = self._module("gw_notify_defaults")
        self.assertFalse(gw.AEE_TELEGRAM_TERMINAL_NOTIFY)
        self.assertEqual(gw.AEE_TERMINAL_NOTIFY_COMMAND, "hermes")
        self.assertEqual(gw.AEE_TELEGRAM_NOTIFY_TARGET, "")

    def test_7_truthy_variants_enable_the_hook(self):
        for value in ("1", "true", "yes", "on", "YES"):
            name = "gw_notify_truthy_" + value.strip().lower() + "_x"
            gw = self._module(name, **{NOTIFY_ENV: value,
                                       TARGET_ENV: "telegram:2"})
            self.assertTrue(gw.AEE_TELEGRAM_TERMINAL_NOTIFY, value)

    def test_durable_owner_and_verification_suppress_legacy_sender(self):
        gw = self._module("gw_notify_owner", **{NOTIFY_ENV: "1", TARGET_ENV: "telegram:42"})
        with patch.object(gw.subprocess, "run") as sender:
            with patch.object(gw, "_DURABLE_JOB_NOTIFIER_ACTIVE", True):
                gw._notify_terminal("J6", "completed", 0)
            self.suppressed.return_value = True
            gw._notify_terminal("J6", "completed", 0)
            sender.assert_not_called()

            self.suppressed.return_value = False
            gw._notify_terminal("J6", "completed", 0)
            sender.assert_called_once()
            self.assertIn("J6", sender.call_args.kwargs["input"])


if __name__ == "__main__":
    unittest.main()
