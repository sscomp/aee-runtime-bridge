"""AEE Bootstrap v1 — Stage 05_health_check executor (spec §4 + §11).

Runs ``aee doctor`` (the existing H1-H10 health-check surface) and
records the verdict. When the doctor reports FAIL, the stage fails;
CAVEAT is treated as a pass-with-warnings (SKIPPED with reason).

This stage does NOT provision credentials. It reads env vars the
operator already provisioned; a missing required secret surfaces as
a doctor CAVEAT/FAIL and is recorded honestly.
"""
from __future__ import annotations

import time
from typing import Optional

from aee.installer.lifecycle import StageName, StageState
from aee.installer.stages.base import (
    StageContext,
    StageOutcome,
    StageResult,
    _record_result,
)


class HealthCheckStage:
    """Stage 05_health_check — run aee doctor (spec §4, §11)."""

    @property
    def name(self) -> StageName:
        return StageName.HEALTH_CHECK

    def run(self, ctx: StageContext) -> StageResult:
        t0 = time.monotonic()
        lifecycle = ctx.lifecycle
        lifecycle.record_stage(
            StageName.HEALTH_CHECK, StageState.IN_PROGRESS
        )

        # Dry-run: plan only.
        if ctx.dry_run:
            result = StageResult(
                stage=StageName.HEALTH_CHECK,
                outcome=StageOutcome.SKIPPED,
                message="dry-run: would run aee doctor",
                evidence={"mode": "dry_run"},
                duration_seconds=time.monotonic() - t0,
            )
            _record_result(lifecycle, StageName.HEALTH_CHECK, result)
            return result

        # Import here so dry-run does not pay the import cost.
        from aee.doctor import run_doctor, DoctorReport

        try:
            report: DoctorReport = run_doctor(
                repo_root=ctx.repo_root,
                environ=ctx.environ,
                profile=ctx.profile,
                network=False,  # bootstrap health check is offline
            )
        except Exception as exc:  # pragma: no cover — defensive
            result = StageResult(
                stage=StageName.HEALTH_CHECK,
                outcome=StageOutcome.FAILED,
                message="doctor raised: {e}".format(e=exc),
                evidence={"error_class": type(exc).__name__},
                error_class=type(exc).__name__,
                duration_seconds=time.monotonic() - t0,
            )
            _record_result(lifecycle, StageName.HEALTH_CHECK, result)
            return result

        verdict = report.verdict  # "PASS" / "CAVEAT" / "FAIL"
        summary = dict(report.summary)

        if verdict == "PASS":
            outcome = StageOutcome.COMPLETED
            message = "doctor PASS ({p} checks, {c} caveats, {f} fails)".format(
                p=summary.get("PASS", 0),
                c=summary.get("CAVEAT", 0),
                f=summary.get("FAIL", 0),
            )
        elif verdict == "CAVEAT":
            # CAVEAT is a pass-with-warnings; record as COMPLETED.
            outcome = StageOutcome.COMPLETED
            message = "doctor CAVEAT ({p} pass, {c} caveats, {f} fails)".format(
                p=summary.get("PASS", 0),
                c=summary.get("CAVEAT", 0),
                f=summary.get("FAIL", 0),
            )
        else:  # FAIL
            outcome = StageOutcome.FAILED
            message = "doctor FAIL ({f} failures)".format(
                f=summary.get("FAIL", 0)
            )

        result = StageResult(
            stage=StageName.HEALTH_CHECK,
            outcome=outcome,
            message=message,
            evidence={
                "verdict": verdict,
                "summary": summary,
                "checks": [
                    {"name": c.name, "status": c.status, "detail": c.detail}
                    for c in report.checks
                ],
            },
            error_class="DoctorFailedError" if outcome is StageOutcome.FAILED else None,
            duration_seconds=time.monotonic() - t0,
        )
        _record_result(lifecycle, StageName.HEALTH_CHECK, result)
        return result
