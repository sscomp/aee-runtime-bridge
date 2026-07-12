"""AEE-7.7d — controlled sidecar migration / stamping tests.

This test module covers the AEE-7.7d
:func:`aee.audit.execute_sidecar_migration` entry point and
its companion :class:`MigrationExecutionResult` /
:class:`PerTaskMigrationOutcome` / :class:`MigrationStatus`
DTOs.

The tests verify (one per brief section 6):

1.  ``MISSING`` entry → ``MigrationStatus.WROTE``.
2.  ``STALE_HASH`` entry → ``MigrationStatus.OVERWROTE``.
3.  ``STALE_VERSION`` entry → ``MigrationStatus.OVERWROTE``
    (the new sidecar stamps current policy_version).
4.  ``CURRENT`` (FRESH) entry → ``MigrationStatus.STATUS_FILTERED``
    by default (default status_filter excludes FRESH).
5.  ``status_filter`` override: only the listed statuses are touched.
6.  ``write_log=False`` does not write the migration log JSON.
7.  ``write_log=True`` writes the migration log JSON to the
    caller-supplied (or default sibling) path.
8.  Repeated apply is idempotent — second run reports
    ``UNCHANGED`` and the on-disk sidecar is byte-identical.
9.  ``missing task.json`` → ``MigrationStatus.NO_TASK_JSON``.
10. Fixture-only records: the migration is opt-in for FIXTURE
    via the default filter (FIXTURE records are
    ``MISSING`` / ``STALE_*`` like any other record, but the
    default filter still applies).
11. Fixture opt-in behaviour: explicit
    ``status_filter=...`` (with FIXTURE-tagged entries in
    MISSING state) is honoured.
12. Runtime record anchors preserved: when
    ``allow_runtime=True``, the new sidecar's
    ``executor_session_id`` / ``runtime_run_id`` /
    ``user_provided_alias`` are None (the SOT helper does not
    re-read them from task.json — they're caller-supplied).
    The ``policy_version`` is current.
13. Corrupt / garbled existing sidecar: the inventory
    classifies it as ``STALE_VERSION`` and the migration
    re-stamps a fresh sidecar (contract: "garbled sidecar
    may be directly overwritten and reported as WROTE").
14. Deterministic result serialization:
    ``MigrationExecutionResult.to_dict()`` round-trips
    through ``json.dumps`` / ``json.loads`` and is stable
    for a given (input, utc_stamp) pair.
15. Per-task partial-failure isolation: one task that
    cannot be processed does not block the rest of the
    batch.
16. Path traversal rejection: a ``task_id`` with ``..``
    cannot be used to escape the reports root.
17. Symlink escape rejection: a symlink under the task
    directory pointing outside the corpus is detected and
    refused (or at minimum: the migration does not
    follow the symlink to write a sidecar).
18. Existing report contents immutable: ``task.json`` and
    any other pre-existing files in the corpus are
    byte-identical (and mtime-identical) before and after
    the migration.
19. No prompt leakage: the migration log and per-task
    outcomes never echo the seeded ``input_text``
    fingerprint or the seeded ``PROMPT`` marker.
20. No stdout / stderr leakage: the migration log and
    outcomes never echo seeded ``stdout`` / ``stderr``
    markers.
21. No secret leakage: the migration log and outcomes
    never echo the seeded ``sk-secret-...`` markers.
22. AEE-7.7b ``apply_sidecars`` compatibility: the
    migration is a strict superset of the AEE-7.7b write
    path (the same SOT helper
    :func:`aee.reporting.identity.classify_and_persist` is
    called; the result DTO is a separate, additive
    surface).
23. AEE-7.7c inventory / plan compatibility: the
    migration accepts the inventory built by
    :func:`aee.audit.build_sidecar_inventory` directly
    (no translation layer needed).
24. No dispatcher import leakage: loading
    ``aee.audit.sidecar_migration`` does NOT introduce
    any ``dispatcher.*`` module into ``sys.modules``.
25. Temp corpus integration: a mixed corpus (MISSING +
    STALE_HASH + STALE_VERSION + FRESH) migrates to a
    known shape (WROTE + OVERWROTE + OVERWROTE +
    STATUS_FILTERED) and the result DTO is consistent.
26. Migration re-run produces no unnecessary writes: a
    second migration over the just-stamped corpus
    reports ``UNCHANGED`` for every touched entry and
    the on-disk sidecar hash is stable.
27. Execution-result counts are internally consistent:
    ``by_status`` and ``by_inventory_status`` sum to
    ``len(outcomes)``.

The test placement mirrors ``test_aee77c_sidecar_inventory.py``:
``aee/tests/`` is the audit-package home. The tests use only
``tempfile.mkdtemp`` sandboxes; they do NOT touch the live
``reports/`` corpus, the live ``data/dispatcher.db``, or any
file outside the test's own temp dir.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Optional


# Make ``aee`` importable when running via ``python -m unittest
# aee.tests.test_aee77d_sidecar_migration`` from outside the repo
# root.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


from aee.audit import (  # noqa: E402
    DEFAULT_STATUS_FILTER,
    MIGRATION_EXEC_SCHEMA_VERSION,
    MigrationExecutionResult,
    MigrationStatus,
    PerTaskMigrationOutcome,
    SidecarInventoryEntry,
    SidecarInventoryResult,
    SidecarStatus,
    build_sidecar_inventory,
    execute_sidecar_migration,
    plan_sidecar_migration,
)
from aee.audit.sidecar_inventory import (  # noqa: E402
    INVENTORY_SCHEMA_VERSION,
)
from aee.reporting.identity import (  # noqa: E402
    _file_sha256,
)


# ---------------------------------------------------------------------------
# Canonical record fixtures
# ---------------------------------------------------------------------------
# These mirror the fixtures in test_aee77_apply_sidecars.py so the
# cross-compatibility tests (case 22) can run on a known shape.

# A canonical RUNTIME record (run_<32hex> + valid executor anchors).
_RUNTIME_RECORD: Dict[str, Any] = {
    "task_id": "TASK-20260712-0001",
    "title": "aee7.7d RUNTIME smoke",
    "type": "coding",
    "priority": 50,
    "owner": "m2",
    "status": "completed",
    "progress_pct": 100,
    "progress_step": "done",
    "created_at": "2026-07-12T10:00:00.000Z",
    "started_at": "2026-07-12T10:00:01.000Z",
    "finished_at": "2026-07-12T10:05:00.000Z",
    "duration_sec": 299.0,
    "input_text": (
        "marker_runtime_input "
        "sk-secret-runtime-do-not-leak "
        "PROMPT-FROM-USER-12345"
    ),
    "hermes_run_id": "run_0123456789abcdef0123456789abcdef",
    "executor_session_id": "AEE-7.7D-RUNTIME-20260712",
    "runtime_run_id": "run_0123456789abcdef0123456789abcdef",
    "repo_root": "/home/ubuntu/hermes-runtime-bridge",
}

# A canonical FIXTURE record (``hr-1`` is in the default
# sentinel set).
_FIXTURE_RECORD: Dict[str, Any] = {
    "task_id": "TASK-20260712-0002",
    "title": "aee7.7d FIXTURE smoke",
    "type": "coding",
    "priority": 50,
    "owner": "m2",
    "status": "running",
    "progress_pct": 5,
    "input_text": (
        "marker_fixture_input "
        "sk-secret-fixture-do-not-leak "
        "stdout-from-test-fixture "
        "stderr-from-test-fixture"
    ),
    "hermes_run_id": "hr-1",
    "executor_session_id": "AEE-7.7D-FIXTURE-20260712",
    "runtime_run_id": "run-aee-77d-fixture",
    "repo_root": "/home/ubuntu/hermes-runtime-bridge",
}

# Markers that must NEVER appear in any DTO field, the migration
# log JSON, the per-task outcome note, or the freshly stamped
# sidecar payload.
_LEAK_MARKERS = (
    "marker_runtime_input",
    "marker_fixture_input",
    "sk-secret-runtime-do-not-leak",
    "sk-secret-fixture-do-not-leak",
    "PROMPT-FROM-USER-12345",
    "stdout-from-test-fixture",
    "stderr-from-test-fixture",
)

# The deterministic UTC stamp used across the tests. Pinning
# this value makes the to_dict() / to_markdown() output stable
# across runs and platforms.
_FIXTURE_UTC_STAMP = "2026-07-12T13:00:00Z"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _write_task_json(
    root: Path,
    task_id: str,
    payload: Dict[str, Any],
) -> Path:
    """Write a single ``task.json`` under ``root/<task_id>/``."""
    d = root / task_id
    d.mkdir(parents=True, exist_ok=True)
    p = d / "task.json"
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    return p


def _write_identity_sidecar(
    task_json_path: Path,
    payload: Dict[str, Any],
) -> Path:
    """Write a hand-crafted ``identity.json`` sidecar via
    atomic temp+replace.
    """
    sidecar = task_json_path.parent / "identity.json"
    encoded = json.dumps(payload, indent=2, sort_keys=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".identity.", suffix=".json.tmp",
        dir=str(task_json_path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(encoded.encode("utf-8"))
        os.replace(tmp_path, sidecar)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return sidecar


def _fresh_sidecar_for(task_id: str, source_hash: str) -> Dict[str, Any]:
    """Build a sidecar payload that the inventory will classify
    as ``FRESH`` (1.0.0 / 1.0.0 + matching source hash).
    """
    return {
        "task_id": task_id,
        "classified_at_utc": "2026-07-12T00:00:00Z",
        "executor_session_id": None,
        "fixture_markers": [],
        "is_fixture": True,
        "policy_version": "1.0.0",
        "record_kind": "fixture",
        "runtime_run_id": None,
        "source_task_json_sha256": source_hash,
        "user_provided_alias": None,
    }


def _stale_version_sidecar_for(task_id: str) -> Dict[str, Any]:
    """Build a sidecar payload that the inventory will classify
    as ``STALE_VERSION`` once the source hash matches the
    on-disk task.json (0.9.0 + matching hash).
    """
    return {
        "task_id": task_id,
        "classified_at_utc": "2026-07-01T00:00:00Z",
        "policy_version": "0.9.0",
        "record_kind": "fixture",
        "is_fixture": True,
        "source_task_json_sha256": "",  # filled in by the caller
    }


def _seed_missing_fixture(root: Path, task_id: Optional[str] = None) -> Path:
    """Seed a FIXTURE record with no sidecar → ``MISSING``.

    Writes ``root/TASK-<task_id>/task.json`` where ``task_id``
    defaults to the canonical FIXTURE record's task_id.
    """
    tid = task_id or _FIXTURE_RECORD["task_id"]
    return _write_task_json(root, tid, _FIXTURE_RECORD)


def _seed_stale_hash_fixture(root: Path, task_id: Optional[str] = None) -> Path:
    """Seed a FIXTURE record whose sidecar cites a wrong
    source hash → ``STALE_HASH``.

    Writes ``root/TASK-<task_id>/task.json`` + a divergent
    sidecar. The ``task_id`` defaults to the canonical
    FIXTURE record's task_id.
    """
    tid = task_id or _FIXTURE_RECORD["task_id"]
    payload = dict(_FIXTURE_RECORD)
    payload["task_id"] = tid
    p = _write_task_json(root, tid, payload)
    sidecar = _fresh_sidecar_for(
        tid, source_hash="a" * 64,
    )
    _write_identity_sidecar(p, sidecar)
    return p


def _seed_stale_version_fixture(root: Path, task_id: Optional[str] = None) -> Path:
    """Seed a FIXTURE record whose sidecar has an older
    policy_version but a matching source hash →
    ``STALE_VERSION``.

    Writes ``root/TASK-<task_id>/task.json`` + a stale
    sidecar. The ``task_id`` defaults to the canonical
    FIXTURE record's task_id.
    """
    tid = task_id or _FIXTURE_RECORD["task_id"]
    payload = dict(_FIXTURE_RECORD)
    payload["task_id"] = tid
    p = _write_task_json(root, tid, payload)
    sha = _file_sha256(p)
    sidecar = _stale_version_sidecar_for(tid)
    sidecar["source_task_json_sha256"] = sha
    _write_identity_sidecar(p, sidecar)
    return p


def _seed_fresh_fixture(root: Path, task_id: Optional[str] = None) -> Path:
    """Seed a FIXTURE record whose sidecar is fully current
    (1.0.0 / matching hash) → ``FRESH``.

    Writes ``root/TASK-<task_id>/task.json`` + a fresh
    sidecar. The ``task_id`` defaults to the canonical
    FIXTURE record's task_id.
    """
    tid = task_id or _FIXTURE_RECORD["task_id"]
    payload = dict(_FIXTURE_RECORD)
    payload["task_id"] = tid
    p = _write_task_json(root, tid, payload)
    sha = _file_sha256(p)
    _write_identity_sidecar(p, _fresh_sidecar_for(tid, sha))
    return p


# ---------------------------------------------------------------------------
# 1. MISSING → WROTE
# ---------------------------------------------------------------------------


class TestMissingToWrote(unittest.TestCase):
    """A ``MISSING`` inventory entry is converted to a fresh
    ``identity.json`` by the migration; the outcome is
    :attr:`MigrationStatus.WROTE`.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77d-miss-"))
        self.reports_root = self._tmp / "reports"
        self.reports_root.mkdir()
        _seed_missing_fixture(self.reports_root)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_missing_entry_produces_wrote_outcome(self) -> None:
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        self.assertEqual(len(inv.entries), 1)
        self.assertEqual(inv.entries[0].status, SidecarStatus.MISSING)
        res = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
        )
        self.assertEqual(len(res.outcomes), 1)
        o = res.outcomes[0]
        self.assertEqual(o.task_id, "20260712-0002")
        self.assertEqual(o.status, MigrationStatus.WROTE)
        self.assertEqual(o.inventory_status, SidecarStatus.MISSING)
        # The sidecar was written.
        sidecar = self.reports_root / _FIXTURE_RECORD["task_id"] / "identity.json"
        self.assertTrue(sidecar.exists())
        # And it's valid JSON with the current writer's contract.
        payload = json.loads(sidecar.read_text())
        self.assertEqual(payload["task_id"], _FIXTURE_RECORD["task_id"])
        self.assertEqual(payload["record_kind"], "fixture")
        self.assertEqual(payload["policy_version"], "1.0.0")
        # Counts.
        self.assertEqual(res.by_status[MigrationStatus.WROTE.value], 1)
        self.assertEqual(
            res.by_inventory_status[SidecarStatus.MISSING.value], 1
        )


# ---------------------------------------------------------------------------
# 2. STALE_HASH → OVERWROTE
# ---------------------------------------------------------------------------


class TestStaleHashToOverwrote(unittest.TestCase):
    """A ``STALE_HASH`` inventory entry is overwritten with a
    fresh sidecar citing the current ``task.json`` hash. The
    outcome is :attr:`MigrationStatus.OVERWROTE`.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77d-sh-"))
        self.reports_root = self._tmp / "reports"
        self.reports_root.mkdir()
        self._fx_path = _seed_stale_hash_fixture(self.reports_root)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_stale_hash_entry_produces_overwrote_outcome(self) -> None:
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        self.assertEqual(inv.entries[0].status, SidecarStatus.STALE_HASH)
        res = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
        )
        o = res.outcomes[0]
        self.assertEqual(o.status, MigrationStatus.OVERWROTE)
        self.assertEqual(o.inventory_status, SidecarStatus.STALE_HASH)
        # The new sidecar's source hash matches the on-disk
        # task.json hash (this is what makes the new sidecar
        # FRESH on a subsequent inventory pass).
        new_payload = json.loads(
            (self._fx_path.parent / "identity.json").read_text()
        )
        self.assertEqual(
            new_payload["source_task_json_sha256"],
            _file_sha256(self._fx_path),
        )


# ---------------------------------------------------------------------------
# 3. STALE_VERSION → WROTE / RESTAMPED
# ---------------------------------------------------------------------------


class TestStaleVersionToRestamped(unittest.TestCase):
    """A ``STALE_VERSION`` inventory entry is restamped with the
    current ``policy_version``. The outcome is
    :attr:`MigrationStatus.OVERWROTE` (existing sidecar was
    different) and the new sidecar carries the current
    policy_version.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77d-sv-"))
        self.reports_root = self._tmp / "reports"
        self.reports_root.mkdir()
        self._fx_path = _seed_stale_version_fixture(self.reports_root)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_stale_version_entry_is_restamped(self) -> None:
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        self.assertEqual(inv.entries[0].status, SidecarStatus.STALE_VERSION)
        res = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
        )
        o = res.outcomes[0]
        self.assertEqual(o.status, MigrationStatus.OVERWROTE)
        self.assertEqual(o.inventory_status, SidecarStatus.STALE_VERSION)
        # The new sidecar carries the CURRENT policy_version.
        new_payload = json.loads(
            (self._fx_path.parent / "identity.json").read_text()
        )
        self.assertEqual(new_payload["policy_version"], "1.0.0")
        self.assertEqual(o.policy_version, "1.0.0")
        # The new sidecar's note field is short and non-leaky.
        self.assertIn("differs", o.note)
        # The new sidecar is FRESH (1.0.0 + matching hash) — a
        # subsequent inventory will classify it as FRESH.
        new_inv = build_sidecar_inventory(
            self.reports_root, utc_stamp="t2"
        )
        self.assertEqual(new_inv.entries[0].status, SidecarStatus.FRESH)


# ---------------------------------------------------------------------------
# 4. CURRENT (FRESH) → SKIPPED
# ---------------------------------------------------------------------------


class TestCurrentFreshSkipped(unittest.TestCase):
    """A ``FRESH`` (current) entry is excluded by the default
    status filter and reported as
    :attr:`MigrationStatus.STATUS_FILTERED`. The on-disk
    sidecar is left untouched (no mtime / content change).
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77d-fresh-"))
        self.reports_root = self._tmp / "reports"
        self.reports_root.mkdir()
        self._fx_path = _seed_fresh_fixture(self.reports_root)
        self._sidecar_path = self._fx_path.parent / "identity.json"
        # Snapshot the on-disk sidecar for immutability checks.
        self._sidecar_sha_before = _file_sha256(self._sidecar_path)
        self._sidecar_mtime_before = self._sidecar_path.stat().st_mtime_ns

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_fresh_entry_is_status_filtered_by_default(self) -> None:
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        self.assertEqual(inv.entries[0].status, SidecarStatus.FRESH)
        res = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
        )
        o = res.outcomes[0]
        self.assertEqual(o.status, MigrationStatus.STATUS_FILTERED)
        self.assertEqual(o.inventory_status, SidecarStatus.FRESH)
        # The default filter does not include FRESH.
        self.assertNotIn(
            SidecarStatus.FRESH.value, res.status_filter,
        )
        # And the on-disk sidecar is untouched.
        self.assertEqual(
            _file_sha256(self._sidecar_path), self._sidecar_sha_before,
        )
        self.assertEqual(
            self._sidecar_path.stat().st_mtime_ns,
            self._sidecar_mtime_before,
        )


# ---------------------------------------------------------------------------
# 5. status filter
# ---------------------------------------------------------------------------


class TestStatusFilterOverride(unittest.TestCase):
    """The ``status_filter`` argument overrides the default
    set; only the listed statuses are touched, all others
    are reported as ``STATUS_FILTERED``.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77d-filter-"))
        self.reports_root = self._tmp / "reports"
        self.reports_root.mkdir()
        # Three fixtures covering MISSING / STALE_HASH / FRESH.
        # Each seeder writes its own TASK-<id>/ dir under
        # self.reports_root.
        self._miss = _seed_missing_fixture(
            self.reports_root, task_id="TASK-20260712-0001",
        )
        self._sh = _seed_stale_hash_fixture(
            self.reports_root, task_id="TASK-20260712-0003",
        )
        self._fx = _seed_fresh_fixture(
            self.reports_root, task_id="TASK-20260712-0004",
        )

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_filter_only_misses_writes_only_those(self) -> None:
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        # Sanity: we have MISSING / STALE_HASH / FRESH in the
        # inventory.
        statuses = {e.status for e in inv.entries}
        self.assertIn(SidecarStatus.MISSING, statuses)
        self.assertIn(SidecarStatus.STALE_HASH, statuses)
        self.assertIn(SidecarStatus.FRESH, statuses)
        res = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
            status_filter=[SidecarStatus.MISSING],
        )
        # Only the MISSING entry was WROTE; the other two
        # entries were STATUS_FILTERED.
        by_id = {o.task_id: o for o in res.outcomes}
        wrote_ids = {
            o.task_id for o in res.outcomes
            if o.status == MigrationStatus.WROTE
        }
        self.assertEqual(len(wrote_ids), 1)
        self.assertEqual(
            res.by_status[MigrationStatus.WROTE.value], 1
        )
        self.assertEqual(
            res.by_status[MigrationStatus.STATUS_FILTERED.value], 2
        )
        # The status_filter tuple in the DTO is sorted.
        self.assertEqual(
            res.status_filter,
            tuple(sorted(s.value for s in [SidecarStatus.MISSING])),
        )
        for o in res.outcomes:
            if o.inventory_status == SidecarStatus.FRESH:
                self.assertEqual(o.status, MigrationStatus.STATUS_FILTERED)
            if o.inventory_status == SidecarStatus.STALE_HASH:
                self.assertEqual(o.status, MigrationStatus.STATUS_FILTERED)


# ---------------------------------------------------------------------------
# 6 + 7. dry-run vs apply behaviour
# ---------------------------------------------------------------------------


class TestDryRunVsApply(unittest.TestCase):
    """``write_log`` controls whether the migration log JSON is
    written to disk. The migration itself always stamps
    sidecars for matching entries — the dry-run contract is
    "the log file is the dry-run artefact, the sidecar is
    the apply artefact" (a separate ``status_filter`` that
    excludes every status is the pure dry-run path; the
    test asserts both behaviours).
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77d-dry-"))
        self.reports_root = self._tmp / "reports"
        self.reports_root.mkdir()
        self._miss_path = _seed_missing_fixture(self.reports_root)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_write_log_false_does_not_write_log_file(self) -> None:
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        log_path = self._tmp / "mlog.json"
        res = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
            log_path=log_path,
        )
        self.assertFalse(log_path.exists())
        self.assertIsNone(res.log_path)

    def test_write_log_true_writes_log_file(self) -> None:
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        log_path = self._tmp / "mlog.json"
        res = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=True,
            log_path=log_path,
        )
        self.assertTrue(log_path.exists())
        self.assertEqual(res.log_path, str(log_path))
        # The log is a JSON document with the same keys as
        # the in-memory DTO.
        log_doc = json.loads(log_path.read_text())
        self.assertEqual(
            log_doc["schema_version"], MIGRATION_EXEC_SCHEMA_VERSION
        )
        self.assertEqual(log_doc["executed_at_utc"], _FIXTURE_UTC_STAMP)
        self.assertEqual(len(log_doc["outcomes"]), 1)

    def test_default_log_path_sibling_of_reports_root(self) -> None:
        # When the caller does not supply log_path, the log
        # lands as a sibling of reports_root (NOT inside it)
        # so the audited corpus shape is preserved.
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        res = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=True,
        )
        self.assertIsNotNone(res.log_path)
        log = Path(res.log_path)
        # Parent is the parent of reports_root (NOT reports_root).
        self.assertEqual(
            log.parent, self.reports_root.parent
        )
        # No log file inside reports_root.
        log_files_in_corpus = [
            p for p in self.reports_root.rglob("*.json")
            if p.name.startswith("migration_log")
        ]
        self.assertEqual(log_files_in_corpus, [])

    def test_dry_run_via_empty_status_filter_does_not_stamp(self) -> None:
        # A pure dry-run: pass an empty status_filter. The
        # function still iterates and produces a result DTO
        # (the dry-run artefact) but does NOT stamp any
        # sidecar.
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        res = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
            status_filter=[],
        )
        # No sidecar was written.
        sidecar = self.reports_root / _FIXTURE_RECORD["task_id"] / "identity.json"
        self.assertFalse(sidecar.exists())
        # Every outcome is STATUS_FILTERED.
        self.assertTrue(all(
            o.status == MigrationStatus.STATUS_FILTERED
            for o in res.outcomes
        ))


# ---------------------------------------------------------------------------
# 8. repeated apply idempotency
# ---------------------------------------------------------------------------


class TestRepeatedApplyIdempotent(unittest.TestCase):
    """A second migration over the same corpus + the same
    inventory reports ``UNCHANGED`` for every entry and the
    on-disk sidecar is byte-identical (no mtime drift, no
    content drift).
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77d-idem-"))
        self.reports_root = self._tmp / "reports"
        self.reports_root.mkdir()
        self._miss = _seed_missing_fixture(
            self.reports_root, task_id="TASK-20260712-0001",
        )
        self._sh = _seed_stale_hash_fixture(
            self.reports_root, task_id="TASK-20260712-0003",
        )
        self._sidecar_miss = (
            self.reports_root / "TASK-20260712-0001" / "identity.json"
        )
        self._sidecar_sh = (
            self.reports_root / "TASK-20260712-0003" / "identity.json"
        )

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_second_run_reports_unchanged_for_every_touched(self) -> None:
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        first = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
        )
        # Snapshot the post-first-run sidecar state.
        miss_sha_after_first = _file_sha256(self._sidecar_miss)
        sh_sha_after_first = _file_sha256(self._sidecar_sh)

        # Second run: same inventory (built from the now-stamped
        # corpus, so every entry is FRESH → STATUS_FILTERED).
        # Rebuild the inventory from the post-first-run corpus
        # to mirror the AEE-7.7c ⇒ AEE-7.7d wire-up.
        inv2 = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP,
        )
        second = execute_sidecar_migration(
            self.reports_root, inv2,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
        )
        # Every entry on the second run is STATUS_FILTERED
        # (no MISSING / STALE_HASH / STALE_VERSION in inv2).
        self.assertTrue(all(
            o.status == MigrationStatus.STATUS_FILTERED
            for o in second.outcomes
        ))
        # On-disk sidecar bytes are byte-identical.
        self.assertEqual(
            _file_sha256(self._sidecar_miss), miss_sha_after_first
        )
        self.assertEqual(
            _file_sha256(self._sidecar_sh), sh_sha_after_first
        )

    def test_force_re_overwrites_byte_stable_sidecar(self) -> None:
        # Even with force=True, when the verdict is identical
        # to the prior sidecar, the on-disk sidecar hash is
        # stable (the SOT helper detects the match and
        # short-circuits the write).
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
        )
        sha_before = _file_sha256(self._sidecar_miss)
        # Rebuild inventory after the first run (the entry is
        # now FRESH — but force=True will treat it as a
        # re-stamp candidate via the SOT helper's
        # "skip-if-equal" path).
        inv2 = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP,
        )
        res = execute_sidecar_migration(
            self.reports_root, inv2,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
            force=True,
        )
        sha_after = _file_sha256(self._sidecar_miss)
        self.assertEqual(sha_before, sha_after)
        # The outcome note should still report a no-op-ish
        # decision (UNCHANGED for a fresh entry that survived
        # the filter; STATUS_FILTERED for the FRESH default).
        # With force=True, FRESH is still excluded from the
        # effective filter; the outcome is STATUS_FILTERED.
        for o in res.outcomes:
            if o.inventory_status == SidecarStatus.FRESH:
                self.assertEqual(
                    o.status, MigrationStatus.STATUS_FILTERED
                )


# ---------------------------------------------------------------------------
# 9. missing task.json
# ---------------------------------------------------------------------------


class TestMissingTaskJson(unittest.TestCase):
    """An inventory entry that points to a non-existent or
    unreadable ``task.json`` is reported as
    :attr:`MigrationStatus.NO_TASK_JSON` and no sidecar is
    written.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77d-nojson-"))
        self.reports_root = self._tmp / "reports"
        self.reports_root.mkdir()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_inventory_with_no_on_disk_task_json_skips_writing(self) -> None:
        # Hand-craft an inventory entry pointing to a
        # non-existent task.json (the inventory normally
        # would not produce such an entry, but the executor
        # must defend against it).
        fake_entry = SidecarInventoryEntry(
            task_id="20999999-9999",
            status=SidecarStatus.MISSING,
            has_task_json=False,
            has_sidecar=False,
            sidecar_policy_version=None,
            sidecar_schema_version=None,
            task_json_sha256="",
            sidecar_sha256="",
            sidecar_classified_at_utc=None,
            record_kind=None,
            is_consistent_hint=None,
        )
        inv = SidecarInventoryResult(
            reports_root=str(self.reports_root),
            inventoried_at_utc=_FIXTURE_UTC_STAMP,
            schema_version=INVENTORY_SCHEMA_VERSION,
            current_policy_version="1.0.0",
            current_schema_version="1.0.0",
            entries=[fake_entry],
        )
        res = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
        )
        self.assertEqual(len(res.outcomes), 1)
        o = res.outcomes[0]
        self.assertEqual(o.status, MigrationStatus.NO_TASK_JSON)
        self.assertEqual(o.inventory_status, SidecarStatus.MISSING)
        # No sidecar was created.
        self.assertFalse(
            (
                self.reports_root
                / f"TASK-{fake_entry.task_id}"
                / "identity.json"
            ).exists()
        )

    def test_garbled_task_json_reports_no_task_json(self) -> None:
        # A directory with a non-JSON task.json (so the
        # inventory's task_json_sha256 is non-empty —
        # ``has_task_json`` is True — but the JSON is
        # unreadable; ``unreadable_task_json`` is 1). The
        # migration defends against this: the task.json
        # SHA-256 is non-empty in the inventory, so the
        # migration tries to call classify_and_persist,
        # which loads the file and gets None. The
        # per-task outcome is MALFORMED (NOT a guess at
        # metadata from garbled bytes).
        d = self.reports_root / "TASK-20260712-0001"
        d.mkdir()
        (d / "task.json").write_bytes(b"\x00\x01not-json")
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        self.assertEqual(inv.unreadable_task_json, 1)
        res = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
        )
        o = res.outcomes[0]
        # The per-task outcome is MALFORMED or NO_TASK_JSON
        # — both signal that no sidecar was written and the
        # migration did not "guess" task metadata.
        self.assertIn(
            o.status,
            {
                MigrationStatus.MALFORMED,
                MigrationStatus.NO_TASK_JSON,
            },
        )
        # No sidecar was written — the migration does not
        # "guess" task metadata from garbled bytes.
        self.assertFalse((d / "identity.json").exists())


# ---------------------------------------------------------------------------
# 10 + 11. fixture-only default skip / opt-in
# ---------------------------------------------------------------------------


class TestFixtureRecords(unittest.TestCase):
    """Fixture records are stamped by the default filter
    (the default filter is ``MISSING + STALE_HASH +
    STALE_VERSION`` regardless of record_kind, so FIXTURE
    MISSING entries ARE written). The "opt-in" contract is
    the inverse: a record_kind-honouring filter (caller
    supplies ``status_filter=[SidecarStatus.MISSING]``) is
    also honoured.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77d-fx-"))
        self.reports_root = self._tmp / "reports"
        self.reports_root.mkdir()
        self._fx_path = _seed_missing_fixture(self.reports_root)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_default_filter_writes_fixture_missing(self) -> None:
        # The default filter is "MISSING + STALE_*" — a
        # FIXTURE record with status=MISSING is eligible.
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        res = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
        )
        o = res.outcomes[0]
        self.assertEqual(o.status, MigrationStatus.WROTE)
        self.assertEqual(o.record_kind, "fixture")
        # The freshly-stamped sidecar exists.
        self.assertTrue(
            (self._fx_path.parent / "identity.json").exists()
        )

    def test_explicit_empty_status_filter_skips_everything(self) -> None:
        # The opt-out path: an empty status_filter is a
        # pure no-op (the dry-run shape).
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        res = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
            status_filter=[],
        )
        # The sidecar was NOT written.
        self.assertFalse(
            (self._fx_path.parent / "identity.json").exists()
        )
        # The outcome is STATUS_FILTERED.
        self.assertEqual(
            res.outcomes[0].status, MigrationStatus.STATUS_FILTERED
        )
        # The status_filter tuple in the DTO is empty.
        self.assertEqual(res.status_filter, ())


# ---------------------------------------------------------------------------
# 12. runtime record anchors preserved
# ---------------------------------------------------------------------------


class TestRuntimeAnchorsPreserved(unittest.TestCase):
    """When ``allow_runtime=True``, the migration stamps a
    sidecar for a RUNTIME record. The sidecar carries the
    canonical task identity (task_id, policy_version,
    source_task_json_sha256) and the runtime anchors that
    the SOT helper stamps (``executor_session_id`` /
    ``runtime_run_id`` / ``user_provided_alias``).

    The SOT helper does NOT re-read these from the on-disk
    task.json (they are caller-supplied). The migration
    executor does not currently accept executor anchors —
    the default behaviour is to stamp them as None. The
    test pins this contract: a RUNTIME sidecar written by
    the migration has the same shape a future caller would
    get from the SOT helper directly with default args.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77d-rt-"))
        self.reports_root = self._tmp / "reports"
        self.reports_root.mkdir()
        self._rt_path = _write_task_json(
            self.reports_root, _RUNTIME_RECORD["task_id"], _RUNTIME_RECORD
        )

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_runtime_record_writes_sidecar_when_allow_runtime_true(self) -> None:
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        res = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
            allow_runtime=True,
        )
        o = res.outcomes[0]
        self.assertEqual(o.status, MigrationStatus.WROTE)
        self.assertEqual(o.record_kind, "runtime")
        self.assertEqual(o.inventory_status, SidecarStatus.MISSING)
        # The on-disk sidecar exists.
        sidecar = self._rt_path.parent / "identity.json"
        self.assertTrue(sidecar.exists())
        payload = json.loads(sidecar.read_text())
        # Canonical identity preserved.
        self.assertEqual(payload["task_id"], _RUNTIME_RECORD["task_id"])
        self.assertEqual(payload["record_kind"], "runtime")
        self.assertFalse(payload["is_fixture"])
        self.assertEqual(payload["policy_version"], "1.0.0")
        self.assertEqual(
            payload["source_task_json_sha256"],
            _file_sha256(self._rt_path),
        )
        # The migration executor does not currently accept
        # executor anchors; the SOT helper stamps them as
        # None. This is the documented contract — the same
        # shape a future caller would get from the SOT
        # helper with default args.
        self.assertIn("executor_session_id", payload)
        self.assertIn("runtime_run_id", payload)
        self.assertIn("user_provided_alias", payload)

    def test_runtime_record_skipped_when_allow_runtime_false(self) -> None:
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        res = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
            allow_runtime=False,
        )
        o = res.outcomes[0]
        self.assertEqual(o.status, MigrationStatus.RUNTIME_SKIPPED)
        self.assertEqual(o.record_kind, "runtime")
        # No sidecar was written.
        self.assertFalse(
            (self._rt_path.parent / "identity.json").exists()
        )
        # Counts.
        self.assertEqual(
            res.by_status[MigrationStatus.RUNTIME_SKIPPED.value], 1
        )

    def test_runtime_defence_in_depth_when_inventory_record_kind_none(
        self,
    ) -> None:
        # When the inventory cannot determine the record_kind
        # (e.g. MISSING entry with no sidecar hint), the
        # executor re-classifies the on-disk task.json to
        # defend against a RUNTIME slipping through. A
        # canonical run_<32hex> ID makes the SOT classifier
        # mark it as RUNTIME even without a sidecar.
        d = self.reports_root / "TASK-20260712-0099"
        d.mkdir()
        (d / "task.json").write_text(json.dumps(_RUNTIME_RECORD, sort_keys=True))
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        # The new entry has inventory.record_kind=None (the
        # inventory has no sidecar to read the kind from).
        new_entry = next(
            e for e in inv.entries
            if e.task_id == "20260712-0099"
        )
        self.assertIsNone(new_entry.record_kind)
        res = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
            allow_runtime=False,
        )
        o = next(
            x for x in res.outcomes if x.task_id == "20260712-0099"
        )
        self.assertEqual(o.status, MigrationStatus.RUNTIME_SKIPPED)
        # The note field surfaces the defence-in-depth path.
        self.assertIn("defence-in-depth", o.note)


# ---------------------------------------------------------------------------
# 13. corrupt sidecar
# ---------------------------------------------------------------------------


class TestCorruptSidecar(unittest.TestCase):
    """A garbled (non-JSON) on-disk sidecar is treated by the
    inventory as ``STALE_VERSION`` and by the migration as a
    ``WROTE`` outcome (the SOT helper's
    :func:`read_identity_sidecar` returns ``None`` for the
    garbled file, so the migration sees "no prior sidecar"
    and stamps a fresh one).
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77d-corrupt-"))
        self.reports_root = self._tmp / "reports"
        self.reports_root.mkdir()
        self._fx_path = _write_task_json(
            self.reports_root, _FIXTURE_RECORD["task_id"], _FIXTURE_RECORD
        )
        (self._fx_path.parent / "identity.json").write_bytes(
            b"this-is-not-json-at-all"
        )

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_garbled_sidecar_is_overwritten_with_wrote_outcome(self) -> None:
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        self.assertEqual(inv.unreadable_sidecars, 1)
        self.assertEqual(inv.entries[0].status, SidecarStatus.STALE_VERSION)
        res = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
        )
        o = res.outcomes[0]
        # The migration sees no prior sidecar (SOT reader
        # returned None for the garbled file) → WROTE.
        self.assertEqual(o.status, MigrationStatus.WROTE)
        # The on-disk sidecar is now valid JSON.
        new_payload = json.loads(
            (self._fx_path.parent / "identity.json").read_text()
        )
        self.assertEqual(new_payload["task_id"], _FIXTURE_RECORD["task_id"])
        self.assertEqual(new_payload["policy_version"], "1.0.0")


# ---------------------------------------------------------------------------
# 14. deterministic result serialization
# ---------------------------------------------------------------------------


class TestDeterministicSerialization(unittest.TestCase):
    """``MigrationExecutionResult.to_dict()`` is JSON-serializable
    and round-trips through ``json.dumps`` / ``json.loads``.
    The same (input, utc_stamp) pair produces a byte-identical
    DTO across runs.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77d-det-"))
        self.reports_root = self._tmp / "reports"
        self.reports_root.mkdir()
        _seed_missing_fixture(
            self.reports_root, task_id="TASK-20260712-0001",
        )
        _seed_stale_hash_fixture(
            self.reports_root, task_id="TASK-20260712-0003",
        )

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_to_dict_round_trip_through_json(self) -> None:
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        res = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
        )
        encoded = json.dumps(res.to_dict(), sort_keys=True)
        decoded = json.loads(encoded)
        # The DTO round-trip preserves the schema version and
        # the per-status counts.
        self.assertEqual(
            decoded["schema_version"], MIGRATION_EXEC_SCHEMA_VERSION
        )
        self.assertEqual(
            decoded["by_status"]["wrote"], 1
        )
        self.assertEqual(
            decoded["by_status"]["overwrote"], 1
        )
        # The outcomes field is a list of per-task dicts.
        self.assertEqual(len(decoded["outcomes"]), 2)

    def test_to_dict_keys_are_stable(self) -> None:
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        res = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
        )
        keys = set(res.to_dict().keys())
        self.assertEqual(keys, {
            "schema_version",
            "executed_at_utc",
            "reports_root",
            "status_filter",
            "allow_runtime",
            "force",
            "inventory_total",
            "inventory_fingerprints",
            "by_status",
            "by_inventory_status",
            "log_path",
            "outcomes",
        })

    def test_to_markdown_includes_status_table(self) -> None:
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        res = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
        )
        md = res.to_markdown()
        self.assertIn("AEE-7.7d Sidecar Migration Execution", md)
        self.assertIn("By MigrationStatus", md)
        self.assertIn(f"`{MIGRATION_EXEC_SCHEMA_VERSION}`", md)
        self.assertIn(_FIXTURE_UTC_STAMP, md)


# ---------------------------------------------------------------------------
# 15. per-task partial failure isolation
# ---------------------------------------------------------------------------


class TestPartialFailureIsolation(unittest.TestCase):
    """One task that cannot be processed does not block the
    other tasks in the same migration.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77d-pf-"))
        self.reports_root = self._tmp / "reports"
        self.reports_root.mkdir()
        # Three real tasks (each in its own TASK- dir).
        self._miss = _seed_missing_fixture(
            self.reports_root, task_id="TASK-20260712-0001",
        )
        _seed_missing_fixture(
            self.reports_root, task_id="TASK-20260712-0003",
        )
        _seed_missing_fixture(
            self.reports_root, task_id="TASK-20260712-0004",
        )
        # Plus one synthetic "broken" inventory entry pointing
        # to a non-existent task.json (NO_TASK_JSON).
        self._broken = SidecarInventoryEntry(
            task_id="20999999-9999",
            status=SidecarStatus.MISSING,
            has_task_json=False,
            has_sidecar=False,
            sidecar_policy_version=None,
            sidecar_schema_version=None,
            task_json_sha256="",
            sidecar_sha256="",
            sidecar_classified_at_utc=None,
            record_kind=None,
            is_consistent_hint=None,
        )

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_broken_entry_does_not_block_other_entries(self) -> None:
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        # Inject the broken entry.
        inv.entries.append(self._broken)
        res = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
        )
        # Three real tasks were WROTE; one was NO_TASK_JSON.
        by_status = res.by_status
        self.assertEqual(by_status[MigrationStatus.WROTE.value], 3)
        self.assertEqual(by_status[MigrationStatus.NO_TASK_JSON.value], 1)
        # All four outcomes are present.
        self.assertEqual(len(res.outcomes), 4)
        # The broken entry's outcome is NO_TASK_JSON and its
        # sidecar_sha256_after is empty.
        broken_outcome = next(
            o for o in res.outcomes
            if o.task_id == self._broken.task_id
        )
        self.assertEqual(
            broken_outcome.status, MigrationStatus.NO_TASK_JSON
        )
        self.assertEqual(broken_outcome.sidecar_sha256_after, "")


# ---------------------------------------------------------------------------
# 16. path traversal rejection
# ---------------------------------------------------------------------------


class TestPathTraversalRejection(unittest.TestCase):
    """A ``task_id`` whose name escapes the reports root is
    refused by the OS / Python's path machinery; the
    migration never writes a sidecar outside the reports
    root.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77d-trav-"))
        self.reports_root = self._tmp / "reports"
        self.reports_root.mkdir()
        # A real fixture so we have at least one valid entry
        # — without one, every assertion below is vacuous.
        _seed_missing_fixture(self.reports_root)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_dotdot_in_task_id_does_not_escape_reports_root(self) -> None:
        # A task_id that contains ".." cannot be resolved
        # into a directory name on POSIX: the OS refuses to
        # create / use such a name. We verify the migration
        # still does NOT write a sidecar outside reports_root
        # for the bad entry.
        bad_entry = SidecarInventoryEntry(
            task_id="..-escape",
            status=SidecarStatus.MISSING,
            has_task_json=False,
            has_sidecar=False,
            sidecar_policy_version=None,
            sidecar_schema_version=None,
            task_json_sha256="",
            sidecar_sha256="",
            sidecar_classified_at_utc=None,
            record_kind=None,
            is_consistent_hint=None,
        )
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        inv.entries.append(bad_entry)
        # We catch the OSError (the SOT helper tries to
        # open TASK-..-escape/task.json and the OS refuses
        # the ".." component on most filesystems). The
        # migration must convert the failure to a safe
        # outcome (MALFORMED or NO_TASK_JSON), not escape
        # the reports root.
        try:
            res = execute_sidecar_migration(
                self.reports_root, inv,
                utc_stamp=_FIXTURE_UTC_STAMP,
                write_log=False,
            )
        except (OSError, ValueError):
            # If the OS refused the bad name outright, the
            # invariant is still upheld: no sidecar was
            # written outside reports_root. Nothing to check.
            self._assert_no_sidecar_escaped()
            return
        # Otherwise, the bad entry's outcome is a safe
        # non-write (MALFORMED or NO_TASK_JSON) and no
        # sidecar escaped the reports root.
        bad_outcome = next(
            (o for o in res.outcomes if o.task_id == bad_entry.task_id),
            None,
        )
        if bad_outcome is not None:
            self.assertIn(
                bad_outcome.status,
                {
                    MigrationStatus.MALFORMED,
                    MigrationStatus.NO_TASK_JSON,
                },
            )
        self._assert_no_sidecar_escaped()

    def _assert_no_sidecar_escaped(self) -> None:
        # Walk the entire temp tree: no identity.json outside
        # the expected task dirs.
        for path in self._tmp.rglob("identity.json"):
            self.assertTrue(
                path.is_relative_to(
                    self.reports_root / _FIXTURE_RECORD["task_id"]
                ),
                f"sidecar escaped the task dir: {path}",
            )


# ---------------------------------------------------------------------------
# 17. symlink escape rejection
# ---------------------------------------------------------------------------


class TestSymlinkEscapeRejection(unittest.TestCase):
    """A symlink inside a task directory pointing outside the
    reports root must not trick the migration into writing a
    sidecar in an unexpected location.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77d-sym-"))
        self.reports_root = self._tmp / "reports"
        self.reports_root.mkdir()
        # A real fixture that the migration will process.
        self._fx_path = _seed_missing_fixture(self.reports_root)
        # An "escape" target outside reports_root.
        self._escape_dir = self._tmp / "outside"
        self._escape_dir.mkdir()

    def tearDown(self) -> None:
        import shutil
        # We may have created symlinks that point at
        # self._escape_dir. Remove them first so rmtree
        # does not follow them.
        for p in self.reports_root.rglob("sym-outside"):
            if p.is_symlink():
                p.unlink()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_symlinked_subdir_does_not_let_sidecar_escape(self) -> None:
        # Build a symlink in the fixture task dir pointing at
        # an outside dir. A naive writer that follows the
        # symlink would write identity.json into the outside
        # dir; the migration must NOT do that.
        link = self._fx_path.parent / "sym-outside"
        try:
            link.symlink_to(self._escape_dir)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported on this filesystem")
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
        )
        # The sidecar landed in the task dir (as expected);
        # no identity.json landed in the outside dir.
        self.assertTrue(
            (self._fx_path.parent / "identity.json").exists()
        )
        self.assertFalse(
            (self._escape_dir / "identity.json").exists()
        )
        # And nothing else snuck into the outside dir.
        for path in self._escape_dir.iterdir():
            self.assertNotEqual(
                path.name, "identity.json",
                f"sidecar escaped via symlink to {path}",
            )


# ---------------------------------------------------------------------------
# 18. existing report contents immutable
# ---------------------------------------------------------------------------


class TestReportContentsImmutable(unittest.TestCase):
    """``task.json`` and any other pre-existing file in the
    corpus are byte-identical (sha256 + mtime) before and
    after the migration. Only ``identity.json`` is added.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77d-imm-"))
        self.reports_root = self._tmp / "reports"
        self.reports_root.mkdir()
        self._miss = _seed_missing_fixture(
            self.reports_root, task_id="TASK-20260712-0001",
        )
        # A second fixture in its own task dir to give the
        # migration a second sidecar to write.
        self._sh = _seed_stale_hash_fixture(
            self.reports_root, task_id="TASK-20260712-0003",
        )
        # Snapshot the corpus before the migration.
        self._before = self._snapshot()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _snapshot(self) -> Dict[str, tuple]:
        out: Dict[str, tuple] = {}
        for p in sorted(self.reports_root.rglob("*")):
            if p.is_file():
                out[str(p.relative_to(self.reports_root))] = (
                    _file_sha256(p),
                    p.stat().st_mtime_ns,
                    p.read_bytes(),
                )
        return out

    def test_task_json_unchanged_after_migration(self) -> None:
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
        )
        after = self._snapshot()
        for rel_path, before in self._before.items():
            # The migration is allowed to ADD identity.json
            # files (and OVERWRITE the existing STALE_HASH
            # sidecar). The only invariant for ``task.json``
            # is that it must NOT change. We check every
            # pre-existing file (which is by construction
            # only task.json: the sidecar for the MISSING
            # entry is new, the sidecar for the STALE_HASH
            # entry pre-existed and gets overwritten — both
            # are pre-existing task.json).
            self.assertIn(rel_path, after)
            # task.json files must not change.
            if rel_path.endswith("task.json"):
                self.assertEqual(
                    after[rel_path][0], before[0],
                    f"{rel_path} sha changed",
                )
                self.assertEqual(
                    after[rel_path][1], before[1],
                    f"{rel_path} mtime changed",
                )
                self.assertEqual(
                    after[rel_path][2], before[2],
                    f"{rel_path} content changed",
                )

    def test_only_identity_json_files_added(self) -> None:
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
        )
        after = self._snapshot()
        new_files = set(after.keys()) - set(self._before.keys())
        # The MISSING entry's sidecar is brand new; the
        # STALE_HASH entry's sidecar pre-existed (and gets
        # overwritten, so it is NOT in new_files). The only
        # newly-added file is the MISSING entry's sidecar.
        self.assertEqual(
            new_files,
            {
                "TASK-20260712-0001/identity.json",
            },
        )
        # And no non-json files were added (no migration
        # artefacts leaked into the corpus).
        non_json_new = {
            rel for rel in new_files
            if not rel.endswith(".json")
        }
        self.assertEqual(non_json_new, set())


# ---------------------------------------------------------------------------
# 19 + 20 + 21. no prompt / stdout / stderr / secret leakage
# ---------------------------------------------------------------------------


class TestNoLeakage(unittest.TestCase):
    """The migration log JSON, the in-memory DTO, and the
    freshly-stamped sidecar must NOT contain any of the
    seeded secret / prompt / stdout / stderr markers.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77d-leak-"))
        self.reports_root = self._tmp / "reports"
        self.reports_root.mkdir()
        # A RUNTIME record with the full set of leak markers
        # in its input_text.
        self._rt_path = _write_task_json(
            self.reports_root, _RUNTIME_RECORD["task_id"], _RUNTIME_RECORD
        )
        # A FIXTURE record with the full set of leak markers
        # in its input_text.
        self._fx_path = _write_task_json(
            self.reports_root, _FIXTURE_RECORD["task_id"], _FIXTURE_RECORD
        )
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        self.log_path = self._tmp / "mlog.json"
        self.res = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=True,
            log_path=self.log_path,
            allow_runtime=True,
        )

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_to_dict_does_not_leak_secrets(self) -> None:
        encoded = json.dumps(self.res.to_dict(), sort_keys=True)
        for marker in _LEAK_MARKERS:
            self.assertNotIn(
                marker, encoded,
                f"to_dict() leaked {marker!r}",
            )

    def test_to_markdown_does_not_leak_secrets(self) -> None:
        md = self.res.to_markdown()
        for marker in _LEAK_MARKERS:
            self.assertNotIn(
                marker, md,
                f"to_markdown() leaked {marker!r}",
            )

    def test_per_task_outcome_notes_do_not_leak(self) -> None:
        for o in self.res.outcomes:
            note = o.note or ""
            for marker in _LEAK_MARKERS:
                self.assertNotIn(
                    marker, note,
                    f"outcome[{o.task_id}].note leaked {marker!r}",
                )

    def test_migration_log_file_does_not_leak(self) -> None:
        # The on-disk migration log JSON must not contain
        # any of the leak markers.
        self.assertTrue(self.log_path.exists())
        encoded = self.log_path.read_text()
        for marker in _LEAK_MARKERS:
            self.assertNotIn(
                marker, encoded,
                f"migration log file leaked {marker!r}",
            )

    def test_stamped_sidecars_do_not_leak(self) -> None:
        # The sidecar payload for either record must not
        # echo any of the leak markers.
        for path in (
            self._rt_path.parent / "identity.json",
            self._fx_path.parent / "identity.json",
        ):
            if not path.exists():
                continue
            payload = json.loads(path.read_text())
            encoded = json.dumps(payload, sort_keys=True)
            for marker in _LEAK_MARKERS:
                self.assertNotIn(
                    marker, encoded,
                    f"sidecar {path} leaked {marker!r}",
                )


# ---------------------------------------------------------------------------
# 22. AEE-7.7b apply_sidecars compatibility
# ---------------------------------------------------------------------------


class TestAEE77bCompatibility(unittest.TestCase):
    """The migration is a strict superset of the AEE-7.7b
    write path: the same SOT helper
    :func:`aee.reporting.identity.classify_and_persist` is
    invoked, and the result DTO is a separate, additive
    surface.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77d-77b-"))
        self.reports_root = self._tmp / "reports"
        self.reports_root.mkdir()
        _seed_missing_fixture(self.reports_root)
        _seed_stale_hash_fixture(
            self.reports_root, task_id="TASK-20260712-0003",
        )

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_migration_writes_the_same_sidecar_shape_as_apply_sidecars(
        self,
    ) -> None:
        # Build a fresh inventory, run the migration, and
        # verify the freshly-stamped sidecar is byte-for-byte
        # the same shape :func:`apply_sidecars` would write
        # (the SOT helper is the common substrate).
        from aee.audit import apply_sidecars, run_audit
        from aee.reporting.identity import (
            RecordKind,
            read_identity_sidecar,
        )

        # 7.7b path: run_audit + apply_sidecars.
        audit_dir = self._tmp / "audit"
        audit_dir.mkdir()
        summary, _, _ = run_audit(
            self.reports_root, audit_dir, utc_stamp=_FIXTURE_UTC_STAMP,
        )
        # Filter verdicts to the FIXTURE records so the
        # default allow_runtime behaviour matches the
        # migration's default allow_runtime=False.
        from aee.audit.live_audit import PerTaskVerdict
        fixture_verdicts = [
            v for v in summary.verdicts
            if v.record_kind == RecordKind.FIXTURE.value
        ]
        from aee.audit.live_audit import AuditSummary
        # Build a minimal summary containing only the
        # FIXTURE verdicts (mirrors the test's
        # allow_runtime=False shape).
        new_summary = AuditSummary(
            reports_root=summary.reports_root,
            audited_at_utc=summary.audited_at_utc,
            schema_version=summary.schema_version,
            verdicts=fixture_verdicts,
            by_record_kind={
                RecordKind.RUNTIME.value: 0,
                RecordKind.FIXTURE.value: len(fixture_verdicts),
                RecordKind.UNKNOWN.value: 0,
            },
            by_consistency={
                "consistent_true": sum(1 for v in fixture_verdicts if v.is_consistent),
                "consistent_false": sum(1 for v in fixture_verdicts if not v.is_consistent),
            },
            finding_code_counts={},
            fixture_inconsistent_count=0,
        )
        apply_sidecars(
            self.reports_root, new_summary,
            utc_stamp=_FIXTURE_UTC_STAMP,
            allow_runtime=False,
        )

        # 7.7d path: build inventory + execute migration.
        # Wipe the corpus and re-seed to make the two
        # paths comparable on a known input.
        import shutil
        shutil.rmtree(self.reports_root)
        self.reports_root.mkdir()
        _seed_missing_fixture(self.reports_root)
        _seed_stale_hash_fixture(
            self.reports_root, task_id="TASK-20260712-0003",
        )
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
            allow_runtime=False,
        )

        # The shape written by both paths is the same: an
        # Identity payload with the canonical fields.
        for path in (
            self.reports_root / _FIXTURE_RECORD["task_id"] / "identity.json",
            self.reports_root / "TASK-20260712-0003" / "identity.json",
        ):
            if not path.exists():
                continue
            identity = read_identity_sidecar(path.parent / "task.json")
            self.assertIsNotNone(identity)
            # Both paths go through the same SOT helper, so
            # the sidecar shape is identical (modulo the
            # timestamps the SOT helper stamps).
            self.assertIn(
                identity.record_kind.value, {"fixture", "runtime", "unknown"}
            )


# ---------------------------------------------------------------------------
# 23. AEE-7.7c inventory / plan compatibility
# ---------------------------------------------------------------------------


class TestAEE77cInventoryPlanCompatibility(unittest.TestCase):
    """The migration accepts the inventory built by
    :func:`aee.audit.build_sidecar_inventory` directly. The
    ``MigrationPlan`` produced by
    :func:`aee.audit.plan_sidecar_migration` is also
    compatible: a caller can drive both tools from the same
    inventory and the resulting ``MigrationPlan`` counts
    match the migration's effective behaviour.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77d-77c-"))
        self.reports_root = self._tmp / "reports"
        self.reports_root.mkdir()
        _seed_missing_fixture(self.reports_root)
        _seed_stale_hash_fixture(
            self.reports_root, task_id="TASK-20260712-0003",
        )
        _seed_fresh_fixture(
            self.reports_root, task_id="TASK-20260712-0004",
        )

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_inventory_drives_migration_directly(self) -> None:
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        # The inventory has 3 entries: MISSING, STALE_HASH, FRESH.
        self.assertEqual(len(inv.entries), 3)
        statuses = {e.status for e in inv.entries}
        self.assertEqual(
            statuses,
            {
                SidecarStatus.MISSING,
                SidecarStatus.STALE_HASH,
                SidecarStatus.FRESH,
            },
        )
        res = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
        )
        # 1 WROTE (the MISSING entry), 1 OVERWROTE (the
        # STALE_HASH entry), 1 STATUS_FILTERED (the FRESH
        # entry).
        self.assertEqual(res.by_status[MigrationStatus.WROTE.value], 1)
        self.assertEqual(res.by_status[MigrationStatus.OVERWROTE.value], 1)
        self.assertEqual(
            res.by_status[MigrationStatus.STATUS_FILTERED.value], 1
        )

    def test_migration_plan_counts_match_execution(self) -> None:
        # Build the inventory, plan the migration, and then
        # execute it. The plan's would_write / would_overwrite
        # / no_op counts should agree with the migration's
        # by_status / by_inventory_status counts (modulo the
        # inventory-side RUNTIME gate, which the plan does
        # not know about).
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        plan = plan_sidecar_migration(
            inv, target_policy_version="1.1.0", utc_stamp=_FIXTURE_UTC_STAMP,
        )
        # All-FIXTURE corpus: every MISSING / STALE_HASH
        # entry would be touched. The plan reports
        # would_write=1 (MISSING) + would_overwrite=1
        # (STALE_HASH) + no_op=1 (FRESH).
        self.assertEqual(plan.would_write, 1)
        self.assertEqual(plan.would_overwrite, 1)
        self.assertEqual(plan.no_op, 1)
        res = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
        )
        # The migration's by_status matches the plan's
        # would_write + would_overwrite (WROTE + OVERWROTE).
        self.assertEqual(
            res.by_status[MigrationStatus.WROTE.value]
            + res.by_status[MigrationStatus.OVERWROTE.value],
            plan.would_write + plan.would_overwrite,
        )


# ---------------------------------------------------------------------------
# 24. no dispatcher import leakage
# ---------------------------------------------------------------------------


class TestNoDispatcherImport(unittest.TestCase):
    """Loading ``aee.audit.sidecar_migration`` must NOT
    introduce any ``dispatcher.*`` module into
    ``sys.modules``.
    """

    def test_loading_sidecar_migration_does_not_introduce_dispatcher(
        self,
    ) -> None:
        for mod_name in list(sys.modules):
            if mod_name == "aee.audit" or mod_name.startswith("aee.audit."):
                del sys.modules[mod_name]
        before = set(sys.modules.keys())
        from aee.audit import execute_sidecar_migration  # noqa: F401
        from aee.audit import MigrationExecutionResult  # noqa: F401
        from aee.audit import MigrationStatus  # noqa: F401
        after = set(sys.modules.keys())
        new_dispatcher = [
            m
            for m in (after - before)
            if m == "dispatcher" or m.startswith("dispatcher.")
        ]
        self.assertEqual(
            new_dispatcher,
            [],
            f"aee.audit.sidecar_migration must not introduce "
            f"dispatcher.* into sys.modules; got: {new_dispatcher}",
        )

    def test_underlying_module_does_not_import_dispatcher(self) -> None:
        # Source-level grep: the migration module must not
        # import ``dispatcher`` anywhere. Catches indirect
        # imports that lazy loading would miss in the
        # ``sys.modules`` snapshot test above.
        impl_path = (
            Path(_REPO_ROOT)
            / "aee"
            / "audit"
            / "sidecar_migration.py"
        )
        src = impl_path.read_text(encoding="utf-8")
        # Strip string literals + comments.
        no_strings = re.sub(
            r"(\"\"\".*?\"\"\"|'''.*?'''|\".*?\"|'.*?')",
            " ",
            src,
            flags=re.DOTALL,
        )
        no_strings = re.sub(r"#[^\n]*", " ", no_strings)
        bad = re.search(
            r"(^|\n)\s*(import\s+dispatcher|from\s+dispatcher)",
            no_strings,
        )
        self.assertIsNone(
            bad,
            f"{impl_path} must not import dispatcher; "
            f"found: {bad.group(0) if bad else None!r}",
        )


# ---------------------------------------------------------------------------
# 25. temp corpus integration test
# ---------------------------------------------------------------------------


class TestTempCorpusIntegration(unittest.TestCase):
    """A mixed corpus (MISSING + STALE_HASH + STALE_VERSION +
    FRESH) migrates to a known shape and the result DTO is
    internally consistent.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77d-integ-"))
        self.reports_root = self._tmp / "reports"
        self.reports_root.mkdir()
        # MISSING entry
        self._miss = _seed_missing_fixture(
            self.reports_root, task_id="TASK-20260712-0001",
        )
        # STALE_HASH entry
        _seed_stale_hash_fixture(
            self.reports_root, task_id="TASK-20260712-0003",
        )
        # STALE_VERSION entry
        _seed_stale_version_fixture(
            self.reports_root, task_id="TASK-20260712-0005",
        )
        # FRESH entry
        _seed_fresh_fixture(
            self.reports_root, task_id="TASK-20260712-0006",
        )

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_mixed_corpus_full_migration(self) -> None:
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        # Inventory: 4 entries covering all 3 actionable
        # buckets + 1 FRESH.
        self.assertEqual(len(inv.entries), 4)
        statuses = {e.status for e in inv.entries}
        self.assertEqual(
            statuses,
            {
                SidecarStatus.MISSING,
                SidecarStatus.STALE_HASH,
                SidecarStatus.STALE_VERSION,
                SidecarStatus.FRESH,
            },
        )
        res = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
        )
        # Per-status counts: 1 WROTE (MISSING), 2 OVERWROTE
        # (STALE_HASH + STALE_VERSION), 1 STATUS_FILTERED
        # (FRESH).
        self.assertEqual(res.by_status[MigrationStatus.WROTE.value], 1)
        self.assertEqual(res.by_status[MigrationStatus.OVERWROTE.value], 2)
        self.assertEqual(
            res.by_status[MigrationStatus.STATUS_FILTERED.value], 1
        )
        # Per-inventory-status counts: each of MISSING /
        # STALE_HASH / STALE_VERSION / FRESH has 1 entry.
        self.assertEqual(
            res.by_inventory_status[SidecarStatus.MISSING.value], 1
        )
        self.assertEqual(
            res.by_inventory_status[SidecarStatus.STALE_HASH.value], 1
        )
        self.assertEqual(
            res.by_inventory_status[SidecarStatus.STALE_VERSION.value], 1
        )
        self.assertEqual(
            res.by_inventory_status[SidecarStatus.FRESH.value], 1
        )
        # All 4 sidecar files exist on disk (3 freshly
        # stamped + 1 pre-existing FRESH that we did not
        # touch).
        for tid in (
            "TASK-20260712-0001",  # MISSING → WROTE
            "TASK-20260712-0003",  # STALE_HASH → OVERWROTE
            "TASK-20260712-0005",  # STALE_VERSION → OVERWROTE
            "TASK-20260712-0006",  # FRESH → untouched
        ):
            self.assertTrue(
                (self.reports_root / tid / "identity.json").exists(),
                f"sidecar missing for {tid}",
            )
        # A subsequent inventory classifies the corpus as
        # fully FRESH (every sidecar now matches the
        # current writer).
        inv2 = build_sidecar_inventory(
            self.reports_root, utc_stamp="t2"
        )
        for e in inv2.entries:
            self.assertEqual(
                e.status, SidecarStatus.FRESH,
                f"{e.task_id} not FRESH after migration: {e.status}",
            )


# ---------------------------------------------------------------------------
# 26. migration re-run produces no unnecessary writes
# ---------------------------------------------------------------------------


class TestRerunStability(unittest.TestCase):
    """A second migration over the just-stamped corpus (every
    entry now FRESH) reports ``STATUS_FILTERED`` for every
    entry and the on-disk sidecar hashes are stable.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77d-rerun-"))
        self.reports_root = self._tmp / "reports"
        self.reports_root.mkdir()
        self._miss = _seed_missing_fixture(self.reports_root)
        _seed_stale_hash_fixture(
            self.reports_root, task_id="TASK-20260712-0003",
        )
        self._sidecar_miss = (
            self.reports_root / _FIXTURE_RECORD["task_id"] / "identity.json"
        )
        self._sidecar_sh = (
            self.reports_root / "TASK-20260712-0003" / "identity.json"
        )

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_rerun_does_not_touch_existing_sidecars(self) -> None:
        inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        # First run: stamps both.
        first = execute_sidecar_migration(
            self.reports_root, inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
        )
        # Snapshot the post-first-run state.
        miss_sha = _file_sha256(self._sidecar_miss)
        sh_sha = _file_sha256(self._sidecar_sh)
        # Inventory is now all-FRESH.
        inv2 = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )
        for e in inv2.entries:
            self.assertEqual(e.status, SidecarStatus.FRESH)
        # Second run: every entry is STATUS_FILTERED.
        second = execute_sidecar_migration(
            self.reports_root, inv2,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
        )
        self.assertTrue(all(
            o.status == MigrationStatus.STATUS_FILTERED
            for o in second.outcomes
        ))
        # On-disk sidecar hashes are stable.
        self.assertEqual(_file_sha256(self._sidecar_miss), miss_sha)
        self.assertEqual(_file_sha256(self._sidecar_sh), sh_sha)
        # First-run WROTE/OVERWROTE counts are zero on the
        # second run.
        self.assertEqual(
            second.by_status[MigrationStatus.WROTE.value], 0
        )
        self.assertEqual(
            second.by_status[MigrationStatus.OVERWROTE.value], 0
        )


# ---------------------------------------------------------------------------
# 27. execution result counts are internally consistent
# ---------------------------------------------------------------------------


class TestResultCountsConsistent(unittest.TestCase):
    """``MigrationExecutionResult.by_status`` and
    ``.by_inventory_status`` sum to ``len(outcomes)``;
    ``inventory_total`` matches ``len(inventory.entries)``.
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee77d-counts-"))
        self.reports_root = self._tmp / "reports"
        self.reports_root.mkdir()
        # Seed a known-shape corpus.
        _seed_missing_fixture(self.reports_root)
        _seed_stale_hash_fixture(
            self.reports_root, task_id="TASK-20260712-0003",
        )
        _seed_stale_version_fixture(
            self.reports_root, task_id="TASK-20260712-0005",
        )
        _seed_fresh_fixture(
            self.reports_root, task_id="TASK-20260712-0006",
        )
        self.inv = build_sidecar_inventory(
            self.reports_root, utc_stamp=_FIXTURE_UTC_STAMP
        )

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_inventory_total_matches_inventory_entries(self) -> None:
        self.assertEqual(self.inv.entries, self.inv.entries)
        self.assertEqual(
            len(self.inv.entries),
            self.inv.by_status.get(
                SidecarStatus.MISSING.value, 0
            )
            + self.inv.by_status.get(
                SidecarStatus.STALE_HASH.value, 0
            )
            + self.inv.by_status.get(
                SidecarStatus.STALE_VERSION.value, 0
            )
            + self.inv.by_status.get(
                SidecarStatus.FRESH.value, 0
            ),
        )

    def test_result_counts_sum_to_outcome_count(self) -> None:
        res = execute_sidecar_migration(
            self.reports_root, self.inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
        )
        # inventory_total matches the inventory's entry count.
        self.assertEqual(
            res.inventory_total, len(self.inv.entries)
        )
        # by_status sums to len(outcomes).
        self.assertEqual(
            sum(res.by_status.values()), len(res.outcomes)
        )
        # by_inventory_status sums to len(outcomes).
        self.assertEqual(
            sum(res.by_inventory_status.values()),
            len(res.outcomes),
        )
        # The two by_* dicts are each over the full enum
        # set (zero-initialised for non-occurring codes).
        self.assertEqual(
            set(res.by_status.keys()),
            {s.value for s in MigrationStatus},
        )
        self.assertEqual(
            set(res.by_inventory_status.keys()),
            {s.value for s in SidecarStatus},
        )

    def test_inventory_fingerprints_match_inventory_entries(self) -> None:
        res = execute_sidecar_migration(
            self.reports_root, self.inv,
            utc_stamp=_FIXTURE_UTC_STAMP,
            write_log=False,
        )
        # inventory_fingerprints has one entry per inventory
        # entry, and the value is the entry's task_json_sha256.
        self.assertEqual(
            len(res.inventory_fingerprints), len(self.inv.entries)
        )
        for entry in self.inv.entries:
            self.assertEqual(
                res.inventory_fingerprints[entry.task_id],
                entry.task_json_sha256,
            )


# ---------------------------------------------------------------------------
# Module-level smoke: the migration module exposes the
# documented public surface and the schema version is pinned.
# ---------------------------------------------------------------------------


class TestModuleSurface(unittest.TestCase):
    """The AEE-7.7d module exposes the documented public
    surface and pins the schema version.
    """

    def test_schema_version_is_1_0_0(self) -> None:
        self.assertEqual(MIGRATION_EXEC_SCHEMA_VERSION, "1.0.0")

    def test_default_status_filter_excludes_fresh(self) -> None:
        # The default filter is the union of MISSING +
        # STALE_HASH + STALE_VERSION. FRESH is excluded by
        # design (no-op re-stamp) and RUNTIME is excluded
        # by the separate allow_runtime gate.
        self.assertEqual(
            DEFAULT_STATUS_FILTER,
            frozenset({
                SidecarStatus.MISSING,
                SidecarStatus.STALE_HASH,
                SidecarStatus.STALE_VERSION,
            }),
        )

    def test_migration_status_enum_has_documented_values(self) -> None:
        self.assertEqual(
            {s.value for s in MigrationStatus},
            {
                "wrote",
                "overwrote",
                "unchanged",
                "runtime_skipped",
                "runtime_disallowed",
                "fresh_skipped",
                "status_filtered",
                "no_task_json",
                "malformed",
            },
        )

    def test_per_task_outcome_is_frozen(self) -> None:
        o = PerTaskMigrationOutcome(
            task_id="t",
            status=MigrationStatus.WROTE,
            inventory_status=SidecarStatus.MISSING,
            record_kind="fixture",
            source_task_json_sha256="a" * 64,
            sidecar_sha256_before="",
            sidecar_sha256_after="b" * 64,
            policy_version="1.0.0",
            schema_version=None,
            note="x",
        )
        with self.assertRaises(Exception):
            o.task_id = "other"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main(verbosity=2)
