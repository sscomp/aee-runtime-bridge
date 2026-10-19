# Candidate installation and rollback — no deployment performed

This candidate targets the generic adapter route used on policy-absent hosts.
The separately approved native Codex/broker route keeps its reviewed resource,
receipt and deployment contracts. It still uses legacy bounded transport and must
not be advertised as large-workload qualified. Do not remove its manifest or gate
to bypass containment. Qualifying/migrating that route is a separate review gate.

A fresh host installs the existing locked Python/MCP dependencies (no new package
is required) and current tested CLIs. Codex requires native read-only sandbox;
Claude requires plan/JSON/no-session-persistence; Pi must advertise JSON plus
no-mcp/no-extensions/no-skills/no-prompt-templates/no-approve/no-session/tools.
An unknown Pi build fails admission, rather than enabling unrestricted tools.
Hermes remains refused. Runtime identity and node metadata use OS discovery.

Copy config/execution-budget.example.json to an operator-owned private config
path outside the release, mode 0600. Set AEE_EXECUTION_POLICY to that absolute
file path in the service-specific environment. A3_JOB_STORE_DIR must be a
private state directory outside the checkout, owned by the actual service user.
Spool is its owner-only `spool/` child. Existing records need no destructive
migration. Back up records, release, unit, environment and policy before any
separately authorized cutover. Keep secrets outside evidence and source control.

Budget precedence and restart semantics are in EXECUTION_BUDGET_CONTRACT.md.
Remove obsolete explicit A3_CODEX_TIMEOUT_SEC when adopting per-adapter policy,
or retain it consciously as a migration fallback; policy defaults/overrides win.
A3_CODEX_MAX_SUMMARY retains its bounded character-based summary role. A3 limits
continue to apply to the approved broker; they are not a way to raise its pinned
resource profile. Recheck inherited MemoryMax/TasksMax/CPUQuota/KillMode and the
sandbox on each host. Budget policy is not CPU/memory containment. Use an
operator-managed bounded filesystem/project quota and reserved state space where
system-wide ENOSPC resilience is required. Stream quotas cannot reserve free disk
against unrelated writers.

Before deployment review the exact commit and clean checkout, baseline backup,
CLI/backend authentication, isolated suite results, schema/auth allowlist and
current native support gaps. Do not deploy on the strength of fixture tests alone.
An approved deployment would stop new admissions, wait/cancel active work, back up
state, atomically select the release/config, restart only the intended service,
verify health/auth/tools and run separately authorized ChatGPT→MCP E2E. None of
these service operations are authorized by this candidate work order.

Rollback restores the prior release/unit/environment/policy atomically after
operator approval. Preserve job records and private spool evidence. No job resumes
after a crash: lease reconciliation marks it EXECUTION_INTERRUPTED and recovers
bounded spool counters/digests. A manifest may be complete even when the record
is interrupted (crash between the two atomic writes); this never implies job
completion. Old code ignores the additive execution fields but does not clean
new spool directories. Review/delete only expired unleased artifact IDs using
candidate cleanup after rollback, or an offline operator-reviewed process; never
run `rm -rf` on a live store. Config reload requires a restart, never a live edit
of Python source. Development rollback is a local revert/worktree disposal only.
