"""Prepare a hash manifest from an already-reviewed committed Git tree.

This is an operator preparation tool, never a ChatGPT tool. Review and run
the secret gate before trusting an artifact. Credentials, symlinks, gitlinks
and Git history are excluded. Working-tree modifications do not change the
committed hashes and consequently fail snapshot validation.
"""
import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

from .sandbox import MAX_FILE_BYTES, MAX_MANIFEST_BYTES, MAX_WORKSPACE_BYTES, MAX_WORKSPACE_FILES, safe_relative
from .store import JobError


def git_source_modes(workspace, revision="HEAD"):
    """Executable intent from the committed tree, never checkout permissions."""
    records = subprocess.check_output([
        "git", "--no-replace-objects", "-C", str(workspace),
        "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
        "ls-tree", "-r", "-z", revision,
    ]).split(b"\0")
    entries = {}
    for record in records:
        if record:
            metadata, name = record.split(b"\t", 1)
            mode, kind, _ = metadata.decode().split()
            name = name.decode()
            if mode in {"100644", "100755"} and kind == "blob" and safe_relative(name):
                entries[name] = mode
    return entries


def make_manifest(workspace, revision="HEAD"):
    command = ["git", "--no-replace-objects", "-C", str(workspace),
               "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]
    records = subprocess.check_output(command + ["ls-tree", "-r", "-z", revision]).split(b"\0")
    entries = {}
    total = 0
    process = subprocess.Popen(command + ["cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for record in records:
            if not record:
                continue
            metadata, name = record.split(b"\t", 1)
            mode, kind, oid = metadata.decode().split()
            name = name.decode()
            if mode not in {"100644", "100755"} or kind != "blob" or not safe_relative(name):
                continue
            process.stdin.write(oid.encode() + b"\n")
            process.stdin.flush()
            header = process.stdout.readline().decode().split()
            size = int(header[2])
            if header[1] != "blob" or size > MAX_FILE_BYTES:
                raise JobError("OUTPUT_LIMIT_EXCEEDED", "Reviewed file exceeds snapshot limit")
            data = process.stdout.read(size)
            if len(data) != size or process.stdout.read(1) != b"\n":
                raise JobError("ISOLATION_UNAVAILABLE", "Incomplete Git object")
            total += size
            if total > MAX_WORKSPACE_BYTES:
                raise JobError("OUTPUT_LIMIT_EXCEEDED", "Reviewed tree exceeds snapshot limit")
            entries[name] = hashlib.sha256(data).hexdigest()
            if len(entries) > MAX_WORKSPACE_FILES:
                raise JobError("OUTPUT_LIMIT_EXCEEDED", "Manifest contains too many files")
        if len(json.dumps(entries).encode()) > MAX_MANIFEST_BYTES:
            raise JobError("OUTPUT_LIMIT_EXCEEDED", "Manifest exceeds supported size")
        return entries
    finally:
        process.stdin.close()
        process.stdout.close()
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    output = args.output.resolve()
    if output.is_relative_to(workspace):
        parser.error("Keep the trusted manifest outside the execution workspace")
    entries = make_manifest(workspace)
    output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(entries, stream, sort_keys=True)
        stream.write("\n")
    print(json.dumps({"files": len(entries), "manifest": str(output)}))


if __name__ == "__main__":
    main()
