"""Host-agnostic metadata contract for the MCP gateway (REV 2 candidate).

Fixture-import style like ``test_p2c_gateway``: every check runs against an
in-process gateway import with a fixture job store. No live gateway, no
dispatch, no network. Covers the REV 2 operator decision (2026-10-18):
* tool descriptions are generic AEE wording (no hardcoded hostnames),
* aee_status reports OS-native hostname/platform/execution identity for
  mocked Linux identities (Arch-based and non-Arch) and a mocked macOS
  identity, while legacy identity fields stay backward-compatible,
* aee_agents follows the adapter registry instead of a hardcoded roster,
* the exposed tool surface (names + input schemas) stays byte-equal to the
  deployed base commit.
"""
import asyncio
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aee.mcp_runtime.store import JobError

ROOT = Path(__file__).resolve().parents[2]
BASE_COMMIT = "3d95cedfe3ec637f7b665516cacb7c9438a16ff1"
EXPOSED = ["aee_status", "aee_agents", "aee_dispatch",
           "aee_job_status", "aee_job_result", "aee_job_cancel"]
# Runtime identity fields defined at the base deployment commit must remain.
LEGACY_NODE_KEYS = {"hostname", "os", "kernel", "boot_time", "uptime_seconds",
                    "cpu", "memory", "disks"}
LEGACY_GATEWAY_KEYS = {"bind", "gateway_started_at", "gateway_uptime_seconds",
                       "localhost_only", "auth_required"}


def _import_gateway(case, source=None):
    """Import a gateway module fixture-style (clean env, tmp job store)."""
    tmp = tempfile.TemporaryDirectory(prefix="aee-gateway-meta-")
    case.addCleanup(tmp.cleanup)
    env = {
        "A3_JOB_STORE_DIR": str(Path(tmp.name) / "jobs"),
        "A3_CODEX_BIN": str(Path(tmp.name) / "absent-codex"),
        "AEE_MCP_PORT": "8791", "AEE_MCP_SURFACE": "restricted",
        "AEE_MCP_REQUIRE_AUTH": "true", "MCP_BRIDGE_API_KEY": "fixture-only-auth",
    }
    starter = patch.dict(os.environ, env, clear=True)
    starter.start()
    case.addCleanup(starter.stop)
    name = "aee_gateway_meta_" + str(len(sys.modules))
    spec = importlib.util.spec_from_file_location(
        name, source if source is not None else ROOT / "mcp_gateway.py")
    gateway = importlib.util.module_from_spec(spec)
    sys.modules[name] = gateway
    case.addCleanup(sys.modules.pop, name, None)
    spec.loader.exec_module(gateway)
    case.addCleanup(gateway.JOB_STORE.close)
    return gateway


def _import_base_gateway(case):
    """Import the base deployment commit's gateway for surface comparison."""
    out = subprocess.run(
        ["git", "show", f"{BASE_COMMIT}:mcp_gateway.py"],
        cwd=ROOT, capture_output=True, text=True, check=True)
    tmp = tempfile.TemporaryDirectory(prefix="aee-meta-base-")
    case.addCleanup(tmp.cleanup)
    base = Path(tmp.name) / "base_mcp_gateway.py"
    base.write_text(out.stdout)
    return _import_gateway(case, source=base)


def _tool_by_name(gateway, name):
    for tool in gateway.mcp._tool_manager.list_tools():
        if tool.name == name:
            return tool
    raise AssertionError(f"tool {name} is not registered")


def _mock_identity(case, hostname, *, system="Linux", release="9.9.9-meta",
                   machine="x86_64", distro_pretty=None, mac_ver=None):
    """Patch the OS-native seams to simulate one host identity."""
    plats = [
        patch("socket.gethostname", return_value=hostname),
        patch("platform.system", return_value=system),
        patch("platform.release", return_value=release),
        patch("platform.machine", return_value=machine),
        patch("platform.platform", return_value=f"{system}-{release}-{machine}"),
    ]
    if system == "Darwin":
        plats.append(patch("platform.mac_ver", return_value=(mac_ver, "", "")))
        plats.append(patch("platform.freedesktop_os_release",
                           side_effect=OSError("/etc/os-release not found")))
    else:
        plats.append(patch("platform.freedesktop_os_release",
                           return_value={"PRETTY_NAME": distro_pretty}))
    for p in plats:
        p.start()
        case.addCleanup(p.stop)


class ToolMetadataContract(unittest.TestCase):
    """Descriptions are generic AEE wording; no hardcoded hostnames."""

    def setUp(self):
        self.g = _import_gateway(self)

    def test_exposed_tools_present(self):
        names = {t.name for t in self.g.mcp._tool_manager.list_tools()}
        self.assertTrue(set(EXPOSED) <= names)

    def test_descriptions_are_host_agnostic(self):
        for name in EXPOSED:
            desc = _tool_by_name(self.g, name).description
            self.assertTrue(desc, name)
            self.assertNotIn("Omarchy", desc, name)
            self.assertNotIn("-A2", desc, name)
            self.assertNotIn("-A3", desc, name)

    def test_dispatch_description_contract(self):
        desc = _tool_by_name(self.g, "aee_dispatch").description
        for needle in ("OS-identity", "Codex CLI", "Claude Code CLI",
                       "working_directory is a job parameter",
                       "aee_job_status", "aee_job_result", "aee_job_cancel"):
            self.assertIn(needle, desc)

    def test_status_description_is_os_native_and_generic(self):
        desc = _tool_by_name(self.g, "aee_status").description
        self.assertIn("OS-native", desc)
        self.assertNotIn("Omarchy", desc)

    def test_agents_description_is_registry_driven(self):
        desc = _tool_by_name(self.g, "aee_agents").description
        self.assertIn("Agent Registry", desc)
        self.assertNotIn("hermes, codex", desc)  # hardcoded roster removed


class StatusOSNativeIdentity(unittest.TestCase):
    """aee_status reports hostname/platform/identity via OS-native APIs."""

    def setUp(self):
        self.g = _import_gateway(self)

    def _node(self, **kwargs):
        _mock_identity(self, **kwargs)
        return json.loads(asyncio.run(self.g.aee_status()))["node"]

    def test_linux_arch_identity(self):
        node = self._node(hostname="omarchy-a2", distro_pretty="Arch Linux")
        self.assertEqual(node["hostname"], "omarchy-a2")
        self.assertEqual(node["os"], "Linux-9.9.9-meta-x86_64")
        self.assertEqual(node["kernel"], "9.9.9-meta")
        self.assertEqual(node["platform"], {
            "system": "Linux", "release": "9.9.9-meta",
            "machine": "x86_64", "distro": "Arch Linux"})

    def test_linux_other_host_identity(self):
        node = self._node(hostname="some-aee-host",
                          release="6.6.75-generic",
                          distro_pretty="Ubuntu 24.04 LTS")
        self.assertEqual(node["hostname"], "some-aee-host")
        self.assertEqual(node["platform"]["distro"], "Ubuntu 24.04 LTS")
        self.assertEqual(node["kernel"], "6.6.75-generic")

    def test_macos_identity(self):
        node = self._node(hostname="aee-mac-mini", system="Darwin",
                          release="23.5.0", machine="arm64", mac_ver="14.5.0")
        self.assertEqual(node["hostname"], "aee-mac-mini")
        self.assertEqual(node["os"], "Darwin-23.5.0-arm64")
        self.assertEqual(node["kernel"], "23.5.0")
        self.assertEqual(node["platform"], {
            "system": "Darwin", "release": "23.5.0",
            "machine": "arm64", "distro": "macOS 14.5.0"})

    def test_execution_identity_is_os_native(self):
        mocks = [
            patch("os.getuid", return_value=1000), patch("os.geteuid", return_value=1000),
            patch("os.getgid", return_value=101), patch("os.getegid", return_value=101),
            patch("os.getgrouplist", return_value=[1000, 101]),
            patch("pwd.getpwuid", return_value=SimpleNamespace(pw_name="sscomp")),
            patch("grp.getgrgid", side_effect=lambda g: SimpleNamespace(
                gr_name={1000: "sscomp", 101: "wheel"}[g])),
        ]
        for p in mocks:
            p.start()
            self.addCleanup(p.stop)
        _mock_identity(self, hostname="omarchy-a2", distro_pretty="Arch Linux")
        identity = json.loads(asyncio.run(self.g.aee_status()))["node"]["execution_identity"]
        self.assertEqual(identity, {
            "username": "sscomp", "uid": 1000, "gid": 101,
            "effective_uid": 1000, "effective_gid": 101,
            "groups": ["sscomp", "wheel"]})

    def test_execution_identity_degrades_without_passwd_entry(self):
        # No passwd entry and a failing getgrouplist must not crash status.
        mocks = [
            patch("os.getuid", return_value=1234), patch("os.geteuid", return_value=1234),
            patch("os.getgid", return_value=1234), patch("os.getegid", return_value=1234),
            patch("pwd.getpwuid", side_effect=KeyError("not found")),
            patch("os.getgrouplist", side_effect=OSError("no groups")),
        ]
        for p in mocks:
            p.start()
            self.addCleanup(p.stop)
        _mock_identity(self, hostname="nomad-host", distro_pretty=None)
        identity = json.loads(asyncio.run(self.g.aee_status()))["node"]["execution_identity"]
        self.assertEqual(identity["username"], None)
        self.assertEqual(identity["groups"], ["1234"])


class StatusBackwardCompatibility(unittest.TestCase):
    """Pre-existing runtime identity fields keep names (and gateway keys)."""

    def setUp(self):
        self.g = _import_gateway(self)

    def test_node_identity_fields_remains(self):
        payload = json.loads(asyncio.run(self.g.aee_status()))
        self.assertTrue(LEGACY_NODE_KEYS <= set(payload["node"]))
        self.assertTrue(LEGACY_GATEWAY_KEYS <= set(payload["gateway"]))
        for key in ("aee_gateway_version", "aee_product_version"):
            self.assertIn(key, payload)


class AgentsFollowRegistry(unittest.TestCase):
    """aee_agents lists from the adapter registry, not a hardcoded roster."""

    def setUp(self):
        self.g = _import_gateway(self)

    def _specs(self):
        return {
            "codex": SimpleNamespace(name="codex", read_only_enforcement="NATIVE_SANDBOX",
                                     enforcement_note="fixture native sandbox"),
            "zzz-custom": SimpleNamespace(name="zzz-custom", read_only_enforcement="UNSUPPORTED",
                                          enforcement_note="fixture unsupported"),
        }

    def test_listing_follows_registry(self):
        specs = self._specs()
        reg = patch.object(self.g, "adapter_specs", lambda: specs)
        reg.start(); self.addCleanup(reg.stop)
        probe = patch.object(
            self.g, "resolve_codex",
            side_effect=JobError("EXECUTOR_RESOLUTION_FAILED", "fixture: absent"))
        probe.start(); self.addCleanup(probe.stop)
        agents = json.loads(asyncio.run(self.g.aee_agents()))["agents"]
        # Registry order (sorted), including the un-discoverable custom entry.
        self.assertEqual([a["agent"] for a in agents], ["codex", "zzz-custom"])
        # Registry decoration is present for both entries.
        self.assertEqual(agents[0]["read_only_enforcement"], "NATIVE_SANDBOX")
        self.assertEqual(agents[1]["read_only_enforcement"], "UNSUPPORTED")
        self.assertFalse(agents[1]["dispatchable"])
        # Only the registry set is listed — hermes/claude/pi stay out.
        self.assertNotIn("hermes", [a["agent"] for a in agents])
        self.assertNotIn("claude", [a["agent"] for a in agents])
        self.assertNotIn("pi", [a["agent"] for a in agents])

    def test_registry_change_changes_listing(self):
        reg = patch.object(self.g, "adapter_specs", lambda: {})
        reg.start(); self.addCleanup(reg.stop)
        agents = json.loads(asyncio.run(self.g.aee_agents()))["agents"]
        self.assertEqual(agents, [])


class ToolSurfaceLockedToBase(unittest.TestCase):
    """Names + input schemas stay byte-equal to the deployed base commit."""

    def test_schemas_equal_base_commit(self):
        base = _import_base_gateway(self)
        cand = _import_gateway(self)
        base_tools = {t.name: t for t in base.mcp._tool_manager.list_tools()}
        cand_tools = {t.name: t for t in cand.mcp._tool_manager.list_tools()}
        self.assertEqual(set(base_tools), set(cand_tools))
        for name, tool in base_tools.items():
            self.assertEqual(tool.parameters, cand_tools[name].parameters, name)

    def test_signatures_equal_base_commit(self):
        base = _import_base_gateway(self)
        cand = _import_gateway(self)
        for name in EXPOSED + ["aee_exec"]:
            self.assertEqual(
                str(inspect.signature(getattr(base, name))),
                str(inspect.signature(getattr(cand, name))),
                name)


if __name__ == "__main__":
    unittest.main()