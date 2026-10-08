"""Actual installed CLI -> unchanged broker -> in-memory synthetic SSE.

Only structural snapshots leave this harness. Requests, prompts, header values,
and responses are never written. The observer is an in-process test seam for
reviewing built-in tool schemas; it is not a broker option or runtime API.
"""
import hashlib
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from aee.mcp_runtime import broker
from aee.mcp_runtime.executor import final_agent_message
from aee.mcp_runtime.process import Limits, run_bounded
from aee.mcp_runtime.profiles import codex_arguments, resolve_codex, select_profile
from aee.mcp_runtime.runtime import broker_arguments
from aee.mcp_runtime.sandbox import sandbox_command
from aee.mcp_runtime.store import JobError
from test_p2c_broker import FixtureTransport, JOB


def schema_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def tool_shape(tool):
    result = {key: tool[key] for key in ("type", "name") if key in tool}
    result["schema_sha256"] = schema_digest(tool)
    if "tools" in tool:
        result["tools"] = [tool_shape(child) for child in tool["tools"]]
    return result


def normalize_request(method, path, headers, body):
    additional = [item["tools"] for item in body["input"] if item.get("type") == "additional_tools"]
    return {"method": method, "path": path, "header_names": sorted(headers),
            "keys": sorted(body), "model": body.get("model"), "reasoning": body.get("reasoning"),
            "store": body.get("store"), "stream": body.get("stream"),
            "tool_choice": body.get("tool_choice"), "parallel_tool_calls": body.get("parallel_tool_calls"),
            "tools": [tool_shape(t) for t in body.get("tools", [])],
            "additional_tools": [[tool_shape(t) for t in tools] for tools in additional],
            "additional_tools_sha256": [schema_digest(tools) for tools in additional],
            "input_types": [item.get("type", "message") for item in body["input"]]}


def probe_native(native, overrides=(), *, transport=None, observer=None, catalog=None, workspace_files=None,
                 resource_revision=None, sandbox_revision=None, result_observer=None,
                 workspace_links=None, runtime_limits=None):
    identity = resolve_codex(str(native))
    snapshots = []
    accepted = []
    validate = broker.validate_request
    transport = transport or FixtureTransport()

    def capture(method, path, headers, payload):
        body = json.loads(payload)
        snapshots.append(normalize_request(method, path, headers, body))
        if observer is not None:
            observer(body)  # memory only; never receives header values
        result = validate(method, path, headers, payload)  # policy is never bypassed
        accepted.append(True)
        return result

    limits = runtime_limits or Limits(timeout=20, file_bytes=4 * 1024**2)
    with tempfile.TemporaryDirectory(prefix="aee-p2c-r1-wire-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir(mode=0o700)
        for name, data in (workspace_files or {}).items():
            from aee.mcp_runtime.sandbox import safe_relative
            assert safe_relative(name)
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        for name, target in (workspace_links or {}).items():
            from aee.mcp_runtime.sandbox import safe_relative
            assert safe_relative(name)
            (workspace / name).symlink_to(target)
        if catalog is not None:
            # Explicit operator metadata fixture; read-only inside the namespace.
            (workspace / 'model-catalog.json').write_bytes(catalog)
        server = broker.Broker(root / "jobs", os.getuid(), transport)
        try:
            socket = server.create(JOB)
            argv = broker_arguments(codex_arguments(select_profile()))
            argv = argv[:-1] + [part for value in overrides for part in ("-c", value)] + [argv[-1]]
            with patch("aee.mcp_runtime.broker.validate_request", capture):
                result = run_bounded(sandbox_command(Path(identity.executable), workspace, argv,
                                                    limits, broker_socket=socket,
                                                    resource_revision=resource_revision,
                                                    sandbox_revision=sandbox_revision), env={},
                                     stdin=b"Return the fixture response.", limits=limits)
            if result_observer is not None:
                result_observer(result)
            try:
                terminal_valid = result.returncode == 0 and bool(final_agent_message(result.stdout, limits))
            except JobError:
                terminal_valid = False
            return {"version": identity.version, "executable_sha256": identity.sha256,
                    "overrides": list(overrides), "requests": snapshots,
                    "accepted_requests": len(accepted), "upstream_requests": len(transport.requests),
                    "exit_code": result.returncode, "terminal_valid": terminal_valid,
                    "event_types": [json.loads(line).get("type") for line in result.stdout.splitlines()]}
        finally:
            server.close()
