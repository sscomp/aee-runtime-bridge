# Use cases and scenario matrix

What ChatGPT (and approved Secure MCP principals) can actually do on an AEE
host, per deployment profile and dispatch mode. The scenario matrix is the
navigation table; each scenario below states its preconditions, the profile
and mode it uses, its boundaries, and what evidence backs the claim.
Evidence labels follow the
[runtime capability matrix](runtime-capability-matrix.md):
**Reported** = operator acceptance receipt / job record, not re-executed by
the doc author; **Confirmed** = independently inspected contract/code;
**Not Tested** = open gate.

## Scenario matrix

| # | Scenario | Profile / mode | Adapter(s) | Evidence status |
|---|---|---|---|---|
| 1 | ChatGPT remote read-only dispatch (A2 / A3) | os-native or restricted / `read_only` | codex; claude where policy allows | Reported (A3 E2E job, exit 0) |
| 2 | Read-only audit of a source tree | os-native / `read_only` (trusted principal) or restricted broker path | codex (native sandbox), claude (tool allowlist), pi (tool denylist) | Reported for codex+claude; pi = untested E2E |
| 3 | OS-native write / autonomous authorized actions | os-native / `write`, `autonomous` (trusted-dispatch policy) | codex (workspace-write), claude (acceptEdits) | Reported (A3 write E2E, proof file; Claude autonomous docs job exit 0); codex `autonomous` dedicated E2E Not Tested |
| 4 | Multi-host independent dispatch | any profile, per host | any admitted | Confirmed architecture (host-local contract); cross-host E2E Not Tested |
| 5 | Deployment / reinstall of a host | os-native or restricted install | n/a | Reported (A3 clean reinstall 2026-10-20, PASS WITH CONDITIONS) |
| 6 | Operational diagnostics | n/a (status/result tools + audits) | n/a | Reported (local gates G1–G8, 401 negatives, six-tool surface) |
| 7 | Adapter unavailable / fail-closed | any | codex, claude, pi, hermes | Confirmed code paths; codex env-pin incident reported as real-world example |

## Preconditions common to every remote scenario

- A deployed gateway (loopback-only, restricted six-tool surface, bearer
  auth) and, for ChatGPT, a provisioned Secure MCP Tunnel (outbound-only).
- For `write`/`autonomous`: an explicitly provisioned trusted-dispatch
  policy (`aee-secure-mcp-trusted-dispatch-v1`, 0600/0700, principal
  fingerprint, per-agent list, `enabled: true` by explicit opt-in only).
  Policy-state semantics: **absent** policy falls back to the legacy
  authorization gates (read-only across supported adapters; write/autonomous
  only with the legacy per-job operator approval); a **configured-but-
  malformed** policy blocks all new dispatch, including `read_only`;
  **`enabled: false`** preserves read-only and refuses new
  write/autonomous; a **revoked principal** is refused for every mode.
- The job `working_directory` must already exist and be accessible under the
  service OS identity (os-native) or inside the allowed roots (restricted);
  autonomous and trusted-policy `write` dispatch validate the path directly
  and skip the allowed-roots list (see scenario 3 and the
  [capability matrix](runtime-capability-matrix.md)).
- Provider credentials available to the service identity; on A3 the Claude
  adapter additionally requires `ANTHROPIC_*` presentation env + user dbus,
  while Codex uses its own `~/.codex/auth.json` (host-specific, see install
  receipts).

## 1. ChatGPT remote read-only dispatch

ChatGPT's approved connector issues `aee_dispatch(agent=codex,
mode=read_only, working_directory=…, task=…)`. Transport chain:
ChatGPT → tunnel (outbound HTTPS) → loopback gateway → principal check →
job → adapter. Acceptance requires `queued` + job ID first, then poll
`aee_job_status`, then `aee_job_result` with `ok=true`, `status=completed`,
exit 0, bounded summary, ordered timestamps and a successful completion
proof — `adapter_proof` for adapter-path jobs (all agents) or the
`aee-completed-v1` native/broker contract for the approved codex broker path.
A concurrent second admission returns `BUSY`.
Evidence: A3 E2E job `A3JOB-20261019-928c4174f778e8f7edba4049e2b7be28`
(codex, exit 0, 2 native tool events started/completed, 0 failed) —
Reported. External ChatGPT Plugin `read_only` E2E passed for codex
(`928c4174…`) and claude (`A3JOB-20261019-6929c5831f9b3c8937cd6a0ee06ee6b9`,
exit 0) — Reported (2026-10-19 receipts, Secure MCP transport — the claude job started
`17:35:30Z` and the codex job `18:02:21Z`, after the install receipt
`2026-10-19T17:19:46Z`, so they already postdate the reinstall); plugin-path
`write`/`autonomous` E2E and a fresh ChatGPT-originated plugin revalidation
since those receipts remain Not Tested.

## 2. Read-only audit

An operator asks the agent to summarize/inspect a tree without modification.
`read_only` retains its boundary on every adapter (claude restricted safe
mode; codex native sandbox; pi denylist). Two working-directory models exist:
generic adapter jobs run the agent on the **actual working directory**, gated
only by the service OS identity's real DAC/ACL permissions as checked at
admission — on os-native the service identity is an existing OS user that can
normally read its **own** home directory, so `/home/<user>/...` targets
succeed there (admission also requires the directory to be enterable/readable
by the executor OS user, plus the optional `A3_DISPATCH_ALLOWED_ROOTS` list
when configured; without it no path roots are imposed); on the **restricted
broker path**, and only there, the codex job instead sees an admitted
read-only snapshot inside a hardened namespace where host HOME
(`ProtectHome`) is excluded. A directory the service identity cannot enter
(e.g. `/root` from a non-root service identity) fails DAC at admission
(`INVALID_WORKING_DIRECTORY` — receipt on A3). Residual risk of same-UID
execution remains: the agent inherits everything that OS user can read.
Acceptance: result only, no writes, unchanged source. Evidence: Reported for
codex and claude read E2E receipts; Pi E2E Not Tested (registry supports
`read_only` only).

## 3. OS-native write / autonomous authorized actions

With write-autonomous enabled by the policy, a trusted principal may modify
or operate in an authorized working directory: `write` uses the adapter's
native write scope (codex `workspace-write` confined to the job directory
and /tmp, network disabled by default; claude headless `acceptEdits`), and
`autonomous` uses headless bypass flags inherited within the OS identity.
Boundaries: DAC/ACL of the service identity (no escalation), TOCTOU
directory-identity recheck at launch (applies to `autonomous` and
trusted-admission jobs, including trusted-policy `write` — not to legacy
generic jobs without the policy), and per-adapter failure semantics: failed
tool events are recorded without failing a job whose final result succeeds
(Claude), while codex/hermes/pi and the broker contract treat a failed
required operation as `REQUIRED_TOOL_FAILED` — see
[Failure semantics](architecture.md). Legacy
per-job write gates are superseded when the policy is configured.
Emergency stop is an atomic policy flip (`enabled: false`).
Evidence: Reported — A3 real codex write job
`A3JOB-20261019-c06ebe28…` (under the policy principal, no per-job token)
created the proof file `trusted-write-proof.md` in its dedicated 0700
workspace; hermes refused both modes, `/root` dispatch refused
`INVALID_WORKING_DIRECTORY`; per-host install receipts 2026-10-20
(PASS WITH CONDITIONS). A dedicated Claude `autonomous` E2E receipt exists:
job `A3JOB-20261019-5f8b72872cd103dbedf364d5de90c430` (the A3 documentation
Git workflow) completed exit 0 — Reported — recording 57 completed tool
events and 4 failed tool events (native structured events; the failed events
are part of the record and are not concealed). Codex `autonomous` and
Claude `write` dedicated E2E receipts: Not Tested.

## 4. Multi-host independent dispatch

Each host runs its own gateway, tunnel client, adapters, job store and
policy; jobs never cross hosts and there is no central dispatcher. AEE does
not automatically route jobs, share job state or share policy between hosts
— dispatching on host A neither reads nor writes host B's store. This is not
an isolation guarantee from host B itself: B's dispatched agents run with
host B's service-identity permissions (including its network access), and B
is reached only by whoever independently dispatches there. SSH is optional
administration/debug transport only. Acceptance per host is separate (each
host's own gates). Evidence: architecture Confirmed (this contract is pinned
in the deployment-profile code/docs); a multi-host simultaneous E2E exercise
is Not Tested. Prior A2 parity audit reports real jobs for Claude/Codex/
Hermes on the A2 host — Reported historical evidence, distinguish it from
fresh per-host revalidation.

## 5. Deployment / reinstall

Canonical sequence: clone a reviewed commit (record `git rev-parse HEAD`),
hash-locked `uv pip sync --require-hashes requirements-mcp.lock`,
credential-free smoke, then either profile install path (installer never
acquires privileges; restricted staging prints an operator sudo sequence it
never executes) with preflight fail-before-write, idempotent apply, receipts
(env names only) and rollback. Acceptance: clean worktree, schema validation
`PASS`, secret scan `PASS`, post-install `/health` + six-tool handshake +
read-only E2E. Evidence: Reported — A3 clean reinstall 2026-10-20 from
commit `2b123e21` (os-native, evidence bundle
`~/AEE_A3_REINSTALL_EVIDENCE_20261020/`).

## 6. Operational diagnostics

`aee_status` / `aee_agents` (discovery is informational, not authorization),
`aee_job_status` / `aee_job_result` for job state, `aee_job_cancel` for
cancellation, `scripts/audit_adapters.py` for adapter resolution under the
service identity, the `REQUIRED_TOOL_FAILED` attribution runbook in
[troubleshooting](troubleshooting.md) for fail-shot analysis, and
operator-only `journalctl`. Evidence: Reported — A3 local/tunnel gates
G1–G8 parity, six-tool surface, 401 negatives, restart persistence, fresh
job-store continuity all PASS (2026-10-20 receipts).

## 7. Adapter unavailable / fail-closed

When an adapter cannot be resolved or cannot honestly enforce a mode, dispatch
refuses before launch (`ADAPTER_NOT_INSTALLED`, `ADAPTER_UNSUPPORTED`) —
availability does not silently downgrade mode. Two distinct checks are in
play: dispatch-time resolution is the registry's executable/`--help` probe
and mode refusal (which accepts any executable absolute path, including a
script wrapper), while the **native-ELF expectation is an operator
provenance check** on the resolved binary. Real-world instance: the A3
registry had previously declared codex `dispatchable=true` (static policy)
while the pinned binary was the npm JS wrapper, so the operator's
native-ELF identity check flagged it and the operator repinned both
`A3_CODEX_BIN`/`A3_ADAPTER_CODEX_BIN` to the native 0.160.0 ELF; the
subsequent read-only E2E completed exit 0 under `NATIVE_SANDBOX`.
Acceptance: refusing is the passing behavior for any unresolvable or
unsupported combination. Evidence: Confirmed code paths
(`job_adapters.py::refuse_unsupported`, `resolve_adapter`); incident
Reported with job ID `A3JOB-20261019-8a218391…`.