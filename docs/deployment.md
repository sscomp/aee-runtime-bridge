# Canonical MCP host setup

This is the single recommended AEE v2 MCP installation sequence. It uses the
existing offline `aee.mcp_runtime.packaging` entrypoint and supplied systemd units.
`install.sh` remains the compatibility HTTP/profile installer. No source checkout,
package build or CI result constitutes permission to deploy a host.

The sequence has five tiers; stop at the tier your authorization allows:

- **§0–2 Local validation** (no root, no credentials) — proves the repository
  installs and the MCP protocol surface is intact. **Verified working on a fresh
  host with these exact commands** (see §5 evidence note).
- **§2c Broker-qualified local dispatch** (no root, needs an OpenAI API key
  provisioned by the operator) — real read-only Codex jobs on a running gateway.
  **Verified working on a fresh host.**
- **§3–4 Production deployment** (root) — immutable release layout, system
  accounts, pinned binaries, operator review gate. Adds native telemetry receipts,
  cgroup containment checks and broker corroboration on top of §2c.

## 0. Host tooling (user-level, reversible, no root)

```bash
command -v git curl gcc prlimit bwrap || true
# Missing uv? Install it user-level; it manages its own CPython 3.13:
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv --version
```

`uv venv --python 3.13` fetches and runs a uv-managed CPython 3.13 when the host
Python differs — no system Python change is needed. Rollback: delete `~/.local/bin/uv*`
and `~/.local/share/uv`.

## 1. Inspect the host and choose a reviewed commit

Use a fresh Linux x86_64 host with systemd/cgroup v2, CPython 3.13.x, `uv`, Git,
GCC, `prlimit`, and rootless Bubblewrap. Read these checks before changing anything:

```bash
uname -sm
cat /etc/os-release
python3.13 --version
command -v uv git gcc prlimit bwrap systemctl systemd-analyze
bwrap --version
stat -fc %T /sys/fs/cgroup
ss -ltn '( sport = :8790 or sport = :8791 or sport = :8080 )'
systemctl list-unit-files 'aee*'
```

Stop if a dependency is missing, an existing installation is present, or ports
are occupied. Obtain operator direction for host package provisioning; do not
replace another installation. The production sandbox pins Bubblewrap 0.12.0 at
`/usr/bin/bwrap` and its SHA in `config/p2c/sandbox-profile.json`; a distribution's
other build may run offline fixtures but cannot pass production admission.

Clone using the README command. Until this candidate is merged, explicitly
checkout `feat/stage2c-canonical-v2`. The operator then chooses a reviewed commit;
record `git rev-parse HEAD`, checkout that commit, and require a clean worktree.

## 2. Install locked dependencies and run offline smoke

Run the README's `uv venv`, `uv pip sync --require-hashes`, and
`test_p2b_protocol` commands from the checkout. If Python/uv is unavailable, stop
and provision it rather than switching runtime versions. Re-running `uv pip sync`
converges to the same closure. The `.venv` is ignored and never packaged.

The smoke uses synthetic credentials, an in-memory ASGI MCP session and a compiled
C fixture. It checks handshake, exact tools, authentication and safe job lifecycle.
It does not bind a live port, contact OpenAI or approve production inference.
Run all required controlled runtime tests with:

```bash
PYTHONPATH=.:tests/mcp .venv/bin/python -m unittest discover -s tests/mcp -p 'test_*.py' -v
.venv/bin/python scripts/check-canonical-docs.py
```

From a clean committed checkout, `python scripts/smoke-canonical-package.py`
also builds/verifies/plans the actual repository using a synthetic ELF and keeps
the deployment gate closed. It requires temporary space; do not delete another
operator's `/tmp` data to satisfy it.

Native qualification tests report explicit skips without operator-provided native
paths. They are documented in [runtime tests](../tests/mcp/README.md).

## 2b. Local gateway run (no root; documented non-production run mode)

The gateway is env-driven and runs from the checkout as an unprivileged user. This
mode exercises the real HTTP MCP server without any production approval; it emits
`0.2.0-p2c-candidate` health data and must not be represented as a production
deployment.

1. Provision a private env file (mode 0600, **outside** the checkout) from
   `config/p2c/gateway-restricted.env.example`, replacing every `<...>` value.
   Key minimum for a gateway-only run: `MCP_BRIDGE_API_KEY` (generate with
   `python -c 'import secrets; print(secrets.token_urlsafe(32))'`), the
   `AEE_MCP_*` block verbatim from the example, and `A3_JOB_STORE_DIR` pointing
   outside the checkout (job state must not dirty the tree).
2. Generate the dispatch workspace manifest for your chosen allowed root with the
   packager's own generator (any path→sha256 map of the working tree works):
   `aee.mcp_runtime.packaging.make_manifest(root, git_commit)`.
3. Run it under a dedicated user service (distinct unit name, e.g.
   `aee-v2-local-gateway`): `ExecStart=.../.venv/bin/python mcp_gateway.py` with
   that EnvironmentFile; do not enable it at boot until the operator decides.
4. Verify with [agent operations](agent-operations.md): `/health`, MCP
   `initialize`, `tools/list` (exactly five tools), auth negatives.

Without the broker, dispatch **admits and enforces** jobs but a real Codex run
fails `OUTPUT_LIMIT_EXCEEDED` (SIGXFSZ): codex's own session-state writes exceed
the default `Limits.file_bytes` of 64 KiB, which only the broker-relayed path
([§2c](#2c-broker-qualified-local-dispatch-no-root-needed)) raises to 4 MiB. Treat a
queued-but-failed job as **not** completed dispatch.

## 2c. Broker-qualified local dispatch (no root needed)

Real dispatch E2E requires the inference broker: it holds the model credential in
a private file and hands each job a per-job UDS to the fixed OpenAI Responses
endpoint. Same host, no root:

1. **Operator provisions the credential** (never via chat/reports/commits): place
   the OpenAI API key in a directory-restricted path, mode 0600, e.g.
   `~/.private/aee/openai-api-key`. Record the path only.
2. Start the broker with a private socket directory and the credential:
   `.venv/bin/python -m aee.mcp_runtime.broker --directory <private-socket-dir>
   --gateway-uid <your-numeric-uid> --credential-file <credential-path>`
   under its own user unit (distinct name, e.g. `aee-v2-local-broker`),
   `Restart=on-failure`, bounded `MemoryMax`.
3. Extend the gateway env with `AEE_BROKER_CONTROL=<socket-dir>/control.sock` and
   restart only the gateway unit.
4. Dispatch a bounded read-only job per [agent operations](agent-operations.md).
   With the broker socket present, job tempfs/file limits are relaxed to the
   measured codex runtime needs (4 MiB RLIMIT_FSIZE) and the job can complete.
5. Keep both units *not enabled at boot* unless the operator decides otherwise;
   stop/disable for full rollback. This mode is **local-qualified dispatch, not
   production**: no operator telemetry, no native-receipt corroboration, no
   cgroup containment proof (§3–4 production adds those).

## 5. Host evidence notes (provenance)

When deploying pinned binaries, always re-hash after download and record the
source URL and attestations with the deployment evidence. The `provider_contract.py`
native Codex/companion digests correspond to the official
`openai/codex` release `rust-v0.159.2` musl artifacts (verify with `sha256sum`
after extraction; the release publishes sigstore attestations for each asset).
`config/p2c/sandbox-profile.json` additionally pins an exact Bubblewrap `0.12.0`
binary digest whose upstream provenance is **not documented in this repository**;
`bwrap-x86_64-unknown-linux-musl` shipped with `openai/codex` releases hashes
differently, so production sandbox admission requires the operator to supply the
exact artifact recorded at review time (or a re-pinned policy after an explicit
review).

## 3. Build, verify and plan an immutable release

Obtain the **native ELF** Codex 0.159.2 and adjacent `codex-code-mode-host` from an
operator-reviewed distribution. `provider_contract.py` pins both digests and the
version; npm shim scripts, different builds and unreviewed upgrades are rejected.
The deployment plan must stay outside the checkout. Set these local variables:

```bash
: "${AEE_NATIVE_CODEX:?Set absolute path to reviewed native Codex ELF}"
AEE_SOURCE_COMMIT=$(git rev-parse HEAD)
AEE_BUNDLE_PARENT=$(mktemp -d /tmp/aee-release.XXXXXX)
AEE_BUNDLE="$AEE_BUNDLE_PARENT/bundle"
.venv/bin/python -m aee.mcp_runtime.packaging build \
  --workspace "$PWD" --destination "$AEE_BUNDLE" --codex "$AEE_NATIVE_CODEX"
.venv/bin/python -m aee.mcp_runtime.packaging verify \
  --directory "$AEE_BUNDLE" --workspace "$PWD" --source-commit "$AEE_SOURCE_COMMIT"
.venv/bin/python -m aee.mcp_runtime.packaging plan \
  --directory "$AEE_BUNDLE" --runtime-root "/opt/aee/releases/$AEE_SOURCE_COMMIT" \
  > "$AEE_BUNDLE_PARENT/deployment-plan.json"
```

Build refuses a dirty checkout, existing output, protected destination, symlink,
unsafe source or invalid metadata. It writes only a new private `/tmp` path.
Never edit a bundle in place or rebuild over an existing release. Its initial
manifest is `STAGE1_NOT_DEPLOYABLE`, with unverified host/tool/failure fields.
`plan` emits data only; **it does not install or authorize anything**.

## 4. Operator qualification and installation

This step needs explicit host authorization and an independent security review.
There is no automatic fresh-host qualification/approval generator in this release.
The reviewer must validate native provider/tool behavior, sticky tool failures,
completed-result proof, namespace isolation, broker credentials and enforced
cgroup v2 limits on the target host. Fixture patches cannot be reused as evidence.
Keep the resulting approval and host evidence private. If any check is unavailable,
**stop at `DEPLOYMENT_GATE_CLOSED`**; never omit `AEE_RUNTIME_MANIFEST` to proceed.

Only after that review, provision the documented layout:

| Path | Owner and purpose |
|---|---|
| `/opt/aee/releases/<commit>/` | Root-owned immutable bundle (`source/`, `runner/`, workspace manifest) |
| `/opt/aee/current` | Operator-switched release pointer; no partial overwrite |
| `/opt/aee/venvs/mcp-py313` | External CPython 3.13 venv, installed from the release's hashed lock |
| `/etc/aee/gateway-restricted.env` | Root-owned 0600; rendered restricted environment template |
| `/etc/aee/broker.env` | Root-owned 0600; dedicated gateway account's numeric UID |
| `/etc/aee/secrets/openai-api-key` | Root-owned 0600; broker `LoadCredential` source only |
| `/var/lib/aee/jobs` | Private gateway-owned 0700 job store; files 0600 |
| `/run/aee-broker` | Broker-managed socket directory, group `aee-inference` |

Create non-login `aee-gateway` and `aee-broker` accounts plus `aee-inference` group.
Gateway group membership and broker UID must match the supplied units. The offline metadata contract binds every node to the builder's effective UID/GID;
it does not require a developer account numbered 1000. Verify that original bundle
before the separately authorized root-owned installation. Preserve
immutable bundle modes (0555 executable/directories, 0444 data) and root ownership.
Render the emitted runtime plan into the release's deployment manifest using the
actual paths and independently verified review result, retaining the full
inventory. A reviewed manifest needs `APPROVED_STAGE2`, `operator_approval_id`,
verified native/tool/resource/failure fields and exact provider/resource/sandbox
contracts. Changing a label alone is not qualification; startup still verifies
identity, Bubblewrap and inherited cgroups. Keep the approval record separately.

Copy `config/p2c/gateway-restricted.env.example` and `broker.env.example` to their
private paths. Replace every `<...>` value, including the immutable native path,
numeric UID and dedicated MCP bearer credential. Do not `source` these examples:
they are systemd EnvironmentFile templates. Install the external venv using the
same hash-locked sync, with its Python executable matching the units. Install the
three unit files from `config/p2c/systemd/` after `systemd-analyze verify`; leave
the optional tunnel drop-in uninstalled until a separately reviewed tunnel unit
exists. Do not install/start the local/full gateway unless locally required.

The authorized operator may then run:

```bash
sudo systemctl daemon-reload
sudo systemctl start aee-p2c-broker.service aee-p2c-gateway@restricted.service
sudo systemctl is-active aee-p2c-broker.service aee-p2c-gateway@restricted.service
curl --fail --silent --output /dev/null http://127.0.0.1:8791/health
```

These commands are planned target-host steps, **not executed by publication CI**.
An auth/config/gate failure is a stop condition. Validate MCP using
[agent operations](agent-operations.md), then connect [ChatGPT](chatgpt-mcp.md).
See [rollback](troubleshooting.md) before enabling services at boot.
