"""Stale task reaper — marks in-flight tasks as `timeout` when they
exceed configured inactivity / total-age thresholds.

Design
------
Runs inside the watcher loop (one asyncio task) on every tick. Pure
functions: no I/O of its own, only manager.* side effects. Keeping it
separate from the watcher makes it unit-testable without HTTP.

Heuristics
----------
* `stale_running_sec` (default 1800s = 30 min): an in-flight `running`
  or `waiting` task whose *last* `progress` event is older than this
  is reaped. Rationale: if Hermes is still chugging, the watcher
  updates progress every 2s; if no progress has been written for
  30 minutes, the task is effectively dead.

* `stale_queued_sec` (default 300s = 5 min): a `queued` task that has
  not transitioned to `running` within this window is reaped.
  Rationale: the dispatch is synchronous, so a queued task > 5 min
  means the watcher crashed, the upstream 8642 is unreachable, or
  the bridge is wedged.

* `max_total_age_sec` (default 7200s = 2 hours): even if a task keeps
  emitting progress, if its total age exceeds this we still reap.
  Matches BRIDGE_MAX_TIMEOUT in app.py.

* `grace_period_sec` (default 30s): do NOT reap tasks whose created_at
  is within this window. Prevents a race where the task is just
  starting up and hasn't written its first progress event yet.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from dispatcher.manager import TaskManager, IllegalTransition

log = logging.getLogger("dispatcher.reaper")


@dataclass
class ReaperConfig:
    stale_running_sec: int = 1800
    stale_queued_sec: int = 300
    max_total_age_sec: int = 7200
    grace_period_sec: int = 30
    enabled: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "ReaperConfig":
        return cls(
            stale_running_sec=int(d.get("stale_running_sec", 1800)),
            stale_queued_sec=int(d.get("stale_queued_sec", 300)),
            max_total_age_sec=int(d.get("max_total_age_sec", 7200)),
            grace_period_sec=int(d.get("grace_period_sec", 30)),
            enabled=bool(d.get("enabled", True)),
        )


@dataclass
class ReapResult:
    reaped: List[str]              # task_ids that were just timed out
    skipped: List[Tuple[str, str]]  # (task_id, reason) that were NOT reaped
    scanned: int                    # how many candidates we looked at

    def as_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "reaped_count": len(self.reaped),
            "reaped": self.reaped,
            "skipped_count": len(self.skipped),
            "skipped": [{"task_id": t, "reason": r} for t, r in self.skipped],
        }


def _last_progress_ts(manager: TaskManager, task_id: str) -> Optional[float]:
    """Return the unix-epoch of the most recent heartbeat / progress
    for this task.

    Order of preference (AEE-2):
      1. `tasks.heartbeat_at` — set by the worker via
         POST /jobs/{id}/heartbeat. The most direct signal that
         the worker is still alive.
      2. `tasks.started_at` if heartbeat_at is null but the task
         is in `running` — better than nothing.
      3. Fall back to the most recent `progress` event (legacy
         path; the watcher used to advance progress every 2s).
      4. Fall back to `created_at`.
    """
    from datetime import datetime
    t = manager.get(task_id)
    if t is None:
        return None
    # AEE-2: prefer the heartbeat_at column when present.
    for ts_field in ("heartbeat_at", "started_at", "created_at"):
        raw = getattr(t, ts_field, None)
        if not raw:
            continue
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except Exception:  # noqa: BLE001
            continue
    # Legacy: scan events.
    ev = manager.events(task_id, limit=500)
    for e in ev:
        if e.kind == "progress":
            try:
                ts = datetime.fromisoformat(e.ts.replace("Z", "+00:00"))
                return ts.timestamp()
            except Exception:  # noqa: BLE001
                continue
    return None


def reap_once(manager: TaskManager, cfg: ReaperConfig) -> ReapResult:
    """Scan all in-flight tasks and reap any that are stale.

    Returns a `ReapResult` describing what happened. Side effects:
    `manager.timeout(task_id, reason)` for each reaped task (which
    itself writes a log line + emits a `timeout` event).
    """
    result = ReapResult(reaped=[], skipped=[], scanned=0)
    if not cfg.enabled:
        return result
    now = time.time()
    # Look at all in-flight statuses.
    in_flight = []
    for s in ("queued", "running", "waiting"):
        in_flight.extend(manager.list(status=s, limit=2000))
    result.scanned = len(in_flight)
    for t in in_flight:
        try:
            # Grace period + total age cap both need `created` (epoch).
            created: Optional[float] = None
            if t.created_at:
                from datetime import datetime
                created = datetime.fromisoformat(t.created_at.replace("Z", "+00:00")).timestamp()
            if created is not None and (now - created) < cfg.grace_period_sec:
                result.skipped.append((t.task_id, "in grace period"))
                continue
            # Total age cap
            if created is not None and (now - created) > cfg.max_total_age_sec:
                total_age = now - created
                reason = f"reaper: total age {total_age:.0f}s exceeds max_total_age_sec={cfg.max_total_age_sec}"
                manager.timeout(t.task_id, reason)
                result.reaped.append(t.task_id)
                continue
            # Status-specific thresholds
            if t.status == "queued":
                if created is None:
                    result.skipped.append((t.task_id, "no created_at"))
                    continue
                age = now - created
                if age > cfg.stale_queued_sec:
                    reason = f"reaper: queued {age:.0f}s exceeds stale_queued_sec={cfg.stale_queued_sec}"
                    manager.timeout(t.task_id, reason)
                    result.reaped.append(t.task_id)
                else:
                    result.skipped.append((t.task_id, f"queued {age:.0f}s < {cfg.stale_queued_sec}s"))
            else:
                # running / waiting
                last_p = _last_progress_ts(manager, t.task_id)
                if last_p is None:
                    result.skipped.append((t.task_id, "no last-progress ts (corrupt)"))
                    continue
                idle = now - last_p
                if idle > cfg.stale_running_sec:
                    # AEE-2: distinguish "worker stopped heartbeating"
                    # (timeout) from "adapter said it failed" (failed).
                    # Both are reaped via `manager.timeout` here; the
                    # distinct reason string makes it auditable.
                    if t.worker_id:
                        reason = (
                            f"reaper: worker_id={t.worker_id} no heartbeat for "
                            f"{idle:.0f}s (threshold={cfg.stale_running_sec}s)"
                        )
                    else:
                        reason = (
                            f"reaper: no progress for {idle:.0f}s "
                            f"(threshold={cfg.stale_running_sec}s)"
                        )
                    manager.timeout(t.task_id, reason)
                    result.reaped.append(t.task_id)
                else:
                    result.skipped.append((t.task_id, f"idle {idle:.0f}s < {cfg.stale_running_sec}s"))
        except IllegalTransition as exc:
            result.skipped.append((t.task_id, f"illegal transition: {exc}"))
        except Exception as exc:  # noqa: BLE001
            result.skipped.append((t.task_id, f"reaper error: {type(exc).__name__}: {exc}"))
    if result.reaped:
        log.warning("reaper: reaped %d task(s): %s", len(result.reaped), result.reaped)
    return result


def stale_count(manager: TaskManager, cfg: ReaperConfig) -> dict:
    """Return a summary suitable for /health: how many tasks *would*
    be reaped if reap_once() ran now (read-only)."""
    now = time.time()
    counts = {"running": 0, "queued": 0, "waiting": 0, "would_reap": 0}
    for s in ("queued", "running", "waiting"):
        for t in manager.list(status=s, limit=2000):
            counts[s] += 1
            created: Optional[float] = None
            if t.created_at:
                from datetime import datetime
                created = datetime.fromisoformat(t.created_at.replace("Z", "+00:00")).timestamp()
            if created is None:
                continue
            if now - created < cfg.grace_period_sec:
                continue
            if now - created > cfg.max_total_age_sec:
                counts["would_reap"] += 1
                continue
            if t.status == "queued" and (now - created) > cfg.stale_queued_sec:
                counts["would_reap"] += 1
            else:
                last_p = _last_progress_ts(manager, t.task_id)
                if last_p is not None and (now - last_p) > cfg.stale_running_sec:
                    counts["would_reap"] += 1
    return counts
