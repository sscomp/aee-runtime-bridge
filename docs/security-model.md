# AEE security model

## Trust boundaries

`127.0.0.1:8790/mcp` is local/full; `127.0.0.1:8791/mcp` is restricted remote-facing
through the private tunnel. Both are private loopback services. Candidate port 8791
forces the restricted surface, requires bearer auth, and rejects an explicitly empty
or expanded tool allowlist. Its fixed five-tool list/call guard excludes `aee_exec`,
including future registrations. The current published protocol and bootstrap
regressions enforce this boundary; publication is not a live service upgrade.

Secure MCP Tunnel is transport, not AEE authorization policy. Plugin access does not
grant unrestricted host access. Workspace/tunnel access, forwarded AEE bearer checks,
dispatch validation and the executor boundary each have separate responsibilities.
All callers with the shared gateway credential share its authority: per-user job
ownership/OAuth/multi-tenant authorization are not implemented here.

`/health` and `/status` bypass bearer checks and expose diagnostic host information.
Loopback/private binding matters; do not make them publicly reachable. Secrets and
sensitive source can still be disclosed through permitted task output; review the
snapshot/data allowed for remote use. Redaction is best effort, not a data-classification system.

## Execution authorization

Remote execution is Codex/read_only only. The candidate accepts only its named reviewed
profile and matching model/reasoning values, a reviewed workspace manifest and native
ELF executor identity. Task text is literal stdin, not shell interpolation or CLI flags.
Unsupported modes, missing prerequisites and closed deployment gates fail before admission.
Discovery of another runtime does not authorize its use.

There is no remote arbitrary-shell tool. The local `aee_exec` implementation launches
allowlisted argv without a shell, with timeout and output truncation. Its broad binary
allowlist includes interpreters/system tools, and its root check is lexical rather than
a complete symlink/sandbox boundary. It must be treated as trusted local operator
capability; do not claim it proves universal read-only execution or expose it remotely.
Candidate timeout/buffer controls for Codex are separate from this local helper.

Write/destructive execution is rejected, not gated by an implemented remote approval
workflow. Candidate Codex approval policy is `never` inside the constrained read-only
profile; it is not permission to perform writes. Production deployment, privileged host
changes and any future capability expansion require separate operator/reviewer approval.
ChatGPT confirmations cannot override these server restrictions.

## Paths, snapshots and resources (candidate implementation)

Dispatch canonicalizes cwd with realpath, checks an existing allowed directory and
rejects forbidden credential/config paths. This check alone is not race-proof file
containment. Snapshot construction opens the source and each descendant via directory
FDs with `O_NOFOLLOW`, rejects symlinks/hardlinked or nonregular files, validates digests,
and rejects absolute/traversal/credential paths. Only the reviewed snapshot becomes
read-only `/workspace`; host HOME and host credentials are absent. The admitted
job-specific broker socket is the sole host socket capability.

Bubblewrap isolates mounts/PID/network and private HOME/tmp/proc; the P2C broker gives
an expiring job-specific inference capability rather than host credentials. Snapshot
bounds are 10,000 files, 4 MiB/file, 64 MiB total. Codex timeout is at most 900 seconds;
default stdout/stderr caps are 65,536 bytes each, result 65,536 bytes, summary 4,000
characters and log excerpt 2,000. Limits validation and whole-process-group cleanup
fail closed. P2C resource/cgroup/provider contracts have additional qualified bounds;
see [resource policy](../config/p2c/resource-profile.json) and
[sandbox policy](../config/p2c/sandbox-profile.json). Host enforcement is independently
verified before production approval; portable fixtures are not host evidence.

Candidate job storage uses private permissions, bounded/schema-validated atomic
records, kernel leases for admission, lost-owner reconciliation without retry, and
validated terminal-result integrity. A completed label alone is not a successful
result. Baseline store concurrency/orphan limitations remain distinct.

## Credentials and repository safety

Separate OpenAI tunnel runtime key, restricted gateway forwarding key, local gateway
key and inference-provider credential. Never put administrative credentials in the
tunnel daemon. Keep private env/profile files 0600 and state/profile directories 0700.
Examples contain placeholders; current live values are intentionally not embedded.
Do not print/copy/archive private files, raw headers, tokens or support logs. The
accepted secret scanner reports only locations/categories, but pattern checks cannot
detect every opaque secret. Use the accepted pattern-only evidence scan here without
opening private env files; no credential rotation is authorized.

The live bootstrap runs from a different working tree. Do not alter its branch, files,
venv, units, bindings or store to test this candidate. Preserve original evidence.
P2C acceptance permits review of its qualified scope; Stage 2 remains STOPPED.
Merge/push/tag/release and Production rollout require later explicit authorization.
