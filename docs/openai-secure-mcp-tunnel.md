# OpenAI Secure MCP Tunnel — end-to-end setup guide

Stage 3B guide. **Status label: EXECUTED ON PRODUCTION A2 — the tunnel + Plugin
E2E has been run on Omarchy-A2 with a host-distinct tunnel and connector
("Omarchy-A2"); ChatGPT-side dispatch, polling and result retrieval were
exercised for real (completed jobs and a deterministic fail-shot case — see the
attribution runbook in [troubleshooting](troubleshooting.md)).** Everything below
remains operator-executed; nothing in this repository registers, connects or
authorizes a tunnel.

The Secure MCP Tunnel exposes a running restricted AEE v2 MCP gateway to ChatGPT
over **outbound HTTPS only**: the `tunnel-client` you run dials OpenAI's control
plane; no inbound port is opened on the gateway host. ChatGPT sends MCP requests
through the tunnel; the client forwards them to `127.0.0.1:8791` (restricted
six-tool surface). Checked against the official guides:

- [Secure MCP tunnels](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
- [Custom MCP servers](https://developers.openai.com/api/docs/guides/custom-mcp-server)

A shorter summary also lives in [ChatGPT setup](chatgpt-mcp.md).

## 0. Prerequisites (all four, in order)

| # | Requirement | Where to confirm |
|---|---|---|
| 1 | The restricted gateway runs and answers auth-positive on `127.0.0.1:8791/mcp` | [agent operations](agent-operations.md) checks |
| 2 | Dispatch actually completes (broker-qualified local dispatch or production) — otherwise ChatGPT can list tools but every job fails | [host setup §2c](deployment.md) |
| 3 | An OpenAI Platform account with permission to manage Secure MCP Tunnels, and an associated ChatGPT workspace | operator, in OpenAI Platform |
| 4 | An operator-reviewed `tunnel-client` binary (released from the official `openai/tunnel-client` project) | operator judgment, as with the Codex pin |

On Omarchy-A2 the A2-executed pin is `tunnel-client` **v0.0.16**, verified against
the release's `SHA256SUMS` and sigstore provenance before install. Pin and verify
your own binary the same way.

Do not proceed on a gateway that passes `/health` yet fails the MCP handshake —
the tunnel will happily forward traffic to a broken surface.

## 1. Obtain tunnel credentials (operator, in OpenAI Platform)

The operator creates a tunnel in OpenAI Platform and records two values:

- the **tunnel ID** (a resource identifier, not secret);
- the **control-plane API key** (`CONTROL_PLANE_API_KEY` — secret; never paste it
  into a shell history, file in a checkout, ticket or transcript; hand it to the
  client through a secret-reference or protected environment, per its `--help`).

The operator then grants the tunnel association with the target ChatGPT
workspace and creates the Plugin in ChatGPT ([plugin
walkthrough](chatgpt-plugin-deployment.md)).

## 2. Initialize the profile (gateway host, unprivileged)

```bash
tunnel-client --version
tunnel-client init --help
tunnel-client init --profile aee-restricted --tunnel-id "$AEE_TUNNEL_ID" \
  --mcp-server-url http://127.0.0.1:8791/mcp --health-listen-addr 127.0.0.1:8792
```

Flags were checked against tunnel-client `0.0.15`–`0.0.16`; verify them with the
installed client's `--help` before use. `--mcp-server-url` must be the restricted
surface (`:8791/mcp`), never the local/full `:8790`. Keep the health/UI listener
on loopback and avoid ports already bound — `8080` is commonly occupied (on A2
it is), which is why the executed host uses `127.0.0.1:8792`
(`ss -ltn 'sport = :8792'` first).

`init` writes the local profile; verify afterwards that no literal secret landed
in it. If your version stores secrets in the profile file, place the file in a
mode-0600 directory outside any checkout instead.

## 3. Wire credentials, run, and check health

The client needs two distinct secrets at run time:

1. `CONTROL_PLANE_API_KEY` — tunnel control plane (from Platform, §1);
2. the forwarded `Authorization: Bearer` header value — **must equal the
   gateway's own bearer credential** (`MCP_BRIDGE_API_KEY` in the gateway env
   file); it is forwarded as an `--mcp.*-extra-headers` reference, not a literal:

```bash
tunnel-client run --profile aee-restricted \
  --control-plane.api-key env:CONTROL_PLANE_API_KEY \
  --mcp.extra-headers 'Authorization: env:AEE_MCP_FORWARD_AUTH' \
  --mcp.discovery-extra-headers 'Authorization: env:AEE_MCP_FORWARD_AUTH'
```

**Header secret-reference semantics (verified against `v0.0.16` source): a
header value resolves a secret only when the *entire* value is a reference.**
`Authorization: Bearer env:VAR` would forward the literal string
`Bearer env:VAR`; the correct form is `Authorization: env:AEE_MCP_FORWARD_AUTH`
with the environment value containing the full `Bearer <key>` string. Confirm
against your installed version (`run --help`) — the reference must be a
whole-value `env:VAR` or `file:/path`. `AEE_MCP_FORWARD_AUTH` must be provided
by the same protected mechanism as `CONTROL_PLANE_API_KEY` — same channel, two
separate values.

Run it as a systemd **user** service with `loginctl enable-linger` so it persists
after logout (executed on A2: unit `aee-a2-tunnel.service`, `Restart=on-failure`,
`EnvironmentFile` holding the two secrets mode-0600 in a 0700 directory).

While it runs, the client reports health on loopback endpoints only
(`/healthz`, `/readyz`; optionally `/metrics` and a local UI). Confirm:

```bash
curl --fail --silent --output /dev/null http://127.0.0.1:8792/healthz
curl --fail --silent --output /dev/null http://127.0.0.1:8792/readyz
```

A green transport is not an MCP acceptance test — ChatGPT could still be
rejected at gateway auth or the workspace could refuse the Plugin. Complete §4
before trusting it.

## 4. Verify end-to-end (ChatGPT side, after the Plugin exists)

Follow [plugin walkthrough](chatgpt-plugin-deployment.md) to create the private
Plugin, then perform exactly the same MCP checks as [local
acceptance](agent-operations.md), but through ChatGPT:

1. discovery/status invocation succeeds and exposes exactly the five restricted
   tools (`aee_status`, `aee_agents`, `aee_dispatch`, `aee_job_status`,
   `aee_job_result`);
2. one operator-approved **read-only** dispatch completes and its result is
   bounded and redacted as expected;
3. negatives: wrong forwarded credential → rejected by the gateway; local/full
   `aee_exec` unreachable through the tunnel; revoking the tunnel in Platform
   disconnects the Plugin.

If any boundary check fails, stop the tunnel (§5), leave the gateway untouched,
and re-run local-only acceptance before retrying.

## 5. Failure handling, containment and rollback

- Only outbound HTTPS leaves the host; there is no inbound listener besides the
  loopback health port. Never point `--mcp-server-url` at anything except the
  restricted loopback surface.
- Tasks, admitted source content and bounded results cross the OpenAI boundary
  when a job runs; read [security and privacy](security-model.md) and the
  operator's data policy first. Tunnel transport does not promise zero product
  logging on the ChatGPT side.
- Rollback order: remove the Plugin (ChatGPT) → stop `tunnel-client` → revoke or
  park the tunnel in Platform. The gateway keeps running for local clients.
- ChatGPT custom-MCP-server authentication: the gateway is a static bearer
  target, not an OAuth server. The executed A2 flow used the **no-auth** Plugin
  mode (ChatGPT trusts the tunnel; the tunnel injects the forwarded bearer).
  Never weaken gateway auth to fit an OAuth-only mode
  ([walkthrough §2](chatgpt-plugin-deployment.md)).
- Still treat first runs on new hosts as fresh: run with restricted secrets only,
  and prefer stopping over weakening gateway authentication if auth modes
  conflict.

## 6. Status summary

| Item | Status |
|---|---|
| Official-docs basis of this guide | verified against `developers.openai.com` guides listed above |
| Local dispatch E2E without tunnel | verified on A2 (`/opt/aee/current/source` release `5b3a23b8…`; completed jobs with attestation) |
| Tunnel + Plugin E2E | **executed on Omarchy-A2, `tunnel-client` v0.0.16**: connector "Omarchy-A2" listed tools, dispatched, polled and retrieved results; one ChatGPT-path job failed fail-shot (`REQUIRED_TOOL_FAILED`) and was attributed — see [troubleshooting](troubleshooting.md) |

Node identity note: the job-id prefix `A3JOB-` and the gateway's internal
self-label are legacy naming from the shared AEE codebase; jobs so numbered
execute on the node that owns the tunnel. Verify with `aee_status` (hostname)
and the job's `working_directory` resolving to that node's release path.