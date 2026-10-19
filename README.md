# AEE v2 — private-host MCP execution bridge

**AEE lets ChatGPT dispatch AI coding agents to real Linux hosts through the
OpenAI Secure MCP Tunnel: the MCP gateway, the agent CLIs, the sandbox and the
job store all run on each host; inference runs at the provider; no inbound
ports are opened on any host.** Tasks, admitted source content and bounded,
redacted results are the only things that cross the tunnel boundary. See
[security and privacy](docs/security-model.md).

The current deployment target is **Linux x86_64, systemd with cgroup v2,
CPython 3.13.x**, rootless user/mount/PID/network namespaces, GCC and
Bubblewrap. The production sandbox profile pins Codex `0.159.2` plus its
vendored Bubblewrap artifact in
[`config/p2c/sandbox-profile.json`](config/p2c/sandbox-profile.json); other
binary versions/hosts require a reviewed policy change. Host adapter audits report the
resolved native Codex binary each host actually runs (see the
[runtime capability matrix](docs/runtime-capability-matrix.md)); registry
support alone does not prove an end-to-end job in a given mode. A
ChatGPT-originated Plugin E2E is **not yet validated** (Stage 3B).

## How a dispatch flows (architecture in one paragraph)

```
ChatGPT connector / MCP client
        │  Secure MCP Tunnel — outbound HTTPS only, no inbound ports
        ▼
host tunnel-client  →  127.0.0.1:8791/mcp (restricted gateway, bearer-auth)
        │  six-tool MCP surface, trusted-dispatch principal check
        ▼
one active job (flock-leased store)  →  host-local agent adapter CLI
        │  Codex / Claude Code / Pi under their own native enforcement
        ▼
per-job broker socket (restricted profile) or provider env (os-native)
        → inference completes → bounded, redacted result back through MCP
```

Each **host is independent**: every Linux host running AEE has its own
gateway on loopback, its own tunnel client, its own host-local agent
binaries and job store, and its own trusted-dispatch policy and principal.
Dispatching on host A tells you nothing about host B; there is no central
dispatcher. SSH is an optional operator transport for remote
administration/debugging — it is **never** an AEE runtime or installer
dependency, and nothing in the runtime path calls it
([deployment profiles](docs/deployment-profiles.md)).

## Pick your route first

| Route | Who it is for | Where documented | Auth/model credential needed? | Status label |
|---|---|---|---|---|
| **Local validation** (no root) | any agent proving the repo installs and MCP works | [host setup §0–2](docs/deployment.md) | No (synthetic smokes) | `LOCAL_VALIDATION_OK` |
| **Broker-qualified local dispatch** (no root) | real read-only Codex jobs via the inference broker | [host setup §2c](docs/deployment.md) | Yes — operator-provisioned OpenAI API key file | `LOCAL_QUALIFIED_DISPATCH` |
| **Production deployment** (root) | a persistent, reviewed host install | [host setup §3–4](docs/deployment.md) | Yes, via systemd credentials | requires `APPROVED_STAGE2` review |
| **OpenAI Secure MCP Tunnel + ChatGPT Plugin** (optional, after either dispatch route) | exposing the gateway to ChatGPT with **no inbound ports** | [tunnel guide](docs/openai-secure-mcp-tunnel.md) + [Plugin walkthrough](docs/chatgpt-plugin-deployment.md) | Platform tunnel id + runtime key | `NOT YET TESTED — Stage 3B` |

All routes start with the same commands below. `install.sh` (the legacy
HTTP/profile installer) is **not** part of the MCP route; the
[legacy reference](docs/legacy-http-profiles.md) remains only for the four
older HTTP/CLI profiles.

## Choose a deployment profile

Gateway installs choose exactly one of two canonical deployment profiles
(same six-tool surface, same fail-closed defaults, verified contract in
[deployment profiles](docs/deployment-profiles.md)):

- **`os-native` (default)** — the gateway runs under an existing OS identity
  as a systemd *user* unit. The boundary is the OS itself: DAC/ACL on the
  dispatch working directory plus each adapter's own native enforcement
  (Codex native sandbox, Claude tool allowlist/acceptEdits, Pi tool
  denylist). Manifests and the broker control socket are not required.
- **`restricted` (explicit opt-in)** — dedicated system accounts
  (`aee-gateway`, `aee-broker`), full systemd sandbox hardening, mandatory
  `A3_DISPATCH_ALLOWED_ROOTS` workspace confinement, per-job broker socket,
  deployment manifests and an operator approval gate.

Do not mix profiles on one host. Write and autonomous dispatch require an
explicitly provisioned [trusted-dispatch policy](docs/mcp-runtime-autonomous.md)
on either profile; it supersedes the legacy per-job write gates.

## Quick start

```bash
git clone https://github.com/sscomp/aee-runtime-bridge.git
cd aee-runtime-bridge
# The operator selects and records the reviewed commit being deployed (always
# `git rev-parse HEAD` the exact SHA and keep the worktree clean).
uv venv --python 3.13 .venv
uv pip sync --python .venv/bin/python --require-hashes requirements-mcp.lock
PYTHONPATH=.:tests/mcp .venv/bin/python -m unittest test_p2b_protocol -v
```

If `uv` is missing, install it user-level (no root): `curl -LsSf https://astral.sh/uv/install.sh | sh`
— `uv venv --python 3.13` downloads and manages its own CPython 3.13 when the host
Python is another version. Full details in [host setup](docs/deployment.md).

Then audit what the *service identity* can actually resolve — a CLI installed
on the host is not automatically available to every service identity:

```bash
python3 scripts/audit_adapters.py                       # resolution facts, JSON, exit 0
python3 scripts/audit_adapters.py --strict --expect claude,codex
```

Configure `config/p2c/gateway-restricted.env.example` privately, retaining
`127.0.0.1:8791/mcp (restricted)`, authentication and exactly the six-tool
surface: `aee_status`, `aee_agents`, `aee_dispatch`, `aee_job_cancel`,
`aee_job_status`, `aee_job_result`. With the A2 private trusted-dispatch
policy provisioned, approved Secure MCP principals may dispatch
`read_only`/`write`/`autonomous` without per-job tokens
([A2 deploy and verification](docs/a2-secure-mcp-deploy-verify.md)); without
that policy only `codex` / `read_only` dispatch is supported.
`aee_exec` remains local/full only on port 8790.
[Validation](docs/agent-operations.md) covers health, MCP handshake, agent
discovery, safe dispatch and completed-result verification.

Use [tunnel setup](docs/chatgpt-mcp.md) and the Plugin walkthrough
([chatgpt-plugin-deployment](docs/chatgpt-plugin-deployment.md)) after local
validation. Operator-managed credentials and workspace authorization are
required. See [troubleshooting and rollback](docs/troubleshooting.md).

## Trust boundaries (summary)

- The gateway and tunnel are transport only; a Plugin connection does not
  grant unrestricted host access.
- Admission checks chain explicitly: bearer → restricted surface → trusted
  policy (write/autonomous) → working-directory identity → adapter registry
  → native sandbox. All callers with the shared gateway bearer share its
  authority; per-user/multi-tenant authorization is intentionally absent.
- Unsupported modes fail closed: an adapter that cannot honestly enforce a
  mode is refused (`ADAPTER_UNSUPPORTED`), never simulated.
- `aee_exec` is a trusted local-operator capability and must stay off the
  remote surface. `/health` and `/status` expose diagnostics and must remain
  loopback-only.

Full model: [security model](docs/security-model.md) ·
[architecture](docs/architecture.md) ·
[trusted dispatch](docs/mcp-runtime-autonomous.md).

## What is proven, and where the record lives

Acceptance evidence is labeled per claim in the
[runtime capability matrix](docs/runtime-capability-matrix.md)
(`Confirmed` = independently inspected contract/code, `Reported` = operator
receipt/job record not re-executed, `Not Tested` = open gate). Known open
gates at time of writing: ChatGPT-originated External Plugin E2E and the
cold-boot/linger gate. Registry support does **not** by itself prove an
end-to-end job in a given mode.

## Repository map and verification

- [Architecture](docs/architecture.md): trust boundaries, profiles, adapter
  registry, job lifecycle and failure semantics.
- [Use cases](docs/use-cases.md): scenario matrix with preconditions,
  mode and acceptance for the supported dispatch scenarios.
- [Agent operations](docs/agent-operations.md): independent-agent checklist and CI.
- [Retirement ownership](docs/legacy-retirement.md): 24 external archive
  checks retired; current profiles, APIs, adapters and real-host upgrade
  path preserved.
- [Config contracts](config/p2c/README.md): immutable release layout,
  credentials, resource limits and approval gate. Examples are uninstalled
  templates.
- [Runtime tests](tests/mcp/README.md): controlled C/socket/SSE fixtures and
  separate optional native qualification. Fixture success is not real model
  inference.

Required CI covers isolated HTTP API/job tests, all four legacy profile
suites, shell bootstrap regression, MCP auth and six-tool boundary, job
lifecycle/results, broker policies, packaging, documentation/configuration
and public-source secret scanning. It never installs services or publishes a
release. Host qualification and the planned Stage 3 E2E are separate,
explicit acceptance steps.