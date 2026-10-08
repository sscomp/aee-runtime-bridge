# Independent agent operations

Read [README](../README.md), then [deployment](deployment.md). You need no earlier
chat, Master Plan or local reports. Detect the host and stop on conflicts before
requesting operator authorization for provisioning, inference or tunnel linkage.
Offline tests/builds are safe preparation; production installation is separate.

## Local acceptance after an approved installation

1. Check `/health` with the deployment command. Inspect its body privately and
   verify the runtime source commit matches the approved release. HTTP 200 alone
   does not prove dispatch qualification or inference success.
2. Use an MCP SDK/Inspector HTTP client to `http://127.0.0.1:8791/mcp`, with the
   dedicated `Authorization` bearer credential supplied privately. Initialize a
   session, send `notifications/initialized`, and call `tools/list`. Require exactly
   `aee_status`, `aee_agents`, `aee_dispatch`, `aee_job_status`, `aee_job_result`.
   Missing/invalid credentials must fail 401; `aee_exec` must be unavailable.
3. Call `aee_status` and `aee_agents`. Codex identity must match the reviewed
   manifest. Hermes/Claude discovery is informational; neither is a restricted
   dispatch target. Never paste raw host details or credentials into public logs.
4. After operator approval for model inference, call `aee_dispatch` with
   `agent="codex"`, `mode="read_only"`, the approved immutable source directory,
   and task `Summarize README.md without modifying files or using Code Mode.`
   The only execution profile is `codex-readonly-high` (configured model
   `gpt-6.1-sol`, reasoning `high`). Do not infer the observed model from settings.
5. Require `queued` and a valid job ID; poll `aee_job_status` with a bounded wait
   (maximum configured deadline plus cleanup allowance). A concurrent second
   admission must return `BUSY`. Poll `aee_job_result` after terminal state.
6. Accepted success requires `ok=true`, `status=completed`, exit zero, bounded
   summary, ordered timestamps and `aee-completed-v1` native/broker completion
   proof. Preserve structured failures; a final sentence or zero exit alone is
   not proof. Check lease release, private store modes and unchanged source.
7. Check negative agent/mode/profile/traversal inputs are rejected before launch.
   Run destructive probes only in an isolated qualification fixture, never on
   production. Then perform [planned ChatGPT E2E](chatgpt-mcp.md).

For the exact client/schema/auth examples and lifecycle assertions, see
[`test_p2b_protocol.py`](../tests/mcp/test_p2b_protocol.py). The README offline
smoke executes them without live listeners or real credentials.

## Required CI and its limits

- Four profile jobs: full current Epic 9 suite; mini/edge/developer supported
  subsets; dry-run install, real CLI import smoke and 25 shell wrapper assertions.
- MCP runtime job: full published portable runtime suite, compiled C fixtures,
  broker socket/SSE stubs, auth/admission/result negative cases and bundle/migration
  tests; documentation/config/secret gate and import/build checks.
- HTTP compatibility job: hash-locked API closure, temporary database tests for
  job claims/heartbeat/cancel/reaper, workers and runtime dispatch.
- Merge Gate depends on **all three job groups**, fails on skipped/cancelled/failed jobs,
  and never deploys a service. Branch protection is configured by the repository
  administrator; workflow success does not itself enforce administrative policy.

The HTTP `full` profile suite is not the entire repository suite. Published
runtime discovery is separately required. Optional installed-native qualification
reports its skips; clean-host cgroup/systemd/inference/ChatGPT acceptance remains
Stage 3 and cannot be inferred from CI. See [runtime tests](../tests/mcp/README.md)
and [retirement ownership](legacy-retirement.md).
