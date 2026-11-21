# AEE v2 — private-host MCP execution bridge

**AEE lets ChatGPT dispatch AI coding agents to real Linux hosts through the
OpenAI Secure MCP Tunnel: the MCP gateway, the agent CLIs, the sandbox and the
job store all run on each host; inference runs at the provider; no inbound
ports are opened on any host.** Tasks, admitted source content, and bounded,
redacted results cross the tunnel boundary alongside the same bearer-authenticated
job-control metadata (status, discovery, cancel/result payloads) — all under
the same restricted surface. See [security and privacy](docs/security-model.md).

The current deployment target is **Linux x86_64, systemd with cgroup v2,
CPython 3.13.x**, rootless user/mount/PID/network namespaces, GCC and
Bubblewrap. The production sandbox profile pins Codex `0.159.2` plus its
vendored Bubblewrap artifact in
[`config/p2c/sandbox-profile.json`](config/p2c/sandbox-profile.json); other
binary versions/hosts require a reviewed policy change. Host adapter audits report the
resolved native Codex binary each host actually runs (see the
[runtime capability matrix](docs/runtime-capability-matrix.md)); registry
support alone does not prove an end-to-end job in a given mode. External
ChatGPT Plugin `read_only` E2E on A3 **passed** (codex job
`A3JOB-20261019-928c4174…`, claude job `A3JOB-20261019-6929c583…`, both exit 0,
Reported receipts through the Secure MCP transport). Both Plugin jobs started
2026-10-19 (`18:02:21Z` codex, `17:35:30Z` claude) **after** the os-native
reinstall recorded at `2026-10-19T17:19:46Z` (the "2026-10-20 reinstall"
label is the operator evidence-bundle date, so these receipts already
postdate it); Secure-MCP provenance and task descriptions support that
Reported attribution but do not independently authenticate a specific
ChatGPT UI session. A fresh ChatGPT-originated Plugin revalidation since
those receipts (Stage 3B) and the cold-boot gate remain open.

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
per-job broker socket (approved codex `read_only` contract) or provider env (generic adapter path)
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
| **Broker/restricted production deployment** (root) | a persistent, reviewed broker/manifest install | [host setup §3–4](docs/deployment.md) | Yes, via systemd credentials | requires `APPROVED_STAGE2` review |
| **os-native production install** (systemd user unit, no root) | a persistent gateway install as the host's own OS identity | [deployment profiles §Installation](docs/deployment-profiles.md) | Platform credentials via the OS identity's own environment | installer receipt gate (preflight/apply/rollback) |
| **OpenAI Secure MCP Tunnel + ChatGPT Plugin** (optional, after either dispatch route) | exposing the gateway to ChatGPT with **no inbound ports** | [tunnel guide](docs/openai-secure-mcp-tunnel.md) + [Plugin walkthrough](docs/chatgpt-plugin-deployment.md) | Platform tunnel id + runtime key | `read_only` E2E Reported passed (2026-10-19); fresh revalidation Not Tested (Stage 3B) |

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
  denylist). Manifests and the broker control socket are not required;
  adapter jobs (including codex without an approved manifest) run on the
  generic adapter path against the actual working directory, not a snapshot.
- **`restricted` (explicit opt-in)** — dedicated system accounts
  (`aee-gateway`, `aee-broker`), full systemd sandbox hardening, mandatory
  `A3_DISPATCH_ALLOWED_ROOTS` workspace confinement, per-job broker socket,
  deployment manifests and an operator approval gate. The per-job broker
  snapshot path is reserved to the approved codex contract (`read_only` only —
  codex `write` is refused there); everything else dispatches through the
  generic adapter path. Autonomous dispatch — and `write` dispatch under the
  trusted-dispatch policy — validates the working directory directly as the
  service OS identity and does not apply the allowed-roots list.

> Scoping note for two linked pages: [deployment profiles](docs/deployment-profiles.md)
> still summarizes an absent, unreadable and invalid trusted policy as one
> fail-closed state, and [host setup](docs/deployment.md) §1 still directs a
> pre-merge `feat/stage2c-canonical-v2` checkout. Treat this page as current:
> an **absent** policy falls back to the legacy authorization gates (see
> Quick start below), while a **configured-but-unreadable or invalid** policy
> blocks every mode, including `read_only`.

Do not mix profiles on one host. On either profile, `write`/`autonomous`
dispatch requires explicit authorization: an explicitly provisioned
[trusted-dispatch policy](docs/mcp-runtime-autonomous.md) — which supersedes
the legacy per-job write gates — or the legacy per-job operator approval
gates when the policy is absent (falling back as described under Quick
start). Read-only dispatch requires no policy.

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

The packager emits `STAGE1_NOT_DEPLOYABLE`; tests never authorize deployment.

Then audit what the *service identity* can actually resolve — a CLI installed
on the host is not automatically available to every service identity:

```bash
python3 scripts/audit_adapters.py                       # resolution facts, JSON, exit 0
python3 scripts/audit_adapters.py --strict --expect claude,codex
```

Configure the private gateway env file for the profile you chose —
[`config/profiles/os-native.env.example`](config/profiles/os-native.env.example)
(os-native) or
[`config/profiles/restricted.env.example`](config/profiles/restricted.env.example)
(restricted, which also points at the broker/manifest template
`config/p2c/gateway-restricted.env.example`) — retaining
`127.0.0.1:8791/mcp (restricted)`, authentication and exactly the six-tool
surface: `aee_status`, `aee_agents`, `aee_dispatch`, `aee_job_cancel`,
`aee_job_status`, `aee_job_result`. With the A2 private trusted-dispatch
policy provisioned, approved Secure MCP principals may dispatch
`read_only`/`write`/`autonomous` without per-job tokens
([A2 deploy and verification](docs/a2-secure-mcp-deploy-verify.md)). Without
that policy, dispatch falls back to the legacy authorization gates (still
implemented): read-only jobs across supported adapters require no policy, and
write/autonomous need the legacy per-job operator approval
(`AEE_WRITE_APPROVAL_TOKEN`/`AEE_WRITE_ALLOWED_ROOTS` and the autonomous
authority token). A configured-but-malformed policy blocks all new dispatch —
see [trusted dispatch](docs/mcp-runtime-autonomous.md).
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
  policy (all modes when provisioned; legacy per-job gates cover
  write/autonomous when the policy is absent) → working-directory identity →
  adapter registry → native sandbox. All callers with the shared gateway bearer share its
  authority; per-user/multi-tenant authorization is intentionally absent.
- Unsupported modes fail closed: an adapter that cannot honestly enforce a
  mode is refused (`ADAPTER_UNSUPPORTED`), never simulated.
- `aee_exec` is a trusted local-operator capability and must stay off the
  remote surface. `/health` and `/status` expose diagnostics and must remain
  loopback-only.

History note: the [security model](docs/security-model.md) paragraphs that
state remote execution is "Codex/read_only only" and that write is "rejected"
describe the earlier Stage-1 candidate contract. Trusted-dispatch policy
(above) and the two deployment profiles supersede them: `write`/`autonomous`
are admitted under an explicitly provisioned policy, and the approved broker
contract alone remains `read_only`-only.

Full model: [security model](docs/security-model.md) ·
[architecture](docs/architecture.md) ·
[trusted dispatch](docs/mcp-runtime-autonomous.md).

## What is proven, and where the record lives

Acceptance evidence is labeled per claim in the
[runtime capability matrix](docs/runtime-capability-matrix.md)
(`Confirmed` = independently inspected contract/code, `Reported` = operator
receipt/job record not re-executed, `Not Tested` = open gate). Known open
gates at time of writing: a fresh ChatGPT-originated External Plugin E2E since
the cited receipts (the recorded A3 plugin `read_only` receipts all date from
2026-10-19, already after the reinstall receipt at `2026-10-19T17:19:46Z`),
plugin-path `write`/`autonomous` E2E, the cold-boot/linger
gate, and `write`/`autonomous` E2E across every adapter (codex `autonomous`,
Claude `write`, and all Pi modes lack receipts). A2/A3 multi-host parity is not
universally verified — no multi-host simultaneous E2E exercise exists.
Registry support does **not** by itself prove an end-to-end job in a given
mode.

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