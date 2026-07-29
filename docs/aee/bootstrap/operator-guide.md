# AEE Bootstrap v1 — Operator Guide

> **Audience:** Operators deploying AEE on production or development hosts.
> **Spec reference:** `reports/aee_bootstrap_v1_spec.md` (§1–§20)
> **Status:** Living document — updated as bootstrap v1 ships.

---

## 1. Quick Start

### 1.1 POSIX (Ubuntu / Debian / macOS)

```bash
# Default profile (mini on Linux containers, developer on macOS)
curl -fsSL https://example.com/aee-install | bash

# Specific profile
curl -fsSL https://example.com/aee-install | bash -s -- --profile full

# Non-interactive
curl -fsSL https://example.com/aee-install | bash -s -- --profile mini --yes
```

### 1.2 Windows (PowerShell, experimental)

```powershell
# Default profile
irm https://example.com/aee-install.ps1 | iex

# Specific profile
& ([scriptblock]::Create((irm https://example.com/aee-install.ps1))) -Profile developer
```

> Windows support is **experimental** until the Windows adapter (§13.4)
> ships first-class detection. The bootstrap runs in UNKNOWN capability
> mode on Windows today.

---

## 2. Profiles

| Profile | Target | DB | Hardening | Supervisor |
|---------|--------|----|-----------|------------|
| `full` | Production / operator-managed | Production DB | Full hardening | systemd / supervisord |
| `mini` | Linux containers | Production DB (read-write) | AEE-MINI hardening (system user, 0600 env, smoke test) | supervisord |
| `edge` | Read-only / edge nodes | Read-only DB (PRAGMA query_only=1) | Edge read-only env | systemd |
| `developer` | Local development | tempdir SQLite | Sandbox only | None (manual) |

**Profile switch on an existing install is rejected.** To change profiles,
uninstall first, then re-install with the new profile (§2.4).

---

## 3. CLI Commands

### 3.1 `aee install`

```bash
aee install [--profile {full,mini,edge,developer}]
            [--channel {stable,rc,dev}]
            [--ref <git-ref>]
            [--yes] [--ci] [--offline] [--system]
            [--resume] [--from <stage>]
            [--log-format {text,json}]
            [--dry-run]
```

- **Default:** dry-run plan only (no side effects).
- Use `--dry-run` to preview the plan. Remove `--dry-run` (or add `--execute`)
  to perform side effects.
- Exit codes: see §10.4 of the spec.

### 3.2 `aee doctor`

```bash
aee doctor [--json] [--offline] [--reproducibility] [--quiet]
```

- Runs health checks H1–H10 (spec §11).
- Exit 0 = all PASS/WARN; non-zero = at least one FAIL.
- `--json` emits machine-readable output.
- `--reproducibility` re-runs `uv pip install` against the lock and compares.

### 3.3 `aee update`

```bash
aee update [--channel {stable,rc,dev}] [--ref <git-ref>]
           [--yes] [--offline-bundle <path>]
           [--log-format {text,json}]
```

- Without arguments: advances to the latest of the current channel.
- `--channel` switches channel (refuses if profile would change).
- Exit codes: see §10.4.

---

## 4. Release Channels

| Channel | Ref | Stability |
|---------|-----|-----------|
| `stable` | `refs/tags/latest-stable` | Production |
| `rc` | `refs/tags/latest-rc` | Release candidate |
| `dev` | `main` branch HEAD | Latest development |

Default: `stable`. Switch with `aee update --channel <new>`.

---

## 5. Version Pinning

The installed version is recorded in the `03_pin` stage marker:

```yaml
channel: stable
ref: refs/tags/v1.0.0
commit_sha: <40-char SHA>
pinned_at: 2026-07-25T08:00:00Z
requirements_lock_sha256: <sha256 of python.requirements.lock>
```

`aee doctor` verifies that on-disk `git rev-parse HEAD` matches the
recorded `commit_sha` and that the lock file hash matches. Drift is
reported as `state: drifted` (exit 9).

---

## 6. Stage Lifecycle

| Stage | Owner | Description |
|-------|-------|-------------|
| `00_detect` | Shell | Detect platform, resolve profile |
| `01_deps` | Shell | Install hard dependencies (git, python) |
| `02_clone` | Shell | Clone or update the repo |
| `03_pin` | Python | Record version pin |
| `04_runtime_setup` | Python | Set up venv, install Python deps |
| `05_health_check` | Python | Wait for /health to return 200 |
| `06_smoke_test` | Python | Run bounded smoke test |
| `07_agent_ready` | Python | Mark AGENT_READY |

**Resume:** `aee install --resume` skips completed stages and restarts
from the first incomplete or failed one.

**Partial failure:** Stage markers record state (PENDING / IN_PROGRESS /
COMPLETED / FAILED / SKIPPED). Re-running `aee install` resumes from the
last completed stage.

---

## 7. Secrets

- Secrets are NEVER embedded in committed files.
- Read at runtime from:
  - Env file (`$AEE_ENV_FILE`, default `~/.config/aee/env`), mode `0600`.
  - OS keychain (macOS Keychain, Windows Credential Manager, libsecret).
  - Platform secret manager (AWS/GCP/Azure) for `full` profile on cloud.
- All log lines and stage markers redact secret patterns (§8.2).
- `aee install --generate-env-template` writes a template env file with
  empty values for the operator to fill in.

---

## 8. Production Safety

1. **No automatic deploy.** Bootstrap brings the Agent to "ready";
   production deployment is a separate operator action.
2. **No automatic restart.** `aee doctor` reports health but does not
   restart the runtime.
3. **Dry-run by default.** `aee install` without `--execute` only plans.
4. **No force over an existing install.** Profile switch is rejected.
5. **No secret material in commits.** Redaction filter + pre-commit hook.
6. **Read-only doctor.** `aee doctor` performs no mutations.
7. **No telemetry without consent.**

---

## 9. Idempotency

Re-running `aee install` on an already-installed host is safe and a no-op
when nothing changed. Stages check for existing state before acting:
- `venv` stage: skip if `.venv` exists and is valid.
- `supervisor_conf` stage: skip if the program file is byte-identical.
- `health_check` stage: skip if `/health` already returns 200.

---

## 10. Rollback

`aee update --ref <older-tag>` downgrades to a previous release.

- The bootstrap does NOT auto-rollback dependencies (owned by the package
  manager).
- `aee doctor` may WARN on dependency drift after a downgrade.
- Hard `git reset` requires `--force-reset` if the working tree is dirty.

---

_End of operator guide._