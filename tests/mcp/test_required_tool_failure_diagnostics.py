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


class EvidenceValidationDiagnostics(unittest.TestCase):
    """TOOL_EVIDENCE_INCOMPLETE keeps scalar-only receipt state for attribution."""

    @staticmethod
    def stream(events):
        return ("\n".join(events) + "\n").encode()

    def test_incomplete_receipt_retains_bounded_receipt_state(self):
        from aee.mcp_runtime.native_telemetry import REVISION
        receipt = json.dumps({
            'type': 'aee.native_tool_receipt', 'revision': REVISION, 'complete': False,
            'conversation': 'thr_evidence_1',
            'tools': [{'sequence': 1, 'tool_name': 'exec_command', 'source': 'code_mode',
                       'success': True, 'exit_code': 0, 'conversation': 'thr_evidence_1'}],
        })
        events = [
            '{"type":"thread.started","thread_id":"thr_evidence_1"}',
            '{"type":"turn.started"}',
            '{"type":"turn.completed"}',
            '{"type":"aee.native_tool_receipt",' + receipt[1:-1] + '}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
        ]
        with self.assertRaises(JobError) as error:
            final_agent_message(self.stream(events), Limits(), require_receipt=True)
        self.assertEqual(error.exception.code, "TOOL_EVIDENCE_INCOMPLETE")
        failure = error.exception.failure
        self.assertFalse(failure["evidence_complete"])
        state = failure["receipt_state"]
        self.assertTrue(state["receipt_present"])
        self.assertTrue(state["thread_present"])
        self.assertFalse(state["receipt_complete"])
        self.assertEqual(state["tool_count"], 1)
        self.assertEqual(state["tools"][0]["tool_name"], "exec_command")
        self.assertEqual(state["tools"][0]["exit_code"], 0)
        self.assertNotIn("conversation", state["tools"][0])
        self.assertEqual(failure["condition"], "receipt_missing_or_malformed")
        raw = json.dumps(failure)
        self.assertLessEqual(len(raw), 20000)

    def test_missing_receipt_is_attributed(self):
        events = [
            '{"type":"thread.started","thread_id":"thr_evidence_2"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
            '{"type":"turn.completed"}',
        ]
        with self.assertRaises(JobError) as error:
            final_agent_message(self.stream(events), Limits(), require_receipt=True)
        self.assertEqual(error.exception.code, "TOOL_EVIDENCE_INCOMPLETE")
        state = error.exception.failure["receipt_state"]
        self.assertFalse(state["receipt_present"])
        self.assertTrue(state["thread_present"])

    def test_unverified_completion_cell_is_labeled(self):
        from aee.mcp_runtime.native_telemetry import REVISION
        receipt = json.dumps({
            'type': 'aee.native_tool_receipt', 'revision': REVISION, 'complete': True,
            'conversation': 'thr_evidence_4',
            'tools': [{'sequence': 1, 'tool_name': 'exec', 'source': 'direct',
                       'success': True, 'call_id': 'call_' + 'a' * 10,
                       'unverified_completion': True, 'conversation': 'thr_evidence_4'}],
        })
        events = [
            '{"type":"thread.started","thread_id":"thr_evidence_4"}',
            '{"type":"turn.started"}',
            '{"type":"turn.completed"}',
            '{"type":"aee.native_tool_receipt",' + receipt[1:-1] + '}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
        ]
        with self.assertRaises(JobError) as error:
            final_agent_message(self.stream(events), Limits(), require_receipt=True)
        self.assertEqual(error.exception.code, "TOOL_EVIDENCE_INCOMPLETE")
        failure = error.exception.failure
        self.assertEqual(failure["condition"], "unverified_completion_cell")
        state = failure["receipt_state"]
        self.assertEqual(state["unverified_cells"], 1)
        self.assertEqual(state["missing_exit_code"], 0)
        self.assertTrue(state["receipt_complete"])

    def test_missing_exit_code_is_labeled(self):
        from aee.mcp_runtime.native_telemetry import REVISION
        receipt = json.dumps({
            'type': 'aee.native_tool_receipt', 'revision': REVISION, 'complete': True,
            'conversation': 'thr_evidence_5',
            'tools': [{'sequence': 1, 'tool_name': 'exec_command', 'source': 'code_mode',
                       'success': True, 'call_id': 'call_' + 'b' * 10,
                       'conversation': 'thr_evidence_5'}],
        })
        events = [
            '{"type":"thread.started","thread_id":"thr_evidence_5"}',
            '{"type":"turn.started"}',
            '{"type":"turn.completed"}',
            '{"type":"aee.native_tool_receipt",' + receipt[1:-1] + '}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
        ]
        with self.assertRaises(JobError) as error:
            final_agent_message(self.stream(events), Limits(), require_receipt=True)
        self.assertEqual(error.exception.code, "TOOL_EVIDENCE_INCOMPLETE")
        failure = error.exception.failure
        self.assertEqual(failure["condition"], "exit_code_missing")
        state = failure["receipt_state"]
        self.assertEqual(state["missing_exit_code"], 1)
        self.assertEqual(state["unverified_cells"], 0)
        self.assertNotIn("exit_code", state["tools"][0])

    def test_broker_declaration_mismatch_is_labeled(self):
        from aee.mcp_runtime.native_telemetry import REVISION
        receipt = json.dumps({
            'type': 'aee.native_tool_receipt', 'revision': REVISION, 'complete': True,
            'conversation': 'thr_evidence_6',
            'tools': [{'sequence': 1, 'tool_name': 'exec_command', 'source': 'direct',
                       'success': True, 'exit_code': 0, 'call_id': 'call_' + 'a' * 10,
                       'conversation': 'thr_evidence_6'}],
        })
        events = [
            '{"type":"thread.started","thread_id":"thr_evidence_6"}',
            '{"type":"turn.started"}',
            '{"type":"turn.completed"}',
            '{"type":"aee.native_tool_receipt",' + receipt[1:-1] + '}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
        ]
        broker_receipt = {'valid': True, 'response_complete': True,
                          'expected_calls': {'call_' + 'b' * 10: 'exec_command'}}
        with self.assertRaises(JobError) as error:
            final_agent_message(self.stream(events), Limits(), require_receipt=True,
                                broker_receipt=broker_receipt)
        self.assertEqual(error.exception.code, "TOOL_EVIDENCE_INCOMPLETE")
        failure = error.exception.failure
        self.assertEqual(failure["condition"], "broker_receipt_invalid")
        state = failure["receipt_state"]
        self.assertTrue(state["broker_valid"])
        self.assertTrue(state["broker_response_complete"])
        self.assertTrue(state["broker_declaration_mismatch"])
        self.assertEqual(state["broker_expected_calls"],
                         {'call_' + 'b' * 10: 'exec_command'})
        self.assertEqual(state["observed_direct_calls"],
                         {'call_' + 'a' * 10: 'exec_command'})
        raw = json.dumps(failure)
        self.assertLessEqual(len(raw), 20000)

    def test_valid_receipt_with_broker_receipt_succeeds_unchanged(self):
        from aee.mcp_runtime.native_telemetry import REVISION
        receipt = json.dumps({
            'type': 'aee.native_tool_receipt', 'revision': REVISION, 'complete': True,
            'conversation': 'thr_evidence_3',
            'tools': [{'sequence': 1, 'tool_name': 'curr_time', 'source': 'direct',
                       'success': True, 'call_id': 'call_' + 'a' * 10,
                       'conversation': 'thr_evidence_3'}],
        })
        events = [
            '{"type":"thread.started","thread_id":"thr_evidence_3"}',
            '{"type":"turn.started"}',
            '{"type":"turn.completed"}',
            '{"type":"aee.native_tool_receipt",' + receipt[1:-1] + '}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
        ]
        # No broker receipt: accepted=False path validates lifecycle only.
        self.assertEqual(
            final_agent_message(self.stream(events), Limits(), require_receipt=True), "done")


if __name__ == "__main__":
    unittest.main()