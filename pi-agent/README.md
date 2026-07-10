# AEE-4 Part B — Pi Agent Worker

The first AEE-4 conformant Worker runtime. Speaks the
[Worker Runtime Contract](../runtime/Worker_Runtime_Contract.md)
to the bridge at `/v1/...` and is independent of Hermes.

```
┌────────────────────────────────────┐
│        Bridge (FastAPI)            │
│   /v1/jobs/claim, /v1/workers/...  │
└────────────────┬───────────────────┘
                 │ HTTP
                 │
        ┌────────▼─────────┐
        │   pi_worker.py   │  (this dir)
        │   Python daemon  │
        └────────┬─────────┘
                 │ subprocess + JSON spec
                 │
        ┌────────▼─────────┐
        │ pi-agent-runtime │  (runtime/)
        │   Node.js LLM    │
        └────────┬─────────┘
                 │ OpenAI-compatible API
                 │
        ┌────────▼─────────┐
        │   LLM provider   │  (configurable)
        └──────────────────┘
```

## What's here

- **`pi_worker.py`** — the Python daemon. Reads the YAML
  config, registers with the bridge, claims jobs, spawns
  the node runtime per claim, streams logs, heartbeats with
  the 5-value status model, completes / fails.
- **`runtime/`** — the Node.js LLM half. ~250 lines, 6 files.
  Reads a JSON spec, calls an OpenAI-compatible LLM with
  tool-calling, runs shell / file_read / file_write inside
  a per-job workdir + allowlist, prints a single JSON result
  on stdout.
- **`config.example.yaml`** — the YAML config template.
  Copy to `config.yaml`, fill in, point the daemon at it.
- **`pi_agent.provider.env.example`** — the provider env
  file template. Copy to `provider.env`, set
  `PI_PROVIDER_*` variables, `chmod 600`.
- **`systemd/pi-agent.service`** — reference systemd unit
  (developer laptops; this host uses supervisord).
- **`supervisor/pi-agent.conf`** — production supervisord
  unit (matches the bridge's unit at
  `hermes-runtime-bridge/supervisor/hermes-runtime-bridge.conf`).
- **`tests/test_pi_worker.py`** — 13 unit tests for the
  daemon. Mocked HTTP and subprocess; no real bridge.
- **`tests/test_smoke.py`** — 2 closed-loop tests. The
  second spins up a real uvicorn bridge on a random port,
  runs the daemon as a subprocess, and asserts the full
  register → claim → execute → complete cycle works.

## Quick start

```bash
# 1. Install dependencies.
cd /home/ubuntu/hermes-runtime-bridge
.venv/bin/pip install -r pi-agent/requirements.txt
cd pi-agent/runtime && npm install && cd ../..

# 2. Configure.
cp pi-agent/config.example.yaml pi-agent/config.yaml
cp pi-agent/pi_agent.provider.env.example pi-agent/provider.env
chmod 600 pi-agent/provider.env
# Edit provider.env: set PI_PROVIDER_API_KEY to a real key.

# 3. Run the daemon.
PYTHONPATH=. .venv/bin/python -u pi-agent/pi_worker.py \
    --config pi-agent/config.yaml

# 4. In another shell, create a Job.
curl -X POST http://127.0.0.1:8787/v1/jobs \
  -H "Authorization: Bearer $BRIDGE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "echo test",
    "input": "echo hello from pi",
    "target_runtime": "pi_agent",
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

# 2. Install the Pi Worker unit.
sudo cp /home/ubuntu/hermes-runtime-bridge/pi-agent/supervisor/pi-agent.conf \
        /etc/supervisor/conf.d/pi-agent.conf
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl status pi-agent

# 3. Tail the logs.
sudo tail -F /var/log/pi-agent.err.log
```

## Install on a developer laptop (systemd)

```bash
# 1. Install the unit.
sudo cp /home/ubuntu/hermes-runtime-bridge/pi-agent/systemd/pi-agent.service \
        /etc/systemd/system/pi-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now pi-agent

# 2. Watch the logs.
journalctl -u pi-agent -f
```

## Run the tests

```bash
# Node runtime tests (3 tests, ~300ms).
cd /home/ubuntu/hermes-runtime-bridge/pi-agent/runtime
npm test

# Python daemon unit tests (13 tests, ~30ms).
cd /home/ubuntu/hermes-runtime-bridge
PYTHONPATH=. .venv/bin/python -m unittest pi-agent.tests.test_pi_worker

# Closed-loop smoke test (2 tests, ~3s; one spins up a real bridge).
PYTHONPATH=. .venv/bin/python -m unittest pi-agent.tests.test_smoke

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
| §9.1 Register | `pi_worker.py:_register()` |
| §9.2 Heartbeat (Worker) | `pi_worker.py:_heartbeat_loop()` |
| §9.3 Claim | `pi_worker.py:_claim_and_execute_once()` |
| §9.4 Execute | `pi_worker.py:_execute_job()` + `runtime/lib/{loop,tools,provider}.js` |
| §9.5 Complete / Fail | `pi_worker.py:_complete_job()` / `_fail_job()` |
| §9.6 Lifecycle hygiene | `pi_worker.py:_send_offline_then_exit()` + the runtime's allowlist / workdir containment |

## Status model (AEE-4 Worker Runtime Contract §5)

The daemon sends these statuses via `POST /v1/workers/{id}/heartbeat`:

- `idle` — between jobs (default after register)
- `busy` — currently executing a Job (with `job_id` set)
- `offline` — final heartbeat before process exit
- `draining` — not currently sent; reserved for graceful drain
- `error` — not currently sent; the daemon logs and exits on
  unrecoverable errors (the bridge reaper handles the in-flight Job)

The daemon is **conformant** on the 5-value vocabulary. The
`unknown` value is the schema default for pre-AEE-4 workers
and is never sent by the daemon.

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
§10. The Pi Worker implements everything in v1; AEE-5+ work
will address the open questions.
