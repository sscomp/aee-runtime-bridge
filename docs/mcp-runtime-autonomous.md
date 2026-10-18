# A2 Secure MCP trusted dispatch

A2 explicitly enables `secure_mcp_trusted_dispatch`. The existing approved
ChatGPT Secure MCP transport principal may dispatch `read_only`, `write`, and
`autonomous` jobs without approval metadata, capability minting or token handoff.
The principal identifies the transport, not an individual remote human.

The chain is ChatGPT's approved connector → outbound Secure MCP tunnel →
`127.0.0.1:8791/mcp` → bearer verification → restricted middleware identity →
private policy principal mapping → dispatch admission. Metadata, forwarded
headers, caller names and loopback reachability cannot establish this identity.
Local/full MCP and future transports do not receive this trust context.

Set `AEE_TRUSTED_DISPATCH_POLICY` to an owner-private JSON file (0600, parent
0700). The placeholder below is invalid until filled by the local operator:

```json
{
  "schema": "aee-secure-mcp-trusted-dispatch-v1",
  "policy": "secure_mcp_trusted_dispatch",
  "transport": "secure_mcp",
  "enabled": true,
  "principals": {"<SHA256 of dedicated gateway bearer>": "a2-tunnel"},
  "agents": ["claude", "codex", "hermes"]
}
```

Only qualified agents belong in `agents`; a trusted caller cannot bypass
installation, native feature or registry checks. Pi is outside this A2 change.
The policy is checked before CLI probes, after probes, at worker startup and
immediately before execution. Malformed, insecure, missing or revoked policy
fails closed. Do not enable this policy if the dedicated bearer is shared with
unapproved callers. The OS service account and its administrator remain trusted.

Example arguments for `aee_dispatch` (omit metadata and selector fields):

```json
{
  "agent": "codex",
  "mode": "autonomous",
  "working_directory": "/operator/chosen/disposable-fixture",
  "task": "Create probe.txt, read it, update it, verify it, delete it, then report the verified operations."
}
```

`autonomous` uses the existing OS-native headless CLI flags, without per-tool
prompts or an AEE directory allowlist. It inherits service UID/GID/groups and
actual DAC/ACL privileges, without escalation. `write` retains native write
adapter behavior (Codex workspace-write, Claude acceptEdits), while its legacy
AEE write-root/token gates are replaced by the explicit principal policy.
`read_only` retains its existing boundary: Claude restricted safe mode with
Read/Glob/Grep and empty strict MCP; Codex native read-only sandbox. Never
reinterpret a read-only job as autonomous. Existing OS-account capabilities,
including group membership and provider credentials, remain relevant.

Emergency stop: atomically replace the policy with `enabled: false`. This
immediately refuses new write/autonomous admissions and pending launches without
restarting the service; read-only dispatch and authenticated status/result/cancel
remain available. Removing a principal prevents its new dispatches and pending
launches. Already-running processes continue until normal completion, budget
expiry or `aee_job_cancel`; revocation does not implicitly cancel a job.

Audit records contain non-secret principal, transport, agent, mode, task digest,
resolved cwd/inode, OS identity and admission time. Workers retain start/end,
exit/error, process identity, bounded redacted results and durable streaming.
Existing single-active-job limits, budgets, cancellation, retention and restart
reconciliation apply. Interrupted jobs are not automatically retried.

A2 no longer configures `AEE_AUTONOMOUS_GRANTS`, `AEE_AUTONOMOUS_POLICY`,
`AEE_WRITE_APPROVAL_TOKEN` or `AEE_WRITE_ALLOWED_ROOTS`. The legacy capability
module and legacy security tests remain for other deployments whose explicit
trusted policy is absent. A configured broken trusted policy never falls back
to legacy authorization. Roll back A2 by disabling this policy, then restoring
the previous unit and legacy environment if needed; retain the hardened
read-only release. No credential values belong in documentation or argv.

The restricted tool contract has six tools: `aee_status`, `aee_agents`,
`aee_dispatch`, `aee_job_status`, `aee_job_result`, `aee_job_cancel`.
Final external ChatGPT autonomous E2E must be performed independently after
Production deployment; local MCP tests do not satisfy that acceptance gate.
