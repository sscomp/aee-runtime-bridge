#!/usr/bin/env python3
"""Fresh-process, fixture-only Product result projection; no gateway initialization.

Compiles actual gateway function bodies without server, environment config or
live job-store access. Requires an explicit fixture store under /tmp. Never connects to a model/broker.
"""
import ast
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from aee.mcp_runtime.store import JobStore, JobError
from aee.mcp_runtime.result_contract import validate_success


def gateway_functions(store):
    tree = ast.parse((ROOT / 'mcp_gateway.py').read_text())
    wanted = {'aee_job_result', 'job_error_payload', 'redact', 'redact_obj'}
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in wanted]
    assert {n.name for n in nodes} == wanted
    for n in nodes:
        n.decorator_list = []
    # Extract the actual redaction constants too, without loading environment.
    constants = [n for n in tree.body if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id in {'_SECRET_VALUE_RE', '_SECRET_ENV_NAMES'} for t in n.targets)]
    import re
    namespace = {'json': json, 'asyncio': asyncio, 're': re, 'Any': object, 'Dict': dict,
                 'JOB_STORE': store, '_JobError': JobError, 'validate_success': validate_success}
    exec(compile(ast.Module(body=constants + nodes, type_ignores=[]), str(ROOT / 'mcp_gateway.py'), 'exec'), namespace)
    return namespace


def main():
    store_path = Path(sys.argv[1]).resolve()
    if not store_path.is_relative_to('/tmp'):
        raise SystemExit('Fixture path denied')
    store = JobStore(store_path)
    try:
        result = asyncio.run(gateway_functions(store)['aee_job_result'](sys.argv[2]))
        print(result)
    finally:
        store.close()


if __name__ == '__main__':
    main()
