# AEE v2 — private-host MCP execution bridge

AEE runs the MCP gateway, reviewed agent executable, sandbox and job store on
**your Linux host**. ChatGPT and inference models run at OpenAI; Secure MCP Tunnel
forwards requests over outbound HTTPS. Tasks, admitted source content and bounded
results can cross that boundary. See [security and privacy](docs/security-model.md).

The current deployment target is **Linux x86_64, systemd with cgroup v2, CPython
3.13.x**, rootless user/mount/PID/network namespaces, GCC and Bubblewrap. Production
pins Codex 0.159.2 plus its Code Mode companion and the exact Bubblewrap artifact
in `config/p2c/sandbox-profile.json`. Other binaries/hosts require a reviewed policy
change. A fresh independent host and ChatGPT E2E are **not yet validated**.

Start with the single [host setup](docs/deployment.md) sequence: clone a reviewed
commit, install the hash-locked MCP closure, run the credential-free smoke below,
then use the existing offline packager and operator-approved systemd deployment.
The packager emits `STAGE1_NOT_DEPLOYABLE`; tests never authorize deployment.

```bash
git clone https://github.com/sscomp/aee-runtime-bridge.git
cd aee-runtime-bridge
# For this review candidate, checkout feat/stage2c-canonical-v2 before setup.
# For deployment, the operator must select and record an approved commit.
uv venv --python 3.13 .venv
uv pip sync --python .venv/bin/python --require-hashes requirements-mcp.lock
PYTHONPATH=.:tests/mcp .venv/bin/python -m unittest test_p2b_protocol -v
```

Configure `config/p2c/gateway-restricted.env.example` privately, retaining
`127.0.0.1:8791/mcp (restricted)`, authentication and exactly `aee_status`,
`aee_agents`, `aee_dispatch`, `aee_job_status`, `aee_job_result`.
Only `codex` / `read_only` dispatch is supported; `aee_exec` remains local/full only
on port 8790. Follow [host setup](docs/deployment.md) to qualify and start the
broker/gateway; [validation](docs/agent-operations.md) covers health, MCP handshake,
agent discovery, safe dispatch and completed-result verification.

Use [tunnel setup](docs/chatgpt-mcp.md) and its [Plugin checklist](docs/chatgpt-mcp.md)
after local validation. Operator-managed credentials and workspace authorization
are required. See [troubleshooting and rollback](docs/troubleshooting.md).

The four supported HTTP/CLI install profiles remain available through the
[legacy HTTP/profile reference](docs/legacy-http-profiles.md).
**Do not use the legacy HTTP installer for this route**: `install.sh` does not
install the MCP closure or qualify production MCP dispatch.

## Repository map and verification

- [Architecture](docs/architecture.md): trust boundaries and code entrypoints.
- [Agent operations](docs/agent-operations.md): independent-agent checklist and CI.
- [Retirement ownership](docs/legacy-retirement.md): 24 external archive checks
  retired; current profiles, APIs, adapters and real-host upgrade path preserved.
- [Config contracts](config/p2c/README.md): immutable release layout, credentials,
  resource limits and approval gate. Examples are uninstalled templates.
- [Runtime tests](tests/mcp/README.md): controlled C/socket/SSE fixtures and separate
  optional native qualification. Fixture success is not real model inference.

Required CI covers isolated HTTP API/job tests, all four profile suites, shell bootstrap regression, MCP auth
and five-tool boundary, job lifecycle/results, broker policies, packaging,
documentation/configuration and public-source secret scanning. It never installs
services or publishes a release. Host qualification and planned Stage 3 E2E are
separate, explicit acceptance steps.
