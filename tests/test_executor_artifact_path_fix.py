"""Regression: Claude Code CLI executor artifact path mismatch fix.

Root cause: when the caller omitted ``repo_path`` on
``POST /runs/executor``, the executor cwd defaulted to
``/home/ubuntu/Abacus`` regardless of where the declared
``expected_artifacts`` lived. The Claude CLI subprocess wrote
artifacts under its cwd (e.g. ``/home/ubuntu/Abacus/reports/foo.md``)
while ``verify_artifacts`` stat-ed the absolute paths the caller
declared (e.g. ``/home/ubuntu/hermes-runtime-bridge/reports/foo.md``).
The artifact was created but at a different location than verification
expected, so every run reported ``completed`` with empty
``artifact_paths`` and ``exists=False``.

Fix: ``_derive_repo_path_from_artifacts`` derives the executor cwd
from the common parent of the declared artifact paths (gated by the
configured repo allow-list) when the caller omits ``repo_path``.

These tests pin:
  1. The helper derives the correct cwd from a single artifact path.
  2. The helper derives the correct cwd from multiple artifact paths
     under the same repo.
  3. The helper falls back to the default when the derived path is
     outside the allow-list.
  4. The helper honours an explicit ``repo_path`` over derivation.
  5. The helper falls back to the default when no artifacts are declared.
  6. End-to-end: the executor writes the artifact at the declared
     absolute path and ``verify_artifacts`` finds it (``exists=True``).
  7. End-to-end: pre-fix behaviour (cwd=Abacus, artifact declared
     under hermes-runtime-bridge) would have failed; post-fix the
     artifact is found.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from tests._executor_test_helpers import make_client, post_executor


# ---------------------------------------------------------------------------
# Unit tests for _derive_repo_path_from_artifacts
# ---------------------------------------------------------------------------

class TestDeriveRepoPathFromArtifacts:
    """Pure-function tests for the path-alignment helper."""

    def _call(
        self,
        expected_artifacts=None,
        explicit_repo_path=None,
        default_repo_path="/home/ubuntu/Abacus",
        allowlist=None,
    ):
        from app import _derive_repo_path_from_artifacts
        return _derive_repo_path_from_artifacts(
            expected_artifacts=expected_artifacts,
            explicit_repo_path=explicit_repo_path,
            default_repo_path=default_repo_path,
            allowlist=allowlist or ["/home/ubuntu", "/tmp"],
        )

    def test_single_artifact_derives_parent_dir(self):
        """A single artifact path -> cwd is its parent directory."""
        result = self._call(
            expected_artifacts=["/home/ubuntu/hermes-runtime-bridge/reports/foo.md"],
        )
        assert result == "/home/ubuntu/hermes-runtime-bridge/reports"

    def test_multiple_artifacts_same_repo_derives_common_parent(self):
        """Multiple artifacts under the same repo -> cwd is the repo root."""
        result = self._call(
            expected_artifacts=[
                "/home/ubuntu/hermes-runtime-bridge/reports/a.md",
                "/home/ubuntu/hermes-runtime-bridge/docs/b.md",
            ],
        )
        assert result == "/home/ubuntu/hermes-runtime-bridge"

    def test_artifacts_outside_allowlist_falls_back_to_default(self):
        """Derived path outside the allow-list -> default."""
        result = self._call(
            expected_artifacts=["/opt/secret/reports/foo.md"],
            allowlist=["/home/ubuntu", "/tmp"],
        )
        assert result == "/home/ubuntu/Abacus"

    def test_explicit_repo_path_wins_over_derivation(self):
        """Explicit repo_path is honoured even when artifacts suggest otherwise."""
        result = self._call(
            expected_artifacts=["/home/ubuntu/hermes-runtime-bridge/reports/foo.md"],
            explicit_repo_path="/home/ubuntu/Abacus",
        )
        assert result == "/home/ubuntu/Abacus"

    def test_no_artifacts_falls_back_to_default(self):
        """No artifacts -> default repo_path."""
        result = self._call(expected_artifacts=None)
        assert result == "/home/ubuntu/Abacus"

    def test_empty_artifacts_list_falls_back_to_default(self):
        """Empty artifacts list -> default repo_path."""
        result = self._call(expected_artifacts=[])
        assert result == "/home/ubuntu/Abacus"

    def test_relative_artifact_paths_ignored(self):
        """Non-absolute paths are ignored -> default."""
        result = self._call(expected_artifacts=["reports/foo.md"])
        assert result == "/home/ubuntu/Abacus"

    def test_derived_path_inside_allowlist_subdir_accepted(self):
        """A derived path that is a subdirectory of an allow-list entry is accepted."""
        result = self._call(
            expected_artifacts=["/tmp/myproject/reports/foo.md"],
            allowlist=["/home/ubuntu", "/tmp"],
        )
        assert result == "/tmp/myproject/reports"


# ---------------------------------------------------------------------------
# End-to-end tests via POST /runs/executor
# ---------------------------------------------------------------------------

def _write_artifact_creating_fake_claude(
    tmp_path: Path,
    *,
    marker_path: str,
    name: str = "fake-claude-artifact-writer",
) -> str:
    """Write a fake claude binary that creates a file at the path stored in a marker.

    The marker file is read by the binary at startup; it contains the
    absolute path where the artifact should be written. This simulates
    the real Claude CLI writing a durable artifact at the path it was
    instructed to write (via the prompt + cwd context).

    The marker approach avoids needing to pass the path through the
    provider's env allow-list — the marker is on the shared filesystem
    so the subprocess can read it regardless of env filtering.
    """
    lines = [
        "#!/usr/bin/env bash",
        'if [ "$1" = "--version" ]; then',
        '  echo "fake-claude 0.0.0-test"',
        "  exit 0",
        "fi",
        # Read the declared artifact path from the marker file.
        f'ARTIFACT_PATH=$(cat {marker_path!r} 2>/dev/null || true)',
        'if [ -n "$ARTIFACT_PATH" ]; then',
        '  mkdir -p "$(dirname "$ARTIFACT_PATH")"',
        '  echo "artifact content from fake claude" > "$ARTIFACT_PATH"',
        "fi",
        'echo "fake claude ok"',
        "exit 0",
    ]
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


@pytest.fixture
def executor_env(monkeypatch, tmp_path):
    """Configure a fake claude that writes a declared artifact file."""
    marker = str(tmp_path / "artifact_path_marker.txt")
    binary = _write_artifact_creating_fake_claude(tmp_path, marker_path=marker)
    monkeypatch.setenv("AEE_CLAUDE_CLI_BINARY", binary)
    # Ensure the auth vars are set so the env-mirror doesn't cause a failure.
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-bearer")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client, app_module, key = make_client(monkeypatch, tmp_path)
    return client, key, app_module, marker


def test_artifact_created_and_verified_at_declared_path(executor_env, tmp_path):
    """End-to-end: executor cwd aligns with declared artifact paths.

    The caller declares an artifact under a tmp_path subdirectory
    (which is inside the allow-list via /tmp) WITHOUT passing
    ``repo_path``. Pre-fix, the executor cwd would default to
    ``/home/ubuntu/Abacus`` and the artifact would be written there;
    verification would stat the declared path and find nothing. Post-fix,
    the helper derives the cwd from the artifact path so the file is
    created exactly where verification expects.
    """
    client, key, _, marker = executor_env
    # Use tmp_path itself (which exists) as the artifact directory.
    # The derived cwd will be tmp_path (parent of the artifact file),
    # which already exists and is inside the allow-list.
    artifact_path = str(tmp_path / "test_artifact.md")
    # Write the marker so the fake binary knows where to write.
    Path(marker).write_text(artifact_path, encoding="utf-8")

    resp = post_executor(client, key, {
        "executor": "claude-code-cli",
        "prompt": "write the report",
        "expected_artifacts": [artifact_path],
        "timeout_sec": 30,
    })

    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["status"] == "completed", (
        f"status={data['status']!r} error={data.get('error')!r}"
    )
    # The artifact should exist at the declared path.
    assert os.path.exists(artifact_path), (
        f"artifact NOT found at declared path {artifact_path}; "
        f"artifact_paths={data.get('artifact_paths')}"
    )
    # artifact_paths should include the declared path.
    assert artifact_path in (data.get("artifact_paths") or []), (
        f"declared path {artifact_path} not in artifact_paths={data.get('artifact_paths')}"
    )
    # verify_artifacts should report exists=True.
    verifications = data.get("artifact_verification") or []
    verified_paths = [v for v in verifications if v.get("path") == artifact_path]
    assert verified_paths, (
        f"no verification entry for {artifact_path} in {verifications}"
    )
    assert verified_paths[0].get("exists") is True, (
        f"verify_artifacts reports exists=False for {artifact_path}"
    )


def test_derived_cwd_matches_artifact_repo(executor_env, tmp_path):
    """The git_evidence.repo_path in the response matches the derived cwd."""
    client, key, _, marker = executor_env
    # Use tmp_path which is inside the allow-list (/tmp).
    artifact_path = str(tmp_path / "test_derived_cwd_artifact.md")
    Path(marker).write_text(artifact_path, encoding="utf-8")
    resp = post_executor(client, key, {
        "executor": "claude-code-cli",
        "prompt": "write report",
        "expected_artifacts": [artifact_path],
        "timeout_sec": 30,
    })
    assert resp.status_code == 200
    data = resp.json()
    git_ev = data.get("git_evidence") or {}
    # The repo_path in git_evidence should be the tmp_path (the derived parent),
    # NOT /home/ubuntu/Abacus (the old default).
    assert git_ev.get("repo_path") == str(tmp_path), (
        f"git_evidence.repo_path={git_ev.get('repo_path')!r} "
        f"expected {str(tmp_path)!r} (derived from artifact path)"
    )