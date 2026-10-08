#!/usr/bin/env python3
"""Package the actual clean checkout using a synthetic ELF; never deploy/infer."""
import argparse
import json
import subprocess
import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aee.mcp_runtime.packaging import build_bundle, verify_bundle, runtime_manifest_plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=ROOT)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    with tempfile.TemporaryDirectory(prefix='aee-canonical-smoke-', dir='/tmp') as name:
        root = Path(name)
        native = root / 'codex'
        subprocess.run(['gcc', '-O2', str(workspace / 'tests/mcp/fixtures/codex_probe.c'),
                        '-o', str(native)], check=True)
        companion = root / 'codex-code-mode-host'
        companion.write_bytes(native.read_bytes()); companion.chmod(0o755)
        bundle = root / 'bundle'
        built = build_bundle(workspace, bundle, native)
        verified = verify_bundle(bundle, workspace=workspace, source_commit=built['source_commit'])
        plan = runtime_manifest_plan(bundle, Path('/opt/aee/releases') / built['source_commit'])
        assert verified['deployment_status'] == plan['deployment_status'] == 'STAGE1_NOT_DEPLOYABLE'
        assert verified['native_provider_compatibility'] == 'BLOCKED_UNVERIFIED_EXECUTOR'
        print(json.dumps({'result': 'PASS', 'source_commit': built['source_commit'],
                          'source_files': len(json.loads((bundle / 'workspace-manifest.json').read_text())),
                          'deployment_status': plan['deployment_status'],
                          'scope': 'actual committed checkout; synthetic ELF; no inference or deployment'}))


if __name__ == '__main__':
    main()
