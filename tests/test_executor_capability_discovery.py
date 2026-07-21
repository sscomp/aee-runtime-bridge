"""Capability discovery + runtime identity acceptance tests (work-order
TASK_AEE_EXECUTOR_CAPABILITY_ENHANCEMENT, Parts A & B).

Targeted only — does NOT rewrite any existing test. Covers:

* GET /executors (Part A):
    - supported_executors is correct.
    - default_executor is correct.
    - aliases map is correct (identity self-maps excluded).
    - read-only / no side effects (no dispatch, no task created).
    - 401 without a bearer token (auth unchanged).
* runtime_identity (Part B):
    - exists on the claude-code-cli envelope.
    - exists on the hermes envelope.
    - provider / bridge_commit / generated_at_utc are factual.
    - unavailable values are null / "unknown" (never fabricated).
* OpenAPI (Acceptance H / L):
    - gpt/aee_executor_openapi.json validates as an OpenAPI 3.0 doc.
    - the ExecutorsResponse + ExecutorRunResponse (with runtime_identity)
      examples validate against their schemas.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

from tests._executor_test_helpers import (
    make_client,
    post_executor,
    set_fake_binary,
    write_fake_claude,
)

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

OPENAPI_PATH = _ROOT / "gpt" / "aee_executor_openapi.json"


# --- OpenAPI loaders / validators ------------------------------------


@pytest.fixture(scope="module")
def openapi_doc() -> Dict[str, Any]:
    return json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))


def _try_openapi_validator():
    try:
        from openapi_spec_validator import validate as _validate
        return _validate
    except ImportError:  # pragma: no cover
        return None


def _try_jsonschema():
    try:
        from jsonschema import Draft202012Validator, RefResolver
        return Draft202012Validator, RefResolver
    except ImportError:  # pragma: no cover
        return None, None


def _to_jsonschema202012(node: Any) -> Any:
    """Recursively rewrite an OpenAPI 3.0 schema node into a JSON Schema
    2020-12-compatible node. OpenAPI 3.0's ``nullable: true`` is not
    understood by Draft202012Validator, so we convert
    ``{type: T, nullable: true}`` into ``{type: [T, "null"]}`` and drop
    the ``nullable`` keyword. $ref nodes are left untouched (resolved by
    the RefResolver at validation time)."""
    if isinstance(node, dict):
        out: Dict[str, Any] = {}
        for k, v in node.items():
            if k == "nullable":
                continue
            if k == "type" and isinstance(v, str) and node.get("nullable") is True:
                out[k] = [v, "null"]
            else:
                out[k] = _to_jsonschema202012(v)
        return out
    if isinstance(node, list):
        return [_to_jsonschema202012(x) for x in node]
    return node


def _resolve_schema(doc: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
    """Return a schema with $ref resolved against the OpenAPI doc."""
    Validator, RefResolver = _try_jsonschema()
    if Validator is None:
        pytest.skip("jsonschema not available")
    resolver = RefResolver.from_schema(doc)
    # Expand any top-level $ref into the referenced schema for validation.
    if "$ref" in schema:
        with resolver.resolving(schema["$ref"]) as resolved:
            schema = resolved
    return schema


# --- Part A: GET /executors ------------------------------------------


@pytest.fixture
def client(monkeypatch, tmp_path):
    c, _app, key = make_client(monkeypatch, tmp_path)
    return c, key


def test_executors_returns_supported(client):
    c, key = client
    resp = c.get("/executors", headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    data = resp.json()
    assert "supported_executors" in data
    supported = data["supported_executors"]
    assert isinstance(supported, list)
    assert "claude-code-cli" in supported
    assert "hermes" in supported


def test_executors_returns_default(client):
    c, key = client
    data = c.get("/executors", headers={"Authorization": f"Bearer {key}"}).json()
    assert data["default_executor"] == "claude-code-cli"


def test_executors_returns_aliases(client):
    c, key = client
    data = c.get("/executors", headers={"Authorization": f"Bearer {key}"}).json()
    aliases = data["aliases"]
    assert isinstance(aliases, dict)
    # The documented aliases canonicalise to claude-code-cli.
    assert aliases.get("claude_code") == "claude-code-cli"
    assert aliases.get("claude-code") == "claude-code-cli"
    # Identity self-map is excluded from the alias surface.
    assert "claude-code-cli" not in aliases
    # Every alias value must point at a supported executor.
    for v in aliases.values():
        assert v in data["supported_executors"]


def test_executors_requires_auth(monkeypatch, tmp_path):
    """No authentication changes — the endpoint enforces the same bearer gate."""
    c, _app, _key = make_client(monkeypatch, tmp_path)
    resp = c.get("/executors")  # no Authorization header
    assert resp.status_code == 401


def test_executors_is_read_only(client):
    """GET /executors returns only capability keys (no run_id / status /
    dispatch evidence) and is stable across repeated calls — i.e. it is
    pure discovery, not dispatch."""
    c, key = client
    bodies = []
    for _ in range(3):
        r = c.get("/executors", headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 200
        body = r.json()
        # Only the three capability keys; no dispatch / run evidence leaks.
        assert set(body.keys()) == {"supported_executors", "default_executor", "aliases"}
        bodies.append(body)
    # Read-only + deterministic: every call returns the same capability set.
    assert all(b == bodies[0] for b in bodies)


# --- Part B: runtime_identity ----------------------------------------


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    artifact = str(tmp_path / "identity.md")
    binary = write_fake_claude(tmp_path, artifact=artifact)
    set_fake_binary(monkeypatch, binary)
    c, _app, key = make_client(monkeypatch, tmp_path)
    return c, key, artifact, binary


def test_runtime_identity_exists_on_cli_envelope(cli_env):
    c, key, artifact, binary = cli_env
    resp = post_executor(c, key, {
        "executor": "claude-code-cli",
        "prompt": "create the artifact",
        "expected_artifacts": [artifact],
        "timeout_sec": 30,
        "repo_path": "/home/ubuntu/Abacus",
    })
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    data = resp.json()
    assert "runtime_identity" in data, "envelope missing runtime_identity"
    ri = data["runtime_identity"]
    assert isinstance(ri, dict)
    for field in (
        "provider", "provider_version", "executor_binary",
        "executor_version", "runtime_bridge_version",
        "bridge_commit", "bridge_branch", "bridge_repository",
        "generated_at_utc",
    ):
        assert field in ri, f"runtime_identity missing field: {field!r}"


def test_runtime_identity_values_are_factual(cli_env):
    c, key, artifact, binary = cli_env
    data = post_executor(c, key, {
        "executor": "claude-code-cli",
        "prompt": "create the artifact",
        "expected_artifacts": [artifact],
        "timeout_sec": 30,
        "repo_path": "/home/ubuntu/Abacus",
    }).json()
    ri = data["runtime_identity"]
    # provider is the factual display name for the selected executor.
    assert ri["provider"] == "Claude Code"
    # executor_binary echoes the configured (fake) binary path.
    assert ri["executor_binary"] == binary
    # executor_version is whatever the binary printed to --version; the
    # fake binary ignores argv and prints its stdout, so this is non-empty
    # and factual (never fabricated).
    assert isinstance(ri["executor_version"], str) and ri["executor_version"]
    # provider_version mirrors executor_version for the CLI executor.
    assert ri["provider_version"] == ri["executor_version"]
    # bridge version is the shipped constant (no fabrication).
    assert ri["runtime_bridge_version"] == "unknown"
    # bridge_commit is the real HEAD of the bridge worktree (factual).
    assert isinstance(ri["bridge_commit"], str) and len(ri["bridge_commit"]) >= 7
    # bridge_branch is the real current branch.
    assert isinstance(ri["bridge_branch"], str) and ri["bridge_branch"]
    # bridge_repository is the local repo path (no remote configured here).
    assert isinstance(ri["bridge_repository"], str) and ri["bridge_repository"]
    # generated_at_utc is a real ISO-8601 UTC timestamp.
    assert isinstance(ri["generated_at_utc"], str)
    assert ri["generated_at_utc"].endswith("Z")
    assert "T" in ri["generated_at_utc"]


def test_runtime_identity_bridge_commit_matches_git(cli_env):
    """Acceptance G — bridge_commit is factual (matches `git rev-parse HEAD`)."""
    import subprocess
    c, key, artifact, binary = cli_env
    data = post_executor(c, key, {
        "executor": "claude-code-cli",
        "prompt": "create the artifact",
        "expected_artifacts": [artifact],
        "timeout_sec": 30,
        "repo_path": "/home/ubuntu/Abacus",
    }).json()
    real = subprocess.run(
        ["git", "-C", str(_ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=5,
    ).stdout.strip()
    assert data["runtime_identity"]["bridge_commit"] == real


def test_runtime_identity_exists_on_hermes_envelope(monkeypatch, tmp_path):
    """runtime_identity is present on the hermes (async) envelope too."""
    from aee.adapters.base import RuntimePollResult, RuntimeSubmitResult
    from aee.core.registry import adapter_registry

    class _StubHermes:
        name = "hermes"
        runtime_type = "hermes"

        async def submit(self, job):
            return RuntimeSubmitResult(external_run_id="stub-hermes-run", status="queued")

        async def poll(self, external_run_id):
            return RuntimePollResult(external_run_id=external_run_id, status="completed", is_terminal=True)

        async def cancel(self, external_run_id):
            from aee.adapters.base import RuntimeCancelResult
            return RuntimeCancelResult(external_run_id=external_run_id, cancelled=True)

    saved = dict(adapter_registry._adapters)
    adapter_registry._adapters["hermes"] = _StubHermes()
    try:
        c, _app, key = make_client(monkeypatch, tmp_path)
        resp = post_executor(c, key, {
            "executor": "hermes",
            "prompt": "do a thing",
            "timeout_sec": 30,
        })
        assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
        ri = resp.json()["runtime_identity"]
        assert isinstance(ri, dict)
        # Hermes has no local binary, so these are null (factual, not fabricated).
        assert ri["provider"] == "Hermes"
        assert ri["executor_binary"] is None
        assert ri["executor_version"] is None
        assert ri["provider_version"] is None
        # bridge facts are still populated.
        assert ri["bridge_commit"]
        assert ri["generated_at_utc"].endswith("Z")
    finally:
        adapter_registry._adapters.clear()
        adapter_registry._adapters.update(saved)


def test_runtime_identity_unknown_provider_for_unsupported(monkeypatch, tmp_path):
    """collect_runtime_identity maps an unrecognised executor to 'unknown'
    rather than fabricating a provider name."""
    from aee.runtimes.runtime_identity import collect_runtime_identity
    ri = collect_runtime_identity(selected_executor="gemini", cfg={})
    assert ri["provider"] == "unknown"
    assert ri["executor_binary"] is None
    assert ri["executor_version"] is None


# --- OpenAPI validation (Acceptance H / L) ---------------------------


def test_openapi_json_parses_and_validates(openapi_doc):
    """gpt/aee_executor_openapi.json is a valid OpenAPI 3.0 document."""
    validate = _try_openapi_validator()
    if validate is None:
        pytest.skip("openapi_spec_validator not available")
    validate(openapi_doc)  # raises on invalid


def test_openapi_has_executors_path_and_schema(openapi_doc):
    paths = openapi_doc["paths"]
    assert "/executors" in paths
    assert "get" in paths["/executors"]
    assert "ExecutorsResponse" in openapi_doc["components"]["schemas"]
    assert "RuntimeIdentity" in openapi_doc["components"]["schemas"]


def test_openapi_executor_response_includes_runtime_identity(openapi_doc):
    resp_schema = openapi_doc["components"]["schemas"]["ExecutorRunResponse"]
    assert "runtime_identity" in resp_schema["required"]
    assert "runtime_identity" in resp_schema["properties"]


def test_openapi_executors_example_validates(openapi_doc):
    """The ExecutorsResponse example validates against its schema."""
    Draft202012Validator, RefResolver = _try_jsonschema()
    if Draft202012Validator is None:
        pytest.skip("jsonschema not available")
    doc = _to_jsonschema202012(openapi_doc)
    schema = doc["components"]["schemas"]["ExecutorsResponse"]
    example = openapi_doc["paths"]["/executors"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["examples"]["default_capabilities"]["value"]
    resolver = RefResolver.from_schema(doc)
    validator = Draft202012Validator(schema, resolver=resolver)
    errors = sorted(validator.iter_errors(example), key=lambda e: e.path)
    assert not errors, f"ExecutorsResponse example invalid: {[e.message for e in errors]}"


def test_openapi_executor_run_response_example_validates(openapi_doc):
    """The full ExecutorRunResponse example (with runtime_identity) validates
    against its schema — Acceptance L (GPT can import without manual edits)."""
    Draft202012Validator, RefResolver = _try_jsonschema()
    if Draft202012Validator is None:
        pytest.skip("jsonschema not available")
    doc = _to_jsonschema202012(openapi_doc)
    schema = doc["components"]["schemas"]["ExecutorRunResponse"]
    example = openapi_doc["paths"]["/runs/executor"]["post"]["responses"]["200"][
        "content"
    ]["application/json"]["examples"]["claude_code_cli_completed"]["value"]
    resolver = RefResolver.from_schema(doc)
    validator = Draft202012Validator(schema, resolver=resolver)
    errors = sorted(validator.iter_errors(example), key=lambda e: e.path)
    assert not errors, f"ExecutorRunResponse example invalid: {[e.message for e in errors]}"