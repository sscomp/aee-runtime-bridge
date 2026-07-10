"""AEE-5 built-in Runtime descriptors.

The only Runtime AEE-5 ships is `aee-lightweight-local`:
the AEE-4 conformant AEE Lightweight Agent Runtime. Its
capabilities match the worker contract the
`aee-runtime/aee_runtime.py` daemon self-reports
(see `aee-runtime/config.example.yaml`):

  * `runtime.aee_runtime`        — the canonical capability
    the AEE-4 contract uses to identify a Runtime of this
    type (the bridge's `/workers/register` accepts this
    and the AEE-5 Runtime Descriptor mirrors it).
  * `task.shell`                 — the runtime can execute
    shell commands (allowlisted in the daemon config).
  * `task.python`                — the runtime can execute
    Python (the daemon ships `python3` in its
    allowlist).
  * `task.git`                   — the runtime can run git
    operations.
  * `task.filesystem`            — the runtime can read /
    write files in the configured workdir allowlist.

Future Runtimes (Claude Code, Shell, HTTP, Container)
will register their own descriptors at runtime; the AEE-5
Registry makes that a `POST /v1/runtimes` away.

This module is intentionally tiny — the built-in
descriptor is just data. The selector / health / API
layers consume it without knowing where the data came
from.
"""
from __future__ import annotations

from ..models import (
    RuntimeCapabilities,
    RuntimeDescriptor,
    RuntimeHealth,
    RuntimeHealthStatus,
    RuntimeLimits,
)


def build_default_descriptor(
    *,
    default_runtime_id: str = "aee-lightweight-local",
) -> RuntimeDescriptor:
    """Build the canonical built-in Runtime descriptor.

    Parameterized only on `default_runtime_id` so the
    config layer (AEE-5 §6) can override the id while
    keeping everything else stable.
    """
    return RuntimeDescriptor(
        runtime_id=default_runtime_id,
        runtime_type="aee_lightweight",
        display_name="AEE Lightweight Local Runtime",
        version="1.0.0",
        enabled=True,
        endpoint="local",
        capabilities=RuntimeCapabilities(
            [
                "runtime.aee_runtime",
                "task.shell",
                "task.python",
                "task.git",
                "task.filesystem",
            ]
        ),
        labels={
            "environment": "local",
            "trust_level": "internal",
        },
        limits=RuntimeLimits(
            max_concurrency=2,
            timeout_seconds=1800,
        ),
        health=RuntimeHealth(
            status=RuntimeHealthStatus.UNKNOWN,
        ),
    )


__all__ = ["build_default_descriptor"]
