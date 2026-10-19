#!/usr/bin/env python3
"""Validate AEE deployment profile env files against the profile schema contract.

Schema: config/profiles/profile-schema.json (aee-deployment-profile-schema-v1).
Used three ways:
  1. CI / local check: validate the shipped example files (no arguments).
  2. Installer preflight: validate an operator env file for a profile.
  3. Trusted-dispatch onboarding: confirm the profile file stays schema-clean.

Secret VALUES must never appear here or in any example/provisioned artifact:
real values live only in operator-owned 0600 files on the target host.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / 'config' / 'profiles' / 'profile-schema.json'
EXAMPLES = {
    'os-native': ROOT / 'config' / 'profiles' / 'os-native.env.example',
    'restricted': ROOT / 'config' / 'profiles' / 'restricted.env.example',
}


def load_schema():
    schema = json.loads(SCHEMA_PATH.read_text())
    if schema.get('schema') != 'aee-deployment-profile-schema-v1':
        raise ValueError('profile schema version changed; review validators')
    return schema


def _dequote(value):
    """Systemd EnvironmentFile-style single-level quote removal.

    systemd removes a matching wrapping quote pair from assigned values
    (which would otherwise hide e.g. a placeholder like "<...>" from schema
    checks or a real bearer inside quotes from the placeholder rejection;
    dequoting first keeps validator semantics aligned with the consumer).
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def parse_env(text):
    """Return {key: value} for KEY=VALUE lines (comments/blanks ignored).

    Aligns with systemd EnvironmentFile consumer semantics: a leading
    `export ` prefix is ignored, and wrapping quotes are removed (see
    _dequote). The inline-`#`-is-value and last-key-wins behaviors match
    systemd as-is.
    """
    fields = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[len('export '):].strip()
        if '=' not in line:
            raise ValueError(f'not a KEY=VALUE line: {line[:40]!r}')
        key, value = line.split('=', 1)
        fields[key.strip()] = _dequote(value.strip())
    return fields


def _placeholder_ok(value, spec):
    prefix = load_placeholder_prefix()
    if spec.get('fixed'):
        return value == spec['value']
    if 'value' in spec:
        return value == spec['value'] or value.startswith(prefix)
    return value.startswith(prefix)


_PLACEHOLDER_PREFIX = None


def load_placeholder_prefix():
    global _PLACEHOLDER_PREFIX
    if _PLACEHOLDER_PREFIX is None:
        _PLACEHOLDER_PREFIX = load_schema()['value_placeholder_prefix']
    return _PLACEHOLDER_PREFIX


def validate_fields(fields, profile, *, source='example'):
    """Validate parsed env fields for a profile; returns list of error strings.

    source='example'      shipped artifacts: non-fixed keys MUST remain placeholders
    source='operator-env' operator's real 0600 env on the target host: values must
                          be real (secret keys must NOT be placeholders); key names
                          and fixed boundary values are still enforced
    """
    schema = load_schema()
    spec = schema['profiles'].get(profile)
    if spec is None:
        return [f'unknown profile {profile!r}']
    prefix = schema['value_placeholder_prefix']
    operator_env = source == 'operator-env'
    errors = []
    for item in spec['env'].get('required', []):
        name = item['name']
        if name not in fields:
            errors.append(f'{profile}/{source}: missing required key {name}')
            continue
        value = fields[name]
        if item.get('fixed') and value != item['value']:
            errors.append(f'{profile}/{source}: {name} boundary changed '
                          f"(got {value!r}, expected {item['value']!r})")
        elif operator_env:
            if not value:
                errors.append(f'{profile}/{source}: {name} must have a provisioned value')
            elif value.startswith(prefix):
                errors.append(f'{profile}/{source}: {name} still carries a placeholder; '
                              'provision the real operator-owned value (0600 file)')
        elif not _placeholder_ok(value, item):
            errors.append(f'{profile}/{source}: {name} must keep its '
                          f'{prefix}... placeholder')
    # Secret-bearing keys: in shipped/staged artifacts never allow real values;
    # in operator env files they must exist as real provisioned values.
    for name, value in fields.items():
        if (name in schema['secret_keys'] and not operator_env
                and not value.startswith(prefix)):
            errors.append(f'{profile}/{source}: {name} must remain a placeholder '
                          'in shipped/staged artifacts')
    for item in spec['env'].get('forbidden_defaults', []):
        name = item['name']
        if name in fields:
            errors.append(f'{profile}/{source}: {name} is a restricted-profile '
                          'artifact and must not be provisioned on this profile')
    if profile == 'restricted':
        if not fields.get('A3_DISPATCH_ALLOWED_ROOTS'):
            errors.append('restricted/operator-env: explicit workspace confinement '
                          '(A3_DISPATCH_ALLOWED_ROOTS) is required on this profile')
    if profile == 'os-native':
        # A trusted-policy pointer is optional; but if present it must look like
        # a path placeholder or an operator path, never inline JSON/secrets.
        pass
    return errors


def validate_env_path(path, profile):
    try:
        fields = parse_env(Path(path).read_text())
    except OSError as exc:
        return [f'{path}: unreadable ({type(exc).__name__})']
    except ValueError as exc:
        return [f'{path}: {exc}']
    return validate_fields(fields, profile, source='operator-env')


def validate_examples():
    errors = []
    for profile, path in EXAMPLES.items():
        try:
            fields = parse_env(path.read_text())
        except (OSError, ValueError) as exc:
            errors.append(f'{path}: {exc}')
            continue
        errors.extend(validate_fields(fields, profile, source='example'))
    errors.extend(_schema_internal_consistency())
    return errors


def _schema_internal_consistency():
    schema = load_schema()
    errors = []
    defaults = [p for p, s in schema['profiles'].items() if s.get('default')]
    if len(defaults) != 1:
        errors.append(f'schema: exactly one default profile required, found {defaults}')
    for name, spec in schema['profiles'].items():
        if not spec.get('env', {}).get('required'):
            errors.append(f'schema: profile {name} lacks required env contract')
    return errors


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-path', help='operator env file to validate')
    parser.add_argument('--profile', choices=sorted(EXAMPLES),
                        help='profile to validate against (with --env-path)')
    args = parser.parse_args()
    if args.env_path:
        errors = validate_env_path(args.env_path, args.profile or 'os-native')
    else:
        errors = validate_examples()
    print(json.dumps({'result': 'PASS' if not errors else 'FAIL',
                      'errors': errors,
                      'schema': SCHEMA_PATH.name,
                      'target': args.env_path or 'shipped-examples'}))
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())