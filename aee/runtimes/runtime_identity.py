"""Runtime identity metadata for the ``POST /runs/executor`` envelope.

Pure, read-only collectors — no git mutation, no network, no executor
launch, no dispatch. Every field is either a real, discovered value or
``None`` / ``"unknown"`` when its source is unavailable. **Never
fabricated.**

The collected dict is attached to the response envelope as
``runtime_identity`` so a caller (GPT, operator, reviewer) can see
exactly which provider / executor / bridge produced a run, with the
git evidence of the bridge itself.

Fields (work-order Part B):

* ``provider``            — display name of the selected executor's
                           provider (``"Claude Code"`` / ``"Hermes"``).
* ``provider_version``    — the provider's own version string, when
                           discoverable.
* ``executor_binary``     — absolute path of the executor binary, when
                           applicable (claude-code-cli only).
* ``executor_version``    — version string reported by the executor
                           binary (``<binary> --version``).
* ``runtime_bridge_version`` — bridge software version (a shipped
                           constant; ``"unknown"`` until stamped).
* ``bridge_commit``       — HEAD sha of the bridge git worktree.
* ``bridge_branch``       — current branch of the bridge worktree.
* ``bridge_repository``   — remote origin URL if configured, else the
                           local bridge repo path.
* ``generated_at_utc``    — ISO-8601 UTC timestamp of collection.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# The bridge ships no version stamp today; we expose the constant here so
# a future release can set it in one place. "unknown" is factual — it is
# never a fabricated version number.
RUNTIME_BRIDGE_VERSION = "unknown"

# Selected executor wire value -> provider display name. Anything not
# listed maps to "unknown" rather than a guess.
_PROVIDER_NAME: Dict[str, str] = {
    "claude-code-cli": "Claude Code",
    "hermes": "Hermes",
}

# The bridge repo root is two parents above this file
# (aee/runtimes/runtime_identity.py -> aee/runtimes/ -> aee/ -> repo root).
_BRIDGE_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


def _git(args: list, *, cwd: str) -> Optional[str]:
    """Run a read-only git command in ``cwd``; return stripped stdout or None."""
    try:
        res = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    out = (res.stdout or "").strip()
    return out or None


def _executor_version(binary: Optional[str]) -> Optional[str]:
    """Ask the executor binary for its version; return the string or None.

    ``--version`` is the universal, non-mutating flag. We accept the
    version from stdout or stderr (some CLIs print it to stderr) and
    return it verbatim, trimmed. Never raises.
    """
    if not binary or not isinstance(binary, str):
        return None
    if not os.path.exists(binary):
        return None
    try:
        res = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = (res.stdout or "").strip()
    if not out:
        out = (res.stderr or "").strip()
    return out or None


def collect_runtime_identity(
    *,
    selected_executor: Optional[str],
    cfg: Optional[Dict[str, Any]] = None,
    bridge_repo: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the ``runtime_identity`` dict for the response envelope.

    All parameters are keyword-only. ``cfg`` is the merged executor
    config (used only for the claude-code-cli binary path).
    ``bridge_repo`` defaults to the discovered bridge repo root.

    Returns a dict with every Part B field present; unavailable values
    are ``None`` (or ``"unknown"`` for the provider / bridge version).
    """
    cfg = cfg if cfg is not None else {}
    repo = bridge_repo or _BRIDGE_ROOT

    provider = _PROVIDER_NAME.get(selected_executor or "", "unknown")

    executor_binary: Optional[str] = None
    executor_version: Optional[str] = None
    provider_version: Optional[str] = None
    if selected_executor == "claude-code-cli":
        binary = cfg.get("claude_cli_binary")
        if isinstance(binary, str) and binary:
            executor_binary = binary
        # The Claude Code CLI is both the provider and the executor, so
        # its --version is reported in both version fields. For hermes
        # there is no local binary, so both stay None (factual).
        executor_version = _executor_version(executor_binary)
        provider_version = executor_version

    bridge_commit = _git(["rev-parse", "HEAD"], cwd=repo)
    bridge_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    bridge_remote = _git(["config", "--get", "remote.origin.url"], cwd=repo)
    # Factual: prefer the configured remote URL; fall back to the local
    # repo path when no remote is configured (this bridge has none).
    bridge_repository = bridge_remote or repo

    return {
        "provider": provider,
        "provider_version": provider_version,
        "executor_binary": executor_binary,
        "executor_version": executor_version,
        "runtime_bridge_version": RUNTIME_BRIDGE_VERSION,
        "bridge_commit": bridge_commit,
        "bridge_branch": bridge_branch,
        "bridge_repository": bridge_repository,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


__all__ = [
    "RUNTIME_BRIDGE_VERSION",
    "collect_runtime_identity",
]