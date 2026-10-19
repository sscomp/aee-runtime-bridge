"""Versioned operator policy for generic adapters, never supplied by MCP tasks."""
from dataclasses import asdict, dataclass, fields, replace
import hashlib
import json
import math
import os
from pathlib import Path
import stat

from .store import AGENT_NAMES, JobError


@dataclass(frozen=True)
class ExecutionBudget:
    wall_timeout_seconds: float = 3600
    idle_timeout_seconds: float | None = None
    max_turns: int | None = None
    memory_capture_bytes: int = 65536
    spool_max_bytes_per_stream: int = 64 * 1024**2
    spool_max_bytes_per_job: int = 96 * 1024**2
    spool_max_bytes_total: int = 512 * 1024**2
    result_inline_bytes: int = 16384
    log_excerpt_bytes: int = 2000
    retention_seconds: int = 86400
    max_concurrent_jobs: int = 1
    on_spool_full: str = 'fail'
    parser_mode: str = 'structured-v1'

    def __post_init__(self):
        bounds = {
            'wall_timeout_seconds': (.01, 7 * 86400),
            'memory_capture_bytes': (1024, 1024**2),
            'spool_max_bytes_per_stream': (1, 1024**3),
            'spool_max_bytes_per_job': (1, 2 * 1024**3),
            'spool_max_bytes_total': (1, 8 * 1024**3),
            'result_inline_bytes': (1, 65536), 'log_excerpt_bytes': (1, 2000),
            'retention_seconds': (1, 30 * 86400), 'max_concurrent_jobs': (1, 1)}
        for name, (low, high) in bounds.items():
            value = getattr(self, name)
            kind = (int, float) if name == 'wall_timeout_seconds' else (int,)
            if type(value) not in kind or not low <= value <= high or not math.isfinite(value):
                raise JobError('INVALID_EXECUTION_BUDGET', 'Invalid budget field: ' + name)
        if self.idle_timeout_seconds is not None:
            v = self.idle_timeout_seconds
            if type(v) not in (int, float) or not .01 <= v <= self.wall_timeout_seconds or not math.isfinite(v):
                raise JobError('INVALID_EXECUTION_BUDGET', 'Invalid idle timeout')
        if self.max_turns is not None and (type(self.max_turns) is not int or not 1 <= self.max_turns <= 100000):
            raise JobError('INVALID_EXECUTION_BUDGET', 'Invalid max_turns')
        if not self.spool_max_bytes_per_stream <= self.spool_max_bytes_per_job <= self.spool_max_bytes_total:
            raise JobError('INVALID_EXECUTION_BUDGET', 'Spool budgets must satisfy stream <= job <= total')
        if self.on_spool_full != 'fail' or self.parser_mode != 'structured-v1':
            raise JobError('INVALID_EXECUTION_BUDGET', 'Unsupported spool/parser policy')

    def record(self):
        return asdict(self)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


class BudgetPolicy:
    """Load once, validate all adapters, snapshot at admission; no hot reload."""
    def __init__(self, path=None, *, legacy=None):
        data = {'schema': 1, 'defaults': {}, 'adapters': {}}
        if path:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
                with os.fdopen(fd, 'rb') as f:
                    info = os.fstat(f.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022:
                        raise ValueError('policy ownership/mode')
                    raw = f.read(65537)
                if len(raw) > 65536:
                    raise ValueError('policy size')
                data = json.loads(raw, object_pairs_hook=_unique)
            except (OSError, ValueError):
                raise JobError('INVALID_EXECUTION_BUDGET', 'Policy must be bounded, unique-key JSON in an owner-controlled regular file') from None
        try:
            if not isinstance(data, dict) or set(data) - {'schema', 'defaults', 'adapters'} or type(data.get('schema')) is not int or data['schema'] != 1:
                raise ValueError()
            defaults, adapters = data.get('defaults', {}), data.get('adapters', {})
            allowed = {f.name for f in fields(ExecutionBudget)}
            if not isinstance(defaults, dict) or set(defaults) - allowed or not isinstance(adapters, dict) or set(adapters) - AGENT_NAMES:
                raise ValueError()
            self.budgets = {}
            for agent in AGENT_NAMES:
                overrides = adapters.get(agent, {})
                if not isinstance(overrides, dict) or set(overrides) - allowed:
                    raise ValueError()
                value = ExecutionBudget(**{**(legacy or {}), **defaults, **overrides})
                if agent != 'claude' and value.max_turns is not None:
                    raise ValueError()
                self.budgets[agent] = value
        except (ValueError, TypeError):
            raise JobError('INVALID_EXECUTION_BUDGET', 'Invalid policy schema, adapter or field') from None
        canonical = {a: b.record() for a, b in sorted(self.budgets.items())}
        self.revision = 'execution-budget-v1:' + hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()

    def for_agent(self, agent):
        return self.budgets[agent]
