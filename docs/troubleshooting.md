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
| Code Mode accepted success rejected | Since R3, accepted success needs each observed code-mode execution decoded from the pinned binary's own result header with broker cross-matched declared calls, still failing closed (`REQUIRED_OPERATION_UNVERIFIABLE`) for code-mode `exec`/`wait` or anything without machine-verified completion; check the receipt and [deployment §4c](deployment.md#4c-completed-job-smoke-test-real-http-mcp-production-seal) |
| Tunnel absent from ChatGPT | Check workspace association, tunnel permissions and healthy client; follow official linked guides |
| Job fails `REQUIRED_TOOL_FAILED`, `exit_code=0`, null summary | Inspect the retained failing event and private spool. A command with an integer exit code is a recoverable observation, including nonzero exits. A `failed` `mcp_tool_call` is recoverable only when its `result` key is present; `completed` / `in_progress` MCP items remain nonfatal without `result`. Declined MCP calls, failed MCP calls lacking `result`, error items and other failed/declined items remain fatal. Recovery still requires the final turn completion and answer plus process success; later completion cannot clear a fatal event. Compare streamed and buffered parser results using the original spool read-only. Never relabel or rewrite a historical failed record |
| `INVALID_AGENT` / `INVALID_MODE` on dispatch | Expected: the restricted contract is `agent=codex`, `mode=read_only` only. `aee_agents` availability for Claude Code/Hermes is discovery, not authorization |
| `/home/...` dispatch target rejected/invisible | By design: the gateway service unit sets `ProtectHome=yes`, so `/home` cannot be traversed. Do not chmod or relax; dispatch against the approved snapshot `/opt/aee/current/source` |

### MCP recovery verification

The MCP exception checks `result` key presence, not truthiness. It does not
clear an earlier fatal error or weaken process, terminal-event or answer checks.
Run `test_codex_tool_failure_recovery_semantics` with `PYTHONPATH=.:tests/mcp`
through `python -m unittest`; replay retained spools through both parsers read-only.
Record baseline and installed source digests, test evidence and a rollback copy
before restarting. Restart only from an independent operator process when no jobs
are active, then verify the new PID, health and imported source digests. If restart
or verification is unsafe, report live activation as blocked. Roll back only the
changed parser files from the saved copy; preserve other working-tree changes.

### `REQUIRED_TOOL_FAILED` attribution runbook

1. Record the job ID and timestamps (UTC) from `aee_job_status`.
2. Read the bounded result through `aee_job_result`; with the diagnostics
   patch the record carries the failing event summary — otherwise the failing
   native event is not retained on disk (sandbox tmpfs is destroyed), and
   attribution uses reproduction instead.
3. Reproduce through the same production `/mcp` surface with a small matrix of
   read-only tasks: (A) pure `ls` listing — completes; (B) exit-zero-guarded
   existence check — completes; (C) an *expected* nonzero exit (`grep -q`
   no-match) — fails `REQUIRED_TOOL_FAILED`, proving the fail-shot class;
   (D) multi-step inspection — completes; (E) no-tool-use reply — completes.
   Keep attempts bounded (≤3 per case).
4. Distinguish: *operation execution failure* vs *expected negative query
   result* vs *agent task failure* — only (C)-shaped benign nonzero exits
   should be rewritten into exit-zero-safe task phrasing; genuine required-tool
   failures must keep failing.
5. `exit_code=0` together with a `failed` status is intentional and preserved:
   the native executor finished its turn normally; the contract fails the job
   because a required operation failed. Never reinterpret it as success.
6. Job records live in a `0700` store (no credentials, redacted fields only);
   copy them out for analysis only with an operator-authorized sudo command
   that writes 0644 copies to a private `/tmp` evidence directory.

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
