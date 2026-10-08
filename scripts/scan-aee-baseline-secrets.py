#!/usr/bin/env python3
"""Read-only baseline secret gate. Reports locations, never matched values.

Known local env secrets stay in memory, never subprocess argv. Pattern checks
are conservative; this is a capture gate, not a guarantee against every secret
format. Historical test-marker exceptions are explicit and path-scoped.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

PATTERNS = {
    "openai_key": rb"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}",
    "github_token": rb"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}",
    "slack_token": rb"xox[baprs]-[A-Za-z0-9-]{15,}",
    "oauth_jwt": rb"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+",
    "ssh_private_material": rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    "literal_bearer": rb"(?i)\bBearer[ \t]+[A-Za-z0-9_-]{20,}",
    "literal_credential_assignment": rb"(?i)\b(?:[A-Z_]*API_KEY|API_SERVER_KEY|[A-Z_]*TOKEN|PASSWORD|[A-Z_]*SECRET)\s*[:=]\s*[\"\'][A-Za-z0-9_+/=-]{16,}[\"\']",
}
FIXTURE_PATHS = {
    "aee/tests/test_aee77_apply_sidecars.py",
    "aee/tests/test_aee77d_sidecar_migration.py",
    "aee/tests/test_aee77e_live_migration_dryrun.py",
}


def git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args])


def local_secrets(repo: Path) -> list[bytes]:
    values = []
    for path in repo.glob(".env*"):
        if path.name.endswith("example") or not path.is_file():
            continue
        for line in path.read_text().splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip("\"'")
            if re.search(r"key|token|secret|password|auth", key, re.I) and len(value) > 10:
                values.append(value.encode())
    return values


def findings(path: str, data: bytes, known: list[bytes]) -> tuple[list[dict], int]:
    found = []
    exceptions = 0
    for value in known:
        offset = data.find(value)
        if offset >= 0:
            found.append({"path": path, "line": data[:offset].count(b"\n") + 1,
                          "kind": "known_local_secret"})
    for kind, pattern in PATTERNS.items():
        for match in re.finditer(pattern, data):
            value = match.group().lower()
            fixture = (path in FIXTURE_PATHS and kind == "openai_key"
                       and (b"runtime" in value or b"fixture" in value))
            literal = re.search(rb"[\"']([^\"']+)[\"']$", value)
            fixture = fixture or (path == "tests/test_b4_db_guard_fail_closed.py"
                                  and kind == "literal_credential_assignment" and literal
                                  and literal.group(1) == b"sandbox-test-key")
            placeholder = (kind == "literal_credential_assignment" and literal and
                           re.fullmatch(rb"(?:dummy|fake|test|example|placeholder|changeme|redacted|synthetic|your)[a-z0-9_-]*",
                                        literal.group(1)))
            if fixture or placeholder:
                exceptions += 1
                continue
            found.append({"path": path, "line": data[:match.start()].count(b"\n") + 1,
                          "kind": kind})
    return found, exceptions


def history_blobs(repo: Path, revision: str | None):
    args = ["rev-list", "--objects", revision] if revision else ["rev-list", "--objects", "--all"]
    objects = git(repo, *args).decode().splitlines()
    process = subprocess.Popen(["git", "-C", str(repo), "cat-file", "--batch"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for item in objects:
            oid, _, path = item.partition(" ")
            process.stdin.write((oid + "\n").encode())
            process.stdin.flush()
            header = process.stdout.readline().decode().split()
            if len(header) != 3:
                raise RuntimeError("Git object unavailable")
            size = int(header[2])
            data = process.stdout.read(size)
            if len(data) != size or process.stdout.read(1) != b"\n":
                raise RuntimeError("Incomplete Git object")
            if header[1] == "blob":
                yield path, data
    finally:
        process.stdin.close()
        process.stdout.close()
        if process.wait() != 0:
            raise RuntimeError("Git object reader failed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--known-env-dir", type=Path,
                        help="Optional local credential directory; values stay in memory")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--staged", action="store_true")
    scope.add_argument("--history", action="store_true", help="all reachable local Git refs")
    scope.add_argument("--history-range", help="e.g. main..HEAD")
    args = parser.parse_args()
    repo = args.repo.resolve()
    known = local_secrets(repo)
    if args.known_env_dir is not None and args.known_env_dir.resolve() != repo:
        known.extend(local_secrets(args.known_env_dir.resolve()))
    if args.history or args.history_range:
        blobs = history_blobs(repo, args.history_range)
    elif args.staged:
        names = git(repo, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").decode().split("\0")
        blobs = ((name, git(repo, "show", ":" + name)) for name in names if name)
    else:
        names = git(repo, "ls-files", "-z").decode().split("\0")
        blobs = ((name, (repo / name).read_bytes()) for name in names
                 if name and (repo / name).is_file())
    count = exceptions = 0
    issues = []
    for path, data in blobs:
        count += 1
        if Path(path).name in {".env.aee-mcp", ".env.aee-mcp-tunnel", ".env.aee-tunnel-creds"}:
            issues.append({"path": path, "kind": "live_credential_file"})
        found, exempted = findings(path, data, known)
        issues.extend(found)
        exceptions += exempted
    print(json.dumps({"result": "FAIL" if issues else "PASS", "blobs_scanned": count,
                      "documented_fixture_exceptions": exceptions, "findings": issues}, indent=2))
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
