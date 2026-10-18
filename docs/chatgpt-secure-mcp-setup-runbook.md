# ChatGPT Secure MCP — setup and refresh runbook (A2 trusted dispatch)

Sanitized operator runbook for standing up, verifying and refreshing the
ChatGPT connector against an A2 deployment running the **Secure MCP trusted
dispatch** policy. Every value marked `<...>` is private: provision it on the
host, never paste it into a ticket, chat, screenshot or any file that leaves
the machine. This runbook is sanitized-safe by construction — it contains no
tunnel IDs, bearer values, principal digests or connector names; all of them
live in the operator's local provisioning notes.

The transport prerequisites are documented once in the
[end-to-end tunnel guide](openai-secure-mcp-tunnel.md) and the connector
UI walkthrough in the [Plugin deployment walkthrough](chatgpt-plugin-deployment.md).
The A2 trust model itself is described in
[autonomous MCP runtime](mcp-runtime-autonomous.md). This runbook strings the
three together in execution order and adds the refresh (rotation) path.

## 0. Preconditions

1. A2 deployment installed per [host setup](deployment.md) §3–§4, gateway
   healthy on `127.0.0.1:8791` with `AEE_MCP_SURFACE=restricted` and
   `AEE_MCP_REQUIRE_AUTH=true` (the gateway refuses to start otherwise when
   the trusted policy is configured).
2. Tunnel client provisioned per [tunnel guide](openai-secure-mcp-tunnel.md)
   §1–§3 — outbound HTTPS only, no inbound ports on the host.
3. A verified commit checkout: the deploy/verify guide lists the exact
   review gates ([deploy and verify](a2-secure-mcp-deploy-verify.md)).

## 1. Provision the private materials (host-local, never in Git)

| Item | Where it lives | Notes |
|---|---|---|
| Gateway bearer `MCP_BRIDGE_API_KEY` | gateway env (mode 0600) via systemd credential | `<provision-private-restricted-key>` |
| Trusted-dispatch policy file | operator-chosen path, mode `0600`, parent dir `0700` | referenced by `AEE_TRUSTED_DISPATCH_POLICY` |
| Principal entry | inside the policy file only | the **SHA-256 digest** of the bearer, a name, the allowed agents |
| Tunnel forwarding secret | tunnel client config (mode 0600) | same bearer value; the gateway sees the digest |

Generate the policy file from a private scratch location (never the repo
checkout directory):

```bash
umask 077
install -d -m 0700 "$HOME/.config/aee"
# Print the digest from the private bearer for the policy file (run in a
# private shell; do not capture the output into any log or ticket):
printf '%s' '<gateway-bearer>' | sha256sum | cut -d' ' -f1
```

Then create the policy JSON at `$HOME/.config/aee/trusted-dispatch.json`
(mode `0600`) with this shape — schema and values are checked by the gateway
at startup; a readable-but-invalid file is a hard failure, never a fallback:

```json
{
  "schema": "aee-secure-mcp-trusted-dispatch-v1",
  "policy": "secure_mcp_trusted_dispatch",
  "transport": "secure_mcp",
  "enabled": true,
  "agents": ["claude", "codex"],
  "principals": {
    "<sha256-of-bearer>": "<principal-name>"
  }
}
```

Sanitization boundary: the JSON template above uses a placeholder digest and a
generic descriptor. Substitute both privately. Never export, screenshot or
archive the populated file.

## 2. Enable trusted dispatch in the gateway env

Add exactly one env entry to the restricted gateway env (keep the existing
bearer, surface and bound-host entries unchanged):

```bash
AEE_TRUSTED_DISPATCH_POLICY=$HOME/.config/aee/trusted-dispatch.json
```

Startup contract at `mcp_gateway.py` import time: with the policy configured,
the process requires `AEE_MCP_REQUIRE_AUTH=true`, a bearer configured, a
loopback host binding and the `restricted` surface — otherwise it **refuses to
start**. Also confirm `AEE_MCP_EXPOSED_TOOLS` equals the six-tool contract or
is unset (the gateway enforces the full six-tool set on this surface):

```bash
systemctl cat aee-p2c-gateway@restricted.service | grep -E 'EnvironmentFile|ExecStart'
systemctl --quiet show aee-p2c-gateway@restricted.service -p ActiveState
```

Apply with the normal service lifecycle (this is an operator step; the
runbook does not replace change-approval):

```bash
systemctl restart aee-p2c-gateway@restricted.service
```

## 3. Verify the trust chain (local, before touching ChatGPT)

1. `GET /health` and `GET /status` (unauthenticated, fact-only) report
   `trusted_dispatch`: `policy`, `configured: true`, `valid: true`,
   `write_autonomous_enabled` matching the policy's `enabled`, and
   `per_job_token_required: false`.
2. `/status.autonomous.authorization` reads
   `secure_mcp_trusted_dispatch` (legacy deployments keep reporting
   `single-job-capability`).
3. Auth negatives still reject: a wrong bearer is `401`; forged forwarded
   headers (`X-User`, `X-Forwarded-Authorization`) never establish trust; the
   trust context is only set for the restricted loopback surface.
4. Elevated dispatch smoke from the host (read a synthetic task against the
   approved workspace snapshot) completes and the job record carries the
   authorization proof — principal name, agent, mode, canonical
   `working_directory` — in the job metadata.
5. Kill switch: atomically replace the policy file with `enabled: false` —
   the policy is re-checked before CLI probes, at worker start and immediately
   before every execution, so new write/autonomous admissions are refused
   while read-only dispatch still works (a restart is not required, but is
   always safe). Revert afterwards.

Gates 1–3 must pass before the connector is registered. Gate 5 is the
operator's rehearsal of the emergency stop and is normally done once per
deployment, not per refresh.

## 4. Register/refresh the ChatGPT connector

Follow the [Plugin walkthrough](chatgpt-plugin-deployment.md) §2–§4.

```bash
systemctl --quiet is-active tunnel-client
```

- First setup: create the private connector bound to the provisioned tunnel
  ID; validate the six-tool discovery list and the `mode` enum
  (`read_only | write | autonomous`).
- Refresh (bearer rotation): rotate the gateway bearer and the tunnel
  forwarding secret together (walkthrough §6), recompute the digest from the
  new credential, replace the single principal digest line in the policy file
  with the new one, restart the gateway once, then re-run §3 gates 1–4. The
  old digest is dead the moment the policy file is rewritten; no other config
  changes are needed.
- Refresh (policy change): edit `agents`/`principals` in the policy file only.
  `principals` is capped at 32 entries; names must be unique. Restart once;
  in-flight jobs re-validate at start and at execution against the new file —
  a revoked principal cannot persist a job that was admitted before the edit.

## 5. Emergency stop and rollback

- **Stop elevated dispatch, keep read-only**: `"enabled": false` + restart
  (§3.5). This is the fastest lever and leaves the tunnel and connector intact.
- **Disconnect ChatGPT entirely**: remove/demote the private connector, stop
  `tunnel-client`, park the tunnel ([walkthrough §6](chatgpt-plugin-deployment.md#6-rollback)).
- **Full legacy fallback**: remove `AEE_TRUSTED_DISPATCH_POLICY` from the env
  and restart; the gateway returns to the documented pre-A2 path (legacy
  write roots + per-job approval token / single-use autonomy capability). No
  data migration is needed in either direction
  ([migration plan](a2-uninstall-reinstall-migration.md)).

## 6. Sanitization checklist (audience: whoever archives evidence)

Never export from the host into tickets, PRs, screenshots or reports: bearer
values or their digests, the populated policy JSON, tunnel IDs/control-plane
credentials, connector names beyond the label, job spool contents, and
machine-specific env files. Archive instead the fact-only surfaces —
`/health`, `/status` JSON with only field names/state booleans — and note the
commit SHA of the checked-out release.