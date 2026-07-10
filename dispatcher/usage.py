"""Usage aggregation + cost rollup (Phase 2 P4).

Reads the `usage` JSON stored on each completed task and rolls it up
by (period, task_type, model). Source of truth is the same SQLite
DB the rest of the dispatcher uses; we read it read-only.

Usage schema (from Hermes M2 /v1/runs/{id}):
    {
        "input_tokens": 1234,
        "output_tokens": 567,
        "total_tokens": 1801,
        "model": "claude-sonnet-4-6",
        ...
    }

The bridge already captures `usage` and `model_name` on every
completed task (see `manager.complete()`). This module just queries
and aggregates.

Output schema:
    {
        "period": "today",
        "totals": {
            "task_count": N, "input_tokens": N, "output_tokens": N,
            "estimated_cost_usd": float
        },
        "by_type":   [...],
        "by_model":  [...],
        "by_day":    [...]   (for longer windows)
    }
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import load as config_load
from dispatcher.db import DB_PATH


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def _usage_dict(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _cost_per_1m(model: Optional[str]) -> tuple[float, float]:
    pricing = config_load("pricing").get("models", {})
    p = pricing.get(model or "", pricing.get("default", {"input_per_1m": 0, "output_per_1m": 0}))
    return float(p.get("input_per_1m", 0.0)), float(p.get("output_per_1m", 0.0))


def _normalize_usage(usage: Dict[str, Any]) -> Dict[str, int]:
    """Accept either the OpenAI-style schema
    ({input_tokens, output_tokens, total_tokens}) or Hermes M2's
    short-form ({p, c, t}). Always return {input_tokens,
    output_tokens, total_tokens} with integer values.
    """
    if not usage:
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    inp = int(usage.get("input_tokens") or usage.get("p") or 0)
    out = int(usage.get("output_tokens") or usage.get("c") or 0)
    tot = int(usage.get("total_tokens") or usage.get("t") or (inp + out))
    return {"input_tokens": inp, "output_tokens": out, "total_tokens": tot}


def _row_cost(usage: Dict[str, Any], model: Optional[str]) -> float:
    n = _normalize_usage(usage)
    p_in, p_out = _cost_per_1m(model)
    return (n["input_tokens"] / 1_000_000) * p_in + (n["output_tokens"] / 1_000_000) * p_out


def aggregate(
    period: str = "today",
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Aggregate usage over a period.

    `period` ∈ {today, 7d, 30d, all}.
    `task_id`: if set, ignores period and returns the single task's
    usage.
    """
    now = datetime.now(timezone.utc)
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "7d":
        start = now - timedelta(days=7)
    elif period == "30d":
        start = now - timedelta(days=30)
    elif period == "all":
        start = datetime(1970, 1, 1, tzinfo=timezone.utc)
    else:
        raise ValueError(f"unknown period: {period!r}")

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        if task_id:
            rows = conn.execute(
                "SELECT t.task_id, t.type, t.model_name, t.status, o.usage_json, "
                "       t.started_at, t.finished_at "
                "FROM tasks t LEFT JOIN task_outputs o ON t.task_id = o.task_id "
                "WHERE t.task_id = ?",
                (task_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT t.task_id, t.type, t.model_name, t.status, o.usage_json, "
                "       t.started_at, t.finished_at "
                "FROM tasks t LEFT JOIN task_outputs o ON t.task_id = o.task_id "
                "WHERE t.finished_at IS NOT NULL AND t.finished_at >= ?",
                (start.isoformat().replace("+00:00", "Z"),),
            ).fetchall()
    finally:
        conn.close()

    by_type: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "task_count": 0, "input_tokens": 0, "output_tokens": 0,
        "estimated_cost_usd": 0.0,
    })
    by_model: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "task_count": 0, "input_tokens": 0, "output_tokens": 0,
        "estimated_cost_usd": 0.0,
    })
    by_day: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "task_count": 0, "input_tokens": 0, "output_tokens": 0,
        "estimated_cost_usd": 0.0,
    })
    # Track the label per bucket so we can return it in by_type/by_model.
    _type_label: Dict[str, str] = {}
    _model_label: Dict[str, str] = {}
    totals = {
        "task_count": 0, "input_tokens": 0, "output_tokens": 0,
        "estimated_cost_usd": 0.0,
    }
    per_task: List[Dict[str, Any]] = []

    for r in rows:
        if r[3] not in ("completed", "failed", "timeout", "cancelled"):
            continue
        usage = _usage_dict(r[4])
        n = _normalize_usage(usage)
        inp = n["input_tokens"]
        out = n["output_tokens"]
        type_label = r[1] or "unknown"
        model_label = r[2] or usage.get("model") or "unknown"
        model = r[2] or usage.get("model")
        cost = _row_cost(usage, model)
        for d in (by_type[type_label], by_model[model_label]):
            d["task_count"] += 1
            d["input_tokens"] += inp
            d["output_tokens"] += out
            d["estimated_cost_usd"] += cost
        # Day bucket (UTC date).
        day = (r[5] or r[6] or "")[:10]  # YYYY-MM-DD
        if day:
            dd = by_day[day]
            dd["task_count"] += 1
            dd["input_tokens"] += inp
            dd["output_tokens"] += out
            dd["estimated_cost_usd"] += cost
        totals["task_count"] += 1
        totals["input_tokens"] += inp
        totals["output_tokens"] += out
        totals["estimated_cost_usd"] += cost
        per_task.append({
            "task_id": r[0],
            "type": r[1],
            "model": model,
            "status": r[3],
            "input_tokens": inp,
            "output_tokens": out,
            "estimated_cost_usd": round(cost, 6),
            "started_at": r[5],
            "finished_at": r[6],
        })

    def _round(d: Dict[str, Any]) -> Dict[str, Any]:
        return {
            **d,
            "estimated_cost_usd": round(d["estimated_cost_usd"], 6),
        }

    by_day_list: List[Any] = []
    if period in ("7d", "30d", "all"):
        for day, d in sorted(by_day.items()):
            rounded = _round(d)
            by_day_list.append({"day": day, **rounded})

    return {
        "period": period,
        "task_id": task_id,
        "totals": _round(totals),
        "by_type": [
            {"type": k, **_round(v)}
            for k, v in sorted(by_type.items(), key=lambda kv: -kv[1]["estimated_cost_usd"])
        ],
        "by_model": [
            {"model": k, **_round(v)}
            for k, v in sorted(by_model.items(), key=lambda kv: -kv[1]["estimated_cost_usd"])
        ],
        "by_day": by_day_list,
        "per_task": per_task if task_id else per_task[:50],  # cap
    }
