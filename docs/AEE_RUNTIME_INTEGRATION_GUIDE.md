# AEE Runtime Integration Guide

**Version:** 1.0
**Date:** 2026-07-10
**Status:** Shipped (AEE-4 Part B freeze)
**Source spec:** `Abacus/AEE4_WORKER_RUNTIME_CONTRACT_AND_PI_REFERENCE_IMPLEMENTATION.md`
**Companion documents:**
- `docs/runtime/Worker_Runtime_Contract.md` — the canonical
  contract every runtime must satisfy.
- `docs/AEE4_PI_REFERENCE_IMPLEMENTATION_REPORT.md` — the
  first conformant runtime (AEE Lightweight Agent Runtime; in-house, not a third-party "Pi Agent" package).
- `docs/AEE_RUNTIME_INTEGRATION_GUIDE.md` — this document.

---

## 1. Purpose

This guide is the **Runtime integration handbook** for
any developer who wants to add a new runtime to AEE.
It's written as a general template: where the API
surface is runtime-neutral, we describe the protocol;
where the runtime is Pi-specific (Node.js + OpenAI
provider), we describe that choice explicitly so a
future Claude Code / GPT / MCP runtime can follow the
same shape with a different runtime internals.

AEE Lightweight Agent Runtime is the **first conformant runtime**. The
guide is shaped around the same steps a future runtime
(Claude Code in AEE-5, MCP in AEE-9) will go through.

The conformance checklist is in
`docs/runtime/Worker_Runtime_Contract.md` §9. If a
runtime fails any item in the checklist, it is **not
conformant** and should not be added to
`AdapterRegistry`.

## 2. Install prerequisites

Every runtime needs:

- A **bridge reachable over HTTP** at a stable URL.
  Default: `http://127.0.0.1:8787`. The runtime reads
  this from its config.
- A **bearer key** (`BRIDGE_API_KEY` or
  `DISPATCHER_API_KEY`) that the bridge accepts. The
  runtime reads this from its config (which reads it
  from an env var; never commit a real key).
- The runtime's own dependencies (Node.js, Python,
  Go, etc., depending on the runtime).
- A **provider env file** (only if the runtime needs
  an LLM / external API). AEE Lightweight Agent Runtime uses
  `provider.env`; future runtimes that don't call an
  LLM (e.g. a shell-only runtime) can skip this.

For AEE Lightweight Agent Runtime specifically:

```bash
# 1. Install the daemon deps (PyYAML is the only new dep).
cd /home/ubuntu/hermes-runtime-bridge
.venv/bin/pip install -r aee-runtime/requirements.txt

# 2. Install the runtime deps (openai, commander, dotenv, zod).
cd aee-runtime/runtime
npm install
```

For a Claude Code Worker (AEE-5 — future):

```bash
# 1. The Claude Code SDK (npm: @anthropic-ai/claude-code).
# 2. The daemon deps (same as Pi: PyYAML + a stdlib HTTP
#    client — we deliberately avoid httpx to keep the
#    daemon stdlib-only where possible).
```

For an MCP Worker (AEE-9 — future):

```bash
# 1. The MCP SDK (npm: @modelcontextprotocol/sdk).
# 2. The daemon deps (same).
```

## 3. Configuration

Every runtime reads a config file. The convention is
YAML for the daemon's config and a `KEY=VALUE` env file
for provider secrets. Templates are checked in next to
the runtime.

### 3.1 The daemon's YAML config

AEE Lightweight Agent Runtime's `config.example.yaml` (template; copy to
`config.yaml`):

```yaml
bridge_base_url: "http://127.0.0.1:8787"
bridge_api_key: "${BRIDGE_API_KEY}"  # from env, never committed

worker_id: "aee-runtime-m2-001"
worker_name: "aee-runtime-m2-001"
worker_type: "aee_lightweight"             # matches the job's adapter_name
capabilities:
  - "runtime.pi"
  - "tool.shell"
  - "tool.python"
  - "tool.git"
  - "tool.filesystem"

workdir_root: "/path/to/per-job-workdir-root"
workdir_allowlist:
  - "/path/to/per-job-workdir-root"  # must contain workdir_root

max_concurrent: 1
claim_interval_sec: 5
heartbeat_interval_sec: 15
log_max_bytes: 4096

allowlist_commands: ["ls", "cat", "echo", ...]  # shell first-token allowlist

env_file: "/path/to/provider.env"
runtime_path: ""                                # default: <repo>/aee-runtime/runtime/aee-runtime.js
runtime_flags: []                                # e.g. ["--dry-run"] for tests
```

A future Claude Code Worker would have the same shape,
with `worker_type=claude_code` and
`runtime_path=.../claude-code-runtime.js`.

### 3.2 The provider env file

AEE Runtime's `aee_runtime.provider.env.example` (template;
copy to `provider.env`, `chmod 600`):

```
PI_PROVIDER_BASE_URL=https://api.openai.com/v1
PI_PROVIDER_API_KEY=sk-REPLACE-ME
PI_PROVIDER_MODEL=gpt-4o-mini

# Optional:
# PI_PROVIDER_RUNTIME_NAME=pi
# PI_PROVIDER_RUNTIME_VERSION=0.1.0
```

The daemon refuses to start if `PI_PROVIDER_API_KEY` is
a placeholder value. The convention is the same for any
runtime: three `PI_PROVIDER_*` variables (base URL, API
key, model) — for Claude Code it would be
`PI_PROVIDER_*` for Anthropic, etc.

### 3.3 Substitution rules

- The daemon reads `${VAR_NAME}` in the YAML and
  substitutes from `os.environ`. The `BRIDGE_API_KEY`
  convention (above) is a use of this.
- The provider env file uses simple `KEY=VALUE` (no
  shell expansion, no quotes). The runtime reads it
  via `dotenv` (Node) or stdlib parser (Python).
- A runtime MUST refuse to start if its config has an
  unresolved `${VAR_NAME}` or a placeholder value in
  the provider env file.

## 4. Service (systemd / supervisord)

Every runtime runs as a long-lived process. The
convention is one runtime per supervisor / systemd
unit. The unit's `priority` is set to start after the
bridge (priority 20), so a runtime never claims a Job
before the bridge is up.

### 4.1 Supervisord (this host's convention)

AEE Runtime's `aee-runtime/supervisor/aee-runtime.conf`:

```ini
[program:aee-runtime]
command=/home/ubuntu/hermes-runtime-bridge/.venv/bin/python
    -u
    /home/ubuntu/hermes-runtime-bridge/aee-runtime/aee_runtime.py
    --config /home/ubuntu/hermes-runtime-bridge/aee-runtime/config.yaml
directory=/home/ubuntu/hermes-runtime-bridge
user=ubuntu
autostart=true
autorestart=true
startsecs=10
startretries=3
stopwaitsecs=15
stopsignal=TERM
stdout_logfile=/var/log/aee-runtime.out.log
stdout_logfile_maxbytes=10MB
stdout_logfile_backups=5
stderr_logfile=/var/log/aee-runtime.err.log
stderr_logfile_maxbytes=10MB
stderr_logfile_backups=5
priority=30
```

Install:

```bash
sudo cp aee-runtime/supervisor/aee-runtime.conf \
        /etc/supervisor/conf.d/aee-runtime.conf
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl status aee-runtime
```

### 4.2 systemd (developer-laptop convention)

AEE Lightweight Agent Runtime's `aee-runtime/systemd/aee-runtime.service`:

```ini
[Unit]
Description=AEE-4 Part B — AEE Lightweight Agent Runtime daemon
After=network-online.target hermes-runtime-bridge.service
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/hermes-runtime-bridge
ExecStart=/home/ubuntu/hermes-runtime-bridge/.venv/bin/python \
    -u \
    /home/ubuntu/hermes-runtime-bridge/aee-runtime/aee_runtime.py \
    --config /home/ubuntu/hermes-runtime-bridge/aee-runtime/config.yaml
Restart=on-failure
RestartSec=10
TimeoutStopSec=15
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/ubuntu/hermes-runtime-bridge/runtime_data/aee-runtime

[Install]
WantedBy=multi-user.target
```

Install:

```bash
sudo cp aee-runtime/systemd/aee-runtime.service \
        /etc/systemd/system/aee-runtime.service
sudo systemctl daemon-reload
sudo systemctl enable --now aee-runtime
```

A future Claude Code Worker unit would be a near-copy
of either file, with `aee-runtime` → `claude-code-worker`
and the `ExecStart` pointing to the Claude Code
runtime.

## 5. Register / Claim / Heartbeat / Complete API walkthrough

This section is **runtime-agnostic**. It documents the
5 required + 6 supplementary endpoints every runtime
implements. Full request / response shapes are in
`docs/runtime/Worker_Runtime_Contract.md` §2.

### 5.1 Register

```bash
curl -X POST $BRIDGE_URL/v1/workers/register \
  -H "Authorization: Bearer $BRIDGE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "worker_name": "...",
    "worker_type": "<your-runtime>",
    "capabilities": ["runtime.<your-runtime>", "tool.shell", ...],
    "workdir_allowlist": ["<per-job-workdir-root>"],
    "max_concurrent": 1,
    "runtime_name": "<your-runtime>",
    "runtime_version": "<x.y.z>",
    "operating_system": "linux",
    "architecture": "x86_64",
    "python_version": "3.11.2",
    "node_version": "v22.22.2",
    "git_version": "2.34.1",
    "start_time": "2026-07-10T00:00:00Z"
  }'
# Response: {"version": "v1", "worker_id": "...", "registered": true, ...}
```

### 5.2 Heartbeat (Worker)

The runtime calls this every `heartbeat_interval_sec`
(seconds; default 15). The body includes the 5-value
status:

```bash
curl -X POST $BRIDGE_URL/v1/workers/$WORKER_ID/heartbeat \
  -H "Authorization: Bearer $BRIDGE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "status": "busy",
    "status_message": "executing TASK-...",
    "job_id": "TASK-..."
  }'
```

A status change stamps `last_status_change_at` on the
server. A same-status heartbeat is a no-op for the
status columns. The runtime MUST heartbeat at least
every `stale_running_sec / 3` (default 20 s) while it
has a running Job.

### 5.3 Claim

The runtime calls this in a loop, sleeping
`claim_interval_sec` (default 5 s) on 404:

```bash
curl -X POST $BRIDGE_URL/v1/jobs/claim \
  -H "Authorization: Bearer $BRIDGE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "worker_id": "$WORKER_ID",
    "worker_type": "<your-runtime>",
    "capabilities": ["runtime.<your-runtime>", "tool.shell", ...]
  }'
# Response: {"version": "v1", "job_id": "...", "claim_token": "<plain>",
#            "title": "...", "input": "...", "timeout_seconds": 900,
#            "required_capabilities": [...]}
```

The runtime stores `claim_token` (or its sha256 hash)
locally; never logs the plain token after this point.

### 5.4 Heartbeat (Job)

The runtime calls this at the same cadence as the
Worker heartbeat, while a Job is in `running`:

```bash
curl -X POST $BRIDGE_URL/v1/jobs/$JOB_ID/heartbeat \
  -H "Authorization: Bearer $BRIDGE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"claim_token_hash": "<sha256 of claim_token>"}'
```

### 5.5 Logs

The runtime streams stderr (or any per-step output) to
the bridge as the Job runs:

```bash
curl -X POST $BRIDGE_URL/v1/jobs/$JOB_ID/logs \
  -H "Authorization: Bearer $BRIDGE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"claim_token_hash": "<sha256>", "line": "shell: echo hello"}'
```

The bridge appends the line to `<bridge_root>/logs/{job_id}.log`
and emits a `task_events` row of kind `"log"`.

### 5.6 Complete / Fail

On success:

```bash
curl -X POST $BRIDGE_URL/v1/jobs/$JOB_ID/complete \
  -H "Authorization: Bearer $BRIDGE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "claim_token_hash": "<sha256>",
    "output_text": "the final answer",
    "usage": {"input_tokens": 50, "output_tokens": 12, "total_tokens": 62},
    "raw": {"finish_reason": "stop", "tool_calls": [...]}
  }'
```

On failure:

```bash
curl -X POST $BRIDGE_URL/v1/jobs/$JOB_ID/fail \
  -H "Authorization: Bearer $BRIDGE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"claim_token_hash": "<sha256>", "error": "step timeout after 30s"}'
```

## 6. Smoke test

Every runtime MUST ship a closed-loop smoke test that:

1. Spins up a real bridge on a random port.
2. Runs the runtime as a subprocess.
3. Creates a Job via direct SQL on the bridge's DB.
4. Waits up to N seconds for the Job to reach a
   terminal state.
5. Asserts the Job is `completed` and the output_text
   contains the expected marker.

AEE Lightweight Agent Runtime's smoke test is
`aee-runtime/tests/test_smoke.py::TestEndToEndDryRun`. A
future Claude Code / MCP smoke test copies this
template and changes:

- The `runtime_flags` to use `--dry-run` (or a real
  LLM call if the test has a key).
- The expected `output_text` marker.
- The `worker_type` and `capabilities` in the register
  body.

### 6.1 Operator-only live provider smoke test

The AEE Lightweight Agent Runtime ALSO ships a second
smoke test that exercises a real LLM provider
(Ollama, RouteLLM, or any OpenAI-compatible
endpoint):

- **File:** `aee-runtime/tests/test_live_provider.py`
- **Off by default.** The standard `python -m unittest`
  invocation will skip it; the test is for operator
  use only.
- **How to run:**

      # 1. Set up the provider env file.
      $ cp aee-runtime/aee_runtime.provider.env.example \\
            aee-runtime/provider.env
      $ chmod 600 aee-runtime/provider.env
      $ $EDITOR aee-runtime/provider.env   # set real key

      # 2. Set the env var to enable the test.
      $ export AEE_RUNTIME_RUN_LIVE_PROVIDER=1

      # 3. Run.
      $ PYTHONPATH=. .venv/bin/python -m unittest \\
            aee-runtime.tests.test_live_provider

- **What it does:** spins up a real uvicorn bridge,
  launches the daemon against a real provider, creates
  a Job whose instruction tells the LLM to invoke the
  `shell` tool to echo a known string, and asserts the
  Job reaches `completed` with the expected `output_text`.
- **API-key safety:** the test **never** writes
  `PI_PROVIDER_API_KEY` to stdout, stderr, the bridge
  DB, or the disk. It reads the key from `provider.env`,
  passes it to the daemon subprocess via the env file,
  and asserts at the end that the key is not present
  in any persisted row.
- **Why operator-only:** it needs a real provider and
  a real key, both of which CI doesn't have. CI
  continues to run only the offline tests
  (`test_aee_runtime.py` + `test_smoke.py`).

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `register failed: 401` | `BRIDGE_API_KEY` mismatch | Check `bridge_api_key` in the config resolves to the same key the bridge was started with. |
| `register failed: 400` `worker_id must match [A-Za-z0-9_.-]{1,64}` | worker_id has a bad character | Rename to use only alphanumeric + `_`, `.`, `-`. |
| `register failed: 500` `BRIDGE_API_KEY is not configured` | The bridge process has no `BRIDGE_API_KEY` env | Restart the bridge with `--env-file .env` (or equivalent). |
| `claim returns 404` forever | No queued Jobs | Create a Job via `POST /v1/jobs`. |
| `claim returns 409` "already has 1 running job(s) >= max_concurrent" | Worker is already busy | Set `max_concurrent` higher in the config, or wait for the current Job to finish. |
| `runtime: provider failure` | `PI_PROVIDER_API_KEY` is wrong, or `PI_PROVIDER_BASE_URL` is unreachable | Check the provider env file; `curl -I $PI_PROVIDER_BASE_URL` from the runtime's host. |
| `runtime: allowlist blocked` | The LLM tried to call a binary not in `allowlist_commands` | Add the binary to `allowlist_commands` in the config, or rewrite the prompt to use an allowed binary. |
| `runtime: workdir violation` | The LLM tried to read/write a path outside `workdir_root` | The runtime's per-job workdir is the only writable path; check the workdir allowlist. |
| `bridge: GET /v1/jobs/{id}` returns 404 | The Job is older than the bridge's history, or the bridge was restarted with an empty DB | Re-create the Job. |
| `logs not appearing in /tasks/{id}/events` | The runtime sent a log line but the bridge's `task_events` table isn't being read by anything | The events are in the DB; check the bridge's `task_events` table or the `/tasks/{id}/events` endpoint. |
| `worker gets reaped to timeout mid-job` | The runtime's heartbeat interval is too slow, or the runtime is blocked on a synchronous call | Reduce `heartbeat_interval_sec` (default 15 s, well under the 60 s threshold); check that long-running ops in the runtime are async or chunked. |

## 8. AEE Lightweight Agent Runtime–specific appendix

The non-runtime-neutral bits, kept here so future
runtimes can skip them.

### 8.1 Why split into Python + Node

The Python side is the bridge client (the
"talking to the dispatcher" half). The Node side is
the LLM client (the "talking to the model" half).
The split is deliberate for two reasons:

- A future MCP runtime might be Go + a TypeScript
  MCP client; the seam (a JSON spec file) is
  language-neutral.
- The bridge's HTTP client is stdlib-only (`urllib`);
  this keeps the daemon's dep tree minimal.

### 8.2 The job spec

The contract between the Python daemon and the Node
runtime is a single JSON file at
`{workdir_root}/jobs/{job_id}/spec.json`. See
`aee-runtime/runtime/lib/spec.js` for the zod schema. The
key fields are:

- `job_id` — the bridge's `TASK-...` id.
- `input` — the user instruction.
- `tools` — `["shell", "file_read", "file_write"]`.
- `max_steps` — cap on LLM iterations (default 20).
- `per_step_timeout_ms` — per-tool timeout (default
  30 000 ms).
- `max_output_bytes` — stdout/stderr cap (default
  204 800 bytes = 200 KB).
- `workdir` — the per-job workdir.
- `allowlist_cmds` — first-token allowlist for shell.
- `approval_required` — reserved for AEE-5+ human-in-
  the-loop.

### 8.3 The node runtime exit codes

| Code | Meaning |
|---|---|
| 0 | Success; single JSON result on stdout. |
| 2 | Invalid job spec. |
| 3 | Provider failure. |
| 4 | Step timeout. |
| 5 | Allowlist blocked. |
| 6 | Workdir violation. |
| 7 | Unknown tool. |
| 8 | Internal error. |
| 9 | Lease expired (killed). |
| 124 | SIGKILL by the daemon's `timeout_seconds`. |

The daemon maps these to `/v1/jobs/{id}/fail` calls
with the appropriate `error` field. See
`aee-runtime/aee_runtime.py:_RUNTIME_EXIT_TO_ERROR` for
the full map.

### 8.4 The `--dry-run` mode

The Node runtime supports `--dry-run`, which
short-circuits the provider call and prints a canned
response. The closed-loop smoke test
(`aee-runtime/tests/test_smoke.py`) uses this so the
test doesn't need a real LLM key.

To use `--dry-run` outside tests, add it to
`runtime_flags` in `config.yaml`:

```yaml
runtime_flags: ["--dry-run"]
```

The daemon will pass `--dry-run` to every spawn; the
runtime will skip the provider call and the Job's
`output_text` will be a deterministic canned response.

## 9. References

- `docs/runtime/Worker_Runtime_Contract.md` — the
  canonical contract. Read this first; the rest of the
  guide assumes §2 and §3 of the contract.
- `docs/AEE4_AEE_RUNTIME_REPORT.md` — the
  first conformant runtime (AEE Lightweight Agent
  Runtime, in-house).
- `aee-runtime/README.md` — operational guide for the
  AEE Lightweight Agent Runtime.
- `Abacus/AEE_MASTER_PLAN.md` — §9 (ADRs), §10.5
  (deliverables), §11 (release history).
- `docs/AEE3_CAPABILITY_MATCHING_REPORT.md` — the
  capability matcher (AEE-3) that gates the claim.
- `docs/AEE4_CAPABILITY_NAMING_SPEC.md` — the
  capability namespace (ADR-003).
