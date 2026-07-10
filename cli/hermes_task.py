"""Hermes Task CLI — observability for the Dispatcher.

Usage:
    hermes-task list [--status S] [--type T] [--limit N]
    hermes-task show <TASK_ID>
    hermes-task logs <TASK_ID> [--tail N]
    hermes-task rerun <TASK_ID> [--yes]

This CLI reads `data/dispatcher.db` directly (read-only), so it can
be used while the bridge is running. For actions that mutate state
(rerun) it goes through the bridge's HTTP API.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

# Make sibling packages importable when this file is run directly.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dispatcher.db import open_ro, db_path  # noqa: E402
from dispatcher.manager import TaskManager  # noqa: E402


def _format_table(headers: List[str], rows: List[List[str]]) -> str:
    if not rows:
        return "(no rows)"
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    sep = "  "
    out: List[str] = []
    out.append(sep.join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    out.append(sep.join("-" * widths[i] for i in range(len(headers))))
    for r in rows:
        out.append(sep.join(r[i].ljust(widths[i]) for i in range(len(headers))))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> int:
    manager = TaskManager()
    tasks = manager.list(status=args.status, type=args.type, limit=args.limit)
    if args.json:
        print(json.dumps([t.to_dict() for t in tasks], indent=2, ensure_ascii=False, default=str))
        return 0
    rows: List[List[str]] = []
    for t in tasks:
        pct = f"{t.progress_pct}%" + (f" {t.progress_step or ''}" if t.progress_step else "")
        rows.append([
            t.task_id,
            t.status.ljust(9),
            t.type,
            str(t.priority),
            pct,
            (t.title or "")[:40],
            t.created_at,
        ])
    print(_format_table(
        ["TASK_ID", "STATUS", "TYPE", "PRI", "PROGRESS", "TITLE", "CREATED"],
        rows,
    ))
    print(f"\n{len(tasks)} task(s)")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    manager = TaskManager()
    t = manager.get(args.task_id)
    if t is None:
        print(f"Task not found: {args.task_id}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(t.to_dict(), indent=2, ensure_ascii=False, default=str))
        return 0
    d = t.to_dict()
    print(f"Task: {d['task_id']}")
    for k in (
        "title", "type", "status", "priority", "owner",
        "progress_pct", "progress_step",
        "created_at", "started_at", "finished_at", "duration_sec",
        "hermes_run_id", "openai_run_id", "session_id", "mode",
        "result_path", "error_message", "warning_count", "retry_count",
        "prompt_version", "model_name", "git_commit", "git_branch",
    ):
        v = d.get(k)
        if v is not None and v != "":
            print(f"  {k:18s} {v}")
    print()
    print("Recent events:")
    events = manager.events(args.task_id, limit=20)
    for e in events[-20:]:
        line = f"  {e.ts} {e.kind}"
        if e.payload:
            keys = list(e.payload.keys())[:4]
            summary = ", ".join(f"{k}={e.payload[k]!s:.40}" for k in keys)
            line += f"  ({summary})"
        print(line)
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    from dispatcher.manager import _log_path
    p = _log_path(args.task_id)
    if not p.exists():
        print(f"No log file: {p}", file=sys.stderr)
        return 1
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if args.tail and len(lines) > args.tail:
        lines = lines[-args.tail:]
        print(f"... (truncated to last {args.tail} of {len(text.splitlines())} lines)")
    print("\n".join(lines))
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    from dispatcher.usage import aggregate
    if args.task:
        agg = aggregate(period="all", task_id=args.task)
    else:
        agg = aggregate(period=args.period)
    if args.json:
        print(json.dumps(agg, indent=2, ensure_ascii=False, default=str))
        return 0
    t = agg["totals"]
    print(f"=== usage ({agg['period']}{', task=' + agg['task_id'] if agg['task_id'] else ''}) ===")
    print(f"  tasks:        {t['task_count']}")
    print(f"  input_tokens: {t['input_tokens']:,}")
    print(f"  output_tokens:{t['output_tokens']:,}")
    print(f"  est. cost:    ${t['estimated_cost_usd']:.4f}")
    if agg["by_type"]:
        print()
        print("  by type:")
        for d in agg["by_type"]:
            tname = d.get("type") or d.get("task_type") or "?"
            print(f"    {tname}: "
                  f"{d['task_count']} tasks, "
                  f"in={d['input_tokens']:,} out={d['output_tokens']:,} "
                  f"${d['estimated_cost_usd']:.4f}")
    if agg["by_model"]:
        print()
        print("  by model:")
        for d in agg["by_model"]:
            print(f"    {d.get('model') or '?'}: "
                  f"{d['task_count']} tasks, "
                  f"in={d['input_tokens']:,} out={d['output_tokens']:,} "
                  f"${d['estimated_cost_usd']:.4f}")
    if agg.get("by_day"):
        print()
        print("  by day:")
        for d in agg["by_day"]:
            print(f"    {d['day']}: {d['task_count']} tasks, "
                  f"${d['estimated_cost_usd']:.4f}")
    return 0


def cmd_rerun(args: argparse.Namespace) -> int:
    import urllib.request
    import urllib.error
    base = os.getenv("BRIDGE_BASE_URL", "http://127.0.0.1:8787").rstrip("/")
    key = os.getenv("BRIDGE_API_KEY") or os.getenv("GPT_BRIDGE_API_KEY") or ""
    if not key:
        print("Set BRIDGE_API_KEY or GPT_BRIDGE_API_KEY to rerun via API.", file=sys.stderr)
        return 2
    if not args.yes:
        print(f"About to rerun {args.task_id} via {base}.")
        print("Pass --yes to skip this prompt.")
        return 2
    url = f"{base}/tasks/{args.task_id}/rerun"
    req = urllib.request.Request(url, method="POST")
    req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}", file=sys.stderr)
        return 1
    print(body)
    return 0


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hermes-task",
        description="Hermes Task Dispatcher CLI (read-only by default).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List recent tasks")
    p_list.add_argument("--status", choices=["pending", "queued", "running", "waiting", "completed", "failed", "cancelled"])
    p_list.add_argument("--type", choices=["research", "coding", "ops", "review", "normal"])
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--json", action="store_true", help="Emit JSON")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Show one task")
    p_show.add_argument("task_id")
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(func=cmd_show)

    p_logs = sub.add_parser("logs", help="Tail a task's log file")
    p_logs.add_argument("task_id")
    p_logs.add_argument("--tail", type=int, default=200)
    p_logs.set_defaults(func=cmd_logs)

    p_rerun = sub.add_parser("rerun", help="Rerun a failed/cancelled task via the bridge API")
    p_rerun.add_argument("task_id")
    p_rerun.add_argument("--yes", action="store_true")
    p_rerun.set_defaults(func=cmd_rerun)

    p_usage = sub.add_parser("usage", help="Aggregate token usage + cost")
    p_usage.add_argument("--today", dest="period", action="store_const", const="today", default="today")
    p_usage.add_argument("--7d", dest="period", action="store_const", const="7d")
    p_usage.add_argument("--30d", dest="period", action="store_const", const="30d")
    p_usage.add_argument("--all", dest="period", action="store_const", const="all")
    p_usage.add_argument("--task", help="Single task_id (overrides period)")
    p_usage.add_argument("--json", action="store_true")
    p_usage.set_defaults(func=cmd_usage)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
