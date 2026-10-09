# Secure MCP Tunnel and ChatGPT Plugin

First finish [host installation](deployment.md) and [local MCP acceptance](agent-operations.md).
Tunnel setup and Plugin linkage require operator-managed Platform and ChatGPT
workspace permissions. This is a **private custom Plugin** workflow, not public
Plugin distribution. Instructions were checked against the official
[Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
and [custom MCP server guide](https://developers.openai.com/api/docs/guides/custom-mcp-server).

## Tunnel setup

The operator obtains a tunnel ID and runtime control-plane key in Platform,
associates the target ChatGPT workspace, and grants tunnel use/manage permissions
as needed. Install an operator-reviewed `tunnel-client` release. Check its local
`--version`, `init --help`, `run --help` and `doctor --help` before using examples;
the flags below were checked with 0.0.15. It needs outbound HTTPS to OpenAI and
local access to the restricted MCP server. Never open public gateway ingress.

```bash
: "${AEE_TUNNEL_ID:?Set operator-authorized tunnel ID}"
tunnel-client init --profile aee-restricted --tunnel-id "$AEE_TUNNEL_ID" \
  --mcp-server-url http://127.0.0.1:8791/mcp --health-listen-addr 127.0.0.1:8080
```

`init` writes a local profile. Use a private secret manager/service environment
for `CONTROL_PLANE_API_KEY` and a **different** `AEE_MCP_FORWARD_KEY` equal to the
restricted gateway bearer credential. No literal credential belongs in a profile,
repo, shell argument or transcript. The forwarding header reference is:

```bash
tunnel-client run --profile aee-restricted \
  --control-plane.api-key env:CONTROL_PLANE_API_KEY \
  --mcp.extra-headers 'Authorization: Bearer env:AEE_MCP_FORWARD_KEY' \
  --mcp.discovery-extra-headers 'Authorization: Bearer env:AEE_MCP_FORWARD_KEY'
```

Check the installed client's secret-reference syntax before use. Keep admin/UI
listeners on loopback and avoid an existing 8080 listener. Run doctor using the
same private header references supported by that version; protect doctor output.
A healthy tunnel transport does not prove successful MCP auth or inference.
No tunnel was registered or connected during this publication work.

## Plugin checklist and planned Stage 3 E2E

The operator opens ChatGPT Plugins, chooses **Add custom MCP server**, selects
**Tunnel**, picks/pastes the authorized tunnel ID, configures the available
application authentication, reviews the risk disclosure and creates the private
Plugin. Workspace policies may restrict this flow. The AEE gateway implements
static bearer authentication, not an OAuth authorization server; validate the
selected ChatGPT/tunnel auth mode with the actual account. Stop on an incompatible
mode instead of disabling authentication.

Confirm the exact five tools, invoke status/discovery, then perform one approved
read-only dispatch and poll its result as in [local acceptance](agent-operations.md).
Require ChatGPT → tunnel → restricted MCP → reviewed Codex → validated job result.
Check invalid credentials, unavailable `aee_exec`, revoked leases and bounded
redacted results. Capture redacted failures and rollback if any boundary fails.
This independent-host/account E2E is **planned Stage 3, not already passed**.

Tasks/source/results can reach OpenAI; private-network transport is not a promise
of zero product logging. Review [security/privacy](security-model.md) and the
operator's data policy before using sensitive source.
