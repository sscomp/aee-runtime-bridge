"""AEE-7.1 ``ExecutionOrchestrator`` — single-step provider coordinator.

The orchestrator is the *seam* the dispatcher calls into when it
sees a job with ``runtime_requirements.runtime_type != "hermes"``
(or any explicit runtime_type). It:

1. Resolves a :class:`RuntimeDescriptor` via the existing AEE-5
   :class:`RuntimeSelector` (no regression to the legacy path).
2. Looks up a concrete :class:`Provider` via
   :func:`aee.orchestrator.factory.provider_for_descriptor`.
3. Calls ``provider.submit(...)`` and persists a
   ``dispatch_records`` row.
4. Caches the :class:`ProviderRun` so subsequent
   ``poll(external_run_id)`` / ``cancel(external_run_id)`` /
   ``artifacts_dir(external_run_id)`` calls route to the same
   provider instance.
5. Translates :class:`ProviderError` subclasses to the
   dispatcher's terminal states (``failed`` / ``timeout`` /
   ``cancelled``).

The orchestrator is **single-step**: one ``ProviderRun`` per
``submit`` call. AEE-7.3 will add a DAG layer on top; that work
is deliberately not in scope here.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from aee.runtimes.models import (
    DispatchRecord,
    DispatchStatus,
    RuntimeDescriptor,
    TaskRuntimeRequirements,
)
from aee.runtimes.repository import RuntimeRepository
from aee.runtimes.selector import (
    RuntimeNotFoundError,
    RuntimeSelector,
    select_runtime,
)

from .factory import provider_for_descriptor
from .provider import (
    Provider,
    ProviderCancelResult,
    ProviderError,
    ProviderExitError,
    ProviderNotFoundError,
    ProviderRun,
    ProviderStatus,
    ProviderStatusResult,
    ProviderSubmitError,
    ProviderTimeoutError,
)


log = logging.getLogger("aee.orchestrator")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# AEE-7.2 — ProviderStatus → DispatchStatus mapping
# ---------------------------------------------------------------------------
#
# The orchestrator previously wrote ``DispatchStatus.DISPATCHED`` for any
# non-running status and ``DispatchStatus.FAILED`` for everything else
# (``orchestrator.py:614-618`` ternary). That collapsed every terminal
# outcome (completed / timed_out / cancelled) into ``failed``, which made
# ``dispatch_records.dispatch_status`` misleading for ops queries
# (e.g. "how many tasks completed in the last 24h?" returned 0).
#
# The mapping below is the single source of truth. Both
# ``_persist_dispatch`` (initial row at submit time) and
# ``_update_dispatch_status`` (post-poll terminal update) use it.
#
# Edge cases:
# * An unknown / blank ``status`` string is coerced to ``failed``
#   with a debug log (defensive — never silently drop an update).
# * ``queued`` is preserved as ``dispatched`` because the row is
#   already on disk; the orchestrator does not have a separate
#   "queued" terminal state.
# * ``completed`` / ``timeout`` / ``cancelled`` / ``failed`` all
#   map to their own DispatchStatus literal (no collapsing).
_PROVIDER_TO_DISPATCH: Dict[str, str] = {
    ProviderStatus.QUEUED.value: DispatchStatus.DISPATCHED,
    ProviderStatus.RUNNING.value: DispatchStatus.DISPATCHED,
    ProviderStatus.COMPLETED.value: DispatchStatus.COMPLETED,
    ProviderStatus.FAILED.value: DispatchStatus.FAILED,
    ProviderStatus.TIMEOUT.value: DispatchStatus.TIMED_OUT,
    ProviderStatus.CANCELLED.value: DispatchStatus.CANCELLED,
}


def _map_provider_status_to_dispatch(status: str) -> str:
    """AEE-7.2 — single source of truth for ProviderStatus → DispatchStatus.

    Returns the canonical ``DispatchStatus`` literal for the given
    provider status string. Unknown values fall back to
    ``DispatchStatus.FAILED`` (defensive: an orchestrator should never
    write ``selected`` on a terminal update).
    """
    if not status:
        return DispatchStatus.FAILED
    mapped = _PROVIDER_TO_DISPATCH.get(status)
    if mapped is not None:
        return mapped
    log.debug(
        "orchestrator: unknown provider status %r; falling back to "
        "DispatchStatus.FAILED",
        status,
    )
    return DispatchStatus.FAILED


# ---------------------------------------------------------------------------
# AEE-7.3 — ProviderStatus → FailureCode vocabulary
# ---------------------------------------------------------------------------
#
# ``failure_code`` is the *reason* a run did not complete. It lives
# in a separate column from ``dispatch_status`` and is intentionally
# NULL when the run succeeded. Prior to AEE-7.3, the mapping was
# duplicated in two places (the initial submit-row writer and the
# post-poll terminal-update writer) and used magic strings
# inline. The vocabulary below is the single source of truth.
class FailureCode:
    """Canonical failure_code values for ``dispatch_records.failure_code``.

    These are persisted as TEXT and are read by the audit/ops
    dashboards. Adding a new code: extend this class + the
    ``_FAILURE_CODE_FOR_STATUS`` mapping; never inline a new
    literal at a call site.
    """

    # Provider reported a non-terminal failure at submit time.
    SUBMIT_ERROR = "submit_error"
    # Provider reported a non-terminal failure at poll time.
    PROVIDER_FAILED = "provider_failed"
    # Provider hit its internal timeout (mapped to
    # ``DispatchStatus.TIMED_OUT``).
    PROVIDER_TIMEOUT = "provider_timeout"
    # Provider acknowledged a cancel() request (mapped to
    # ``DispatchStatus.CANCELLED``).
    PROVIDER_CANCELLED = "provider_cancelled"

    @classmethod
    def all(cls) -> frozenset:
        return frozenset(
            {
                cls.SUBMIT_ERROR,
                cls.PROVIDER_FAILED,
                cls.PROVIDER_TIMEOUT,
                cls.PROVIDER_CANCELLED,
            }
        )


_FAILURE_CODE_FOR_STATUS: Dict[str, Optional[str]] = {
    ProviderStatus.FAILED.value: FailureCode.PROVIDER_FAILED,
    ProviderStatus.TIMEOUT.value: FailureCode.PROVIDER_TIMEOUT,
    ProviderStatus.CANCELLED.value: FailureCode.PROVIDER_CANCELLED,
}


def _failure_code_for_status(
    status: str, *, error: Optional[str]
) -> Optional[str]:
    """AEE-7.3 — single source of truth for ProviderStatus → failure_code.

    Returns the canonical ``failure_code`` for the given provider
    status, or ``None`` when the run completed successfully.

    The ``error`` argument disambiguates the two failure modes of
    ``ProviderStatus.FAILED``:

    * ``error`` is non-empty → the provider raised at submit
      time → :attr:`FailureCode.SUBMIT_ERROR`.
    * ``error`` is empty → the provider returned a terminal
      failure at poll time → :attr:`FailureCode.PROVIDER_FAILED`.

    Any other (non-terminal) status returns ``None``: the row is
    in-flight, not a failure, and ``failure_code`` is meaningless.
    Unknown status strings also return ``None`` (do not invent a
    code for an unmapped status — let ops notice the anomaly).
    """
    if status in (ProviderStatus.COMPLETED.value, ""):
        return None
    base = _FAILURE_CODE_FOR_STATUS.get(status)
    if base is None:
        return None
    if status == ProviderStatus.FAILED.value and error:
        return FailureCode.SUBMIT_ERROR
    return base


# ---------------------------------------------------------------------------
# Public result
# ---------------------------------------------------------------------------


@dataclass
class OrchestratorResult:
    """Returned by :meth:`ExecutionOrchestrator.submit`.

    This is the shape the dispatcher consumes; the watcher's
    existing ``RuntimePollResult`` translation continues to work
    unchanged because the orchestrator re-emits that shape on
    each ``poll()`` call.

    Fields
    ------
    provider_name
        ``provider.name`` — for logging / dispatch_records.
    runtime_type
        ``descriptor.runtime_type`` — for dispatch_records.
    external_run_id
        The id the watcher polls; persisted to
        ``tasks.external_run_id``.
    status
        Initial orchestrator status (``RUNNING`` or
        ``FAILED`` if submit raised).
    is_terminal
        True iff ``status`` is terminal (i.e. submit raised).
    output
        Best-effort output text. Empty for the immediate
        post-submit snapshot; populated by ``poll()``.
    error
        Error message if submit raised; ``None`` on success.
    exit_code
        Subprocess exit code or HTTP status; ``None`` while running.
    dispatch_record_id
        The ``dispatch_records.rowid`` of the audit row written
        by this submit. ``None`` if the repository is not
        configured to persist.
    provider
        The actual :class:`Provider` instance — held so the
        orchestrator can keep the handle alive (and the
        :class:`ProviderRun` cache can find it on poll).
    run
        The :class:`ProviderRun` handle. Stored in the
        orchestrator's in-memory cache; tests inspect it via
        :meth:`ExecutionOrchestrator.get_run`.
    """

    provider_name: str
    runtime_type: str
    external_run_id: str
    status: ProviderStatus
    is_terminal: bool
    output: Optional[str] = None
    error: Optional[str] = None
    exit_code: Optional[int] = None
    dispatch_record_id: Optional[str] = None
    provider: Optional[Provider] = field(default=None, repr=False)
    run: Optional[ProviderRun] = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class ExecutionOrchestrator:
    """Single-step provider coordinator.

    Lifetime
    --------
    A single orchestrator instance is created at app startup and
    held by the dispatcher (singleton). The run cache is
    process-local; a process restart orphans live runs, and the
    dispatcher reaper marks them ``timeout`` on the next tick.
    AEE-7.3 (DAG engine) will move this state into SQLite for
    durable orchestration; that is explicitly out of scope here.

    Thread safety
    -------------
    The run cache is guarded by an ``RLock``. The watcher's
    poll loop is single-threaded asyncio, but the orchestrator
    is also exercised by sync test code, so a lock keeps the
    invariants simple.
    """

    def __init__(
        self,
        *,
        repository: Optional[RuntimeRepository] = None,
        selector: Optional[RuntimeSelector] = None,
        allow_unknown_health: bool = True,
    ) -> None:
        self._repository = repository
        self._allow_unknown_health = bool(allow_unknown_health)
        # Selector is a thin wrapper; allow callers to inject
        # a custom one (e.g. a fake for tests).
        self._selector = selector or RuntimeSelector(
            allow_unknown_health=self._allow_unknown_health
        )
        self._runs: Dict[str, _CachedRun] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def select_descriptor(
        self, requirements: TaskRuntimeRequirements
    ) -> RuntimeDescriptor:
        """Resolve a :class:`RuntimeDescriptor` for the given requirements.

        Wraps :func:`aee.runtimes.selector.select_runtime` and
        extracts the descriptor. Raises :class:`RuntimeNotFoundError`
        (re-raised) when no match.
        """
        with self._lock:
            runtimes = self._available_runtimes_locked()
        record = select_runtime(
            task=requirements,
            available_runtimes=runtimes,
            allow_unknown_health=self._allow_unknown_health,
        )
        # ``record.selected_runtime_id`` is a string. Resolve
        # back to the descriptor we already have in
        # ``available_runtimes`` so the runtime_type / limits
        # are authoritative.
        chosen_id = getattr(record, "selected_runtime_id", None) or getattr(
            record, "runtime_id", None
        )
        for desc in runtimes:
            if desc.runtime_id == chosen_id:
                return desc
        # Fallback: caller passed a runtime not in our list.
        # Try the in-memory safe-lookup helper.
        for desc in list_runtimes_safe():
            if desc.runtime_id == chosen_id:
                return desc
        raise RuntimeNotFoundError(
            f"selector returned runtime_id={chosen_id!r} but no "
            f"descriptor found in available list"
        )

    async def submit(
        self,
        *,
        job: Any,  # dispatcher.models.Task — type-hinted as Any to avoid
                   # an import cycle (aee.core -> dispatcher is one-way).
        prompt: str,
        requirements: TaskRuntimeRequirements,
    ) -> OrchestratorResult:
        """Submit a job to its selected provider.

        Flow
        ----
        1. Resolve the descriptor (selector).
        2. Build a Provider (factory).
        3. Call ``provider.submit(...)`` and capture the
           :class:`ProviderRun`.
        4. Persist a ``dispatch_records`` audit row.
        5. Cache the run for poll / cancel / artifacts_dir.
        6. Return an :class:`OrchestratorResult`.

        Errors
        ------
        * :class:`RuntimeNotFoundError` — bubbled up so the
          dispatcher can fail() the task.
        * :class:`ProviderNotFoundError` — bubbled up.
        * :class:`ProviderSubmitError` — caught; the result has
          ``status=FAILED`` and the error message.
        """
        descriptor = self.select_descriptor(requirements)
        provider = provider_for_descriptor(
            descriptor, repository=self._repository
        )
        external_run_id = f"orch-{uuid.uuid4().hex[:12]}"
        started_at = _utcnow_iso()
        log.debug(
            "orchestrator.submit: job_id=%s runtime_type=%s "
            "provider=%s external_run_id=%s repo_root=%s",
            getattr(job, "task_id", None),
            descriptor.runtime_type,
            getattr(provider, "name", descriptor.runtime_type),
            external_run_id,
            getattr(requirements, "repo_root", None),
        )
        try:
            run = await provider.submit(
                prompt=prompt,
                requirements=requirements,
                repo=self._repository,
                run_id=external_run_id,
            )
        except ProviderNotFoundError:
            raise
        except ProviderError as exc:
            # ProviderError covers the "normal" failure modes the
            # provider contract defines (submit / timeout / exit).
            # Persist an audit row with the failure reason, return
            # a FAILED result; the dispatcher will transition the
            # task to `failed`.
            log.warning(
                "orchestrator.submit: provider %s raised %s: %s",
                getattr(provider, "name", descriptor.runtime_type),
                type(exc).__name__,
                exc,
            )
            record_id = self._persist_dispatch(
                descriptor=descriptor,
                external_run_id=external_run_id,
                status=ProviderStatus.FAILED.value,
                started_at=started_at,
                error=f"{type(exc).__name__}: {exc}",
            )
            return OrchestratorResult(
                provider_name=getattr(provider, "name", descriptor.runtime_type),
                runtime_type=descriptor.runtime_type,
                external_run_id=external_run_id,
                status=ProviderStatus.FAILED,
                is_terminal=True,
                error=f"{type(exc).__name__}: {exc}",
                dispatch_record_id=record_id,
                provider=provider,
            )
        # NOTE: We intentionally do NOT catch ``RuntimeError``
        # broadly here. A ``ProviderBinaryMissingError`` (a
        # ``RuntimeError`` subclass) is a *host-config* error —
        # the provider's binary is missing — and the dispatcher
        # needs to know that the failure is non-recoverable at
        # the provider layer. Propagating it lets the dispatcher
        # escalate (AEE-7.2 surface) instead of pretending the
        # task ran and FAILED. Other RuntimeErrors (NoneType,
        # AttributeError) are bugs that should fail loud, not
        # get papered over as a generic FAILED status.

        # Cache the run.
        cached = _CachedRun(
            provider=provider,
            run=run,
            descriptor=descriptor,
            task_id=getattr(job, "task_id", None),
        )
        with self._lock:
            self._runs[external_run_id] = cached

        # Persist a successful submit audit row.
        record_id = self._persist_dispatch(
            descriptor=descriptor,
            external_run_id=external_run_id,
            status=ProviderStatus.RUNNING.value,
            started_at=started_at,
        )

        log.info(
            "orchestrator.submit: task_id=%s external_run_id=%s provider=%s runtime_type=%s",
            getattr(job, "task_id", "?"),
            external_run_id,
            provider.name,
            descriptor.runtime_type,
        )
        return OrchestratorResult(
            provider_name=provider.name,
            runtime_type=descriptor.runtime_type,
            external_run_id=external_run_id,
            status=ProviderStatus.RUNNING,
            is_terminal=False,
            dispatch_record_id=record_id,
            provider=provider,
            run=run,
        )

    async def poll(
        self,
        run_or_id: Union[str, OrchestratorResult, ProviderRun],
    ) -> ProviderStatusResult:
        """Poll the underlying provider for status.

        Accepts one of three identifier shapes — the dispatcher's
        call site varies, so we keep all three working:

        * ``str`` ``external_run_id`` (the production watcher path).
        * :class:`OrchestratorResult` (the E2E test path).
        * :class:`ProviderRun` (defensive; matches the underlying
          :class:`Provider.poll` contract).

        Translates provider-specific states to
        :class:`ProviderStatus` and re-emits a
        :class:`ProviderStatusResult` so the watcher can consume
        it without knowing the transport.
        """
        if isinstance(run_or_id, str):
            external_run_id = run_or_id
        elif isinstance(run_or_id, OrchestratorResult):
            external_run_id = run_or_id.external_run_id
        elif isinstance(run_or_id, ProviderRun):
            # A ProviderRun handle is only useful if we can
            # reverse-map it to an external_run_id. We keep the
            # map in ``_runs_by_provider_run`` and the test path
            # is via OrchestratorResult, so this branch is
            # mostly defensive.
            with self._lock:
                for rid, cached in self._runs.items():
                    if cached.run is run_or_id:
                        external_run_id = rid
                        break
                else:
                    raise LookupError(
                        "no cached run matching the supplied ProviderRun"
                    )
        else:
            raise TypeError(
                f"poll() expects str | OrchestratorResult | ProviderRun, "
                f"got {type(run_or_id).__name__}"
            )
        cached = self._require(external_run_id)
        try:
            result = await cached.provider.poll(cached.run)
        except ProviderTimeoutError as exc:
            # Supervisor fired. Translate to TIMEOUT.
            self._update_dispatch_status(
                external_run_id, ProviderStatus.TIMEOUT.value, error=str(exc)
            )
            return ProviderStatusResult(
                external_run_id=external_run_id,
                status=ProviderStatus.TIMEOUT,
                is_terminal=True,
                error=str(exc),
            )
        except ProviderExitError as exc:
            self._update_dispatch_status(
                external_run_id, ProviderStatus.FAILED.value, error=str(exc)
            )
            return ProviderStatusResult(
                external_run_id=external_run_id,
                status=ProviderStatus.FAILED,
                is_terminal=True,
                error=str(exc),
            )
        except ProviderError as exc:
            log.warning(
                "orchestrator.poll: provider %s raised %s: %s",
                cached.provider.name,
                type(exc).__name__,
                exc,
            )
            # Treat other provider errors as transient — return
            # RUNNING and let the watcher / reaper decide.
            return ProviderStatusResult(
                external_run_id=external_run_id,
                status=ProviderStatus.RUNNING,
                is_terminal=False,
            )

        # Translate provider result to orchestrator status.
        translated = _translate_status(result, cached.provider)
        if translated.is_terminal:
            self._update_dispatch_status(
                external_run_id,
                translated.status.value,
                exit_code=translated.exit_code,
                error=translated.error,
            )
        return translated

    async def cancel(self, external_run_id: str) -> ProviderCancelResult:
        """Cancel the run via the underlying provider.

        Maps the provider's cancel() return into the orchestrator's
        ``ProviderCancelResult`` shape and persists a final
        ``dispatch_records`` row with status=cancelled.
        """
        cached = self._require(external_run_id)
        try:
            cancel_result = await cached.provider.cancel(cached.run)
        except ProviderError as exc:
            log.warning(
                "orchestrator.cancel: provider %s raised %s: %s",
                cached.provider.name,
                type(exc).__name__,
                exc,
            )
            cancel_result = ProviderCancelResult(
                external_run_id=external_run_id,
                cancelled=False,
                reason=f"{type(exc).__name__}: {exc}",
            )
        self._update_dispatch_status(
            external_run_id, ProviderStatus.CANCELLED.value
        )
        return cancel_result

    def artifacts_dir(self, external_run_id: str) -> Optional[str]:
        """Return the worker's artifacts directory, if any.

        Used by the dispatcher's `_verify_expected_delivery` to
        scope the artifact scan.
        """
        try:
            cached = self._require(external_run_id)
        except LookupError:
            return None
        try:
            return cached.provider.artifacts_dir(cached.run)
        except Exception as exc:  # noqa: BLE001 - defensive
            log.warning(
                "orchestrator.artifacts_dir: provider %s raised %s: %s",
                cached.provider.name,
                type(exc).__name__,
                exc,
            )
            return None

    def get_run(self, external_run_id: str) -> Optional[_CachedRun]:
        """Inspect the cached run (tests use this; production does not)."""
        with self._lock:
            return self._runs.get(external_run_id)

    def list_runs(self) -> List[str]:
        """List the external_run_ids currently in the cache."""
        with self._lock:
            return sorted(self._runs)

    def reset(self) -> None:
        """Drop all cached runs. Tests use this; never call from production."""
        with self._lock:
            self._runs.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _available_runtimes_locked(self) -> List[RuntimeDescriptor]:
        """Read the runtimes from the repository (or fall back to
        the in-memory list).
        """
        if self._repository is None:
            return list_runtimes_safe()
        try:
            return list(self._repository.list_all())
        except Exception as exc:  # noqa: BLE001 - defensive
            log.warning(
                "orchestrator: repository.list_all failed: %s; "
                "falling back to in-memory list",
                exc,
            )
            return list_runtimes_safe()

    def _require(self, external_run_id: str) -> "_CachedRun":
        with self._lock:
            cached = self._runs.get(external_run_id)
            if cached is None:
                raise LookupError(
                    f"no cached run for external_run_id={external_run_id!r}"
                )
            return cached

    def _persist_dispatch(
        self,
        *,
        descriptor: RuntimeDescriptor,
        external_run_id: str,
        status: str,
        started_at: str,
        error: Optional[str] = None,
    ) -> Optional[str]:
        if self._repository is None:
            return None
        try:
            dispatch_id = str(uuid.uuid4())
            # AEE-7.3: failure_code is derived via the single
            # source of truth ``_failure_code_for_status`` — same
            # helper used by the post-poll update path. The
            # ``error`` arg disambiguates submit-time vs.
            # poll-time failure (see FailureCode docstring).
            record = DispatchRecord(
                dispatch_id=dispatch_id,
                task_id=self._task_id_for(external_run_id) or "",
                run_id=external_run_id,
                selected_runtime_id=descriptor.runtime_id,
                selected_runtime_type=descriptor.runtime_type,
                selection_reason=f"orchestrator submit (status={status})",
                dispatched_at=started_at,
                # AEE-7.2: status reflects what the provider reported
                # *at submit time* — typically ``running`` or ``queued``
                # for a healthy start, ``failed`` for a provider
                # that failed synchronously (e.g. binary missing).
                # The terminal status (completed / timed_out / cancelled)
                # comes in via ``_update_dispatch_status``.
                dispatch_status=_map_provider_status_to_dispatch(status),
                failure_code=_failure_code_for_status(status, error=error),
                failure_message=error,
            )
            self._repository.insert_dispatch_record(record)
            return dispatch_id
        except Exception as exc:  # noqa: BLE001 - defensive
            log.warning(
                "orchestrator: failed to persist dispatch_records: %s", exc
            )
            return None

    def _task_id_for(self, external_run_id: str) -> Optional[str]:
        with self._lock:
            cached = self._runs.get(external_run_id)
            if cached is None:
                return None
            return cached.task_id

    def _update_dispatch_status(
        self,
        external_run_id: str,
        status: str,
        *,
        exit_code: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        if self._repository is None:
            return
        # Look up the dispatch_id by run_id; this avoids the
        # orchestrator having to track its own dispatch_id -> run_id
        # mapping.
        try:
            records = self._repository.list_dispatch_records(
                task_id=None, runtime_id=None, limit=50
            )
        except Exception:  # noqa: BLE001
            records = []
        matching: List[DispatchRecord] = [
            r for r in records if r.run_id == external_run_id
        ]
        if not matching:
            return
        # Pick the most recent.
        matching.sort(key=lambda r: r.dispatched_at, reverse=True)
        target = matching[0]
        # AEE-7.3: failure_code now comes from a single
        # source-of-truth helper ``_failure_code_for_status``,
        # shared with the initial submit-row writer. The two
        # sites can no longer drift on the failure_code
        # vocabulary. ``error`` carries through for
        # ``submit_error`` vs ``provider_failed`` disambiguation
        # when status is ``failed``.
        failure_code: Optional[str] = _failure_code_for_status(
            status, error=error
        )
        failure_message: Optional[str] = error or ""
        if exit_code is not None:
            failure_message = f"exit_code={exit_code} {failure_message}".strip()
        try:
            self._repository.update_dispatch_status(
                target.dispatch_id,
                status=_map_provider_status_to_dispatch(status),
                failure_code=failure_code,
                failure_message=failure_message,
            )
        except Exception as exc:  # noqa: BLE001 - defensive
            log.warning(
                "orchestrator: failed to update dispatch_records: %s", exc
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _CachedRun:
    """Internal: per-run state the orchestrator holds."""

    provider: Provider
    run: ProviderRun
    descriptor: RuntimeDescriptor
    task_id: Optional[str] = None


# A test-only override list for ``list_runtimes_safe()``. When
# non-empty, this is returned instead of the built-in list. Tests
# use this to inject a ``fake``-typed runtime descriptor so the
# selector can pick it. Always restored to ``[]`` in setUp.
_TEST_RUNTIMES: List[RuntimeDescriptor] = []


def register_runtimes_for_test(
    runtimes: List[RuntimeDescriptor],
) -> List[RuntimeDescriptor]:
    """Replace the in-memory safe-list with ``runtimes`` (test-only)."""
    global _TEST_RUNTIMES
    _TEST_RUNTIMES = list(runtimes)
    return list(_TEST_RUNTIMES)


def clear_test_runtimes() -> None:
    """Restore the built-in safe-list (test-only)."""
    global _TEST_RUNTIMES
    _TEST_RUNTIMES = []


def list_runtimes_safe() -> List[RuntimeDescriptor]:
    """Return a safe fallback list of runtimes when no repository is
    available. The list contains the canonical AEE-5 + AEE-6
    descriptors (built-in only). Tests can register custom
    descriptors via :func:`register_runtimes_for_test`.
    """
    if _TEST_RUNTIMES:
        return list(_TEST_RUNTIMES)
    from aee.runtimes.builtins.aee_lightweight import build_default_descriptor
    from aee.runtimes.builtins.claude_code_local import build_claude_code_descriptor

    rts: List[RuntimeDescriptor] = []
    rts.append(build_default_descriptor())
    claude = build_claude_code_descriptor()
    if claude is not None:
        rts.append(claude)
    return rts


def _translate_status(
    provider_result: ProviderStatusResult, provider: Provider
) -> ProviderStatusResult:
    """Re-emit the provider's result with normalized status.

    The provider's result already conforms to the
    :class:`ProviderStatusResult` shape, so this is mostly a
    pass-through plus a defensive status-string normalization.
    """
    raw = provider_result.status
    if isinstance(raw, ProviderStatus):
        norm = raw
    else:
        # Some providers may return a string (e.g. the Hermes
        # adapter's RuntimePollResult); normalize it.
        try:
            norm = ProviderStatus(str(raw).lower())
        except ValueError:
            # Unknown status — treat as running so the watcher
            # does not falsely mark the task terminal.
            return ProviderStatusResult(
                external_run_id=provider_result.external_run_id,
                status=ProviderStatus.RUNNING,
                is_terminal=False,
                output=provider_result.output,
                error=provider_result.error,
                exit_code=provider_result.exit_code,
                usage=provider_result.usage,
                raw=provider_result.raw,
            )
    return ProviderStatusResult(
        external_run_id=provider_result.external_run_id,
        status=norm,
        is_terminal=norm.is_terminal,
        output=provider_result.output,
        error=provider_result.error,
        exit_code=provider_result.exit_code,
        usage=provider_result.usage,
        raw=provider_result.raw,
    )


__all__ = ["ExecutionOrchestrator", "OrchestratorResult"]
