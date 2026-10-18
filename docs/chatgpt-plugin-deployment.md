# ChatGPT Plugin deployment (custom MCP server)

Stage 3B walkthrough. **Status label: EXECUTED ON PRODUCTION A2 — a private
Plugin/connector ("Omarchy-A2") over an A2-distinct Secure MCP Tunnel was
created and exercised end-to-end: tool discovery, read-only dispatch, job
polling and result retrieval all ran for real (completed jobs and a
deterministic fail-shot case are documented in
[troubleshooting](troubleshooting.md)).** The official steps summarized here
were checked against the [custom MCP server
guide](https://developers.openai.com/api/docs/guides/custom-mcp-server) and the
[Secure MCP tunnel
guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).

## 1a. Dispatch contract the Plugin exposes (Stage 3B baseline; A2 trusted dispatch)

At the Stage 3B execution recorded here, the restricted surface dispatched only
`agent="codex"` with `mode="read_only"`. That remains the hard fallback today
for any deployment **without** a configured trusted-dispatch policy:

- `agent="claude"` → `INVALID_AGENT`; `mode="read_write"` → `INVALID_MODE`.
  `aee_agents` reporting Claude Code as installed/available is *filesystem
  discovery only*, not an authorized dispatch target — do not treat it as a
  capability.
- Only the profile `codex-readonly-high` (model `gpt-6.1-sol`, reasoning
  `high`, sandbox `read-only`, approval `never`) is registered.
- The working directory must be an approved immutable snapshot under the
  dispatch allowlist — on production A2 that is `/opt/aee/current/source`.
  `/home/...` paths are invisible to the gateway by design
  (`ProtectHome=yes`) and are never a fix target.
- Identity note: job IDs use the legacy `A3JOB-` prefix and the gateway
  self-labels legacy-names in metadata; this does **not** imply execution on
  any node other than the one owning the tunnel. Confirm via `aee_status`
  hostname and the job's resolved `working_directory`.

**A2 change — Secure MCP trusted dispatch.** The `aee_dispatch` schema now
publishes the `mode` enum `read_only | write | autonomous`. A deployment that
provisions the private trusted-dispatch policy
([overview](mcp-runtime-autonomous.md), [setup and refresh
runbook](chatgpt-secure-mcp-setup-runbook.md)) authorizes named Secure MCP
principals — keyed to the SHA-256 digest of the tunnel bearer, never the value
itself — to dispatch `write` and `autonomous` jobs over this same restricted
surface with **no per-job approval token**. Authorization is not capability:
agent adapters still verify their own execution fitness and fail closed before
any launch (`ADAPTER_UNSUPPORTED`). Deployment state reporting keeps both
models visible: `/status` shows
`autonomous.authorization = secure_mcp_trusted_dispatch` when the policy is
configured, `single-job-capability` for the legacy path.

**Task authoring matters.** Native enforcement fails a job
(`REQUIRED_TOOL_FAILED`, exit-free fail-shot) when any command execution the
agent runs exits nonzero — including benign negative queries (`grep -q` with no
match, `diff` on differing files, `git …` against a snapshot with no `.git`).
Write tasks whose commands exit zero: use plain `ls`/`cat`, and guard
existence checks like `ls <file> || printf 'ABSENT\n'`. See
[troubleshooting](troubleshooting.md) for the attribution runbook.

Goal: a **private** custom Plugin in the operator's ChatGPT workspace that talks
MCP over the Secure MCP Tunnel to the restricted six-tool AEE v2 gateway. This
is not public Plugin distribution. Complete the tunnel setup first — see
[end-to-end tunnel guide](openai-secure-mcp-tunnel.md).

## 1. Prerequisites

1. Restricted gateway healthy with auth enabled (`127.0.0.1:8791/mcp`, exactly
   the six-tool restricted surface) — [agent operations](agent-operations.md).
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

1. Discovery/status invocation succeeds; the tool list is exactly the six
   restricted tools — nothing beyond `aee_status`, `aee_agents`, `aee_dispatch`,
   `aee_job_cancel`, `aee_job_status`, `aee_job_result`.
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

Executed on production A2 (connector "Omarchy-A2", `tunnel-client` v0.0.16):
Plugin creation, tunnel connection, discovery, dispatch, polling and result
retrieval were all run for real. Historical reports stay in the operator's
local evidence directory, not in the canonical happy path.