"""AEE Bootstrap v1 — Stage 06_smoke_test executor (spec §4).

Runs a bounded smoke test that verifies the runtime is importable and
the CLI responds. The smoke test is deliberately minimal:

1. Import ``aee.cli`` from the venv's Python.
2. Run ``aee doctor --profile <profile>`` (offline) and check exit 0
   or CAVEAT.

The smoke test has a hard wall-clock cap (``ctx.timeout_seconds``,
default 300s). Failures are recorded with the stdout/stderr tail.
"""
from __future__ import annotations

import time
from typing import List

from aee.installer.lifecycle import StageName, StageState
from aee.installer.stages.base import (
    StageContext,
    StageOutcome,
    StageResult,
    _record_result,
    _run_subprocess,
)


class SmokeTestStage:
    """Stage 06_smoke_test — bounded import + CLI smoke (spec §4)."""

    @property
    def name(self) -> StageName:
        return StageName.SMOKE_TEST

    def run(self, ctx: StageContext) -> StageResult:
        t0 = time.monotonic()
        lifecycle = ctx.lifecycle
        lifecycle.record_stage(
            StageName.SMOKE_TEST, StageState.IN_PROGRESS
        )

        # Dry-run: plan only.
        if ctx.dry_run:
            result = StageResult(
                stage=StageName.SMOKE_TEST,
                outcome=StageOutcome.SKIPPED,
                message="dry-run: would run smoke test",
                evidence={"mode": "dry_run"},
                duration_seconds=time.monotonic() - t0,
            )
            _record_result(lifecycle, StageName.SMOKE_TEST, result)
            return result

        venv_python = str(ctx.install_path / "bin" / "python")
        repo_root = str(ctx.repo_root)

        # Step 1: import aee.cli from the venv python.
        import_cmd: List[str] = [
            venv_python, "-c", "import aee.cli; print('aee.cli import OK')",
        ]
        ec, out, err = _run_subprocess(
            import_cmd,
            cwd=ctx.repo_root,
            env=ctx.environ,
            timeout=ctx.timeout_seconds,
        )
        if ec != 0:
            result = StageResult(
                stage=StageName.SMOKE_TEST,
                outcome=StageOutcome.FAILED,
                message="aee.cli import failed (exit {e})".format(e=ec),
                evidence={
                    "command": " ".join(import_cmd[:3]) + " ...",
                    "exit_code": ec,
                    "stdout_tail": out[-512:],
                    "stderr_tail": err,
                },
                error_class="SmokeImportError",
                duration_seconds=time.monotonic() - t0,
            )
            _record_result(lifecycle, StageName.SMOKE_TEST, result)
            return result

        # Step 2: run aee doctor (offline) via the venv python.
        doctor_cmd: List[str] = [
            venv_python, "-m", "aee.cli", "doctor",
            "--profile", ctx.profile,
        ]
        ec2, out2, err2 = _run_subprocess(
            doctor_cmd,
            cwd=ctx.repo_root,
            env=ctx.environ,
            timeout=ctx.timeout_seconds,
        )
        # doctor exit 0 = PASS, non-zero = FAIL/CAVEAT.
        if ec2 == 0:
            result = StageResult(
                stage=StageName.SMOKE_TEST,
                outcome=StageOutcome.COMPLETED,
                message="smoke test passed (import + doctor)",
                evidence={
                    "import_exit": ec,
                    "doctor_exit": ec2,
                    "doctor_stdout_tail": out2[-512:],
                },
                duration_seconds=time.monotonic() - t0,
            )
        else:
            result = StageResult(
                stage=StageName.SMOKE_TEST,
                outcome=StageOutcome.FAILED,
                message="doctor smoke failed (exit {e})".format(e=ec2),
                evidence={
                    "import_exit": ec,
                    "doctor_exit": ec2,
                    "doctor_stdout_tail": out2[-512:],
                    "stderr_tail": err2,
                },
                error_class="SmokeDoctorError",
                duration_seconds=time.monotonic() - t0,
            )
        _record_result(lifecycle, StageName.SMOKE_TEST, result)
        return result
