"""Local-filesystem job store with one kernel-leased active slot.

All registry operations use the same stable flock inode. Admission acquires
the active lease before publishing a queued record and holds it until a
terminal write. Kernel lease release, rather than wall-clock guesses, permits
restart reconciliation. No retries, queue or database are introduced.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import stat
import tempfile
import threading
import weakref
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .result_contract import seal_record, verify_integrity, validate_success

ACTIVE = {"queued", "running"}
TERMINAL = {"completed", "failed", "timed_out", "cancelled"}
MAX_RECORD_BYTES = 65536
MAX_TASK_CHARS = 8000
MAX_SUMMARY_CHARS = 4000
MAX_LOG_CHARS = 2000


def now():
    return datetime.now(timezone.utc).isoformat()


class JobError(Exception):
    def __init__(self, code, message, *, exit_code=None, failure=None):
        self.code = code
        self.message = str(message)[:500]
        self.exit_code = exit_code
        self.failure = failure
        super().__init__(self.message)


def owner_identity():
    # /proc stat's comm may contain spaces or ')'; split after its final ')'.
    fields = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()
    return {"pid": os.getpid(), "start_ticks": fields[19],
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}


class JobStore:
    _REGISTRY_LOCK = ".registry.lock"

    def __init__(self, root: Path, redact=lambda value: value):
        self._root = Path(root)
        if self._root.is_symlink():
            raise JobError("JOB_STORE_CORRUPT", "Job store cannot be a symlink")
        self._root.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self._root, 0o700)
        self._redact = redact
        self._lock = threading.RLock()
        self._leases = {}
        self._pid = os.getpid()
        ref = weakref.ref(self)

        def child_reset():
            store = ref()
            if store is not None:
                store._lock = threading.RLock()
                store._check_process()

        os.register_at_fork(after_in_child=child_reset)
        # Expected invalid persisted state must not crash gateway startup. Reads
        # and admission still reject it without changing the original evidence.
        try:
            self.reconcile()
        except JobError:
            pass

    def _check_process(self):
        if os.getpid() != self._pid:
            # An inherited flock fd belongs to the same open-file description;
            # never let a forked child treat the parent's lease as its own.
            for fd in self._leases.values():
                os.close(fd)
            self._leases = {}
            self._pid = os.getpid()

    def _open_lock(self, name):
        try:
            fd = os.open(self._root / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError:
            raise JobError("JOB_STORE_CORRUPT", "Job lock is unavailable") from None
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise JobError("JOB_STORE_CORRUPT", "Invalid lock file")
        os.fchmod(fd, 0o600)
        return fd

    @contextmanager
    def _guard(self):
        with self._lock:
            self._check_process()
            fd = self._open_lock(self._REGISTRY_LOCK)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    def _try_lease(self):
        fd = self._open_lock(".active.lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        return fd

    def _job_path(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r"A3JOB-[0-9]{8}-[0-9a-f]{32}", job_id):
            raise JobError("JOB_NOT_FOUND", "Job not found")
        return self._root / (job_id + ".json")

    def _read(self, job_id):
        try:
            fd = os.open(self._job_path(job_id), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("not regular")
                payload = stream.read(MAX_RECORD_BYTES + 1)
            if len(payload) > MAX_RECORD_BYTES:
                raise ValueError("oversized")
            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError('duplicate field')
                    result[key] = value
                return result
            def invalid_constant(value):
                raise ValueError('invalid JSON constant')
            data = json.loads(payload, object_pairs_hook=unique_object, parse_constant=invalid_constant)
            if (not isinstance(data, dict) or data.get("job_id") != job_id or
                    data.get("status") not in ACTIVE | TERMINAL or
                    data.get("agent") != "codex" or data.get("mode") != "read_only"):
                raise ValueError("invalid contract")
            for key in ["created_at", "updated_at", "working_directory", "task"]:
                if not isinstance(data.get(key), str):
                    raise ValueError("invalid field")
            for key in ["started_at", "finished_at", "summary", "log_excerpt", "error", "error_code"]:
                if key not in data or (data[key] is not None and not isinstance(data[key], str)):
                    raise ValueError("invalid optional field")
            if data.get("artifacts") != [] or not isinstance(data.get("truncated"), bool):
                raise ValueError("invalid result metadata")
            if (data.get("exit_code") is not None and type(data["exit_code"]) is not int):
                raise ValueError("invalid exit code")
            if data['status'] in ACTIVE and not isinstance(data.get('owner'), dict):
                raise ValueError('legacy active owner')
            self._bounded(data)
            verify_integrity(data)
            if data['status'] == 'completed':
                validate_success(data)
            return data
        except FileNotFoundError:
            raise JobError("JOB_NOT_FOUND", "Job not found") from None
        except JobError:
            raise
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            raise JobError("JOB_STORE_CORRUPT", "Malformed or unavailable job record") from None

    def _bounded(self, data):
        limits = {"task": MAX_TASK_CHARS, "summary": MAX_SUMMARY_CHARS,
                  "log_excerpt": MAX_LOG_CHARS, "error": 500, "working_directory": 4096}
        for key, cap in limits.items():
            value = data.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > cap):
                raise JobError("OUTPUT_LIMIT_EXCEEDED", "Stored field exceeds contract limit")
        if data.get("artifacts") != []:
            raise JobError("OUTPUT_LIMIT_EXCEEDED", "Artifacts are not supported by this profile")
        if len(json.dumps(data, ensure_ascii=False).encode()) > MAX_RECORD_BYTES:
            raise JobError("OUTPUT_LIMIT_EXCEEDED", "Job record exceeds size limit")

    def _write(self, data):
        if data.get('status') == 'completed':
            validate_success(data, persisted=False)
        seal_record(data)
        self._bounded(data)
        payload = json.dumps(data, ensure_ascii=False).encode()
        fd, tmp = tempfile.mkstemp(dir=self._root, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self._job_path(data["job_id"]))
            dfd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            raise JobError("JOB_STORE_CORRUPT", "Job record could not be durably persisted") from None
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _records(self):
        try:
            return [self._read(p.stem) for p in sorted(self._root.glob("A3JOB-*.json"))]
        except JobError as error:
            if error.code == "JOB_NOT_FOUND":
                raise JobError("JOB_STORE_CORRUPT", "Registry contains an invalid job filename") from None
            raise

    def _reconcile_unleased(self):
        records = self._records()  # validate all first; corruption fails closed
        active = [d for d in records if d["status"] in ACTIVE]
        if any(not isinstance(d.get("owner"), dict) for d in active):
            raise JobError("JOB_STORE_CORRUPT", "Legacy active jobs require an offline migration review")
        for data in active:
            # Existing result evidence is preserved; interrupted tasks never retry.
            data.update(status="failed", finished_at=now(), updated_at=now(),
                        error_code="EXECUTION_INTERRUPTED",
                        error="Executor lease disappeared; task was not retried")
            self._write(data)
        return len(active)

    def reconcile(self):
        with self._guard():
            fd = self._try_lease()
            if fd is None:
                return 0  # another gateway owns a live queued/running job
            try:
                return self._reconcile_unleased()
            finally:
                os.close(fd)

    @staticmethod
    def _new_job_id():
        return f"A3JOB-{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(16)}"

    def create(self, agent, task, working_directory, mode, execution=None):
        if agent != "codex" or mode != "read_only":
            raise JobError("INVALID_AGENT" if agent != "codex" else "INVALID_MODE", "Unsupported executor contract")
        if not isinstance(task, str) or not task.strip() or len(task) > MAX_TASK_CHARS:
            raise JobError("INVALID_TASK", "Task must be nonempty and at most 8000 characters")
        try:
            task.encode("utf-8")
        except UnicodeError:
            raise JobError("INVALID_TASK", "Task must contain valid UTF-8 text") from None
        with self._guard():
            fd = self._try_lease()
            if fd is None:
                raise JobError("BUSY", "One Codex job is already active")
            try:
                self._reconcile_unleased()
                stamp = now()
                data = {"job_id": self._new_job_id(), "agent": agent,
                        "task": self._redact(task), "working_directory": working_directory,
                        "mode": mode, "status": "queued", "created_at": stamp,
                        "updated_at": stamp, "started_at": None, "finished_at": None,
                        "summary": None, "exit_code": None, "error_code": None,
                        "error": None, "artifacts": [], "log_excerpt": None,
                        "truncated": False, "owner": owner_identity(), "execution": execution}
                self._write(data)
                self._leases[data["job_id"]] = fd
                fd = None
                return data
            finally:
                if fd is not None:
                    os.close(fd)

    def get(self, job_id):
        self.reconcile()
        with self._guard():
            return self._read(job_id)

    def update(self, job_id, **fields):
        allowed = {"status", "started_at", "finished_at", "summary", "exit_code", "error_code",
                   "error", "artifacts", "log_excerpt", "truncated", "execution"}
        if set(fields) - allowed:
            raise JobError("JOB_STATE_CONFLICT", "Immutable job fields cannot be changed")
        with self._guard():
            data = self._read(job_id)
            if data["status"] in TERMINAL or job_id not in self._leases:
                raise JobError("JOB_STATE_CONFLICT", "Only the active lease owner can update this job")
            target = fields.get("status", data["status"])
            transitions = {"queued": {"queued", "running"} | TERMINAL,
                           "running": {"running"} | TERMINAL}
            if target not in transitions[data["status"]]:
                raise JobError("JOB_STATE_CONFLICT", "Invalid job transition")
            data.update(fields)
            data["updated_at"] = now()
            for key in ["summary", "log_excerpt", "error"]:
                if isinstance(data.get(key), str):
                    data[key] = self._redact(data[key])
            self._write(data)
            if target in TERMINAL:
                os.close(self._leases.pop(job_id))
            return data

    def has_active(self):
        with self._guard():
            return any(d["status"] in ACTIVE for d in self._records())

    def list_ids(self, limit=50):
        with self._guard():
            return [d["job_id"] for d in self._records()][-max(0, min(limit, 50)):][::-1] if limit > 0 else []

    def close(self):
        with self._lock:
            self._check_process()
            for fd in self._leases.values():
                os.close(fd)
            self._leases.clear()

    def release(self, job_id):
        """Worker-finally cleanup if a terminal write itself failed."""
        with self._lock:
            self._check_process()
            fd = self._leases.pop(job_id, None)
            if fd is not None:
                os.close(fd)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
