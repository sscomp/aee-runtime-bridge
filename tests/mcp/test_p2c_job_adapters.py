"""Offline multi-agent adapter-contract tests for the P2C gateway.

Covers the recovery work order's generic adapter contract: registry
refusals, stub-executor jobs (cwd fidelity, result contract), path
denial before execution, cancel/timeout process-group teardown and the
Stage-2 startup decoupling. No real agent CLI is executed.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aee.mcp_runtime.result_contract import validate_success
from aee.mcp_runtime.store import JobError, TERMINAL
from aee.mcp_runtime.budget import ExecutionBudget

ROOT = Path(__file__).resolve().parents[2]

SLEEP_STUB = """#!/usr/bin/python3
import os, sys, time
open(os.environ['STUB_PIDFILE'], 'w').write(str(os.getpid()))
time.sleep(float(os.environ.get('STUB_SLEEP', '120')))
"""

CLAUDE_STUB = """#!/usr/bin/python3
import json, os, sys
sys.stdin.read()
print(json.dumps({"type": "result", "result": "stub-final cwd=" + os.getcwd(), "is_error": False}))
"""

# Real `claude -p --output-format json --verbose` emits a JSON array whose
# last element is the result event; the parser must scan arrays, not only
# single documents or per-line JSONL.
CLAUDE_ARRAY_STUB = """#!/usr/bin/python3
import json, os, sys
sys.stdin.read()
print(json.dumps([{"type": "system", "subtype": "init"},
                  {"type": "result", "result": "array cwd=" + os.getcwd(),
                   "is_error": False}]))
"""

PI_STUB = """#!/usr/bin/python3
import json, os, sys
if "--help" in sys.argv:
 print("--no-mcp --no-extensions --no-skills --no-prompt-templates --no-approve --mode --no-session --tools"); sys.exit(0)
sys.stdin.read()
print(json.dumps({"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"pi-stub-final cwd=" + os.getcwd()}]}}))
print(json.dumps({"type":"agent_settled","aborted":False}))
"""

# Post-fix `claude -p --output-format json` (no --verbose) emits ONE final
# result object carrying result/is_error/usage — empirically verified on
# Claude Code 2.1.284.
CLAUDE_FINAL_OBJECT_STUB = """#!/usr/bin/python3
import json, os, sys
sys.stdin.read()
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "single-object cwd=" + os.getcwd(),
                  "num_turns": 1,
                  "usage": {"input_tokens": 1, "output_tokens": 1}}))
"""

# Regression fixture: the pre-fix `--verbose` event-array shape scaled past
# the 64 KiB stdout capture cap while the final answer stays tiny. Transport
# must fail explicitly (OUTPUT_LIMIT_EXCEEDED) — never misreport truncated
# output as success — and the failure record must retain bounded diagnostics.
CLAUDE_OVERSIZE_VERBOSE_STUB = """#!/usr/bin/python3
import json, sys
sys.stdin.read()
pad = "x" * 2048
events = [{"type": "assistant", "message": pad}] * 40
events.append({"type": "result", "result": "tiny", "is_error": False})
print(json.dumps(events))
"""


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class AdapterContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store_tmp = tempfile.TemporaryDirectory(prefix="aee-adapters-store-")
        cls.gateway = load_module("aee_p2c_adapter_gateway", ROOT / "mcp_gateway.py")

    @classmethod
    def tearDownClass(cls):
        cls.store_tmp.cleanup()
        sys.modules.pop("aee_p2c_adapter_gateway", None)

    def setUp(self):
        # These executables are synthetic protocol fixtures, not native CLI qualification.
        probe = patch('aee.mcp_runtime.job_adapters.verify_claude_readonly_capabilities')
        probe.start(); self.addCleanup(probe.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="aee-adapters-job-")
        self.addCleanup(self.temp.cleanup)
        self.workdir = Path(self.temp.name) / "work"
        self.workdir.mkdir()
        self.stubs = Path(self.temp.name) / "stubs"
        self.stubs.mkdir()
        self.gateway.JOB_STORE = self.gateway.JobStore(Path(self.temp.name) / "jobs")
        # OS-identity permission model (operator A2 recovery decision): the
        # working directory is a job parameter; by default no allowlist is
        # imposed and OS access decides. A list may still be configured by
        # an operator; dedicated tests below exercise that mode.
        roots = patch.object(self.gateway, "A3_DISPATCH_ALLOWED_ROOTS", [])
        roots.start()
        self.addCleanup(roots.stop)

    def stub(self, name, body):
        """An executable stub replacing an agent CLI binary."""
        path = self.stubs / name
        path.write_text(body)
        path.chmod(0o755)
        return path

    def python_stub(self, name, body):
        # Rebind the shebang to this interpreter; CI hosts may not ship /usr/bin/python3.
        return self.stub(name, body.replace("#!/usr/bin/python3", f"#!{sys.executable}"))

    def override_env(self, key, value):
        old = os.environ.get(key)
        os.environ[key] = value
        if old is None:
            self.addCleanup(lambda: os.environ.pop(key, None))
        else:
            self.addCleanup(lambda: os.environ.__setitem__(key, old))

    def wait_terminal(self, job_id, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = self.gateway.JOB_STORE.get(job_id)
            if record["status"] in TERMINAL:
                return record
            time.sleep(0.05)
        return self.gateway.JOB_STORE.get(job_id)

    def wait_running(self, job_id, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.gateway.JOB_STORE.get(job_id)["status"] == "running":
                return
            time.sleep(0.05)
        self.fail("job never reached running")

    def wait_gone(self, child_pid, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not Path(f"/proc/{child_pid}").exists():
                return
            time.sleep(0.05)
        self.fail(f"child process {child_pid} survived teardown")

    # -- admission refusals ------------------------------------------------

    def test_registry_refusals_never_launch(self):
        """Unknown agent → INVALID_AGENT; absent override → ADAPTER_NOT_INSTALLED;
        hermes remains ADAPTER_UNSUPPORTED from the registry alone."""
        with patch.object(self.gateway, "_start_job_worker") as worker:
            with self.assertRaises(self.gateway._JobError) as error:
                self.gateway.dispatch_job("docker", "x", str(self.workdir), "read_only")
            self.assertEqual(error.exception.code, "INVALID_AGENT")
            self.override_env("A3_ADAPTER_CLAUDE_BIN", "/synthetic/absent")
            with self.assertRaises(self.gateway._JobError) as error:
                self.gateway.dispatch_job("claude", "x", str(self.workdir), "read_only")
            self.assertEqual(error.exception.code, "ADAPTER_NOT_INSTALLED")
            self.override_env("A3_ADAPTER_HERMES_BIN", str(self.stub("hermes-cli", "echo hi")))
            with self.assertRaises(self.gateway._JobError) as error:
                self.gateway.dispatch_job("hermes", "x", str(self.workdir), "read_only")
            self.assertEqual(error.exception.code, "ADAPTER_UNSUPPORTED")
            worker.assert_not_called()
            self.assertEqual(self.gateway.JOB_STORE.list_ids(), [])

    def test_model_selectors_rejected_on_adapter_path(self):
        self.override_env("A3_ADAPTER_CLAUDE_BIN", "/synthetic/absent")
        with patch.object(self.gateway, "_start_job_worker") as worker:
            with self.assertRaises(self.gateway._JobError) as error:
                self.gateway.dispatch_job("claude", "x", str(self.workdir), "read_only",
                                          model="other-model")
            self.assertEqual(error.exception.code, "INVALID_PROFILE")
            worker.assert_not_called()

    def test_nonexistent_and_file_paths_refused(self):
        """Keep the basic filesystem validation with no allowlist set."""
        self.override_env("A3_ADAPTER_CLAUDE_BIN", "/synthetic/absent")
        plain_file = self.workdir / "file.txt"
        plain_file.write_text("x")
        with patch.object(self.gateway, "_start_job_worker") as worker:
            for candidate in [str(self.workdir / "nonexistent"), str(plain_file)]:
                with self.assertRaises(self.gateway._JobError) as error:
                    self.gateway.dispatch_job("claude", "x", candidate, "read_only")
                self.assertEqual(error.exception.code, "INVALID_WORKING_DIRECTORY")
            worker.assert_not_called()
            self.assertEqual(self.gateway.JOB_STORE.list_ids(), [])

    def test_working_directory_is_a_job_parameter_os_identity_decides(self):
        """Operator permission model F-series: with no allowlist configured,
        any existing directory enterable as the OS user is accepted — inside
        the home, outside it, and through symlinks that resolve normally.
        Nothing is rejected merely for being outside the home."""
        # A real (stub) binary: admission must pass; the worker is mocked, so
        # only the working-directory acceptance is under test here.
        self.override_env("A3_ADAPTER_CLAUDE_BIN", str(self.stub("claude", "echo unused")))
        inside_home = tempfile.mkdtemp(prefix="aee-osmodel-home-", dir=str(Path.home()))
        self.addCleanup(lambda: shutil.rmtree(inside_home, ignore_errors=True))
        outside_home = tempfile.mkdtemp(prefix="aee-osmodel-outside-")
        self.addCleanup(lambda: shutil.rmtree(outside_home, ignore_errors=True))
        link = self.workdir / "oslink"
        link.symlink_to(outside_home, target_is_directory=True)
        with patch.object(self.gateway, "_start_job_worker") as worker:
            accepted = []
            for candidate in [inside_home, outside_home]:
                record = self.gateway.dispatch_job("claude", "x", candidate, "read_only")
                self.assertEqual(record["status"], "queued")
                self.assertEqual(record["working_directory"], str(Path(candidate).resolve()))
                accepted.append(record)
                # The worker is mocked, so each job stays queued and holds
                # the single-flight lease; settle it before the next job.
                self.gateway.JOB_STORE.update(record["job_id"], status="cancelled")
            record = self.gateway.dispatch_job("claude", "x", str(link), "read_only")
            # Symlinks resolve normally under OS rules: the job runs in the
            # real target, not in the symlink path itself.
            self.assertEqual(record["status"], "queued")
            self.assertEqual(record["working_directory"], str(outside_home))
            self.assertEqual(worker.call_count, 3)

    def test_inaccessible_directory_refused_by_os_identity(self):
        """A directory the executor OS user cannot enter is refused; refusal
        is decided by actual OS permissions, not by any path root."""
        self.override_env("A3_ADAPTER_CLAUDE_BIN", "/synthetic/absent")
        locked = Path(self.temp.name) / "locked"
        locked.mkdir()
        locked.chmod(0o000)
        if os.access(locked, os.R_OK):  # running as root in CI would pass
            self.skipTest("directory check not meaningful for uid 0")
        self.addCleanup(lambda: locked.chmod(0o755))
        with patch.object(self.gateway, "_start_job_worker") as worker:
            with self.assertRaises(self.gateway._JobError) as error:
                self.gateway.dispatch_job("claude", "x", str(locked), "read_only")
            self.assertEqual(error.exception.code, "INVALID_WORKING_DIRECTORY")
            worker.assert_not_called()

    def test_configured_allowlist_still_confines(self):
        """An operator who configures A3_DISPATCH_ALLOWED_ROOTS keeps the
        deployment-level confinement (optional, off by default)."""
        self.override_env("A3_ADAPTER_CLAUDE_BIN", "/synthetic/absent")
        with patch.object(self.gateway, "_start_job_worker") as worker, \
                patch.object(self.gateway, "A3_DISPATCH_ALLOWED_ROOTS", ["/synthetic/no-root"]):
            with self.assertRaises(self.gateway._JobError) as error:
                self.gateway.dispatch_job("claude", "x", str(self.workdir), "read_only")
            self.assertEqual(error.exception.code, "INVALID_WORKING_DIRECTORY")
            worker.assert_not_called()

    def test_executor_runs_as_gw_os_user_without_escalation(self):
        """No privilege escalation: the job subprocess executes with the
        launcher's own uid/gid and the gateway's cwd, per OS rules."""
        uid = os.getuid()
        gid = os.getgid()
        stub = self.python_stub("claude", """#!/usr/bin/python3
import json, os, sys
sys.stdin.read()
print(json.dumps({"type": "result",
                  "result": f"uid={os.getuid()} gid={os.getgid()} cwd={os.getcwd()}",
                  "is_error": False}))
""")
        self.override_env("A3_ADAPTER_CLAUDE_BIN", str(stub))
        record = self.gateway.dispatch_job("claude", "x", str(self.workdir), "read_only")
        terminal = self.wait_terminal(record["job_id"])
        self.assertEqual(terminal["status"], "completed", terminal)
        self.assertEqual(terminal["summary"],
                         f"uid={uid} gid={gid} cwd={self.workdir}")

    def test_parent_traversal_is_canonicalized_not_escaped(self):
        self.override_env("A3_ADAPTER_CLAUDE_BIN", "/synthetic/absent")
        with patch.object(self.gateway, "A3_DISPATCH_ALLOWED_ROOTS", [str(self.temp.name)]):
            resolved = self.gateway._validate_working_directory(
                str(self.workdir / ".." / "work"))
            self.assertEqual(resolved, str(self.workdir))

    # -- stub executor jobs ------------------------------------------------

    def test_claude_stub_job_runs_in_requested_cwd_with_result_contract(self):
        """F02/F04 analogue: bounded stub job executes in the requested
        canonical working directory and returns a contract-valid result."""
        self.override_env("A3_ADAPTER_CLAUDE_BIN", str(self.python_stub("claude", CLAUDE_STUB)))
        record = self.gateway.dispatch_job("claude", "stub inspection", str(self.workdir),
                                           "read_only")
        self.assertEqual(record["status"], "queued")
        terminal = self.wait_terminal(record["job_id"])
        self.assertEqual(terminal["status"], "completed", terminal)
        self.assertEqual(terminal["exit_code"], 0)
        # The stub echoes os.getcwd() in its result: prove the subprocess cwd.
        self.assertIn(f"cwd={self.workdir}", terminal["summary"])
        proof = terminal["execution"]["adapter_proof"]
        self.assertEqual(proof["adapter"], "claude")
        self.assertEqual(proof["read_only_enforcement"], "TOOL_ALLOWLIST")
        self.assertEqual(proof["working_directory"], str(self.workdir))
        self.assertTrue(validate_success(terminal))

    def test_claude_array_output_parsed(self):
        """claude -p --output-format json --verbose emits an event array; the
        final_message parser must extract the result event from it."""
        self.override_env("A3_ADAPTER_CLAUDE_BIN", str(self.python_stub("claude", CLAUDE_ARRAY_STUB)))
        record = self.gateway.dispatch_job("claude", "stub inspection", str(self.workdir),
                                           "read_only")
        terminal = self.wait_terminal(record["job_id"])
        self.assertEqual(terminal["status"], "completed", terminal)
        self.assertIn(f"cwd={self.workdir}", terminal["summary"])

    def test_pi_stub_text_adapter_completes(self):
        """F10 analogue: an optional adapter with TOOL_DENYLIST enforcement."""
        self.override_env("A3_ADAPTER_PI_BIN", str(self.python_stub("pi", PI_STUB)))
        record = self.gateway.dispatch_job("pi", "stub inspection", str(self.workdir),
                                           "read_only")
        terminal = self.wait_terminal(record["job_id"])
        self.assertEqual(terminal["status"], "completed", terminal)
        self.assertIn(f"cwd={self.workdir}", terminal["summary"])
        self.assertEqual(terminal["execution"]["adapter_proof"]["read_only_enforcement"],
                         "TOOL_DENYLIST")

    def test_failing_child_exit_marks_failed_record_with_bounded_error(self):
        """F07 partial: failed child keeps its bounded stderr as the error."""
        self.override_env("A3_ADAPTER_CLAUDE_BIN", str(
            self.stub("fail", "#!/bin/sh\nprintf 'synthetic boom' >&2\nexit 3\n")))
        record = self.gateway.dispatch_job("claude", "x", str(self.workdir), "read_only")
        terminal = self.wait_terminal(record["job_id"])
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["exit_code"], 3)
        self.assertIn("synthetic boom", terminal["log_excerpt"])

    def test_summary_over_cap_is_clipped_and_flagged(self):
        """F07: completed adapter jobs clip the summary to the configured cap
        and set truncation, mirroring the codex contract."""
        self.override_env("A3_ADAPTER_CLAUDE_BIN", str(self.python_stub("claude", CLAUDE_STUB)))
        limits = self.gateway.Limits(timeout=60, summary_chars=32,
                                     log_chars=100, result_bytes=65536)
        with patch.object(self.gateway, "A3_LIMITS", limits):
            record = self.gateway.dispatch_job("claude", "long", str(self.workdir), "read_only")
            terminal = self.wait_terminal(record["job_id"])
        self.assertEqual(terminal["status"], "completed", terminal)
        self.assertTrue(terminal["truncated"])
        self.assertEqual(len(terminal["summary"]), 32)

    # -- cancellation / timeout --------------------------------------------

    def test_cancel_kills_child_persists_terminal_state(self):
        """F08: cooperative cancel stops the whole child process group and
        persists a cancelled terminal record attributed EXECUTION_CANCELLED."""
        pidfile = Path(self.temp.name) / "sleep.pid"
        self.override_env("STUB_PIDFILE", str(pidfile))
        self.override_env("A3_ADAPTER_CLAUDE_BIN", str(self.python_stub("sleeper", SLEEP_STUB)))
        record = self.gateway.dispatch_job("claude", "long", str(self.workdir), "read_only")
        job_id = record["job_id"]
        deadline = time.monotonic() + 15
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(pidfile.exists(), "stub child never started")
        child_pid = int(pidfile.read_text())
        self.wait_running(job_id)
        self.gateway._cancel_events[job_id].set()
        terminal = self.wait_terminal(job_id)
        self.assertEqual(terminal["status"], "cancelled")
        self.assertEqual(terminal["error_code"], "EXECUTION_CANCELLED")
        self.wait_gone(child_pid)

    def test_timeout_kills_child_persists_timed_out_state(self):
        """F08 timeout half: deadline teardown stops the process group."""
        pidfile = Path(self.temp.name) / "sleep.pid"
        self.override_env("STUB_PIDFILE", str(pidfile))
        self.override_env("A3_ADAPTER_CLAUDE_BIN", str(self.python_stub("sleeper", SLEEP_STUB)))
        limits = self.gateway.Limits(timeout=1, summary_chars=4000,
                                     log_chars=2000, result_bytes=65536)
        with patch.object(self.gateway, "A3_LIMITS", limits), patch.dict(
                self.gateway.EXECUTION_POLICY.budgets, {"claude": ExecutionBudget(wall_timeout_seconds=1)}):
            record = self.gateway.dispatch_job("claude", "long", str(self.workdir), "read_only")
            job_id = record["job_id"]
            deadline = time.monotonic() + 15
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            child_pid = int(pidfile.read_text())
            terminal = self.wait_terminal(job_id, timeout=30)
        self.assertEqual(terminal["status"], "timed_out")
        self.assertEqual(terminal["error_code"], "EXECUTION_TIMEOUT")
        self.wait_gone(child_pid)

    # -- admission-pinned identity ------------------------------------------

    def test_identity_drift_between_admission_and_dispatch_detected(self):
        """Executor identity pinned at admission is re-checked at dispatch."""
        stub_a = self.stub("claude-a", "#!/bin/sh\necho one\n")
        stub_b = self.stub("claude-b", "#!/bin/sh\necho two\n")
        with patch.object(self.gateway, "_start_job_worker") as worker:
            self.override_env("A3_ADAPTER_CLAUDE_BIN", str(stub_a))
            record = self.gateway.dispatch_job("claude", "x", str(self.workdir), "read_only")
            job_id = record["job_id"]
            # Worker thread is mocked out; now simulate the worker-side
            # re-resolution after the binary was swapped for stub_b.
            self.override_env("A3_ADAPTER_CLAUDE_BIN", str(stub_b))
        self.gateway.run_agent_job(job_id, "x", str(self.workdir))
        terminal = self.gateway.JOB_STORE.get(job_id)
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["error_code"], "EXECUTOR_IDENTITY_MISMATCH")

    # -- Stage-2 startup decoupling ------------------------------------------

    def test_stage2_absent_manifest_starts_gateway_with_safe_dispatch(self):
        """F11: a well-formed but unapproved manifest does NOT close startup;
        codex admission with a closed manifest still fails closed before
        admission; generic adapter dispatch keeps its own gates."""
        from aee.mcp_runtime.runtime import deployment_gate_approved, require_deployment_ready
        stage1 = {"schema": 1, "config_schema": "aee-p2c-1",
                  "deployment_status": "STAGE1_NOT_DEPLOYABLE",
                  "codex": {"path": "/synthetic/codex", "version": "codex-cli 0.159.0",
                            "sha256": "a" * 64},
                  "execution_profile": "codex-readonly-high",
                  "egress": "per-job-uds-responses-broker"}
        self.assertFalse(deployment_gate_approved(stage1))
        with self.assertRaises(JobError) as error:
            require_deployment_ready(stage1)
        self.assertEqual(error.exception.code, "DEPLOYMENT_GATE_CLOSED")
        self.assertEqual(self.gateway.startup_gate_mode(stage1), "OPEN_GENERIC")
        self.assertEqual(self.gateway.startup_gate_mode(None), "OPEN_GENERIC")
        stage1_file = Path(self.temp.name) / "stage1-manifest.json"
        stage1_file.write_text(json.dumps(stage1))
        self.override_env("AEE_RUNTIME_MANIFEST", str(stage1_file))
        self.override_env("A3_ADAPTER_CLAUDE_BIN", "/synthetic/absent")
        with patch.object(self.gateway, "A3_WORKSPACE_MANIFEST", "{}-fixture") as _ws, \
                patch.object(self.gateway, "_start_job_worker") as worker:
            with patch.object(self.gateway, "read_manifest", return_value={}):
                with self.assertRaises(self.gateway._JobError) as error:
                    self.gateway.dispatch_job("codex", "x", str(self.workdir), "read_only")
            self.assertEqual(error.exception.code, "DEPLOYMENT_GATE_CLOSED")
            worker.assert_not_called()

    # -- Claude output-contract recovery (AEE_CLAUDE_ADAPTER_OUTPUT_CONTRACT_RECOVERY)

    def test_claude_argv_final_json_omits_verbose(self):
        """Regression lock: the claude adapter must request single-object
        final JSON (`--output-format json`, no `--verbose`). The verbose
        event array streams intermediate events that alone overflow the
        64 KiB stdout capture cap before the final result arrives."""
        from aee.mcp_runtime.job_adapters import adapter_specs as adapter_specs_registry
        spec = adapter_specs_registry()["claude"]
        argv = spec.build_argv("/abs/work", "claude")
        self.assertNotIn("--verbose", argv)
        self.assertIn("--output-format", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertIn("--permission-mode", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertNotIn("--max-turns", argv)

    def test_registry_surface_unchanged(self):
        """The four-adapter registry and codex/pi argv contracts are
        untouched by the claude output-contract fix."""
        from aee.mcp_runtime.job_adapters import adapter_specs as adapter_specs_registry
        specs = adapter_specs_registry()
        self.assertEqual(set(specs), {"codex", "claude", "pi", "hermes"})
        self.assertEqual(
            specs["codex"].build_argv("/abs/work", "codex"),
            ["codex", "exec", "--json", "--ephemeral", "--skip-git-repo-check",
             "--sandbox", "read-only", "--cd", "/abs/work", "-"])
        self.assertEqual(
            specs["pi"].build_argv("/abs/work", "pi"),
            ["pi", "-p", "--no-session", "--mode", "json",
             "--no-extensions", "--no-mcp", "--no-skills", "--no-prompt-templates",
             "--no-approve", "--tools", "read"])
        with self.assertRaises(NotImplementedError):
            specs["hermes"].build_argv("/abs/work", "hermes")

    def test_claude_single_object_final_json_completes(self):
        """The fixed argv shape: one final result JSON object completes with
        its contract proof, and no output-limit diagnostics ever attach to a
        completed record."""
        import asyncio
        self.override_env("A3_ADAPTER_CLAUDE_BIN",
                          str(self.python_stub("claude", CLAUDE_FINAL_OBJECT_STUB)))
        record = self.gateway.dispatch_job("claude", "stub inspection", str(self.workdir),
                                           "read_only")
        terminal = self.wait_terminal(record["job_id"])
        self.assertEqual(terminal["status"], "completed", terminal)
        self.assertIn(f"cwd={self.workdir}", terminal["summary"])
        self.assertIs(terminal["execution"].get("output_limit_diagnostics"), None)
        result = json.loads(asyncio.run(self.gateway.aee_job_result(record["job_id"])))
        self.assertTrue(result["ok"])
        self.assertNotIn("output_limit_diagnostics", result["execution"])

    def test_verbose_array_over_old_capture_cap_completes_with_spool_evidence(self):
        """Budget route no longer kills on >64KiB intermediate events."""
        import asyncio
        self.override_env("A3_ADAPTER_CLAUDE_BIN", str(
            self.python_stub("claude", CLAUDE_OVERSIZE_VERBOSE_STUB)))
        record = self.gateway.dispatch_job("claude", "large", str(self.workdir), "read_only")
        terminal = self.wait_terminal(record["job_id"])
        self.assertEqual(terminal["status"], "completed", terminal)
        self.assertEqual(terminal["summary"], "tiny")
        meta = terminal['execution']['streaming']['streams']['stdout']
        self.assertGreater(meta['observed_bytes'], 65536)
        self.assertEqual(meta['observed_bytes'], meta['spool_bytes'])
        self.assertTrue(meta['capture_truncated'])
        result = json.loads(asyncio.run(self.gateway.aee_job_result(record['job_id'])))
        self.assertTrue(result['ok'])
        self.assertEqual(result['execution']['streaming']['streams']['stdout'], meta)

    def test_final_message_cap_yields_native_final_message_diagnostics(self):
        """A genuinely oversized final answer (small total stdout, but the
        final message exceeds result_bytes) stays an explicit bounded
        failure with native_final_message diagnostics persisted."""
        self.override_env("A3_ADAPTER_CLAUDE_BIN", str(self.python_stub("claude", """#!/usr/bin/python3
import json, sys
sys.stdin.read()
print(json.dumps({"type": "result", "is_error": False, "result": "y" * 1000}))
""")))
        small_result = self.gateway.Limits(timeout=60, result_bytes=512)
        with patch.object(self.gateway, "A3_LIMITS", small_result), patch.dict(
                self.gateway.EXECUTION_POLICY.budgets, {"claude": ExecutionBudget(result_inline_bytes=512)}):
            record = self.gateway.dispatch_job("claude", "big final", str(self.workdir),
                                               "read_only")
            terminal = self.wait_terminal(record["job_id"])
        self.assertEqual(terminal["status"], "failed", terminal)
        self.assertEqual(terminal["error_code"], "OUTPUT_LIMIT_EXCEEDED")
        diagnostics = terminal["execution"]["output_limit_diagnostics"]
        self.assertEqual(diagnostics["failure_boundary"], "native_final_message")
        self.assertEqual(diagnostics["measurement_stage"], "final_message_utf8")
        self.assertEqual(diagnostics["measurement_unit"], "utf8_bytes")
        self.assertEqual(diagnostics["limit_bytes"], 512)
        self.assertEqual(diagnostics["observed_bytes"], 1000)
        self.assertTrue(diagnostics["observed_measured"])

    def test_stderr_heavy_success_keeps_bounded_excerpt(self):
        """A talkative-but-legal stderr stream (< cap) completes and the
        persisted log excerpt stays bounded."""
        self.override_env("A3_ADAPTER_CLAUDE_BIN", str(self.python_stub("claude", """#!/usr/bin/python3
import json, sys
sys.stdin.read()
sys.stderr.write("n" * 50000)
sys.stderr.flush()
print(json.dumps({"type": "result", "is_error": False, "result": "ok"}))
""")))
        record = self.gateway.dispatch_job("claude", "noisy", str(self.workdir), "read_only")
        terminal = self.wait_terminal(record["job_id"])
        self.assertEqual(terminal["status"], "completed", terminal)
        excerpt = terminal["log_excerpt"] or ""
        self.assertGreater(len(excerpt), 0)
        self.assertLessEqual(len(excerpt), self.gateway.A3_LIMITS.log_chars)

    def test_claude_final_message_parser_semantics(self):
        """Direct parser semantics: success/error extraction, oversized final
        answers, and malformed/partial/unexpected output shapes never parse
        into a fabricated success."""
        from aee.mcp_runtime.job_adapters import _ClaudeAdapter

        def parse(stdout):
            return _ClaudeAdapter.final_message(stdout, self.gateway.Limits())

        # Success: single final object (post-fix CLI shape).
        self.assertEqual(
            parse(json.dumps({"type": "result", "is_error": False, "result": "final"})),
            "final")
        # Success: verbose array with intermediate events (forward-compatible).
        self.assertEqual(
            parse(json.dumps([{"type": "system", "subtype": "init"},
                              {"type": "assistant", "message": {"content": "hi"}},
                              {"type": "result", "is_error": False,
                               "result": "array final"}])),
            "array final")
        # Error shapes are explicit failures.
        for bad in (
            json.dumps({"type": "result", "is_error": True, "result": "boom"}),
            json.dumps({"type": "result", "subtype": "error_max_turns",
                        "is_error": True}),
            json.dumps([{"type": "assistant"}, {"type": "result",
                        "is_error": True, "result": "x"}])):
            with self.subTest(shape=bad[:40]):
                with self.assertRaises(JobError) as error:
                    parse(bad)
                self.assertEqual(error.exception.code, "TURN_BUDGET_EXHAUSTED" if "error_max_turns" in bad else "EXECUTION_FAILED")
        # Malformed, partial, truncated, empty, and missing-result documents
        # all fail closed.
        for raw in ('{"type": "result", "resul', "[system] notices only",
                    "", "{'single quotes': true}",
                    json.dumps({"type": "result", "is_error": False}),
                    json.dumps([{"type": "system"}])):
            with self.subTest(raw=raw[:40]):
                with self.assertRaises(JobError) as error:
                    parse(raw)
                self.assertEqual(error.exception.code, "EXECUTION_FAILED")
        # A final answer larger than result_bytes is an explicit output-limit
        # failure with native_final_message diagnostics.
        parser_limits = self.gateway.Limits(result_bytes=64)
        with self.assertRaises(JobError) as error:
            _ClaudeAdapter.final_message(
                json.dumps({"type": "result", "is_error": False,
                            "result": "z" * 100}), parser_limits)
        self.assertEqual(error.exception.code, "OUTPUT_LIMIT_EXCEEDED")
        self.assertEqual(error.exception.context["failure_boundary"],
                         "native_final_message")
        self.assertEqual(error.exception.context["observed_bytes"], 100)

    def test_optional_claude_turns_and_progress_mode_flags(self):
        from dataclasses import replace
        body = """#!/usr/bin/python3
import json,sys
sys.stdin.read()
print(json.dumps({'type':'result','result':' '.join(sys.argv[1:])}))
"""
        self.override_env('A3_ADAPTER_CLAUDE_BIN', str(self.python_stub('claude-flags', body)))
        budget=ExecutionBudget(wall_timeout_seconds=10,idle_timeout_seconds=5,max_turns=23)
        with patch.dict(self.gateway.EXECUTION_POLICY.budgets, {'claude':budget}):
            record=self.gateway.dispatch_job('claude','flags',str(self.workdir),'read_only')
            terminal=self.wait_terminal(record['job_id'])
        self.assertEqual(terminal['status'],'completed',terminal)
        self.assertIn('--max-turns 23',terminal['summary'])
        self.assertIn('--output-format stream-json',terminal['summary'])
        self.assertIn('--verbose',terminal['summary'])

    def test_pi_missing_restriction_flags_fails_closed(self):
        self.override_env('A3_ADAPTER_PI_BIN',str(self.python_stub('old-pi','#!/usr/bin/python3\nprint("old pi help")\n')))
        with self.assertRaises(JobError) as error:
            self.gateway.dispatch_job('pi','x',str(self.workdir),'read_only')
        self.assertEqual(error.exception.code,'ADAPTER_UNSUPPORTED')

    def test_version_probe_is_bounded(self):
        from aee.mcp_runtime.job_adapters import adapter_identity
        body='#!/usr/bin/python3\nimport os\nos.write(1,b"x"*100000)\n'
        self.override_env('A3_ADAPTER_CLAUDE_BIN',str(self.python_stub('noisy-version',body)))
        self.assertEqual(adapter_identity('claude')['version'],'unavailable')


if __name__ == "__main__":
    unittest.main()