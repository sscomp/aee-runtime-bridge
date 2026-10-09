"""Operator-versioned resource policy; never accepted from restricted MCP."""
import hashlib
import json
from pathlib import Path

from .store import JobError

PROFILE_PATH = Path(__file__).resolve().parents[2] / 'config/p2c/resource-profile.json'
# Pin updated only by a reviewed source change, never by an environment override.
PROFILE_SHA256 = 'c1d2a56bf2d5e2b9f461c128b026f92b49c0aa0adf9d3ddc3f1b8f46500b9c39'


def resource_profile():
    try:
        if PROFILE_PATH.is_symlink():
            raise ValueError()
        data = PROFILE_PATH.read_bytes()
        if hashlib.sha256(data).hexdigest() != PROFILE_SHA256:
            raise ValueError()
        return json.loads(data)
    except (OSError, ValueError):
        raise JobError('RESOURCE_POLICY_INVALID', 'Reviewed resource profile is unavailable or changed') from None


def resource_record():
    profile = resource_profile()
    return {'id': profile['revision'], 'sha256': PROFILE_SHA256, 'scope': profile['scope']}


def verify_current_containment():
    """Check real inherited cgroup bounds before allowing the larger AS limit.

    The sandbox has no cgroupfs or manager socket; children cannot move out.
    A smaller limit is allowed, an absent/unlimited/looser controller is denied.
    """
    try:
        membership = Path('/proc/self/cgroup').read_text().strip()
        if not membership.startswith('0::/') or '\n' in membership:
            raise ValueError()
        root = Path('/sys/fs/cgroup') / membership[4:]
        if '..' in root.parts or root == Path('/sys/fs/cgroup'):
            raise ValueError()
        policy = resource_profile()['cgroup']
        bounds = {'memory.high': policy['memory_high_bytes'], 'memory.max': policy['memory_max_bytes'],
                  'memory.swap.max': policy['memory_swap_max_bytes'], 'pids.max': policy['tasks_max']}
        values = {name: int((root / name).read_text().strip()) for name in bounds}
        if any(values[name] > cap or values[name] < 0 for name, cap in bounds.items()):
            raise ValueError()
        if not (0 < values['memory.high'] <= values['memory.max'] and values['pids.max'] > 0):
            raise ValueError()
        quota, period = map(int, (root / 'cpu.max').read_text().split())
        if quota <= 0 or period <= 0 or quota * 100 > period * policy['cpu_quota_percent']:
            raise ValueError()
        return {'cgroup': str(root), **values, 'cpu.max': [quota, period]}
    except (OSError, ValueError):
        raise JobError('RESOURCE_CONTAINMENT_UNAVAILABLE', 'Required inherited cgroup v2 limits are not enforced') from None
