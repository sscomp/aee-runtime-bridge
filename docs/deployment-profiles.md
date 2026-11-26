# Deployment profiles: os-native and restricted

AEE v2 supports two deployment profiles with the same canonical source, the
same six-tool MCP surface and the same fail-closed authorization defaults.
They differ in the OS identity that runs the gateway and in how the
execution boundary is expressed. Choose one profile per gateway install; do
not mix their artifacts on one host.

| | **os-native** (default) | **restricted** (explicit opt-in) |
| --- | --- | --- |
| Service identity | an existing OS user (systemd user unit) | dedicated system accounts (`aee-gateway`, `aee-broker`) |
| Unit scope | `~/.config/systemd/user/aee-gateway.service` | `/etc/systemd/system/aee-p2c-gateway@.service` (staged only) |
| systemd hardening | `NoNewPrivileges=yes`, resource accounting | `ProtectHome=yes`, `ProtectSystem=strict`, full sandbox profile |
| Dispatch cwd model | OS-identity DAC/ACL check; allowlist optional, off by default | mandatory `A3_DISPATCH_ALLOWED_ROOTS` workspace confinement |
| Broker control socket | not used | `AEE_BROKER_CONTROL` fixed at `/run/aee-broker/control.sock` |
| Manifests | not required (`AEE_RUNTIME_MANIFEST` must stay unset) | `A3_WORKSPACE_MANIFEST`, `AEE_RUNTIME_MANIFEST` required |
| Write/autonomous dispatch | trusted-dispatch policy (see below) | trusted-dispatch policy + broker approval chain |
| Profiles never mixed | restricted artifacts (manifests, broker socket, confinement list) are forbidden defaults here | requires every restricted key |

Boundary invariants that both profiles pin (`config/profiles/profile-schema.json`,
schema `aee-deployment-profile-schema-v1`):

- `AEE_MCP_HOST=127.0.0.1` loopback only, `AEE_MCP_REQUIRE_AUTH=true`
- `AEE_MCP_SURFACE=restricted` six-tool surface: `aee_status`, `aee_agents`,
  `aee_dispatch`, `aee_job_cancel`, `aee_job_status`, `aee_job_result`
- `MCP_BRIDGE_API_KEY` is operator-provisioned in a 0600 env file; shipped
  artifacts carry `<...>` placeholders only and real values never enter any
  repository artifact, receipt, log or diff

## Identity model and residual risk (os-native)

os-native deliberately runs the gateway under an OS identity that already
owns the agent toolchain (the identity a human or automation would use).
The access boundary is the OS itself — DAC permissions, ACLs, and each
adapter's own technical sandbox (Claude tool allowlist, Codex native
sandbox, Pi tool denylist). Consequences, stated plainly:

- There is **no privilege separation between the gateway and its adapter
  workers**: they share the UID by design. Protect the gateway's own state
  (env file 0600, job store 0700, trusted policy 0600 in a 0700 directory).
- Any process able to read the gateway env can use the bearer key. Same-UID
  compromise means full compromise — this is the accepted residual risk of a
  single-identity install, not a hidden control.
- The legacy per-job write gates (`AEE_WRITE_APPROVAL_TOKEN`,
  `AEE_WRITE_ALLOWED_ROOTS`) remain available for non-autonomous callers;
  when a trusted-dispatch policy is configured and valid, it supersedes them
  for write dispatch (per-principal fingerprints, per-host, fail-closed,
  with worker-start revalidation against TOCTOU).

## Env contract and validation

Every env file is validated against an entry in
`config/profiles/profile-schema.json` before any installation:

```bash
# shipped artifacts stay placeholder-clean (CI also runs this)
python3 scripts/validate_profiles.py

# an operator env on the target host (real values required for a given profile)
python3 scripts/validate_profiles.py --env-path /etc/aee/gw.env --profile os-native
```

Key semantics: fixed boundary keys (`AEE_MCP_HOST`, `AEE_MCP_REQUIRE_AUTH`,
`AEE_MCP_SURFACE`, `AEE_DEPLOYMENT_PROFILE`, `AEE_BROKER_CONTROL` on
restricted) must render their documented values; `<...>` placeholders are
**required** in shipped/staged artifacts and **rejected** in an operator env
for *every* required key (not only secret keys) — a placeholder anywhere in a
deployed env means the operator skipped provisioning, fail before write.
Values are parsed with systemd EnvironmentFile semantics (`export ` prefixes
ignored, one wrapping quote level removed, inline `#` literal, last key
wins), so a quoted placeholder cannot hide from these checks.

Fresh hosts on either profile must explicitly provision `AEE_JOB_ID_PREFIX`
and pass [Fresh Install Smoke](fresh-host-bootstrap.md#fresh-install-smoke)
before the first dispatch. Legacy env files can omit this key and retain the
`A3JOB` default.

## Adapter provisioning (`A3_ADAPTER_<NAME>_BIN`)

Adapter discovery per agent (`claude`, `codex`, `hermes`, `pi`) resolves, in
order:

1. `A3_ADAPTER_<NAME>_BIN` — absolute path override in the gateway env
   (uppercase agent name, e.g. `A3_ADAPTER_CLAUDE_BIN=/usr/local/bin/claude`).
   A relative or missing-path value fails closed.
2. `PATH` lookup for the agent's default executable name.

Resolution failure is fail-closed with a coded reason
(`ADAPTER_NOT_INSTALLED`, `ADAPTER_UNSUPPORTED`); unsupported profiles never
silently downgrade (Hermes cannot serve `read_only` dispatch). Each adapter
keeps its mode enforcement regardless of how the binary was found. Audit the
installed host without touching it:

```bash
python3 scripts/audit_adapters.py                      # JSON report, exit 0
python3 scripts/audit_adapters.py --strict --expect claude,codex
```

The report contains resolution facts only; it never prints secret values.

## Trusted dispatch onboarding (write/autonomous)

The optional `AEE_DEFAULT_DISPATCH_MODE` environment variable selects the mode
used when an MCP caller omits `mode` (`read_only` when unset). Supported values
are `read_only`, `write`, and `autonomous`; an invalid value refuses startup.
Selecting `autonomous` does not grant authority: provision the trusted policy
below before enabling that default. Explicit per-job modes retain their native
boundaries. `aee_status`, `/status`, and the MCP tool schema report the configured
default. A connector that caches tool schemas may need its tools refreshed.

Write and autonomous dispatch are enabled only through
`scripts/provision_trusted_dispatch.py`, which writes a
`aee-secure-mcp-trusted-dispatch-v1` policy and re-validates it against the
live runtime parser before declaring success:

```bash
printf 'OPERATOR_TOKEN' | python3 scripts/provision_trusted_dispatch.py \
  --policy-path /etc/aee/trusted-dispatch-policy.json \
  --principal operator-laptop \
  --key-stdin \
  --set-agents claude,codex \
  --enable-write-autonomous
```

- The bearer is hashed to a SHA-256 fingerprint; only fingerprints are
  written to the policy, echoed to output or stored anywhere.
- The runtime key comes from `--key-env <VARNAME>` or `--key-stdin`, never
  argv; a too-short key is refused.
- The gate is explicit opt-in: the policy's `enabled` flag is set to true
  ONLY by `--enable-write-autonomous`. On later runs a previously enabled
  policy stays enabled (an explicit opt-in is, by design, preserved) unless
  `--disable-write-autonomous` revokes it; passing both flags at once is
  refused. It is never enabled implicitly.
- If the policy file already exists but is corrupt or carries an unknown
  schema, provisioning refuses (that policy fails closed at runtime too);
  a deliberate replacement requires `--overwrite-invalid`.
- Policy file mode 0600, parent 0700, atomic replace; `--revoke <fp>` removes
  one principal.

Point the gateway env at it with `AEE_TRUSTED_DISPATCH_POLICY=<path>`. An
absent, unreadable or invalid policy makes `TrustedDispatch.configured()`
return unconfigured — write/autonomous admission then fails closed instead
of running on a degraded policy.

## Installation and rollback

### Hermes builds without stdin or JSONL CLI flags

For an installed Hermes build that exposes `hermes_cli.oneshot.run_oneshot`,
the optional `deploy/hermes-aee` wrapper provides the gateway's stdin/JSONL
contract. Set `A3_ADAPTER_HERMES_BIN` to its absolute path and keep the
`hermes-aee` filename in the configured path. The gateway service's `PATH`
must resolve `python3` to the interpreter with Hermes installed. The help probe
checks module discovery without loading the Hermes profile. A qualified native
`hermes` CLI can be selected through the same setting or normal PATH discovery
and uses `--toolsets hermes-cli` along with its stdin/JSONL flags.

The wrapper uses actual Hermes tool callbacks and the native completed run
record. It refuses partial runs, failed tools and incomplete callbacks.
The worker pins `TERMINAL_CWD` to the job's launch directory and requires a local
terminal backend; it does not modify shared Hermes configuration. This is an
autonomous-only integration: constrained `read_only` and `write` mappings remain
unsupported. A successful help probe verifies CLI discovery and flag shape.
Provision `hermes` in the trusted agent policy after the operator qualifies
the installed Hermes build, provider, and local terminal backend on the host.

### Gateway installer

The installer (`scripts/install_aee_gateway.py`, wrapper
`scripts/install-aee-gateway.sh`) never acquires privileges: the os-native
profile writes a user unit, the restricted profile **stages the canonical
P2C unit template verbatim** (`config/p2c/systemd/aee-p2c-gateway@.service`
— a fully static file, no installer substitutions) next to the
`UNINSTALLED` release tree, prints the complete operator sudo sequence, and
executes none of it. Backups cover installer-managed unit files only — the
operator env is never copied or archived by the installer. Preflight runs
fail-before-write (env schema, 0600 mode, `--port` vs env `AEE_MCP_PORT`
cross-check, port bind probe, disk headroom,
unmanaged-collision refusal verified by sidecar marker **and** recorded unit
hash), apply is an idempotent transaction (byte-identical unit and sidecar
rewrite nothing; only files that actually change are backed up — mode drift
is corrected even when bytes are identical; the sidecar marker exists in the
staging dir too, so restricted re-runs are no-ops, not refusals), rollback
restores only profile-matched, sha-verified, provenance-listed unit paths
from `BACKUP-META.json`, and receipts
record env var *names*, never values.

```bash
# inspect (no writes)
scripts/install-aee-gateway.sh --profile os-native --check \
  --release-dir /opt/aee/current --env-path /etc/aee/gw.env

# install the os-native user unit (enable/start remain explicit opt-ins)
scripts/install-aee-gateway.sh --profile os-native --apply \
  --release-dir /opt/aee/current --env-path /etc/aee/gw.env \
  --receipt /etc/aee/install-receipt.json

# restricted profile: stage system artifacts; operator runs the printed commands
scripts/install-aee-gateway.sh --profile restricted --apply \
  --release-dir /opt/aee/current --env-path /etc/aee/gateway-restricted.env \
  --staging-dir /tmp/aee-restricted-staging --unit-mode system

# After the staging step the OPERATOR (not the installer) runs the printed
# sequence, including placing the env at the path the template expects:
#   sudo install -o root -g root -m 0644 \
#     <staging>/aee-p2c-gateway@.service /etc/systemd/system/aee-p2c-gateway@.service
#   sudo install -o root -g root -m 0600 /etc/aee/gateway-restricted.env \
#     /etc/aee/gateway-restricted.env
#   sudo systemctl daemon-reload && sudo systemctl enable --now aee-p2c-gateway@restricted.service
# Broker units, the runtime slice, dedicated accounts, manifests and the
# deployment gate are prerequisites — see docs/deployment.md §3–4; production
# admission for that line remains gated by its runtime manifest
# (STAGE1_NOT_DEPLOYABLE until the evidence chain completes).

# revert the last transaction, or remove the unit (operator data is never touched)
scripts/install-aee-gateway.sh --profile os-native --rollback-last \
  --backup-dir ~/.local/state/aee-installer/backups
scripts/install-aee-gateway.sh --profile os-native --uninstall
```

Cold boot: an os-native user unit starts at login/graphical session and
survives reboot **only** with user lingering
(`loginctl enable-linger <user>`); the installer reports lingering state and
never enables it. Restricted system units enable on the operator's explicit
commands. SSH is a debug transport for remote administration only — it is
never an AEE runtime or installer dependency, and nothing in the runtime
path may call SSH.

## Fresh hosts

For the full brand-new host sequence — including where profile selection
fits relative to tooling, provenance and production admission — see
[fresh-host bootstrap](fresh-host-bootstrap.md); for the restricted profile's
system-account and sandbox qualification details see
[canonical MCP host setup](deployment.md) and the
[security model](security-model.md).

When the [durable job notifier](job-telegram-notifications.md) starts, it owns
terminal delivery for that gateway process. The legacy terminal hook remains
available for hosts without a running durable notifier. Its opt-in settings
belong in the private gateway service environment. The default is off.

```dotenv
# Optional settings for the private gateway service environment.
# AEE_DEFAULT_DISPATCH_MODE=read_only
# A3_ADAPTER_HERMES_BIN=/absolute/path/to/deploy/hermes-aee
# AEE_TELEGRAM_TERMINAL_NOTIFY=0
# AEE_TERMINAL_NOTIFY_TARGET=telegram:<chat_id>
# AEE_TERMINAL_NOTIFY_COMMAND=hermes
```

An enabled legacy hook requires a recipient. The configured messaging CLI
resolves its own credentials and receives the bounded notice on stdin. An
unavailable sender cannot change a terminal job state. Verification uses the
shared notification suppression guard.
