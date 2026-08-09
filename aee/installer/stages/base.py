"""AEE Bootstrap v1 — Stage Executor Framework (stages 02-07).

Defines the Protocol, dataclasses, and helpers every stage executor
consumes. Nothing in this module performs side effects.
"""
from __future__ import annotations

import enum
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)

from aee.installer.lifecycle import (
    BootstrapLifecycle,
    StageName,
    StageState,
)


# ---------------------------------------------------------------------------#
# Outcome enum
# ---------------------------------------------------------------------------#


class StageOutcome(enum.Enum):
    """The outcome of running a stage executor.

    * :data:`COMPLETED` — stage succeeded; marker state COMPLETED.
    * :data:`SKIPPED` — stage was a no-op (e.g. already done, or a
      required precondition was not met and the stage is optional).
      Marker state SKIPPED.
    * :data:`FAILED` — stage failed; marker state FAILED with
      ``error_class`` + ``stderr_tail``.
    """

    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


# ---------------------------------------------------------------------------#
# Context — the input shape every stage receives
# ---------------------------------------------------------------------------#


@dataclass(frozen=True)
class StageContext:
    """The immutable context handed to every stage executor.

    Fields:

    * ``repo_root`` — absolute path to the repo (where ``install.sh``
      lives). Stage 02 clones *into* a subdirectory of this if the
      repo is not already present; for an in-place bootstrap (the
      common case) the repo is already at ``repo_root`` and 02 is a
      no-op SKIPPED.
    * ``profile`` — the canonical profile name (``full``, ``mini``,
      ``edge``, ``developer``).
    * ``install_path`` — where the venv + runtime live. Defaults to
      ``repo_root / ".venv"``.
    * ``environ`` — the environment mapping (read-only). Stages MUST
      NOT mutate this; the runner threads ``os.environ`` by default
      and tests inject fakes.
    * ``lifecycle`` — the :class:`BootstrapLifecycle` recorder. Stages
      call ``lifecycle.record_stage(...)`` to persist transitions.
    * ``run_id`` — the bootstrap run id (shared across all stages).
    * ``dry_run`` — when True, stages plan but do not execute side
      effects. The runner passes ``dry_run=False`` for the real
      execute path; ``dry_run=True`` is the audit-only path.
    * ``lockfile_path`` — path to the pinned requirements lockfile
      (spec §4 stage 03). Defaults to
      ``repo_root / "bootstrap/manifests/python.requirements.lock"``.
    * ``timeout_seconds`` — per-stage wall-clock cap (default 300s).
    * ``extra`` — stage-specific free-form kwargs (e.g. ``git_url``
      for 02_clone, ``health_url`` for 05_health_check).
    """

    repo_root: Path
    profile: str
    install_path: Path
    environ: Mapping[str, str]
    lifecycle: BootstrapLifecycle
    run_id: str
    dry_run: bool = False
    lockfile_path: Optional[Path] = None
    timeout_seconds: int = 300
    extra: Tuple[Tuple[str, Any], ...] = ()

    def extra_get(self, key: str, default: Any = None) -> Any:
        """Read a value from the ``extra`` mapping."""
        for k, v in self.extra:
            if k == key:
                return v
        return default


# ---------------------------------------------------------------------------#
# Result — the output shape every stage returns
# ---------------------------------------------------------------------------#


@dataclass(frozen=True)
class StageResult:
    """The result of running a stage executor.

    Fields:

    * ``stage`` — the :class:`StageName` this result describes.
    * ``outcome`` — :class:`StageOutcome` (COMPLETED / SKIPPED / FAILED).
    * ``message`` — short human-readable summary.
    * ``evidence`` — dict of machine-readable evidence (paths, exit
      codes, stdout tails). Stored verbatim in the marker's
      ``stderr_tail`` field for FAILED outcomes (truncated to 4 KB).
    * ``error_class`` — for FAILED, the exception class name.
    * ``duration_seconds`` — wall-clock time the stage took.
    """

    stage: StageName
    outcome: StageOutcome
    message: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)
    error_class: Optional[str] = None
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        """True iff the stage completed or was legitimately skipped."""
        return self.outcome in (StageOutcome.COMPLETED, StageOutcome.SKIPPED)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage.value,
            "outcome": self.outcome.value,
            "message": self.message,
            "evidence": self.evidence,
            "error_class": self.error_class,
            "duration_seconds": self.duration_seconds,
        }


# ---------------------------------------------------------------------------#
# StageExecutor Protocol
# ---------------------------------------------------------------------------#


@runtime_checkable
class StageExecutor(Protocol):
    """The Protocol every stage executor satisfies.

    ``name`` returns the :class:`StageName` this executor owns.
    ``run(ctx)`` performs the stage's work and returns a
    :class:`StageResult`.
    """

    @property
    def name(self) -> StageName:
        ...

    def run(self, ctx: StageContext) -> StageResult:
        ...


# ---------------------------------------------------------------------------#
# Helpers shared by stage implementations
# ---------------------------------------------------------------------------#


def _truncate_tail(text: str, limit: int = 4096) -> str:
    """Truncate ``text`` to the last ``limit`` characters (spec §5.3)."""
    if len(text) <= limit:
        return text
    return text[-limit:]


def _record_result(
    lifecycle: BootstrapLifecycle,
    stage: StageName,
    result: StageResult,
) -> None:
    """Record a stage result into the lifecycle marker store.

    Maps :class:`StageOutcome` to :class:`StageState` and persists the
    transition via :meth:`BootstrapLifecycle.record_stage`.
    """
    if result.outcome is StageOutcome.COMPLETED:
        lifecycle.record_stage(stage, StageState.COMPLETED)
    elif result.outcome is StageOutcome.SKIPPED:
        lifecycle.record_stage(stage, StageState.SKIPPED)
    elif result.outcome is StageOutcome.FAILED:
        lifecycle.record_stage(
            stage,
            StageState.FAILED,
            error_class=result.error_class,
            stderr_tail=_truncate_tail(
                result.evidence.get("stderr_tail", "") or result.message
            ),
        )
    else:  # pragma: no cover — defensive
        lifecycle.record_stage(stage, StageState.FAILED, error_class="UnknownOutcome")


def _run_subprocess(
    cmd: List[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    timeout: int = 300,
    capture: bool = True,
) -> Tuple[int, str, str]:
    """Run ``cmd`` and return ``(exit_code, stdout, stderr)``.

    Thin wrapper over :func:`subprocess.run` with a hard timeout and
    stdout/stderr capture. Never raises on non-zero exit; the caller
    inspects the tuple.
    """
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            timeout=timeout,
            capture_output=capture,
            text=True,
        )
        return (
            proc.returncode,
            proc.stdout or "",
            proc.stderr or "",
        )
    except subprocess.TimeoutExpired as exc:
        return (
            124,
            (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            "timeout after {t}s: {cmd}".format(t=timeout, cmd=" ".join(cmd)),
        )
    except FileNotFoundError as exc:
        return (127, "", "binary not found: {e}".format(e=exc))


__all__ = [
    "StageOutcome",
    "StageContext",
    "StageResult",
    "StageExecutor",
    "_record_result",
    "_run_subprocess",
    "_truncate_tail",
]
