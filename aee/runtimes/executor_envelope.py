"""Evidence/verification helpers for the ``POST /runs/executor`` envelope.

Pure, read-only functions — no side effects, no git mutation, no network.

* :func:`truncate_summary` — cap stdout/stderr text for the response.
* :func:`verify_artifacts` — per-path ``exists``/``size``/``mtime``/``sha256``.
* :func:`collect_git_evidence` — read-only ``git rev-parse`` / ``status``
  evidence (HEAD sha, branch, dirty flag, staged file count). Returns a
  null-shaped dict when the path is not a git worktree so the envelope
  is always well-formed.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from typing import Any, Dict, List, Optional


def truncate_summary(text: Optional[str], cap: int) -> str:
    """Return ``text`` capped at ``cap`` bytes/chars with a trailer."""
    if text is None:
        return ""
    if cap <= 0:
        return ""
    if len(text) <= cap:
        return text
    return text[:cap] + f"... [truncated, full length={len(text)}]"


def _sha256(path: str) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def verify_artifacts(
    paths: Optional[List[str]],
    *,
    compute_sha256: bool = True,
) -> List[Dict[str, Any]]:
    """Stat each declared artifact path. Read-only; never creates files."""
    out: List[Dict[str, Any]] = []
    for p in paths or []:
        entry: Dict[str, Any] = {
            "path": p,
            "exists": False,
            "size": None,
            "mtime": None,
            "sha256": None,
        }
        try:
            st = os.stat(p)
        except OSError:
            out.append(entry)
            continue
        entry["exists"] = True
        entry["size"] = st.st_size
        entry["mtime"] = int(st.st_mtime)
        if compute_sha256 and st.st_size > 0:
            entry["sha256"] = _sha256(p)
        out.append(entry)
    return out


def _git(repo: str, args: List[str]) -> Optional[str]:
    try:
        res = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    return res.stdout.strip()


def collect_git_evidence(repo_path: Optional[str]) -> Dict[str, Any]:
    """Read-only git evidence for ``repo_path``.

    Null-shaped (all ``None`` / 0) when the path is missing or not a git
    worktree, so the response envelope is always well-formed. Never runs
    a mutating git command.
    """
    null: Dict[str, Any] = {
        "head_sha": None,
        "branch": None,
        "dirty": None,
        "staged_file_count": 0,
        "repo_path": repo_path,
    }
    if not repo_path or not os.path.isdir(repo_path):
        return null
    head = _git(repo_path, ["rev-parse", "HEAD"])
    if head is None:
        return null
    branch = _git(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"])
    # `--porcelain` is read-only. Count any porcelain entry as "dirty".
    porcelain = _git(repo_path, ["status", "--porcelain"])
    dirty: Optional[bool]
    staged = 0
    if porcelain is None:
        dirty = None
    else:
        lines = [ln for ln in porcelain.splitlines() if ln.strip()]
        dirty = len(lines) > 0
        # Staged = first column (index) is non-empty and not ' ' / '?'.
        for ln in lines:
            if len(ln) >= 2 and ln[0] not in (" ", "?"):
                staged += 1
    return {
        "head_sha": head,
        "branch": branch or None,
        "dirty": dirty,
        "staged_file_count": staged,
        "repo_path": repo_path,
    }


__all__ = ["truncate_summary", "verify_artifacts", "collect_git_evidence"]