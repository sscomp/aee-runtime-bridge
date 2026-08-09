"""AEE Bootstrap v1 — Stage 07_agent_ready executor (spec §4).

Writes the ``AGENT_READY`` marker file at ``repo_root /
AGENT_READY``. The marker is a small JSON document recording:

* ``version`` — the AEE version (read from ``aee.__version__`` if
  available, else ``"unknown"``).
* ``profile`` — the installed profile.
* ``timestamp`` — ISO-8601 UTC.
* ``run_id`` — the bootstrap run id.

This is the terminal stage; once it completes, the bootstrap is
done and the agent is ready to serve.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from aee.installer.lifecycle import StageName, StageState
from aee.installer.stages.base import (
    StageContext,
    StageOutcome,
    StageResult,
    _record_result,
)


class AgentReadyStage:
    """Stage 07_agent_ready — write AGENT_READY marker (spec §4)."""

    MARKER_FILENAME = "AGENT_READY"

    @property
    def name(self) -> StageName:
        return StageName.AGENT_READY

    def run(self, ctx: StageContext) -> StageResult:
        t0 = time.monotonic()
        lifecycle = ctx.lifecycle
        lifecycle.record_stage(
            StageName.AGENT_READY, StageState.IN_PROGRESS
        )

        marker_path = ctx.repo_root / self.MARKER_FILENAME

        # Dry-run: plan only.
        if ctx.dry_run:
            result = StageResult(
                stage=StageName.AGENT_READY,
                outcome=StageOutcome.SKIPPED,
                message="dry-run: would write {p}".format(p=str(marker_path)),
                evidence={
                    "marker_path": str(marker_path),
                    "mode": "dry_run",
                },
                duration_seconds=time.monotonic() - t0,
            )
            _record_result(lifecycle, StageName.AGENT_READY, result)
            return result

        # Read version (best-effort).
        version = "unknown"
        try:
            import aee
            version = getattr(aee, "__version__", "unknown")
        except Exception:
            pass

        marker_doc = {
            "version": version,
            "profile": ctx.profile,
            "timestamp": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "run_id": ctx.run_id,
        }

        try:
            marker_path.write_text(
                json.dumps(marker_doc, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            result = StageResult(
                stage=StageName.AGENT_READY,
                outcome=StageOutcome.FAILED,
                message="failed to write marker: {e}".format(e=exc),
                evidence={
                    "marker_path": str(marker_path),
                    "error_class": type(exc).__name__,
                },
                error_class=type(exc).__name__,
                duration_seconds=time.monotonic() - t0,
            )
            _record_result(lifecycle, StageName.AGENT_READY, result)
            return result

        result = StageResult(
            stage=StageName.AGENT_READY,
            outcome=StageOutcome.COMPLETED,
            message="AGENT_READY marker written at {p}".format(p=str(marker_path)),
            evidence={
                "marker_path": str(marker_path),
                "marker_content": marker_doc,
            },
            duration_seconds=time.monotonic() - t0,
        )
        _record_result(lifecycle, StageName.AGENT_READY, result)
        return result
