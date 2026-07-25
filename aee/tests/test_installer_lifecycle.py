"""AEE Bootstrap v1 — W1 Bootstrap Core Skeleton targeted tests.

Targets the shared bootstrap core in :mod:`aee.installer.lifecycle`.
All tests are stdlib ``unittest`` — no pytest, no filesystem, no network.

Coverage:

* :class:`TestExitConstants` — proposed exit codes 7..12 do not collide
  with the verified constants {0, 2, 3, 4, 5, 6}.
* :class:`TestStageVocabulary` — StageName ordering + values; SHELL vs
  PYTHON stage partition is exhaustive and disjoint.
* :class:`TestInMemoryMarkerStore` — read/write/list idempotency,
  insertion order, missing-run returns None.
* :class:`TestBootstrapLifecycleTransitions` — start, record_stage for
  PENDING/IN_PROGRESS/COMPLETED/FAILED/SKIPPED, started_at /
  completed_at semantics, retry_count increment, error_class /
  stderr_tail recorded only on FAILED.
* :class:`TestBootstrapLifecycleResume` — get_resume_stage returns
  first PENDING (no markers), first FAILED, first IN_PROGRESS;
  returns None when complete; is_complete semantics.
* :class:`TestBootstrapLifecycleRunId` — explicit run_id resume,
  double-start guard, store persistence across instances.
* :class:`TestDetectPlatformHook` — detect_platform delegates to the
  existing resolver; Windows resolves to UNKNOWN (honest skeleton);
  explicit platform arg respected.
* :class:`TestDefaultProfileFor` — LINUX → "full", MACOS →
  "developer", UNKNOWN → "" (machine-readable no-default signal).
* :class:`TestMarkerStoreProtocol` — InMemoryMarkerStore satisfies
  the runtime_checkable MarkerStore Protocol.
* :class:`TestSerialization` — StageMarker.to_dict / BootstrapState
  .to_dict shapes are machine-readable.
"""
from __future__ import annotations

import unittest

from aee.installer.backend import (
    EXIT_EXECUTE_NOT_AUTHORIZED,
    EXIT_OK,
    EXIT_PRE_FLIGHT_FAILED,
    EXIT_PROFILE_INVALID,
    EXIT_PROFILE_SWITCH_REJECTED,
)
from aee.installer.lifecycle import (
    EXIT_DEPENDENCY_FLOOR_NOT_MET,
    EXIT_DRIFT_DETECTED,
    EXIT_NETWORK_ERROR,
    EXIT_SECRET_MISSING,
    EXIT_STAGE_FAILED_PERMANENT,
    EXIT_STAGE_FAILED_RETRYABLE,
    MAX_RETRY,
    PYTHON_STAGES,
    RETRY_BACKOFF_SECONDS,
    SHELL_STAGES,
    BootstrapLifecycle,
    BootstrapState,
    InMemoryMarkerStore,
    MarkerStore,
    StageMarker,
    StageName,
    StageState,
    default_profile_for,
    detect_platform,
)
from aee.platform.current import PlatformIdentity


# ---------------------------------------------------------------------------#
# Exit constants (§10.4)
# ---------------------------------------------------------------------------#


class TestExitConstants(unittest.TestCase):
    """Proposed bootstrap v1 exit codes do not collide with verified
    constants and occupy the documented free range."""

    _VERIFIED = {0, 2, 3, 4, 5, 6}

    def test_proposed_codes_in_documented_free_range(self) -> None:
        proposed = {
            EXIT_STAGE_FAILED_RETRYABLE,
            EXIT_STAGE_FAILED_PERMANENT,
            EXIT_DRIFT_DETECTED,
            EXIT_NETWORK_ERROR,
            EXIT_SECRET_MISSING,
            EXIT_DEPENDENCY_FLOOR_NOT_MET,
        }
        for code in proposed:
            self.assertGreaterEqual(code, 7, f"{code} below free range 7")
            self.assertLessEqual(code, 12, f"{code} above free range 12")

    def test_no_collision_with_verified_constants(self) -> None:
        proposed = {
            EXIT_STAGE_FAILED_RETRYABLE,
            EXIT_STAGE_FAILED_PERMANENT,
            EXIT_DRIFT_DETECTED,
            EXIT_NETWORK_ERROR,
            EXIT_SECRET_MISSING,
            EXIT_DEPENDENCY_FLOOR_NOT_MET,
        }
        self.assertEqual(
            proposed & self._VERIFIED,
            set(),
            "proposed bootstrap exit codes collide with verified constants",
        )

    def test_verified_constants_unchanged(self) -> None:
        # Defence in depth: pin the verified values the proposed codes
        # must avoid.
        self.assertEqual(EXIT_OK, 0)
        self.assertEqual(EXIT_PROFILE_INVALID, 3)
        self.assertEqual(EXIT_PRE_FLIGHT_FAILED, 4)
        self.assertEqual(EXIT_PROFILE_SWITCH_REJECTED, 5)
        self.assertEqual(EXIT_EXECUTE_NOT_AUTHORIZED, 6)

    def test_proposed_constants_have_distinct_values(self) -> None:
        proposed = [
            EXIT_STAGE_FAILED_RETRYABLE,
            EXIT_STAGE_FAILED_PERMANENT,
            EXIT_DRIFT_DETECTED,
            EXIT_NETWORK_ERROR,
            EXIT_SECRET_MISSING,
            EXIT_DEPENDENCY_FLOOR_NOT_MET,
        ]
        self.assertEqual(
            len(proposed), len(set(proposed)), "duplicate exit codes"
        )

    def test_documented_values(self) -> None:
        # Pin the exact §10.4 values so a future rename does not
        # silently renumber them.
        self.assertEqual(EXIT_STAGE_FAILED_RETRYABLE, 7)
        self.assertEqual(EXIT_STAGE_FAILED_PERMANENT, 8)
        self.assertEqual(EXIT_DRIFT_DETECTED, 9)
        self.assertEqual(EXIT_NETWORK_ERROR, 10)
        self.assertEqual(EXIT_SECRET_MISSING, 11)
        self.assertEqual(EXIT_DEPENDENCY_FLOOR_NOT_MET, 12)

    def test_max_retry_is_three(self) -> None:
        self.assertEqual(MAX_RETRY, 3)

    def test_retry_backoff_schedule(self) -> None:
        self.assertEqual(RETRY_BACKOFF_SECONDS, (2, 4, 8))


# ---------------------------------------------------------------------------#
# Stage vocabulary (§4)
# ---------------------------------------------------------------------------#


class TestStageVocabulary(unittest.TestCase):
    """StageName ordering + shell/python partition."""

    def test_stage_ordering(self) -> None:
        ordered = list(StageName)
        self.assertEqual(ordered[0], StageName.DETECT)
        self.assertEqual(ordered[-1], StageName.AGENT_READY)
        self.assertEqual(len(ordered), 8)

    def test_stage_values_are_canonical_marker_filenames(self) -> None:
        expected = [
            "00_detect",
            "01_deps",
            "02_clone",
            "03_pin",
            "04_runtime_setup",
            "05_health_check",
            "06_smoke_test",
            "07_agent_ready",
        ]
        self.assertEqual([s.value for s in StageName], expected)

    def test_shell_stages_are_detect_deps_clone(self) -> None:
        self.assertEqual(
            SHELL_STAGES,
            frozenset({StageName.DETECT, StageName.DEPS, StageName.CLONE}),
        )

    def test_python_stages_are_pin_through_agent_ready(self) -> None:
        self.assertEqual(
            PYTHON_STAGES,
            frozenset(
                {
                    StageName.PIN,
                    StageName.RUNTIME_SETUP,
                    StageName.HEALTH_CHECK,
                    StageName.SMOKE_TEST,
                    StageName.AGENT_READY,
                }
            ),
        )

    def test_shell_and_python_partition_is_exhaustive_disjoint(self) -> None:
        all_stages = set(StageName)
        self.assertEqual(SHELL_STAGES | PYTHON_STAGES, all_stages)
        self.assertEqual(SHELL_STAGES & PYTHON_STAGES, frozenset())

    def test_stage_state_values(self) -> None:
        self.assertEqual(StageState.PENDING.value, "pending")
        self.assertEqual(StageState.IN_PROGRESS.value, "in_progress")
        self.assertEqual(StageState.COMPLETED.value, "completed")
        self.assertEqual(StageState.FAILED.value, "failed")
        self.assertEqual(StageState.SKIPPED.value, "skipped")

    def test_stage_state_distinct(self) -> None:
        states = [StageState.PENDING, StageState.IN_PROGRESS,
                  StageState.COMPLETED, StageState.FAILED, StageState.SKIPPED]
        self.assertEqual(len(states), len(set(states)))


# ---------------------------------------------------------------------------#
# InMemoryMarkerStore
# ---------------------------------------------------------------------------#


class TestInMemoryMarkerStore(unittest.TestCase):
    """Default in-memory store: read/write/list idempotency + order."""

    def test_read_missing_run_returns_none(self) -> None:
        store = InMemoryMarkerStore()
        self.assertIsNone(store.read_state("nope"))

    def test_write_then_read_roundtrip(self) -> None:
        store = InMemoryMarkerStore()
        state = BootstrapState(
            run_id="r1",
            started_at="2026-07-25T00:00:00Z",
            last_updated_at="2026-07-25T00:00:00Z",
            markers={},
        )
        store.write_state(state)
        got = store.read_state("r1")
        self.assertIsNotNone(got)
        self.assertEqual(got.run_id, "r1")  # type: ignore[union-attr]

    def test_write_is_idempotent(self) -> None:
        store = InMemoryMarkerStore()
        state = BootstrapState(
            run_id="r1",
            started_at="t",
            last_updated_at="t",
            markers={},
        )
        store.write_state(state)
        store.write_state(state)
        self.assertEqual(store.list_runs(), ["r1"])

    def test_list_runs_insertion_order(self) -> None:
        store = InMemoryMarkerStore()
        for rid in ["a", "b", "c"]:
            store.write_state(
                BootstrapState(rid, "t", "t", {})
            )
        self.assertEqual(store.list_runs(), ["a", "b", "c"])

    def test_replacing_state_does_not_duplicate_run_id(self) -> None:
        store = InMemoryMarkerStore()
        s1 = BootstrapState("r1", "t1", "t1", {})
        store.write_state(s1)
        s2 = BootstrapState("r1", "t1", "t2", {})
        store.write_state(s2)
        self.assertEqual(store.list_runs(), ["r1"])
        self.assertEqual(store.read_state("r1").last_updated_at, "t2")  # type: ignore[union-attr]


# ---------------------------------------------------------------------------#
# BootstrapLifecycle transitions
# ---------------------------------------------------------------------------#


class TestBootstrapLifecycleTransitions(unittest.TestCase):
    """record_stage semantics for each state."""

    def _fresh(self) -> BootstrapLifecycle:
        return BootstrapLifecycle(InMemoryMarkerStore())

    def test_start_creates_state_with_no_markers(self) -> None:
        lc = self._fresh()
        state = lc.start("r1")
        self.assertEqual(state.run_id, "r1")
        self.assertEqual(state.markers, {})

    def test_start_generates_run_id_when_none(self) -> None:
        lc = self._fresh()
        state = lc.start()
        self.assertTrue(state.run_id)

    def test_record_stage_requires_start(self) -> None:
        lc = self._fresh()
        with self.assertRaises(RuntimeError):
            lc.record_stage(StageName.DETECT, StageState.COMPLETED)

    def test_completed_marker_has_completed_at(self) -> None:
        lc = self._fresh()
        lc.start("r1")
        lc.record_stage(StageName.DETECT, StageState.IN_PROGRESS)
        marker = lc.record_stage(StageName.DETECT, StageState.COMPLETED)
        self.assertEqual(marker.state, StageState.COMPLETED)
        self.assertIsNotNone(marker.started_at)
        self.assertIsNotNone(marker.completed_at)
        self.assertIsNone(marker.error_class)
        self.assertIsNone(marker.stderr_tail)
        self.assertEqual(marker.retry_count, 0)

    def test_in_progress_marker_has_started_at_no_completed_at(self) -> None:
        lc = self._fresh()
        lc.start("r1")
        marker = lc.record_stage(StageName.DETECT, StageState.IN_PROGRESS)
        self.assertEqual(marker.state, StageState.IN_PROGRESS)
        self.assertIsNotNone(marker.started_at)
        self.assertIsNone(marker.completed_at)
        self.assertEqual(marker.retry_count, 0)

    def test_failed_marker_records_error_class_and_stderr_tail(self) -> None:
        lc = self._fresh()
        lc.start("r1")
        lc.record_stage(StageName.DEPS, StageState.IN_PROGRESS)
        marker = lc.record_stage(
            StageName.DEPS,
            StageState.FAILED,
            error_class="apt.InstallError",
            stderr_tail="E: Unable to locate package foo",
        )
        self.assertEqual(marker.state, StageState.FAILED)
        self.assertEqual(marker.error_class, "apt.InstallError")
        self.assertEqual(marker.stderr_tail, "E: Unable to locate package foo")
        self.assertEqual(marker.retry_count, 1)
        self.assertIsNotNone(marker.completed_at)

    def test_failed_marker_retry_count_increments(self) -> None:
        lc = self._fresh()
        lc.start("r1")
        m1 = lc.record_stage(StageName.DEPS, StageState.FAILED,
                             error_class="X")
        m2 = lc.record_stage(StageName.DEPS, StageState.FAILED,
                             error_class="X")
        m3 = lc.record_stage(StageName.DEPS, StageState.FAILED,
                             error_class="X")
        self.assertEqual(m1.retry_count, 1)
        self.assertEqual(m2.retry_count, 2)
        self.assertEqual(m3.retry_count, 3)

    def test_explicit_retry_count_respected(self) -> None:
        lc = self._fresh()
        lc.start("r1")
        marker = lc.record_stage(
            StageName.DEPS, StageState.FAILED,
            error_class="X", retry_count=99,
        )
        self.assertEqual(marker.retry_count, 99)

    def test_completed_does_not_carry_error_class(self) -> None:
        lc = self._fresh()
        lc.start("r1")
        # First fail, then complete (operator fixed the issue).
        lc.record_stage(StageName.DEPS, StageState.FAILED, error_class="X")
        marker = lc.record_stage(StageName.DEPS, StageState.COMPLETED)
        self.assertEqual(marker.state, StageState.COMPLETED)
        self.assertIsNone(marker.error_class)
        self.assertIsNone(marker.stderr_tail)
        # retry_count persists across transitions (audit trail).
        self.assertEqual(marker.retry_count, 1)

    def test_skipped_marker_has_completed_at(self) -> None:
        lc = self._fresh()
        lc.start("r1")
        marker = lc.record_stage(StageName.SMOKE_TEST, StageState.SKIPPED)
        self.assertEqual(marker.state, StageState.SKIPPED)
        self.assertIsNotNone(marker.completed_at)
        self.assertEqual(marker.retry_count, 0)

    def test_pending_removes_marker(self) -> None:
        lc = self._fresh()
        lc.start("r1")
        lc.record_stage(StageName.DETECT, StageState.COMPLETED)
        # Recording PENDING removes the marker (defence in depth).
        lc.record_stage(StageName.DETECT, StageState.PENDING)
        self.assertIsNone(lc.get_marker(StageName.DETECT))

    def test_started_at_preserved_across_transitions(self) -> None:
        lc = self._fresh()
        lc.start("r1")
        m1 = lc.record_stage(StageName.DETECT, StageState.IN_PROGRESS)
        started = m1.started_at
        m2 = lc.record_stage(StageName.DETECT, StageState.FAILED,
                             error_class="X")
        m3 = lc.record_stage(StageName.DETECT, StageState.COMPLETED)
        self.assertEqual(m2.started_at, started)
        self.assertEqual(m3.started_at, started)


# ---------------------------------------------------------------------------#
# BootstrapLifecycle resume semantics (§5.5)
# ---------------------------------------------------------------------------#


class TestBootstrapLifecycleResume(unittest.TestCase):
    """get_resume_stage + is_complete per §5.5."""

    def test_resume_returns_first_stage_when_no_markers(self) -> None:
        lc = BootstrapLifecycle(InMemoryMarkerStore())
        lc.start("r1")
        self.assertEqual(lc.get_resume_stage(), StageName.DETECT)

    def test_resume_returns_first_pending_after_partial_completion(self) -> None:
        lc = BootstrapLifecycle(InMemoryMarkerStore())
        lc.start("r1")
        lc.record_stage(StageName.DETECT, StageState.COMPLETED)
        lc.record_stage(StageName.DEPS, StageState.COMPLETED)
        # CLONE has no marker → resume there.
        self.assertEqual(lc.get_resume_stage(), StageName.CLONE)

    def test_resume_returns_failed_stage(self) -> None:
        lc = BootstrapLifecycle(InMemoryMarkerStore())
        lc.start("r1")
        lc.record_stage(StageName.DETECT, StageState.COMPLETED)
        lc.record_stage(StageName.DEPS, StageState.FAILED, error_class="X")
        self.assertEqual(lc.get_resume_stage(), StageName.DEPS)

    def test_resume_returns_in_progress_stage(self) -> None:
        # An IN_PROGRESS stage may belong to a dead process → re-run.
        lc = BootstrapLifecycle(InMemoryMarkerStore())
        lc.start("r1")
        lc.record_stage(StageName.DETECT, StageState.COMPLETED)
        lc.record_stage(StageName.DEPS, StageState.IN_PROGRESS)
        self.assertEqual(lc.get_resume_stage(), StageName.DEPS)

    def test_resume_none_when_all_completed(self) -> None:
        lc = BootstrapLifecycle(InMemoryMarkerStore())
        lc.start("r1")
        for stage in StageName:
            lc.record_stage(stage, StageState.COMPLETED)
        self.assertIsNone(lc.get_resume_stage())
        self.assertTrue(lc.is_complete())

    def test_resume_none_when_all_completed_or_skipped(self) -> None:
        lc = BootstrapLifecycle(InMemoryMarkerStore())
        lc.start("r1")
        for stage in StageName:
            if stage is StageName.SMOKE_TEST:
                lc.record_stage(stage, StageState.SKIPPED)
            else:
                lc.record_stage(stage, StageState.COMPLETED)
        self.assertIsNone(lc.get_resume_stage())
        self.assertTrue(lc.is_complete())

    def test_not_complete_when_a_stage_failed(self) -> None:
        lc = BootstrapLifecycle(InMemoryMarkerStore())
        lc.start("r1")
        for stage in StageName:
            if stage is StageName.HEALTH_CHECK:
                lc.record_stage(stage, StageState.FAILED, error_class="X")
            else:
                lc.record_stage(stage, StageState.COMPLETED)
        self.assertFalse(lc.is_complete())
        self.assertEqual(lc.get_resume_stage(), StageName.HEALTH_CHECK)

    def test_not_complete_when_a_stage_pending(self) -> None:
        lc = BootstrapLifecycle(InMemoryMarkerStore())
        lc.start("r1")
        lc.record_stage(StageName.DETECT, StageState.COMPLETED)
        # DEPS has no marker → incomplete.
        self.assertFalse(lc.is_complete())


# ---------------------------------------------------------------------------#
# BootstrapLifecycle run-id + persistence
# ---------------------------------------------------------------------------#


class TestBootstrapLifecycleRunId(unittest.TestCase):
    """Explicit run_id, double-start guard, store persistence."""

    def test_explicit_run_id_resumes_existing_run(self) -> None:
        store = InMemoryMarkerStore()
        lc1 = BootstrapLifecycle(store)
        lc1.start("r1")
        lc1.record_stage(StageName.DETECT, StageState.COMPLETED)
        # New lifecycle instance, same store, same run_id → resume.
        lc2 = BootstrapLifecycle(store, run_id="r1")
        self.assertIsNotNone(lc2.state)
        self.assertEqual(lc2.state.run_id, "r1")  # type: ignore[union-attr]
        self.assertIsNotNone(lc2.get_marker(StageName.DETECT))
        # Continue recording.
        lc2.record_stage(StageName.DEPS, StageState.COMPLETED)
        self.assertEqual(lc2.get_resume_stage(), StageName.CLONE)

    def test_double_start_same_run_id_resumes_existing(self) -> None:
        # Calling start() twice with the same run_id on the same
        # lifecycle resumes the existing run (the store already holds
        # its state) — this is the resume path, not an error.
        lc = BootstrapLifecycle(InMemoryMarkerStore())
        s1 = lc.start("r1")
        s2 = lc.start("r1")
        self.assertEqual(s1.run_id, "r1")
        self.assertEqual(s2.run_id, "r1")
        # Same state object — resume is a no-op.
        self.assertEqual(s1, s2)

    def test_start_with_new_run_id_rebinds(self) -> None:
        lc = BootstrapLifecycle(InMemoryMarkerStore())
        lc.start("r1")
        lc.record_stage(StageName.DETECT, StageState.COMPLETED)
        # Rebind to a fresh run_id — fresh state.
        state = lc.start("r2")
        self.assertEqual(state.run_id, "r2")
        self.assertEqual(state.markers, {})
        self.assertIsNone(lc.get_marker(StageName.DETECT))

    def test_constructor_run_id_loads_existing_state(self) -> None:
        store = InMemoryMarkerStore()
        lc1 = BootstrapLifecycle(store)
        lc1.start("r1")
        lc1.record_stage(StageName.DETECT, StageState.COMPLETED)
        # Constructor with run_id loads existing state eagerly.
        lc2 = BootstrapLifecycle(store, run_id="r1")
        self.assertIsNotNone(lc2.state)
        self.assertEqual(lc2.state.run_id, "r1")  # type: ignore[union-attr]
        self.assertIn(StageName.DETECT, lc2.state.markers)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------#
# Detection framework hooks (§2.3 + §2.4)
# ---------------------------------------------------------------------------#


class TestDetectPlatformHook(unittest.TestCase):
    """detect_platform delegates to the existing resolver; Windows
    resolves to UNKNOWN (honest skeleton — no Windows adapter yet)."""

    def test_linux(self) -> None:
        self.assertEqual(detect_platform("linux"), PlatformIdentity.LINUX)
        self.assertEqual(detect_platform("linux2"), PlatformIdentity.LINUX)

    def test_macos(self) -> None:
        self.assertEqual(detect_platform("darwin"), PlatformIdentity.MACOS)

    def test_windows_resolves_to_unknown(self) -> None:
        # W1 narrow scope: no Windows adapter; the hook does NOT
        # pretend Windows is supported.
        self.assertEqual(detect_platform("win32"), PlatformIdentity.UNKNOWN)
        self.assertEqual(detect_platform("cygwin"), PlatformIdentity.UNKNOWN)

    def test_unknown_platforms_resolve_to_unknown(self) -> None:
        self.assertEqual(detect_platform("haiku"), PlatformIdentity.UNKNOWN)
        self.assertEqual(detect_platform("freebsd"), PlatformIdentity.UNKNOWN)


class TestDefaultProfileFor(unittest.TestCase):
    """default_profile_for matches existing capability defaults."""

    def test_linux_defaults_to_full(self) -> None:
        self.assertEqual(default_profile_for(PlatformIdentity.LINUX), "full")

    def test_macos_defaults_to_developer(self) -> None:
        self.assertEqual(default_profile_for(PlatformIdentity.MACOS), "developer")

    def test_unknown_defaults_to_empty_string(self) -> None:
        # Empty string is the explicit machine-readable signal that
        # no default exists; the caller must refuse work rather than
        # guessing.
        self.assertEqual(default_profile_for(PlatformIdentity.UNKNOWN), "")


# ---------------------------------------------------------------------------#
# Protocol conformance
# ---------------------------------------------------------------------------#


class TestMarkerStoreProtocol(unittest.TestCase):
    """InMemoryMarkerStore satisfies the runtime_checkable Protocol."""

    def test_in_memory_store_is_a_marker_store(self) -> None:
        store = InMemoryMarkerStore()
        self.assertIsInstance(store, MarkerStore)


# ---------------------------------------------------------------------------#
# Serialization
# ---------------------------------------------------------------------------#


class TestSerialization(unittest.TestCase):
    """StageMarker.to_dict + BootstrapState.to_dict are machine-readable."""

    def test_stage_marker_to_dict(self) -> None:
        marker = StageMarker(
            stage=StageName.DEPS,
            run_id="r1",
            state=StageState.FAILED,
            started_at="2026-07-25T00:00:00Z",
            completed_at="2026-07-25T00:01:00Z",
            error_class="apt.InstallError",
            stderr_tail="E: broken",
            retry_count=2,
        )
        d = marker.to_dict()
        self.assertEqual(d["stage"], "01_deps")
        self.assertEqual(d["run_id"], "r1")
        self.assertEqual(d["state"], "failed")
        self.assertEqual(d["error_class"], "apt.InstallError")
        self.assertEqual(d["retry_count"], 2)

    def test_bootstrap_state_to_dict(self) -> None:
        lc = BootstrapLifecycle(InMemoryMarkerStore())
        lc.start("r1")
        lc.record_stage(StageName.DETECT, StageState.COMPLETED)
        state = lc.state
        self.assertIsNotNone(state)
        d = state.to_dict()  # type: ignore[union-attr]
        self.assertEqual(d["run_id"], "r1")
        self.assertIn("00_detect", d["markers"])
        self.assertEqual(d["markers"]["00_detect"]["state"], "completed")

    def test_marker_stage_field_is_canonical_filename(self) -> None:
        marker = StageMarker(
            stage=StageName.AGENT_READY,
            run_id="r",
            state=StageState.COMPLETED,
        )
        self.assertEqual(marker.to_dict()["stage"], "07_agent_ready")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()