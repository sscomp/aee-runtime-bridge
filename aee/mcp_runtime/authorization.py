"""OS-issued single-job autonomous capabilities. No remote minting endpoint."""
from __future__ import annotations

import argparse
from contextvars import ContextVar
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import time

from .store import JobError
from .spool import _private_dir, _file

authenticated_transport = ContextVar('aee_authenticated_transport', default=None)
_HASH = re.compile(r'[a-f0-9]{64}\Z')
_NAME = re.compile(r'[A-Za-z0-9:_.-]{1,80}\Z')
TOKEN_PREFIX = 'aee-aut1.'


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def directory_identity(path):
    info = os.stat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise JobError('INVALID_WORKING_DIRECTORY', 'Expected a directory')
    return {'device': info.st_dev, 'inode': info.st_ino}


def os_identity():
    return {'uid': os.geteuid(), 'gid': os.getegid(), 'groups': sorted(os.getgroups())}


def _read(fd, name):
    handle = _file(fd, name, os.O_RDONLY)
    with os.fdopen(handle, 'rb') as stream:
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValueError('oversized authority record')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate authority field')
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError('non-finite authority value')
    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)


class Authority:
    def __init__(self, policy, grants):
        self.policy_path = Path(policy) if policy else None
        self.grants = Path(grants) if grants else None

    @classmethod
    def configured(cls):
        return cls(os.getenv('AEE_AUTONOMOUS_POLICY'), os.getenv('AEE_AUTONOMOUS_GRANTS'))

    def policy(self):
        try:
            if not self.policy_path or not self.grants:
                raise ValueError('absent policy')
            fd = _private_dir(self.policy_path.parent)
            try:
                data = _read(fd, self.policy_path.name)
            finally:
                os.close(fd)
            if (not isinstance(data, dict) or data.get('schema') != 'aee-autonomous-policy-v1'
                    or data.get('enabled') is not True):
                raise ValueError('disabled policy')
            principals = data.get('principals')
            if (not isinstance(principals, dict) or not principals or len(principals) > 32
                    or any(not _HASH.fullmatch(k) or not isinstance(v, str) or not _NAME.fullmatch(v)
                           for k, v in principals.items())):
                raise ValueError('invalid principals')
            agents = data.get('agents')
            if (not isinstance(agents, list) or not agents or len(agents) != len(set(agents))
                    or set(agents) - {'claude', 'codex', 'hermes', 'pi'}):
                raise ValueError('invalid agent policy')
            return data
        except (OSError, ValueError, TypeError, JobError):
            raise JobError('AUTONOMOUS_NOT_CONFIGURED', 'Private autonomous policy unavailable or disabled') from None

    def scope(self, principal, agent, task, cwd):
        return {'principal': principal, 'agent': agent, 'mode': 'autonomous',
                'profile': 'os-native', 'host': socket.gethostname(),
                'os_identity': os_identity(), 'task_sha256': digest(task),
                'working_directory': cwd, 'directory_identity': directory_identity(cwd)}

    def _check(self, record, expected, policy):
        now = time.time()
        if not isinstance(record, dict) or record.get('schema') != 'aee-autonomous-grant-v1':
            raise JobError('AUTONOMOUS_APPROVAL_INVALID', 'Invalid job capability')
        if (type(record.get('issued_at')) not in (int, float)
                or type(record.get('expires_at')) not in (int, float)
                or not math.isfinite(record['issued_at']) or not math.isfinite(record['expires_at'])
                or not now - 900 <= record['issued_at'] <= now + 5
                or not 0 < record['expires_at'] - record['issued_at'] <= 900
                or now >= record['expires_at']):
            raise JobError('AUTONOMOUS_APPROVAL_EXPIRED', 'Job capability expired or invalid lifetime')
        if (record.get('scope') != expected or expected['principal'] not in policy['principals'].values()
                or expected['agent'] not in policy['agents']):
            raise JobError('AUTONOMOUS_SCOPE_MISMATCH', 'Job capability scope does not match request')
        if not isinstance(record.get('operator'), str) or not _NAME.fullmatch(record['operator']):
            raise JobError('AUTONOMOUS_APPROVAL_INVALID', 'Invalid operator provenance')

    def consume(self, token, agent, task, cwd, *, consume=True):
        policy = self.policy()
        transport = authenticated_transport.get()
        principal = policy['principals'].get(transport) if transport else None
        if not principal:
            raise JobError('AUTONOMOUS_CALLER_UNVERIFIED', 'Authenticated authorized transport identity required')
        if (not isinstance(token, str) or not re.fullmatch(r'aee-aut1\.[A-Za-z0-9_-]{43}', token)):
            raise JobError('AUTONOMOUS_APPROVAL_INVALID', 'Single-job operator capability required')
        identifier = digest(token)
        expected = self.scope(principal, agent, task, cwd)
        fd = None
        lock = None
        try:
            fd = _private_dir(self.grants)
            lock = _file(fd, '.lock')
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                record = _read(fd, identifier + '.pending')
            except FileNotFoundError:
                raise JobError('AUTONOMOUS_APPROVAL_INVALID', 'Capability absent, revoked or already consumed') from None
            self._check(record, expected, policy)
            if consume:
                os.rename(identifier + '.pending', identifier + '.used', src_dir_fd=fd, dst_dir_fd=fd)
                os.fsync(fd)
            return {'schema': 'aee-autonomous-proof-v1', 'grant_id': identifier,
                    'operator': record['operator'], 'issued_at': record['issued_at'],
                    'expires_at': record['expires_at'], **expected}
        except (OSError, ValueError, TypeError):
            raise JobError('AUTONOMOUS_APPROVAL_INVALID', 'Private capability record unavailable') from None
        finally:
            if lock is not None:
                os.close(lock)
            if fd is not None:
                os.close(fd)

    def check_start(self, proof, agent, task, cwd):
        policy = self.policy()
        if not isinstance(proof, dict) or not _HASH.fullmatch(str(proof.get('grant_id', ''))):
            raise JobError('AUTONOMOUS_APPROVAL_INVALID', 'Missing consumed authorization proof')
        expected = self.scope(proof.get('principal'), agent, task, cwd)
        fd = None
        try:
            fd = _private_dir(self.grants)
            record = _read(fd, proof['grant_id'] + '.used')
            self._check(record, expected, policy)
            if proof != {'schema': 'aee-autonomous-proof-v1', 'grant_id': proof['grant_id'],
                         'operator': record['operator'], 'issued_at': record['issued_at'],
                         'expires_at': record['expires_at'], **expected}:
                raise ValueError('proof mismatch')
        except (OSError, ValueError, TypeError):
            raise JobError('AUTONOMOUS_APPROVAL_INVALID', 'Authorization revoked or changed before start') from None
        finally:
            if fd is not None:
                os.close(fd)

    def issue(self, principal, agent, task, cwd, ttl=300):
        policy = self.policy()
        if (principal not in policy['principals'].values() or agent not in policy['agents']
                or type(ttl) is not int or not 1 <= ttl <= 900 or not task.strip()):
            raise JobError('AUTONOMOUS_SCOPE_MISMATCH', 'Invalid issuance scope')
        cwd = str(Path(cwd).resolve(strict=True))
        if not os.access(cwd, os.R_OK | os.X_OK):
            raise JobError('INVALID_WORKING_DIRECTORY', 'OS account cannot enter directory')
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        issued = time.time()
        record = {'schema': 'aee-autonomous-grant-v1', 'operator': f'local-uid:{os.geteuid()}',
                  'issued_at': issued, 'expires_at': issued + ttl,
                  'scope': self.scope(principal, agent, task, cwd)}
        fd = _private_dir(self.grants)
        try:
            handle = _file(fd, digest(token) + '.pending', os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(handle, 'w') as stream:
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(fd)
        finally:
            os.close(fd)
        return token

    def revoke(self, identifier):
        if not _HASH.fullmatch(identifier):
            raise ValueError('Invalid grant identifier')
        fd = _private_dir(self.grants)
        lock = _file(fd, '.lock')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for suffix in ('.pending', '.used'):
                try:
                    os.unlink(identifier + suffix, dir_fd=fd)
                except FileNotFoundError:
                    pass
            os.fsync(fd)
        finally:
            os.close(lock)
            os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', required=True)
    parser.add_argument('--grants', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    issue = commands.add_parser('issue')
    issue.add_argument('--principal', required=True)
    issue.add_argument('--agent', choices=['claude', 'codex', 'hermes', 'pi'], required=True)
    issue.add_argument('--cwd', required=True)
    issue.add_argument('--task-file', required=True)
    issue.add_argument('--ttl', type=int, default=300)
    issue.add_argument('--output', required=True, help='New private file for protected capability delivery')
    commands.add_parser('revoke').add_argument('--grant-id', required=True)
    args = parser.parse_args()
    authority = Authority(args.policy, args.grants)
    if args.command == 'revoke':
        authority.revoke(args.grant_id)
        print('Revoked (running jobs require aee_job_cancel)')
        return
    output = Path(args.output)
    fd = _private_dir(output.parent)
    token = None
    try:
        handle = _file(fd, output.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(handle, 'w') as stream:
            token = authority.issue(args.principal, args.agent,
                                    Path(args.task_file).read_text(), args.cwd, args.ttl)
            stream.write(token + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(fd)
    except Exception:
        if token:
            authority.revoke(digest(token))
        raise
    finally:
        os.close(fd)
    print(json.dumps({'issued': True, 'grant_id': digest(token), 'token_printed': False, 'ttl': args.ttl}))


if __name__ == '__main__':
    main()
