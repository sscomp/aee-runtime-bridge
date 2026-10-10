"""Regression: bounded, secret-free native failure diagnostics on fail-shot paths.

The R3 contract treats any nonzero command exit (or failed/declined/error item)
as REQUIRED_TOOL_FAILED and fails the job — intentionally strict, and unchanged
here. What was lost was attribution evidence: the failure record carried only
``evidence_source``/``recovery_permitted``, so the exact failing native event
could not be reconstructed after the sandbox was torn down. These tests pin the
bounded diagnostics contract: a scalar-only summary of the failing event in
``JobError.failure``, with no raw command output retained.
"""
import json
import unittest

from aee.mcp_runtime.executor import final_agent_message
from aee.mcp_runtime.process import Limits
from aee.mcp_runtime.store import JobError


class RequiredToolFailureDiagnostics(unittest.TestCase):
    @staticmethod
    def stream(events):
        return ("\n".join(events) + "\n").encode()

    def assert_bounded(self, failure):
        """Scalar-only summary; no free-text payloads ride along."""
        self.assertIn("evidence_source", failure)
        self.assertIn("recovery_permitted", failure)
        if "failing_event" in failure:
            for key, value in failure["failing_event"].items():
                self.assertIsInstance(value, (str, int, type(None)), key)
                if isinstance(value, str):
                    self.assertLessEqual(len(value), 100, key)

    def test_nonzero_command_exit_is_failed_and_names_the_event(self):
        events = [
            '{"type":"thread.started","thread_id":"thr_probe_1"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"id":"item_7","type":"command_execution",'
            '"exit_code":2,"aggregate_output":"raw command output must not be retained"}}',
            '{"type":"turn.completed"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
        ]
        with self.assertRaises(JobError) as error:
            final_agent_message(self.stream(events), Limits())
        self.assertEqual(error.exception.code, "REQUIRED_TOOL_FAILED")
        self.assertEqual(error.exception.exit_code, 0)
        failure = error.exception.failure
        self.assertEqual(failure["evidence_source"], "native-jsonl")
        self.assertFalse(failure["recovery_permitted"])
        self.assertEqual(failure["failing_event"]["item_type"], "command_execution")
        self.assertEqual(failure["failing_event"]["exit_code"], 2)
        self.assertEqual(failure["thread_id"], "thr_probe_1")
        raw = json.dumps(failure)
        self.assertNotIn("raw command output must not be retained", raw)
        self.assert_bounded(failure)

    def test_failed_item_and_error_items_are_attributed(self):
        events = [
            '{"type":"thread.started","thread_id":"thr_probe_2"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"id":"item_2","type":"reasoning",'
            '"status":"failed","text":"unused"}}',
            '{"type":"turn.completed"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
        ]
        with self.assertRaises(JobError) as error:
            final_agent_message(self.stream(events), Limits())
        self.assertEqual(error.exception.code, "REQUIRED_TOOL_FAILED")
        self.assertEqual(error.exception.failure["failing_event"]["item_type"], "reasoning")
        self.assertEqual(error.exception.failure["failing_event"]["item_status"], "failed")

        error_events = [
            '{"type":"thread.started","thread_id":"thr_probe_3"}',
            '{"type":"turn.started"}',
            '{"type":"turn.failed"}',
        ]
        with self.assertRaises(JobError) as error:
            final_agent_message(self.stream(error_events), Limits())
        self.assertEqual(error.exception.code, "EXECUTION_FAILED")
        self.assertEqual(error.exception.failure["failing_event"]["event_type"], "turn.failed")
        self.assert_bounded(error.exception.failure)

    def test_declined_item_is_required_tool_failure(self):
        events = [
            '{"type":"thread.started","thread_id":"thr_probe_4"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"id":"item_3","type":"exec_action",'
            '"status":"declined"}}',
            '{"type":"turn.completed"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
        ]
        with self.assertRaises(JobError) as error:
            final_agent_message(self.stream(events), Limits())
        self.assertEqual(error.exception.code, "REQUIRED_TOOL_FAILED")
        self.assertEqual(error.exception.failure["failing_event"]["item_status"], "declined")

    def test_clean_completion_is_unchanged(self):
        events = [
            '{"type":"thread.started","thread_id":"thr_probe_5"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"all good"}}',
            '{"type":"turn.completed"}',
        ]
        self.assertEqual(final_agent_message(self.stream(events), Limits()), "all good")


if __name__ == "__main__":
    unittest.main()