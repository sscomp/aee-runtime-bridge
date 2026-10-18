# OS-native autonomous jobs

`aee_dispatch(mode="autonomous")` runs a headless CLI as the gateway OS account
without per-tool approvals. It adds no OS privileges. The account's existing
credentials, groups (including Docker), network and DAC/ACL permissions remain
relevant. `read_only` and constrained `write` keep their existing argv and gates.
The six-tool restricted MCP surface gains only the explicit mode enum value.

Production must use authenticated loopback transport, systemd `NoNewPrivileges=yes`,
an owner-private `AEE_AUTONOMOUS_POLICY` file, and owner-private
`AEE_AUTONOMOUS_GRANTS` directory. Example policy (hash placeholder is deliberately
invalid until the operator replaces it locally):

```json
{
  "schema": "aee-autonomous-policy-v1",
  "enabled": true,
  "principals": {"<SHA256 of authenticated bridge bearer>": "a2-tunnel"},
  "agents": ["claude", "codex", "hermes"]
}
```

The tunnel principal identifies transport, not a remote human. An independent
single-use job capability proves authorization issued by the local OS operator.
Never put bearer tokens or job capabilities in documentation, argv, logs or Git.
The protected metadata field is `autonomous_approval`. Capability records store
only its SHA256, bind host/UID/GID/groups/principal/agent/exact-task/cwd-inode,
expire within 900 seconds, and are consumed atomically across processes.
Caller names in metadata are never trusted. No remote issuance tool exists.

Create a task file and an owner-private output directory, then run locally:

```bash
python -m aee.mcp_runtime.authorization \
  --policy /path/to/private/policy.json --grants /path/to/private/grants \
  issue --principal a2-tunnel --agent codex --cwd /operator/chosen/directory \
  --task-file /private/task.txt --ttl 300 --output /private/new-capability.txt
```

Supply that capability through a protected client channel in dispatch metadata.
Submit exactly the task file content. Native profile is `os-native`; omit
`execution_profile`, `model` and `reasoning_effort` selector fields. `aee_agents`
reports CLI installation, feature support, dispatch modes and enabled policy
separately; CLI presence does not prove provider readiness. Policy controls which
agents passed operator qualification and may actually be admitted.

Delete a pending or consumed grant with the local `revoke --grant-id <sha256>`
subcommand. Revocation/policy changes are checked at admission and before launch.
To stop running work, call `aee_job_cancel`; expiry does not kill an already
running job. A consumed capability is burned even if later admission is busy or
fails: obtain a new grant. Existing job/spool timeout, quota, retention and restart
reconciliation remain in force; interrupted jobs are not automatically retried.

Any OS-accessible existing absolute cwd is valid. Legacy `AEE_WRITE_ALLOWED_ROOTS`
and `A3_DISPATCH_ALLOWED_ROOTS` do not limit autonomous authority. A cwd scope
binds request identity and prevents substitution; it does not confine the agent's
filesystem operations. Held directory descriptors pin cwd at process creation.
No optional directory sandbox is currently advertised for autonomous.

Native mappings are version/feature checked at admission and execution:
Codex `exec --dangerously-bypass-approvals-and-sandbox`, Claude
`-p --dangerously-skip-permissions`, Hermes `chat --yolo --query-file - --format
stream-json` (no TUI/PTY), Pi `-p --mode json` with explicit built-in tools and
extensions/MCP disabled. Pi does not accept Claude's `bypassPermissions` flag;
its built-in tools need no approval when extension approval hooks are absent.
Provider failure is a failed job, never simulated success. Result validation
requires autonomous-specific authorization and process evidence, including for
Hermes whose read-only adapter is unsupported.
