# ChatGPT Plugin deployment (custom MCP server)

Stage 3B walkthrough. **Status label: NOT YET TESTED — an independent
host/account Plugin E2E has not been run yet; publication CI is offline.** The
official steps summarized here were checked against the [custom MCP server
guide](https://developers.openai.com/api/docs/guides/custom-mcp-server) and the
[Secure MCP tunnel
guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).

Goal: a **private** custom Plugin in the operator's ChatGPT workspace that talks
MCP over the Secure MCP Tunnel to the restricted five-tool AEE v2 gateway. This
is not public Plugin distribution. Complete the tunnel setup first — see
[end-to-end tunnel guide](openai-secure-mcp-tunnel.md).

## 1. Prerequisites

1. Restricted gateway healthy with auth enabled (`127.0.0.1:8791/mcp`, exactly
   five exposed tools) — [agent operations](agent-operations.md).
2. Dispatch proven end-to-end locally (real completed job) — [host setup
   §2c](deployment.md). Without it, the Plugin would expose a surface whose every
   job fails.
3. A connected tunnel profile with matching forwarded bearer (tunnel guide §2–3).
4. ChatGPT workspace permission to add custom MCP servers; workspace policies may
   restrict this flow entirely — that is a stop condition, not an error to work
   around.

## 2. Application-authentication mode — read before creating

The custom MCP server flow offers OAuth, no-auth and mixed application
authentication. **The AEE gateway implements a static bearer auth check; it is
not an OAuth authorization server.** So:

- If the workspace requires the OAuth mode for this Plugin type, the gateway as
  deployed cannot satisfy it — stop, or plan an explicitly reviewed gateway-side
  OAuth change. Do **not** weaken or disable gateway authentication to make a
  mode fit.
- The correct fit is the mode where the tunnel-injected `Authorization` header
  (tunnel guide §3) carries the gateway bearer and the Plugin trusts the
  tunnel connection. Validate the exact no-auth/mixed labels and risk wording in
  the ChatGPT UI with your actual account — the labels are not reproduced here.
- Stop on an incompatible mode instead of disabling authentication.

## 3. Create the private Plugin (operator steps, in ChatGPT)

1. Open the Plugins / connectors area for the target workspace.
2. Choose **Add custom MCP server**.
3. For connectivity choose **Tunnel**, then select or paste the authorized
   tunnel ID from the tunnel guide.
4. Configure the available application authentication per §2.
5. Read the risk disclosure shown before creation, then create the private
   Plugin.
6. Expect the workspace to prompt for a first approval/enable of server access
   (org admin approval may apply).

If the tunnel is not connected at this moment, creation may fail or the Plugin
sits dormant until connectivity returns — restart the tunnel client and retry;
do not modify gateway auth.

## 4. First-use validation (same checks as local acceptance, via ChatGPT)

1. Discovery/status invocation succeeds; the tool list is exactly the five
   restricted tools — nothing beyond `aee_status`, `aee_agents`, `aee_dispatch`,
   `aee_job_status`, `aee_job_result`.
2. The gateway's `readOnlyHint` for read-only tools is respected (visible in the
   ChatGPT tool description).
3. One operator-approved **read-only** dispatch from ChatGPT completes; verify
   the job through `aee_job_status`/`aee_job_result` and that the returned
   content is bounded and redacted as in [agent operations](agent-operations.md).
4. Negative checks as in the tunnel guide §4 (bad forwarded credential rejected;
   `aee_exec` unreachable; tunnel revocation disconnects the Plugin).

Any failed boundary → remove/demote the Plugin, stop the tunnel, re-run
local-only acceptance, and investigate before retrying.

## 5. Data governance

Through this Plugin, task text, admitted workspace source and bounded results
reach ChatGPT/OpenAI while jobs run. Private network transport through the
tunnel is not a promise of zero product use or logging on the ChatGPT side. Read
[security and privacy](security-model.md) and the operator's data policy before
enabling it against source you would not otherwise share with OpenAI.

## 6. Rollback

Remove the Plugin in ChatGPT, stop `tunnel-client`, park/revoke the tunnel in
Platform; the local gateway and local clients are unaffected. Rotate the
gateway's `MCP_BRIDGE_API_KEY` if the forwarded credential is suspected of
leakage, and update the gateway env + tunnel forwarding reference together.

## 7. Status

Planned Stage 3, **not yet passed**: no Plugin creation, tunnel connection or
ChatGPT dispatch has been executed by publication work.