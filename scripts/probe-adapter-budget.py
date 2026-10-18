#!/usr/bin/env python3
"""Explicit local real-CLI qualification. Creates a NEW private state directory.

Uses host CLI auth under caller identity. Never binds HTTP, reads production job
records, changes a service or claims ChatGPT→MCP production verification.
"""
import argparse
import asyncio
import hashlib
import subprocess
import json
import os
from pathlib import Path
import secrets
import sys
import time

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--agent',required=True,choices=['codex','claude','pi'])
parser.add_argument('--scenario',choices=['smoke','chain','large','turn-limit','readonly'],default='smoke')
parser.add_argument('--state-dir',required=True,type=Path)
parser.add_argument('--wall-seconds',type=int,default=600)
args=parser.parse_args()
root=args.state_dir
if not root.is_absolute():parser.error('state-dir must be absolute')
root.mkdir(mode=0o700,parents=False,exist_ok=False)
fixture=root/'fixture';fixture.mkdir(mode=0o700)
(fixture/'RTK.md').write_text('RTK fixture: read-only commands only; no additional commands are required.\n')
(fixture/'AGENTS.md').write_text('Read only this fixture directory. Do not access parent directories, auth files or services. Do not edit files.\n')
(fixture/'input.txt').write_text('AEE_ISOLATED_FIXTURE_OK\n')
if args.scenario in {'chain','turn-limit'}:
    names=['START.txt']+[secrets.token_hex(8)+'.txt' for _ in range(19)]
    for i,name in enumerate(names):
        (fixture/name).write_text('Read next file: '+names[i+1]+'\n' if i+1<len(names) else 'Final answer must be CHAIN_OK_20\n')
    method = 'Use only your Read/read tool.' if args.agent != 'codex' else 'Use read-only shell cat commands, one file per command.'
    task=method+' Read START.txt, then follow its exact next-file pointer one file at a time until the terminal instruction. Do not use search, glob or directory listings. Do not guess filenames. Each file must be read in a separate sequential tool call. Return only the terminal marker.'
elif args.scenario=='large':
    (fixture/'large.txt').write_text('INTERMEDIATE_FIXTURE_LINE\n'*6500+'\nFinal marker: LARGE_OK\n')
    task='Read the entire large.txt using your read-only file tool in multiple calls if needed. Verify the last line. Return only LARGE_OK. Stay within this fixture directory.'
elif args.scenario=='readonly':
    task='List the available tools relevant to file changes and explain whether you can edit files under the current invocation. Do not attempt edits or shell commands. Read input.txt with your file read tool. End with AEE_ISOLATED_FIXTURE_OK.'
else:
    task='Read input.txt with your read-only file tool. Return only its one-line marker. Do not access other directories.'
policy=root/'policy.json'
policy.write_text(json.dumps({'schema':1,'defaults':{'wall_timeout_seconds':args.wall_seconds},'adapters':{'claude':{'max_turns':1 if args.scenario=='turn-limit' else None}}}))
policy.chmod(0o600)
os.environ.update(A3_JOB_STORE_DIR=str(root/'jobs'),AEE_EXECUTION_POLICY=str(policy),
                  AEE_MCP_PORT='18791',AEE_MCP_SURFACE='restricted',
                  MCP_BRIDGE_API_KEY="synthetic-probe-key",AEE_MCP_REQUIRE_AUTH='true')
# This probe has no approved native broker route; caller supplied deployment
# manifests are not copied into an isolated generic-adapter qualification.
os.environ.pop('AEE_RUNTIME_MANIFEST',None)
os.environ.pop('AEE_BROKER_CONTROL',None)
import mcp_gateway as gateway
from aee.mcp_runtime.store import TERMINAL, JobError
from aee.mcp_runtime.budget import ExecutionBudget
revision=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
source_hashes={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('aee/mcp_runtime').glob('*.py')}
started=time.monotonic()
try:
    data=gateway.dispatch_job(args.agent,task,str(fixture),'read_only')
    deadline=time.monotonic()+args.wall_seconds+15
    while time.monotonic()<deadline:
        data=gateway.JOB_STORE.get(data['job_id'])
        if data['status'] in TERMINAL:break
        time.sleep(.1)
    else:
        gateway._cancel_events[data['job_id']].set()
        raise RuntimeError('local probe terminal deadline exceeded')
    tool_result=json.loads(asyncio.run(gateway.aee_job_result(data['job_id'])))
    expected={'chain':'CHAIN_OK_20','turn-limit':None,'large':'LARGE_OK',
              'smoke':'AEE_ISOLATED_FIXTURE_OK','readonly':'AEE_ISOLATED_FIXTURE_OK'}[args.scenario]
    expected_final_verified=expected is None or expected in (data.get('summary') or '')
    data.pop('task',None)
    output={'real_cli':True,'production_e2e':False,'scenario':args.scenario,
            'elapsed_seconds':time.monotonic()-started,'record':data,
            'revision':revision,'source_hashes':source_hashes,
            'job_result_ok':tool_result.get('ok'),
            'job_result_bytes':len(json.dumps(tool_result,ensure_ascii=False).encode()),
            'expected_final_verified':expected_final_verified}
except JobError as error:
    output={'real_cli':True,'production_e2e':False,'scenario':args.scenario,
            'admission_error':{'error_code':error.code,'error':error.message}}
print(json.dumps(gateway.redact_obj(output),indent=2,ensure_ascii=False))
if args.scenario=='turn-limit':
    sys.exit(0 if output.get('record',{}).get('error_code')=='TURN_BUDGET_EXHAUSTED' else 1)
sys.exit(0 if output.get('record',{}).get('status')=='completed' and output.get('expected_final_verified') and output.get('job_result_ok') else 1)
