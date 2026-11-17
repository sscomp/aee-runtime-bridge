"""Focused tests for the opt-in Codex ``.git`` writable-root grant.

Openai/codex#5034: the native workspace-write sandbox denies ref locks
inside ``.git`` with EPERM, breaking mid-job git branch/checkout. The
adapter may scope a write grant to the job's resolved ``.git`` directory
only when the operator sets AEE_CODEX_GIT_WRITABLE=1. Default posture
must stay unchanged (no grant, no -c config), and the interpolated path
must be safe against config-string injection.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aee.mcp_runtime.job_adapters import adapter_specs


BASE_ARGV = ["codex", "exec", "--json", "--ephemeral", "--skip-git-repo-check",
             "--sandbox", "workspace-write", "--cd", None, "-"]


class CodexGitWritableTests(unittest.TestCase):
    def setUp(self):
        self.specs = adapter_specs()
        self.codex = self.specs["codex"]
        self.workdir = tempfile.mkdtemp(prefix="aee-codex-git-test-")
        Path(self.workdir, ".git").mkdir()

    def test_default_unset_no_git_grant(self):
        argv = self.codex.build_argv(self.workdir, "codex", "write")
        self.assertNotIn("-c", argv)
        self.assertNotIn("writable_roots", " ".join(argv))
        self.assertEqual(argv[:-1], [p if p is not None else self.workdir
                                     for p in BASE_ARGV[:-1]])

    def test_optin_scopes_grant_to_resolved_git_dir(self):
        expected = str((Path(self.workdir) / ".git").resolve())
        with patch.dict(os.environ, {"AEE_CODEX_GIT_WRITABLE": "1"}):
            argv = self.codex.build_argv(self.workdir, "codex", "write")
        self.assertIn("-c", argv)
        self.assertIn(f'sandbox_workspace_write.writable_roots=["{expected}"]',
                      argv[argv.index("-c") + 1])
        self.assertEqual(argv[-1], "-")
        base = list(BASE_ARGV[:-1])
        base[self.workdir_index()] = self.workdir
        self.assertEqual(argv[:len(base)], base)
        self.assertEqual(len(argv), len(base) + 3)

    def test_optin_value_other_than_1_is_ignored(self):
        for value in ("0", "true", "yes", "1 ", " 1", ""):
            with patch.dict(os.environ, {"AEE_CODEX_GIT_WRITABLE": value}):
                argv = self.codex.build_argv(self.workdir, "codex", "write")
            self.assertNotIn("-c", argv, value)

    def test_optin_without_git_dir_adds_nothing(self):
        import shutil
        shutil.rmtree(Path(self.workdir) / ".git")
        with patch.dict(os.environ, {"AEE_CODEX_GIT_WRITABLE": "1"}):
            argv = self.codex.build_argv(self.workdir, "codex", "write")
        self.assertNotIn("-c", argv)

    def test_optin_read_only_mode_unchanged(self):
        with patch.dict(os.environ, {"AEE_CODEX_GIT_WRITABLE": "1"}):
            argv = self.codex.build_argv(self.workdir, "codex", "read_only")
        self.assertIn("--sandbox", argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertNotIn("-c", argv)

    def test_optin_path_with_quote_is_toml_escaped(self):
        # Injection fixture: a workspace whose .git path contains a double
        # quote must not terminate the TOML string or add extra roots.
        import shutil
        tricky = self.workdir + 'we"ird'
        os.mkdir(tricky)
        Path(tricky, ".git").mkdir()
        with patch.dict(os.environ, {"AEE_CODEX_GIT_WRITABLE": "1"}):
            argv = self.codex.build_argv(tricky, "codex", "write")
        config = argv[argv.index("-c") + 1]
        expected = str((Path(tricky) / ".git").resolve()).replace('"', '\\"')
        self.assertIn(f'sandbox_workspace_write.writable_roots=["{expected}"]',
                      config)
        # Exactly one path entry, no way a second root was injected.
        self.assertEqual(config.count("writable_roots"), 1)

    def test_optin_grant_is_scoped_not_global(self):
        # The grant must be a per-invocation -c override on THIS argv only;
        # it must not leak into the returned default argv for other jobs
        # and must not widen beyond the resolved .git path.
        with patch.dict(os.environ, {"AEE_CODEX_GIT_WRITABLE": "1"}):
            argv = self.codex.build_argv(self.workdir, "codex", "write")
        config = argv[argv.index("-c") + 1]
        git_dir = str((Path(self.workdir) / ".git").resolve())
        self.assertTrue(config.startswith("sandbox_workspace_write.writable_roots=["))
        self.assertIn(git_dir, config)
        # Only the .git dir in writable_roots — no "*" or parent dirs.
        self.assertNotIn('"*"', config)

    def workdir_index(self):
        return BASE_ARGV.index("--cd") + 1


if __name__ == "__main__":
    unittest.main()