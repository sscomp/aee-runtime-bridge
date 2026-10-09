# Host operations

The authoritative setup sequence is [deployment](deployment.md). Follow
[agent operations](agent-operations.md) for local acceptance and
[troubleshooting](troubleshooting.md) for rollback/uninstall. The old profile
installer remains documented in [HTTP/profile compatibility](legacy-http-profiles.md).

Quick reference for a running host (paths follow whichever route was installed;
see [deployment](deployment.md) §2b/§2c for local units and §4 for the
production layout). Names and paths are examples — match the locally installed
unit names.

## Health and surface

```bash
curl --fail --silent --show-error http://127.0.0.1:8791/health
echo "Bearer $MCP_BRIDGE_API_KEY"; : # bearer for the checks below
```

MCP handshake, tools list (expected: exactly the restricted five), auth
negatives and completed-dispatch checks are scripted in
[agent operations](agent-operations.md). Local/full `aee_exec` lives on port
8790 and is intentionally not exposed through the restricted surface.

## Services

```bash
systemctl --user status aee-v2-local-gateway aee-v2-local-broker
journalctl --user -u aee-v2-local-gateway -n 100 --no-pager
# production (root-installed) equivalents:
sudo systemctl status aee-p2c-broker.service aee-p2c-gateway@restricted.service
```

Keep user-level units **not enabled at boot** unless the operator decides
otherwise; re-check after host restarts (`systemctl --user list-units 'aee*'`).
Production system units are enabled for boot persistence only after the
documented cold-boot validation ([deployment](deployment.md) §4b) — on the
reviewed host, broker + restricted gateway survive reboot with persisted
completed records re-fetchable over the HTTP MCP.

## Boot persistence cold check

```bash
systemctl is-active      aee-p2c-broker.service aee-p2c-gateway@restricted.service
systemctl is-enabled     aee-p2c-broker.service aee-p2c-gateway@restricted.service
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8791/health
ss -ltn | grep 8791      # restricted listener stays loopback-only
```

## Job store

Job records live under the configured `A3_JOB_STORE_DIR` (0700 directory, files
0600 in the production layout; `/var/lib/aee/jobs` in the production install).
Inspect with the MCP tools
(`aee_job_status`/`aee_job_result`) rather than reading files directly; a
completed record must remain a valid manifest-conforming checkpoint. Stop any
gateway before moving or pruning the store.

Persisted completed records survive gateway restarts and host reboots
bit-for-bit. After a restart/reboot, re-fetch them over the HTTP MCP and confirm
`status: completed` with the `aee-completed-v1` result-contract seal
([deployment](deployment.md) §4c).

## Credential handling

Gateway bearer and broker model credential are file-based, outside any
checkout; rotate by writing the new file, restarting the gateway (and broker for
the model credential), and confirming an auth negative with the old value
before discarding it. Never paste credential values into tickets, transcripts
or command output.

## Rollback entry points

For stop/disable/removal sequences by route, start at
[troubleshooting](troubleshooting.md); the tunnel/Plugin rollback order is in
[end-to-end tunnel guide](openai-secure-mcp-tunnel.md) §5.

## Legacy bridge cutover (aee-bridge / a2-aee-tracker)

The legacy loopback HTTP bridge (`127.0.0.1:8787`, e.g. `uvicorn app:app`) and
its task tracker both run as user units. If a v2 deployment takes over as the
only AEE dispatch surface, retire exactly those two units — never their data,
files or credentials:

1. Inventory consumers first: any unit or script polling `:8787` (the tracker
   polls `/tasks`), and any ingress rule pointing a public hostname at the
   legacy port. Ingress (e.g. a cloudflared config) is **not** part of the
   cutover — leave it untouched and record which external hostname still
   resolves to the retired port.
2. Back up the unit files (copy + sha256) and record listener baselines.
3. Stop with the tracker first, then the bridge, so the tracker never sees the
   bridge disappear while running:
   `systemctl --user stop a2-aee-tracker aee-bridge`
   (example unit names — match locally installed ones), then
   `systemctl --user disable ...` both.
4. Verify: `:8787` gone, the v2 gateway healthy on its documented port, and
   unrelated services unaffected.
5. On any failure restore with `systemctl --user enable --now` on both units
   and report ROLLED_BACK with evidence; do not keep a mixed state.