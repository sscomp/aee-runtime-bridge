"""Agent Execution Engine (AEE) — runtime-neutral task dispatcher.

This package replaces the Hermes-specific concerns that previously lived
inline in `app.py`. The bridge still exposes the same `/runs` and
`/tasks` surface for backward compat (see `aee.api.compatibility`),
but internally every job now flows through a `RuntimeAdapter`.

Layering:

    aee.core            — job models, state machine, dispatcher, registry
    aee.adapters        — RuntimeAdapter Protocol + concrete adapters
    aee.api             — FastAPI routers (jobs, workers, compatibility)
    aee.security        — policy / safety entrypoints
    aee.storage         — SQLite + future stores

Public re-exports below are intentionally narrow; reach into the
submodules for anything not listed here.
"""
from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
