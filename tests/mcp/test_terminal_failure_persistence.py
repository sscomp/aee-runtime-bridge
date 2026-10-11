"""K02-R1 remediation: deterministic bounded terminal-failure persistence.

An oversized terminal-failed JobStore record (> 65536 serialized UTF-8 bytes)
must persist durably with its original ``error_code``, failure boundary and
explicit measured truncation evidence — never fall through to the reconcile
``EXECUTION_INTERRUPTED`` fallback when a reduced write can succeed, never
become a partial write, never leak secret-shaped content through the
truncation marker. Reduction applies only to terminal unsuccessful statuses,
is bounded, deterministic, at most one attempt per tier, and never mutates
the caller's dictionaries. The 65536-byte cap and all fail-closed behavior
are unchanged.

Tier contract tested here:
  tier 0: unchanged first attempt (no reducer call for fitting records);
  tier 1: validated diagnostics retained + bounded dropped-key evidence;
  tier 2: full truncation-evidence marker (executed when tier 1 still
          overflows or retained nothing validated);
  tier 3: minimal marker fallback; beyond it the original
          OUTPUT_LIMIT_EXCEEDED raise is preserved (fail closed).

Offline only: every store lives in a private temp directory.
"""
import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from aee.mcp_runtime import store as store_module
from aee.mcp_runtime.result_contract import seal_record, verify_integrity
from aee.mcp_runtime.store import (JobStore, JobError, MAX_RECORD_BYTES,
                                   MAX_TRUNCATION_MARKER_BYTES,
                                   output_limit_context)

SENTINEL = "AKIA-EXAMPLE-SENTINEL-0123456789abcdef"
FILLER = "F" * (MAX_RECORD_BYTES + 4096)


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def persisted_record(root, job_id):
    return json.loads((root / f"{job_id}.json").read_text())


def diagnostics(observed):
    return output_limit_context('store_record_serialize', 'record_json_utf8',
                                'utf8_bytes', limit=MAX_RECORD_BYTES,
                                observed=observed, measured=True)


def measured_record_bytes(record, fields, execution):
    """Recompute the serialized size the store will measure for one record.

    Replicates the write path's ordering (update fields, seal, json) purely
    for assert purposes; it is not part of the system under test's contract.
    """
    candidate = copy.deepcopy(record)
    candidate.update(fields)
    candidate["execution"] = execution
    candidate["updated_at"] = record["updated_at"]
    candidate["finished_at"] = record["updated_at"]
    seal_record(candidate)
    return len(json.dumps(candidate, ensure_ascii=False).encode())


class TerminalPersistenceBase(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="aee-k02r1-"))
        self.root = self.base / "store"
        self.store = JobStore(self.root)
        job = self.store.create("codex", "K02-R1 remediation task", str(self.root), "read_only")
        self.jid = job["job_id"]
        self.store.update(self.jid, status="running",
                          started_at=self.store.get(self.jid)["updated_at"])
        self.record_path = self.root / f"{self.jid}.json"
        self.failure_fields = {"error_code": "OUTPUT_LIMIT_EXCEEDED",
                               "error": "K02-R1 remediation probe", "summary": "probe"}

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.base, ignore_errors=True)

    def terminal_update(self, execution):
        """Attempt the terminal failed write; return the raised JobError or None."""
        try:
            return self.store.update(
                self.jid, status="failed",
                finished_at=self.store.get(self.jid)["updated_at"],
                error_code=self.failure_fields["error_code"],
                error=self.failure_fields["error"], summary=self.failure_fields["summary"],
                execution=execution)
        except JobError as error:
            return error

    def successful_terminal_update(self, execution):
        """Attempt the terminal failed write, requiring success."""
        result = self.terminal_update(execution)
        self.assertNotIsInstance(result, JobError,
                                 msg=f"unexpected raise: {getattr(result, 'code', '')}")
        return result

    def reducer_spy(self):
        """Wrap the reducer with a counting passthrough: records tier values."""
        original = JobStore._reduced_terminal_failure
        calls = []
        def counting(inner, data, error, tier, first_overflow, original_keys=None):
            calls.append(tier)
            return original(inner, data, error, tier, first_overflow, original_keys)
        return mock.patch.object(JobStore, "_reduced_terminal_failure", counting), calls

    def assert_seal(self, persisted):
        verify_integrity(dict(persisted))


class NegativeControl(TerminalPersistenceBase):
    def test_normal_failure_persists_unchanged_and_reducer_never_called(self):
        execution = {"output_limit_diagnostics": diagnostics(MAX_RECORD_BYTES + 11),
                     "isolation": {"egress": "fixed-openai-responses-broker"}}
        patcher, calls = self.reducer_spy()
        with patcher:
            returned = self.successful_terminal_update(execution)
        self.assertEqual(calls, [], "reducer must not run for a fitting record")
        persisted = persisted_record(self.root, self.jid)
        self.assertEqual(persisted["status"], "failed")
        self.assertEqual(persisted["error_code"], "OUTPUT_LIMIT_EXCEEDED")
        self.assertEqual(persisted["error"], "K02-R1 remediation probe")
        self.assertEqual(persisted["execution"], execution)
        self.assertNotIn("terminal_write_truncated", persisted["execution"])
        self.assertIsNotNone(persisted.get("integrity"))
        self.assertEqual(returned, persisted)
        self.assertLessEqual(self.record_path.stat().st_size, MAX_RECORD_BYTES)
        self.assert_seal(persisted)

    def test_tier0_path_is_byte_identical_to_reducer_disabled(self):
        """Same fixture, same fixed clock: the fitting-record path is
        byte-for-byte what the pre-remediation (reducer disabled) path wrote."""
        execution = {"output_limit_diagnostics": diagnostics(MAX_RECORD_BYTES + 11)}
        records = []
        for with_reducer in (False, True):
            clock = "2026-10-12T00:00:00.000000+00:00"
            root = self.base / ("reduced" if with_reducer else "base")
            root.mkdir()
            working_directory = "/aee-k02r1-equivalence-wd"  # identical for both
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(store_module, "now",
                                                      lambda: clock))
                stack.enter_context(mock.patch.object(store_module.secrets, "token_hex",
                                                      lambda n: "ab" * 16))
                if not with_reducer:
                    stack.enter_context(mock.patch.object(
                        JobStore, "_reduced_terminal_failure",
                        lambda inner, *args, **kwargs: None))
                instance = JobStore(root)
                try:
                    job = instance.create("codex", "equivalence task",
                                          working_directory, "read_only")
                    jid = job["job_id"]
                    instance.update(jid, status="running", started_at=clock)
                    instance.update(jid, status="failed", finished_at=clock,
                                    error_code="OUTPUT_LIMIT_EXCEEDED",
                                    error="equivalence probe", summary="probe",
                                    execution=execution)
                    records.append((root / f"{jid}.json").read_bytes())
                finally:
                    instance.close()
        self.assertEqual(records[0], records[1])


class TierOne(TerminalPersistenceBase):
    def test_oversized_execution_keeps_validated_diagnostics_and_original_code(self):
        observed = MAX_RECORD_BYTES + 4201
        execution = {"output_limit_diagnostics": diagnostics(observed),
                     "synthetic_payload_filler": FILLER}
        expected_overflow = measured_record_bytes(
            self.store.get(self.jid),
            {"status": "failed", **self.failure_fields,
             "finished_at": self.store.get(self.jid)["updated_at"]},
            execution)
        patcher, calls = self.reducer_spy()
        with patcher:
            returned = self.successful_terminal_update(execution)
        self.assertEqual(calls, [0])
        persisted = persisted_record(self.root, self.jid)
        self.assertEqual(persisted["error_code"], "OUTPUT_LIMIT_EXCEEDED")
        self.assertEqual(persisted["status"], "failed")
        self.assertEqual(persisted["error"], "K02-R1 remediation probe")
        on_disk = persisted["execution"]
        self.assertEqual(on_disk["output_limit_diagnostics"], diagnostics(observed))
        self.assertEqual(on_disk["execution_keys_dropped"], ["synthetic_payload_filler"])
        self.assertTrue(on_disk["terminal_write_truncated"])
        self.assertEqual(on_disk["record_json_utf8_overflow_bytes"], expected_overflow)
        self.assertGreater(on_disk["record_json_utf8_overflow_bytes"], MAX_RECORD_BYTES)
        self.assertLessEqual(self.record_path.stat().st_size, MAX_RECORD_BYTES)
        self.assert_seal(persisted)
        self.assertEqual(returned, persisted)

    def test_native_tool_failure_validated_shape_is_retained(self):
        failure = {"contract": "codex-native-otlp-r3-1", "evidence_complete": False,
                   "condition": "receipt_missing_or_malformed"}
        execution = {"native_tool_failure": failure, "synthetic_payload_filler": FILLER}
        patcher, calls = self.reducer_spy()
        with patcher:
            self.successful_terminal_update(execution)
        self.assertEqual(calls, [0])
        persisted = persisted_record(self.root, self.jid)
        self.assertEqual(persisted["execution"].get("native_tool_failure"), failure)
        self.assertEqual(persisted["error_code"], "OUTPUT_LIMIT_EXCEEDED")
        self.assert_seal(persisted)

    def test_invalid_oversized_diagnostics_fall_through_to_the_marker(self):
        execution = {"output_limit_diagnostics": {"unknown_key": "s" * 4096},
                     "synthetic_payload_filler": FILLER}
        patcher, calls = self.reducer_spy()
        with patcher:
            self.successful_terminal_update(execution)
        # Nothing validated was retained, so the tier-1 candidate already
        # carries the full truncation-evidence marker; one tier sufficed.
        self.assertEqual(calls, [0])
        persisted = persisted_record(self.root, self.jid)
        on_disk = persisted["execution"]
        self.assertTrue(on_disk["terminal_write_truncated"])
        self.assertNotIn("output_limit_diagnostics", on_disk)
        self.assertNotIn("unknown_key", json.dumps(on_disk))
        self.assertEqual(on_disk["execution_keys_dropped"],
                         ["output_limit_diagnostics", "synthetic_payload_filler"])
        self.assertEqual(persisted["error_code"], "OUTPUT_LIMIT_EXCEEDED")
        self.assert_seal(persisted)


class TierTwo(TerminalPersistenceBase):
    def test_marker_replaces_oversized_execution_without_content_leak(self):
        oversized_diag = {"unknown_key": "s" * MAX_RECORD_BYTES,
                          "secret_looking": SENTINEL}
        execution = {"output_limit_diagnostics": oversized_diag,
                     "raw": FILLER, "synthetic_payload_filler": FILLER}
        self.successful_terminal_update(execution)
        raw = self.record_path.read_bytes()
        persisted = json.loads(raw)
        on_disk = persisted["execution"]
        self.assertTrue(on_disk["terminal_write_truncated"])
        self.assertEqual(on_disk["execution_keys_dropped"],
                         ["output_limit_diagnostics", "raw", "synthetic_payload_filler"])
        self.assertNotIn("execution_keys_omitted_count", on_disk)
        self.assertGreater(on_disk["record_json_utf8_overflow_bytes"], MAX_RECORD_BYTES)
        self.assertLessEqual(len(json.dumps(on_disk, ensure_ascii=False).encode()),
                             MAX_TRUNCATION_MARKER_BYTES)
        self.assertEqual(persisted["error_code"], "OUTPUT_LIMIT_EXCEEDED")
        raw.decode("utf-8")  # persisted record remains valid UTF-8 JSON
        self.assertNotIn(SENTINEL.encode(), raw)
        self.assertNotIn(FILLER[:128].encode(), raw)
        self.assertNotIn(b"unknown_key", raw)
        self.assert_seal(persisted)

    def test_secret_shaped_key_names_are_never_persisted(self):
        execution = {"AWS_SECRET_ACCESS_KEY": FILLER,
                     "ANTHROPIC_AUTH_TOKEN-value": FILLER,
                     "ok_name-1.x": FILLER, "password": FILLER}
        self.terminal_update(execution)
        persisted = persisted_record(self.root, self.jid)
        on_disk = persisted["execution"]
        self.assertEqual(on_disk["execution_keys_dropped"], ["ok_name-1.x"])
        self.assertEqual(on_disk["execution_keys_omitted_count"], 3)
        raw = self.record_path.read_bytes().decode("utf-8")
        for secret_name in ("AWS_SECRET_ACCESS_KEY", "ANTHROPIC_AUTH_TOKEN-value",
                            "password"):
            self.assertNotIn(secret_name, raw)
        self.assertEqual(persisted["error_code"], "OUTPUT_LIMIT_EXCEEDED")

    def test_update_returns_the_persisted_reduced_record(self):
        execution = {"output_limit_diagnostics": {"unknown_key": "s" * MAX_RECORD_BYTES},
                     "synthetic_payload_filler": FILLER}
        returned = self.successful_terminal_update(execution)
        persisted = persisted_record(self.root, self.jid)
        self.assertEqual(returned, persisted)


class TierThree(TerminalPersistenceBase):
    def _marker_sizes(self, names):
        """(full, minimal) sealed record sizes for the given dropped names."""
        record = self.store.get(self.jid)
        fields = {"status": "failed", **self.failure_fields}
        full = store_module._truncation_marker(999999, names, 0)
        minimal = {"terminal_write_truncated": True,
                   "record_json_utf8_overflow_bytes": 999999}
        return (measured_record_bytes(record, fields, full),
                measured_record_bytes(record, fields, minimal))

    def test_minimal_marker_persists_when_full_marker_overflows(self):
        names = [chr(97 + index) * 48 for index in range(8)]
        size_full, size_minimal = self._marker_sizes(names)
        cap = size_minimal + 16
        self.assertGreater(size_full, cap)
        execution = {name: FILLER for name in names}
        with mock.patch.object(store_module, "MAX_RECORD_BYTES", cap):
            patcher, calls = self.reducer_spy()
            with patcher:
                self.successful_terminal_update(execution)
        self.assertEqual(calls, [0, 1, 2])
        persisted = persisted_record(self.root, self.jid)
        self.assertEqual(
            persisted["execution"],
            {"terminal_write_truncated": True,
             "record_json_utf8_overflow_bytes":
                 persisted["execution"]["record_json_utf8_overflow_bytes"]})
        self.assertGreater(persisted["execution"]["record_json_utf8_overflow_bytes"], cap)
        self.assertEqual(persisted["error_code"], "OUTPUT_LIMIT_EXCEEDED")
        self.assertLessEqual(self.record_path.stat().st_size, cap)
        self.assert_seal(persisted)

    def test_beyond_tier3_fails_closed_without_masking_or_partial_write(self):
        execution = {"output_limit_diagnostics": diagnostics(999999),
                     "synthetic_payload_filler": FILLER}
        running_size = self.record_path.stat().st_size
        minimal_size = measured_record_bytes(
            self.store.get(self.jid),
            {"status": "failed", **self.failure_fields,
             "finished_at": self.store.get(self.jid)["updated_at"]},
            {"terminal_write_truncated": True,
             "record_json_utf8_overflow_bytes": 999999})
        cap = running_size + (minimal_size - running_size) // 2
        if minimal_size - running_size < 8:
            self.skipTest("minimal fallback indistinguishable from running record")
        pre_hash = file_sha(self.record_path)
        with mock.patch.object(store_module, "MAX_RECORD_BYTES", cap):
            error = self.terminal_update(execution)
        self.assertIsInstance(error, JobError)
        self.assertEqual(error.code, "OUTPUT_LIMIT_EXCEEDED")
        self.assertEqual(error.context.get("failure_boundary"), "store_record_serialize")
        self.assertEqual(file_sha(self.record_path), pre_hash)
        got = self.store.get(self.jid)
        self.assertEqual(got["status"], "running")
        # Documented fail-closed fallback: lease release + reconcile still
        # applies today's EXECUTION_INTERRUPTED rewrite; there is no false
        # success and no fabricated durable record.
        self.store.release(self.jid)
        reconciled = self.store.get(self.jid)
        self.assertEqual(reconciled["error_code"], "EXECUTION_INTERRUPTED")


class ReconcileAndStateHygiene(TerminalPersistenceBase):
    def test_reconcile_never_masks_a_reduced_terminal_failure(self):
        execution = {"output_limit_diagnostics": {"unknown_key": "s" * 8192},
                     "synthetic_payload_filler": FILLER}
        self.successful_terminal_update(execution)
        pre_hash = file_sha(self.record_path)
        self.store.release(self.jid)
        got = self.store.get(self.jid)
        self.assertEqual(got["error_code"], "OUTPUT_LIMIT_EXCEEDED")
        self.assertEqual(got["status"], "failed")
        self.assertTrue(got["execution"]["terminal_write_truncated"])
        self.assertEqual(file_sha(self.record_path), pre_hash)

    def test_second_store_instance_reads_the_reduced_record_without_rewrite(self):
        execution = {"output_limit_diagnostics": {"unknown_key": "s" * 8192},
                     "synthetic_payload_filler": FILLER}
        self.successful_terminal_update(execution)
        pre_hash = file_sha(self.record_path)
        other = JobStore(self.root)
        try:
            got = other.get(self.jid)
            self.assertEqual(got["error_code"], "OUTPUT_LIMIT_EXCEEDED")
            self.assertTrue(got["execution"]["terminal_write_truncated"])
            self.assert_seal(got)
        finally:
            other.close()
        self.assertEqual(file_sha(self.record_path), pre_hash)

    def test_terminal_lease_is_released_and_rewrites_rejected(self):
        execution = {"synthetic_payload_filler": FILLER}
        self.successful_terminal_update(execution)
        with self.assertRaises(JobError) as caught:
            self.store.update(self.jid, status="failed", error_code="x")
        self.assertEqual(caught.exception.code, "JOB_STATE_CONFLICT")

    def test_completed_path_never_reduces(self):
        execution = {"synthetic_payload_filler": FILLER}
        patcher, calls = self.reducer_spy()
        with patcher:
            with self.assertRaises(JobError) as caught:
                self.store.update(self.jid, status="completed",
                                  finished_at=self.store.get(self.jid)["updated_at"],
                                  exit_code=0, summary="x", execution=execution)
        self.assertEqual(caught.exception.code, "INVALID_TERMINAL_STATE")
        self.assertEqual(calls, [])

    def test_field_contract_overflow_is_not_reduced(self):
        patcher, calls = self.reducer_spy()
        with patcher:
            with self.assertRaises(JobError) as caught:
                self.store.update(self.jid, status="failed", summary="s" * 4001)
        self.assertEqual(caught.exception.code, "OUTPUT_LIMIT_EXCEEDED")
        self.assertEqual(caught.exception.context.get("failure_boundary"),
                         "store_field_contract")
        self.assertEqual(calls, [0], "reducer consulted but must refuse to act")
        got = self.store.get(self.jid)
        self.assertEqual(got["status"], "running")
        self.assertNotIn("terminal_write_truncated", got.get("execution") or {})


class Boundary(TerminalPersistenceBase):
    def _execution_fitting_record_at(self, target_bytes, filler_char="F"):
        """Execution filler landing the sealed record at an exact byte size."""
        record = self.store.get(self.jid)
        fields = {"status": "failed", **self.failure_fields}
        probe = copy.deepcopy(record)
        probe.update(fields)
        probe["execution"] = {"synthetic_payload_filler": filler_char * 1}
        probe["updated_at"] = record["updated_at"]
        probe["finished_at"] = record["updated_at"]
        seal_record(probe)
        base = len(json.dumps(probe, ensure_ascii=False).encode())
        pad = target_bytes - base  # each extra filler char is exactly one byte
        if pad < 0:
            self.skipTest("target below the record floor")
        return {"synthetic_payload_filler": filler_char * (1 + pad)}

    def test_exactly_at_limit_persists_without_reduction(self):
        execution = self._execution_fitting_record_at(MAX_RECORD_BYTES)
        patcher, calls = self.reducer_spy()
        with patcher:
            self.successful_terminal_update(execution)
        self.assertEqual(calls, [])
        self.assertEqual(self.record_path.stat().st_size, MAX_RECORD_BYTES)
        persisted = persisted_record(self.root, self.jid)
        self.assertEqual(persisted["error_code"], "OUTPUT_LIMIT_EXCEEDED")
        self.assertNotIn("terminal_write_truncated", persisted["execution"])
        self.assert_seal(persisted)

    def test_one_byte_over_triggers_measured_reduction(self):
        execution = self._execution_fitting_record_at(MAX_RECORD_BYTES + 1)
        patcher, calls = self.reducer_spy()
        with patcher:
            self.successful_terminal_update(execution)
        self.assertEqual(calls, [0])
        persisted = persisted_record(self.root, self.jid)
        self.assertEqual(persisted["execution"]["record_json_utf8_overflow_bytes"],
                         MAX_RECORD_BYTES + 1)
        self.assertTrue(persisted["execution"]["terminal_write_truncated"])

    def test_multibyte_utf8_overflow_is_measured_in_bytes(self):
        # The same character count as a fitting ASCII filler, but three bytes
        # per character pushes the serialized record past the byte cap.
        fitting = self._execution_fitting_record_at(MAX_RECORD_BYTES)
        chars = len(fitting["synthetic_payload_filler"])
        execution = {"synthetic_payload_filler": "ℵ" * chars}
        patcher, calls = self.reducer_spy()
        with patcher:
            self.successful_terminal_update(execution)
        self.assertEqual(calls, [0])
        persisted = persisted_record(self.root, self.jid)
        self.assertGreater(persisted["execution"]["record_json_utf8_overflow_bytes"],
                           MAX_RECORD_BYTES)
        self.assertEqual(persisted["error_code"], "OUTPUT_LIMIT_EXCEEDED")


class WriteFailureInjection(TerminalPersistenceBase):
    def test_persistent_write_failure_reports_no_false_success(self):
        """Fault injection: if the durable write itself fails (OSError), the
        existing JOB_STORE_CORRUPT fail-closed path is unchanged — no reduced
        record is claimed as persisted, and the on-disk record is untouched."""
        import os as os_module
        execution = {"synthetic_payload_filler": FILLER}
        pre_hash = file_sha(self.record_path)
        real_replace = os_module.replace

        def failing_replace(src, dst, *args, **kwargs):
            if str(dst).endswith(f"{self.jid}.json"):
                raise OSError(28, "No space left on device")
            return real_replace(src, dst, *args, **kwargs)

        with mock.patch.object(os_module, "replace", failing_replace):
            error = self.terminal_update(execution)
        self.assertIsInstance(error, JobError)
        self.assertEqual(error.code, "JOB_STORE_CORRUPT")
        self.assertEqual(file_sha(self.record_path), pre_hash)
        got = self.store.get(self.jid)
        self.assertEqual(got["status"], "running")


class NonDictExecution(TerminalPersistenceBase):
    def test_non_dict_oversized_execution_leaves_measured_evidence(self):
        execution = SENTINEL + "x" * MAX_RECORD_BYTES
        self.successful_terminal_update(execution)
        persisted = persisted_record(self.root, self.jid)
        on_disk = persisted["execution"]
        self.assertTrue(on_disk["terminal_write_truncated"])
        self.assertGreater(on_disk["record_json_utf8_overflow_bytes"], MAX_RECORD_BYTES)
        self.assertEqual(on_disk["execution_keys_dropped"], [])
        self.assertNotIn(SENTINEL.encode(), self.record_path.read_bytes())
        self.assertEqual(persisted["error_code"], "OUTPUT_LIMIT_EXCEEDED")
        self.assert_seal(persisted)


class NoCallerMutation(TerminalPersistenceBase):
    def test_caller_execution_dict_is_never_mutated(self):
        diagnostics_value = diagnostics(MAX_RECORD_BYTES + 4201)
        execution = {"output_limit_diagnostics": diagnostics_value,
                     "synthetic_payload_filler": FILLER}
        expected = json.loads(json.dumps(execution))
        self.successful_terminal_update(execution)
        self.assertEqual(execution, expected)
        persisted = persisted_record(self.root, self.jid)
        self.assertNotIn("synthetic_payload_filler", persisted["execution"])


class BaseReaderCompatibility(TerminalPersistenceBase):
    def test_base_370c024_reader_reads_seals_and_rewrites_not(self):
        """Materialized base (370c024) reader: a tier-2 record must read,
        verify its integrity seal and keep the original error_code, and the
        base reader must not rewrite the record file."""
        if shutil.which("git") is None:
            self.skipTest("git not available")
        base_dir = self.base / "basepkg"
        (base_dir / "aee" / "mcp_runtime").mkdir(parents=True)
        (base_dir / "aee" / "__init__.py").write_text("")
        (base_dir / "aee" / "mcp_runtime" / "__init__.py").write_text("")
        for module in ("store.py", "result_contract.py"):
            source = subprocess.run(
                ["git", "-C", str(Path(__file__).resolve().parents[2]),
                 "show", f"370c024:aee/mcp_runtime/{module}"],
                check=True, capture_output=True, text=True).stdout
            (base_dir / "aee" / "mcp_runtime" / module).write_text(source)
        execution = {"output_limit_diagnostics": {"unknown_key": "s" * 8192},
                     "synthetic_payload_filler": FILLER}
        self.successful_terminal_update(execution)
        pre_hash = file_sha(self.record_path)
        child = base_dir / "_read_base.py"
        child.write_text(
            "import sys\n"
            "sys.path.insert(0, %r)\n" % str(base_dir) +
            "from aee.mcp_runtime.store import JobStore\n"
            "store = JobStore(%r)\n" % str(self.root) +
            "record = store._read(%r)\n" % str(self.jid) +
            "assert record['error_code'] == 'OUTPUT_LIMIT_EXCEEDED', record['error_code']\n"
            "assert record['status'] == 'failed', record['status']\n"
            "assert record['execution']['terminal_write_truncated'] is True\n"
            "assert 'output_limit_diagnostics' not in record['execution']\n"
            "print('BASE_READER_OK')\n")
        result = subprocess.run([sys.executable, str(child)], capture_output=True,
                                text=True, timeout=120)
        self.assertEqual(result.returncode, 0,
                         msg=f"stdout={result.stdout}\nstderr={result.stderr}")
        self.assertIn("BASE_READER_OK", result.stdout)
        self.assertEqual(file_sha(self.record_path), pre_hash)


if __name__ == "__main__":
    unittest.main()