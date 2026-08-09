"""AEE Bootstrap v1 — BootstrapRunner orchestrator (spec §4 + §5).

Drives stages 02-07 in order, threading a :class:`StageContext`
through each executor. Honors resume (§5.5): on start, reads the
marker store and skips already-completed stages. Records every
transition in the :class:`BootstrapLifecycle`.

Design contract:

* **No credential provisioning.** The runner never reads, writes, or
  generates API keys. It threads ``os.environ`` to stages; stages
  that need secrets read them from the environment the operator
  already provisioned.
* **Dry-run by default.** ``dry_run=True`` produces a plan-only run
  (every stage returns SKIPPED with a ``mode=dry_run`` evidence
  block). ``dry_run=False`` is the real execute path.
* **Bounded.** Each stage gets ``timeout_seconds``; the runner
  itself has no wall-clock cap (the sum of stage timeouts is the
  cap).
* **Honest.** A stage failure stops the run; the runner does NOT
  retry (retry is the shell layer's job per §5.4). The result
  carries the failing stage's evidence.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from aee.installer.lifecycle import (
    BootstrapLifecycle,
    InMemoryMarkerStore,
    MarkerStore,
    StageName,
    StageState,
)
from aee.installer.stages import STAGE_EXECUTORS, StageExecutor
from aee.installer.stages.base import (
    StageContext,
    StageOutcome,
    StageResult,
)


# ---------------------------------------------------------------------------#
# Runner result
# ---------------------------------------------------------------------------#


@dataclass(frozen=True)
class BootstrapRunResult:
    """The result of a full bootstrap run.

    Fields:

    * ``run_id`` — the bootstrap run id.
    * ``dry_run`` — whether this was a plan-only run.
    * ``stages`` — tuple of :class:`StageResult` in execution order.
    * ``ok`` — True iff every stage completed or was skipped.
    * ``failing_stage`` — the :class:`StageName` that failed (or None).
    * ``duration_seconds`` — total wall-clock time.
    * ``agent_ready`` — True iff the AGENT_READY marker was written.
    """

    run_id: str
    dry_run: bool
    stages: Tuple[StageResult, ...]
    ok: bool
    failing_stage: Optional[StageName]
    duration_seconds: float
    agent_ready: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "dry_run": self.dry_run,
            "ok": self.ok,
            "failing_stage": (
                self.failing_stage.value if self.failing_stage else None
            ),
            "duration_seconds": self.duration_seconds,
            "agent_ready": self.agent_ready,
            "stages": [s.to_dict() for s in self.stages],
        }


# ---------------------------------------------------------------------------#
# BootstrapRunner
# ---------------------------------------------------------------------------#


class BootstrapRunner:
    """Drives stages 02-07 in order (spec §4).

    Construction is cheap; no I/O happens until :meth:`run` is called.

    Args:

    * ``repo_root`` — absolute path to the repo.
    * ``profile`` — canonical profile name.
    * ``store`` — optional :class:`MarkerStore` (defaults to
      :class:`InMemoryMarkerStore`).
    * ``executors`` — optional tuple of :class:`StageExecutor`
      (defaults to :data:`STAGE_EXECUTORS`).
    * ``environ`` — optional environment mapping (defaults to
      ``os.environ``).
    * ``install_path`` — optional venv path (defaults to
      ``repo_root / ".venv"``).
    * ``dry_run`` — default False (real execute).
    * ``timeout_seconds`` — per-stage cap (default 300).
    * ``extra`` — optional stage-specific kwargs.
    """

    def __init__(
        self,
        repo_root: Path,
        profile: str,
        *,
        store: Optional[MarkerStore] = None,
        executors: Optional[Tuple[StageExecutor, ...]] = None,
        environ: Optional[Mapping[str, str]] = None,
        install_path: Optional[Path] = None,
        dry_run: bool = False,
        timeout_seconds: int = 300,
        extra: Optional[Tuple[Tuple[str, Any], ...]] = None,
        run_id: Optional[str] = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.profile = profile
        self.store = store or InMemoryMarkerStore()
        self.executors = executors or STAGE_EXECUTORS
        self.environ = environ if environ is not None else os.environ
        self.install_path = (
            Path(install_path) if install_path is not None
            else self.repo_root / ".venv"
        )
        self.dry_run = dry_run
        self.timeout_seconds = timeout_seconds
        self.extra = extra or ()
        self.run_id = run_id

    def run(self) -> BootstrapRunResult:
        """Drive all stages in order; return the aggregate result."""
        t0 = time.monotonic()
        lifecycle = BootstrapLifecycle(self.store, run_id=self.run_id)
        state = lifecycle.start()
        run_id = state.run_id

        ctx = StageContext(
            repo_root=self.repo_root,
            profile=self.profile,
            install_path=self.install_path,
            environ=self.environ,
            lifecycle=lifecycle,
            run_id=run_id,
            dry_run=self.dry_run,
            timeout_seconds=self.timeout_seconds,
            extra=self.extra,
        )

        results: List[StageResult] = []
        failing_stage: Optional[StageName] = None
        agent_ready = False

        for executor in self.executors:
            # Resume: skip already-completed stages (§5.5).
            existing = lifecycle.get_marker(executor.name)
            if existing is not None and existing.state in (
                StageState.COMPLETED, StageState.SKIPPED
            ):
                # Stage already done; record a synthetic SKIPPED result.
                results.append(StageResult(
                    stage=executor.name,
                    outcome=StageOutcome.SKIPPED,
                    message="stage already {s}; skipped (resume)".format(
                        s=existing.state.value
                    ),
                    evidence={"resume": True, "prior_state": existing.state.value},
                    duration_seconds=0.0,
                ))
                if executor.name is StageName.AGENT_READY:
                    agent_ready = True
                continue

            result = executor.run(ctx)
            results.append(result)

            if result.outcome is StageOutcome.FAILED:
                failing_stage = executor.name
                break

            if (
                executor.name is StageName.AGENT_READY
                and result.outcome is StageOutcome.COMPLETED
            ):
                agent_ready = True

        ok = failing_stage is None
        duration = time.monotonic() - t0

        return BootstrapRunResult(
            run_id=run_id,
            dry_run=self.dry_run,
            stages=tuple(results),
            ok=ok,
            failing_stage=failing_stage,
            duration_seconds=duration,
            agent_ready=agent_ready,
        )


__all__ = [
    "BootstrapRunResult",
    "BootstrapRunner",
]
