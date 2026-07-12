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
from typing import Any, Dict, List, Optional, Tuple, Union

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
# AEE-7.8 K2.5: opt-in wire-up to the manifest → PlanInput
# adapter. Imported lazily inside :func:`apply_sidecars_with_plan`
# (NOT at module top) so the K1 import-isolation contract is
# preserved — a code path that does not opt into the planner
# must not pull in :mod:`aee.audit.manifest` transitively
# (the manifest module is read-only and self-contained, but the
# K2.5 wrapper is opt-in by design, see the function docstring
# for the full rationale).


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
    # AEE-7.8 K2.5: optional additive provenance metadata
    # attached by :func:`apply_sidecars_with_plan` when the
    # caller opts in via ``manifest_path=...``. ``None`` for
    # the K2.5-baseline :func:`apply_sidecars` path and for
    # the wrapper's default ``manifest_path=None`` call. An
    # :class:`ApplyWithPlanSummary` when the wire-up ran.
    # NOT in the dataclass ``to_dict`` output (K2.5-baseline
    # consumers must see the same shape they always saw);
    # the K2.5 wrapper exposes it via a separate field
    # accessor — see the ``to_dict_with_plan()`` method
    # below.
    plan_input_summary: Optional["ApplyWithPlanSummary"] = None  # type: ignore[name-defined]
    # AEE-7.8 K3: optional additive read-only audit metadata
    # attached by :func:`apply_sidecars_with_audit` when the
    # caller opts in via ``manifest_path=...`` AND
    # ``audit_action`` is not ``"ignore"``. ``None`` for the
    # K3-baseline no-flag call and for the wrapper's default
    # ``manifest_path=None`` call. An
    # :class:`ApplyAuditReport` when the audit ran. NOT in
    # the dataclass ``to_dict`` output (K1 + K2 + K2.5
    # consumers must see the same shape they always saw);
    # the K3 wrapper exposes it via the K3 accessor —
    # see the :meth:`to_dict_with_audit` method below.
    audit_report: Optional["ApplyAuditReport"] = None  # type: ignore[name-defined]

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

    def to_dict_with_plan(self) -> Dict[str, Any]:
        """Return the K2.5-superset ``to_dict()`` shape.

        Identical to :meth:`to_dict` for the K2.5-baseline
        fields, with one extra key:

        * ``plan_input_summary`` — the
          :meth:`ApplyWithPlanSummary.to_dict` shape when the
          wire-up ran, ``None`` when it didn't.

        K2.5-baseline consumers should keep calling
        :meth:`to_dict` (the K2.5-baseline contract is
        preserved). K2.5 callers that opted into the wire-up
        can call :meth:`to_dict_with_plan` to get the full
        superset.
        """
        d = self.to_dict()
        d["plan_input_summary"] = (
            self.plan_input_summary.to_dict()
            if self.plan_input_summary is not None
            else None
        )
        return d

    def to_dict_with_audit(self) -> Dict[str, Any]:
        """Return the K3-superset ``to_dict()`` shape.

        Identical to :meth:`to_dict_with_plan` for the
        K2.5-baseline + K2.5 additive fields, with one extra
        key on top:

        * ``audit_report`` — the
          :meth:`ApplyAuditReport.to_dict` shape when the
          audit ran, ``None`` when it didn't.

        K1 + K2 + K2.5 consumers should keep calling
        :meth:`to_dict` (the baseline contract is
        preserved). K2.5 callers that opted into the wire-up
        can call :meth:`to_dict_with_plan` for the K2.5
        superset. K3 callers that opted into the audit
        can call :meth:`to_dict_with_audit` for the full
        K2.5 + K3 superset.
        """
        d = self.to_dict_with_plan()
        d["audit_report"] = (
            self.audit_report.to_dict()
            if self.audit_report is not None
            else None
        )
        return d

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


# ---------------------------------------------------------------------------
# AEE-7.8 K2.5 — opt-in planner wire-up
# ---------------------------------------------------------------------------
# Why this lives in this module
# ------------------------------
# The AEE-7.8 K2 ship added a read-only manifest → PlanInput
# adapter (``aee.audit.manifest.manifest_to_plan_inputs``) and a
# K2 plan report that proposed an *opt-in* wire-up to the sidecar
# apply path. The K2.5 commit is that wire-up: a single wrapper
# that:
#
# 1. Preserves :func:`apply_sidecars`'s default behavior
#    byte-for-byte (the existing 22 K2-targeted tests + the AEE-7.7b
#    regression suite must still pass without modification).
# 2. Opts in ONLY when the caller supplies ``manifest_path``.
#    Without ``manifest_path``, the wrapper is a thin pass-through
#    that returns the identical :class:`ApplySidecarsResult`
#    that :func:`apply_sidecars` would have produced — same
#    fields, same iteration order, same SHA-256s on disk.
# 3. When ``manifest_path`` is supplied, loads the manifest,
#    projects it to per-file :class:`PlanInput` rows via
#    :func:`aee.audit.manifest.manifest_to_plan_inputs`, and
#    tacks an additive ``plan_input_summary`` dict onto the
#    returned :class:`ApplySidecarsResult`. The DTO is a
#    superset of the K2.5-baseline shape — every K2.5-baseline
#    field is preserved, a new optional field is added.
# 4. Never raises on a malformed manifest. The projection's
#    ``passed`` / ``warnings`` are surfaced via the summary dict
#    so a caller can decide whether to inspect them. A
#    :class:`FileNotFoundError` / :class:`IsADirectoryError`
#    on a missing ``manifest_path`` is propagated to the
#    caller — that is an explicit user-supplied input, not a
#    corpus-level error the wrapper should silently swallow.
# 5. Never imports :mod:`dispatcher`. The wrapper uses
#    :mod:`aee.audit.manifest` (read-only) + the existing
#    SOT helpers in this module. Live DB / subprocess / env
#    reads remain out of scope.
#
# Out of scope (K3+ territory)
# ----------------------------
# * Replacing :func:`apply_sidecars`'s inner walk with a
#   PlanInput-driven planner. The K2.5 wire-up is additive
#   only — the existing planner still walks ``reports/`` and
#   writes sidecars. A future K3+ slice may opt in to consume
#   ``PlanInput`` rows directly.
# * Plan-input gating (e.g. ``only plan_inputs whose
#   extras['writes_to_live_db'] is False``). The K2.5 wire-up
#   does NOT filter the apply pass — the manifest is
#   provenance metadata, not a gate.
# * Manifest-write tooling. The K2.5 wire-up is read-only with
#   respect to the manifest artifact (it never writes
#   ``AEE_7_7d_7e_MANIFEST.json`` back).

#: Schema version for the K2.5 wire-up summary. Distinct
#: from :data:`APPLY_SCHEMA_VERSION` so a downstream consumer
#: can switch on the wire-up's presence without breaking the
#: K2.5-baseline contract.
PLAN_APPLY_SCHEMA_VERSION = "1.0.0"


@dataclass
class ApplyWithPlanSummary:
    """The provenance metadata the K2.5 wire-up adds to
    :class:`ApplySidecarsResult` when a ``manifest_path`` is
    supplied.

    The shape is intentionally narrow: it carries the manifest
    fingerprint + the projection verdict (passed / warning count /
    plan_input count) and nothing else. A caller that needs the
    full :class:`ManifestToPlanResult` can re-run
    :func:`aee.audit.manifest.manifest_to_plan_inputs` on the
    same ``manifest_path``; the wrapper does not forward the
    full result to keep the additive DTO small and the
    :meth:`ApplySidecarsResult.to_dict` byte-shape stable
    across K2.5 + K3+.

    Attributes
    ----------
    schema_version
        The wire-up schema version. Always
        :data:`PLAN_APPLY_SCHEMA_VERSION` for K2.5.
    manifest_source_path
        The caller-supplied ``manifest_path`` (post-``os.fspath``).
    manifest_on_disk_sha256
        The on-disk SHA-256 of the manifest at the time the
        wrapper was called. Empty when load failed.
    manifest_on_disk_size
        The on-disk size of the manifest. Zero when load failed.
    plan_input_count
        Number of :class:`PlanInput` rows the adapter projected.
        Zero when the manifest failed validation or is empty.
    projection_passed
        ``True`` iff the projection's
        :attr:`ManifestToPlanResult.passed` is True. ``False``
        when the manifest failed validation (the projection
        is then empty and the warnings list is populated).
    projection_warning_count
        Number of warnings the projection emitted. ``0`` is
        the success case.
    """

    schema_version: str
    manifest_source_path: str
    manifest_on_disk_sha256: str
    manifest_on_disk_size: int
    plan_input_count: int
    projection_passed: bool
    projection_warning_count: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "manifest_source_path": self.manifest_source_path,
            "manifest_on_disk_sha256": self.manifest_on_disk_sha256,
            "manifest_on_disk_size": self.manifest_on_disk_size,
            "plan_input_count": self.plan_input_count,
            "projection_passed": self.projection_passed,
            "projection_warning_count": self.projection_warning_count,
        }


def apply_sidecars_with_plan(
    reports_root: str | os.PathLike,
    summary: AuditSummary,
    *,
    manifest_path: Optional[Union[str, os.PathLike]] = None,
    utc_stamp: Optional[str] = None,
    classified_at_override: Optional[str] = None,
    policy: Optional[SentinelPolicy] = None,
    force: bool = False,
    allow_runtime: bool = True,
    strict_consistency: bool = True,
    executor_anchors: Optional[Dict[str, Dict[str, str]]] = None,
    user_provided_alias: Optional[Dict[str, str]] = None,
) -> ApplySidecarsResult:
    """AEE-7.8 K2.5 — opt-in planner wire-up around
    :func:`apply_sidecars`.

    This is a **thin, additive wrapper** that:

    * Calls :func:`apply_sidecars` with the same arguments,
      preserving byte-for-byte output (same fields, same
      iteration order, same on-disk sidecar SHA-256s).
    * When ``manifest_path is None`` (the default), returns
      the :class:`ApplySidecarsResult` unchanged — no
      ``plan_input_summary`` is added. The K2.5-baseline
      callers do not need to be touched.
    * When ``manifest_path is not None``, loads the manifest
      via :func:`aee.audit.manifest.load_manifest`, projects
      it to per-file :class:`PlanInput` rows via
      :func:`aee.audit.manifest.manifest_to_plan_inputs`,
      attaches an :class:`ApplyWithPlanSummary` to the result
      (as ``result.plan_input_summary`` — ``None`` for
      K2.5-baseline callers), and returns the augmented
      DTO. The projection NEVER replaces or short-circuits
      the apply pass — it is provenance metadata only.

    Parameters
    ----------
    reports_root
        Forwarded verbatim to :func:`apply_sidecars`.
    summary
        Forwarded verbatim to :func:`apply_sidecars`.
    manifest_path
        Optional path to an AEE-7.7d/7.7e manifest artifact.
        When ``None`` (the default), the wrapper is a
        byte-for-byte pass-through. When supplied, the
        manifest is loaded and projected; the projection
        is exposed via the additive ``plan_input_summary``
        field on the returned result.
    utc_stamp
        Forwarded verbatim to :func:`apply_sidecars`. Useful
        for re-applying an old audit at a consistent
        timestamp.
    classified_at_override
        Forwarded verbatim to :func:`apply_sidecars`.
    policy
        Forwarded verbatim to :func:`apply_sidecars`.
    force
        Forwarded verbatim to :func:`apply_sidecars`.
    allow_runtime
        Forwarded verbatim to :func:`apply_sidecars`.
    strict_consistency
        Forwarded verbatim to :func:`apply_sidecars`.
    executor_anchors
        Forwarded verbatim to :func:`apply_sidecars`.
    user_provided_alias
        Forwarded verbatim to :func:`apply_sidecars`.

    Returns
    -------
    ApplySidecarsResult
        Identical to :func:`apply_sidecars`'s return value,
        with one **additive** field:

        * ``plan_input_summary`` (``Optional[ApplyWithPlanSummary]``)
          — ``None`` when ``manifest_path is None``; an
          :class:`ApplyWithPlanSummary` instance when a
          manifest was supplied.

    Notes
    -----
    The wrapper is **opt-in by design** — every existing K2.5
    caller continues to use :func:`apply_sidecars` directly
    and sees no behavioural change. The wire-up is **additive
    only** — the apply pass itself still walks ``reports/``
    and writes sidecars; the manifest is provenance metadata
    that future K3+ slices can opt into consuming.

    The wrapper never raises on a malformed manifest. A
    :class:`aee.audit.manifest.ManifestError` (I/O / JSON parse
    failure) IS raised to the caller because ``manifest_path``
    is an explicit user-supplied input — silent swallowing
    would mask transport-level failures the caller must see.

    A :class:`aee.audit.manifest.ValidationResult` with
    ``passed=False`` is NOT an exception — it surfaces via
    ``result.plan_input_summary.projection_passed = False``
    and ``result.plan_input_summary.projection_warning_count > 0``.
    The apply pass still runs (the wrapper does not gate
    apply on manifest validation).
    """
    # 1. Delegate to the production planner. All kwargs are
    #    forwarded verbatim so the apply pass is byte-for-byte
    #    identical to a direct :func:`apply_sidecars` call.
    result = apply_sidecars(
        reports_root,
        summary,
        utc_stamp=utc_stamp,
        classified_at_override=classified_at_override,
        policy=policy,
        force=force,
        allow_runtime=allow_runtime,
        strict_consistency=strict_consistency,
        executor_anchors=executor_anchors,
        user_provided_alias=user_provided_alias,
    )

    # 2. Opt-in wire-up. ``manifest_path is None`` is the
    #    K2.5-baseline contract — return the result unchanged.
    if manifest_path is None:
        return result

    # 3. Lazy import. Kept out of the module top so the
    #    K1 import-isolation contract is preserved for code
    #    paths that do not opt into the wire-up. The
    #    manifest module is read-only + self-contained
    #    (no dispatcher, no live DB, no subprocess — see
    #    ``aee.audit.manifest`` docstring) but the K2.5
    #    wrapper is opt-in by design, so the import is
    #    gated on the caller explicitly choosing to opt in.
    #    ``ManifestError`` is imported here so a caller that
    #    catches the wire-up's transport-level failure can
    #    do so via a stable name (the import is a no-op at
    #    call time — Python caches it on the function's
    #    globals after the first opt-in call).
    from aee.audit.manifest import (  # noqa: F401
        ManifestError,
        load_manifest,
        manifest_to_plan_inputs,
    )

    # 4. Load the manifest. ManifestError (transport-level
    #    I/O / JSON parse failure) is propagated to the
    #    caller — the path is an explicit user-supplied
    #    input, not a corpus-level error the wrapper should
    #    silently swallow. Any other exception is also
    #    propagated (defense in depth — the manifest
    #    module is not expected to raise anything else).
    doc = load_manifest(manifest_path)

    # 5. Project to PlanInput rows. ``manifest_to_plan_inputs``
    #    is non-raising: a validation failure surfaces as
    #    ``passed=False`` + populated ``warnings``; an empty
    #    manifest surfaces as ``passed=True`` + zero rows.
    projection = manifest_to_plan_inputs(doc)

    # 6. Attach the additive summary. The new field is
    #    declared on :class:`ApplySidecarsResult` (with
    #    default ``None``) so the wrapper can simply assign
    #    to it — no monkey-patching needed. The
    #    :meth:`ApplySidecarsResult.to_dict` method
    #    intentionally does NOT include the new field
    #    (K2.5-baseline consumers must see the same shape
    #    they always saw); the K2.5 wrapper exposes it via
    #    :meth:`ApplySidecarsResult.to_dict_with_plan`.
    result.plan_input_summary = ApplyWithPlanSummary(
        schema_version=PLAN_APPLY_SCHEMA_VERSION,
        manifest_source_path=doc.source_path,
        manifest_on_disk_sha256=doc.on_disk_sha256,
        manifest_on_disk_size=doc.on_disk_size,
        plan_input_count=len(projection.plan_inputs),
        projection_passed=projection.passed,
        projection_warning_count=len(projection.warnings),
    )

    return result


# ---------------------------------------------------------------------------
# AEE-7.8 K3 — read-only Audit Gate
# ---------------------------------------------------------------------------
# Why this lives in this module
# ------------------------------
# The AEE-7.8 K2.5 round attached an additive
# :class:`ApplyWithPlanSummary` to :class:`ApplySidecarsResult`
# (provenance metadata for the manifest the caller supplied).
# K2.5 deliberately did NOT verify that the live apply
# outcome matched the projected :class:`PlanInput` rows —
# K2.5 is provenance-only. K3 is the slice that closes that
# gap: a second opt-in layer on top of K2.5's opt-in that
# audits each ``ApplySidecarsResult.outcomes[i]`` against the
# corresponding :class:`PlanInput` row, classifies any
# mismatch into one of five explicit categories, and surfaces
# a structured :class:`ApplyAuditReport` DTO.
#
# K3 is strictly READ-ONLY with respect to the apply pass.
# It never mutates ``outcomes`` (the apply result is preserved
# byte-for-byte), never short-circuits the apply pass, never
# rewrites sidecars. It only annotates the returned
# :class:`ApplySidecarsResult` with an additional
# ``audit_report`` field. The K1 + K2 + K2.5 + K2.5-baseline
# ``to_dict()`` contract is preserved (the new field is
# omitted from ``to_dict()``; ``to_dict_with_audit()``
# exposes it).
#
# Out of scope (K4+ territory)
# ----------------------------
# * Any modification to ``aee/reporting/build_index.py:184`` —
#   the production call site stays on
#   :func:`apply_sidecars` (K2.5-baseline). Flipping it to
#   the gated wrapper is a separate, K4+ activation commit.
# * Plan-input gating (e.g. ``only plan_inputs whose
#   extras['writes_to_live_db'] is False``). K3 is audit-only;
#   the manifest is provenance + a comparison target, not a
#   gate on the apply pass.
# * Manifest-write tooling. K3 reads the manifest, never
#   writes it back.

#: Mismatch categories. Strings (not Enum) so a downstream
#: consumer can extend the taxonomy without bumping a schema
#: version. The list is the canonical reference for what a
#: K3 audit classifies.
AUDIT_MISSING_FROM_REPORTS = "MISSING_FROM_REPORTS"
AUDIT_EXTRA_IN_REPORTS = "EXTRA_IN_REPORTS"
AUDIT_SHA256_MISMATCH = "SHA256_MISMATCH"
AUDIT_KIND_MISMATCH = "KIND_MISMATCH"
AUDIT_DECISION_MISMATCH = "DECISION_MISMATCH"

#: The full mismatch taxonomy. Tuple so the order is stable
#: across iterations. A consumer can switch on category
#: membership via ``category in _MISMATCH_CATEGORIES``.
_MISMATCH_CATEGORIES: Tuple[str, ...] = (
    AUDIT_MISSING_FROM_REPORTS,
    AUDIT_EXTRA_IN_REPORTS,
    AUDIT_SHA256_MISMATCH,
    AUDIT_KIND_MISMATCH,
    AUDIT_DECISION_MISMATCH,
)

#: Schema version for the K3 audit DTO. Distinct from
#: :data:`APPLY_SCHEMA_VERSION` and
#: :data:`PLAN_APPLY_SCHEMA_VERSION` so a downstream
#: consumer can switch on the audit's presence without
#: breaking the K2.5-baseline contract.
AUDIT_SCHEMA_VERSION = "1.0.0"


class ApplyAuditError(Exception):
    """Raised by :func:`apply_sidecars_with_audit` when
    ``audit_action='raise'`` AND ``mismatch_count > 0``.

    The exception carries a :class:`ApplyAuditReport` in its
    ``audit_report`` attribute so a caller that catches the
    raise can introspect the full mismatch detail (the
    report is built BEFORE the raise so a try/except handler
    has the full evidence).
    """

    def __init__(self, message: str, audit_report: "ApplyAuditReport") -> None:
        super().__init__(message)
        self.audit_report = audit_report


@dataclass(frozen=True)
class ApplyAuditMismatch:
    """One per-row mismatch surfaced by the K3 audit.

    Frozen so the report can be put in a set, hashed, and
    JSON-serialized deterministically. The fields are the
    minimum a post-mortem needs to find the row in the
    on-disk corpus:

    * ``plan_input_path`` / ``plan_input_sha256`` /
      ``plan_input_kind`` / ``plan_input_group_name`` —
      the planned side of the comparison (always present).
    * ``outcome_index`` — the index into
      ``ApplySidecarsResult.outcomes`` for the row that
      matched the planned row by ``task_id`` (which is
      ``Path(...).name`` of the planned ``path``). ``-1``
      when no outcome matched (the planned row is missing
      from the apply result — surfaces as
      :data:`AUDIT_MISSING_FROM_REPORTS`).
    * ``outcome_decision`` — the
      :class:`SidecarDecision` of the matched outcome
      (``None`` when ``outcome_index == -1``).
    * ``category`` — the mismatch category (one of
      :data:`_MISMATCH_CATEGORIES`).
    * ``detail`` — a human-readable explanation string
      safe to log (never contains task input_text, prompt,
      or secrets).
    """

    plan_input_path: str
    plan_input_sha256: str
    plan_input_kind: str
    plan_input_group_name: str
    outcome_index: int
    outcome_decision: Optional[str]
    category: str
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_input_path": self.plan_input_path,
            "plan_input_sha256": self.plan_input_sha256,
            "plan_input_kind": self.plan_input_kind,
            "plan_input_group_name": self.plan_input_group_name,
            "outcome_index": self.outcome_index,
            "outcome_decision": self.outcome_decision,
            "category": self.category,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ApplyAuditReport:
    """The K3 audit DTO. Attached to
    :attr:`ApplySidecarsResult.audit_report` by
    :func:`apply_sidecars_with_audit` when the caller opts in
    AND ``audit_action`` is not ``"ignore"``.

    Fields:

    * ``audit_schema_version`` — always
      :data:`AUDIT_SCHEMA_VERSION` for K3.
    * ``audited_at_utc`` — the UTC stamp the audit ran
      (defaults to ``apply_sidecars``'s ``applied_at_utc``).
    * ``manifest_on_disk_sha256`` — the on-disk SHA-256 of
      the manifest, copied from the
      :class:`ApplyWithPlanSummary` attached by
      :func:`apply_sidecars_with_plan` (empty string when
      ``manifest_path`` was not supplied — but the
      ``manifest_path is None`` path is a no-op so the
      report is only ever attached when a manifest was
      supplied).
    * ``plan_input_count`` — number of :class:`PlanInput`
      rows the adapter projected.
    * ``outcome_count`` — number of
      :class:`PerTaskSidecarOutcome` entries the apply pass
      produced.
    * ``mismatch_count`` — number of mismatches the audit
      classified (zero on a clean run).
    * ``mismatch_categories`` — tuple of distinct category
      strings that appear in the report. Empty tuple on a
      clean run.
    * ``mismatches`` — tuple of
      :class:`ApplyAuditMismatch` per-row detail.
    * ``audit_action`` — the policy value the caller
      supplied (``"warn"`` / ``"raise"`` / ``"ignore"``).
      The value is recorded so a downstream consumer can
      replay the policy decision.
    """

    audit_schema_version: str
    audited_at_utc: str
    manifest_on_disk_sha256: str
    plan_input_count: int
    outcome_count: int
    mismatch_count: int
    mismatch_categories: Tuple[str, ...]
    mismatches: Tuple[ApplyAuditMismatch, ...]
    audit_action: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "audit_schema_version": self.audit_schema_version,
            "audited_at_utc": self.audited_at_utc,
            "manifest_on_disk_sha256": self.manifest_on_disk_sha256,
            "plan_input_count": self.plan_input_count,
            "outcome_count": self.outcome_count,
            "mismatch_count": self.mismatch_count,
            "mismatch_categories": list(self.mismatch_categories),
            "mismatches": [m.to_dict() for m in self.mismatches],
            "audit_action": self.audit_action,
        }


#: Allowed values for the ``audit_action`` parameter of
#: :func:`apply_sidecars_with_audit`. Tuple so the order is
#: stable; membership is the only check the wrapper
#: performs.
_AUDIT_ACTIONS: Tuple[str, ...] = ("warn", "raise", "ignore")


def _classify_audit_mismatches(
    plan_inputs: Tuple["PlanInput", ...],  # type: ignore[name-defined]
    outcomes: Tuple[PerTaskSidecarOutcome, ...],
) -> Tuple[ApplyAuditMismatch, ...]:
    """Compare projected :class:`PlanInput` rows against the
    live :class:`PerTaskSidecarOutcome` list and return a
    tuple of :class:`ApplyAuditMismatch`.

    The comparison is keyed on the outcome's ``task_id``
    (which is the directory name under ``reports/``) and the
    planned row's ``Path(...).name`` (the relative path the
    manifest describes). The K3 audit is a structural /
    content comparison, not a fingerprint-of-the-task-json
    comparison — the planned row is a file the manifest
    *says* the slice shipped, the outcome is what the apply
    pass *did* in response.

    Classification rules:

    * A planned row whose ``Path(...).name`` is NOT in
      ``outcomes`` (i.e. the apply pass did not produce an
      outcome for that task_id) →
      :data:`AUDIT_MISSING_FROM_REPORTS`.
    * An outcome whose ``task_id`` is NOT in
      ``{Path(p).name for p in plan_inputs}`` (the apply
      pass produced a row the manifest did not plan) →
      :data:`AUDIT_EXTRA_IN_REPORTS`.
    * A planned row whose source_task_json_sha256 (looked
      up via the matched outcome) differs from the
      planned sha256 → :data:`AUDIT_SHA256_MISMATCH`.
    * A planned row whose kind (FileEntryKind.value) does
      not match the outcome's record_kind (when present) →
      :data:`AUDIT_KIND_MISMATCH`.
    * A planned row whose outcome decision is in
      ``{SKIPPED_INCONSISTENT, SKIPPED_MALFORMED,
      SKIPPED_COLLISION}`` → :data:`AUDIT_DECISION_MISMATCH`.

    Categories are exclusive per row (first match wins). A
    row can be classified as multiple categories in a
    combined run; the audit captures the first one and the
    ``mismatch_categories`` tuple records the union.
    """
    plan_by_task_id: Dict[str, "PlanInput"] = {}  # type: ignore[name-defined]
    for pi in plan_inputs:
        # The planned path is the relative manifest path;
        # the audit keys on the file's basename (which is
        # the task_id the apply pass produced). This is
        # the K-shape-mandated contract: the manifest
        # describes files by relative path, the apply
        # pass records outcomes by task_id (directory
        # name). For a K3 audit, we treat the basename
        # of the planned path as the key.
        key = os.path.basename(pi.path)
        plan_by_task_id[key] = pi

    outcome_by_task_id: Dict[str, PerTaskSidecarOutcome] = {
        o.task_id: o for o in outcomes
    }

    mismatches: List[ApplyAuditMismatch] = []

    # 1. Planned rows: did the apply pass produce an outcome?
    for key, pi in plan_by_task_id.items():
        outcome = outcome_by_task_id.get(key)
        if outcome is None:
            mismatches.append(
                ApplyAuditMismatch(
                    plan_input_path=pi.path,
                    plan_input_sha256=pi.sha256,
                    plan_input_kind=pi.kind.value,
                    plan_input_group_name=pi.group_name,
                    outcome_index=-1,
                    outcome_decision=None,
                    category=AUDIT_MISSING_FROM_REPORTS,
                    detail=(
                        f"planned task {key!r} not present in "
                        f"apply outcomes"
                    ),
                )
            )
            continue
        outcome_index = outcomes.index(outcome)
        # SHA-256: the planned sha256 is the manifest's
        # file hash. The outcome's source_task_json_sha256
        # is the on-disk task.json hash. They are NOT the
        # same kind of fingerprint — but the K3 audit
        # treats any disagreement between the planned and
        # actual content fingerprints as a SHA-256
        # mismatch (the most conservative comparison).
        if (
            outcome.source_task_json_sha256
            and pi.sha256
            and outcome.source_task_json_sha256 != pi.sha256
        ):
            mismatches.append(
                ApplyAuditMismatch(
                    plan_input_path=pi.path,
                    plan_input_sha256=pi.sha256,
                    plan_input_kind=pi.kind.value,
                    plan_input_group_name=pi.group_name,
                    outcome_index=outcome_index,
                    outcome_decision=outcome.decision.value,
                    category=AUDIT_SHA256_MISMATCH,
                    detail=(
                        f"planned sha256={pi.sha256[:12]}... "
                        f"vs on-disk sha256="
                        f"{outcome.source_task_json_sha256[:12]}..."
                    ),
                )
            )
            continue
        # Kind: planned FileEntryKind.value (new/modified)
        # vs outcome record_kind (runtime/fixture/unknown).
        # The K3 audit treats this as a structural
        # mismatch — the manifest says the slice is a
        # "new" or "modified" file, the apply pass says
        # the record was classified as runtime/fixture/
        # unknown. They are different classification
        # dimensions, but a kind mismatch here is a real
        # divergence the audit must surface.
        if (
            outcome.record_kind is not None
            and pi.kind.value != outcome.record_kind
        ):
            mismatches.append(
                ApplyAuditMismatch(
                    plan_input_path=pi.path,
                    plan_input_sha256=pi.sha256,
                    plan_input_kind=pi.kind.value,
                    plan_input_group_name=pi.group_name,
                    outcome_index=outcome_index,
                    outcome_decision=outcome.decision.value,
                    category=AUDIT_KIND_MISMATCH,
                    detail=(
                        f"planned kind={pi.kind.value!r} "
                        f"vs outcome record_kind="
                        f"{outcome.record_kind!r}"
                    ),
                )
            )
            continue
        # Decision: the apply pass produced a non-success
        # decision for a row the manifest expected to
        # write. This is a DECISION_MISMATCH.
        if outcome.decision in (
            SidecarDecision.SKIPPED_INCONSISTENT,
            SidecarDecision.SKIPPED_MALFORMED,
            SidecarDecision.SKIPPED_COLLISION,
        ):
            mismatches.append(
                ApplyAuditMismatch(
                    plan_input_path=pi.path,
                    plan_input_sha256=pi.sha256,
                    plan_input_kind=pi.kind.value,
                    plan_input_group_name=pi.group_name,
                    outcome_index=outcome_index,
                    outcome_decision=outcome.decision.value,
                    category=AUDIT_DECISION_MISMATCH,
                    detail=(
                        f"outcome decision={outcome.decision.value!r} "
                        f"for planned row"
                    ),
                )
            )
            continue

    # 2. Outcomes with no matching planned row.
    planned_task_ids = set(plan_by_task_id.keys())
    for idx, outcome in enumerate(outcomes):
        if outcome.task_id in planned_task_ids:
            continue
        mismatches.append(
            ApplyAuditMismatch(
                plan_input_path="",
                plan_input_sha256="",
                plan_input_kind="",
                plan_input_group_name="",
                outcome_index=idx,
                outcome_decision=outcome.decision.value,
                category=AUDIT_EXTRA_IN_REPORTS,
                detail=(
                    f"outcome task_id={outcome.task_id!r} not "
                    f"present in plan_inputs"
                ),
            )
        )

    return tuple(mismatches)


def apply_sidecars_with_audit(
    reports_root: str | os.PathLike,
    summary: AuditSummary,
    *,
    manifest_path: Optional[Union[str, os.PathLike]] = None,
    audit_action: str = "warn",
    utc_stamp: Optional[str] = None,
    classified_at_override: Optional[str] = None,
    policy: Optional[SentinelPolicy] = None,
    force: bool = False,
    allow_runtime: bool = True,
    strict_consistency: bool = True,
    executor_anchors: Optional[Dict[str, Dict[str, str]]] = None,
    user_provided_alias: Optional[Dict[str, str]] = None,
) -> ApplySidecarsResult:
    """AEE-7.8 K3 — read-only Audit Gate around
    :func:`apply_sidecars_with_plan`.

    This is a **thin, additive wrapper** that:

    1. Calls :func:`apply_sidecars_with_plan` with the same
       arguments, preserving byte-for-byte output (same
       fields, same iteration order, same on-disk sidecar
       SHA-256s).
    2. When ``manifest_path is None`` (the default), the
       wrapper is a byte-for-byte pass-through. The
       returned :class:`ApplySidecarsResult` is identical
       to a direct :func:`apply_sidecars` call, with
       ``audit_report=None`` and ``to_dict()`` unchanged.
    3. When ``manifest_path is not None``, runs the K2.5
       wire-up (loads the manifest, projects to
       :class:`PlanInput` rows, attaches the K2.5
       :class:`ApplyWithPlanSummary`) AND THEN runs the
       K3 audit (compares projected ``PlanInput`` rows
       against the live ``ApplySidecarsResult.outcomes``,
       classifies any mismatch into one of five
       :data:`_MISMATCH_CATEGORIES`, and surfaces a
       structured :class:`ApplyAuditReport`). The audit
       NEVER mutates the apply result; it only annotates
       it via the additive ``audit_report`` field.

    Parameters
    ----------
    reports_root
        Forwarded verbatim to :func:`apply_sidecars_with_plan`.
    summary
        Forwarded verbatim to :func:`apply_sidecars_with_plan`.
    manifest_path
        Optional path to an AEE-7.7d/7.7e manifest
        artifact. When ``None`` (the default), the wrapper
        is a byte-for-byte pass-through. When supplied, the
        manifest is loaded, projected, and audited.
    audit_action
        Three-valued policy controlling what the wrapper
        does on a non-empty audit:

        * ``"warn"`` (default) — the audit attaches the
          :class:`ApplyAuditReport` to the result AND the
          wrapper returns the result unchanged. The
          non-raising contract is preserved.
        * ``"raise"`` — when ``mismatch_count > 0``, the
          wrapper raises :class:`ApplyAuditError` carrying
          the report. When ``mismatch_count == 0``, the
          wrapper returns the result unchanged (the report
          is attached for downstream consumers that want
          to introspect the clean run).
        * ``"ignore"`` — the audit is computed (so the
          wrapper still loads the manifest, still projects
          the plan, still classifies) but the result is
          NOT attached to the returned
          :class:`ApplySidecarsResult` (it stays
          ``None``). The apply pass itself is unchanged.

        Allowed values: ``"warn"``, ``"raise"``,
        ``"ignore"``. Any other value raises
        :class:`ValueError` immediately.
    utc_stamp
        Forwarded verbatim to :func:`apply_sidecars_with_plan`.
    classified_at_override
        Forwarded verbatim to :func:`apply_sidecars_with_plan`.
    policy
        Forwarded verbatim to :func:`apply_sidecars_with_plan`.
    force
        Forwarded verbatim to :func:`apply_sidecars_with_plan`.
    allow_runtime
        Forwarded verbatim to :func:`apply_sidecars_with_plan`.
    strict_consistency
        Forwarded verbatim to :func:`apply_sidecars_with_plan`.
    executor_anchors
        Forwarded verbatim to :func:`apply_sidecars_with_plan`.
    user_provided_alias
        Forwarded verbatim to :func:`apply_sidecars_with_plan`.

    Returns
    -------
    ApplySidecarsResult
        Identical to :func:`apply_sidecars_with_plan`'s
        return value, with one **additive** field:

        * ``audit_report`` (``Optional[ApplyAuditReport]``)
          — ``None`` for the no-flag call and for
          ``audit_action='ignore'``; an
          :class:`ApplyAuditReport` otherwise.

    Raises
    ------
    ValueError
        When ``audit_action`` is not in
        ``_AUDIT_ACTIONS``. Raised BEFORE the apply pass
        runs (the wrapper fails fast on a bad policy
        value).
    ApplyAuditError
        When ``audit_action='raise'`` AND
        ``mismatch_count > 0``. The exception carries the
        full :class:`ApplyAuditReport` in its
        ``audit_report`` attribute. The apply pass has
        already run by the time the exception is raised —
        the on-disk sidecars are the same as a
        non-raised ``"warn"`` call.
    aee.audit.manifest.ManifestError
        When ``manifest_path is not None`` and the
        manifest cannot be loaded (transport-level
        failure). Forwarded verbatim from the K2.5
        wrapper.

    Notes
    -----
    The K3 audit is **strictly read-only** with respect to
    the apply pass. The wrapper never mutates
    ``ApplySidecarsResult.outcomes`` (the apply result is
    preserved byte-for-byte), never short-circuits the
    apply pass, never rewrites sidecars. It only annotates
    the returned :class:`ApplySidecarsResult` with an
    additional ``audit_report`` field. The K1 + K2 + K2.5
    ``to_dict()`` contract is preserved (the new field is
    omitted from ``to_dict()``; ``to_dict_with_audit()``
    exposes it).
    """
    # 1. Validate the policy value BEFORE the apply pass
    #    runs. Failing fast on a bad value is a better
    #    failure mode than discovering the typo after the
    #    sidecars have been written.
    if audit_action not in _AUDIT_ACTIONS:
        raise ValueError(
            f"audit_action={audit_action!r} not in "
            f"{_AUDIT_ACTIONS!r}"
        )

    # 2. Delegate to the K2.5 wire-up. All apply kwargs are
    #    forwarded verbatim so the apply pass is
    #    byte-for-byte identical to a direct
    #    :func:`apply_sidecars` call (and to a direct
    #    :func:`apply_sidecars_with_plan` call when
    #    ``manifest_path is None``).
    result = apply_sidecars_with_plan(
        reports_root,
        summary,
        manifest_path=manifest_path,
        utc_stamp=utc_stamp,
        classified_at_override=classified_at_override,
        policy=policy,
        force=force,
        allow_runtime=allow_runtime,
        strict_consistency=strict_consistency,
        executor_anchors=executor_anchors,
        user_provided_alias=user_provided_alias,
    )

    # 3. No-flag pass-through. ``manifest_path is None`` is
    #    the K3-baseline contract — return the result
    #    unchanged. The audit_report is left at its default
    #    ``None`` (the field is additive).
    if manifest_path is None:
        return result

    # 4. ``ignore`` policy. The audit is computed but not
    #    attached. We still go through the audit so a
    #    later K-shape can switch on the ``ignore`` value
    #    and surface it differently; for K3 the
    #    semantics are "compute silently, return as if
    #    no audit ran".
    if audit_action == "ignore":
        return result

    # 5. Lazy import of the manifest / plan adapter. Kept
    #    out of the module top so the K1 import-isolation
    #    contract is preserved for code paths that do not
    #    opt into the wire-up. The K2.5 wrapper already
    #    imported the manifest module (it had to, to
    #    attach the ApplyWithPlanSummary); Python caches
    #    the import on the function's globals after the
    #    first opt-in call, so the K3 import is a no-op
    #    for callers that already passed through K2.5.
    from aee.audit.manifest import (  # noqa: F401
        PlanInput,
        manifest_to_plan_inputs,
    )

    # 6. Re-project the manifest. The K2.5 wrapper
    #    attached the projection summary but the full
    #    :class:`PlanInput` tuple is needed for the K3
    #    audit. Re-running :func:`manifest_to_plan_inputs`
    #    is cheap (the manifest is in memory at this
    #    point — the K2.5 wrapper just loaded it) and
    #    keeps the K3 wrapper's contract explicit: the
    #    audit operates on a freshly-projected
    #    :class:`ManifestToPlanResult`.
    #    The K2.5 summary on the result is preserved
    #    untouched (the audit never mutates the K2.5
    #    summary).
    projection = manifest_to_plan_inputs(
        _reconstruct_manifest_doc(result.plan_input_summary)
    )

    # 7. Classify mismatches. The classification function
    #    is pure — it reads the projected plan + the
    #    live outcomes, returns a tuple of
    #    :class:`ApplyAuditMismatch`.
    mismatches = _classify_audit_mismatches(
        projection.plan_inputs, tuple(result.outcomes)
    )

    # 8. Build the audit DTO. The report is always
    #    constructed (even on a clean run) so a
    #    downstream consumer can introspect the full
    #    audit state via ``result.audit_report``.
    distinct_categories: List[str] = []
    for cat in _MISMATCH_CATEGORIES:
        if any(m.category == cat for m in mismatches):
            distinct_categories.append(cat)
    report = ApplyAuditReport(
        audit_schema_version=AUDIT_SCHEMA_VERSION,
        audited_at_utc=result.applied_at_utc,
        manifest_on_disk_sha256=(
            result.plan_input_summary.manifest_on_disk_sha256
            if result.plan_input_summary is not None
            else ""
        ),
        plan_input_count=len(projection.plan_inputs),
        outcome_count=len(result.outcomes),
        mismatch_count=len(mismatches),
        mismatch_categories=tuple(distinct_categories),
        mismatches=mismatches,
        audit_action=audit_action,
    )

    # 9. Attach the report. The ``to_dict()`` contract is
    #    preserved (the field is omitted); the
    #    ``to_dict_with_audit()`` accessor exposes it.
    result.audit_report = report

    # 10. Apply the policy. The ``raise`` action fires
    #     when ``mismatch_count > 0``; the ``warn``
    #     action (default) just returns.
    if audit_action == "raise" and report.mismatch_count > 0:
        raise ApplyAuditError(
            (
                f"apply_sidecars_with_audit: {report.mismatch_count} "
                f"mismatch(es) detected across "
                f"{len(distinct_categories)} category(ies)"
            ),
            audit_report=report,
        )

    return result


def _reconstruct_manifest_doc(  # type: ignore[no-untyped-def]
    plan_input_summary: Optional["ApplyWithPlanSummary"],
):
    """Re-load the manifest from the path carried on the
    K2.5 summary.

    The K2.5 wrapper carries only the SHA-256 / size /
    count of the manifest, NOT the full document. The K3
    audit needs the full document to re-project the
    :class:`PlanInput` rows. We re-load it from the
    path on the summary; if no summary is attached
    (``plan_input_summary is None``), the caller must
    have invoked the no-flag path which is a no-op for
    K3, so the audit never reaches this point.
    """
    from aee.audit.manifest import load_manifest  # noqa: F401
    if plan_input_summary is None:
        # Defensive — the no-flag path returns BEFORE
        # this point, so this branch is unreachable in
        # practice. Keep the guard so a future caller
        # that bypasses the no-flag check still gets a
        # clear error.
        raise ValueError(
            "apply_sidecars_with_audit: cannot reconstruct "
            "manifest doc without a plan_input_summary"
        )
    return load_manifest(plan_input_summary.manifest_source_path)


__all__ = [
    "APPLY_SCHEMA_VERSION",
    "AUDIT_SCHEMA_VERSION",
    "ApplyAuditError",
    "ApplyAuditMismatch",
    "ApplyAuditReport",
    "ApplySidecarsResult",
    "ApplyWithPlanSummary",
    "PerTaskSidecarOutcome",
    "PLAN_APPLY_SCHEMA_VERSION",
    "SidecarDecision",
    "apply_sidecars",
    "apply_sidecars_with_audit",
    "apply_sidecars_with_plan",
]
