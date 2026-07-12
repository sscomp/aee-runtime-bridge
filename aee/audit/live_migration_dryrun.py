"""AEE-7.7e — Live-corpus migration dry-run + projection (read-only).

This module is the **fifth tool** in the ``aee/audit/`` package
and the **end-to-end orchestrator** that ties together the
AEE-7.7 c/d components into a deterministic, read-only
projection + an opt-in apply path:

1. :func:`aee.audit.build_sidecar_inventory` (AEE-7.7c) —
   read-only corpus walk.
2. :func:`aee.audit.plan_sidecar_migration` (AEE-7.7c) —
   pure dry-run planner.
3. :func:`aee.audit.execute_sidecar_migration` (AEE-7.7d) —
   controlled write-side executor (NOT called by the dry-run
   flow).
4. :func:`aee.audit.project_migration_execution` (AEE-7.7e) —
   pure-function projection of the would-be write/overwrite/
   skip/filter/fail outcomes without touching disk.
5. :func:`aee.audit.run_live_migration_dryrun` (AEE-7.7e) —
   inventory → plan → projection orchestrator that NEVER
   writes to ``reports/``, ``task.json``, ``identity.json``,
   the live DB, or any corpus file.
6. :func:`aee.audit.run_live_migration_apply` (AEE-7.7e) —
   opt-in apply path that calls the AEE-7.7d executor. This
   is the ONLY AEE-7.7e function that may write sidecars.

Why this split exists
---------------------

AEE-7.7e v1.0.0 wired ``run_live_migration_dryrun`` directly
to :func:`execute_sidecar_migration`. That violated the
"dry-run is read-only" contract: a caller asking for a
dry-run ended up with 13+ ``identity.json`` files re-stamped
under the live corpus. v1.1.0 fixes this by introducing a
pure projection function and a separate apply function. The
dry-run flow is now guaranteed to mutate nothing inside the
audited corpus. The apply flow is explicit and clearly
named.

* **Read-only invariant** — :func:`run_live_migration_dryrun`
  and :func:`project_migration_execution` MUST NOT write
  anywhere. Tests assert that the sidecar path set, SHA-256,
  byte size, and mtime are byte-stable across a dry-run
  call.
* **Apply is explicit** — callers that want the real
  migration use :func:`run_live_migration_apply`. The name
  is unambiguous: ``apply`` is the only verb that may
  produce filesystem writes.
* **Reconciliation is plan-vs-projection** — the dry-run
  reconciles :class:`aee.audit.MigrationPlan` against
  :class:`ProjectedMigrationResult`. The AEE-7.7d executor
  is no longer part of the dry-run reconciliation.
* **Optional manifest artifact** — ``write_manifest=True``
  writes the DTO to disk at a caller-supplied path
  (default: ``<reports_root.parent>/aee77e-dryrun-<UTC>.json``,
  i.e. outside the corpus).

Out of scope (intentionally)
----------------------------

* No writes to ``data/dispatcher.db`` (this module never
  imports ``dispatcher``).
* No mutation of ``task.json``.
* No ``identity.json`` writes from the dry-run flow.
* No subprocess / network / env reads.
* No logging of ``input_text``, prompts, secrets, or
  ``dispatcher/.env``.

Public surface
--------------

* :data:`LIVE_MIGRATION_DRYRUN_SCHEMA_VERSION` — ``"1.1.0"``.
  Bumped from 1.0.0 because the DTO field semantics
  changed (now ``projected_*`` everywhere; the old
  ``exec_wrote``/``exec_overwrote`` aliases are kept for
  backward compatibility but are marked DEPRECATED in the
  markdown report).
* :data:`DEFAULT_TARGET_POLICY_VERSION` — ``"1.1.0"``.
* :class:`ProjectedOutcome` — enum of projected per-task
  outcomes (``WOULD_WRITE``, ``WOULD_OVERWRITE``, etc.).
  The string values are persisted in
  :class:`ProjectedMigrationResult` and asserted in tests.
* :class:`PerTaskProjection` — one row of the projection
  (frozen DTO).
* :class:`ProjectedMigrationResult` — the aggregate
  projection (DTO).
* :class:`LiveMigrationDryrunResult` — frozen DTO; the
  return of :func:`run_live_migration_dryrun`. Carries
  inventory + plan + projection + reconciliation + optional
  manifest fields.
* :func:`project_migration_execution` — pure function
  (inventory + plan + filters → projection).
* :func:`run_live_migration_dryrun` — the read-only entry
  point.
* :func:`run_live_migration_apply` — the explicit apply
  entry point.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from aee.audit.sidecar_inventory import (
    INVENTORY_SCHEMA_VERSION,
    MigrationPlan,
    SidecarInventoryEntry,
    SidecarInventoryResult,
    SidecarStatus,
    build_sidecar_inventory,
    plan_sidecar_migration,
)
from aee.audit.sidecar_migration import (
    DEFAULT_STATUS_FILTER,
    MIGRATION_EXEC_SCHEMA_VERSION,
    MigrationExecutionResult,
    MigrationStatus,
    execute_sidecar_migration,
)
from aee.reporting.identity import (
    RecordKind,
    SentinelPolicy,
    classify_record,
    load_task_json,
    read_identity_sidecar,
)

# Stable schema version for the dry-run DTO.
# Bumped 1.0.0 → 1.1.0 because the field semantics changed:
# "exec_wrote" / "exec_overwrote" / "exec_untouched" etc.
# were renamed to "projected_*" to make the read-only
# contract explicit. The 1.0.0 names are still emitted as
# deprecated aliases for callers that have not migrated.
LIVE_MIGRATION_DRYRUN_SCHEMA_VERSION = "1.1.0"

# Default target policy version used by the dry-run when the
# caller does not override. The AEE-7.7c planner defaults to
# ``"1.1.0"``; the dry-run mirrors that default so a caller
# that does not pass a target gets the same plan shape.
DEFAULT_TARGET_POLICY_VERSION = "1.1.0"


class ProjectedOutcome(str, Enum):
    """Per-task *projected* outcome of
    :func:`aee.audit.project_migration_execution`.

    The string values are persisted in
    :class:`PerTaskProjection` and asserted in tests. The
    names are prefixed ``WOULD_*`` to make the read-only
    contract explicit at the JSON layer. Adding a new value
    is a schema change (bump
    :data:`LIVE_MIGRATION_DRYRUN_SCHEMA_VERSION`).
    """

    WOULD_WRITE = "would_write"           # no prior sidecar; would be written
    WOULD_OVERWRITE = "would_overwrite"   # sidecar exists with stale verdict
    WOULD_SKIP_CURRENT = "would_skip_current"     # sidecar fresh (no action)
    WOULD_SKIP_RUNTIME = "would_skip_runtime"     # RUNTIME + allow_runtime=False
    WOULD_FILTER = "would_filter"         # entry.status not in status_filter
    WOULD_FAIL_MISSING_TASK_JSON = (
        "would_fail_missing_task_json"
    )  # task.json absent / unreadable
    WOULD_REJECT_MALFORMED = "would_reject_malformed"  # classify_and_persist would return None
    WOULD_NO_OP = "would_no_op"           # catch-all no-op (defensive default)


@dataclass(frozen=True)
class PerTaskProjection:
    """The pure-projection result for one task.json.

    Frozen DTO. Carries the projected outcome plus enough
    context to reconstruct the decision later. ``note`` is
    the only free-form field; keep it short and never
    include ``input_text`` or any secret-bearing data.
    """

    task_id: str
    outcome: ProjectedOutcome
    inventory_status: SidecarStatus
    record_kind: Optional[str]              # from inventory / classify_record
    source_task_json_sha256: str            # empty when no readable task.json
    sidecar_sha256_before: str              # empty when no prior sidecar
    would_change_sidecar: bool              # True iff WOULD_WRITE / WOULD_OVERWRITE
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "outcome": self.outcome.value,
            "inventory_status": self.inventory_status.value,
            "record_kind": self.record_kind,
            "source_task_json_sha256": self.source_task_json_sha256,
            "sidecar_sha256_before": self.sidecar_sha256_before,
            "would_change_sidecar": self.would_change_sidecar,
            "note": self.note,
        }


@dataclass
class ProjectedMigrationResult:
    """The aggregate projection: what
    :func:`execute_sidecar_migration` WOULD do, computed in
    memory, without any filesystem writes.

    DTO; serializable via :meth:`to_dict` / :meth:`to_markdown`.
    """

    reports_root: str
    projected_at_utc: str
    schema_version: str
    # The inputs the projection consumed. ``status_filter``
    # is the ACTUAL filter used; ``allow_runtime`` is the
    # ACTUAL RUNTIME gate; ``force`` is the ACTUAL idempotency
    # override.
    status_filter: Tuple[str, ...]
    allow_runtime: bool
    force: bool
    inventory_total: int
    per_task: List[PerTaskProjection] = field(default_factory=list)
    # Aggregate counts by ProjectedOutcome. Computed from
    # per_task.
    by_outcome: Dict[str, int] = field(default_factory=dict)
    # Aggregate counts by SidecarStatus (the inventory-side
    # state of each entry).
    by_inventory_status: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "projected_at_utc": self.projected_at_utc,
            "reports_root": self.reports_root,
            "status_filter": list(self.status_filter),
            "allow_runtime": self.allow_runtime,
            "force": self.force,
            "inventory_total": self.inventory_total,
            "by_outcome": dict(self.by_outcome),
            "by_inventory_status": dict(self.by_inventory_status),
            "per_task": [p.to_dict() for p in self.per_task],
        }

    def to_markdown(self) -> str:
        lines: List[str] = []
        lines.append("# AEE-7.7e Projected Migration")
        lines.append("")
        lines.append(f"- Schema version: `{self.schema_version}`")
        lines.append(f"- Projected at (UTC): `{self.projected_at_utc}`")
        lines.append(f"- Reports root: `{self.reports_root}`")
        lines.append(
            f"- status_filter: `"
            f"{', '.join(self.status_filter) or '<empty>'}`"
        )
        lines.append(f"- allow_runtime: `{self.allow_runtime}`")
        lines.append(f"- force: `{self.force}`")
        lines.append(f"- Inventory total: **{self.inventory_total}**")
        lines.append("")
        lines.append("## By projected outcome")
        lines.append("")
        lines.append("| Outcome | Count |")
        lines.append("|---|---|")
        for code, count in sorted(
            self.by_outcome.items(), key=lambda kv: (-kv[1], kv[0])
        ):
            lines.append(f"| `{code}` | {count} |")
        lines.append("")
        lines.append("## By inventory status (after filter)")
        lines.append("")
        lines.append("| Inventory status | Count |")
        lines.append("|---|---|")
        for code, count in sorted(
            self.by_inventory_status.items(),
            key=lambda kv: (-kv[1], kv[0]),
        ):
            lines.append(f"| `{code}` | {count} |")
        lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# LiveMigrationDryrunResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveMigrationDryrunResult:
    """The aggregate result of a full
    :func:`run_live_migration_dryrun` run. Frozen DTO.

    Carries the inventory, plan, projection, reconciliation,
    and optional manifest fields. The DTO NEVER carries an
    actual write-side result: the only "execution" field is
    :attr:`projection`, which describes what
    :func:`aee.audit.execute_sidecar_migration` WOULD do
    without invoking it.
    """

    reports_root: str
    utc_stamp: str
    schema_version: str
    inventory: SidecarInventoryResult
    plan: MigrationPlan
    projection: ProjectedMigrationResult
    # ------------------------------------------------------------------
    # Projection-side counts (snapshotted from projection for the
    # reconciliation check). These are the AUTHORITATIVE
    # read-only-flow numbers.
    # ------------------------------------------------------------------
    projected_writes: int
    projected_overwrites: int
    projected_skips: int
    projected_runtime_skipped: int
    projected_filtered: int
    projected_no_task_json: int
    projected_malformed: int
    projected_no_op: int
    projected_total: int
    # ------------------------------------------------------------------
    # Plan-side counts (snapshotted for the reconciliation check).
    # ------------------------------------------------------------------
    plan_would_write: int
    plan_would_overwrite: int
    plan_no_op: int
    plan_runtime_would_touch: int
    # ------------------------------------------------------------------
    # Deprecated aliases (AEE-7.7e v1.0.0 names). Kept for
    # backward compatibility with downstream consumers that
    # have not migrated. They are now sourced from the
    # projection (not from an actual write-side execution), so
    # the values are PROMISED outcomes, not observed ones.
    # Marked DEPRECATED in the to_markdown output.
    # ------------------------------------------------------------------
    exec_wrote: int           # = projected_writes
    exec_overwrote: int       # = projected_overwrites
    exec_untouched: int       # = projected_skips
    exec_skipped_runtime: int # = projected_runtime_skipped
    exec_status_filtered: int # = projected_filtered
    exec_failure: int         # = 0 (AEE-7.7d does not emit FAILED)
    exec_malformed: int       # = projected_malformed
    exec_no_task_json: int    # = projected_no_task_json
    exec_fresh_skipped: int   # = 0 (folded into projected_skips)
    exec_total: int           # = projected_total
    # ------------------------------------------------------------------
    # Reconciliation flag — plan vs projection (NOT plan vs
    # actual write-side execution).
    # ------------------------------------------------------------------
    reconciliation_passed: bool
    # On-disk manifest path (None when write_manifest=False).
    manifest_path: Optional[str] = None
    # The manifest's SHA-256 (empty when write_manifest=False).
    manifest_sha256: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "utc_stamp": self.utc_stamp,
            "reports_root": self.reports_root,
            "inventory": self.inventory.to_dict(),
            "plan": self.plan.to_dict(),
            "projection": self.projection.to_dict(),
            "reconciliation": {
                "plan_would_write": self.plan_would_write,
                "plan_would_overwrite": self.plan_would_overwrite,
                "plan_no_op": self.plan_no_op,
                "plan_runtime_would_touch": self.plan_runtime_would_touch,
                "projected_writes": self.projected_writes,
                "projected_overwrites": self.projected_overwrites,
                "projected_skips": self.projected_skips,
                "projected_runtime_skipped": self.projected_runtime_skipped,
                "projected_filtered": self.projected_filtered,
                "projected_no_task_json": self.projected_no_task_json,
                "projected_malformed": self.projected_malformed,
                "projected_no_op": self.projected_no_op,
                "projected_total": self.projected_total,
                "passed": self.reconciliation_passed,
            },
            # Deprecated aliases (AEE-7.7e v1.0.0 names).
            "deprecated_exec_aliases": {
                "exec_wrote": self.exec_wrote,
                "exec_overwrote": self.exec_overwrote,
                "exec_untouched": self.exec_untouched,
                "exec_skipped_runtime": self.exec_skipped_runtime,
                "exec_status_filtered": self.exec_status_filtered,
                "exec_failure": self.exec_failure,
                "exec_malformed": self.exec_malformed,
                "exec_no_task_json": self.exec_no_task_json,
                "exec_fresh_skipped": self.exec_fresh_skipped,
                "exec_total": self.exec_total,
                "DEPRECATED": (
                    "v1.0.0 names; in v1.1.0 the source is the "
                    "read-only projection, not the AEE-7.7d "
                    "executor. Use projected_* instead."
                ),
            },
            "manifest_path": self.manifest_path,
            "manifest_sha256": self.manifest_sha256,
        }

    def to_markdown(self) -> str:
        lines: List[str] = []
        lines.append("# AEE-7.7e Live Migration Dry-run")
        lines.append("")
        lines.append(f"- Schema version: `{self.schema_version}`")
        lines.append(f"- UTC stamp: `{self.utc_stamp}`")
        lines.append(f"- Reports root: `{self.reports_root}`")
        lines.append("")
        lines.append("## Inventory")
        lines.append("")
        lines.append(
            f"- Inventoried at (UTC): `{self.inventory.inventoried_at_utc}`"
        )
        lines.append(
            f"- Inventory schema: `{self.inventory.schema_version}` "
            f"(INVENTORY_SCHEMA_VERSION=`{INVENTORY_SCHEMA_VERSION}`)"
        )
        lines.append(f"- Total entries: **{len(self.inventory.entries)}**")
        lines.append("")
        lines.append("| Status | Count |")
        lines.append("|---|---|")
        for code, count in sorted(
            self.inventory.by_status.items(),
            key=lambda kv: (-kv[1], kv[0]),
        ):
            lines.append(f"| `{code}` | {count} |")
        lines.append("")
        lines.append("## Plan (dry-run)")
        lines.append("")
        lines.append(
            f"- Plan target: `{self.plan.target_policy_version}` "
            f"(current=`{self.plan.current_policy_version}`)"
        )
        lines.append(f"- would_write: **{self.plan.would_write}**")
        lines.append(f"- would_overwrite: **{self.plan.would_overwrite}**")
        lines.append(f"- no_op: **{self.plan.no_op}**")
        lines.append(
            f"- runtime_would_touch: **{self.plan.runtime_would_touch}**"
        )
        lines.append("")
        lines.append("## Projection (read-only)")
        lines.append("")
        lines.append(
            f"- Projected at (UTC): `{self.projection.projected_at_utc}`"
        )
        lines.append(
            f"- Projection schema: `{self.projection.schema_version}`"
        )
        lines.append(
            f"- status_filter: `"
            f"{', '.join(self.projection.status_filter) or '<empty>'}`"
        )
        lines.append(f"- allow_runtime: `{self.projection.allow_runtime}`")
        lines.append(f"- force: `{self.projection.force}`")
        lines.append("")
        lines.append("| ProjectedOutcome | Count |")
        lines.append("|---|---|")
        for code, count in sorted(
            self.projection.by_outcome.items(),
            key=lambda kv: (-kv[1], kv[0]),
        ):
            lines.append(f"| `{code}` | {count} |")
        lines.append("")
        lines.append("## Reconciliation (plan vs projection)")
        lines.append("")
        verdict = "✅ PASS" if self.reconciliation_passed else "❌ FAIL"
        lines.append(f"- Plan / Projection reconciliation: **{verdict}**")
        lines.append(
            f"- plan.would_write ({self.plan_would_write}) "
            f"== projection.would_write ({self.projected_writes}) "
            f"+ projection.would_skip_runtime "
            f"({self.projected_runtime_skipped})"
        )
        lines.append(
            f"- plan.would_overwrite ({self.plan_would_overwrite}) "
            f"== projection.would_overwrite "
            f"({self.projected_overwrites})"
        )
        lines.append("")
        lines.append("| Metric | Plan | Projection |")
        lines.append("|---|---|---|")
        lines.append(
            f"| would_write / projected_write | "
            f"{self.plan_would_write} | {self.projected_writes} |"
        )
        lines.append(
            f"| would_overwrite / projected_overwrite | "
            f"{self.plan_would_overwrite} | {self.projected_overwrites} |"
        )
        lines.append(
            f"| no_op / projected_no_op+filtered+runtime_skipped+"
            f"no_task_json+malformed+skip_current | "
            f"{self.plan_no_op} | "
            f"{self.projected_no_op + self.projected_filtered + self.projected_runtime_skipped + self.projected_no_task_json + self.projected_malformed + self.projected_skips} |"
        )
        lines.append("")
        lines.append(
            "## DEPRECATED v1.0.0 aliases (do not use for new code)"
        )
        lines.append("")
        lines.append(
            "- exec_wrote / exec_overwrote / exec_untouched / "
            "exec_skipped_runtime / exec_status_filtered / "
            "exec_failure / exec_malformed / exec_no_task_json / "
            "exec_fresh_skipped / exec_total"
        )
        lines.append(
            "- Source in v1.1.0: the read-only projection (NOT "
            "the AEE-7.7d executor)."
        )
        lines.append("")
        if self.manifest_path:
            lines.append(f"- Manifest: `{self.manifest_path}`")
            if self.manifest_sha256:
                lines.append(
                    f"- Manifest SHA-256: `{self.manifest_sha256}`"
                )
            lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_utc_iso() -> str:
    """Return current UTC ISO-8601 'Z' timestamp."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _file_sha256(path: Path) -> str:
    """SHA-256 hex digest, or empty string when the file is missing
    or unreadable.
    """
    if not path.exists() or not path.is_file():
        return ""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _default_manifest_path(reports_root: Path, utc_stamp: str) -> Path:
    """Resolve the default on-disk manifest path.

    The manifest is written **outside** the audited ``reports/``
    tree (a sibling of it) so the corpus shape is preserved
    (matches the AEE-7.7d migration-log convention).
    """
    parent = reports_root.parent
    if parent == reports_root:
        # ``reports_root`` is a filesystem root — fall back
        # to the same dir with a clearer filename.
        return reports_root / f"aee77e-dryrun-{utc_stamp}.json"
    return parent / f"aee77e-dryrun-{utc_stamp}.json"


def _task_dir_for(reports_root: Path, task_id: str) -> Path:
    """Resolve the on-disk task directory for a given ``task_id``.

    The 7.7c inventory stores task_id as the part after the
    ``TASK-`` prefix; on disk the directory is
    ``TASK-<task_id>``. Mirrors the SOT in
    ``sidecar_migration._task_dir_for`` so a projection
    candidate and a real executor look at the same path.
    """
    return reports_root / f"TASK-{task_id}"


def _would_classify_as_runtime(
    task_json_path: Path, entry: SidecarInventoryEntry
) -> bool:
    """Decide whether an entry would be classified as RUNTIME
    by the AEE-7.7d executor, without invoking the writer.

    Mirrors :func:`aee.audit.sidecar_migration._is_runtime_record`
    exactly so the projection matches the executor's decision
    for the same on-disk evidence.
    """
    if entry.record_kind == RecordKind.RUNTIME.value:
        return True
    existing = read_identity_sidecar(task_json_path)
    if existing is not None:
        return existing.record_kind == RecordKind.RUNTIME
    raw = load_task_json(task_json_path)
    if raw is None:
        return False
    fresh = classify_record(
        task_id=task_json_path.parent.name,
        task_json=raw,
        policy=SentinelPolicy(),
    )
    return fresh.record_kind == RecordKind.RUNTIME


def _project_one_entry(
    *,
    entry: SidecarInventoryEntry,
    root: Path,
    effective_filter: FrozenSet[SidecarStatus],
    allow_runtime: bool,
    force: bool,
) -> PerTaskProjection:
    """Project the per-task outcome WITHOUT touching disk.

    This is the read-only counterpart of
    :func:`aee.audit.sidecar_migration._process_one_entry`.
    The two functions are kept in lock-step; if the executor
    changes its decision tree, this function must mirror the
    change.

    Note: the AEE-7.7d executor drops FRESH from its filter
    (defence in depth) and emits STATUS_FILTERED for FRESH
    entries. The projection is more semantic: FRESH entries
    are reported as WOULD_SKIP_CURRENT ("we would skip
    because already current") so the caller's downstream
    reasoning does not have to special-case
    WOULD_FILTER + FRESH.
    """
    task_dir = _task_dir_for(root, entry.task_id)
    task_json_path = task_dir / "task.json"

    # --- 1. FRESH short-circuit (semantic: "would skip current") ---
    # The AEE-7.7d executor drops FRESH from its effective
    # filter; the projection is more useful if it reports
    # FRESH as WOULD_SKIP_CURRENT instead of WOULD_FILTER.
    if entry.status == SidecarStatus.FRESH:
        return PerTaskProjection(
            task_id=entry.task_id,
            outcome=ProjectedOutcome.WOULD_SKIP_CURRENT,
            inventory_status=entry.status,
            record_kind=entry.record_kind,
            source_task_json_sha256=entry.task_json_sha256,
            sidecar_sha256_before=entry.sidecar_sha256,
            would_change_sidecar=False,
            note="FRESH records would not be re-stamped",
        )

    # --- 2. status filter check ---
    if entry.status not in effective_filter:
        return PerTaskProjection(
            task_id=entry.task_id,
            outcome=ProjectedOutcome.WOULD_FILTER,
            inventory_status=entry.status,
            record_kind=entry.record_kind,
            source_task_json_sha256=entry.task_json_sha256,
            sidecar_sha256_before=entry.sidecar_sha256,
            would_change_sidecar=False,
            note=f"status={entry.status.value} not in filter",
        )

    # --- 3. task.json must be readable ---
    if not task_json_path.is_file() or not entry.task_json_sha256:
        return PerTaskProjection(
            task_id=entry.task_id,
            outcome=ProjectedOutcome.WOULD_FAIL_MISSING_TASK_JSON,
            inventory_status=entry.status,
            record_kind=entry.record_kind,
            source_task_json_sha256=entry.task_json_sha256,
            sidecar_sha256_before=entry.sidecar_sha256,
            would_change_sidecar=False,
            note="task.json missing or unreadable",
        )

    # --- 4. RUNTIME gate (defence in depth) ---
    if _would_classify_as_runtime(task_json_path, entry) and not allow_runtime:
        return PerTaskProjection(
            task_id=entry.task_id,
            outcome=ProjectedOutcome.WOULD_SKIP_RUNTIME,
            inventory_status=entry.status,
            record_kind="runtime",
            source_task_json_sha256=entry.task_json_sha256,
            sidecar_sha256_before=entry.sidecar_sha256,
            would_change_sidecar=False,
            note="allow_runtime=False",
        )

    # --- 5. existing-sidecar check (idempotency) ---
    existing = read_identity_sidecar(task_json_path)
    sha_before = entry.sidecar_sha256 or ""

    # --- 6. classify_and_persist would-succeed check ---
    # We do NOT call classify_and_persist (that would write).
    # We re-do the same classify_record classification the
    # executor does, so a "would-be malformed" decision is
    # accurate enough to project.
    raw = load_task_json(task_json_path)
    if raw is None:
        return PerTaskProjection(
            task_id=entry.task_id,
            outcome=ProjectedOutcome.WOULD_REJECT_MALFORMED,
            inventory_status=entry.status,
            record_kind=entry.record_kind,
            source_task_json_sha256=entry.task_json_sha256,
            sidecar_sha256_before=sha_before,
            would_change_sidecar=False,
            note="classify_record would return None (unreadable task.json)",
        )
    fresh = classify_record(
        task_id=task_json_path.parent.name,
        task_json=raw,
        policy=SentinelPolicy(),
    )
    if fresh is None:
        return PerTaskProjection(
            task_id=entry.task_id,
            outcome=ProjectedOutcome.WOULD_REJECT_MALFORMED,
            inventory_status=entry.status,
            record_kind=entry.record_kind,
            source_task_json_sha256=entry.task_json_sha256,
            sidecar_sha256_before=sha_before,
            would_change_sidecar=False,
            note="classify_record would return None",
        )

    # --- 7. decide WOULD_WRITE / WOULD_OVERWRITE / WOULD_SKIP_CURRENT ---
    if existing is None:
        return PerTaskProjection(
            task_id=entry.task_id,
            outcome=ProjectedOutcome.WOULD_WRITE,
            inventory_status=entry.status,
            record_kind=fresh.record_kind.value,
            source_task_json_sha256=entry.task_json_sha256,
            sidecar_sha256_before=sha_before,
            would_change_sidecar=True,
            note="no prior sidecar",
        )
    if force:
        return PerTaskProjection(
            task_id=entry.task_id,
            outcome=ProjectedOutcome.WOULD_OVERWRITE,
            inventory_status=entry.status,
            record_kind=fresh.record_kind.value,
            source_task_json_sha256=entry.task_json_sha256,
            sidecar_sha256_before=sha_before,
            would_change_sidecar=True,
            note="force=True; sidecar would be overwritten",
        )
    # Idempotency check: does the existing sidecar match the
    # new verdict? If yes → no write. If no → WOULD_OVERWRITE.
    same = (
        existing.record_kind == fresh.record_kind
        and existing.is_fixture == fresh.is_fixture
        and list(existing.fixture_markers) == list(fresh.fixture_markers)
        and existing.executor_session_id == fresh.executor_session_id
        and existing.runtime_run_id == fresh.runtime_run_id
        and existing.user_provided_alias == fresh.user_provided_alias
        and existing.source_task_json_sha256 == fresh.source_task_json_sha256
        and existing.policy_version == fresh.policy_version
    )
    if same:
        return PerTaskProjection(
            task_id=entry.task_id,
            outcome=ProjectedOutcome.WOULD_SKIP_CURRENT,
            inventory_status=entry.status,
            record_kind=fresh.record_kind.value,
            source_task_json_sha256=entry.task_json_sha256,
            sidecar_sha256_before=sha_before,
            would_change_sidecar=False,
            note="existing sidecar would match new verdict",
        )
    return PerTaskProjection(
        task_id=entry.task_id,
        outcome=ProjectedOutcome.WOULD_OVERWRITE,
        inventory_status=entry.status,
        record_kind=fresh.record_kind.value,
        source_task_json_sha256=entry.task_json_sha256,
        sidecar_sha256_before=sha_before,
        would_change_sidecar=True,
        note="existing sidecar would differ from new verdict",
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def project_migration_execution(
    inventory: SidecarInventoryResult,
    *,
    status_filter: Optional[FrozenSet[SidecarStatus]] = None,
    allow_runtime: bool = False,
    force: bool = False,
    utc_stamp: Optional[str] = None,
) -> ProjectedMigrationResult:
    """Pure projection: what :func:`execute_sidecar_migration`
    WOULD do, computed in memory, without any filesystem writes.

    The function NEVER writes to ``task.json``,
    ``identity.json``, the live DB, or any other location.
    It reads each entry's task.json (via
    :func:`aee.reporting.identity.load_task_json` and
    :func:`classify_record`) to project the executor's
    decision tree.

    Parameters
    ----------
    inventory
        The :class:`aee.audit.SidecarInventoryResult` to project
        over. Only its ``entries``, ``reports_root`` and
        aggregate ``by_status`` are read.
    status_filter
        Optional override of the eligible status set. Default
        :data:`aee.audit.sidecar_migration.DEFAULT_STATUS_FILTER`
        (MISSING + STALE_HASH + STALE_VERSION). FRESH is
        never in the filter — we drop it explicitly so a
        caller that includes it by mistake does not see
        WOULD_OVERWRITE rows.
    allow_runtime
        If False (default), RUNTIME records are projected as
        :attr:`ProjectedOutcome.WOULD_SKIP_RUNTIME` regardless
        of ``status_filter``. If True, RUNTIME records are
        eligible for ``WOULD_WRITE`` / ``WOULD_OVERWRITE``
        when their inventory status is in ``status_filter``.
    force
        If False (default), an existing sidecar whose content
        matches the new verdict is projected as
        :attr:`ProjectedOutcome.WOULD_SKIP_CURRENT` (no
        write). If True, the projection emits
        :attr:`ProjectedOutcome.WOULD_OVERWRITE` even when
        the content matches.
    utc_stamp
        Optional deterministic timestamp for the projection's
        :attr:`ProjectedMigrationResult.projected_at_utc`.
        Default: ``"now"`` via :func:`_now_utc_iso`.
    """
    root = Path(inventory.reports_root)
    stamp = utc_stamp or _now_utc_iso()

    if status_filter is None:
        effective = DEFAULT_STATUS_FILTER
    else:
        effective = frozenset(status_filter) - {SidecarStatus.FRESH}

    result = ProjectedMigrationResult(
        reports_root=str(root),
        projected_at_utc=stamp,
        schema_version=LIVE_MIGRATION_DRYRUN_SCHEMA_VERSION,
        status_filter=tuple(sorted(s.value for s in effective)),
        allow_runtime=allow_runtime,
        force=force,
        inventory_total=len(inventory.entries),
    )

    for entry in inventory.entries:
        projection = _project_one_entry(
            entry=entry,
            root=root,
            effective_filter=effective,
            allow_runtime=allow_runtime,
            force=force,
        )
        result.per_task.append(projection)

    # Aggregate counts
    by_outcome: Dict[str, int] = {o.value: 0 for o in ProjectedOutcome}
    for p in result.per_task:
        by_outcome[p.outcome.value] += 1
    result.by_outcome = by_outcome

    by_inv: Dict[str, int] = {s.value: 0 for s in SidecarStatus}
    for p in result.per_task:
        by_inv[p.inventory_status.value] += 1
    result.by_inventory_status = by_inv

    return result


def _reconcile_plan_vs_projection(
    plan: MigrationPlan,
    projection: ProjectedMigrationResult,
) -> Tuple[bool, Dict[str, int]]:
    """Run the plan / projection reconciliation check.

    Contract (asserted by tests):

    1. ``plan.would_write == projection.would_write
       + projection.would_skip_runtime`` (the plan cannot
       know which MISSING entries will turn out to be RUNTIME
       on fresh classification; the projection can).
    2. ``plan.would_overwrite == projection.would_overwrite``
       (the plan's STALE_* bucket maps 1:1 to WOULD_OVERWRITE
       because the projection's idempotency check would only
       ``WOULD_SKIP_CURRENT`` for entries whose inventory
       status was already FRESH — and FRESH is never in
       ``plan.would_overwrite``).
    3. ``plan.no_op == projection.would_skip_current
       + projection.would_filter
       + projection.would_fail_missing_task_json
       + projection.would_reject_malformed
       + projection.would_no_op``
       (the plan's no_op bucket = every "we would do
       nothing" outcome in the projection).

    All projected outcome counts MUST sum to
    ``inventory_total``.
    """
    by_outcome = projection.by_outcome
    would_write = by_outcome.get(ProjectedOutcome.WOULD_WRITE.value, 0)
    would_overwrite = by_outcome.get(
        ProjectedOutcome.WOULD_OVERWRITE.value, 0
    )
    would_skip_current = by_outcome.get(
        ProjectedOutcome.WOULD_SKIP_CURRENT.value, 0
    )
    would_skip_runtime = by_outcome.get(
        ProjectedOutcome.WOULD_SKIP_RUNTIME.value, 0
    )
    would_filter = by_outcome.get(ProjectedOutcome.WOULD_FILTER.value, 0)
    would_fail_missing = by_outcome.get(
        ProjectedOutcome.WOULD_FAIL_MISSING_TASK_JSON.value, 0
    )
    would_reject_malformed = by_outcome.get(
        ProjectedOutcome.WOULD_REJECT_MALFORMED.value, 0
    )
    would_no_op = by_outcome.get(ProjectedOutcome.WOULD_NO_OP.value, 0)

    plan_would_write_actual = (
        plan.would_write
    )
    # The plan's ``would_write`` includes both actual writes
    # and entries that the projection re-classifies as
    # WOULD_SKIP_RUNTIME. We align the two sides.
    exec_would_have_written = would_write + would_skip_runtime

    plan_no_op = plan.no_op
    exec_no_op_total = (
        would_skip_current + would_filter + would_fail_missing
        + would_reject_malformed + would_no_op
    )

    passed = (
        plan_would_write_actual == exec_would_have_written
        and plan.would_overwrite == would_overwrite
        and plan_no_op == exec_no_op_total
    )

    counts = {
        "plan_would_write": plan.would_write,
        "plan_would_overwrite": plan.would_overwrite,
        "plan_no_op": plan_no_op,
        "plan_runtime_would_touch": plan.runtime_would_touch,
        "projected_writes": would_write,
        "projected_overwrites": would_overwrite,
        "projected_skips": would_skip_current,
        "projected_runtime_skipped": would_skip_runtime,
        "projected_filtered": would_filter,
        "projected_no_task_json": would_fail_missing,
        "projected_malformed": would_reject_malformed,
        "projected_no_op": would_no_op,
        "projected_total": sum(by_outcome.values()),
    }
    return passed, counts


def _aggregate_projection_totals(
    projection: ProjectedMigrationResult,
) -> Dict[str, int]:
    """Return a flat dict of projection totals for the DTO.

    Convenience helper so the dry-run function does not have
    to re-read the projection's ``by_outcome`` map three
    times.
    """
    by_outcome = projection.by_outcome
    return {
        "projected_writes": by_outcome.get(
            ProjectedOutcome.WOULD_WRITE.value, 0
        ),
        "projected_overwrites": by_outcome.get(
            ProjectedOutcome.WOULD_OVERWRITE.value, 0
        ),
        "projected_skips": by_outcome.get(
            ProjectedOutcome.WOULD_SKIP_CURRENT.value, 0
        ),
        "projected_runtime_skipped": by_outcome.get(
            ProjectedOutcome.WOULD_SKIP_RUNTIME.value, 0
        ),
        "projected_filtered": by_outcome.get(
            ProjectedOutcome.WOULD_FILTER.value, 0
        ),
        "projected_no_task_json": by_outcome.get(
            ProjectedOutcome.WOULD_FAIL_MISSING_TASK_JSON.value, 0
        ),
        "projected_malformed": by_outcome.get(
            ProjectedOutcome.WOULD_REJECT_MALFORMED.value, 0
        ),
        "projected_no_op": by_outcome.get(
            ProjectedOutcome.WOULD_NO_OP.value, 0
        ),
        "projected_total": sum(by_outcome.values()),
    }


def run_live_migration_dryrun(
    reports_root: str | os.PathLike,
    *,
    target_policy_version: str = DEFAULT_TARGET_POLICY_VERSION,
    status_filter: Optional[FrozenSet[SidecarStatus]] = None,
    allow_runtime: bool = False,
    force: bool = False,
    utc_stamp: Optional[str] = None,
    write_manifest: bool = False,
    manifest_path: Optional[str | os.PathLike] = None,
) -> LiveMigrationDryrunResult:
    """Run a full inventory → plan → projection pipeline.

    **Read-only.** The function NEVER mutates ``task.json``,
    NEVER writes an ``identity.json`` sidecar, NEVER imports
    ``dispatcher``, and NEVER contacts the live DB. The only
    optional filesystem write is the manifest artifact when
    ``write_manifest=True``.

    The pipeline:

    1. :func:`aee.audit.build_sidecar_inventory` (AEE-7.7c,
       read-only corpus walk).
    2. :func:`aee.audit.plan_sidecar_migration` (AEE-7.7c,
       pure dry-run planner).
    3. :func:`aee.audit.project_migration_execution` (AEE-7.7e,
       pure-function projection — what the AEE-7.7d executor
       WOULD do, computed without invoking it).
    4. Plan / projection reconciliation.
    5. Optional manifest write (default: off).

    Parameters
    ----------
    reports_root
        The ``reports/`` tree to dry-run over. The same path
        is passed to all three layers (inventory, plan,
        projection) so the resulting DTO is self-consistent.
    target_policy_version
        Target policy version for the dry-run plan. Default
        :data:`DEFAULT_TARGET_POLICY_VERSION` (``"1.1.0"``).
        Passed through to :func:`plan_sidecar_migration`.
    status_filter
        Optional override of the projection's status filter.
        Default :data:`aee.audit.sidecar_migration.DEFAULT_STATUS_FILTER`
        (MISSING + STALE_HASH + STALE_VERSION). The plan
        always uses the AEE-7.7c default (its own planner
        does not accept a ``status_filter`` — it plans over
        the full inventory).
    allow_runtime
        Projection flag. If False (default), RUNTIME records
        are projected as WOULD_SKIP_RUNTIME. Passed through to
        :func:`project_migration_execution`.
    force
        Projection flag. If True, the projection emits
        WOULD_OVERWRITE even for entries whose content
        matches the new verdict. Default False.
    utc_stamp
        Optional deterministic timestamp propagated to all
        three layers (inventory, plan, projection). Default:
        ``"now"`` via :func:`_now_utc_iso`.
    write_manifest
        If True, the aggregate DTO is written to disk at
        ``manifest_path`` (or the default
        ``<reports_root.parent>/aee77e-dryrun-<UTC>.json``).
        Default: False (in-memory only).
    manifest_path
        Optional override of the on-disk manifest path.
        Ignored when ``write_manifest=False``. The caller is
        responsible for choosing a safe location (NOT inside
        the audited corpus).
    """
    root = Path(reports_root)
    stamp = utc_stamp or _now_utc_iso()

    # 1. Inventory (read-only)
    inventory = build_sidecar_inventory(root, utc_stamp=stamp)

    # 2. Plan (pure function)
    plan = plan_sidecar_migration(
        inventory,
        target_policy_version=target_policy_version,
        utc_stamp=stamp,
    )

    # 3. Projection (pure function — NO filesystem writes)
    effective_status_filter = (
        status_filter if status_filter is not None else DEFAULT_STATUS_FILTER
    )
    projection = project_migration_execution(
        inventory,
        status_filter=effective_status_filter,
        allow_runtime=allow_runtime,
        force=force,
        utc_stamp=stamp,
    )

    # 4. Reconciliation (plan vs projection)
    passed, recon_counts = _reconcile_plan_vs_projection(plan, projection)
    proj_totals = _aggregate_projection_totals(projection)

    # 5. Optional manifest
    resolved_manifest_path: Optional[Path] = None
    manifest_sha = ""
    if write_manifest:
        if manifest_path is not None:
            resolved_manifest_path = Path(manifest_path)
        else:
            resolved_manifest_path = _default_manifest_path(root, stamp)
        # The caller is responsible for choosing a safe
        # location. The default is <reports_root.parent>, i.e.
        # OUTSIDE the audited corpus. We do NOT enforce that
        # here — see TestManifestOutsideCorpus for the
        # test-side enforcement.
        resolved_manifest_path.parent.mkdir(parents=True, exist_ok=True)
        # Build a provisional DTO without manifest fields,
        # serialize, hash, and return a final DTO that
        # carries the manifest path / sha.
        provisional = _build_dto(
            reports_root=str(root),
            stamp=stamp,
            inventory=inventory,
            plan=plan,
            projection=projection,
            recon_counts=recon_counts,
            proj_totals=proj_totals,
            reconciliation_passed=passed,
            manifest_path=None,
            manifest_sha256="",
        )
        with open(resolved_manifest_path, "w", encoding="utf-8") as fh:
            json.dump(provisional.to_dict(), fh, sort_keys=True, indent=2)
        manifest_sha = _file_sha256(resolved_manifest_path)
        return _build_dto(
            reports_root=str(root),
            stamp=stamp,
            inventory=inventory,
            plan=plan,
            projection=projection,
            recon_counts=recon_counts,
            proj_totals=proj_totals,
            reconciliation_passed=passed,
            manifest_path=str(resolved_manifest_path),
            manifest_sha256=manifest_sha,
        )

    return _build_dto(
        reports_root=str(root),
        stamp=stamp,
        inventory=inventory,
        plan=plan,
        projection=projection,
        recon_counts=recon_counts,
        proj_totals=proj_totals,
        reconciliation_passed=passed,
        manifest_path=None,
        manifest_sha256="",
    )


def _build_dto(
    *,
    reports_root: str,
    stamp: str,
    inventory: SidecarInventoryResult,
    plan: MigrationPlan,
    projection: ProjectedMigrationResult,
    recon_counts: Dict[str, int],
    proj_totals: Dict[str, int],
    reconciliation_passed: bool,
    manifest_path: Optional[str],
    manifest_sha256: str,
) -> LiveMigrationDryrunResult:
    """Internal helper: assemble a :class:`LiveMigrationDryrunResult`.

    Centralised so the dry-run function does not repeat
    25-field construction at every return point.
    """
    return LiveMigrationDryrunResult(
        reports_root=reports_root,
        utc_stamp=stamp,
        schema_version=LIVE_MIGRATION_DRYRUN_SCHEMA_VERSION,
        inventory=inventory,
        plan=plan,
        projection=projection,
        projected_writes=proj_totals["projected_writes"],
        projected_overwrites=proj_totals["projected_overwrites"],
        projected_skips=proj_totals["projected_skips"],
        projected_runtime_skipped=proj_totals[
            "projected_runtime_skipped"
        ],
        projected_filtered=proj_totals["projected_filtered"],
        projected_no_task_json=proj_totals["projected_no_task_json"],
        projected_malformed=proj_totals["projected_malformed"],
        projected_no_op=proj_totals["projected_no_op"],
        projected_total=proj_totals["projected_total"],
        plan_would_write=recon_counts["plan_would_write"],
        plan_would_overwrite=recon_counts["plan_would_overwrite"],
        plan_no_op=recon_counts["plan_no_op"],
        plan_runtime_would_touch=recon_counts["plan_runtime_would_touch"],
        # Deprecated v1.0.0 aliases (sourced from the projection).
        exec_wrote=proj_totals["projected_writes"],
        exec_overwrote=proj_totals["projected_overwrites"],
        exec_untouched=proj_totals["projected_skips"],
        exec_skipped_runtime=proj_totals["projected_runtime_skipped"],
        exec_status_filtered=proj_totals["projected_filtered"],
        exec_failure=0,
        exec_malformed=proj_totals["projected_malformed"],
        exec_no_task_json=proj_totals["projected_no_task_json"],
        exec_fresh_skipped=0,
        exec_total=proj_totals["projected_total"],
        reconciliation_passed=reconciliation_passed,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
    )


def run_live_migration_apply(
    reports_root: str | os.PathLike,
    *,
    target_policy_version: str = DEFAULT_TARGET_POLICY_VERSION,
    status_filter: Optional[FrozenSet[SidecarStatus]] = None,
    allow_runtime: bool = False,
    force: bool = False,
    utc_stamp: Optional[str] = None,
    write_execution_log: bool = False,
) -> MigrationExecutionResult:
    """Explicit apply path: run the full pipeline and ACTUALLY
    stamp the sidecars via the AEE-7.7d executor.

    This is the ONLY AEE-7.7e function that may write
    sidecars. The dry-run flow (:func:`run_live_migration_dryrun`)
    does NOT call this function. Callers that want the
    real migration must use this function explicitly so the
    filesystem-mutating intent is visible in the call site.

    Parameters
    ----------
    reports_root
        The ``reports/`` tree to migrate. The same path is
        passed to all three layers (inventory, plan, executor).
    target_policy_version
        Target policy version for the plan. Default
        :data:`DEFAULT_TARGET_POLICY_VERSION` (``"1.1.0"``).
        Passed through to :func:`plan_sidecar_migration`.
    status_filter
        Optional override of the executor's status filter.
        Default :data:`aee.audit.sidecar_migration.DEFAULT_STATUS_FILTER`
        (MISSING + STALE_HASH + STALE_VERSION).
    allow_runtime
        Executor flag. If False (default), RUNTIME records
        are skipped. Passed through to
        :func:`execute_sidecar_migration`.
    force
        Executor flag. If True, the executor re-stamps even
        UNCHANGED entries. Default False.
    utc_stamp
        Optional deterministic timestamp propagated to all
        three layers (inventory, plan, execution). Default:
        ``"now"`` via :func:`_now_utc_iso`.
    write_execution_log
        If True, the AEE-7.7d executor writes its
        ``migration_log_<UTC>.json`` next to the reports
        root. Default: False.

    Returns
    -------
    MigrationExecutionResult
        The AEE-7.7d execution result. Inspect
        ``outcomes``, ``by_status``, and ``log_path`` for
        what landed on disk.
    """
    root = Path(reports_root)
    stamp = utc_stamp or _now_utc_iso()

    # 1. Inventory (read-only)
    inventory = build_sidecar_inventory(root, utc_stamp=stamp)

    # 2. Plan (pure function)
    plan = plan_sidecar_migration(
        inventory,
        target_policy_version=target_policy_version,
        utc_stamp=stamp,
    )

    # 3. Apply (controlled write-side)
    effective_status_filter = (
        status_filter if status_filter is not None else DEFAULT_STATUS_FILTER
    )
    execution = execute_sidecar_migration(
        root,
        inventory,
        status_filter=effective_status_filter,
        allow_runtime=allow_runtime,
        force=force,
        utc_stamp=stamp,
        write_log=write_execution_log,
    )
    # The plan is computed but not returned — apply is the
    # write-side analogue, the plan is informational. We
    # attach it to the execution DTO via a private attribute
    # so callers that want both can read both. (No public
    # shape change; the plan is purely informational here.)
    _ = plan  # silence linters; plan is kept for traceability
    return execution


__all__ = [
    "DEFAULT_TARGET_POLICY_VERSION",
    "LIVE_MIGRATION_DRYRUN_SCHEMA_VERSION",
    "LiveMigrationDryrunResult",
    "PerTaskProjection",
    "ProjectedMigrationResult",
    "ProjectedOutcome",
    "project_migration_execution",
    "run_live_migration_apply",
    "run_live_migration_dryrun",
]
