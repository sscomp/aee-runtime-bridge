"""WO-FIX-ARTIFACT-PATH-CASE-PRESERVATION tests.

The Completion Gate's ``expected_artifacts`` contract must preserve
the original case of declared filesystem paths. Linux filesystems are
case-sensitive: declaring ``/home/ubuntu/Abacus/report.md`` and
having the gate stat ``/home/ubuntu/abacus/report.md`` produces a
false ``missing_expected_artifacts`` failure.

Root cause this test pins: ``manager.create()`` previously persisted
``expected_artifacts`` via ``db.encode_capabilities`` which calls
``normalize_capabilities`` → ``v.strip().lower()``. Capability
strings are case-insensitive identifiers, but filesystem paths are
not. The fix introduces ``db.encode_artifact_paths`` which trims /
dedupes / sorts WITHOUT case-folding.

Test matrix:
  1. ``/home/ubuntu/Abacus/report.md`` stored with case preserved.
  2. ``/home/ubuntu/Abacus/report.md`` and
     ``/home/ubuntu/abacus/report.md`` are treated as two distinct
     paths (NOT collapsed by case-folding) — only the one that
     actually exists on disk passes the gate.
  3. Existing matching behavior (all-present completes, missing
     fails) is preserved.
  4. ``db.encode_artifact_paths`` round-trips paths with uppercase,
     mixed case, and trailing-slash variants.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from dispatcher.db import encode_artifact_paths, encode_capabilities, get_conn
from dispatcher.manager import TaskManager


class TestArtifactPathCasePreservation(unittest.TestCase):
    """Storage-layer: ``encode_artifact_paths`` does NOT case-fold."""

    def test_encode_artifact_paths_preserves_uppercase_dir(self):
        """``/home/ubuntu/Abacus/report.md`` survives storage intact."""
        blob = encode_artifact_paths(["/home/ubuntu/Abacus/report.md"])
        self.assertEqual(blob, '["/home/ubuntu/Abacus/report.md"]')

    def test_encode_artifact_paths_does_not_lowercase(self):
        """Negative parity with ``encode_capabilities`` (which DOES)."""
        path = "/home/ubuntu/Abacus/Report.MD"
        # Capabilities helper lowercases (this is its contract).
        self.assertEqual(
            json.loads(encode_capabilities([path])),
            ["/home/ubuntu/abacus/report.md"],
        )
        # Artifact-paths helper MUST preserve case.
        self.assertEqual(
            json.loads(encode_artifact_paths([path])),
            ["/home/ubuntu/Abacus/Report.MD"],
        )

    def test_encode_artifact_paths_dedupes_and_sorts(self):
        """Storage determinism: trim / dedupe / sort, no case-fold."""
        blob = encode_artifact_paths([
            "/home/ubuntu/Abacus/b.md",
            "  /home/ubuntu/Abacus/a.md  ",
            "/home/ubuntu/Abacus/b.md",   # dup
            "",                           # empty dropped
            "   ",                        # whitespace dropped
        ])
        self.assertEqual(
            json.loads(blob),
            ["/home/ubuntu/Abacus/a.md", "/home/ubuntu/Abacus/b.md"],
        )

    def test_encode_artifact_paths_distinct_case_paths_not_collapsed(self):
        """``/Abacus/x.md`` and ``/abacus/x.md`` are two distinct entries."""
        blob = encode_artifact_paths([
            "/home/ubuntu/Abacus/report.md",
            "/home/ubuntu/abacus/report.md",
        ])
        stored = json.loads(blob)
        self.assertEqual(len(stored), 2)
        self.assertIn("/home/ubuntu/Abacus/report.md", stored)
        self.assertIn("/home/ubuntu/abacus/report.md", stored)

    def test_encode_artifact_paths_none_and_empty(self):
        self.assertEqual(encode_artifact_paths(None), "[]")
        self.assertEqual(encode_artifact_paths([]), "[]")


class TestCompletionGateCasePreservation(unittest.TestCase):
    """End-to-end gate behavior with case-sensitive paths."""

    def setUp(self) -> None:
        # Build a real directory tree that mixes case so the gate
        # must preserve case to distinguish existing vs. missing.
        self.tmpdir = tempfile.mkdtemp(prefix="wo-case-fix-")
        # /tmp/.../Abacus/report.md EXISTS (uppercase A)
        self.upper_dir = Path(self.tmpdir) / "Abacus"
        self.upper_dir.mkdir()
        self.existing_upper = self.upper_dir / "report.md"
        self.existing_upper.write_text("hello", encoding="utf-8")
        # /tmp/.../abacus/report.md does NOT exist (lowercase a)
        self.missing_lower = Path(self.tmpdir) / "abacus" / "report.md"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _create_start_complete(
        self, *, title: str, expected_artifacts, output_text: str = "done"
    ):
        m = TaskManager()
        t = m.create(
            title=title,
            type="research",
            input_text="placeholder input",
            expected_artifacts=expected_artifacts,
        )
        m.start(t.task_id, hermes_run_id="test-case-fix-run")
        out = m.complete(t.task_id, output_text=output_text)
        return m, t, out

    def test_uppercase_dir_path_preserved_in_db(self):
        """The persisted ``expected_artifacts_json`` keeps the original case."""
        m = TaskManager()
        t = m.create(
            title="wo-case-persist",
            type="research",
            input_text="x",
            expected_artifacts=[str(self.existing_upper)],
        )
        conn = get_conn()
        row = conn.execute(
            "SELECT expected_artifacts_json FROM tasks WHERE task_id = ?",
            (t.task_id,),
        ).fetchone()
        blob = row["expected_artifacts_json"]
        # Must contain the original uppercase path, NOT a lowercased one.
        self.assertIn("Abacus", blob)
        self.assertNotIn("abacus", blob)
        self.assertEqual(
            json.loads(blob),
            [str(self.existing_upper)],
        )

    def test_uppercase_existing_path_passes_gate(self):
        """``/tmp/.../Abacus/report.md`` (existing) → completed.

        This is the regression test for the false
        ``missing_expected_artifacts`` failure: pre-fix the path was
        lowercased to ``/tmp/.../abacus/report.md`` (which does NOT
        exist) and the gate failed.
        """
        m, t, out = self._create_start_complete(
            title="wo-case-upper-exists",
            expected_artifacts=[str(self.existing_upper)],
        )
        self.assertEqual(out.status, "completed")
        self.assertEqual(out.expected_artifacts, [str(self.existing_upper)])

    def test_lowercase_missing_path_fails_gate(self):
        """``/tmp/.../abacus/report.md`` (does NOT exist) → failed.

        Pre-fix the lowercased path would collide with the uppercased
        existing path's lowercased form; post-fix they are distinct
        and the gate correctly reports the lowercase one as missing.
        """
        m, t, out = self._create_start_complete(
            title="wo-case-lower-missing",
            expected_artifacts=[str(self.missing_lower)],
        )
        self.assertEqual(out.status, "failed")
        self.assertIn("missing_expected_artifacts", out.error_message or "")
        self.assertIn(str(self.missing_lower), out.error_message or "")

    def test_mixed_case_paths_distinct_in_single_contract(self):
        """Both paths declared together: only the uppercase one exists.

        Pinning the exact semantics: when the orchestrator declares
        ``[/Abacus/report.md, /abacus/report.md]`` the gate must
        accept the upper (exists) and reject the lower (missing).
        Pre-fix both were lowercased to the same string, deduped to
        one entry, and the gate either passed (if the lowercased form
        happened to exist) or failed (if it didn't) — the contract
        was non-deterministic w.r.t. case.
        """
        m, t, out = self._create_start_complete(
            title="wo-case-mixed",
            expected_artifacts=[
                str(self.existing_upper),
                str(self.missing_lower),
            ],
        )
        self.assertEqual(out.status, "failed")
        self.assertIn("missing_expected_artifacts", out.error_message or "")
        # The lower (missing) path is named in the error; the upper
        # (existing) path is NOT.
        self.assertIn(str(self.missing_lower), out.error_message or "")
        self.assertNotIn(
            str(self.existing_upper),
            (out.error_message or "").replace(
                str(self.missing_lower), ""
            ),
        )

    def test_existing_behavior_preserved_all_present(self):
        """All-declared-present still completes (regression guard)."""
        # Use a path that has no case ambiguity to keep this test
        # focused on the existing happy path.
        plain = Path(self.tmpdir) / "plain.txt"
        plain.write_text("ok", encoding="utf-8")
        m, t, out = self._create_start_complete(
            title="wo-case-plain",
            expected_artifacts=[str(plain)],
        )
        self.assertEqual(out.status, "completed")


if __name__ == "__main__":
    unittest.main()