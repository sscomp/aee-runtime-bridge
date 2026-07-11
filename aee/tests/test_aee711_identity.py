"""AEE Audit Namespace Hardening — tripwire + unit tests.

Covers the read-side identity model in
``aee/reporting/identity.py`` and the index builder in
``aee/reporting/build_index.py``.

Test inventory (all stdlib ``unittest`` — no pytest needed):

* :class:`TestRecordKindEnum` — enum contract, string value.
* :class:`TestClassifyFixtureSignals` — fixture detection.
* :class:`TestClassifyRuntime` — runtime detection.
* :class:`TestClassifyUnknown` — unknown fallthrough.
* :class:`TestClassifySentinelOverride` — policy extension.
* :class:`TestSidecarRoundTrip` — atomic write + idempotent read.
* :class:`TestSidecarBackwardCompat` — older sidecars still read.
* :class:`TestPathTraversalNotExecuted` — path-traversal input
  is recognised, not executed.
* :class:`TestIterReports` — directory iterator.
* :class:`TestBuildIndex` — index builder CLI + summary shape.
* :class:`TestBuildIndexAliasMapping` — alias → canonical.
* :class:`TestSentinelStuckPctOnlyFalsePositive` — regression
  test: stuck-pct alone is NOT a fixture signal.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aee.reporting import (
    Identity,
    RecordKind,
    SentinelPolicy,
    classify_and_persist,
    classify_record,
    iter_reports,
    load_task_json,
    read_identity_sidecar,
    write_identity_sidecar,
)
from aee.reporting.build_index import build_index


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_task_json(
    *,
    task_id: str = "TASK-20260711-0001",
    title: str = "ops: legitimate task",
    hermes_run_id: str = "run_0123456789abcdef0123456789abcdef",
    input_text: str = "do the thing",
    progress_pct: int = 0,
    status: str = "queued",
) -> Dict[str, Any]:
    return {
        "task_id": task_id,
        "title": title,
        "hermes_run_id": hermes_run_id,
        "input_text": input_text,
        "progress_pct": progress_pct,
        "status": status,
    }


# ---------------------------------------------------------------------------
# Enum / dataclass
# ---------------------------------------------------------------------------


class TestRecordKindEnum(unittest.TestCase):
    def test_string_values_persisted(self) -> None:
        # The string values land in identity.json — adding a
        # new value is a schema change.
        self.assertEqual(RecordKind.RUNTIME.value, "runtime")
        self.assertEqual(RecordKind.FIXTURE.value, "fixture")
        self.assertEqual(RecordKind.UNKNOWN.value, "unknown")

    def test_string_enum_isinstance(self) -> None:
        # The Enum subclasses str so it JSON-serialises as its
        # value, not as the Enum repr.
        import json
        self.assertEqual(json.dumps(RecordKind.RUNTIME), '"runtime"')

    def test_identity_round_trip(self) -> None:
        ident = Identity(
            record_kind=RecordKind.FIXTURE,
            task_id="TASK-X",
            is_fixture=True,
            fixture_markers=["m1", "m2"],
            executor_session_id="sess-1",
            runtime_run_id="r1",
            user_provided_alias="alias-1",
        )
        d = ident.to_dict()
        self.assertEqual(d["record_kind"], "fixture")
        ident2 = Identity.from_dict(d)
        self.assertEqual(ident, ident2)


# ---------------------------------------------------------------------------
# Fixture detection
# ---------------------------------------------------------------------------


class TestClassifyFixtureSignals(unittest.TestCase):
    def test_sentinel_hermes_run_id(self) -> None:
        ident = classify_record(
            "TASK-20260711-0099",
            _make_task_json(hermes_run_id="run-traversal"),
        )
        self.assertEqual(ident.record_kind, RecordKind.FIXTURE)
        self.assertTrue(ident.is_fixture)
        self.assertTrue(
            any("run-traversal" in m for m in ident.fixture_markers)
        )

    def test_run_success_sentinel(self) -> None:
        # aee6-success pair (the audit fixture set)
        ident = classify_record(
            "TASK-20260711-0015",
            _make_task_json(
                hermes_run_id="run-success", title="aee6-success"
            ),
        )
        self.assertEqual(ident.record_kind, RecordKind.FIXTURE)

    def test_fixture_title(self) -> None:
        ident = classify_record(
            "TASK-20260711-0001",
            _make_task_json(title="aee6-traversal"),
        )
        self.assertEqual(ident.record_kind, RecordKind.FIXTURE)
        self.assertTrue(
            any("aee6-traversal" in m for m in ident.fixture_markers)
        )

    def test_path_traversal_input(self) -> None:
        ident = classify_record(
            "TASK-20260711-0001",
            _make_task_json(
                input_text="read /tmp/../etc/whatever now"
            ),
        )
        self.assertEqual(ident.record_kind, RecordKind.FIXTURE)
        self.assertIn("path_traversal_input", ident.fixture_markers)

    def test_path_traversal_windows_separator(self) -> None:
        ident = classify_record(
            "TASK-20260711-0001",
            _make_task_json(input_text="read C:\\..\\..\\file"),
        )
        self.assertEqual(ident.record_kind, RecordKind.FIXTURE)

    def test_stuck_running_with_strong_signal(self) -> None:
        # stuck + sentinel run_id = fixture (the original
        # TASK-20260711-0018 case)
        ident = classify_record(
            "TASK-20260711-0018",
            _make_task_json(
                title="aee6-traversal",
                hermes_run_id="run-traversal",
                input_text="read /tmp/../etc/whatever now",
                progress_pct=5,
                status="running",
            ),
        )
        self.assertEqual(ident.record_kind, RecordKind.FIXTURE)
        self.assertGreaterEqual(len(ident.fixture_markers), 2)

    def test_test_run_id_pattern(self) -> None:
        # test-* run_id is always fixture — even when other
        # fields look real, the pattern wins. Use a test-
        # prefixed id NOT in the explicit sentinel set to
        # exercise the regex path.
        ident = classify_record(
            "TASK-X",
            _make_task_json(
                hermes_run_id="test-other-9999",
                title="ops: legit title",
                input_text="do the thing",
            ),
        )
        self.assertEqual(ident.record_kind, RecordKind.FIXTURE)
        self.assertTrue(
            any("fixture_run_id_pattern" in m
                for m in ident.fixture_markers)
        )

    def test_orch_run_id_pattern(self) -> None:
        ident = classify_record(
            "TASK-X",
            _make_task_json(hermes_run_id="orch-z"),
        )
        self.assertEqual(ident.record_kind, RecordKind.FIXTURE)


# ---------------------------------------------------------------------------
# Runtime detection
# ---------------------------------------------------------------------------


class TestClassifyRuntime(unittest.TestCase):
    def test_real_run_id_format(self) -> None:
        ident = classify_record(
            "TASK-20260711-0001",
            _make_task_json(
                hermes_run_id="run_0123456789abcdef0123456789abcdef"
            ),
        )
        self.assertEqual(ident.record_kind, RecordKind.RUNTIME)
        self.assertFalse(ident.is_fixture)
        self.assertEqual(ident.fixture_markers, [])

    def test_real_run_id_with_progress(self) -> None:
        ident = classify_record(
            "TASK-20260711-0001",
            _make_task_json(
                hermes_run_id="run_0123456789abcdef0123456789abcdef",
                progress_pct=42,
            ),
        )
        self.assertEqual(ident.record_kind, RecordKind.RUNTIME)

    def test_executor_anchors_propagated(self) -> None:
        ident = classify_record(
            "TASK-20260711-0001",
            _make_task_json(
                hermes_run_id="run_0123456789abcdef0123456789abcdef"
            ),
            executor_session_id="sess-X",
            runtime_run_id="r1-r2",
            user_provided_alias="audit-alias-1",
        )
        self.assertEqual(ident.executor_session_id, "sess-X")
        self.assertEqual(ident.runtime_run_id, "r1-r2")
        self.assertEqual(ident.user_provided_alias, "audit-alias-1")


# ---------------------------------------------------------------------------
# Unknown fallthrough
# ---------------------------------------------------------------------------


class TestClassifyUnknown(unittest.TestCase):
    def test_null_hermes_run_id(self) -> None:
        ident = classify_record(
            "TASK-X",
            _make_task_json(hermes_run_id=""),
        )
        self.assertEqual(ident.record_kind, RecordKind.UNKNOWN)

    def test_short_hex_run_id(self) -> None:
        # run_xxx with non-32 hex is unknown, not runtime
        ident = classify_record(
            "TASK-X",
            _make_task_json(hermes_run_id="run_short"),
        )
        self.assertEqual(ident.record_kind, RecordKind.UNKNOWN)

    def test_stuck_pct_alone_is_not_fixture(self) -> None:
        # Regression: lone "stuck at 5%" with a real-looking
        # run_id is NOT a fixture. (Canonical TASK-20260711-0015
        # has progress_pct=5 and was the real executor per
        # the prior audit, until the on-disk task.json
        # revealed it too is a fixture — but the lone-stuck
        # rule should NOT be the reason.)
        ident = classify_record(
            "TASK-X",
            _make_task_json(
                hermes_run_id="run_0123456789abcdef0123456789abcdef",
                progress_pct=5,
                status="running",
            ),
        )
        self.assertEqual(ident.record_kind, RecordKind.RUNTIME)
        self.assertFalse(ident.is_fixture)


# ---------------------------------------------------------------------------
# Sentinel policy override
# ---------------------------------------------------------------------------


class TestClassifySentinelOverride(unittest.TestCase):
    def test_extend_sentinel_set(self) -> None:
        # Add a custom sentinel — the new value should now
        # be flagged, but the existing ones still are.
        custom = SentinelPolicy(
            hermes_run_id_sentinels=frozenset({"custom-sentinel"}),
        )
        ident_new = classify_record(
            "TASK-X",
            _make_task_json(hermes_run_id="custom-sentinel"),
            policy=custom,
        )
        self.assertEqual(ident_new.record_kind, RecordKind.FIXTURE)
        ident_old = classify_record(
            "TASK-X",
            _make_task_json(hermes_run_id="run-traversal"),
            policy=custom,
        )
        # run-traversal NOT in custom set; if no other
        # signal fires, becomes UNKNOWN.
        self.assertEqual(ident_old.record_kind, RecordKind.UNKNOWN)

    def test_disable_path_traversal_via_input(self) -> None:
        # Path-traversal signal is hard-coded (not policy-
        # configurable) so it cannot be disabled. This test
        # is the tripwire.
        ident = classify_record(
            "TASK-X",
            _make_task_json(input_text="read /tmp/../etc/whatever now"),
            policy=SentinelPolicy(flag_stuck_running=False),
        )
        self.assertIn("path_traversal_input", ident.fixture_markers)

    def test_disable_stuck_running(self) -> None:
        # Stuck-running signal is policy-controllable. With
        # it off, a stuck-alone record is NOT fixture.
        custom = SentinelPolicy(flag_stuck_running=False)
        ident = classify_record(
            "TASK-X",
            _make_task_json(
                title="other title",
                input_text="do the thing",
                progress_pct=5,
                status="running",
            ),
            policy=custom,
        )
        # No other fixture signal → not fixture.
        self.assertEqual(ident.record_kind, RecordKind.RUNTIME)


# ---------------------------------------------------------------------------
# Sidecar round-trip
# ---------------------------------------------------------------------------


class TestSidecarRoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="aee-iden-"))
        self.task_json = self.tmp / "task.json"
        self.task_json.write_text(
            json.dumps(_make_task_json()), encoding="utf-8"
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_then_read(self) -> None:
        ident = classify_record("TASK-X", _make_task_json())
        sidecar = write_identity_sidecar(self.task_json, ident)
        self.assertTrue(sidecar.exists())
        roundtripped = read_identity_sidecar(self.task_json)
        self.assertIsNotNone(roundtripped)
        self.assertEqual(roundtripped, ident)

    def test_idempotent_write(self) -> None:
        ident = classify_record("TASK-X", _make_task_json())
        s1 = write_identity_sidecar(self.task_json, ident)
        mtime1 = s1.stat().st_mtime_ns
        # Re-write the same content — file should NOT be
        # re-touched (no overwrite).
        s2 = write_identity_sidecar(self.task_json, ident)
        mtime2 = s2.stat().st_mtime_ns
        self.assertEqual(s1, s2)
        self.assertEqual(mtime1, mtime2)

    def test_force_overwrite(self) -> None:
        ident1 = classify_record("TASK-X", _make_task_json())
        write_identity_sidecar(self.task_json, ident1)
        ident2 = Identity(
            record_kind=RecordKind.RUNTIME,
            task_id="TASK-X",
            is_fixture=False,
        )
        s = write_identity_sidecar(
            self.task_json, ident2, force=True
        )
        roundtripped = read_identity_sidecar(self.task_json)
        self.assertEqual(roundtripped.record_kind, RecordKind.RUNTIME)


class TestSidecarBackwardCompat(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="aee-iden-bc-"))
        self.task_json = self.tmp / "task.json"
        self.task_json.write_text(
            json.dumps(_make_task_json()), encoding="utf-8"
        )
        self.sidecar = self.task_json.parent / "identity.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_legacy_sidecar(self) -> None:
        legacy = {
            "record_kind": "fixture",
            "task_id": "TASK-X",
            "is_fixture": True,
            "fixture_markers": ["m1"],
        }
        self.sidecar.write_text(
            json.dumps(legacy), encoding="utf-8"
        )
        result = read_identity_sidecar(self.task_json)
        self.assertIsNotNone(result)
        assert result is not None  # for pyright
        self.assertEqual(result.record_kind, RecordKind.FIXTURE)
        self.assertEqual(result.fixture_markers, ["m1"])
        self.assertIsNone(result.executor_session_id)
        self.assertIsNone(result.runtime_run_id)

    def test_malformed_sidecar_returns_none(self) -> None:
        self.sidecar.write_text("{not valid json", encoding="utf-8")
        result = read_identity_sidecar(self.task_json)
        self.assertIsNone(result)
        assert result is None  # for pyright

    def test_missing_sidecar_returns_none(self) -> None:
        self.assertIsNone(read_identity_sidecar(self.task_json))


# ---------------------------------------------------------------------------
# Path-traversal input is not executed
# ---------------------------------------------------------------------------


class TestPathTraversalNotExecuted(unittest.TestCase):
    """The validator never executes file reads. It only
    pattern-matches the input_text. This is the security
    boundary test."""

    def test_no_file_read_on_path_traversal_input(self) -> None:
        # Call classify_record with a path-traversal input
        # targeting a non-existent file. The validator must
        # not raise OSError / FileNotFoundError.
        ident = classify_record(
            "TASK-X",
            _make_task_json(
                input_text="read /tmp/../nonexistent-file-12345"
            ),
        )
        self.assertEqual(ident.record_kind, RecordKind.FIXTURE)

    def test_path_traversal_targeted_at_real_file(self) -> None:
        # If the input points to a real file via path
        # traversal, the validator must STILL just
        # pattern-match, not read the file. We verify
        # (a) the pattern-match fires and (b) the
        # validator returns without any side effect.
        import os
        real_path = "/etc/passwd"
        if not os.path.exists(real_path):
            self.skipTest("requires Linux")
        # The input must contain `..` to actually be a
        # path-traversal pattern (a bare /etc/passwd read
        # is not a traversal signal — it's a plain file
        # read, which the validator does not flag).
        ident = classify_record(
            "TASK-X",
            _make_task_json(
                input_text=f"read /tmp/../..{real_path} now"
            ),
        )
        self.assertEqual(ident.record_kind, RecordKind.FIXTURE)
        # The validator must NOT leak the file's content
        # anywhere in the verdict. No fixture_markers
        # entry should contain /etc/passwd content.
        for marker in ident.fixture_markers:
            self.assertNotIn("root:", marker)


# ---------------------------------------------------------------------------
# iter_reports
# ---------------------------------------------------------------------------


class TestIterReports(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="aee-iter-"))
        (self.tmp / "TASK-A").mkdir()
        (self.tmp / "TASK-A" / "task.json").write_text(
            json.dumps(_make_task_json(task_id="TASK-A")),
            encoding="utf-8",
        )
        (self.tmp / "TASK-B").mkdir()
        (self.tmp / "TASK-B" / "task.json").write_text(
            json.dumps(_make_task_json(task_id="TASK-B")),
            encoding="utf-8",
        )
        (self.tmp / "TASK-EMPTY").mkdir()  # no task.json
        (self.tmp / "loose.txt").write_text("nope")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sorted_output(self) -> None:
        ids = [tid for tid, _ in iter_reports(self.tmp)]
        self.assertEqual(ids, ["TASK-A", "TASK-B"])

    def test_skips_empty_dir(self) -> None:
        ids = [tid for tid, _ in iter_reports(self.tmp)]
        self.assertNotIn("TASK-EMPTY", ids)

    def test_skips_loose_files(self) -> None:
        ids = [tid for tid, _ in iter_reports(self.tmp)]
        self.assertNotIn("loose.txt", ids)

    def test_missing_root_returns_empty(self) -> None:
        ids = [tid for tid, _ in iter_reports(self.tmp / "nope")]
        self.assertEqual(ids, [])


# ---------------------------------------------------------------------------
# build_index
# ---------------------------------------------------------------------------


class TestBuildIndex(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_root = Path(tempfile.mkdtemp(prefix="aee-bi-"))
        self.reports = self.tmp_root / "reports"
        self.audit = self.tmp_root / "audit"
        self.reports.mkdir()
        # Build a small corpus with known mix.
        for i, kind in enumerate(
            ["runtime", "fixture", "fixture", "runtime"]
        ):
            tid = f"TASK-20260711-{i:04d}"
            (self.reports / tid).mkdir()
            if kind == "runtime":
                tj = _make_task_json(
                    task_id=tid,
                    hermes_run_id=f"run_{i:032x}",
                )
            else:
                tj = _make_task_json(
                    task_id=tid,
                    title="aee6-traversal",
                    hermes_run_id="run-traversal",
                    input_text="read /tmp/../etc/whatever now",
                )
            (self.reports / tid / "task.json").write_text(
                json.dumps(tj), encoding="utf-8"
            )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_root, ignore_errors=True)

    def test_build_index_counts(self) -> None:
        result = build_index(
            reports_root=self.reports,
            audit_dir=self.audit,
            final_head_sha="abc",
        )
        self.assertEqual(result["summary"]["counts"]["runtime"], 2)
        self.assertEqual(result["summary"]["counts"]["fixture"], 2)
        self.assertEqual(result["summary"]["total_reports"], 4)

    def test_build_index_writes_sidecars_for_fixture(self) -> None:
        build_index(
            reports_root=self.reports, audit_dir=self.audit,
        )
        # Fixture records get sidecars.
        for i in (1, 2):
            self.assertTrue(
                (self.reports / f"TASK-20260711-{i:04d}" /
                 "identity.json").exists(),
                f"fixture TASK-20260711-{i:04d} missing sidecar",
            )
        # Runtime records do NOT get sidecars by default.
        for i in (0, 3):
            self.assertFalse(
                (self.reports / f"TASK-20260711-{i:04d}" /
                 "identity.json").exists(),
            )

    def test_build_index_sidecar_for_runtime_opt_in(self) -> None:
        build_index(
            reports_root=self.reports,
            audit_dir=self.audit,
            sidecar_for_runtime=True,
        )
        for i in (0, 3):
            self.assertTrue(
                (self.reports / f"TASK-20260711-{i:04d}" /
                 "identity.json").exists(),
            )

    def test_build_index_alias_resolution(self) -> None:
        # Create one more report (TASK-20260711-9999) and
        # alias it to TASK-20260711-0000. The new record is
        # RUNTIME (no fixture signal) so we need
        # ``sidecar_for_runtime=True`` to write the sidecar
        # the test reads back.
        tid = "TASK-20260711-9999"
        (self.reports / tid).mkdir()
        (self.reports / tid / "task.json").write_text(
            json.dumps(
                _make_task_json(
                    task_id=tid,
                    hermes_run_id=(
                        "run_99999999999999999999999999999999"
                    ),
                )
            ),
            encoding="utf-8",
        )
        result = build_index(
            reports_root=self.reports,
            audit_dir=self.audit,
            aliases={"TASK-AUDIT-001": tid},
            executor_session_id="sess-real",
            runtime_run_id="r1",
            sidecar_for_runtime=True,
        )
        # The alias must be stamped on the canonical
        # record's identity sidecar.
        sidecar = json.loads(
            (self.reports / tid / "identity.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            sidecar["user_provided_alias"], "TASK-AUDIT-001"
        )
        self.assertEqual(
            sidecar["executor_session_id"], "sess-real"
        )
        self.assertEqual(sidecar["runtime_run_id"], "r1")
        # The summary's aliases map must carry the binding.
        self.assertEqual(
            result["summary"]["aliases"],
            {"TASK-AUDIT-001": tid},
        )


# ---------------------------------------------------------------------------
# Canonical identity validator integration (the smoke that
# proves the validator can be used as a consumer guard)
# ---------------------------------------------------------------------------


class TestRuntimeVsFixtureGuard(unittest.TestCase):
    """A consumer of `reports/` MUST be able to ask the
    validator: 'is this a real executor record?' and get a
    single boolean. This is the use-case the package exists
    to serve."""

    def test_guard_rejects_fixture(self) -> None:
        fixture = _make_task_json(
            task_id="TASK-20260711-0018",
            title="aee6-traversal",
            hermes_run_id="run-traversal",
            input_text="read /tmp/../etc/whatever now",
            progress_pct=5,
            status="running",
        )
        ident = classify_record("TASK-20260711-0018", fixture)
        # The guard: a real executor is record_kind == RUNTIME
        self.assertFalse(ident.record_kind == RecordKind.RUNTIME)
        self.assertTrue(ident.is_fixture)
        # The first marker is the sentinel run_id (the
        # strongest signal).
        self.assertIn(
            "sentinel_hermes_run_id:'run-traversal'",
            ident.fixture_markers,
        )

    def test_guard_accepts_runtime(self) -> None:
        real = _make_task_json(
            task_id="TASK-20260711-9999",
            hermes_run_id="run_0123456789abcdef0123456789abcdef",
        )
        ident = classify_record("TASK-20260711-9999", real)
        self.assertEqual(ident.record_kind, RecordKind.RUNTIME)
        self.assertFalse(ident.is_fixture)


# ---------------------------------------------------------------------------
# Suite entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    unittest.main()
