# AEE Runtime Bridge

AEE (Agent Execution Engine) runs controlled agent jobs on a private Linux host.
ChatGPT connects to its restricted MCP gateway through OpenAI Secure MCP Tunnel.
AEE runs on your host; OpenAI hosts the tunnel control plane. The separate
`tunnel-client` forwards requests over outbound HTTPS.

```text
ChatGPT custom MCP Plugin -> OpenAI Platform tunnel
                                      ^ outbound HTTPS
<AEE_HOST>:                     tunnel-client
                                      |
                              127.0.0.1:8791/mcp (restricted)
                                      |
                              AEE jobs / controlled runtime
Local operator -------------> 127.0.0.1:8790/mcp (full/local)
```

## One-host quick start

1. Obtain an operator-approved source revision containing `mcp_gateway.py` and
   `requirements-mcp.lock`. Follow [host setup](docs/operations.md) to install
   locked dependencies and configure a private gateway environment.
2. Start the approved restricted gateway on loopback 8791. Check bearer auth and
   the exact five-tool list. The optional full/local 8790 endpoint must never
   be the tunnel target.
3. Download OpenAI `tunnel-client`, associate a Platform tunnel with the intended
   ChatGPT workspace, and configure separate control-plane and gateway-forward
   credentials. Follow [tunnel setup](docs/openai-secure-mcp-tunnel.md).
4. In ChatGPT Plugins, add a custom MCP server using **Tunnel**, install the Plugin,
   and check `aee_status` and `aee_agents`. Follow the
   [Plugin checklist](docs/chatgpt-plugin-deployment.md).

The public `main` revision inspected for this review does **not** contain the MCP
gateway. Do not use the legacy HTTP installer for this route. Fetchability of an
approved MCP revision on a new host is **NOT VERIFIED**; obtain that revision or a
reviewed source bundle from the operator. P2C is a deployment candidate with its
own closed production gate; this guide does not authorize or qualify its rollout.

## Security and verification

The remote surface is exactly `aee_status`, `aee_agents`, `aee_dispatch`,
`aee_job_status`, and `aee_job_result`. Dispatch accepts only `codex` / `read_only`
with a reviewed existing working directory and a nonempty task. Other discovered
agents are not authorized for remote execution. `aee_exec` remains local/full only.

Keep gateways on loopback, require bearer auth, and separate tunnel, gateway and
inference credentials. Health/discovery does not prove successful inference.
New-host startup, ChatGPT account access, end-to-end calls and reboot persistence
are **NOT VERIFIED** by this documentation patch.

| Reference | Purpose |
|---|---|
| [Operations](docs/operations.md) | Install, gateway configuration, smoke checks and troubleshooting |
| [Tunnel guide](docs/openai-secure-mcp-tunnel.md) | Client, profile and workspace association |
| [Plugin guide](docs/chatgpt-plugin-deployment.md) | Connection, authentication and tools |
| [Architecture](docs/architecture.md) | Bootstrap and candidate responsibilities |
| [Security model](docs/security-model.md) | Authorization and isolation limits |
| [Configuration examples](config/examples/README.md) | Non-secret templates and scope |
| [Changelog](CHANGELOG.md) | Documentation changes |

The legacy HTTP/profile bridge (`app.py`, default 8787) and its
[GPT Action setup](gpt/GPT_SETUP_GUIDE.md) remain separate compatibility paths. See the
[legacy HTTP/profile reference](docs/legacy-http-profiles.md) for profile
selection, installer/Docker behavior and the historical migration context.
MCP uses `requirements-mcp.lock`; legacy HTTP has its own dependencies.

Product metadata remains `2.0.0-rc1`. Historical Stage 2B BLOCKED decisions remain
unchanged; documentation review is independent of runtime qualification. Centralized
updates, fleet patch orchestration and advanced policy are roadmap items, **not
implemented**. Do not publish private environments, logs, job stores or historical
evidence automatically.
