"""Static activation-confine tests for the transport diagnostic env injection.

The activation-only commit adds exactly one bwrap --setenv pair for
AEE_TRANSPORT_DIAGNOSTICS inside the production-only telemetry block
(sandbox_revision is not None). These tests assert the injection lands only on
that path, is singular, leaves unrelated command shapes untouched, and that the
base runner environment never carries the flag (the collector gate must be
reachable only through the sandbox injection).
"""
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aee.mcp_runtime.sandbox import sandbox_command, runner_env


def listening_socket():
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    path = tempfile.mktemp(suffix='.sock')
    listener.bind(path)
    listener.listen(1)
    return listener, path


def build(revision, sock):
    with tempfile.TemporaryDirectory() as root:
        return sandbox_command(Path('/usr/bin/env'), Path(root) / 'workspace',
                               ['--version'], broker_socket=sock,
                               sandbox_revision=revision)


def injection_count(command, name):
    return sum(1 for i, part in enumerate(command)
               if part == '--setenv' and i + 2 < len(command)
               and command[i + 1] == name)


class ActivationConfinedToTelemetryPath(unittest.TestCase):
    def setUp(self):
        self._listener, self.socket_path = listening_socket()
        self.addCleanup(self._listener.close)
        self.verification = mock.patch('aee.mcp_runtime.sandbox_policy.verify_sandbox',
                                       mock.Mock(return_value=None))
        self.verification.start()
        self.addCleanup(self.verification.stop)

    def test_single_injection_on_telemetry_path_adjacent_to_receipt_flag(self):
        command = build(object(), self.socket_path)
        self.assertEqual(injection_count(command, 'AEE_TRANSPORT_DIAGNOSTICS'), 1)
        self.assertEqual(injection_count(command, 'AEE_NATIVE_TOOL_RECEIPT'), 1)
        markers = [i for i, part in enumerate(command)
                   if part == 'AEE_TRANSPORT_DIAGNOSTICS']
        self.assertEqual(len(markers), 1)
        self.assertEqual(command[markers[0] - 4:markers[0] + 2],
                         ['--setenv', 'AEE_NATIVE_TOOL_RECEIPT', '1',
                          '--setenv', 'AEE_TRANSPORT_DIAGNOSTICS', '1'])
        # The injection is colocated with the telemetry ro-bind block.
        self.assertEqual(command.count('/runner/native_telemetry.py'), 1)
        self.assertEqual(command[markers[0] - 7], '--ro-bind')
        self.assertEqual(command[markers[0] - 5], '/runner/native_telemetry.py')

    def test_non_telemetry_broker_path_has_no_injection(self):
        command = build(None, self.socket_path)
        self.assertEqual(command.count('AEE_TRANSPORT_DIAGNOSTICS'), 0)
        self.assertEqual(command.count('AEE_NATIVE_TOOL_RECEIPT'), 0)
        self.assertEqual(command.count('/runner/native_telemetry.py'), 0)
        self.assertEqual(command.count('/runner/relay.py'), 2)  # bind + argv target

    def test_no_broker_socket_leaves_environment_injections_absent(self):
        with tempfile.TemporaryDirectory() as root:
            command = sandbox_command(Path('/usr/bin/env'), Path(root) / 'workspace',
                                      ['--version'], sandbox_revision=object())
        self.assertEqual(command.count('AEE_TRANSPORT_DIAGNOSTICS'), 0)
        self.assertEqual(command.count('AEE_NATIVE_TOOL_RECEIPT'), 0)

    def test_base_runner_env_never_carries_the_gate_flag(self):
        # --clearenv precedes every --setenv; the collector gate must therefore
        # be reachable only via the sandbox injection, never a host leak.
        self.assertEqual(runner_env().keys() & {'AEE_TRANSPORT_DIAGNOSTICS'}, set())


if __name__ == '__main__':
    unittest.main()