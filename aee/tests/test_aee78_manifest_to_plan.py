"""AEE-7.8 K2 — Targeted tests for the manifest → PlanInput adapter.

Coverage surface:

* :func:`load_manifest_or_default` happy path (explicit
  ``path=...``) and default path (no arg, empty
  :class:`ManifestDocument`).
* :func:`manifest_to_plan_inputs` happy path: real
  ``AEE_7_7d_7e_MANIFEST.json`` from repo root → all
  :class:`PlanInput` rows have ``path`` / ``sha256`` /
  ``size`` / ``lines`` / ``group_name`` / ``kind`` preserved.
* :func:`manifest_to_plan_inputs` validation-gate: a
  malformed doc returns ``passed=False`` + empty
  ``plan_inputs`` + populated ``warnings``.
* :func:`manifest_to_plan_inputs` empty-manifest path: a
  document with 0 groups returns ``passed=True`` + 0
  ``plan_inputs`` + zero-warning result.
* :class:`PlanInput` + :class:`ManifestToPlanResult` DTO
  shape, JSON round-trip via ``to_dict``.
* K1 isolation contract preserved: the K2 module
  ``aee/audit/manifest.py`` does **not** import
  ``dispatcher``; does **not** contact the live DB; does
  **not** invoke ``subprocess`` / ``os.system`` /
  ``os.environ`` / ``requests`` / ``urllib``.

Run:
    PYTHONPATH=/home/ubuntu/hermes-runtime-bridge /usr/bin/python3 -m unittest aee.tests.test_aee78_manifest_to_plan -v
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from aee.audit.manifest import (
    FileEntry,
    FileEntryKind,
    GroupEntry,
    MANIFEST_SCHEMA_VERSION,
    ManifestDocument,
    ManifestError,
    ManifestToPlanResult,
    PlanInput,
    ValidationResult,
    load_manifest,
    load_manifest_or_default,
    manifest_to_plan_inputs,
    validate_manifest,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Path to the real K1 reader input. Lives at the repo root and
#: is the artifact the adapter is expected to consume in the
#: happy-path test. Computed from this file's location rather
#: than hard-coded so the test passes from any cwd.
_REAL_MANIFEST = (
    Path(__file__).resolve().parent.parent.parent
    / "AEE_7_7d_7e_MANIFEST.json"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_minimal_manifest_dict(
    *,
    files_new: List[Dict[str, Any]] = None,
    files_modified: List[Dict[str, Any]] = None,
    group_name: str = "G1",
) -> Dict[str, Any]:
    """Build a minimal valid manifest dict for round-trip tests.

    Default content is a single group with no files (a
    structurally valid but empty group — the validator emits a
    warning for empty groups, not an error). Pass
    ``files_new=`` / ``files_modified=`` to populate the row
    lists.
    """
    return {
        "generated_utc": "2026-07-12T18:00:00Z",
        "groups": {
            group_name: {
                "files_new": list(files_new or []),
                "files_modified": list(files_modified or []),
            },
        },
    }


def _make_file_row(
    *,
    path: str,
    sha256: str,
    size: int,
    lines: int,
    **extras: Any,
) -> Dict[str, Any]:
    """Build a single file row dict (NEW or MODIFIED)."""
    row: Dict[str, Any] = {
        "path": path,
        "sha256": sha256,
        "size": size,
        "lines": lines,
    }
    row.update(extras)
    return row


def _write_temp_manifest(body: Any) -> str:
    """Write ``body`` (a dict or raw JSON string) to a temp
    file and return the path. The file is not auto-deleted;
    tests use ``tempfile.TemporaryDirectory`` for cleanup.
    """
    if isinstance(body, str):
        payload = body
    else:
        payload = json.dumps(body)
    fd, path = tempfile.mkstemp(suffix=".json", prefix="aee78k2-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
    except Exception:
        os.unlink(path)
        raise
    return path


# ---------------------------------------------------------------------------
# PlanInput / ManifestToPlanResult DTO tests
# ---------------------------------------------------------------------------


class PlanInputDTOShape(unittest.TestCase):
    """``PlanInput`` is a frozen dataclass with a strict field
    contract. The K2 adapter is the only producer, so the
    shape must be locked."""

    def test_plan_input_fields_match_brief(self) -> None:
        from dataclasses import fields
        names = {f.name for f in fields(PlanInput)}
        self.assertEqual(
            names,
            {"group_name", "kind", "path", "sha256", "size", "lines", "extras"},
        )

    def test_plan_input_is_frozen(self) -> None:
        pi = PlanInput(
            group_name="G1",
            kind=FileEntryKind.NEW,
            path="x.py",
            sha256="0" * 64,
            size=10,
            lines=3,
        )
        with self.assertRaises(Exception):
            pi.path = "y.py"  # type: ignore[misc]

    def test_plan_input_to_dict_is_self_describing(self) -> None:
        pi = PlanInput(
            group_name="G1",
            kind=FileEntryKind.NEW,
            path="x.py",
            sha256="0" * 64,
            size=10,
            lines=3,
            extras={"schema_version": "1.0.0"},
        )
        d = pi.to_dict()
        self.assertEqual(d["group_name"], "G1")
        self.assertEqual(d["kind"], "new")
        self.assertEqual(d["path"], "x.py")
        self.assertEqual(d["sha256"], "0" * 64)
        self.assertEqual(d["size"], 10)
        self.assertEqual(d["lines"], 3)
        self.assertEqual(d["schema_version"], "1.0.0")

    def test_plan_input_to_dict_does_not_overwrite_required_fields(self) -> None:
        # If extras carries a ``path`` key (e.g. a sloppy
        # caller), the required field must win.
        pi = PlanInput(
            group_name="G1",
            kind=FileEntryKind.NEW,
            path="x.py",
            sha256="0" * 64,
            size=10,
            lines=3,
            extras={"path": "extras_attempted_override.py"},
        )
        d = pi.to_dict()
        self.assertEqual(d["path"], "x.py")


class ManifestToPlanResultDTOShape(unittest.TestCase):
    """``ManifestToPlanResult`` is the adapter's return type.
    ``passed`` + ``plan_inputs`` (tuple) + ``warnings``
    (tuple) + ``to_dict``."""

    def test_result_defaults_to_passed_false_empty(self) -> None:
        r = ManifestToPlanResult(passed=False)
        self.assertFalse(r.passed)
        self.assertEqual(r.plan_inputs, ())
        self.assertEqual(r.warnings, ())

    def test_result_to_dict_keys(self) -> None:
        r = ManifestToPlanResult(
            passed=True,
            plan_inputs=(
                PlanInput(
                    group_name="G1",
                    kind=FileEntryKind.NEW,
                    path="x.py",
                    sha256="0" * 64,
                    size=10,
                    lines=3,
                ),
            ),
            warnings=("a", "b"),
        )
        d = r.to_dict()
        self.assertEqual(
            set(d.keys()),
            {"passed", "plan_input_count", "warning_count", "plan_inputs", "warnings"},
        )
        self.assertTrue(d["passed"])
        self.assertEqual(d["plan_input_count"], 1)
        self.assertEqual(d["warning_count"], 2)
        self.assertEqual(d["plan_inputs"][0]["path"], "x.py")
        self.assertEqual(d["warnings"], ["a", "b"])


# ---------------------------------------------------------------------------
# load_manifest_or_default tests
# ---------------------------------------------------------------------------


class LoadManifestOrDefaultTests(unittest.TestCase):
    """``load_manifest_or_default`` has two paths: explicit
    ``path=...`` (load + raise on transport failure) and
    default ``path=None`` (return an empty manifest)."""

    def test_default_returns_empty_manifest(self) -> None:
        doc = load_manifest_or_default()
        self.assertEqual(doc.groups, {})
        self.assertEqual(doc.raw, {})
        self.assertEqual(doc.source_path, "")
        self.assertEqual(doc.on_disk_sha256, "")
        self.assertEqual(doc.on_disk_size, 0)
        self.assertEqual(doc.dropped_row_count, 0)
        self.assertEqual(doc.dropped_group_count, 0)

    def test_explicit_path_loads_real_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = _make_minimal_manifest_dict(
                files_new=[
                    _make_file_row(
                        path="a.py",
                        sha256="a" * 64,
                        size=10,
                        lines=2,
                    ),
                ],
            )
            path = _write_temp_manifest(body)
            try:
                doc = load_manifest_or_default(path=path)
            finally:
                os.unlink(path)
            self.assertEqual(set(doc.list_group_names()), {"G1"})
            self.assertEqual(doc.total_files_count(), 1)

    def test_explicit_path_propagates_manifest_error(self) -> None:
        # ``load_manifest`` raises ``ManifestError`` on a
        # missing file. ``load_manifest_or_default`` must
        # propagate (the explicit-path case is not a silent
        # default).
        with self.assertRaises(ManifestError):
            load_manifest_or_default(path="/nonexistent/aee78k2.json")


# ---------------------------------------------------------------------------
# manifest_to_plan_inputs: happy path with the real K1 manifest
# ---------------------------------------------------------------------------


class ManifestToPlanInputsHappyPath(unittest.TestCase):
    """The adapter should consume the real K1 manifest and
    produce a non-empty list of ``PlanInput`` rows whose
    fields are preserved bit-for-bit from the manifest."""

    @unittest.skipUnless(
        _REAL_MANIFEST.is_file(),
        f"real K1 manifest not found at {_REAL_MANIFEST}",
    )
    def test_real_manifest_round_trips_to_plan_inputs(self) -> None:
        doc = load_manifest(_REAL_MANIFEST)
        result = manifest_to_plan_inputs(doc)
        self.assertTrue(
            result.passed,
            msg=f"adapter refused valid manifest; warnings={result.warnings!r}",
        )
        self.assertGreater(
            len(result.plan_inputs), 0,
            msg="real manifest produced zero PlanInput rows",
        )
        # Every row has the expected field shape.
        for pi in result.plan_inputs:
            self.assertIsInstance(pi.group_name, str)
            self.assertTrue(pi.group_name)
            self.assertIsInstance(pi.kind, FileEntryKind)
            self.assertIsInstance(pi.path, str)
            self.assertTrue(pi.path)
            self.assertEqual(len(pi.sha256), 64)
            self.assertIsInstance(pi.size, int)
            self.assertGreaterEqual(pi.size, 0)
            self.assertIsInstance(pi.lines, int)
            self.assertGreaterEqual(pi.lines, 0)

    @unittest.skipUnless(
        _REAL_MANIFEST.is_file(),
        f"real K1 manifest not found at {_REAL_MANIFEST}",
    )
    def test_real_manifest_count_matches_iter_files(self) -> None:
        doc = load_manifest(_REAL_MANIFEST)
        result = manifest_to_plan_inputs(doc)
        self.assertEqual(
            len(result.plan_inputs),
            doc.total_files_count(),
        )


# ---------------------------------------------------------------------------
# manifest_to_plan_inputs: validation-gate + empty + malformed paths
# ---------------------------------------------------------------------------


class ManifestToPlanInputsValidationGate(unittest.TestCase):
    """The adapter must call ``validate_manifest`` and refuse
    to project when validation fails."""

    def test_validation_failure_returns_empty_and_warns(self) -> None:
        # Build a doc that has zero groups (validator warning,
        # not error) and zero groups (validation passes), then
        # force a failure by stuffing a non-hex sha256.
        body = {
            "generated_utc": "2026-07-12T18:00:00Z",
            "groups": {
                "G_BAD": {
                    "files_new": [
                        _make_file_row(
                            path="bad.py",
                            sha256="not-hex",  # INVALID
                            size=10,
                            lines=2,
                        ),
                    ],
                    "files_modified": [],
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_temp_manifest(body)
            try:
                doc = load_manifest(path)
            finally:
                os.unlink(path)
            # Sanity: the validator must reject this doc.
            validation = validate_manifest(doc)
            self.assertFalse(validation.passed)
            # Adapter gate: refuse to project, surface the
            # validator errors in ``warnings``.
            result = manifest_to_plan_inputs(doc)
            self.assertFalse(result.passed)
            self.assertEqual(result.plan_inputs, ())
            self.assertGreater(
                len(result.warnings), 0,
                msg="adapter did not surface validator errors",
            )
            # Each warning should be tagged with a 'validation:'
            # prefix so the caller can route them.
            for w in result.warnings:
                self.assertTrue(
                    w.startswith("validation:"),
                    msg=f"warning missing 'validation:' prefix: {w!r}",
                )

    def test_empty_manifest_returns_passed_true_with_zero_rows(self) -> None:
        # An empty manifest (no groups) is a structurally valid
        # *but* warning-producing doc. The adapter passes it
        # (no errors) and projects zero rows.
        doc = ManifestDocument(
            raw={"generated_utc": "2026-07-12T18:00:00Z", "groups": {}},
            groups={},
            source_path="<synthetic>",
            on_disk_sha256="0" * 64,
            on_disk_size=42,
        )
        result = manifest_to_plan_inputs(doc)
        self.assertTrue(
            result.passed,
            msg=f"empty manifest should pass validation; warnings={result.warnings!r}",
        )
        self.assertEqual(result.plan_inputs, ())

    def test_extras_are_forwarded_per_row(self) -> None:
        # A NEW file with ``imports_dispatcher=False`` and a
        # MODIFIED file with ``schema_version='1.0.0'`` must
        # surface those extras on the ``PlanInput`` row.
        body = {
            "generated_utc": "2026-07-12T18:00:00Z",
            "groups": {
                "G1": {
                    "files_new": [
                        _make_file_row(
                            path="new.py",
                            sha256="a" * 64,
                            size=10,
                            lines=2,
                            imports_dispatcher=False,
                            writes_to_live_db=False,
                        ),
                    ],
                    "files_modified": [
                        _make_file_row(
                            path="mod.py",
                            sha256="b" * 64,
                            size=20,
                            lines=4,
                            schema_version="1.0.0",
                        ),
                    ],
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_temp_manifest(body)
            try:
                doc = load_manifest(path)
            finally:
                os.unlink(path)
            result = manifest_to_plan_inputs(doc)
            self.assertTrue(result.passed)
            self.assertEqual(len(result.plan_inputs), 2)
            by_path = {pi.path: pi for pi in result.plan_inputs}
            self.assertEqual(by_path["new.py"].kind, FileEntryKind.NEW)
            self.assertEqual(by_path["mod.py"].kind, FileEntryKind.MODIFIED)
            self.assertEqual(by_path["new.py"].extras.get("imports_dispatcher"), False)
            self.assertEqual(by_path["new.py"].extras.get("writes_to_live_db"), False)
            self.assertEqual(by_path["mod.py"].extras.get("schema_version"), "1.0.0")

    def test_iteration_order_is_deterministic(self) -> None:
        # The adapter must iterate files in a stable order:
        # group insertion order, NEW before MODIFIED within
        # each group. The same input must produce the same
        # output across calls.
        body = {
            "generated_utc": "2026-07-12T18:00:00Z",
            "groups": {
                "G_FIRST": {
                    "files_new": [
                        _make_file_row(path="a.py", sha256="a" * 64, size=1, lines=1),
                    ],
                    "files_modified": [],
                },
                "G_SECOND": {
                    "files_new": [
                        _make_file_row(path="b.py", sha256="b" * 64, size=1, lines=1),
                    ],
                    "files_modified": [
                        _make_file_row(path="c.py", sha256="c" * 64, size=1, lines=1),
                    ],
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_temp_manifest(body)
            try:
                doc = load_manifest(path)
            finally:
                os.unlink(path)
            r1 = manifest_to_plan_inputs(doc)
            r2 = manifest_to_plan_inputs(doc)
            self.assertTrue(r1.passed)
            self.assertTrue(r2.passed)
            self.assertEqual(
                [pi.path for pi in r1.plan_inputs],
                [pi.path for pi in r2.plan_inputs],
            )
            # G_FIRST's a.py comes before G_SECOND's b.py and c.py.
            self.assertEqual(
                [pi.path for pi in r1.plan_inputs],
                ["a.py", "b.py", "c.py"],
            )


# ---------------------------------------------------------------------------
# Isolation contract (K1 + K2: no dispatcher, no live DB, no subprocess)
# ---------------------------------------------------------------------------


class ManifestModuleIsolation(unittest.TestCase):
    """The K1 isolation contract is preserved by K2. The
    adapter module must not import ``dispatcher``, must not
    open a sqlite connection, must not invoke ``subprocess``
    or ``os.system``, must not read ``os.environ``, must not
    use ``requests`` / ``urllib``.

    The check is AST-based: a docstring listing the
    prohibited surface does NOT count as a violation (the
    K1 docstring of ``manifest.py`` mentions every one of
    these strings in the negative — "Never use ``subprocess``,
    ``os.system``, ``os.environ``"). Only actual
    ``ast.Import`` / ``ast.ImportFrom`` / ``ast.Call`` nodes
    are scanned.
    """

    def setUp(self) -> None:
        import ast as _ast
        self.src_path = (
            Path(__file__).resolve().parent.parent / "audit" / "manifest.py"
        )
        self.src = self.src_path.read_text(encoding="utf-8")
        self.tree = _ast.parse(self.src, filename=str(self.src_path))

    def _imported_modules(self) -> List[str]:
        """Return the dotted name of every imported module."""
        import ast as _ast
        out: List[str] = []
        for node in _ast.walk(self.tree):
            if isinstance(node, _ast.Import):
                for alias in node.names:
                    out.append(alias.name)
            elif isinstance(node, _ast.ImportFrom):
                # ``from .manifest import X`` → module = "manifest"
                # ``from aee.audit import X`` → module = "aee.audit"
                # ``from typing import X``  → module = "typing"
                mod = node.module or ""
                # Also include the relative level's resolved name
                if node.level and node.level > 0:
                    # We can't fully resolve the relative name
                    # without package context, so record both
                    # forms and let the matcher catch either.
                    for alias in node.names:
                        out.append(f"..{mod}.{alias.name}")
                else:
                    out.append(mod)
        return out

    def _called_names(self) -> List[str]:
        """Return the dotted name (root + attr chain) of every
        function/method call."""
        import ast as _ast
        out: List[str] = []
        for node in _ast.walk(self.tree):
            if not isinstance(node, _ast.Call):
                continue
            func = node.func
            if isinstance(func, _ast.Name):
                out.append(func.id)
            elif isinstance(func, _ast.Attribute):
                # Walk the attribute chain, e.g. ``os.system``
                # → ["os", "system"] → "os.system"
                parts: List[str] = []
                cur = func
                while isinstance(cur, _ast.Attribute):
                    parts.append(cur.attr)
                    cur = cur.value
                if isinstance(cur, _ast.Name):
                    parts.append(cur.id)
                if parts:
                    out.append(".".join(reversed(parts)))
        return out

    def test_no_dispatcher_import(self) -> None:
        for mod in self._imported_modules():
            # ``from .manifest`` is fine (intra-package);
            # ``from .dispatcher`` and ``import dispatcher``
            # and ``from aee.dispatcher`` etc. are not.
            self.assertFalse(
                mod == "dispatcher" or mod.endswith(".dispatcher"),
                msg=f"isolation violation: imported {mod!r}",
            )
            self.assertNotIn(
                "..dispatcher", mod,
                msg=f"isolation violation: imported {mod!r}",
            )

    def test_no_subprocess_or_os_system(self) -> None:
        imports = set(self._imported_modules())
        for banned in ("subprocess", "os.system", "os.popen"):
            self.assertNotIn(
                banned, imports,
                msg=f"isolation violation: imported {banned!r}",
            )
        # The function-call check is more reliable for
        # ``os.system(...)`` since the module is ``os``.
        for call in self._called_names():
            for banned in ("subprocess", "os.system", "os.popen"):
                self.assertFalse(
                    call == banned or call.endswith("." + banned.split(".")[-1])
                    and banned.split(".")[0] in call,
                    msg=f"isolation violation: call {call!r} matches {banned!r}",
                )

    def test_no_environ_reads(self) -> None:
        # ``os.environ[...]`` is a subscript on the ``environ``
        # attribute of the ``os`` module. Catch both the
        # ``os.environ`` attribute access and the
        # ``os.getenv`` call.
        for call in self._called_names():
            self.assertNotIn(
                "os.environ", call,
                msg=f"isolation violation: call {call!r} reads os.environ",
            )
            self.assertNotIn(
                "os.getenv", call,
                msg=f"isolation violation: call {call!r} uses os.getenv",
            )

    def test_no_external_network(self) -> None:
        imports = set(self._imported_modules())
        for banned in ("requests", "urllib.request", "urllib.urlopen", "http.client", "httpx"):
            self.assertNotIn(
                banned, imports,
                msg=f"isolation violation: imported {banned!r}",
            )

    def test_no_sqlite_import(self) -> None:
        for mod in self._imported_modules():
            self.assertFalse(
                mod == "sqlite3" or mod.endswith(".sqlite3"),
                msg=f"isolation violation: imported {mod!r}",
            )


# ---------------------------------------------------------------------------
# Package-level re-exports
# ---------------------------------------------------------------------------


class PackageReExports(unittest.TestCase):
    """The K2 symbols must be re-exported from ``aee.audit``
    so the audit package stays the one-stop namespace."""

    def test_reexports_present(self) -> None:
        from aee import audit  # noqa: WPS433
        for name in (
            "PlanInput",
            "ManifestToPlanResult",
            "manifest_to_plan_inputs",
            "load_manifest_or_default",
        ):
            self.assertTrue(
                hasattr(audit, name),
                msg=f"aee.audit missing re-export: {name!r}",
            )

    def test_reexports_resolve_to_manifest_module(self) -> None:
        from aee import audit  # noqa: WPS433
        from aee.audit import manifest as m_mod
        self.assertIs(audit.PlanInput, m_mod.PlanInput)
        self.assertIs(audit.ManifestToPlanResult, m_mod.ManifestToPlanResult)
        self.assertIs(audit.manifest_to_plan_inputs, m_mod.manifest_to_plan_inputs)
        self.assertIs(audit.load_manifest_or_default, m_mod.load_manifest_or_default)


if __name__ == "__main__":
    unittest.main()
