# AEE Bootstrap v1 — Onboarding Guide

**Audience:** operators (human or AI agent) setting up a fresh machine
to run AEE.

**Scope:** this guide covers the executable bootstrap flow — stages
02 through 07 plus the unified entrypoint. It does NOT cover external
credential provisioning (API keys, tokens, OAuth); see the operator
guide (`operator-guide.md` §7) for the credential checklist.

---

## 1. Prerequisites (Operator-Supplied)

Before running the bootstrap, the operator must:

1. **Install system dependencies** (stages 00-01, shell-owned):
   - `git`, `python3` (≥ 3.11), `python3-venv`, `curl`
   - On Ubuntu/Debian: `sudo apt install git python3 python3-venv curl`
   - On macOS: `brew install git python@3.11 curl`

2. **Provision required credentials** (NOT automated by the bootstrap):
   - `HERMES_API_KEY` — from Nous Portal
   - `HERMES_BASE_URL` — Hermes gateway URL
   - `BRIDGE_HOST`, `BRIDGE_PORT` — bridge listener config
   - Optional: `BRIDGE_API_KEY`, `GPT_BRIDGE_API_KEY`, etc.
   - See `operator-guide.md` §7 for the full list.

3. **Clone the repository** (or ensure the operator is already in the
   repo directory). Stage 02 handles in-place and fresh-clone cases.

---

## 2. Quick Start

### Dry-run (plan only, no side effects)

```bash
./install.sh --profile mini --dry-run
```

This produces an 8-step plan and exits 0. No venv, no pip install, no
marker writes. Safe to run on any machine.

### Execute (real bootstrap, stages 02-07)

```bash
./install.sh --profile mini --execute
```

This drives the full stage chain:

| Stage | Owner | Action |
|-------|-------|--------|
| 00 detect | shell | Platform detection (already done by `install.sh`) |
| 01 deps | shell | System deps (operator installs before running) |
| 02 clone | Python | `git clone` or `git fetch` (skip if in-place) |
| 03 pin | Python | Read lockfile, record release channel |
| 04 runtime_setup | Python | Create venv, `pip install -r lockfile` |
| 05 health_check | Python | Run `aee doctor` (offline) |
| 06 smoke_test | Python | Import `aee.cli` + run `aee doctor` |
| 07 agent_ready | Python | Write `AGENT_READY` marker |

On success, `AGENT_READY` is written at the repo root and exit code is 0.

### Resume from failure

```bash
./install.sh --profile mini --execute --resume
```

The runner reads the marker store and skips already-completed stages,
resuming at the first pending or failed stage.

---

## 3. Stage Details

### Stage 02 — Clone

- **In-place** (repo already at `repo_root`): SKIPPED.
- **Fresh clone** (requires `git_url` in `ctx.extra`): `git clone
  <url> <path>`.
- **Existing clone, refresh**: `git fetch --prune`.
- Idempotent: re-running is a fetch, not a re-clone.

### Stage 04 — Runtime Setup

- Creates venv at `repo_root/.venv` (or `install_path`).
- Prefers `uv venv` when `uv` is on PATH; falls back to `python -m
  venv`.
- Installs from `bootstrap/manifests/python.requirements.lock`.
- Idempotent: if venv exists, only the pip install runs (refresh).

### Stage 05 — Health Check

- Runs `aee doctor` (H1-H10 checks, offline mode).
- Verdict PASS or CAVEAT → stage COMPLETED.
- Verdict FAIL → stage FAILED (run stops).
- Does NOT provision credentials; reads env vars the operator already
  set.

### Stage 06 — Smoke Test

- Imports `aee.cli` from the venv Python.
- Runs `aee doctor --profile <profile>` via the venv Python.
- Bounded by `timeout_seconds` (default 300s).
- Both steps must succeed for the stage to complete.

### Stage 07 — Agent Ready

- Writes `AGENT_READY` marker (JSON) at `repo_root/AGENT_READY`.
- Marker contains: `version`, `profile`, `timestamp`, `run_id`.
- Terminal stage — once complete, the bootstrap is done.

---

## 4. End-to-End Verification

To verify a freshly cloned repository can reach agent-ready state:

```bash
# 1. Clone the repo
git clone <repo-url> /opt/aee
cd /opt/aee

# 2. Install system deps (stage 01)
sudo apt install git python3 python3-venv curl

# 3. Provision credentials (operator step)
export HERMES_API_KEY=...
export HERMES_BASE_URL=...
export BRIDGE_HOST=...
export BRIDGE_PORT=...

# 4. Run the bootstrap
./install.sh --profile mini --execute

# 5. Verify
cat AGENT_READY  # should show JSON with profile, timestamp, run_id
aee doctor       # should exit 0
```

---

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `CloneTargetMissingError` | No repo at `repo_root` and no `git_url` | Clone the repo first, or pass `git_url` |
| `LockfileMissingError` | `bootstrap/manifests/python.requirements.lock` missing | Ensure the repo is fully cloned |
| `VenvCreationError` | `python -m venv` failed | Check Python version (≥ 3.11) |
| `PipInstallError` | Lockfile install failed | Check network; try `uv pip install` manually |
| `DoctorFailedError` | `aee doctor` reported FAIL | Run `aee doctor -v` for details |
| `SmokeImportError` | `aee.cli` not importable from venv | Check venv Python path |
| `SmokeDoctorError` | `aee doctor` exited non-zero | Check env vars are set in the venv |

See also: `troubleshooting.md`, `operator-guide.md`.

---

## 6. What the Bootstrap Does NOT Do

- **No credential provisioning.** API keys, tokens, OAuth flows are
  operator steps. The bootstrap reads env vars; it never writes or
  generates secrets.
- **No system user creation.** The `system_user` step in the plan is
  a shell-layer follow-up (spec §4 stage 01).
- **No supervisord reload.** The `supervisor_conf` step in the plan
  is a shell-layer follow-up.
- **No network-dependent health checks.** Stage 05 runs `aee doctor`
  in offline mode (`network=False`).

---

## 7. File Layout (New in Hardening)

```
aee/installer/stages/
  __init__.py          # Package + STAGE_EXECUTORS tuple
  base.py              # StageContext, StageResult, StageExecutor Protocol
  clone.py             # Stage 02_clone
  runtime_setup.py     # Stage 04_runtime_setup
  health_check.py     # Stage 05_health_check
  smoke_test.py        # Stage 06_smoke_test
  agent_ready.py       # Stage 07_agent_ready
aee/installer/runner.py         # BootstrapRunner orchestrator
aee/tests/test_bootstrap_stages.py  # Unit tests (20 tests)
aee/tests/test_bootstrap_runner.py  # Integration tests (5 tests)
docs/aee/bootstrap/onboarding.md   # This file
```
