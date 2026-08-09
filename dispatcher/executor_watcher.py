"""Background watcher for executor_runs (P2.1 completion sync).

Polls the runtime adapter for non-terminal Hermes-dispatched runs
and updates the durable ``executor_runs`` row when the upstream
reports a terminal state. The existing GET-triggered reconciliation
(``_maybe_reconcile_hermes_run`` in app.py) is preserved unchanged;
this watcher adds the execution-lifecycle / background-poller path
mandated by work-order TASK-AEE-P2-BRIDGE-HERMES-COMPLETION-SYNC.

Design
------
* Single asyncio task launched at app startup, cancelled on
  shutdown (same pattern as ``dispatcher.watcher.Watcher``).
* Each tick scans ``executor_runs`` for non-terminal rows with
  ``selected_executor == 'hermes'`` (the only async executor we
  own; claude-code-cli is already terminal after POST).
* For each in-flight row the watcher calls
  ``_reconcile_hermes_run_once`` (the shared core extracted from
  the GET path) which performs exactly one bounded
  ``adapter.poll(external_run_id)``. If upstream reports terminal,
  the durable row is updated with the final state via
  ``_persist_terminal_reconciliation``.
* Idempotent: a row already terminal is never polled. The
  ``_reconcile_hermes_run_once`` core guards against the terminal
  check up-front so duplicate ticks are safe.
* No retry/cancel/requeue features (work-order §5).
* Bounded: ``limit=200`` rows scanned per tick, exactly one upstream
  poll per non-terminal row, ``tick_sec`` between scans.
* Does NOT touch ``dispatcher.tasks`` (the dispatcher task table).
  The existing ``dispatcher.watcher.Watcher`` continues to own that
  namespace.

Safety
------
* No executor launch (no POST /v1/runs) — read-only GET on Hermes.
* No mutation of unrelated rows.
* No mutation of already-terminal rows.
* Transient upstream errors leave the row in-flight.
* UnknownExternalRunError persists ``timeout`` (same as the GET
  path and the dispatcher watcher).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any, Dict, Optional

log = logging.getLogger("dispatcher.executor_watcher")

# Default tick cadence (seconds). Overridable via env var so the
# operator can tune without a restart. 5s is the same default as
# the dispatcher watcher's tick but the executor watcher is
# independent and does not share the env var name.
DEFAULT_EXECUTOR_WATCHER_TICK_SEC = float(
    os.getenv("EXECUTOR_WATCHER_TICK_SEC", "5.0")
)

# Maximum non-terminal rows scanned per tick. Bounded to keep the
# per-tick upstream HTTP cost finite even when the DB has thousands
# of rows (legacy in-flight rows that will be cleaned up by the
# reaper-equivalent logic in the GET path or by operator action).
MAX_ROWS_PER_TICK = int(os.getenv("EXECUTOR_WATCHER_MAX_ROWS", "200"))


class ExecutorRunWatcher:
    """Poll the runtime adapter for non-terminal executor_runs rows.

    Lifecycle
    ---------
        w = ExecutorRunWatcher(tick_sec=5.0)
        await w.start()
        ... app runs ...
        await w.stop()
    """

    def __init__(self, tick_sec: Optional[float] = None) -> None:
        self.tick_sec = tick_sec if tick_sec is not None else DEFAULT_EXECUTOR_WATCHER_TICK_SEC
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        # Periodic stale-run reconciliation: call reconcile_stale_runs
        # every N ticks to clean up orphaned executor_runs (task_id=NULL,
        # status=running, age > 1h) created after bridge startup. Same
        # idempotent function called by init_executor_runs on startup;
        # calling it periodically ensures post-start orphans are cleaned
        # up without requiring a bridge restart.
        # Default: 360 ticks (~30 min at 5s/tick). Overridable via env var.
        self._tick_count = 0
        self._reconcile_every_n_ticks = max(
            1, int(os.getenv("EXECUTOR_RECONCILE_EVERY_N_TICKS", "360"))
        )

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            self._loop(), name="executor-run-watcher"
        )

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
                print(
                    f"[executor_watcher] tick error: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.tick_sec
                )
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        # Import the reconciliation core from app.py. The core is
        # shared with the GET path so behaviour is byte-for-byte
        # identical (idempotent guard, status translation, terminal
        # persistence). The import is deferred to avoid circular
        # imports at module load time.
        from app import _reconcile_hermes_run_once
        from dispatcher.db import get_conn
        from dispatcher.executor_runs import list_non_terminal_runs, reconcile_stale_runs

        try:
            conn = get_conn()
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("executor_watcher: DB unavailable: %s", exc)
            return

        # Periodic stale-run reconciliation (every N ticks). Calls the
        # same idempotent reconcile_stale_runs used by init_executor_runs
        # on bridge startup. Bounded: single SELECT + bounded UPDATE,
        # non-destructive (no DELETE), audit-preserving. Fresh orphans
        # (< 1h old) and rows with task_id are never matched.
        self._tick_count += 1
        if self._tick_count % self._reconcile_every_n_ticks == 0:
            try:
                reconcile_stale_runs(conn)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "executor_watcher: periodic reconcile_stale_runs failed: %s",
                    exc,
                )

        try:
            rows = list_non_terminal_runs(
                conn,
                selected_executor="hermes",
                limit=MAX_ROWS_PER_TICK,
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "executor_watcher: list_non_terminal_runs failed: %s", exc
            )
            return

        if not rows:
            return  # nothing to do this tick

        for row in rows:
            run_id = row.get("run_id")
            if not run_id:
                continue
            try:
                await _reconcile_hermes_run_once(run_id, row)
            except Exception as exc:  # noqa: BLE001
                # Per-row failure must not crash the tick.
                log.warning(
                    "executor_watcher: reconcile failed for %s: %s",
                    run_id, exc,
                )