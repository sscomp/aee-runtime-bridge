"""Regression test: ``aee.adapters.claude_cli`` module must exist.

Commit ``bc41c2d`` (Commit D) added ``from .claude_cli import
ClaudeCliAdapter`` to ``aee/adapters/__init__.py``. The corresponding
module file ``aee/adapters/claude_cli.py`` was never added to git
history — it only ever existed as an untracked dirty-tree file in
the live working tree. Any clean checkout of the committed tree
therefore fails at package import time with::

    ModuleNotFoundError: No module named 'aee.adapters.claude_cli'

This blocks ``aee.adapters`` itself from importing, which in turn
blocks every consumer of the package (``test_adapter.py``,
``test_dsh_headless_adapter.py``, ``aee.core.registry.bootstrap_defaults``
when ``AEE_BACKEND=claude_cli`` or default-Hermes startup, etc.).

These regression tests cover the two acceptance invariants for the
module-presence fix:

1. The canonical import path ``aee.adapters.claude_cli`` resolves to a
   Python module exposing :class:`ClaudeCliAdapter`.
2. The package-level import ``aee.adapters`` succeeds end-to-end
   (which is the actual failure mode seen in production / CI) and
   exposes :class:`ClaudeCliAdapter` and :class:`DshHeadlessAdapter`.

We also assert the DSH-headless identity is unchanged so the fix
does not regress Commit D's lifecycle / executor-identity contract
(smoke-level: DSH adapter name, runtime_type, and registry entry).
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# 1. Canonical submodule import path
# ---------------------------------------------------------------------------

def test_canonical_submodule_import_resolves():
    """``aee.adapters.claude_cli`` must be importable as a module."""
    mod = importlib.import_module("aee.adapters.claude_cli")
    assert mod is not None
    # The class itself must be exposed at module level (not just inside
    # an inner name).
    assert hasattr(mod, "ClaudeCliAdapter"), (
        "aee.adapters.claude_cli must expose ClaudeCliAdapter at module level"
    )


def test_canonical_submodule_class_identity():
    """The imported symbol is the canonical ClaudeCliAdapter class."""
    from aee.adapters.claude_cli import ClaudeCliAdapter
    # Identity check: importing via the submodule and via the package
    # re-export must resolve to the same class object.
    from aee.adapters import ClaudeCliAdapter as PackageClaudeCliAdapter
    assert ClaudeCliAdapter is PackageClaudeCliAdapter, (
        "package-level ClaudeCliAdapter re-export must be the same class "
        "as aee.adapters.claude_cli.ClaudeCliAdapter"
    )


# ---------------------------------------------------------------------------
# 2. Package-level import works (the actual production failure)
# ---------------------------------------------------------------------------

def test_package_level_import_succeeds():
    """``import aee.adapters`` must not raise at any stage.

    This is the actual production failure mode: a clean checkout of
    the committed tree triggers this failure during ``import aee.adapters``
    because ``__init__.py`` eagerly imports the (missing) submodule.
    """
    # Force a fresh import so we do not depend on test-order caching.
    for name in [n for n in list(sys.modules) if n.startswith("aee.adapters")]:
        sys.modules.pop(name, None)
    pkg = importlib.import_module("aee.adapters")
    assert pkg is not None
    # Both the legacy Claude fallback and the default DSH executor must
    # be reachable from the package surface.
    assert hasattr(pkg, "ClaudeCliAdapter"), (
        "aee.adapters must re-export ClaudeCliAdapter"
    )
    assert hasattr(pkg, "DshHeadlessAdapter"), (
        "aee.adapters must re-export DshHeadlessAdapter (Commit D default)"
    )


def test_adapter_registry_can_register_claude_cli():
    """bootstrap_defaults must be able to register ClaudeCliAdapter.

    This is the smoke-level check that the canonical Claude fallback
    is reachable from the registry bootstrap path used by app.py at
    startup when ``AEE_BACKEND=claude_cli``.
    """
    from aee.core.registry import AdapterRegistry

    reg = AdapterRegistry()
    from aee.adapters.claude_cli import ClaudeCliAdapter
    reg.register(ClaudeCliAdapter(), replace=True)
    assert "claude_cli" in reg.names()
    adapter = reg.get("claude_cli")
    # Identity invariants: the legacy Claude fallback exposes the
    # RuntimeAdapter-protocol attributes the rest of AEE consumes.
    assert adapter.name == "claude_cli"
    assert adapter.runtime_type == "claude_cli"


# ---------------------------------------------------------------------------
# 3. DSH-headless identity is unchanged (Commit D regression guard)
# ---------------------------------------------------------------------------

def test_dsh_headless_identity_unchanged():
    """DSH remains the default executor; claude_cli restore must not change it.

    Commit D set ``DshHeadlessAdapter`` as the default executor and
    defined the executor-identity predicates. The claude_cli module
    restoration must not touch that surface.
    """
    from aee.adapters.dsh_headless import DshHeadlessAdapter

    adapter = DshHeadlessAdapter()
    assert adapter.name == "dsh-headless"
    # ``runtime_type`` uses the underscored form (mirrors the executor
    # identity string and the dispatcher's DB column enum).
    assert adapter.runtime_type == "dsh_headless"
    # RuntimeAdapter protocol surface still wired up.
    for attr in ("submit", "poll", "cancel", "health"):
        assert callable(getattr(adapter, attr)), (
            f"DshHeadlessAdapter missing {attr!r} (would regress Commit D)"
        )


def test_dsh_headless_predicates_still_work():
    """executor_identity predicates are the source of truth for DSH."""
    from dispatcher.executor_identity import (
        is_dsh_executor,
        is_executor_placeholder,
        is_non_hermes_executor,
    )

    assert is_dsh_executor("dsh-headless") is True
    assert is_executor_placeholder("dsh-headless-pending-TASK-X") is True
    assert is_executor_placeholder("claude-cli-pending-TASK-X") is True
    assert is_non_hermes_executor("dsh-headless") is True
    assert is_non_hermes_executor("hermes") is False
