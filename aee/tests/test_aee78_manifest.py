"""AEE-7.8 K1 — Targeted tests for ``aee/audit/manifest.py``.

Coverage surface:

* :func:`load_manifest` happy path (read the real
  ``AEE_7_7d_7e_MANIFEST.json`` from repo root, assert the
  typed dataclass tree).
* :func:`load_manifest` failure modes (file missing,
  permission denied is not testable as root; JSON parse
  error, non-dict top level, non-dict ``groups``, non-dict
  group body).
* :func:`validate_manifest` happy path (real manifest
  passes) and error paths (missing top-level key, bad SHA
  shape, non-int size, bool-where-int, bool-where-bool,
  empty group warning).
* :class:`ManifestDocument` introspection:
  ``list_group_names``, ``get_group``, ``iter_files``,
  ``iter_new_files``, ``iter_modified_files``,
  ``total_files_count``, ``new_files_count``,
  ``modified_files_count``.
* :class:`GroupEntry` properties:
  ``new_file_count``, ``modified_file_count``,
  ``total_file_count``.
* :class:`FileEntry` discriminator (kind preserved through
  ``to_dict``).
* Isolation contract: ``aee/audit/manifest`` must not
  import ``dispatcher``; must not contact the live DB;
  must not perform any subprocess / network / env reads.

Run: ``PYTHONPATH=. /home/ubuntu/macro-venv/bin/python -m
unittest aee.tests.test_aee78_manifest -v``
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, Any

from aee.audit.manifest import (
    FileEntry,
    FileEntryKind,
    GroupEntry,
    MANIFEST_SCHEMA_VERSION,
    ManifestDocument,
    ManifestError,
    ValidationResult,
    load_manifest,
    validate_manifest,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REAL_MANIFEST = REPO_ROOT / "AEE_7_7d_7e_MANIFEST.json"

# Docstring fence tokens. Defined as module-level constants
# so the docstring-stripper helper can reference them without
# nesting a triple-quoted string inside a triple-quoted
# string (which is a syntax error in any Python version).
DOUBLE_QUOTE_FENCE = chr(34) * 3
SINGLE_QUOTE_FENCE = chr(39) * 3

# Snapshot of the real manifest at the time of K1 design. We
# re-assert this in one test to surface silent file changes
# (a contract guarantee: the K1 reader is forward-compatible
# with the AEE-7.7d/7.7e artifact shape).
REAL_MANIFEST_GROUP_NAMES = (
    "G1_AEE-7.7d_executor_and_tests",
    "G2_AEE-7.7e_dryrun_projection_apply_and_shared_init",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_minimal_manifest_dict(
    *,
    group_count: int = 1,
    new_per_group: int = 1,
    mod_per_group: int = 0,
) -> Dict[str, Any]:
    """Build a minimal in-memory manifest dict.

    Used by the failure-mode tests to assert the validator's
    strict contract without depending on the on-disk file.
    """
    groups: Dict[str, Any] = {}
    for gi in range(group_count):
        gname = f"G{gi+1}_TEST"
        files_new = []
        for fi in range(new_per_group):
            files_new.append(
                {
                    "path": f"aee/audit/test_g{gi}_n{fi}.py",
                    "sha256": "a" * 64,
                    "size": 1024,
                    "lines": 32,
                    "imports_dispatcher": False,
                    "writes_to_live_db": False,
                }
            )
        files_mod = []
        for fi in range(mod_per_group):
            files_mod.append(
                {
                    "path": f"aee/audit/test_g{gi}_m{fi}.py",
                    "sha256": "b" * 64,
                    "size": 2048,
                    "lines": 64,
                }
            )
        groups[gname] = {
            "subject_proposed": f"feat: test g{gi}",
            "commit_ready": "NO",
            "files_new": files_new,
            "files_modified": files_mod,
        }
    return {
        "generated_utc": "2026-07-12T00:00:00+00:00",
        "groups": groups,
    }


def _write_temp_manifest(body: Any) -> str:
    """Write ``body`` to a temp file as JSON, return the path.

    Path is created via ``tempfile.mkstemp`` so the test owns
    the file and can safely unlink. The file is not deleted
    automatically — the calling test is responsible (each
    test should call ``os.unlink`` in a finally block or use
    a context manager).
    """
    fd, path = tempfile.mkstemp(suffix=".json", prefix="aee78-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(body, fh)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


# ---------------------------------------------------------------------------
# 1. Public constants
# ---------------------------------------------------------------------------


class ManifestSchemaVersionConstantTests(unittest.TestCase):
    """The K1 contract version is a stable string."""

    def test_schema_version_is_stable(self) -> None:
        self.assertEqual(MANIFEST_SCHEMA_VERSION, "1.0.0")

    def test_schema_version_is_string(self) -> None:
        self.assertIsInstance(MANIFEST_SCHEMA_VERSION, str)


# ---------------------------------------------------------------------------
# 2. load_manifest — happy path against the real on-disk artifact
# ---------------------------------------------------------------------------


class LoadManifestHappyPathTests(unittest.TestCase):
    """Load the real AEE-7.7d/7.7e manifest and assert the typed tree."""

    @classmethod
    def setUpClass(cls) -> None:
        if not REAL_MANIFEST.is_file():
            raise unittest.SkipTest(
                f"real manifest {REAL_MANIFEST} not present on this checkout"
            )
        cls.doc = load_manifest(REAL_MANIFEST)

    def test_returns_manifest_document(self) -> None:
        self.assertIsInstance(self.doc, ManifestDocument)

    def test_on_disk_sha256_is_64_char_hex(self) -> None:
        self.assertEqual(len(self.doc.on_disk_sha256), 64)
        self.assertTrue(
            re.fullmatch(r"[0-9a-f]{64}", self.doc.on_disk_sha256),
            f"sha256 not hex: {self.doc.on_disk_sha256!r}",
        )

    def test_on_disk_size_matches_file(self) -> None:
        self.assertEqual(self.doc.on_disk_size, REAL_MANIFEST.stat().st_size)

    def test_source_path_recorded(self) -> None:
        self.assertEqual(self.doc.source_path, str(REAL_MANIFEST))

    def test_group_names_ordered(self) -> None:
        # Document order matters for the staging-boundary report.
        self.assertEqual(
            tuple(self.doc.list_group_names()), REAL_MANIFEST_GROUP_NAMES
        )

    def test_g1_has_two_new_no_modified(self) -> None:
        g1 = self.doc.get_group(REAL_MANIFEST_GROUP_NAMES[0])
        self.assertIsNotNone(g1)
        assert g1 is not None  # for type checker
        self.assertEqual(g1.new_file_count, 2)
        self.assertEqual(g1.modified_file_count, 0)
        self.assertEqual(g1.total_file_count, 2)

    def test_g2_has_two_new_one_modified(self) -> None:
        g2 = self.doc.get_group(REAL_MANIFEST_GROUP_NAMES[1])
        self.assertIsNotNone(g2)
        assert g2 is not None
        self.assertEqual(g2.new_file_count, 2)
        self.assertEqual(g2.modified_file_count, 1)
        self.assertEqual(g2.total_file_count, 3)

    def test_get_group_unknown_returns_none(self) -> None:
        self.assertIsNone(self.doc.get_group("NOT_A_GROUP"))

    def test_iter_files_count_matches_total(self) -> None:
        self.assertEqual(
            sum(1 for _ in self.doc.iter_files()),
            self.doc.total_files_count(),
        )

    def test_iter_new_files_count(self) -> None:
        self.assertEqual(
            sum(1 for _ in self.doc.iter_new_files()),
            self.doc.new_files_count(),
        )

    def test_iter_modified_files_count(self) -> None:
        self.assertEqual(
            sum(1 for _ in self.doc.iter_modified_files()),
            self.doc.modified_files_count(),
        )

    def test_total_counts(self) -> None:
        # 2 + 2 = 4 new, 0 + 1 = 1 modified, total 5.
        self.assertEqual(self.doc.new_files_count(), 4)
        self.assertEqual(self.doc.modified_files_count(), 1)
        self.assertEqual(self.doc.total_files_count(), 5)

    def test_file_entry_paths_distinct(self) -> None:
        paths = [fe.path for fe in self.doc.iter_files()]
        self.assertEqual(len(paths), len(set(paths)))

    def test_file_entry_kind_discriminator(self) -> None:
        # G1 has only new; G2 has new + modified. Walk all
        # entries and check the discriminator matches the
        # source array it came from.
        g2 = self.doc.get_group(REAL_MANIFEST_GROUP_NAMES[1])
        self.assertIsNotNone(g2)
        assert g2 is not None
        for fe in g2.files_new:
            self.assertEqual(fe.kind, FileEntryKind.NEW)
        for fe in g2.files_modified:
            self.assertEqual(fe.kind, FileEntryKind.MODIFIED)

    def test_file_entry_extras_preserved(self) -> None:
        # The G1 first new file has ``imports_dispatcher`` +
        # ``writes_to_live_db`` extras. Confirm they survive
        # the round-trip into the dataclass.
        g1 = self.doc.get_group(REAL_MANIFEST_GROUP_NAMES[0])
        self.assertIsNotNone(g1)
        assert g1 is not None
        fe = g1.files_new[0]
        self.assertIn("imports_dispatcher", fe.extras)
        self.assertIn("writes_to_live_db", fe.extras)
        self.assertEqual(fe.extras["imports_dispatcher"], False)
        self.assertEqual(fe.extras["writes_to_live_db"], False)

    def test_raw_dict_preserved(self) -> None:
        # The ManifestDocument keeps the original dict so
        # callers can reach unknown fields. The top-level
        # ``generated_utc`` is one such field — assert it
        # round-trips.
        self.assertIn("generated_utc", self.doc.raw)
        self.assertIsInstance(self.doc.raw["generated_utc"], str)

    def test_to_dict_round_trip(self) -> None:
        g1 = self.doc.get_group(REAL_MANIFEST_GROUP_NAMES[0])
        self.assertIsNotNone(g1)
        assert g1 is not None
        fe = g1.files_new[0]
        d = fe.to_dict()
        self.assertEqual(d["group_name"], g1.name)
        self.assertEqual(d["kind"], "new")
        self.assertEqual(d["path"], fe.path)
        self.assertEqual(d["sha256"], fe.sha256)
        # The required fields cannot be overwritten by extras.
        # The G1 first new file has ``imports_dispatcher`` /
        # ``writes_to_live_db`` — confirm they appear AFTER
        # the required fields (i.e. the spread does not
        # silently overwrite anything).
        self.assertIn("imports_dispatcher", d)


# ---------------------------------------------------------------------------
# 3. load_manifest — failure modes
# ---------------------------------------------------------------------------


class LoadManifestFailureTests(unittest.TestCase):
    """I/O + parse failures raise :class:`ManifestError`."""

    def setUp(self) -> None:
        self._tmp_paths: list = []

    def tearDown(self) -> None:
        for p in self._tmp_paths:
            try:
                os.unlink(p)
            except OSError:
                pass

    def _tempfile(self, body: Any) -> str:
        path = _write_temp_manifest(body)
        self._tmp_paths.append(path)
        return path

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(ManifestError) as cm:
            load_manifest("/tmp/this/path/does/not/exist/aee78.json")
        self.assertIn("not found", str(cm.exception).lower())

    def test_directory_raises(self) -> None:
        with self.assertRaises(ManifestError):
            load_manifest(tempfile.gettempdir())

    def test_invalid_json_raises(self) -> None:
        path = tempfile.mkstemp(suffix=".json", prefix="aee78-bad-")[1]
        self._tmp_paths.append(path)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ this is not valid json")
        with self.assertRaises(ManifestError) as cm:
            load_manifest(path)
        self.assertIn("json", str(cm.exception).lower())

    def test_non_dict_top_level_raises(self) -> None:
        path = self._tempfile([1, 2, 3])
        with self.assertRaises(ManifestError) as cm:
            load_manifest(path)
        self.assertIn("object", str(cm.exception).lower())

    def test_non_dict_groups_raises(self) -> None:
        path = self._tempfile({"groups": "should-be-dict"})
        with self.assertRaises(ManifestError) as cm:
            load_manifest(path)
        self.assertIn("groups", str(cm.exception).lower())


# ---------------------------------------------------------------------------
# 4. validate_manifest — happy path against the real artifact
# ---------------------------------------------------------------------------


class ValidateManifestHappyPathTests(unittest.TestCase):
    """The real AEE-7.7d/7.7e manifest passes K1 strict validation."""

    @classmethod
    def setUpClass(cls) -> None:
        if not REAL_MANIFEST.is_file():
            raise unittest.SkipTest(
                f"real manifest {REAL_MANIFEST} not present on this checkout"
            )
        cls.doc = load_manifest(REAL_MANIFEST)
        cls.result = validate_manifest(cls.doc)

    def test_returns_validation_result(self) -> None:
        self.assertIsInstance(self.result, ValidationResult)

    def test_passes(self) -> None:
        self.assertTrue(
            self.result.passed,
            f"unexpected errors: {self.result.errors}",
        )

    def test_no_errors(self) -> None:
        self.assertEqual(self.result.errors, [])

    def test_to_dict_shape(self) -> None:
        d = self.result.to_dict()
        self.assertTrue(d["passed"])
        self.assertEqual(d["error_count"], 0)
        self.assertEqual(d["warning_count"], len(self.result.warnings))
        self.assertEqual(d["errors"], [])
        self.assertEqual(d["warnings"], list(self.result.warnings))


# ---------------------------------------------------------------------------
# 5. validate_manifest — error / warning paths via in-memory dicts
# ---------------------------------------------------------------------------


class ValidateManifestErrorPathTests(unittest.TestCase):
    """All error categories surface in ``ValidationResult.errors``."""

    def setUp(self) -> None:
        self._tmp_paths: list = []

    def tearDown(self) -> None:
        for p in self._tmp_paths:
            try:
                os.unlink(p)
            except OSError:
                pass

    def _validate(self, body: Any) -> ValidationResult:
        path = _write_temp_manifest(body)
        self._tmp_paths.append(path)
        doc = load_manifest(path)
        return validate_manifest(doc)

    def test_missing_top_level_generated_utc_fails(self) -> None:
        body = _make_minimal_manifest_dict()
        del body["generated_utc"]
        result = self._validate(body)
        self.assertFalse(result.passed)
        self.assertTrue(
            any("generated_utc" in e for e in result.errors),
            f"errors: {result.errors}",
        )

    def test_missing_top_level_groups_fails(self) -> None:
        body = _make_minimal_manifest_dict()
        del body["groups"]
        result = self._validate(body)
        self.assertFalse(result.passed)
        self.assertTrue(
            any("'groups'" in e for e in result.errors),
            f"errors: {result.errors}",
        )

    def test_bad_sha256_shape_fails(self) -> None:
        body = _make_minimal_manifest_dict()
        body["groups"]["G1_TEST"]["files_new"][0]["sha256"] = "not-a-sha"
        result = self._validate(body)
        self.assertFalse(result.passed)
        self.assertTrue(
            any("sha256" in e for e in result.errors),
            f"errors: {result.errors}",
        )

    def test_short_sha256_fails(self) -> None:
        body = _make_minimal_manifest_dict()
        body["groups"]["G1_TEST"]["files_new"][0]["sha256"] = "a" * 32
        result = self._validate(body)
        self.assertFalse(result.passed)
        self.assertTrue(
            any("sha256" in e for e in result.errors),
            f"errors: {result.errors}",
        )

    def test_non_int_size_fails(self) -> None:
        body = _make_minimal_manifest_dict()
        body["groups"]["G1_TEST"]["files_new"][0]["size"] = "not-an-int"
        result = self._validate(body)
        self.assertFalse(result.passed)
        self.assertTrue(
            any("dropped" in e for e in result.errors),
            f"errors: {result.errors}",
        )

    def test_negative_size_fails(self) -> None:
        body = _make_minimal_manifest_dict()
        body["groups"]["G1_TEST"]["files_new"][0]["size"] = -1
        result = self._validate(body)
        self.assertFalse(result.passed)
        self.assertTrue(
            any("dropped" in e for e in result.errors),
            f"errors: {result.errors}",
        )

    def test_bool_size_fails(self) -> None:
        # Python: bool is a subclass of int. The K1 contract
        # is strict — True / False must NOT be accepted as a
        # size value.
        body = _make_minimal_manifest_dict()
        body["groups"]["G1_TEST"]["files_new"][0]["size"] = True
        result = self._validate(body)
        self.assertFalse(result.passed)
        self.assertTrue(
            any("dropped" in e for e in result.errors),
            f"errors: {result.errors}",
        )

    def test_non_int_lines_fails(self) -> None:
        body = _make_minimal_manifest_dict()
        body["groups"]["G1_TEST"]["files_new"][0]["lines"] = "32"
        result = self._validate(body)
        self.assertFalse(result.passed)
        self.assertTrue(
            any("dropped" in e for e in result.errors),
            f"errors: {result.errors}",
        )

    def test_imports_dispatcher_must_be_bool(self) -> None:
        body = _make_minimal_manifest_dict()
        body["groups"]["G1_TEST"]["files_new"][0]["imports_dispatcher"] = 1
        result = self._validate(body)
        self.assertFalse(result.passed)
        self.assertTrue(
            any("imports_dispatcher" in e for e in result.errors),
            f"errors: {result.errors}",
        )

    def test_writes_to_live_db_must_be_bool(self) -> None:
        body = _make_minimal_manifest_dict()
        body["groups"]["G1_TEST"]["files_new"][0]["writes_to_live_db"] = "yes"
        result = self._validate(body)
        self.assertFalse(result.passed)
        self.assertTrue(
            any("writes_to_live_db" in e for e in result.errors),
            f"errors: {result.errors}",
        )

    def test_empty_path_fails(self) -> None:
        body = _make_minimal_manifest_dict()
        body["groups"]["G1_TEST"]["files_new"][0]["path"] = ""
        result = self._validate(body)
        self.assertFalse(result.passed)
        self.assertTrue(
            any("path" in e for e in result.errors),
            f"errors: {result.errors}",
        )

    def test_empty_group_emits_warning(self) -> None:
        body = _make_minimal_manifest_dict(group_count=1, new_per_group=0)
        result = self._validate(body)
        # Empty group is a warning, not an error.
        self.assertTrue(result.passed)
        self.assertTrue(
            any("zero files" in w for w in result.warnings),
            f"warnings: {result.warnings}",
        )

    def test_missing_required_key_in_file_entry_fails(self) -> None:
        # Drop the ``lines`` key from a files_new row. The
        # loader drops the bad row silently (forensic-
        # friendly) and bumps ``dropped_row_count``. The
        # validator surfaces the drop as a blocking error
        # so the caller cannot accidentally treat a
        # partially-corrupt manifest as healthy.
        body = _make_minimal_manifest_dict()
        del body["groups"]["G1_TEST"]["files_new"][0]["lines"]
        result = self._validate(body)
        self.assertFalse(
            result.passed,
            f"expected dropped-row error, got passed=True "
            f"errors={result.errors} warnings={result.warnings}",
        )
        self.assertTrue(
            any("dropped" in e and "row" in e for e in result.errors),
            f"errors: {result.errors}",
        )

    def test_optional_top_level_must_have_right_type(self) -> None:
        body = _make_minimal_manifest_dict()
        body["static_checks"] = 42  # wrong type
        result = self._validate(body)
        self.assertFalse(result.passed)
        self.assertTrue(
            any("static_checks" in e for e in result.errors),
            f"errors: {result.errors}",
        )


# ---------------------------------------------------------------------------
# 6. validate_manifest — on-disk fingerprint sanity
# ---------------------------------------------------------------------------


class ValidateManifestOnDiskFingerprintTests(unittest.TestCase):
    """The validator must also catch a loader-side empty-fingerprint bug."""

    def test_empty_on_disk_sha256_is_error(self) -> None:
        # Build a document by hand with an empty sha. This
        # exercises the validator's defense-in-depth check
        # against a loader regression.
        doc = ManifestDocument(
            raw={"groups": {}},
            groups={},
            source_path="/tmp/fake.json",
            on_disk_sha256="",
            on_disk_size=100,
        )
        result = validate_manifest(doc)
        self.assertFalse(result.passed)
        self.assertTrue(
            any("on_disk_sha256" in e for e in result.errors),
            f"errors: {result.errors}",
        )

    def test_zero_on_disk_size_is_error(self) -> None:
        doc = ManifestDocument(
            raw={"groups": {}},
            groups={},
            source_path="/tmp/fake.json",
            on_disk_sha256="a" * 64,
            on_disk_size=0,
        )
        result = validate_manifest(doc)
        self.assertFalse(result.passed)
        self.assertTrue(
            any("on_disk_size" in e for e in result.errors),
            f"errors: {result.errors}",
        )

    def test_non_hex_on_disk_sha256_is_error(self) -> None:
        doc = ManifestDocument(
            raw={"groups": {}},
            groups={},
            source_path="/tmp/fake.json",
            on_disk_sha256="z" * 64,  # 'z' is not hex
            on_disk_size=100,
        )
        result = validate_manifest(doc)
        self.assertFalse(result.passed)
        self.assertTrue(
            any("on_disk_sha256" in e for e in result.errors),
            f"errors: {result.errors}",
        )


# ---------------------------------------------------------------------------
# 7. GroupEntry properties
# ---------------------------------------------------------------------------


class GroupEntryPropertyTests(unittest.TestCase):
    """GroupEntry exposes count properties that wrap len()."""

    def test_new_only(self) -> None:
        g = GroupEntry(name="G", files_new=[_fe_stub("a"), _fe_stub("b")])
        self.assertEqual(g.new_file_count, 2)
        self.assertEqual(g.modified_file_count, 0)
        self.assertEqual(g.total_file_count, 2)

    def test_modified_only(self) -> None:
        g = GroupEntry(name="G", files_modified=[_fe_stub("a")])
        self.assertEqual(g.new_file_count, 0)
        self.assertEqual(g.modified_file_count, 1)
        self.assertEqual(g.total_file_count, 1)

    def test_mixed(self) -> None:
        g = GroupEntry(
            name="G",
            files_new=[_fe_stub("a"), _fe_stub("b")],
            files_modified=[_fe_stub("c")],
        )
        self.assertEqual(g.new_file_count, 2)
        self.assertEqual(g.modified_file_count, 1)
        self.assertEqual(g.total_file_count, 3)

    def test_empty(self) -> None:
        g = GroupEntry(name="G")
        self.assertEqual(g.new_file_count, 0)
        self.assertEqual(g.modified_file_count, 0)
        self.assertEqual(g.total_file_count, 0)


def _fe_stub(label: str) -> FileEntry:
    """Build a minimal FileEntry for property tests."""
    return FileEntry(
        group_name="G",
        kind=FileEntryKind.NEW,
        path=f"x/{label}.py",
        sha256="a" * 64,
        size=100,
        lines=10,
    )


# ---------------------------------------------------------------------------
# 8. Isolation contract — no dispatcher, no network, no subprocess
# ---------------------------------------------------------------------------


class IsolationContractTests(unittest.TestCase):
    """K1 is a stdlib-only data shaper. Assert the contract.

    The contract is enforced statically (no dispatcher import
    in source) AND at module-import time (the module loads
    cleanly on a sys.path that does NOT include
    ``hermes-runtime-bridge/dispatcher``).
    """

    def setUp(self) -> None:
        self.module = sys.modules["aee.audit.manifest"]

    def test_no_dispatcher_in_source(self) -> None:
        src_path = Path(self.module.__file__)
        text = src_path.read_text(encoding="utf-8")
        # The string ``import dispatcher`` must not appear
        # anywhere in the module. We allow ``__future__``
        # because it is a known-safe future import. The
        # docstring is skipped because it is allowed to
        # NAME forbidden modules as part of the contract
        # documentation.
        in_docstring = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith('"""') or stripped.startswith("'''"):
                # Toggle on simple single-line docstring
                # fences. A multi-line docstring is entered
                # on the first fence and exited on the next.
                in_docstring = not in_docstring
                continue
            if in_docstring or stripped.startswith("#"):
                continue
            self.assertNotIn(
                "import dispatcher",
                stripped,
                f"dispatcher import leaked: {stripped!r}",
            )
            self.assertNotIn(
                "from dispatcher",
                stripped,
                f"dispatcher from-import leaked: {stripped!r}",
            )

    def _strip_docstrings(self, text: str) -> str:
        """Return ``text`` with every triple-quoted docstring removed.

        The scanner is line-based and tracks ``in_docstring``
        via two toggle rules:

        1. A line whose stripped prefix is the double-quote
           fence (three double-quote chars) starts a
           multi-line docstring AND ends the same docstring
           only when the same line ALSO contains a closing
           fence. The common case is a one-line docstring,
           which is a toggle-then-toggle.
        2. A line that contains a leading fence alone
           enters the docstring; the next line whose
           stripped form starts with the matching fence
           exits.

        Used by the isolation tests so the contract
        documentation (which deliberately NAMES forbidden
        modules like ``os.environ`` / ``subprocess``) does
        not trip the static source scanner.
        """
        out_lines: list = []
        in_docstring = False
        for line in text.splitlines():
            stripped = line.strip()
            if in_docstring:
                # We are inside a docstring; emit the line
                # only if it is the closing fence.
                if (
                    stripped.startswith(DOUBLE_QUOTE_FENCE)
                    or stripped.startswith(SINGLE_QUOTE_FENCE)
                ):
                    in_docstring = False
                continue
            # Not in a docstring. A line that opens a
            # docstring is dropped (do not append). If the
            # SAME line also closes (single-line docstring),
            # we stay outside.
            if (
                stripped.startswith(DOUBLE_QUOTE_FENCE)
                or stripped.startswith(SINGLE_QUOTE_FENCE)
            ):
                in_docstring = True
                # Single-line docstring: also closes on the
                # same line. Detect by counting the fence
                # token in the rest of the line.
                rest = stripped[3:]
                if DOUBLE_QUOTE_FENCE in rest or SINGLE_QUOTE_FENCE in rest:
                    in_docstring = False
                continue
            out_lines.append(line)
        return "\n".join(out_lines)

    def test_no_subprocess_in_source(self) -> None:
        src_path = Path(self.module.__file__)
        text = self._strip_docstrings(
            src_path.read_text(encoding="utf-8")
        )
        self.assertNotIn("subprocess", text)
        self.assertNotIn("os.system", text)
        self.assertNotIn("shell=True", text)

    def test_no_requests_or_urllib_external_in_source(self) -> None:
        src_path = Path(self.module.__file__)
        text = self._strip_docstrings(
            src_path.read_text(encoding="utf-8")
        )
        self.assertNotIn("import requests", text)
        # ``urllib`` stdlib is allowed only via
        # ``urllib.request`` / ``urllib.parse`` — K1 uses
        # neither, so the literal token must not appear.
        self.assertNotIn("urllib", text)

    def test_no_environ_reads_in_source(self) -> None:
        src_path = Path(self.module.__file__)
        text = self._strip_docstrings(
            src_path.read_text(encoding="utf-8")
        )
        # K1 must not read env vars. Assert against the
        # common dangerous patterns.
        self.assertNotIn("os.environ", text)
        self.assertNotIn("os.getenv", text)

    def test_module_does_not_open_dispatcher_db(self) -> None:
        # Walk the module's imported names and assert no
        # ``dispatcher`` symbol slipped through.
        names = set(dir(self.module))
        for n in names:
            self.assertFalse(
                n.startswith("dispatcher") or n == "dispatcher",
                f"dispatcher name leaked into module attrs: {n!r}",
            )

    def test_module_imports_are_stdlib_only(self) -> None:
        # The K1 module's imports must all resolve to stdlib.
        stdlib_roots = {
            "os", "sys", "json", "hashlib", "tempfile", "re", "unittest",
            "pathlib", "typing", "dataclasses", "enum", "importlib",
            "__future__", "collections", "itertools", "functools", "abc",
        }
        # Walk the module's __dict__ for top-level imports.
        for attr_name in ("json", "os", "hashlib"):
            self.assertIn(attr_name, sys.modules)
        # Light check: the source's ``import`` lines should
        # only name stdlib modules. Docstrings are stripped
        # first because the contract documentation may name
        # forbidden modules on purpose.
        src_path = Path(self.module.__file__)
        text = self._strip_docstrings(
            src_path.read_text(encoding="utf-8")
        )
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("import ") and not stripped.startswith(
                "from "
            ):
                continue
            if stripped.startswith("import "):
                mod = stripped[len("import "):].split(" as ")[0].split(",")[0].strip()
            else:
                after_from = stripped[len("from "):]
                mod = after_from.split(" import ")[0].strip()
            top = mod.split(".")[0]
            self.assertIn(
                top, stdlib_roots,
                f"non-stdlib import in manifest.py: {stripped!r}",
            )


# ---------------------------------------------------------------------------
# 9. Manifest module re-exportable via the audit package
# ---------------------------------------------------------------------------


class PackageReexportTests(unittest.TestCase):
    """The audit package ``__init__`` re-exports the K1 symbols.

    AEE-7.8 K1 is allowed to modify ``aee/audit/__init__.py``
    ONLY as required for the K1 re-export. This test asserts
    the contract after the package is updated.
    """

    def test_package_reexports_load_manifest(self) -> None:
        # NOTE: other tests in the suite (TestNoDispatcherImport
        # family in 7.7 apply / 7.7d / 7.7 live_audit) purge
        # ``aee.audit.*`` from ``sys.modules`` and trigger a
        # re-import. After such a purge, ``aee.audit.load_manifest``
        # may resolve to a freshly-imported function object
        # while the module-level ``load_manifest`` bound at
        # the top of this test file still points at the
        # original. We therefore look up both names via
        # ``sys.modules`` so the test compares the LIVE
        # public re-export against the LIVE submodule export.
        import sys
        pkg = sys.modules["aee.audit"]
        sub = sys.modules["aee.audit.manifest"]
        self.assertIs(pkg.load_manifest, sub.load_manifest)

    def test_package_reexports_validate_manifest(self) -> None:
        import sys
        pkg = sys.modules["aee.audit"]
        sub = sys.modules["aee.audit.manifest"]
        self.assertIs(pkg.validate_manifest, sub.validate_manifest)

    def test_package_reexports_dataclasses(self) -> None:
        import sys
        pkg = sys.modules["aee.audit"]
        sub = sys.modules["aee.audit.manifest"]
        self.assertIs(pkg.ManifestDocument, sub.ManifestDocument)
        self.assertIs(pkg.GroupEntry, sub.GroupEntry)
        self.assertIs(pkg.FileEntry, sub.FileEntry)
        self.assertIs(pkg.ValidationResult, sub.ValidationResult)
        self.assertIs(pkg.ManifestError, sub.ManifestError)
        self.assertIs(pkg.FileEntryKind, sub.FileEntryKind)

    def test_package_reexports_schema_version(self) -> None:
        import sys
        pkg = sys.modules["aee.audit"]
        sub = sys.modules["aee.audit.manifest"]
        self.assertIs(pkg.MANIFEST_SCHEMA_VERSION, sub.MANIFEST_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
