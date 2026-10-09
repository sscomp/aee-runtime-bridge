"""Pinned sandbox ownership and native failure-observation contract."""
import hashlib
import json
import stat
from pathlib import Path

from .resource_policy import resource_record
from .store import JobError

PROFILE_PATH = Path(__file__).resolve().parents[2] / 'config/p2c/sandbox-profile.json'
PROFILE_SHA256 = '25055189bc2812380b51fbb4a0f3d80766f509fe891a8a58f635bd5896770ae0'
FAILURE_REVISION = 'codex-native-otlp-r3-1'


def sandbox_profile():
    try:
        if PROFILE_PATH.is_symlink():
            raise ValueError()
        payload = PROFILE_PATH.read_bytes()
        if hashlib.sha256(payload).hexdigest() != PROFILE_SHA256:
            raise ValueError()
        return json.loads(payload)
    except (OSError, ValueError):
        raise JobError('SANDBOX_POLICY_INVALID', 'Pinned sandbox profile is unavailable or changed') from None


def sandbox_record():
    profile = sandbox_profile()
    return {'id': profile['revision'], 'sha256': PROFILE_SHA256,
            'failure_semantics': FAILURE_REVISION}


def verify_sandbox(revision, resource_revision, bwrap):
    profile = sandbox_profile()
    if revision != profile['revision'] or resource_revision != resource_record()['id']:
        raise JobError('SANDBOX_POLICY_INVALID', 'Sandbox requires the reviewed resource and ownership policy')
    path = Path(bwrap)
    try:
        mode = path.stat().st_mode
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if (str(path.resolve()) != profile['bwrap_path'] or path.is_symlink()
                or not stat.S_ISREG(mode) or mode & (stat.S_ISUID | stat.S_ISGID)
                or digest != profile['bwrap_sha256']):
            raise ValueError()
    except (OSError, ValueError):
        raise JobError('SANDBOX_BINARY_CHANGED', 'Reviewed Bubblewrap identity is unavailable or changed') from None


def telemetry_arguments(arguments):
    overrides = [
        'otel.exporter={otlp-http={endpoint="http://127.0.0.1:18081/v1/logs",protocol="json"}}',
        'otel.trace_exporter={otlp-http={endpoint="http://127.0.0.1:18081/v1/traces",protocol="json"}}',
        'otel.metrics_exporter="none"', 'otel.log_user_prompt=false',
        'otel.log_agent_responses=false', 'otel.tool_result.max_bytes=512',
    ]
    return arguments[:-1] + [part for value in overrides for part in ('-c', value)] + [arguments[-1]]
