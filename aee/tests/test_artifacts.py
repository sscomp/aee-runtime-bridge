"""AEE-6 Artifact Pipeline unit tests.

Run with:
    .venv/bin/python -m unittest aee.tests.test_artifacts -v
"""
from __future__ import annotations

import hashlib
import io
import os
import sqlite3
import tempfile
import unittest
from typing import List

from aee.artifacts import (
    ARTIFACT_KIND_ARTIFACT,
    ARTIFACT_KIND_COVERAGE,
    ARTIFACT_KIND_LOG,
    ARTIFACT_KIND_PATCH,
    ARTIFACT_KIND_REPORT,
    ARTIFACT_KIND_UNKNOWN,
    ARTIFACT_KINDS,
    Artifact,
    ArtifactCollector,
    ArtifactError,
    ArtifactAccessError,
    ArtifactNotFoundError,
    ArtifactPipeline,
    ArtifactTooLargeError,
    InMemoryArtifactRepository,
    SqliteArtifactRepository,
    classify_by_path,
    ensure_aee6_schema,
    sha256_file,
    sha256_stream,
    sha256_text,
)
from aee.artifacts.errors import ArtifactError as _AE  # re-export sanity


def _write(path: str, content: str) -> str:
    """Helper: write content to path, return the path."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


# ---------------------------------------------------------------------------
# Kind vocabulary
# ---------------------------------------------------------------------------


class TestArtifactKindVocabulary(unittest.TestCase):
    """The 5 named kinds + UNKNOWN are stable public constants."""

    def test_known_kinds_are_unique(self):
        self.assertEqual(len(ARTIFACT_KINDS), len(set(ARTIFACT_KINDS)))

    def test_all_kinds_in_tuple(self):
        # The orchestrator can iterate ARTIFACT_KINDS; the
        # constants must all be present.
        for kind in (
            ARTIFACT_KIND_REPORT,
            ARTIFACT_KIND_PATCH,
            ARTIFACT_KIND_LOG,
            ARTIFACT_KIND_COVERAGE,
            ARTIFACT_KIND_ARTIFACT,
            ARTIFACT_KIND_UNKNOWN,
        ):
            self.assertIn(kind, ARTIFACT_KINDS)


# ---------------------------------------------------------------------------
# Path classification heuristic
# ---------------------------------------------------------------------------


class TestClassifyByPath(unittest.TestCase):
    """The path-based classifier picks the right kind for the
    4 named kinds from AEE-5 + a sane default for everything else.
    """

    def test_report_md(self):
        self.assertEqual(classify_by_path("/tmp/run/report.md"), ARTIFACT_KIND_REPORT)

    def test_report_html(self):
        self.assertEqual(classify_by_path("/tmp/run/report.html"), ARTIFACT_KIND_REPORT)

    def test_summary_json(self):
        self.assertEqual(classify_by_path("/tmp/run/summary.json"), ARTIFACT_KIND_REPORT)

    def test_final_prefix_html(self):
        self.assertEqual(
            classify_by_path("/tmp/run/Final-石垣島_五日旅行手冊.html"),
            ARTIFACT_KIND_REPORT,
        )

    def test_diff_suffix(self):
        self.assertEqual(classify_by_path("/tmp/run/patch.diff"), ARTIFACT_KIND_PATCH)

    def test_patch_suffix(self):
        self.assertEqual(classify_by_path("/tmp/run/0001-foo.patch"), ARTIFACT_KIND_PATCH)

    def test_log_suffix(self):
        self.assertEqual(classify_by_path("/tmp/run/test.log"), ARTIFACT_KIND_LOG)

    def test_build_log_prefix(self):
        self.assertEqual(classify_by_path("/tmp/run/build_log.txt"), ARTIFACT_KIND_LOG)

    def test_coverage_xml(self):
        self.assertEqual(classify_by_path("/tmp/run/coverage.xml"), ARTIFACT_KIND_COVERAGE)

    def test_lcov_info(self):
        self.assertEqual(classify_by_path("/tmp/run/lcov.info"), ARTIFACT_KIND_COVERAGE)

    def test_known_extension_falls_to_artifact(self):
        self.assertEqual(
            classify_by_path("/tmp/run/data.csv"), ARTIFACT_KIND_ARTIFACT
        )

    def test_no_extension_is_unknown(self):
        self.assertEqual(classify_by_path("/tmp/run/Makefile"), ARTIFACT_KIND_UNKNOWN)

    def test_empty_path_is_unknown(self):
        self.assertEqual(classify_by_path(""), ARTIFACT_KIND_UNKNOWN)

    def test_classify_is_case_insensitive(self):
        self.assertEqual(classify_by_path("/tmp/REPORT.MD"), ARTIFACT_KIND_REPORT)

    def test_diff_beats_log_when_both_match(self):
        # `.diff` ends in `.log`? No — but the rule order matters:
        # `_CLASSIFY_RULES` puts PATCH first, so a file named
        # "build.log.diff" is a patch, not a log. This is the
        # intended behavior.
        self.assertEqual(
            classify_by_path("/tmp/build.log.diff"), ARTIFACT_KIND_PATCH
        )


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


class TestSha256Helpers(unittest.TestCase):
    def test_text_hash_matches_hashlib(self):
        self.assertEqual(sha256_text("hello"), hashlib.sha256(b"hello").hexdigest())

    def test_text_hash_unicode(self):
        # 一二三大寫進 UTF-8
        self.assertEqual(
            sha256_text("鼎鼎"),
            hashlib.sha256("鼎鼎".encode("utf-8")).hexdigest(),
        )

    def test_file_hash_matches_hashlib(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write(os.path.join(tmp, "x.txt"), "hello world")
            self.assertEqual(
                sha256_file(p), hashlib.sha256(b"hello world").hexdigest()
            )

    def test_file_hash_empty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "empty.bin")
            with open(p, "wb") as f:
                f.write(b"")
            self.assertEqual(sha256_file(p), hashlib.sha256(b"").hexdigest())

    def test_file_hash_large_chunked(self):
        # 1MB file: exercises the chunked read path.
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "big.bin")
            with open(p, "wb") as f:
                f.write(b"a" * (1024 * 1024))
            expected = hashlib.sha256(b"a" * (1024 * 1024)).hexdigest()
            self.assertEqual(sha256_file(p), expected)

    def test_stream_hash_matches(self):
        data = b"stream me"
        stream = io.BytesIO(data)
        self.assertEqual(
            sha256_stream(stream), hashlib.sha256(data).hexdigest()
        )

    def test_file_too_large_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "big.bin")
            with open(p, "wb") as f:
                f.write(b"x" * 10_000)
            with self.assertRaises(ValueError):
                sha256_file(p, max_bytes=1000)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TestArtifactErrors(unittest.TestCase):
    def test_subclass_tree(self):
        for cls in (ArtifactNotFoundError, ArtifactAccessError, ArtifactTooLargeError):
            self.assertTrue(issubclass(cls, ArtifactError))
            self.assertTrue(hasattr(cls, "code"))

    def test_path_attribute(self):
        e = ArtifactNotFoundError("nope", path="/foo/bar")
        self.assertEqual(e.path, "/foo/bar")
        self.assertIn("/foo/bar", str(e))


# ---------------------------------------------------------------------------
# In-memory repository
# ---------------------------------------------------------------------------


class TestInMemoryArtifactRepository(unittest.TestCase):
    def test_save_assigns_id_and_returns(self):
        repo = InMemoryArtifactRepository()
        a = Artifact(
            path="/tmp/x.md", task_id="T1", exists=True,
            sha256="abc", size=3, mtime="2026-01-01T00:00:00Z",
        )
        saved = repo.save(a)
        self.assertIsNotNone(saved.artifact_id)
        self.assertEqual(len(repo), 1)

    def test_get_unknown_id_raises(self):
        repo = InMemoryArtifactRepository()
        with self.assertRaises(ArtifactNotFoundError):
            repo.get("nonexistent")

    def test_resave_same_path_bumps_version(self):
        repo = InMemoryArtifactRepository()
        a = Artifact(path="/tmp/x.md", task_id="T1", exists=True)
        s1 = repo.save(a)
        s2 = repo.save(a)
        self.assertNotEqual(s1.artifact_id, s2.artifact_id)
        self.assertEqual(s2, repo.latest("T1", "/tmp/x.md"))

    def test_find_filters_by_kind(self):
        repo = InMemoryArtifactRepository()
        repo.save(Artifact(path="/tmp/r.md", task_id="T1", kind=ARTIFACT_KIND_REPORT, exists=True))
        repo.save(Artifact(path="/tmp/p.diff", task_id="T1", kind=ARTIFACT_KIND_PATCH, exists=True))
        self.assertEqual(len(repo.find("T1", kind=ARTIFACT_KIND_REPORT)), 1)

    def test_find_filters_by_path(self):
        repo = InMemoryArtifactRepository()
        repo.save(Artifact(path="/tmp/a.md", task_id="T1", exists=True))
        repo.save(Artifact(path="/tmp/b.md", task_id="T1", exists=True))
        self.assertEqual(
            len(repo.find("T1", path="/tmp/a.md")), 1
        )

    def test_latest_missing_returns_none(self):
        repo = InMemoryArtifactRepository()
        self.assertIsNone(repo.latest("T1", "/nope"))


# ---------------------------------------------------------------------------
# Collector (per-path)
# ---------------------------------------------------------------------------


class TestArtifactCollector(unittest.TestCase):
    def test_collect_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write(os.path.join(tmp, "report.md"), "# hi")
            c = ArtifactCollector()
            a = c.collect_one("T1", p)
            self.assertTrue(a.exists)
            self.assertEqual(a.kind, ARTIFACT_KIND_REPORT)
            self.assertEqual(a.size, 4)
            self.assertEqual(a.sha256, hashlib.sha256(b"# hi").hexdigest())
            self.assertEqual(a.task_id, "T1")
            self.assertEqual(a.classification_source, "auto")

    def test_collect_missing_raises(self):
        c = ArtifactCollector()
        with self.assertRaises(ArtifactNotFoundError):
            c.collect_one("T1", "/no/such/file.md")

    def test_collect_missing_helper(self):
        c = ArtifactCollector()
        a = c.collect_missing("T1", "/no/such/file.md")
        self.assertFalse(a.exists)
        self.assertIsNone(a.sha256)
        self.assertEqual(a.kind, ARTIFACT_KIND_UNKNOWN)

    def test_kind_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write(os.path.join(tmp, "weird.md"), "x")
            c = ArtifactCollector()
            a = c.collect_one("T1", p, kind_override=ARTIFACT_KIND_LOG)
            self.assertEqual(a.kind, ARTIFACT_KIND_LOG)
            self.assertEqual(a.classification_source, "override")

    def test_empty_path_raises(self):
        c = ArtifactCollector()
        with self.assertRaises(ArtifactError):
            c.collect_one("T1", "")

    def test_content_type_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write(os.path.join(tmp, "data.json"), "{}")
            c = ArtifactCollector()
            a = c.collect_one("T1", p)
            self.assertEqual(a.content_type, "application/json")

    def test_too_large_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "big.bin")
            with open(p, "wb") as f:
                f.write(b"x" * 10_000)
            c = ArtifactCollector(max_bytes=1000)
            with self.assertRaises(ArtifactTooLargeError):
                c.collect_one("T1", p)

    def test_absolute_path_canonicalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write(os.path.join(tmp, "r.md"), "x")
            # Relative path input should still resolve to absolute.
            c = ArtifactCollector()
            old = os.getcwd()
            try:
                os.chdir(tmp)
                a = c.collect_one("T1", "./r.md")
                self.assertTrue(os.path.isabs(a.path))
                self.assertTrue(a.path.endswith("r.md"))
            finally:
                os.chdir(old)


# ---------------------------------------------------------------------------
# Pipeline (batch)
# ---------------------------------------------------------------------------


class TestArtifactPipeline(unittest.TestCase):
    def test_collect_batch_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            p1 = _write(os.path.join(tmp, "report.md"), "report")
            p2 = _write(os.path.join(tmp, "test.log"), "log")
            p3 = _write(os.path.join(tmp, "patch.diff"), "patch")
            repo = InMemoryArtifactRepository()
            pl = ArtifactPipeline(repo=repo)
            results = pl.collect("T1", [p1, p2, p3])
            self.assertEqual(len(results), 3)
            self.assertEqual(len(repo), 3)
            kinds = sorted([r.kind for r in results])
            self.assertEqual(
                kinds,
                sorted([ARTIFACT_KIND_REPORT, ARTIFACT_KIND_LOG, ARTIFACT_KIND_PATCH]),
            )

    def test_missing_path_recorded_not_raised(self):
        repo = InMemoryArtifactRepository()
        pl = ArtifactPipeline(repo=repo)
        results = pl.collect("T1", ["/no/such/file.md"])
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].exists)
        self.assertIsNone(results[0].sha256)

    def test_mixed_existing_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write(os.path.join(tmp, "r.md"), "ok")
            repo = InMemoryArtifactRepository()
            pl = ArtifactPipeline(repo=repo)
            results = pl.collect("T1", [p, "/nope.md"])
            self.assertEqual(len(results), 2)
            self.assertTrue(results[0].exists)
            self.assertFalse(results[1].exists)

    def test_classifications_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write(os.path.join(tmp, "weird.md"), "x")
            repo = InMemoryArtifactRepository()
            pl = ArtifactPipeline(repo=repo)
            results = pl.collect(
                "T1", [p], classifications={p: ARTIFACT_KIND_COVERAGE}
            )
            self.assertEqual(results[0].kind, ARTIFACT_KIND_COVERAGE)
            self.assertEqual(results[0].classification_source, "override")

    def test_pipeline_returns_persisted_records(self):
        # Each saved record has an artifact_id; the pipeline must
        # return those (not the input dataclasses).
        with tempfile.TemporaryDirectory() as tmp:
            p = _write(os.path.join(tmp, "r.md"), "x")
            repo = InMemoryArtifactRepository()
            pl = ArtifactPipeline(repo=repo)
            results = pl.collect("T1", [p])
            self.assertIsNotNone(results[0].artifact_id)


# ---------------------------------------------------------------------------
# SQLite repository (uses an in-memory :memory: connection)
# ---------------------------------------------------------------------------


def _mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_aee6_schema(conn)
    return conn


class TestSqliteArtifactRepository(unittest.TestCase):
    def test_round_trip(self):
        conn = _mem_conn()
        repo = SqliteArtifactRepository(conn=conn)
        a = Artifact(
            path="/tmp/r.md", task_id="T1", kind=ARTIFACT_KIND_REPORT,
            sha256="abc", size=10, mtime="2026-01-01T00:00:00Z",
            exists=True, content_type="text/markdown",
            classification_source="auto",
        )
        saved = repo.save(a)
        self.assertIsNotNone(saved.artifact_id)
        got = repo.get(saved.artifact_id)
        self.assertEqual(got.path, "/tmp/r.md")
        self.assertEqual(got.kind, ARTIFACT_KIND_REPORT)
        self.assertEqual(got.sha256, "abc")
        self.assertEqual(got.size, 10)
        self.assertTrue(got.exists)

    def test_save_is_idempotent_at_v1(self):
        conn = _mem_conn()
        repo = SqliteArtifactRepository(conn=conn)
        a = Artifact(path="/tmp/r.md", task_id="T1", exists=True, sha256="x")
        s1 = repo.save(a)
        s2 = repo.save(a)
        # Both saves have an id; the version should differ.
        self.assertNotEqual(s1.artifact_id, s2.artifact_id)
        self.assertEqual(s2, repo.latest("T1", "/tmp/r.md"))

    def test_find_filters(self):
        conn = _mem_conn()
        repo = SqliteArtifactRepository(conn=conn)
        repo.save(Artifact(path="/tmp/r.md", task_id="T1", kind=ARTIFACT_KIND_REPORT, exists=True))
        repo.save(Artifact(path="/tmp/l.log", task_id="T1", kind=ARTIFACT_KIND_LOG, exists=True))
        repo.save(Artifact(path="/tmp/r2.md", task_id="T2", kind=ARTIFACT_KIND_REPORT, exists=True))
        self.assertEqual(len(repo.find("T1", kind=ARTIFACT_KIND_REPORT)), 1)
        self.assertEqual(len(repo.find("T1")), 2)
        self.assertEqual(len(repo.find("T2")), 1)

    def test_schema_is_idempotent(self):
        # Running ensure_aee6_schema twice should not error.
        conn = _mem_conn()
        ensure_aee6_schema(conn)  # second call
        # Insert + read still works.
        repo = SqliteArtifactRepository(conn=conn)
        repo.save(Artifact(path="/tmp/x", task_id="T1", exists=True))

    def test_get_unknown_raises(self):
        conn = _mem_conn()
        repo = SqliteArtifactRepository(conn=conn)
        with self.assertRaises(ArtifactNotFoundError):
            repo.get("nope")

    def test_latest_returns_highest_version(self):
        conn = _mem_conn()
        repo = SqliteArtifactRepository(conn=conn)
        a = Artifact(path="/tmp/r.md", task_id="T1", exists=True, sha256="v1")
        s1 = repo.save(a)
        a2 = Artifact(path="/tmp/r.md", task_id="T1", exists=True, sha256="v2")
        s2 = repo.save(a2)
        latest = repo.latest("T1", "/tmp/r.md")
        self.assertEqual(latest.artifact_id, s2.artifact_id)


# ---------------------------------------------------------------------------
# End-to-end: pipeline + sqlite repo on a real tmp file
# ---------------------------------------------------------------------------


class TestPipelineE2EWithSQLite(unittest.TestCase):
    def test_full_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            content = "hello artifact world"
            p = _write(os.path.join(tmp, "final.md"), content)
            conn = _mem_conn()
            repo = SqliteArtifactRepository(conn=conn)
            pl = ArtifactPipeline(repo=repo)
            results = pl.collect("T-E2E", [p])
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0].exists)
            self.assertEqual(
                results[0].sha256, hashlib.sha256(content.encode()).hexdigest()
            )
            # Round-trip back from SQLite
            got = repo.get(results[0].artifact_id)
            self.assertEqual(got.sha256, results[0].sha256)


if __name__ == "__main__":
    unittest.main()
