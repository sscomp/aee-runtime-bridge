"""AEE-7.2 — contract tests for ``aee.artifacts.policy_factory``.

These tests pin the behavior of the per-job ``repo_root`` factory
and its integration with the ``ArtifactPolicy`` allow-list. They
verify:

1. None / empty / whitespace inputs return ``None`` (fail-safe).
2. Absolute paths produce a policy with the resolved absolute root.
3. Relative paths are rejected with ``ValueError`` (author must
   commit to an absolute root).
4. Wrong-typed inputs are rejected with ``TypeError``.
5. ``repo_root_from_runtime_requirements`` is tolerant of
   ``None``, empty dict, wrong-typed values, and missing keys.
6. End-to-end: ``ArtifactPipeline`` built with the per-job
   policy *rejects* paths outside the repo and *accepts* paths
   inside (incl. ``..`` segments that stay inside, symlink
   escapes outside, broken symlinks).
7. Symlink escape: a symlink that resolves to a file outside the
   repo is rejected.
8. Broken symlink: a symlink whose target does not exist is
   rejected when ``allow_broken=False`` (the default).

The tests are isolated to ``aee/tests/`` and use only stdlib +
``unittest``. They do **not** touch the live dispatcher DB.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from aee.artifacts import ArtifactPipeline, SqliteArtifactRepository
from aee.artifacts.policy_factory import (
    policy_for_repo_root,
    repo_root_from_runtime_requirements,
)
from aee.artifacts.policy import ArtifactPolicy


# ---------------------------------------------------------------------------
# repo_root_from_runtime_requirements
# ---------------------------------------------------------------------------


class TestRepoRootExtraction(unittest.TestCase):
    def test_none_data_returns_none(self) -> None:
        self.assertIsNone(repo_root_from_runtime_requirements(None))

    def test_empty_dict_returns_none(self) -> None:
        self.assertIsNone(repo_root_from_runtime_requirements({}))

    def test_missing_key_returns_none(self) -> None:
        self.assertIsNone(
            repo_root_from_runtime_requirements({"other_key": "x"})
        )

    def test_none_value_returns_none(self) -> None:
        self.assertIsNone(
            repo_root_from_runtime_requirements({"repo_root": None})
        )

    def test_empty_string_returns_none(self) -> None:
        self.assertIsNone(
            repo_root_from_runtime_requirements({"repo_root": ""})
        )

    def test_whitespace_only_returns_none(self) -> None:
        self.assertIsNone(
            repo_root_from_runtime_requirements({"repo_root": "   "})
        )

    def test_string_is_stripped(self) -> None:
        self.assertEqual(
            repo_root_from_runtime_requirements(
                {"repo_root": "  /tmp/foo  "}
            ),
            "/tmp/foo",
        )

    def test_pathlike_is_coerced(self) -> None:
        # PathLike inputs are accepted (defensive — some callers
        # may pass a ``pathlib.Path``).
        result = repo_root_from_runtime_requirements(
            {"repo_root": Path("/tmp/bar")}
        )
        self.assertEqual(result, "/tmp/bar")

    def test_non_string_non_pathlike_returns_none(self) -> None:
        # ints, lists, dicts are dropped silently.
        self.assertIsNone(
            repo_root_from_runtime_requirements({"repo_root": 42})
        )
        self.assertIsNone(
            repo_root_from_runtime_requirements({"repo_root": ["a"]})
        )


# ---------------------------------------------------------------------------
# policy_for_repo_root
# ---------------------------------------------------------------------------


class TestPolicyForRepoRoot(unittest.TestCase):
    def test_none_returns_none(self) -> None:
        self.assertIsNone(policy_for_repo_root(None))

    def test_empty_string_returns_none(self) -> None:
        self.assertIsNone(policy_for_repo_root(""))

    def test_whitespace_returns_none(self) -> None:
        self.assertIsNone(policy_for_repo_root("   "))

    def test_relative_path_raises(self) -> None:
        with self.assertRaises(ValueError):
            policy_for_repo_root("relative/path")

    def test_dot_relative_path_raises(self) -> None:
        with self.assertRaises(ValueError):
            policy_for_repo_root("./foo")

    def test_parent_relative_path_raises(self) -> None:
        with self.assertRaises(ValueError):
            policy_for_repo_root("../foo")

    def test_non_string_raises_typeerror(self) -> None:
        with self.assertRaises(TypeError):
            policy_for_repo_root(42)  # type: ignore[arg-type]

    def test_absolute_path_returns_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = policy_for_repo_root(tmp)
            assert policy is not None  # type narrowing
            self.assertEqual(len(policy.allowed_roots), 1)
            self.assertEqual(
                policy.allowed_roots[0], os.path.abspath(tmp)
            )

    def test_policy_description_includes_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = policy_for_repo_root(tmp)
            assert policy is not None  # type narrowing
            self.assertIn(os.path.abspath(tmp), policy.description)
            self.assertTrue(policy.description.startswith("per_job_repo_root:"))


# ---------------------------------------------------------------------------
# End-to-end: policy applied to a real pipeline
# ---------------------------------------------------------------------------


class TestPolicyEnforcement(unittest.TestCase):
    """Drive a real ``ArtifactPipeline`` with a per-job policy.

    Uses an in-memory SQLite repository so the tests do not
    touch the live dispatcher DB.
    """

    def setUp(self) -> None:
        # Repo layout:
        #   tmp/
        #     repo/         (the allowed root)
        #       inside.txt
        #     outside.txt   (must be rejected)
        #     repo/link_to_outside -> ../outside.txt  (symlink escape)
        #     repo/broken -> /nonexistent  (broken symlink)
        self._tmp = tempfile.mkdtemp(prefix="aee72-policy-")
        self._repo = os.path.join(self._tmp, "repo")
        os.makedirs(self._repo, exist_ok=True)
        with open(os.path.join(self._repo, "inside.txt"), "w") as f:
            f.write("inside")
        with open(os.path.join(self._tmp, "outside.txt"), "w") as f:
            f.write("outside")
        # Symlink inside the repo pointing outside.
        os.symlink(
            os.path.join(self._tmp, "outside.txt"),
            os.path.join(self._repo, "link_to_outside"),
        )
        # Broken symlink inside the repo.
        os.symlink(
            "/nonexistent-aee72-target",
            os.path.join(self._repo, "broken"),
        )

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _build_pipeline(self, policy: ArtifactPolicy) -> ArtifactPipeline:
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        # The pipeline writes to the artifacts / policy_events
        # tables; ``ensure_aee6_schema`` is the public, idempotent
        # way to provision them. In production the dispatcher
        # calls this on get_conn(); tests must do it themselves
        # for in-memory connections.
        from aee.artifacts.repository import ensure_aee6_schema
        ensure_aee6_schema(conn)
        return ArtifactPipeline(
            repo=SqliteArtifactRepository(conn),
            policy=policy,
        )

    def test_inside_path_accepted(self) -> None:
        policy = policy_for_repo_root(self._repo)
        assert policy is not None
        pipeline = self._build_pipeline(policy)
        results = pipeline.collect(
            "t-inside",
            [os.path.join(self._repo, "inside.txt")],
        )
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].exists)
        # The classifier picks ``artifact`` for any path the
        # collector decides to persist. The exact kind is
        # not the contract under test — only that the path
        # was accepted.
        self.assertIsNotNone(results[0].kind)

    def test_outside_path_rejected(self) -> None:
        policy = policy_for_repo_root(self._repo)
        assert policy is not None
        pipeline = self._build_pipeline(policy)
        results = pipeline.collect(
            "t-outside",
            [os.path.join(self._tmp, "outside.txt")],
        )
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].exists)

    def test_symlink_escape_rejected(self) -> None:
        policy = policy_for_repo_root(self._repo)
        assert policy is not None
        pipeline = self._build_pipeline(policy)
        results = pipeline.collect(
            "t-sym",
            [os.path.join(self._repo, "link_to_outside")],
        )
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].exists)

    def test_broken_symlink_rejected(self) -> None:
        policy = policy_for_repo_root(self._repo)
        assert policy is not None
        pipeline = self._build_pipeline(policy)
        results = pipeline.collect(
            "t-broken",
            [os.path.join(self._repo, "broken")],
        )
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].exists)

    def test_per_job_policy_does_not_leak_to_default(self) -> None:
        """A policy built for one repo must not affect a different
        pipeline's default policy."""
        policy_a = policy_for_repo_root(self._repo)
        assert policy_a is not None
        # Build a *separate* pipeline that uses the default policy
        # and verify the symlink escape is *accepted* there (the
        # default is permissive).
        pipeline_default = self._build_pipeline(
            ArtifactPolicy.permissive()
        )
        results = pipeline_default.collect(
            "t-default",
            [os.path.join(self._repo, "link_to_outside")],
        )
        # Default policy: the symlink resolves to a real file
        # outside the repo, but the policy does not restrict
        # roots, so it is accepted.
        self.assertTrue(results[0].exists)

    def test_default_policy_used_when_repo_root_none(self) -> None:
        """If the manager passes ``None`` (no per-job constraint),
        the factory returns ``None`` and the caller uses its
        default. The default must remain permissive — not the
        per-job restricted policy."""
        per_job = policy_for_repo_root(None)
        self.assertIsNone(per_job)
        # The manager code path uses the pipeline's own default
        # (ArtifactPolicy.permissive()) when per_job is None.
        pipeline = self._build_pipeline(ArtifactPolicy.permissive())
        results = pipeline.collect(
            "t-default2",
            [os.path.join(self._tmp, "outside.txt")],
        )
        self.assertTrue(results[0].exists)


# ---------------------------------------------------------------------------
# Different jobs, different roots
# ---------------------------------------------------------------------------


class TestPerJobPolicyIsolation(unittest.TestCase):
    """Two jobs with different repo_roots must not share state.

    The factory returns a *new* ``ArtifactPolicy`` instance per
    call — there is no shared cache. Verify the per-job
    constraint for job A does not widen the constraint for job B.
    """

    def setUp(self) -> None:
        self._tmp_a = tempfile.mkdtemp(prefix="aee72-A-")
        self._tmp_b = tempfile.mkdtemp(prefix="aee72-B-")
        for p in (
            os.path.join(self._tmp_a, "a.txt"),
            os.path.join(self._tmp_b, "b.txt"),
        ):
            with open(p, "w") as f:
                f.write("x")

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp_a, ignore_errors=True)
        shutil.rmtree(self._tmp_b, ignore_errors=True)

    def test_distinct_policies_for_distinct_roots(self) -> None:
        policy_a = policy_for_repo_root(self._tmp_a)
        policy_b = policy_for_repo_root(self._tmp_b)
        assert policy_a is not None and policy_b is not None
        # The two policies must have different allowed_roots.
        self.assertNotEqual(
            policy_a.allowed_roots, policy_b.allowed_roots
        )

    def test_a_rejects_b_files(self) -> None:
        import sqlite3
        from aee.artifacts.repository import ensure_aee6_schema
        policy_a = policy_for_repo_root(self._tmp_a)
        assert policy_a is not None
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        ensure_aee6_schema(conn)
        pipeline = ArtifactPipeline(
            repo=SqliteArtifactRepository(conn),
            policy=policy_a,
        )
        results = pipeline.collect(
            "t-a-vs-b",
            [os.path.join(self._tmp_b, "b.txt")],
        )
        # b.txt is in tmp_b, not tmp_a → outside the allow-list
        # → rejected.
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].exists)


if __name__ == "__main__":
    unittest.main()
