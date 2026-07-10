"""Prompt loader.

Prompts live in `<bridge_root>/prompts/<name>_v<n>.md`. `load()` returns
the body of the requested prompt, or the latest version when no
version is given.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_BRIDGE_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = _BRIDGE_ROOT / "prompts"

_VERSION_RE = re.compile(r"^(?P<name>.+)_v(?P<n>\d+)$")


def list_prompts() -> List[Dict[str, str]]:
    """Return [{name, version, path, size}, ...] for every prompt on disk."""
    if not PROMPTS_DIR.exists():
        return []
    out: List[Dict[str, str]] = []
    for p in sorted(PROMPTS_DIR.glob("*.md")):
        stem = p.stem
        m = _VERSION_RE.match(stem)
        if not m:
            continue
        out.append({
            "name": m.group("name"),
            "version": f"v{m.group('n')}",
            "prompt_version": stem,  # canonical id, e.g. "macro_v1"
            "path": str(p),
            "size": str(p.stat().st_size),
        })
    return out


def list_versions(name: str) -> List[str]:
    """Return available versions for `name` (e.g. ['v1', 'v2']) sorted by n."""
    if not PROMPTS_DIR.exists():
        return []
    versions: List[Tuple[int, str]] = []
    for p in PROMPTS_DIR.glob(f"{name}_v*.md"):
        m = _VERSION_RE.match(p.stem)
        if m:
            versions.append((int(m.group("n")), m.group("v" + "n") if False else f"v{m.group('n')}"))
    versions.sort()
    return [v for _, v in versions]


def latest_version(name: str) -> Optional[str]:
    vs = list_versions(name)
    return vs[-1] if vs else None


def load(name: str, version: Optional[str] = None) -> str:
    """Return the prompt body. `version` like 'v1' / 'v2'. Defaults to latest."""
    if version is None:
        version = latest_version(name)
    if version is None:
        raise FileNotFoundError(f"No prompts found for name={name!r} in {PROMPTS_DIR}")
    # Tolerate caller passing 'v1' or '1'.
    v = version.lstrip("v")
    path = PROMPTS_DIR / f"{name}_v{v}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt not found: {path}")
    return path.read_text(encoding="utf-8")


def prompts_dir() -> Path:
    return PROMPTS_DIR
