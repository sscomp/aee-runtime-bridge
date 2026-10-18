"""Write-capable dispatch contract (A2 write dispatch recovery).

Offline: verifies the mode enum in the connector-facing schema, the
fail-closed write gates (approval secret, operator write-root scope,
AEE-owned region exclusion), the per-adapter write argv mapping, the
authorization audit proof and unchanged read_only behavior. No agent is
launched; every worker start is patched out.
"""
import asyncio
import importlib.util
import json
import os
import re
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


class WriteDispatchContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store_dir = tempfile.TemporaryDirectory(prefix="aee-write-contract-")
        with patch.dict(os.environ, {
            "A3_JOB_STORE_DIR": str(Path(cls.store_dir.name) / "jobs"),
            "AEE_MCP_SURFACE": "local", "AEE_MCP_PORT": "8790",
            "AEE_MCP_EXPOSED_TOOLS": "",
            "MCP_BRIDGE_API_KEY": "P2A_SYNTHETIC_AUTH",
            "A3_WORKSPACE_MANIFEST": "",
        }):
            # No write scope / approval configured at import: gates must fail
            # closed before any test fixture configures them per-case.
            cls.gateway = load_module("aee_write_dispatch_gateway", ROOT / "mcp_gateway.py")

    @classmethod
    def tearDownClass(cls):
        cls.store_dir.cleanup()
        sys.modules.pop("aee_write_dispatch_gateway", None)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aee-write-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.fixture = Path(self.temp.name) / "fixture"
        self.fixture.mkdir()
        self.gateway.JOB_STORE = self.gateway.JobStore(
            Path(self.temp.name) / "jobs")

    def token(self):
        return "SYNTHETIC_APPROVAL_" + "a" * 16

    def configure_write(self, root=None, token=None):
        token = token if token is not None else self.token()
        token_patch = patch.object(self.gateway, "WRITE_APPROVAL_TOKEN",
                                   token.encode() if token else None)
        token_patch.start()
        self.addCleanup(token_patch.stop)
        roots_patch = patch.object(
            self.gateway, "WRITE_ALLOWED_ROOTS",
            [Path(root)] if root else [])
        roots_patch.start()
        self.addCleanup(roots_patch.stop)
        return token

    def dispatch(self, metadata=None, workdir=None, agent="claude", mode="write"):
        identity = {"adapter": agent, "executable": "/synthetic/absent",
                    "executable_sha256": "a" * 64, "version": "fixture",
                    "read_only_enforcement": "NATIVE_SANDBOX",
                    "enforcement_note": "fixture note",
                    "write_enforcement": "NATIVE_SANDBOX",
                    "write_note": "fixture write note"}
        with patch.object(self.gateway, "_start_job_worker") as worker, \
                patch.object(self.gateway, "adapter_execution_metadata",
                             return_value={"kind": "job-adapter", "adapter": identity}):
            record = self.gateway.dispatch_job(
                agent, "fixture write task", workdir or str(self.fixture), mode,
                metadata=metadata)
        return record

    # --- connector-facing schema -----------------------------------------

    def test_connector_schema_publishes_mode_enum(self):
        tools = {t.name: t for t in self.gateway.mcp._tool_manager.list_tools()}
        mode = tools["aee_dispatch"].parameters["properties"]["mode"]
        self.assertEqual(mode.get("type"), "string")
        self.assertEqual(mode.get("enum"), ["read_only", "write", "autonomous"])
        self.assertEqual(mode.get("default"), "read_only")

    def test_dispatch_description_names_both_modes_and_approval(self):
        description = " ".join(
            tool.description for tool in self.gateway.mcp._tool_manager.list_tools()
            if tool.name == "aee_dispatch")
        self.assertIn("read_only", description)
        self.assertIn("acceptEdits", description)
        self.assertIn("workspace-write", description)
        self.assertIn("approval", description)

    # --- fail-closed gates -------------------------------------------------

    def test_unknown_mode_still_rejected_structured(self):
        with patch.object(self.gateway, "_start_job_worker") as worker:
            with self.assertRaises(self.gateway._JobError) as error:
                self.gateway.dispatch_job("claude", "x", str(self.fixture), "read_write")
            self.assertEqual(error.exception.code, "INVALID_MODE")
            self.assertIn("read_write", error.exception.message)
            worker.assert_not_called()

    def test_write_without_operator_token_configured(self):
        # No approval token (module default) and no roots: fail closed first
        # at the approval gate with an actionable code.
        with patch.object(self.gateway, "_start_job_worker") as worker:
            with self.assertRaises(self.gateway._JobError) as error:
                self.gateway.dispatch_job("claude", "x", str(self.fixture), "write",
                                          metadata={"approval": "tok"})
            self.assertEqual(error.exception.code, "WRITE_MODE_NOT_CONFIGURED")
            worker.assert_not_called()

    def test_write_with_missing_or_wrong_approval(self):
        self.configure_write(root=self.fixture)
        for metadata in [None, {}, {"approval": ""}, {"approval": "wrong"}]:
            with self.assertRaises(self.gateway._JobError) as error:
                self.dispatch(metadata=metadata)
            self.assertEqual(error.exception.code, "WRITE_APPROVAL_INVALID", metadata)

    def test_write_without_any_write_roots_configured(self):
        token = self.configure_write(root=None)
        with self.assertRaises(self.gateway._JobError) as error:
            self.dispatch(metadata={"approval": token})
        self.assertEqual(error.exception.code, "WRITE_SCOPE_NOT_CONFIGURED")

    def test_write_outside_operator_roots(self):
        token = self.configure_write(root=Path(self.temp.name) / "other-root")
        with self.assertRaises(self.gateway._JobError) as error:
            self.dispatch(metadata={"approval": token})
        self.assertEqual(error.exception.code, "INVALID_WRITE_SCOPE")

    def test_write_rejects_aee_operational_regions(self):
        # An AEE-owned operational region is denied even if the operator's
        # root list accidentally contains it (defense in depth).
        token = self.configure_write(root=Path.home() / "aee")
        with self.assertRaises(self.gateway._JobError) as error:
            self.dispatch(metadata={"approval": token},
                          workdir=str(Path.home() / "aee" / "releases"))
        self.assertEqual(error.exception.code, "INVALID_WRITE_SCOPE")

    def test_write_metadata_must_be_object(self):
        with patch.object(self.gateway, "_start_job_worker") as worker:
            with self.assertRaises(self.gateway._JobError) as error:
                self.gateway.dispatch_job("claude", "x", str(self.fixture), "write",
                                          metadata="not-a-dict")
            self.assertEqual(error.exception.code, "INVALID_METADATA")
            worker.assert_not_called()

    def test_write_refused_for_unqualified_adapters(self):
        token = self.configure_write(root=self.fixture)
        with self.assertRaises(self.gateway._JobError) as error:
            self.gateway.dispatch_job("pi", "x", str(self.fixture), "write",
                                      metadata={"approval": token})
        self.assertEqual(error.exception.code, "ADAPTER_UNSUPPORTED")

    # --- authorized admission + audit proof --------------------------------

    def test_authorized_write_admits_with_fingerprint_and_scope(self):
        token = self.configure_write(root=self.fixture)
        record = self.dispatch(metadata={"approval": token, "requested_by": "ChatGPT-e2e"})
        self.assertEqual(record["mode"], "write")
        authorization = record["execution"]["authorization"]
        self.assertEqual(authorization["mode"], "write")
        self.assertNotIn("approval", authorization["provenance"])
        self.assertEqual(authorization["provenance"].get("requested_by"), "ChatGPT-e2e")
        self.assertEqual(authorization["approval_fingerprint"],
                         self.gateway.hashlib.sha256(token.encode()).hexdigest()[:16])
        self.assertEqual(authorization["write_root"], str(self.fixture.resolve()))
        # The raw token must not appear anywhere in the persisted record.
        blob = json.dumps(record, ensure_ascii=False)
        self.assertNotIn(token, blob)

    def test_write_argv_mapping_per_adapter(self):
        codex = self.gateway.adapter_specs()["codex"]
        claude = self.gateway.adapter_specs()["claude"]
        self.assertIn("workspace-write", codex.build_argv("/fixture", "codex", "write"))
        self.assertIn("read-only", codex.build_argv("/fixture", "codex", "read_only"))
        self.assertNotIn("workspace-write", codex.build_argv("/fixture", "codex", "read_only"))
        claude_write = claude.build_argv("/fixture", "claude", "write")
        self.assertIn("acceptEdits", claude_write)
        self.assertNotIn("plan", [a for a in claude_write if a != "acceptEdits"])
        self.assertIn("plan", claude.build_argv("/fixture", "claude", "read_only"))
        self.assertNotIn("acceptEdits", claude.build_argv("/fixture", "claude", "read_only"))

    def test_read_only_regressions_unchanged_for_all_adapters(self):
        for agent, needle in [("codex", "read-only"), ("claude", "plan"), ("pi", "read")]:
            spec = self.gateway.adapter_specs()[agent]
            argv = spec.build_argv("/fixture", agent, "read_only")
            self.assertIn(needle, argv, agent)
            self.assertNotIn("workspace-write", argv, agent)
            self.assertNotIn("acceptEdits", argv, agent)

    def test_store_persists_write_mode_and_rejects_others(self):
        token = self.configure_write(root=self.fixture)
        record = self.dispatch(metadata={"approval": token})
        reloaded = self.gateway.JobStore(self.gateway.JOB_STORE._root)
        self.assertEqual(reloaded.get(record["job_id"])["mode"], "write")
        for mode in ["read_write", "WRITE", ""]:
            with self.assertRaises(self.gateway._JobError):
                self.gateway.JOB_STORE.create("claude", "task", str(self.fixture), mode)

    def test_hermes_write_and_read_only_refused(self):
        for mode in ("read_only", "write"):
            with self.assertRaises(self.gateway._JobError) as error:
                self.gateway.dispatch_job("hermes", "x", str(self.fixture), mode)
            self.assertEqual(error.exception.code, "ADAPTER_UNSUPPORTED")

    def test_production_codex_contract_refuses_write(self):
        # The broker-mandated codex contract executes a reviewed read-only
        # snapshot; write mode must refuse there rather than be discarded.
        token = self.configure_write(root=self.fixture)
        identity = self.gateway.ExecutorIdentity("/synthetic/codex", "a" * 64, "codex-cli fixture")
        profile = self.gateway.select_profile()
        with patch.object(self.gateway, "deployment_policy",
                          return_value={"codex": {}}), \
                patch.object(self.gateway, "deployment_gate_approved", return_value=True), \
                patch.object(self.gateway, "read_manifest", return_value={}), \
                patch.object(self.gateway, "require_deployment_ready", return_value=None), \
                patch.object(self.gateway, "resolve_codex", return_value=identity), \
                patch.object(self.gateway, "execution_metadata",
                             return_value=self.gateway.execution_metadata(profile, identity)), \
                patch.object(self.gateway, "_start_job_worker") as worker:
            with self.assertRaises(self.gateway._JobError) as error:
                self.gateway.dispatch_job("codex", "x", str(self.fixture), "write",
                                          metadata={"approval": token})
            self.assertEqual(error.exception.code, "INVALID_MODE")


if __name__ == "__main__":
    unittest.main()