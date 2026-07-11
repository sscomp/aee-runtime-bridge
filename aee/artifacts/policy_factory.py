"""AEE-7.2 — Per-job ``repo_root`` artifact policy factory.

This module closes the AEE-6.4 promise that every job can carry a
``repo_root`` constraint, and that the ``ArtifactPipeline`` will
honour it at ``complete()`` time. The shape is deliberately small:

* :func:`policy_for_repo_root` — turn a string path (or ``None``)
  into the policy the collector should use. The function is
  **fail-safe**: when ``repo_root`` is missing or empty, it returns
  ``None`` so the caller can fall back to whatever default it
  already had (today: ``ArtifactPolicy.permissive()``).
* :func:`repo_root_from_runtime_requirements` — pull the
  ``repo_root`` field out of a ``TaskRuntimeRequirements`` /
  ``JobCreate`` dict, with input-shape tolerance.

Why a separate module
---------------------
``ArtifactPolicy`` lives in ``aee/artifacts/policy.py`` and is the
*evaluation* surface (it has ``check(path)`` etc.). Keeping the
*construction* surface in a dedicated module avoids adding
``repo_root``-specific helpers to the policy class itself, which
would couple policy evaluation with task/job wire-up concerns.

Security model
--------------
When a ``repo_root`` is provided:

1. The factory resolves it via ``os.path.abspath`` so the policy
   comparison is on canonical absolute paths. Relative inputs are
   rejected with ``ValueError`` — a job author must commit to an
   absolute root or omit the field entirely.
2. The resulting policy uses ``allowed_roots=(<absolute_repo_root>,)``.
   This is **more restrictive** than the pipeline default
   (``permissive()`` which allows ``/``); tightening, not loosening.
3. Symlink/broken-symlink/non-regular checks from AEE-6.3 are
   inherited unchanged. ``follow_symlinks=False`` (the policy
   default) is preserved so a symlink that escapes the repo still
   gets a ``SYMLINK_ESCAPE`` violation.
4. ``..`` segments are normalised by ``os.path.normpath`` *before*
   the allow-list check (already policy behaviour); the
   ``traversal_hint`` flag is set when the original contained
   literal ``..`` segments, so the audit log captures attempted
   escapes.

When ``repo_root`` is missing (``None``/empty), the factory returns
``None``. The caller (e.g. ``dispatcher/manager.py``) keeps using
its current default policy — for the bridge manager today that is
``ArtifactPolicy.permissive()`` (AEE-6.3 + AEE-6.2). **No
broadening** of the existing default.

Backward compatibility
----------------------
* Existing AEE-6 / AEE-7.1 tests that build a pipeline without a
  ``repo_root`` continue to work — the factory is a new code path
  they don't touch.
* AEE-7.1's ``traversal_hint`` audit contract is preserved: a path
  inside the repo that contained ``..`` still gets the secondary
  ``code='traversal'`` audit row emitted by
  ``ArtifactPipeline.collect()``.
"""
from __future__ import annotations

import os
from typing import Any, Mapping, Optional

from .policy import ArtifactPolicy


def repo_root_from_runtime_requirements(
    data: Optional[Mapping[str, Any]],
) -> Optional[str]:
    """Extract ``repo_root`` from a runtime-requirements mapping.

    Tolerant to ``None``, empty dict, wrong-typed values, and
    whitespace-only strings. Returns the trimmed string when
    present, otherwise ``None``.

    This is the bridge between the wire-format (``JobCreate``,
    ``runtime_requirements`` JSON) and the policy factory. Keeping
    it here means callers do not have to repeat the null / strip /
    coerce dance.
    """
    if not data or not isinstance(data, dict):
        return None
    value = data.get("repo_root")
    if value is None:
        return None
    if not isinstance(value, str):
        # Coerce ints / PathLike to str; anything else is dropped.
        try:
            value = os.fspath(value)
        except TypeError:
            return None
    value = value.strip()
    return value or None


def policy_for_repo_root(
    repo_root: Optional[str],
    *,
    description_suffix: str = "per_job_repo_root",
) -> Optional[ArtifactPolicy]:
    """Build the per-job ``ArtifactPolicy`` for the given ``repo_root``.

    Returns ``None`` when ``repo_root`` is missing — callers MUST
    treat ``None`` as "no per-job constraint, use your default".

    Raises ``ValueError`` when ``repo_root`` is a relative path
    (a job author must commit to an absolute root).
    """
    if not repo_root:
        return None
    if not isinstance(repo_root, str):
        raise TypeError(
            f"repo_root must be str or None, got {type(repo_root).__name__}"
        )
    # Whitespace-only is treated as missing — same as empty
    # string. The caller almost certainly meant "unset" if they
    # passed a string of spaces.
    stripped = repo_root.strip()
    if not stripped:
        return None
    abs_root = os.path.abspath(stripped)
    # Disallow relative roots. ``os.path.abspath`` does NOT raise
    # on relative input — it just prefixes cwd. We require
    # authors to be explicit.
    if not os.path.isabs(stripped):
        raise ValueError(
            f"repo_root must be an absolute path, got {repo_root!r}"
        )
    return ArtifactPolicy(
        allowed_roots=(abs_root,),
        # The AEE-6.3 symlink handling is preserved by default
        # (follow_symlinks=False; broken symlinks rejected).
        description=(
            f"per_job_repo_root:{description_suffix}:{abs_root}"
        ),
    )


__all__ = [
    "repo_root_from_runtime_requirements",
    "policy_for_repo_root",
]
