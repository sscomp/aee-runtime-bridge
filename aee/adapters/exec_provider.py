"""AEE Provider 抽象 — 為 subprocess-based worker 設計的介面骨架。

Why a separate abstraction?
---------------------------
AEE-1 的 `RuntimeAdapter` Protocol（見 `aee/adapters/base.py`）封裝了
*HTTP 對接的* worker（Hermes via `/v1/runs`、未來 FastAPI-based worker）。
AEE-6 引入 `Artifact Pipeline` 後，下一個自然演進是 **Claude Code Worker**
（AEE-5 候選 adapter），但 Claude Code 的介面是 *本地 subprocess*（`claude -p`
或 `claude -p --bare --output-format stream-json`），跟 HTTP worker 的
lifecycle、錯誤模型、stdout/stderr 處理、cancel 語意都不同。

AEE-6.2 為這個演進建立**介面骨架**：

    ExecProvider (Protocol)
      ├── name / runtime_type        # 識別用
      ├── submit(...)                 # 非同步，傳回 external_run_id
      ├── poll(...)                   # 查狀態
      ├── cancel(...)                 # 中止（kill subprocess）
      ├── is_terminal(status)         # 判斷收尾
      ├── read_stdout(run_id)         # 拿 stdout bytes
      ├── read_stderr(run_id)         # 拿 stderr bytes
      ├── exit_code(run_id)           # process return code
      └── artifacts_dir(run_id)       # worker 寫入 artifacts 的 root

實作細節
--------
- **不**繼承 `RuntimeAdapter` Protocol — 兩者用途不同（一個是 HTTP、
  一個是 subprocess）。但所有 `RuntimeAdapter` Protocol 的三個方法
  （submit/poll/cancel）都會在後續 patch 中以 *adapter 形式*由
  `ExecProvider` 實作，遵守 single-implementation-multiple-roles
  的 AEE 慣例。
- `ExecProvider` 保留「process lifecycle」的所有資訊（pid、
  stdout buffer、stderr buffer、exit code、started_at、
  ended_at、timeout_state）— 這是 HTTP worker 沒有的。
- `artifacts_dir(run_id)` 回傳 worker 寫入的目錄根（Claude Code
  預設是 cwd；未來可能支援 `--out-dir` 旗標），dispatcher 把整個
  目錄交給 AEE-6 `ArtifactPipeline` 掃描。

Scope of this slice (AEE-6.2)
-----------------------------
- 定義 `ExecProvider` Protocol + 結果 dataclasses。
- 定義 `ProviderNotFoundError` / `ProviderTimeoutError` / `ProviderExitError`。
- 實作 `FakeClaudeCodeProvider` 純記憶體測試 stub。
- 寫 `aee/tests/test_exec_provider.py` 涵蓋 lifecycle。
- **不**實作 `RealClaudeCodeProvider`（subprocess）— 那是 AEE-6.3 /
  AEE-7 的工作，這一輪留為下一輪 patch 計畫。
- **不**動 `aee/adapters/base.py`（既有 RuntimeAdapter 是另一個抽象）。

Migration path
--------------
1. AEE-6.3：在 `aee/adapters/` 新增 `claude_code.py`，實作
   `ClaudeCodeProvider(ExecProvider)`，呼叫 `claude -p --bare
   --output-format stream-json`，supervised by a `Popen` lifecycle
   manager。預期 200–300 行 + 15+ 測試。
2. AEE-6.4：把 `ExecProvider` 透過 *adapter shim* 包成
   `RuntimeAdapter`，註冊到 AEE-5 `RuntimeRegistry`，讓
   orchestrator 可以 `POST /v1/jobs` with
   `runtime_requirements: {runtime_type: "claude_code"}`。
3. AEE-7：整合 `ExecProvider.artifacts_dir()` ↔ `ArtifactPipeline` —
   worker 結束時，dispatcher 把整個 artifacts_dir 丟給 pipeline 做
   收斂式 collect()。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import (
    Any,
    BinaryIO,
    Dict,
    Mapping,
    Optional,
    Protocol,
    runtime_checkable,
)


# ---------------------------------------------------------------------------
# Status vocabulary (subprocess-specific)
# ---------------------------------------------------------------------------


class ExecStatus(str, Enum):
    """Subprocess lifecycle states.

    Distinct from `RuntimePollResult.status` (HTTP-side vocabulary).
    The dispatcher adapter shim translates between the two.
    """

    PENDING = "pending"          # submit() called, Popen not yet started
    RUNNING = "running"          # Popen started, no exit yet
    EXITED = "exited"            # Popen returned (exit_code available)
    CANCELLED = "cancelled"      # killed on cancel()
    TIMED_OUT = "timed_out"      # killed by supervisor timeout
    FAILED = "failed"            # spawn() error / transport error


# Subset that the dispatcher considers "this run is done — no more polls".
_TERMINAL_STATUSES = frozenset(
    {ExecStatus.EXITED, ExecStatus.CANCELLED, ExecStatus.TIMED_OUT, ExecStatus.FAILED}
)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ExecSubmitResult:
    """Returned by `ExecProvider.submit()`.

    Attributes:
        external_run_id: The run id we use to refer to this subprocess
            (e.g. `claude-run-{uuid4}`). Distinct from the OS process
            pid; the run id is what the caller (dispatcher, orchestrator)
            uses as the durable handle.
        pid: OS process id (best-effort, None if Popen failed early).
        status: Initial status (PENDING or RUNNING depending on
            whether spawn() succeeded synchronously).
        started_at: ISO-8601 UTC timestamp of the spawn.
        raw: Unmodified backend response, for audit.
    """

    external_run_id: str
    pid: Optional[int] = None
    status: ExecStatus = ExecStatus.PENDING
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    )
    raw: Optional[Mapping[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "external_run_id": self.external_run_id,
            "pid": self.pid,
            "status": self.status.value,
            "started_at": self.started_at,
            "raw": dict(self.raw) if self.raw is not None else None,
        }


@dataclass
class ExecPollResult:
    """Returned by `ExecProvider.poll()`.

    Attributes:
        external_run_id: Same id returned by `submit()`.
        status: Current status (PENDING/RUNNING/EXITED/...).
        is_terminal: True iff no more polls will change the status.
        exit_code: Process return code (None if still running).
        output: Best-effort aggregated output text (from stdout).
        error: Backend error message (set on FAILED / TIMED_OUT).
        stdout_bytes: Number of stdout bytes received so far.
        stderr_bytes: Number of stderr bytes received so far.
        raw: Unmodified backend response, for audit.
    """

    external_run_id: str
    status: ExecStatus
    is_terminal: bool = False
    exit_code: Optional[int] = None
    output: Optional[str] = None
    error: Optional[str] = None
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    raw: Optional[Mapping[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "external_run_id": self.external_run_id,
            "status": self.status.value,
            "is_terminal": self.is_terminal,
            "exit_code": self.exit_code,
            "output": self.output,
            "error": self.error,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "raw": dict(self.raw) if self.raw is not None else None,
        }


@dataclass
class ExecCancelResult:
    """Returned by `ExecProvider.cancel()`.

    Attributes:
        external_run_id: The run id we tried to cancel.
        cancelled: True iff the backend acknowledges the cancellation
            AND the process is no longer running.
        reason: Optional human-readable reason.
    """

    external_run_id: str
    cancelled: bool
    reason: str = ""
    raw: Optional[Mapping[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "external_run_id": self.external_run_id,
            "cancelled": self.cancelled,
            "reason": self.reason,
            "raw": dict(self.raw) if self.raw is not None else None,
        }


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProviderError(Exception):
    """Base class for ExecProvider transport / spawn failures.

    Adapters should raise subclasses (or this class) for conditions
    that the dispatcher should treat as a transient infrastructure
    problem rather than a job-level failure.
    """

    def __init__(
        self,
        message: str,
        *,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.__cause__ = cause


class ProviderNotFoundError(ProviderError):
    """No provider registered for the requested `provider_name`."""


class ProviderTimeoutError(ProviderError):
    """Process exceeded its `timeout_seconds` and was killed."""


class ProviderExitError(ProviderError):
    """Process exited with a non-zero code or failed spawn."""

    def __init__(
        self,
        message: str,
        *,
        exit_code: Optional[int] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message, cause=cause)
        self.exit_code = exit_code


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ExecProvider(Protocol):
    """The seam AEE uses to talk to any subprocess-based worker.

    Concrete providers (`ClaudeCodeProvider`, future `CodexProvider`,
    `GeminiCliProvider`) must implement all methods. The protocol is
    structural — providers do not need to inherit from this class.

    Lifecycle (callers MUST follow this order):
        1. submit()          — start the process
        2. poll()            — read state (loop)
        3. on terminal:      — read_stdout() / read_stderr() / artifacts_dir()
                               to get the worker's outputs.
        4. cancel()          — best-effort cancel during step 2

    Threading: all methods are async. Implementations should not
    block the event loop on I/O — use threads or asyncio subprocess.
    """

    name: str
    runtime_type: str  # "claude_code" | "codex" | "gemini_cli" | ...

    async def submit(
        self,
        *,
        prompt: str,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        timeout_seconds: Optional[int] = None,
        run_id: Optional[str] = None,
    ) -> ExecSubmitResult: ...

    async def poll(self, external_run_id: str) -> ExecPollResult: ...

    async def cancel(self, external_run_id: str) -> ExecCancelResult: ...

    def read_stdout(self, external_run_id: str) -> BinaryIO: ...

    def read_stderr(self, external_run_id: str) -> BinaryIO: ...

    def exit_code(self, external_run_id: str) -> Optional[int]: ...

    def artifacts_dir(self, external_run_id: str) -> str: ...


# Imported here to break circular import (Job lives in aee.core).
# Concrete provider implementations can take `Job` as a parameter
# type-hint without importing it at module top-level.
from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from aee.core.job_models import Job


__all__ = [
    # status
    "ExecStatus",
    # result dataclasses
    "ExecSubmitResult",
    "ExecPollResult",
    "ExecCancelResult",
    # errors
    "ProviderError",
    "ProviderNotFoundError",
    "ProviderTimeoutError",
    "ProviderExitError",
    # protocol
    "ExecProvider",
]
