"""Deny host filesystem access using a fresh Bubblewrap mount/PID/net namespace.

Only an operator-reviewed manifest snapshot is visible at /workspace. No
host HOME, repository .git, AEE env, job store, sockets or auth is mounted.
Networking is disabled pending a P2C credential/egress-broker design.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path, PurePosixPath

from .process import Limits
from .store import JobError

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_WORKSPACE_FILES = 10000
MAX_FILE_BYTES = 4 * 1024**2
MAX_WORKSPACE_BYTES = 64 * 1024**2
BLOCKED = {".git", ".ssh", ".codex", ".config", ".venv", "secrets", "credentials", "private"}


def safe_relative(name):
    if not isinstance(name, str) or not name or len(name.encode()) > 1024 or "\x00" in name:
        return False
    path = PurePosixPath(name)
    if not path.parts or path.is_absolute() or ".." in path.parts or str(path) != name:
        return False
    return not any(part.lower() in BLOCKED or part.lower().startswith(".env")
                   or part.lower().endswith((".pem", ".key", ".p12")) for part in path.parts)


def read_manifest(path):
    if path is None:
        raise JobError("ISOLATION_UNAVAILABLE", "An operator-reviewed workspace manifest is required")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("not regular")
            payload = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(payload) > MAX_MANIFEST_BYTES:
            raise ValueError("oversized")
        entries = json.loads(payload)
        if not isinstance(entries, dict) or len(entries) > MAX_WORKSPACE_FILES:
            raise ValueError("invalid manifest")
        for name, digest in entries.items():
            if not safe_relative(name) or not isinstance(digest, str) or not re.fullmatch("[a-f0-9]{64}", digest):
                raise ValueError("invalid entry")
        return entries
    except (OSError, TypeError, ValueError):
        raise JobError("ISOLATION_UNAVAILABLE", "Invalid workspace manifest") from None


def snapshot_workspace(source, entries, destination, *, source_modes=None):
    if source_modes is not None and (set(source_modes) != set(entries) or
            any(mode not in {"100644", "100755"} for mode in source_modes.values())):
        raise JobError("ISOLATION_UNAVAILABLE", "Invalid committed executable metadata")
    rootfd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    total = 0
    try:
        for name, digest in entries.items():
            if not safe_relative(name):
                raise JobError("ISOLATION_UNAVAILABLE", "Unsafe manifest path")
            parentfd = os.dup(rootfd)
            try:
                parts = PurePosixPath(name).parts
                for part in parts[:-1]:
                    nextfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                     dir_fd=parentfd)
                    os.close(parentfd)
                    parentfd = nextfd
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=parentfd)
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ValueError("not a private regular source file")
                    data = stream.read(MAX_FILE_BYTES + 1)
                total += len(data)
                if len(data) > MAX_FILE_BYTES or total > MAX_WORKSPACE_BYTES:
                    raise JobError("OUTPUT_LIMIT_EXCEEDED", "Workspace snapshot exceeds size limit")
                if hashlib.sha256(data).hexdigest() != digest:
                    raise JobError("ISOLATION_UNAVAILABLE", "Source differs from reviewed manifest")
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                executable = (source_modes[name] == "100755" if source_modes is not None
                              else bool(info.st_mode & 0o111))
                target.chmod(0o555 if executable else 0o444)
            finally:
                os.close(parentfd)
    except (OSError, ValueError):
        raise JobError("ISOLATION_UNAVAILABLE", "Workspace snapshot could not be safely constructed") from None
    finally:
        os.close(rootfd)
    return {"files": len(entries), "bytes": total,
            "manifest_sha256": hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()}


def runner_env():
    return {"HOME": "/runner/state/home", "CODEX_HOME": "/runner/state/codex",
            "XDG_CONFIG_HOME": "/runner/state/config", "XDG_DATA_HOME": "/runner/state/data",
            "XDG_CACHE_HOME": "/runner/state/cache", "XDG_STATE_HOME": "/runner/state/xdg-state",
            "TMPDIR": "/tmp", "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}


def sandbox_command(executable, workspace, arguments, limits=Limits(), *, broker_socket=None,
                    resource_revision=None, sandbox_revision=None):
    bwrap = shutil.which("bwrap", path="/usr/bin:/bin")
    prlimit = shutil.which("prlimit", path="/usr/bin:/bin")
    if not bwrap or not prlimit:
        raise JobError("ISOLATION_UNAVAILABLE", "Required sandbox/resource enforcement is unavailable")
    address_bytes = limits.memory_bytes  # historical P2B identity/test profile
    cpu_soft, cpu_hard = limits.cpu_seconds, limits.cpu_seconds + 1
    file_bytes, nofile, core = limits.file_bytes, 128, 0
    if resource_revision is not None:
        from .resource_policy import resource_profile, verify_current_containment
        policy = resource_profile()
        if resource_revision != policy['revision']:
            raise JobError('RESOURCE_POLICY_INVALID', 'Unreviewed resource revision')
        verify_current_containment()  # larger virtual reserve requires real containment
        rlimits = policy['rlimits']
        address_bytes = rlimits['address_space_bytes']
        cpu_soft = min(cpu_soft, rlimits['cpu_soft_seconds'])
        cpu_hard = min(cpu_hard, rlimits['cpu_hard_seconds'])
        file_bytes = min(file_bytes, rlimits['file_bytes'])
        nofile, core = rlimits['open_files'], rlimits['core_bytes']
    if sandbox_revision is not None:
        from .sandbox_policy import verify_sandbox
        verify_sandbox(sandbox_revision, resource_revision, bwrap)
    command = [bwrap, "--unshare-all", "--die-with-parent", "--cap-drop", "ALL",
               "--ro-bind", "/usr/bin", "/usr/bin", "--ro-bind", "/usr/lib", "/usr/lib",
               "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
               "--symlink", "usr/lib", "/lib64", "--proc", "/proc", "--dev", "/dev",
               "--size", "16777216", "--tmpfs", "/tmp",
               "--size", "16777216", "--tmpfs", "/runner/state",
               "--ro-bind", str(workspace), "/workspace",
               "--ro-bind", str(executable), "/runner/codex", "--chdir", "/workspace",
               "--clearenv"]
    for name in ["home", "codex", "config", "data", "cache", "xdg-state"]:
        command.extend(["--dir", "/runner/state/" + name])
    if broker_socket is not None:
        # A single admitted job's UDS is the only added host capability.
        path = Path(broker_socket)
        if path.is_symlink() or not stat.S_ISSOCK(path.stat().st_mode):
            raise JobError("BROKER_UNAVAILABLE", "Job inference socket is unavailable")
        relay = Path(__file__).with_name("relay.py")
        command.extend(["--ro-bind", str(path), "/runner/inference.sock",
                        "--ro-bind", str(relay), "/runner/relay.py"])
        if sandbox_revision is not None:
            telemetry = Path(__file__).with_name('native_telemetry.py')
            command.extend(['--ro-bind', str(telemetry), '/runner/native_telemetry.py',
                            '--setenv', 'AEE_NATIVE_TOOL_RECEIPT', '1'])
        companion = Path(executable).with_name("codex-code-mode-host")
        if companion.exists():
            command.extend(["--ro-bind", str(companion), "/runner/codex-code-mode-host"])
        arguments = ["/usr/bin/python3", "-I", "-B", "/runner/relay.py", *arguments]
    command.extend(["--remount-ro", "/"])
    if sandbox_revision is None:
        command.extend(['--remount-ro', '/proc'])
    # R3 leaves only this fresh job-PID proc mount writable for nested uid/gid
    # setup. Host proc is never bound; native tools create a further PID/proc
    # sandbox and seccomp boundary. Root/source/HOME admission are unchanged.
    for key, value in runner_env().items():
        command.extend(["--setenv", key, value])
    # prlimit performs limit setup before exec; no unsafe preexec_fn in threads.
    command.extend(["--", prlimit, f"--as={address_bytes}", f"--cpu={cpu_soft}:{cpu_hard}",
                    f"--fsize={file_bytes}", f"--nofile={nofile}", f"--core={core}", "--"])
    command.extend(arguments)
    return command
