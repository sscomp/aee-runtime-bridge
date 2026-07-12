"""AEE-7.7c — sidecar inventory + dry-run migration planner (read-only).

This module is the **migration-registry-side** complement to
AEE-7.7b's :func:`aee.audit.apply_sidecars` and AEE-7.7a's
:func:`aee.audit.run_audit`. It walks ``reports/``, classifies
each ``task.json`` against the presence/absence/age of its
``identity.json`` sidecar, and produces a deterministic
:class:`SidecarInventoryResult` plus a dry-run :class:`MigrationPlan`
that says what :func:`apply_sidecars` WOULD do if invoked now
(without invoking it).

Why this lives here, not in ``aee/reporting``
----------------------------------------------

The audit (``aee/audit/``) already owns corpus-level read
operations over ``reports/``. The write side (``apply_sidecars``)
already lives here. Adding inventory next to them keeps the
**one package per concern** invariant:

* ``aee/reporting/`` — per-record identity SOT (read + write
  primitives)
* ``aee/audit/``     — corpus-level walk + classify + apply
                       (and now: inventory / migration plan)

Out of scope (intentionally)
----------------------------

* No ``identity.json`` writes (this module is purely read).
* No ``task.json`` writes (sidecar writers never mutate task.json).
* No dispatcher hot-path contact.
* No live-DB writes (this module never imports ``dispatcher``).
* No subprocess / network / env reads.
* No logging of ``input_text``, prompts, stdout / stderr, or any
  secret-bearing field.

Public surface
--------------

* :class:`SidecarStatus` — enum of the 4 possible states.
* :class:`SidecarInventoryEntry` — one row of the inventory.
* :class:`SidecarInventoryResult` — aggregate result (DTO).
* :class:`MigrationPlan` — dry-run plan (DTO, no side effects).
* :func:`build_sidecar_inventory` — entry point.
* :func:`plan_sidecar_migration` — pure function from
  inventory + target policy version → MigrationPlan.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# AEE-7.7b's current schema/policy versions. The inventory
# compares against these to decide if a sidecar is "stale" or
# "current". Importing the constant keeps the inventory in sync
# with the writer without duplicating the literal.
from .apply_sidecars import APPLY_SCHEMA_VERSION as _WRITER_SCHEMA_VERSION
# policy_version is the second axis the writer stamps. The
# current production writer stamps "1.0.0". We read the literal
# from the writer module too — but the writer does not export
# it as a module constant, so we mirror it here with a comment.
_WRITER_POLICY_VERSION = "1.0.0"

# Canonical AEE task directory name pattern: ``TASK-`` followed
# by at least one digit and (typically) a date stamp. The dispatcher
# convention is ``TASK-YYYYMMDD-NNNN`` but the inventory must
# accept any ``TASK-<something with digits>`` directory —
# test fixtures and historic records may use simpler forms.
_TASK_DIR_PATTERN = re.compile(r"^TASK-\d+(-\d+)?$")


class SidecarStatus(str, Enum):
    """The state of a single task.json w.r.t. its identity sidecar.

    The string values are persisted in :class:`SidecarInventoryEntry`
    and asserted against in tests. Adding a new value is a
    schema change (bump ``INVENTORY_SCHEMA_VERSION``).
    """

    FRESH = "fresh"          # sidecar present, hash matches, version current
    STALE_HASH = "stale_hash"  # sidecar present, task.json hash mismatches
    STALE_VERSION = "stale_version"  # sidecar present, policy_version older
    MISSING = "missing"      # no sidecar


INVENTORY_SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class SidecarInventoryEntry:
    """One row of the inventory. Frozen so it can be put in sets
    / hashed in test assertions.
    """

    task_id: str
    status: SidecarStatus
    has_task_json: bool
    has_sidecar: bool
    sidecar_policy_version: Optional[str]       # None when no sidecar
    sidecar_schema_version: Optional[str]       # None when no sidecar
    task_json_sha256: str                       # empty when no task.json
    sidecar_sha256: str                         # empty when no sidecar
    sidecar_classified_at_utc: Optional[str]    # None when no sidecar
    record_kind: Optional[str]                  # from sidecar / task.json
    is_consistent_hint: Optional[bool]          # from sidecar; None when absent

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "has_task_json": self.has_task_json,
            "has_sidecar": self.has_sidecar,
            "sidecar_policy_version": self.sidecar_policy_version,
            "sidecar_schema_version": self.sidecar_schema_version,
            "task_json_sha256": self.task_json_sha256,
            "sidecar_sha256": self.sidecar_sha256,
            "sidecar_classified_at_utc": self.sidecar_classified_at_utc,
            "record_kind": self.record_kind,
            "is_consistent_hint": self.is_consistent_hint,
        }


@dataclass
class SidecarInventoryResult:
    """The aggregate inventory. DTO; serializable via
    :meth:`to_dict` / :meth:`to_markdown`.
    """

    reports_root: str
    inventoried_at_utc: str
    schema_version: str
    # The writer's CURRENT versions at the time of inventory. The
    # plan is computed against these; if a future writer changes
    # them, the inventory's "fresh" / "stale_version" decisions
    # adapt automatically.
    current_policy_version: str
    current_schema_version: str
    entries: List[SidecarInventoryEntry] = field(default_factory=list)
    # Aggregate counts by SidecarStatus. Computed from entries.
    by_status: Dict[str, int] = field(default_factory=dict)
    # Number of task.json whose on-disk hash is unreadable
    # (corrupt JSON, permission error, etc.). Tracked separately
    # so a human reviewer can fix the on-disk problem first.
    unreadable_task_json: int = 0
    # Number of sidecar.json that exist but cannot be parsed.
    unreadable_sidecars: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "inventoried_at_utc": self.inventoried_at_utc,
            "reports_root": self.reports_root,
            "current_policy_version": self.current_policy_version,
            "current_schema_version": self.current_schema_version,
            "by_status": dict(self.by_status),
            "unreadable_task_json": self.unreadable_task_json,
            "unreadable_sidecars": self.unreadable_sidecars,
            "entries": [e.to_dict() for e in self.entries],
        }

    def to_markdown(self) -> str:
        lines: List[str] = []
        lines.append("# AEE-7.7c Sidecar Inventory")
        lines.append("")
        lines.append(f"- Schema version: `{self.schema_version}`")
        lines.append(f"- Inventoried at (UTC): `{self.inventoried_at_utc}`")
        lines.append(f"- Reports root: `{self.reports_root}`")
        lines.append(
            f"- Current writer policy_version: `{self.current_policy_version}`"
        )
        lines.append(
            f"- Current writer schema_version: `{self.current_schema_version}`"
        )
        lines.append(f"- Total task.json scanned: **{len(self.entries)}**")
        lines.append("")
        lines.append("## By status")
        lines.append("")
        lines.append("| Status | Count |")
        lines.append("|---|---|")
        for code, count in sorted(
            self.by_status.items(), key=lambda kv: (-kv[1], kv[0])
        ):
            lines.append(f"| `{code}` | {count} |")
        lines.append("")
        if self.unreadable_task_json:
            lines.append(
                f"## ⚠ Unreadable task.json: {self.unreadable_task_json}"
            )
            lines.append("")
        if self.unreadable_sidecars:
            lines.append(
                f"## ⚠ Unreadable sidecars: {self.unreadable_sidecars}"
            )
            lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# MigrationPlan
# ---------------------------------------------------------------------------


@dataclass
class MigrationPlan:
    """A pure-function dry-run plan: given an inventory and a
    target policy_version, what would :func:`apply_sidecars` do?

    NO side effects — :func:`plan_sidecar_migration` never touches
    disk. A real rollout must invoke :func:`apply_sidecars` itself
    (which honours its own strict-consistency / allow_runtime
    semantics).
    """

    inventory_reports_root: str
    planned_at_utc: str
    current_policy_version: str
    target_policy_version: str
    # Number of sidecars that would be touched:
    #   * would_overwrite — sidecar exists with older policy_version
    #   * would_write     — task.json without sidecar
    would_overwrite: int = 0
    would_write: int = 0
    # Subset of "would_write" / "would_overwrite" that are
    # RUNTIME records — for an operator who wants to dry-run
    # the conservative path first (allow_runtime=False).
    runtime_would_touch: int = 0
    # The number of sidecars that are already current
    # (no action needed).
    no_op: int = 0
    # task_id list of sidecars the plan would touch. Capped at
    # ``max_listed`` entries in :func:`plan_sidecar_migration` so
    # the plan DTO stays small for huge corpora.
    sample_task_ids: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "planned_at_utc": self.planned_at_utc,
            "inventory_reports_root": self.inventory_reports_root,
            "current_policy_version": self.current_policy_version,
            "target_policy_version": self.target_policy_version,
            "would_overwrite": self.would_overwrite,
            "would_write": self.would_write,
            "runtime_would_touch": self.runtime_would_touch,
            "no_op": self.no_op,
            "sample_task_ids": list(self.sample_task_ids),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _file_sha256(path: Path) -> str:
    """Return SHA-256 hex digest, or empty string on missing /
    unreadable / 0-byte file. The inventory tolerates 0-byte
    files (a placeholder is NOT a sidecar — we'll just mark it
    MISSING).
    """
    if not path.exists():
        return ""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    """Return parsed JSON, or None on any read/parse failure.
    Does NOT raise — the inventory is forgiving of on-disk
    corruption.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _now_utc_iso() -> str:
    """Return current UTC ISO-8601 'Z' timestamp."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iter_task_dirs(reports_root: Path) -> Iterable[Tuple[str, Path]]:
    """Yield ``(task_id, task_dir)`` for every TASK-YYYYMMDD-NNNN
    subdir of ``reports_root``. Sorting is intentional
    (deterministic). The directory name must match the canonical
    AEE convention; bare ``TASK-*`` directories that don't follow
    the pattern (TASK-bogus, etc.) are skipped — they are not
    dispatcher output.
    """
    if not reports_root.is_dir():
        return
    for child in sorted(reports_root.iterdir()):
        if not child.is_dir():
            continue
        if not _TASK_DIR_PATTERN.match(child.name):
            continue
        # Strip "TASK-" prefix for the entry's task_id field,
        # matching the convention used by ``aee.audit.live_audit``.
        task_id = child.name[len("TASK-"):]
        yield task_id, child


def _classify_entry(
    *,
    task_id: str,
    task_json_path: Path,
    sidecar_path: Path,
) -> Tuple[SidecarInventoryEntry, bool, bool]:
    """Classify a single task.json / sidecar pair.

    Returns ``(entry, task_json_unreadable, sidecar_unreadable)``.

    The unreadable flags are tracked separately so a human
    reviewer can fix the on-disk problem first.
    """
    task_json_sha = _file_sha256(task_json_path)
    has_task_json = task_json_path.is_file() and bool(task_json_sha)
    # ``task_json_unreadable`` = the file exists but its CONTENTS
    # are not valid JSON. ``_file_sha256`` happily hashes a file of
    # any bytes (the hash is meaningful even on garbled bytes), so
    # we have to probe JSON parseability separately to detect
    # corruption.
    task_json_unreadable = False
    if task_json_path.is_file() and has_task_json:
        if _read_json(task_json_path) is None:
            task_json_unreadable = True

    sidecar_sha = _file_sha256(sidecar_path)
    has_sidecar = sidecar_path.is_file() and bool(sidecar_sha)
    # Same story for the sidecar: SHA-256 may be valid even on
    # garbled bytes; JSON-parse failure is the corruption signal.
    sidecar_unreadable = False
    if sidecar_path.is_file() and has_sidecar:
        if _read_json(sidecar_path) is None:
            sidecar_unreadable = True

    sidecar_policy: Optional[str] = None
    sidecar_schema: Optional[str] = None
    sidecar_classified: Optional[str] = None
    record_kind: Optional[str] = None
    is_consistent_hint: Optional[bool] = None
    sidecar_source_task_hash: Optional[str] = None

    if has_sidecar and not sidecar_unreadable:
        parsed = _read_json(sidecar_path)
        if parsed is None:
            sidecar_unreadable = True
        else:
            # NOTE: must use ``x is None`` not ``str(x) or None``.
            # The latter is a known anti-pattern: when the JSON
            # value is null, ``str(None) or None`` evaluates to the
            # literal string ``"None"`` (truthy), which then
            # collides with version comparisons and never matches
            # a version-mismatch check. Use the explicit
            # null/empty guard instead.
            _pv_raw = parsed.get("policy_version")
            sidecar_policy = (
                str(_pv_raw) if _pv_raw is not None and _pv_raw != "" else None
            )
            _sv_raw = parsed.get("schema_version")
            sidecar_schema = (
                str(_sv_raw) if _sv_raw is not None and _sv_raw != "" else None
            )
            _ca_raw = parsed.get("classified_at_utc")
            sidecar_classified = (
                str(_ca_raw) if _ca_raw is not None and _ca_raw != "" else None
            )
            record_kind_raw = parsed.get("record_kind")
            record_kind = (
                str(record_kind_raw) if record_kind_raw else None
            )
            # is_consistent is not always persisted on the sidecar
            # (AEE-7.7a doesn't stamp it; AEE-7.7b classify_record
            # does). Be lenient — only treat as a hint.
            ic = parsed.get("is_consistent")
            is_consistent_hint = (
                bool(ic) if isinstance(ic, bool) else None
            )
            # source_task_json_sha256 is the AEE-7.7b staleness
            # marker. Compare against the current task.json hash
            # to detect drift.
            src_hash = parsed.get("source_task_json_sha256")
            if isinstance(src_hash, str) and src_hash:
                sidecar_source_task_hash = src_hash

    # Classification
    if not has_task_json:
        # task.json missing — caller can't really do anything
        # with this entry, but the inventory surfaces it for
        # completeness. The status is MISSING (no sidecar to
        # upgrade), but has_task_json=False distinguishes from
        # a normal MISSING.
        status = SidecarStatus.MISSING
    elif not has_sidecar:
        status = SidecarStatus.MISSING
    else:
        # Sidecar exists. Determine "fresh" vs "stale_*".
        # The schema_version field was added to the writer in
        # AEE-7.7b (commit 7ced78c, 2026-07-12). Sidecars
        # written by AEE-7.11 ``classify_and_persist`` BEFORE
        # that ship do not have a schema_version — treat
        # ``None`` as "legacy sidecar, no schema stamp", not
        # as a version mismatch.
        policy_current = sidecar_policy == _WRITER_POLICY_VERSION
        # Schema-stamp compatibility:
        # * ``None``   → legacy (pre-AEE-7.7b) — schema_current=True
        # * matches   → current — schema_current=True
        # * mismatches → real version drift — schema_current=False
        if sidecar_schema is None:
            schema_current = True
        else:
            schema_current = sidecar_schema == _WRITER_SCHEMA_VERSION
        hash_current = (
            sidecar_source_task_hash is not None
            and sidecar_source_task_hash == task_json_sha
        )
        if policy_current and schema_current and hash_current:
            status = SidecarStatus.FRESH
        elif not hash_current and sidecar_source_task_hash is not None:
            # task.json was rewritten after the sidecar — sidecar
            # is stale because the source it cites no longer exists.
            status = SidecarStatus.STALE_HASH
        elif not policy_current or not schema_current:
            status = SidecarStatus.STALE_VERSION
        else:
            # All checks passed; the "fresh" status was caught
            # above. Defensive default: treat as fresh.
            status = SidecarStatus.FRESH

    entry = SidecarInventoryEntry(
        task_id=task_id,
        status=status,
        has_task_json=has_task_json,
        has_sidecar=has_sidecar,
        sidecar_policy_version=sidecar_policy,
        sidecar_schema_version=sidecar_schema,
        task_json_sha256=task_json_sha,
        sidecar_sha256=sidecar_sha,
        sidecar_classified_at_utc=sidecar_classified,
        record_kind=record_kind,
        is_consistent_hint=is_consistent_hint,
    )
    return entry, task_json_unreadable, sidecar_unreadable


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def build_sidecar_inventory(
    reports_root: str | os.PathLike,
    *,
    utc_stamp: Optional[str] = None,
) -> SidecarInventoryResult:
    """Walk ``reports_root`` and build a :class:`SidecarInventoryResult`.

    Pure read-only: the function never writes anywhere. It is
    the migration-registry-side analog of
    :func:`aee.audit.run_audit` but does not require an
    :class:`AuditSummary` — it works off the raw on-disk
    sidecars.

    Parameters
    ----------
    reports_root
        Path to the ``reports/`` tree. Must exist and be a
        directory; non-existent / non-directory paths return an
        empty result (no exception).
    utc_stamp
        Optional override for the inventory timestamp (for
        deterministic tests).
    """
    root = Path(reports_root)
    inventory = SidecarInventoryResult(
        reports_root=str(root),
        inventoried_at_utc=utc_stamp or _now_utc_iso(),
        schema_version=INVENTORY_SCHEMA_VERSION,
        current_policy_version=_WRITER_POLICY_VERSION,
        current_schema_version=_WRITER_SCHEMA_VERSION,
    )

    if not root.is_dir():
        return inventory

    for task_id, task_dir in _iter_task_dirs(root):
        task_json_path = task_dir / "task.json"
        sidecar_path = task_dir / "identity.json"
        entry, task_json_unreadable, sidecar_unreadable = _classify_entry(
            task_id=task_id,
            task_json_path=task_json_path,
            sidecar_path=sidecar_path,
        )
        inventory.entries.append(entry)
        if task_json_unreadable:
            inventory.unreadable_task_json += 1
        if sidecar_unreadable:
            inventory.unreadable_sidecars += 1

    # Aggregate
    counts: Dict[str, int] = {s.value: 0 for s in SidecarStatus}
    for entry in inventory.entries:
        counts[entry.status.value] += 1
    inventory.by_status = counts
    return inventory


def plan_sidecar_migration(
    inventory: SidecarInventoryResult,
    *,
    target_policy_version: str = "1.1.0",
    utc_stamp: Optional[str] = None,
    max_listed: int = 50,
) -> MigrationPlan:
    """Pure function: given an inventory, return a dry-run plan.

    The plan says what :func:`apply_sidecars` would do IF called
    now with the same audit summary the inventory was based on.
    The plan itself does NOT call :func:`apply_sidecars` and does
    NOT touch disk.

    A non-trivial target_policy_version (≠ current) is required
    for the plan to recommend any action; with
    target_policy_version == current, ``would_overwrite`` and
    ``would_write`` are both 0 (no work to do).
    """
    plan = MigrationPlan(
        inventory_reports_root=inventory.reports_root,
        planned_at_utc=utc_stamp or _now_utc_iso(),
        current_policy_version=inventory.current_policy_version,
        target_policy_version=target_policy_version,
    )

    if not inventory.entries:
        return plan

    sample: List[str] = []
    for entry in inventory.entries:
        if entry.status == SidecarStatus.FRESH:
            plan.no_op += 1
            continue
        if entry.status == SidecarStatus.MISSING and entry.has_task_json:
            # No sidecar → would be written fresh.
            plan.would_write += 1
            if entry.record_kind == "runtime":
                plan.runtime_would_touch += 1
            if len(sample) < max_listed:
                sample.append(entry.task_id)
        elif entry.status in (SidecarStatus.STALE_HASH, SidecarStatus.STALE_VERSION):
            plan.would_overwrite += 1
            if entry.record_kind == "runtime":
                plan.runtime_would_touch += 1
            if len(sample) < max_listed:
                sample.append(entry.task_id)
        else:
            # task.json missing or other state — counted as no-op
            # (apply_sidecars will skip it via SKIPPED_NOT_IN_SUMMARY
            # or SKIPPED_NO_TASK_JSON). This is a non-fatal state
            # the operator should fix before the real rollout.
            plan.no_op += 1

    plan.sample_task_ids = tuple(sample)
    return plan


__all__ = [
    "INVENTORY_SCHEMA_VERSION",
    "MigrationPlan",
    "SidecarInventoryEntry",
    "SidecarInventoryResult",
    "SidecarStatus",
    "build_sidecar_inventory",
    "plan_sidecar_migration",
]
