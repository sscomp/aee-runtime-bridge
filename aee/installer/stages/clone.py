"""AEE Bootstrap v1 — Stage 02_clone executor (spec §4 + §9).

Clones (or fetches) the repository to the install path. When the repo
is already present at ``repo_root`` (the common in-place bootstrap
case), the stage is a SKIPPED no-op — the operator already has the
source. When ``git_url`` is supplied via ``ctx.extra``, the stage
clones into ``install_path`` (or fetches if it already exists).

Idempotent: re-running on an existing clone is a ``git fetch --prune``,
not a fresh clone.
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


class CloneStage:
    """Stage 02_clone — git clone or fetch (spec §4, §9)."""

    @property
    def name(self) -> StageName:
        return StageName.CLONE

    def run(self, ctx: StageContext) -> StageResult:
        t0 = time.monotonic()
        lifecycle = ctx.lifecycle
        lifecycle.record_stage(StageName.CLONE, StageState.IN_PROGRESS)

        git_url = ctx.extra_get("git_url")
        repo_root = ctx.repo_root

        # In-place bootstrap: repo already present.
        if (repo_root / ".git").exists() and not git_url:
            result = StageResult(
                stage=StageName.CLONE,
                outcome=StageOutcome.SKIPPED,
                message="repo already present at {p}; clone skipped".format(
                    p=str(repo_root)
                ),
                evidence={"repo_root": str(repo_root), "mode": "in_place"},
                duration_seconds=time.monotonic() - t0,
            )
            _record_result(lifecycle, StageName.CLONE, result)
            return result

        # No git_url and no repo → cannot clone.
        if not git_url:
            result = StageResult(
                stage=StageName.CLONE,
                outcome=StageOutcome.FAILED,
                message=(
                    "no git_url supplied and repo not present at {p}"
                ).format(p=str(repo_root)),
                evidence={"repo_root": str(repo_root), "mode": "missing"},
                error_class="CloneTargetMissingError",
                duration_seconds=time.monotonic() - t0,
            )
            _record_result(lifecycle, StageName.CLONE, result)
            return result

        # Dry-run: plan only.
        if ctx.dry_run:
            result = StageResult(
                stage=StageName.CLONE,
                outcome=StageOutcome.SKIPPED,
                message="dry-run: would clone {u} → {p}".format(
                    u=git_url, p=str(repo_root)
                ),
                evidence={
                    "git_url": git_url,
                    "repo_root": str(repo_root),
                    "mode": "dry_run",
                },
                duration_seconds=time.monotonic() - t0,
            )
            _record_result(lifecycle, StageName.CLONE, result)
            return result

        # Real clone or fetch.
        cmd: List[str]
        if (repo_root / ".git").exists():
            cmd = ["git", "fetch", "--prune", "--quiet"]
        else:
            cmd = ["git", "clone", "--quiet", git_url, str(repo_root)]

        exit_code, stdout, stderr = _run_subprocess(
            cmd,
            cwd=repo_root if (repo_root / ".git").exists() else None,
            env=ctx.environ,
            timeout=ctx.timeout_seconds,
        )

        if exit_code == 0:
            result = StageResult(
                stage=StageName.CLONE,
                outcome=StageOutcome.COMPLETED,
                message="git operation succeeded ({c})".format(c=" ".join(cmd[:2])),
                evidence={
                    "command": " ".join(cmd),
                    "exit_code": exit_code,
                    "stdout_tail": stdout[-512:],
                    "mode": "fetch" if (repo_root / ".git").exists() else "clone",
                },
                duration_seconds=time.monotonic() - t0,
            )
        else:
            result = StageResult(
                stage=StageName.CLONE,
                outcome=StageOutcome.FAILED,
                message="git operation failed (exit {e})".format(e=exit_code),
                evidence={
                    "command": " ".join(cmd),
                    "exit_code": exit_code,
                    "stdout_tail": stdout[-512:],
                    "stderr_tail": stderr,
                },
                error_class="CloneFailedError",
                duration_seconds=time.monotonic() - t0,
            )
        _record_result(lifecycle, StageName.CLONE, result)
        return result
