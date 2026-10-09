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

## Job store

Job records live under the configured `A3_JOB_STORE_DIR` (0700 directory, files
0600 in the production layout). Inspect with the MCP tools
(`aee_job_status`/`aee_job_result`) rather than reading files directly; a
completed record must remain a valid manifest-conforming checkpoint. Stop any
gateway before moving or pruning the store.

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