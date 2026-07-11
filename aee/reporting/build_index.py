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

from .identity import (
    Identity,
    RecordKind,
    SentinelPolicy,
    _file_sha256,
    classify_and_persist,
    classify_record,
    iter_reports,
    load_task_json,
    write_identity_sidecar,
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
    the summary dict (also written to ``audit_dir``)."""
    ts = classified_at_utc or _now_utc()
    policy = SentinelPolicy()
    reports: List[Dict[str, Any]] = []
    counts = {k.value: 0 for k in RecordKind}
    counts["errors"] = 0

    for task_id, task_json_path in iter_reports(reports_root):
        raw = load_task_json(task_json_path)
        if raw is None:
            counts["errors"] += 1
            continue
        sha = _file_sha256(task_json_path)
        # Cross-reference: does the user know this task by
        # another name? If yes, store the alias on the
        # identity sidecar for traceability.
        # Inverted lookup: if task_id IS a value in
        # aliases, then the key is the user-provided alias.
        user_alias = None
        for alias_key, alias_value in (aliases or {}).items():
            if alias_value == task_id:
                user_alias = alias_key
                break
        # Per-record executor anchors: if THIS task is the
        # canonical real executor, stamp it. Otherwise leave
        # None (the audit can correlate via the manifest).
        this_exec = (
            executor_session_id
            if task_id in (aliases or {}).values()
            else None
        )
        this_run = (
            runtime_run_id
            if task_id in (aliases or {}).values()
            else None
        )
        identity = classify_record(
            task_id=task_id,
            task_json=raw,
            policy=policy,
            user_provided_alias=user_alias,
            executor_session_id=this_exec,
            runtime_run_id=this_run,
            source_task_json_sha256=sha,
            classified_at_utc=ts,
        )
        if identity.record_kind != RecordKind.RUNTIME or sidecar_for_runtime:
            write_identity_sidecar(task_json_path, identity)
        counts[identity.record_kind.value] += 1
        reports.append(
            {
                "task_id": task_id,
                "record_kind": identity.record_kind.value,
                "is_fixture": identity.is_fixture,
                "fixture_markers": identity.fixture_markers,
                "hermes_run_id": raw.get("hermes_run_id"),
                "title": raw.get("title"),
                "progress_pct": raw.get("progress_pct"),
                "status": raw.get("status"),
                "task_json_sha256": sha,
                "user_provided_alias": identity.user_provided_alias,
                "executor_session_id": identity.executor_session_id,
                "runtime_run_id": identity.runtime_run_id,
                "sidecar_written": (
                    identity.record_kind != RecordKind.RUNTIME
                    or sidecar_for_runtime
                ),
            }
        )

    summary = {
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
    }
    return {
        "summary": summary,
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
