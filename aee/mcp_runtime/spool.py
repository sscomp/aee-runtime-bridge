"""Owner-private durable streams with cross-process quota/cleanup serialization."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time

from .store import JobError

_ID = re.compile('[a-f0-9]{32}\\Z')
_JOB = re.compile('A3JOB-[0-9]{8}-[a-f0-9]{32}\\Z')
MAX_SPOOLS = 4096  # mandatory metadata/inode host safety ceiling


def _private_dir(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise JobError('SPOOL_BOUNDARY', 'Spool directory must be private and owned by execution identity')
    return fd


def _file(dirfd, name, flags=os.O_RDWR | os.O_CREAT):
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600, dir_fd=dirfd)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
        os.close(fd)
        raise JobError('SPOOL_BOUNDARY', 'Invalid spool file boundary')
    return fd


class SpoolStore:
    def __init__(self, root):
        self.root = Path(root)
        self.fd = _private_dir(root)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    @contextmanager
    def guard(self):
        fd = _file(self.fd, '.quota.lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def entries(self):
        entries = os.listdir(self.fd)
        names = [n for n in entries if _ID.fullmatch(n)]
        if len(entries) > MAX_SPOOLS + 1 or any(n != '.quota.lock' and not _ID.fullmatch(n) for n in entries):
            raise JobError('SPOOL_BOUNDARY', 'Unknown or excessive spool entries; operator inspection required')
        return names

    def _dir(self, name):
        if not _ID.fullmatch(name):
            raise JobError('SPOOL_BOUNDARY', 'Invalid artifact identity')
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.fd)
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            os.close(fd)
            raise JobError('SPOOL_BOUNDARY', 'Invalid artifact directory')
        return fd

    def total(self):
        total = 0
        for name in self.entries():
            fd = self._dir(name)
            try:
                for stream in ('stdout', 'stderr'):
                    try:
                        f = _file(fd, stream, os.O_RDONLY)
                    except FileNotFoundError:
                        continue
                    try:
                        total += os.fstat(f).st_size
                    finally:
                        os.close(f)
            finally:
                os.close(fd)
        return total

    def _read_meta(self, fd):
        f = _file(fd, 'manifest.json', os.O_RDONLY)
        with os.fdopen(f, 'rb') as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise JobError('SPOOL_BOUNDARY', 'Oversized spool manifest')
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise JobError('SPOOL_BOUNDARY', 'Invalid spool manifest')
        return data

    def write_meta(self, fd, data):
        payload = json.dumps(data, ensure_ascii=True).encode()
        if len(payload) > 16384:
            raise JobError('SPOOL_IO_ERROR', 'Spool metadata exceeds host safety ceiling')
        name = '.manifest-' + secrets.token_hex(8)
        f = _file(fd, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            with os.fdopen(f, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, 'manifest.json', src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            try:
                os.unlink(name, dir_fd=fd)
            except FileNotFoundError:
                pass

    def reconcile(self, retention, on_recovered=None):
        """Never touch leased jobs. Interrupted artifacts stay inspectable until TTL."""
        with self.guard():
            for name in self.entries():
                fd = self._dir(name)
                lease = None
                try:
                    lease = _file(fd, '.active.lock')
                    try:
                        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    children = os.listdir(fd)
                    if len(children) > 16 or any(x not in {'stdout', 'stderr', 'manifest.json', '.active.lock'}
                                               and not re.fullmatch(r'\.manifest-[a-f0-9]{16}', x) for x in children):
                        raise JobError('SPOOL_BOUNDARY', 'Unexpected artifact entries require operator inspection')
                    for child in children:
                        if child.startswith('.manifest-'):
                            os.unlink(child, dir_fd=fd)
                    try:
                        data = self._read_meta(fd)
                    except FileNotFoundError:
                        data = {'schema': 'aee-stream-v1', 'artifact_id': name,
                                'created_at': os.fstat(fd).st_mtime, 'state': 'interrupted',
                                'failure_stage': 'restart', 'streams': {}}
                    if data.get('state') in {'running', 'interrupted'}:
                        streams = {}
                        for s in ('stdout', 'stderr'):
                            digest, count = hashlib.sha256(), 0
                            try:
                                f = _file(fd, s, os.O_RDONLY)
                            except FileNotFoundError:
                                continue
                            with os.fdopen(f, 'rb') as stream:
                                while block := stream.read(65536):
                                    count += len(block)
                                    digest.update(block)
                            streams[s] = {'spool_bytes': count, 'sha256': digest.hexdigest(),
                                          'observed_bytes': None, 'captured_bytes': 0, 'truncated': True}
                        data.update(state='interrupted', failure_stage='restart', streams=streams,
                                    elapsed_seconds=None, exit_code=None)
                        self.write_meta(fd, data)
                    if on_recovered and _JOB.fullmatch(str(data.get('job_id', ''))):
                        on_recovered(data['job_id'], data)
                    expiry = data.get('retention_seconds', retention)
                    if type(expiry) is not int or not 1 <= expiry <= 30 * 86400:
                        raise JobError('SPOOL_BOUNDARY', 'Invalid artifact retention')
                    created = data.get('created_at', os.fstat(fd).st_mtime)
                    if type(created) not in (int, float):
                        raise JobError('SPOOL_BOUNDARY', 'Invalid artifact timestamp')
                    if time.time() - created >= expiry:
                        children = os.listdir(fd)
                        if any(x not in {'stdout', 'stderr', 'manifest.json', '.active.lock'} and not re.fullmatch(r'\.manifest-[a-f0-9]{16}', x) for x in children):
                            raise JobError('SPOOL_BOUNDARY', 'Unknown artifact files; refuse cleanup')
                        for child in children:
                            # Unlink only leaf names; never follow a target.
                            os.unlink(child, dir_fd=fd)
                        os.rmdir(name, dir_fd=self.fd)
                        os.fsync(self.fd)
                except (ValueError, OSError) as exc:
                    raise JobError('SPOOL_IO_ERROR', 'Spool recovery unavailable') from None
                finally:
                    if lease is not None:
                        os.close(lease)
                    os.close(fd)

    def create(self, job_id, budget, revision):
        if not _JOB.fullmatch(job_id):
            raise JobError('SPOOL_BOUNDARY', 'Invalid job identity')
        with self.guard():
            if len(self.entries()) >= MAX_SPOOLS:
                raise JobError('SPOOL_QUOTA_EXCEEDED', 'Artifact inode safety ceiling exhausted')
            name = secrets.token_hex(16)
            os.mkdir(name, mode=0o700, dir_fd=self.fd)
            fd = self._dir(name)
            lease = None
            streams = {}
            try:
                lease = _file(fd, '.active.lock', os.O_RDWR | os.O_CREAT | os.O_EXCL)
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                for s in ('stdout', 'stderr'):
                    streams[s] = _file(fd, s, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                job = SpoolJob(self, fd, lease, streams, name, job_id, budget, revision)
                self.write_meta(fd, job.metadata('running'))
                os.fsync(self.fd)
                return job
            except BaseException:
                for stream in streams.values():
                    os.close(stream)
                if lease is not None:
                    os.close(lease)
                os.close(fd)
                raise


class SpoolJob:
    def __init__(self, store, fd, lease, streams, name, job_id, budget, revision):
        self.store, self.fd, self.lease, self.files = store, fd, lease, streams
        self.name, self.job_id, self.budget, self.revision = name, job_id, budget, revision
        self.started, self.created = time.monotonic(), time.time()
        self.observed = {s: 0 for s in streams}
        self.written = {s: 0 for s in streams}
        self.tails = {s: bytearray() for s in streams}
        self.hashes = {s: hashlib.sha256() for s in streams}
        self.closed = False

    def append(self, stream, chunk):
        self.observed[stream] += len(chunk)
        self.tails[stream].extend(chunk)
        del self.tails[stream][:-self.budget.memory_capture_bytes]
        with self.store.guard():
            b = self.budget
            if (self.written[stream] + len(chunk) > b.spool_max_bytes_per_stream or
                sum(self.written.values()) + len(chunk) > b.spool_max_bytes_per_job or
                self.store.total() + len(chunk) > b.spool_max_bytes_total):
                raise JobError('SPOOL_QUOTA_EXCEEDED', 'Configured stream/job/retained spool quota exhausted')
            view = memoryview(chunk)
            while view:
                n = os.write(self.files[stream], view)
                if n <= 0:
                    raise OSError('short spool write')
                self.hashes[stream].update(view[:n])
                self.written[stream] += n
                view = view[n:]

    def metadata(self, state, *, code=None, exit_code=None, redact=lambda x: x):
        cap = min(self.budget.log_excerpt_bytes, 512)
        def excerpt(s):
            # Decode boundary-safe, redact before reapplying the byte bound.
            return redact(bytes(self.tails[s][-cap:]).decode('utf-8', 'replace')).encode()[-cap:].decode('utf-8', 'ignore')
        return {'schema': 'aee-stream-v1', 'artifact_id': self.name, 'job_id': self.job_id,
                'policy_version': self.revision, 'state': state, 'failure_stage': code,
                'created_at': self.created, 'retention_seconds': self.budget.retention_seconds,
                'elapsed_seconds': round(time.monotonic() - self.started, 6), 'exit_code': exit_code,
                'streams': {s: {'observed_bytes': self.observed[s], 'captured_bytes': len(self.tails[s]),
                                 'spool_bytes': self.written[s], 'sha256': self.hashes[s].hexdigest(),
                                 'truncated': self.observed[s] != self.written[s],
                                 'capture_truncated': self.observed[s] > len(self.tails[s]),
                                 'tail': excerpt(s)} for s in self.files}}

    def finish(self, *, code=None, exit_code=None, redact=lambda x: x):
        data = self.metadata('failed' if code else 'complete', code=code, exit_code=exit_code, redact=redact)
        for fd in self.files.values():
            os.fsync(fd)
        self.store.write_meta(self.fd, data)
        return data

    def close(self):
        if not self.closed:
            self.closed = True
            for fd in self.files.values():
                os.close(fd)
            os.close(self.lease)
            os.close(self.fd)
