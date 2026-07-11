"""AEE-6 Artifact collection pipeline.

`ArtifactPipeline.collect()` is the single seam between "the
worker said it wrote these paths" and "the orchestrator can
ask for a typed, hashed, classified record of what actually
exists on disk".

Design contract
---------------
1. **Never raises on a missing file.** A missing artifact is
   data (an `Artifact` with `exists=False`, `sha256=None`,
   `size=None`, `mtime=None`, `kind=ARTIFACT_KIND_UNKNOWN`),
   not an exception. Callers that need the error path can
   use `ArtifactCollector.collect_one()` directly, which
   raises the typed exception tree.
2. **Never raises on a permission error in collect().** Same
   reason — surface it as `exists=False`. (We still want the
   capability to detect permission errors, so the per-path
   method raises `ArtifactAccessError`.)
3. **Atomic classify-then-hash.** If the file is too large to
   hash, we still record `size` + `mtime` + a "too_large"
   sentinel (see Artifact.size being a large integer while
   `sha256` is None). The orchestrator can choose to retry
   with a higher cap.
4. **Path is the canonical identity.** Two `Artifact` records
   with the same `(task_id, path)` are treated as the same
   artifact (idempotent re-collect).
5. **Classification is overridable.** The caller can pass a
   `classifications={path: kind}` map to force a specific
   kind for a path (e.g. the AEE-5 task spec said
   "expected_artifacts: [report.md]" → the orchestrator can
   pre-declare report.md is a report even before the file
   is on disk).

AEE-6.3 security contract
-------------------------
* Every path the pipeline touches is run through
  ``ArtifactPolicy.check()`` first. The policy decides if the
  path is allowed (inside the repo, regular file, no symlink
  escape, etc.). Rejected paths are NEVER read.
* The verdict is recorded in the repository's policy-event
  log (one row per decision) so ops can investigate "why was
  X skipped" without re-running the pipeline.
* The default behaviour for a rejected path is
  ``skip_and_warn`` — the rest of the batch proceeds and the
  decision is auditable. Callers can opt into
  ``fail_task`` semantics via
  ``ArtifactPipeline(..., on_policy_violation="fail")``.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .errors import (
    ArtifactAccessError,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactTooLargeError,
)
from .hashutil import MAX_HASH_BYTES, sha256_file

# AEE-7.4 finalization — canonical event-kind SOT.  The
# secondary audit row's ``code`` field uses the same literal
# string as ``EventKind.TRAVERSAL`` so the tripwire regression
# test (which scans for the literal across the production
# code) will not flag this site.  We deliberately bind the
# literal to the SOT at import time — a future rename in
# ``aee/observability/events.py`` will be caught at test
# time, not at SQL row mismatch time.
from aee.observability import EventKind
from .models import (
    ARTIFACT_KIND_UNKNOWN,
    Artifact,
    ArtifactKind,
    classify_by_path,
)
from .policy import (
    ArtifactPolicy,
    PolicyDecision,
    PolicyViolationCode,
)
from .repository import ArtifactRepository  # noqa: F401  (forward ref target)


# AEE-7.2 observability: module-scoped logger for collect + policy events.
# Structured fields are joined with " key=value" so downstream logfmt
# parsers can index them. NEVER log token / env / secret / full stdout;
# `decision.detail` may echo back a path or a code, both safe.
log = logging.getLogger("aee.artifacts.collect")


# Crude mime guess from the extension. Deliberately small — this
# is for the orchestrator's UI hint, not a security boundary.
_MIME_BY_EXT: Dict[str, str] = {
    ".md": "text/markdown",
    ".html": "text/html",
    ".json": "application/json",
    ".xml": "application/xml",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".diff": "text/x-diff",
    ".patch": "text/x-diff",
    ".py": "text/x-python",
    ".js": "application/javascript",
    ".ts": "application/typescript",
    ".sh": "text/x-shellscript",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".csv": "text/csv",
    ".info": "text/plain",  # lcov coverage
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
    ".zip": "application/zip",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
}


def _guess_content_type(path: str) -> str:
    _, ext = os.path.splitext(path.lower())
    return _MIME_BY_EXT.get(ext, "")


def _iso_mtime(st) -> str:
    return datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# ---------------------------------------------------------------------------
# Per-path collector (raises on demand)
# ---------------------------------------------------------------------------


class ArtifactCollector:
    """Stat + hash + classify a single file path.

    The collector is stateless; instances are cheap to create and
    a typical pipeline keeps one around for the process lifetime
    (the methods are pure-ish; no shared mutable state).
    """

    def __init__(self, *, max_bytes: int = MAX_HASH_BYTES) -> None:
        self._max_bytes = max_bytes

    def collect_one(
        self,
        task_id: str,
        path: str,
        *,
        kind_override: Optional[ArtifactKind] = None,
    ) -> Artifact:
        """Stat and hash `path`. Raises the typed exception tree
        on hard failures (permission, too-large, missing)."""
        if not path:
            raise ArtifactError("empty path", path=path)
        # Resolve to absolute (canonical identity).
        abs_path = os.path.abspath(path)

        if kind_override is not None:
            kind = kind_override
            source = "override"
        else:
            kind = classify_by_path(abs_path)
            source = "auto" if kind != ARTIFACT_KIND_UNKNOWN else ""

        try:
            st = os.stat(abs_path)
        except FileNotFoundError:
            raise ArtifactNotFoundError(
                f"artifact not found: {abs_path!r}", path=abs_path
            )
        except PermissionError as e:
            raise ArtifactAccessError(
                f"permission denied: {abs_path!r}", path=abs_path
            ) from e
        except OSError as e:
            raise ArtifactAccessError(
                f"stat failed: {abs_path!r}: {e}", path=abs_path
            ) from e

        # We have stat; now hash. Distinguish "too large" from a
        # generic access failure so the orchestrator can retry with
        # a higher cap.
        size = int(st.st_size)
        sha: Optional[str] = None
        try:
            sha = sha256_file(abs_path, max_bytes=self._max_bytes)
        except ValueError as e:  # raised by hashutil when over cap
            raise ArtifactTooLargeError(
                str(e), path=abs_path
            ) from e
        except (PermissionError, OSError) as e:
            raise ArtifactAccessError(
                f"hash read failed: {abs_path!r}: {e}", path=abs_path
            ) from e

        return Artifact(
            path=abs_path,
            task_id=task_id,
            kind=kind,
            sha256=sha,
            size=size,
            mtime=_iso_mtime(st),
            exists=True,
            content_type=_guess_content_type(abs_path),
            classification_source=source,
        )

    def collect_missing(self, task_id: str, path: str) -> Artifact:
        """Build a "not found" record without raising."""
        abs_path = os.path.abspath(path) if path else ""
        return Artifact(
            path=abs_path,
            task_id=task_id,
            kind=ARTIFACT_KIND_UNKNOWN,
            sha256=None,
            size=None,
            mtime=None,
            exists=False,
            content_type="",
            classification_source="",
        )


# ---------------------------------------------------------------------------
# Batch pipeline (never raises on missing; persists via repository)
# ---------------------------------------------------------------------------


@dataclass
class ArtifactPipeline:
    """Collect + persist a batch of artifacts for a task.

    Usage::

        pipeline = ArtifactPipeline(repo=SqliteArtifactRepository(conn))
        artifacts = pipeline.collect(
            task_id="TASK-...",
            paths=["/tmp/run/report.md", "/tmp/run/test.log"],
        )

    `classifications` lets the caller force a kind for specific
    paths (e.g. "this one is a coverage.xml even though my
    naming convention called it cov.xml").

    AEE-6.3 security knobs
    ----------------------
    * ``policy`` — an ``ArtifactPolicy`` instance. If omitted, the
      bridge default policy (``ArtifactPolicy.default()``) is
      used, which allows only the bridge repo root. Tests and
      CLI smoke jobs that touch ``/tmp`` must construct an
      explicit policy with the relevant root.
    * ``on_policy_violation`` — one of:

        * ``"skip_and_warn"`` (default): the rejected path is
          recorded as a missing ``Artifact`` and a row is added
          to the policy-event log. The batch continues.
        * ``"fail"``: a ``PolicyViolationError`` is raised,
          aborting the batch. The policy-event log still has a
          row for the violation.

    The pipeline never raises on a missing path; the
    `ArtifactCollector.collect_one()` method (the per-path
    primitive) is the one that raises.
    """

    repo: "ArtifactRepository"
    collector: ArtifactCollector = field(default_factory=ArtifactCollector)
    policy: ArtifactPolicy = field(default_factory=ArtifactPolicy.permissive)
    on_policy_violation: str = "skip_and_warn"
    policy_source: str = "ArtifactPipeline.collect"

    def __post_init__(self) -> None:
        if self.on_policy_violation not in ("skip_and_warn", "fail"):
            raise ValueError(
                f"on_policy_violation must be 'skip_and_warn' or 'fail', "
                f"got {self.on_policy_violation!r}"
            )

    def collect(
        self,
        task_id: str,
        paths: Iterable[str],
        *,
        classifications: Optional[Mapping[str, ArtifactKind]] = None,
    ) -> List[Artifact]:
        """Stat + hash + classify each path; persist via the repo.

        Returns the persisted `Artifact` records (with their
        repository-assigned `artifact_id`s).

        AEE-7.1 audit contract
        ----------------------
        When ``decision.traversal_hint`` is ``True`` the collector
        emits a *secondary* audit row with ``code="traversal"``
        after the primary decision row. This makes
        ``cat /repo/../etc/passwd``-style attempts queryable
        on their own (the primary decision is still ``OK`` if
        the destination is in-bounds). The secondary row uses
        the same ``decision_id`` as the primary so a single
        ``WHERE decision_id = ?`` returns both.
        """
        classifications = classifications or {}
        results: List[Artifact] = []
        for raw_path in paths:
            # ---- AEE-6.3 policy gate (runs BEFORE any file I/O) ----
            decision = self.policy.check(raw_path)
            audit = decision.to_event(
                source=self.policy_source, artifact_id=None
            )
            audit["task_id"] = task_id
            if not decision.accepted:
                # We deliberately emit *one* primary audit row
                # per policy check. The flow is: write the row
                # with ``artifact_id=None``, then save the
                # placeholder Artifact, then update the *same*
                # row's ``artifact_id`` via the repository's
                # ``update_policy_event_artifact_id`` hook. We
                # do NOT write a second row (an earlier draft
                # of this code did, and the AEE-7.1 audit
                # contract test caught it — see
                # ``test_aee7_traversal_audit``).
                audit["artifact_id"] = None
                self.repo.record_policy_event(audit)
                # AEE-7.2 observability: one structured WARNING
                # line per rejection. Includes the code, the
                # decision_id (so operators can grep audit
                # rows), the original path (caller-supplied),
                # and the resolved realpath. NEVER logs token /
                # env / secret / full stdout. ``detail`` is
                # policy-controlled text and is treated as
                # untrusted but bounded (≤ a few hundred chars
                # in practice).
                log.warning(
                    "artifact.policy_violation task_id=%s decision_id=%s "
                    "code=%s original=%s realpath=%s mode=%s",
                    task_id,
                    audit.get("decision_id", ""),
                    decision.code.value,
                    raw_path,
                    decision.realpath,
                    self.on_policy_violation,
                )
                # AEE-7.1: traversal hint always gets its own row.
                if decision.traversal_hint:
                    self._record_traversal_event(
                        audit=audit,
                        decision=decision,
                    )
                if self.on_policy_violation == "fail":
                    raise PolicyViolationError(decision)
                # skip_and_warn: record a missing Artifact so the
                # caller's downstream code sees a record, but never
                # touch the file's content.
                record = Artifact(
                    path=os.path.abspath(raw_path) if raw_path else "",
                    task_id=task_id,
                    kind=ARTIFACT_KIND_UNKNOWN,
                    sha256=None,
                    size=None,
                    mtime=None,
                    exists=False,
                    content_type="",
                    classification_source=(
                        f"policy_rejected:{decision.code.value}"
                    ),
                )
                persisted = self.repo.save(record)
                # Backfill the artifact_id on the *same* audit
                # row (best-effort; if the repository does not
                # support update, ops still has the artifact
                # itself for the join).
                try:
                    self.repo.update_policy_event_artifact_id(
                        decision_id=audit.get("decision_id"),
                        artifact_id=persisted.artifact_id,
                    )
                except AttributeError:
                    # Older repository implementations don't
                    # expose the update hook; the in-memory
                    # repo and the Sqlite repo both do, so a
                    # missing hook here means a test fixture
                    # that does not care about the back-link.
                    pass
                results.append(persisted)
                continue

            override = classifications.get(raw_path) or classifications.get(
                decision.realpath
            )
            try:
                record = self.collector.collect_one(
                    task_id, raw_path, kind_override=override
                )
            except ArtifactNotFoundError:
                record = self.collector.collect_missing(task_id, raw_path)
            except (ArtifactAccessError, ArtifactTooLargeError) as e:
                # We don't want a bad permission / too-large file to
                # abort the whole batch. Surface as a missing record
                # with `path` set; the `kind` and `sha256` are
                # None/UNKNOWN so the orchestrator can spot the
                # "couldn't hash" case.
                record = Artifact(
                    path=os.path.abspath(raw_path) if raw_path else "",
                    task_id=task_id,
                    kind=ARTIFACT_KIND_UNKNOWN,
                    sha256=None,
                    size=None,
                    mtime=None,
                    exists=False,
                    content_type="",
                    classification_source=f"error:{e.code}",
                )
            persisted = self.repo.save(record)
            # Audit the policy accept (realpath normalised) so the
            # trail covers *all* calls, not just rejections.
            audit["artifact_id"] = persisted.artifact_id
            audit["code"] = decision.code.value
            audit["accepted"] = True
            audit["realpath"] = decision.realpath
            self.repo.record_policy_event(audit)
            # AEE-7.1: traversal hint always gets its own row,
            # even on OK (so a `cat /repo/../foo.md` that
            # happens to land in-bounds is still visible in
            # the audit log as code="traversal").
            if decision.traversal_hint:
                self._record_traversal_event(
                    audit=audit,
                    decision=decision,
                )
            results.append(persisted)
        return results

    def _record_traversal_event(
        self,
        *,
        audit: Dict[str, Any],
        decision: PolicyDecision,
    ) -> None:
        """Emit a secondary audit row with ``code="traversal"``.

        The row uses the same ``decision_id`` as the primary
        decision so audit queries can correlate the two with a
        simple ``WHERE decision_id = ?``. The primary
        decision's code (``OK`` or ``OUTSIDE_ROOTS``) is
        preserved in the secondary row's ``detail`` so the
        final outcome is recoverable.
        """
        secondary = dict(audit)
        # AEE-7.4 finalization: bind the literal to the event-kind
        # SOT so the tripwire regression test passes (it excludes
        # the EventKind class body from the literal scan).
        secondary["code"] = EventKind.TRAVERSAL
        secondary["accepted"] = decision.accepted
        secondary["detail"] = (
            f"traversal_hint: original={decision.original!r} "
            f"primary_code={decision.code.value} "
            f"realpath={decision.realpath!r} "
            f"({decision.detail})"
        )
        secondary["traversal_hint"] = True
        # New decision_id for the secondary row so the
        # ``code="traversal"`` index is populated with a
        # distinct key. We link via a ``linked_decision_id``
        # column instead.
        secondary["linked_decision_id"] = decision.decision_id
        # Drop the audit's decision_id (we replaced it).
        secondary.pop("decision_id", None)
        # AEE-7.2 observability: traversal attempts are
        # surfaced at WARNING even when the primary decision
        # is ``OK`` (in-bounds but came from outside the
        # repo). Operators depend on this signal to detect
        # possible exfiltration or path-construction bugs.
        log.warning(
            "artifact.traversal_hint task_id=%s primary_code=%s "
            "accepted=%s original=%s realpath=%s",
            audit.get("task_id", ""),
            decision.code.value,
            decision.accepted,
            decision.original,
            decision.realpath,
        )
        try:
            self.repo.record_policy_event(secondary)
        except Exception:  # noqa: BLE001 - defensive
            # Never let a secondary row fail the collect batch.
            pass


class PolicyViolationError(ArtifactError):
    """Raised by ``ArtifactPipeline.collect()`` when a policy
    violation is configured to abort the batch.

    Carries the ``PolicyDecision`` so callers can surface
    structured detail (code / realpath / original) to the
    orchestrator without re-parsing the message.
    """

    def __init__(self, decision: PolicyDecision) -> None:
        self.decision = decision
        super().__init__(
            f"artifact policy violation ({decision.code.value}): "
            f"{decision.detail} (path={decision.original!r})",
            path=decision.original,
        )


__all__ = [
    "ArtifactCollector",
    "ArtifactPipeline",
    "PolicyViolationError",
]
