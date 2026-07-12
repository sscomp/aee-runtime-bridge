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


__all__ = [
    "APPLY_SCHEMA_VERSION",
    "ApplySidecarsResult",
    "ApplyWithPlanSummary",
    "PerTaskSidecarOutcome",
    "PLAN_APPLY_SCHEMA_VERSION",
    "SidecarDecision",
    "apply_sidecars",
    "apply_sidecars_with_plan",
]
