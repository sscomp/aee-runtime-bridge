"""AEE-7.7e — live-corpus migration dry-run + projection tests.

This test module covers the AEE-7.7e read-only
:func:`aee.audit.run_live_migration_dryrun` entry point, the
pure-projection helper :func:`aee.audit.project_migration_execution`,
the explicit apply path :func:`aee.audit.run_live_migration_apply`,
and the companion :class:`LiveMigrationDryrunResult` /
:class:`ProjectedMigrationResult` DTOs.

The headline invariant: **a dry-run must not mutate the
corpus**. The safety tests assert, end-to-end, that across a
dry-run invocation the following are byte-stable for every
TASK-* directory under the test corpus:

* the set of identity.json paths
* the SHA-256 of every identity.json
* the byte size of every identity.json
* the mtime of every identity.json
* the SHA-256 of every task.json
* the mtime of every task.json
* the set of task.json paths
* no new files appear inside the reports/ tree
* no migration_log_<UTC>.json file is created

The projection tests verify that the pure-function
:func:`project_migration_execution` produces the expected
outcomes for stale-hash, stale-version, missing, runtime,
fresh, and corrupt entries, and that it never touches
disk. The apply path tests verify the AEE-7.7d executor is
still reachable through the new :func:`run_live_migration_apply`
entry point (so the existing AEE-7.7d tests still work
unchanged).
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Make ``aee`` importable when running via ``python -m unittest
# aee.tests.test_aee77e_live_migration_dryrun`` from outside
# the repo root.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


from aee.audit import (  # noqa: E402
    DEFAULT_STATUS_FILTER,
    DEFAULT_TARGET_POLICY_VERSION,
    LIVE_MIGRATION_DRYRUN_SCHEMA_VERSION,
    MigrationStatus,
    ProjectedMigrationResult,
    ProjectedOutcome,
    SidecarStatus,
    build_sidecar_inventory,
    plan_sidecar_migration,
    project_migration_execution,
    run_live_migration_apply,
    run_live_migration_dryrun,
)


# Canonical record fixtures (mirror AEE-7.7d)
_RUNTIME_RECORD: Dict[str, Any] = {
    "task_id": "TASK-20260712-9001",
    "title": "aee7.7e RUNTIME smoke",
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
    "executor_session_id": "AEE-7.7E-RUNTIME-20260712",
    "runtime_run_id": "run_0123456789abcdef0123456789abcdef",
    "repo_root": "/home/ubuntu/hermes-runtime-bridge",
}

_FIXTURE_RECORD: Dict[str, Any] = {
    "task_id": "TASK-20260712-9002",
    "title": "aee7.7e FIXTURE smoke",
    "type": "normal",
    "priority": 50,
    "owner": "m2",
    "status": "running",
    "progress_pct": 5,
    "progress_step": None,
    "created_at": "2026-07-12T10:00:00.000Z",
    "started_at": "2026-07-12T10:00:00.001Z",
    "finished_at": None,
    "duration_sec": None,
    "input_text": "marker_fixture_input sk-secret-fixture-do-not-leak",
    "hermes_run_id": "r_77e_fixture",
    "openai_run_id": None,
    "session_id": "s_77e_fixture",
    "mode": None,
    "result_path": None,
    "error_message": None,
    "warning_count": 0,
    "retry_count": 0,
    "prompt_version": None,
    "model_name": None,
    "git_commit": None,
    "git_branch": None,
    "output_excerpt": "ok",
    "usage": {"input_tokens": 1, "output_tokens": 1},
    "input_tokens": 1,
    "output_tokens": 1,
}

# Patterns the leakage tripwire scans for. Matches the
# AEE-7.7d test pattern: any real secret in the DTO,
# manifest, or markdown is a hard fail.
_LEAK_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{12,}"),
    re.compile(r"ghp_[A-Za-z0-9]{16,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"eyJ[A-Za-z0-9_=+\-/]{20,}\.[A-Za-z0-9_=+\-/]{20,}"),
    re.compile(r"HW[A-Z0-9]{20,}"),  # hermes-key-literal
    re.compile(r"BW[A-Z0-9]{20,}"),  # bridge-key-literal
    re.compile(r"PROMPT-FROM-USER-12345"),
    re.compile(r"sk-secret-runtime-do-not-leak"),
    re.compile(r"sk-secret-fixture-do-not-leak"),
]

# Deterministic UTC stamp for stable test output.
_FIXTURE_UTC_STAMP = "2026-07-12T13:30:00Z"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _file_sha256(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_size(path: Path) -> int:
    if not path.exists() or not path.is_file():
        return 0
    return path.stat().st_size


def _file_mtime_ns(path: Path) -> int:
    if not path.exists() or not path.is_file():
        return 0
    return path.stat().st_mtime_ns


def _snapshot_corpus(root: Path) -> Dict[str, Any]:
    """Capture the full corpus fingerprint for byte-stability
    assertions.
    """
    sidecars: Dict[str, Dict[str, Any]] = {}
    task_jsons: Dict[str, Dict[str, Any]] = {}
    for task_dir in sorted(root.iterdir()):
        if not task_dir.is_dir() or not task_dir.name.startswith("TASK-"):
            continue
        sc = task_dir / "identity.json"
        if sc.is_file():
            sidecars[task_dir.name] = {
                "sha256": _file_sha256(sc),
                "size": _file_size(sc),
                "mtime_ns": _file_mtime_ns(sc),
            }
        tj = task_dir / "task.json"
        if tj.is_file():
            task_jsons[task_dir.name] = {
                "sha256": _file_sha256(tj),
                "size": _file_size(tj),
                "mtime_ns": _file_mtime_ns(tj),
            }
    return {
        "sidecars": sidecars,
        "task_jsons": task_jsons,
    }


def _write_task_json(
    root: Path,
    task_id: str,
    payload: Dict[str, Any],
) -> Path:
    d = root / task_id
    d.mkdir(parents=True, exist_ok=True)
    p = d / "task.json"
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    return p


def _fresh_sidecar_for(task_id: str, source_hash: str) -> Dict[str, Any]:
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
    return {
        "task_id": task_id,
        "classified_at_utc": "2026-07-12T00:00:00Z",
        "executor_session_id": None,
        "fixture_markers": [],
        "is_fixture": True,
        "policy_version": "0.9.0",
        "record_kind": "fixture",
        "runtime_run_id": None,
        "source_task_json_sha256": "0000" * 16,  # arbitrary, hash mismatches
        "user_provided_alias": None,
    }


def _write_sidecar(task_json_path: Path, payload: Dict[str, Any]) -> Path:
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


def _build_fresh_corpus(root: Path) -> Path:
    fresh_payload = dict(_FIXTURE_RECORD)
    fresh_payload["task_id"] = "TASK-20260101-0001"
    p = _write_task_json(root, "TASK-20260101-0001", fresh_payload)
    h = _file_sha256(p)
    _write_sidecar(p, _fresh_sidecar_for("TASK-20260101-0001", h))
    return p


def _build_missing_corpus(root: Path) -> Path:
    fresh_payload = dict(_FIXTURE_RECORD)
    fresh_payload["task_id"] = "TASK-20260101-0002"
    p = _write_task_json(root, "TASK-20260101-0002", fresh_payload)
    return p


def _build_stale_hash_corpus(root: Path) -> Path:
    fresh_payload = dict(_FIXTURE_RECORD)
    fresh_payload["task_id"] = "TASK-20260101-0003"
    p = _write_task_json(root, "TASK-20260101-0003", fresh_payload)
    _write_sidecar(p, _fresh_sidecar_for(
        "TASK-20260101-0003", "deadbeef" * 8,
    ))
    return p


def _build_stale_version_corpus(root: Path) -> Path:
    fresh_payload = dict(_FIXTURE_RECORD)
    fresh_payload["task_id"] = "TASK-20260101-0004"
    p = _write_task_json(root, "TASK-20260101-0004", fresh_payload)
    h = _file_sha256(p)
    sc = _fresh_sidecar_for("TASK-20260101-0004", h)
    sc["policy_version"] = "0.9.0"
    _write_sidecar(p, sc)
    return p


def _build_runtime_corpus(root: Path) -> Path:
    fresh_payload = dict(_RUNTIME_RECORD)
    fresh_payload["task_id"] = "TASK-20260101-0005"
    p = _write_task_json(root, "TASK-20260101-0005", fresh_payload)
    return p


def _scan_for_secrets(blob: str) -> List[Tuple[str, str]]:
    """Return a list of (pattern, match) tuples for any leaked
    secret in the input string. Empty list = no leaks.
    """
    hits: List[Tuple[str, str]] = []
    for pat in _LEAK_PATTERNS:
        m = pat.search(blob)
        if m:
            hits.append((pat.pattern, m.group(0)))
    return hits


# ---------------------------------------------------------------------------
# 1. Schema version + DTO shape
# ---------------------------------------------------------------------------


class TestSchemaVersion(unittest.TestCase):
    def test_schema_version_is_1_1_0(self):
        self.assertEqual(LIVE_MIGRATION_DRYRUN_SCHEMA_VERSION, "1.1.0")

    def test_default_target_policy_version_is_1_1_0(self):
        self.assertEqual(DEFAULT_TARGET_POLICY_VERSION, "1.1.0")

    def test_dto_is_frozen(self):
        from dataclasses import FrozenInstanceError
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            res = run_live_migration_dryrun(root, utc_stamp=_FIXTURE_UTC_STAMP)
            with self.assertRaises(FrozenInstanceError):
                res.utc_stamp = "hacked"  # type: ignore[misc]

    def test_dto_has_projection_field(self):
        """v1.1.0 DTO carries a ``projection`` field (replaces the
        v1.0.0 ``execution`` field).
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            res = run_live_migration_dryrun(root, utc_stamp=_FIXTURE_UTC_STAMP)
            d = res.to_dict()
            # New field present
            self.assertIn("projection", d)
            # Old field removed at the top level (replaced by projection)
            self.assertNotIn("execution", d)
            # Deprecated aliases are present, marked DEPRECATED.
            self.assertIn("deprecated_exec_aliases", d)
            self.assertIn(
                "DEPRECATED", d["deprecated_exec_aliases"]
            )

    def test_dto_reconciliation_block_uses_projected_names(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            res = run_live_migration_dryrun(root, utc_stamp=_FIXTURE_UTC_STAMP)
            recon = res.to_dict()["reconciliation"]
            for f in (
                "projected_writes", "projected_overwrites",
                "projected_skips", "projected_runtime_skipped",
                "projected_filtered", "projected_no_task_json",
                "projected_malformed", "projected_no_op",
                "projected_total", "passed",
                "plan_would_write", "plan_would_overwrite",
                "plan_no_op", "plan_runtime_would_touch",
            ):
                self.assertIn(f, recon)


# ---------------------------------------------------------------------------
# 2. Empty / no-op cases
# ---------------------------------------------------------------------------


class TestEmptyCorpus(unittest.TestCase):
    def test_empty_reports_root_returns_empty_dto(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            res = run_live_migration_dryrun(
                root, utc_stamp=_FIXTURE_UTC_STAMP
            )
            self.assertEqual(len(res.inventory.entries), 0)
            self.assertEqual(res.plan.would_write, 0)
            self.assertEqual(res.plan.would_overwrite, 0)
            self.assertEqual(res.plan.no_op, 0)
            self.assertEqual(res.projected_total, 0)
            self.assertTrue(res.reconciliation_passed)
            self.assertIsNone(res.manifest_path)
            self.assertEqual(res.manifest_sha256, "")

    def test_nonexistent_reports_root_is_handled(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "does-not-exist"
            res = run_live_migration_dryrun(
                root, utc_stamp=_FIXTURE_UTC_STAMP
            )
            self.assertEqual(len(res.inventory.entries), 0)
            self.assertTrue(res.reconciliation_passed)


# ---------------------------------------------------------------------------
# 3. Mixed corpus (all 4 inventory statuses)
# ---------------------------------------------------------------------------


class TestMixedCorpus(unittest.TestCase):
    def test_fresh_miss_hash_stale_all_present(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_fresh_corpus(root)
            _build_missing_corpus(root)
            _build_stale_hash_corpus(root)
            _build_stale_version_corpus(root)
            res = run_live_migration_dryrun(
                root, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            self.assertEqual(len(res.inventory.entries), 4)
            self.assertEqual(res.inventory.by_status.get("fresh"), 1)
            self.assertEqual(res.inventory.by_status.get("missing"), 1)
            self.assertEqual(res.inventory.by_status.get("stale_hash"), 1)
            self.assertEqual(res.inventory.by_status.get("stale_version"), 1)
            # Plan shape (target 1.1.0, current 1.0.0)
            self.assertEqual(res.plan.would_write, 1)  # MISSING
            self.assertEqual(res.plan.would_overwrite, 2)  # STALE_HASH + STALE_VERSION
            self.assertEqual(res.plan.no_op, 1)  # FRESH
            # Projection shape
            self.assertEqual(res.projected_writes, 1)  # MISSING → WOULD_WRITE
            self.assertEqual(res.projected_overwrites, 2)  # STALE_* → WOULD_OVERWRITE
            self.assertEqual(res.projected_skips, 1)  # FRESH → WOULD_SKIP_CURRENT
            self.assertEqual(res.projected_total, 4)
            # Reconciliation
            self.assertTrue(res.reconciliation_passed)


# ---------------------------------------------------------------------------
# 4. Projected outcome correctness
# ---------------------------------------------------------------------------


class TestProjectionOutcomes(unittest.TestCase):
    def test_stale_hash_entry_would_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_stale_hash_corpus(root)
            inv = build_sidecar_inventory(root, utc_stamp=_FIXTURE_UTC_STAMP)
            proj = project_migration_execution(inv, utc_stamp=_FIXTURE_UTC_STAMP)
            self.assertEqual(len(proj.per_task), 1)
            self.assertEqual(
                proj.per_task[0].outcome, ProjectedOutcome.WOULD_OVERWRITE,
            )
            self.assertTrue(proj.per_task[0].would_change_sidecar)

    def test_stale_version_entry_would_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_stale_version_corpus(root)
            inv = build_sidecar_inventory(root, utc_stamp=_FIXTURE_UTC_STAMP)
            proj = project_migration_execution(inv, utc_stamp=_FIXTURE_UTC_STAMP)
            self.assertEqual(
                proj.per_task[0].outcome, ProjectedOutcome.WOULD_OVERWRITE,
            )

    def test_missing_entry_would_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_missing_corpus(root)
            inv = build_sidecar_inventory(root, utc_stamp=_FIXTURE_UTC_STAMP)
            proj = project_migration_execution(inv, utc_stamp=_FIXTURE_UTC_STAMP)
            self.assertEqual(
                proj.per_task[0].outcome, ProjectedOutcome.WOULD_WRITE,
            )
            self.assertTrue(proj.per_task[0].would_change_sidecar)

    def test_fresh_entry_would_skip_current(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_fresh_corpus(root)
            inv = build_sidecar_inventory(root, utc_stamp=_FIXTURE_UTC_STAMP)
            proj = project_migration_execution(inv, utc_stamp=_FIXTURE_UTC_STAMP)
            self.assertEqual(
                proj.per_task[0].outcome, ProjectedOutcome.WOULD_SKIP_CURRENT,
            )
            self.assertFalse(proj.per_task[0].would_change_sidecar)

    def test_runtime_entry_would_skip_runtime(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_runtime_corpus(root)
            inv = build_sidecar_inventory(root, utc_stamp=_FIXTURE_UTC_STAMP)
            proj = project_migration_execution(
                inv, allow_runtime=False, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            self.assertEqual(
                proj.per_task[0].outcome, ProjectedOutcome.WOULD_SKIP_RUNTIME,
            )
            self.assertFalse(proj.per_task[0].would_change_sidecar)

    def test_allow_runtime_true_runtime_would_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_runtime_corpus(root)
            inv = build_sidecar_inventory(root, utc_stamp=_FIXTURE_UTC_STAMP)
            proj = project_migration_execution(
                inv, allow_runtime=True, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            self.assertEqual(
                proj.per_task[0].outcome, ProjectedOutcome.WOULD_WRITE,
            )

    def test_empty_status_filter_results_in_would_filter(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_missing_corpus(root)
            _build_stale_hash_corpus(root)
            inv = build_sidecar_inventory(root, utc_stamp=_FIXTURE_UTC_STAMP)
            proj = project_migration_execution(
                inv, status_filter=frozenset(),
                utc_stamp=_FIXTURE_UTC_STAMP,
            )
            for p in proj.per_task:
                self.assertEqual(p.outcome, ProjectedOutcome.WOULD_FILTER)
                self.assertFalse(p.would_change_sidecar)

    def test_force_true_only_changes_projection(self):
        """force=True only changes the projection for entries
        whose inventory status is in the filter (MISSING /
        STALE_*). FRESH entries are short-circuited to
        WOULD_SKIP_CURRENT before the force check, mirroring
        the AEE-7.7d executor's defence-in-depth behaviour.
        For STALE_HASH entries, force=True flips the
        projection from WOULD_OVERWRITE (idempotent) — well,
        actually for a STALE_HASH entry the sidecar mismatch
        already makes it WOULD_OVERWRITE; force=True is the
        same here. We use a fresh + force=True case to
        document the FRESH short-circuit and a stale_hash +
        force=False/True case to show the sidecar SHA still
        matches on disk in both cases.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_stale_hash_corpus(root)
            inv = build_sidecar_inventory(root, utc_stamp=_FIXTURE_UTC_STAMP)
            # Both force=False and force=True → WOULD_OVERWRITE
            # (STALE_HASH sidecar mismatch is already decisive).
            proj_no_force = project_migration_execution(
                inv, force=False, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            self.assertEqual(
                proj_no_force.per_task[0].outcome,
                ProjectedOutcome.WOULD_OVERWRITE,
            )
            proj_force = project_migration_execution(
                inv, force=True, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            self.assertEqual(
                proj_force.per_task[0].outcome,
                ProjectedOutcome.WOULD_OVERWRITE,
            )
            # NEITHER touched disk: still a stale_hash sidecar
            # with the original source-task mismatch.
            sc = root / "TASK-20260101-0003" / "identity.json"
            self.assertTrue(sc.is_file())
            content = json.loads(sc.read_text())
            self.assertEqual(
                content["source_task_json_sha256"],
                "deadbeef" * 8,
            )

        # And a fresh entry: force=True still short-circuits
        # to WOULD_SKIP_CURRENT (FRESH defence-in-depth).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_fresh_corpus(root)
            inv = build_sidecar_inventory(root, utc_stamp=_FIXTURE_UTC_STAMP)
            proj = project_migration_execution(
                inv, force=True, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            self.assertEqual(
                proj.per_task[0].outcome,
                ProjectedOutcome.WOULD_SKIP_CURRENT,
            )
            # Disk unchanged: sidecar still has policy_version=1.0.0
            sc = root / "TASK-20260101-0001" / "identity.json"
            self.assertEqual(
                json.loads(sc.read_text())["policy_version"], "1.0.0",
            )

    def test_corrupt_sidecar_does_not_overwrite(self):
        """A sidecar with non-JSON content is classified as
        unreadable; the projection must NOT report WOULD_OVERWRITE
        in a way that would change disk content.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            fresh_payload = dict(_FIXTURE_RECORD)
            fresh_payload["task_id"] = "TASK-20260101-9999"
            p = _write_task_json(root, "TASK-20260101-9999", fresh_payload)
            # Write a non-JSON sidecar
            sc = p.parent / "identity.json"
            sc.write_text("{ this is not valid json")
            inv = build_sidecar_inventory(root, utc_stamp=_FIXTURE_UTC_STAMP)
            proj = project_migration_execution(
                inv, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            # The entry is unreadable, so its inventory status
            # is MISSING (the inventory can't classify it as
            # STALE_* because it can't read the sidecar). The
            # projection should be WOULD_WRITE.
            self.assertEqual(len(proj.per_task), 1)
            # We don't assert a specific outcome here because
            # the exact classification depends on the inventory;
            # we DO assert that whatever the outcome, the
            # original sidecar bytes are still on disk
            # (projection must not write).
            self.assertTrue(sc.is_file())
            self.assertEqual(
                sc.read_text(), "{ this is not valid json",
            )

    def test_missing_task_json_projection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            # Make a directory with NO task.json
            d = root / "TASK-20260101-0099"
            d.mkdir()
            inv = build_sidecar_inventory(root, utc_stamp=_FIXTURE_UTC_STAMP)
            proj = project_migration_execution(
                inv, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            self.assertEqual(
                proj.per_task[0].outcome,
                ProjectedOutcome.WOULD_FAIL_MISSING_TASK_JSON,
            )
            self.assertFalse(proj.per_task[0].would_change_sidecar)

    def test_projected_totals_internally_consistent(self):
        """Sum of by_outcome MUST equal inventory_total."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_fresh_corpus(root)
            _build_missing_corpus(root)
            _build_stale_hash_corpus(root)
            _build_stale_version_corpus(root)
            _build_runtime_corpus(root)
            inv = build_sidecar_inventory(root, utc_stamp=_FIXTURE_UTC_STAMP)
            proj = project_migration_execution(
                inv, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            self.assertEqual(
                sum(proj.by_outcome.values()), len(inv.entries),
            )
            self.assertEqual(proj.inventory_total, len(inv.entries))


# ---------------------------------------------------------------------------
# 5. Read-only safety invariants (the headline contract)
# ---------------------------------------------------------------------------


class TestReadOnlySafetyInvariants(unittest.TestCase):
    """These tests are the headline safety contract: a dry-run
    must not mutate the audited corpus in any way.
    """

    def test_dryrun_does_not_overwrite_stale_hash_sidecar(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_stale_hash_corpus(root)
            sc_path = (
                root / "TASK-20260101-0003" / "identity.json"
            )
            sha_before = _file_sha256(sc_path)
            size_before = _file_size(sc_path)
            mtime_before = _file_mtime_ns(sc_path)
            run_live_migration_dryrun(
                root, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            self.assertEqual(_file_sha256(sc_path), sha_before)
            self.assertEqual(_file_size(sc_path), size_before)
            self.assertEqual(_file_mtime_ns(sc_path), mtime_before)

    def test_dryrun_does_not_overwrite_stale_version_sidecar(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_stale_version_corpus(root)
            sc_path = (
                root / "TASK-20260101-0004" / "identity.json"
            )
            sha_before = _file_sha256(sc_path)
            size_before = _file_size(sc_path)
            mtime_before = _file_mtime_ns(sc_path)
            run_live_migration_dryrun(
                root, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            self.assertEqual(_file_sha256(sc_path), sha_before)
            self.assertEqual(_file_size(sc_path), size_before)
            self.assertEqual(_file_mtime_ns(sc_path), mtime_before)

    def test_dryrun_does_not_create_missing_sidecar(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_missing_corpus(root)
            sc_path = (
                root / "TASK-20260101-0002" / "identity.json"
            )
            self.assertFalse(sc_path.is_file())
            run_live_migration_dryrun(
                root, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            self.assertFalse(
                sc_path.is_file(),
                "dry-run must NOT create identity.json for MISSING entries",
            )

    def test_dryrun_does_not_create_runtime_sidecar(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_runtime_corpus(root)
            sc_path = (
                root / "TASK-20260101-0005" / "identity.json"
            )
            self.assertFalse(sc_path.is_file())
            run_live_migration_dryrun(
                root, allow_runtime=True, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            self.assertFalse(
                sc_path.is_file(),
                "dry-run with allow_runtime=True must NOT create sidecar",
            )

    def test_dryrun_sidecar_path_set_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_fresh_corpus(root)
            _build_missing_corpus(root)
            _build_stale_hash_corpus(root)
            _build_stale_version_corpus(root)
            _build_runtime_corpus(root)
            pre = _snapshot_corpus(root)
            run_live_migration_dryrun(
                root, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            post = _snapshot_corpus(root)
            self.assertEqual(
                set(pre["sidecars"].keys()),
                set(post["sidecars"].keys()),
            )
            self.assertEqual(
                set(pre["task_jsons"].keys()),
                set(post["task_jsons"].keys()),
            )

    def test_dryrun_sidecar_sha_all_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_fresh_corpus(root)
            _build_missing_corpus(root)
            _build_stale_hash_corpus(root)
            _build_stale_version_corpus(root)
            pre = _snapshot_corpus(root)
            run_live_migration_dryrun(
                root, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            post = _snapshot_corpus(root)
            for name, fingerprint in pre["sidecars"].items():
                self.assertEqual(
                    fingerprint["sha256"],
                    post["sidecars"][name]["sha256"],
                    f"sidecar SHA changed for {name}",
                )

    def test_dryrun_sidecar_mtime_all_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_fresh_corpus(root)
            _build_missing_corpus(root)
            _build_stale_hash_corpus(root)
            _build_stale_version_corpus(root)
            pre = _snapshot_corpus(root)
            run_live_migration_dryrun(
                root, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            post = _snapshot_corpus(root)
            for name, fingerprint in pre["sidecars"].items():
                self.assertEqual(
                    fingerprint["mtime_ns"],
                    post["sidecars"][name]["mtime_ns"],
                    f"sidecar mtime changed for {name}",
                )

    def test_dryrun_task_json_sha_all_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_fresh_corpus(root)
            _build_missing_corpus(root)
            _build_stale_hash_corpus(root)
            _build_stale_version_corpus(root)
            pre = _snapshot_corpus(root)
            run_live_migration_dryrun(
                root, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            post = _snapshot_corpus(root)
            for name, fingerprint in pre["task_jsons"].items():
                self.assertEqual(
                    fingerprint["sha256"],
                    post["task_jsons"][name]["sha256"],
                    f"task.json SHA changed for {name}",
                )

    def test_dryrun_task_json_mtime_all_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_fresh_corpus(root)
            _build_missing_corpus(root)
            _build_stale_hash_corpus(root)
            _build_stale_version_corpus(root)
            pre = _snapshot_corpus(root)
            run_live_migration_dryrun(
                root, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            post = _snapshot_corpus(root)
            for name, fingerprint in pre["task_jsons"].items():
                self.assertEqual(
                    fingerprint["mtime_ns"],
                    post["task_jsons"][name]["mtime_ns"],
                    f"task.json mtime changed for {name}",
                )

    def test_dryrun_does_not_create_migration_log(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_missing_corpus(root)
            _build_stale_hash_corpus(root)
            _build_stale_version_corpus(root)
            run_live_migration_dryrun(
                root, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            # Look for AEE-7.7d migration log files anywhere
            # under the temp dir.
            for p in Path(td).rglob("migration_log_*.json"):
                self.fail(f"dry-run must not write {p}")

    def test_dryrun_does_not_create_extra_files_in_corpus(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_fresh_corpus(root)
            _build_missing_corpus(root)
            _build_stale_hash_corpus(root)
            _build_stale_version_corpus(root)
            pre_files = set()
            for p in root.rglob("*"):
                if p.is_file():
                    pre_files.add(str(p))
            run_live_migration_dryrun(
                root, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            post_files = set()
            for p in root.rglob("*"):
                if p.is_file():
                    post_files.add(str(p))
            self.assertEqual(
                pre_files, post_files,
                "dry-run must not create or delete files inside the corpus",
            )


# ---------------------------------------------------------------------------
# 6. Manifest artifact (write_manifest=True)
# ---------------------------------------------------------------------------


class TestManifestArtifact(unittest.TestCase):
    def test_write_manifest_false_does_not_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            res = run_live_migration_dryrun(
                root, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            self.assertIsNone(res.manifest_path)
            self.assertEqual(res.manifest_sha256, "")
            for p in Path(td).iterdir():
                if p.is_file():
                    self.fail(f"unexpected file: {p}")

    def test_write_manifest_true_writes_json(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_missing_corpus(root)
            manifest = Path(td) / "manifest.json"
            res = run_live_migration_dryrun(
                root,
                write_manifest=True,
                manifest_path=manifest,
                utc_stamp=_FIXTURE_UTC_STAMP,
            )
            self.assertEqual(res.manifest_path, str(manifest))
            self.assertTrue(manifest.is_file())
            self.assertEqual(res.manifest_sha256, _file_sha256(manifest))
            with open(manifest, "r", encoding="utf-8") as fh:
                decoded = json.load(fh)
            self.assertEqual(decoded["schema_version"], "1.1.0")
            self.assertEqual(decoded["utc_stamp"], _FIXTURE_UTC_STAMP)

    def test_write_manifest_default_path_outside_corpus(self):
        with tempfile.TemporaryDirectory() as td:
            reports_parent = Path(td) / "parent"
            root = reports_parent / "reports"
            root.mkdir(parents=True)
            _build_missing_corpus(root)
            res = run_live_migration_dryrun(
                root,
                write_manifest=True,
                utc_stamp=_FIXTURE_UTC_STAMP,
            )
            expected_path = (
                reports_parent
                / f"aee77e-dryrun-{_FIXTURE_UTC_STAMP}.json"
            )
            self.assertTrue(expected_path.is_file())
            self.assertEqual(res.manifest_path, str(expected_path))
            # The manifest is OUTSIDE the reports/ tree.
            self.assertNotEqual(
                str(expected_path).startswith(str(root) + os.sep), True,
            )
            # And reports/ has not gained any new files beyond
            # what _build_missing_corpus wrote.
            files_in_corpus = sorted(
                [str(p) for p in root.rglob("*") if p.is_file()]
            )
            expected_files = sorted(
                [str(p) for p in root.rglob("TASK-*/*") if p.is_file()]
            )
            self.assertEqual(files_in_corpus, expected_files)

    def test_manifest_deterministic(self):
        with tempfile.TemporaryDirectory() as td1, \
                tempfile.TemporaryDirectory() as td2:
            for td in (td1, td2):
                root = Path(td) / "reports"
                root.mkdir()
                _build_fresh_corpus(root)
                _build_missing_corpus(root)
                _build_stale_hash_corpus(root)
            m1 = Path(td1) / "m.json"
            m2 = Path(td2) / "m.json"
            run_live_migration_dryrun(
                Path(td1) / "reports",
                write_manifest=True,
                manifest_path=m1,
                utc_stamp=_FIXTURE_UTC_STAMP,
            )
            run_live_migration_dryrun(
                Path(td2) / "reports",
                write_manifest=True,
                manifest_path=m2,
                utc_stamp=_FIXTURE_UTC_STAMP,
            )
            c1 = m1.read_text()
            c2 = m2.read_text()
            # Reports root differs, so strip that key.
            c1_norm = c1.replace(str(Path(td1) / "reports"), "ROOT")
            c2_norm = c2.replace(str(Path(td2) / "reports"), "ROOT")
            self.assertEqual(c1_norm, c2_norm)

    def test_manifest_path_inside_corpus_rejected_or_outside(self):
        """The caller is responsible for choosing a safe
        manifest path. The function does NOT enforce "outside
        corpus" by default; that is a documented responsibility.
        This test verifies that IF the caller picks a path
        inside the corpus, the dry-run produces a manifest
        THERE — but the documented expectation is to pick a
        path outside. This test pins the current behaviour.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_missing_corpus(root)
            manifest = root / "manifest.json"
            run_live_migration_dryrun(
                root,
                write_manifest=True,
                manifest_path=manifest,
                utc_stamp=_FIXTURE_UTC_STAMP,
            )
            # The manifest was created where the caller asked.
            self.assertTrue(manifest.is_file())


# ---------------------------------------------------------------------------
# 7. apply path (AEE-7.7d executor wiring, opt-in)
# ---------------------------------------------------------------------------


class TestApplyPath(unittest.TestCase):
    def test_apply_writes_stale_hash_sidecar(self):
        """``run_live_migration_apply`` is the only AEE-7.7e
        function that may write sidecars.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_stale_hash_corpus(root)
            sc_path = (
                root / "TASK-20260101-0003" / "identity.json"
            )
            sha_before = _file_sha256(sc_path)
            result = run_live_migration_apply(
                root, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            sha_after = _file_sha256(sc_path)
            self.assertNotEqual(
                sha_before, sha_after,
                "apply path must re-stamp STALE_HASH sidecar",
            )
            # And the executor's by_status has OVERWROTE
            self.assertGreater(
                result.by_status.get(MigrationStatus.OVERWROTE.value, 0), 0,
            )

    def test_apply_writes_missing_sidecar(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_missing_corpus(root)
            sc_path = (
                root / "TASK-20260101-0002" / "identity.json"
            )
            self.assertFalse(sc_path.is_file())
            result = run_live_migration_apply(
                root, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            self.assertTrue(sc_path.is_file())
            self.assertGreater(
                result.by_status.get(MigrationStatus.WROTE.value, 0), 0,
            )

    def test_apply_does_not_create_runtime_sidecar_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_runtime_corpus(root)
            sc_path = (
                root / "TASK-20260101-0005" / "identity.json"
            )
            self.assertFalse(sc_path.is_file())
            result = run_live_migration_apply(
                root, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            self.assertFalse(
                sc_path.is_file(),
                "apply with allow_runtime=False must NOT create sidecar",
            )
            self.assertGreater(
                result.by_status.get(
                    MigrationStatus.RUNTIME_SKIPPED.value, 0,
                ), 0,
            )


# ---------------------------------------------------------------------------
# 8. UTC stamp propagation
# ---------------------------------------------------------------------------


class TestUtcStampPropagation(unittest.TestCase):
    def test_utc_stamp_propagates_to_all_three_layers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_missing_corpus(root)
            stamp = "2026-07-12T13:30:00Z"
            res = run_live_migration_dryrun(root, utc_stamp=stamp)
            self.assertEqual(res.utc_stamp, stamp)
            self.assertEqual(res.inventory.inventoried_at_utc, stamp)
            self.assertEqual(res.plan.planned_at_utc, stamp)
            self.assertEqual(res.projection.projected_at_utc, stamp)

    def test_no_utc_stamp_uses_now(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            res = run_live_migration_dryrun(root)
            self.assertRegex(
                res.utc_stamp,
                r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
            )
            self.assertEqual(
                res.inventory.inventoried_at_utc, res.utc_stamp,
            )
            self.assertEqual(res.plan.planned_at_utc, res.utc_stamp)
            self.assertEqual(res.projection.projected_at_utc, res.utc_stamp)


# ---------------------------------------------------------------------------
# 9. Deterministic serialization
# ---------------------------------------------------------------------------


class TestDeterministicSerialization(unittest.TestCase):
    def test_to_dict_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_missing_corpus(root)
            res = run_live_migration_dryrun(
                root, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            d = res.to_dict()
            encoded = json.dumps(d, sort_keys=True, indent=2)
            decoded = json.loads(encoded)
            self.assertEqual(decoded["schema_version"], "1.1.0")
            self.assertEqual(decoded["utc_stamp"], _FIXTURE_UTC_STAMP)
            self.assertEqual(
                decoded["reconciliation"]["passed"], True,
            )
            self.assertEqual(
                decoded["reconciliation"]["projected_writes"], 1,
            )

    def test_to_markdown_contains_inventory_plan_projection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_fresh_corpus(root)
            _build_missing_corpus(root)
            res = run_live_migration_dryrun(
                root, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            md = res.to_markdown()
            self.assertIn("# AEE-7.7e Live Migration Dry-run", md)
            self.assertIn("## Inventory", md)
            self.assertIn("## Plan (dry-run)", md)
            self.assertIn("## Projection (read-only)", md)
            self.assertIn("## Reconciliation (plan vs projection)", md)
            self.assertIn("PASS", md)
            self.assertIn("| `fresh` |", md)
            self.assertIn("| `missing` |", md)
            # v1.0.0 aliases are explicitly marked deprecated
            self.assertIn("DEPRECATED", md)
            self.assertIn("deprecated", md.lower())

    def test_two_runs_with_same_stamp_yield_byte_identical_dto(self):
        with tempfile.TemporaryDirectory() as td1, \
                tempfile.TemporaryDirectory() as td2:
            for td in (td1, td2):
                root = Path(td) / "reports"
                root.mkdir()
                _build_fresh_corpus(root)
                _build_missing_corpus(root)
                _build_stale_hash_corpus(root)
            res1 = run_live_migration_dryrun(
                Path(td1) / "reports", utc_stamp=_FIXTURE_UTC_STAMP,
            )
            res2 = run_live_migration_dryrun(
                Path(td2) / "reports", utc_stamp=_FIXTURE_UTC_STAMP,
            )
            d1 = json.dumps(res1.to_dict(), sort_keys=True, indent=2)
            d2 = json.dumps(res2.to_dict(), sort_keys=True, indent=2)
            d1 = d1.replace(str(Path(td1) / "reports"), "ROOT")
            d2 = d2.replace(str(Path(td2) / "reports"), "ROOT")
            self.assertEqual(d1, d2)


# ---------------------------------------------------------------------------
# 10. Plan/projection reconciliation contract
# ---------------------------------------------------------------------------


class TestReconciliationContract(unittest.TestCase):
    def test_projection_counts_align_with_plan(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_fresh_corpus(root)
            _build_missing_corpus(root)
            _build_stale_hash_corpus(root)
            _build_stale_version_corpus(root)
            res = run_live_migration_dryrun(
                root, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            recon = res.to_dict()["reconciliation"]
            # Plan: 1 would_write (MISSING), 2 would_overwrite
            # (STALE_HASH + STALE_VERSION), 1 no_op (FRESH)
            self.assertEqual(recon["plan_would_write"], 1)
            self.assertEqual(recon["plan_would_overwrite"], 2)
            self.assertEqual(recon["plan_no_op"], 1)
            # Projection: 1 WOULD_WRITE, 2 WOULD_OVERWRITE,
            # 1 WOULD_SKIP_CURRENT (FRESH), 0 WOULD_SKIP_RUNTIME
            self.assertEqual(recon["projected_writes"], 1)
            self.assertEqual(recon["projected_overwrites"], 2)
            self.assertEqual(recon["projected_skips"], 1)
            self.assertEqual(recon["projected_runtime_skipped"], 0)
            self.assertEqual(recon["projected_total"], 4)
            self.assertTrue(recon["passed"])

    def test_runtime_moves_would_write_to_skip_runtime(self):
        """When allow_runtime=False, a RUNTIME MISSING entry
        contributes to plan.would_write but appears as
        WOULD_SKIP_RUNTIME in the projection. The reconciliation
        contract absorbs the difference.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            # MISSING runtime record
            runtime_payload = dict(_RUNTIME_RECORD)
            runtime_payload["task_id"] = "TASK-20260101-0006"
            _write_task_json(root, "TASK-20260101-0006", runtime_payload)
            res = run_live_migration_dryrun(
                root,
                allow_runtime=False,
                utc_stamp=_FIXTURE_UTC_STAMP,
            )
            recon = res.to_dict()["reconciliation"]
            # plan: 1 would_write (MISSING entry)
            self.assertEqual(recon["plan_would_write"], 1)
            # projection: 0 WOULD_WRITE, 1 WOULD_SKIP_RUNTIME
            self.assertEqual(recon["projected_writes"], 0)
            self.assertEqual(recon["projected_runtime_skipped"], 1)
            # Reconciliation passes (the RUNTIME gap is absorbed).
            self.assertTrue(recon["passed"])

    def test_projected_totals_equal_inventory_total(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_fresh_corpus(root)
            _build_missing_corpus(root)
            _build_stale_hash_corpus(root)
            _build_stale_version_corpus(root)
            _build_runtime_corpus(root)
            res = run_live_migration_dryrun(
                root, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            self.assertEqual(
                res.projected_total, len(res.inventory.entries),
            )


# ---------------------------------------------------------------------------
# 11. No dispatcher import
# ---------------------------------------------------------------------------


class TestNoDispatcherImport(unittest.TestCase):
    def test_module_does_not_import_dispatcher(self):
        """The module source has 0 ``import dispatcher`` or
        ``from dispatcher ...`` statements.
        """
        module_path = (
            Path(_REPO_ROOT) / "aee" / "audit" / "live_migration_dryrun.py"
        )
        src = module_path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(module_path))
        bad: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for n in node.names:
                    if n.name == "dispatcher" or n.name.startswith(
                        "dispatcher."
                    ):
                        bad.append(
                            f"import {n.name} at line {node.lineno}"
                        )
            elif isinstance(node, ast.ImportFrom):
                if node.module and (
                    node.module == "dispatcher"
                    or node.module.startswith("dispatcher.")
                ):
                    bad.append(
                        f"from {node.module} import ... at line "
                        f"{node.lineno}"
                    )
        self.assertEqual(
            bad, [],
            f"dispatcher import found: {bad}",
        )

    def test_loading_does_not_introduce_dispatcher_modules(self):
        before = {
            m for m in sys.modules
            if m == "dispatcher" or m.startswith("dispatcher.")
        }
        from aee.audit import live_migration_dryrun  # noqa: F401
        after = {
            m for m in sys.modules
            if m == "dispatcher" or m.startswith("dispatcher.")
        }
        self.assertEqual(
            before, after,
            f"dispatcher modules introduced: {after - before}",
        )

    def test_dryrun_does_not_introduce_dispatcher_modules(self):
        """Calling run_live_migration_dryrun does not import
        dispatcher.* into sys.modules.
        """
        before = {
            m for m in sys.modules
            if m == "dispatcher" or m.startswith("dispatcher.")
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_missing_corpus(root)
            run_live_migration_dryrun(
                root, utc_stamp=_FIXTURE_UTC_STAMP,
            )
        after = {
            m for m in sys.modules
            if m == "dispatcher" or m.startswith("dispatcher.")
        }
        self.assertEqual(
            before, after,
            f"dispatcher modules introduced by run_live_migration_dryrun: "
            f"{after - before}",
        )


# ---------------------------------------------------------------------------
# 12. Secret leakage tripwire
# ---------------------------------------------------------------------------


class TestNoLeakage(unittest.TestCase):
    def test_no_secret_pattern_in_dto(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_runtime_corpus(root)
            _build_missing_corpus(root)
            res = run_live_migration_dryrun(
                root,
                allow_runtime=True,
                utc_stamp=_FIXTURE_UTC_STAMP,
            )
            encoded = json.dumps(res.to_dict(), sort_keys=True, indent=2)
            hits = _scan_for_secrets(encoded)
            self.assertEqual(
                hits, [],
                f"secret pattern leaked in to_dict: {hits}",
            )
            md = res.to_markdown()
            hits = _scan_for_secrets(md)
            self.assertEqual(
                hits, [],
                f"secret pattern leaked in to_markdown: {hits}",
            )

    def test_no_secret_pattern_in_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_runtime_corpus(root)
            _build_missing_corpus(root)
            manifest = Path(td) / "manifest.json"
            run_live_migration_dryrun(
                root,
                allow_runtime=True,
                write_manifest=True,
                manifest_path=manifest,
                utc_stamp=_FIXTURE_UTC_STAMP,
            )
            with open(manifest, "r", encoding="utf-8") as fh:
                content = fh.read()
            hits = _scan_for_secrets(content)
            self.assertEqual(
                hits, [],
                f"secret pattern leaked in manifest: {hits}",
            )


# ---------------------------------------------------------------------------
# 13. AEE-7.7c compatibility (inventory/plan functions unchanged)
# ---------------------------------------------------------------------------


class TestAee77cCompat(unittest.TestCase):
    def test_inventory_plan_functions_still_exported(self):
        from aee.audit import (
            build_sidecar_inventory,
            plan_sidecar_migration,
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_missing_corpus(root)
            inv = build_sidecar_inventory(root, utc_stamp=_FIXTURE_UTC_STAMP)
            plan = plan_sidecar_migration(inv, utc_stamp=_FIXTURE_UTC_STAMP)
            self.assertEqual(len(inv.entries), 1)
            self.assertEqual(plan.would_write, 1)

    def test_dryrun_uses_same_inventory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "reports"
            root.mkdir()
            _build_fresh_corpus(root)
            _build_missing_corpus(root)
            _build_stale_hash_corpus(root)
            res = run_live_migration_dryrun(
                root, utc_stamp=_FIXTURE_UTC_STAMP,
            )
            # Plan's reports_root matches inventory's
            self.assertEqual(
                res.plan.inventory_reports_root,
                res.inventory.reports_root,
            )
            # Plan total = inventory total
            self.assertEqual(
                res.plan.would_write + res.plan.would_overwrite
                + res.plan.no_op,
                len(res.inventory.entries),
            )

    def test_dryrun_does_not_call_executor(self):
        """The dry-run flow MUST NOT call execute_sidecar_migration.
        We verify this by patching the executor to raise, and
        confirming the dry-run still works.

        Note: another 7.7d test (``TestNoDispatcherImport``)
        purges ``aee.audit.*`` from ``sys.modules`` to verify
        it can be re-imported cleanly. After that purge, the
        local ``run_live_migration_dryrun`` reference in this
        test module's namespace is stale and points to a
        ``live_migration_dryrun`` module that is no longer
        in ``sys.modules``. The patch on the new module
        would not affect the stale reference.

        We re-look-up both ``mod`` and ``run_live_migration_dryrun``
        from the LIVE ``sys.modules`` entry.
        """
        import importlib
        mod = importlib.import_module(
            "aee.audit.live_migration_dryrun"
        )
        dryrun_fn = getattr(mod, "run_live_migration_dryrun")

        original = mod.execute_sidecar_migration
        def boom(*args, **kwargs):
            raise AssertionError(
                "run_live_migration_dryrun must not call "
                "execute_sidecar_migration"
            )
        mod.execute_sidecar_migration = boom
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td) / "reports"
                root.mkdir()
                _build_stale_hash_corpus(root)
                _build_stale_version_corpus(root)
                res = dryrun_fn(
                    root, utc_stamp=_FIXTURE_UTC_STAMP,
                )
                # Reconciliation still passes
                self.assertTrue(res.reconciliation_passed)
                # And the dry-run's projection is the read-only
                # projection, not the executor's outcome.
                self.assertEqual(
                    res.projection.schema_version,
                    LIVE_MIGRATION_DRYRUN_SCHEMA_VERSION,
                )
        finally:
            mod.execute_sidecar_migration = original


# ---------------------------------------------------------------------------
# 14. AEE-7.7d compatibility (executor still works via apply path)
# ---------------------------------------------------------------------------


class TestAee77dCompat(unittest.TestCase):
    def test_executor_still_importable(self):
        from aee.audit import execute_sidecar_migration
        self.assertTrue(callable(execute_sidecar_migration))

    def test_apply_path_uses_executor(self):
        """run_live_migration_apply must call execute_sidecar_migration.
        We verify by patching the executor and confirming the
        apply path raises the patch's error.

        Note: another 7.7d test (``TestNoDispatcherImport``)
        purges ``aee.audit.*`` from ``sys.modules`` to verify
        it can be re-imported cleanly. After that purge, the
        local ``run_live_migration_apply`` reference in this
        test module's namespace is stale and points to a
        ``live_migration_dryrun`` module that is no longer
        in ``sys.modules``. The patch on the new module
        would not affect the stale reference.

        To handle both orderings, we re-look-up
        ``run_live_migration_apply`` from the LIVE
        ``sys.modules`` entry each time the test runs, and
        patch the LIVE ``live_migration_dryrun`` module's
        ``execute_sidecar_migration`` attribute.
        """
        import importlib
        # Make sure aee.audit.live_migration_dryrun is loaded.
        mod = importlib.import_module("aee.audit.live_migration_dryrun")
        # Re-resolve run_live_migration_apply from the live
        # module so a previous sys.modules purge cannot
        # detach us.
        apply_fn = getattr(mod, "run_live_migration_apply")

        original = mod.execute_sidecar_migration
        called: List[Tuple] = []
        def tracker(*args, **kwargs):
            called.append((args, kwargs))
            return original(*args, **kwargs)
        mod.execute_sidecar_migration = tracker
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td) / "reports"
                root.mkdir()
                _build_missing_corpus(root)
                apply_fn(root, utc_stamp=_FIXTURE_UTC_STAMP)
                self.assertEqual(len(called), 1)
        finally:
            mod.execute_sidecar_migration = original


# ---------------------------------------------------------------------------
# 15. Optional: live corpus gated dry-run
# ---------------------------------------------------------------------------


class TestLiveCorpusGatedDryRun(unittest.TestCase):
    """Optional: scan the real ``reports/`` corpus.

    Skipped by default (set ``AEE77E_LIVE_CORPUS=1`` to enable).
    The headline assertion: a dry-run against the live corpus
    must not change any sidecar SHA, mtime, or size; and the
    live DB must not be touched.
    """

    LIVE_ENABLED = os.environ.get("AEE77E_LIVE_CORPUS") == "1"
    REPO_ROOT = Path(__file__).resolve().parent.parent.parent
    REPORTS_ROOT = REPO_ROOT / "reports"
    DB_PATH = REPO_ROOT / "data" / "dispatcher.db"

    @unittest.skipUnless(
        LIVE_ENABLED,
        "Set AEE77E_LIVE_CORPUS=1 to run against the real reports/ corpus",
    )
    def test_real_corpus_dryrun_is_byte_stable(self):
        if not self.REPORTS_ROOT.is_dir():
            self.skipTest("no live reports/ in this checkout")

        # Pre-snapshot
        pre = _snapshot_corpus(self.REPORTS_ROOT)
        if self.DB_PATH.is_file():
            pre_db_sha = _file_sha256(self.DB_PATH)
        else:
            pre_db_sha = ""

        # Run dry-run
        res = run_live_migration_dryrun(
            self.REPORTS_ROOT, utc_stamp="2026-07-12T00:00:00Z",
        )
        # All current sidecars are policy_version=1.0.0 →
        # no STALE_VERSION entries from the existing corpus.
        self.assertEqual(
            res.inventory.by_status.get("stale_version", 0), 0,
        )
        # Reconciliation must pass on the real corpus.
        self.assertTrue(res.reconciliation_passed)
        # Total outcomes == inventory total.
        self.assertEqual(
            res.projected_total, len(res.inventory.entries),
        )

        # Post-snapshot
        post = _snapshot_corpus(self.REPORTS_ROOT)
        if self.DB_PATH.is_file():
            post_db_sha = _file_sha256(self.DB_PATH)
        else:
            post_db_sha = ""

        # Sidecar path set unchanged
        self.assertEqual(
            set(pre["sidecars"].keys()), set(post["sidecars"].keys()),
        )
        # All sidecar SHAs unchanged
        for name, fingerprint in pre["sidecars"].items():
            self.assertEqual(
                fingerprint["sha256"],
                post["sidecars"][name]["sha256"],
                f"sidecar SHA changed for {name}",
            )
        # All sidecar mtimes unchanged
        for name, fingerprint in pre["sidecars"].items():
            self.assertEqual(
                fingerprint["mtime_ns"],
                post["sidecars"][name]["mtime_ns"],
                f"sidecar mtime changed for {name}",
            )
        # All sidecar sizes unchanged
        for name, fingerprint in pre["sidecars"].items():
            self.assertEqual(
                fingerprint["size"],
                post["sidecars"][name]["size"],
                f"sidecar size changed for {name}",
            )
        # All task.json SHAs unchanged
        for name, fingerprint in pre["task_jsons"].items():
            self.assertEqual(
                fingerprint["sha256"],
                post["task_jsons"][name]["sha256"],
                f"task.json SHA changed for {name}",
            )
        # All task.json mtimes unchanged
        for name, fingerprint in pre["task_jsons"].items():
            self.assertEqual(
                fingerprint["mtime_ns"],
                post["task_jsons"][name]["mtime_ns"],
                f"task.json mtime changed for {name}",
            )
        # Live DB SHA unchanged
        self.assertEqual(pre_db_sha, post_db_sha)


if __name__ == "__main__":
    unittest.main(verbosity=2)
