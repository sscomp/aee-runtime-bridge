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

Contract v2 additions (remediation of review findings F01-F03): a
table-driven streamed/buffered parity matrix over the same event fixtures
(final-answer placement, malformed exit_code values, fatal-event
precedence, multi-turn recovery), and process-level tests locking the EOF
precedence — process-exit mapping before the parser verdict on both paths.
"""
import json
import unittest

from aee.mcp_runtime.event_stream import EventParser
from aee.mcp_runtime.executor import final_agent_message
from aee.mcp_runtime.process import Limits, run_streamed
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

    def test_duplicate_item_id_member_is_not_verdict_evidence(self):
        # Native codex 0.159.0 web_search items carry the turn-level item id
        # and the internal exec id under the same `id` member name. Neither is
        # projected on this decision path, so a duplicated `id` is the native
        # CLI's shape, not ambiguous verdict evidence: both decision paths
        # classify the stream identically (parity), keeping the exit_code /
        # thread_id / type / status duplicate rejection fail-closed.
        item_id = '"id":"item_2","type":"web_search","id":"exec-bb886692","query":"q"'
        stdout = ('{"type":"thread.started","thread_id":"thr_w1"}\n'
                  '{"type":"turn.started"}\n'
                  '{"type":"item.started","item":{' + item_id + '}}\n'
                  '{"type":"item.completed","item":{' + item_id + ',"query":"resolved"}}\n'
                  '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n'
                  '{"type":"turn.completed"}\n')
        message = final_agent_message(stdout.encode(), Limits())
        self.assertEqual(message, 'ok')
        parser = EventParser('codex', 16384)
        parser.feed(stdout.encode(), eof=True)
        self.assertEqual(parser.result(), 'ok')

    def test_duplicate_message_text_member_stays_fail_closed(self):
        # A duplicated agent_message text would make the final answer
        # ambiguous; the decision-critical duplicate rejection is retained.
        stdout = ('{"type":"thread.started","thread_id":"thr_e15"}\n'
                  '{"type":"turn.started"}\n'
                  '{"type":"item.completed","item":{"type":"agent_message",'
                  '"text":"a","text":"b"}}\n'
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


def _command_item(exit_code='MISSING', status='failed'):
    item = {'type': 'command_execution', 'status': status}
    if exit_code != 'MISSING':
        item['exit_code'] = exit_code
    return {"type": "item.completed", "item": item}


def _message(text):
    return {"type": "item.completed", "item": {"type": "agent_message", "text": text}}


def decide(events):
    """Run the identical event fixture through both native decision paths."""
    def streamed():
        parser = EventParser('codex', 16384)
        parser.feed(codex_stream(*events), eof=True)
        return parser.result()

    def buffered():
        return final_agent_message(codex_stream(*events), Limits())
    outcomes = {}
    for name, fn in (('streamed', streamed), ('buffered', buffered)):
        try:
            outcomes[name] = ('completed', fn())
        except JobError as exc:
            outcomes[name] = ('failed', {'code': exc.code})
    return outcomes


class StreamedBufferedParityMatrix(unittest.TestCase):
    """Contract v2 parity matrix: same fixture, both paths, same verdict.

    Locked precedence (SEMANTICS_CONTRACT v2):
      process exit mapping -> fatal agent events -> terminal event + answer.
    Error-code differences between paths are allowed only where the contract
    names the per-path code (missing answer: streamed PARSER_ERROR,
    buffered EXECUTION_FAILED); verdict direction (completed/failed) and the
    recovered-evidence semantics must agree everywhere.
    """

    def assertAgree(self, events, expect, *, msg=None):
        outcomes = decide(events)
        for name, outcome in outcomes.items():
            self.assertEqual(outcome, expect,
                             f'{name} disagreed: {outcome} != {expect}' + (f' ({msg})' if msg else ''))

    def test_parity_malformed_exit_code_never_resolves_success(self):
        # exit_code table per contract: only a substantiated integer is a
        # command outcome; every non-integer (including missing) is ambiguous,
        # stays a sticky REQUIRED_TOOL_FAILED on both paths, and is never a
        # silent success.
        for value in (None, [], {}, "1", 1.5, True, False, 'missing', 0.0):
            events = ('{"type":"turn.started"}', _command_item(value),
                      _message('ok'), '{"type":"turn.completed"}', )
            self.assertAgree(events, ('failed', {'code': 'REQUIRED_TOOL_FAILED'}),
                             msg=f'exit_code={value!r}')

    def test_parity_integer_exit_codes_are_recovered_evidence(self):
        for value in (-2, -1, 1, 7, 0):
            events = ('{"type":"thread.started","thread_id":"t"}',
                      '{"type":"turn.started"}', _command_item(value),
                      _message('ok'), '{"type":"turn.completed"}')
            self.assertAgree(events, ('completed', 'ok'))
        parser = EventParser('codex', 16384)
        parser.feed(codex_stream(
            '{"type":"turn.started"}', _command_item(1), _command_item(0),
            _message('ok'), '{"type":"turn.completed"}'), eof=True)
        parser.result()
        evidence = parser.tool_evidence()
        self.assertEqual(evidence['tools_failed'], 1)
        self.assertEqual(evidence['tools_completed'], 2)

    def test_parity_final_answer_is_last_agent_message_of_final_turn(self):
        # (a) Last final answer removed, earlier same-turn message retained:
        # the native stream protocol never deletes an emitted event, so this
        # control is a synthetic mutilation. Contract rule: the answer is the
        # last nonblank agent_message text of the final turn — the earlier
        # same-turn message is therefore emitted on BOTH paths (not the
        # 4284-byte removed answer). This documents the corrected negative
        # control that review finding F01 challenged.
        events_a = ('{"type":"thread.started","thread_id":"t"}',
                    '{"type":"turn.started"}', _command_item(1),
                    _message('MID-TURN NARRATION'), _command_item(1),
                    '{"type":"turn.completed"}')
        self.assertAgree(events_a, ('completed', 'MID-TURN NARRATION'))

    def test_parity_final_message_on_current_turn_wins(self):
        # (d) with the final message present the newest answer is emitted.
        events_d = ('{"type":"turn.started"}', _command_item(1),
                    _message('FINAL ANSWER'), '{"type":"turn.completed"}')
        self.assertAgree(events_d, ('completed', 'FINAL ANSWER'))

    def test_parity_turn_without_any_agent_message_fails_closed(self):
        # (b) No answer anywhere: both paths fail closed. Path-specific codes
        # (streamed PARSER_ERROR / buffered EXECUTION_FAILED) are named by
        # the contract's per-path error-code table.
        events_b = ('{"type":"thread.started","thread_id":"t"}',
                    '{"type":"turn.started"}', _command_item(1),
                    '{"type":"turn.completed"}')
        outcomes = decide(events_b)
        self.assertEqual(outcomes['streamed'], ('failed', {'code': 'PARSER_ERROR'}))
        self.assertEqual(outcomes['buffered'], ('failed', {'code': 'EXECUTION_FAILED'}))

    def test_parity_blank_or_textless_message_cannot_publish_an_answer(self):
        # An agent_message with blank/missing text yields NO pending answer;
        # a later textful message restores it. A turn ending on a blank
        # message has no answer and fails closed on both paths, with the
        # contract's per-path missing-answer code.
        outcomes = decide(('{"type":"turn.started"}', _command_item(1),
                           _message('   '), '{"type":"turn.completed"}'))
        self.assertEqual(outcomes['streamed'], ('failed', {'code': 'PARSER_ERROR'}))
        self.assertEqual(outcomes['buffered'], ('failed', {'code': 'EXECUTION_FAILED'}))
        self.assertAgree(('{"type":"turn.started"}', _message(['no']),
                          _message('restored'), '{"type":"turn.completed"}'),
                         ('completed', 'restored'))

    def test_parity_multi_turn_recovery_emits_only_newest_turn_answer(self):
        # (c) Legitimate recovery must keep working and must never emit the
        # earlier TURN's answer after a subsequent turn.started (contract v2:
        # cross-turn staleness is forbidden). Both paths agree.
        events_c = ('{"type":"thread.started","thread_id":"t"}',
                    '{"type":"turn.started"}', _message('STALE TURN-1 ANSWER'),
                    '{"type":"turn.completed"}',
                    '{"type":"turn.started"}', _command_item(1),
                    _message('TURN-2 RECOVERED'), '{"type":"turn.completed"}')
        self.assertAgree(events_c, ('completed', 'TURN-2 RECOVERED'))
        parser = EventParser('codex', 16384)
        parser.feed(codex_stream(*events_c), eof=True)
        parser.result()
        self.assertEqual(parser.tool_evidence()['tools_failed'], 1)

    def test_parity_sticky_fatal_survives_a_new_turn(self):
        # A fatal agent event is sticky: a later turn.started, completion or
        # answer can never override it (buffered raises in place — same
        # verdict deterministically).
        events = ('{"type":"thread.started","thread_id":"t"}',
                  '{"type":"turn.started"}', '{"type":"turn.failed"}',
                  '{"type":"turn.started"}', _message('pretend fine'),
                  '{"type":"turn.completed"}')
        self.assertAgree(events, ('failed', {'code': 'EXECUTION_FAILED'}))


class StreamedEofPrecedence(unittest.TestCase):
    """run_streamed maps the process exit before the parser verdict (F03)."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from aee.mcp_runtime.spool import SpoolStore
        self.temp = tempfile.TemporaryDirectory(prefix='aee-codex-precedence-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SpoolStore(self.root / 'spool')
        self.addCleanup(self.store.close)
        self.counter = 0

    def run_stream(self, body_events, exit_code):
        import sys
        from aee.mcp_runtime.budget import ExecutionBudget
        body = ('import json,sys\nfor e in ' + json.dumps(list(body_events)) +
                ':sys.stdout.write(json.dumps(e)+"\\n")\nsys.stdout.flush()\n'
                f'sys.exit({exit_code})\n')
        budget = ExecutionBudget(wall_timeout_seconds=30)
        self.counter += 1
        job = self.store.create('A3JOB-20261020-' + f'{self.counter:032x}', budget, 'fixture-policy')
        self.addCleanup(job.close)
        return run_streamed(
            [sys.executable, '-c', body], env={}, stdin=b'', budget=budget, spool=job,
            parser=EventParser('codex', budget.result_inline_bytes, budget.memory_capture_bytes),
            cwd=str(self.root), cancel_event=None, redact=lambda v: v)

    def test_nonzero_exit_outranks_parser_verdict(self):
        # Clean parser verdict (turn.completed + answer would complete), but
        # the process exited 1: the job fails on the exit mapping — the exit
        # code is the strongest process-level outcome at EOF.
        with self.assertRaises(JobError) as error:
            self.run_stream(
                [{"type": "turn.started"},
                 _command_item(1),
                 _message('ok'),
                 {"type": "turn.completed"}], exit_code=1)
        self.assertEqual(error.exception.code, 'EXECUTION_FAILED')
        self.assertEqual(error.exception.exit_code, 1)

    def test_nonzero_exit_outranks_ambiguous_command_evidence(self):
        # Sticky parser fatal (ambiguous exit evidence) + process exit 1:
        # the exit mapping outranks the parser verdict (buffered parity).
        with self.assertRaises(JobError) as error:
            self.run_stream(
                [{"type": "turn.started"},
                 _command_item('MISSING'),
                 _message('ok'),
                 {"type": "turn.completed"}], exit_code=1)
        self.assertEqual(error.exception.code, 'EXECUTION_FAILED')
        self.assertEqual(error.exception.exit_code, 1)

    def test_zero_exit_defers_to_sticky_parser_fatal(self):
        # Process exit 0: the sticky parser fatal decides (unchanged).
        with self.assertRaises(JobError) as error:
            self.run_stream(
                [{"type": "turn.started"},
                 _command_item('MISSING'),
                 _message('ok'),
                 {"type": "turn.completed"}], exit_code=0)
        self.assertEqual(error.exception.code, 'REQUIRED_TOOL_FAILED')
        self.assertEqual(error.exception.exit_code, 0)


if __name__ == '__main__':
    unittest.main()