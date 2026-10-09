"""Explicit deployment policy, shared by discovery and dispatch."""
import json
import os
from pathlib import Path

from .profiles import resolve_codex, executable_digest
from .store import JobError
from .provider_contract import SINGLE_AGENT_OVERRIDES, contract_record, matches_reviewed_executor
from .resource_policy import resource_record, verify_current_containment
from .sandbox_policy import sandbox_record, FAILURE_REVISION


def deployment_policy(path=None):
    name = path or os.getenv("AEE_RUNTIME_MANIFEST")
    if not name:
        return None  # isolated P2B tests only; production unit always requires manifest
    try:
        p = Path(name)
        if p.is_symlink() or p.stat().st_size > 1024 * 1024:
            raise ValueError()
        body = json.loads(p.read_text())
        if body["schema"] != 1 or body["config_schema"] != "aee-p2c-1":
            raise ValueError()
        identity = body["codex"]
        if not Path(identity["path"]).is_absolute() or not identity["version"].startswith("codex-cli "):
            raise ValueError()
        import re
        if not re.fullmatch("[a-f0-9]{64}", identity["sha256"]):
            raise ValueError()
        if body["execution_profile"] != "codex-readonly-high":
            raise ValueError()
        if body["egress"] != "per-job-uds-responses-broker":
            raise ValueError()
        return body
    except (OSError, ValueError, TypeError, KeyError):
        raise JobError("RUNTIME_POLICY_INVALID", "Deployment manifest is invalid") from None


def resolve_executor(configured):
    policy = deployment_policy()
    if policy and str(Path(configured).resolve()) != policy["codex"]["path"]:
        raise JobError("EXECUTOR_IDENTITY_MISMATCH", "Executor path differs from deployment manifest")
    identity = resolve_codex(configured)
    if policy and (identity.version != policy["codex"]["version"] or identity.sha256 != policy["codex"]["sha256"]):
        raise JobError("EXECUTOR_IDENTITY_MISMATCH", "Executor version or digest differs from deployment manifest")
    if policy:
        companion = policy["codex"].get("code_mode_host")
        if not isinstance(companion, dict):
            raise JobError("RUNTIME_POLICY_INVALID", "Pinned Code Mode companion is required")
        path = Path(identity.executable).with_name("codex-code-mode-host")
        try:
            matches = str(path) == companion.get("path") and executable_digest(path) == companion.get("sha256")
        except (OSError,JobError):
            matches = False
        if not matches:
            raise JobError("EXECUTOR_IDENTITY_MISMATCH", "Executor companion differs from deployment manifest")
    return identity


def require_deployment_ready(policy):
    identity = policy.get('codex', {})
    if (policy.get('native_provider_compatibility') != 'VERIFIED'
            or policy.get('native_tool_runtime_compatibility') != 'VERIFIED'
            or policy.get('resource_containment') != 'VERIFIED'
            or policy.get('resource_policy_revision') != resource_record()
            or policy.get('sandbox_policy_revision') != sandbox_record()
            or policy.get('job_failure_semantics') != 'VERIFIED'
            or policy.get('job_failure_contract') != FAILURE_REVISION
            or policy.get('deployment_status') != 'APPROVED_STAGE2'
            or not policy.get('operator_approval_id')
            or policy.get('provider_contract') != contract_record()
            or not matches_reviewed_executor(identity.get('version'), identity.get('sha256'),
                                             identity.get('code_mode_host', {}).get('sha256'))):
        raise JobError('DEPLOYMENT_GATE_CLOSED', 'Candidate has not passed the reviewed deployment gate')
    verify_current_containment()


def broker_arguments(arguments):
    # Keep task '-' last; every option is operator-controlled configuration.
    overrides = [
        'model_provider="aee_broker"', 'model_providers.aee_broker.name="AEE inference broker"',
        'model_providers.aee_broker.base_url="http://127.0.0.1:18080/v1"',
        'model_providers.aee_broker.wire_api="responses"',
        'model_providers.aee_broker.requires_openai_auth=false',
        'model_providers.aee_broker.supports_websockets=false',
        'model_providers.aee_broker.request_max_retries=0',
        'model_providers.aee_broker.stream_max_retries=0',
        'model_providers.aee_broker.stream_idle_timeout_ms=120000',
        'cli_auth_credentials_store="ephemeral"', 'web_search="disabled"',
        'allow_login_shell=false', 'otel.exporter="none"',
        'features.multi_agent=false', 'features.multi_agent_v2=false',
        'features.skill_mcp_dependency_install=false', 'features.skill_search=false',
        'features.skip_host_skill_discovery=true',
        *SINGLE_AGENT_OVERRIDES,
    ]
    return arguments[:-1] + [part for value in overrides for part in ["-c", value]] + [arguments[-1]]
