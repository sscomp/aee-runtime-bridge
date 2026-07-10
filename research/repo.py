"""Repository scanning — files, dependencies, import graph, entry points.

Used by the Research Agent to auto-generate Architecture Reports.
"""
from __future__ import annotations

import os
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# File extensions to consider source files (for stats + import graph).
SOURCE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".sh", ".bash"}
# Skip heavy / generated directories by default.
SKIP_DIRS = {
    "__pycache__", ".git", ".venv", "venv", "node_modules", "dist", "build",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "target", ".next", "out",
}
# File extensions counted as config / docs (for stats).
CONFIG_EXTS = {".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".conf", ".env"}
DOC_EXTS = {".md", ".rst", ".txt"}


def _walk(root: Path, max_files: int = 5000) -> List[Path]:
    """Yield files under root, skipping SKIP_DIRS, up to max_files."""
    out: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skip dirs in place.
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            out.append(p)
            if len(out) >= max_files:
                return out
    return out


def _classify(p: Path) -> str:
    ext = p.suffix.lower()
    if ext in SOURCE_EXTS:
        return "source"
    if ext in CONFIG_EXTS:
        return "config"
    if ext in DOC_EXTS:
        return "doc"
    return "other"


def _parse_python_imports(text: str) -> Set[str]:
    """Extract top-level module names from `import x` / `from x import y`."""
    mods: Set[str] = set()
    for m in re.finditer(r"^\s*import\s+([\w.]+)", text, re.MULTILINE):
        mods.add(m.group(1).split(".")[0])
    for m in re.finditer(r"^\s*from\s+([\w.]+)\s+import", text, re.MULTILINE):
        mods.add(m.group(1).split(".")[0])
    return mods


def _read_requirements(path: Path) -> List[str]:
    if not path.exists():
        return []
    out: List[str] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def _read_pyproject_deps(path: Path) -> List[str]:
    if not path.exists():
        return []
    try:
        import tomllib  # py3.11+
    except ImportError:
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    deps = (data.get("project") or {}).get("dependencies") or []
    return [str(d) for d in deps]


def _detect_entry_points(root: Path) -> List[Dict[str, str]]:
    """Find likely entry points: FastAPI `app = FastAPI(...)`, `def main(`, CLI shims."""
    hits: List[Dict[str, str]] = []
    for p in _walk(root, max_files=2000):
        if p.suffix != ".py":
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        rel = str(p.relative_to(root))
        if re.search(r"^app\s*=\s*FastAPI\(", text, re.MULTILINE):
            hits.append({"kind": "fastapi_app", "path": rel})
        if re.search(r"^def\s+main\s*\(", text, re.MULTILINE):
            hits.append({"kind": "cli_main", "path": rel})
        if re.search(r"^if\s+__name__\s*==\s*['\"]__main__['\"]", text, re.MULTILINE):
            hits.append({"kind": "script_entry", "path": rel})
    return hits


def _git_info(root: Path) -> Dict[str, Optional[str]]:
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, stderr=subprocess.DEVNULL, text=True
        ).strip()
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root,
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        return {"commit": commit, "branch": branch}
    except Exception:
        return {"commit": None, "branch": None}


def scan(root: str | Path) -> Dict[str, Any]:
    """Return a structured snapshot of `root` repository."""
    root = Path(root).resolve()
    if not root.exists() or not root.is_dir():
        return {"error": f"root not found: {root}"}
    files = _walk(root)
    by_class: Dict[str, int] = defaultdict(int)
    by_ext: Dict[str, int] = defaultdict(int)
    py_imports: Set[str] = set()
    py_files: List[Path] = []
    for p in files:
        cls = _classify(p)
        by_class[cls] += 1
        by_ext[p.suffix.lower() or "<noext>"] += 1
        if p.suffix == ".py":
            py_files.append(p)
    # Sample imports from the first 100 .py files (full graph for tiny repos).
    for p in py_files[:100]:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        py_imports.update(_parse_python_imports(text))
    # Deps
    reqs = _read_requirements(root / "requirements.txt")
    pyproject = _read_pyproject_deps(root / "pyproject.toml")
    entry_points = _detect_entry_points(root)
    return {
        "root": str(root),
        "file_count": len(files),
        "by_class": dict(by_class),
        "by_ext_top10": dict(sorted(by_ext.items(), key=lambda kv: -kv[1])[:10]),
        "py_file_count": len(py_files),
        "py_imports_sample": sorted(py_imports)[:80],
        "requirements_txt": reqs,
        "pyproject_dependencies": pyproject,
        "entry_points": entry_points,
        "git": _git_info(root),
    }
