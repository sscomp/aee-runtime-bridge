import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path

from aee.mcp_runtime.store import JobError, JobStore
from completed_result_fixtures import successful_fields


def concurrent_request(root, barrier, queue, finish):
    store = JobStore(Path(root))
    barrier.wait(timeout=15)
    try:
        job = store.create("codex", "fixture", root, "read_only")
        queue.put(("ADMITTED", job["job_id"]))
        finish.wait(15)  # hold lease until all competitors reported
        store.update(job["job_id"], **successful_fields())
    except JobError as error:
        queue.put((error.code, None))
    finally:
        store.close()


def crashed_worker(root, queue, running):
    store = JobStore(Path(root))
    job = store.create("codex", "fixture", root, "read_only")
    if running:
        store.update(job["job_id"], status="running", summary="prior evidence", log_excerpt="prior log")
    queue.put(job["job_id"])
    queue.close()
    queue.join_thread()
    os._exit(7)  # kernel releases lease; no cleanup or terminal update


class StoreCorrectness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aee-p2b-store-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = JobStore(self.root)
        self.addCleanup(self.store.close)

    def create(self):
        return self.store.create("codex", "fixture", str(self.root), "read_only")

    def test_eight_simultaneous_processes_exactly_one_admitted(self):
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(8)
        queue = ctx.Queue()
        finish = ctx.Event()
        children = [ctx.Process(target=concurrent_request,
                                args=(str(self.root), barrier, queue, finish)) for _ in range(8)]
        try:
            for child in children:
                child.start()
            results = [queue.get(timeout=20) for _ in children]
            self.assertEqual(sum(kind == "ADMITTED" for kind, _ in results), 1)
            self.assertEqual(sum(kind == "BUSY" for kind, _ in results), 7)
            active = [json.loads(p.read_text()) for p in self.root.glob("A3JOB-*.json")]
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["status"], "queued")
        finally:
            finish.set()
            for child in children:
                child.join(20)
                if child.is_alive():
                    child.kill()
                    child.join()
            queue.close()
        self.assertTrue(all(child.exitcode == 0 for child in children))

    def test_crashed_queued_and_running_reconciled_without_retry(self):
        ctx = multiprocessing.get_context("spawn")
        for running in [False, True]:
            queue = ctx.Queue()
            process = ctx.Process(target=crashed_worker, args=(str(self.root), queue, running))
            process.start()
            job_id = queue.get(timeout=15)
            process.join(15)
            self.assertEqual(process.exitcode, 7)
            self.assertEqual(self.store.reconcile(), 1)
            record = self.store.get(job_id)
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["error_code"], "EXECUTION_INTERRUPTED")
            if running:
                self.assertEqual(record["summary"], "prior evidence")
                self.assertEqual(record["log_excerpt"], "prior log")
            self.assertEqual(self.store.reconcile(), 0)
            queue.close()

    def test_live_lease_not_reconciled_by_another_instance(self):
        job = self.create()
        self.store.update(job["job_id"], status="running")
        other = JobStore(self.root)
        self.addCleanup(other.close)
        self.assertEqual(other.reconcile(), 0)
        self.assertEqual(other.get(job["job_id"])["status"], "running")
        with self.assertRaises(JobError) as error:
            other.create("codex", "fixture", str(self.root), "read_only")
        self.assertEqual(error.exception.code, "BUSY")

    def test_poll_reconciles_lost_lease_without_new_dispatch(self):
        job = self.create()
        self.store.update(job["job_id"], status="running", summary="evidence")
        self.store.release(job["job_id"])
        result = self.store.get(job["job_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "EXECUTION_INTERRUPTED")
        self.assertEqual(result["summary"], "evidence")

    def test_invalid_task_never_publishes_or_acquires_active_slot(self):
        for task in ["", "x" * 8001, "\ud800"]:
            with self.assertRaises(JobError) as error:
                self.store.create("codex", task, str(self.root), "read_only")
            self.assertEqual(error.exception.code, "INVALID_TASK")
        self.assertFalse(self.store.has_active())
        self.assertEqual(self.store.list_ids(), [])

    def test_oversized_or_invalid_record_is_not_silently_ignored(self):
        job = self.create()
        self.store.update(job["job_id"], **successful_fields())
        path = self.store._job_path(job["job_id"])
        original = path.read_bytes()
        for content in [b"x" * 65537, b"[]", original.replace(b'"completed"', b'"unknown"')]:
            path.write_bytes(content)
            with self.assertRaises(JobError) as error:
                self.store.get(job["job_id"])
            self.assertEqual(error.exception.code, "JOB_STORE_CORRUPT")
            self.assertEqual(path.read_bytes(), content)

    def test_fork_child_does_not_inherit_slot_ownership(self):
        job = self.create()
        readfd, writefd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(readfd)
            os.write(writefd, str(len(self.store._leases)).encode())
            os._exit(0)
        os.close(writefd)
        self.assertEqual(os.read(readfd, 10), b"0")
        os.close(readfd)
        os.waitpid(pid, 0)
        self.assertEqual(self.store.get(job["job_id"])["status"], "queued")

    def test_corrupt_record_blocks_admission_and_is_preserved(self):
        job = self.create()
        self.store.update(job["job_id"], **successful_fields())
        path = self.store._job_path(job["job_id"])
        path.write_bytes(b'{"partial":')
        with self.assertRaises(JobError) as error:
            self.create()
        self.assertEqual(error.exception.code, "JOB_STORE_CORRUPT")
        self.assertEqual(path.read_bytes(), b'{"partial":')
        self.assertEqual(len(list(self.root.glob("A3JOB-*.json"))), 1)

    def test_legacy_active_record_requires_migration_review(self):
        job = self.create()
        self.store.close()
        path = self.store._job_path(job["job_id"])
        data = json.loads(path.read_text())
        data.pop("owner")
        path.write_text(json.dumps(data))
        with self.assertRaises(JobError) as error:
            reopened = JobStore(self.root)
            self.addCleanup(reopened.close)
            reopened.get(job["job_id"])
        self.assertEqual(error.exception.code, "JOB_STORE_CORRUPT")
        self.assertEqual(json.loads(path.read_text())["status"], "queued")

    def test_failed_atomic_write_does_not_publish_or_leak_slot(self):
        from unittest.mock import patch
        with patch("aee.mcp_runtime.store.os.replace", side_effect=OSError("fixture")):
            with self.assertRaises(JobError) as error:
                self.create()
            self.assertEqual(error.exception.code, "JOB_STORE_CORRUPT")
        self.assertFalse(list(self.root.glob(".tmp-*")))
        self.assertFalse(list(self.root.glob("A3JOB-*.json")))
        self.assertEqual(self.create()["status"], "queued")

    def test_invalid_filename_and_lock_symlink_fail_closed(self):
        invalid = self.root / "A3JOB-malformed.json"
        invalid.write_text("{}")
        with self.assertRaises(JobError) as error:
            self.create()
        self.assertEqual(error.exception.code, "JOB_STORE_CORRUPT")
        invalid.unlink()
        lock = self.root / ".registry.lock"
        lock.unlink()
        lock.symlink_to(Path(__file__))
        with self.assertRaises(JobError) as error:
            self.create()
        self.assertEqual(error.exception.code, "JOB_STORE_CORRUPT")

    def test_record_symlink_does_not_read_external_fixture(self):
        job = self.create()
        self.store.update(job["job_id"], **successful_fields())
        path = self.store._job_path(job["job_id"])
        path.unlink()
        path.symlink_to(Path(__file__))
        with self.assertRaises(JobError) as error:
            self.store.get(job["job_id"])
        self.assertEqual(error.exception.code, "JOB_STORE_CORRUPT")

    def test_terminal_and_immutable_fields_cannot_be_rewritten(self):
        job = self.create()
        with self.assertRaises(JobError):
            self.store.update(job["job_id"], working_directory="another")
        self.store.update(job["job_id"], **successful_fields("preserved"))
        with self.assertRaises(JobError) as error:
            self.store.update(job["job_id"], status="running")
        self.assertEqual(error.exception.code, "JOB_STATE_CONFLICT")
        self.assertEqual(self.store.get(job["job_id"])["summary"], "preserved")

    def test_payload_artifacts_and_permissions_bounded(self):
        job = self.create()
        for update in [{"summary": "x" * 4001}, {"log_excerpt": "x" * 2001},
                       {"artifacts": ["unexpected"]}]:
            with self.assertRaises(JobError) as error:
                self.store.update(job["job_id"], **update)
            self.assertEqual(error.exception.code, "OUTPUT_LIMIT_EXCEEDED")
        for path in self.root.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
