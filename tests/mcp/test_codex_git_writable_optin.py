"""Verify the per-invocation Codex Git write opt-in and its path boundary."""

import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from aee.mcp_runtime.job_adapters import adapter_specs


class CodexGitWritableTests(unittest.TestCase):
    def setUp(self):
        self.codex = adapter_specs()["codex"]
        temp = tempfile.TemporaryDirectory(prefix="aee-codex-git-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workdir = self.root / "workspace"
        self.git_dir = self.workdir / ".git"
        self.git_dir.mkdir(parents=True)
        self.write_argv = [
            "codex", "exec", "--json", "--ephemeral", "--skip-git-repo-check",
            "-c", "project_doc_max_bytes=65536",
            "--sandbox", "workspace-write", "--cd", str(self.workdir), "-",
        ]

    def test_default_unset_no_git_grant(self):
        with patch.dict(os.environ):
            os.environ.pop("AEE_CODEX_GIT_WRITABLE", None)
            argv = self.codex.build_argv(str(self.workdir), "codex", "write")
        self.assertEqual(argv, self.write_argv)

    def test_optin_scopes_grant_to_resolved_git_dir(self):
        with patch.dict(os.environ, {"AEE_CODEX_GIT_WRITABLE": "1"}):
            argv = self.codex.build_argv(str(self.workdir), "codex", "write")
        self.assertEqual(argv, [
            *self.write_argv[:-1], "-c",
            f'sandbox_workspace_write.writable_roots=["{self.git_dir.resolve()}"]', "-",
        ])

    def test_optin_value_other_than_1_is_ignored(self):
        for value in ("0", "true", "yes", "1 ", " 1", ""):
            with self.subTest(value=value), patch.dict(os.environ, {"AEE_CODEX_GIT_WRITABLE": value}):
                argv = self.codex.build_argv(str(self.workdir), "codex", "write")
                self.assertEqual(argv, self.write_argv)

    @patch.dict(os.environ, {"AEE_CODEX_GIT_WRITABLE": "1"})
    def test_optin_without_git_dir_adds_nothing(self):
        self.git_dir.rmdir()
        self.assertEqual(self.codex.build_argv(str(self.workdir), "codex", "write"), self.write_argv)

    @patch.dict(os.environ, {"AEE_CODEX_GIT_WRITABLE": "1"})
    def test_optin_requires_git_directory(self):
        self.git_dir.rmdir()
        self.git_dir.write_text("gitdir: linked-worktree-metadata\n")
        self.assertEqual(self.codex.build_argv(str(self.workdir), "codex", "write"), self.write_argv)

    @patch.dict(os.environ, {"AEE_CODEX_GIT_WRITABLE": "1"})
    def test_optin_requires_git_directory_inside_workspace(self):
        other_dir = self.root / "outside"
        other_dir.mkdir()
        self.git_dir.rmdir()
        for target in (other_dir, self.workdir):
            with self.subTest(target=target.name):
                self.git_dir.symlink_to(target, target_is_directory=True)
                try:
                    argv = self.codex.build_argv(str(self.workdir), "codex", "write")
                    self.assertEqual(argv, self.write_argv)
                finally:
                    self.git_dir.unlink()

    def test_optin_applies_only_to_write_mode(self):
        for mode in ("read_only", "autonomous"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"AEE_CODEX_GIT_WRITABLE": "1"}):
                argv = self.codex.build_argv(str(self.workdir), "codex", mode)
                self.assertEqual(argv.count("-c"), 1)
                self.assertEqual(argv[argv.index("-c") + 1], "project_doc_max_bytes=65536")

    @patch.dict(os.environ, {"AEE_CODEX_GIT_WRITABLE": "1"})
    def test_optin_path_is_escaped_as_one_toml_string(self):
        tricky = self.root / 'w"ork\\tree\t測試'
        git_dir = tricky / ".git"
        git_dir.mkdir(parents=True)
        argv = self.codex.build_argv(str(tricky), "codex", "write")
        self.assertEqual(argv[-3], "-c")
        self.assertEqual(tomllib.loads(argv[-2]), {
            "sandbox_workspace_write": {"writable_roots": [str(git_dir.resolve())]},
        })

    def test_optin_grant_is_per_invocation(self):
        with patch.dict(os.environ, {"AEE_CODEX_GIT_WRITABLE": "1"}):
            opted_in = self.codex.build_argv(str(self.workdir), "codex", "write")
        with patch.dict(os.environ, {"AEE_CODEX_GIT_WRITABLE": "0"}):
            default = self.codex.build_argv(str(self.workdir), "codex", "write")
        self.assertEqual(opted_in.count("-c"), 2)
        self.assertEqual(default, self.write_argv)


if __name__ == "__main__":
    unittest.main()
