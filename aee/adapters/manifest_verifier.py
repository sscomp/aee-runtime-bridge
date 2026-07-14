"""Manifest Verifier — Hermes-side independent verification of the
Runner ``completion.verified.json``.

Purpose
-------
Eliminate the premature-completion failure mode where the Runner
subprocess exits successfully but the task has not actually
completed in a verifiable way. The verifier treats
``completion.verified.json`` as *untrusted input* — it is parsed
in-process and the evidence it references is re-computed from
disk before the dispatcher transitions the task to ``completed``.

Verification scope (MVP)
-----------------------
For MVP the verifier enforces a hard list of invariants; a single
missing check downgrades the run to ``failed`` (the watcher maps
this to ``manager.fail``). The list matches the task contract
(``TASK-M2-Executor-Router-Claude-Adapter-Manifest-Gate-MVP.md``):

* Manifest exists, is a regular file, is not a symlink, lives
  inside the expected run directory, and JSON-parses.
* Schema version is one of the supported versions (MVP: 1.0.0).
* ``task_id`` matches the composite ``<HERMES_TASK_ID>--<HERMES_RUN_ID>``.
* **Top-level ``status == "COMPLETED"`` and ``verdict == "PASS"``**
  (TASK-M3 FIX-1). The executor-block fields alone are insufficient
  because a fake/buggy Runner can lie in the executor block while
  the top-level fields honestly say ``CANCELLED`` / ``FAIL``.
* Executor is ``claude-code`` and its ``is_error`` is false, with
  ``subtype == "success"`` and ``terminal_reason == "completed"``.
* The Runner-recorded exit code matches the subprocess exit code
  the adapter observed.
* ``verification_errors`` is empty and ``safety_violations`` is empty.
* The companion ``completion.claim.json`` exists and its on-disk
  SHA-256 matches ``verification.claim_manifest_hash``.
* Every required artifact (relative path under ``repo_path``)
  exists, is a regular file, is not a symlink, and its on-disk
  SHA-256 matches the value the Runner recorded.
* ``process_group.verified_dead`` is true.

Artifact schema compatibility (TASK-M3 FIX-2)
---------------------------------------------
The committed Runner (``scripts/claude_code_runner.py``) writes the
artifacts list at the **top level** of the manifest under the key
``artifacts``. Some intermediate drafts placed it under
``verification.artifacts``. To remain compatible with both, the
verifier reads ``manifest.artifacts`` first and falls back to
``verification.artifacts``. Only one of the two is required for
the manifest to verify.

The verifier is a pure function — no global state, no network, no
subprocess. The caller (the adapter, the watcher) is responsible
for sequencing.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


# --- Supported schema versions --------------------------------------------

SUPPORTED_SCHEMA_VERSIONS = frozenset({"1.0.0"})


# --- Error codes (string constants — used by tests + watcher) -----------

E_VERIFIED_MANIFEST_MISSING = "verified_manifest_missing"
E_VERIFIED_MANIFEST_SYMLINK = "verified_manifest_symlink"
E_VERIFIED_MANIFEST_NOT_REGULAR = "verified_manifest_not_regular"
E_VERIFIED_MANIFEST_OUTSIDE_RUN_DIR = "verified_manifest_outside_run_dir"
E_VERIFIED_MANIFEST_INVALID_JSON = "verified_manifest_invalid_json"
E_UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
E_TASK_ID_MISMATCH = "task_id_mismatch"
E_EXECUTOR_TYPE_MISMATCH = "executor_type_mismatch"
E_RUNNER_EXIT_CODE_MISMATCH = "runner_exit_code_mismatch"
E_RUNNER_TERMINAL_REASON_NOT_COMPLETED = "runner_terminal_reason_not_completed"
E_RUNNER_SUBTYPE_NOT_SUCCESS = "runner_subtype_not_success"
E_RUNNER_IS_ERROR_TRUE = "runner_is_error_true"
E_RUNNER_VERIFICATION_ERRORS_PRESENT = "runner_verification_errors_present"
E_RUNNER_SAFETY_VIOLATIONS_PRESENT = "runner_safety_violations_present"
E_REQUIRED_ARTIFACT_MISSING = "required_artifact_missing"
E_ARTIFACT_PATH_ESCAPE = "artifact_path_escape"
E_ARTIFACT_SYMLINK = "artifact_symlink"
E_ARTIFACT_SHA256_MISMATCH = "artifact_sha256_mismatch"
E_CLAIM_MANIFEST_MISSING = "claim_manifest_missing"
E_CLAIM_MANIFEST_HASH_MISMATCH = "claim_manifest_hash_mismatch"
E_PROCESS_GROUP_NOT_VERIFIED_DEAD = "process_group_not_verified_dead"
# TASK-M3 FIX-1: top-level status/verdict must be COMPLETED/PASS
# for a manifest to be considered verified. The executor-block
# fields alone are insufficient (see the docstring).
E_STATUS_NOT_COMPLETED = "status_not_completed"
E_VERDICT_NOT_PASS = "verdict_not_pass"


# --- Result dataclass ---------------------------------------------------


@dataclass
class VerificationResult:
    """Outcome of an independent verify of ``completion.verified.json``.

    Attributes:
        verified: True iff *all* required checks passed.
        verification_errors: List of error codes. Empty on success.
        schema_version: The version that was verified, or None if the
            manifest could not be read.
        artifacts_rechecked: Number of required artifacts whose
            SHA-256 was recomputed against disk.
        claim_hash_match: True iff the claim manifest hash matched.
        process_group_verified_dead: True iff the manifest confirmed
            the Claude process group is verified dead.
    """

    verified: bool
    verification_errors: List[str] = field(default_factory=list)
    schema_version: Optional[str] = None
    artifacts_rechecked: int = 0
    claim_hash_match: bool = False
    process_group_verified_dead: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verified": self.verified,
            "verification_errors": list(self.verification_errors),
            "schema_version": self.schema_version,
            "artifacts_rechecked": self.artifacts_rechecked,
            "claim_hash_match": self.claim_hash_match,
            "process_group_verified_dead": self.process_group_verified_dead,
        }


# --- Public API ---------------------------------------------------------


def verify_completion_manifest(
    *,
    verified_manifest_path: os.PathLike[str] | str,
    expected_task_id: str,
    expected_run_dir: os.PathLike[str] | str,
    repo_path: os.PathLike[str] | str,
    required_artifacts: Sequence[str] = (),
    subprocess_exit_code: Optional[int] = None,
) -> VerificationResult:
    """Run all hard checks against the Runner's verified manifest.

    Args:
        verified_manifest_path: Absolute path to
            ``completion.verified.json``.
        expected_task_id: The composite Runner task id
            ``<HERMES_TASK_ID>--<HERMES_RUN_ID>`` the caller expects
            to find in the manifest.
        expected_run_dir: Absolute path to the run directory
            (``<runs_root>/<composite_id>/``). The manifest must
            resolve inside this directory.
        repo_path: Absolute path of the target git repository.
            Required artifacts are resolved relative to this path
            and must stay inside it.
        required_artifacts: Sequence of relative paths (forward
            slashes, no traversal). The manifest must list each
            one with a matching ``verified`` flag and SHA-256.
        subprocess_exit_code: The exit code the adapter observed
            from the Runner subprocess. Compared against
            ``executor.exit_code`` recorded in the manifest.

    Returns:
        A :class:`VerificationResult` with ``verified`` and the
        error list. The result is always returned (never raised)
        so the caller can convert to a watcher decision.
    """
    errors: List[str] = []
    manifest_obj: Optional[Dict[str, Any]] = None
    schema_version: Optional[str] = None
    artifacts_rechecked = 0
    claim_hash_match = False
    pg_verified_dead = False

    expected_dir = Path(expected_run_dir).resolve()
    repo_root = Path(repo_path).resolve()
    manifest_path = Path(verified_manifest_path)

    # 1. Manifest path validation
    if not manifest_path.exists():
        # Follow symlink-safe check first: even if exists() is
        # True because of a broken symlink, lstat will reveal it.
        try:
            st = os.lstat(manifest_path)
        except OSError:
            st = None
        if st is None or not (manifest_path.is_file() or manifest_path.is_symlink()):
            errors.append(E_VERIFIED_MANIFEST_MISSING)
            return VerificationResult(
                verified=False,
                verification_errors=errors,
            )
    try:
        st = os.lstat(manifest_path)
    except OSError:
        errors.append(E_VERIFIED_MANIFEST_MISSING)
        return VerificationResult(
            verified=False,
            verification_errors=errors,
        )
    if os.path.islink(manifest_path) or os.path.islink(str(manifest_path)):
        errors.append(E_VERIFIED_MANIFEST_SYMLINK)
        return VerificationResult(
            verified=False,
            verification_errors=errors,
        )
    if not stat_is_regular_file(st):
        errors.append(E_VERIFIED_MANIFEST_NOT_REGULAR)
        return VerificationResult(
            verified=False,
            verification_errors=errors,
        )
    # Real path must live inside expected_run_dir
    try:
        real_manifest = manifest_path.resolve(strict=True)
    except (OSError, RuntimeError):
        errors.append(E_VERIFIED_MANIFEST_MISSING)
        return VerificationResult(
            verified=False,
            verification_errors=errors,
        )
    try:
        real_manifest.relative_to(expected_dir)
    except ValueError:
        errors.append(E_VERIFIED_MANIFEST_OUTSIDE_RUN_DIR)
        return VerificationResult(
            verified=False,
            verification_errors=errors,
        )

    # 2. Parse JSON
    try:
        with open(real_manifest, "r", encoding="utf-8") as f:
            manifest_obj = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        errors.append(E_VERIFIED_MANIFEST_INVALID_JSON)
        return VerificationResult(
            verified=False,
            verification_errors=errors,
        )
    if not isinstance(manifest_obj, dict):
        errors.append(E_VERIFIED_MANIFEST_INVALID_JSON)
        return VerificationResult(
            verified=False,
            verification_errors=errors,
        )

    # 3. Schema version
    schema_version = manifest_obj.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        errors.append(E_UNSUPPORTED_SCHEMA_VERSION)
        return VerificationResult(
            verified=False,
            verification_errors=errors,
            schema_version=schema_version,
        )

    # 4. Task identity
    actual_task_id = manifest_obj.get("task_id")
    if actual_task_id != expected_task_id:
        errors.append(E_TASK_ID_MISMATCH)
        # Identity mismatch is the most fundamental failure; we
        # bail early because downstream fields cannot be trusted.
        return VerificationResult(
            verified=False,
            verification_errors=errors,
            schema_version=schema_version,
        )

    # 4a. TASK-M3 FIX-1: top-level status + verdict.
    # The executor block can say "completed" while the top-level
    # ``status`` and ``verdict`` honestly say "CANCELLED" / "FAIL".
    # We require both at the top level so a cancellation or
    # failure can never be promoted to ``verified=True``.
    top_status = manifest_obj.get("status")
    if top_status != "COMPLETED":
        errors.append(E_STATUS_NOT_COMPLETED)
    top_verdict = manifest_obj.get("verdict")
    if top_verdict != "PASS":
        errors.append(E_VERDICT_NOT_PASS)

    # 5. Executor block
    executor = manifest_obj.get("executor") or {}
    if not isinstance(executor, dict):
        errors.append(E_EXECUTOR_TYPE_MISMATCH)
        return VerificationResult(
            verified=False,
            verification_errors=errors,
            schema_version=schema_version,
        )
    if executor.get("type") != "claude-code":
        errors.append(E_EXECUTOR_TYPE_MISMATCH)
        return VerificationResult(
            verified=False,
            verification_errors=errors,
            schema_version=schema_version,
        )
    if executor.get("is_error") is True:
        errors.append(E_RUNNER_IS_ERROR_TRUE)
    if executor.get("subtype") != "success":
        errors.append(E_RUNNER_SUBTYPE_NOT_SUCCESS)
    if executor.get("terminal_reason") != "completed":
        errors.append(E_RUNNER_TERMINAL_REASON_NOT_COMPLETED)
    if subprocess_exit_code is not None:
        runner_exit = executor.get("exit_code")
        if runner_exit is None or int(runner_exit) != int(subprocess_exit_code):
            errors.append(E_RUNNER_EXIT_CODE_MISMATCH)

    # 6. Runner self-reported verification + safety
    verification = manifest_obj.get("verification") or {}
    if not isinstance(verification, dict):
        verification = {}
    runner_verif_errors = verification.get("verification_errors") or []
    if runner_verif_errors:
        errors.append(E_RUNNER_VERIFICATION_ERRORS_PRESENT)
    safety = manifest_obj.get("safety") or {}
    if not isinstance(safety, dict):
        safety = {}
    safety_violations = safety.get("violations") or []
    if safety_violations:
        errors.append(E_RUNNER_SAFETY_VIOLATIONS_PRESENT)

    # 7. Process group
    process_group = manifest_obj.get("process_group") or {}
    if not isinstance(process_group, dict):
        process_group = {}
    pg_verified_dead = bool(process_group.get("verified_dead"))
    if not pg_verified_dead:
        errors.append(E_PROCESS_GROUP_NOT_VERIFIED_DEAD)

    # 8. Claim manifest
    claim_manifest_path = expected_dir / "completion.claim.json"
    if not claim_manifest_path.exists():
        errors.append(E_CLAIM_MANIFEST_MISSING)
    else:
        try:
            actual_claim_hash = _sha256_file(claim_manifest_path)
        except OSError:
            actual_claim_hash = None
            errors.append(E_CLAIM_MANIFEST_MISSING)
        expected_claim_hash = verification.get("claim_manifest_hash")
        if (
            actual_claim_hash is not None
            and isinstance(expected_claim_hash, str)
            and actual_claim_hash == expected_claim_hash
        ):
            claim_hash_match = True
        elif not errors or E_CLAIM_MANIFEST_MISSING not in errors:
            # Only flag hash mismatch if we actually had a
            # claim file present (avoid double-reporting missing).
            errors.append(E_CLAIM_MANIFEST_HASH_MISMATCH)

    # 9. Required artifacts
    # TASK-M3 FIX-2: the committed Runner places ``artifacts`` at
    # the top level (``scripts/claude_code_runner.py:1178``); some
    # intermediate drafts placed it under ``verification.artifacts``.
    # We read top-level first, then fall back to the legacy key.
    artifacts_block = manifest_obj.get("artifacts")
    if not isinstance(artifacts_block, list):
        artifacts_block = verification.get("artifacts")
    if not isinstance(artifacts_block, list):
        artifacts_block = []
    artifacts_by_path: Dict[str, Dict[str, Any]] = {}
    for entry in artifacts_block:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            artifacts_by_path[entry["path"]] = entry

    for rel in required_artifacts:
        if not _is_relative_safe_path(rel):
            errors.append(E_ARTIFACT_PATH_ESCAPE)
            continue
        entry = artifacts_by_path.get(rel)
        if entry is None or entry.get("verified") is not True:
            errors.append(E_REQUIRED_ARTIFACT_MISSING)
            continue
        recorded_hash = entry.get("sha256")
        if not isinstance(recorded_hash, str):
            errors.append(E_ARTIFACT_SHA256_MISMATCH)
            continue
        # Resolve the artifact under repo_path and verify on disk.
        # We must check symlink-ness BEFORE resolving the path,
        # because ``Path.resolve()`` follows symlinks and would
        # make the symlink check see the *target* file instead
        # of the link itself. Build an un-resolved absolute path
        # under repo_root for the symlink check, then resolve
        # for the containment + hash check.
        joined = repo_root / rel
        # Containment check using the (still possibly-symlinked)
        # resolved path — if it would escape repo_root we fail.
        try:
            real_abs = joined.resolve(strict=False)
        except (OSError, RuntimeError):
            errors.append(E_REQUIRED_ARTIFACT_MISSING)
            continue
        try:
            real_abs.relative_to(repo_root)
        except ValueError:
            errors.append(E_ARTIFACT_PATH_ESCAPE)
            continue
        # Symlink check on the link path itself (not the target).
        if os.path.islink(str(joined)):
            errors.append(E_ARTIFACT_SYMLINK)
            continue
        try:
            st = os.lstat(joined)
        except OSError:
            errors.append(E_REQUIRED_ARTIFACT_MISSING)
            continue
        if not stat_is_regular_file(st):
            errors.append(E_REQUIRED_ARTIFACT_MISSING)
            continue
        try:
            actual_hash = _sha256_file(joined)
        except OSError:
            errors.append(E_REQUIRED_ARTIFACT_MISSING)
            continue
        artifacts_rechecked += 1
        if actual_hash != recorded_hash:
            errors.append(E_ARTIFACT_SHA256_MISMATCH)

    verified = len(errors) == 0
    return VerificationResult(
        verified=verified,
        verification_errors=errors,
        schema_version=schema_version,
        artifacts_rechecked=artifacts_rechecked,
        claim_hash_match=claim_hash_match,
        process_group_verified_dead=pg_verified_dead,
    )


# --- Helpers ------------------------------------------------------------


def stat_is_regular_file(st: os.stat_result) -> bool:
    """Return True iff ``st`` describes a regular file (no dir, no link)."""
    import stat as _stat
    return _stat.S_ISREG(st.st_mode)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_relative_safe_path(rel: str) -> bool:
    if not rel or not isinstance(rel, str):
        return False
    if rel.startswith("/"):
        return False
    parts = rel.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    return True


__all__ = [
    "SUPPORTED_SCHEMA_VERSIONS",
    "VerificationResult",
    "verify_completion_manifest",
    "stat_is_regular_file",
    # Error codes (re-exported for tests + adapter):
    "E_VERIFIED_MANIFEST_MISSING",
    "E_VERIFIED_MANIFEST_SYMLINK",
    "E_VERIFIED_MANIFEST_NOT_REGULAR",
    "E_VERIFIED_MANIFEST_OUTSIDE_RUN_DIR",
    "E_VERIFIED_MANIFEST_INVALID_JSON",
    "E_UNSUPPORTED_SCHEMA_VERSION",
    "E_TASK_ID_MISMATCH",
    "E_EXECUTOR_TYPE_MISMATCH",
    "E_RUNNER_EXIT_CODE_MISMATCH",
    "E_RUNNER_TERMINAL_REASON_NOT_COMPLETED",
    "E_RUNNER_SUBTYPE_NOT_SUCCESS",
    "E_RUNNER_IS_ERROR_TRUE",
    "E_RUNNER_VERIFICATION_ERRORS_PRESENT",
    "E_RUNNER_SAFETY_VIOLATIONS_PRESENT",
    "E_REQUIRED_ARTIFACT_MISSING",
    "E_ARTIFACT_PATH_ESCAPE",
    "E_ARTIFACT_SYMLINK",
    "E_ARTIFACT_SHA256_MISMATCH",
    "E_CLAIM_MANIFEST_MISSING",
    "E_CLAIM_MANIFEST_HASH_MISMATCH",
    "E_PROCESS_GROUP_NOT_VERIFIED_DEAD",
    "E_STATUS_NOT_COMPLETED",
    "E_VERDICT_NOT_PASS",
]
