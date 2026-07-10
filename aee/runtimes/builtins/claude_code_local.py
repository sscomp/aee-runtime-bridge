"""AEE-6.3 built-in Runtime descriptors.

Ship a single descriptor: ``claude-code-local``. It is registered at
app startup via :func:`bootstrap_default_runtimes` if (and only if) the
host has the ``claude`` CLI on ``$PATH``. The runtime is **not enabled
by default** for new task dispatch (the existing AEE Lightweight Agent
Runtime remains the production default); it exists so callers can opt
in by passing ``runtime_requirements: {runtime_type: "claude_code"}`` on
a Job.

Capabilities mirror the AEE-4 worker contract:

* ``runtime.claude_code``  — the canonical capability
* ``task.shell``           — the runtime can run shell commands
* ``task.python``          — the runtime can run Python
* ``task.git``             — the runtime can run git
* ``task.filesystem``      — the runtime can read/write the cwd
* ``task.streaming``       — the runtime emits incremental output (vs.
                              the AEE Lightweight Agent Runtime's
                              request/response shape)

Limits are deliberately conservative for a 2.1.206 ``claude`` subprocess:

* ``max_concurrency=1``  — a single Claude Code session is heavyweight
                            (model + tools + agent loop); a second
                            concurrent run on the same host would
                            contend for the CLI's per-user lockfile.
* ``timeout_seconds=900`` — 15 min cap. The CLI's own --max-turns is 1,
                              so the cap is mostly the model latency.
                              Operators can raise it via
                              ``update_runtime()``.

Health starts at ``unknown`` and is updated by the supervisord loop
based on the last successful ``claude --doctor`` or live run.
"""
from __future__ import annotations

import shutil
from typing import Optional

from ..models import (
    RuntimeCapabilities,
    RuntimeDescriptor,
    RuntimeHealth,
    RuntimeHealthStatus,
    RuntimeLimits,
)


def _binary_available(binary: str) -> bool:
    """Return True iff the named binary resolves on $PATH."""
    return shutil.which(binary) is not None


def build_claude_code_descriptor(
    *,
    default_runtime_id: str = "claude-code-local",
    binary: str = "claude",
    max_concurrency: int = 1,
    timeout_seconds: int = 900,
) -> Optional[RuntimeDescriptor]:
    """Build the built-in ``claude-code-local`` descriptor, or ``None``
    if the CLI is not on $PATH.

    Returning ``None`` (rather than raising) lets the bootstrap routine
    in :mod:`aee.runtimes.registry` quietly skip the descriptor on
    hosts without the binary; the existing AEE Lightweight Agent
    Runtime remains the only registered runtime in that case.
    """
    if not _binary_available(binary):
        return None
    return RuntimeDescriptor(
        runtime_id=default_runtime_id,
        runtime_type="claude_code",
        display_name="Claude Code (local CLI subprocess)",
        version="1.0.0",
        enabled=True,
        endpoint=f"local://{binary}",
        capabilities=RuntimeCapabilities(
            [
                "runtime.claude_code",
                "task.shell",
                "task.python",
                "task.git",
                "task.filesystem",
                "task.streaming",
            ]
        ),
        labels={
            "environment": "local",
            "trust_level": "internal",
            "cli_binary": binary,
        },
        limits=RuntimeLimits(
            max_concurrency=max_concurrency,
            timeout_seconds=timeout_seconds,
        ),
        health=RuntimeHealth(
            status=RuntimeHealthStatus.UNKNOWN,
        ),
    )


__all__ = [
    "build_claude_code_descriptor",
    "_binary_available",
]
