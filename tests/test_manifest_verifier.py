"""Tests for ``aee.adapters.manifest_verifier``.

The verifier is a pure function. Tests build a temp run directory
under ``/tmp`` with a fake ``completion.verified.json`` and
``completion.claim.json`` plus optional required artifacts, then
exercise the 21 listed failure / success modes from TASK-M2.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

# Make the bridge root importable when running this file directly.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aee.adapters import manifest_verifier as mv  # noqa: E402


# --- Fixtures ---------------------------------------------------------


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """Empty run directory for a fake task."""
    d = tmp_path / "TASK-1--RUN-1"
    d.mkdir()
    return d


@pytest.fixture
def repo_path(tmp_path: Path) -> Path:
    """A fake git repository root (empty)."""
    p = tmp_path / "repo"
    p.mkdir()
    return p


def _write_manifest(
    run_dir: Path,
    *,
    schema_version: str = "1.0.0",
    task_id: str = "TASK-1--RUN-1",
    executor_overrides: dict | None = None,
    verification_overrides: dict | None = None,
    safety_overrides: dict | None = None,
    process_group_overrides: dict | None = None,
    claim_manifest: dict | None = None,
    top_status: str = "COMPLETED",
    top_verdict: str = "PASS",
) -> Path:
    """Write a synthetic ``completion.verified.json`` and return its path.

    Defaults are all "pass" — tests override to inject failure
    modes. ``claim_manifest`` is also written beside it.

    TASK-M3 FIX-1: the helper now defaults to top-level
    ``status="COMPLETED"`` and ``verdict="PASS"`` so the existing
    tests (which only override the executor block) keep working
    under the stricter verifier. Tests that want to inject
    cancellation / failure pass ``top_status="CANCELLED"`` and
    ``top_verdict="FAIL"`` explicitly.
    """
    executor = {
        "type": "claude-code",
        "is_error": False,
        "subtype": "success",
        "terminal_reason": "completed",
        "exit_code": 0,
    }
    if executor_overrides:
        executor.update(executor_overrides)
    verification = {
        "verification_errors": [],
        "safety_violations": [],
        "artifacts": [],
    }
    if verification_overrides:
        verification.update(verification_overrides)
    safety = {"violations": []}
    if safety_overrides:
        safety.update(safety_overrides)
    process_group = {"verified_dead": True}
    if process_group_overrides:
        process_group.update(process_group_overrides)
    manifest = {
        "schema_version": schema_version,
        "task_id": task_id,
        "status": top_status,
        "verdict": top_verdict,
        "executor": executor,
        "verification": verification,
        "safety": safety,
        "process_group": process_group,
    }
    p = run_dir / "completion.verified.json"
    p.write_text(json.dumps(manifest), encoding="utf-8")
    # Companion claim
    claim_obj = claim_manifest if claim_manifest is not None else {
        "task_id": task_id,
        "ran_at": "2026-07-14T00:00:00Z",
    }
    cp = run_dir / "completion.claim.json"
    cp.write_text(json.dumps(claim_obj), encoding="utf-8")
    # Stamp claim_manifest_hash to match on-disk
    actual = hashlib.sha256(cp.read_bytes()).hexdigest()
    manifest["verification"]["claim_manifest_hash"] = actual
    p.write_text(json.dumps(manifest), encoding="utf-8")
    return p


def _write_artifact(repo: Path, rel: str, content: bytes) -> str:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    h = hashlib.sha256(content).hexdigest()
    return h


# --- Positive path ----------------------------------------------------


def test_valid_manifest_passes(run_dir, repo_path):
    art_rel = "src/ok.txt"
    art_hash = _write_artifact(repo_path, art_rel, b"hello")
    p = _write_manifest(
        run_dir,
        verification_overrides={
            "artifacts": [
                {"path": art_rel, "sha256": art_hash, "verified": True}
            ]
        },
    )
    res = mv.verify_completion_manifest(
        verified_manifest_path=p,
        expected_task_id="TASK-1--RUN-1",
        expected_run_dir=run_dir,
        repo_path=repo_path,
        required_artifacts=[art_rel],
        subprocess_exit_code=0,
    )
    assert res.verified is True
    assert res.verification_errors == []
    assert res.schema_version == "1.0.0"
    assert res.artifacts_rechecked == 1
    assert res.claim_hash_match is True
    assert res.process_group_verified_dead is True


# --- Negative cases ---------------------------------------------------


def test_missing_manifest(run_dir, repo_path):
    res = mv.verify_completion_manifest(
        verified_manifest_path=run_dir / "completion.verified.json",
        expected_task_id="TASK-1--RUN-1",
        expected_run_dir=run_dir,
        repo_path=repo_path,
    )
    assert res.verified is False
    assert mv.E_VERIFIED_MANIFEST_MISSING in res.verification_errors


def test_manifest_symlink(run_dir, repo_path):
    target = run_dir / "real.json"
    target.write_text("{}", encoding="utf-8")
    link = run_dir / "completion.verified.json"
    os.symlink(target, link)
    res = mv.verify_completion_manifest(
        verified_manifest_path=link,
        expected_task_id="TASK-1--RUN-1",
        expected_run_dir=run_dir,
        repo_path=repo_path,
    )
    assert res.verified is False
    assert mv.E_VERIFIED_MANIFEST_SYMLINK in res.verification_errors


def test_invalid_json(run_dir, repo_path):
    p = run_dir / "completion.verified.json"
    p.write_text("not json {", encoding="utf-8")
    res = mv.verify_completion_manifest(
        verified_manifest_path=p,
        expected_task_id="TASK-1--RUN-1",
        expected_run_dir=run_dir,
        repo_path=repo_path,
    )
    assert res.verified is False
    assert mv.E_VERIFIED_MANIFEST_INVALID_JSON in res.verification_errors


def test_unsupported_schema(run_dir, repo_path):
    p = _write_manifest(run_dir, schema_version="2.5.0")
    res = mv.verify_completion_manifest(
        verified_manifest_path=p,
        expected_task_id="TASK-1--RUN-1",
        expected_run_dir=run_dir,
        repo_path=repo_path,
    )
    assert res.verified is False
    assert mv.E_UNSUPPORTED_SCHEMA_VERSION in res.verification_errors
    assert res.schema_version == "2.5.0"


def test_task_id_mismatch(run_dir, repo_path):
    p = _write_manifest(run_dir, task_id="OTHER--TASK")
    res = mv.verify_completion_manifest(
        verified_manifest_path=p,
        expected_task_id="TASK-1--RUN-1",
        expected_run_dir=run_dir,
        repo_path=repo_path,
    )
    assert res.verified is False
    assert mv.E_TASK_ID_MISMATCH in res.verification_errors


def test_executor_type_mismatch(run_dir, repo_path):
    p = _write_manifest(
        run_dir, executor_overrides={"type": "hermes"}
    )
    res = mv.verify_completion_manifest(
        verified_manifest_path=p,
        expected_task_id="TASK-1--RUN-1",
        expected_run_dir=run_dir,
        repo_path=repo_path,
    )
    assert res.verified is False
    assert mv.E_EXECUTOR_TYPE_MISMATCH in res.verification_errors


def test_exit_code_mismatch(run_dir, repo_path):
    p = _write_manifest(run_dir, executor_overrides={"exit_code": 0})
    res = mv.verify_completion_manifest(
        verified_manifest_path=p,
        expected_task_id="TASK-1--RUN-1",
        expected_run_dir=run_dir,
        repo_path=repo_path,
        subprocess_exit_code=1,  # subprocess said 1
    )
    assert res.verified is False
    assert mv.E_RUNNER_EXIT_CODE_MISMATCH in res.verification_errors


def test_is_error_true(run_dir, repo_path):
    p = _write_manifest(run_dir, executor_overrides={"is_error": True})
    res = mv.verify_completion_manifest(
        verified_manifest_path=p,
        expected_task_id="TASK-1--RUN-1",
        expected_run_dir=run_dir,
        repo_path=repo_path,
    )
    assert res.verified is False
    assert mv.E_RUNNER_IS_ERROR_TRUE in res.verification_errors


def test_subtype_not_success(run_dir, repo_path):
    p = _write_manifest(run_dir, executor_overrides={"subtype": "error"})
    res = mv.verify_completion_manifest(
        verified_manifest_path=p,
        expected_task_id="TASK-1--RUN-1",
        expected_run_dir=run_dir,
        repo_path=repo_path,
    )
    assert res.verified is False
    assert mv.E_RUNNER_SUBTYPE_NOT_SUCCESS in res.verification_errors


def test_terminal_reason_not_completed(run_dir, repo_path):
    p = _write_manifest(
        run_dir, executor_overrides={"terminal_reason": "cancelled"}
    )
    res = mv.verify_completion_manifest(
        verified_manifest_path=p,
        expected_task_id="TASK-1--RUN-1",
        expected_run_dir=run_dir,
        repo_path=repo_path,
    )
    assert res.verified is False
    assert mv.E_RUNNER_TERMINAL_REASON_NOT_COMPLETED in res.verification_errors


def test_verification_errors_present(run_dir, repo_path):
    p = _write_manifest(
        run_dir,
        verification_overrides={"verification_errors": ["x"]},
    )
    res = mv.verify_completion_manifest(
        verified_manifest_path=p,
        expected_task_id="TASK-1--RUN-1",
        expected_run_dir=run_dir,
        repo_path=repo_path,
    )
    assert res.verified is False
    assert mv.E_RUNNER_VERIFICATION_ERRORS_PRESENT in res.verification_errors


def test_safety_violations_present(run_dir, repo_path):
    p = _write_manifest(
        run_dir,
        safety_overrides={"violations": ["touched /etc/passwd"]},
    )
    res = mv.verify_completion_manifest(
        verified_manifest_path=p,
        expected_task_id="TASK-1--RUN-1",
        expected_run_dir=run_dir,
        repo_path=repo_path,
    )
    assert res.verified is False
    assert mv.E_RUNNER_SAFETY_VIOLATIONS_PRESENT in res.verification_errors


def test_claim_manifest_missing(run_dir, repo_path):
    p = _write_manifest(run_dir)
    (run_dir / "completion.claim.json").unlink()
    res = mv.verify_completion_manifest(
        verified_manifest_path=p,
        expected_task_id="TASK-1--RUN-1",
        expected_run_dir=run_dir,
        repo_path=repo_path,
    )
    assert res.verified is False
    assert mv.E_CLAIM_MANIFEST_MISSING in res.verification_errors


def test_claim_hash_mismatch(run_dir, repo_path):
    p = _write_manifest(run_dir)
    # Tamper the claim file
    (run_dir / "completion.claim.json").write_text(
        json.dumps({"task_id": "TAMPERED"}), encoding="utf-8"
    )
    res = mv.verify_completion_manifest(
        verified_manifest_path=p,
        expected_task_id="TASK-1--RUN-1",
        expected_run_dir=run_dir,
        repo_path=repo_path,
    )
    assert res.verified is False
    assert mv.E_CLAIM_MANIFEST_HASH_MISMATCH in res.verification_errors


def test_required_artifact_missing(run_dir, repo_path):
    p = _write_manifest(
        run_dir,
        verification_overrides={
            "artifacts": [
                {"path": "nope.txt", "sha256": "x" * 64, "verified": True}
            ]
        },
    )
    res = mv.verify_completion_manifest(
        verified_manifest_path=p,
        expected_task_id="TASK-1--RUN-1",
        expected_run_dir=run_dir,
        repo_path=repo_path,
        required_artifacts=["nope.txt"],
    )
    assert res.verified is False
    assert mv.E_REQUIRED_ARTIFACT_MISSING in res.verification_errors


def test_artifact_path_traversal_rejected(run_dir, repo_path):
    # Required artifact asks for a traversal; never reachable.
    p = _write_manifest(run_dir)
    res = mv.verify_completion_manifest(
        verified_manifest_path=p,
        expected_task_id="TASK-1--RUN-1",
        expected_run_dir=run_dir,
        repo_path=repo_path,
        required_artifacts=["../escape.txt"],
    )
    assert res.verified is False
    assert mv.E_ARTIFACT_PATH_ESCAPE in res.verification_errors


def test_artifact_symlink_rejected(run_dir, repo_path):
    rel = "a/link.txt"
    real = repo_path / "real.txt"
    real.write_bytes(b"hello")
    link = repo_path / rel
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(real, link)
    h = hashlib.sha256(b"hello").hexdigest()
    p = _write_manifest(
        run_dir,
        verification_overrides={
            "artifacts": [
                {"path": rel, "sha256": h, "verified": True}
            ]
        },
    )
    res = mv.verify_completion_manifest(
        verified_manifest_path=p,
        expected_task_id="TASK-1--RUN-1",
        expected_run_dir=run_dir,
        repo_path=repo_path,
        required_artifacts=[rel],
    )
    assert res.verified is False
    assert mv.E_ARTIFACT_SYMLINK in res.verification_errors


def test_artifact_byte_mismatch(run_dir, repo_path):
    rel = "a/x.txt"
    real = repo_path / rel
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_bytes(b"actual content")
    # Manifest claims the wrong hash
    p = _write_manifest(
        run_dir,
        verification_overrides={
            "artifacts": [
                {"path": rel, "sha256": "0" * 64, "verified": True}
            ]
        },
    )
    res = mv.verify_completion_manifest(
        verified_manifest_path=p,
        expected_task_id="TASK-1--RUN-1",
        expected_run_dir=run_dir,
        repo_path=repo_path,
        required_artifacts=[rel],
    )
    assert res.verified is False
    assert mv.E_ARTIFACT_SHA256_MISMATCH in res.verification_errors


def test_artifact_sha256_mismatch(run_dir, repo_path):
    rel = "a/x.txt"
    real = repo_path / rel
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_bytes(b"actual content")
    # Hash is technically a valid hex but wrong
    fake_hash = "a" * 64
    p = _write_manifest(
        run_dir,
        verification_overrides={
            "artifacts": [
                {"path": rel, "sha256": fake_hash, "verified": True}
            ]
        },
    )
    res = mv.verify_completion_manifest(
        verified_manifest_path=p,
        expected_task_id="TASK-1--RUN-1",
        expected_run_dir=run_dir,
        repo_path=repo_path,
        required_artifacts=[rel],
    )
    assert res.verified is False
    assert mv.E_ARTIFACT_SHA256_MISMATCH in res.verification_errors


def test_process_group_not_verified_dead(run_dir, repo_path):
    p = _write_manifest(
        run_dir, process_group_overrides={"verified_dead": False}
    )
    res = mv.verify_completion_manifest(
        verified_manifest_path=p,
        expected_task_id="TASK-1--RUN-1",
        expected_run_dir=run_dir,
        repo_path=repo_path,
    )
    assert res.verified is False
    assert mv.E_PROCESS_GROUP_NOT_VERIFIED_DEAD in res.verification_errors


# --- TASK-M3 FIX-7: cancellation regression ---------------------------
#
# A manifest with top-level ``status="CANCELLED"`` and
# ``verdict="FAIL"`` MUST NEVER be promoted to ``verified=True``,
# even if the executor block says ``is_error=False``,
# ``subtype="success"``, ``terminal_reason="completed"``. This is
# the exact failure mode that Probe A in the Independent Review
# demonstrated: a fake Runner that writes a lying executor block
# on top of honest top-level fields.


def test_cancelled_status_cannot_become_verified(run_dir, repo_path):
    """A manifest with ``status="CANCELLED"`` must be rejected even
    if every other field looks like a clean completion. The new
    ``E_STATUS_NOT_COMPLETED`` error code is the contract.
    """
    p = _write_manifest(
        run_dir,
        # Top-level: the Runner honestly says it was cancelled.
        # (We bypass the default to inject this; the
        # ``_write_manifest`` helper always writes a passing
        # top-level, so we have to patch the file in place.)
    )
    # Patch the top-level status to CANCELLED.
    import json as _json
    obj = _json.loads(p.read_text(encoding="utf-8"))
    obj["status"] = "CANCELLED"
    obj["verdict"] = "FAIL"
    p.write_text(_json.dumps(obj), encoding="utf-8")
    res = mv.verify_completion_manifest(
        verified_manifest_path=p,
        expected_task_id="TASK-1--RUN-1",
        expected_run_dir=run_dir,
        repo_path=repo_path,
    )
    # CRITICAL: verified MUST be False. A CANCELLED run is never
    # a completed run, regardless of what the executor block says.
    assert res.verified is False, (
        "CANCELLED manifest must NEVER verify as True (TASK-M3 FIX-7)"
    )
    assert mv.E_STATUS_NOT_COMPLETED in res.verification_errors
    assert mv.E_VERDICT_NOT_PASS in res.verification_errors


def test_failed_verdict_cannot_become_verified(run_dir, repo_path):
    """A manifest with ``verdict="FAIL"`` must be rejected even if
    the status is still COMPLETED (e.g. partial completion).
    """
    p = _write_manifest(run_dir)
    import json as _json
    obj = _json.loads(p.read_text(encoding="utf-8"))
    obj["verdict"] = "FAIL"
    p.write_text(_json.dumps(obj), encoding="utf-8")
    res = mv.verify_completion_manifest(
        verified_manifest_path=p,
        expected_task_id="TASK-1--RUN-1",
        expected_run_dir=run_dir,
        repo_path=repo_path,
    )
    assert res.verified is False
    assert mv.E_VERDICT_NOT_PASS in res.verification_errors


def test_timed_out_status_cannot_become_verified(run_dir, repo_path):
    """A manifest with ``status="TIMED_OUT"`` must be rejected; the
    only positive top-level status is ``"COMPLETED"``."""
    p = _write_manifest(run_dir)
    import json as _json
    obj = _json.loads(p.read_text(encoding="utf-8"))
    obj["status"] = "TIMED_OUT"
    p.write_text(_json.dumps(obj), encoding="utf-8")
    res = mv.verify_completion_manifest(
        verified_manifest_path=p,
        expected_task_id="TASK-1--RUN-1",
        expected_run_dir=run_dir,
        repo_path=repo_path,
    )
    assert res.verified is False
    assert mv.E_STATUS_NOT_COMPLETED in res.verification_errors


def test_missing_top_level_status_and_verdict_rejected(run_dir, repo_path):
    """A manifest that omits the top-level ``status`` and
    ``verdict`` keys entirely must be rejected with both error
    codes (defence in depth: schema drift in the Runner cannot
    silently re-open the contract)."""
    p = _write_manifest(run_dir)
    import json as _json
    obj = _json.loads(p.read_text(encoding="utf-8"))
    obj.pop("status", None)
    obj.pop("verdict", None)
    p.write_text(_json.dumps(obj), encoding="utf-8")
    res = mv.verify_completion_manifest(
        verified_manifest_path=p,
        expected_task_id="TASK-1--RUN-1",
        expected_run_dir=run_dir,
        repo_path=repo_path,
    )
    assert res.verified is False
    assert mv.E_STATUS_NOT_COMPLETED in res.verification_errors
    assert mv.E_VERDICT_NOT_PASS in res.verification_errors


def test_completed_status_pass_verdict_passes(run_dir, repo_path):
    """Positive control: a manifest with top-level
    ``status="COMPLETED"`` and ``verdict="PASS"`` (the canonical
    Runner happy-path shape) MUST verify as True.
    """
    p = _write_manifest(run_dir)
    import json as _json
    obj = _json.loads(p.read_text(encoding="utf-8"))
    obj["status"] = "COMPLETED"
    obj["verdict"] = "PASS"
    p.write_text(_json.dumps(obj), encoding="utf-8")
    res = mv.verify_completion_manifest(
        verified_manifest_path=p,
        expected_task_id="TASK-1--RUN-1",
        expected_run_dir=run_dir,
        repo_path=repo_path,
    )
    assert res.verified is True
    assert res.verification_errors == []
