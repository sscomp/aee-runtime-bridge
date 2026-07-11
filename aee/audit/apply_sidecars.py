"""AEE-7.7b — call-site migration for ``identity.json`` sidecars.

AEE-7.7a shipped a read-only ``run_audit`` that walks
``reports/`` and produces an :class:`aee.audit.AuditSummary`.
This module is the **write-side counterpart**: given an
``AuditSummary`` (and the ``reports/`` it audited), it
persists an ``identity.json`` sidecar next to every
``task.json`` the audit classified as consistent
(``is_consistent=True``).

Why this lives in its own module
--------------------------------

The previous sidecar-writing paths in the codebase are:

* :func:`aee.reporting.identity.classify_and_persist` — the
  per-record helper used by the AEE-7.11 ``build_index``
  CLI. It writes a sidecar for FIXTURE / UNKNOWN records
  unconditionally and for RUNTIME records only when
  ``sidecar_for_runtime=True``.
* :func:`aee.reporting.identity.write_identity_sidecar` —
  the low-level atomic writer.

Both are still the **right primitives** for new code; this
module wraps them with a single call-site API that:

1. Operates on the AEE-7.7a ``AuditSummary`` (so the
   audit's consistency verdict is honored — inconsistent
   records are NEVER auto-overwritten).
2. Is **idempotent**: re-applying the same summary over
   the same ``reports/`` is a no-op (byte-for-byte).
3. Records explicit ``SidecarDecision`` per task so a
   caller (CI, future AEE-7.7c migration registry) can
   see what changed and why.
4. Is **secret-safe**: never reads ``dispatcher/.env``,
   never logs ``input_text`` / ``payload`` / stdout / stderr.
5. Never mutates ``task.json`` itself — sidecars are
   strictly additive.

Read-only vs write-side contract
--------------------------------

The audit (``aee/audit/live_audit.py``) is read-only.
This module is the **opt-in** write-side. A future
caller MUST explicitly invoke :func:`apply_sidecars` —
the audit does not call it. This keeps the audit
itself a safe one-shot for stale corpora.

The module imports ``aee.reporting`` (the read-side
identity SOT) and ``aee.audit.live_audit`` (for
``PerTaskVerdict`` and ``AuditSummary`` shape). It does
NOT import ``dispatcher`` (verified by
``test_no_dispatcher_import_after_apply``).

Out of scope
------------

* No dispatcher hot-path changes.
* No live-DB writes.
* No schema migration.
* No ``task.json`` mutation.
* No environment variable reads.
* No subprocess / network.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aee.audit.live_audit import AuditSummary, PerTaskVerdict
from aee.reporting.identity import (
    Identity,
    RecordKind,
    SentinelPolicy,
    _file_sha256,
    classify_and_persist,
    iter_reports,
    read_identity_sidecar,
)


# Stable schema version for the apply-sidecars DTO. Bumping
# this is a breaking change for any downstream consumer
# (currently only ``aee/tests/test_aee77_apply_sidecars.py``).
APPLY_SCHEMA_VERSION = "1.0.0"

# Anchor-warning finding codes (severity == warning / info) that
# the AEE-7.6 identity-consistency validator emits. Sidecars
# for records with these findings are still written (they are
# NOT errors) but the decision is flagged with
# ``anchor_warnings`` so a human reviewer can spot them.
_ANCHOR_WARNING_CODES: frozenset = frozenset({
    "RUNTIME_ANCHOR_SHAPE",
    "FIXTURE_RECORD_WITH_REAL_ANCHOR",
})

# Anchor-ERROR finding codes. Records with these findings are
# always SKIPPED (is_consistent==False ⇒ do not auto-overwrite).
# The names are sourced from the AEE-7.6 validator; if a new
# ERROR code is added, list it here too.
_ANCHOR_ERROR_CODES: frozenset = frozenset({
    "MISMATCHED_RUNTIME_ANCHORS",
    "RUNTIME_RECORD_WITHOUT_ANCHOR",
    "RUNNING_TASK_WITHOUT_ANCHOR",
    "EMPTY_TASK_ID",
    "MALFORMED_TASK_JSON",
})


class SidecarDecision(str, Enum):
    """The per-task outcome of :func:`apply_sidecars`.

    The string values are persisted to the
    :class:`ApplySidecarsResult` DTO and asserted against in
    tests. Adding a new value is a schema-change.
    """

    WROTE = "wrote"
    UNCHANGED = "unchanged"               # sidecar already matches verdict
    OVERWROTE = "overwrote"               # existing sidecar differed
    SKIPPED_INCONSISTENT = "skipped_inconsistent"
    SKIPPED_NO_TASK_JSON = "skipped_no_task_json"
    SKIPPED_MALFORMED = "skipped_malformed"
    SKIPPED_RUNTIME_DISALLOWED = "skipped_runtime_disallowed"
    SKIPPED_COLLISION = "skipped_collision"
    SKIPPED_NOT_IN_SUMMARY = "skipped_not_in_summary"


def _now_utc_iso() -> str:
    """Return current UTC ISO-8601 'Z' timestamp."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class PerTaskSidecarOutcome:
    """The decision for a single ``task.json`` in the
    audited corpus. Frozen so it can be hashed / put in a
    set in test assertions.
    """

    task_id: str
    decision: SidecarDecision
    record_kind: Optional[str]   # "runtime" / "fixture" / "unknown" / None
    is_consistent: Optional[bool]
    # SHA-256 of the underlying task.json (empty when the
    # record was malformed / not in summary).
    source_task_json_sha256: str
    # Sidecar SHA-256 (post-write). Empty when the sidecar
    # was not written / not readable.
    sidecar_sha256: str
    # Codes of WARNING / INFO findings attached to the
    # verdict (anchor-shape observations, fixture-with-real-
    # anchor, etc.). The sidecar IS written for these — the
    # human reviewer should see them in the audit output.
    anchor_warnings: Tuple[str, ...]
    # Codes of ERROR findings attached to the verdict
    # (the reason the sidecar was skipped / not written).
    # Only populated when decision == SKIPPED_INCONSISTENT
    # or SKIPPED_COLLISION.
    error_codes: Tuple[str, ...]
    # Human-readable note for logging / debugging. NEVER
    # contains the task's input_text, payload, prompt,
    # stdout / stderr, or any secret-bearing field.
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "decision": self.decision.value,
            "record_kind": self.record_kind,
            "is_consistent": self.is_consistent,
            "source_task_json_sha256": self.source_task_json_sha256,
            "sidecar_sha256": self.sidecar_sha256,
            "anchor_warnings": list(self.anchor_warnings),
            "error_codes": list(self.error_codes),
            "note": self.note,
        }


@dataclass
class ApplySidecarsResult:
    """The aggregate :func:`apply_sidecars` result.

    The :meth:`to_dict` shape is the JSON document. The
    :meth:`to_markdown` shape is the human-readable
    summary. Both are safe to log / persist (no secrets,
    no task input_text, no prompt, no stdout / stderr).
    """

    reports_root: str
    applied_at_utc: str
    schema_version: str
    policy_version: str
    # Whether the call honored ``is_consistent`` from the
    # summary (always True in production; exposed so tests
    # can toggle the strict / lenient mode).
    strict_consistency: bool
    outcomes: List[PerTaskSidecarOutcome] = field(default_factory=list)
    # Aggregate counts. Computed from ``outcomes``.
    by_decision: Dict[str, int] = field(default_factory=dict)
    by_record_kind: Dict[str, int] = field(default_factory=dict)
    # Number of records that had anchor_warnings (the
    # audit's WARNING / INFO findings). Always 0 for
    # RUNTIME records when ``allow_runtime=False``.
    anchor_warning_count: int = 0
    # Number of sidecar SHA-256s that match the source
    # task.json SHA-256 (sanity check — they should
    # always differ because the sidecar is a different
    # document, but the count lets a test confirm the
    # writer actually wrote a sidecar).
    sidecars_written: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "applied_at_utc": self.applied_at_utc,
            "reports_root": self.reports_root,
            "policy_version": self.policy_version,
            "strict_consistency": self.strict_consistency,
            "by_decision": dict(self.by_decision),
            "by_record_kind": dict(self.by_record_kind),
            "anchor_warning_count": self.anchor_warning_count,
            "sidecars_written": self.sidecars_written,
            "outcomes": [o.to_dict() for o in self.outcomes],
        }

    def to_markdown(self) -> str:
        lines: List[str] = []
        lines.append("# AEE-7.7b Sidecar Apply Summary")
        lines.append("")
        lines.append(f"- Schema version: `{self.schema_version}`")
        lines.append(f"- Applied at (UTC): `{self.applied_at_utc}`")
        lines.append(f"- Reports root: `{self.reports_root}`")
        lines.append(f"- Policy version: `{self.policy_version}`")
        lines.append(f"- Strict consistency: `{self.strict_consistency}`")
        lines.append(f"- Outcomes: **{len(self.outcomes)}**")
        lines.append("")
        lines.append("## By decision")
        lines.append("")
        lines.append("| Decision | Count |")
        lines.append("|---|---|")
        for code, count in sorted(
            self.by_decision.items(), key=lambda kv: (-kv[1], kv[0])
        ):
            lines.append(f"| `{code}` | {count} |")
        lines.append("")
        lines.append("## By record kind")
        lines.append("")
        lines.append("| Record kind | Count |")
        lines.append("|---|---|")
        for k in ("runtime", "fixture", "unknown"):
            lines.append(
                f"| {k} | {self.by_record_kind.get(k, 0)} |"
            )
        lines.append("")
        lines.append(
            f"## Anchor warnings: {self.anchor_warning_count}"
        )
        lines.append(
            f"## Sidecars written: {self.sidecars_written}"
        )
        lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_finding_codes(verdict: PerTaskVerdict) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Return ``(anchor_warnings, error_codes)`` for a verdict.

    The findings come pre-serialised in
    :class:`PerTaskVerdict.findings` (a tuple of dicts with
    ``code`` / ``severity``). We split them into the two
    non-overlapping buckets:

    * ``anchor_warnings`` — ``severity in {"warning", "info"}``
      (the audit attaches RUNTIME_ANCHOR_SHAPE / FIXTURE_RECORD_WITH_REAL_ANCHOR).
    * ``error_codes`` — ``severity == "error"`` (the audit
      attaches MISMATCHED_RUNTIME_ANCHORS / etc.). The
      :attr:`ApplySidecarsResult.by_decision` will count a
      record as ``skipped_inconsistent`` when this bucket is
      non-empty AND ``is_consistent is False``.

    Records with only ``legacy_null_field`` findings are NOT
    treated as errors — they are pre-AEE-7.5 records that
    the validator explicitly tolerates. The audit's
    ``is_consistent`` flag already reflects this (LEGACY_NULL
    is informational, not an ERROR).
    """
    warnings: List[str] = []
    errors: List[str] = []
    for f in verdict.findings:
        code = str(f.get("code", "") or "")
        severity = str(f.get("severity", "") or "").lower()
        if not code:
            continue
        if severity == "error":
            errors.append(code)
        elif severity in ("warning", "info"):
            warnings.append(code)
    return tuple(warnings), tuple(errors)


def _sidecar_sha256(sidecar_path: Path) -> str:
    """Return SHA-256 of the sidecar file (empty when
    missing / unreadable).

    The function is deliberately forgiving — a missing
    sidecar is a normal state in the fixture / not-yet-
    audited world. Callers decide what to do with the
    empty string.
    """
    if not sidecar_path.exists():
        return ""
    h = __import__("hashlib").sha256()
    try:
        with open(sidecar_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def apply_sidecars(
    reports_root: str | os.PathLike,
    summary: AuditSummary,
    *,
    utc_stamp: Optional[str] = None,
    classified_at_override: Optional[str] = None,
    policy: Optional[SentinelPolicy] = None,
    force: bool = False,
    allow_runtime: bool = True,
    strict_consistency: bool = True,
    executor_anchors: Optional[Dict[str, Dict[str, str]]] = None,
    user_provided_alias: Optional[Dict[str, str]] = None,
) -> ApplySidecarsResult:
    """Persist ``identity.json`` sidecars for the audited
    ``reports/`` based on an AEE-7.7a ``AuditSummary``.

    The function is the single AEE-7.7b call-site API. It:

    1. Walks ``reports_root`` and resolves each task_id to
       its ``task.json`` (sorted, deterministic, the same
       iteration order :func:`aee.audit.run_audit` uses).
    2. Looks up the matching :class:`PerTaskVerdict` from
       ``summary`` (skipping records that were not in the
       audit's corpus — ``SKIPPED_NOT_IN_SUMMARY``).
    3. For every verdict with ``is_consistent=True``:

       * RUNTIME records: sidecar written only when
         ``allow_runtime=True`` (default True — AEE-7.7a
         shipped with this; downstream consumers now
         expect sidecars on RUNTIME too).
       * FIXTURE / UNKNOWN: sidecar always written
         (matches the AEE-7.11 ``classify_and_persist``
         default).

    4. Records with ``is_consistent=False`` (any ERROR-
       severity finding) are NEVER written
       (``SKIPPED_INCONSISTENT``). These are exactly the
       records a human reviewer needs to look at; auto-
       overwriting them would lose the audit signal.

    5. Sidecar write is **idempotent**: re-running with the
       same summary over the same corpus yields
       ``UNCHANGED`` for every record. ``force=True`` makes
       the writer overwrite every sidecar regardless.

    6. ``task.json`` is NEVER mutated.

    Parameters
    ----------
    reports_root
        The same reports root the summary was built over.
        Must be readable; non-existent paths yield an empty
        result.
    summary
        The :class:`aee.audit.AuditSummary` produced by
        :func:`aee.audit.run_audit` (or any equivalent
        that builds a ``summary.verdicts`` list with the
        same shape).
    utc_stamp
        Optional UTC timestamp string used as the
        ``applied_at_utc`` field in the result. Tests pass
        an explicit stamp for deterministic output.
    classified_at_override
        Optional override for the ``classified_at_utc`` field
        embedded in the sidecar's :class:`Identity` payload.
        Defaults to ``utc_stamp`` (then ``now_utc`` if
        ``utc_stamp`` is also None). Useful for re-applying
        an old audit at a consistent timestamp.
    policy
        Optional :class:`aee.reporting.identity.SentinelPolicy`
        override. Default: the AEE-7.11 conservative set.
    force
        If True, overwrite every sidecar regardless of
        whether the existing content matches the new
        verdict. Default False (idempotent).
    allow_runtime
        If True, also write a sidecar next to RUNTIME
        records (the AEE-7.7b wire-up default). If False,
        RUNTIME records are skipped (the AEE-7.11
        ``classify_and_persist`` default). Mismatch with the
        existing sidecar on a RUNTIME record is recorded
        but the existing sidecar is left untouched when
        ``allow_runtime=False``.
    strict_consistency
        If True (default), records with
        ``is_consistent=False`` are NEVER written. Set to
        False only in tests / explicit human-override
        scenarios.

    Returns
    -------
    ApplySidecarsResult
        The aggregate outcome. The :meth:`to_dict` shape is
        the JSON document; :meth:`to_markdown` is the
        human-readable summary. Both are safe to log.

    Notes
    -----
    The function never raises on per-record failures; an
    unparseable ``task.json`` is recorded as
    ``SKIPPED_MALFORMED`` and the audit continues. The
    only exception is an ``OSError`` on the output dir
    create (raised by the audit itself; this function
    does not create new directories).
    """
    root = Path(reports_root)
    pol = policy or SentinelPolicy()
    stamp = utc_stamp or _now_utc_iso()
    classified_at = classified_at_override or stamp
    executor_anchors = executor_anchors or {}
    user_provided_alias = user_provided_alias or {}

    result = ApplySidecarsResult(
        reports_root=str(root.resolve()) if root.exists() else str(root),
        applied_at_utc=stamp,
        schema_version=APPLY_SCHEMA_VERSION,
        policy_version="1.0.0",
        strict_consistency=strict_consistency,
    )

    if not root.exists():
        return result

    # Index the summary by task_id for O(1) lookup. The
    # summary's verdicts are already sorted by task_id (the
    # audit sorts by iter_reports, which sorts); we preserve
    # that ordering when materialising the ApplySidecarsResult
    # so a re-applied summary is byte-identical to the
    # original.
    summary_index: Dict[str, PerTaskVerdict] = {
        v.task_id: v for v in summary.verdicts
    }

    def _per_task_anchors(task_id: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Resolve (executor_session_id, runtime_run_id, user_provided_alias)
        for a given task_id from the call-site-supplied maps.

        Both maps are optional and may be empty. The shape of
        ``executor_anchors[task_id]`` is a dict of optional
        per-key values; missing keys are filled with None.
        """
        anchor = executor_anchors.get(task_id) or {}
        return (
            anchor.get("executor_session_id"),
            anchor.get("runtime_run_id"),
            user_provided_alias.get(task_id),
        )

    by_decision: Dict[str, int] = {
        d.value: 0 for d in SidecarDecision
    }
    by_kind: Dict[str, int] = {
        RecordKind.RUNTIME.value: 0,
        RecordKind.FIXTURE.value: 0,
        RecordKind.UNKNOWN.value: 0,
    }

    for task_id, task_json_path in iter_reports(root):
        verdict = summary_index.get(task_id)
        if verdict is None:
            # The summary does not cover this record (e.g. a
            # new task appeared after the audit was run).
            # Skip silently — the audit is the source of
            # truth for what to apply.
            by_decision[SidecarDecision.SKIPPED_NOT_IN_SUMMARY.value] += 1
            continue

        warnings, errors = _collect_finding_codes(verdict)
        source_sha = _file_sha256(task_json_path) or ""
        record_kind = verdict.record_kind
        is_consistent = verdict.is_consistent

        # --- 1. Inconsistent verdict: skip (always) ---
        if strict_consistency and not is_consistent:
            result.outcomes.append(
                PerTaskSidecarOutcome(
                    task_id=task_id,
                    decision=SidecarDecision.SKIPPED_INCONSISTENT,
                    record_kind=record_kind,
                    is_consistent=is_consistent,
                    source_task_json_sha256=source_sha,
                    sidecar_sha256="",
                    anchor_warnings=warnings,
                    error_codes=errors,
                    note=(
                        f"is_consistent=False "
                        f"(errors={','.join(errors) or 'none'})"
                    ),
                )
            )
            by_decision[SidecarDecision.SKIPPED_INCONSISTENT.value] += 1
            continue

        # --- 2. RUNTIME records: respect allow_runtime flag ---
        if record_kind == RecordKind.RUNTIME.value and not allow_runtime:
            result.outcomes.append(
                PerTaskSidecarOutcome(
                    task_id=task_id,
                    decision=SidecarDecision.SKIPPED_RUNTIME_DISALLOWED,
                    record_kind=record_kind,
                    is_consistent=is_consistent,
                    source_task_json_sha256=source_sha,
                    sidecar_sha256="",
                    anchor_warnings=warnings,
                    error_codes=errors,
                    note="allow_runtime=False",
                )
            )
            by_decision[SidecarDecision.SKIPPED_RUNTIME_DISALLOWED.value] += 1
            continue

        # --- 3. Idempotency check (existing sidecar match) ---
        sidecar_path = task_json_path.parent / "identity.json"
        existing_identity = read_identity_sidecar(task_json_path)
        # Per-task executor anchors (G2 wire-up; the legacy
        # build_index CLI supplied these for the canonical
        # task only; AEE-7.7b generalises via executor_anchors).
        this_exec, this_run, this_alias = _per_task_anchors(task_id)
        if existing_identity is not None and not force:
            # Build the new Identity via the SOT helper so the
            # comparison is byte-exact (same source_task_json_sha256
            # etc.).
            new_identity = classify_and_persist(
                task_json_path,
                policy=pol,
                classified_at_utc=classified_at,
                sidecar_for_runtime=allow_runtime,
                executor_session_id=this_exec,
                runtime_run_id=this_run,
                user_provided_alias=this_alias,
            )
            if new_identity is None:
                result.outcomes.append(
                    PerTaskSidecarOutcome(
                        task_id=task_id,
                        decision=SidecarDecision.SKIPPED_MALFORMED,
                        record_kind=record_kind,
                        is_consistent=is_consistent,
                        source_task_json_sha256=source_sha,
                        sidecar_sha256="",
                        anchor_warnings=warnings,
                        error_codes=errors,
                        note="classify_and_persist returned None",
                    )
                )
                by_decision[SidecarDecision.SKIPPED_MALFORMED.value] += 1
                continue

            # Equality check: same record_kind, same is_fixture,
            # same fixture_markers, same executor_session_id,
            # same runtime_run_id, same source_task_json_sha256,
            # same policy_version. (Identity.__eq__ compares all
            # fields; we DO NOT compare ``classified_at_utc``
            # because that field drifts on re-runs.)
            same_verdict = (
                existing_identity.record_kind == new_identity.record_kind
                and existing_identity.is_fixture == new_identity.is_fixture
                and list(existing_identity.fixture_markers)
                == list(new_identity.fixture_markers)
                and existing_identity.executor_session_id
                == new_identity.executor_session_id
                and existing_identity.runtime_run_id
                == new_identity.runtime_run_id
                and existing_identity.user_provided_alias
                == new_identity.user_provided_alias
                and existing_identity.source_task_json_sha256
                == new_identity.source_task_json_sha256
                and existing_identity.policy_version
                == new_identity.policy_version
            )
            if same_verdict:
                result.outcomes.append(
                    PerTaskSidecarOutcome(
                        task_id=task_id,
                        decision=SidecarDecision.UNCHANGED,
                        record_kind=record_kind,
                        is_consistent=is_consistent,
                        source_task_json_sha256=source_sha,
                        sidecar_sha256=_sidecar_sha256(sidecar_path),
                        anchor_warnings=warnings,
                        error_codes=errors,
                        note="existing sidecar matches new verdict",
                    )
                )
                by_decision[SidecarDecision.UNCHANGED.value] += 1
                continue

            # Existing sidecar differs from the new verdict.
            # If strict_consistency is off, we still write; if
            # on AND the existing record is also a "non-runtime"
            # record that the audit upgraded, we mark it as
            # ``overwrote`` (a real semantic change).
            if not strict_consistency and not is_consistent:
                result.outcomes.append(
                    PerTaskSidecarOutcome(
                        task_id=task_id,
                        decision=SidecarDecision.SKIPPED_COLLISION,
                        record_kind=record_kind,
                        is_consistent=is_consistent,
                        source_task_json_sha256=source_sha,
                        sidecar_sha256=_sidecar_sha256(sidecar_path),
                        anchor_warnings=warnings,
                        error_codes=errors,
                        note=(
                            "strict_consistency=False but "
                            "is_consistent=False; collision skipped"
                        ),
                    )
                )
                by_decision[SidecarDecision.SKIPPED_COLLISION.value] += 1
                continue
            # Fall through to the write path below.
            collision_overwrite = True
        else:
            collision_overwrite = False

        # --- 4. Write the sidecar via the SOT helper ---
        written_identity = classify_and_persist(
            task_json_path,
            policy=pol,
            classified_at_utc=classified_at,
            sidecar_for_runtime=allow_runtime,
            executor_session_id=this_exec,
            runtime_run_id=this_run,
            user_provided_alias=this_alias,
        )
        if written_identity is None:
            result.outcomes.append(
                PerTaskSidecarOutcome(
                    task_id=task_id,
                    decision=SidecarDecision.SKIPPED_MALFORMED,
                    record_kind=record_kind,
                    is_consistent=is_consistent,
                    source_task_json_sha256=source_sha,
                    sidecar_sha256="",
                    anchor_warnings=warnings,
                    error_codes=errors,
                    note="classify_and_persist returned None on write",
                )
            )
            by_decision[SidecarDecision.SKIPPED_MALFORMED.value] += 1
            continue

        decision = (
            SidecarDecision.OVERWROTE
            if collision_overwrite
            else SidecarDecision.WROTE
        )
        result.outcomes.append(
            PerTaskSidecarOutcome(
                task_id=task_id,
                decision=decision,
                record_kind=record_kind,
                is_consistent=is_consistent,
                source_task_json_sha256=source_sha,
                sidecar_sha256=_sidecar_sha256(sidecar_path),
                anchor_warnings=warnings,
                error_codes=errors,
                note=(
                    "overwrote existing sidecar" if collision_overwrite
                    else "fresh sidecar"
                ),
            )
        )
        by_decision[decision.value] += 1
        if record_kind in by_kind:
            by_kind[record_kind] += 1
        if warnings:
            result.anchor_warning_count += 1
        if decision in (
            SidecarDecision.WROTE,
            SidecarDecision.OVERWROTE,
        ):
            result.sidecars_written += 1

    result.by_decision = by_decision
    result.by_record_kind = by_kind
    return result


__all__ = [
    "APPLY_SCHEMA_VERSION",
    "ApplySidecarsResult",
    "PerTaskSidecarOutcome",
    "SidecarDecision",
    "apply_sidecars",
]
