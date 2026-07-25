"""Executor configuration for the ``POST /runs/executor`` endpoint.

This loads ``config/executor.json`` (falling back to the module-level
defaults below if the file is absent or unreadable) and exposes a small,
pure-function API used by the endpoint:

* :func:`load_executor_config` — the merged config dict (file > defaults,
  with a handful of scalar env overrides so operators can retarget the
  CLI binary / timeout without editing the file).
* :func:`canonical_executor` — normalise an alias (``claude_code``,
  ``claude-code``) to the single canonical wire value ``claude-code-cli``.
  Returns ``None`` for an unknown / unsupported value so the caller can
  surface a deterministic 400.
* :func:`supported_executors` / :func:`is_supported` — convenience.

The config is intentionally minimal and additive: the existing
``metadata.executor`` path on ``POST /runs`` is untouched.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

# Canonical defaults mirrored into config/executor.json. Kept here so
# the loader is robust even if the JSON file is deleted.
_DEFAULTS: Dict[str, Any] = {
    "supported_executors": ["claude-code-cli", "hermes"],
    "executor_aliases": {
        "claude-code-cli": "claude-code-cli",
        "claude_code": "claude-code-cli",
        "claude-code": "claude-code-cli",
        "claudecode": "claude-code-cli",
    },
    "claude_cli_binary": "/home/ubuntu/.local/bin/claude",
    "default_executor": "claude-code-cli",
    "default_timeout_sec": 120,
    "max_timeout_sec": 7200,
    "max_turns": 80,
    "bare": False,
    "output_format": "text",
    "stdout_summary_cap": 2000,
    "stderr_summary_cap": 1000,
    "artifact_sha256": True,
    "extra_cli_args": [],
    "repo_allowlist": ["/home/ubuntu/Abacus", "/tmp"],
}

# Env var -> config key for the scalar knobs an operator is most likely
# to override at deploy time. Lists / dicts are file-only (no env parse)
# to keep the override surface tiny and predictable.
_ENV_OVERRIDES = {
    "AEE_CLAUDE_CLI_BINARY": "claude_cli_binary",
    "AEE_EXECUTOR_DEFAULT": "default_executor",
    "AEE_EXECUTOR_DEFAULT_TIMEOUT": "default_timeout_sec",
    "AEE_EXECUTOR_MAX_TIMEOUT": "max_timeout_sec",
    "AEE_EXECUTOR_MAX_TURNS": "max_turns",
    "AEE_EXECUTOR_BARE": "bare",
    "AEE_EXECUTOR_OUTPUT_FORMAT": "output_format",
}


def _parse_extra_args(raw: str) -> List[str]:
    """Parse a shell-style extra-args string into an argv list."""
    import shlex
    try:
        return shlex.split(raw)
    except ValueError:
        return []


def _coerce(key: str, raw: str) -> Any:
    if key in {"default_timeout_sec", "max_timeout_sec", "max_turns",
               "stdout_summary_cap", "stderr_summary_cap"}:
        try:
            return int(raw)
        except ValueError:
            return raw
    if key == "bare":
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return raw


def load_executor_config() -> Dict[str, Any]:
    """Return the merged executor config (file > defaults > env)."""
    merged: Dict[str, Any] = {k: (v.copy() if isinstance(v, (dict, list)) else v)
                              for k, v in _DEFAULTS.items()}
    try:
        from config import load as _config_load
        file_data = _config_load("executor")
        if isinstance(file_data, dict):
            for k, v in file_data.items():
                merged[k] = v
    except Exception:  # pragma: no cover - config loader is stdlib-safe
        pass
    for env_key, cfg_key in _ENV_OVERRIDES.items():
        if env_key in os.environ:
            merged[cfg_key] = _coerce(cfg_key, os.environ[env_key])
    # Extra CLI args (e.g. a scoped --allowedTools grant) are appended,
    # not replaced, so an operator can layer on a permission without
    # editing the file. Parsed shell-style into an argv list.
    if "AEE_CLAUDE_EXTRA_ARGS" in os.environ:
        base = list(merged.get("extra_cli_args") or [])
        base.extend(_parse_extra_args(os.environ["AEE_CLAUDE_EXTRA_ARGS"]))
        merged["extra_cli_args"] = base
    return merged


def supported_executors(cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    c = cfg if cfg is not None else load_executor_config()
    lst = c.get("supported_executors") or []
    return [x for x in lst if isinstance(x, str)]


def canonical_executor(
    name: Optional[str],
    cfg: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Normalise an executor name to its canonical wire value.

    Returns the canonical string (e.g. ``claude-code-cli``) for any
    accepted alias, or ``None`` if the name is unknown / unsupported.
    Never silently falls back to a different executor: ``None`` is the
    caller's signal to return a 400 ``unsupported_executor``.
    """
    if name is None or not isinstance(name, str):
        return None
    c = cfg if cfg is not None else load_executor_config()
    aliases = c.get("executor_aliases") or {}
    if isinstance(aliases, dict) and name in aliases:
        return aliases[name]
    if name in supported_executors(c):
        return name
    return None


def is_supported(name: Optional[str], cfg: Optional[Dict[str, Any]] = None) -> bool:
    return canonical_executor(name, cfg) is not None


__all__ = [
    "load_executor_config",
    "supported_executors",
    "canonical_executor",
    "is_supported",
]