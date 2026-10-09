# Troubleshooting, rollback and uninstall

Use private diagnostics. Never publish environment files, keys, full job records,
raw model traffic, tunnel profiles or internal host inventories.

| Symptom | Action |
|---|---|
| Missing lock/module/GCC/Bubblewrap | Repeat deployment prerequisites and hash-locked sync; do not silently change Python/pins |
| Dirty source / bundle already exists | Use a clean reviewed checkout and new private `/tmp` destination; preserve original work |
| `DEPLOYMENT_GATE_CLOSED` | Obtain independent host qualification and operator approval; never drop the manifest |
| `RESOURCE_CONTAINMENT_UNAVAILABLE` | Check real service cgroup v2 memory/swap/task/CPU limits against pinned policy |
| Executor/sandbox identity mismatch | Compare native, companion and Bubblewrap to reviewed digests; no automatic upgrade |
| 401 / MCP discovery fails | Privately verify distinct gateway bearer and tunnel control-plane credentials and header references |
| `BUSY` | Poll the admitted job; verify the owning process/lease; do not delete a live lock |
| `JOB_NOT_COMPLETE` | Poll with a finite deadline; inspect terminal failure, not just HTTP status |
| Completed label rejected | Missing/tampered integrity or incomplete native/broker proof is a failure; never repair by relabeling |
| Code Mode accepted success rejected | Expected inner-operation proof is unsupported; use supported direct operations or await reviewed implementation |
| Tunnel absent from ChatGPT | Check workspace association, tunnel permissions and healthy client; follow official linked guides |

`journalctl -u aee-p2c-broker -u aee-p2c-gateway@restricted` is an operator-only
private check after installation. A failed service never justifies relaxing
localhost, authentication, allowed roots, native identity or tool filtering.

## Rollback

Before deployment record prior release pointer, config hashes, state backup and
unit files privately. Operator authorization is required to stop/restart services.
Stop the tunnel first; stop the restricted gateway and broker; preserve job/state
and verify no process owns a live lease. Switch `/opt/aee/current` to the previously
reviewed immutable release, restore its matched venv/config/units, reload systemd,
start broker then gateway, and repeat local identity/auth/tool acceptance before
reconnecting the tunnel. Never reuse a mismatched manifest or migrate live state.

The migration CLI supports dry-run and new **fixture-only** copies:
`python -m aee.mcp_runtime.migration --help`. It is not a production migrator.
The stricter result contract rejects unsealed legacy job records; preserve them
as private historical evidence. Do not fabricate success proofs. Plan any real
state conversion separately with the operator and a verified backup.

## Uninstall

With operator authorization, stop/disable the tunnel, gateway and broker; remove
only their installed unit/drop-in files, then reload systemd. Preserve private
state, credential backups and release artifacts until retention decisions are
approved. Remove the dedicated venv/release/config/accounts only after checking
ownership and that no other service uses them. Deleting a checkout or `.venv`
removes offline preparation only; it does not uninstall a system service.
