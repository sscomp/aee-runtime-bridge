# OpenAI Secure MCP Tunnel for AEE

Complete [host setup](operations.md) first. The tunnel transports requests to the
private restricted gateway; it does not install AEE or grant execution rights.

## Install and associate

Download the matching client from
[Platform tunnel settings](https://platform.openai.com/settings/organization/tunnels)
or the upstream [latest release](https://github.com/openai/tunnel-client/releases/latest).
Install the reviewed executable on PATH, then inspect it:

```bash
tunnel-client --version
tunnel-client help quickstart
tunnel-client init --help
```

Create/select a tunnel in the intended Platform organization and associate it with
the intended ChatGPT workspace. Retain `<TUNNEL_ID>` privately. Runtime users and
the key principal need **Tunnels Read + Use**; administrators need **Read + Manage**
for tunnel administration. Use a runtime API key, not an administrative key, for
the client. Allow outbound HTTPS to `api.openai.com:443` (or `mtls.api.openai.com:443`
for configured control-plane mTLS), plus local gateway access. No public AEE listener
or inbound firewall opening is needed. See the
[official guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).

Account access, association and new-host installation are **NOT VERIFIED** by this
review. Secure MCP Tunnel supports private connections; public Plugin distribution
is a separate flow requiring public HTTPS. Publishing repository docs does not
publish a ChatGPT Plugin.

## Create a host profile

Use a new name `<TUNNEL_PROFILE>`; do not overwrite an existing profile. Replace
quoted placeholders locally. The standard directory is `~/.config/tunnel-client/`
unless XDG or client options override it.

```bash
tunnel-client init \
  --sample sample_mcp_remote_no_auth \
  --profile '<TUNNEL_PROFILE>' \
  --tunnel-id '<TUNNEL_ID>' \
  --mcp-server-url http://127.0.0.1:8791/mcp \
  --health-listen-addr 127.0.0.1:8792
```

The sample means no MCP OAuth flow; it does not disable AEE bearer auth. Before
doctor/run, privately edit the generated YAML to include these entries, using the
[profile template listed in configuration examples](../config/examples/README.md) as a schema
reference. Preserve the generated tunnel ID and other required fields:

```yaml
control_plane:
  api_key: "env:CONTROL_PLANE_API_KEY"
mcp:
  extra_headers:
    Authorization: "env:AEE_MCP_FORWARD_AUTH"
  discovery_extra_headers:
    Authorization: "env:AEE_MCP_FORWARD_AUTH"
```

Forward/discovery headers must reach only the configured private gateway origin.
The discovery option was checked against installed help; another client version
needs its own help/doctor check. Keep profile parents 0700 and profiles/private env
files 0600. Never commit the resulting profile.

| Private runtime variable | Meaning |
|---|---|
| `CONTROL_PLANE_API_KEY` | OpenAI runtime key from `<API_KEY_FILE>` through approved private handling |
| `AEE_MCP_FORWARD_AUTH` | `Bearer ` plus restricted gateway `MCP_BRIDGE_API_KEY` |
| `CONTROL_PLANE_TUNNEL_ID` | Optional env representation of `<TUNNEL_ID>`; generated profile already records it |

Supply variables through a private service EnvironmentFile or existing secret loader;
do not paste keys into argv or shell history. The
[credential template](../config/examples/tunnel-creds.env.example) uses systemd syntax.
OpenAI, gateway and inference credentials are separate.

## Validate, run and connect

With the gateway ready and private variables loaded, validate before starting:

```bash
tunnel-client doctor --profile '<TUNNEL_PROFILE>' --explain
tunnel-client run --profile '<TUNNEL_PROFILE>'
```

In another terminal check only the return status of loopback health endpoints:

```bash
curl --fail --silent --output /dev/null http://127.0.0.1:8792/healthz
curl --fail --silent --output /dev/null http://127.0.0.1:8792/readyz
```

Keep the client running for Plugin creation/discovery/calls. Doctor and run must not
compete for the same listener. Adapt the existing unit for persistence only in an
approved installation; reboot recovery is **NOT VERIFIED**. Continue to
[Plugin setup](chatgpt-plugin-deployment.md) after readiness.

CLI flags were checked against installed help. No real doctor, tunnel connection or
account modification was performed; end-to-end steps remain **NOT VERIFIED**. Use
the [troubleshooting table](operations.md) for missing tunnels, 401 and disconnections.
Stop use if `aee_exec` appears; never forward to full/local 8790. Preserve private
state during rollback.
