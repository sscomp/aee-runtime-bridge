# AEE architecture and deployment boundaries

AEE Runtime Bridge is host software maintained in `sscomp/aee-runtime-bridge`.
OpenAI Platform hosts the tunnel control plane, not AEE or its job store.
The OpenAI-provided `tunnel-client` moves MCP traffic; it does not orchestrate AEE jobs.

```text
OpenAI / ChatGPT product boundary
  ChatGPT -> Plugin / Custom MCP Server configuration
                     |
                     v
             OpenAI Secure MCP Tunnel
                     ^
                     | outbound HTTPS initiated from private host
---------------------|-----------------------------------------------
<AEE_HOST> private execution boundary
             OpenAI tunnel-client (separate binary)
                     | HTTP /mcp + restricted gateway bearer credential
                     v
          127.0.0.1:8791 restricted AEE MCP gateway
          status / agents / dispatch / job_status / job_result
                     |
Local operator -> 127.0.0.1:8790 full AEE MCP gateway (also aee_exec)
                     |
          AEE job store + gateway worker threads
                     |
          Controlled Codex runtime / executor adapter

Separate operator listener: 127.0.0.1:8792 tunnel health (not MCP)
```

## Three states that must stay distinct

| State | Evidence | Supported conclusion |
|---|---|---|
| Captured/live A3 bootstrap | bootstrap baseline in the approved MCP source, private historical bootstrap milestone, read-only Stage 2A service topology | Existing user services and five-tool tunnel route; Codex/read_only dispatch contract; bootstrap concurrency/reboot limitations |
| Current isolated P2C implementation | `mcp_gateway.py`, `aee/mcp_runtime/`, candidate runbook in the approved MCP source | Hardened implementation exists and can be tested offline; it is not installed in live A3 |
| Accepted P2C observation evidence | private historical acceptance report and its sealed independent reopen | P2C accepted, 11 successful captured invocations, repeatability 10/10; Stage 2 remains STOPPED |

Historical NO-GO reports describe earlier gates. The latest acceptance supersedes their
readiness verdict for its qualified observation scope; it does not authorize deployment.
The repository contains uncommitted accepted implementation deltas. A clean HEAD checkout
alone does not contain the whole accepted candidate until a later reviewed commit.

## Component responsibilities

| Component | Responsibility / trust boundary |
|---|---|
| ChatGPT product | Conversation, workspace permissions and tool confirmation settings |
| Plugin / Custom MCP Server | Select tunnel and authentication, discover/use permitted tools |
| OpenAI Secure MCP Tunnel | Private transport control plane and organization/workspace access |
| OpenAI `tunnel-client` | host-side polling and forwarding; separate control-plane/forward credentials |
| AEE gateway | MCP Streamable HTTP `/mcp`, bearer middleware, tool filtering, dispatch validation |
| AEE job/runtime layer | Persist job state; worker threads launch executor; candidate adds leases and terminal integrity |
| Agent adapters | MCP route supports Codex/read_only only; discovery of Hermes/Claude is not dispatch authorization |
| <AEE_HOST> | Private host, user services, local runtime binaries and operator-managed credentials |

The five remotely exposed names are `aee_status`, `aee_agents`, `aee_dispatch`,
`aee_job_status`, `aee_job_result`. The local/full endpoint additionally registers
`aee_exec`; keep it local. Restriction applies to both list and call in the candidate.
The generic profile/HTTP API in `app.py` and `aee/adapters/` is a separate compatibility
surface, not the five-tool MCP route. The legacy HTTP bridge's 8787 and Hermes 8642
are not tunnel targets in this deployment.

## Runtime semantics

`aee_dispatch` requires `agent`, `working_directory`, and nonempty `task` (or `prompt`
fallback); `mode` defaults to `read_only`. Other agents/modes are rejected. It returns
`job_id` promptly; poll status then terminal result. Candidate selectors must match
`codex-readonly-high` (`gpt-6.1-sol`, reasoning `high`). These are configured values,
not proof of live account access or observed model identity.

Candidate workers read a reviewed immutable workspace snapshot, use a pinned native
ELF executor, bounded stdin/JSONL/output, and fail closed on missing identity or
incomplete results. The broker separates inference credentials from job processes;
it is distinct from tunnel authentication. See [security](security-model.md).
The bootstrap's sequential busy check and orphan-job limitations remain documented
in the baseline; candidate leases/reconciliation do not silently upgrade it.

See [tunnel setup](openai-secure-mcp-tunnel.md), [Plugin setup](chatgpt-plugin-deployment.md)
and [operations](operations.md). Exact live identifiers and credential values are
intentionally absent. The claim/evidence matrix lives in the Stage 2A review package.
