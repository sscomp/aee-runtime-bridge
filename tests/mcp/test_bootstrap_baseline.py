"""Offline A3 contracts against the unchanged gateway; no agent is launched."""
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class BootstrapBaseline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.initial_store = tempfile.TemporaryDirectory(prefix="aee-baseline-import-")
        cls.manifest = Path(cls.initial_store.name) / "manifest.json"
        cls.manifest.write_text("{}")
        with patch.dict(os.environ, {"A3_JOB_STORE_DIR": cls.initial_store.name,
                                    "A3_DISPATCH_ALLOWED_ROOTS": str(ROOT),
                                    "AEE_MCP_SURFACE": "local", "AEE_MCP_PORT": "8790",
                                    "A3_WORKSPACE_MANIFEST": str(cls.manifest),
                                    "AEE_MCP_EXPOSED_TOOLS": "",
                                    "MCP_BRIDGE_API_KEY": "P2A_SYNTHETIC_AUTH"}):
            cls.gateway = load_module("aee_p2a_offline_gateway", ROOT / "mcp_gateway.py")

    @classmethod
    def tearDownClass(cls):
        cls.initial_store.cleanup()
        sys.modules.pop("aee_p2a_offline_gateway", None)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aee-baseline-job-")
        self.addCleanup(self.temp.cleanup)
        self.gateway.JOB_STORE = self.gateway.JobStore(Path(self.temp.name) / "jobs")

    def job(self):
        return self.gateway.JOB_STORE.create("codex", "synthetic task", str(ROOT), "read_only")

    def test_local_tool_surface_and_dispatch_schema(self):
        tools = {t.name: t for t in self.gateway.mcp._tool_manager.list_tools()}
        self.assertEqual(set(tools), {"aee_status", "aee_agents", "aee_exec",
                                     "aee_dispatch", "aee_job_status", "aee_job_result"})
        self.assertEqual(set(tools["aee_dispatch"].parameters["required"]),
                         {"agent", "working_directory"})

    def test_restricted_tool_manager_removes_exec(self):
        # Separate module so filtering does not affect the other offline cases.
        name = "aee_p2a_restricted_gateway"
        with patch.dict(os.environ, {"A3_JOB_STORE_DIR": self.temp.name,
                                    "AEE_MCP_SURFACE": "restricted",
                                    "AEE_MCP_EXPOSED_TOOLS": "aee_status,aee_agents,aee_dispatch,aee_job_status,aee_job_result"}):
            gateway = load_module(name, ROOT / "mcp_gateway.py")
        self.addCleanup(sys.modules.pop, name, None)
        gateway._apply_tool_exposure_filter()
        self.assertEqual({t.name for t in gateway.mcp._tool_manager.list_tools()},
                         {"aee_status", "aee_agents", "aee_dispatch", "aee_job_status", "aee_job_result"})

    def test_invalid_agents_and_modes_never_launch(self):
        with patch.object(self.gateway, "_start_job_worker") as worker:
            for agent in ["hermes", "claude", "nmap"]:
                with self.assertRaises(self.gateway._JobError) as error:
                    self.gateway.dispatch_job(agent, "x", str(ROOT), "read_only")
                self.assertEqual(error.exception.code, "INVALID_AGENT")
            with self.assertRaises(self.gateway._JobError) as error:
                self.gateway.dispatch_job("codex", "x", str(ROOT), "read_write")
            self.assertEqual(error.exception.code, "INVALID_MODE")
            worker.assert_not_called()

    def test_workdir_traversal_and_symlink_escape_rejected(self):
        allowed = Path(self.temp.name) / "allowed"
        outside = Path(self.temp.name) / "outside"
        allowed.mkdir()
        outside.mkdir()
        (allowed / "escape").symlink_to(outside, target_is_directory=True)
        with patch.object(self.gateway, "A3_DISPATCH_ALLOWED_ROOTS", [str(allowed)]):
            self.assertEqual(self.gateway._validate_working_directory(str(allowed)), str(allowed))
            for candidate in [str(allowed / ".." / "outside"), str(allowed / "escape")]:
                with self.assertRaises(self.gateway._JobError) as error:
                    self.gateway._validate_working_directory(candidate)
                self.assertEqual(error.exception.code, "INVALID_WORKING_DIRECTORY")

    def test_dispatch_returns_queued_and_sequential_busy(self):
        identity = self.gateway.ExecutorIdentity("/synthetic/codex", "a" * 64, "codex-cli fixture")
        with patch.object(self.gateway, "_start_job_worker") as worker, \
                patch.object(self.gateway, "resolve_codex", return_value=identity):
            record = self.gateway.dispatch_job("codex", "x", str(ROOT), "read_only")
            self.assertEqual(record["status"], "queued")
            worker.assert_called_once()
            with self.assertRaises(self.gateway._JobError) as error:
                self.gateway.dispatch_job("codex", "x", str(ROOT), "read_only")
            self.assertEqual(error.exception.code, "BUSY")

    def test_store_reload_and_private_permissions(self):
        record = self.job()
        store = self.gateway.JOB_STORE
        from completed_result_fixtures import successful_fields
        store.update(record["job_id"], **successful_fields("synthetic summary"))
        reloaded = self.gateway.JobStore(store._root)
        self.assertEqual(reloaded.get(record["job_id"])["summary"], "synthetic summary")
        self.assertEqual(store._root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(store._job_path(record["job_id"]).stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(store._root.glob(".tmp-*")))

    def test_malformed_job_ids_rejected(self):
        for job_id in ["../escape", "A3JOB-20261002-0001", "not-a-job"]:
            with self.assertRaises(self.gateway._JobError) as error:
                self.gateway.JOB_STORE.get(job_id)
            self.assertEqual(error.exception.code, "JOB_NOT_FOUND")

    def test_result_waits_for_terminal_state(self):
        record = self.job()
        result = json.loads(asyncio.run(self.gateway.aee_job_result(record["job_id"])))
        self.assertEqual(result["error_code"], "JOB_NOT_COMPLETE")
        from completed_result_fixtures import successful_fields
        self.gateway.JOB_STORE.update(record["job_id"], **successful_fields("done"))
        result = json.loads(asyncio.run(self.gateway.aee_job_result(record["job_id"])))
        self.assertTrue(result["ok"])
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("log_excerpt", result)

    def test_exec_rejections_do_not_launch_subprocess(self):
        with patch.object(self.gateway.subprocess, "run") as runner:
            for args in [["curl", "https://example.invalid"], ["ls", "/etc"],
                         ["cat", "/outside-host/.ssh/id_rsa"]]:
                self.assertFalse(self.gateway.bounded_exec(args)["ok"])
            runner.assert_not_called()

    def test_codex_success_contract_without_real_agent(self):
        record = self.job()
        from aee.mcp_runtime.executor import ExecutionResult
        identity = self.gateway.ExecutorIdentity("/synthetic/codex", "a" * 64, "codex-cli fixture")
        from completed_result_fixtures import successful_fields
        proof = successful_fields()['execution']
        outcome = ExecutionResult("synthetic final message", "synthetic log", False, 0, {"files": 0},
                                  proof['native_tool_evidence'], proof['result_contract'])
        with patch.object(self.gateway, "resolve_codex", return_value=identity), \
                patch.object(self.gateway, "execute_codex", return_value=outcome) as executor:
            self.gateway.run_codex_job(record["job_id"], "synthetic task", str(ROOT))
        result = self.gateway.JOB_STORE.get(record["job_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["summary"], "synthetic final message")
        self.assertEqual(executor.call_args.args[2], "synthetic task")
        self.assertEqual(result["execution"]["configured"]["sandbox"], "read-only")
        self.assertEqual(result["execution"]["configured"]["approval_policy"], "never")
        self.assertNotIn(record["job_id"], self.gateway.JOB_STORE._leases)

    def test_codex_timeout_and_nonzero_exit_contracts(self):
        identity = self.gateway.ExecutorIdentity("/synthetic/codex", "a" * 64, "codex-cli fixture")
        for code in ["EXECUTION_TIMEOUT", "EXECUTION_FAILED"]:
            record = self.job()
            with patch.object(self.gateway, "resolve_codex", return_value=identity), \
                    patch.object(self.gateway, "execute_codex", side_effect=self.gateway._JobError(code, "synthetic failure")):
                self.gateway.run_codex_job(record["job_id"], "synthetic task", str(ROOT))
            result = self.gateway.JOB_STORE.get(record["job_id"])
            self.assertEqual(result["status"], "timed_out" if code == "EXECUTION_TIMEOUT" else "failed")
            self.assertEqual(result["error_code"], code)


class CaptureSecretGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scanner = load_module("aee_p2a_secret_gate", ROOT / "scripts/scan-aee-baseline-secrets.py")

    def test_known_opaque_secret_reports_location_only(self):
        value = b"P2A_SYNTHETIC_OPAQUE_VALUE"
        issues, _ = self.scanner.findings("sample.txt", b"prefix\n" + value, [value])
        self.assertEqual(issues, [{"path": "sample.txt", "line": 2, "kind": "known_local_secret"}])
        self.assertNotIn(value.decode(), json.dumps(issues))

    def test_pattern_key_is_not_reported_verbatim(self):
        value = b"sk-" + b"Z" * 30
        issues, _ = self.scanner.findings("sample.txt", value, [])
        self.assertEqual(issues[0]["kind"], "openai_key")
        self.assertNotIn(value.decode(), json.dumps(issues))

    def test_colon_assignment_does_not_exempt_field_name(self):
        value = b"P2A_OPAQUE_RANDOM_VALUE"
        issues, exceptions = self.scanner.findings("sample.txt", b'API_KEY: "' + value + b'"', [])
        self.assertEqual(issues[0]["kind"], "literal_credential_assignment")
        self.assertEqual(exceptions, 0)

    def test_fixture_exception_never_suppresses_known_secret(self):
        value = b"sandbox-test-key"
        issues, exceptions = self.scanner.findings("tests/test_b4_db_guard_fail_closed.py",
                                                  b'api_key="' + value + b'"', [value])
        self.assertEqual(issues[0]["kind"], "known_local_secret")
        self.assertEqual(exceptions, 1)


if __name__ == "__main__":
    unittest.main()
