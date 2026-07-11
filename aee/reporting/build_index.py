"""AEE Audit Namespace Hardening — build the audit index.

This is a one-shot CLI that:

1. Walks ``reports/`` and classifies every report.
2. Writes a companion ``identity.json`` next to every
   FIXTURE / UNKNOWN ``task.json`` (RUNTIME records get a
   sidecar only on demand).
3. Emits a manifest at ``audit/identity_index_<TS>.json`` that
   catalogs every report and the per-bucket counts.
4. Emits a mapping at ``audit/mapping_<TS>.json`` that links
   any user-provided alias (e.g. ``TASK-20260711-0018`` in
   audit session) to the canonical task_id (e.g.
   ``TASK-20260711-0015``) with full evidence.

The CLI never modifies ``task.json`` itself and never touches
the dispatcher SQLite. It is read-only against the dispatcher
hot path.

AEE-7.7b wire-up
-----------------

This module is the **G2 call-site migration**. The legacy
implementation called ``classify_record`` + ``write_identity_sidecar``
inline for every record (per-record read+write loop). That logic
has been replaced with the canonical two-step API:

1. :func:`aee.audit.run_audit` — produces a read-only ``AuditSummary``
   over the same ``reports/`` tree, with full consistency verdicts.
2. :func:`aee.audit.apply_sidecars` — turns the summary into
   persisted ``identity.json`` sidecars (deterministic, idempotent,
   secret-safe). The per-task executor anchors (the canonical real
   executor's ``executor_session_id`` / ``runtime_run_id`` /
   ``user_provided_alias``) are now passed in via
   ``apply_sidecars(..., executor_anchors={task_id: {...}},
   user_provided_alias={task_id: alias})``.

The output shape (``summary`` + ``reports`` dict) is preserved
byte-for-byte so downstream consumers (the audit's
``identity_index_<TS>.json`` artifact + the alias mapping) see no
change.

Usage
-----
::

    # default: report root = ./reports, audit dir = ~/Abacus/AEE/audit
    .venv/bin/python -m aee.reporting.build_index

    # custom paths
    .venv/bin/python -m aee.reporting.build_index \\
        --reports-root ./reports \\
        --audit-dir  /home/ubuntu/Abacus/AEE/audit \\
        --alias TASK-20260711-0018=TASK-20260711-0015 \\
        --executor-session AEE-R1-R4-ATOMIC-COMMITS-20260711 \\
        --runtime-run-id r1-r4-atomic-20260711-1258

The script is idempotent: re-running writes a new timestamped
artifact (no overwrite) and re-writes sidecars atomically.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# AEE-7.7b G2 call-site migration: the legacy module imported
# classify_record / write_identity_sidecar / load_task_json /
# iter_reports / classify_and_persist directly. Those primitives
# are still the right low-level helpers; the new top-level API
# is run_audit + apply_sidecars. We retain the imports below for
# the inner loop's report-shape construction (which doesn't need
# the SOT sidecar writer anymore).
from .identity import (
    Identity,
    RecordKind,
    SentinelPolicy,
    _file_sha256,
    classify_record,
    iter_reports,
    load_task_json,
)


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_alias(arg: str) -> Tuple[str, str]:
    if "=" not in arg:
        raise argparse.ArgumentTypeError(
            f"--alias expects KEY=VALUE, got {arg!r}"
        )
    k, v = arg.split("=", 1)
    return k.strip(), v.strip()


def _parse_kv(arg: str) -> Tuple[str, str]:
    if "=" not in arg:
        raise argparse.ArgumentTypeError(
            f"expect KEY=VALUE, got {arg!r}"
        )
    k, v = arg.split("=", 1)
    return k.strip(), v.strip()


def build_index(
    *,
    reports_root: Path,
    audit_dir: Path,
    aliases: Optional[Dict[str, str]] = None,
    executor_session_id: Optional[str] = None,
    runtime_run_id: Optional[str] = None,
    final_head_sha: Optional[str] = None,
    commit_shas: Optional[List[str]] = None,
    telegram_message_id: Optional[str] = None,
    sidecar_for_runtime: bool = False,
    classified_at_utc: Optional[str] = None,
) -> Dict[str, Any]:
    """Walk reports, classify, write sidecars + index. Returns
    the summary dict (also written to ``audit_dir``).

    AEE-7.7b G2: the inline ``classify_record`` +
    ``write_identity_sidecar`` per-record loop has been replaced
    with a two-step ``run_audit`` + ``apply_sidecars`` call.
    The per-record shape (``reports[]`` dict items) is built
    from the same inputs as before (the ``AuditSummary`` plus a
    re-read of the raw ``task.json`` for fields the audit
    doesn't surface — ``hermes_run_id`` / ``title`` /
    ``progress_pct`` / ``status``).

    The output ``summary`` dict is preserved byte-compatibly so
    downstream consumers (the index artifact + alias mapping)
    see no change.
    """
    # Local import to keep ``build_index`` importable even if
    # ``aee.audit`` is not on the import path (it always is in
    # the bridge repo, but tests in isolation may need the
    # local-only path).
    from aee.audit import apply_sidecars, run_audit

    ts = classified_at_utc or _now_utc()
    policy = SentinelPolicy()
    reports: List[Dict[str, Any]] = []
    counts = {k.value: 0 for k in RecordKind}
    counts["errors"] = 0

    # --- Step 1: read-only audit over reports/ ---
    # The audit also writes its own json+md into ``audit_dir``
    # (one-shot artifact for downstream CI / archival). We
    # capture the summary so apply_sidecars can use the
    # consistency verdicts (is_consistent=True ⇒ safe to
    # auto-overwrite; is_consistent=False ⇒ leave for human
    # review).
    audit_dir.mkdir(parents=True, exist_ok=True)
    summary, _, _ = run_audit(
        reports_root,
        audit_dir,
        policy=policy,
        utc_stamp=ts,
    )

    # --- Step 2: per-task anchor map for the canonical real executor ---
    # The legacy CLI only stamped the canonical task (the value
    # side of the --alias mapping). Generalise to per-task
    # maps so future callers can stamp multiple tasks.
    canonical_task_ids = set((aliases or {}).values())
    executor_anchors_map: Dict[str, Dict[str, str]] = {}
    if executor_session_id is not None or runtime_run_id is not None:
        for canonical in canonical_task_ids:
            executor_anchors_map[canonical] = {
                "executor_session_id": executor_session_id or "",
                "runtime_run_id": runtime_run_id or "",
            }
    user_alias_map: Dict[str, str] = {}
    for alias_key, alias_value in (aliases or {}).items():
        user_alias_map[alias_value] = alias_key

    # --- Step 3: turn the summary into persisted sidecars ---
    apply_result = apply_sidecars(
        reports_root,
        summary,
        utc_stamp=ts,
        classified_at_override=ts,
        policy=policy,
        force=False,
        allow_runtime=sidecar_for_runtime,
        strict_consistency=True,
        executor_anchors=executor_anchors_map or None,
        user_provided_alias=user_alias_map or None,
    )

    # --- Step 4: build the per-record reports[] for the manifest ---
    # The apply result's outcomes list is the authoritative
    # record of what happened (wrote / unchanged / overwrote /
    # skipped_*). We re-read each task.json only for the small
    # set of fields the audit doesn't expose (hermes_run_id /
    # title / progress_pct / status) — these are convenience
    # for the manifest reader, not authoritative for
    # classification.
    by_task_outcome = {o.task_id: o for o in apply_result.outcomes}
    for task_id, task_json_path in iter_reports(reports_root):
        raw = load_task_json(task_json_path)
        if raw is None:
            counts["errors"] += 1
            continue
        sha = _file_sha256(task_json_path)
        outcome = by_task_outcome.get(task_id)
        if outcome is None:
            # The summary did not cover this record (e.g.
            # concurrent write). Skip from the manifest so we
            # never claim a sidecar we didn't touch.
            continue
        record_kind = outcome.record_kind or RecordKind.UNKNOWN.value
        # user_provided_alias / executor_session_id /
        # runtime_run_id are pulled from the per-task anchor
        # map (canonical task) OR the existing sidecar (other
        # tasks). Reading the existing sidecar is the most
        # consistent way to get the merged view (apply_sidecars
        # already merged; we just read it back).
        from .identity import read_identity_sidecar
        existing_sidecar = read_identity_sidecar(task_json_path)
        if record_kind in counts:
            counts[record_kind] += 1
        reports.append(
            {
                "task_id": task_id,
                "record_kind": record_kind,
                "is_fixture": bool(
                    existing_sidecar.is_fixture if existing_sidecar else False
                ),
                "fixture_markers": list(
                    existing_sidecar.fixture_markers
                    if existing_sidecar else ()
                ),
                "hermes_run_id": raw.get("hermes_run_id"),
                "title": raw.get("title"),
                "progress_pct": raw.get("progress_pct"),
                "status": raw.get("status"),
                "task_json_sha256": sha,
                "user_provided_alias": (
                    existing_sidecar.user_provided_alias
                    if existing_sidecar else None
                ),
                "executor_session_id": (
                    existing_sidecar.executor_session_id
                    if existing_sidecar else None
                ),
                "runtime_run_id": (
                    existing_sidecar.runtime_run_id
                    if existing_sidecar else None
                ),
                "sidecar_written": (
                    outcome.decision in ("wrote", "overwrote", "unchanged")
                ),
            }
        )

    out_summary = {
        "audit_record_type": "identity_index",
        "audit_artifact_version": "1.0.0",
        "classified_at_utc": ts,
        "reports_root": str(reports_root),
        "final_head_sha": final_head_sha,
        "commit_shas": commit_shas or [],
        "executor_session_id": executor_session_id,
        "runtime_run_id": runtime_run_id,
        "telegram_message_id": telegram_message_id,
        "policy": {
            "hermes_run_id_sentinels": sorted(
                policy.hermes_run_id_sentinels
            ),
            "fixture_titles": sorted(policy.fixture_titles),
            "fixture_run_id_patterns": [
                p.pattern for p in policy.fixture_run_id_patterns
            ],
            "stuck_pct": policy.stuck_pct,
            "flag_stuck_running": policy.flag_stuck_running,
        },
        "counts": counts,
        "total_reports": len(reports),
        "aliases": aliases or {},
        # AEE-7.7b: the apply-sidecars side-bucket is
        # exposed under a namespaced key so downstream
        # consumers can verify the wire-up without
        # recomputing from the per-record reports[] list.
        "apply_sidecars": {
            "by_decision": dict(apply_result.by_decision),
            "by_record_kind": dict(apply_result.by_record_kind),
            "anchor_warning_count": apply_result.anchor_warning_count,
            "sidecars_written": apply_result.sidecars_written,
            "schema_version": apply_result.schema_version,
        },
    }
    return {
        "summary": out_summary,
        "reports": reports,
    }


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="aee.reporting.build_index",
        description=(
            "Walk reports/, classify each report as "
            "RUNTIME / FIXTURE / UNKNOWN, persist sidecars, "
            "and emit the audit index + mapping."
        ),
    )
    p.add_argument(
        "--reports-root",
        default="./reports",
        help="Path to the reports/ directory (default: ./reports).",
    )
    p.add_argument(
        "--audit-dir",
        default="/home/ubuntu/Abacus/AEE/audit",
        help="Output directory for the audit index + mapping.",
    )
    p.add_argument(
        "--alias",
        action="append",
        type=_parse_alias,
        default=[],
        help=(
            "User-provided alias mapping. Format: "
            "USER_ALIAS=CANONICAL_TASK_ID. May be repeated."
        ),
    )
    p.add_argument(
        "--executor-session",
        default=None,
        help=(
            "Executor session_id to stamp on the canonical "
            "task (the one in --alias values)."
        ),
    )
    p.add_argument(
        "--runtime-run-id",
        default=None,
        help=(
            "Executor's real run_id (NOT the dispatcher's "
            "hermes_run_id sentinel) to stamp on the canonical "
            "task."
        ),
    )
    p.add_argument(
        "--final-head-sha",
        default=None,
        help="Repo HEAD SHA at the time of this audit (string).",
    )
    p.add_argument(
        "--commit-sha",
        action="append",
        default=[],
        help=(
            "A canonical commit SHA. May be repeated; all "
            "values are listed in the manifest."
        ),
    )
    p.add_argument(
        "--telegram-message-id",
        default=None,
        help="Telegram message_id that authoritatively links to this audit.",
    )
    p.add_argument(
        "--sidecar-for-runtime",
        action="store_true",
        help=(
            "Also write identity.json next to RUNTIME records "
            "(default: only FIXTURE/UNKNOWN get a sidecar)."
        ),
    )
    args = p.parse_args(argv)

    aliases = dict(args.alias) if args.alias else {}
    audit_dir = Path(args.audit_dir)
    audit_dir.mkdir(parents=True, exist_ok=True)
    reports_root = Path(args.reports_root)

    result = build_index(
        reports_root=reports_root,
        audit_dir=audit_dir,
        aliases=aliases,
        executor_session_id=args.executor_session,
        runtime_run_id=args.runtime_run_id,
        final_head_sha=args.final_head_sha,
        commit_shas=list(args.commit_sha or []),
        telegram_message_id=args.telegram_message_id,
        sidecar_for_runtime=args.sidecar_for_runtime,
    )
    summary = result["summary"]
    reports = result["reports"]
    ts = summary["classified_at_utc"]
    # Filename-safe timestamp (replace ':' with '-')
    ts_safe = ts.replace(":", "-")

    # Write the per-classification index.
    index_path = audit_dir / f"identity_index_{ts_safe}.json"
    index_path.write_text(
        json.dumps(
            {"summary": summary, "reports": reports},
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # Write the mapping (alias → canonical) with full evidence.
    mapping = {
        "audit_record_type": "task_id_identity_mapping",
        "audit_artifact_version": "1.0.0",
        "classified_at_utc": ts,
        "final_head_sha": summary["final_head_sha"],
        "commit_shas": summary["commit_shas"],
        "executor_session_id": summary["executor_session_id"],
        "runtime_run_id": summary["runtime_run_id"],
        "telegram_message_id": summary["telegram_message_id"],
        "authoritative_mapping": aliases,
        "alias_lookup": {
            "by_alias": aliases,
            "by_canonical": {v: k for k, v in aliases.items()},
        },
        "notes": [
            "Each USER_ALIAS resolves to exactly one CANONICAL_TASK_ID.",
            "CANONICAL_TASK_ID MUST be the value in reports/<id>/task.json.",
            "Any report whose task_id is NOT a CANONICAL value is by "
            "definition EITHER a sibling real-execution OR a fixture; "
            "consumers must call classify_record() to disambiguate.",
        ],
    }
    mapping_path = audit_dir / f"mapping_{ts_safe}.json"
    mapping_path.write_text(
        json.dumps(mapping, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )

    # Print the summary (handy for cron / dry-run verification).
    out = {
        "index_path": str(index_path),
        "mapping_path": str(mapping_path),
        "counts": summary["counts"],
        "total_reports": summary["total_reports"],
        "ts_utc": ts,
    }
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
