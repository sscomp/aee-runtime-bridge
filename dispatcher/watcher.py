"""Background watcher: poll the runtime adapter for in-flight runs
and update the dispatcher DB with progress + final state.

The watcher is launched as a FastAPI `asyncio.create_task` at app
startup, and cancelled on shutdown. It walks `tasks` rows with
`status = 'running'` and:

  1. Resolves the runtime adapter via
     `adapter_registry.get(task.adapter_name)`.
  2. Calls `adapter.poll(task.external_run_id)` (the adapter
     implements the actual HTTP call against the underlying
     runtime — Hermes today, Pi / Claude Code / MCP tomorrow).
  3. Translates the result into dispatcher progress + state.
  4. On completed/failed/cancelled/timeout, writes the final
     output and stops polling that task.

AEE-1 / AEE-2 changes (2026-07-10):
  * The watcher used to hardcode `HERMES_BASE_URL` and call
    `httpx.AsyncClient` directly. AEE-2 moves that to
    `HermesAdapter` and routes via the registry.
  * The reaper now distinguishes "upstream reported failure" →
    `failed` from "upstream disappeared / heartbeat aged out" →
    `timeout` (see `dispatcher/reaper.py` for the policy).

Why a single polling loop instead of one task per run?
- Adapters have no event subscription (Hermes 8642 has only GET).
- One loop is easier to bound (concurrency=1) and observe.
- A few hundred ms latency is fine for the 2-second tick.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger("dispatcher.watcher")

from dispatcher.manager import TaskManager, TaskNotFound
from dispatcher.progress import next_pct_hint
from dispatcher.reaper import ReaperConfig, reap_once, stale_count as reaper_stale_count
from config import load as config_load


# AEE-2: the watcher no longer reads HERMES_BASE_URL / HERMES_API_KEY
# itself. Adapter selection happens via the registry, and each
# adapter owns its own transport config.


def _translate_status(upstream_status: str) -> tuple[str, int, Optional[str]]:
    """Return (dispatcher_status, pct, step) given a runtime status.

    `upstream_status` is the *adapter-translated* status string —
    see `RuntimeAdapter` for the vocabulary. The watcher only has
    to do the secondary translation from the small set
    {queued, running, completed, failed, cancelled, timeout} to
    the dispatcher's slightly larger set.
    """
    status = (upstream_status or "").lower()
    if status in {"completed", "succeeded", "success"}:
        return "completed", 100, "Completed"
    if status in {"failed", "error"}:
        return "failed", 100, status.title()
    if status in {"cancelled", "canceled"}:
        return "cancelled", 100, status.title()
    if status in {"timeout"}:
        # An adapter reporting timeout is treated like a final
        # failure; the reaper is the normal source of `timeout`.
        return "timeout", 100, "Timeout"
    if status in {"running", "in_progress", "started"}:
        return "running", 0, None  # let heuristic decide
    return "running", 0, None


class Watcher:
    """Polls the runtime adapter for in-flight tasks and updates the
    dispatcher DB.

    Lifecycle:
        w = Watcher(tick_sec=2.0)
        await w.start()
        ... app runs ...
        await w.stop()
    """

    def __init__(self, tick_sec: float = 2.0) -> None:
        self.tick_sec = tick_sec
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._manager = TaskManager()
        # Runtime run_id -> start time (for heuristic progress).
        # Keyed by external_run_id (AEE-2); falls back to
        # hermes_run_id for legacy compatibility.
        self._run_started: Dict[str, float] = {}
        # Reaper configuration (re-read each tick so config edits apply live).
        self._reaper_cfg = ReaperConfig.from_dict(config_load("reaper"))
        # Reaper runs every Nth tick (the reaper is heavier than the
        # poll, and we don't need to re-evaluate thresholds every 2s).
        self._reaper_every_n_ticks = max(1, int(os.getenv("REAPER_EVERY_N_TICKS", "5")))
        self._tick_count = 0
        # Latest reaper snapshot for /health.
        self._last_reaper_counts: Dict[str, int] = {
            "queued": 0, "running": 0, "waiting": 0, "would_reap": 0,
        }
        # Notification throttle (events already pushed to /logs/reaper.log).
        self._last_reap: Dict[str, float] = {}

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        # AEE-2: bootstrap the default adapter registry (HermesAdapter)
        # so the watcher can resolve `task.adapter_name` lookups.
        try:
            from aee.core.registry import bootstrap_defaults
            bootstrap_defaults(force=False)
        except Exception:  # noqa: BLE001
            log.exception("watcher.startup: bootstrap_defaults failed (non-fatal)")
        # Re-arm: any task that was in-flight last time the bridge ran
        # is either still running upstream (we'll re-attach on the
        # next tick) or stuck (Phase 2.4 reaper will mark it timeout).
        # Either way we must NOT orphan it.
        try:
            rearmed = 0
            for t in self._manager.list(limit=2000):
                if t.status in ("queued", "running", "waiting"):
                    rearmed += 1
            if rearmed:
                log.info("watcher.startup: %d in-flight task(s) re-armed for re-polling", rearmed)
        except Exception:  # noqa: BLE001
            log.exception("watcher.startup: re-arm scan failed (non-fatal)")
        self._task = asyncio.create_task(self._loop(), name="dispatcher-watcher")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                # Don't let one bad tick kill the watcher.
                print(f"[watcher] tick error: {type(exc).__name__}: {exc}", flush=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick_sec)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        # 1) Poll running tasks via the runtime adapter. The
        # adapter is resolved per-task via
        # `adapter_registry.get(task.adapter_name)` so Pi Agent /
        # Claude Code Agent / MCP adapters all flow through the
        # same code path.
        running = self._manager.list(status="running", limit=200)
        for t in running:
            external_id = t.external_run_id or t.hermes_run_id
            if not external_id:
                continue
            try:
                await self._poll_one(t, external_id)
            except Exception as exc:  # noqa: BLE001
                # Log to the task log, don't crash the loop.
                try:
                    self._manager.warning(t.task_id, f"watcher poll error: {type(exc).__name__}: {exc}")
                except Exception:
                    pass
        # 2) Reaper (every Nth tick) — pure-function, no HTTP.
        self._tick_count += 1
        if self._tick_count % self._reaper_every_n_ticks == 0:
            try:
                # Re-read config each time so live edits to
                # config/reaper.json apply without a restart.
                self._reaper_cfg = ReaperConfig.from_dict(config_load("reaper"))
                res = reap_once(self._manager, self._reaper_cfg)
                self._last_reaper_counts = reaper_stale_count(self._manager, self._reaper_cfg)
                if res.reaped:
                    for tid in res.reaped:
                        self._last_reap[tid] = time.time()
                    # Append a reaper log line for observability.
                    self._append_reaper_log(res)
                    # Notify (Phase 2 P2) — best-effort, never raise.
                    try:
                        from dispatcher.notifier import notify_timeout
                        for tid in res.reaped:
                            notify_timeout(tid)
                    except Exception:  # noqa: BLE001
                        pass
            except Exception as exc:  # noqa: BLE001
                log.exception("reaper tick failed: %s", exc)

    def _append_reaper_log(self, res) -> None:
        from datetime import datetime
        from dispatcher.manager import _BRIDGE_ROOT
        log_dir = _BRIDGE_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "reaper.log"
        line = json.dumps({
            "ts": datetime.utcnow().isoformat() + "Z",
            "event": "reap",
            "scanned": res.scanned,
            "reaped": res.reaped,
            "skipped_count": len(res.skipped),
        }, ensure_ascii=False)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    async def _poll_one(self, t, external_id: str) -> None:
        # Resolve the adapter for this task. Legacy tasks with no
        # adapter_name default to "hermes".
        from aee.core.registry import adapter_registry
        from aee.adapters.base import (
            AdapterNotFoundError,
            RuntimeError as AdapterRuntimeError,
            UnknownExternalRunError,
        )
        adapter_name = t.adapter_name or "hermes"
        try:
            adapter = adapter_registry.get(adapter_name)
        except AdapterNotFoundError as exc:
            self._manager.warning(
                t.task_id,
                f"watcher: no adapter for adapter_name={adapter_name!r}: {exc}",
            )
            return
        try:
            poll_result = await adapter.poll(external_id)
        except UnknownExternalRunError:
            # AEE-2: 404 / unknown run from the adapter is a strong
            # signal the upstream no longer tracks it. Treat as
            # `timeout` (the reaper's main "stale" semantic) so
            # operators can distinguish "worker crash" from
            # "upstream error" downstream. AEE-1 used `fail` here;
            # AEE-2 narrows that to timeout to match the §7 split.
            self._manager.timeout(
                t.task_id,
                f"upstream {adapter_name} no longer tracks external_run_id={external_id!r}",
            )
            return
        except AdapterRuntimeError as exc:
            self._manager.warning(
                t.task_id, f"upstream HTTP error: {type(exc).__name__}: {exc}"
            )
            return
        new_status, fixed_pct, step = _translate_status(poll_result.status)
        # Heuristic progress: time-based if we don't have a fixed pct.
        if fixed_pct == 0 and new_status == "running":
            started = self._run_started.get(external_id)
            if started is None:
                started = time.time()
                self._run_started[external_id] = started
            elapsed = time.time() - started
            output_text = poll_result.output
            has_output = bool(output_text and len(str(output_text)) > 0)
            suggested = next_pct_hint(
                current_pct=t.progress_pct,
                elapsed_sec=elapsed,
                timeout_sec=900,
                has_output=has_output,
            )
            # Only advance, never regress.
            if suggested > t.progress_pct:
                try:
                    self._manager.progress(t.task_id, suggested, "Running on adapter")
                except ValueError:
                    pass
        # Terminal?
        if new_status in {"completed", "failed", "cancelled", "timeout"}:
            output_text = poll_result.output
            usage = poll_result.usage
            raw = dict(poll_result.raw) if poll_result.raw else None
            if new_status == "completed":
                self._manager.complete(
                    t.task_id, output_text=output_text, usage=usage, raw=raw,
                )
            elif new_status == "failed":
                err = (
                    (poll_result.error if poll_result.error else None)
                    or (raw.get("error") if isinstance(raw, dict) else None)
                    or (raw.get("last_event") if isinstance(raw, dict) else None)
                    or "upstream failed"
                )
                self._manager.fail(t.task_id, str(err)[:500])
            elif new_status == "cancelled":
                self._manager.cancel(t.task_id)
            else:
                # timeout reported by adapter (rare; reaper is the
                # normal source of timeout)
                self._manager.timeout(
                    t.task_id, f"adapter {adapter_name} reported timeout"
                )
            # Drop start-time cache.
            self._run_started.pop(external_id, None)
        elif fixed_pct and step:
            try:
                self._manager.progress(t.task_id, fixed_pct, step)
            except ValueError:
                pass
