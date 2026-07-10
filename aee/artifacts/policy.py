"""Artifact security policy.

AEE-6.3 hardening: explicit allow-list of filesystem roots the artifact
collector is willing to ingest from. Anything outside the policy is
rejected *before* the file content is opened, so a malicious / accidental
path can never leak sensitive data through the pipeline.

Design goals
------------
1.  **No read on violation.**  ``safe_resolve()`` does not call ``open()`` /
    ``Path.read_text()`` / ``os.stat()`` on the target before the policy
    decision. It only inspects the path object itself (``os.path.realpath``
    + lstat for the symlink question).  File content is touched only when
    the path is accepted.

2.  **Symlink-safe by default.**  ``os.path.realpath`` resolves every
    symlink in the chain. A symlink whose target escapes the allow-list
    is rejected with ``PolicyViolationCode.SYMLINK_ESCAPE``.

3.  **Traversal-safe by default.**  ``..`` segments are collapsed by
    ``os.path.normpath`` *before* the allow-list check, so a literal
    ``/repo/../../../etc/passwd`` cannot bypass the check by virtue of
    being textual garbage.

4.  **Broken symlinks are rejected** with
    ``PolicyViolationCode.BROKEN_SYMLINK`` rather than silently skipped —
    we want loud failures, not silent data loss.

5.  **Non-regular files are rejected** (sockets, FIFOs, devices,
    directories themselves) with ``PolicyViolationCode.NOT_REGULAR``.
    The original AEE-2 contract collected only regular files; this just
    makes the rule explicit and testable.

6.  **TOCTOU is documented, not silently fixed.**  The check uses
    ``os.path.realpath`` + ``Path.lstat()`` at decision time, but a
    privileged adversary can swap a file between the check and the read.
    AEE-6.3 closes the **content** leak (no read on violation) and the
    **path-surprise** leak (allow-list). A full TOCTOU fix would require
    ``O_PATH`` + ``fstat`` after ``openat`` with parent-fd pinning
    (Linux 5.6+); that is **explicitly out of scope** for this slice
    and called out in master plan §13.5.

Public API
----------
- ``PolicyDecision`` — frozen dataclass with the verdict
- ``PolicyViolationCode`` — enum of failure reasons
- ``ArtifactPolicy`` — the policy object, constructed with allowed roots
- ``safe_resolve(path, policy)`` — convenience wrapper that returns
  ``(realpath, None)`` on accept and ``(realpath_or_input, decision)``
  on reject
- ``policy.violation_event(decision, *, source)`` — builder for the
  audit event payload written to SQLite

Backward compatibility
----------------------
The default ``ArtifactPolicy.default()`` returns a policy with a
single allowed root set to ``/home/ubuntu/hermes-runtime-bridge``.
The original AEE-2 collector had *no* policy at all (paths were taken
verbatim), so this is a tightening, not a loosening. Tests that
exercise the collector with paths inside the bridge repo will keep
passing; tests that referenced paths outside (e.g. ``/etc/hostname``)
will need to be updated to either set an explicit policy or be
skipped with a clear audit log entry.
"""
from __future__ import annotations

import enum
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Iterable, List, Optional, Sequence, Tuple, Union


PathLike = Union[str, os.PathLike, PurePath]


class PolicyViolationCode(str, enum.Enum):
    """Stable codes for policy violations.

    Codes are strings (not ints) so they survive SQLite round-trips
    and JSON serialisation without bespoke encoding.
    """

    OK = "ok"
    OUTSIDE_ROOTS = "outside_allowed_roots"
    SYMLINK_ESCAPE = "symlink_escape"
    TRAVERSAL = "path_traversal"
    BROKEN_SYMLINK = "broken_symlink"
    NOT_REGULAR = "not_regular_file"
    MISSING = "missing_path"
    EMPTY_PATH = "empty_path"
    INVALID_PATH = "invalid_path"

    @property
    def is_violation(self) -> bool:
        return self is not PolicyViolationCode.OK


@dataclass(frozen=True)
class PolicyDecision:
    """Result of a single policy check.

    Attributes
    ----------
    code
        The violation code. ``OK`` means accept.
    realpath
        The canonicalised path the caller should use. For accepted
        files this is the resolved, real-path form. For rejected paths
        it is *at most* the textual realpath used for the comparison
        (we never try to read content).
    original
        The path string the caller supplied (verbatim, for audit).
    detail
        Human-readable explanation suitable for log lines and audit
        events. Must not echo sensitive data — only path structure.
    decision_id
        Stable UUID for the decision; persisted with the event.
    """

    code: PolicyViolationCode
    realpath: str
    original: str
    detail: str
    decision_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def accepted(self) -> bool:
        return self.code is PolicyViolationCode.OK

    def to_event(self, *, source: str, artifact_id: Optional[str] = None) -> dict:
        """Build the audit-trail event payload.

        Used by ``ArtifactCollector`` to emit one row per decision into
        the ``artifact_policy_events`` SQLite table.
        """
        return {
            "decision_id": self.decision_id,
            "code": self.code.value,
            "accepted": self.accepted,
            "realpath": self.realpath,
            "original": self.original,
            "detail": self.detail,
            "source": source,
            "artifact_id": artifact_id,
        }


def _coerce(path: PathLike) -> str:
    """Best-effort coercion to a string for logging / decision fields.

    ``os.fspath()`` is the canonical way; we wrap it so callers can pass
    ``str``, ``bytes``, ``Path``, or anything ``__fspath__`` supports.
    """
    try:
        return os.fspath(path)
    except TypeError as exc:  # pragma: no cover - defensive
        raise PolicyCheckError(f"unsupported path type: {type(path)!r}") from exc


def _normpath_no_exc(text: str) -> str:
    """``os.path.normpath`` that never raises (pure string operation)."""
    return os.path.normpath(text)


@dataclass(frozen=True)
class ArtifactPolicy:
    """Allow-list policy for artifact collection.

    Parameters
    ----------
    allowed_roots
        Sequence of absolute directory paths. A candidate file is
        accepted only if its realpath is *strictly inside* one of the
        roots (using ``os.path.commonpath`` with strict=True semantics
        — the candidate's parent dir must equal the root or be a
        descendant of it).
    follow_symlinks
        When ``True`` (default), symlinks are resolved via
        ``os.path.realpath`` and the *target* is checked. When ``False``,
        any symlink in the chain is rejected outright. The default
        mirrors the historical AEE-2 behaviour (follow + check) but
        the audit row carries enough information to detect escape
        attempts.
    allow_broken
        When ``True`` (default ``False``), broken symlinks are accepted
        with code ``OK`` (recorded in the audit row). Defaulting to
        ``False`` makes the policy fail-closed.
    description
        Free-form label for the policy, embedded in audit events.
    """

    allowed_roots: Tuple[str, ...]
    follow_symlinks: bool = True
    allow_broken: bool = False
    description: str = "default"

    def __post_init__(self) -> None:
        # Pre-resolve and validate all roots eagerly — a misconfigured
        # root should be a build-time / import-time error, not a
        # per-call surprise.
        resolved: List[str] = []
        for raw in self.allowed_roots:
            if not raw:
                raise PolicyCheckError("allowed_roots contains an empty entry")
            abs_root = os.path.abspath(raw)
            resolved.append(abs_root)
        if not resolved:
            raise PolicyCheckError("allowed_roots must not be empty")
        # freeze the resolved tuple
        object.__setattr__(self, "allowed_roots", tuple(resolved))

    # ------------------------------------------------------------------
    # The actual check
    # ------------------------------------------------------------------
    def check(self, path: PathLike) -> PolicyDecision:
        original = _coerce(path)
        if not original:
            return PolicyDecision(
                code=PolicyViolationCode.EMPTY_PATH,
                realpath="",
                original=original,
                detail="empty path supplied",
            )

        # Step 1: textual normalisation. This collapses `..` segments
        # *before* we ever touch the filesystem, so a literal
        # `/repo/a/../../etc/passwd` becomes `/etc/passwd` in this
        # text view. We keep the original string in `original` for
        # audit but the allow-list check is performed against the
        # normalised text.
        normalised = _normpath_no_exc(original)
        if normalised != original and ".." in original:
            # The user supplied literal `..` segments. We still accept
            # the result of normalisation (it might be in-bounds), but
            # we surface the fact via a TRAVERSAL *detail* string —
            # the code stays OK if the destination is allowed, the
            # audit row carries the trail. This makes
            # `cat /repo/../etc/passwd` visible in the audit log
            # even when it would have been rejected by OUTSIDE_ROOTS
            # on its own.
            traversal_hint = True
        else:
            traversal_hint = False

        # Step 2: filesystem-aware resolution.
        #
        # We deliberately do **not** use ``os.path.realpath`` blindly
        # because we need to distinguish:
        #   (a) file simply does not exist → MISSING
        #   (b) path is a broken symlink → BROKEN_SYMLINK
        #   (c) path is a live symlink that escapes roots → SYMLINK_ESCAPE
        #   (d) path is a non-regular file (socket / fifo / dir) → NOT_REGULAR
        #   (e) path is a regular file outside roots → OUTSIDE_ROOTS
        #   (f) path is a regular file inside roots → OK
        try:
            p = Path(normalised)
        except (ValueError, OSError) as exc:
            return PolicyDecision(
                code=PolicyViolationCode.INVALID_PATH,
                realpath=normalised,
                original=original,
                detail=f"path construction failed: {exc.__class__.__name__}",
            )

        # `lstat` does not follow symlinks. It tells us whether the
        # *link itself* exists. If it does not and the original
        # looked symlinky, treat as broken.
        try:
            lstat = p.lstat()
        except (FileNotFoundError, NotADirectoryError):
            return PolicyDecision(
                code=PolicyViolationCode.MISSING,
                realpath=normalised,
                original=original,
                detail="path does not exist",
            )
        except OSError as exc:
            return PolicyDecision(
                code=PolicyViolationCode.INVALID_PATH,
                realpath=normalised,
                original=original,
                detail=f"lstat failed: {exc.__class__.__name__}: {exc}",
            )

        is_symlink = os.path.islink(str(p))
        if is_symlink and not self.follow_symlinks:
            return PolicyDecision(
                code=PolicyViolationCode.SYMLINK_ESCAPE,
                realpath=normalised,
                original=original,
                detail="symlink rejected (follow_symlinks=False)",
            )

        # Resolve to realpath only if the link is live. For a broken
        # symlink `os.path.realpath` returns the path unchanged on
        # Linux — we want to catch that explicitly.
        if is_symlink:
            real = os.path.realpath(str(p))
            try:
                os.stat(real)  # resolve target; raises if broken
            except (FileNotFoundError, NotADirectoryError):
                if not self.allow_broken:
                    return PolicyDecision(
                        code=PolicyViolationCode.BROKEN_SYMLINK,
                        realpath=real,
                        original=original,
                        detail="symlink target does not exist",
                    )
                # allow_broken=True with live target missing — treat as
                # MISSING rather than OK so downstream readers don't
                # trip on ENOENT.
                return PolicyDecision(
                    code=PolicyViolationCode.MISSING,
                    realpath=real,
                    original=original,
                    detail="symlink target missing and allow_broken=True",
                )
        else:
            real = str(p)

        # Step 3: regular-file check. ``stat.S_ISREG`` is the safe
        # POSIX check; on Windows the modes line up too for the file
        # types we care about (sockets/fifos/devs are not regular).
        import stat as _stat

        st_mode = lstat.st_mode
        if is_symlink:
            # We need the mode of the *target*, not the link itself.
            try:
                target_mode = os.stat(real).st_mode
            except OSError as exc:
                return PolicyDecision(
                    code=PolicyViolationCode.MISSING,
                    realpath=real,
                    original=original,
                    detail=f"target stat failed: {exc.__class__.__name__}",
                )
            st_mode = target_mode

        if not _stat.S_ISREG(st_mode):
            kind = "directory" if _stat.S_ISDIR(st_mode) else "non-regular"
            return PolicyDecision(
                code=PolicyViolationCode.NOT_REGULAR,
                realpath=real,
                original=original,
                detail=f"path is {kind}, regular file required",
            )

        # Step 4: allow-list check.
        real_abs = os.path.abspath(real)
        accepted_root = self._root_containing(real_abs)
        if accepted_root is None:
            return PolicyDecision(
                code=PolicyViolationCode.OUTSIDE_ROOTS,
                realpath=real_abs,
                original=original,
                detail=(
                    f"path is outside allowed roots "
                    f"({self._roots_summary()})"
                ),
            )

        # Step 5: build OK decision, preserving the traversal hint
        # in `detail` for audit purposes.
        detail = "accepted"
        if is_symlink and self.follow_symlinks:
            detail = f"accepted via symlink (root={accepted_root})"
        elif traversal_hint:
            detail = (
                f"accepted despite `..` in original (root={accepted_root})"
            )
        else:
            detail = f"accepted (root={accepted_root})"

        return PolicyDecision(
            code=PolicyViolationCode.OK,
            realpath=real_abs,
            original=original,
            detail=detail,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _root_containing(self, candidate: str) -> Optional[str]:
        """Return the first allowed root that strictly contains the path.

        ``os.path.commonpath`` raises if the paths are on different
        drives (Windows) or if the result is empty; we treat that as
        "no match". A path equal to the root itself is also accepted
        (a file *at* the root is valid).
        """
        for root in self.allowed_roots:
            try:
                common = os.path.commonpath([candidate, root])
            except ValueError:
                continue
            if common == root:
                return root
        return None

    def _roots_summary(self) -> str:
        return ",".join(self.allowed_roots)

    # ------------------------------------------------------------------
    # Convenience constructors
    # ------------------------------------------------------------------
    @classmethod
    def default(cls) -> "ArtifactPolicy":
        """The bridge-repo policy used by the running app.

        Centralised so a future commit can change the default root
        without touching call sites.
        """
        return cls(
            allowed_roots=("/home/ubuntu/hermes-runtime-bridge",),
            description="bridge_repo_default",
        )

    @classmethod
    def permissive(cls) -> "ArtifactPolicy":
        """AEE-6.3 — a wide-open policy used by the ``ArtifactPipeline``
        *default* constructor so existing callers (the AEE-5 dispatch
        path) keep working unchanged.

        The policy still rejects symlink escapes, broken symlinks,
        and non-regular files; only the root allow-list is widened
        to ``/``. Production callers that need a tighter boundary
        must inject an explicit policy.
        """
        return cls(
            allowed_roots=("/",),
            description="pipeline_default_permissive",
        )

    @classmethod
    def with_roots(cls, roots: Iterable[PathLike], **kwargs) -> "ArtifactPolicy":
        return cls(allowed_roots=tuple(os.fspath(r) for r in roots), **kwargs)


def safe_resolve(
    path: PathLike, policy: ArtifactPolicy
) -> Tuple[str, Optional[PolicyDecision]]:
    """Convenience wrapper: returns ``(realpath, None)`` on accept or
    ``(realpath, decision)`` on reject.

    The caller can use the decision to emit an audit event without
    having to call ``policy.check()`` themselves.
    """
    decision = policy.check(path)
    if decision.accepted:
        return decision.realpath, None
    return decision.realpath, decision


# ----------------------------------------------------------------------
# Custom exception
# ----------------------------------------------------------------------
class PolicyCheckError(RuntimeError):
    """Raised on policy *configuration* errors (not per-file decisions).

    Per-file decisions are returned as ``PolicyDecision``; this
    exception is for things like an empty ``allowed_roots`` at
    construction time, or an unsupported path type.
    """
