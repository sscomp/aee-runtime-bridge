"""MCP/ASGI integration in memory; no live listeners, keys or jobs."""
import asyncio
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[2]
FIVE = {"aee_status", "aee_agents", "aee_dispatch", "aee_job_status", "aee_job_result"}


class HardenedProtocol(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="aee-protocol-fixture-")
        cls.binary = Path(cls.build.name) / "codex"
        subprocess.run(["/usr/bin/gcc", "-Wall", "-Wextra", "-O2",
                        str(Path(__file__).parent / "fixtures/codex_probe.c"), "-o", str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aee-p2b-protocol-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        source = self.root / "source"
        source.mkdir()
        (source / "safe.txt").write_text("reviewed fixture")
        self.source = str(source)
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"safe.txt": hashlib.sha256((source / "safe.txt").read_bytes()).hexdigest()}))
        self.env = {"A3_JOB_STORE_DIR": str(self.root / "jobs"), "A3_DISPATCH_ALLOWED_ROOTS": self.source,
                    "A3_CODEX_BIN": str(self.binary), "A3_WORKSPACE_MANIFEST": str(manifest),
                    "AEE_MCP_PORT": "8791", "AEE_MCP_SURFACE": "restricted",
                    "MCP_BRIDGE_API_KEY": "P2B_SYNTHETIC_AUTH", "AEE_MCP_REQUIRE_AUTH": "true"}
        self.gateway = self.load(self.env)
        self.addCleanup(self.gateway.JOB_STORE.close)
        self.gateway._apply_tool_exposure_filter()

    def load(self, environment):
        name = "aee_p2b_protocol_" + str(len(sys.modules))
        spec = importlib.util.spec_from_file_location(name, ROOT / "mcp_gateway.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        with patch.dict(os.environ, environment, clear=True):
            spec.loader.exec_module(module)
        return module

    async def with_client(self, action):
        gateway = self.gateway
        app = gateway._build_auth_middleware(gateway.mcp.streamable_http_app())
        async with gateway.mcp.session_manager.run():
            # ASGITransport makes no socket connection. Use the SDK's trusted
            # loopback Host header so its DNS-rebinding defense stays enabled.
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8791") as client:
                async def rpc(method, params=None, key="P2B_SYNTHETIC_AUTH"):
                    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
                    if key is not None:
                        headers["Authorization"] = "Bearer " + key
                    body = {"jsonrpc": "2.0", "id": 1, "method": method}
                    if params is not None:
                        body["params"] = params
                    response = await client.post("/mcp", json=body, headers=headers)
                    return response.status_code, response.json()
                await rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "p2b-isolated", "version": "1"}})
                await action(rpc)

    @staticmethod
    def payload(body):
        return json.loads(body["result"]["content"][0]["text"])

    def test_exact_restricted_surface_future_tools_never_leak(self):
        calls = []

        @self.gateway.mcp.tool(name="aee_future_full_tool")
        async def future_tool():
            calls.append(True)
            return "UNSAFE"

        async def checks(rpc):
            status, body = await rpc("tools/list")
            self.assertEqual(status, 200)
            self.assertEqual({tool["name"] for tool in body["result"]["tools"]}, FIVE)
            for tool in ["aee_exec", "aee_future_full_tool"]:
                _, body = await rpc("tools/call", {"name": tool, "arguments": {}})
                self.assertTrue(body["result"]["isError"])
            self.assertFalse(calls)
        asyncio.run(self.with_client(checks))

    def test_missing_empty_extra_or_unsafe_restricted_configuration(self):
        # Absent exposure env is safe by construction; explicit empty/expanded
        # values fail startup. Both port classification and auth fail closed.
        for extra in [{"AEE_MCP_EXPOSED_TOOLS": ""}, {"AEE_MCP_EXPOSED_TOOLS": ",".join(FIVE | {"aee_exec"})},
                      {"AEE_MCP_SURFACE": "local"}, {"AEE_MCP_SURFACE": "unknown"},
                      {"AEE_MCP_REQUIRE_AUTH": "false"}]:
            with self.assertRaises(ValueError):
                self.load({**self.env, **extra})

    def test_full_surface_future_tool_stays_local_only(self):
        local = self.load({**self.env, "AEE_MCP_PORT": "8790", "AEE_MCP_SURFACE": "local"})
        self.addCleanup(local.JOB_STORE.close)

        @local.mcp.tool(name="aee_future_full_tool")
        async def future_tool():
            return "local fixture"

        local._apply_tool_exposure_filter()
        self.assertEqual({t.name for t in local.mcp._tool_manager.list_tools()}, FIVE | {"aee_exec", "aee_future_full_tool"})
        self.assertEqual({t.name for t in self.gateway.mcp._tool_manager.list_tools()}, FIVE)

    def test_auth_invalid_job_and_profile_errors_structured(self):
        async def checks(rpc):
            for key in [None, "P2B_INVALID_AUTH"]:
                status, _ = await rpc("tools/list", key=key)
                self.assertEqual(status, 401)
            for tool in ["aee_job_status", "aee_job_result"]:
                _, body = await rpc("tools/call", {"name": tool, "arguments": {"job_id": "../fixture"}})
                self.assertEqual(self.payload(body)["error_code"], "JOB_NOT_FOUND")
            for extra, code in [({"agent": "hermes"}, "INVALID_AGENT"),
                                ({"mode": "read_write"}, "INVALID_MODE"),
                                ({"model": "other-model"}, "INVALID_EXECUTION_PROFILE"),
                                ({"reasoning_effort": "--help"}, "INVALID_EXECUTION_PROFILE"),
                                ({"execution_profile": "--sandbox"}, "INVALID_EXECUTION_PROFILE")]:
                args = {"agent": "codex", "task": "fixture", "working_directory": self.source, **extra}
                _, body = await rpc("tools/call", {"name": "aee_dispatch", "arguments": args})
                self.assertEqual(self.payload(body)["error_code"], code)
            self.assertEqual(self.gateway.JOB_STORE.list_ids(), [])
        asyncio.run(self.with_client(checks))

    def test_async_dispatch_result_and_identity_agree_with_discovery(self):
        gateway = self.gateway
        discovery = gateway.discover_agent("codex")

        async def checks(rpc):
            args = {"agent": "codex", "task": "--help", "working_directory": self.source,
                    "execution_profile": "codex-readonly-high", "model": "gpt-6.1-sol", "reasoning_effort": "high"}
            _, body = await rpc("tools/call", {"name": "aee_dispatch", "arguments": args})
            queued = self.payload(body)
            self.assertTrue(queued["ok"])
            self.assertEqual(queued["status"], "queued")
            deadline = asyncio.get_running_loop().time() + 8
            while True:
                _, body = await rpc("tools/call", {"name": "aee_job_result", "arguments": {"job_id": queued["job_id"]}})
                result = self.payload(body)
                if result.get("error_code") != "JOB_NOT_COMPLETE":
                    break
                self.assertLess(asyncio.get_running_loop().time(), deadline)
                await asyncio.sleep(0.01)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["summary"], "--help")
            execution = result["execution"]
            self.assertEqual(execution["configured"]["executable"], discovery["executable"])
            self.assertEqual(execution["configured"]["executable_sha256"], discovery["executable_sha256"])
            self.assertEqual(execution["observed"]["agent_version_probe"], discovery["version"])
            self.assertIsNone(execution["observed"]["model"])
            self.assertIsNone(execution["observed"]["reasoning_effort"])
            self.assertEqual(execution["configured"]["model"], "gpt-6.1-sol")
            self.assertEqual(execution["configured"]["reasoning_effort"], "high")
            self.assertEqual(result["artifacts"], [])
            self.assertLessEqual(len(json.dumps(result).encode()), 65536)
        # Preserve the real bounded C probe execution. Add explicit synthetic
        # no-tool corroboration: that probe has no provider/native telemetry.
        from dataclasses import replace
        from completed_result_fixtures import successful_fields
        original_execute = gateway.execute_codex
        def corroborated_fixture(*args, **kwargs):
            result = original_execute(*args, **kwargs)
            proof = successful_fields()['execution']
            return replace(result, evidence=proof['native_tool_evidence'], outcome=proof['result_contract'])
        with patch.object(gateway, 'execute_codex', side_effect=corroborated_fixture):
            asyncio.run(self.with_client(checks))
        # Completion may precede the tiny bookkeeping finally by one instruction.
        for thread in list(gateway._dispatch_pool.values()):
            thread.join(2)
        self.assertFalse(gateway._dispatch_pool)

    def test_eight_concurrent_requests_one_queued_and_seven_busy(self):
        async def checks(rpc):
            request = {"name": "aee_dispatch", "arguments": {"agent": "codex", "task": "fixture", "working_directory": self.source}}
            with patch.object(self.gateway, "_start_job_worker") as worker:
                results = await asyncio.gather(*(rpc("tools/call", request) for _ in range(8)))
            values = [self.payload(body) for _, body in results]
            self.assertEqual(sum(value["ok"] for value in values), 1)
            self.assertEqual(sum(value.get("error_code") == "BUSY" for value in values), 7)
            worker.assert_called_once()
        asyncio.run(self.with_client(checks))

    def test_worker_start_failure_does_not_keep_slot(self):
        with patch.object(self.gateway, "_start_job_worker", side_effect=RuntimeError("fixture")):
            with self.assertRaises(self.gateway._JobError) as error:
                self.gateway.dispatch_job("codex", "fixture", self.source, "read_only")
        self.assertEqual(error.exception.code, "EXECUTION_FAILED")
        self.assertFalse(self.gateway.JOB_STORE.has_active())
        with patch.object(self.gateway.threading.Thread, "start", side_effect=RuntimeError("fixture")):
            with self.assertRaises(self.gateway._JobError):
                self.gateway.dispatch_job("codex", "fixture", self.source, "read_only")
        self.assertFalse(self.gateway._dispatch_pool)
        self.assertFalse(self.gateway.JOB_STORE.has_active())
        with patch.object(self.gateway, "_start_job_worker"):
            self.assertEqual(self.gateway.dispatch_job("codex", "fixture", self.source, "read_only")["status"], "queued")


if __name__ == "__main__":
    unittest.main()
