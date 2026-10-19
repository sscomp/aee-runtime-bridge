# A2 — clean uninstall, reinstall and migration plan (A3-era and Abacus)

Sanitized plan for taking an existing AEE v2 deployment cleanly offline,
reinstalling it onto the A2 Secure MCP trusted-dispatch release, and migrating
its persistent state — without leaking anything and without losing durable
records. Two target profiles are covered:

- **A3-era profile** — deployments on the A2/A3 job-store contract
  (`A3_*` env, `A3JOB-` job IDs, restricted six-tool gateway), where only the
  gateway/broker/credential material must be replaced and the job store
  carries over.
- **Abacus profile** — a fleet host whose AEE checkout and provider
  configuration live under a private per-host root (`~/Abacus/...` on the
  reviewed host), including the Abacus.ai RouteLLM provider settings consumed
  elsewhere in the runtime.

Every `<...>` value is private. The plan deliberately names files and
boundaries, never values: bearer keys, provider keys, tunnel credentials and
host-specific paths stay inside the 0600 files described here. Nothing from
this document belongs in Git, tickets or screenshots after provisioning.

## 1. Persistent data boundaries (decide before touching anything)

| Boundary | Location | Backup? | Migrate? | Notes |
|---|---|---|---|---|
| Job store | `A3_JOB_STORE_DIR` (`/var/lib/aee/jobs` in production) | **yes** | **yes** | Completed records are durable and bit-exact; moving the store re-publishes them at the new path. Stop the gateway before touching it. |
| Gateway/broker env + credentials | `/etc/aee/*.env`, `/etc/aee/secrets/` | **yes, private-only** | re-provision, not copy | Secrets are regenerated/re-authorized on reinstall; copied old credentials are only a rollback safety net. |
| Trusted-dispatch policy | host-local policy JSON (mode 0600) | **yes, private-only** | **yes** | A2-only file; carry across reinstalls of the same deployment, never between deployments. |
| Workspace snapshots under dispatch roots | `A3_DISPATCH_ALLOWED_ROOTS` / `AEE_MCP_ALLOWED_ROOTS` targets | **yes** | **yes** | Immutable snapshots; the gateway binds jobs to their canonical root + directory inode identity. |
| Legacy runtime DB | `data/*.db` (+`-wal`/`-shm`), `dispatcher.db*` | optional | no | Old HTTP/profile runtime state, unrelated to the MCP job store. |
| Logs, spool temp, caches, `.venv` | `logs/`, `/run/aee-broker`, venv dirs | no | no | Regenerated on reinstall. |
| Provider/provider-key config (Abacus profile) | host-local provider env (0600) | **yes, private-only** | re-provision | Base URL/endpoint name may rotate; the key never migrates in backups that leave the host. |

Backup rule: one archive per deployment, written **to operator-controlled
local storage** (not the release tree, not the job store), never pushed or
synced anywhere. Record SHA-256 receipts next to the archive and verify before
uninstall.

## 2. Backup (explicit, before stop)

```bash
umask 077
BACKUP_ROOT="<operator-local-backup-root>/aee-backup-<datestamp>"
install -d -m 0700 "$BACKUP_ROOT"
systemctl is-active aee-p2c-gateway@restricted.service aee-p2c-broker.service || true
```

The gateway must already be stopped for the job-store step (§3); collect env,
policy and snapshot copies first, gate the job store on the stop below.

## 3. Clean uninstall (operator-authorized, production)

Order and boundaries follow [troubleshooting](troubleshooting.md) §Uninstall:

1. **Quiesce.** Drain or cancel in-flight jobs (`aee_job_status`), then stop
   and disable the units; keep them listed for reference:

```bash
sudo systemctl stop aee-p2c-gateway@restricted.service aee-p2c-broker.service
sudo systemctl disable aee-p2c-gateway@restricted.service aee-p2c-broker.service
sudo systemctl list-unit-files 'aee*'
```

2. **Job store offline copy** (now that the gateway is stopped). Resolve the
   store path from the restricted env file rather than sourcing it
   ([p2c README](../config/p2c/README.md): render values privately, never
   `source`):

```bash
JOB_STORE="$(grep -E '^A3_JOB_STORE_DIR=/.+' /etc/aee/gateway-restricted.env | cut -d= -f2-)"
JOB_STORE="${JOB_STORE:-/var/lib/aee/jobs}"
tar -czf "$BACKUP_ROOT/job-store.tgz" -C "$(dirname "$JOB_STORE")" "$(basename "$JOB_STORE")"
sha256sum "$BACKUP_ROOT/job-store.tgz" > "$BACKUP_ROOT/job-store.tgz.sha256"
sha256sum -c "$BACKUP_ROOT/job-store.tgz.sha256"
```

   The production layout requires the directory 0700 and its files 0600 when
   restored — fix ownership after copying back.
3. **Kill switch first, secrets last.** Flip the trusted-dispatch policy to
   `enabled: false` (or delete the env line) so no later step can re-admit
   elevated jobs; then archive the private env/policy/provider-key material
   into the same 0700 backup root as a second, separately-stored archive that
   never leaves the host.
4. **Remove installed material selectively**, not a `rm -rf` sweep: unit and
   drop-in files, then `systemctl daemon-reload`; then (only after checking
   ownership and that nothing else uses them) the release tree
   (`/opt/aee/releases/<commit>`, `/opt/aee/current`), the gateway venv,
   `/etc/aee`, and the dedicated runtime accounts. A plain checkout or `.venv`
   deletion removes offline preparation only — systemd units, accounts and
   the job store are not touched by deleting files and must be handled
   explicitly as above.
5. **Tunnel/ChatGPT side** ([rollback](chatgpt-plugin-deployment.md#6-rollback)):
   remove/demote the private connector, stop `tunnel-client`, park the tunnel.

## 4. Reinstall to A2

1. Clone/checkout the reviewed A2 release commit
   (`d90f248` on `repair/a2-secure-mcp-trusted-dispatch`, or its published
   review descendant) and re-run every source-state gate from the
   [deploy/verify guide §1](a2-secure-mcp-deploy-verify.md) on **this** host.
2. Re-provision — do not blindly copy — env and credentials: follow
   [host setup](deployment.md) §0–§4 and the restricted env template. Restore
   the job-store archive into the new `A3_JOB_STORE_DIR` and fix ownership
   (0700 dir / 0600 files). Re-provision the trusted-dispatch policy file and,
   on the Abacus profile, the provider config (base URL may rotate; the key
   stays the operator's new/rotated value).
3. Build, verify and qualify the immutable release, then start through the
   deployment gate exactly as [deploy/verify §2–§3](a2-secure-mcp-deploy-verify.md).

## 5. Post-migration acceptance gates

Run the full gate table from [deploy/verify §4](a2-secure-mcp-deploy-verify.md)
(G1–G8) plus migration-specific checks:

```bash
curl -fsS http://127.0.0.1:8791/status | .venv/bin/python -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps(d.get("trusted_dispatch"), indent=2))'
systemctl is-enabled aee-p2c-gateway@restricted.service aee-p2c-broker.service
```

- **M1 job-store continuity:** every completed record from the backup is
  re-fetchable over the MCP tools (`aee_job_status`/`aee_job_result`) and its
  integrity validates against the completed-record manifest — never bless a
  record that fails validation.
- **M2 identity:** migrated paths resolve to the new host's canonical roots;
  `aee_status` hostname and old job `working_directory` values must be
  interpreted as historical, not authoritative.
- **M3 elevated-dispatch smoke:** one approved `write` or `autonomous` job
  against a workspace snapshot completes with the authorization proof attached.
- **M4 provider smoke (Abacus profile):** one broker-qualified job using the
  RouteLLM provider completes, proving the re-provisioned provider env works.
- **M5 cold boot:** reboot once and confirm units return
  ([operations](operations.md) lifecycle checks).

## 6. Rollback matrix

| Situation | Action |
|---|---|
| Elevated dispatch misbehaves, keep serving | kill switch: policy `enabled: false` (no restart needed) |
| Gateway config regression | restore the backup env file, restart gateway, re-run G2–G4 |
| Bad release (code-level) | check out the previous reviewed commit, rebuild, reinstall `current` symlink; the job store is release-agnostic |
| Full redeploy failed | re-provision from the §2 backup into the old layout only after the new layout is stopped; restore the job store archive; restore legacy units |
| ChatGPT-side regret | remove connector, stop tunnel, park tunnel — gateway unaffected ([runbook §5](chatgpt-secure-mcp-setup-runbook.md)) |

Rollback never force-overwrites the job store: restore archives only to a
stopped gateway, and keep both a pre-migration and a post-migration archive
until retention is approved.

## 7. Records

Archive locally: backup SHA receipts, uninstall checklist with timestamps,
reinstall env **names**, gate results per G1–G8 and M1–M5, restart/reboot
timestamps. No bearer or provider key values, no digests, no tunnel IDs, no
job-content excerpts.