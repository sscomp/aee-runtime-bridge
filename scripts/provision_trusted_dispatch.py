#!/usr/bin/env python3
"""Provision a per-host Secure MCP trusted-dispatch policy (fail-closed).

Creates/updates the operator's private policy JSON (0600 in a 0700 parent)
for schema aee-secure-mcp-trusted-dispatch-v1, then re-validates the written
file with the runtime validators used by the gateway itself
(aee.mcp_runtime.trusted_dispatch).

Boundaries (work order AEE_V2_PHASE3_*):
- the principal bearer is read from --key-env / --key-stdin ONLY and is never
  printed, logged, archived, echoed, or copied across hosts; only its SHA-256
  fingerprint is stored in the policy
- no principal wildcard: every admission is a fingerprint -> principal-name map
- write/autonomous run ONLY when the policy's `enabled` flag is true; this
  tool sets that flag to true exclusively via an explicit
  --enable-write-autonomous (a prior opt-in is preserved on later runs;
  --disable-write-autonomous revokes it). It is never enabled implicitly.
- a missing/empty/invalid policy file fails closed at runtime; this tool
  never silently "repairs" an invalid policy the runtime would refuse —
  rebuilding over one requires --overwrite-invalid
- an existing policy file that is valid JSON but carries an unknown schema is
  likewise refused without --overwrite-invalid
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

POLICY_NAME = 'trusted-dispatch-policy.json'
ALLOWED_AGENTS = ('claude', 'codex', 'hermes', 'pi')


def _fingerprint(bearer: str) -> str:
    return hashlib.sha256(bearer.encode('utf-8')).hexdigest()


def _read_key(args):
    """Read the secret from env or stdin strictly; never echo it."""
    if args.key_env and args.key_stdin:
        raise SystemExit('use only one of --key-env / --key-stdin')
    if not args.key_env and not args.key_stdin:
        raise SystemExit('--key-env or --key-stdin is required (never pass secrets '
                         'as command-line arguments)')
    if args.key_stdin:
        data = sys.stdin.readline().rstrip('\n')
    else:
        data = os.environ.get(args.key_env, '')
    if not data or len(data) < 16:
        raise SystemExit('principal key empty or suspiciously short (<16 bytes); '
                         'refusing to provision')
    return data


def _load_existing(policy_path: Path, *, overwrite_invalid=False):
    """Load an existing policy; refuse to silently reuse/rebuild over an
    invalid one (the gateway rejects invalid policies at runtime, and a
    rebuild would silently drop its principals)."""
    if not policy_path.is_file():
        return {}
    try:
        data = json.loads(policy_path.read_text())
    except (OSError, ValueError) as exc:
        if overwrite_invalid:
            return {}
        raise SystemExit(f'existing policy file is unreadable or corrupt '
                         f'({type(exc).__name__}); the gateway fails closed on it. '
                         'Rebuilding would drop its principals — pass '
                         '--overwrite-invalid to replace it deliberately.')
    if isinstance(data, dict) and data.get('schema') != 'aee-secure-mcp-trusted-dispatch-v1':
        if overwrite_invalid:
            return {}
        raise SystemExit(f"existing policy schema {data.get('schema')!r} is unknown "
                         'to this tool; pass --overwrite-invalid to replace it '
                         'deliberately.')
    return data if isinstance(data, dict) else {}


def build_policy(existing, args, fingerprint):
    if args.enable_write_autonomous and args.disable_write_autonomous:
        raise SystemExit('--enable-write-autonomous and --disable-write-autonomous '
                         'are mutually exclusive')
    principals = dict(existing.get('principals') or {})
    for fp in args.revoke or []:
        if fp in principals:
            del principals[fp]
    if args.principal:
        principals[fingerprint] = args.principal
    agents = sorted(set(existing.get('agents') or [])
                    | set(a for a in (args.agents or '').split(',') if a)) \
        if not args.set_agents else sorted(set(a for a in args.set_agents.split(',') if a))
    invalid = [a for a in agents if a not in ALLOWED_AGENTS]
    if invalid:
        raise SystemExit(f'unknown agents {invalid}; allowed: {ALLOWED_AGENTS}')
    if not agents:
        agents = ['claude']
    # enabled flag: an explicit operator flag wins; otherwise a prior opt-in
    # (already enabled policy) is preserved — never implicitly enabled here.
    enabled = bool(existing.get('enabled'))
    if args.enable_write_autonomous:
        enabled = True
    if args.disable_write_autonomous:
        enabled = False
    return {
        'schema': 'aee-secure-mcp-trusted-dispatch-v1',
        'policy': 'secure_mcp_trusted_dispatch',
        'transport': 'secure_mcp',
        'enabled': bool(enabled),
        'principals': principals,
        'agents': agents,
    }


def validate_written(path: Path):
    import importlib
    os.environ['AEE_TRUSTED_DISPATCH_POLICY'] = str(path)
    td = importlib.import_module('aee.mcp_runtime.trusted_dispatch')
    configured = td.TrustedDispatch.configured()
    return configured is not None, configured.policy()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--policy-path', default=str(Path('~/.config/aee') / POLICY_NAME))
    parser.add_argument('--principal', help='principal display name to admit '
                        '(with --key-env/--key-stdin supplying its bearer)')
    parser.add_argument('--key-env', help='env VAR NAME holding the principal bearer '
                        '(name only; value never echoed)')
    parser.add_argument('--key-stdin', action='store_true', help='read one bearer line from stdin')
    parser.add_argument('--agents', default='', help='agents to add to the trusted set '
                        '(comma-separated; default: keep existing or claude)')
    parser.add_argument('--set-agents', help='replace the trusted agent set exactly')
    parser.add_argument('--enable-write-autonomous', action='store_true',
                        help='explicit operator opt-in for write/autonomous (first '
                             'time; a previously enabled policy stays enabled on '
                             're-runs unless --disable-write-autonomous is passed)')
    parser.add_argument('--disable-write-autonomous', action='store_true',
                        help='revoke the enabled flag (write/autonomous fail closed)')
    parser.add_argument('--revoke', action='append', default=[],
                        help='principal fingerprint (sha256 hex) to revoke; repeatable')
    parser.add_argument('--overwrite-invalid', action='store_true',
                        help='explicitly replace an existing unreadable/corrupt/unknown-'
                             'schema policy file (its principals are NOT recoverable)')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    if args.enable_write_autonomous and args.disable_write_autonomous:
        raise SystemExit('--enable-write-autonomous and --disable-write-autonomous '
                         'are mutually exclusive')

    policy_path = Path(args.policy_path).expanduser().resolve()
    fingerprint = None
    if args.principal or args.key_env or args.key_stdin:
        if not args.principal:
            raise SystemExit('--principal name is required whenever a key is admitted')
        bearer = _read_key(args)
        fingerprint = _fingerprint(bearer)
        del bearer
    existing = _load_existing(policy_path, overwrite_invalid=args.overwrite_invalid)
    policy = build_policy(existing, args, fingerprint)
    if fingerprint and fingerprint not in policy['principals']:
        raise SystemExit('internal error: new principal fingerprint missing from policy')

    result = {'schema': policy['schema'],
              'policy_path': str(policy_path),
              'principals': sorted(policy['principals']),
              'revoked': args.revoke,
              'agents': policy['agents'],
              'write_autonomous_enabled': policy['enabled'],
              'mode': 'dry-run' if args.dry_run else 'apply'}
    if not args.dry_run:
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(policy_path.parent, 0o700)
        tmp = policy_path.with_suffix('.tmp')
        with open(tmp, 'w') as handle:
            json.dump(policy, handle, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, policy_path)  # atomic update, private-by-construction
        os.chmod(policy_path, 0o600)
        ok, parsed = validate_written(policy_path)
        result['runtime_validation'] = 'PASS' if ok else 'FAIL'
        if not ok:
            print(json.dumps(result, indent=1))
            raise SystemExit('runtime rejected the written policy; REMOVE it or fix '
                             'before dispatch (gateway fails closed meanwhile)')
    print(json.dumps(result, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())