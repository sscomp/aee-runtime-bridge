"""Explicit A2 Secure MCP admission policy; never inferred from request metadata.

The context is populated only by the authenticated restricted HTTP middleware.
Legacy capability authorization remains available on deployments without this policy.
"""
from contextvars import ContextVar
from dataclasses import dataclass
import math
import os
from pathlib import Path
import time

from .authorization import _HASH, _NAME, _read, digest, directory_identity, os_identity
from .spool import _private_dir
from .store import JobError
import socket

POLICY = 'secure_mcp_trusted_dispatch'
PROOF = 'aee-secure-mcp-admission-v1'


@dataclass(frozen=True)
class VerifiedSecureMCP:
    bearer_sha256: str


secure_mcp_transport = ContextVar('aee_verified_secure_mcp_transport', default=None)


class TrustedDispatch:
    def __init__(self, path):
        self.path = Path(path)

    @classmethod
    def configured(cls):
        # An explicitly configured but empty/broken file must never fall back.
        path = os.environ.get('AEE_TRUSTED_DISPATCH_POLICY')
        return None if path is None else cls(path)

    def policy(self):
        try:
            fd = _private_dir(self.path.parent)
            try:
                data = _read(fd, self.path.name)
            finally:
                os.close(fd)
            if (not isinstance(data, dict)
                    or data.get('schema') != 'aee-secure-mcp-trusted-dispatch-v1'
                    or data.get('policy') != POLICY
                    or type(data.get('enabled')) is not bool
                    or data.get('transport') != 'secure_mcp'):
                raise ValueError('invalid policy')
            principals = data.get('principals')
            if (not isinstance(principals, dict) or len(principals) > 32
                    or any(not _HASH.fullmatch(k) or not isinstance(v, str) or not _NAME.fullmatch(v)
                           for k, v in principals.items())
                    or len(set(principals.values())) != len(principals)):
                raise ValueError('invalid principals')
            agents = data.get('agents')
            if (not isinstance(agents, list) or not agents
                    or any(not isinstance(a, str) for a in agents)
                    or len(agents) != len(set(agents))
                    or set(agents) - {'claude', 'codex', 'hermes', 'pi'}):
                raise ValueError('invalid agents')
            return data
        except (OSError, ValueError, TypeError, JobError):
            raise JobError('TRUSTED_DISPATCH_NOT_CONFIGURED', 'Private trusted dispatch policy unavailable') from None

    def identity(self, mode, agent):
        policy = self.policy()
        verified = secure_mcp_transport.get()
        principal = (policy['principals'].get(verified.bearer_sha256)
                     if isinstance(verified, VerifiedSecureMCP) else None)
        if not principal:
            raise JobError('TRUSTED_DISPATCH_CALLER_UNVERIFIED', 'Approved authenticated Secure MCP principal required')
        self._enabled(policy, mode, agent)
        return verified.bearer_sha256, principal

    @staticmethod
    def _enabled(policy, mode, agent):
        if mode not in {'read_only', 'write', 'autonomous'}:
            raise JobError('INVALID_MODE', 'Unsupported admission mode')
        if mode != 'read_only':
            if not policy['enabled']:
                raise JobError('TRUSTED_DISPATCH_DISABLED', 'New write/autonomous admissions disabled by operator')
            if agent not in policy['agents']:
                raise JobError('TRUSTED_DISPATCH_AGENT_UNAPPROVED', 'Agent is not qualified for trusted execution')

    @staticmethod
    def scope(principal, key, agent, mode, task, cwd):
        return {'schema': PROOF, 'policy': POLICY, 'transport': 'secure_mcp',
                'principal': principal, 'principal_key_sha256': key,
                'agent': agent, 'mode': mode,
                'profile': 'os-native' if mode == 'autonomous' else mode,
                'host': socket.gethostname(), 'os_identity': os_identity(),
                'task_sha256': digest(task), 'working_directory': cwd,
                'directory_identity': directory_identity(cwd)}

    def admit(self, agent, mode, task, cwd):
        key, principal = self.identity(mode, agent)
        return {**self.scope(principal, key, agent, mode, task, cwd), 'admitted_at': time.time()}

    def check_start(self, proof, agent, mode, task, cwd):
        policy = self.policy()
        self._enabled(policy, mode, agent)
        try:
            principal = policy['principals'].get(proof['principal_key_sha256'])
            if not principal or principal != proof['principal']:
                raise ValueError('revoked principal')
            expected = self.scope(principal, proof['principal_key_sha256'], agent, mode, task, cwd)
            if (type(proof['admitted_at']) not in (int, float)
                    or not math.isfinite(proof['admitted_at'])
                    or proof != {**expected, 'admitted_at': proof['admitted_at']}):
                raise ValueError('changed authorization scope')
        except (KeyError, TypeError, ValueError, OSError):
            raise JobError('TRUSTED_DISPATCH_PROOF_INVALID', 'Admission revoked or scope changed before execution') from None

    def status(self):
        try:
            policy = self.policy()
            return {'policy': POLICY, 'configured': True, 'valid': True,
                    'write_autonomous_enabled': policy['enabled'],
                    'per_job_token_required': False, 'transport': 'secure_mcp'}
        except JobError:
            return {'policy': POLICY, 'configured': True, 'valid': False,
                    'write_autonomous_enabled': False, 'per_job_token_required': False}


def check_execution(proof, agent, mode, task, cwd):
    """Revalidate against current policy just before worker/process launch."""
    authority = TrustedDispatch.configured()
    trusted_proof = isinstance(proof, dict) and proof.get('schema') == PROOF
    if authority is not None:
        if not trusted_proof:
            raise JobError('TRUSTED_DISPATCH_PROOF_INVALID', 'Trusted admission proof required')
        authority.check_start(proof, agent, mode, task, cwd)
    elif trusted_proof:
        raise JobError('TRUSTED_DISPATCH_NOT_CONFIGURED', 'Trusted policy removed before execution')
    elif mode == 'autonomous':
        from .authorization import Authority
        Authority.configured().check_start(proof, agent, task, cwd)
