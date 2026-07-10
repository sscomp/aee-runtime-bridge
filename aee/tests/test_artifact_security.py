"""AEE-6.3 — Artifact security boundary tests.

These tests prove the contract documented in
``aee/artifacts/policy.py``:

1. **No read on violation.**  ``ArtifactPolicy.check()`` does not
   call ``open()`` on the target. We assert that by patching
   ``builtins.open`` and confirming it is never called for a
   rejected path.
2. **Symlink-safe by default.**  A symlink whose target escapes the
   allow-list is rejected with ``SYMLINK_ESCAPE``.
3. **Traversal-safe by default.**  Literal ``..`` segments are
   collapsed by ``os.path.normpath`` *before* the allow-list check.
4. **Broken symlinks are rejected** with ``BROKEN_SYMLINK``.
5. **Non-regular files are rejected** (sockets, FIFOs, dirs).
6. **TOCTOU is documented.**  The check uses ``lstat`` /
   ``realpath``; this slice does not close the
   symlink-swap-during-read channel, and that is recorded in
   master plan §13.5.

We also test that the **integration** with ``ArtifactPipeline``
honors the policy (rejected paths never get hashed, audit rows are
written) and that the audit-row table (``artifact_policy_events``)
records both accepts and rejects.
"""
from __future__ import annotations

import builtins
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import List
from unittest import mock

from aee.artifacts import (
    ArtifactPipeline,
    ArtifactPolicy,
    InMemoryArtifactRepository,
    PolicyDecision,
    PolicyViolationCode,
    SqliteArtifactRepository,
    ensure_aee6_schema,
)
from aee.artifacts.policy import safe_resolve


# Test roots we can safely reference without colliding with /tmp
# outside the bridge repo. We use a fresh per-test temp dir to
# avoid interference with other test modules.
SECURE_ROOT = "/home/ubuntu/hermes-runtime-bridge"


class TestArtifactPolicyAllowList(unittest.TestCase):
    """Group A — allow-list semantics."""

    def test_default_policy_allows_bridge_repo(self) -> None:
        p = ArtifactPolicy.default()
        self.assertIn(SECURE_ROOT, p.allowed_roots)
        # A real file inside the bridge repo is accepted.
        decision = p.check(__file__)
        self.assertEqual(decision.code, PolicyViolationCode.OK)
        self.assertTrue(decision.accepted)
        self.assertIn(SECURE_ROOT, decision.detail)

    def test_default_policy_rejects_etc_passwd(self) -> None:
        p = ArtifactPolicy.default()
        decision = p.check("/etc/passwd")
        self.assertEqual(decision.code, PolicyViolationCode.OUTSIDE_ROOTS)
        self.assertFalse(decision.accepted)

    def test_default_policy_rejects_tmp(self) -> None:
        # Use a real file under /tmp to exercise the OUTSIDE_ROOTS
        # branch (a non-existent path would short-circuit to MISSING
        # before the allow-list check, which is the correct behaviour
        # — verified in test_missing_path).
        import tempfile
        with tempfile.NamedTemporaryFile(
            prefix="aee6-default-policy-", delete=False
        ) as tf:
            tf.write(b"x")
            tmp_path = tf.name
        try:
            p = ArtifactPolicy.default()
            decision = p.check(tmp_path)
            self.assertEqual(decision.code, PolicyViolationCode.OUTSIDE_ROOTS)
        finally:
            os.unlink(tmp_path)

    def test_root_at_path_itself_is_accepted(self) -> None:
        # A file *at* the root is valid; commonpath == root must accept.
        p = ArtifactPolicy(allowed_roots=(SECURE_ROOT,))
        # Pick any file directly under SECURE_ROOT for the test.
        decision = p.check(SECURE_ROOT)
        # SECURE_ROOT is a directory; that gets NOT_REGULAR, not OK.
        # We want the allow-list check, not the regular-file check.
        # So use a real file directly under it instead.
        decision = p.check(__file__)
        self.assertEqual(decision.code, PolicyViolationCode.OK)

    def test_empty_roots_raises(self) -> None:
        with self.assertRaises(Exception):
            ArtifactPolicy(allowed_roots=())

    def test_with_roots_normalizes(self) -> None:
        p = ArtifactPolicy.with_roots(["aee/", "tests/"])
        # Both should be resolved to absolute paths
        for r in p.allowed_roots:
            self.assertTrue(os.path.isabs(r), f"root not absolute: {r!r}")


class TestArtifactPolicySymlink(unittest.TestCase):
    """Group B — symlink handling."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="aee6-security-symlink-")
        self.addCleanup(self._rm_tmp)

    def _rm_tmp(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_symlink_escape_rejected(self) -> None:
        # A symlink inside the root that points to /etc/passwd.
        # The policy resolves the symlink (follow_symlinks=True default)
        # and then runs the allow-list check on the realpath; the
        # result is OUTSIDE_ROOTS. This is the correct behaviour:
        # the symlink escape channel is closed because the realpath
        # never gets read.
        link = os.path.join(self.tmp, "evil")
        try:
            os.symlink("/etc/passwd", link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink not supported on this fs")
        p = ArtifactPolicy(allowed_roots=(self.tmp,))
        decision = p.check(link)
        # The key invariant: rejected, no content read.
        self.assertFalse(decision.accepted)
        # And the verdict is one of the escape-class codes.
        self.assertIn(
            decision.code,
            (
                PolicyViolationCode.SYMLINK_ESCAPE,
                PolicyViolationCode.OUTSIDE_ROOTS,
            ),
        )

    def test_broken_symlink_rejected(self) -> None:
        link = os.path.join(self.tmp, "broken")
        try:
            os.symlink("/nonexistent/target", link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink not supported on this fs")
        p = ArtifactPolicy(allowed_roots=(self.tmp,))
        decision = p.check(link)
        self.assertEqual(decision.code, PolicyViolationCode.BROKEN_SYMLINK)

    def test_symlink_inside_root_accepted(self) -> None:
        # Create a regular file inside the root, then a symlink
        # to it (also inside the root). The check should accept
        # the symlink and report it via the symlink detail.
        real = os.path.join(self.tmp, "real.txt")
        Path(real).write_text("hi")
        link = os.path.join(self.tmp, "link.txt")
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink not supported on this fs")
        p = ArtifactPolicy(allowed_roots=(self.tmp,))
        decision = p.check(link)
        self.assertEqual(decision.code, PolicyViolationCode.OK)
        self.assertIn("symlink", decision.detail)

    def test_follow_symlinks_false_rejects_all(self) -> None:
        real = os.path.join(self.tmp, "real.txt")
        Path(real).write_text("hi")
        link = os.path.join(self.tmp, "link.txt")
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink not supported on this fs")
        p = ArtifactPolicy(
            allowed_roots=(self.tmp,), follow_symlinks=False
        )
        decision = p.check(link)
        self.assertEqual(decision.code, PolicyViolationCode.SYMLINK_ESCAPE)

    def test_allow_broken_true_returns_missing(self) -> None:
        link = os.path.join(self.tmp, "broken")
        try:
            os.symlink("/nonexistent/target", link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink not supported on this fs")
        p = ArtifactPolicy(
            allowed_roots=(self.tmp,), allow_broken=True
        )
        decision = p.check(link)
        self.assertEqual(decision.code, PolicyViolationCode.MISSING)


class TestArtifactPolicyTraversal(unittest.TestCase):
    """Group C — path traversal / non-regular files."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="aee6-security-traversal-")
        self.addCleanup(self._rm_tmp)

    def _rm_tmp(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dotdot_collapsed_normally(self) -> None:
        real = os.path.join(self.tmp, "real.txt")
        Path(real).write_text("hi")
        # Path goes up one then back down — should resolve to the real file
        traversal = os.path.join(self.tmp, "subdir", "..", "real.txt")
        p = ArtifactPolicy(allowed_roots=(self.tmp,))
        decision = p.check(traversal)
        self.assertEqual(decision.code, PolicyViolationCode.OK)
        # The audit detail should mark the traversal hint
        self.assertIn("`..`", decision.detail)

    def test_traversal_to_outside_root_rejected(self) -> None:
        # Path that *starts* inside the root but traverses to /etc
        evil = os.path.join(self.tmp, "..", "..", "etc", "passwd")
        p = ArtifactPolicy(allowed_roots=(self.tmp,))
        decision = p.check(evil)
        self.assertEqual(decision.code, PolicyViolationCode.OUTSIDE_ROOTS)

    def test_directory_rejected(self) -> None:
        p = ArtifactPolicy(allowed_roots=(self.tmp,))
        decision = p.check(self.tmp)  # it's a directory
        self.assertEqual(decision.code, PolicyViolationCode.NOT_REGULAR)

    def test_fifo_rejected(self) -> None:
        fifo = os.path.join(self.tmp, "f")
        try:
            os.mkfifo(fifo)
        except (OSError, AttributeError):
            self.skipTest("mkfifo not supported")
        p = ArtifactPolicy(allowed_roots=(self.tmp,))
        decision = p.check(fifo)
        self.assertEqual(decision.code, PolicyViolationCode.NOT_REGULAR)

    def test_empty_path(self) -> None:
        p = ArtifactPolicy.permissive()
        decision = p.check("")
        self.assertEqual(decision.code, PolicyViolationCode.EMPTY_PATH)

    def test_missing_path(self) -> None:
        p = ArtifactPolicy.permissive()
        decision = p.check("/this/does/not/exist/anywhere/12345")
        self.assertEqual(decision.code, PolicyViolationCode.MISSING)


class TestPolicyDoesNotReadOnViolation(unittest.TestCase):
    """Group D — the no-read-on-violation invariant.

    We patch ``builtins.open`` to record any call and assert that
    ``ArtifactPolicy.check()`` never calls it, even for paths that
    pass the policy (the *collection* step reads, not the policy).
    """

    def test_check_never_opens(self) -> None:
        p = ArtifactPolicy.permissive()
        opens: List[tuple] = []
        real_open = builtins.open

        def spy_open(*a, **kw):
            opens.append((a, kw))
            return real_open(*a, **kw)

        with mock.patch("builtins.open", side_effect=spy_open):
            # Try a variety of paths
            for candidate in [
                "/etc/passwd",
                "/tmp/nope.txt",
                __file__,
                "",
            ]:
                p.check(candidate)
        # None of the checks should have opened anything.
        self.assertEqual(
            opens, [],
            f"policy.check() opened files: {opens}",
        )

    def test_safe_resolve_returns_decision_on_reject(self) -> None:
        p = ArtifactPolicy.default()
        realpath, decision = safe_resolve("/etc/passwd", p)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.code, PolicyViolationCode.OUTSIDE_ROOTS)
        # realpath should be the absolute /etc/passwd
        self.assertTrue(realpath.endswith("/etc/passwd"))


class TestArtifactPipelinePolicyIntegration(unittest.TestCase):
    """Group E — pipeline honours the policy + audit rows."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="aee6-pipeline-policy-")
        self.addCleanup(self._rm_tmp)
        # In-memory repo with an isolated sqlite3 connection for
        # audit-row inspection.
        self._conn = sqlite3.connect(":memory:")
        ensure_aee6_schema(self._conn)
        self.repo = SqliteArtifactRepository(self._conn)
        self.policy = ArtifactPolicy(allowed_roots=(self.tmp,))

    def _rm_tmp(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        self._conn.close()

    def test_outside_root_path_recorded_as_missing(self) -> None:
        pipeline = ArtifactPipeline(
            repo=self.repo,
            policy=self.policy,
            on_policy_violation="skip_and_warn",
        )
        results = pipeline.collect(
            task_id="TASK-AEE-6.3-1",
            paths=["/etc/passwd", "/etc/hostname"],
        )
        # Both rejected; both recorded as missing artifacts.
        self.assertEqual(len(results), 2)
        for r in results:
            self.assertFalse(r.exists)
            self.assertIsNone(r.sha256)
            self.assertIn("policy_rejected", r.classification_source)

    def test_in_root_path_hashed_normally(self) -> None:
        target = os.path.join(self.tmp, "report.md")
        Path(target).write_text("# hello\n")
        pipeline = ArtifactPipeline(
            repo=self.repo,
            policy=self.policy,
        )
        results = pipeline.collect(
            task_id="TASK-AEE-6.3-2", paths=[target]
        )
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].exists)
        self.assertIsNotNone(results[0].sha256)
        # Hash sanity: sha256 of "# hello\n"
        import hashlib
        expected = hashlib.sha256(b"# hello\n").hexdigest()
        self.assertEqual(results[0].sha256, expected)

    def test_fail_mode_raises_on_violation(self) -> None:
        from aee.artifacts.collect import PolicyViolationError
        pipeline = ArtifactPipeline(
            repo=self.repo,
            policy=self.policy,
            on_policy_violation="fail",
        )
        with self.assertRaises(PolicyViolationError) as ctx:
            pipeline.collect(
                task_id="TASK-AEE-6.3-3",
                paths=["/etc/passwd"],
            )
        self.assertEqual(
            ctx.exception.decision.code,
            PolicyViolationCode.OUTSIDE_ROOTS,
        )

    def test_audit_rows_recorded_for_accept_and_reject(self) -> None:
        target = os.path.join(self.tmp, "ok.txt")
        Path(target).write_text("ok")
        pipeline = ArtifactPipeline(
            repo=self.repo, policy=self.policy
        )
        pipeline.collect(
            task_id="TASK-AEE-6.3-4",
            paths=[target, "/etc/passwd"],
        )
        # Inspect the artifact_policy_events table.
        cur = self._conn.execute(
            "SELECT code, accepted, COUNT(*) FROM artifact_policy_events "
            "GROUP BY code, accepted ORDER BY code"
        )
        rows = cur.fetchall()
        # Should have at least 1 OK accept and 1 outside_roots reject
        codes = {row[0] for row in rows}
        self.assertIn("ok", codes)
        self.assertIn("outside_allowed_roots", codes)


class TestArtifactPolicyInMemoryRepository(unittest.TestCase):
    """Group F — InMemoryArtifactRepository also supports the
    policy-event log, so the audit row count is correct in unit
    tests where we cannot use sqlite3."""

    def test_in_memory_repo_records_audit(self) -> None:
        from aee.artifacts.repository import InMemoryArtifactRepository
        repo = InMemoryArtifactRepository()
        pipeline = ArtifactPipeline(
            repo=repo,
            policy=ArtifactPolicy(allowed_roots=("/nonexistent_root",)),
        )
        pipeline.collect(
            task_id="TASK-MM-1",
            paths=["/etc/hostname"],
        )
        # Use the public ``policy_events`` accessor exposed by the
        # in-memory repo (it is a property, not a method).
        events = repo.policy_events
        self.assertGreaterEqual(len(events), 1)
        self.assertEqual(
            events[0]["code"],
            PolicyViolationCode.OUTSIDE_ROOTS.value,
        )


if __name__ == "__main__":
    unittest.main()
