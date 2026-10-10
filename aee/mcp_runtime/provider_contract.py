"""Reviewed R1 contract, scope re-reviewed in R3 after a live tool-using E2E;
schema or executor drift requires another review."""
import hashlib
import json

CONTRACT_ID = "codex-0.159.2-responses-lite-single-agent-r3"
NATIVE_VERSION = "codex-cli 0.159.2"
NATIVE_SHA256 = "1748767b230ebfc3d4ab7e4e254920d0c0ad9691fd8c11f190e7d44511a4a92e"
COMPANION_SHA256 = "5b2c075ac2380fa04d76d7313fbc044d29c8d0a0d0b9138415acd4610211ca03"
TOOLS_SHA256 = "8b94e0eb34175a0d2ac7e364d2f6b38524ee5cb8f24ea419169a342d51e13917"
SINGLE_AGENT_OVERRIDES = (
    "agents.enabled=false",
    "features.sleep_tool=false",
    "tools.experimental_request_user_input.enabled=false",
    "features.goals=false",
)


def contract_record():
    return {"id": CONTRACT_ID, "additional_tools_sha256": TOOLS_SHA256,
            "single_agent_overrides": list(SINGLE_AGENT_OVERRIDES),
            "verification_scope": "installed-native-through-broker-attested-read-only-tool-dispatch"}


def matches_reviewed_executor(version, native_digest, companion_digest):
    return (version, native_digest, companion_digest) == (
        NATIVE_VERSION, NATIVE_SHA256, COMPANION_SHA256)


def approved_tools(tools):
    # Hash the complete native schemas, including the nested Code Mode guidance.
    # Names alone cannot prove what a custom execution wrapper exposes.
    if not isinstance(tools, list):
        return False
    encoded = json.dumps(tools, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest() == TOOLS_SHA256
