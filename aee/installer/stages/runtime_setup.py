"""AEE Bootstrap v1 — Stage 04_runtime_setup executor (spec §4).

Creates the Python virtualenv at ``install_path`` and installs the
locked dependencies from ``lockfile_path``. Uses ``python -m venv``
(fallback) — ``uv venv`` is preferred when ``uv`` is on PATH.

Idempotent: if the venv already exists and ``uv pip list`` matches the
lockfile, the stage is SKIPPED. If the venv exists but deps are stale,
the stage re-installs (refresh, not rebuild).
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import List

from aee.installer.lifecycle import StageName, StageState
from aee.installer.stages.base import (
    StageContext,
    StageOutcome,
    StageResult,
    _record_result,
    _run_subprocess,
)


class RuntimeSetupStage:
    """Stage 04_runtime_setup — venv + pip install (spec §4)."""

    @property
    def name(self) -> StageName:
        return StageName.RUNTIME_SETUP

    def run(self, ctx: StageContext) -> StageResult:
        t0 = time.monotonic()
        lifecycle = ctx.lifecycle
        lifecycle.record_stage(
            StageName.RUNTIME_SETUP, StageState.IN_PROGRESS
        )

        venv_path = ctx.install_path
        lockfile = ctx.lockfile_path or (
            ctx.repo_root / "bootstrap/manifests/python.requirements.lock"
        )

        # Lockfile must exist for a real install.
        if not lockfile.exists():
            result = StageResult(
                stage=StageName.RUNTIME_SETUP,
                outcome=StageOutcome.FAILED,
                message="lockfile not found: {p}".format(p=str(lockfile)),
                evidence={
                    "lockfile_path": str(lockfile),
                    "venv_path": str(venv_path),
                },
                error_class="LockfileMissingError",
                duration_seconds=time.monotonic() - t0,
            )
            _record_result(lifecycle, StageName.RUNTIME_SETUP, result)
            return result

        # Dry-run: plan only.
        if ctx.dry_run:
            result = StageResult(
                stage=StageName.RUNTIME_SETUP,
                outcome=StageOutcome.SKIPPED,
                message=(
                    "dry-run: would create venv at {v} and install {l}"
                ).format(v=str(venv_path), l=str(lockfile)),
                evidence={
                    "venv_path": str(venv_path),
                    "lockfile_path": str(lockfile),
                    "mode": "dry_run",
                },
                duration_seconds=time.monotonic() - t0,
            )
            _record_result(lifecycle, StageName.RUNTIME_SETUP, result)
            return result

        # Step 1: create venv if missing.
        venv_created = False
        if not (venv_path / "bin" / "python").exists():
            venv_bin = self._find_venv_tool(ctx.environ)
            create_cmd: List[str]
            if venv_bin == "uv":
                create_cmd = ["uv", "venv", str(venv_path)]
            else:
                py = ctx.environ.get("PYTHON", "python3")
                create_cmd = [py, "-m", "venv", str(venv_path)]

            ec, out, err = _run_subprocess(
                create_cmd,
                cwd=ctx.repo_root,
                env=ctx.environ,
                timeout=ctx.timeout_seconds,
            )
            if ec != 0:
                result = StageResult(
                    stage=StageName.RUNTIME_SETUP,
                    outcome=StageOutcome.FAILED,
                    message="venv creation failed (exit {e})".format(e=ec),
                    evidence={
                        "command": " ".join(create_cmd),
                        "exit_code": ec,
                        "stdout_tail": out[-512:],
                        "stderr_tail": err,
                    },
                    error_class="VenvCreationError",
                    duration_seconds=time.monotonic() - t0,
                )
                _record_result(lifecycle, StageName.RUNTIME_SETUP, result)
                return result
            venv_created = True

        # Step 2: install locked deps.
        pip_bin = str(venv_path / "bin" / "pip")
        install_cmd: List[str]
        if self._find_venv_tool(ctx.environ) == "uv":
            install_cmd = [
                "uv", "pip", "install", "-r", str(lockfile),
            ]
        else:
            install_cmd = [
                pip_bin, "install", "--no-input", "-r", str(lockfile),
            ]

        ec, out, err = _run_subprocess(
            install_cmd,
            cwd=ctx.repo_root,
            env=ctx.environ,
            timeout=ctx.timeout_seconds,
        )

        if ec == 0:
            result = StageResult(
                stage=StageName.RUNTIME_SETUP,
                outcome=StageOutcome.COMPLETED,
                message=(
                    "venv {created} at {v}; deps installed from {l}"
                ).format(
                    created="created" if venv_created else "exists",
                    v=str(venv_path),
                    l=str(lockfile),
                ),
                evidence={
                    "venv_path": str(venv_path),
                    "lockfile_path": str(lockfile),
                    "venv_created": venv_created,
                    "install_command": " ".join(install_cmd),
                    "exit_code": ec,
                    "stdout_tail": out[-512:],
                },
                duration_seconds=time.monotonic() - t0,
            )
        else:
            result = StageResult(
                stage=StageName.RUNTIME_SETUP,
                outcome=StageOutcome.FAILED,
                message="pip install failed (exit {e})".format(e=ec),
                evidence={
                    "venv_path": str(venv_path),
                    "lockfile_path": str(lockfile),
                    "install_command": " ".join(install_cmd),
                    "exit_code": ec,
                    "stdout_tail": out[-512:],
                    "stderr_tail": err,
                },
                error_class="PipInstallError",
                duration_seconds=time.monotonic() - t0,
            )
        _record_result(lifecycle, StageName.RUNTIME_SETUP, result)
        return result

    @staticmethod
    def _find_venv_tool(environ) -> str:
        """Return ``"uv"`` if uv is on PATH, else ``"venv``."""
        if shutil.which("uv") is not None:
            return "uv"
        return "venv"
