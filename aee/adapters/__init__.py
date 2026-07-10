"""AEE runtime adapters.

The `RuntimeAdapter` Protocol is the single seam between AEE and any
particular agent / worker backend. AEE-1 ships:

    base            — Protocol + shared result dataclasses
    hermes_adapter  — wraps `POST/GET/STOP /v1/runs` against Hermes M2
    fake_adapter    — in-memory adapter for tests

Adding the AEE Lightweight Agent Runtime / Claude Code / MCP is purely additive: implement the
Protocol, register it in `AdapterRegistry`, done.
"""
from __future__ import annotations

from .base import (  # noqa: F401
    RuntimeAdapter,
    RuntimeSubmitResult,
    RuntimePollResult,
    RuntimeCancelResult,
    RuntimeError,
    AdapterNotFoundError,
    UnknownExternalRunError,
)
from .hermes_adapter import HermesAdapter  # noqa: F401
from .fake_adapter import FakeAdapter  # noqa: F401

__all__ = [
    "RuntimeAdapter",
    "RuntimeSubmitResult",
    "RuntimePollResult",
    "RuntimeCancelResult",
    "RuntimeError",
    "AdapterNotFoundError",
    "UnknownExternalRunError",
    "HermesAdapter",
    "FakeAdapter",
]
