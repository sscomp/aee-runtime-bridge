"""Builder-owned immutable candidate metadata contract; no live filesystem writes."""
import os
import stat
from pathlib import Path

from .sandbox import safe_relative
from .store import JobError

CONTRACT = {'revision': 'aee-p2c-metadata-2', 'uid': os.geteuid(), 'gid': os.getegid(),
            'data_mode': '0444', 'executable_mode': '0555', 'directory_mode': '0555',
            'symlinks': False, 'special_nodes': False}
NATIVE = {'runner/codex', 'runner/codex-code-mode-host'}
GENERATED = {'workspace-manifest.json', 'source-metadata.json', 'deployment-manifest.json'}


def inventory(root):
    """lstat every node before reading manifests; never traverse special nodes."""
    root = Path(root)
    nodes = {}

    def visit(path):
        info = path.lstat()
        name = str(path.relative_to(root))
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise JobError('ARTIFACT_INTEGRITY_FAILED', 'Candidate contains a symlink or special node')
        if (info.st_uid, info.st_gid) != (CONTRACT['uid'], CONTRACT['gid']):
            raise JobError('ARTIFACT_INTEGRITY_FAILED', 'Candidate ownership differs from contract')
        mode = stat.S_IMODE(info.st_mode)
        directory = stat.S_ISDIR(info.st_mode)
        if mode not in ({0o555} if directory else {0o444, 0o555}):
            raise JobError('ARTIFACT_INTEGRITY_FAILED', 'Candidate mode differs from contract')
        nodes[name] = info
        if directory:
            with os.scandir(path) as entries:
                for entry in entries:
                    visit(Path(entry.path))
    visit(root)
    return nodes


def expected_modes(source_modes):
    if not isinstance(source_modes, dict) or not source_modes:
        raise ValueError('Missing committed source metadata')
    files = {name: 0o444 for name in GENERATED}
    files.update({name: 0o555 for name in NATIVE})
    for name, mode in source_modes.items():
        if not safe_relative(name) or mode not in {'100644', '100755'}:
            raise ValueError('Invalid source metadata')
        files['source/' + name] = 0o555 if mode == '100755' else 0o444
    modes = {'.': 0o555, **files}
    for name in files:
        for parent in Path(name).parents:
            key = str(parent)
            if key in files:
                raise ValueError('File/directory path collision')
            modes[key] = 0o555
    return files, modes


def normalize(root, source_modes):
    """Final staging boundary: set every mode including manifests and root."""
    files, modes = expected_modes(source_modes)
    paths = [root] + list(root.rglob('*'))
    if {str(p.relative_to(root)) for p in paths} != set(modes):
        raise JobError('ARTIFACT_INTEGRITY_FAILED', 'Staging inventory differs from contract')
    for path in sorted(paths, key=lambda p: len(p.parts), reverse=True):
        name = str(path.relative_to(root))
        info = path.lstat()
        expected_type = stat.S_ISREG if name in files else stat.S_ISDIR
        if not expected_type(info.st_mode):
            raise JobError('ARTIFACT_INTEGRITY_FAILED', 'Unexpected staging node type')
        path.chmod(modes[name], follow_symlinks=False)
