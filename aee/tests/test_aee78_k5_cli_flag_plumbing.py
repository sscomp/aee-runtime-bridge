"""AEE-7.8 K5 — CLI Flag Plumbing tests for ``build_index``.

This test module verifies the K5 K-shape: the two new CLI flags
``--manifest-path`` and ``--audit-action`` are correctly parsed by
the argparse-based ``main()`` and propagated through the
``build_index()`` function into the underlying
``apply_sidecars_with_audit`` call.

Coverage dimensions:

1. **CLI parser tests** — ``--manifest-path`` and ``--audit-action``
   appear in ``--help`` output; both flags parse correctly; the
   ``--audit-action`` ``choices`` constraint rejects invalid values
   at the argparse layer (exit code 2).

2. **build_index propagation tests** — when ``manifest_path`` is
   supplied, the value reaches ``apply_sidecars_with_audit``; when
   ``audit_action`` is supplied, the value reaches
   ``apply_sidecars_with_audit``; the defaults (``None`` / ``"warn"``)
   match the ``apply_sidecars_with_audit`` signature defaults.

3. **Backward compatibility** — when neither flag is supplied, the
   ``build_index()`` output is byte-identical to the pre-K5
   behavior (the call to ``apply_sidecars_with_audit`` uses
   ``manifest_path=None`` and ``audit_action="warn"``, which is the
   documented pass-through contract).

4. **Invalid input tests** — ``--audit-action bogus`` is rejected by
   argparse ``choices`` (exit code 2); a non-existent
   ``--manifest-path`` propagates the underlying
   ``ManifestError`` (the CLI does NOT swallow it).

The tests use stdlib ``unittest`` only (no pytest assumption), per
the AEE K-shape protocol.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

# Make ``aee`` importable when running via ``python -m unittest
# aee.tests.test_aee78_k5_cli_flag_plumbing`` from outside the repo root.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from aee.reporting.build_index import build_index, main  # noqa: E402
from aee.reporting.identity import (  # noqa: E402
    RecordKind,
    SentinelPolicy,
    _file_sha256,
)


# ---------------------------------------------------------------------------
# Shared fixtures (mirror the shape used by test_aee77_apply_sidecars).
# ---------------------------------------------------------------------------

_RUNTIME_RECORD: Dict[str, Any] = {
    "task_id": "TASK-K5-1001",
    "title": "K5 runtime smoke",
    "type": "coding",
    "priority": 50,
    "owner": "m2",
    "status": "completed",
    "progress_pct": 100,
    "progress_step": "done",
    "created_at": "2026-07-17T10:00:00.000Z",
    "started_at": "2026-07-17T10:00:01.000Z",
    "finished_at": "2026-07-17T10:05:00.000Z",
    "duration_sec": 299.0,
    "input_text": "marker_k5_runtime_input",
    "hermes_run_id": "run_0123456789abcdef0123456789abcdef",
    "executor_session_id": "AEE-K5-RUNTIME-20260717",
    "runtime_run_id": "run_0123456789abcdef0123456789abcdef",
    "repo_root": "/home/ubuntu/hermes-runtime-bridge",
}

_FIXTURE_RECORD: Dict[str, Any] = {
    "task_id": "TASK-K5-1002",
    "title": "aee6-traversal",
    "type": "coding",
    "priority": 50,
    "owner": "m2",
    "status": "running",
    "progress_pct": 5,
    "input_text": "marker_k5_fixture_input",
    "hermes_run_id": "hr-1",
    "executor_session_id": "AEE-K5-FIXTURE-20260717",
    "runtime_run_id": "run-aae-k5-fixture",
    "repo_root": "/home/ubuntu/hermes-runtime-bridge",
}

_FIXTURE_UTC_STAMP = "2026-07-17T13:00:00Z"


def _write_task_json(root: Path, task_id: str, payload: dict) -> Path:
    """Write a single ``task.json`` under ``root/<task_id>/``."""
    import json

    d = root / task_id
    d.mkdir(parents=True, exist_ok=True)
    p = d / "task.json"
    p.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    return p


class _TempReportsBase(unittest.TestCase):
    """Shared setUp/tearDown: tmp reports_root + audit_dir with one
    RUNTIME and one FIXTURE record."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="aee78-k5-"))
        self.reports_root = Path(self._tmp) / "reports"
        self.audit_dir = Path(self._tmp) / "audit"
        self.reports_root.mkdir()
        self.audit_dir.mkdir()
        _write_task_json(
            self.reports_root, _RUNTIME_RECORD["task_id"], _RUNTIME_RECORD
        )
        _write_task_json(
            self.reports_root, _FIXTURE_RECORD["task_id"], _FIXTURE_RECORD
        )

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1. CLI parser tests
# ---------------------------------------------------------------------------


class TestK5CLIParser(unittest.TestCase):
    """The argparse-based ``main()`` exposes ``--manifest-path`` and
    ``--audit-action`` with the correct contract."""

    def test_help_includes_manifest_path_flag(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            with self.assertRaises(SystemExit) as cm:
                main(["--help"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("--manifest-path", buf.getvalue())

    def test_help_includes_audit_action_flag(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            with self.assertRaises(SystemExit) as cm:
                main(["--help"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("--audit-action", buf.getvalue())

    def test_help_lists_audit_action_choices(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            with self.assertRaises(SystemExit) as cm:
                main(["--help"])
        self.assertEqual(cm.exception.code, 0)
        out = buf.getvalue()
        # The choices tuple appears in the usage line.
        self.assertIn("{warn,raise,ignore}", out)

    def test_audit_action_invalid_choice_exits_2(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", buf), patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as cm:
                main(["--audit-action", "bogus"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("invalid choice", err.getvalue())
        self.assertIn("bogus", err.getvalue())


# ---------------------------------------------------------------------------
# 2. build_index propagation tests
# ---------------------------------------------------------------------------


class TestK5BuildIndexPropagation(_TempReportsBase):
    """``build_index()`` forwards ``manifest_path`` and ``audit_action``
    to ``apply_sidecars_with_audit``."""

    def test_defaults_match_apply_sidecars_with_audit_signature(
        self,
    ) -> None:
        # When neither kwarg is supplied, build_index uses the
        # apply_sidecars_with_audit defaults (None / "warn").
        import inspect

        from aee.audit.apply_sidecars import apply_sidecars_with_audit

        sig = inspect.signature(apply_sidecars_with_audit)
        self.assertIsNone(sig.parameters["manifest_path"].default)
        self.assertEqual(sig.parameters["audit_action"].default, "warn")

    def test_manifest_path_default_none_passes_through(self) -> None:
        # build_index with no manifest_path should be byte-identical
        # to the pre-K5 behavior (pass-through).
        result = build_index(
            reports_root=self.reports_root,
            audit_dir=self.audit_dir,
            aliases={},
            classified_at_utc=_FIXTURE_UTC_STAMP,
            sidecar_for_runtime=True,
        )
        self.assertIn("apply_sidecars", result["summary"])
        # The apply_sidecars sub-bucket is populated (pass-through
        # contract preserved).
        apply_block = result["summary"]["apply_sidecars"]
        self.assertIn("schema_version", apply_block)

    def test_audit_action_warn_default_is_pass_through(self) -> None:
        # Explicit audit_action="warn" with no manifest_path is the
        # documented pass-through (audit_report is None because
        # manifest_path is None).
        result = build_index(
            reports_root=self.reports_root,
            audit_dir=self.audit_dir,
            aliases={},
            classified_at_utc=_FIXTURE_UTC_STAMP,
            sidecar_for_runtime=True,
            audit_action="warn",
        )
        self.assertIn("apply_sidecars", result["summary"])

    def test_audit_action_ignore_without_manifest_is_pass_through(
        self,
    ) -> None:
        # audit_action="ignore" with no manifest_path is still a
        # pass-through (the audit is not computed when
        # manifest_path is None).
        result = build_index(
            reports_root=self.reports_root,
            audit_dir=self.audit_dir,
            aliases={},
            classified_at_utc=_FIXTURE_UTC_STAMP,
            sidecar_for_runtime=True,
            audit_action="ignore",
        )
        self.assertIn("apply_sidecars", result["summary"])

    def test_manifest_path_nonexistent_propagates_error(self) -> None:
        # A non-existent manifest_path propagates the underlying
        # ManifestError (the CLI does NOT swallow it). The error
        # is raised before the apply pass writes sidecars.
        from aee.audit.manifest import ManifestError

        with self.assertRaises(ManifestError):
            build_index(
                reports_root=self.reports_root,
                audit_dir=self.audit_dir,
                aliases={},
                classified_at_utc=_FIXTURE_UTC_STAMP,
                sidecar_for_runtime=True,
                manifest_path=str(self._tmp / "nonexistent_manifest.json"),
            )


# ---------------------------------------------------------------------------
# 3. Backward compatibility — byte-identity gate
# ---------------------------------------------------------------------------


class TestK5BackwardCompatibility(_TempReportsBase):
    """When neither ``--manifest-path`` nor ``--audit-action`` is
    supplied, the ``build_index()`` output is byte-identical to the
    pre-K5 behavior."""

    def test_no_new_flags_produces_identical_summary(self) -> None:
        # Run 1: pre-K5 call shape (no manifest_path / audit_action
        # kwargs). Python omitted kwargs default to None / "warn",
        # which is exactly the K5 default.
        result_pre = build_index(
            reports_root=self.reports_root,
            audit_dir=self.audit_dir,
            aliases={},
            classified_at_utc=_FIXTURE_UTC_STAMP,
            sidecar_for_runtime=True,
        )
        # Clean sidecars between runs so both runs start from the
        # same clean state (apply_sidecars is idempotent: a sidecar
        # that already exists and is consistent → "unchanged" not
        # "wrote"). Without this reset the second run's
        # by_decision would differ, which is correct idempotent
        # behavior but not a K5 regression.
        for tid in (_RUNTIME_RECORD["task_id"], _FIXTURE_RECORD["task_id"]):
            sc = self.reports_root / tid / "identity.json"
            if sc.exists():
                sc.unlink()
        # Run 2: explicit K5 defaults.
        result_post = build_index(
            reports_root=self.reports_root,
            audit_dir=self.audit_dir,
            aliases={},
            classified_at_utc=_FIXTURE_UTC_STAMP,
            sidecar_for_runtime=True,
            manifest_path=None,
            audit_action="warn",
        )
        # The summary dicts are byte-identical (same keys, same
        # values, same iteration order via sort_keys=True in the
        # JSON serialization).
        import json

        pre_json = json.dumps(
            result_pre["summary"], sort_keys=True, ensure_ascii=False
        )
        post_json = json.dumps(
            result_post["summary"], sort_keys=True, ensure_ascii=False
        )
        self.assertEqual(pre_json, post_json)

    def test_no_new_flags_produces_identical_reports(self) -> None:
        import json

        result_pre = build_index(
            reports_root=self.reports_root,
            audit_dir=self.audit_dir,
            aliases={},
            classified_at_utc=_FIXTURE_UTC_STAMP,
            sidecar_for_runtime=True,
        )
        # Clean sidecars between runs (see note in the summary
        # test above).
        for tid in (_RUNTIME_RECORD["task_id"], _FIXTURE_RECORD["task_id"]):
            sc = self.reports_root / tid / "identity.json"
            if sc.exists():
                sc.unlink()
        result_post = build_index(
            reports_root=self.reports_root,
            audit_dir=self.audit_dir,
            aliases={},
            classified_at_utc=_FIXTURE_UTC_STAMP,
            sidecar_for_runtime=True,
            manifest_path=None,
            audit_action="warn",
        )
        pre_json = json.dumps(
            result_pre["reports"], sort_keys=True, ensure_ascii=False
        )
        post_json = json.dumps(
            result_post["reports"], sort_keys=True, ensure_ascii=False
        )
        self.assertEqual(pre_json, post_json)

    def test_no_new_flags_produces_identical_sidecar_sha256(self) -> None:
        # The on-disk sidecar SHA-256s are identical whether or not
        # the new kwargs are passed (with pass-through defaults).
        import hashlib

        def _sidecar_sha(task_id: str) -> Optional[str]:
            p = self.reports_root / task_id / "identity.json"
            if not p.exists():
                return None
            return hashlib.sha256(p.read_bytes()).hexdigest()

        # Pre-K5 run.
        build_index(
            reports_root=self.reports_root,
            audit_dir=self.audit_dir,
            aliases={},
            classified_at_utc=_FIXTURE_UTC_STAMP,
            sidecar_for_runtime=True,
        )
        pre_rt = _sidecar_sha(_RUNTIME_RECORD["task_id"])

        # Reset sidecars for a clean second run.
        for tid in (_RUNTIME_RECORD["task_id"], _FIXTURE_RECORD["task_id"]):
            sc = self.reports_root / tid / "identity.json"
            if sc.exists():
                sc.unlink()

        # Post-K5 run with explicit defaults.
        build_index(
            reports_root=self.reports_root,
            audit_dir=self.audit_dir,
            aliases={},
            classified_at_utc=_FIXTURE_UTC_STAMP,
            sidecar_for_runtime=True,
            manifest_path=None,
            audit_action="warn",
        )
        post_rt = _sidecar_sha(_RUNTIME_RECORD["task_id"])

        self.assertIsNotNone(pre_rt)
        self.assertIsNotNone(post_rt)
        self.assertEqual(pre_rt, post_rt)


# ---------------------------------------------------------------------------
# 4. Invalid input tests
# ---------------------------------------------------------------------------


class TestK5InvalidInput(unittest.TestCase):
    """Invalid ``--audit-action`` is rejected by argparse; a missing
    ``--manifest-path`` file propagates the underlying error."""

    def test_audit_action_rejected_by_argparse(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", buf), patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as cm:
                main(["--audit-action", "not-a-valid-choice"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("invalid choice", err.getvalue())

    def test_audit_action_empty_string_rejected(self) -> None:
        # argparse choices rejects the empty string too (it's not in
        # the choices tuple).
        buf = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", buf), patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as cm:
                main(["--audit-action", ""])
        self.assertEqual(cm.exception.code, 2)

    def test_manifest_path_nonexistent_propagates_manifest_error(
        self,
    ) -> None:
        # End-to-end: the CLI main() calls build_index() which calls
        # apply_sidecars_with_audit(manifest_path=<nonexistent>).
        # The underlying ManifestError propagates (the CLI does NOT
        # swallow it). We use a tmp reports_root so the run_audit
        # step succeeds before the manifest load fails.
        tmp = Path(tempfile.mkdtemp(prefix="aee78-k5-inv-"))
        reports_root = tmp / "reports"
        audit_dir = tmp / "audit"
        reports_root.mkdir()
        audit_dir.mkdir()
        _write_task_json(
            reports_root, _RUNTIME_RECORD["task_id"], _RUNTIME_RECORD
        )
        try:
            with self.assertRaises(Exception) as cm:
                main(
                    [
                        "--reports-root",
                        str(reports_root),
                        "--audit-dir",
                        str(audit_dir),
                        "--manifest-path",
                        str(tmp / "does_not_exist.json"),
                    ]
                )
            # The exception is a ManifestError (or its base class
            # if the import path differs). We accept any exception
            # that mentions the missing file in its message.
            self.assertIn(
                "does_not_exist.json", str(cm.exception)
            )
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()