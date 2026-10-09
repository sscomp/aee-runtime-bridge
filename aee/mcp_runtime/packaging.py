"""Offline, manifest-backed candidate bundles and fixture-only atomic pointers.

No live installer, service restart, account provisioning or privileged cutover
is exposed by this CLI. Stage 2 is a separately approved operator runbook.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import sys
import importlib.metadata
from pathlib import Path

from aee import __version__
from .manifest import make_manifest, git_source_modes
from .bundle_metadata import CONTRACT, inventory, expected_modes, normalize
from .provider_contract import contract_record, matches_reviewed_executor
from .resource_policy import resource_record
from .sandbox_policy import sandbox_record, FAILURE_REVISION
from .profiles import resolve_codex, executable_digest
from .runtime import deployment_policy
from .sandbox import snapshot_workspace
from .store import JobError

PROTECTED = [Path.home(),
             Path('/opt'), Path('/etc'), Path('/var'), Path('/run')]


def safe_output(path):
    path = Path(path)
    resolved = path.resolve()
    if (path.is_symlink() or not resolved.is_relative_to('/tmp') or resolved == Path('/tmp')
            or any(resolved == p or resolved.is_relative_to(p) for p in PROTECTED)):
        raise JobError('DEPLOYMENT_PATH_DENIED', 'Stage 1 output must be a separate private /tmp fixture path')
    return resolved


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def build_bundle(workspace, destination, native):
    workspace = Path(workspace).resolve(); destination = safe_output(destination)
    if destination.exists() or destination.is_relative_to(workspace):
        raise JobError('DEPLOYMENT_PATH_DENIED', 'Candidate output must be new and outside source')
    status = subprocess.check_output(['git','--no-replace-objects','-C',str(workspace),'-c','core.filemode=false','status','--porcelain','--untracked-files=all'])
    if status:
        raise JobError('DIRTY_CANDIDATE', 'Bundle requires a clean committed source tree')
    source = subprocess.check_output(['git','--no-replace-objects','-C',str(workspace),'rev-parse','HEAD'],text=True).strip()
    identity = resolve_codex(str(native))
    companion = Path(identity.executable).with_name('codex-code-mode-host')
    companion_digest = executable_digest(companion)
    reviewed_executor = matches_reviewed_executor(identity.version, identity.sha256, companion_digest)
    entries = make_manifest(workspace, source)
    source_modes = git_source_modes(workspace, source)
    resources = resource_record()
    if entries.get('config/p2c/resource-profile.json') != resources['sha256']:
        raise JobError('RESOURCE_POLICY_INVALID', 'Bundle must include the pinned resource profile')
    sandbox = sandbox_record()
    if entries.get('config/p2c/sandbox-profile.json') != sandbox['sha256']:
        raise JobError('SANDBOX_POLICY_INVALID', 'Bundle must include the pinned sandbox profile')
    destination.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.aee-stage-',dir=destination.parent) as temporary:
        # A sibling staging root permits rename after root is finalized 0555;
        # moving a readonly directory between parents requires write on it.
        stage=Path(temporary)
        src=stage/'source';src.mkdir()
        snapshot_workspace(workspace,entries,src,source_modes=source_modes)
        runner=stage/'runner';runner.mkdir()
        shutil.copyfile(identity.executable,runner/'codex');(runner/'codex').chmod(0o555)
        shutil.copyfile(companion,runner/'codex-code-mode-host');(runner/'codex-code-mode-host').chmod(0o555)
        if digest(runner/'codex') != identity.sha256 or digest(runner/'codex-code-mode-host') != companion_digest:
            raise JobError('EXECUTOR_IDENTITY_MISMATCH','Executor changed while staging')
        (stage/'workspace-manifest.json').write_text(json.dumps(entries,sort_keys=True)+'\n')
        (stage/'workspace-manifest.json').chmod(0o444)
        (stage/'source-metadata.json').write_text(json.dumps(source_modes,sort_keys=True)+'\n')
        (stage/'source-metadata.json').chmod(0o444)
        units={str(p.relative_to(src/'config/p2c/systemd')):digest(p)
               for p in src.joinpath('config/p2c/systemd').rglob('*') if p.is_file()}
        manifest={'schema':1,'config_schema':'aee-p2c-1','source_commit':source,'input_baseline':source,
                  'aee_version':__version__,'gateway_version':'0.2.0-p2c-candidate',
                  'codex':{'path':str(destination/'runner/codex'),'version':identity.version,'sha256':identity.sha256,
                           'code_mode_host':{'path':str(destination/'runner/codex-code-mode-host'),'sha256':companion_digest}},
                  'execution_profile':'codex-readonly-high','egress':'per-job-uds-responses-broker',
                  'python':{'policy':'CPython 3.13.x; external venv; hash-verified requirements-mcp.lock',
                            'runtime_path':'/opt/aee/venvs/mcp-py313/bin/python',
                            'dependency_lock_sha256':digest(src/'requirements-mcp.lock'),
                            'builder_version':sys.version.split()[0],
                            'builder_packages':dict(sorted((p.metadata['Name'],p.version)
                                                           for p in importlib.metadata.distributions()))},
                  'systemd_unit_revision':units,'metadata_contract':dict(CONTRACT),
                  'source_metadata_sha256':digest(stage/'source-metadata.json'),'workspace_manifest_sha256':digest(stage/'workspace-manifest.json'),
                  'artifact_files':{str(p.relative_to(stage)):digest(p) for p in stage.rglob('*') if p.is_file()},
                  'provider_contract':contract_record(),
                  'native_provider_compatibility':('VERIFIED' if reviewed_executor else 'BLOCKED_UNVERIFIED_EXECUTOR'),
                  'native_tool_runtime_compatibility':('BLOCKED_R3_EVIDENCE_REQUIRED' if reviewed_executor
                                                       else 'BLOCKED_UNVERIFIED_EXECUTOR'),
                  'resource_containment':'BLOCKED_RESOURCE_EVIDENCE_REQUIRED',
                  'resource_policy_revision':resources,
                  'sandbox_policy_revision':sandbox,
                  'job_failure_semantics':'BLOCKED_R3_EVIDENCE_REQUIRED',
                  'job_failure_contract':FAILURE_REVISION,
                  'deployment_status':'STAGE1_NOT_DEPLOYABLE'}
        (stage/'deployment-manifest.json').write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n')
        (stage/'deployment-manifest.json').chmod(0o444)
        normalize(stage,source_modes)
        # Verify everything before publication; manifest paths name the final destination.
        _verify_bundle(stage, location=destination, workspace=workspace, source_commit=source)
        os.rename(stage,destination)
        fd=os.open(destination.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    verify_bundle(destination, workspace=workspace, source_commit=source)
    return manifest


def runtime_manifest_plan(directory,runtime_root):
    manifest=verify_bundle(directory)
    root=Path(runtime_root)
    if not root.is_absolute() or not root.is_relative_to('/opt/aee/releases'):
        raise JobError('DEPLOYMENT_PATH_DENIED','Runtime plan must use a reviewed release path')
    # Return data only; do not write /opt or authorize a deployment.
    manifest['codex']['path']=str(root/'runner/codex')
    manifest['codex']['code_mode_host']['path']=str(root/'runner/codex-code-mode-host')
    return manifest


def verify_bundle(directory, *, workspace=None, source_commit=None, manifest_sha256=None):
    """Verify physical metadata and content, optionally anchored to trusted Git/pin.

    Offline verification uses the serialized committed mode map. Acceptance
    callers supply authoritative Git and a detached manifest digest as well.
    No builder receipt is consumed.
    """
    return _verify_bundle(directory, workspace=workspace, source_commit=source_commit,
                          manifest_sha256=manifest_sha256)


def _verify_bundle(directory, *, location=None, workspace=None, source_commit=None,
                   manifest_sha256=None):
    root = Path(directory).absolute()
    try:
        nodes = inventory(root)
        root = root.resolve()
        manifest = deployment_policy(root/'deployment-manifest.json')
        if manifest_sha256 is not None and digest(root/'deployment-manifest.json') != manifest_sha256:
            raise ValueError('Deployment manifest pin differs')
        if source_commit is not None and manifest['source_commit'] != source_commit:
            raise ValueError('Source commit differs')
        if manifest['metadata_contract'] != CONTRACT:
            raise ValueError('Metadata contract differs')
        expected_location = Path(location).resolve() if location is not None else root
        if (manifest['codex']['path'] != str(expected_location/'runner/codex') or
                manifest['codex']['code_mode_host']['path'] != str(expected_location/'runner/codex-code-mode-host')):
            raise JobError('EXECUTOR_IDENTITY_MISMATCH','Bundle location differs from manifest')
        source_modes = json.loads((root/'source-metadata.json').read_text())
        entries = json.loads((root/'workspace-manifest.json').read_text())
        if not isinstance(entries, dict):
            raise ValueError('Invalid workspace manifest')
        files, modes = expected_modes(source_modes)
        if set(entries) != set(source_modes) or set(nodes) != set(modes):
            raise ValueError('Exact node inventory differs')
        for name, info in nodes.items():
            expected_type = stat.S_ISREG if name in files else stat.S_ISDIR
            if not expected_type(info.st_mode) or stat.S_IMODE(info.st_mode) != modes[name]:
                raise ValueError('Node type or executable intent differs')
        expected = manifest['artifact_files']
        if not isinstance(expected, dict) or set(expected) != set(files) - {'deployment-manifest.json'}:
            raise ValueError('Artifact inventory differs')
        for name, value in expected.items():
            if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{64}',value) or digest(root/name) != value:
                raise ValueError('Artifact digest differs')
        for name, value in entries.items():
            if expected['source/'+name] != value:
                raise ValueError('Source manifest differs')
        if (digest(root/'source-metadata.json') != manifest['source_metadata_sha256'] or
                digest(root/'workspace-manifest.json') != manifest['workspace_manifest_sha256'] or
                expected['runner/codex'] != manifest['codex']['sha256'] or
                expected['runner/codex-code-mode-host'] != manifest['codex']['code_mode_host']['sha256']):
            raise ValueError('Manifest identity differs')
        if workspace is not None:
            revision = source_commit or manifest['source_commit']
            if not isinstance(revision,str) or not re.fullmatch('[a-f0-9]{40}',revision):
                raise ValueError('Invalid source identity')
            if (git_source_modes(workspace,revision) != source_modes or
                    make_manifest(workspace,revision) != entries):
                raise ValueError('Authoritative Git content or executable intent differs')
        return manifest
    except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError):
        raise JobError('ARTIFACT_INTEGRITY_FAILED','Candidate violates metadata/content contract') from None


def fixture_pointer(root, release):
    root=safe_output(root);root.mkdir(mode=0o700,parents=True,exist_ok=True)
    release=Path(release).resolve();verify_bundle(release)
    if not release.is_relative_to(root) or release==root:
        raise JobError('DEPLOYMENT_PATH_DENIED','Fixture release must be below fixture root')
    temporary=root/'.next'
    if temporary.exists() or temporary.is_symlink():
        raise JobError('DEPLOYMENT_PATH_DENIED','Fixture pointer temp already exists')
    os.symlink(os.path.relpath(release,root),temporary)
    os.replace(temporary,root/'current')
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    build=sub.add_parser('build');build.add_argument('--workspace',type=Path,required=True)
    build.add_argument('--destination',type=Path,required=True);build.add_argument('--codex',type=Path,required=True)
    verify=sub.add_parser('verify');verify.add_argument('--directory',type=Path,required=True)
    verify.add_argument('--workspace',type=Path)
    verify.add_argument('--source-commit')
    verify.add_argument('--manifest-sha256')
    plan=sub.add_parser('plan');plan.add_argument('--directory',type=Path,required=True)
    plan.add_argument('--runtime-root',type=Path,required=True)
    args=parser.parse_args()
    if args.action=='plan':
        print(json.dumps(runtime_manifest_plan(args.directory,args.runtime_root),sort_keys=True,indent=2))
        return
    result=build_bundle(args.workspace,args.destination,args.codex) if args.action=='build' else verify_bundle(args.directory,workspace=args.workspace,
                   source_commit=args.source_commit,manifest_sha256=args.manifest_sha256)
    print(json.dumps({'result':'PASS','source_commit':result['source_commit'],
                      'status':result['deployment_status'],'codex_version':result['codex']['version']}))


if __name__=='__main__':main()
