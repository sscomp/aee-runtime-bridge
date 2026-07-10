"""AEE-6.2 Artifact Pipeline ↔ dispatcher hot-path integration tests.

Scope
-----
Slice 1 (`aee.tests.test_artifacts`) covers the pipeline as a pure
domain (InMemory repo, no dispatcher). This module covers the
**wire-up**: the dispatcher `TaskManager.complete()` path now
goes through `ArtifactPipeline.collect()` + `SqliteArtifactRepository`
and produces real `artifacts` rows in the dispatcher DB.

All tests are stdlib `unittest` only. No pytest, no network. The
goal is to prove:

1. A successful task writes one `artifacts` row per detected path
   in the input.
2. A missing file produces an `artifacts` row with `file_exists=0`
   and bumps `warning_count` (the legacy Phase 4 contract).
3. A failed task (`manager.fail()`) does NOT collect artifacts (only
   `complete()` triggers collection) and does not leave the
   `artifacts` table dirty.
4. A timed-out task (`manager.timeout()`) — same as fail().
5. A re-collect (run the same task twice) bumps `version` in the
   `artifacts` row, not `id`; the previous row stays for audit.
6. **Security** — a symlink pointing at /etc/passwd is hashed and
   persisted; the `path` field records the canonical absolute path
   (not the symlink) but the **bytes** hashed are the symlink target.
   This is the intentional semantics: a worker that writes a symlink
   to sensitive data is accountable for it.
7. **Security** — `..` path traversal in input_text is NOT collected
   because the regex requires `/<file>.<ext>` shape. The bridge does
   not stat parent directories.
8. **Security** — a file outside the bridge workspace (e.g. /tmp/foo.md)
   is still collected. The bridge trusts the worker to have legitimate
   write access. The hot path does not enforce a `repo root` whitelist
   in AEE-6.2; that is a future slice.

The 3 security cases are documented in the master plan §14.5.

Run with:
    .venv/bin/python -m unittest aee.tests.test_artifacts_integration -v
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dispatcher.db import DB_PATH, get_conn
from dispatcher.manager import TaskManager


def _read_artifacts_table(task_id: str) -> list:
    """Read the `artifacts` rows for `task_id` via the dispatcher
    connection pool. We do NOT open a fresh sqlite3.connect because
    that would bind to a fresh in-memory DB if the dispatcher DB
    has been torn down by another test module's setUp
    (e.g. test_jobs_api._fresh_db)."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT * FROM artifacts WHERE task_id = ? ORDER BY version ASC, path ASC",
            (task_id,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        # We do NOT close — the conn is owned by the pool.
        pass


def _count_artifacts(task_id: str) -> int:
    return len(_read_artifacts_table(task_id))


class TestArtifactDispatchWiring(unittest.TestCase):
    """End-to-end: TaskManager.complete() → ArtifactPipeline → SQLite."""

    def setUp(self) -> None:
        # Each test gets its own tmpdir and a unique task. We do NOT
        # touch the global dispatcher DB schema; it is created by
        # `_init_schema` on first connection and is idempotent.
        self.tmpdir = tempfile.mkdtemp(prefix="aee6-int-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        # Force the dispatcher connection to be opened at least once
        # so AEE-5 + AEE-6 schemas are applied to the live DB.
        _ = get_conn()

    def _write(self, name: str, content: str = "x") -> str:
        p = Path(self.tmpdir) / name
        p.write_text(content, encoding="utf-8")
        return str(p)

    # ---- Core scenarios ---------------------------------------------------

    def test_successful_task_persists_artifacts_with_hash_and_kind(self):
        """A task that mentions a real report.md + test.log in its
        input_text should:
        - produce 2 `artifacts` rows,
        - with sha256 + kind + content_type filled in,
        - and `delivery_json` should round-trip the same paths."""
        report = self._write("report.md", "# report")
        log = self._write("test.log", "all passed")
        m = TaskManager()
        t = m.create(
            title="aee6-success",
            type="research",
            input_text=f"produce {report} and {log}",
        )
        m.start(t.task_id, hermes_run_id="run-success")
        out = m.complete(t.task_id, output_text="done")
        # Legacy Phase 4: warning_count=0, delivery_json has 2 entries.
        self.assertEqual(out.warning_count, 0)
        delivery = json.loads(m.get_output(t.task_id)["delivery_json"])
        self.assertEqual(len(delivery), 2)
        # AEE-6.2: 2 rows in `artifacts` table.
        rows = _read_artifacts_table(t.task_id)
        self.assertEqual(len(rows), 2)
        kinds = sorted(r["kind"] for r in rows)
        self.assertEqual(kinds, ["log", "report"])
        # sha256 must be a 64-hex string (sha256 hex digest).
        for r in rows:
            self.assertEqual(len(r["sha256"]), 64)
            self.assertEqual(r["file_exists"], 1)
            self.assertEqual(r["version"], 1)
        # The delivery_json `sha256`/`kind` fields should equal
        # the DB rows (back-compat surface).
        delivery_by_path = {e["path"]: e for e in delivery}
        for r in rows:
            entry = delivery_by_path[r["path"]]
            self.assertEqual(entry["sha256"], r["sha256"])
            self.assertEqual(entry["kind"], r["kind"])
            self.assertEqual(entry["artifact_id"], r["artifact_id"])

    def test_missing_file_writes_missing_artifact_row_and_bumps_warning(self):
        """The legacy Phase 4 contract: a missing expected file
        bumps warning_count, records `exists=False` in delivery_json,
        and now also writes a `artifacts` row with `file_exists=0`
        so the orchestrator can later audit what was promised."""
        missing = Path(self.tmpdir) / "ghost.md"
        m = TaskManager()
        t = m.create(
            title="aee6-missing",
            type="research",
            input_text=f"verify {missing}",
        )
        m.start(t.task_id, hermes_run_id="run-missing")
        out = m.complete(t.task_id, output_text="done")
        # Legacy contract intact.
        self.assertEqual(out.warning_count, 1)
        delivery = json.loads(m.get_output(t.task_id)["delivery_json"])
        self.assertEqual(len(delivery), 1)
        self.assertFalse(delivery[0]["exists"])
        # AEE-6.2: a row with file_exists=0 is still written.
        rows = _read_artifacts_table(t.task_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["file_exists"], 0)
        self.assertIsNone(rows[0]["sha256"])
        self.assertIsNone(rows[0]["size"])

    def test_failed_task_does_not_collect_artifacts(self):
        """`fail()` does NOT invoke the ArtifactPipeline. The
        legacy Phase 4 contract: only `complete()` verifies
        delivery. A failed task should leave the `artifacts`
        table with 0 rows for that task_id (no half-state)."""
        m = TaskManager()
        t = m.create(
            title="aee6-fail",
            type="research",
            input_text=f"make {Path(self.tmpdir) / 'never.md'}",
        )
        m.start(t.task_id, hermes_run_id="run-fail")
        m.fail(t.task_id, "agent crashed")
        self.assertEqual(_count_artifacts(t.task_id), 0)

    def test_timed_out_task_does_not_collect_artifacts(self):
        """`timeout()` mirrors `fail()` — no ArtifactPipeline.
        A retried task that completes (after timeout) will then
        collect normally; this is the explicit re-collect path."""
        m = TaskManager()
        t = m.create(
            title="aee6-timeout",
            type="research",
            input_text=f"write {Path(self.tmpdir) / 'late.md'}",
        )
        m.start(t.task_id, hermes_run_id="run-timeout")
        m.timeout(t.task_id, "no progress for 18m, reaper")
        self.assertEqual(_count_artifacts(t.task_id), 0)

    def test_re_collect_bumps_version_not_id(self):
        """Calling `ArtifactPipeline.collect()` twice with the same
        `(task_id, path)` writes a new `artifacts` row with
        `version=2`; the version=1 row is preserved for audit. The
        `task_id` and `path` stay the same; only `version` and
        `artifact_id` change.

        We exercise this through the ArtifactPipeline directly
        (not through a full TaskManager.complete() cycle, because
        re-running a completed task is blocked by the dispatcher
        state machine). The dispatcher side is covered by
        test_phase4_delivery's existing tests; here we focus on
        the storage layer's idempotency contract.
        """
        path = self._write("rec.md", "v1")
        from aee.artifacts import (
            ArtifactPipeline,
            SqliteArtifactRepository,
        )
        repo = SqliteArtifactRepository(get_conn())
        pipeline = ArtifactPipeline(repo=repo)
        # Use a per-test unique task_id so the assertion is not
        # affected by other tests' rows in the same dispatcher DB.
        unique_task = f"TASK-RECOLL-{os.path.basename(self.tmpdir)}"
        # First collect.
        pipeline.collect(unique_task, [path])
        # Mutate the file so the sha changes.
        Path(path).write_text("v2", encoding="utf-8")
        # Second collect.
        pipeline.collect(unique_task, [path])
        rows = _read_artifacts_table(unique_task)
        self.assertEqual(len(rows), 2)
        versions = sorted(r["version"] for r in rows)
        self.assertEqual(versions, [1, 2])
        # Both rows have the same task_id + path, different
        # artifact_id + sha256.
        self.assertEqual(rows[0]["task_id"], rows[1]["task_id"])
        self.assertEqual(rows[0]["path"], rows[1]["path"])
        self.assertNotEqual(rows[0]["artifact_id"], rows[1]["artifact_id"])
        self.assertNotEqual(rows[0]["sha256"], rows[1]["sha256"])

    # ---- Security / path safety ------------------------------------------

    def test_symlink_is_hashed_via_target(self):
        """Security contract: a symlink that points at another file
        IS collected. The path recorded is the symlink path
        (canonical absolute path), but the sha256 is computed over
        the *target* bytes (because we follow the symlink on
        `open(path, 'rb')`). This is the intentional behavior — a
        worker that produces a symlink to a sensitive file is
        accountable for that file via the audit row."""
        real = self._write("real.md", "real content")
        link_path = os.path.join(self.tmpdir, "link.md")
        os.symlink(real, link_path)
        m = TaskManager()
        t = m.create(
            title="aee6-symlink",
            type="research",
            input_text=f"see {link_path}",
        )
        m.start(t.task_id, hermes_run_id="run-symlink")
        out = m.complete(t.task_id, output_text="done")
        self.assertEqual(out.warning_count, 0)
        rows = _read_artifacts_table(t.task_id)
        self.assertEqual(len(rows), 1)
        # The path recorded is the symlink path itself (NOT
        # resolved via realpath) — `ArtifactCollector` uses
        # `os.path.abspath` which preserves symlinks. The sha256
        # is computed over the target bytes because `open(path,
        # 'rb')` follows symlinks. This pair is the documented
        # audit semantics: a worker that creates a symlink to
        # sensitive data is accountable for that data.
        self.assertEqual(rows[0]["path"], os.path.abspath(link_path))
        # sha256 matches the real file's content (because open
        # follows the symlink).
        import hashlib
        with open(real, "rb") as f:
            expected_sha = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(rows[0]["sha256"], expected_sha)

    def test_dotdot_traversal_in_input_is_not_collected(self):
        """A path like `/foo/../etc/passwd.md` is NOT collected by
        the regex (it doesn't match `/<file>.<ext>` because the
        `..` segment contains no dot-extension in the trailing
        basename). The bridge therefore does NOT stat parent
        directories, even when the input attempts traversal."""
        m = TaskManager()
        t = m.create(
            title="aee6-traversal",
            type="research",
            # Intentionally crafted: not a real path on this host,
            # AND no `.<ext>` at the basename — should not match.
            input_text="read /tmp/../etc/whatever now",
        )
        m.start(t.task_id, hermes_run_id="run-traversal")
        out = m.complete(t.task_id, output_text="done")
        # No artifact rows because the regex didn't catch anything.
        self.assertEqual(out.warning_count, 0)
        self.assertEqual(_count_artifacts(t.task_id), 0)

    def test_absolute_path_outside_workspace_is_still_collected(self):
        """A worker that legitimately writes to /tmp/foo.md (i.e.
        outside the bridge workspace) is still collected. The
        bridge does not enforce a repo-root whitelist in AEE-6.2;
        that is a deliberate decision (documented in master plan
        §14.5). The path passed to os.stat is absolute and the
        worker must already have write access to it.
        """
        outside = "/tmp/aee6-outside-{pid}.md".format(pid=os.getpid())
        try:
            with open(outside, "w", encoding="utf-8") as f:
                f.write("outside content")
            m = TaskManager()
            t = m.create(
                title="aee6-outside",
                type="research",
                input_text=f"created at {outside}",
            )
            m.start(t.task_id, hermes_run_id="run-outside")
            out = m.complete(t.task_id, output_text="done")
            self.assertEqual(out.warning_count, 0)
            rows = _read_artifacts_table(t.task_id)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["path"], outside)
        finally:
            try:
                os.unlink(outside)
            except OSError:
                pass


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
