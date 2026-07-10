"""AEE-6 Artifact data model + path-based classification.

A single `Artifact` is a typed, hash-addressable, immutable record
of one file that a worker produced for a given task. The model is
intentionally minimal: everything an orchestrator (or a future
Web UI) needs to render "what did this run produce" without
re-stat'ing the file system.

Why frozen dataclass: Artifact records are written once and never
mutated; if a worker re-runs, the repository creates a new row
(or, on re-collect, a new artifact_id with a bumped version).
Hashing the bytes makes the artifact content-addressable.

`classify_by_path` is the canonical "what kind of file is this?"
heuristic. AEE-5 already documented the 4 named kinds (report,
patch, log, coverage) in `AEE_MASTER_PLAN.md` §AEE-5; this slice
adds a 5th ("artifact" for anything explicitly named) plus
"unknown" as the safe default.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Kind vocabulary (public constants — orchestrator-facing)
# ---------------------------------------------------------------------------

ARTIFACT_KIND_REPORT = "report"      # report.md, final-*.html, summary.json
ARTIFACT_KIND_PATCH = "patch"        # patch.diff, *.patch
ARTIFACT_KIND_LOG = "log"            # test.log, build.log, *.log
ARTIFACT_KIND_COVERAGE = "coverage"  # coverage.xml, coverage.json, lcov.info
ARTIFACT_KIND_ARTIFACT = "artifact"  # everything else with a known extension
ARTIFACT_KIND_UNKNOWN = "unknown"    # empty extension / no signal

ARTIFACT_KINDS = (
    ARTIFACT_KIND_REPORT,
    ARTIFACT_KIND_PATCH,
    ARTIFACT_KIND_LOG,
    ARTIFACT_KIND_COVERAGE,
    ARTIFACT_KIND_ARTIFACT,
    ARTIFACT_KIND_UNKNOWN,
)


# ---------------------------------------------------------------------------
# Type alias (for callers that want a string literal type)
# ---------------------------------------------------------------------------

ArtifactKind = str  # one of ARTIFACT_KINDS


# ---------------------------------------------------------------------------
# Path → kind heuristic
# ---------------------------------------------------------------------------

# Order matters: the first matching pattern wins. Each pattern
# matches against the basename (lowercased) of the path.
_CLASSIFY_RULES: tuple = (
    # patches — match before generic logs because *.patch is also a log-ish ext
    (re.compile(r"\.diff$|\.patch$"), ARTIFACT_KIND_PATCH),
    (re.compile(r"^patch[\-_]"), ARTIFACT_KIND_PATCH),
    # coverage
    (re.compile(r"coverage[\-_.].*\.(xml|json|info)$"), ARTIFACT_KIND_COVERAGE),
    (re.compile(r"^coverage\.(xml|json|info)$"), ARTIFACT_KIND_COVERAGE),
    (re.compile(r"lcov\.info$"), ARTIFACT_KIND_COVERAGE),
    # logs
    (re.compile(r"\.log$"), ARTIFACT_KIND_LOG),
    (re.compile(r"^test[\-_]?log|^build[\-_]?log"), ARTIFACT_KIND_LOG),
    # reports
    (re.compile(r"^report\.(md|html|json)$"), ARTIFACT_KIND_REPORT),
    (re.compile(r"^final[\-_].*\.(md|html|json)$"), ARTIFACT_KIND_REPORT),
    (re.compile(r"^summary\.(md|json)$"), ARTIFACT_KIND_REPORT),
    (re.compile(r"\.report\.(md|html|json)$"), ARTIFACT_KIND_REPORT),
)


def classify_by_path(path: str) -> ArtifactKind:
    """Return one of ARTIFACT_KINDS based on the path basename.

    The heuristic is intentionally narrow — only filenames
    documented in `AEE_MASTER_PLAN.md` §AEE-5 (report.md,
    patch.diff, test.log, coverage.xml) plus a small set of
    obvious variants. Anything else is `ARTIFACT_KIND_ARTIFACT`
    if it has a known extension, `ARTIFACT_KIND_UNKNOWN`
    otherwise.
    """
    if not path:
        return ARTIFACT_KIND_UNKNOWN
    base = os.path.basename(path).lower()
    if not base:
        return ARTIFACT_KIND_UNKNOWN
    for rx, kind in _CLASSIFY_RULES:
        if rx.search(base):
            return kind
    if "." in base:
        return ARTIFACT_KIND_ARTIFACT
    return ARTIFACT_KIND_UNKNOWN


# ---------------------------------------------------------------------------
# Artifact record
# ---------------------------------------------------------------------------

# Iso-8601 UTC timestamp, second precision (matches the rest of
# the dispatcher's timestamp convention; see dispatcher/db.py
# `_now()`).
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Artifact:
    """A single produced artifact (frozen, content-addressable).

    Fields:
        artifact_id: Repository-assigned id (None for "in-memory
            only" instances that haven't been persisted).
        task_id: Owning task.
        path: Absolute path on disk (the canonical identity
            alongside the sha256).
        kind: One of ARTIFACT_KINDS — derived from `classify_by_path`
            but the caller can override via `ArtifactPipeline.collect()`.
        sha256: Hex digest of the file bytes. None only if the
            file was unreadable at collect time.
        size: File size in bytes. None if unreadable.
        mtime: File mtime as ISO-8601 UTC string. None if unreadable.
        exists: False iff the file was missing at collect time.
        content_type: Best-effort mime guess from the extension
            (e.g. "text/markdown", "text/x-diff"). Empty string
            if unknown.
        classification_source: "auto" if `classify_by_path` set
            the kind, "override" if the caller forced it, "" if
            the file was missing.
        collected_at: ISO-8601 UTC timestamp of when this record
            was created.
    """
    path: str
    task_id: str
    kind: ArtifactKind = ARTIFACT_KIND_UNKNOWN
    sha256: Optional[str] = None
    size: Optional[int] = None
    mtime: Optional[str] = None
    exists: bool = False
    content_type: str = ""
    classification_source: str = ""
    artifact_id: Optional[str] = None
    collected_at: str = field(default_factory=_utc_now_iso)

    # -- Convenience --------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


__all__ = [
    "ARTIFACT_KIND_REPORT",
    "ARTIFACT_KIND_PATCH",
    "ARTIFACT_KIND_LOG",
    "ARTIFACT_KIND_COVERAGE",
    "ARTIFACT_KIND_ARTIFACT",
    "ARTIFACT_KIND_UNKNOWN",
    "ARTIFACT_KINDS",
    "ArtifactKind",
    "Artifact",
    "classify_by_path",
]
