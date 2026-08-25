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
#
# P0 bridge (work order §4 + §18): ``dsh-headless`` is added to the
# canonical executor vocabulary and accepted under every documented
# alias (work order uses ``dsh-headless``; we also accept the
# underscore / hyphen variants an operator might pass by accident).
# ``default_executor`` is NOT switched — the production runtime
# stays on ``claude-code-cli`` (or whichever the JSON file says) so
# flipping the default is a deliberate, separate activation step
# (work order §18 / §30). The acceptance of ``dsh-headless`` here
# only widens ``POST /runs/executor``'s selector vocabulary; the
# actual dispatch path is gated by ``adapter_registry.get(...)``
# resolving the name — which still requires
# ``register_dsh_headless()`` to have been called by the
# bootstrap. See ``aee.core.registry.register_dsh_headless`` for
# the explicit-opt-in contract.
_DEFAULTS: Dict[str, Any] = {
    "supported_executors": ["claude-code-cli", "hermes", "dsh-headless"],
    "executor_aliases": {
        "claude-code-cli": "claude-code-cli",
        "claude_code": "claude-code-cli",
        "claude-code": "claude-code-cli",
        "claudecode": "claude-code-cli",
        "dsh-headless": "dsh-headless",
        "dsh_headless": "dsh-headless",
        "dsh": "dsh-headless",
        "deepseek-harness": "dsh-headless",
        "deepseek_harness": "dsh-headless",
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
# to override at deploy time. Lists / dicts are file-only UNLESS we add
# a dedicated parser (see ``AEE_EXECUTOR_REPO_ALLOWLIST`` below).
_ENV_OVERRIDES = {
    "AEE_CLAUDE_CLI_BINARY": "claude_cli_binary",
    "AEE_EXECUTOR_DEFAULT": "default_executor",
    "AEE_EXECUTOR_DEFAULT_TIMEOUT": "default_timeout_sec",
    "AEE_EXECUTOR_MAX_TIMEOUT": "max_timeout_sec",
    "AEE_EXECUTOR_MAX_TURNS": "max_turns",
    "AEE_EXECUTOR_BARE": "bare",
    "AEE_EXECUTOR_OUTPUT_FORMAT": "output_format",
    # TLE-Box / A2 default: comma-separated absolute paths appended to
    # the JSON file's allowlist, so operators don't have to hand-edit
    # ``config/executor.json`` on every host. Empty/whitespace tokens
    # are skipped. Forward slashes only — Windows-style backslashes
    # are NOT normalised (this bridge is Linux-only).
    "AEE_EXECUTOR_REPO_ALLOWLIST": "repo_allowlist",
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
    # ``repo_allowlist`` lives in env as a comma-separated list of
    # absolute paths; we tokenise here so the caller can ``.extend()``
    # the existing JSON allowlist (see ``load_executor_config``).
    if key == "repo_allowlist":
        return [tok.strip() for tok in raw.split(",") if tok.strip()]
    return raw


def _merge_repo_allowlist(base: List[str], override: List[str]) -> List[str]:
    """Combine two path lists, preserving order, dedup'ing case-sensitively.

    The JSON file's entries come first (operator intent on the host that
    controls the file), then the env var's entries (host-level override).
    Order matters because the executor router validates ``repo_path``
    by membership — not position.
    """
    seen: set = set()
    out: List[str] = []
    for src in (base, override):
        if not isinstance(src, list):
            continue
        for p in src:
            if not isinstance(p, str) or not p:
                continue
            if p in seen:
                continue
            seen.add(p)
            out.append(p)
    return out


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
            coerced = _coerce(cfg_key, os.environ[env_key])
            # ``repo_allowlist`` is additive — JSON entries win on
            # tie, env var fills gaps for hosts that don't have the
            # JSON-managed paths (e.g. TLE-Box doesn't have
            # /home/ubuntu/Abacus, so the JSON list is empty for it).
            if cfg_key == "repo_allowlist":
                merged[cfg_key] = _merge_repo_allowlist(
                    list(merged.get(cfg_key) or []),
                    list(coerced or []),
                )
            else:
                merged[cfg_key] = coerced
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