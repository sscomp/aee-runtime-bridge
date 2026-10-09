import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aee.mcp_runtime.executor import execute_codex, final_agent_message
from aee.mcp_runtime.process import Limits, run_bounded
from aee.mcp_runtime.profiles import codex_arguments, execution_metadata, resolve_codex, select_profile
from aee.mcp_runtime.sandbox import read_manifest, runner_env, sandbox_command, snapshot_workspace
from aee.mcp_runtime.store import JobError

FIXTURES = Path(__file__).parent / "fixtures"


class IsolatedExecutor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="aee-native-fixture-")
        cls.binary = Path(cls.build.name) / "codex-fixture"
        subprocess.run(["/usr/bin/gcc", "-Wall", "-Wextra", "-O2",
                        str(FIXTURES / "codex_probe.c"), "-o", str(cls.binary)], check=True)
        cls.identity = resolve_codex(str(cls.binary))  # real bwrap/version probe
        cls.profile = select_profile()

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aee-p2b-executor-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "source"
        self.workspace.mkdir()
        self.safe = self.workspace / "safe.txt"
        self.safe.write_text("reviewed source")
        self.secret = self.root / ".env.aee-credential-fixture"
        self.secret.write_text("P2B_SYNTHETIC_CREDENTIAL_VALUE")
        self.secret.chmod(0o600)
        (self.workspace / ".env.aee-mcp").write_text("P2B_SYNTHETIC_IN_TREE_SECRET")
        self.manifest = self.root / "reviewed-manifest.json"
        self.manifest.write_text(json.dumps({"safe.txt": hashlib.sha256(self.safe.read_bytes()).hexdigest()}))

    def execute(self, task, **kwargs):
        return execute_codex(self.identity, self.profile, task, str(self.workspace), self.manifest,
                             limits=kwargs.get("limits", Limits(timeout=5)))

    def assert_code(self, code, action):
        with self.assertRaises(JobError) as error:
            action()
        self.assertEqual(error.exception.code, code)

    def test_outside_and_unreviewed_credentials_unreadable(self):
        for path in [str(self.secret), "/workspace/.env.aee-mcp", "/outside-host/.codex/auth.json",
                     "/proc/1/root" + str(self.secret)]:
            result = self.execute("READ:" + path)
            self.assertEqual(result.summary, "DENIED")
            self.assertNotIn("P2B_SYNTHETIC", result.log_excerpt)
        self.assertEqual(self.execute("READ:/workspace/safe.txt").summary, "reviewed source")
        self.assertEqual(self.secret.read_text(), "P2B_SYNTHETIC_CREDENTIAL_VALUE")

    def test_inherited_credential_fd_is_closed(self):
        with self.secret.open("rb") as stream:
            os.set_inheritable(stream.fileno(), True)
            result = self.execute("READ:/proc/self/fd/" + str(stream.fileno()))
        self.assertEqual(result.summary, "DENIED")

    def test_namespace_workspace_writes_denied(self):
        self.assertEqual(self.execute("WRITE_WORKSPACE").summary, "DENIED")
        self.assertEqual(self.execute("WRITE_ROOT").summary, "DENIED")
        self.assertEqual(self.safe.read_text(), "reviewed source")

    def test_environment_not_inherited_and_home_xdg_private(self):
        with patch.dict(os.environ, {"MCP_BRIDGE_API_KEY": "P2B_SYNTHETIC_AUTH",
                                    "CONTROL_PLANE_API_KEY": "P2B_SYNTHETIC_AUTH",
                                    "HOME": str(self.root), "SSH_AUTH_SOCK": str(self.secret)}):
            self.assertEqual(self.execute("ENV_CHECK").summary, "SAFE")
        self.assertEqual(set(runner_env()), {"HOME", "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                                           "XDG_CACHE_HOME", "XDG_STATE_HOME", "TMPDIR", "PATH", "LANG"})

    def test_cli_looking_tasks_are_literal_stdin(self):
        for task in ["--help", "--model evil", "--sandbox danger-full-access", "-C /etc", "--config x=y"]:
            self.assertEqual(self.execute(task).summary, task)
            self.assertNotIn(task, codex_arguments(self.profile))
        self.assertEqual(codex_arguments(self.profile)[-1], "-")

    def test_model_reasoning_and_approval_are_validated_configuration(self):
        from dataclasses import replace
        argv = codex_arguments(select_profile("codex-readonly-high", "gpt-6.1-sol", "high"))
        self.assertEqual(argv[argv.index("--model") + 1], "gpt-6.1-sol")
        self.assertIn('model_reasoning_effort="high"', argv)
        self.assertIn('approval_policy="never"', argv)
        for selection in [{"name": "--help"}, {"name": ""}, {"model": "other-model"},
                          {"reasoning_effort": "--config"}, {"reasoning_effort": "medium"}]:
            self.assert_code("INVALID_EXECUTION_PROFILE", lambda: select_profile(**selection))
        for fields in [{"sandbox": "danger-full-access"}, {"approval_policy": "on-request"},
                       {"model": "other-model"}, {"agent": "hermes"}]:
            self.assert_code("INVALID_EXECUTION_PROFILE", lambda: codex_arguments(replace(self.profile, **fields)))

    def test_timeout_even_with_closed_output_pipes(self):
        for task in ["P2B_TIMEOUT", "P2B_CLOSED_PIPES_TIMEOUT"]:
            self.assert_code("EXECUTION_TIMEOUT", lambda: self.execute(task, limits=Limits(timeout=0.2)))

    def test_stdout_and_stderr_overflow_terminate(self):
        for task in ["P2B_STDOUT_OVERFLOW", "P2B_STDERR_OVERFLOW"]:
            self.assert_code("OUTPUT_LIMIT_EXCEEDED", lambda: self.execute(task, limits=Limits(timeout=5, stdout_bytes=1024, stderr_bytes=1024)))

    def test_file_resource_limit_is_enforced(self):
        self.assert_code("OUTPUT_LIMIT_EXCEEDED", lambda: self.execute("P2B_FILE_LIMIT"))

    def test_network_namespace_has_no_host_or_external_network(self):
        self.assertEqual(self.execute("NETWORK_CHECK").summary, "DENIED")

    def test_kernel_resource_limits_and_state_quota_effective(self):
        self.assertEqual(self.execute("RESOURCE_CHECK").summary, "BOUNDED")
        self.assertEqual(self.execute("P2B_STATE_QUOTA").summary, "BOUNDED")
        self.assert_code("RESOURCE_LIMIT_EXCEEDED", lambda: self.execute("P2B_CPU_LIMIT", limits=Limits(timeout=4, cpu_seconds=1)))

    def test_summary_truncation_and_log_excerpt_bounded(self):
        result = self.execute("P2B_TRUNCATE")
        self.assertEqual(len(result.summary), 4000)
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.log_excerpt), 2000)
        self.assert_code("OUTPUT_LIMIT_EXCEEDED", lambda: self.execute("P2B_TRUNCATE", limits=Limits(timeout=5, result_bytes=1024)))

    def test_nonzero_failed_or_malformed_events_are_structured(self):
        for task in ["P2B_NONZERO", "P2B_INVALID_EVENTS", "P2B_FAILED_EVENT"]:
            self.assert_code("EXECUTION_FAILED", lambda: self.execute(task))
        self.assert_code("EXECUTION_FAILED", lambda: final_agent_message(b'{"type":"item.completed","item":{"type":"agent_message","text":"incomplete"}}\n', Limits()))

    def test_missing_sandbox_manifest_or_hash_change_fails_closed(self):
        with patch("aee.mcp_runtime.sandbox.shutil.which", return_value=None):
            self.assert_code("ISOLATION_UNAVAILABLE", lambda: self.execute("fixture"))
        self.assert_code("ISOLATION_UNAVAILABLE", lambda: read_manifest(None))
        self.safe.write_text("unreviewed mutation")
        self.assert_code("ISOLATION_UNAVAILABLE", lambda: self.execute("fixture"))

    def test_manifest_traversal_credential_and_symlink_rejections(self):
        for name in [".", "../escape", "/absolute", ".env.aee-mcp", ".git/config", ".ssh/id_rsa", "private/key.txt"]:
            self.manifest.write_text(json.dumps({name: "a" * 64}))
            self.assert_code("ISOLATION_UNAVAILABLE", lambda: read_manifest(self.manifest))
        for directory in [False, True]:
            link = self.workspace / "link"
            link.symlink_to(self.root if directory else self.secret, target_is_directory=directory)
            dest = self.root / ("destination-dir" if directory else "destination-file")
            dest.mkdir()
            name = "link/.env.aee-credential-fixture" if directory else "link"
            self.assert_code("ISOLATION_UNAVAILABLE", lambda: snapshot_workspace(self.workspace, {name: "a" * 64}, dest))
            link.unlink()

    def test_manifest_hardlink_and_oversized_file_rejected(self):
        link = self.workspace / "hardlink.txt"
        os.link(self.secret, link)
        dest = self.root / "destination"
        dest.mkdir()
        self.assert_code("ISOLATION_UNAVAILABLE", lambda: snapshot_workspace(self.workspace, {"hardlink.txt": hashlib.sha256(link.read_bytes()).hexdigest()}, dest))
        self.safe.write_bytes(b"x" * (4 * 1024**2 + 1))
        self.assert_code("OUTPUT_LIMIT_EXCEEDED", lambda: snapshot_workspace(self.workspace, {"safe.txt": "a" * 64}, dest))

    def test_operator_manifest_uses_committed_hashes_and_excludes_credentials(self):
        from aee.mcp_runtime.manifest import make_manifest
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "add", "--", "safe.txt", ".env.aee-mcp"], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "-c", "user.name=fixture", "-c",
                        "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], check=True)
        entries = make_manifest(self.workspace)
        self.assertEqual(set(entries), {"safe.txt"})
        self.safe.write_text("uncommitted change")
        self.assertEqual(entries, make_manifest(self.workspace))
        dest = self.root / "snapshot"
        dest.mkdir()
        self.assert_code("ISOLATION_UNAVAILABLE", lambda: snapshot_workspace(self.workspace, entries, dest))

    def test_wrappers_not_launched_and_identity_is_not_fabricated(self):
        wrapper = self.root / "wrapper"
        wrapper.write_text("#!/bin/sh\nexit 7\n")
        wrapper.chmod(0o700)
        self.assert_code("AGENT_UNAVAILABLE", lambda: resolve_codex(str(wrapper)))
        metadata = execution_metadata(self.profile, self.identity)
        self.assertEqual(metadata["observed"]["agent_version_probe"], "codex-cli 0.0.0-fixture")
        self.assertIsNone(metadata["observed"]["model"])
        self.assertIsNone(metadata["observed"]["reasoning_effort"])
        self.assertEqual(metadata["configured"]["executable"], str(self.binary.resolve()))

    def test_limits_cannot_be_disabled_or_expanded(self):
        for values in [{"timeout": 0}, {"stdout_bytes": 2**30}, {"memory_bytes": 2**40}, {"file_bytes": 2**30}]:
            self.assert_code("INVALID_EXECUTION_PROFILE", lambda: Limits(**values))

    def test_stdin_nonreader_cannot_block_timeout(self):
        self.assert_code("EXECUTION_TIMEOUT", lambda: run_bounded([str(self.binary), "--stall-stdin"],
                         env={}, stdin=b"x" * 32000, limits=Limits(timeout=0.2)))

    def test_process_group_children_terminated_on_timeout(self):
        seen = []
        threads = []

        def observe(pid):
            def find_child():
                for _ in range(30):
                    path = Path(f"/proc/{pid}/task/{pid}/children")
                    if path.exists():
                        children = path.read_text().split()
                        if children:
                            seen.extend(map(int, children))
                            return
                    time.sleep(0.005)
            thread = threading.Thread(target=find_child)
            threads.append(thread)
            thread.start()

        self.assert_code("EXECUTION_TIMEOUT", lambda: run_bounded([str(self.binary), "--child"],
                         env={}, limits=Limits(timeout=0.3), on_start=observe))
        for thread in threads:
            thread.join()
        self.assertTrue(seen)
        for pid in seen:
            path = Path(f"/proc/{pid}/stat")
            if path.exists():
                self.assertEqual(path.read_text().rsplit(")", 1)[1].split()[0], "Z")


if __name__ == "__main__":
    unittest.main()
