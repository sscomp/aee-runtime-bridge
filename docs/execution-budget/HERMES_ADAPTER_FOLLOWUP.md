# Hermes adapter follow-up work order

Status: UNSUPPORTED on this MCP route. Registry refusal ADAPTER_UNSUPPORTED
remains active even though a Hermes executable may be installed. Executable
presence and `--version` are not evidence of safe tool enforcement.

Required separate implementation/review:

1. Pin an installed Hermes version, inspect its noninteractive prompt input,
   JSON event/final formats, session persistence, toolset discovery and exact
   read-only enforcement. Never use an unverified `--safe-mode` assertion.
2. Prove exclusion of shell/edit/write, extensions, MCP side effects and toolset
   escalation under an isolated fixture and real CLI. Preserve actual OS identity.
3. Implement stdin-only prompt delivery, executable admission hash, capability
   probe, final-answer/progress projection and native budget-exhaustion mapping.
   Reuse ExecutionBudget/run_streamed/SpoolStore and the parent-death guardian.
4. Acceptance tests: >8MiB both streams, complete final event, malformed/partial
   UTF-8/JSON, quota/ENOSPC, cancel descendants, parent crash/restart, auth/path
   boundaries, no-session execution, blocked write attempts, real backend success
   and failure, fresh install on a second host. Mocks are not native E2E evidence.
5. Keep registry UNSUPPORTED until both enforcement and real result retrieval are
   evidenced. No production rollout, gate bypass or model-auth changes are part
   of this follow-up proposal without separate operator authorization.
