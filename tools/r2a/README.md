# R2-A retirement candidates — review only

Status: DRAFT. No deployment, publication, retirement, Owner release or Operator approval.

This matched candidate set derives from the CR-01–CR-03 review. The source hashes
are recorded in `docs/r2a-mcp-tunnel-remediation-review.md`. The launcher is unchanged;
the retirement candidate and publisher are portable revisions, with synthetic tests.
Do not reuse old approvals or interpret a passing test as release authorization.

## Configuration and approval protocol

There are no live process, user, profile or endpoint defaults. The candidate reads
only `/etc/aee-r2a-runtime.json`, through bounded, root-owned, non-symlink directory
FDs and a root-owned regular file without group/other write permission. It rejects
missing/extra fields, root/boolean executor UID, duplicate/nonpositive process IDs,
invalid ticks, inconsistent parent relationships, relative/parent paths and invalid
or duplicate ports. Validate the actual configuration independently; JSON validation
does not prove a PID or executable belongs to the intended owner.

`runtime.example.json` contains fictional identities and nonexistent paths. It has
no credential, public-key registry, signed envelope or approval. Do not install it.
Actual configuration, host identities, runtime key, trust registry and signed
materials must stay outside source control. Restrict read access to authorized
operators while retaining root ownership and no group/other write permissions.
The designated non-root executor must be able to read required protected inputs.
The two Supervisor program names remain `aee-v2-gateway` and `aee-v2-tunnel-client`;
this candidate supports that supervised topology, not arbitrary service layouts.

The action protocol is **V4** (`aee-r2a-dual-approval-v4`, domain
`AEE-R2A-RETIRE-V4|`). It adds `runtime_config_sha256` to the signed scope. Hash the
exact protected configuration bytes, including whitespace. Each authorization
refresh rejects a changed configuration before consulting trust or approvals.
Candidate, launcher, host boot, target identities and formal service identities
remain bound to distinct Ed25519 Owner and Operator signatures. V2/V3 action
approvals cannot authorize this version. The public-key registry schema remains
`aee-r2a-dual-approval-trust-v2`; it describes keys, not the action protocol.

The fixed root-protected registry is `/etc/aee-r2a-approval-trust.json`. The fixed
preprovisioned lock is `/run/aee-r2a-apply.lock`. No tool here creates either.
Owner release, no active turn/UI dependency, shared profile shutdown consent and
the Operator window must be established independently. Test seeds are deliberately
public fixtures, used only in memory; never provision their keys as trust anchors.

## Preserved boundaries and publication review

Only pidfd SIGTERM is supported. There is no kill, SIGKILL or restart fallback.
PID/start-tick/executable/role/UID/ancestry and exact descendant checks remain.
Supervisor comparisons retain only Program/State/PID, ignoring volatile uptime.
The strict signal deadline and separate observation deadline remain independent;
observation requires 120 monotonic seconds, at least 20 samples and a 150-second
upper bound. Simulated observations are not evidence of real retirement.

Publisher sources must be regular, non-symlink, mode 0600 files owned by the
explicit `--source-uid` (positive, non-root). The Operator must independently verify
that UID, both review hashes and the publisher bootstrap. Git checkout modes are
not publication-ready source permissions. The dry-run validates two bounded FD
snapshots with before/after stat and hash equality. Root publication is a separate,
unperformed operation into `/opt/aee-r2a/releases/<candidate-sha256>` using directory
FDs, root protection, exclusive creation, readback, fsync and READY written last.
Existing or incomplete releases are never overwritten or repaired automatically.
The launcher verifies both hashes/READY and hashes, compiles and executes the same
candidate bytes. Candidate changes require a new release hash, review and signatures.

Root must freeze the entire release path, interpreter, runtime imports, registry,
configuration and lock inode throughout execution/observation. A cooperative lock
does not stop root from replacing its inode. Initial launcher loading still relies
on that root freeze; its self-check cannot prove which bytes Python initially
loaded against malicious concurrent root writes. No OS attestation, hostile-root
protection, atomic final userspace-check/syscall cutoff or hard realtime guarantee
is claimed. Trust the system clock; reserve margin before the signal cutoff.
Inherited `/proc/stat` parsing assumes ordinary process comm names without embedded
whitespace; unusual names can fail closed and require separate parser hardening.

## Local verification

Use system Python 3.11 with `-I -B`, and an installed `cryptography` distribution.
From this directory (no service changes):

```sh
/usr/bin/python3.11 -I -B test_final_candidate_no_signals.py -v
/usr/bin/python3.11 -I -B r2a_retire_final_candidate.py --dry-run
```

Tests use only synthetic identities, local temporary files, fake clocks and mocked
signal boundaries; an audit hook denies actual kill/killpg/pidfd signals. The
candidate dry-run additionally denies writes and restricts subprocesses to the
configured Supervisor status query. Without a provisioned configuration the direct
entry exits 2, emits `BLOCKED_OR_ABORTED`, and sends zero signals. Exit 3 means
technical checks passed but approval/release/lock gates remain blocked. Exit 0 is
not expected on an unprovisioned review checkout.

A private in-memory configuration adapter was used for a read-only mechanical
check of the revised bytes on the originating host. It substituted only the
unprovisioned configuration read and retained real approval/release/lock failures.
It is not shipped, is not a trusted launch path and cannot establish protected
configuration or formal-entry acceptance. Local original/revised checks passed,
with exit 3 and zero signals. Raw outputs remain private because they contain
host process identities. See `verification.json` for the sanitized evidence.
The existing repository CI does not invoke this new suite; CI coverage of these
candidates is NOT TESTED unless a later explicit run is recorded.

## Non-executable release prerequisites

After human code review, separately authorized root administration must review
and freeze the publisher bootstrap, source UID, exact snapshots, published root
release/READY, private runtime configuration, independent trust registry and stable
lock. No command here authorizes those writes. Then obtain fresh V4 dual signatures,
Owner release and a change window; use an independent management terminal and
protected launcher for a fresh dry-run. Only a separately authorized later change
may reach the signal boundary. Stop if any gate fails.

Rollback is manual and separately authorized. Restarting the shared old profile
can restore the competing consumer, cannot restore in-memory turns, and is not
permitted by a retirement signature. Do not copy profile/session databases or
credentials into a rollback package. There is no runnable rollback helper here.

Visible local consumer inventory does not establish global tunnel exclusivity.
Remote MCP discovery, each of aee_agents/aee_status repeated ten times, auth
negative checks and trusted control-plane inventory are NOT TESTED in this work.
A2 G1–G8 and real Dispatch E2E remain separate and unaccepted.
