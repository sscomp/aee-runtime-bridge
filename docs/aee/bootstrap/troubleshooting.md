# AEE Bootstrap v1 — Troubleshooting Guide

> **Audience:** Operators debugging AEE bootstrap issues.
> **Spec reference:** `reports/aee_bootstrap_v1_spec.md` §5 (Idempotency,
> Rollback, Partial-Failure Recovery, Retry, Resume), §11 (Health Checks),
> §13 (Platform-Specific Details)

---

## 1. Common Issues

### 1.0 Profile or channel invalid (exit 3)

**Symptom:** `aee install` or `aee update` exits 3
(`EXIT_PROFILE_INVALID`).

**Cause:** One of two conditions:

1. **Unknown profile** — The `--profile` value is not in the canonical
   profile set (`full`, `mini`, `edge`, `developer`). The
   :class:`UnknownProfileError` exception is raised by the backend's
   profile parser (defence in depth; argparse ``choices`` is the
   primary guard).

2. **Unknown release channel** — The `--channel` value is not in the
   canonical channel set (`stable`, `rc`, `dev`). The
   :class:`UnknownChannelError` exception is raised by
   :func:`aee.installer.backend.validate_channel` (defence in depth;
   argparse ``choices`` is the primary guard).

Both exceptions reuse exit code 3 — no new exit code was introduced.

**Fix:**

```bash
# Check the valid profile set
aee install --help   # see --profile choices

# Check the valid channel set
aee update --help     # see --channel choices

# Re-run with a valid value
aee install --repo-root /path/to/repo --profile mini
aee update --channel stable
```

> **Note:** Channel validation is case-insensitive — `STABLE`,
> `Stable`, and `  stable  ` are all accepted and canonicalised to
> `stable`. Profile validation is case-sensitive (profiles are
> lowercase by convention).

### 1.1 Pre-flight failed: "repo_root does not exist"

**Symptom:** `aee install` exits 4 (`EXIT_PRE_FLIGHT_FAILED`).

**Cause:** The `--repo-root` path does not exist or is not a directory.

**Fix:**
```bash
# Verify the path exists
ls -la /path/to/repo

# If missing, clone the repo first
git clone <remote> /path/to/repo
aee install --repo-root /path/to/repo --profile mini
```

### 1.2 Profile switch rejected

**Symptom:** `aee install` exits 5 (`EXIT_PROFILE_SWITCH_REJECTED`).

**Cause:** An existing install with a different profile was detected
(via the `.aee-profile` marker file).

**Fix:**
```bash
# Check the existing profile
cat /path/to/repo/.aee-profile

# To switch: uninstall first, then reinstall
rm /path/to/repo/.aee-profile
aee install --repo-root /path/to/repo --profile <new-profile>
```

> Per §2.4, profile switch requires uninstall + reinstall. This is a
> safety guard, not a bug.

### 1.3 Execute not authorized

**Symptom:** `aee install --execute` exits 6 (`EXIT_EXECUTE_NOT_AUTHORIZED`).

**Cause:** The shell-level execution path (system user creation, env file
writes, supervisord reload, smoke test) is not authorized in the current
slice. The backend defaults to dry-run.

**Fix:** This is expected in the current phase. The plan + pre-flight
are the deliverable. Use `--dry-run` (default) for planning; the shell
trampoline (`install.sh` / `install.ps1`) handles the actual side effects.

### 1.4 Drift detected

**Symptom:** `aee doctor` or `aee update` exits 9 (`EXIT_DRIFT_DETECTED`).

**Cause:** The on-disk `git rev-parse HEAD` does not match the
`commit_sha` recorded in the `03_pin` stage marker, or the lock file
hash has changed.

**Fix:**
```bash
# Check the recorded pin
cat /path/to/repo/bootstrap/stages/03_pin

# Check actual HEAD
git -C /path/to/repo rev-parse HEAD

# If the drift is intentional (e.g. you ran git pull manually):
aee update --channel stable  # re-pins to the current HEAD

# If the drift is unexpected, investigate:
git -C /path/to/repo log --oneline -5
```

### 1.5 Network error during clone/fetch

**Symptom:** `aee install` exits 10 (`EXIT_NETWORK_ERROR`).

**Cause:** The git remote or package-manager mirror is unreachable.

**Fix:**
```bash
# Test connectivity
git ls-remote <remote>

# If behind a proxy, set git proxy config
git config --global http.proxy http://proxy:port

# For air-gapped hosts, use the offline bundle (§7.5)
aee install --offline --offline-bundle /path/to/bundle
```

### 1.6 Secret missing

**Symptom:** `aee install` or `aee doctor` exits 11 (`EXIT_SECRET_MISSING`).

**Cause:** A required secret (API key, token, password) is absent or
fails validation.

**Fix:**
```bash
# Generate a template env file
aee install --generate-env-template

# Fill in the values
vi ~/.config/aee/env

# Re-run
aee doctor
```

> The error message names the missing secret but never includes its
> value. Check the env file at `$AEE_ENV_FILE` (default
> `~/.config/aee/env` on POSIX, `%APPDATA%\AEE\env` on Windows).

### 1.7 Dependency floor not met

**Symptom:** `aee install` exits 12 (`EXIT_DEPENDENCY_FLOOR_NOT_MET`).

**Cause:** git, python, or node version is below the required floor and
cannot be auto-installed.

**Fix:**
```bash
# Check versions
git --version    # floor: see spec §1.4
python3 --version  # floor: 3.11

# Install/upgrade manually, then re-run
aee install
```

### 1.8 Stage failed (retryable)

**Symptom:** `aee install` exits 7 (`EXIT_STAGE_FAILED_RETRYABLE`).

**Cause:** A bootstrap stage failed with a transient error (network blip,
lock contention).

**Fix:**
```bash
# Re-run with --resume to skip completed stages
aee install --resume
```

The bootstrap retries up to 3 times with 2s/4s/8s backoff before
reporting a retryable failure (§5.3, §5.4).

### 1.9 Stage failed (permanent)

**Symptom:** `aee install` exits 8 (`EXIT_STAGE_FAILED_PERMANENT`).

**Cause:** Max retries (3) exceeded for a stage.

**Fix:**
```bash
# Investigate the failure marker
cat /path/to/repo/bootstrap/stages/<stage_name>
# Look at error_class and stderr_tail (redacted)

# Fix the root cause, then force-retry
aee install --resume --force-retry
```

---

## 2. Platform-Specific Issues

### 2.1 Ubuntu / Debian

- **supervisor not found:** Install with `sudo apt install supervisor`.
- **Python 3.11 not available:** On Ubuntu 22.04, install via
  `sudo apt install python3.11 python3.11-venv`. On older Ubuntu, use
  the deadsnakes PPA or `uv` to install Python.
- **systemd vs supervisord:** The bootstrap supports both. `full` profile
  prefers systemd; `mini` profile uses supervisord.

### 2.2 macOS

- **Homebrew not installed:** Install from https://brew.sh, then re-run.
- **Python version:** macOS ships Python 3.9. Install 3.11+ via
  `brew install python@3.11`.
- **launchd:** The `developer` profile uses no supervisor; `full` profile
  can use a launchd plist (future work).

### 2.3 Windows (experimental)

- **PowerShell version:** Requires 5.1+ or 7+ (pwsh). Check with
  `$PSVersionTable.PSVersion`.
- **winget not available:** Install App Installer from the Microsoft
  Store, or use the offline bundle.
- **UAC elevation:** The bootstrap does NOT request UAC elevation by
  default. If a dependency install requires admin, run the install.ps1
  from an elevated PowerShell.
- **UNKNOWN capability:** Windows runs in UNKNOWN mode until the Windows
  adapter (§13.4) is implemented. This is expected, not a bug.

---

## 3. Diagnostics

### 3.1 Stage markers

Stage markers are written to `bootstrap/stages/<stage_name>` (e.g.
`bootstrap/stages/00_detect`, `bootstrap/stages/03_pin`). Each marker
records:

```json
{
  "stage": "03_pin",
  "state": "completed",
  "started_at": "2026-07-25T08:00:00Z",
  "completed_at": "2026-07-25T08:00:05Z",
  "error_class": null,
  "stderr_tail": null,
  "retry_count": 0
}
```

A `FAILED` marker includes `error_class` and a redacted `stderr_tail`.

### 3.2 Doctor JSON output

```bash
aee doctor --json | python3 -m json.tool
```

Each check returns `PASS`, `WARN`, `FAIL`, or `SKIP` with a detail string.

### 3.3 Diagnostics bundle

```bash
aee doctor --json > diagnostics.json
tar czf aee-diagnostics.tar.gz diagnostics.json bootstrap/stages/
```

Attach the bundle to support tickets. All secret material is redacted.

---

## 4. Recovery Procedures

### 4.1 Resume from a failed stage

```bash
aee install --resume
```

Skips completed stages; restarts from the first FAILED or PENDING stage.

### 4.2 Resume from a specific stage

```bash
aee install --from 03_pin
```

Skips all stages before `03_pin`; starts from `03_pin`.

### 4.3 Rollback to a stage

```bash
aee install --rollback-to 02_clone
```

Reverts stages after `02_clone` to PENDING and re-runs from there.

### 4.4 Full reinstall

```bash
rm -rf /path/to/repo/.aee-profile /path/to/repo/bootstrap/stages/
aee install --profile mini
```

> This does NOT remove the venv or installed dependencies. For a
> completely clean install, also remove `.venv/` and re-run.

---

_End of troubleshooting guide._