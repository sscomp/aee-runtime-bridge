"""R1 contract fixtures, hostile drift, real native serialization and Code Mode."""
import copy
import io
import json
import unittest
from unittest.mock import patch
from pathlib import Path

from aee.mcp_runtime.broker import PolicyError, validate_request
from aee.mcp_runtime.provider_contract import (COMPANION_SHA256, NATIVE_SHA256, NATIVE_VERSION,
    TOOLS_SHA256, contract_record, matches_reviewed_executor)
from aee.mcp_runtime.runtime import require_deployment_ready
from aee.mcp_runtime.store import JobError
from aee.mcp_runtime.resource_policy import resource_record
from native_provider_harness import normalize_request, probe_native, schema_digest
from test_p2c_broker import FixtureTransport, NATIVE, events, request_body

FIXTURES = Path(__file__).parent / "fixtures"
NATIVE160 = Path(__import__('os').environ.get('AEE_TEST_UNREVIEWED_CODEX', '/nonexistent/qualification/unreviewed-codex'))


def approved_request():
    body = request_body()
    tools = json.loads((FIXTURES / 'codex-r1-approved-tools.json').read_text())
    body['input'].insert(0, {'type': 'additional_tools', 'role': 'developer', 'tools': tools})
    return body


class CodeModeTransport(FixtureTransport):
    """Expose the remaining Code Mode resource blocker with a pure JS cell."""
    def open(self, body):
        self.requests.append(json.loads(body))
        if len(self.requests) != 1:
            return io.BytesIO(), io.BytesIO(events())
        item = {'type': 'custom_tool_call', 'id': 'ctc_fixture', 'call_id': 'call_fixture',
                'namespace': 'functions', 'name': 'exec', 'input': "text('R1 Code Mode fixture');"}
        stream = [
            {'type': 'response.created', 'response': {'id': 'resp_fixture', 'status': 'in_progress'}},
            {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
            {'type': 'response.completed', 'response': {'id': 'resp_fixture', 'status': 'completed',
                 'output': [item], 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}},
        ]
        return io.BytesIO(), io.BytesIO(b''.join(('event: ' + e['type'] + '\ndata: ' +
                                                json.dumps(e) + '\n\n').encode() for e in stream))


class R1Policy(unittest.TestCase):
    def validate(self, body):
        return json.loads(validate_request('POST', '/v1/responses', {}, json.dumps(body).encode()))

    def test_exact_reviewed_schema_is_accepted_and_pinned(self):
        body = approved_request()
        self.assertEqual(schema_digest(body['input'][0]['tools']), TOOLS_SHA256)
        self.assertEqual(self.validate(body)['input'], body['input'])

    def test_collaboration_arbitrary_tools_and_same_name_schema_drift_rejected(self):
        original = approved_request()
        variants = []
        for namespace in ['collaboration', 'multi_agent_v1', 'multi_agent_v2', 'unknown']:
            body = copy.deepcopy(original)
            body['input'][0]['tools'].append({'type': 'namespace', 'name': namespace,
                'tools': [{'type': 'function', 'name': 'spawn_agent'}]})
            variants.append(body)
        for mutate in [lambda tools: tools[0]['tools'][0].update(description='unreviewed shell authority'),
                       lambda tools: tools[0]['tools'][1].update(parameters={'type': 'object'}),
                       lambda tools: tools[0]['tools'].append({'type': 'function', 'name': 'spawn_agent'}),
                       lambda tools: tools[0].update(extra='unreviewed'),
                       lambda tools: tools.clear()]:
            body = copy.deepcopy(original);mutate(body['input'][0]['tools']);variants.append(body)
        for body in variants:
            with self.assertRaises(PolicyError):self.validate(body)

    def test_top_level_function_tools_are_not_a_schema_bypass(self):
        for tool in [{'type': 'function', 'name': 'spawn_agent'}, {'type': 'custom', 'name': 'exec'},
                     {'type': 'function', 'name': 'exec_command'}, {'type': 'web_search'}, {'type': 'mcp'}]:
            body = approved_request();body['tools'] = [tool]
            with self.assertRaises(PolicyError):self.validate(body)

    def test_tool_item_envelope_position_role_and_duplicates_rejected(self):
        original = approved_request()
        variants = [list(reversed(original['input'])), original['input'] * 2]
        for key, value in [('role', 'user'), ('authority', 'arbitrary')]:
            inputs = copy.deepcopy(original['input']);inputs[0][key] = value;variants.append(inputs)
        for inputs in variants:
            with self.assertRaises(PolicyError):self.validate({**original, 'input': inputs})

    def test_unapproved_invocation_history_rejected(self):
        for kind, name, namespace in [('function_call', 'spawn_agent', 'collaboration'),
                ('function_call', 'spawn_agent', 'functions'), ('custom_tool_call', 'exec', 'unknown'),
                ('function_call', 'create_goal', 'functions')]:
            body = approved_request();body['input'].append({'type': kind, 'name': name, 'namespace': namespace})
            with self.assertRaises(PolicyError):self.validate(body)

    def test_model_reasoning_storage_and_stream_restrictions_preserved(self):
        for variant in [{'model': 'other'}, {'reasoning': {'effort': 'low'}}, {'store': True},
                        {'stream': False}, {'reasoning': {'effort': 'high', 'context': 'other'}}]:
            with self.assertRaises(PolicyError):self.validate({**approved_request(), **variant})

    def test_snapshot_omits_header_values_prompt_and_metadata(self):
        body = approved_request();body['client_metadata'] = {'private': 'synthetic private metadata'}
        snap = normalize_request('POST', '/v1/responses', {'authorization': 'synthetic private material'}, body)
        text = json.dumps(snap)
        self.assertNotIn('synthetic private', text)
        self.assertNotIn('synthetic inspection', text)
        self.assertEqual(snap['header_names'], ['authorization'])

    def test_native_identity_and_companion_scope_of_verification(self):
        self.assertTrue(matches_reviewed_executor(NATIVE_VERSION, NATIVE_SHA256, COMPANION_SHA256))
        for values in [('codex-cli 0.160.0', NATIVE_SHA256, COMPANION_SHA256),
                       (NATIVE_VERSION, '0' * 64, COMPANION_SHA256),
                       (NATIVE_VERSION, NATIVE_SHA256, '0' * 64)]:
            self.assertFalse(matches_reviewed_executor(*values))

    def test_contract_verification_is_not_deployment_authorization(self):
        body = {'native_provider_compatibility': 'VERIFIED', 'provider_contract': contract_record(),
                'deployment_status': 'STAGE1_NOT_DEPLOYABLE', 'codex': {'version': NATIVE_VERSION,
                'sha256': NATIVE_SHA256, 'code_mode_host': {'sha256': COMPANION_SHA256}}}
        with self.assertRaises(JobError):require_deployment_ready(body)
        # This synthetic object is only a unit fixture, never a persisted runtime manifest.
        body.update(deployment_status='APPROVED_STAGE2', operator_approval_id='synthetic fixture only')
        with self.assertRaises(JobError):require_deployment_ready(body)
        body['native_tool_runtime_compatibility']='VERIFIED'  # hypothetical resolved tool fixture only
        with self.assertRaises(JobError):require_deployment_ready(body)
        body.update(resource_containment='VERIFIED',resource_policy_revision=resource_record())
        from aee.mcp_runtime.sandbox_policy import sandbox_record, FAILURE_REVISION
        body.update(sandbox_policy_revision=sandbox_record(), job_failure_semantics='VERIFIED',
                    job_failure_contract=FAILURE_REVISION)
        # Gate metadata fixture; real inherited cgroup validation is tested in R2.
        with patch('aee.mcp_runtime.runtime.verify_current_containment',return_value={}):
            require_deployment_ready(body)
        for variant in [{'provider_contract': None}, {'codex': {}}, {'operator_approval_id': ''},
                        {'native_tool_runtime_compatibility':'BLOCKED_CODE_MODE_ADDRESS_SPACE'},
                        {'resource_containment':'BLOCKED_RESOURCE_EVIDENCE_REQUIRED'},
                        {'resource_policy_revision':{'id':'unknown'}}, {'sandbox_policy_revision':None},
                        {'job_failure_semantics':'UNVERIFIED'}, {'job_failure_contract':'unknown'}]:
            with self.assertRaises(JobError):require_deployment_ready({**body, **variant})


@unittest.skipUnless(NATIVE.exists(), 'reviewed installed native CLI unavailable')
class R1Native(unittest.TestCase):
    def test_selected_native_matches_exact_actual_wire_fixture(self):
        result = probe_native(NATIVE)
        fixture = json.loads((FIXTURES/'codex-r1-wire-contracts.json').read_text())
        expected = next(row['result'] for row in fixture['after_policy'] if row['name'] == '159-selected')
        self.assertEqual(result, expected)
        self.assertTrue(result['terminal_valid'])
        self.assertNotIn('authorization', result['requests'][0]['header_names'])

    def test_old_p2c_flags_still_fail_closed(self):
        result = probe_native(NATIVE, ['agents.enabled=true', 'features.sleep_tool=true',
                                     'tools.experimental_request_user_input.enabled=true',
                                     'features.goals=true', 'features.view_image=true', 'model_catalog_json=null'])
        self.assertEqual(result['accepted_requests'], 0)
        self.assertEqual(result['upstream_requests'], 0)
        self.assertFalse(result['terminal_valid'])

    def test_task_stdin_and_remaining_code_mode_failure_are_not_false_success(self):
        transport = CodeModeTransport()
        result = probe_native(NATIVE, transport=transport)
        self.assertTrue(result['terminal_valid'])
        self.assertEqual(result['accepted_requests'], 2)
        self.assertEqual(transport.requests[0]['input'][-1]['content'][0]['text'], 'Return the fixture response.')
        outputs = [i for i in transport.requests[-1]['input'] if i.get('type') == 'custom_tool_call_output']
        self.assertEqual(len(outputs), 1)
        self.assertNotIn('R1 Code Mode fixture', json.dumps(outputs))
        # A historical low-AS host failure can surface either EOF or its
        # native SIGTRAP status, depending on supervisor observation timing.
        diagnostic = json.dumps(outputs)
        self.assertTrue('code-mode host closed its stdout' in diagnostic or
                        'code-mode host exited with status signal: 5 (SIGTRAP)' in diagnostic)
        # Historical profile reproduction: terminal SSE is not cell success.

    @unittest.skipUnless(NATIVE160.exists(), 'isolated installed comparison CLI unavailable')
    def test_160_equivalent_contract_without_candidate_upgrade(self):
        result = probe_native(NATIVE160)
        self.assertEqual(result['executable_sha256'],
                         '12eb3e81114588aca3b7998f4f19e8997b056aca08e57a7ca7c8a3ec8c652aad')
        self.assertEqual(result['requests'][0]['additional_tools_sha256'], [TOOLS_SHA256])
        self.assertTrue(result['terminal_valid'])


if __name__ == '__main__': unittest.main()
