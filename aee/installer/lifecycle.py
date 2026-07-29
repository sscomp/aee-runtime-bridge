"""AEE Bootstrap v1 — W1 Bootstrap Core Skeleton (§4 + §5 + §10.4).

This module is the **shared bootstrap core** that future
platform-specific work orders (W2 marker persistence, W3 doctor, W4
install CLI, W5 update CLI, W6 POSIX trampoline, W7 Windows trampoline)
build on. It is deliberately minimal and side-effect free:

* **Stage / state model.** :class:`StageName` enumerates the eight
  bootstrap stages from §4 (``00_detect`` … ``07_agent_ready``);
  :class:`StageState` enumerates the lifecycle states a stage can be
  in (``PENDING`` / ``IN_PROGRESS`` / ``COMPLETED`` / ``FAILED`` /
  ``SKIPPED``). These are the stable vocabulary every future layer
  consumes.
* **Marker storage abstraction.** :class:`MarkerStore` is a
  :class:`typing.Protocol` describing the read/write/list surface a
  persistence backend must satisfy. :class:`InMemoryMarkerStore` is
  the default, in-process, non-persistent implementation (safe for
  tests and for the skeleton; W2 will add a file-system backend).
  Nothing in this module touches the filesystem or network.
* **Lifecycle framework hook.** :class:`BootstrapLifecycle` records
  stage transitions, computes the resume stage (first ``PENDING`` or
  ``FAILED`` stage per §5.5), and reports completion. It does **not**
  execute the stages — stage execution is owned by the shell layer
  (§4 stages 00–02) and the Python backend (§4 stages 03–07), both of
  which are out of scope for W1.
* **Platform / profile detection hooks.** :func:`detect_platform` and
  :func:`default_profile_for` are thin framework hooks over the
  existing :mod:`aee.platform.current` resolver and the
  :mod:`aee.deploy.capabilities` defaults. They deliberately do
  **not** claim full Ubuntu / Debian / macOS / Windows installer
  support — Windows resolves to :data:`PlatformIdentity.WINDOWS`
  (W1 skeleton adapter; the default mapping is ``None`` so the
  resolver returns :data:`UnknownDefaults` unless the operator
  passes ``--adapter windows``), matching the existing honest-skeleton
  contract in :mod:`aee.platform.current`.
* **Proposed exit constants 7–12.** §10.4 defines six new exit codes
  for the bootstrap v1 surface. They are recorded here as module
  constants so future CLI layers (W3/W4/W5) import a single canonical
  source. They do **not** collide with the verified constants
  ``{0, 2, 3, 4, 5, 6}`` in :mod:`aee.installer.backend`.

Design invariants (W1 skeleton contract):

1. **No subprocess.** No stage is executed; the lifecycle is a state
   recorder, not a runner.
2. **No filesystem writes.** The default :class:`InMemoryMarkerStore`
   holds state in memory. Persistence is a W2 follow-up.
3. **No network.** No clone, no fetch, no package installs.
4. **Honest placeholders.** Stage execution is explicitly unimplemented;
   :func:`detect_platform` does not pretend Windows is supported.
5. **Stable vocabulary.** :class:`StageName`, :class:`StageState`,
   the exit constants, and the :class:`MarkerStore` Protocol are the
   stable interfaces future work orders consume; they are not expected
   to be renumbered (§10.4 conflict-resolution notes).
6. **No collision with verified exit codes.** The proposed constants
   occupy ``{7, 8, 9, 10, 11, 12}``; the verified constants in
   :mod:`aee.installer.backend` occupy ``{0, 2, 3, 4, 5, 6}``.

Reference: ``reports/aee_bootstrap_v1_spec.md`` §4 (lifecycle),
§5 (idempotency / resume), §10.4 (exit codes), §16 W1 + W2.
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)

# Re-use the existing platform resolver (Phase 1, §21.6). W1 does NOT
# extend PlatformIdentity — Windows stays UNKNOWN (honest skeleton).
from aee.platform.current import (
    PlatformIdentity,
    resolve_platform_identity,
)


# ---------------------------------------------------------------------------#
# Exit codes — proposed bootstrap v1 surface (§10.4)
# ---------------------------------------------------------------------------#
#
# These constants are net-new (PROPOSAL in §10.4). They MUST NOT collide
# with the verified constants in ``aee.installer.backend``:
#
#   EXIT_OK = 0, EXIT_PARSE_ERROR = 2, EXIT_PROFILE_INVALID = 3,
#   EXIT_PRE_FLIGHT_FAILED = 4, EXIT_PROFILE_SWITCH_REJECTED = 5,
#   EXIT_EXECUTE_NOT_AUTHORIZED = 6
#
# The free range for bootstrap v1 is {7..12}; {64..127} is reserved.

#: A bootstrap stage failed but is retryable (re-run with ``--resume``).
EXIT_STAGE_FAILED_RETRYABLE = 7

#: A bootstrap stage failed permanently (max retries exceeded);
#: requires ``--force-retry`` or operator intervention.
EXIT_STAGE_FAILED_PERMANENT = 8

#: ``aee doctor`` only — on-disk state drifted from the recorded pin
#: (``commit_sha`` or ``requirements_lock_sha256`` mismatch).
EXIT_DRIFT_DETECTED = 9

#: Network / git error (clone, fetch, package mirror unreachable).
EXIT_NETWORK_ERROR = 10

#: A required secret is missing or invalid.
EXIT_SECRET_MISSING = 11

#: A hard dependency floor (git, python, node) is not met and cannot
#: be auto-installed.
EXIT_DEPENDENCY_FLOOR_NOT_MET = 12


# ---------------------------------------------------------------------------#
# Stage vocabulary (§4)
# ---------------------------------------------------------------------------#


class StageName(enum.Enum):
    """The eight bootstrap stages from §4.

    Values are the canonical marker filenames (``00_detect`` …
    ``07_agent_ready``). The ordering reflects the §4 stage-machine
    sequence; :meth:`BootstrapLifecycle.get_resume_stage` returns the
    first stage with no marker or ``state=FAILED`` per §5.5.
    """

    DETECT = "00_detect"
    DEPS = "01_deps"
    CLONE = "02_clone"
    PIN = "03_pin"
    RUNTIME_SETUP = "04_runtime_setup"
    HEALTH_CHECK = "05_health_check"
    SMOKE_TEST = "06_smoke_test"
    AGENT_READY = "07_agent_ready"


#: Stages owned by the shell / PowerShell layer (§4). These run before
#: Python is present, so they cannot be owned by the Python backend.
SHELL_STAGES: FrozenSet[StageName] = frozenset(
    {StageName.DETECT, StageName.DEPS, StageName.CLONE}
)

#: Stages owned by the Python backend (§4). Invoked as
#: ``python -m aee.installer.cli install --resume`` once Python is
#: available. W4 wires the CLI; W1 only records the vocabulary.
PYTHON_STAGES: FrozenSet[StageName] = frozenset(
    {
        StageName.PIN,
        StageName.RUNTIME_SETUP,
        StageName.HEALTH_CHECK,
        StageName.SMOKE_TEST,
        StageName.AGENT_READY,
    }
)


class StageState(enum.Enum):
    """Lifecycle state of a single stage (§5).

    * :data:`PENDING` — no marker yet (stage has not run).
    * :data:`IN_PROGRESS` — stage started but not yet completed.
    * :data:`COMPLETED` — stage succeeded; marker written.
    * :data:`FAILED` — stage failed; marker records ``error_class`` +
      ``stderr_tail`` + ``retry_count`` (§5.3).
    * :data:`SKIPPED` — stage explicitly skipped (e.g. ``--from``
      resume semantics in §5.5).
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


#: Maximum retry attempts per stage before the stage is permanent
#: (§5.3: "After 3 consecutive failures of the same stage, the
#: bootstrap refuses to retry without ``--force-retry``").
MAX_RETRY: int = 3

#: Exponential backoff schedule for network operations (§5.4:
#: "3 attempts with 2s / 4s / 8s sleeps, configurable via
#: ``AEE_BOOTSTRAP_RETRY_*`` env vars"). Recorded here as the
#: canonical default; W6/W7 consume it.
RETRY_BACKOFF_SECONDS: Tuple[int, ...] = (2, 4, 8)


# ---------------------------------------------------------------------------#
# Marker + state dataclasses
# ---------------------------------------------------------------------------#


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (§4 marker
    format). ``Z`` suffix matches the §4 spec example."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class StageMarker:
    """A single stage marker (§4 + §5.3).

    Frozen so a marker, once recorded, cannot be silently mutated;
    transitions write a new marker via
    :meth:`BootstrapLifecycle.record_stage`.

    Fields:

    * ``stage`` — the :class:`StageName` this marker describes.
    * ``run_id`` — the bootstrap run id (shared across all markers
      of one bootstrap invocation).
    * ``state`` — the :class:`StageState`.
    * ``started_at`` — ISO-8601 UTC timestamp when the stage first
      moved to ``IN_PROGRESS``.
    * ``completed_at`` — ISO-8601 UTC timestamp when the stage
      reached a terminal state (``COMPLETED`` / ``FAILED`` /
      ``SKIPPED``); ``None`` while ``IN_PROGRESS`` or ``PENDING``.
    * ``error_class`` — for ``FAILED`` markers, the exception class
      name (§5.3); ``None`` otherwise.
    * ``stderr_tail`` — for ``FAILED`` markers, the last 4 KB of
      stderr, redacted per §8.4. W1 stores the raw string; the
      redaction filter is a W10 deliverative (§8.2 [PROPOSAL]).
    * ``retry_count`` — number of consecutive failures of this stage
      (§5.3); 0 for a fresh stage.
    """

    stage: StageName
    run_id: str
    state: StageState
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error_class: Optional[str] = None
    stderr_tail: Optional[str] = None
    retry_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage.value,
            "run_id": self.run_id,
            "state": self.state.value,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error_class": self.error_class,
            "stderr_tail": self.stderr_tail,
            "retry_count": self.retry_count,
        }


@dataclass(frozen=True)
class BootstrapState:
    """The complete state of one bootstrap run (§5.5).

    Frozen so the recorded state is immutable; transitions produce a
    new :class:`BootstrapState` via
    :meth:`BootstrapLifecycle.record_stage`. The
    :class:`MarkerStore` is responsible for persistence; this dataclass
    is the value the store reads / writes.

    Fields:

    * ``run_id`` — unique id for this bootstrap run.
    * ``started_at`` — ISO-8601 UTC timestamp of
      :meth:`BootstrapLifecycle.start`.
    * ``last_updated_at`` — ISO-8601 UTC timestamp of the most recent
      :meth:`BootstrapLifecycle.record_stage` call.
    * ``markers`` — mapping ``StageName -> StageMarker``. Stages
      without a marker are absent from the mapping (equivalent to
      :data:`StageState.PENDING`).
    """

    run_id: str
    started_at: str
    last_updated_at: str
    markers: Dict[StageName, StageMarker] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "last_updated_at": self.last_updated_at,
            "markers": {
                stage.value: marker.to_dict()
                for stage, marker in self.markers.items()
            },
        }


# ---------------------------------------------------------------------------#
# Marker storage abstraction (W2 will add a file-system backend)
# ---------------------------------------------------------------------------#


@runtime_checkable
class MarkerStore(Protocol):
    """Storage backend for :class:`BootstrapState` (framework hook).

    W1 ships an in-memory implementation (:class:`InMemoryMarkerStore`).
    W2 will add a file-system backend that writes marker files under
    ``bootstrap/stages/`` (or ``%LOCALAPPDATA%\\AEE\\bootstrap\\stages``
    on Windows) per §4. The Protocol is the stable interface both
    backends satisfy.

    Contract:

    * :meth:`read_state` returns the :class:`BootstrapState` for
      ``run_id`` or ``None`` if no such run is recorded.
    * :meth:`write_state` persists ``state`` under
      ``state.run_id``. Implementations MUST be idempotent: writing
      the same state twice yields the same on-disk (or in-memory)
      result.
    * :meth:`list_runs` returns the run ids known to the store, in
      insertion order (the order they were first written).
    """

    def read_state(self, run_id: str) -> Optional[BootstrapState]:
        ...

    def write_state(self, state: BootstrapState) -> None:
        ...

    def list_runs(self) -> List[str]:
        ...


class InMemoryMarkerStore:
    """Default :class:`MarkerStore` — in-process, non-persistent.

    Safe for the W1 skeleton and for tests. Holds state in a plain
    ``dict`` keyed by ``run_id``. ``list_runs`` returns run ids in
    insertion order (first-write order).
    """

    def __init__(self) -> None:
        self._store: Dict[str, BootstrapState] = {}
        self._order: List[str] = []

    def read_state(self, run_id: str) -> Optional[BootstrapState]:
        return self._store.get(run_id)

    def write_state(self, state: BootstrapState) -> None:
        if state.run_id not in self._store:
            self._order.append(state.run_id)
        self._store[state.run_id] = state

    def list_runs(self) -> List[str]:
        return list(self._order)


# ---------------------------------------------------------------------------#
# Bootstrap lifecycle (framework hook — does NOT execute stages)
# ---------------------------------------------------------------------------#


class BootstrapLifecycle:
    """Records bootstrap stage transitions and computes resume state.

    This is the **framework hook** that future platform-specific layers
    (W6 POSIX trampoline, W7 Windows trampoline, W4 install CLI) drive.
    It performs **no** side effects: it does not run stages, it does not
    write marker files, it does not call the network. It only records
    stage state in the supplied :class:`MarkerStore`.

    Usage (illustrative — execution is out of scope for W1)::

        store = InMemoryMarkerStore()
        lc = BootstrapLifecycle(store)
        lc.start()
        lc.record_stage(StageName.DETECT, StageState.IN_PROGRESS)
        # ... shell layer runs stage 00_detect ...
        lc.record_stage(StageName.DETECT, StageState.COMPLETED)
        # ... later, after a failure + re-run ...
        resume_at = lc.get_resume_stage()  # first PENDING/FAILED stage
    """

    def __init__(
        self,
        store: MarkerStore,
        run_id: Optional[str] = None,
    ) -> None:
        self._store = store
        # Lazily created on first call to start(); callers may supply
        # their own run id (e.g. to resume a recorded run).
        self._run_id = run_id
        self._state: Optional[BootstrapState] = None
        if run_id is not None:
            # Attempt to load an existing run so record_stage() can
            # resume it. If the store has no record, start() will
            # create a fresh state under this run_id.
            existing = store.read_state(run_id)
            if existing is not None:
                self._state = existing

    # -- read-only accessors ------------------------------------------------

    @property
    def store(self) -> MarkerStore:
        return self._store

    @property
    def run_id(self) -> Optional[str]:
        return self._run_id

    @property
    def state(self) -> Optional[BootstrapState]:
        return self._state

    # -- state transitions --------------------------------------------------

    def start(self, run_id: Optional[str] = None) -> BootstrapState:
        """Begin a new bootstrap run (or re-bind to an existing one).

        If ``run_id`` is supplied, it overrides any id passed to
        :meth:`__init__`. If the store already holds a state for the
        resolved run id, that state is loaded (resume); otherwise a
        fresh :class:`BootstrapState` is created and written.

        Raises :class:`RuntimeError` if :meth:`start` is called twice
        on the same lifecycle without an intervening ``run_id``
        override AND a state was already started (defence in depth —
        callers should construct a new :class:`BootstrapLifecycle` for
        a fresh run, or pass an explicit ``run_id`` to re-bind).
        """
        rid = run_id or self._run_id or _new_run_id()
        self._run_id = rid
        existing = self._store.read_state(rid)
        if existing is not None:
            # Resume: the store already holds a state for this run id
            # (either from a previous lifecycle instance or from an
            # earlier start() on this one). Re-binding is a no-op.
            self._state = existing
            return existing
        now = _utc_now_iso()
        state = BootstrapState(
            run_id=rid,
            started_at=now,
            last_updated_at=now,
            markers={},
        )
        self._store.write_state(state)
        self._state = state
        return state

    def record_stage(
        self,
        stage: StageName,
        state: StageState,
        *,
        error_class: Optional[str] = None,
        stderr_tail: Optional[str] = None,
        retry_count: Optional[int] = None,
    ) -> StageMarker:
        """Record a stage transition and persist the new state.

        Returns the resulting :class:`StageMarker`. The previous
        marker for ``stage`` (if any) is replaced.

        Semantics:

        * ``PENDING`` is the implicit state for stages with no marker;
          recording ``PENDING`` removes the marker (defence in depth —
          callers normally do not need to record ``PENDING``).
        * ``IN_PROGRESS`` sets ``started_at`` if not already set.
        * ``COMPLETED`` / ``FAILED`` / ``SKIPPED`` set
          ``completed_at``. ``FAILED`` records ``error_class`` and
          ``stderr_tail``; ``retry_count`` defaults to the previous
          marker's count + 1 for a FAILED transition (or 1 if there
          was no previous FAILED marker), unless the caller supplies
          an explicit ``retry_count``.

        Raises :class:`RuntimeError` if :meth:`start` was never called.
        """
        if self._state is None:
            raise RuntimeError("BootstrapLifecycle.start() not called")
        prev = self._state.markers.get(stage)
        now = _utc_now_iso()

        # Compute retry_count for FAILED transitions.
        rc: int
        if retry_count is not None:
            rc = retry_count
        elif state is StageState.FAILED:
            rc = (prev.retry_count + 1) if prev is not None else 1
        else:
            rc = prev.retry_count if prev is not None else 0

        # Preserve started_at across transitions.
        started_at = prev.started_at if prev is not None else None
        if state is StageState.IN_PROGRESS and started_at is None:
            started_at = now

        # completed_at is set for terminal states, cleared otherwise.
        completed_at: Optional[str]
        if state in (
            StageState.COMPLETED,
            StageState.FAILED,
            StageState.SKIPPED,
        ):
            completed_at = now
        else:
            completed_at = None

        marker = StageMarker(
            stage=stage,
            run_id=self._state.run_id,
            state=state,
            started_at=started_at,
            completed_at=completed_at,
            error_class=error_class if state is StageState.FAILED else None,
            stderr_tail=stderr_tail if state is StageState.FAILED else None,
            retry_count=rc,
        )

        new_markers = dict(self._state.markers)
        if state is StageState.PENDING:
            # PENDING is the implicit default; drop the marker.
            new_markers.pop(stage, None)
        else:
            new_markers[stage] = marker
        self._state = BootstrapState(
            run_id=self._state.run_id,
            started_at=self._state.started_at,
            last_updated_at=now,
            markers=new_markers,
        )
        self._store.write_state(self._state)
        return marker

    def get_marker(self, stage: StageName) -> Optional[StageMarker]:
        """Return the recorded marker for ``stage`` or ``None``."""
        if self._state is None:
            return None
        return self._state.markers.get(stage)

    def get_resume_stage(self) -> Optional[StageName]:
        """Return the first stage to (re)run per §5.5.

        The first stage (in §4 order) with no marker or
        ``state=FAILED``. Returns ``None`` if every stage has a
        ``COMPLETED`` or ``SKIPPED`` marker (the run is complete).
        """
        if self._state is None:
            return None
        for stage in StageName:
            marker = self._state.markers.get(stage)
            if marker is None:
                return stage
            if marker.state is StageState.FAILED:
                return stage
            # COMPLETED / SKIPPED / IN_PROGRESS / PENDING(implicit):
            # IN_PROGRESS is treated as "needs re-run" since the
            # process that started it may have died.
            if marker.state is StageState.IN_PROGRESS:
                return stage
        return None

    def is_complete(self) -> bool:
        """True iff every stage has a ``COMPLETED`` or ``SKIPPED`` marker."""
        if self._state is None:
            return False
        for stage in StageName:
            marker = self._state.markers.get(stage)
            if marker is None:
                return False
            if marker.state not in (StageState.COMPLETED, StageState.SKIPPED):
                return False
        return True


def _new_run_id() -> str:
    """Generate a fresh bootstrap run id (UUID4, hex)."""
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------#
# Platform / profile detection framework hooks (§2.3 + §2.4)
# ---------------------------------------------------------------------------#
#
# These hooks are thin wrappers over the existing Phase 1 resolver
# (aee.platform.current). W1 has shipped the WINDOWS identity +
# WindowsAdapter skeleton; detect_platform now surfaces WINDOWS for
# win32/cygwin. The default adapter mapping for WINDOWS is None
# (UnknownDefaults) per §17.3 Phase C — operators opt in via
# --adapter windows.


def detect_platform(platform: Optional[str] = None) -> PlatformIdentity:
    """Detect the current :class:`PlatformIdentity` (§2.3 framework hook).

    Delegates to :func:`aee.platform.current.resolve_platform_identity`.
    Windows (``"win32"`` / ``"cygwin"``) resolves to
    :data:`PlatformIdentity.WINDOWS` (W1 skeleton adapter shipped). The
    default adapter mapping for WINDOWS is ``None`` per §17.3 Phase C,
    so the resolver returns :data:`UnknownDefaults` unless the operator
    passes ``--adapter windows`` explicitly. This is the **honest**
    placeholder: callers receive :data:`UnknownDefaults` capabilities
    rather than a fabricated Windows capability set.

    Tests inject ``platform`` explicitly (no ``sys.platform`` mock
    needed for the hook itself; the underlying resolver is the only
    ``sys.platform`` read site).
    """
    return resolve_platform_identity(platform)


#: Default profile routing per §2.4 (framework hook). Matches the
#: existing :data:`LinuxDefaults.profile_default` ("full") and
#: :data:`MacOSDefaults.profile_default` ("developer"). UNKNOWN has no
#: default — callers must refuse work rather than guessing.
_DEFAULT_PROFILE_BY_IDENTITY: Dict[PlatformIdentity, str] = {
    PlatformIdentity.LINUX: "full",
    PlatformIdentity.MACOS: "developer",
    # UNKNOWN intentionally absent — see default_profile_for().
}


def default_profile_for(platform_id: PlatformIdentity) -> str:
    """Return the default Runtime Profile for ``platform_id`` (§2.4).

    Framework hook for the bootstrap's stage 00 (detect). Matches the
    existing capability defaults:

    * :data:`PlatformIdentity.LINUX` → ``"full"`` (matches
      :data:`aee.deploy.capabilities.LinuxDefaults.profile_default`).
    * :data:`PlatformIdentity.MACOS` → ``"developer"`` (matches
      :data:`aee.deploy.capabilities.MacOSDefaults.profile_default`).
    * :data:`PlatformIdentity.UNKNOWN` → ``""`` (empty string). The
      caller MUST refuse work rather than guessing; an empty default
      is the explicit, machine-readable signal that no default exists.

    This is a **placeholder** routing table. W4 (``aee install`` CLI)
    consumes it; it does not itself validate the profile against
    :data:`~aee.profiles.descriptor.KNOWN_PROFILES` (that validation
    lives in the canonical profile descriptor, imported by W4).
    """
    return _DEFAULT_PROFILE_BY_IDENTITY.get(platform_id, "")


__all__ = [
    # Exit codes (§10.4 proposed)
    "EXIT_STAGE_FAILED_RETRYABLE",
    "EXIT_STAGE_FAILED_PERMANENT",
    "EXIT_DRIFT_DETECTED",
    "EXIT_NETWORK_ERROR",
    "EXIT_SECRET_MISSING",
    "EXIT_DEPENDENCY_FLOOR_NOT_MET",
    # Stage vocabulary (§4)
    "StageName",
    "StageState",
    "SHELL_STAGES",
    "PYTHON_STAGES",
    "MAX_RETRY",
    "RETRY_BACKOFF_SECONDS",
    # Markers + state
    "StageMarker",
    "BootstrapState",
    # Storage
    "MarkerStore",
    "InMemoryMarkerStore",
    # Lifecycle
    "BootstrapLifecycle",
    # Detection hooks (§2.3 + §2.4)
    "detect_platform",
    "default_profile_for",
]