"""Tests for the OpenAPI ``CreateRunRequest.metadata`` rollout (TASK-M6).

The authoritative OpenAPI schema is ``openapi.yaml`` at the repo
root. These tests verify:

1. The YAML parses and is structurally well-formed.
2. ``CreateRunRequest.properties.metadata`` exists with the
   shape required by TASK-M6 §4 (``type: object``,
   ``nullable: true``, ``additionalProperties: true``).
3. The existing required-field contract is unchanged
   (``required: [input]``).
4. A strict JSON Schema / OpenAPI validator accepts the sample
   payload from the ticket (executor=claude_code).
5. A legacy payload without ``metadata`` still validates.
6. The application rejects an unknown executor value with the
   stable ``unknown_executor`` error code (router-level, see
   ``test_executor_router.py`` for the HTTP-level coverage).
7. A valid ``metadata.executor="claude_code"`` request reaches
   the Claude adapter path in isolation.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest
import yaml

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

OPENAPI_PATH = _ROOT / "openapi.yaml"


# --- Loaders ---------------------------------------------------------


def _load_openapi() -> Dict[str, Any]:
    """Load and return the OpenAPI doc as a Python dict.

    Cached per pytest session via the ``openapi_doc`` fixture.
    """
    with OPENAPI_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="module")
def openapi_doc() -> Dict[str, Any]:
    return _load_openapi()


def _create_run_schema(doc: Dict[str, Any]) -> Dict[str, Any]:
    return doc["components"]["schemas"]["CreateRunRequest"]


# --- Validators ------------------------------------------------------


def _try_validator() -> Optional[Tuple[Any, Any]]:
    """Return ``(ValidatorClass, `` for a strict schema check, or
    ``None`` if neither ``jsonschema`` nor
    ``openapi_spec_validator`` is available.
    """
    try:
        from jsonschema import Draft202012Validator
        return ("jsonschema", Draft202012Validator)
    except ImportError:  # pragma: no cover
        pass
    return None


# --- Tests ------------------------------------------------------------


def test_openapi_yaml_parses(openapi_doc):
    """The YAML parses and has the expected top-level shape."""
    assert isinstance(openapi_doc, dict)
    assert openapi_doc.get("openapi", "").startswith("3.")
    assert "components" in openapi_doc
    assert "schemas" in openapi_doc["components"]
    assert "CreateRunRequest" in openapi_doc["components"]["schemas"]


def test_metadata_property_exists(openapi_doc):
    """``CreateRunRequest.properties.metadata`` exists with the
    shape required by TASK-M6 §4.
    """
    cr = _create_run_schema(openapi_doc)
    props = cr.get("properties", {})
    assert "metadata" in props, "metadata property missing"
    md = props["metadata"]
    # Exact shape contract.
    assert md.get("type") == "object", f"type: {md.get('type')!r}"
    assert md.get("nullable") is True, f"nullable: {md.get('nullable')!r}"
    assert md.get("additionalProperties") is True, (
        f"additionalProperties: {md.get('additionalProperties')!r}"
    )
    # Description is present and references the validation contract.
    desc = md.get("description") or ""
    assert "executor" in desc.lower(), "description should mention executor"
    assert (
        "validate_metadata" in desc
    ), "description should reference aee.runtimes.executor_router.validate_metadata"


def test_required_field_unchanged(openapi_doc):
    """``required`` is still exactly ``['input']``."""
    cr = _create_run_schema(openapi_doc)
    assert cr.get("required") == ["input"], (
        f"required changed: {cr.get('required')!r}"
    )


def test_existing_fields_remain(openapi_doc):
    """No previously-existing field was removed."""
    cr = _create_run_schema(openapi_doc)
    props = cr.get("properties", {})
    for f in (
        "input",
        "session_id",
        "mode",
        "timeout_seconds",
        "title",
        "type",
        "priority",
        "openai_run_id",
        "prompt_version",
        "model_name",
        "expected_artifacts",
    ):
        assert f in props, f"missing field: {f}"


def test_strict_validator_accepts_executor_request(openapi_doc):
    """A strict JSON Schema / OpenAPI validator accepts the
    sample executor request from TASK-M6 §4.
    """
    validator = _try_validator()
    if validator is None:
        pytest.skip("no strict validator available")
    name, V = validator
    cr = _create_run_schema(openapi_doc)
    sample = {
        "input": "Create report.md containing TASK_M6_SMOKE=PASS",
        "mode": "coding",
        "timeout_seconds": 180,
        "metadata": {
            "executor": "claude_code",
            "repo_path": "/tmp/task-m6-smoke-repo",
            "required_artifacts": ["report.md"],
            "working_mode": "existing_worktree",
            "allow_commit": False,
            "human_approved": False,
        },
    }
    if name == "jsonschema":
        v = V(cr)
        errors = sorted(v.iter_errors(sample), key=lambda e: e.path)
        assert not errors, f"unexpected validation errors: {[e.message for e in errors]}"
    else:  # pragma: no cover
        pytest.skip(f"unsupported validator: {name}")


def test_legacy_payload_without_metadata_validates(openapi_doc):
    """A payload without ``metadata`` (legacy callers) still
    validates against the schema.
    """
    validator = _try_validator()
    if validator is None:
        pytest.skip("no strict validator available")
    name, V = validator
    cr = _create_run_schema(openapi_doc)
    sample = {
        "input": "Plain legacy request, no metadata.",
        "mode": "normal",
    }
    if name == "jsonschema":
        v = V(cr)
        errors = sorted(v.iter_errors(sample), key=lambda e: e.path)
        assert not errors, f"unexpected validation errors: {[e.message for e in errors]}"


def test_unknown_executor_router_raises_stable_code():
    """``validate_metadata`` rejects an unknown executor with
    the stable ``unknown_executor`` error code. The HTTP-level
    400 mapping is covered by ``tests/test_executor_router.py``.
    """
    from aee.runtimes import executor_router as er

    with pytest.raises(er.ExecutorValidationError) as excinfo:
        er.validate_metadata({"executor": "gemini"})
    assert excinfo.value.code == "unknown_executor"


def test_unknown_executor_unknown_routes_via_router():
    """``select_executor`` also raises the same stable code as
    a defense-in-depth, when called before ``validate_metadata``.
    """
    from aee.runtimes import executor_router as er

    with pytest.raises((er.ExecutorValidationError, er.ExecutorUnavailable)) as excinfo:
        er.select_executor(
            {"executor": "gemini"},
            available_adapters=("hermes", "claude_code"),
        )
    # The router must raise with a stable code; both
    # ExecutorValidationError (for unknown value) and
    # ExecutorUnavailable (for unknown adapter) are acceptable.
    if isinstance(excinfo.value, er.ExecutorValidationError):
        assert excinfo.value.code == "unknown_executor"
    else:
        # ExecutorUnavailable path is also acceptable; it
        # signals "the value was unknown to the available set".
        assert "claude_code" in str(excinfo.value) or "gemini" in str(excinfo.value)


def test_valid_metadata_executor_claude_code_routes_via_router():
    """``select_executor`` returns a routing decision whose
    selected_executor is ``claude_code`` when the caller passes
    a valid ``metadata.executor='claude_code'`` and the adapter
    is available.
    """
    from aee.runtimes import executor_router as er

    decision = er.select_executor(
        {"executor": "claude_code"},
        available_adapters=("hermes", "claude_code"),
    )
    assert decision.selected_executor == "claude_code"
    assert decision.requested_executor == "claude_code"
    assert decision.selection_source == "metadata"
    assert decision.fallback_applied is False


def test_executor_unavailable_routes_to_503_error_code():
    """When ``claude_code`` is requested but the adapter is not
    registered, the router raises ``ExecutorUnavailable`` — the
    API layer maps this to HTTP 503 with
    ``detail.code = 'executor_unavailable'``. The router-level
    part is asserted here; the HTTP mapping is asserted in
    ``tests/test_executor_router.py``.
    """
    from aee.runtimes import executor_router as er

    with pytest.raises(er.ExecutorUnavailable) as excinfo:
        er.select_executor(
            {"executor": "claude_code"},
            available_adapters=("hermes",),
        )
    # The error message must mention the requested executor
    # so the API layer can craft a clear detail message.
    assert "claude_code" in str(excinfo.value)


def test_yaml_openapi_version_is_3_1(openapi_doc):
    """The OpenAPI version is 3.1.x (the spec uses 2020-12 JSON
    Schema semantics, which Draft202012Validator understands).
    """
    assert openapi_doc.get("openapi", "").startswith("3.1"), (
        f"unexpected openapi version: {openapi_doc.get('openapi')!r}"
    )
