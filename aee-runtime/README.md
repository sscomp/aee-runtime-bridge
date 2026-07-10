# AEE-4 Part B — AEE Lightweight Agent Runtime

The first AEE-4 conformant Worker runtime. **In-house**:
we do not install or wrap an external "Pi Agent" / "Pi
Coding Agent" package. The runtime is a from-scratch
Node.js LLM agent plus a small Python daemon. The
naming "AEE Lightweight Agent Runtime" is deliberate —
it distinguishes the AEE-shipped in-house runtime from
any third-party "Pi Agent" product (e.g.
`badlogic/pi-mono`, `earendil-works/pi-mono`,
`pi-agent-core`), none of which are used here. See
`docs/AEE4_FINAL_VALIDATION_REPORT.md` §1 for the
rationale and the original Phase 2 research report.

The runtime speaks the [Worker Runtime Contract](../runtime/Worker_Runtime_Contract.md)
to the bridge at `/v1/...` and is independent of Hermes.

```
┌────────────────────────────────────┐
│        Bridge (FastAPI)            │
│   /v1/jobs/claim, /v1/workers/...  │
└────────────────┬───────────────────┘
                 │ HTTP
                 │
        ┌────────▼─────────┐
        │   aee_runtime.py │  (this dir)
        │   Python daemon  │
        └────────┬─────────┘
                 │ subprocess + JSON spec
                 │
        ┌────────▼─────────┐
        │  aee-runtime.js  │  (runtime/)
        │   Node.js LLM    │
        └────────┬─────────┘
                 │ OpenAI-compatible API
                 │
        ┌────────▼─────────┐
        │   LLM provider   │  (configurable)
        └──────────────────┘
```

## What's here

- **`aee_runtime.py`** — the Python daemon. Reads the YAML
  config, registers with the bridge (`worker_type=
  "aee_lightweight"`), claims jobs, spawns the node
  runtime per claim, streams logs, heartbeats with the
  status model, completes / fails.
- **`runtime/`** — the Node.js LLM half. ~250 lines, 6 files.
  Reads a JSON spec, calls an OpenAI-compatible LLM with
  tool-calling, runs shell / file_read / file_write inside
  a per-job workdir + allowlist, prints a single JSON result
  on stdout.
- **`config.example.yaml`** — the YAML config template.
  Copy to `config.yaml`, fill in, point the daemon at it.
- **`aee_runtime.provider.env.example`** — the provider env
  file template. Copy to `provider.env`, set
  `PI_PROVIDER_*` variables, `chmod 600`.
- **`systemd/aee-runtime.service`** — reference systemd unit
  (developer laptops; this host uses supervisord).
- **`supervisor/aee-runtime.conf`** — production supervisord
  unit (matches the bridge's unit at
  `hermes-runtime-bridge/supervisor/hermes-runtime-bridge.conf`).
- **`tests/test_aee_runtime.py`** — 13 unit tests for the
  daemon. Mocked HTTP and subprocess; no real bridge.
- **`tests/test_smoke.py`** — 2 closed-loop tests. The
  second spins up a real uvicorn bridge on a random port,
  runs the daemon as a subprocess, and asserts the full
  register → claim → execute → complete cycle works.
- **`tests/test_live_provider.py`** — **operator-only**
  live-provider smoke test (NOT in CI). Exercises a
  real Ollama or RouteLLM provider through the full
  register → claim → tool.shell → logs → complete cycle.
  See `tests/test_live_provider.py` for the operator
  runbook.

## Quick start

```bash
# 1. Install dependencies.
cd /home/ubuntu/hermes-runtime-bridge
.venv/bin/pip install -r aee-runtime/requirements.txt
cd aee-runtime/runtime && npm install && cd ../..

# 2. Configure.
cp aee-runtime/config.example.yaml aee-runtime/config.yaml
cp aee-runtime/aee_runtime.provider.env.example aee-runtime/provider.env
chmod 600 aee-runtime/provider.env
# Edit provider.env: set PI_PROVIDER_API_KEY to a real key.

# 3. Run the daemon.
PYTHONPATH=. .venv/bin/python -u aee-runtime/aee_runtime.py \
    --config aee-runtime/config.yaml

# 4. In another shell, create a Job.
curl -X POST http://127.0.0.1:8787/v1/jobs \
  -H "Authorization: Bearer $BRIDGE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "echo test",
    "input": "echo hello from the aee-runtime",
    "target_runtime": "aee_lightweight",
    "required_capabilities": ["tool.shell"]
  }'
# Returns: {"version": "v1", "job_id": "TASK-...", ...}

# 5. Watch the daemon claim and complete it.
# 6. Poll the bridge for the final state.
curl -H "Authorization: Bearer $BRIDGE_API_KEY" \
  http://127.0.0.1:8787/v1/jobs/TASK-...
```

## Install on the production host (supervisord)

```bash
# 1. Make sure the bridge is up (supervisord manages it).
sudo supervisorctl status hermes-runtime-bridge

# 2. Install the AEE Runtime unit.
sudo cp /home/ubuntu/hermes-runtime-bridge/aee-runtime/supervisor/aee-runtime.conf \
        /etc/supervisor/conf.d/aee-runtime.conf
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl status aee-runtime

# 3. Tail the logs.
sudo tail -F /var/log/aee-runtime.err.log
```

## Install on a developer laptop (systemd)

```bash
# 1. Install the unit.
sudo cp /home/ubuntu/hermes-runtime-bridge/aee-runtime/systemd/aee-runtime.service \
        /etc/systemd/system/aee-runtime.service
sudo systemctl daemon-reload
sudo systemctl enable --now aee-runtime

# 2. Watch the logs.
journalctl -u aee-runtime -f
```

## Run the tests

```bash
# Node runtime tests (3 tests, ~300ms).
cd /home/ubuntu/hermes-runtime-bridge/aee-runtime/runtime
npm test

# Python daemon unit tests (13 tests, ~30ms).
cd /home/ubuntu/hermes-runtime-bridge
PYTHONPATH=. .venv/bin/python -m unittest aee-runtime.tests.test_aee_runtime

# Closed-loop smoke test (2 tests, ~3s; one spins up a real bridge).
PYTHONPATH=. .venv/bin/python -m unittest aee-runtime.tests.test_smoke

# AEE test suite (43 tests, ~1.2s).
PYTHONPATH=. .venv/bin/python -m unittest \
    tests.test_workers_api tests.test_jobs_api tests.test_manager_aee1 \
    tests.test_adapter tests.test_safety tests.test_migration_aee1
```

## Conformance

This is the **reference implementation** of the AEE-4
Worker Runtime Contract
(`hermes-runtime-bridge/docs/runtime/Worker_Runtime_Contract.md`).
The conformance checklist (§9 of the contract) maps to:

| Checklist | Where |
|---|---|
| §9.1 Register | `aee_runtime.py:_register()` |
| §9.2 Heartbeat (Worker) | `aee_runtime.py:_heartbeat_loop()` |
| §9.3 Claim | `aee_runtime.py:_claim_and_execute_once()` |
| §9.4 Execute | `aee_runtime.py:_execute_job()` + `runtime/lib/{loop,tools,provider}.js` |
| §9.5 Complete / Fail | `aee_runtime.py:_complete_job()` / `_fail_job()` |
| §9.6 Lifecycle hygiene | `aee_runtime.py:_send_offline_then_exit()` + the runtime's allowlist / workdir containment |

## Status model (AEE-4 Worker Runtime Contract §5)

The daemon sends the following statuses via
`POST /v1/workers/{id}/heartbeat`:

- **`idle`** — between Jobs (default after register)
- **`busy`** — currently executing a Job (with `job_id` set)
- **`offline`** — final heartbeat before process exit

The full 5-value vocabulary (`idle`, `busy`, `offline`,
`draining`, `error`) is **defined in the contract and
accepted by the bridge** (the API rejects unknown
statuses with HTTP 400). The daemon currently exercises
the 3 values above; **`draining` and `error` are
reserved in the source code (see `STATUS_DRAINING` and
`STATUS_ERROR` constants in `aee_runtime.py`) but are
not yet sent by the runtime path**:

- `draining` is not exercised because v1 has no
  external drain signal (e.g. SIGTERM-with-grace) — a
  drain would need a separate supervisor handshake
  that AEE-5+ will design.
- `error` is not exercised because every daemon
  exception path already calls `/v1/jobs/{id}/fail`
  with the error message; sending `status=error` to
  the bridge is redundant when the in-flight Job is
  already marked `failed`.

See `docs/AEE4_FINAL_VALIDATION_REPORT.md` §4 for the
detailed status coverage matrix.

## Exit code map (runtime → daemon → bridge /fail)

| Node runtime exit | Daemon's `error` field |
|---|---|
| 0 | (success) → `output_text` from result |
| 2 | `invalid job spec` |
| 3 | `provider failure` |
| 4 | `step timeout` |
| 5 | `allowlist blocked` |
| 6 | `workdir violation` |
| 7 | `unknown tool` |
| 8 | `internal error` |
| 9 | `lease expired (killed)` |
| 124 | `job exceeded timeout_seconds` (SIGKILL by daemon) |

## Open questions

See `hermes-runtime-bridge/docs/runtime/Worker_Runtime_Contract.md`
§10. The AEE Lightweight Agent Runtime implements the
v1 contract surface; AEE-5+ work will address the
open questions, including the `draining` / `error`
status paths and the future RuntimeAdapter's
`health()` method.
