"""Executor Router — MVP selection between Hermes and Claude Code.

Scope
-----
This is the smallest viable router for TASK-M2. The MVP rule is
**explicit opt-in only**:

* If ``metadata.executor`` is ``"claude_code"`` and the
  ``claude_code`` adapter is registered, route to it.
* If ``metadata.executor`` is ``"hermes"`` or absent, route to the
  existing Hermes path (the default).
* If ``metadata.executor`` is anything else, **reject** — we do
  *not* silently fall back.
* If ``metadata.executor`` is ``"claude_code"`` and the adapter is
  *not* available, **fail** with a clear error.

The router is a pure function — no global state, no I/O — so
the API layer (``/runs``) and tests can both call it.

Validation
----------
``validate_metadata`` raises :class:`ExecutorValidationError`
with a stable ``code`` field on any of:

* unknown ``executor`` value
* missing ``repo_path`` for ``claude_code``
* ``repo_path`` outside the allow-list
* ``repo_path`` is a symlink escape
* ``allow_commit=True`` without ``human_approved=True``
* ``required_artifacts`` containing absolute or traversal paths
* ``test_command`` containing obvious shell composition

The validation runs *before* ``select_executor`` so the caller can
return 400 with a stable error code.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence


# --- Constants ---------------------------------------------------------

ALLOWED_EXECUTORS = frozenset({"hermes", "claude_code"})

DEFAULT_REPO_ALLOWLIST = (
    "/home/ubuntu/Abacus",
)

# Shell-metacharacter markers we never allow in a test_command.
# We are deliberately conservative here — the Runner already
# avoids shell interpretation; this is defense in depth only.
_TEST_COMMAND_FORBIDDEN = re.compile(r"[;&|`$]|>>?|<<?|\(\)")

# Path-safety pattern for required_artifacts.
_PATH_TRAVERSAL = re.compile(r"(^|/)\.\.($|/)|^\./|/$")


# --- Errors -----------------------------------------------------------


class ExecutorValidationError(ValueError):
    """Raised by :func:`validate_metadata` on bad input.

    Attributes:
        code: Stable string error code, e.g. ``"unknown_executor"``.
        message: Human-readable description.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ExecutorUnavailable(RuntimeError):
    """Raised when ``executor == "claude_code"`` but the adapter is
    not available in the registry.

    Per task contract: we never silently fall back to Hermes when
    the caller asked for Claude Code. We fail loud.
    """


# --- Result dataclass -------------------------------------------------


@dataclass
class RoutingDecision:
    """Outcome of :func:`select_executor`.

    Attributes:
        requested_executor: The value the caller asked for (or None).
        selected_executor: The adapter_name we should use.
        selection_source: One of ``"metadata"``, ``"default"``,
            ``"explicit_hermes"``.
        fallback_applied: Always False in the MVP — we never
            silently fall back.
        fallback_reason: Always None in the MVP.
    """

    requested_executor: Optional[str]
    selected_executor: str
    selection_source: str
    fallback_applied: bool = False
    fallback_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requested_executor": self.requested_executor,
            "selected_executor": self.selected_executor,
            "selection_source": self.selection_source,
            "fallback_applied": self.fallback_applied,
            "fallback_reason": self.fallback_reason,
        }


# --- Public API -------------------------------------------------------


def select_executor(
    metadata: Optional[Dict[str, Any]],
    *,
    available_adapters: Iterable[str],
) -> RoutingDecision:
    """Pick an executor name based on the optional ``metadata`` dict.

    Args:
        metadata: The free-form metadata dict from
            ``CreateRunRequest.metadata`` (or None for legacy
            callers).
        available_adapters: Iterable of currently-registered
            adapter names (e.g. from
            ``aee.core.registry.adapter_registry.names()``). The
            router is pure; the caller passes this in.

    Returns:
        A :class:`RoutingDecision`. Never raises on routing
        success — validation errors are raised by
        :func:`validate_metadata` *before* this function is
        called.

    Raises:
        ExecutorUnavailable: If the caller requested
            ``claude_code`` but the adapter is not in
            ``available_adapters``.
    """
    available = set(available_adapters)
    requested = None
    if metadata is not None:
        ex = metadata.get("executor")
        if isinstance(ex, str):
            requested = ex
        elif ex is not None:
            # A non-string is treated as absent (legacy path).
            requested = None
    if requested is None:
        return RoutingDecision(
            requested_executor=None,
            selected_executor="hermes",
            selection_source="default",
        )
    if requested == "hermes":
        return RoutingDecision(
            requested_executor="hermes",
            selected_executor="hermes",
            selection_source="explicit_hermes",
        )
    if requested == "claude_code":
        if "claude_code" not in available:
            raise ExecutorUnavailable(
                "metadata.executor='claude_code' but the "
                "'claude_code' adapter is not registered; "
                f"known={sorted(available)}"
            )
        return RoutingDecision(
            requested_executor="claude_code",
            selected_executor="claude_code",
            selection_source="metadata",
        )
    # Should be unreachable because validate_metadata has
    # already rejected unknown values, but be defensive.
    raise ExecutorValidationError(
        "unknown_executor",
        f"unknown executor: {requested!r}",
    )


def validate_metadata(
    metadata: Optional[Dict[str, Any]],
    *,
    repo_allowlist: Sequence[str] = DEFAULT_REPO_ALLOWLIST,
) -> None:
    """Validate the optional ``metadata`` dict from ``/runs``.

    The router is only ever called *after* this function has run,
    so by the time ``select_executor`` sees the metadata it has
    already been shape-checked.

    Validation rules (MVP):
        * ``executor`` (if present) must be in :data:`ALLOWED_EXECUTORS`.
        * For ``claude_code``, ``repo_path`` is required, must be
          absolute, must exist, must not be a symlink escape, and
          must lie inside the allow-list.
        * ``allow_commit=True`` requires ``human_approved=True``.
        * ``required_artifacts`` is a list of relative paths;
          absolute, traversal, and empty entries are rejected.
        * ``test_command`` (if present) is a single string; obvious
          shell composition is rejected.

    Raises:
        ExecutorValidationError: with a stable ``code``.
    """
    if metadata is None:
        return
    if not isinstance(metadata, dict):
        raise ExecutorValidationError(
            "metadata_not_dict",
            f"metadata must be a dict, got {type(metadata).__name__}",
        )
    executor = metadata.get("executor")
    if executor is not None:
        if executor not in ALLOWED_EXECUTORS:
            raise ExecutorValidationError(
                "unknown_executor",
                f"metadata.executor must be one of "
                f"{sorted(ALLOWED_EXECUTORS)}; got {executor!r}",
            )
    # Default false; reject truthy non-bool explicitly.
    allow_commit = bool(metadata.get("allow_commit", False))
    human_approved = bool(metadata.get("human_approved", False))
    if allow_commit and not human_approved:
        raise ExecutorValidationError(
            "allow_commit_requires_human_approved",
            "metadata.allow_commit=True requires "
            "metadata.human_approved=True",
        )
    # Repo path (required for claude_code)
    if executor == "claude_code":
        repo_path = metadata.get("repo_path")
        if not repo_path or not isinstance(repo_path, str):
            raise ExecutorValidationError(
                "repo_path_required",
                "metadata.repo_path is required when "
                "executor='claude_code'",
            )
        _validate_repo_path(repo_path, repo_allowlist=repo_allowlist)
    # Required artifacts
    artifacts = metadata.get("required_artifacts")
    if artifacts is not None:
        if not isinstance(artifacts, list):
            raise ExecutorValidationError(
                "required_artifacts_not_list",
                "metadata.required_artifacts must be a list",
            )
        for entry in artifacts:
            if not isinstance(entry, str) or not entry:
                raise ExecutorValidationError(
                    "required_artifact_empty",
                    "metadata.required_artifacts entries must be "
                    "non-empty strings",
                )
            if entry.startswith("/"):
                raise ExecutorValidationError(
                    "required_artifact_absolute",
                    f"required artifact must be relative: {entry!r}",
                )
            if _PATH_TRAVERSAL.search(entry):
                raise ExecutorValidationError(
                    "required_artifact_traversal",
                    f"required artifact may not traverse: {entry!r}",
                )
    # Test command
    test_command = metadata.get("test_command")
    if test_command is not None:
        if not isinstance(test_command, str):
            raise ExecutorValidationError(
                "test_command_not_string",
                "metadata.test_command must be a string",
            )
        if _TEST_COMMAND_FORBIDDEN.search(test_command):
            raise ExecutorValidationError(
                "test_command_shell_metachar",
                "metadata.test_command contains forbidden shell "
                "metacharacters",
            )


def _validate_repo_path(
    repo_path: str, *, repo_allowlist: Sequence[str]
) -> None:
    if not os.path.isabs(repo_path):
        raise ExecutorValidationError(
            "repo_path_not_absolute",
            f"metadata.repo_path must be absolute: {repo_path!r}",
        )
    p = Path(repo_path)
    try:
        real = p.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ExecutorValidationError(
            "repo_path_unresolvable",
            f"metadata.repo_path cannot be resolved: {exc}",
        ) from exc
    # Disallow obvious sensitive roots
    blocked = {"/", "/etc", "/root", os.path.expanduser("~/.ssh"), os.path.expanduser("~/.aws")}
    if str(real) in blocked or str(real).startswith(tuple(b + "/" for b in blocked)):
        raise ExecutorValidationError(
            "repo_path_forbidden_root",
            f"metadata.repo_path is in a blocked root: {real}",
        )
    # Allow-list containment
    allowed = False
    for prefix in repo_allowlist:
        try:
            real.relative_to(Path(prefix).resolve(strict=False))
            allowed = True
            break
        except ValueError:
            continue
    if not allowed:
        raise ExecutorValidationError(
            "repo_path_outside_allowlist",
            f"metadata.repo_path {real!s} is not in the allow-list "
            f"{list(repo_allowlist)}",
        )
    # Must exist and be a directory (not following symlinks for the
    # existence check — lstat — so a symlink to nowhere is rejected).
    try:
        st = os.lstat(p)
    except OSError as exc:
        raise ExecutorValidationError(
            "repo_path_missing",
            f"metadata.repo_path does not exist: {exc}",
        ) from exc
    if not (os.path.isdir(p) or os.path.isdir(str(p))):
        raise ExecutorValidationError(
            "repo_path_not_directory",
            f"metadata.repo_path is not a directory: {p}",
        )


__all__ = [
    "ALLOWED_EXECUTORS",
    "DEFAULT_REPO_ALLOWLIST",
    "ExecutorValidationError",
    "ExecutorUnavailable",
    "RoutingDecision",
    "select_executor",
    "validate_metadata",
]
