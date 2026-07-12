"""AEE-7.7d — controlled sidecar migration / stamping (per-task executor).

This module is the **fourth tool** in the ``aee/audit/`` package
and the **write-side executor** for the AEE-7.7c read-only
:func:`aee.audit.build_sidecar_inventory` +
:func:`aee.audit.plan_sidecar_migration` pair. Given a
:class:`SidecarInventoryResult` produced by AEE-7.7c, it
applies a controlled, status-filtered migration:

* Only entries whose :attr:`SidecarInventoryEntry.status` is in
  the caller-supplied ``status_filter`` (default
  ``{MISSING, STALE_HASH, STALE_VERSION}``) are touched.
* RUNTIME records are **never** written (``allow_runtime``
  defaults to ``False``) — this is the "explicit
  ``--allow-runtime=False`` gate" the AEE-7.7d brief requires.
* FRESH records are always skipped (a no-op re-stamp of a
  fresh sidecar is wasteful and noisy).
* Each touched task is processed via
  :func:`aee.reporting.identity.classify_and_persist` — the
  same SOT helper AEE-7.7b :func:`aee.audit.apply_sidecars`
  uses internally.

Why this module does NOT call :func:`aee.audit.apply_sidecars`
----------------------------------------------------------------

The AEE-7.7c ``MigrationPlan`` proposes "calls
``aee.audit.apply_sidecars`` against each entry one-by-one".
A faithful re-implementation would either:

* (A) build a per-task ``AuditSummary`` containing exactly one
  :class:`PerTaskVerdict` and re-invoke ``apply_sidecars`` —
  but ``apply_sidecars`` always iterates the **whole**
  ``reports_root`` and indexes by ``summary_index``; a single-task
  summary would mark every other task ``SKIPPED_NOT_IN_SUMMARY``
  in the result, polluting the migration log with non-migration
  noise; OR
* (B) run ``apply_sidecars`` once with the inventory's
  per-task ``executor_anchors`` populated, and let its
  ``allow_runtime`` flag gate RUNTIME — but ``allow_runtime``
  is a process-wide flag, so RUNTIME skipping is all-or-nothing
  and the inventory's per-status filter is not honored
  (``apply_sidecars`` ignores status).

Both designs either (a) double-walk the corpus 271× or (b)
silently broaden the migration's blast radius. AEE-7.7d
therefore uses :func:`aee.reporting.identity.classify_and_persist`
directly — the same SOT helper ``apply_sidecars`` itself calls
at line 560 and 655 of ``aee/audit/apply_sidecars.py`` — and
provides the per-task status filter + RUNTIME gate as a
first-class API. The behaviour a controlled migration needs
(stamp exactly the inventory entries with status in
``status_filter``) is implemented in this module; the wider
``apply_sidecars`` write-side stays unchanged for callers who
need the bulk, summary-driven write path.

If a future slice chooses to wire ``execute_sidecar_migration``
through ``apply_sidecars`` (e.g. to inherit
``strict_consistency`` / anchor-warning aggregation), the
right shape is to add a ``summary_from_inventory`` helper
that converts ``SidecarInventoryResult`` to ``AuditSummary``
in-place — deferred to a later slice.

Public surface
--------------

* :class:`MigrationStatus` — enum of per-task outcomes.
* :class:`PerTaskMigrationOutcome` — single-task result.
* :class:`MigrationExecutionResult` — aggregate result + log
  file path + counts.
* :func:`execute_sidecar_migration` — entry point.
* :data:`MIGRATION_EXEC_SCHEMA_VERSION` — DTO schema pin.

Out of scope (intentionally)
----------------------------

* No writes to ``data/dispatcher.db`` (read-only contact with
  the live runtime is forbidden; this module never imports
  ``dispatcher``).
* No ``task.json`` mutation.
* No subprocess / network / env reads.
* No logging of ``input_text``, prompts, secrets, or
  ``dispatcher/.env``.
* No commit / push / deploy / restart — callers stage and
  commit themselves.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

from aee.audit.sidecar_inventory import (
    SidecarInventoryEntry,
    SidecarInventoryResult,
    SidecarStatus,
    _file_sha256,
)
from aee.reporting.identity import (
    Identity,
    RecordKind,
    SentinelPolicy,
    classify_and_persist,
    classify_record,
    load_task_json,
    read_identity_sidecar,
)

# Stable schema version for the migration DTOs. Bumping this
# is a breaking change for any downstream consumer.
MIGRATION_EXEC_SCHEMA_VERSION = "1.0.0"

# Default status filter per the AEE-7.7c propose (only
# MISSING and STALE_* are eligible; RUNTIME / FRESH never
# touched). FRESH is the no-op state; RUNTIME is gated by
# ``allow_runtime`` (default False). The default is a
# frozenset of SidecarStatus values.
DEFAULT_STATUS_FILTER: FrozenSet[SidecarStatus] = frozenset({
    SidecarStatus.MISSING,
    SidecarStatus.STALE_HASH,
    SidecarStatus.STALE_VERSION,
})


class MigrationStatus(str, Enum):
    """Per-task outcome of :func:`execute_sidecar_migration`.

    The string values are persisted in
    :class:`PerTaskMigrationOutcome` and asserted in tests.
    Adding a new value is a schema change (bump
    :data:`MIGRATION_EXEC_SCHEMA_VERSION`).
    """

    WROTE = "wrote"                       # sidecar did not exist; now written
    OVERWROTE = "overwrote"               # sidecar existed with different content
    UNCHANGED = "unchanged"               # sidecar exists and matches new verdict
    RUNTIME_SKIPPED = "runtime_skipped"   # record is RUNTIME and allow_runtime=False
    # Reserved for a future "explicit operator override" mode
    # where the caller passes allow_runtime=True but the
    # per-task policy still says no. Not currently emitted
    # by ``execute_sidecar_migration``; left in the enum so
    # downstream consumers can pattern-match on the full set.
    RUNTIME_DISALLOWED = "runtime_disallowed"
    FRESH_SKIPPED = "fresh_skipped"       # status_filter did not include FRESH
    STATUS_FILTERED = "status_filtered"   # entry.status not in status_filter
    NO_TASK_JSON = "no_task_json"         # task_dir has no readable task.json
    MALFORMED = "malformed"               # classify_and_persist returned None


@dataclass(frozen=True)
class PerTaskMigrationOutcome:
    """The result of attempting to migrate one task.json.

    Frozen so it can be put in sets / hashed in test
    assertions. ``note`` is the only free-form field; keep it
    short and never include ``input_text`` or secret-bearing
    data.
    """

    task_id: str
    status: MigrationStatus
    inventory_status: SidecarStatus         # the status the inventory assigned
    record_kind: Optional[str]              # from classify_and_persist, or None
    source_task_json_sha256: str            # empty when no readable task.json
    sidecar_sha256_before: str              # empty when no prior sidecar
    sidecar_sha256_after: str               # empty when no write happened
    policy_version: Optional[str]           # from the new sidecar
    schema_version: Optional[str]           # from the new sidecar
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "inventory_status": self.inventory_status.value,
            "record_kind": self.record_kind,
            "source_task_json_sha256": self.source_task_json_sha256,
            "sidecar_sha256_before": self.sidecar_sha256_before,
            "sidecar_sha256_after": self.sidecar_sha256_after,
            "policy_version": self.policy_version,
            "schema_version": self.schema_version,
            "note": self.note,
        }


@dataclass
class MigrationExecutionResult:
    """The aggregate :func:`execute_sidecar_migration` result.

    DTO; serializable via :meth:`to_dict` / :meth:`to_markdown`.
    The :attr:`log_path` is the on-disk path the migration
    log was written to (``None`` when ``write_log=False``).
    """

    reports_root: str
    executed_at_utc: str
    schema_version: str
    # The inputs to the migration. ``status_filter`` is the
    # ACTUAL filter used (after applying default + caller
    # override); ``allow_runtime`` is the ACTUAL gate; ``force``
    # is the ACTUAL idempotency override. Persisted so the log
    # is self-describing for any future post-mortem.
    status_filter: Tuple[str, ...]
    allow_runtime: bool
    force: bool
    inventory_total: int
    inventory_fingerprints: Dict[str, str]  # task_id -> sha256 of task.json at read time
    outcomes: List[PerTaskMigrationOutcome] = field(default_factory=list)
    # Aggregate counts by MigrationStatus. Computed from
    # outcomes.
    by_status: Dict[str, int] = field(default_factory=dict)
    # Aggregate counts by SidecarStatus (the inventory-side
    # state of each entry). Useful for verifying the filter
    # actually only touched MISSING / STALE_*.
    by_inventory_status: Dict[str, int] = field(default_factory=dict)
    log_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "executed_at_utc": self.executed_at_utc,
            "reports_root": self.reports_root,
            "status_filter": list(self.status_filter),
            "allow_runtime": self.allow_runtime,
            "force": self.force,
            "inventory_total": self.inventory_total,
            "inventory_fingerprints": dict(self.inventory_fingerprints),
            "by_status": dict(self.by_status),
            "by_inventory_status": dict(self.by_inventory_status),
            "log_path": self.log_path,
            "outcomes": [o.to_dict() for o in self.outcomes],
        }

    def to_markdown(self) -> str:
        lines: List[str] = []
        lines.append("# AEE-7.7d Sidecar Migration Execution")
        lines.append("")
        lines.append(f"- Schema version: `{self.schema_version}`")
        lines.append(f"- Executed at (UTC): `{self.executed_at_utc}`")
        lines.append(f"- Reports root: `{self.reports_root}`")
        lines.append(f"- status_filter: `{', '.join(self.status_filter)}`")
        lines.append(f"- allow_runtime: `{self.allow_runtime}`")
        lines.append(f"- force: `{self.force}`")
        lines.append(f"- Inventory total: **{self.inventory_total}**")
        lines.append("")
        lines.append("## By MigrationStatus")
        lines.append("")
        lines.append("| Status | Count |")
        lines.append("|---|---|")
        for code, count in sorted(
            self.by_status.items(), key=lambda kv: (-kv[1], kv[0])
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
        if self.log_path:
            lines.append(f"- Migration log: `{self.log_path}`")
            lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_utc_iso() -> str:
    """Return current UTC ISO-8601 'Z' timestamp."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _task_dir_for(reports_root: Path, task_id: str) -> Path:
    """Resolve the on-disk task directory for a given ``task_id``.

    The 7.7c inventory stores task_id as the part after the
    ``TASK-`` prefix; on disk the directory is
    ``TASK-<task_id>``. We resolve via ``reports_root / f"TASK-{task_id}"``
    to keep the SOT consistent with the inventory's
    ``_iter_task_dirs``.
    """
    return reports_root / f"TASK-{task_id}"


def _serialize_inventory(
    inventory: SidecarInventoryResult,
) -> Dict[str, str]:
    """Return a task_id -> task_json_sha256 map for the log.

    Captured at migration time so any post-mortem can verify
    which on-disk content the migration actually saw (a
    subsequent ``task.json`` rewrite would otherwise be
    invisible to the log reader).
    """
    out: Dict[str, str] = {}
    for entry in inventory.entries:
        out[entry.task_id] = entry.task_json_sha256
    return out


def _is_runtime_record(task_json_path: Path) -> bool:
    """Return True iff the record would be classified as RUNTIME.

    The check honours two sources, in order:

    1. The existing on-disk sidecar (``read_identity_sidecar``).
       If the sidecar says ``record_kind == "runtime"`` the
       record is RUNTIME (this is the cheapest and most
       authoritative signal).
    2. The fresh classification of the on-disk ``task.json``
       via :func:`classify_record`. Used when the sidecar is
       missing (so the inventory's ``record_kind`` is None).

    The function is a defence-in-depth check layered on top
    of the inventory's ``record_kind`` hint — without it, a
    MISSING entry whose underlying ``task.json`` would
    classify as RUNTIME could slip through the
    ``allow_runtime=False`` gate.
    """
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


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def execute_sidecar_migration(
    reports_root: str | os.PathLike,
    inventory: SidecarInventoryResult,
    *,
    status_filter: Optional[Iterable[SidecarStatus]] = None,
    allow_runtime: bool = False,
    force: bool = False,
    utc_stamp: Optional[str] = None,
    log_path: Optional[str | os.PathLike] = None,
    write_log: bool = True,
) -> MigrationExecutionResult:
    """Apply a controlled migration over the inventory's tasks.

    The function iterates ``inventory.entries`` in their
    on-disk order (the inventory sorts by task_id), filters by
    ``status_filter`` and ``allow_runtime``, and for each
    surviving entry calls
    :func:`aee.reporting.identity.classify_and_persist` to
    produce a fresh sidecar. The function NEVER mutates
    ``task.json`` and NEVER imports ``dispatcher``.

    Parameters
    ----------
    reports_root
        The same reports root the inventory was built over.
        Used to resolve each ``task_id`` to its on-disk
        ``TASK-<id>/`` directory.
    inventory
        The :class:`aee.audit.SidecarInventoryResult` to
        migrate. Only its ``entries`` are read.
    status_filter
        Optional override of the eligible status set. Default
        :data:`DEFAULT_STATUS_FILTER` (MISSING + STALE_HASH +
        STALE_VERSION). FRESH is never touched; RUNTIME is
        excluded by the default and is additionally gated by
        ``allow_runtime``.
    allow_runtime
        If False (default), RUNTIME records are skipped with
        :attr:`MigrationStatus.RUNTIME_SKIPPED` regardless of
        ``status_filter``. If True, RUNTIME records are
        eligible for stamping when their inventory status is
        in ``status_filter`` (typically MISSING or
        STALE_VERSION).
    force
        If False (default), an existing sidecar whose content
        matches the new verdict is left as
        :attr:`MigrationStatus.UNCHANGED` (no write). If
        True, the writer overwrites unconditionally; the
        outcome is :attr:`MigrationStatus.OVERWROTE` when the
        old content differed, :attr:`MigrationStatus.WROTE`
        when there was no prior sidecar, and
        :attr:`MigrationStatus.UNCHANGED` never.
    utc_stamp
        Optional deterministic timestamp for the
        :attr:`MigrationExecutionResult.executed_at_utc` and
        each stamped sidecar's ``classified_at_utc``. Tests
        pass an explicit value; callers that want
        ``"now"`` leave it None.
    log_path
        Optional override of the on-disk migration log path.
        Default: ``<reports_root>/../migration_log_<UTC>.json``
        (a sibling of the reports root so the log is not
        inside the audited corpus).
    write_log
        If False, the migration log is not written to disk
        (the in-memory :class:`MigrationExecutionResult` is
        still returned). Useful for tests that want the
        DTO without touching the filesystem.
    """
    root = Path(reports_root)
    stamp = utc_stamp or _now_utc_iso()

    # Resolve the actual filter (apply default if caller
    # passed None). Cast to a tuple of status.value strings
    # so the DTO is JSON-serializable.
    if status_filter is None:
        effective = DEFAULT_STATUS_FILTER
    else:
        effective = frozenset(status_filter)
        # FRESH is never in the filter — we explicitly drop it
        # so a caller that includes it by mistake does not get
        # silent no-op re-stamps. (The status_filtered vs
        # fresh_skipped decision is made below per-entry.)
        effective = effective - {SidecarStatus.FRESH}

    result = MigrationExecutionResult(
        reports_root=str(root),
        executed_at_utc=stamp,
        schema_version=MIGRATION_EXEC_SCHEMA_VERSION,
        status_filter=tuple(sorted(s.value for s in effective)),
        allow_runtime=allow_runtime,
        force=force,
        inventory_total=len(inventory.entries),
        inventory_fingerprints=_serialize_inventory(inventory),
    )

    if not inventory.entries:
        return _finalize(result, log_path, write_log)

    for entry in inventory.entries:
        outcome = _process_one_entry(
            entry=entry,
            root=root,
            effective_filter=effective,
            allow_runtime=allow_runtime,
            force=force,
            stamp=stamp,
        )
        result.outcomes.append(outcome)

    return _finalize(result, log_path, write_log)


def _process_one_entry(
    *,
    entry: SidecarInventoryEntry,
    root: Path,
    effective_filter: FrozenSet[SidecarStatus],
    allow_runtime: bool,
    force: bool,
    stamp: str,
) -> PerTaskMigrationOutcome:
    """Process a single inventory entry. Pure-ish: writes
    at most one sidecar to disk, returns a frozen outcome.
    """
    task_dir = _task_dir_for(root, entry.task_id)
    task_json_path = task_dir / "task.json"

    # --- 1. status filter check (always) ---
    if entry.status not in effective_filter:
        return PerTaskMigrationOutcome(
            task_id=entry.task_id,
            status=MigrationStatus.STATUS_FILTERED,
            inventory_status=entry.status,
            record_kind=entry.record_kind,
            source_task_json_sha256=entry.task_json_sha256,
            sidecar_sha256_before=entry.sidecar_sha256,
            sidecar_sha256_after="",
            policy_version=entry.sidecar_policy_version,
            schema_version=entry.sidecar_schema_version,
            note=f"status={entry.status.value} not in filter",
        )

    # --- 2. FRESH: short-circuit (filter should already exclude it,
    # but defense in depth in case the filter is overridden) ---
    if entry.status == SidecarStatus.FRESH:
        return PerTaskMigrationOutcome(
            task_id=entry.task_id,
            status=MigrationStatus.FRESH_SKIPPED,
            inventory_status=entry.status,
            record_kind=entry.record_kind,
            source_task_json_sha256=entry.task_json_sha256,
            sidecar_sha256_before=entry.sidecar_sha256,
            sidecar_sha256_after=entry.sidecar_sha256,
            policy_version=entry.sidecar_policy_version,
            schema_version=entry.sidecar_schema_version,
            note="FRESH records are never re-stamped",
        )

    # --- 3. task.json must be readable ---
    if not task_json_path.is_file() or not entry.task_json_sha256:
        return PerTaskMigrationOutcome(
            task_id=entry.task_id,
            status=MigrationStatus.NO_TASK_JSON,
            inventory_status=entry.status,
            record_kind=entry.record_kind,
            source_task_json_sha256=entry.task_json_sha256,
            sidecar_sha256_before=entry.sidecar_sha256,
            sidecar_sha256_after="",
            policy_version=entry.sidecar_policy_version,
            schema_version=entry.sidecar_schema_version,
            note="task.json missing or unreadable",
        )

    # --- 4. RUNTIME gate (defence in depth) ---
    # The inventory's record_kind is the primary signal; if it
    # is set, honour it. If it is None (a real on-disk RUNTIME
    # record with no sidecar hint), re-read the sidecar to
    # confirm.
    is_runtime = (
        entry.record_kind == RecordKind.RUNTIME.value
        or _is_runtime_record(task_json_path)
    )
    if is_runtime and not allow_runtime:
        return PerTaskMigrationOutcome(
            task_id=entry.task_id,
            status=MigrationStatus.RUNTIME_SKIPPED,
            inventory_status=entry.status,
            record_kind="runtime",
            source_task_json_sha256=entry.task_json_sha256,
            sidecar_sha256_before=entry.sidecar_sha256,
            sidecar_sha256_after="",
            policy_version=entry.sidecar_policy_version,
            schema_version=entry.sidecar_schema_version,
            note=(
                "allow_runtime=False (inventory hint)"
                if entry.record_kind == RecordKind.RUNTIME.value
                else "allow_runtime=False (defence-in-depth classify_record)"
            ),
        )

    # --- 5. existing-sidecar check (idempotency) ---
    existing = read_identity_sidecar(task_json_path)
    sha_before = entry.sidecar_sha256 or ""

    # --- 6. classify_and_persist (SOT) ---
    # CRITICAL: when allow_runtime=True, the caller has
    # explicitly opted in to writing RUNTIME sidecars. The
    # SOT helper gates RUNTIME writes on its own
    # ``sidecar_for_runtime`` parameter (default False —
    # see aee/reporting/identity.py:classify_and_persist).
    # Without this propagation, classify_and_persist returns
    # an Identity but does NOT write the sidecar file, and
    # the migration would lie to the caller ("wrote") when in
    # fact no file landed on disk.
    new_identity = classify_and_persist(
        task_json_path,
        classified_at_utc=stamp,
        sidecar_for_runtime=allow_runtime,
    )
    if new_identity is None:
        return PerTaskMigrationOutcome(
            task_id=entry.task_id,
            status=MigrationStatus.MALFORMED,
            inventory_status=entry.status,
            record_kind=entry.record_kind,
            source_task_json_sha256=entry.task_json_sha256,
            sidecar_sha256_before=sha_before,
            sidecar_sha256_after="",
            policy_version=entry.sidecar_policy_version,
            schema_version=entry.sidecar_schema_version,
            note="classify_and_persist returned None",
        )

    sha_after = _file_sha256(task_json_path.parent / "identity.json")

    # --- 7. decide WROTE / OVERWROTE / UNCHANGED ---
    if existing is None:
        decision = MigrationStatus.WROTE
        note = "no prior sidecar"
    elif force:
        decision = MigrationStatus.OVERWROTE
        note = "force=True; sidecar overwritten"
    else:
        same = (
            existing.record_kind == new_identity.record_kind
            and existing.is_fixture == new_identity.is_fixture
            and list(existing.fixture_markers)
            == list(new_identity.fixture_markers)
            and existing.executor_session_id
            == new_identity.executor_session_id
            and existing.runtime_run_id == new_identity.runtime_run_id
            and existing.user_provided_alias
            == new_identity.user_provided_alias
            and existing.source_task_json_sha256
            == new_identity.source_task_json_sha256
            and existing.policy_version == new_identity.policy_version
        )
        if same:
            decision = MigrationStatus.UNCHANGED
            note = "existing sidecar matches new verdict"
        else:
            decision = MigrationStatus.OVERWROTE
            note = "existing sidecar differs from new verdict"

    return PerTaskMigrationOutcome(
        task_id=entry.task_id,
        status=decision,
        inventory_status=entry.status,
        record_kind=new_identity.record_kind.value,
        source_task_json_sha256=entry.task_json_sha256,
        sidecar_sha256_before=sha_before,
        sidecar_sha256_after=sha_after,
        policy_version=new_identity.policy_version,
        # The on-disk sidecar does NOT carry a schema_version
        # field (write_identity_sidecar writes only the
        # Identity.to_dict() payload). APPLY_SCHEMA_VERSION is
        # a 7.7b ApplySidecarsResult-level constant, not a
        # sidecar field. We surface the inventory's existing
        # schema_version (None for legacy sidecars) so the
        # migration log can flag a stale-version entry that
        # was just freshly stamped.
        schema_version=entry.sidecar_schema_version,
        note=note,
    )


def _finalize(
    result: MigrationExecutionResult,
    log_path: Optional[str | os.PathLike],
    write_log: bool,
) -> MigrationExecutionResult:
    """Aggregate the outcome counts and (optionally) write the
    migration log JSON to disk.

    The log is written **outside** ``reports_root`` by default
    (a sibling ``migration_log_<UTC>.json``) so the
    audit-time ``reports/`` shape is preserved (no extra
    files inside the audited corpus). When ``log_path`` is
    supplied, the caller is responsible for choosing a safe
    location.
    """
    # Aggregate by MigrationStatus
    by_status: Dict[str, int] = {s.value: 0 for s in MigrationStatus}
    for o in result.outcomes:
        by_status[o.status.value] += 1
    result.by_status = by_status

    # Aggregate by inventory status (after filter)
    by_inv: Dict[str, int] = {s.value: 0 for s in SidecarStatus}
    for o in result.outcomes:
        by_inv[o.inventory_status.value] += 1
    result.by_inventory_status = by_inv

    if not write_log:
        return result

    # Resolve log path
    if log_path is None:
        root = Path(result.reports_root)
        if root.parent != root:
            log_dir = root.parent
        else:
            log_dir = root
        log_path = log_dir / f"migration_log_{result.executed_at_utc}.json"
    else:
        log_path = Path(log_path)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as fh:
        json.dump(result.to_dict(), fh, sort_keys=True, indent=2)
    result.log_path = str(log_path)
    return result


__all__ = [
    "DEFAULT_STATUS_FILTER",
    "MIGRATION_EXEC_SCHEMA_VERSION",
    "MigrationExecutionResult",
    "MigrationStatus",
    "PerTaskMigrationOutcome",
    "execute_sidecar_migration",
]
