# Macmini-N2 local-worker dispatch

The ChatGPT dispatch incident on N2 had two independent causes. The stored
task contained only a smoke-test title. The worker searched for a test and
called the same AEE gateway to dispatch another job; the one-active-job
limit correctly returned `BUSY`. Codex also emitted an `item.type=error`
startup notice about ignoring the home project's `notify` setting. The
generic adapter treated that informational notice as a fatal tool outcome.
Neither observation established a Docker Engine failure.

## Task input and local execution

Put the complete commands, scope and acceptance conditions in `task`, not
just a title. `prompt` remains a legacy alias. When callers send both fields
with different values, the gateway joins them in task-then-prompt order
before admission, authorization hashing and job persistence. Identical
values are retained once. Metadata does not substitute for instructions.

Generic CLI adapters add a fixed local-worker context on stdin. It tells
the agent that it is already running on the target host, must execute the
dispatched work directly, and must not redispatch the job through AEE.
The context's schema, mode and canonical directory are retained under
`execution.worker_context`; the original admitted task and its scoped
authorization remain unchanged. This is execution guidance, not a new
permission grant or an increase to the single-active-job limit.

A read-only workload can be tested in the already authorized autonomous
profile. State the workload's no-write requirements explicitly. Use
`mode=read_only` when a native read-only sandbox is required; calling a
workload read-only does not change an autonomous job's actual permissions.

## Codex startup notices

For generic Codex adapters, only the observed, exact native notice about
an ignored project-local `notify` setting is nonfatal, and only before the
first `turn.started`. It is counted as `startup_config_notices` in streamed
tool evidence; its raw text remains in the private spool. Other error
items, the same text during a turn, failed/declined non-command items,
ambiguous command exits, failed turns and missing terminal completion
remain failures. Duplicate diagnostic messages are rejected as ambiguous
JSON in both parser paths. The approved broker's receipt-required path
retains strict error-item handling.

Do not convert an old failed job to completed: preserve it as incident
evidence and dispatch a fresh, concrete verification task. A successful
CLI turn is not sufficient evidence for a business task's acceptance
criteria; verify the requested commands and results as well.

## Regression checks

On macOS, use a canonical temporary directory because older adapter
fixtures compare temp paths literally and `/var` is a symlink:

```sh
TMPDIR=/private/tmp PYTHONPATH=.:tests/mcp .venv/bin/python -m unittest \
  test_n2_dispatch_regression test_codex_tool_failure_recovery_semantics \
  test_os_native_autonomous test_secure_mcp_trusted_dispatch \
  test_macos_native_execution test_p2c_job_adapters test_bootstrap_baseline -q
```

Then use ChatGPT's N2 connector to dispatch a concrete read-only Docker
inspection, poll status and retrieve the result. Verify the private spool
shows local command execution and no recursive AEE dispatch. Keep original
failed records and provider credentials private.
