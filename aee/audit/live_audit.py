"""AEE-7.7a — Live-Report Audit (core implementation).

This module is the AEE-7.7a ``run_audit`` entry point.  It walks
a ``reports/`` directory, classifies every ``task.json`` via
the AEE-7.11 :func:`aee.reporting.identity.classify_record` SOT,
runs the AEE-7.6 G3 :func:`aee.reporting.identity_consistency.validate_task_identity`
consistency check on each, aggregates the verdicts, and writes
a JSON + a Markdown summary to the caller-supplied output path.

Read-only contract
------------------

The audit never writes inside the audited ``reports/`` directory.
It writes **exactly two** files in the output directory:

* ``aee77a-audit-<utc>.json`` — full per-task verdicts
  (``PerTaskVerdict.to_dict()`` shape).
* ``aee77a-audit-<utc>.md`` — the human-readable summary.

No file in the source tree (``reports/``, ``aee/``, ``tests/``,
``dispatcher/``) is created, modified, or deleted by the audit.
The ``data/dispatcher.db`` is not opened by the audit at all —
it is a pure filesystem walker over the ``reports/`` subtree.

The audit is safe to run against a live bridge / live
dispatcher.  Running it does NOT take a write lock on any file
in the reports directory; the walker only opens each
``task.json`` for reading.

Why this is the smallest AEE-7.7 vertical slice
-----------------------------------------------

The AEE-7.7 plan (per ``AEE_MASTER_PLAN.md`` §A.7.14 final
paragraph) is "wire identity consistency into
:classify_and_persist and run audit on live reports/".  This
slice delivers the **second half** of that promise with zero
dispatcher change.  The wire-up (calling ``classify_and_persist``
from ``dispatcher/manager.py:complete()``) is a separate slice
that touches the hot path; this audit is the read-only
predecessor that gives the user a verifiable AEE-7.7 deliverable
without any production risk.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aee.reporting.identity import (
    RecordKind,
    SentinelPolicy,
    classify_record,
    iter_reports,
    load_task_json,
)
from aee.reporting.identity_consistency import (
    ConsistencySeverity,
    validate_task_identity,
)


# Stable schema version for the audit JSON.  Bumping this is a
# breaking change for any downstream consumer.
AUDIT_SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class PerTaskVerdict:
    """A single record's verdict.

    The dataclass is frozen so it can be hashed / put in a set
    in test assertions.  ``findings`` is a list of structured
    findings (the same shape as
    :class:`aee.reporting.identity_consistency.ConsistencyFinding.to_dict`).
    """

    task_id: str
    record_kind: str  # "runtime" / "fixture" / "unknown"
    is_fixture: bool
    fixture_markers: Tuple[str, ...]
    is_consistent: bool
    findings: Tuple[Dict[str, Any], ...]  # serialised ConsistencyFinding
    # Provenance: where the verdict came from.  Helps the
    # reader reconstruct "which corpus was audited".
    source_task_json_sha256: str
    classified_at_utc: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "record_kind": self.record_kind,
            "is_fixture": self.is_fixture,
            "fixture_markers": list(self.fixture_markers),
            "is_consistent": self.is_consistent,
            "findings": list(self.findings),
            "source_task_json_sha256": self.source_task_json_sha256,
            "classified_at_utc": self.classified_at_utc,
        }


@dataclass
class AuditSummary:
    """The aggregate audit result.

    Holds the per-task verdicts + the aggregate counts.  The
    :meth:`to_dict` shape is the JSON document.  The
    :meth:`to_markdown` shape is the human-readable summary.
    """

    reports_root: str
    audited_at_utc: str
    schema_version: str
    verdicts: List[PerTaskVerdict] = field(default_factory=list)
    # Aggregate buckets.  Computed from ``verdicts`` but cached
    # here so the markdown render is cheap.
    by_record_kind: Dict[str, int] = field(default_factory=dict)
    by_consistency: Dict[str, int] = field(default_factory=dict)
    # Per-finding-code counts (e.g. ``RUNTIME_ANCHOR_SHAPE: 3``).
    finding_code_counts: Dict[str, int] = field(default_factory=dict)
    # Aggregate: count of tasks where ``is_fixture == True``
    # AND ``is_consistent == False`` (the dangerous
    # combination that downstream consumers must not cite).
    fixture_inconsistent_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "audited_at_utc": self.audited_at_utc,
            "reports_root": self.reports_root,
            "total_records": len(self.verdicts),
            "by_record_kind": dict(self.by_record_kind),
            "by_consistency": dict(self.by_consistency),
            "finding_code_counts": dict(self.finding_code_counts),
            "fixture_inconsistent_count": self.fixture_inconsistent_count,
            "verdicts": [v.to_dict() for v in self.verdicts],
        }

    def to_markdown(self) -> str:
        lines: List[str] = []
        lines.append("# AEE-7.7a Live-Report Audit")
        lines.append("")
        lines.append(f"- Schema version: `{self.schema_version}`")
        lines.append(f"- Audited at (UTC): `{self.audited_at_utc}`")
        lines.append(f"- Reports root: `{self.reports_root}`")
        lines.append(f"- Total records: **{len(self.verdicts)}**")
        lines.append("")
        lines.append("## By record kind")
        lines.append("")
        lines.append("| Record kind | Count |")
        lines.append("|---|---|")
        for k in ("runtime", "fixture", "unknown"):
            lines.append(f"| {k} | {self.by_record_kind.get(k, 0)} |")
        lines.append("")
        lines.append("## By consistency verdict")
        lines.append("")
        lines.append("| Consistent | Count |")
        lines.append("|---|---|")
        lines.append(
            f"| True | {self.by_consistency.get('consistent_true', 0)} |"
        )
        lines.append(
            f"| False | {self.by_consistency.get('consistent_false', 0)} |"
        )
        lines.append("")
        lines.append("## Finding code counts")
        lines.append("")
        if not self.finding_code_counts:
            lines.append("_(none)_")
        else:
            lines.append("| Code | Count |")
            lines.append("|---|---|")
            for code, count in sorted(
                self.finding_code_counts.items(),
                key=lambda kv: (-kv[1], kv[0]),
            ):
                lines.append(f"| `{code}` | {count} |")
        lines.append("")
        lines.append(
            f"## Fixture + inconsistent combination: "
            f"{self.fixture_inconsistent_count}"
        )
        lines.append("")
        if self.fixture_inconsistent_count == 0:
            lines.append(
                "No fixture records are also identity-inconsistent. "
                "Downstream consumers may safely classify FIXTURE "
                "records as non-citable based on `is_fixture` alone."
            )
        else:
            lines.append(
                f"{self.fixture_inconsistent_count} records are both "
                "FIXTURE-classified AND identity-inconsistent.  These "
                "are the high-risk cases — the `is_fixture` flag is the "
                "primary guard, but the consistency findings provide "
                "secondary evidence.  See the JSON for per-record "
                "details."
            )
        lines.append("")
        return "\n".join(lines)


def _now_utc_iso() -> str:
    """Return current UTC timestamp in ISO-8601 'Z' form.

    Uses ``datetime.now(timezone.utc)`` so the timestamp is
    timezone-explicit; the trailing 'Z' is appended manually so
    log readers do not have to second-guess offset semantics.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _verdict_for(
    task_id: str,
    task_json: Dict[str, Any],
    source_sha: str,
    policy: SentinelPolicy,
    classified_at: str,
) -> PerTaskVerdict:
    """Classify a single record + run the consistency check.

    The two helpers are chained (classification first, then
    consistency with the classification as input).  The
    consistency check uses the record_kind from the classifier
    — this is the same call shape
    :func:`aee.reporting.identity.classify_and_persist` would
    have produced, minus the sidecar write.
    """
    identity = classify_record(
        task_id=task_id,
        task_json=task_json,
        policy=policy,
    )
    record_kind = identity.record_kind.value
    is_fixture = identity.is_fixture

    consistency_report = validate_task_identity(
        task_json,
        record_kind=record_kind,
    )
    findings = tuple(
        {
            "code": f.code,
            "severity": f.severity.value,
            "message": f.message,
            "field_path": f.field_path,
        }
        for f in consistency_report.findings
    )

    return PerTaskVerdict(
        task_id=task_id,
        record_kind=record_kind,
        is_fixture=is_fixture,
        fixture_markers=tuple(identity.fixture_markers),
        is_consistent=consistency_report.is_consistent,
        findings=findings,
        source_task_json_sha256=source_sha,
        classified_at_utc=classified_at,
    )


def run_audit(
    reports_root: str | os.PathLike,
    output_dir: str | os.PathLike,
    *,
    policy: Optional[SentinelPolicy] = None,
    utc_stamp: Optional[str] = None,
) -> Tuple[AuditSummary, Path, Path]:
    """Run the live-report audit.  Returns (summary, json_path, md_path).

    Parameters
    ----------
    reports_root
        Filesystem path to the reports root.  Must be readable;
        non-existent paths yield an empty summary.
    output_dir
        Directory to write the audit output to.  Must exist or
        be creatable.  Two files are written:
        ``aee77a-audit-<utc>.json`` and
        ``aee77a-audit-<utc>.md``.
    policy
        Optional :class:`SentinelPolicy` override.  Default is
        the AEE-7.11 conservative heuristic set.
    utc_stamp
        Optional UTC timestamp string to use as the audit
        filename suffix.  When ``None`` (default), the audit
        uses the current UTC time.  Tests pass an explicit
        stamp for deterministic output.

    Returns
    -------
    (summary, json_path, md_path)
        ``summary`` is the in-memory aggregate.  ``json_path``
        and ``md_path`` are the absolute paths to the two
        output files that were written.

    Raises
    ------
    OSError
        If ``output_dir`` cannot be created.
    ValueError
        If ``reports_root`` is not a string / Path.
    """
    root = Path(reports_root)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    if policy is None:
        policy = SentinelPolicy()  # conservative default

    if utc_stamp is None:
        utc_stamp = _now_utc_iso()

    summary = AuditSummary(
        reports_root=str(root.resolve()),
        audited_at_utc=utc_stamp,
        schema_version=AUDIT_SCHEMA_VERSION,
    )

    by_kind: Dict[str, int] = {
        RecordKind.RUNTIME.value: 0,
        RecordKind.FIXTURE.value: 0,
        RecordKind.UNKNOWN.value: 0,
    }
    by_consistency: Dict[str, int] = {
        "consistent_true": 0,
        "consistent_false": 0,
    }
    code_counts: Dict[str, int] = {}
    fixture_inconsistent = 0

    # iter_reports is sorted by task_id (deterministic) and
    # yields only records with a parseable task.json.  We use
    # the sorted-by-task_id property so the JSON output is
    # byte-deterministic across runs.
    for task_id, task_json_path in iter_reports(root):
        # Load via the SOT's robust loader so malformed
        # records do not raise.
        raw = load_task_json(task_json_path)
        if raw is None:
            # Malformed record — record as UNKNOWN with
            # no findings, so the audit does not silently
            # drop the task from the count.
            verdict = PerTaskVerdict(
                task_id=task_id,
                record_kind=RecordKind.UNKNOWN.value,
                is_fixture=False,
                fixture_markers=("malformed_task_json",),
                is_consistent=False,
                findings=(
                    {
                        "code": "MALFORMED_TASK_JSON",
                        "severity": ConsistencySeverity.ERROR.value,
                        "message": (
                            "task.json could not be loaded as a dict"
                        ),
                        "field_path": "",
                    },
                ),
                source_task_json_sha256="",
                classified_at_utc=utc_stamp,
            )
        else:
            # Compute the sha via the SOT's helper for
            # consistency with classify_and_persist.
            from aee.reporting.identity import _file_sha256
            source_sha = _file_sha256(task_json_path)
            verdict = _verdict_for(
                task_id=task_id,
                task_json=raw,
                source_sha=source_sha,
                policy=policy,
                classified_at=utc_stamp,
            )

        summary.verdicts.append(verdict)
        by_kind[verdict.record_kind] = by_kind.get(verdict.record_kind, 0) + 1
        if verdict.is_consistent:
            by_consistency["consistent_true"] += 1
        else:
            by_consistency["consistent_false"] += 1
        for f in verdict.findings:
            code = str(f.get("code", ""))
            if code:
                code_counts[code] = code_counts.get(code, 0) + 1
        if verdict.is_fixture and not verdict.is_consistent:
            fixture_inconsistent += 1

    summary.by_record_kind = by_kind
    summary.by_consistency = by_consistency
    summary.finding_code_counts = code_counts
    summary.fixture_inconsistent_count = fixture_inconsistent

    # Sanitise the utc_stamp for the filename (replace ':' with
    # '-' so the filename is portable across filesystems).
    fname_stamp = utc_stamp.replace(":", "-")
    json_path = out / f"aee77a-audit-{fname_stamp}.json"
    md_path = out / f"aee77a-audit-{fname_stamp}.md"

    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(summary.to_dict(), fh, indent=2, sort_keys=True)
        fh.write("\n")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(summary.to_markdown())

    return summary, json_path, md_path
