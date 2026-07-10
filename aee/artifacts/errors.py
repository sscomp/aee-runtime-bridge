"""AEE-6 Artifact error hierarchy.

A typed exception tree so callers (orchestrator, future API
handlers) can switch on a specific class without parsing
strings. The tree mirrors the patterns used in
`aee/runtimes/errors.py` (RuntimeError → RuntimeNotFoundError,
etc.).
"""
from __future__ import annotations

from typing import Optional


class ArtifactError(Exception):
    """Base class for all AEE Artifact Pipeline errors.

    Every subclass carries a `code` so the orchestrator can
    switch on a stable identifier without parsing the message.
    """

    code: str = "artifact_error"

    def __init__(self, message: str, *, path: Optional[str] = None) -> None:
        if path:
            full = f"{message} (path={path!r})"
        else:
            full = message
        super().__init__(full)
        self.path = path


class ArtifactNotFoundError(ArtifactError):
    """The path doesn't exist on disk."""

    code = "artifact_not_found"


class ArtifactAccessError(ArtifactError):
    """Permission denied, or any other OS-level access failure."""

    code = "artifact_access_denied"


class ArtifactTooLargeError(ArtifactError):
    """The artifact exceeds MAX_HASH_BYTES (caller cannot get a hash)."""

    code = "artifact_too_large"


__all__ = [
    "ArtifactError",
    "ArtifactNotFoundError",
    "ArtifactAccessError",
    "ArtifactTooLargeError",
]
