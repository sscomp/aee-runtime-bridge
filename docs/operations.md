# AEE host setup and operations

Use this guide for a new private Linux host or to understand an existing installation.
Commands are operator procedures, not actions performed by this documentation patch.
Keep production installations and job stores separate from review work.

## Prerequisites and source

Use Linux, Git, Python 3.13 and `uv`. A tunnel also needs OpenAI `tunnel-client`,
outbound HTTPS and permitted Platform/ChatGPT workspace access. Controlled dispatch
needs a separately reviewed Codex runtime; P2C additionally needs its pinned native
executor, workspace manifest, broker, sandbox/resource setup and approved deployment
manifest. Use the candidate runbook shipped with that approved MCP source.

Replace quoted placeholders locally. In a new directory, obtain an operator-approved
MCP revision or reviewed source bundle:

```bash
git clone https://github.com/sscomp/aee-runtime-bridge.git '<REPO_PATH>'
cd '<REPO_PATH>'
git checkout --detach '<APPROVED_MCP_REF>'
test -f mcp_gateway.py && test -f requirements-mcp.lock
uv venv --python 3.13 .venv
uv pip sync --python .venv/bin/python --require-hashes requirements-mcp.lock
```

**NOT VERIFIED:** the approved MCP revision's public fetchability. The public `main`
inspected for this review lacks MCP files; the MCP code was checked against a local deployment
branch; this documentation patch targets public `main`. If checkout/file checks fail, obtain approved source instead of running
`install.sh` or `app.py`. Dependency sync was verified in a disposable environment.

## Gateway configuration and launch

Adapt the [restricted environment template](../config/examples/gateway-restricted.env.example)
in a private file outside the repository. It uses systemd EnvironmentFile syntax;
do not `source` it as shell code. Resolve every path and credential locally:

| Setting | Required value / meaning |
|---|---|
| `AEE_MCP_HOST` | `127.0.0.1` |
| `AEE_MCP_PORT` | `8791` restricted; optional local/full endpoint uses `8790` |
| `AEE_MCP_SURFACE` | `restricted` on the candidate |
| `AEE_MCP_REQUIRE_AUTH` | `true` |
| `MCP_BRIDGE_API_KEY` | Private gateway key, separate from OpenAI keys |
| `AEE_MCP_EXPOSED_TOOLS` | `aee_status,aee_agents,aee_dispatch,aee_job_status,aee_job_result` |
| `AEE_MCP_ALLOWED_ROOTS` / `A3_DISPATCH_ALLOWED_ROOTS` | Explicit existing reviewed directories; avoid home-directory defaults |
| `A3_JOB_STORE_DIR` | Private persistent store, separate from fixtures and other hosts |
| `A3_CODEX_BIN` / `A3_WORKSPACE_MANIFEST` | Reviewed native executor / workspace manifest for candidate dispatch |

Private env files require 0600; state directories require 0700. For P2C use the
reviewed P2C configuration shipped with the approved MCP source, including `AEE_RUNTIME_MANIFEST`
and broker configuration. Documentation PASS does not grant `APPROVED_STAGE2` runtime
approval. Never omit a deployment manifest or edit gate fields to bypass rejection.
Bootstrap and P2C templates are different generations.

The entrypoint is `.venv/bin/python mcp_gateway.py` from the reviewed source directory.
For persistent startup, adapt the existing units in
[configuration examples](../config/examples/README.md), or P2C units for that generation,
during an approved installation window. Resolve paths, supply the private EnvironmentFile,
and start the gateway before the tunnel. Per-host runtime approval/startup is
**NOT VERIFIED**; no turnkey P2C production install is claimed. A closed deployment
gate is an expected stop, not a tunnel error.

## Local smoke checks

On the intended host, before connecting the tunnel:

```bash
curl --fail --silent --output /dev/null http://127.0.0.1:8791/health
```

Do not publish diagnostic bodies: `/health` and `/status` include host details and
bypass bearer checks. Use a trusted local MCP client with Streamable HTTP,
`http://127.0.0.1:8791/mcp`, and the private gateway bearer header. Keep keys out of
argv, screenshots and logs. Perform `initialize`, `notifications/initialized`, then
`tools/list`; require exactly the five names above. Missing/wrong auth must return
401; `aee_exec` must be unavailable even via direct `tools/call`. Call `aee_status`
and `aee_agents`, inspecting results privately. The offline suite covers these
protocol checks; live calls are **NOT VERIFIED**. Continue to
[tunnel setup](openai-secure-mcp-tunnel.md).

## Troubleshooting

| Symptom | Check / response |
|---|---|
| MCP files absent | Obtain the approved MCP revision, not the HTTP installer |
| Connection refused | Gateway process, loopback bind and correct port; ordering alone is not readiness |
| Forwarding 401 | Gateway key must match the private forward/discovery header; retain auth |
| Tunnel absent in ChatGPT | Workspace association and Tunnels Read + Use |
| Tunnel disconnected | Client readiness, outbound HTTPS, proxy/CA trust, local MCP reachability |
| Extra tools or `aee_exec` | Stop Plugin use, correct restricted target/filter, refresh discovery |
| `INVALID_AGENT` / `INVALID_MODE` | Only `codex` / `read_only` |
| `INVALID_WORKING_DIRECTORY` | Existing canonical directory within reviewed roots |
| `AGENT_UNAVAILABLE` / `ISOLATION_UNAVAILABLE` | Executor identity, workspace manifest and sandbox prerequisites |
| `DEPLOYMENT_GATE_CLOSED` / `RUNTIME_POLICY_INVALID` / `BROKER_UNAVAILABLE` | Complete separate runtime review; never bypass the gate |
| `JOB_BUSY` / `JOB_NOT_COMPLETE` | Poll the existing job; avoid blind retries |
| Terminal integrity error | Preserve record/error; completed need not mean successful |

Health/discovery does not prove inference. Outages require restoring transport;
no durable replay or automatic job cancellation guarantee is established here.

## Offline verification

Run only the focused protocol suite in a disposable environment, outside the live tree:

```bash
uv venv --python 3.13 /tmp/aee-mcp-review-venv
uv pip sync --python /tmp/aee-mcp-review-venv/bin/python \
  --require-hashes requirements-mcp.lock
PYTHONDONTWRITEBYTECODE=1 /tmp/aee-mcp-review-venv/bin/python \
  -m unittest discover -s tests/mcp -p test_p2b_protocol.py -v
```

The suite needs GCC and uses temporary stores and in-memory ASGI transport. It opens
no live listener and performs no real inference. Historical live acceptance drivers
and the full runtime suite are not generic documentation checks.

## Persistence, disable and multiple hosts

Verify enablement, user-manager startup and credentials before relying on reboot
persistence; unit ordering and restart settings do not prove recovery. Do not run
doctor beside a client using the same health port. During an approved rollback window,
disable new Plugin use and stop only the identified tunnel transport. Preserve
credentials, source and stores; admitted jobs are not guaranteed to cancel. P2C
rollback/migration follows its separate runbook.

For each host choose a private store, gateway keys, tunnel identity and profile. Name
Plugins distinctly, such as `AEE <AEE_HOST>`, and verify host identity privately.
Loopback ports can repeat across hosts; same-host listeners need distinct ports.
Centralized updates, fleet patch orchestration and advanced policy are roadmap,
**not implemented**.
