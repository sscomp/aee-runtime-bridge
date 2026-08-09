"""AEE Bootstrap v1 — Stage Executors (stages 02-07).

This package implements the execution path for bootstrap stages 02
through 07 (spec §4). Each stage is a small, idempotent executor that
takes a :class:`StageContext` and returns a :class:`StageResult`.

Design contract:

* **Protocol-based.** Each stage implements the :class:`StageExecutor`
  Protocol so the :class:`BootstrapRunner` can drive them uniformly and
  tests can substitute fakes.
* **No credential provisioning.** No stage reads, writes, or generates
  API keys, tokens, or secrets. Stages 04 (runtime_setup) and 05
  (health_check) read env vars *that the operator already provisioned*;
  a missing required secret surfaces as a structured ``SKIPPED`` or
  ``FAILED`` result with ``reason="secret missing"`` — never an
  exception that aborts the run.
* **Idempotent.** Re-running a completed stage is a no-op (or a
  safe refresh). The marker store tracks completion.
* **Bounded.** Network operations have timeouts; smoke tests have a
  wall-clock cap.

Stage ownership (spec §4):

* Stages 00 (detect) and 01 (deps) are owned by the shell layer
  (``bootstrap/lib/detect.sh``, ``bootstrap/lib/deps.sh``) and run
  before Python is available. They are NOT in this package.
* Stage 02 (clone) is a shell stage per spec but is implemented here
  in Python for portability and testability; the POSIX trampoline
  (``install.sh``) delegates to it via ``python3 -m aee.cli install
  --execute``.
* Stages 03 (pin) through 07 (agent_ready) are Python-backend stages.
  Stage 03 (pin) is already implemented in
  :mod:`aee.installer.update`; this package adds 04-07.
"""
from __future__ import annotations

from aee.installer.stages.base import (
    StageContext,
    StageResult,
    StageExecutor,
    StageOutcome,
)
from aee.installer.stages.clone import CloneStage
from aee.installer.stages.runtime_setup import RuntimeSetupStage
from aee.installer.stages.health_check import HealthCheckStage
from aee.installer.stages.smoke_test import SmokeTestStage
from aee.installer.stages.agent_ready import AgentReadyStage

#: The ordered list of stage executors the runner drives (spec §4).
STAGE_EXECUTORS = (
    CloneStage(),
    RuntimeSetupStage(),
    HealthCheckStage(),
    SmokeTestStage(),
    AgentReadyStage(),
)

__all__ = [
    "StageContext",
    "StageResult",
    "StageExecutor",
    "StageOutcome",
    "CloneStage",
    "RuntimeSetupStage",
    "HealthCheckStage",
    "SmokeTestStage",
    "AgentReadyStage",
    "STAGE_EXECUTORS",
]
