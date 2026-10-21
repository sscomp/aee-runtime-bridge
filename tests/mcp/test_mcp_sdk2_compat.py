"""SDK-2/protocol-2026-07-28 compatibility tests (WO
ABACUS_AEE_V2_MCP_SDK_PROTOCOL_COMPATIBILITY_REMEDIATION, Phase C).

In-memory ASGI against the real mcp_gateway.py (no live listeners, no real keys).
Runs the same battery under mcp 1.x and 2.x: legacy revisions must keep working
on both lines; the modern per-request envelope MUST work on 2.x and, on 1.x,
must fail explicitly with the -32600 unsupported-version transport rejection
(that is the documented baseline that this work order remediates).

Security parity locked here: bearer fail-closed (401 on missing/bad),
Content-Type enforcement (400), six-tool restricted surface with the local
`aee_exec`/future tools never listed, exposure guard call-path semantics for
both SDK generations.
"""
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

try:
    import importlib.metadata as _md

    _SDK = _md.version("mcp")
    _SDK_MAJOR = int(_SDK.split(".")[0])
except Exception:  # pragma: no cover
    _SDK, _SDK_MAJOR = "unknown", 0

ROOT = Path(__file__).resolve().parents[2]

FIVE = {"aee_status", "aee_agents", "aee_dispatch", "aee_job_status", "aee_job_result"}
SIX = FIVE | {"aee_job_cancel"}

MODERN = "2026-07-28"
LEGACY_NEGOTIATED = "2025-11-25"  # 1.30.0 default; 2.x keeps the same value

ENVELOPE = {
    "io.modelcontextprotocol/protocolVersion": MODERN,
    "io.modelcontextprotocol/clientInfo": {"name": "openai-mcp (Responses API)", "version": "1.0.0"},
    "io.modelcontextprotocol/clientCapabilities": {
        "experimental": {"openai/visibility": {"enabled": True}},
    },
}
MODERN_LEGACY_HEADER = {"Mcp-Protocol-Version": LEGACY_NEGOTIATED}
MODERN_HEADER = {"Mcp-Protocol-Version": MODERN}


class SdkCompat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aee-sdk2-compat-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        source = root / "source"
        source.mkdir()
        (source / "safe.txt").write_text("reviewed fixture")
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps(
            {"safe.txt": hashlib.sha256((source / "safe.txt").read_bytes()).hexdigest()}))
        self.env = {"A3_JOB_STORE_DIR": str(root / "jobs"),
                    "A3_DISPATCH_ALLOWED_ROOTS": str(source),
                    "A3_CODEX_BIN": str(root / "true"),
                    "A3_ADAPTER_CODEX_BIN": str(root / "true"),
                    "A3_WORKSPACE_MANIFEST": str(manifest),
                    "AEE_MCP_HOST": "127.0.0.1",
                    "AEE_MCP_PORT": "8791",
                    "AEE_MCP_SURFACE": "restricted",
                    "MCP_BRIDGE_API_KEY": "SDK_COMPAT_TEST_AUTH",
                    "AEE_MCP_REQUIRE_AUTH": "true"}
        (root / "true").write_text("#!/bin/sh\nexit 0\n")
        os.chmod(root / "true", 0o755)
        self.gateway = self.load(self.env)
        self.addCleanup(self.gateway.JOB_STORE.close)
        self.gateway._apply_tool_exposure_filter()

    def load(self, environment):
        name = "aee_sdk_compat_" + str(len(sys.modules))
        spec = importlib.util.spec_from_file_location(name, ROOT / "mcp_gateway.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        with patch.dict(os.environ, environment, clear=True):
            spec.loader.exec_module(module)
        return module

    def run_async(self, coro):
        return asyncio.run(coro)

    async def _battery(self):
        gateway = self.gateway
        app = gateway._build_auth_middleware(
            gateway.mcp.streamable_http_app(**getattr(gateway, "_STREAMABLE_APP_KWARGS", {})))
        async with gateway.mcp.session_manager.run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://127.0.0.1:8791") as client:
                async def rpc(method, params=None, headers=None, key="SDK_COMPAT_TEST_AUTH"):
                    h = {"Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream"}
                    if key is not None:
                        h["Authorization"] = "Bearer " + key
                    h.update(headers or {})
                    body = {"jsonrpc": "2.0", "id": "t", "method": method, "params": params or {}}
                    return await client.post("/mcp", json=body, headers=h)

                def body_tools(resp):
                    text = resp.text
                    if resp.status_code == 400 and "text/json" or True:
                        try:
                            d = json.loads(text)
                            return d
                        except Exception:
                            pass
                    return resp

                # ---- legacy regressions (both SDK lines) ----
                r = await rpc("initialize", {"protocolVersion": "2025-03-26",
                                             "capabilities": {},
                                             "clientInfo": {"name": "t", "version": "1"}})
                assert resp_ok(r), (r.status_code, r.text[:200])
                d = first_json(r)
                assert d["result"]["protocolVersion"] == "2025-03-26", d

                r = await rpc("tools/list", {})
                assert resp_ok(r), (r.status_code, r.text[:200])
                listed = {t["name"] for t in first_json(r)["result"]["tools"]}
                assert listed == SIX, listed

                # aee_status callable, hidden aee_exec refused on the call path
                r = await rpc("tools/call", {"name": "aee_status", "arguments": {}})
                assert resp_ok(r), (r.status_code, r.text[:200])
                d = first_json(r)
                assert not d["result"]["isError"], d
                r = await rpc("tools/call", {"name": "aee_exec",
                                             "arguments": {"command": "true"}})
                if resp_ok(r):
                    d = first_json(r)
                    assert d["result"]["isError"], d
                    assert "not exposed" in d["result"]["content"][0]["text"], d

                # session continuity on the legacy path: initialize then reuse
                r1 = await rpc("initialize", {"protocolVersion": "2025-03-26",
                                              "capabilities": {},
                                              "clientInfo": {"name": "t", "version": "1"}})
                r2 = await rpc("tools/list", {},
                               headers={"Mcp-Session-Id": r1.headers.get("mcp-session-id", "x")})
                assert resp_ok(r1) and resp_ok(r2), (r1.status_code, r2.status_code)

                # ---- modern era (the remediated path) ----
                modern_headers = {"Mcp-Protocol-Version": MODERN,
                                  "Mcp-Method": "server/discover",
                                  **MODERN_HEADER}
                r = await rpc("server/discover", {"_meta": ENVELOPE}, headers=modern_headers)
                if _SDK_MAJOR >= 2:
                    assert resp_ok(r), (r.status_code, r.text[:300])
                    d = first_json(r)
                    assert "supportedVersions" in d["result"] or "capabilities" in d["result"], d
                else:
                    # 1.x baseline: explicit, safe transport rejection (HTTP 400 -32600)
                    assert r.status_code == 400, (r.status_code, r.text[:200])
                    d = first_json(r)
                    assert d["error"]["code"] == -32600, d

                r = await rpc("tools/list", {"_meta": ENVELOPE},
                              headers={"Mcp-Protocol-Version": MODERN, "Mcp-Method": "tools/list"})
                if _SDK_MAJOR >= 2:
                    assert resp_ok(r), (r.status_code, r.text[:300])
                    listed_m = {t["name"] for t in first_json(r)["result"]["tools"]}
                    assert listed_m == SIX, listed_m
                else:
                    assert r.status_code == 400, (r.status_code, r.text[:200])

                r = await rpc("tools/call", {"_meta": ENVELOPE, "name": "aee_status",
                                             "arguments": {}},
                              headers={"Mcp-Protocol-Version": MODERN,
                                       "Mcp-Method": "tools/call",
                                       "Mcp-Name": "aee_status"})
                if _SDK_MAJOR >= 2:
                    assert resp_ok(r), (r.status_code, r.text[:300])
                    d = first_json(r)
                    assert not d["result"]["isError"], d
                else:
                    assert r.status_code == 400, (r.status_code, r.text[:200])

                # unknown/unrecognized protocol revision: explicit, safe failure
                bad_env = {**ENVELOPE,
                           "io.modelcontextprotocol/protocolVersion": "2099-01-01"}
                r = await rpc("tools/list", {"_meta": bad_env},
                              headers={"Mcp-Protocol-Version": "2099-01-01",
                                       "Mcp-Method": "tools/list"})
                assert is_rejection(r), (r.status_code, r.text[:200])

                # legacy header with unknown value → same treatment
                r = await rpc("tools/list", {"_meta": ENVELOPE},
                              headers={"Mcp-Protocol-Version": "9999-12-31",
                                       "Mcp-Method": "tools/list"})
                assert is_rejection(r), (r.status_code, r.text[:200])

                # ---- security negatives ----
                r = await rpc("tools/list", {}, key=None)
                assert r.status_code == 401, r.status_code
                r = await rpc("tools/list", {}, key="Bearer-wrong")
                assert r.status_code == 401, r.status_code
                # missing Content-Type: raw body (httpx json= would add it back)
                raw = json.dumps({"jsonrpc": "2.0", "id": "t", "method": "tools/list",
                                  "params": {}}).encode()
                r = await client.post("/mcp", content=raw,
                                      headers={"Authorization": "Bearer SDK_COMPAT_TEST_AUTH",
                                               "Accept": "application/json, text/event-stream"})
                assert r.status_code == 400, (r.status_code, r.text[:120])


def resp_ok(r):
    return r.status_code == 200


def first_json(r):
    text = r.text
    if text.startswith("event:") or text.startswith("data:"):
        text = "".join(line[6:] for line in text.splitlines() if line.startswith("data:"))
    return json.loads(text)


def is_rejection(r):
    return (r.status_code in (400, 405, 406, 421)) or (
        r.status_code == 200 and not resp_state_ok(r))


def resp_state_ok(r):
    try:
        d = first_json(r)
        return "error" not in d
    except Exception:
        return False


for name in ("test_compatibility_battery",):
    pass


class SdkCompatTests(SdkCompat, unittest.TestCase):
    def test_full_battery(self):
        self.run_async(self._battery())


if __name__ == "__main__":
    unittest.main()