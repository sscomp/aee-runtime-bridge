# AEE MCP deployment contracts

Start at [canonical deployment](../../docs/deployment.md). These are **uninstalled
operator templates**. This directory supplies resource/sandbox contracts, separate
local/restricted environment examples, broker UID configuration and systemd units.
No generated historical host evidence or example success manifest is shipped.

Resource and sandbox JSON bytes are pinned in the runtime. Editing their prose
also changes their identity and needs review. The production sandbox pins the
exact native Codex/companion and Bubblewrap build; other builds fail closed.

The external MCP Python venv belongs outside the immutable release. Gateway and
broker use different accounts; only the broker has `LoadCredential`. Restricted
port 8791 binds loopback, requires auth and exposes exactly five tools. Local/full
port 8790 is optional and never a tunnel target. Keep all environment substitutions
private; `<...>` values deliberately require operator provisioning.

Build/verify/plan emits a closed manifest. There is no automatic approval tool;
reviewers must qualify the target host and keep the approval evidence separately.
The optional tunnel readiness drop-in assumes an operator-reviewed `aee-tunnel`
unit and should be installed only when that unit exists.
