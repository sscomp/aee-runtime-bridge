# ChatGPT custom MCP Plugin setup

Finish [gateway checks](operations.md) and [tunnel readiness](openai-secure-mcp-tunnel.md)
before creating the Plugin. Four configurations have distinct responsibilities:

| Component | Configuration |
|---|---|
| AEE restricted gateway | Loopback 8791, bearer requirement and five-tool authorization |
| Host `tunnel-client` | Private forward/discovery headers, target and outbound polling |
| Platform tunnel | Identity, organization/workspace association and access |
| ChatGPT custom MCP Plugin | Tunnel selection, authentication and tool access |

## Connect and install

1. In the intended ChatGPT workspace on the web, open **Plugins**, select **+**, then
   **Add custom MCP server**. Name it, for example, `AEE <AEE_HOST>`.
2. Under **Connection**, choose **Tunnel**, selecting the associated tunnel or entering
   `<TUNNEL_ID>` privately. An absent tunnel needs corrected workspace access, not a
   public gateway URL.
3. With private forward/discovery bearer headers configured, choose **No authentication**
   for this route. AEE still enforces its bearer key.
4. Review the risk warning, select **I understand and want to continue**, then
   **Create as a plugin**. Inspect its tools and install the resulting Plugin.
5. Start a new conversation, type `@`, select it and request `aee_status`, followed
   by `aee_agents`. Inspect host details privately.

The UI flow/auth choices are sourced from the
[custom MCP server guide](https://developers.openai.com/api/docs/guides/custom-mcp-server)
and [Plugin connection guide](https://developers.openai.com/plugins/deploy/connect-chatgpt).
Workspace restrictions apply. Account access, live auth selection and ChatGPT calls
are **NOT VERIFIED** by this patch.

No authentication is an integration inference from the static bearer gateway and
tunnel forwarding configuration, not observed live UI behavior. AEE has no OAuth
server or per-user job authorization. If workspace policy requires OAuth, complete
a separate integration; never disable gateway auth or paste an OpenAI key into a
Plugin authentication field.

## Smoke test and expected tools

| Tool | Expected behavior |
|---|---|
| `aee_status` | Host/runtime status without dispatch |
| `aee_agents` | Discovery; availability differs from execution permission |
| `aee_dispatch` | Controlled `codex` / `read_only` job; returns `job_id` |
| `aee_job_status` | Poll the returned job's lifecycle |
| `aee_job_result` | Terminal result/error; unfinished job returns `JOB_NOT_COMPLETE` |

Require exactly these five names. `aee_exec` must never appear or be callable through
this Plugin. Stop use and correct the target/filter if discovery differs. After a
configuration change/reconnect, refresh tools in Plugin details and repeat checks.

Only after separate runtime/job approval, test a harmless task in a reviewed existing
directory using `agent: codex`, `mode: read_only`, `working_directory` and a nonempty
`task`. Retain `job_id`, poll status and inspect the terminal result. Unsupported
agents/modes must fail. Keep private paths and raw results out of shared evidence.
Discovery is not inference acceptance; do not bypass a production gate rejection.

This route implements no unrestricted shell, write/destructive dispatch, Hermes/Claude
remote execution, Computer Use or automatic fleet updates. Follow
[operations](operations.md) for failures and disabling new use. Public Plugin distribution
is outside this private tunnel guide.
