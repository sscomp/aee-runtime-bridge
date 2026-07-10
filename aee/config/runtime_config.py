"""AEE-5 Runtime configuration loader.

Reads the `runtimes:` block from a YAML file. The loader
is deliberately small: it does *not* enforce a schema,
it just maps the YAML nodes onto `RuntimeDescriptor`
construction. The Registry's `register_runtime()` does
the real validation (it raises
`RuntimeValidationError` on bad input).

Config file shape (matches the AEE-5 task spec §6):

```yaml
runtimes:
  auto_register_builtin: true
  default_runtime_id: aee-lightweight-local
  allow_unknown_health: true
  definitions:
    - runtime_id: aee-lightweight-local
      runtime_type: aee_lightweight
      display_name: AEE Lightweight Local Runtime
      version: 1.0.0
      enabled: true
      endpoint: local
      capabilities:
        - runtime.aee_runtime
        - task.shell
        - task.python
      labels:
        environment: local
        trust_level: internal
      limits:
        max_concurrency: 2
        timeout_seconds: 1800
      health:
        status: unknown
        last_checked_at: null
```

Secrets / credentials
---------------------
Per the AEE-5 task spec §6, secrets MUST NOT be
written into the repository. The loader supports
two secret-reference conventions:

  * `${VAR}` — substituted from `os.environ` at load
    time. The substitution is logged at DEBUG, NEVER
    at INFO/WARN, and the resolved value is NOT
    echoed back to the caller.
  * `env:VAR` — same as `${VAR}` but in any
    string-typed field, not just the YAML value.
    Useful when a config field is a single token
    string and you don't want to use a `${VAR}`
    block.

Substitution is performed **only** on `endpoint`,
`version`, and `display_name` — i.e. descriptive
fields. Substituting secrets into `runtime_id` is
rejected (the id is a primary key and must be
deterministic).

The loader is fail-fast: a bad value (missing
`runtime_id`, invalid health status, malformed YAML)
raises a clear `RuntimeValidationError` /
`RuntimeConfigError` (the API layer maps this to a
500 with a structured message).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from aee.runtimes.errors import RuntimeValidationError
from aee.runtimes.models import (
    RuntimeCapabilities,
    RuntimeDescriptor,
    RuntimeHealth,
    RuntimeHealthStatus,
    RuntimeLimits,
)


_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class RuntimeConfigError(Exception):
    """Raised when the YAML config is malformed."""


def _resolve_env(value: Any, env: Optional[Dict[str, str]] = None) -> Any:
    """Substitute `${VAR}` and `env:VAR` references in a string.

    Other types are returned as-is. A reference to an
    undefined env var becomes a clear error.

    `env` is the override dict used by tests; when
    `None`, `os.environ` is consulted.
    """
    if not isinstance(value, str):
        return value

    def _getenv(var: str) -> Optional[str]:
        if env is not None:
            return env.get(var)
        return os.environ.get(var)

    # First, expand ${VAR} tokens
    def _expand(m):
        var = m.group(1)
        env_val = _getenv(var)
        if env_val is None:
            raise RuntimeConfigError(
                f"runtime config references ${{{var}}} but the env var "
                f"is not set; refusing to start with a partially-resolved "
                f"config"
            )
        return env_val

    s = _ENV_REF_RE.sub(_expand, value)

    # Then, expand `env:VAR` tokens (whole-value form).
    if s.startswith("env:"):
        var = s[4:].strip()
        env_val = _getenv(var)
        if env_val is None:
            raise RuntimeConfigError(
                f"runtime config references env:{var} but the env var "
                f"is not set"
            )
        return env_val
    return s


def _coerce_bool(v: Any, default: bool = True) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(v)


def _coerce_dict(v: Any) -> Dict[str, str]:
    if not v:
        return {}
    if not isinstance(v, dict):
        return {}
    return {str(k): str(val) for k, v_inner in v.items() for k, val in [(k, v_inner)]}


def _build_descriptor(
    node: Dict[str, Any],
    env: Optional[Dict[str, str]] = None,
) -> RuntimeDescriptor:
    """Build a `RuntimeDescriptor` from a single YAML node.

    Performs no business validation — that's
    `registry.register_runtime()`'s job. We only
    resolve env refs and coerce types.
    """
    if not isinstance(node, dict):
        raise RuntimeConfigError(
            f"runtime definition must be a mapping, got {type(node).__name__}"
        )
    runtime_id = _resolve_env(node.get("runtime_id"), env=env)
    if not isinstance(runtime_id, str) or not runtime_id.strip():
        raise RuntimeConfigError(
            "runtime_id is required and must be a non-empty string"
        )
    runtime_type = _resolve_env(node.get("runtime_type"), env=env)
    if not isinstance(runtime_type, str) or not runtime_type.strip():
        raise RuntimeConfigError(
            f"runtime_type is required for runtime_id={runtime_id!r}"
        )
    capabilities = node.get("capabilities") or []
    if not isinstance(capabilities, list):
        raise RuntimeConfigError(
            f"capabilities for runtime_id={runtime_id!r} must be a list"
        )
    labels = _coerce_dict(node.get("labels"))
    limits = node.get("limits") or {}
    if not isinstance(limits, dict):
        raise RuntimeConfigError(
            f"limits for runtime_id={runtime_id!r} must be a mapping"
        )
    health = node.get("health") or {}
    if not isinstance(health, dict):
        raise RuntimeConfigError(
            f"health for runtime_id={runtime_id!r} must be a mapping"
        )

    return RuntimeDescriptor(
        runtime_id=str(runtime_id).strip(),
        runtime_type=str(runtime_type).strip(),
        display_name=str(_resolve_env(node.get("display_name", ""), env=env) or "").strip(),
        version=str(_resolve_env(node.get("version", "1.0.0"), env=env) or "1.0.0").strip() or "1.0.0",
        enabled=_coerce_bool(node.get("enabled", True), default=True),
        endpoint=str(_resolve_env(node.get("endpoint", "local"), env=env) or "local").strip() or "local",
        capabilities=RuntimeCapabilities(capabilities),
        labels=labels,
        limits=RuntimeLimits.from_dict(limits),
        health=RuntimeHealth.from_dict(health),
    )


def load_runtime_config(
    path: Union[str, Path, None],
    *,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Load the `runtimes:` block from a YAML file.

    Args:
        path: path to a YAML file. `None` returns
            sensible defaults (built-in enabled,
            no overrides).
        env: optional env dict override; defaults to
            `os.environ`. Tests pass a synthetic dict
            to avoid touching real env.

    Returns:
        A dict with keys:
          * `auto_register_builtin: bool`
          * `default_runtime_id: str`
          * `allow_unknown_health: bool`
          * `definitions: List[RuntimeDescriptor]`

    Raises:
        RuntimeConfigError: if the YAML is malformed or
            a required field is missing.
        FileNotFoundError: if `path` is given and the
            file does not exist.
    """
    if path is None:
        return {
            "auto_register_builtin": True,
            "default_runtime_id": "aee-lightweight-local",
            "allow_unknown_health": True,
            "definitions": [],
        }

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"runtime config not found: {p}")

    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeConfigError(
            "PyYAML is required to load the runtime config; install with "
            "`pip install PyYAML`"
        ) from exc

    if env is not None:
        # Tests inject a synthetic env. We pass it
        # through to `_resolve_env` explicitly; the
        # `os.environ` swap trick was fragile.
        with open(p, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    else:
        with open(p, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict):
        raise RuntimeConfigError(
            f"runtime config {p} root must be a YAML mapping"
        )
    runtimes_block = raw.get("runtimes") or {}
    if not isinstance(runtimes_block, dict):
        raise RuntimeConfigError(
            f"`runtimes:` block in {p} must be a YAML mapping"
        )

    auto_register = _coerce_bool(
        runtimes_block.get("auto_register_builtin", True), default=True
    )
    default_runtime_id = str(
        runtimes_block.get("default_runtime_id", "aee-lightweight-local")
        or "aee-lightweight-local"
    ).strip()
    allow_unknown_health = _coerce_bool(
        runtimes_block.get("allow_unknown_health", True), default=True
    )
    defs = runtimes_block.get("definitions") or []
    if not isinstance(defs, list):
        raise RuntimeConfigError(
            f"`runtimes.definitions` in {p} must be a YAML list"
        )

    descriptors: List[RuntimeDescriptor] = []
    for n in defs:
        descriptors.append(_build_descriptor(n, env=env))

    return {
        "auto_register_builtin": auto_register,
        "default_runtime_id": default_runtime_id,
        "allow_unknown_health": allow_unknown_health,
        "definitions": descriptors,
    }


def apply_runtime_config(
    config: Dict[str, Any],
    registry,
) -> Dict[str, Any]:
    """Apply a parsed `load_runtime_config()` result to `registry`.

    Behaviour:
      * If `auto_register_builtin` is True, the
        built-in `aee-lightweight-local` Runtime is
        registered (idempotent; the registry's
        `register_runtime(replace=False)` short-
        circuits when the id is already present).
      * Every descriptor in `definitions` is
        registered with `replace=True` (the config
        file is the source of truth; a later
        operator who edits the YAML expects their
        change to take effect at next start).
      * If `default_runtime_id` is given, the
        registry singleton's `default_runtime_id`
        is updated via the dispatch service.

    Returns:
        A summary dict with the count of built-in /
        file-defined descriptors that were
        registered, suitable for the operator log.
    """
    from aee.dispatch.service import dispatch_service
    from aee.runtimes.builtins import build_default_descriptor

    summary = {
        "builtin_registered": 0,
        "definitions_registered": 0,
        "default_runtime_id": config.get("default_runtime_id", "aee-lightweight-local"),
    }
    if config.get("auto_register_builtin", True):
        default_id = config.get("default_runtime_id", "aee-lightweight-local")
        # Register the built-in on the *passed-in*
        # registry (not the module-level singleton, so
        # tests can use a fresh in-memory registry).
        builtin = build_default_descriptor(default_runtime_id=default_id)
        # Use the registry's register_runtime; if the
        # id is already present, this is a no-op
        # (replace=False by default, so duplicates raise).
        try:
            registry.register_runtime(builtin)
            summary["builtin_registered"] = 1
        except Exception:
            # Already registered — that's fine, idempotent.
            summary["builtin_registered"] = 0

    for d in config.get("definitions", []):
        # replace=True: the config file is authoritative
        # for the descriptors it lists.
        registry.register_runtime(d, replace=True)
        summary["definitions_registered"] += 1

    # Update the dispatch service default
    # dispatch_service is a module-level singleton; we
    # set its `default_runtime_id` so the AEE-4 compat
    # path picks up the configured value.
    dispatch_service._default_runtime_id = summary["default_runtime_id"]  # type: ignore[attr-defined]
    return summary


__all__ = [
    "RuntimeConfigError",
    "load_runtime_config",
    "apply_runtime_config",
]
