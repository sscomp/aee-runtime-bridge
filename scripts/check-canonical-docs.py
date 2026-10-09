#!/usr/bin/env python3
"""Check the maintained onboarding graph, shell syntax and deployment boundaries."""
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ['README.md', 'docs/architecture.md', 'docs/deployment.md',
        'docs/agent-operations.md', 'docs/chatgpt-mcp.md', 'docs/troubleshooting.md',
        'docs/operations.md', 'docs/openai-secure-mcp-tunnel.md',
        'docs/chatgpt-plugin-deployment.md', 'docs/legacy-http-profiles.md',
        'docs/legacy-retirement.md', 'docs/security-model.md',
        'docs/MIGRATION_FROM_AEE_MINI.md', 'docs/HERMES_ADAPTER_CONTRACT_MATRIX.md',
        'config/p2c/README.md', 'tests/mcp/README.md']
FIVE = {'aee_status', 'aee_agents', 'aee_dispatch', 'aee_job_status', 'aee_job_result'}


def main():
    errors = []
    blocks = links = 0
    for name in DOCS:
        path = ROOT / name
        if not path.is_file():
            errors.append(f'{name}: missing document'); continue
        text = path.read_text()
        if re.search(r'/home/(?!operator(?:/|\b))[A-Za-z0-9_.-]+/|/Users/[A-Za-z0-9_.-]+/', text):
            errors.append(f'{name}: machine-specific path')
        for target in re.findall(r'\[[^\]]+\]\(([^)]+)\)', text):
            if re.match(r'https?://', target) or target.startswith('#'): continue
            links += 1
            target = target.split('#')[0]
            if not (path.parent / target).is_file():
                errors.append(f'{name}: broken local link {target}')
        for code in re.findall(r'```bash\n(.*?)\n```', text, re.S):
            blocks += 1
            result = subprocess.run(['bash', '-n'], input=code, text=True, capture_output=True)
            if result.returncode: errors.append(f'{name}: invalid bash block')
    envs = {}
    for surface in ['local', 'restricted']:
        path = ROOT / f'config/p2c/gateway-{surface}.env.example'
        fields = dict(line.split('=', 1) for line in path.read_text().splitlines()
                      if line and not line.startswith('#'))
        envs[surface] = fields
        for key, value in {'AEE_MCP_HOST': '127.0.0.1', 'AEE_MCP_REQUIRE_AUTH': 'true',
                           'AEE_MCP_SURFACE': surface, 'AEE_MCP_PORT': '8791' if surface == 'restricted' else '8790'}.items():
            if fields.get(key) != value: errors.append(f'{path.name}: {key} boundary changed')
        if not fields.get('MCP_BRIDGE_API_KEY', '').startswith('<'):
            errors.append(f'{path.name}: credential must remain placeholder')
        if not fields.get('AEE_RUNTIME_MANIFEST') or not fields.get('AEE_BROKER_CONTROL'):
            errors.append(f'{path.name}: production gate/broker missing')
    if set(envs['restricted']['AEE_MCP_EXPOSED_TOOLS'].split(',')) != FIVE:
        errors.append('restricted example: five-tool contract changed')
    # Use runtime validators: changed JSON bytes invalidate reviewed identities.
    import sys
    sys.path.insert(0, str(ROOT))
    from aee.mcp_runtime.resource_policy import resource_profile
    from aee.mcp_runtime.sandbox_policy import sandbox_profile
    resource_profile(); sandbox_profile()
    print(json.dumps({'result': 'FAIL' if errors else 'PASS', 'documents': len(DOCS),
                      'local_links': links, 'bash_blocks': blocks, 'errors': errors}))
    return bool(errors)


if __name__ == '__main__':
    raise SystemExit(main())
