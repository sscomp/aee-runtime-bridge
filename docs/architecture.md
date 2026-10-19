# AEE v2 architecture

ChatGPT → OpenAI Secure MCP Tunnel → host `tunnel-client` → authenticated
loopback gateway → leased job → host-local agent adapter CLI → per-job Unix
socket broker (restricted profile) or provider env (os-native) → OpenAI
Responses. Results return through MCP and the tunnel. Each host retains its
own private job records; ChatGPT receives bounded redacted projections. See
[deployment](deployment.md) and [privacy](security-model.md).

| Component | Source | Contract |
|---|---|---|
| Gateway | `mcp_gateway.py` | Restricted six tools on 8791; local/full on 8790 |
| Admission and store | `aee/mcp_runtime/store.py` | One active lease; bounded private JSON; restart handling |
| Agent adapter registry | `aee/mcp_runtime/job_adapters.py` | Per-agent specs; honest mode enforcement; fail-closed resolution |
| Executor and sandbox | `aee/mcp_runtime/executor.py`, `sandbox.py`, `process.py` | Trusted immutable source, stdin task, namespaces, finite output/time/process limits |
| Result contract | `aee/mcp_runtime/result_contract.py` | Exit zero alone is insufficient; independent completion proof; failures sticky |
| Broker | `aee/mcp_runtime/broker.py` | Dedicated credential owner; per-job socket, UID/lease and request policy |
| Trusted dispatch | `aee/mcp_runtime/trusted_dispatch.py` | Explicit policy admission; never inferred from request metadata |
| Packaging and deployment | `aee/mcp_runtime/packaging.py`, `config/p2c/systemd/` | Committed source, immutable metadata, closed default approval gate |
| Compatibility product | `app.py`, `aee/cli.py`, `aee/adapters/`, `install.sh` | Existing HTTP/CLI profiles and deployed-host migration remain maintained |

## Deployment profiles

One canonical source, one six-tool surface, two deployment profiles — see
[deployment profiles](deployment-profiles.md) for the full contract
(`config/profiles/profile-schema.json`, schema `aee-deployment-profile-schema-v1`):

| | **os-native** (default) | **restricted** (explicit opt-in) |
| --- | --- | --- |
| Service identity | existing OS user, systemd user unit | dedicated system accounts (`aee-gateway`, `aee-broker`) |
| Boundary model | OS DAC/ACL + each adapter's native enforcement | systemd sandbox hardening + mandatory workspace confinement (`A3_DISPATCH_ALLOWED_ROOTS`) |
| Broker control socket | not used | `/run/aee-broker/control.sock` |
| Manifests | not required (`AEE_RUNTIME_MANIFEST` unset) | `A3_WORKSPACE_MANIFEST`, `AEE_RUNTIME_MANIFEST` required |
| Write/autonomous | trusted-dispatch policy | trusted-dispatch policy + broker approval chain |

Both profiles pin loopback-only bind (`AEE_MCP_HOST=127.0.0.1`), required
auth (`AEE_MCP_REQUIRE_AUTH=true`) and the restricted six-tool surface.

## Authorization layers, and how they differ

Three independent layers decide what a job may do; a job must pass all of
them, and each layer fails closed independently:

1. **Policy authorization (gateway).** Bearer auth, restricted tool surface,
   and for `write`/`autonomous` the trusted-dispatch policy
   (schema `aee-secure-mcp-trusted-dispatch-v1`): principal = SHA-256
   fingerprint of the dedicated gateway bearer, per-agent allowlist, checked
   before CLI probes, after probes, at worker start (TOCTOU revalidation) and
   immediately before execution. Malformed, absent or revoked policy ⇒ no
   write/autonomous admission; read-only and status/result/cancel are
   unaffected. See [trusted dispatch](mcp-runtime-autonomous.md).
2. **OS DAC/ACL.** The job's `working_directory` is canonicalized at
   admission and re-verified by device/inode identity immediately before
   launch (`INVALID_WORKING_DIRECTORY` when it changed). The worker runs
   under the service OS identity and inherits its actual DAC/ACL privileges
   — without escalation. os-native takes this layer as the primary boundary;
   restricted confines it further with the allowed-roots list.
3. **Agent-native enforcement.** The adapter CLI itself restricts tool use:
   Codex native sandbox (`read-only`, `workspace-write`, or approval bypass
   only in `autonomous`), Claude restricted safe mode with `Read/Glob/Grep`
   and empty strict MCP (`read_only`) or headless `acceptEdits` (`write`),
   Pi tool denylist (`read` only). An adapter with no verifiable enforcement
   for a mode (Hermes `read_only`, Pi write) is refused, never simulated.

Transport is not authorization: the Secure MCP Tunnel forwards packets and
carries the forwarded bearer; it grants nothing else. Plugin access grants no
host administration. All callers sharing the gateway bearer share its full
authority; per-user job ownership/OAuth/multi-tenant authorization are not
implemented.

## Agent Adapter Architecture

Executors are adapters (Codex, Claude Code, Pi, Hermes); none is a mandatory
default backend (work order `AEE_FUNCTIONAL_RECOVERY_A2_A3_MULTI_AGENT`).
The registry (`aee/mcp_runtime/job_adapters.py`) is the single bounded,
synchronous contract the dispatch thread uses when a job is not routed
through the approved Stage-2 Codex broker path:

- Admission resolves the binary: absolute override env
  `A3_ADAPTER_<NAME>_BIN` (uppercased agent name) first, then `PATH` lookup
  under the service identity's effective PATH. Failure is coded
  (`ADAPTER_NOT_INSTALLED`, `ADAPTER_UNSUPPORTED`); a relative or
  non-executable override fails closed (`scripts/audit_adapters.py` reports
  resolution facts, never secrets).
- Registry-only mode refusal (`refuse_unsupported`): an adapter that cannot
  enforce `read_only` or `write` honestly is refused whether or not its CLI
  happens to be installed. Registry support does **not** by itself prove an
  E2E job succeeded in that mode; see the
  [capability matrix](runtime-capability-matrix.md).
- The child runs with `cwd=<working_directory>` (pinned inode via
  `/proc/<pid>/fd`); the task always travels on **stdin**, never argv, so it
  never appears in `/proc/*/cmdline`.
- The child env strips every `AEE_*`, `A3_*` and `MCP_BRIDGE_*` key — the
  control plane never enters the worker's environment; provider credentials
  stay available only because the OS identity legitimately holds them.
- Executor identity (path + SHA-256) is recorded at admission and
  re-checked at dispatch (`EXECUTOR_IDENTITY_MISMATCH` on change).
- Capability probes (`--help` flag verification) run before use: Claude
  read-only flags, Pi JSON/isolation flags, headless autonomous flags per
  agent.

## Job lifecycle and observability

Lifecycle (`aee/mcp_runtime/store.py`): *queued → running →* one terminal of
{`completed`, `failed`, `timed_out`, `cancelled`}. Admission acquires a
kernel flock lease before publishing the queued record and holds it to the
terminal write; a concurrent second admission returns `BUSY`. There is one
active job slot per gateway, no queue, no retry: an interrupted job is not
automatically retried. Restart reconciliation releases lost-owner leases
without retrying jobs.

Records are bounded and private: ≤65,536-byte schema-validated atomic JSON,
task ≤8,000 chars, summary ≤4,000 chars, log excerpt ≤2,000 chars, store
directory `0700`. Accepted success requires the `aee-completed-v1`
native/broker completion proof at every boundary — a completed label alone is
not a successful result. Since the R3 revision an accepted Code Mode success
also records an `operation_attestation` grade, cross-matching the pinned
binary's machine-verified completion evidence; anything without it fails
closed (`REQUIRED_OPERATION_UNVERIFIABLE`).

Observability surfaces (all bounded, all bearer-authenticated unless noted):
the six-tool MCP set (`aee_status`, `aee_agents`, `aee_job_status`,
`aee_job_result`, `aee_job_cancel`), durable per-event streaming with a
spool for budgeted jobs (tool events recorded as evidence), and non-secret
audit fields: principal, transport, agent, mode, task digest, resolved
cwd/inode, OS identity, admission time, worker start/end. `journalctl` on the
gateway/broker units is an operator-only private check.

## Failure semantics

Failures are sticky and classified; they are never relabeled or recovered by
override (sandbox profile `tool_failures: sticky-fail-closed-no-recovery-override`):

- `exit_code=0` together with a `failed` status is **intentional** — the
  fail-shot contract: an agent ran a native command that exited nonzero and
  the job fails (`REQUIRED_TOOL_FAILED`) rather than silently treating the
  expected negative as success. Re-run with exit-zero-guarded task phrasing.
- Unsupported agent/mode and unresolved adapters fail **before admission**:
  `INVALID_AGENT`, `INVALID_MODE`, `ADAPTER_NOT_INSTALLED`,
  `ADAPTER_UNSUPPORTED` (Hermes cannot serve `read_only`; Pi cannot serve
  `write`).
- `EXECUTION_FAILED` for nonzero executor exit or no final agent message;
  `turn budget exhaustion` is its own code (`TURN_BUDGET_EXHAUSTED`).
- Output limits raise `OUTPUT_LIMIT_EXCEEDED` with bounded integer
  diagnostics naming the exact boundary/stage (no raw payloads retained).
- A missing/tampered completion proof, lease breach, identity mismatch or a
  deployment-gate closure (`DEPLOYMENT_GATE_CLOSED`) terminates the job or
  blocks admission; a failed service never justifies relaxing localhost
  binding, authentication, allowed roots, native identity or tool filtering.
- Emergency stop: atomically set the trusted policy `enabled: false` —
  new write/autonomous admissions and pending launches refuse immediately
  without a service restart; read-only and status/result/cancel remain
  available. Already-running processes continue to normal completion,
  budget expiry or `aee_job_cancel`. See [troubleshooting](troubleshooting.md).

## Trust-boundary invariants (retained from the reviewed architecture)

Agent discovery can report Hermes or Claude installed. This does not
authorize dispatch to them beyond what the mode check and trusted policy
admit. Unsupported modes, missing prerequisites and closed deployment gates
fail before admission; discovery of another runtime does not authorize its
use.

Gateway and broker (restricted profile) run as different non-login accounts:
the gateway can request a short-lived broker lease but never receives the
inference credential. On the broker path the agent sees only an admitted
read-only snapshot, a private home and a private inference relay; no host
credential directory or system-manager socket enters its namespace. On the
os-native adapter path the provider credential arrives via the OS identity's
own environment, as documented above.

Arbitrary local `aee_exec` is never exposed to ChatGPT: it launches
allowlisted argv without a shell and must be treated as a trusted local
operator capability.

Production startup and admission require native/companion identity, resource
and sandbox revisions, enforced inherited cgroup bounds, verified failure
semantics and an operator approval identifier. The fixture path without a
deployment manifest exists for isolated tests; it is not a production
installation path.