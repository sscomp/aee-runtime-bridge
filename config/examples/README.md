# Configuration examples

For a new host, begin with [host setup](../../docs/operations.md). Resolve source
availability and runtime approval before adapting these templates. Keep private
files outside Git; these examples provide no automatic installation or fleet updates.

These captured bootstrap/P2B templates are review inputs, not an installer for the
accepted P2C candidate. For the canonical private integration follow
[the tunnel guide](../../docs/openai-secure-mcp-tunnel.md),
[Plugin guide](../../docs/chatgpt-plugin-deployment.md) and
[operations](../../docs/operations.md). `config/p2c/` in the approved MCP source is the separate uninstalled
P2C system-service candidate; `.env.example` describes legacy HTTP.
No current live credential/profile content was copied; all examples stay placeholders.
The older P2B default-store and credential/egress notes below describe their original
phase. P2C acceptance updates qualified readiness, not their deployment authorization.

## Gateway and tunnel templates

These files document the captured contract; P2A does not install them.
On the P2B hardening branch, gateway examples additionally require a native
Codex ELF, trusted manifest and explicit surface. They cannot be promoted to
live use until the runtime deployment gates in the approved MCP source are met.
Every placeholder must be resolved locally. Never source an example as-is.
The `.env` examples use systemd EnvironmentFile syntax, not shell expansions.

| Example | Local destination / relationship |
|---|---|
| `gateway-local.env.example` | `.env.aee-mcp`, mode 0600; full six-tool gateway |
| `gateway-restricted.env.example` | `.env.aee-mcp-tunnel`, mode 0600; explicit five-tool surface |
| `tunnel-creds.env.example` | `.env.aee-tunnel-creds`, mode 0600; separate control-plane and forward credentials |
| `tunnel-client.yaml.example` | `~/.config/tunnel-client/<TUNNEL_PROFILE>.yaml`, mode 0600, parent 0700 |
| `*.service.example` | User systemd unit templates; paths assume `%h/aee-runtime-bridge` |

`AEE_MCP_FORWARD_AUTH` must be `Bearer ` followed by the **restricted
gateway's** key. `CONTROL_PLANE_API_KEY` authenticates tunnel-client to
OpenAI; it is a separate credential. Set tunnel ID in the local profile;
the env ID is also available for the operator's profile-initialization step.
The profile stores env references, not literal keys. The tunnel forwards
to `http://127.0.0.1:8791/mcp`; 8792 is its loopback health listener.

Replace path placeholders with absolute existing paths for your host.
Both gateway envs should point to the same persistent store and dispatch
root. The local surface exposes all registered tools when its exposure
setting is empty. The restricted example lists the five current tools.

In P2B, port 8791 forces the restricted surface. Its absent allowlist defaults
to the fixed five; an explicitly empty/expanded allowlist is rejected.
The default P2B store is `~/.local/state/aee-p2b-jobs`. Do not point a P2B
fixture at the live A3 store; active legacy records require migration review.

The templates capture existing `Type=simple`, restart and ordering behavior;
they provide no new readiness guarantees. Installed restricted-unit comments
still mention the older two-tool A2 surface; the env and fresh `tools/list`
establish the actual five-tool A3 surface. The example wording reflects A3.
Do not run tunnel-client doctor concurrently with the installed client using
the same fixed health port. Installation/reload/restart belongs to a future
authorized deployment task, not this baseline capture.
