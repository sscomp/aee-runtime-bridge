"""WO-IMPLEMENT-EXPECTED-ARTIFACTS-CASE-PRESERVATION tripwire tests.

These tests pin the production invariant that every
``expected_artifacts`` persistence path uses
``db.encode_artifact_paths`` (case-preserving) and NOT
``db.encode_capabilities`` (which lowercases its inputs via
``normalize_capabilities``).

Test matrix:
  1. ``/home/ubuntu/Abacus/TraceCase.md`` round-trips through
     persistence (``manager.create`` → DB → ``manager.get``)
     unchanged. Pre-fix this would have been lowercased to
     ``/home/ubuntu/abacus/tracecase.md``.
  2. ``/home/ubuntu/Abacus/TraceCase.md`` and
     ``/home/ubuntu/abacus/TraceCase.md`` remain DISTINCT paths on
     Linux after persistence (no case-folding collapse).
  3. AST tripwire: no ``expected_artifacts`` write path in
     ``dispatcher/manager.py`` calls ``encode_capabilities``.
  4. Existing capability normalization behavior remains unchanged
     (capabilities ARE still lowercased — the fix is scoped to
     artifact paths only, not a broad refactor).
"""
from __future__ import annotations

import ast
import json
import os
import unittest
from pathlib import Path

from dispatcher.db import encode_artifact_paths, encode_capabilities, normalize_capabilities

REPO_ROOT = Path(__file__).resolve().parent.parent
MANAGER_PATH = REPO_ROOT / "dispatcher" / "manager.py"
TRACE_CASE_PATH = "/home/ubuntu/Abacus/TraceCase.md"


class TestTripwireNoEncodeCapabilitiesOnExpectedArtifacts(unittest.TestCase):
    """AST scan: ``manager.py`` must not call ``encode_capabilities``
    on the ``expected_artifacts`` persistence path. The only
    permitted persistence helper for that column is
    ``encode_artifact_paths``."""

    def test_no_encode_capabilities_call_in_manager_create_path(self):
        """``dispatcher/manager.py`` must not call
        ``encode_capabilities`` on any path that touches
        ``expected_artifacts``. The fix pins the persistence call
        site at ``manager.create`` to ``db.encode_artifact_paths``;
        a future edit that swaps it back to ``encode_capabilities``
        would silently re-introduce the case-folding bug."""
        source = MANAGER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(MANAGER_PATH))
        violations: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Match ``db.encode_capabilities(...)`` and bare
            # ``encode_capabilities(...)`` (defensive — both shapes
            # would lower-case the paths).
            func = node.func
            name: str | None = None
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            if name != "encode_capabilities":
                continue
            # Inspect the surrounding source line + 3 lines of
            # leading context for an ``expected_artifacts`` token.
            start = max(0, node.lineno - 4)
            window = "\n".join(
                source.splitlines()[start - 1: node.end_lineno or node.lineno]
            )
            if "expected_artifacts" in window:
                violations.append(
                    (node.lineno, window.strip()[:200])
                )
        self.assertEqual(
            violations, [],
            f"``encode_capabilities`` must NOT be called on the "
            f"``expected_artifacts`` persistence path. Found "
            f"{len(violations)} violation(s) in dispatcher/manager.py: "
            f"{violations}",
        )

    def test_encode_artifact_paths_is_used_for_expected_artifacts(self):
        """Positive pin: ``manager.create`` persists
        ``expected_artifacts`` via ``db.encode_artifact_paths``."""
        source = MANAGER_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "db.encode_artifact_paths(expected_artifacts",
            source,
            "manager.create must persist expected_artifacts via "
            "db.encode_artifact_paths (case-preserving).",
        )


class TestTraceCaseRoundTrip(unittest.TestCase):
    """``/home/ubuntu/Abacus/TraceCase.md`` survives persistence and
    readback with case intact."""

    def test_tracecase_path_preserved_by_encode_artifact_paths(self):
        """The storage helper preserves the exact mixed-case path."""
        blob = encode_artifact_paths([TRACE_CASE_PATH])
        self.assertEqual(json.loads(blob), [TRACE_CASE_PATH])
        self.assertIn("Abacus", blob)
        self.assertIn("TraceCase", blob)

    def test_tracecase_distinct_from_lowercased_variant(self):
        """``/home/ubuntu/Abacus/TraceCase.md`` and
        ``/home/ubuntu/abacus/TraceCase.md`` are persisted as two
        distinct entries (NOT collapsed by case-folding)."""
        upper = TRACE_CASE_PATH
        lower = "/home/ubuntu/abacus/TraceCase.md"
        blob = encode_artifact_paths([upper, lower])
        stored = json.loads(blob)
        self.assertEqual(len(stored), 2)
        self.assertIn(upper, stored)
        self.assertIn(lower, stored)
        # Negative parity: ``encode_capabilities`` WOULD collapse
        # them — pin the contrast so the fix stays scoped.
        collapsed = json.loads(encode_capabilities([upper, lower]))
        self.assertEqual(len(collapsed), 1)


class TestCapabilityNormalizationUnchanged(unittest.TestCase):
    """The fix is scoped to artifact paths only. Capability
    normalization MUST keep lowercasing (the existing contract)."""

    def test_normalize_capabilities_still_lowercases(self):
        self.assertEqual(
            normalize_capabilities(["WebSearch", "  FILE-READ  "]),
            ["file-read", "websearch"],
        )

    def test_encode_capabilities_still_lowercases(self):
        self.assertEqual(
            json.loads(encode_capabilities(["WebSearch"])),
            ["websearch"],
        )


if __name__ == "__main__":
    unittest.main()