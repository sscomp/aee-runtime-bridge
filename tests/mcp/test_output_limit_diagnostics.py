"""R5-R2: bounded, secret-free OUTPUT_LIMIT_EXCEEDED boundary diagnostics.

Additive only: every OUTPUT_LIMIT_EXCEEDED emission site now attaches a
scalar-only ``context`` dict to ``JobError`` naming the exact boundary
(executor raw-stream capture, file-size signal, final message, serialized
payload, store field/artifacts/record, workspace snapshot, operator manifest)
without retaining any raw payload, prompt, environment or credential value.
Classification, message text, limits and fail-closed semantics are unchanged;
unmeasured observations are null, never fabricated zeros.

The OTLP collector (B5) is deliberately NOT an emitter: it keeps its
ValueError/HTTP-400 fail-closed path, and the additive transport diagnostics
now also mirror the cumulative accepted/rejected payload bytes.
"""
import importlib.util
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aee.mcp_runtime.executor import final_agent_message
from aee.mcp_runtime.native_telemetry import TRANSPORT_SCHEMA_VERSION, Evidence
from aee.mcp_runtime.process import Limits, run_bounded
from aee.mcp_runtime.result_contract import validate_success
from aee.mcp_runtime.sandbox import snapshot_workspace
from aee.mcp_runtime.store import DIAGNOSTIC_SCHEMA, JobError, JobStore

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"

# Sibling test modules import their helpers the same non-package way.
sys.path.insert(0, str(Path(__file__).parent))
from completed_result_fixtures import successful_fields  # noqa: E402


def assert_diagnostic_shape(test, context, boundary=None):
    """Scalar-only, enum- and size-bounded; no free text can ride along."""
    test.assertIsInstance(context, dict)
    test.assertEqual(context["schema"], DIAGNOSTIC_SCHEMA)
    for key, value in context.items():
        test.assertIsInstance(value, (str, int, bool, type(None)), key)
        if isinstance(value, str):
            test.assertLessEqual(len(value), 48, key)
    if boundary is not None:
        test.assertEqual(context["failure_boundary"], boundary)
    # Unknown observations stay null; a zero is never substituted.
    if not context["observed_measured"]:
        test.assertIsNone(context["observed_bytes"])
        test.assertIsNone(context["observed_chars"])
        test.assertIsNone(context["observed_entries"])


class StreamCaptureDiagnostics(unittest.TestCase):
    """K01: independent per-stream raw capture limits and their context."""

    def run_child(self, code, limits):
        return run_bounded(["/usr/bin/python3", "-I", "-B", "-c", code],
                           env={}, stdin=b"", limits=limits)

    def test_below_and_exactly_at_limit_emit_no_diagnostics(self):
        for size in (900, 1024):
            result = self.run_child("import sys;sys.stdout.write('x'*%d)" % size,
                                    Limits(timeout=10, stdout_bytes=1024, stderr_bytes=1024))
            self.assertEqual(result.returncode, 0)
            self.assertEqual(len(result.stdout), size)
            self.assertEqual(len(result.stderr), 0)

    def test_above_limit_names_stream_and_counts_attempted_bytes(self):
        with self.assertRaises(JobError) as error:
            self.run_child("import sys;sys.stdout.write('x'*1025)",
                           Limits(timeout=10, stdout_bytes=1024, stderr_bytes=1024))
        self.assertEqual(error.exception.code, "OUTPUT_LIMIT_EXCEEDED")
        context = error.exception.context
        assert_diagnostic_shape(self, context, "executor_capture_stdout")
        self.assertEqual(context["source_stream"], "stdout")
        self.assertEqual(context["measurement_stage"], "raw_stream")
        self.assertEqual(context["limit_bytes"], 1024)
        self.assertEqual(context["observed_bytes"], 1025)
        self.assertTrue(context["observed_measured"])

    def test_stderr_has_its_own_boundary_and_stream(self):
        with self.assertRaises(JobError) as error:
            self.run_child("import sys;sys.stderr.write('e'*1025)",
                           Limits(timeout=10, stdout_bytes=1024, stderr_bytes=1024))
        context = error.exception.context
        assert_diagnostic_shape(self, context, "executor_capture_stderr")
        self.assertEqual(context["source_stream"], "stderr")
        self.assertEqual(context["limit_bytes"], 1024)
        self.assertEqual(context["observed_bytes"], 1025)

    def test_context_never_carries_payload_or_secret_text(self):
        # Synthetic fixture (WO-B-R1 R-a): placeholder-prefixed token, no real
        # credential; the "sk-" prefix was dropped so the secret scanner's
        # documented placeholder exemption applies instead of openai_key.
        token = "synthetic-credential-token-raw"
        with self.assertRaises(JobError) as error:
            self.run_child("import sys;sys.stdout.write('%s') ; sys.stdout.flush()" % (token * 200),
                           Limits(timeout=10, stdout_bytes=1024, stderr_bytes=1024))
        raw = json.dumps(error.exception.context)
        self.assertNotIn("synthetic-credential", raw)
        self.assertNotIn(token, raw)


class FinalMessageDiagnostics(unittest.TestCase):
    @staticmethod
    def stream(text):
        events = [
            b'{"type":"thread.started","thread_id":"thr_r5r2"}',
            b'{"type":"turn.started"}',
            b'{"type":"item.completed","item":{"type":"agent_message","text":'
            + json.dumps(text).encode() + b"}}",
            b'{"type":"turn.completed"}',
        ]
        return b"\n".join(events) + b"\n"

    def test_exactly_at_limit_is_accepted(self):
        message = "x" * 64
        self.assertEqual(final_agent_message(self.stream(message),
                                             Limits(result_bytes=64)), message)

    def test_above_limit_utf8_measured(self):
        with self.assertRaises(JobError) as error:
            final_agent_message(self.stream("é" * 40), Limits(result_bytes=64))
        self.assertEqual(error.exception.code, "OUTPUT_LIMIT_EXCEEDED")
        context = error.exception.context
        assert_diagnostic_shape(self, context, "native_final_message")
        self.assertEqual(context["measurement_stage"], "final_message_utf8")
        self.assertEqual(context["measurement_unit"], "utf8_bytes")
        self.assertEqual(context["limit_bytes"], 64)
        self.assertEqual(context["observed_bytes"], 80)  # 40 two-byte chars, measured
        self.assertTrue(context["observed_measured"])

    def test_malformed_payload_fails_closed_without_fabricated_diagnostics(self):
        for blob in [b"{not json}\n", b"123\n", b'{"type":"item.completed"}\n']:
            with self.assertRaises(JobError) as error:
                final_agent_message(blob, Limits())
            self.assertEqual(error.exception.code, "EXECUTION_FAILED")
            self.assertIsNone(error.exception.context)

    def test_required_tool_failure_semantics_and_no_context(self):
        # A recovered command failure is a tool observation, not a verdict.
        recovered = [
            b'{"type":"thread.started","thread_id":"thr_probe_1"}',
            b'{"type":"turn.started"}',
            b'{"type":"item.completed","item":{"type":"command_execution","exit_code":2,'
            b'"aggregate_output":"raw command output must not be retained"}}',
            b'{"type":"turn.completed"}',
            b'{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
        ]
        self.assertEqual(final_agent_message(b"\n".join(recovered) + b"\n", Limits()), "done")
        # Ambiguous command evidence (failed with no exit code) stays fail-shot
        # and carries no output-limit context.
        ambiguous = [
            b'{"type":"thread.started","thread_id":"thr_probe_1"}',
            b'{"type":"turn.started"}',
            b'{"type":"item.completed","item":{"type":"command_execution","status":"failed"}}',
            b'{"type":"turn.completed"}',
            b'{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
        ]
        with self.assertRaises(JobError) as error:
            final_agent_message(b"\n".join(ambiguous) + b"\n", Limits())
        self.assertEqual(error.exception.code, "REQUIRED_TOOL_FAILED")
        self.assertEqual(error.exception.exit_code, 0)  # fail-shot, not process exit
        self.assertIsNone(error.exception.context)
        self.assertFalse(error.exception.failure["recovery_permitted"])
        self.assertFalse(error.exception.failure["failing_event"]["exit_code"])


class StoreRecordDiagnostics(unittest.TestCase):
    """K02: field contract, artifacts contract and whole-record serialization."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aee-r5r2-store-")
        self.addCleanup(self.tmp.cleanup)
        self.store = JobStore(Path(self.tmp.name))
        self.addCleanup(self.store.close)
        self.job = self.store.create("codex", "fixture task", str(Path(self.tmp.name)), "read_only")

    def test_field_contract_exceeds_char_limit(self):
        with self.assertRaises(JobError) as error:
            self.store.update(self.job["job_id"], summary="x" * 4001)
        context = error.exception.context
        assert_diagnostic_shape(self, context, "store_field_contract")
        self.assertEqual(context["measurement_unit"], "chars")
        self.assertEqual(context["limit_chars"], 4000)
        self.assertEqual(context["observed_chars"], 4001)  # exactly one over
        self.assertTrue(context["observed_measured"])

    def test_field_with_wrong_type_reports_no_fabricated_metrics(self):
        with self.assertRaises(JobError) as error:
            self.store.update(self.job["job_id"], error=123)
        context = error.exception.context
        assert_diagnostic_shape(self, context, "store_field_contract")
        self.assertIsNone(context["observed_chars"])  # not countable: null, not zero
        self.assertFalse(context["observed_measured"])
        self.assertIsNone(context["limit_bytes"])
        self.assertEqual(context["limit_chars"], 500)

    def test_artifacts_contract_never_invents_sizes(self):
        with self.assertRaises(JobError) as error:
            self.store.update(self.job["job_id"], artifacts=["unexpected"])
        context = error.exception.context
        assert_diagnostic_shape(self, context, "store_artifacts_contract")
        self.assertIsNone(context["observed_bytes"])
        self.assertIsNone(context["limit_bytes"])
        self.assertFalse(context["observed_measured"])

    def test_whole_record_overflow_is_measured_at_serialization(self):
        with self.assertRaises(JobError) as error:
            self.store.update(self.job["job_id"], execution={"synthetic": "y" * 70000})
        context = error.exception.context
        assert_diagnostic_shape(self, context, "store_record_serialize")
        self.assertEqual(context["measurement_stage"], "record_json_utf8")
        self.assertEqual(context["measurement_unit"], "utf8_bytes")
        self.assertEqual(context["limit_bytes"], 65536)
        self.assertGreater(context["observed_bytes"], 65536)
        self.assertTrue(context["observed_measured"])

    def test_successful_update_stays_within_and_carries_no_diagnostics(self):
        self.store.update(self.job["job_id"], summary="preserved", exit_code=0,
                          finished_at=self.job["created_at"])
        data = self.store.get(self.job["job_id"])
        self.assertEqual(data["summary"], "preserved")
        self.assertNotIn("output_limit_diagnostics", data.get("execution") or {})


class WorkspaceSnapshotDiagnostics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aee-r5r2-snapshot-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "source"
        self.workspace.mkdir()
        self.dest = self.root / "destination"
        self.dest.mkdir()

    def test_single_oversized_file_boundary(self):
        (self.workspace / "big.bin").write_bytes(b"x" * (4 * 1024**2 + 1))
        with self.assertRaises(JobError) as error:
            snapshot_workspace(self.workspace, {"big.bin": "a" * 64}, self.dest)
        context = error.exception.context
        assert_diagnostic_shape(self, context, "workspace_snapshot_file")
        self.assertEqual(context["measurement_unit"], "raw_bytes")
        self.assertEqual(context["limit_bytes"], 4 * 1024**2)
        self.assertEqual(context["observed_bytes"], 4 * 1024**2 + 1)
        self.assertTrue(context["observed_measured"])

    def test_cumulative_workspace_total_boundary_bounded_run(self):
        from aee.mcp_runtime import sandbox
        entries = {}
        for name in ("a.bin", "b.bin"):
            (self.workspace / name).write_bytes(b"x" * 600)
            entries[name] = hashlib.sha256(b"x" * 600).hexdigest()
        with patch.object(sandbox, "MAX_WORKSPACE_BYTES", 1024):
            with self.assertRaises(JobError) as error:
                snapshot_workspace(self.workspace, entries, self.dest)
        self.assertEqual(error.exception.code, "OUTPUT_LIMIT_EXCEEDED")
        context = error.exception.context
        assert_diagnostic_shape(self, context, "workspace_snapshot_total")
        self.assertEqual(context["limit_bytes"], 1024)
        self.assertEqual(context["observed_bytes"], 1200)
        self.assertTrue(context["observed_measured"])


class CompletedRecordHygiene(unittest.TestCase):
    def _record(self, execution):
        record = {"job_id": "A3JOB-20260101-" + "0" * 32, "agent": "codex", "status": "completed",
                  "task": "fixture", "working_directory": "/fixture", "mode": "read_only",
                  "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
                  "started_at": "2026-01-01T00:00:00+00:00", "finished_at": "2026-01-01T00:00:01+00:00",
                  "exit_code": 0, "error_code": None, "error": None, "artifacts": [],
                  "log_excerpt": None, "truncated": False}
        record.update(successful_fields(execution=execution))
        return record

    def test_completed_record_must_not_carry_failure_diagnostics(self):
        context = {"schema": DIAGNOSTIC_SCHEMA, "failure_boundary": "executor_capture_stdout"}
        with self.assertRaises(JobError) as error:
            validate_success(self._record({"output_limit_diagnostics": context}), persisted=False)
        self.assertEqual(error.exception.code, "INVALID_TERMINAL_STATE")


class CollectorBudgetSeparation(unittest.TestCase):
    """B5 stays a fail-closed ValueError path, now with mirrored byte totals."""

    def test_budget_rejects_fail_shut_and_mirror_cumulative_bytes(self):
        with patch("aee.mcp_runtime.native_telemetry.MAX_TOTAL", 4096), \
             patch.dict(os.environ, {"AEE_TRANSPORT_DIAGNOSTICS": "1"}):
            evidence = Evidence()
            first = ('{"resourceSpans":[{"scopeSpans":[{"spans":[{"name":"codex.exec",'
                     '"startTimeUnixNano":"100","endTimeUnixNano":"200"}]}]}]}').encode()
            evidence.accept("/v1/traces", first)  # inside budget: accepted, no raise
            second = b'{"resourceSpans":[],"pad":"' + b"X" * 5000 + b'"}'
            with self.assertRaises(ValueError) as error:
                evidence.accept("/v1/traces", second)
            self.assertNotIsInstance(error.exception, JobError)
            self.assertTrue(evidence.invalid)
            receipt = evidence.receipt()
        diagnostics = receipt.get("transport_diagnostics") or {}
        self.assertEqual(receipt["complete"], False)
        reasons = {item["predicate_id"] for item in receipt.get("incomplete_reasons", [])}
        self.assertIn("collector_invalid", reasons)
        self.assertEqual(diagnostics.get("schema_version"), TRANSPORT_SCHEMA_VERSION)
        totals = diagnostics.get("totals", {})
        self.assertEqual(totals.get("total_payload_bytes"), len(first) + len(second))
        self.assertEqual(totals.get("invalid_sites", {}).get("budget"), 1)
        budget = [event for event in diagnostics.get("events", [])
                  if event.get("phase") == "budget" and event.get("reason_code") == "TOTAL_BUDGET"]
        self.assertTrue(budget)
        raw = json.dumps(receipt)
        self.assertNotIn(b"X" * 64, raw.encode())  # no payload text rides along


class GatewayPersistenceDiagnostics(unittest.TestCase):
    def setUp(self):
        from aee.mcp_runtime.provider_contract import contract_record
        from aee.mcp_runtime.resource_policy import resource_record
        from aee.mcp_runtime.sandbox_policy import sandbox_record, FAILURE_REVISION
        from aee.mcp_runtime.profiles import resolve_codex
        self.tmp = tempfile.TemporaryDirectory(prefix="aee-r5r2-gateway-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.binary = self.root / "codex"
        self.source = self.root / "source"
        self.source.mkdir()
        subprocess.run(["/usr/bin/gcc", "-O2", str(FIXTURES / "codex_probe.c"),
                        "-o", str(self.binary)], check=True)
        companion = self.root / "codex-code-mode-host"
        companion.write_bytes(self.binary.read_bytes())
        companion.chmod(0o755)
        identity = resolve_codex(str(self.binary))
        self.policy = {"schema": 1, "config_schema": "aee-p2c-1", "source_commit": "a" * 40,
                       "execution_profile": "codex-readonly-high",
                       "egress": "per-job-uds-responses-broker",
                       "codex": {"path": str(self.binary), "version": identity.version,
                                 "sha256": identity.sha256,
                                 "code_mode_host": {"path": str(companion),
                                                    "sha256": identity.sha256}},
                       "native_provider_compatibility": "VERIFIED",
                       "deployment_status": "APPROVED_STAGE2",
                       "operator_approval_id": "synthetic-fixture-only",
                       "native_tool_runtime_compatibility": "VERIFIED",
                       "resource_containment": "VERIFIED"}
        self.policy["provider_contract"] = contract_record()
        self.policy["resource_policy_revision"] = resource_record()
        self.policy.update(sandbox_policy_revision=sandbox_record(), job_failure_semantics="VERIFIED",
                           job_failure_contract=FAILURE_REVISION)
        containment = patch("aee.mcp_runtime.runtime.verify_current_containment", return_value={})
        containment.start()
        self.addCleanup(containment.stop)
        pin = patch("aee.mcp_runtime.runtime.matches_reviewed_executor", return_value=True)
        pin.start()
        self.addCleanup(pin.stop)
        self.manifest = self.root / "deployment.json"
        self.manifest.write_text(json.dumps(self.policy))
        workspace = self.root / "workspace.json"
        workspace.write_text("{}")
        env = {"AEE_RUNTIME_MANIFEST": str(self.manifest),
               "AEE_BROKER_CONTROL": str(self.root / "control.sock"),
               "A3_JOB_STORE_DIR": str(self.root / "jobs"),
               "A3_CODEX_BIN": str(self.binary),
               "A3_WORKSPACE_MANIFEST": str(workspace),
               "A3_DISPATCH_ALLOWED_ROOTS": str(self.source),
               "AEE_MCP_PORT": "8791", "AEE_MCP_SURFACE": "restricted",
               "MCP_BRIDGE_API_KEY": "fixture-only-auth", "AEE_MCP_REQUIRE_AUTH": "true"}
        self.environment = patch.dict(os.environ, env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        name = "aee_r5r2_fixture_" + str(len(sys.modules))
        spec = importlib.util.spec_from_file_location(name, ROOT / "mcp_gateway.py")
        self.gateway = importlib.util.module_from_spec(spec)
        sys.modules[name] = self.gateway
        self.addCleanup(sys.modules.pop, name, None)
        spec.loader.exec_module(self.gateway)
        self.addCleanup(self.gateway.JOB_STORE.close)

    def dispatch(self):
        with patch.object(self.gateway, "_start_job_worker"):
            return self.gateway.dispatch_job("codex", "synthetic inspection", str(self.source), "read_only")

    def test_failed_record_carries_bounded_boundary_context(self):
        job = self.dispatch()
        context = {"schema": DIAGNOSTIC_SCHEMA, "failure_boundary": "executor_capture_stdout",
                   "measurement_stage": "raw_stream", "limit_bytes": 65536,
                   "observed_bytes": 65537, "observed_measured": True}
        calls = []

        def broker(path, action, job_id):
            calls.append((action, job_id))
            return {"socket": str(self.root / "jobs" / (job_id + ".sock")), "ok": True}

        with patch.object(self.gateway, "broker_control", side_effect=broker), \
             patch.object(self.gateway, "execute_codex", side_effect=JobError(
                 "OUTPUT_LIMIT_EXCEEDED", "Executor output exceeded byte limit", context=context)):
            self.gateway.run_codex_job(job["job_id"], "synthetic inspection", str(self.source))
        self.assertEqual(calls, [("lease", job["job_id"]), ("revoke", job["job_id"])])
        record = self.gateway.JOB_STORE.get(job["job_id"])
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error_code"], "OUTPUT_LIMIT_EXCEEDED")
        stored = record["execution"]["output_limit_diagnostics"]
        assert_diagnostic_shape(self, stored, "executor_capture_stdout")
        self.assertEqual(stored["observed_bytes"], 65537)
        # The MCP response stays unchanged: code and bounded message only.
        payload = self.gateway.job_error_payload(
            JobError("OUTPUT_LIMIT_EXCEEDED", "Executor output exceeded byte limit",
                     context=context))
        self.assertEqual(set(payload), {"ok", "error_code", "error"})
        self.assertNotIn("output_limit_diagnostics", json.dumps(payload))

    def test_injected_context_is_sanitized_at_the_single_guard(self):
        poisoned = {"schema": DIAGNOSTIC_SCHEMA, "failure_boundary": "executor_capture_stdout",
                    "injected_secret": "sk-synthetic", "observed_bytes": 7,
                    "raw_note": "x" * 500}
        error = JobError("OUTPUT_LIMIT_EXCEEDED", "Executor output exceeded byte limit",
                         context=poisoned)
        self.assertNotIn("injected_secret", error.context)
        self.assertNotIn("raw_note", error.context)
        self.assertNotIn("sk-synthetic", json.dumps(error.context))


if __name__ == "__main__":
    unittest.main()