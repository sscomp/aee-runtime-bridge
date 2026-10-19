#!/usr/bin/env python3
"""Audit agent-adapter discovery for the AEE gateway (service-identity).

Reports, per supported adapter, how the RUNNING identity would resolve it:
the documented absolute override env var (A3_ADAPTER_<NAME>_BIN) first, then
executability on the identity's effective PATH — never an interactive shell
PATH. Run under the intended service identity; a CLI installed on the host is
not automatically available to every service identity (a key Phase 1-2
finding). Never reads or prints secrets; only paths, names and codes.

--strict exits non-zero when any adapter in --expect cannot be resolved
(fresh-host preflight), printing ADAPTER_NOT_INSTALLED reasons verbatim.
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aee.mcp_runtime.job_adapters import (  # noqa: E402
    AGENT_NAMES, adapter_specs, resolve_adapter, JobError)


def audit(expect=None, strict=False):
    out = []
    for name in sorted(AGENT_NAMES):
        spec = adapter_specs().get(name)
        entry = {'agent': name,
                 'override_env': spec.env_executable,
                 'override_set': bool(os.environ.get(spec.env_executable)),
                 'default_executable': spec.default_executable,
                 'read_only_enforcement': spec.read_only_enforcement,
                 'write_enforcement': spec.write_enforcement}
        try:
            _found, executable = resolve_adapter(name)
            entry['resolved'] = True
            entry['executable'] = executable
        except JobError as exc:
            entry['resolved'] = False
            entry['error_code'] = getattr(exc, 'code', None) or getattr(
                exc, 'error_code', 'ADAPTER_NOT_INSTALLED')
            entry['reason'] = str(exc)
        out.append(entry)
    missing = [e['agent'] for e in out if not e['resolved']
               and (not expect or e['agent'] in expect)]
    exit_code = 1 if strict and missing else 0
    return {'schema': 'aee-adapter-audit-v1', 'identity_uid': os.getuid(),
            'path': os.environ.get('PATH', ''), 'adapters': out,
            'strict': strict, 'missing_required': missing}, exit_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expect', default='', help='comma-separated agents that must resolve '
                        'for --strict (default: any resolved adapter)')
    parser.add_argument('--strict', action='store_true')
    args = parser.parse_args()
    result, code = audit(expect={a for a in args.expect.split(',') if a}, strict=args.strict)
    print(json.dumps(result, indent=1))
    sys.exit(code)


if __name__ == '__main__':
    main()