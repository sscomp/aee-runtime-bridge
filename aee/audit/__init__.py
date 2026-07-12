"""AEE-7.7a — Live-Report Audit (read-only).

This package runs a one-shot audit over a ``reports/`` directory
hierarchy.  It is the read-only counterpart to the AEE-7.11
``aee/reporting/identity.classify_and_persist`` helper:

* ``classify_and_persist`` writes a ``identity.json`` sidecar
  next to every FIXTURE/UNKNOWN ``task.json``.  It is the
  per-record persistent form.
* :func:`aee.audit.run_audit` walks a reports root, classifies
  every record *in memory* via :func:`classify_record`, runs
  :func:`validate_task_identity` for consistency, aggregates
  the verdicts, and writes **one** JSON + **one** Markdown
  summary to the caller-supplied output directory.  It never
  writes inside the audited ``reports/`` directory.  It is the
  bulk one-shot form, suitable for an AEE-7.7 next-slice
  delivery.

The audit is intentionally **read-only** with respect to the
dispatcher write path:

* No row is written to ``data/dispatcher.db``.
* No file is written under ``reports/``.
* The audit output is a single JSON + a single Markdown written
  to the caller-supplied output path (defaults to
  ``/tmp/aee77a-audit-<UTC>.{json,md}``).

Why this lives in its own package
---------------------------------
``aee/reporting/`` is the *per-record* home (Identity, sidecar
writer, consistency validator).  ``aee/audit/`` is the
*corpus-level* home (walk a directory, aggregate verdicts,
write a summary report).  Separating the two keeps the
``aee/reporting/`` surface small (no I/O helpers, no directory
walkers) and makes it easy to extend the audit (more findings
types, more aggregation buckets) without bloating the
read-side identity model.

Public surface
--------------

* :func:`run_audit` — the entry point.  Takes a reports root
  and an output path, returns a :class:`AuditSummary`.
* :class:`AuditSummary` — the in-memory aggregate, serializable
  via :meth:`to_dict` / :meth:`to_markdown`.
* :class:`PerTaskVerdict` — a single record's verdict
  (classification + consistency findings).

Excluded by design
------------------

* No network calls.  No HTTP.  No ``requests``.
* No subprocess.  No shelling out to ``git`` or ``sqlite3``.
* No environment variable reads.  The audit is deterministic
  given the same on-disk corpus + the same policy version.
* No logging of secrets.  The audit never reads
  ``dispatcher/.env`` or any secret-bearing file.
"""
from __future__ import annotations

from .live_audit import (
    AuditSummary,
    PerTaskVerdict,
    run_audit,
)
# AEE-7.7b: call-site migration. The single AEE-7.7b entry
# point that turns an AEE-7.7a ``AuditSummary`` into persisted
# ``identity.json`` sidecars next to every ``task.json`` the
# audit classified as consistent.
#
#   from aee.audit import run_audit, apply_sidecars
#   summary, _, _ = run_audit(reports_root, output_dir)
#   result = apply_sidecars(reports_root, summary)
#
# Re-exported here so a caller only needs ``from aee.audit
# import ...`` (the audit package is the one-stop namespace).
from .apply_sidecars import (
    APPLY_SCHEMA_VERSION,
    ApplySidecarsResult,
    PerTaskSidecarOutcome,
    SidecarDecision,
    apply_sidecars,
)
# AEE-7.7c: read-only inventory + dry-run migration planner.
# The third tool in the audit package after run_audit (read)
# and apply_sidecars (write) — this one plans without doing.
#
#   from aee.audit import build_sidecar_inventory, plan_sidecar_migration
#   inv = build_sidecar_inventory(reports_root)
#   plan = plan_sidecar_migration(inv, target_policy_version="1.1.0")
#
# Re-exported here so a caller only needs ``from aee.audit
# import ...`` (the audit package is the one-stop namespace).
from .sidecar_inventory import (
    INVENTORY_SCHEMA_VERSION,
    MigrationPlan,
    SidecarInventoryEntry,
    SidecarInventoryResult,
    SidecarStatus,
    build_sidecar_inventory,
    plan_sidecar_migration,
)
# AEE-7.7d: controlled migration executor. The fourth tool in
# the audit package — the write-side counterpart to the
# AEE-7.7c read-only inventory. Given an inventory, it stamps
# a fresh ``identity.json`` for every entry whose status is
# in the caller's ``status_filter`` (default MISSING +
# STALE_HASH + STALE_VERSION; RUNTIME always skipped via
# ``allow_runtime`` gate).
#
#   from aee.audit import execute_sidecar_migration
#   inv = build_sidecar_inventory(reports_root)
#   result = execute_sidecar_migration(
#       reports_root, inv, status_filter=..., allow_runtime=False,
#   )
#
# Re-exported here so a caller only needs ``from aee.audit
# import ...``. The per-task SOT helper (aee.reporting's
# ``classify_and_persist``) is the actual writer; the audit
# package re-export only exposes the controlled-execution
# entry point.
from .sidecar_migration import (
    DEFAULT_STATUS_FILTER,
    MIGRATION_EXEC_SCHEMA_VERSION,
    MigrationExecutionResult,
    MigrationStatus,
    PerTaskMigrationOutcome,
    execute_sidecar_migration,
)
# AEE-7.7e: live-corpus migration dry-run + projection
# orchestrator. The fifth tool in the audit package — the
# end-to-end read-only orchestrator that ties together
# build_sidecar_inventory (AEE-7.7c) →
# plan_sidecar_migration (AEE-7.7c) →
# project_migration_execution (AEE-7.7e) in a single call,
# with a plan/projection reconciliation check and an optional
# on-disk manifest artifact. The dry-run flow is read-only by
# contract; the explicit apply flow lives behind a separate
# ``run_live_migration_apply`` entry point.
#
#   from aee.audit import run_live_migration_dryrun
#   result = run_live_migration_dryrun(
#       reports_root,
#       target_policy_version="1.1.0",
#       write_manifest=True,
#   )
#   assert result.reconciliation_passed
#
#   # To actually stamp sidecars, use the explicit apply API:
#   from aee.audit import run_live_migration_apply
#   exec_result = run_live_migration_apply(reports_root)
#
# Re-exported here so a caller only needs ``from aee.audit
# import ...``. The DTO carries the full inventory, plan, and
# projection so a downstream post-mortem has the entire chain
# visible.
from .live_migration_dryrun import (
    DEFAULT_TARGET_POLICY_VERSION,
    LIVE_MIGRATION_DRYRUN_SCHEMA_VERSION,
    LiveMigrationDryrunResult,
    PerTaskProjection,
    ProjectedMigrationResult,
    ProjectedOutcome,
    project_migration_execution,
    run_live_migration_apply,
    run_live_migration_dryrun,
)
# AEE-7.8 K1: read-only manifest support. The loader /
# validator companion to the AEE-7.7e ``write_manifest=True``
# artifact. Loads a manifest from disk into typed dataclasses
# and exposes a small introspection surface (list_group_names,
# get_group, iter_files, etc.). Pure read — no dispatcher,
# no live DB, no subprocess.
#
#   from aee.audit import load_manifest, validate_manifest
#   doc = load_manifest("AEE_7_7d_7e_MANIFEST.json")
#   result = validate_manifest(doc)
#   assert result.passed
#   for fe in doc.iter_files():
#       print(fe.path, fe.sha256)
#
# Re-exported here so a caller only needs ``from aee.audit
# import ...`` (the audit package is the one-stop namespace).
from .manifest import (
    FileEntry,
    FileEntryKind,
    GroupEntry,
    MANIFEST_SCHEMA_VERSION,
    ManifestDocument,
    ManifestError,
    ValidationResult,
    load_manifest,
    validate_manifest,
)

__all__ = [
    "APPLY_SCHEMA_VERSION",
    "ApplySidecarsResult",
    "AuditSummary",
    "DEFAULT_STATUS_FILTER",
    "DEFAULT_TARGET_POLICY_VERSION",
    "FileEntry",
    "FileEntryKind",
    "GroupEntry",
    "INVENTORY_SCHEMA_VERSION",
    "LIVE_MIGRATION_DRYRUN_SCHEMA_VERSION",
    "MANIFEST_SCHEMA_VERSION",
    "MIGRATION_EXEC_SCHEMA_VERSION",
    "LiveMigrationDryrunResult",
    "ManifestDocument",
    "ManifestError",
    "MigrationExecutionResult",
    "MigrationPlan",
    "MigrationStatus",
    "PerTaskMigrationOutcome",
    "PerTaskProjection",
    "PerTaskSidecarOutcome",
    "PerTaskVerdict",
    "ProjectedMigrationResult",
    "ProjectedOutcome",
    "SidecarDecision",
    "SidecarInventoryEntry",
    "SidecarInventoryResult",
    "SidecarStatus",
    "ValidationResult",
    "apply_sidecars",
    "build_sidecar_inventory",
    "execute_sidecar_migration",
    "load_manifest",
    "plan_sidecar_migration",
    "project_migration_execution",
    "run_audit",
    "run_live_migration_apply",
    "run_live_migration_dryrun",
    "validate_manifest",
]
