"""Codex tool-failure semantics: tool observations versus terminal verdicts.

Fixtures for the codex tool-failure contract (T01-T13 matrix): a completed
``command_execution`` with a nonzero exit code is recovered tool evidence,
recorded in ``tool_evidence`` but never a job-terminal verdict on its own.
The job resolves on the agent turn's authentic terminal event plus the
adapter process outcome, with deterministic sticky precedence — any fatal
agent event (``turn.failed``, top-level ``error``, error items, failed or
declined non-command items) wins over later completion, and ambiguous
command evidence (failed/declined with no exit code) fails closed.
Both native parser paths stay under the same contract: the streamed
budget parser (``mcp_runtime/event_stream``) and the buffered codex
finalizer (``executor.final_agent_message``).
"""
import json
import unittest

from aee.mcp_runtime.event_stream import EventParser
from aee.mcp_runtime.executor import final_agent_message
from aee.mcp_runtime.process import Limits
from aee.mcp_runtime.store import JobError


def codex_stream(*events):
    return ("\n".join(json.dumps(e) if not isinstance(e, str) else e
                      for e in events) + "\n").encode()


class StreamedParserSemantics(unittest.TestCase):
    """Budget/streamed path: mcp_runtime.event_stream.EventParser."""

    def parse(self, *events, limit=16384):
        parser = EventParser('codex', limit)
        parser.feed(codex_stream(*events), eof=True)
        return parser

    def test_recovered_command_failure_completes_with_retained_evidence(self):
        # T01/T02: one or more failed commands, agent recovers, authentic
        # final turn — completed, with the tool failures still queryable.
        parser = self.parse(
            '{"type":"thread.started","thread_id":"thr_t01"}',
            '{"type":"turn.started"}',
            '{"type":"item.started","item":{"type":"command_execution","id":"c1"}}',
            {"type": "item.completed",
             "item": {"type": "command_execution", "id": "c1", "exit_code": 1,
                      "status": "failed", "aggregate_output": "PRIVATE-CONTENT"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
            {"type": "item.started", "item": {"type": "command_execution", "id": "c2"}},
            {"type": "item.started", "item": {"type": "command_execution", "id": "c3"}},
            {"type": "item.completed",
             "item": {"type": "command_execution", "id": "c3", "exit_code": 0, "status": "completed"}},
            {"type": "item.completed",
             "item": {"type": "command_execution", "id": "c2", "exit_code": 127, "status": "failed"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "recovered"}},
            '{"type":"turn.completed"}',
        )
        self.assertEqual(parser.result(), 'recovered')
        evidence = parser.tool_evidence()
        self.assertEqual(evidence['tools_failed'], 2)
        self.assertEqual(evidence['tools_completed'], 3)
        self.assertNotIn('PRIVATE-CONTENT', json.dumps(evidence))

    def test_command_failure_then_turn_failed_is_job_failure(self):
        # T03: a terminal agent failure is fatal even after tool failures.
        parser = self.parse(
            '{"type":"thread.started","thread_id":"thr_t03"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"type":"command_execution","exit_code":1,"status":"failed"}}',
            '{"type":"turn.failed"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"partial"}}',
            '{"type":"turn.completed"}',
        )
        with self.assertRaises(JobError) as error:
            parser.result()
        self.assertEqual(error.exception.code, 'EXECUTION_FAILED')

    def test_error_item_overrides_later_completion(self):
        # T04: a native error item is a fatal agent outcome; later agent text
        # and turn.completed cannot override it (deterministic precedence).
        parser = self.parse(
            '{"type":"thread.started","thread_id":"thr_t04"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"type":"error","message":"session collapsed"}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"pretend done"}}',
            '{"type":"turn.completed"}',
        )
        with self.assertRaises(JobError) as error:
            parser.result()
        self.assertEqual(error.exception.code, 'REQUIRED_TOOL_FAILED')

    def test_noncommand_item_failure_is_fatal(self):
        # Declined/failed non-command items keep the required-operation gate.
        for item in ({'type': 'exec_action', 'status': 'declined'},
                     {'type': 'reasoning', 'status': 'failed'}):
            parser = self.parse(
                '{"type":"thread.started","thread_id":"thr_noncmd"}',
                '{"type":"turn.started"}',
                json.dumps({"type": "item.completed", "item": item}),
                '{"type":"turn.completed"}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}',
            )
            with self.assertRaises(JobError) as error:
                parser.result()
            self.assertEqual(error.exception.code, 'REQUIRED_TOOL_FAILED')

    def test_ambiguous_command_outcome_fails_closed(self):
        # A command exit must be a substantiated integer to become an
        # observation: failed/declined with no exit code, a completed command
        # without exit evidence, or a non-integer exit_code value are all
        # unsubstantiated evidence.
        for item in ({'type': 'command_execution', 'status': 'failed'},
                     {'type': 'command_execution', 'status': 'completed'},
                     {'type': 'command_execution', 'exit_code': 'unknown'}):
            parser = self.parse(
                '{"type":"thread.started","thread_id":"thr_amb"}',
                '{"type":"turn.started"}',
                json.dumps({"type": "item.completed", "item": item}),
                '{"type":"turn.completed"}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}',
            )
            with self.assertRaises(JobError) as error:
                parser.result()
            self.assertEqual(error.exception.code, 'REQUIRED_TOOL_FAILED')

    def test_completed_then_turn_failed_is_deterministic_failure(self):
        # T12: conflicting terminal events — the fatal event wins, never
        # last-event-wins nondeterminism.
        parser = self.parse(
            '{"type":"thread.started","thread_id":"thr_t12"}',
            '{"type":"turn.started"}',
            '{"type":"turn.completed"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}',
            '{"type":"turn.failed"}',
        )
        with self.assertRaises(JobError) as error:
            parser.result()
        self.assertEqual(error.exception.code, 'EXECUTION_FAILED')

    def test_missing_terminal_event_fails_closed(self):
        # T07: process exit 0 but no authentic turn.completed — no synthesis.
        parser = self.parse(
            '{"type":"thread.started","thread_id":"thr_t07"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}',
        )
        with self.assertRaises(JobError) as error:
            parser.result()
        self.assertNotIn(error.exception.code, {'', None})
        self.assertNotEqual(error.exception.code, 'COMPLETED')

    def test_new_turn_does_not_resurrect_completed_state(self):
        # A second turn clears completion; without a fresh terminal event the
        # job cannot stay completed (no stale-verdict reuse).
        parser = self.parse(
            '{"type":"thread.started","thread_id":"thr_second"}',
            '{"type":"turn.started"}',
            '{"type":"turn.completed"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"first"}}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"second"}}',
        )
        with self.assertRaises(JobError):
            parser.result()

    def test_clean_completion_unchanged(self):
        # T13: all tools succeed, valid terminal completion — completed.
        parser = self.parse(
            '{"type":"thread.started","thread_id":"thr_t13"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"type":"command_execution","exit_code":0,"status":"completed"}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"clean"}}',
            '{"type":"turn.completed"}',
        )
        self.assertEqual(parser.result(), 'clean')

    def test_truncated_stream_fails_closed(self):
        # T08: EOF mid-document — no completion can be synthesized.
        parser = EventParser('codex', 16384)
        parser.feed(b'{"type":"turn.started"}\n{"type":"item.completed","item":{"type":"comm')
        with self.assertRaises(JobError) as error:
            parser.feed(b'', eof=True)
        self.assertEqual(error.exception.code, 'PARSER_ERROR')

    def test_oversized_final_answer_still_fails_closed(self):
        # T09: recovered tool failures do not weaken the output budget.
        parser = self.parse(
            '{"type":"thread.started","thread_id":"thr_t09"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"type":"command_execution","exit_code":1,"status":"failed"}}',
            {"type": "item.completed", "item": {"type": "agent_message", "text": "x" * 40}},
            '{"type":"turn.completed"}',
            limit=16,
        )
        with self.assertRaises(JobError) as error:
            parser.result()
        self.assertEqual(error.exception.code, 'OUTPUT_LIMIT_EXCEEDED')


class StreamedProcessIntegration(unittest.TestCase):
    """Full budget path: real child process -> run_streamed -> EventParser."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from aee.mcp_runtime.spool import SpoolStore
        self.temp = tempfile.TemporaryDirectory(prefix='aee-codex-semantics-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SpoolStore(self.root / 'spool')
        self.counter = 0
        self.addCleanup(self.store.close)

    def run_codex_stream(self, *events):
        import sys
        from aee.mcp_runtime.budget import ExecutionBudget
        from aee.mcp_runtime.process import run_streamed
        body = ('import json,sys\nfor e in ' + json.dumps(list(events)) +
                ':sys.stdout.write(json.dumps(e)+"\\n")\nsys.stdout.flush()\n')
        budget = ExecutionBudget(wall_timeout_seconds=30)
        self.counter += 1
        job = self.store.create('A3JOB-20261020-' + f'{self.counter:032x}', budget, 'fixture-policy')
        self.addCleanup(job.close)
        return run_streamed(
            [sys.executable, '-c', body], env={}, stdin=b'', budget=budget, spool=job,
            parser=EventParser('codex', budget.result_inline_bytes, budget.memory_capture_bytes),
            cwd=str(self.root), cancel_event=None, redact=lambda v: v)

    def test_recovered_command_failure_completes_at_process_level(self):
        # T01 end to end: exit-shot fails no more at the process boundary.
        result = self.run_codex_stream(
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "command_execution",
                                                "exit_code": 1, "status": "failed"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
            {"type": "turn.completed"},
        )
        self.assertEqual(result.final, 'ok')
        self.assertEqual(result.streaming['streams']['stdout']['spool_bytes'] > 0, True)

    def test_streamed_turn_failed_fails_job_at_process_level(self):
        # T03 end to end: fatal terminal event poisons the job, process exit 0.
        with self.assertRaises(JobError) as error:
            self.run_codex_stream(
                {"type": "turn.started"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
                {"type": "turn.completed"},
                {"type": "turn.failed"},
            )
        self.assertEqual(error.exception.code, 'EXECUTION_FAILED')


class BufferedFinalizerSemantics(unittest.TestCase):
    """Buffered codex path: executor.final_agent_message."""

    def test_recovered_command_failure_completes(self):
        stdout = codex_stream(
            '{"type":"thread.started","thread_id":"thr_e01"}',
            '{"type":"turn.started"}',
            {"type": "item.completed",
             "item": {"type": "command_execution", "exit_code": 1, "status": "failed"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
            '{"type":"turn.completed"}',
        )
        self.assertEqual(final_agent_message(stdout, Limits()), 'ok')

    def test_command_failure_then_turn_failed_is_job_failure(self):
        stdout = codex_stream(
            '{"type":"thread.started","thread_id":"thr_e03"}',
            '{"type":"turn.started"}',
            {"type": "item.completed",
             "item": {"type": "command_execution", "exit_code": 1, "status": "failed"}},
            '{"type":"turn.failed"}',
        )
        with self.assertRaises(JobError) as error:
            final_agent_message(stdout, Limits())
        self.assertEqual(error.exception.code, 'EXECUTION_FAILED')
        self.assertEqual(error.exception.failure['failing_event']['event_type'], 'turn.failed')

    def test_error_item_overrides_later_completion(self):
        stdout = codex_stream(
            '{"type":"thread.started","thread_id":"thr_e04"}',
            '{"type":"turn.started"}',
            {"type": "item.completed", "item": {"type": "error", "message": "bad"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "pretend"}},
            '{"type":"turn.completed"}',
        )
        with self.assertRaises(JobError) as error:
            final_agent_message(stdout, Limits())
        self.assertEqual(error.exception.code, 'REQUIRED_TOOL_FAILED')

    def test_ambiguous_command_outcome_fails_closed_and_names_the_event(self):
        for item in ({'type': 'command_execution', 'status': 'failed',
                      'aggregate_output': 'raw command output must not be retained'},
                     {'type': 'command_execution', 'exit_code': 'unknown'}):
            stdout = codex_stream(
                '{"type":"thread.started","thread_id":"thr_eamb"}',
                '{"type":"turn.started"}',
                {"type": "item.completed", "item": item},
                '{"type":"turn.completed"}',
                {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
            )
            with self.assertRaises(JobError) as error:
                final_agent_message(stdout, Limits())
            self.assertEqual(error.exception.code, 'REQUIRED_TOOL_FAILED')
            failure = error.exception.failure
            self.assertEqual(failure['failing_event']['item_type'], 'command_execution')
            self.assertNotIn('raw command output', json.dumps(failure))

    def test_prior_turn_completion_cannot_settle_a_new_turn(self):
        # Turn 1 completes; turn 2 then has a failed command and a partial
        # answer but never its own terminal event — the stale completion from
        # turn 1 must not let this stream resolve to success.
        stdout = codex_stream(
            '{"type":"thread.started","thread_id":"thr_e12"}',
            '{"type":"turn.started"}',
            '{"type":"turn.completed"}',
            {"type": "item.completed", "item": {"type": "agent_message", "text": "turn one"}},
            '{"type":"turn.started"}',
            {"type": "item.completed",
             "item": {"type": "command_execution", "exit_code": 1, "status": "failed"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "turn two"}},
        )
        with self.assertRaises(JobError) as error:
            final_agent_message(stdout, Limits())
        self.assertEqual(error.exception.code, 'EXECUTION_FAILED')

    def test_error_item_before_turn_started_is_fatal(self):
        # Buffered path must not ignore native error items that precede the
        # first turn.started (mirrors the streamed parser's classification).
        stdout = codex_stream(
            '{"type":"thread.started","thread_id":"thr_e13"}',
            {"type": "item.completed", "item": {"type": "error", "message": "bad"}},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "pretend"}},
            '{"type":"turn.completed"}',
        )
        with self.assertRaises(JobError) as error:
            final_agent_message(stdout, Limits())
        self.assertEqual(error.exception.code, 'REQUIRED_TOOL_FAILED')

    def test_duplicate_exit_code_member_is_malformed_fallback(self):
        # Native JSONL with a duplicated exit_code member is ambiguous
        # evidence: the buffered stdlib decoder must reject it outright
        # instead of taking the last value (the streamed parser already
        # rejects duplicate projected members).
        stdout = ('{"type":"thread.started","thread_id":"thr_e14"}\n'
                  '{"type":"turn.started"}\n'
                  '{"type":"item.completed","item":{"type":"command_execution",'
                  '"exit_code":null,"exit_code":1,"status":"completed"}}\n'
                  '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n'
                  '{"type":"turn.completed"}\n')
        with self.assertRaises(JobError) as error:
            final_agent_message(stdout.encode(), Limits())
        self.assertEqual(error.exception.code, 'EXECUTION_FAILED')

    def test_malformed_stream_fails_closed(self):
        # T08: malformed native event data never resolves to success.
        with self.assertRaises(JobError) as error:
            final_agent_message(codex_stream('{not json}'), Limits())
        self.assertEqual(error.exception.code, 'EXECUTION_FAILED')

    def test_missing_final_message_fails_closed(self):
        # T07: turn.completed without any agent message cannot complete.
        stdout = codex_stream(
            '{"type":"thread.started","thread_id":"thr_e07"}',
            '{"type":"turn.started"}',
            '{"type":"turn.completed"}',
        )
        with self.assertRaises(JobError) as error:
            final_agent_message(stdout, Limits())
        self.assertEqual(error.exception.code, 'EXECUTION_FAILED')


if __name__ == '__main__':
    unittest.main()