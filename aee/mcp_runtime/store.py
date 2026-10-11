"""Local-filesystem job store with one kernel-leased active slot.

All registry operations use the same stable flock inode. Admission acquires
the active lease before publishing a queued record and holds it until a
terminal write. Kernel lease release, rather than wall-clock guesses, permits
restart reconciliation. No retries, queue or database are introduced.
"""
from __future__ import annotations

import copy
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

# K02-R1: an oversized terminal-failed record used to abort at the store-record
# boundary; lease release + reconciliation then rewrote it failed/
# EXECUTION_INTERRUPTED, losing failure attribution. The store-side reducer
# below (tiers 1-3) only ever applies to terminal unsuccessful statuses
# (never completed, never active), is deterministic, bounded, and preserves
# error_code/error/status/timestamps verbatim. The record-serialize byte cap
# itself is unchanged.
TERMINAL_REDUCE_STATUSES = TERMINAL - {"completed"}
MAX_DROPPED_KEY_NAMES = 8
MAX_DROPPED_KEY_CHARS = 48
MAX_TRUNCATION_MARKER_BYTES = 700

# R5-R2: bounded scalar diagnostics attached at the exact OUTPUT_LIMIT_EXCEEDED
# emission sites, so a future reproduction names the boundary (executor
# capture, final message/payload serialization, store record/field, snapshot,
# manifest) without retaining any raw payload, prompt or credential. Each
# field is an integer, a boolean, or a short fixed-string from the enums
# below; "measured" distinguishes a direct observation from a configured or
# unknown value, and nulls are never fabricated into zeros. Additive only:
# no raise site, message, limit or failure classification changes.
DIAGNOSTIC_SCHEMA = 'aee-output-limit-diagnostics-v1'
BOUNDARIES = {'executor_capture_stdout', 'executor_capture_stderr', 'executor_file_size_limit',
              'native_final_message', 'success_payload_json',
              'store_field_contract', 'store_artifacts_contract', 'store_record_serialize',
              'workspace_snapshot_file', 'workspace_snapshot_total',
              'manifest_file', 'manifest_tree', 'manifest_files', 'manifest_serialized'}
STAGES = {'raw_stream', 'os_write', 'final_message_utf8', 'payload_json_utf8',
          'record_json_utf8', 'store_field_contract', 'manifest_entries',
          'manifest_json_utf8', 'workspace_copy'}
UNITS = {'raw_bytes', 'utf8_bytes', 'chars', 'count'}


def output_limit_context(boundary, stage, unit, *, limit=None, observed=None,
                         measured=False, stream=None):
    """Build the bounded context dict for one OUTPUT_LIMIT_EXCEEDED site."""
    context = {'schema': DIAGNOSTIC_SCHEMA,
               'failure_boundary': boundary if boundary in BOUNDARIES else 'unclassified_boundary',
               'measurement_stage': stage if stage in STAGES else 'unclassified_stage',
               'measurement_unit': unit if unit in UNITS else None,
               'source_stream': stream if stream in {'stdout', 'stderr'} else None,
               'limit_bytes': limit if isinstance(limit, int) and unit in {'raw_bytes', 'utf8_bytes'} else None,
               'observed_bytes': observed if isinstance(observed, int) and unit in {'raw_bytes', 'utf8_bytes'} else None,
               'limit_chars': limit if isinstance(limit, int) and unit == 'chars' else None,
               'observed_chars': observed if isinstance(observed, int) and unit == 'chars' else None,
               'limit_entries': limit if isinstance(limit, int) and unit == 'count' else None,
               'observed_entries': observed if isinstance(observed, int) and unit == 'count' else None,
               'observed_measured': bool(measured)}
    return context


# K02-R1: single source of truth for the sanitized JobError.context keys; the
# tier-1 diagnostic retention guard accepts exactly these (no new allowlist).
DIAGNOSTIC_KEYS = frozenset({'schema', 'failure_boundary', 'measurement_stage',
                             'measurement_unit', 'source_stream', 'limit_bytes',
                             'observed_bytes', 'limit_chars', 'observed_chars',
                             'limit_entries', 'observed_entries', 'observed_measured'})
# A retained tier-1 diagnostic/failure detail is only bounded scalars, and a
# truncation marker only ever names dropped ``execution`` keys: a key name is
# kept verbatim only when it is short and character-safe, and never when it is
# secret-shaped (values would be a disclosure; names are refused as well -
# a sentinel-bearing key name is exactly the leak test T3/9 guards against).
_MARKER_SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,48}")
_MARKER_HIDDEN_NAME = re.compile(
    r"(?i)api[_-]?key|token|secret|password|credential|bearer|authorization")


def _validated_store_diagnostics(value):
    """Tier-1 retention guard for ``execution.output_limit_diagnostics``.

    Accepts exactly the sanitized scalar shape the single JobError context
    guard already emits (DIAGNOSTIC_KEYS, bounded values); anything else is
    not retained and falls through to the tier-2 truncation marker.
    """
    if not isinstance(value, dict) or set(value) - DIAGNOSTIC_KEYS:
        return None
    for item in value.values():
        if not (item is None or isinstance(item, (int, bool))
                or isinstance(item, str) and len(item) <= 48):
            return None
    return dict(value)


def _validated_failure_detail(value):
    """Tier-1 retention guard for ``execution.native_tool_failure``.

    The documented raise-site shape is a flat bounded scalar dict (contract /
    evidence_complete / condition / tool_failures / recovery_permitted);
    nested or unbounded content is not retained.
    """
    if not isinstance(value, dict):
        return None
    for key, item in value.items():
        if (not isinstance(key, str) or len(key) > 48 or not _MARKER_SAFE_NAME.fullmatch(key)
                or not (item is None or isinstance(item, (int, bool))
                        or isinstance(item, str) and len(item) <= 48)):
            return None
    return dict(value)


def _sanitized_dropped_names(keys):
    """Bounded, sanitized key names for the truncation marker.

    Returns (names, omitted): names kept verbatim are deduplicated, sorted,
    character-safe, at most 48 chars and not secret-shaped; everything else
    (non-string, malformed, secret-shaped, or beyond MAX_DROPPED_KEY_NAMES)
    is only counted, never spelled out.
    """
    names, omitted = [], 0
    for key in sorted(keys, key=str):
        if (isinstance(key, str) and len(key) <= MAX_DROPPED_KEY_CHARS
                and _MARKER_SAFE_NAME.fullmatch(key)
                and not _MARKER_HIDDEN_NAME.search(key)
                and key not in names):
            names.append(key)
        else:
            omitted += 1
    bounded = names[:MAX_DROPPED_KEY_NAMES]
    omitted += len(names) - len(bounded)
    return bounded, omitted


def _candidate_with_execution(data, execution):
    """Copy of ``data`` with the execution value replaced; the caller's
    dictionaries (including any shared execution mapping) are never mutated."""
    candidate = copy.deepcopy(data)
    candidate['execution'] = execution
    return candidate


def _truncation_marker(overflow_bytes, dropped_names, omitted):
    """Bounded, schema-valid truncation-evidence execution marker (K02-R1)."""
    marker = {'terminal_write_truncated': True,
              'record_json_utf8_overflow_bytes': overflow_bytes,
              'execution_keys_dropped': dropped_names}
    if omitted:
        marker['execution_keys_omitted_count'] = omitted
    return marker


def _bounded_marker(overflow_bytes, dropped_names, omitted):
    """Truncation marker with a hard measured bound (MAX_TRUNCATION_MARKER_BYTES).

    The name list is the only sizeable part; if the bounded marker would still
    exceed its target it is rebuilt without names (counts only). Returns None
    only if even that bound cannot be met, which today's constants make
    unreachable — a None result fails the whole write closed.
    """
    marker = _truncation_marker(overflow_bytes, dropped_names, omitted)
    if len(json.dumps(marker, ensure_ascii=False).encode()) <= MAX_TRUNCATION_MARKER_BYTES:
        return marker
    marker = _truncation_marker(overflow_bytes, [], omitted + len(dropped_names))
    if len(json.dumps(marker, ensure_ascii=False).encode()) <= MAX_TRUNCATION_MARKER_BYTES:
        return marker
    return None


def now():
    return datetime.now(timezone.utc).isoformat()


class JobError(Exception):
    def __init__(self, code, message, *, exit_code=None, failure=None, context=None):
        self.code = code
        self.message = str(message)[:500]
        self.exit_code = exit_code
        self.failure = failure
        self.context = None
        if isinstance(context, dict):
            # Sanitize, never mask: only known scalar keys survive, bounded so
            # the failure record cannot itself overflow the store contract.
            # None (unknown/unmeasured) is kept — it distinguishes a null
            # observation from a fabricated zero or from omission.
            self.context = {key: value for key, value in context.items()
                            if key in DIAGNOSTIC_KEYS
                            and (value is None or isinstance(value, (int, bool))
                                 or isinstance(value, str) and len(value) <= 48)}
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
                raise JobError("OUTPUT_LIMIT_EXCEEDED", "Stored field exceeds contract limit",
                               context=output_limit_context(
                                   'store_field_contract', 'store_field_contract', 'chars',
                                   limit=cap, observed=len(value) if isinstance(value, str) else None,
                                   measured=isinstance(value, str)))
        if data.get("artifacts") != []:
            raise JobError("OUTPUT_LIMIT_EXCEEDED", "Artifacts are not supported by this profile",
                           context=output_limit_context(
                               'store_artifacts_contract', 'store_field_contract', 'chars'))
        record_bytes = len(json.dumps(data, ensure_ascii=False).encode())
        if record_bytes > MAX_RECORD_BYTES:
            raise JobError("OUTPUT_LIMIT_EXCEEDED", "Job record exceeds size limit",
                           context=output_limit_context(
                               'store_record_serialize', 'record_json_utf8', 'utf8_bytes',
                               limit=MAX_RECORD_BYTES, observed=record_bytes, measured=True))

    def _write(self, data, _reduction_tier=0, _first_overflow=None, _original_keys=None):
        """Atomic durable write of one record.

        K02-R1: if a terminal unsuccessful (failed / timed_out / cancelled)
        record exceeds MAX_RECORD_BYTES at the whole-record serialization
        boundary (``store_record_serialize``), one deterministic bounded
        reduction attempt is made per tier and the write is retried on the
        reduced copy: tier 1 keeps validated execution diagnostics plus bounded
        truncation evidence, tier 2 substitutes the full truncation-evidence
        marker, tier 3 keeps only the minimal marker. ``error_code`` / ``error``
        / terminal ``status`` / timestamps are never rewritten by a reduction,
        the first measured overflow is carried through the tiers, and any other
        failure (field contract, artifacts, integrity, the completed path)
        re-raises exactly as before — fail closed.
        """
        if data.get('status') == 'completed':
            validate_success(data, persisted=False)
        try:
            seal_record(data)
            self._bounded(data)
        except JobError as error:
            context = error.context if isinstance(error.context, dict) else {}
            first_overflow = _first_overflow
            if (first_overflow is None and type(context.get('observed_bytes')) is int
                    and context.get('observed_measured')):
                first_overflow = context['observed_bytes']
            candidate = self._reduced_terminal_failure(data, error, _reduction_tier,
                                                       first_overflow, _original_keys)
            if candidate is not None:
                original_keys = _original_keys
                if original_keys is None:
                    execution = data.get('execution')
                    original_keys = list(execution.keys()) if isinstance(execution, dict) else []
                return self._write(candidate, _reduction_tier=_reduction_tier + 1,
                                   _first_overflow=first_overflow,
                                   _original_keys=original_keys)
            raise
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
        return data

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

    def _reduced_terminal_failure(self, data, error, tier, first_overflow, original_keys=None):
        """K02-R1 deterministic bounded shrink for one terminal-unsuccessful
        record that overflowed the whole-record serialization boundary.

        Returns a reduced *copy* to retry the write with, or None to re-raise
        the original failure unchanged (fail closed). ``tier`` is the number of
        reductions already attempted (0 for the initial write); each tier is
        attempted at most once. The caller's dictionaries are never mutated.
        ``original_keys`` carries the pre-reduction execution key names so the
        tier-2 marker always names what was dropped from the *original* record.
        """
        if (tier not in (0, 1, 2)
                or data.get('status') not in TERMINAL_REDUCE_STATUSES
                or error.code != 'OUTPUT_LIMIT_EXCEEDED'
                or not isinstance(error.context, dict)
                or error.context.get('failure_boundary') != 'store_record_serialize'
                or type(first_overflow) is not int):
            return None
        execution = data.get('execution')
        keys = list(execution.keys()) if isinstance(execution, dict) else []
        if original_keys is None:
            original_keys = keys
        if tier == 0:
            # Tier 1: keep only validated, bounded diagnostics; every dropped
            # original key stays durably named as truncation evidence.
            retained = {}
            for name, validate in (('output_limit_diagnostics', _validated_store_diagnostics),
                                   ('native_tool_failure', _validated_failure_detail)):
                if name in keys:
                    value = validate(execution[name])
                    if value is not None:
                        retained[name] = value
            names, omitted = _sanitized_dropped_names(
                [key for key in original_keys if key not in retained])
            evidence = _bounded_marker(first_overflow, names, omitted)
            if evidence is None:
                return None
            candidate = copy.deepcopy(data)
            evidence.update(retained)
            candidate['execution'] = evidence
            return candidate
        names, omitted = _sanitized_dropped_names(original_keys)
        marker = _bounded_marker(first_overflow, names, omitted)
        if marker is None:
            return None
        if tier == 1:
            return _candidate_with_execution(data, marker)
        # Tier 3 (minimal terminal fallback): schema-valid, field-bounded;
        # keeps the measured overflow and the explicit truncation flag only.
        return _candidate_with_execution(
            data, {'terminal_write_truncated': True,
                   'record_json_utf8_overflow_bytes': first_overflow})

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
            # K02-R1: _write may internally retry on a reduced copy for a
            # terminal-unsuccessful overflow; persist what was actually
            # written, so the caller sees the reduced record too.
            data = self._write(data)
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
