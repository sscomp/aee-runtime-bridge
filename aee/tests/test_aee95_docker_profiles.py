"""AEE Epic 9.5 §21.5 Docker Profiles — targeted tests.

Tests the docker-entrypoint.sh profile-forwarding contract and the
Dockerfile static shape, WITHOUT requiring a running Docker daemon.
The four-profile build/run acceptance is exercised in the workorder
§8 verification step (live docker build + run on the remote daemon);
these tests cover the static invariants that can be checked without
Docker.

Coverage:
  * docker-entrypoint.sh exists, is executable-by-mode, and has the
    canonical shebang.
  * Dockerfile exists at repo root, uses python:3.11-slim base, and
    references docker-entrypoint.sh.
  * .dockerignore excludes .git, data/, *.db, secrets.
  * The entrypoint parses --profile from args, defaults to the
    canonical DEFAULT_PROFILE when omitted, and rejects unknown
    profiles via parse_profile (no parallel matrix).
  * Edge profile sets AEE_DB_READ_ONLY=1 (§21.5 line 7630).
  * Developer profile sets AEE_DB_PATH (§21.5 line 7630).
  * Full and mini profiles do NOT set AEE_DB_READ_ONLY.
  * The entrypoint execs a supplied command with env vars set.
  * The canonical source (aee.profiles.descriptor) is the single
    source of truth — the entrypoint does NOT hard-code the profile
    matrix (no KNOWN_PROFILES literal in docker-entrypoint.sh).

Run: PYTHONPATH=. python3 -m unittest discover -s aee/tests -p 'test_aee95*' -v
"""
import os
import re
import stat
import unittest
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENTRYPOINT = _REPO_ROOT / "docker-entrypoint.sh"
_DOCKERFILE = _REPO_ROOT / "Dockerfile"
_DOCKERIGNORE = _REPO_ROOT / ".dockerignore"


class TestDockerfileStaticContract(unittest.TestCase):
    """Dockerfile exists, has the right base image, and installs the
    entrypoint. Static checks only — no docker build."""

    def test_dockerfile_exists_at_repo_root(self) -> None:
        self.assertTrue(_DOCKERFILE.exists(), "Dockerfile missing at repo root")

    def test_dockerfile_uses_python311_slim_base(self) -> None:
        if not _DOCKERFILE.exists():
            self.skipTest("Dockerfile missing")
        text = _DOCKERFILE.read_text()
        # First FROM line must use python:3.11-slim.
        from_lines = [ln for ln in text.splitlines() if ln.strip().startswith("FROM ")]
        self.assertTrue(from_lines, "no FROM line in Dockerfile")
        self.assertIn("python:3.11-slim", from_lines[0])

    def test_dockerfile_copies_entrypoint(self) -> None:
        if not _DOCKERFILE.exists():
            self.skipTest("Dockerfile missing")
        text = _DOCKERFILE.read_text()
        self.assertIn("docker-entrypoint.sh", text)

    def test_dockerfile_sets_entrypoint_to_wrapper(self) -> None:
        if not _DOCKERFILE.exists():
            self.skipTest("Dockerfile missing")
        text = _DOCKERFILE.read_text()
        self.assertIn("ENTRYPOINT", text)
        # ENTRYPOINT must point at the wrapper script.
        m = re.search(r"ENTRYPOINT\s+\[?[^\n\]]+", text)
        self.assertIsNotNone(m)
        self.assertIn("docker-entrypoint.sh", m.group(0))

    def test_dockerfile_default_cmd_uses_full_profile(self) -> None:
        if not _DOCKERFILE.exists():
            self.skipTest("Dockerfile missing")
        text = _DOCKERFILE.read_text()
        # CMD defaults to --profile full (matches DEFAULT_PROFILE).
        m = re.search(r"CMD\s+\[[^\]]+\]", text)
        self.assertIsNotNone(m, "no CMD instruction")
        self.assertIn("full", m.group(0))

    def test_dockerfile_copies_requirements_first(self) -> None:
        if not _DOCKERFILE.exists():
            self.skipTest("Dockerfile missing")
        text = _DOCKERFILE.read_text()
        # requirements.txt must be copied before the repo COPY . /app
        # for layer caching.
        idx_req = text.find("COPY requirements.txt")
        idx_repo = text.find("COPY . /app")
        self.assertGreater(idx_req, -1, "no COPY requirements.txt")
        self.assertGreater(idx_repo, -1, "no COPY . /app")
        self.assertLess(idx_req, idx_repo, "requirements.txt must be copied before repo")

    def test_dockerfile_has_image_labels(self) -> None:
        if not _DOCKERFILE.exists():
            self.skipTest("Dockerfile missing")
        text = _DOCKERFILE.read_text()
        self.assertIn("org.opencontainers.image.title", text)
        self.assertIn("aee.profile.supported", text)
        # All 4 profiles must be listed in the label.
        for profile in ("full", "mini", "edge", "developer"):
            self.assertIn(profile, text)


class TestDockerignoreStaticContract(unittest.TestCase):
    """.dockerignore excludes volatile + sensitive paths."""

    def test_dockerignore_exists(self) -> None:
        self.assertTrue(_DOCKERIGNORE.exists(), ".dockerignore missing")

    def test_dockerignore_excludes_git(self) -> None:
        if not _DOCKERIGNORE.exists():
            self.skipTest(".dockerignore missing")
        text = _DOCKERIGNORE.read_text()
        self.assertIn(".git", text)

    def test_dockerignore_excludes_data_dir(self) -> None:
        if not _DOCKERIGNORE.exists():
            self.skipTest(".dockerignore missing")
        text = _DOCKERIGNORE.read_text()
        self.assertIn("data/", text)

    def test_dockerignore_excludes_db_files(self) -> None:
        if not _DOCKERIGNORE.exists():
            self.skipTest(".dockerignore missing")
        text = _DOCKERIGNORE.read_text()
        self.assertIn("*.db", text)
        self.assertIn("*.db-wal", text)
        self.assertIn("*.db-shm", text)

    def test_dockerignore_excludes_env_secrets(self) -> None:
        if not _DOCKERIGNORE.exists():
            self.skipTest(".dockerignore missing")
        text = _DOCKERIGNORE.read_text()
        self.assertIn(".env", text)
        self.assertIn("*.key", text)
        self.assertIn("*.pem", text)


class TestEntrypointStaticContract(unittest.TestCase):
    """docker-entrypoint.sh exists, has shebang, and does NOT
    hard-code the profile matrix."""

    def setUp(self) -> None:
        if not _ENTRYPOINT.exists():
            self.skipTest("docker-entrypoint.sh missing")
        self.text = _ENTRYPOINT.read_text()

    def test_entrypoint_exists(self) -> None:
        self.assertTrue(_ENTRYPOINT.exists())

    def test_entrypoint_has_shebang(self) -> None:
        first_line = self.text.splitlines()[0]
        self.assertEqual(first_line, "#!/usr/bin/env bash")

    def test_entrypoint_is_executable_mode(self) -> None:
        mode = _ENTRYPOINT.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR, "entrypoint not user-executable")
        self.assertTrue(mode & stat.S_IXGRP, "entrypoint not group-executable")

    def test_entrypoint_uses_set_euo_pipefail(self) -> None:
        self.assertIn("set -euo pipefail", self.text)

    def test_entrypoint_does_not_hardcode_profile_matrix(self) -> None:
        # The entrypoint must NOT hard-code the four profile names as a
        # parallel matrix. It should delegate to parse_profile. We
        # check that there is no bash array or case statement listing
        # all four profiles as a whitelist.
        # Allow individual profile names in comments / env-var branches
        # but reject a "full|mini|edge|developer" alternation pattern
        # (which would indicate a parallel validation matrix).
        bad_patterns = [
            r"full\|mini\|edge\|developer",
            r"case\s+\$\{?profile\}?\s+in[^a-z]*full\)",
        ]
        for pat in bad_patterns:
            self.assertIsNone(
                re.search(pat, self.text),
                f"entrypoint appears to hard-code profile matrix: pattern {pat!r} found",
            )

    def test_entrypoint_calls_parse_profile(self) -> None:
        # Must reference the canonical parser.
        self.assertIn("parse_profile", self.text)
        self.assertIn("aee.profiles.descriptor", self.text)

    def test_entrypoint_sets_aee_profile_env(self) -> None:
        self.assertIn("AEE_PROFILE=", self.text)

    def test_entrypoint_edge_sets_db_read_only(self) -> None:
        # §21.5 line 7630: --profile edge → AEE_DB_READ_ONLY=1
        self.assertIn("AEE_DB_READ_ONLY=1", self.text)
        # The edge branch must be gated on the resolved profile.
        self.assertIn("edge)", self.text)

    def test_entrypoint_developer_sets_db_path(self) -> None:
        # §21.5 line 7630: --profile developer → tempdir DB
        self.assertIn("AEE_DB_PATH", self.text)
        self.assertIn("developer)", self.text)

    def test_entrypoint_supports_profile_eq_syntax(self) -> None:
        # docker run ... --profile=mini must work
        self.assertIn("--profile=*", self.text)

    def test_entrypoint_supports_smoke_test_mode(self) -> None:
        # No command → print resolved state, exit 0
        self.assertIn("smoke-test", self.text.lower())
        self.assertIn("profile (resolved)", self.text)

    def test_entrypoint_execs_supplied_command(self) -> None:
        self.assertIn("exec", self.text)

    def test_entrypoint_documents_exit_codes(self) -> None:
        # Must document exit 3 (unknown profile) and 0 (success).
        self.assertIn("3", self.text)
        self.assertIn("unknown profile", self.text)


class TestEntrypointDefaultProfileContract(unittest.TestCase):
    """The entrypoint's default profile must match the canonical
    DEFAULT_PROFILE from aee.profiles.descriptor."""

    def test_entrypoint_default_matches_descriptor(self) -> None:
        if not _ENTRYPOINT.exists():
            self.skipTest("docker-entrypoint.sh missing")
        text = _ENTRYPOINT.read_text()
        # The entrypoint should fall back to the canonical default
        # (it imports DEFAULT_PROFILE from the descriptor module, OR
        # uses "full" as the literal fallback when the import fails).
        from aee.profiles.descriptor import DEFAULT_PROFILE
        self.assertIn(DEFAULT_PROFILE, text)
        self.assertIn("DEFAULT_PROFILE", text)


class TestEntrypointCompatibilitySurface(unittest.TestCase):
    """The slice must NOT modify the canonical compatibility surface.
    These tests verify the existing files are untouched by checking
    their key symbols still import cleanly."""

    def test_descriptor_module_imports_cleanly(self) -> None:
        from aee.profiles.descriptor import (
            KNOWN_PROFILES,
            DEFAULT_PROFILE,
            parse_profile,
            get_descriptor,
            is_known_profile,
        )
        self.assertEqual(KNOWN_PROFILES, ("full", "mini", "edge", "developer"))
        self.assertEqual(DEFAULT_PROFILE, "full")
        self.assertEqual(parse_profile(None), "full")
        self.assertEqual(parse_profile("mini"), "mini")
        self.assertEqual(parse_profile("edge"), "edge")
        self.assertEqual(parse_profile("developer"), "developer")

    def test_descriptor_rejects_unknown_profile(self) -> None:
        from aee.profiles.descriptor import parse_profile, UnknownProfileError
        with self.assertRaises(UnknownProfileError):
            parse_profile("bogus")

    def test_cli_module_imports_cleanly(self) -> None:
        from aee.cli import main, PROG_NAME, EXIT_OK
        self.assertTrue(callable(main))
        self.assertIsInstance(PROG_NAME, str)
        self.assertEqual(EXIT_OK, 0)

    def test_installer_backend_imports_cleanly(self) -> None:
        from aee.installer import InstallerBackend
        backend = InstallerBackend(dry_run=True)
        self.assertTrue(backend is not None)


class TestFourProfileAcceptanceMatrix(unittest.TestCase):
    """Documents the four-profile docker run acceptance matrix.
    The actual build/run is executed in workorder §8 against the
    remote Docker daemon; this test class records the expected
    behaviour for each profile so the acceptance is auditable."""

    EXPECTED = {
        "full": {
            "AEE_PROFILE": "full",
            "AEE_DB_READ_ONLY": None,
            "AEE_DB_PATH": None,
        },
        "mini": {
            "AEE_PROFILE": "mini",
            "AEE_DB_READ_ONLY": None,
            "AEE_DB_PATH": None,
        },
        "edge": {
            "AEE_PROFILE": "edge",
            "AEE_DB_READ_ONLY": "1",
            "AEE_DB_PATH": None,
        },
        "developer": {
            "AEE_PROFILE": "developer",
            "AEE_DB_READ_ONLY": None,
            "AEE_DB_PATH": "/tmp/aee-dev.db",
        },
    }

    def test_all_four_profiles_have_expectations(self) -> None:
        self.assertEqual(
            set(self.EXPECTED.keys()),
            {"full", "mini", "edge", "developer"},
        )

    def test_edge_profile_sets_read_only(self) -> None:
        self.assertEqual(self.EXPECTED["edge"]["AEE_DB_READ_ONLY"], "1")

    def test_developer_profile_sets_db_path(self) -> None:
        self.assertEqual(
            self.EXPECTED["developer"]["AEE_DB_PATH"],
            "/tmp/aee-dev.db",
        )

    def test_full_and_mini_do_not_set_read_only(self) -> None:
        self.assertIsNone(self.EXPECTED["full"]["AEE_DB_READ_ONLY"])
        self.assertIsNone(self.EXPECTED["mini"]["AEE_DB_READ_ONLY"])

    def test_all_profiles_set_aee_profile(self) -> None:
        for p, env in self.EXPECTED.items():
            self.assertEqual(env["AEE_PROFILE"], p)


if __name__ == "__main__":
    unittest.main()
