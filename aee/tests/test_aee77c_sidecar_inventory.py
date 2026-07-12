"""AEE-7.7c — sidecar inventory + dry-run migration planner tests.

This test module covers the read-only inventory and the pure
``plan_sidecar_migration`` function. The module under test
MUST NOT touch the dispatcher, the live DB, or any file
outside its own ``tempfile.mkdtemp`` sandbox. These tests
also assert the no-dispatcher-import contract that AEE-7.7a
established.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from aee.audit.sidecar_inventory import (  # noqa: E402
    INVENTORY_SCHEMA_VERSION,
    MigrationPlan,
    SidecarInventoryEntry,
    SidecarInventoryResult,
    SidecarStatus,
    build_sidecar_inventory,
    plan_sidecar_migration,
)


def _make_task_dir(
    root: Path,
    task_id: str,
    *,
    task_json: Optional[Dict[str, Any]] = None,
    sidecar: Optional[Dict[str, Any]] = None,
    sidecar_garbled: bool = False,
    task_json_garbled: bool = False,
) -> Path:
    """Helper: create a TASK-XXXXXX/task.json + optional identity.json.

    ``sidecar_garbled`` writes a non-JSON file in place of the
    sidecar (so the inventory has to handle a parse error).
    ``task_json_garbled`` does the same for task.json.
    """
    d = root / f"TASK-{task_id}"
    d.mkdir(parents=True, exist_ok=True)
    if task_json is None:
        # Minimal task.json: just enough to satisfy _file_sha256.
        task_json = {
            "task_id": f"TASK-{task_id}",
            "title": f"fixture-{task_id}",
            "type": "research",
            "status": "completed",
            "runtime_type": "hermes",
            "adapter_name": "hermes",
            "external_run_id": f"run-{task_id}",
            "session_id": f"sess-{task_id}",
        }
    if task_json_garbled:
        (d / "task.json").write_bytes(b"\x00\x01not-json")
    else:
        (d / "task.json").write_text(
            json.dumps(task_json, sort_keys=True), encoding="utf-8"
        )
    if sidecar is not None:
        (d / "identity.json").write_text(
            json.dumps(sidecar, sort_keys=True), encoding="utf-8"
        )
    elif sidecar_garbled:
        (d / "identity.json").write_bytes(b"not-json-at-all")
    return d


def _fresh_sidecar(task_id: str, *, source_hash: str) -> Dict[str, Any]:
    """A sidecar that matches the current writer (1.0.0 / 1.0.0)
    and the supplied source task.json hash.
    """
    return {
        "task_id": f"TASK-{task_id}",
        "classified_at_utc": "2026-07-12T00:00:00Z",
        "executor_session_id": None,
        "fixture_markers": [],
        "is_fixture": True,
        "policy_version": "1.0.0",
        "schema_version": "1.0.0",
        "record_kind": "fixture",
        "runtime_run_id": None,
        "source_task_json_sha256": source_hash,
        "user_provided_alias": None,
    }


def _stale_version_sidecar(task_id: str) -> Dict[str, Any]:
    return {
        "task_id": f"TASK-{task_id}",
        "classified_at_utc": "2026-07-01T00:00:00Z",
        "policy_version": "0.9.0",  # older
        "schema_version": "1.0.0",
        "record_kind": "fixture",
        "is_fixture": True,
        "source_task_json_sha256": "deadbeef" * 8,
    }


class TestSidecarStatusEnum(unittest.TestCase):
    def test_four_states_present(self):
        # The brief promised 4 buckets; verify the set is
        # exactly the documented four.
        self.assertEqual(
            {s.value for s in SidecarStatus},
            {"fresh", "stale_hash", "stale_version", "missing"},
        )


class TestInventoryBasic(unittest.TestCase):
    """Empty / single-task smoke tests."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aee77c-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_reports_root_returns_empty_result(self):
        inv = build_sidecar_inventory(self.tmp, utc_stamp="2026-07-12T00:00:00Z")
        self.assertIsInstance(inv, SidecarInventoryResult)
        self.assertEqual(inv.entries, [])
        # by_status is always populated with the 4-bucket
        # zero-initialized dict so a downstream consumer never
        # has to do ``inv.by_status.get("missing", 0)``.
        self.assertEqual(
            inv.by_status,
            {"fresh": 0, "stale_hash": 0, "stale_version": 0, "missing": 0},
        )
        self.assertEqual(inv.unreadable_task_json, 0)
        self.assertEqual(inv.unreadable_sidecars, 0)
        self.assertEqual(inv.inventoried_at_utc, "2026-07-12T00:00:00Z")
        self.assertEqual(inv.schema_version, INVENTORY_SCHEMA_VERSION)

    def test_nonexistent_root_returns_empty_result_not_exception(self):
        bogus = self.tmp / "does-not-exist"
        inv = build_sidecar_inventory(bogus, utc_stamp="2026-07-12T00:00:00Z")
        self.assertEqual(inv.entries, [])

    def test_non_directory_root_returns_empty_result(self):
        f = self.tmp / "a-file.txt"
        f.write_text("hi")
        inv = build_sidecar_inventory(f, utc_stamp="2026-07-12T00:00:00Z")
        self.assertEqual(inv.entries, [])

    def test_single_task_no_sidecar_is_missing(self):
        _make_task_dir(self.tmp, "0001")
        inv = build_sidecar_inventory(self.tmp, utc_stamp="2026-07-12T00:00:00Z")
        self.assertEqual(len(inv.entries), 1)
        e = inv.entries[0]
        self.assertEqual(e.task_id, "0001")
        self.assertEqual(e.status, SidecarStatus.MISSING)
        self.assertTrue(e.has_task_json)
        self.assertFalse(e.has_sidecar)
        self.assertEqual(inv.by_status["missing"], 1)


class TestInventoryClassification(unittest.TestCase):
    """The 4 status buckets + edge cases."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aee77c-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _hash_of(self, task_dir: Path) -> str:
        """Recompute the on-disk task.json hash the way the
        inventory does (sha256 of the file).
        """
        import hashlib
        h = hashlib.sha256()
        with open(task_dir / "task.json", "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def test_fresh_sidecar_classified_fresh(self):
        d = _make_task_dir(self.tmp, "0001")
        sidecar = _fresh_sidecar("0001", source_hash=self._hash_of(d))
        d.joinpath("identity.json").write_text(
            json.dumps(sidecar, sort_keys=True), encoding="utf-8"
        )
        inv = build_sidecar_inventory(self.tmp, utc_stamp="t1")
        self.assertEqual(inv.entries[0].status, SidecarStatus.FRESH)
        self.assertEqual(inv.by_status["fresh"], 1)

    def test_stale_hash_when_task_json_rewritten_after_sidecar(self):
        d = _make_task_dir(self.tmp, "0001")
        # Sidecar cites a hash that does NOT match the on-disk
        # task.json → STALE_HASH.
        sidecar = _fresh_sidecar("0001", source_hash="a" * 64)
        d.joinpath("identity.json").write_text(
            json.dumps(sidecar, sort_keys=True), encoding="utf-8"
        )
        inv = build_sidecar_inventory(self.tmp, utc_stamp="t1")
        self.assertEqual(inv.entries[0].status, SidecarStatus.STALE_HASH)
        self.assertEqual(inv.by_status["stale_hash"], 1)

    def test_stale_version_when_policy_version_older(self):
        d = _make_task_dir(self.tmp, "0001")
        # The sidecar's policy_version is older AND the
        # source hash matches (no STALE_HASH condition) — so
        # the inventory must bucket it as STALE_VERSION.
        sidecar = _stale_version_sidecar("0001")
        sidecar["source_task_json_sha256"] = self._hash_of(d)
        d.joinpath("identity.json").write_text(
            json.dumps(sidecar, sort_keys=True), encoding="utf-8"
        )
        inv = build_sidecar_inventory(self.tmp, utc_stamp="t1")
        self.assertEqual(inv.entries[0].status, SidecarStatus.STALE_VERSION)
        self.assertEqual(inv.entries[0].sidecar_policy_version, "0.9.0")
        self.assertEqual(inv.by_status["stale_version"], 1)

    def test_garbled_sidecar_counted_unreadable_status_marks_stale(self):
        _make_task_dir(self.tmp, "0001", sidecar_garbled=True)
        inv = build_sidecar_inventory(self.tmp, utc_stamp="t1")
        self.assertEqual(inv.unreadable_sidecars, 1)
        # Unreadable sidecars get STALE_VERSION (we can detect
        # the file is unreadable but we don't know the policy).
        self.assertEqual(inv.entries[0].status, SidecarStatus.STALE_VERSION)

    def test_garbled_task_json_counted_unreadable_status_marks_missing(self):
        _make_task_dir(self.tmp, "0001", task_json_garbled=True)
        inv = build_sidecar_inventory(self.tmp, utc_stamp="t1")
        # The file exists and has bytes (so has_task_json=True)
        # but the bytes don't parse as JSON (so
        # unreadable_task_json=1). The status is MISSING because
        # the sidecar is also missing.
        self.assertEqual(inv.unreadable_task_json, 1)
        self.assertTrue(inv.entries[0].has_task_json)
        self.assertEqual(inv.entries[0].status, SidecarStatus.MISSING)

    def test_iteration_is_sorted_by_task_id(self):
        # Insert in reverse order → inventory must return sorted.
        for tid in ("0005", "0003", "0007", "0001"):
            _make_task_dir(self.tmp, tid)
        inv = build_sidecar_inventory(self.tmp, utc_stamp="t1")
        ids = [e.task_id for e in inv.entries]
        self.assertEqual(ids, ["0001", "0003", "0005", "0007"])

    def test_non_task_subdirs_skipped(self):
        # Not starting with TASK- → skipped.
        (self.tmp / "README.md").write_text("hi")
        (self.tmp / "TASK-bogus").mkdir()  # NO task.json inside
        _make_task_dir(self.tmp, "0001")
        inv = build_sidecar_inventory(self.tmp, utc_stamp="t1")
        self.assertEqual([e.task_id for e in inv.entries], ["0001"])


class TestInventoryMixedCorpus(unittest.TestCase):
    """A mini corpus with all 4 buckets present."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aee77c-mix-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _hash_of(self, task_dir: Path) -> str:
        import hashlib
        h = hashlib.sha256()
        with open(task_dir / "task.json", "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def test_all_four_buckets(self):
        # Bucket 1: FRESH
        d1 = _make_task_dir(self.tmp, "0001")
        d1.joinpath("identity.json").write_text(
            json.dumps(_fresh_sidecar("0001", source_hash=self._hash_of(d1)),
                       sort_keys=True),
            encoding="utf-8",
        )
        # Bucket 2: STALE_HASH (sidecar cites a wrong source hash)
        d2 = _make_task_dir(self.tmp, "0002")
        d2.joinpath("identity.json").write_text(
            json.dumps(_fresh_sidecar("0002", source_hash="b" * 64),
                       sort_keys=True),
            encoding="utf-8",
        )
        # Bucket 3: STALE_VERSION (policy_version older AND hash matches)
        d3 = _make_task_dir(self.tmp, "0003")
        stale = _stale_version_sidecar("0003")
        stale["source_task_json_sha256"] = self._hash_of(d3)
        d3.joinpath("identity.json").write_text(
            json.dumps(stale, sort_keys=True),
            encoding="utf-8",
        )
        # Bucket 4: MISSING
        _make_task_dir(self.tmp, "0004")

        inv = build_sidecar_inventory(self.tmp, utc_stamp="t1")
        self.assertEqual(len(inv.entries), 4)
        self.assertEqual(inv.by_status["fresh"], 1)
        self.assertEqual(inv.by_status["stale_hash"], 1)
        self.assertEqual(inv.by_status["stale_version"], 1)
        self.assertEqual(inv.by_status["missing"], 1)


class TestMigrationPlanner(unittest.TestCase):
    """``plan_sidecar_migration`` is pure: inventory → plan."""

    def _make_inv(
        self,
        counts: Dict[SidecarStatus, int],
        *,
        runtime_in: Optional[SidecarStatus] = None,
    ) -> SidecarInventoryResult:
        """Build a synthetic inventory with the requested
        per-status counts. ``runtime_in`` optionally tags one
        entry of the named status as ``record_kind="runtime"``
        so the plan's ``runtime_would_touch`` math is exercised.
        """
        entries = []
        for status, n in counts.items():
            for i in range(n):
                record_kind = "runtime" if (
                    runtime_in == status and i == 0
                ) else "fixture"
                entries.append(SidecarInventoryEntry(
                    task_id=f"t-{status.value}-{i}",
                    status=status,
                    has_task_json=True,
                    has_sidecar=(status != SidecarStatus.MISSING),
                    sidecar_policy_version="1.0.0",
                    sidecar_schema_version="1.0.0",
                    task_json_sha256="a" * 64,
                    sidecar_sha256="b" * 64 if status != SidecarStatus.MISSING else "",
                    sidecar_classified_at_utc=(
                        "2026-07-12T00:00:00Z"
                        if status != SidecarStatus.MISSING else None
                    ),
                    record_kind=record_kind,
                    is_consistent_hint=True,
                ))
        return SidecarInventoryResult(
            reports_root="/synthetic",
            inventoried_at_utc="2026-07-12T00:00:00Z",
            schema_version=INVENTORY_SCHEMA_VERSION,
            current_policy_version="1.0.0",
            current_schema_version="1.0.0",
            entries=entries,
        )

    def test_no_op_when_already_current(self):
        inv = self._make_inv({SidecarStatus.FRESH: 5})
        plan = plan_sidecar_migration(
            inv, target_policy_version="1.0.0", utc_stamp="t1"
        )
        self.assertEqual(plan.no_op, 5)
        self.assertEqual(plan.would_overwrite, 0)
        self.assertEqual(plan.would_write, 0)
        self.assertEqual(plan.runtime_would_touch, 0)
        self.assertEqual(plan.sample_task_ids, ())

    def test_missing_records_become_would_write(self):
        inv = self._make_inv({SidecarStatus.MISSING: 3})
        plan = plan_sidecar_migration(
            inv, target_policy_version="1.1.0", utc_stamp="t1"
        )
        self.assertEqual(plan.would_write, 3)
        self.assertEqual(plan.would_overwrite, 0)
        self.assertEqual(plan.no_op, 0)
        self.assertEqual(plan.sample_task_ids, ("t-missing-0", "t-missing-1", "t-missing-2"))

    def test_stale_records_become_would_overwrite(self):
        inv = self._make_inv({
            SidecarStatus.STALE_HASH: 2,
            SidecarStatus.STALE_VERSION: 3,
        })
        plan = plan_sidecar_migration(
            inv, target_policy_version="1.1.0", utc_stamp="t1"
        )
        self.assertEqual(plan.would_overwrite, 5)
        self.assertEqual(plan.would_write, 0)
        self.assertEqual(len(plan.sample_task_ids), 5)

    def test_runtime_records_count_separately(self):
        inv = self._make_inv(
            {SidecarStatus.MISSING: 4},
            runtime_in=SidecarStatus.MISSING,
        )
        plan = plan_sidecar_migration(
            inv, target_policy_version="1.1.0", utc_stamp="t1"
        )
        self.assertEqual(plan.would_write, 4)
        self.assertEqual(plan.runtime_would_touch, 1)

    def test_max_listed_caps_sample(self):
        inv = self._make_inv({SidecarStatus.MISSING: 200})
        plan = plan_sidecar_migration(
            inv, target_policy_version="1.1.0", utc_stamp="t1",
            max_listed=10,
        )
        self.assertEqual(plan.would_write, 200)
        self.assertEqual(len(plan.sample_task_ids), 10)

    def test_empty_inventory_produces_empty_plan(self):
        inv = self._make_inv({})
        plan = plan_sidecar_migration(
            inv, target_policy_version="1.1.0", utc_stamp="t1"
        )
        self.assertEqual(plan.no_op, 0)
        self.assertEqual(plan.would_write, 0)
        self.assertEqual(plan.would_overwrite, 0)
        self.assertEqual(plan.sample_task_ids, ())


class TestInventorySerialization(unittest.TestCase):
    """DTOs are JSON-serializable for the audit manifest."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aee77c-ser-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_inventory_to_dict_is_json_serializable(self):
        _make_task_dir(self.tmp, "0001")
        inv = build_sidecar_inventory(self.tmp, utc_stamp="t1")
        d = inv.to_dict()
        # JSON round-trip — no non-serializable fields
        s = json.dumps(d)
        roundtrip = json.loads(s)
        self.assertEqual(roundtrip["by_status"], d["by_status"])
        self.assertEqual(roundtrip["entries"], d["entries"])

    def test_inventory_to_markdown_includes_status_table(self):
        _make_task_dir(self.tmp, "0001")
        inv = build_sidecar_inventory(self.tmp, utc_stamp="t1")
        md = inv.to_markdown()
        self.assertIn("AEE-7.7c Sidecar Inventory", md)
        self.assertIn("By status", md)
        self.assertIn("`missing`", md)
        self.assertIn("Current writer policy_version", md)

    def test_plan_to_dict_is_json_serializable(self):
        inv = build_sidecar_inventory(self.tmp, utc_stamp="t1")
        plan = plan_sidecar_migration(
            inv, target_policy_version="1.1.0", utc_stamp="t1"
        )
        s = json.dumps(plan.to_dict())
        roundtrip = json.loads(s)
        self.assertEqual(roundtrip["target_policy_version"], "1.1.0")


class TestNoDispatcherImport(unittest.TestCase):
    """The aee.audit sub-package MUST NOT import dispatcher.
    This contract is established by AEE-7.7a and reaffirmed by
    every subsequent audit-side module. AEE-7.7c is no exception.
    """

    def test_no_dispatcher_import_in_sidecar_inventory(self):
        from aee.audit import sidecar_inventory
        # The contract: no module-level ``import dispatcher`` and
        # no ``from dispatcher import ...`` (and no relative-import
        # equivalent). Docstrings and comments can mention the
        # word "dispatcher" freely.
        src = Path(sidecar_inventory.__file__).read_text(encoding="utf-8")
        for banned in ("import dispatcher", "from dispatcher"):
            self.assertNotIn(
                banned, src,
                f"sidecar_inventory source contains '{banned}' — "
                f"this would transitively pull the live DB / hot path "
                f"into the audit module",
            )
        # The module must not pull ``dispatcher`` into sys.modules
        # just by being imported.
        before = set(sys.modules)
        # Re-import to force any deferred imports.
        import importlib
        importlib.reload(sidecar_inventory)
        after = set(sys.modules)
        new = after - before
        for name in new:
            self.assertFalse(
                name.startswith("dispatcher"),
                f"sidecar_inventory transitively imported {name}",
            )


class TestInventorySchemaVersion(unittest.TestCase):
    def test_schema_version_constant_is_1_0_0(self):
        # Pinning the schema version is part of the AEE convention
        # — this is what the migration registry will compare
        # against in a future slice.
        self.assertEqual(INVENTORY_SCHEMA_VERSION, "1.0.0")

    def test_inventory_carries_schema_version(self):
        with tempfile.TemporaryDirectory() as d:
            inv = build_sidecar_inventory(d, utc_stamp="t1")
            self.assertEqual(inv.schema_version, "1.0.0")
            self.assertEqual(
                inv.current_policy_version, "1.0.0"
            )
            self.assertEqual(
                inv.current_schema_version, "1.0.0"
            )


class TestInventoryLiveCorpusOptional(unittest.TestCase):
    """Optional: scan the real ``reports/`` corpus.

    Skipped by default (set ``AEE77C_LIVE_CORPUS=1`` to enable).
    When enabled, asserts the corpus is in a known shape (matches
    the pre-inventory smoke test output from §A.7.18.5).
    """

    LIVE_ENABLED = os.environ.get("AEE77C_LIVE_CORPUS") == "1"
    REPO_ROOT = Path(__file__).resolve().parent.parent.parent
    REPORTS_ROOT = REPO_ROOT / "reports"

    @unittest.skipUnless(
        LIVE_ENABLED,
        "Set AEE77C_LIVE_CORPUS=1 to run against the real reports/ corpus",
    )
    def test_real_corpus_inventory_shape(self):
        if not self.REPORTS_ROOT.is_dir():
            self.skipTest("no live reports/ in this checkout")
        inv = build_sidecar_inventory(
            self.REPORTS_ROOT, utc_stamp="2026-07-12T00:00:00Z"
        )
        # The smoke probe earlier showed 258 dirs, 256 with
        # task.json, 136 with identity.json, all at
        # policy_version=1.0.0. The inventory must reflect the
        # same shape (allowing +0-2 for new tasks recorded since
        # the smoke probe).
        self.assertGreaterEqual(len(inv.entries), 256)
        # Existing 136 sidecars are all 1.0.0 → no STALE_VERSION
        # entries from the existing corpus.
        self.assertEqual(inv.by_status.get("stale_version", 0), 0)
        # All 256+ entries with task.json have a non-empty hash.
        for e in inv.entries:
            self.assertTrue(e.has_task_json)
            self.assertNotEqual(e.task_json_sha256, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
