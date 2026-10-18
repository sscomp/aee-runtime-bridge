# A2 Secure MCP — install, deploy and verification guide

Reproducible install and acceptance procedure for the **A2 Secure MCP trusted
dispatch** release (`repair/a2-secure-mcp-trusted-dispatch`, HEAD
`d90f248`). The generic host path — tooling, locked dependencies, immutable
release build, operator qualification and systemd installation — is
[host setup](deployment.md) §0–§4 and is **not duplicated here**. This guide
adds the A2-specific configuration surface and the pass/fail gates that prove
the trusted dispatch chain works before (and after) any ChatGPT connector is
pointed at it. For the connector/tunnel layer see the
[setup and refresh runbook](chatgpt-secure-mcp-setup-runbook.md); for the
trust model see [autonomous MCP runtime](mcp-runtime-autonomous.md).

All `<...>` values are private and host-specific: document the *names* below,
keep the values in the host's 0600 env/credential files, and never let them
appear in Git, tickets or screenshots.

## 1. Source-state gates (before any install)

Run all of these from a clean checkout of the release commit and keep the exit
codes as release evidence:

```bash
git status --porcelain --untracked-files=no
git rev-parse HEAD
PYTHONPATH=.:tests/mcp .venv/bin/python -m unittest discover -s tests/mcp -p 'test_*.py' -v
.venv/bin/python scripts/check-canonical-docs.py
.venv/bin/python scripts/scan-aee-baseline-secrets.py --repo .
```

| Gate | Expected |
|---|---|
| Clean worktree | empty output |
| `tests/mcp` regression suite | exit 0, `OK` (native-CLI probe tests may report `skipped 'reviewed installed native CLI unavailable'` — that is an explicit, expected skip, not a pass) |
| Canonical docs/config checker | `{"result": "PASS", ...}` and exit 0 |
| Baseline secret scanner | `{"result": "PASS", "findings": []}` and exit 0 |

The legacy `aee/tests` suite additionally exists but is
environment-dependent on this branch's baseline (host-path and host-detection
assertions fail identically on `main`); `tests/mcp` is the deployment gate
recorded by [host setup §2](deployment.md).

Stage-scoped scans before publishing or committing:

```bash
.venv/bin/python scripts/scan-aee-baseline-secrets.py --staged
.venv/bin/python scripts/scan-aee-baseline-secrets.py --history-range main..HEAD
```

## 2. Install and build (per host setup, with A2 additions)

1. Follow [host setup](deployment.md) §0–§3: tooling, locked dependencies,
   offline smoke, then the immutable release build:

```bash
.venv/bin/python -m aee.mcp_runtime.packaging build
.venv/bin/python -m aee.mcp_runtime.packaging verify
.venv/bin/python -m aee.mcp_runtime.packaging plan
```

2. Follow §4 for operator qualification and the `/opt/aee/...` layout,
   `config/p2c/` unit and env templates, and the `APPROVED_STAGE2` +
   `operator_approval_id` deployment gate.
3. **A2 addition:** provision the private trusted-dispatch policy file
   (schema `aee-secure-mcp-trusted-dispatch-v1`, mode `0600`, parent `0700`)
   and set `AEE_TRUSTED_DISPATCH_POLICY` in the restricted gateway env — see
   runbook §1–§2 for the exact shape. Do not configure legacy
   `AEE_AUTONOMOUS_*` / `AEE_WRITE_APPROVAL_TOKEN` / `AEE_WRITE_ALLOWED_ROOTS`
   gates on the same deployment: with the trusted policy present they are
   bypassed by design, and configuring both invites model confusion.

Restricted-surface env names (values stay local; template:
`config/p2c/gateway-restricted.env.example`):

| Name | Purpose |
|---|---|
| `AEE_MCP_HOST=127.0.0.1` / `AEE_MCP_PORT=8791` / `AEE_MCP_SURFACE=restricted` | loopback-only restricted bind (hard-checked) |
| `AEE_MCP_REQUIRE_AUTH=true` + `MCP_BRIDGE_API_KEY` | bearer; mandated when trusted dispatch is on |
| `AEE_MCP_EXPOSED_TOOLS` | must equal the six-tool contract or be unset |
| `AEE_TRUSTED_DISPATCH_POLICY` | path to the private policy JSON |
| `A3_JOB_STORE_DIR`, `A3_DISPATCH_ALLOWED_ROOTS`, `A3_WORKSPACE_MANIFEST` | job store and workspace snapshot roots |
| `AEE_RUNTIME_MANIFEST`, `AEE_BROKER_CONTROL` | production gate + broker control |

## 3. Startup contract (what the gateway refuses)

On the restricted surface the process fails closed at import unless:

- `AEE_MCP_REQUIRE_AUTH` is on and a bearer is configured;
- the bind host is loopback;
- `AEE_MCP_EXPOSED_TOOLS`, when explicitly configured, equals exactly
  `aee_status, aee_agents, aee_dispatch, aee_job_cancel, aee_job_status,
  aee_job_result`;
- with `AEE_TRUSTED_DISPATCH_POLICY` set: all of the above plus a readable,
  schema-valid policy file — an unreadable or malformed policy is a hard
  startup error, never a silent fallback to legacy authorization.

Verification probe:

```bash
systemctl start aee-p2c-gateway@restricted.service
systemctl --quiet show aee-p2c-gateway@restricted.service -p ActiveState
curl -fsS http://127.0.0.1:8791/health
curl -fsS http://127.0.0.1:8791/status
```

## 4. Acceptance gates

| # | Gate | Pass criterion |
|---|---|---|
| G1 | Source gates | everything in §1 returns the expected exit codes on this host |
| G2 | Health/status | `/health` and `/status` reachable; `/status` shows `trusted_dispatch.configured: true`, `valid: true`, `per_job_token_required: false`, and `autonomous.authorization: secure_mcp_trusted_dispatch` |
| G3 | Surface contract | MCP discovery on the restricted surface exposes exactly the six tools; `aee_dispatch.mode` publishes `read_only \| write \| autonomous` |
| G4 | Auth negatives | wrong bearer → 401; forged forwarded headers never establish trust; non-loopback or unauthenticated surface refuses with the trusted policy configured |
| G5 | Trust admission | approved principal can dispatch `read_only` and, per policy, `write`/`autonomous` without any per-job token; job metadata carries the authorization proof (principal, agent, mode, canonical working directory) |
| G6 | Refusal path | principal absent/revoked, unapproved agent, or kill-switch policy → elevated dispatch refused; read-only behavior unchanged |
| G7 | Canonical workspace | trusted native write/autonomous jobs execute pinned to the proof's canonical workspace root (path and directory inode identity), not a `/proc` alias |
| G8 | Packaging gate | `packaging verify` passes and the deployment gate stays closed without `APPROVED_STAGE2` operator approval |

G2–G7 are exercised automatically by the `tests/mcp` suite (§1); on-host they
are re-verified manually only after config changes — the
[runbook §3](chatgpt-secure-mcp-setup-runbook.md) gives the operator sequence.
Gate G8 must never be bypassed; `DEPLOYMENT_GATE_CLOSED` is the designed
refusal, handled in [troubleshooting](troubleshooting.md).

## 5. Post-deploy record

Archive locally (not in Git): commit SHA, exit codes of §1, `/status`
field-name snapshot, gate table with PASS/FAIL per G1–G8, and the operator
approval ID used for §2.3. Keep every recorded artifact free of bearer
values, digests, tunnel IDs and job spool contents.