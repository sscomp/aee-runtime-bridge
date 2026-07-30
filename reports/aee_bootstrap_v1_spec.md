# AEE Bootstrap v1 — Zero-touch Cross-platform Deployment Specification

> Status: **DRAFT — planning only**. No source edits, commits, pushes, or
> deployments were performed to produce this document.
> Author: M2 (Hermes Agent, Abacus.ai runtime).
> Repository inspected (read-only): `/home/ubuntu/hermes-runtime-bridge`
> @ `31ce4678d8f233e4c1bda9eadd35459d3254b849` on `main`.
> Date: 2026-07-25.

This specification defines **AEE Bootstrap v1**: a zero-touch, cross-platform
bootstrap and deployment story that takes a freshly provisioned machine
(Ubuntu / Debian / macOS / Windows) from "no AEE present" to "Agent Ready"
with a single command, then keeps itself healthy and updatable.

This document is a **planning artifact only**. It explicitly distinguishes:

* **Verified repository facts** — assertions backed by read-only inspection
  of the repository at the HEAD listed above. These are marked with the tag
  `[VERIFIED]` and can be re-checked by running the listed commands.
* **Proposed design decisions** — assertions about how the bootstrap *should*
  behave. These are marked with the tag `[PROPOSAL]` and have no current
  on-disk implementation unless explicitly cited.

---

## 1. Goals, Non-Goals, Assumptions, Platform/Version Policy

### 1.1 Goals

1. **Zero-touch bootstrap.** A single command on a fresh machine installs
   the AEE Runtime and brings an Agent to "ready" status without interactive
   prompts for the default profile (`developer` on macOS, `mini` on Linux
   containers, `full` on operator-managed hosts). [PROPOSAL]
2. **Cross-platform parity.** Ubuntu, Debian, macOS, and Windows are
   first-class targets. Platform-specific behavior lives behind a
   `PlatformAdapter` interface; the shared bootstrap core is identical
   across all four. [PROPOSAL]
3. **Idempotent lifecycle.** Re-running the bootstrap on an already-installed
   host is safe and a no-op when nothing changed; `aee update` advances the
   install to a new release channel; `aee doctor` reports drift. [PROPOSAL]
4. **Reproducible deployment.** Pinned dependency manifests, recorded
   release-channel + version, and a verification step make the same bootstrap
   input produce the same on-disk state modulo platform-native artifacts
   (homebrew prefixes, system Python versions). [PROPOSAL]
5. **Safe failure and resume.** Partial failure leaves the host in a known
   state recorded on disk; re-running the bootstrap resumes from the last
   completed stage rather than restarting from scratch. [PROPOSAL]

### 1.2 Non-Goals

1. **Not an OS provisioner.** Bootstrap assumes the OS is already installed
   and reachable via a normal user shell (POSIX) or PowerShell (Windows).
   It does not image disks, partition, or bootstrap cloud instances. [PROPOSAL]
2. **Not a secrets distributor.** The bootstrap wires up *where* secrets are
   read from (env file, OS keyring, platform secret manager) but never
   embeds, transmits, or generates secrets. [PROPOSAL]
3. **Not a CI/CD replacement.** CI integration is an *install mode*, not a
   CI runner. The bootstrap does not own build pipelines. [PROPOSAL]
4. **Not a container orchestrator.** Docker is supported only as an optional
   dependency for the `docker` profile and is not required for the default
   install on any platform. [PROPOSAL]
5. **No automatic deploy/restart in the spec.** Production deployments and
   restarts require explicit operator authorization (see §18). [PROPOSAL]

### 1.3 Assumptions

1. **Outbound network** is available on first bootstrap (for dependency
   install, git clone, and version metadata fetch). Air-gapped operation is
   covered in §7.5 as a separate install mode with extra prerequisites.
   [PROPOSAL]
2. **A non-root user** exists on POSIX targets and is the install owner.
   Windows targets have a normal user account; UAC elevation is requested
   only when required (§6.4). [PROPOSAL]
3. **Git** is either pre-installed or installable via the platform package
   manager. The bootstrap will install git if missing; it is the only
   universal hard dependency. [PROPOSAL]
4. **Python** is either pre-installed at a supported version (§1.4) or
   installable. The bootstrap prefers `uv` for environment management and
   falls back to system `python3` / `python.org` installers. [PROPOSAL]
5. **The repository** being deployed is reachable via git over SSH or HTTPS.
   The default remote is the AEE Runtime Bridge repo. [PROPOSAL]
6. **Profiles** (`full`, `mini`, `edge`, `developer`) are the canonical
   deployment shapes, validated by the existing
   `aee.profiles.descriptor.KNOWN_PROFILES`. [VERIFIED —
   `aee/profiles/descriptor.py`]

### 1.4 Supported Platforms and Version Policy

The bootstrap v1 target matrix. Versions older than the listed floor are
rejected by pre-flight with a clear, actionable error.

| Platform | Floor | Tested | Notes |
| --- | --- | --- | --- |
| Ubuntu Linux | 22.04 LTS | 22.04, 24.04 | systemd-based; supervisor or systemd unit. [PROPOSAL] |
| Debian Linux | 12 (bookworm) | 12 | systemd-based. [PROPOSAL] |
| macOS | 13 (Ventura) | 13, 14, 15 | Homebrew recommended; `launchd` supervisor. [PROPOSAL] |
| Windows | 11 (10.0.22621+) | 11 | PowerShell 5.1+ or 7+ (pwsh); Windows-native paths. [PROPOSAL] |

Python runtime floor: **3.11** (matches the existing `.venv/lib/python3.11`
in the repo and the Python 3.11 toolchain recorded in the runtime
environment). [VERIFIED — `.venv/lib/python3.11/` exists; runtime system
`python3 = 3.11.2`]. Python 3.12 is accepted; 3.10 and earlier are rejected
by pre-flight. [PROPOSAL]

Git floor: **2.30+** (for sparse-checkout stability and modern `clone --filter`
support). [PROPOSAL]

`uv` is the preferred Python environment manager; pip is the fallback when
`uv` is not installable. Node.js is required only for the AEE Lightweight
Agent Runtime component (existing `aee-runtime/runtime/`); floor **Node 20**,
tested 20 / 22. [VERIFIED — `aee-runtime/runtime/` exists with Node-based
runtime; `.venv` is Python-only]. Docker is optional and required only for
the `docker` profile. [PROPOSAL]

---

## 2. Cross-Platform Architecture

### 2.1 Layered Model

The bootstrap follows the system context given in the task brief:

```
Agent → AEE Runtime → Platform Adapter → {Ubuntu | Debian | macOS | Windows}
    → Bootstrap Installer → Environment Detection → Dependency Installation
    → Git Clone/Update → Runtime Setup → Health Check → Smoke Test
    → Agent Ready
```

Mapping to on-disk components (existing substrate is reused, not
re-implemented):

```
┌──────────────────────────────────────────────────────────────┐
│  Agent (operator / CI / orchestrator)                          │
│    │ issues: aee install | aee doctor | aee update             │
│    ▼                                                           │
│  Bootstrap CLI  (shell: install.sh / PowerShell: install.ps1)  │
│    │ parses flags, invokes Python backend                      │
│    ▼                                                           │
│  Installer Backend  (aee.installer.backend)                    │
│    │ profile-aware plan, idempotent, dry-run default           │
│    ▼                                                           │
│  Platform Adapter  (aee.deploy.adapters.*)                     │
│    │ detect() → HostCapabilities                               │
│    │ materialize(profile, cap) → MaterializationResult         │
│    │ health_check(profile) → HealthStatus                      │
│    ▼                                                           │
│  Platform-specific execution                                   │
│    Ubuntu/Debian: apt + systemd/supervisord                    │
│    macOS:        Homebrew + launchd                            │
│    Windows:      PowerShell + Windows Service / scheduled task │
└──────────────────────────────────────────────────────────────┘
```

[VERIFIED — `aee.installer.backend.InstallerBackend` exists with dry-run
default; `aee.deploy.adapters.{abacus,macbook,docker,terraform_aws,zo}`
implement the `PlatformAdapter` Protocol; `aee.deploy.contract.HostCapabilities`
+ `MaterializationResult` + `HealthStatus` exist]. The adapter Protocol is
`@runtime_checkable` with `name`, `detect()`, `materialize(profile, cap)`,
`health_check(profile)`. [VERIFIED — `aee/deploy/adapters/base.py`].

### 2.2 Platform Adapter vs Shared Bootstrap Core — Separation

The hard rule is: **`sys.platform` (and any platform-native command) is read
in exactly one module** — `aee.platform.current.resolve_platform_identity`
and the per-adapter `detect()` methods. Everything else consumes the
`PlatformIdentity` enum or the `PlatformCapabilities` facade. This invariant
is already established for Linux/macOS:

> "Deterministic platform identity. `resolve_platform_identity` is the
> *only* place `sys.platform` is read. Everything else consumes the
> `PlatformIdentity` enum or the facade."
> [VERIFIED — `aee/platform/current.py` module docstring]

Bootstrap v1 extends the same invariant to Windows:

* `PlatformIdentity` currently has `LINUX`, `MACOS`, `UNKNOWN`. Windows
  (`win32`) currently maps to `UNKNOWN`. [VERIFIED —
  `aee/tests/test_platform_bootstrap.py:119`:
  `resolve_platform_identity("win32") == PlatformIdentity.UNKNOWN`].
* Bootstrap v1 adds `WINDOWS` to the enum and a `WindowsAdapter` (see §3,
  §13.4). Until the adapter is added, Windows hosts run via
  `UnknownDefaults`, which is a frozen, explicitly-limited capability set
  that fails safely. [VERIFIED — `aee/platform/current.py` describes
  `UnknownDefaults` as the safe fallback].

### 2.3 Detection Contract

`resolve_platform_identity(platform: Optional[str] = None)` is the single
deterministic entry. Tests inject `platform_id` explicitly or patch
`sys.platform`; production callers pass nothing. [VERIFIED]. The bootstrap
shell/PowerShell entrypoints must NOT re-implement platform detection —
they invoke `python -c "from aee.platform import resolve_platform_identity;
print(resolve_platform_identity().value)"` and branch on the result. This
keeps the shell layer thin and the Python layer authoritative. [PROPOSAL]

### 2.4 Profile Routing

The bootstrap selects a default profile per detected platform unless the
operator passes `--profile`:

| Platform | Default profile | Reason |
| --- | --- | --- |
| Ubuntu / Debian (container) | `mini` | Hardened, strict safety tier; matches existing M2/Abacus deployment. |
| Ubuntu / Debian (VM) | `full` | Operator-managed host. |
| macOS | `developer` | Existing B2/MacBook adapter only accepts `developer`. |
| Windows | `developer` | First-class support deferred; bootstrap on Windows v1 runs `developer` only. |

[VERIFIED — `aee/deploy/adapters/macbook.py` MacBookAdapter accepts only
`developer`; `aee/profiles/descriptor.py` `KNOWN_PROFILES = ("full", "mini",
"edge", "developer")` with `safety_tier` per profile]. Profile switch on an
existing install is **rejected** by the installer backend; the operator
must uninstall + reinstall. [VERIFIED —
`aee.installer.backend.ProfileSwitchRejectedError`].

---
## 3. Proposed Repository Layout

The bootstrap layer adds files that do not exist today. The list is
explicit about which paths are [VERIFIED] (already on disk) and which are
[PROPOSAL] (to be added by future work orders).

```
hermes-runtime-bridge/
├── install.sh                           # [VERIFIED — tracked at HEAD, added by commit 8a43494 "feat(aee-9.3): add install.sh shell wrapper with integration tests"; assess/reuse/migrate, do NOT treat as net-new]
├── install.ps1                          # [PROPOSAL] Windows bootstrap entry (PowerShell)
├── aee/
│   ├── platform/
│   │   ├── __init__.py                  # [VERIFIED]
│   │   ├── current.py                   # [VERIFIED] bootstrap resolver (extend for WINDOWS)
│   │   └── windows.py                   # [PROPOSAL] Windows detection shim
│   ├── deploy/
│   │   ├── adapters/
│   │   │   ├── base.py                  # [VERIFIED] PlatformAdapter Protocol
│   │   │   ├── abacus.py                # [VERIFIED] Ubuntu/Debian container
│   │   │   ├── macbook.py               # [VERIFIED] macOS laptop
│   │   │   ├── docker.py                # [VERIFIED] docker-host
│   │   │   ├── terraform_aws.py         # [VERIFIED] cloud-vm placeholder
│   │   │   ├── zo.py                    # [VERIFIED] N2/Zo profile=mini
│   │   │   ├── debian.py                # [PROPOSAL] Debian VM/physical host
│   │   │   ├── ubuntu_vm.py             # [PROPOSAL] Ubuntu non-container (systemd)
│   │   │   └── windows.py               # [PROPOSAL] Windows host adapter
│   │   └── samples/
│   │       └── host.capabilities.*.yaml # [VERIFIED] reference sample docs
│   ├── installer/
│   │   ├── __init__.py                  # [VERIFIED] InstallerBackend re-exports
│   │   ├── backend.py                   # [VERIFIED] profile-aware plan/execute
│   │   ├── cli.py                       # [PROPOSAL] `aee install` CLI surface
│   │   ├── doctor.py                    # [PROPOSAL] `aee doctor` health checks
│   │   ├── update.py                    # [PROPOSAL] `aee update` channel switch
│   │   ├── lifecycle.py                 # [PROPOSAL] stage marker / resume state
│   │   └── redaction.py                # [PROPOSAL] shared secret-redaction regex (§8.2; no reusable regex exists in aee/artifacts/policy.py)
│   ├── profiles/
│   │   └── descriptor.py               # [VERIFIED] KNOWN_PROFILES + safety tiers
│   └── ...
├── bootstrap/
│   ├── install.sh                       # [VERIFIED — top-level /home/ubuntu/hermes-runtime-bridge/install.sh already exists and is tracked; this path under bootstrap/ is PROPOSAL for a canonical installer body if the bootstrap/ tree is introduced. Work order W6 MUST assess whether to migrate/reuse the existing top-level install.sh rather than create a parallel one]
│   ├── install.ps1                      # [PROPOSAL] canonical Windows installer body
│   ├── lib/
│   │   ├── detect.sh                    # [PROPOSAL] thin POSIX detect → python
│   │   ├── detect.ps1                   # [PROPOSAL] thin Windows detect → python
│   │   ├── deps.sh                      # [PROPOSAL] POSIX dependency install
│   │   ├── deps.ps1                     # [PROPOSAL] Windows dependency install
│   │   └── resume.sh                    # [PROPOSAL] resume-from-last-stage helper
│   ├── manifests/
│   │   ├── apt.deps.txt                 # [PROPOSAL] apt packages required
│   │   ├── brew.deps.txt                # [PROPOSAL] Homebrew formulae
│   │   ├── pwsh.deps.txt                # [PROPOSAL] PowerShell modules / winget ids
│   │   ├── python.requirements.in       # [PROPOSAL] top-level Python deps
│   │   └── python.requirements.lock     # [PROPOSAL] uv-pip-compile lock
│   └── stages/
│       └── 00_detect → 06_smoke         # [PROPOSAL] one marker file per stage
├── reports/
│   └── aee_bootstrap_v1_spec.md         # [VERIFIED — this document]
└── ...
```

The repository root currently has `install.sh` (tracked, added by commit
`8a43494`), `tests/test_install_shell_wrapper.sh` (tracked), and the
`scripts/` directory (`compile-deps.sh`, `verify-deps.sh`). [VERIFIED —
`git ls-files | grep -i install.sh`; `git ls-files scripts/`]. The proposed
`install.ps1`, `bootstrap/` tree, and `aee/installer/{cli,doctor,update,
lifecycle}.py` are net-new and listed in §16 as separate work orders.
[PROPOSAL] Work order W6 (POSIX trampoline) MUST start by assessing the
existing `install.sh` and either reuse, migrate, or formally supersede it —
creating a parallel installer without that assessment is explicitly out of
scope for this spec.

### 3.1 Why a Shell/PowerShell Entry at All

A shell/PowerShell entry already exists on POSIX in the form of the tracked
`install.sh` (added by commit `8a43494`, AEE Epic 9.3 "Installer Shell
Wrapper", Master Plan §21.3). [VERIFIED — `git ls-files --error-unmatch
install.sh`; head of file at HEAD reads `#!/usr/bin/env bash` followed by the
Epic 9.3 banner]. It already delegates ALL profile validation, planning, and
read-only pre-flight to the canonical Python CLI (`aee.cli →
aee.installer.backend`) and is dry-run by default. The bootstrap v1 work
orders in §16 (in particular W6) MUST assess this existing entry and either
reuse, migrate, or formally supersede it; a parallel installer is explicitly
out of scope. The shell/PowerShell entry's *only* job is therefore:

1. Detect the platform (by invoking Python if present, else via a tiny
   native heuristic).
2. Ensure Python itself is present at the §1.4 floor (install via apt,
   brew, or winget).
3. Ensure `git` is present at the §1.4 floor.
4. `git clone` (or `git fetch` + checkout) the repo to the install path.
5. Hand off to the Python backend (`python -m aee.installer.cli`).

The shell layer MUST NOT embed business logic; it is a thin trampoline.
[PROPOSAL]

---

## 4. Bootstrap Lifecycle

The bootstrap is a stage machine. Each stage writes a marker file under
`bootstrap/stages/` (or `%LOCALAPPDATA%\AEE\bootstrap\stages\` on Windows)
on success; the marker contains the stage name, the bootstrap run id, and
an ISO-8601 UTC timestamp. Re-running the bootstrap reads the marker set
and resumes at the first stage without a marker. [PROPOSAL]

| # | Stage | Owner | Idempotent action |
| --- | --- | --- | --- |
| 00 | detect | shell/PS | Identify platform; pick default profile; record platform_id. |
| 01 | deps | shell/PS | Install git, python, uv (or pip fallback), and platform package deps. |
| 02 | clone | shell/PS | `git clone` repo to install path, or `git fetch --prune` if already cloned. |
| 03 | pin | Python backend | Read `bootstrap/manifests/python.requirements.lock`; record release channel + version. |
| 04 | runtime_setup | Python backend | Create venv (`uv venv` or `python -m venv`); install locked deps; install Node deps if profile needs runtime. |
| 05 | health_check | Python backend | `aee doctor` (§10, §11); record results JSON. |
| 06 | smoke_test | Python backend | Run profile smoke; emit exit code + evidence path. |
| 07 | agent_ready | Python backend | Write `AGENT_READY` marker with version + profile + timestamp. |

Stages 00–02 are owned by the shell/PowerShell layer so that they can run
without a Python environment present. Stages 03–07 are owned by the
Python backend and invoked as `python -m aee.installer.cli install
--resume`. [PROPOSAL]

The lifecycle is identical on Windows; stage ownership differs only by
file extension (`.ps1` vs `.sh`). [PROPOSAL]

---

## 5. Idempotency, Rollback, Partial-Failure Recovery, Retry, Resume

### 5.1 Idempotency

Each stage is required to be idempotent: running the same stage with the
same inputs yields the same on-disk state. This is achieved by:

* Package-manager installs use `--no-install-recommends` (apt) / `--quiet`
  (brew) and rely on the manager's own "already installed" short-circuit.
  [PROPOSAL]
* `git clone` is replaced by `git fetch --prune origin && git checkout
  <ref>` when the destination directory already exists and is a git repo.
  [PROPOSAL]
* venv creation uses `uv venv` (idempotent) or `python -m venv
  --clear=false` (idempotent on existing venv). [PROPOSAL]
* The installer backend is already idempotent at the planning layer:
  "Planning the same profile twice yields the same `InstallPlan`."
  [VERIFIED — `aee/installer/__init__.py` module docstring].

### 5.2 Rollback

Bootstrap v1 supports **stage-level rollback**, not full uninstall:

* A failed stage is recorded with `state=failed` and the error class in the
  marker file. The host is left in the last successful state. [PROPOSAL]
* `aee install --rollback-to <stage>` removes markers for stages at or
  after the named stage and re-runs them. It does NOT remove dependencies
  installed by stage 01 (those are owned by the package manager).
  [PROPOSAL]
* Full uninstall is a separate operator action (`aee uninstall`) and is
  out of scope for v1 (tracked as an open question in §17). [PROPOSAL]

### 5.3 Partial-Failure Recovery

When a stage fails:

1. The marker file records `state=failed`, `error_class`, `stderr_tail`
   (last 4 KB, with secrets redacted per §8.4), and `retry_count`.
2. The bootstrap exits with the stage's exit code (§10.4).
3. The operator re-runs `aee install --resume`. The stage is retried.
4. After 3 consecutive failures of the same stage, the bootstrap refuses
   to retry without `--force-retry` and emits a diagnostics bundle path
   (§12.3). [PROPOSAL]

### 5.4 Retry

* Network operations (`git fetch`, package installs) use exponential
  backoff: 3 attempts with 2s / 4s / 8s sleeps, configurable via
  `AEE_BOOTSTRAP_RETRY_*` env vars. [PROPOSAL]
* The Python backend's `execute()` is single-attempt per stage; retry is
  the shell layer's responsibility so that Python crash traces are not
  hidden by an outer retry loop. [PROPOSAL]

### 5.5 Resume

* `aee install --resume` reads the marker set, finds the first stage
  with no marker or `state=failed`, and runs from there. [PROPOSAL]
* `aee install --from <stage>` runs the named stage and all subsequent
  stages regardless of existing markers; used after a manual fix.
  [PROPOSAL]
* The marker directory is per-install-path, so multiple installs on the
  same host (e.g. `~/aee-dev` and `/opt/aee`) do not collide. [PROPOSAL]

---

## 6. Dependency Strategy

### 6.1 Hard Dependencies (always installed)

| Dependency | Floor | Source by platform |
| --- | --- | --- |
| git | 2.30 | apt / apt-get (Debian, Ubuntu); brew (macOS); winget or git-scm (Windows) |
| python | 3.11 | apt (Ubuntu 22.04 has 3.10 → install deadsnakes 3.11); apt (Debian 12 has 3.11); brew `python@3.11` (macOS); winget `Python.Python.3.11` (Windows) |
| uv | latest | `pip install uv` after Python is present, or curl installer from astral.sh |

[PROPOSAL]

### 6.2 Conditional Dependencies (profile-gated)

| Profile | Extra deps |
| --- | --- |
| `full` | Docker (optional), supervisord or systemd unit |
| `mini` | supervisord (`apt install supervisor` / `brew install supervisor` / `pip install supervisor` on Windows) |
| `edge` | Docker, read-only filesystem enforcement |
| `developer` | Node.js 20+ (for AEE Lightweight Agent Runtime), Docker (optional) |

[VERIFIED — `aee-runtime/` is Node-based; `aee-runtime/supervisor/` and
`aee-runtime/systemd/` exist with `aee-runtime.conf` and
`aee-runtime.service`].

### 6.3 Package Managers

* **apt / apt-get** on Debian and Ubuntu. Bootstrap prefers
  `apt-get --no-install-recommends` for reproducibility. [PROPOSAL]
* **Homebrew** on macOS. Bootstrap installs Homebrew if missing and the
  operator did not pass `--no-brew`. [PROPOSAL]
* **winget** on Windows 11 (always present on supported floor). Bootstrap
  falls back to direct MSI installers if winget is unavailable.
  [PROPOSAL]
* **pip / uv** for Python packages. `uv pip install -r
  bootstrap/manifests/python.requirements.lock` is the canonical path.
  [PROPOSAL]

### 6.4 Privilege Escalation

* **POSIX**: `sudo` is used only for system package installs (apt, brew's
  optional sudo steps). The runtime itself runs as the install owner
  (non-root). This matches the existing constraint that supervisord runs
  as `user=ubuntu` and cannot setuid to root. [VERIFIED — memory note:
  "supervisord runs as user=ubuntu (PID 7), cannot setuid to root"].
* **macOS**: `sudo` is avoided entirely on macOS by installing into the
  user's homebrew prefix and using `~/Library/LaunchAgents` (user-scope
  launchd) rather than `/Library/LaunchDaemons`. [PROPOSAL]
* **Windows**: elevation is requested only when installing system-wide
  (default is per-user install into `%LOCALAPPDATA%\AEE`). UAC prompt is
  expected; the bootstrap documents this in the Windows section (§13.4).
  [PROPOSAL]

### 6.5 Install Scope (user vs system)

Default scope is **per-user** on all platforms to avoid privilege
escalation by default. System-scope install requires `--system` and is
only supported on Ubuntu/Debian (systemd unit) and Windows (service). On
macOS, system-scope is not supported in v1 (operator runs launchd by
hand). [PROPOSAL]

---

## 7. Install Modes

### 7.1 Interactive (default)

The default mode prompts the operator only when:

* The detected default profile conflicts with an existing install
  (rejected per §2.4).
* A dependency floor is not met and the operator must approve a
  side-install (e.g. Homebrew on macOS, Python 3.11 on Ubuntu 22.04).

All other inputs use safe defaults. Prompts are emitted to stderr (so
stdout stays machine-readable when piped). [PROPOSAL]

### 7.2 Non-interactive (`--yes` / `-y`)

Skips all prompts; uses defaults. Exits non-zero if a prompt would have
been required and no default exists (e.g. profile conflict). Suitable for
operator-driven SSH bootstrap. [PROPOSAL]

### 7.3 CI (`--ci`)

* Implies `--yes`.
* Forces `--log-format json`.
* Forces `--no-color`.
* Disables any `sudo` interactive password prompt; requires `sudo
  --non-interactive` to be available or `--system` to be declined.
* Captures all stage output to a fixed `AEE_CI_LOG_DIR` for artifact
  upload.
* Exit code is the stage's exit code (§10.4). [PROPOSAL]

### 7.4 User vs System Scope

See §6.5. `--system` opts into system scope; the default is per-user.

### 7.5 Offline / Air-gapped

* **Prerequisite**: a pre-staged bundle at
  `$AEE_OFFLINE_BUNDLE` (POSIX) or `%AEE_OFFLINE_BUNDLE%` (Windows)
  containing:
  * The git repo as a tarball (`repo.tar.gz`).
  * The lock file (`python.requirements.lock`).
  * A Python wheelhouse (`wheelhouse/`) for all locked deps.
  * Platform package manifests pre-downloaded (apt: `.deb` files; brew:
  bottles; winget: MSIX packages).
* **Behavior**: `--offline` skips network calls entirely. `git clone`
  becomes `tar -xf $AEE_OFFLINE_BUNDLE/repo.tar.gz`. `uv pip install`
  uses `--no-index --find-links $AEE_OFFLINE_BUNDLE/wheelhouse`.
* **Verification**: `aee doctor --offline` checks that all required
  artifacts are present in the bundle before declaring ready.
* **Limitation**: Air-gapped upgrades require a new bundle. `aee update`
  refuses to run in `--offline` mode without `--offline-bundle <path>`.
  [PROPOSAL]

---

## 8. Secure Configuration and Secret Handling

### 8.1 Never Embed Secrets

Installers (shell, PowerShell, Python backend) MUST NOT embed secrets,
tokens, API keys, or password material in any committed file. Secrets
are read at runtime from one of:

* An env file at `$AEE_ENV_FILE` (default: per-user
  `~/.config/aee/env` on POSIX, `%APPDATA%\AEE\env` on Windows), mode
  `0600`. [VERIFIED — `aee/installer/__init__.py` documents `0600` env
  file as part of the mini plan].
* The OS keychain (macOS Keychain, Windows Credential Manager, libsecret
  on Linux) when `AEE_SECRET_BACKEND=keychain`.
* The platform secret manager (AWS Secrets Manager, GCP Secret Manager,
  Azure Key Vault) when `AEE_SECRET_BACKEND=cloud` and the corresponding
  SDK is available — used only by `full` profile on cloud hosts.

[PROPOSAL]

### 8.2 Secret Redaction in Logs

All log lines and stage markers redact any string matching the secret
patterns documented below. The patterns are a **[PROPOSAL]** for the
bootstrap v1 surface; they are NOT yet backed by an existing reusable
regex module in this repository.

* `*_API_KEY`, `*_TOKEN`, `*_SECRET`, `*_PASSWORD` env var names →
  redacted as `<REDACTED:NAME>`.
* Bearer tokens, JWTs, and basic-auth headers → redacted.
* Long hex/base64 strings (>40 chars, high entropy) → truncated to first
  8 + `…` + last 4.

[PROPOSAL — there is NO reusable secret-redaction regex in
`aee/artifacts/policy.py`. A read-only inspection at HEAD
`31ce4678d8f233e4c1bda9eadd35459d3254b849` confirms that
`aee/artifacts/policy.py` contains zero matches for the patterns
`redact`, `REDACT`, `sanitize`, `SANITIZE`, `secret`, `SECRET`
(`grep -nE 'redact|REDACT|sanitize|SANITIZE|secret|SECRET'
aee/artifacts/policy.py` → 0 matches). An earlier draft of this spec
claimed `[VERIFIED pattern — aee/artifacts/policy.py already redacts
secrets]`; that claim was false and has been removed. A shared
redaction policy for the bootstrap v1 surface is **[PROPOSAL]** and
must be implemented by a work order (see §16 W10 integration tests and
§17.1 R5 risk note). Existing redaction in this repo lives in
`aee/observability_runtime/serialization.py` (`SECRET_CANARY` +
`<redacted>` sentinel, used by the AEE-7.4 observability wire format)
and in `aee/tests/test_aee74_round_trip_e2e.py` (test fixtures); these
are NOT a generic redaction API and MUST NOT be cited as a verified
reusable regex for bootstrap logs without a separate implementation
work order.]

### 8.3 Env File Generation

When the operator runs `aee install --generate-env-template`, the
installer writes a template env file with all known keys present but
values empty, mode `0600`. The operator fills it in before first run.
The runtime refuses to start if required keys are missing. [PROPOSAL]

### 8.4 Secret Scanning in Stage Markers

The `stderr_tail` field in a failed-stage marker passes through the same
redaction filter as logs (§8.2) before being written to disk. The
filter is applied in Python (not shell) to keep the regex set in one
place. [PROPOSAL]

---

## 9. Git Clone / Update / Version Pinning / Release Channels

### 9.1 Release Channels

The bootstrap supports three channels:

| Channel | Ref | Stability |
| --- | --- | --- |
| `stable` | `refs/tags/latest-stable` (annotated tag moved per release) | Production |
| `rc` | `refs/tags/latest-rc` | Release candidate |
| `dev` | `main` branch HEAD | Latest development |

Default channel is `stable`. The operator selects with `--channel
{stable,rc,dev}`. [PROPOSAL]

### 9.2 Version Pinning

The installed version is recorded in `bootstrap/stages/03_pin`:

```
channel: stable
ref: refs/tags/v1.0.0
commit_sha: <40-char SHA>
pinned_at: 2026-07-25T08:00:00Z
requirements_lock_sha256: <sha256 of python.requirements.lock>
```

`aee doctor` verifies that the on-disk `git rev-parse HEAD` matches the
recorded `commit_sha` and that the lock file hash matches; drift is
reported as `state: drifted`. [PROPOSAL]

### 9.3 Clone / Update Behavior

* **Fresh clone**: `git clone --filter=blob:none <remote> <path> &&
  git -C <path> checkout <ref>`. `--filter=blob:none` keeps the clone
  small on air-gapped/limited-storage hosts. [PROPOSAL]
* **Existing repo, same channel**: `git fetch --prune origin &&
  git checkout <ref> && git reset --hard origin/<ref>`. Hard reset is
  safe because the install path is owned by the bootstrap and should not
  contain uncommitted operator edits. The bootstrap refuses to hard-reset
  if `git status --porcelain` is non-empty unless `--force-reset` is
  passed. [PROPOSAL]
* **Existing repo, channel switch**: `aee update --channel <new>`
  updates the pin and runs `git fetch && git checkout`. Profile switch
  is still rejected per §2.4. [PROPOSAL]

### 9.4 Reproducibility

The same `(channel, ref, commit_sha, requirements_lock_sha256)` tuple
MUST produce byte-identical venv contents modulo platform-native paths
(homebrew prefix, Windows ProgramFiles). `aee doctor --reproducibility`
re-runs `uv pip install` against the lock and compares the resulting
`uv pip list --format json` to a recorded baseline. [PROPOSAL]

---

## 10. CLI Contract

### 10.1 `aee install`

```
aee install [--profile {full,mini,edge,developer}]
            [--channel {stable,rc,dev}]
            [--ref <git-ref>]
            [--yes] [--ci] [--offline] [--system]
            [--resume] [--from <stage>] [--rollback-to <stage>]
            [--log-format {text,json}] [--no-color]
            [--dry-run]
```

* Default: dry-run plan only (matches existing
  `InstallerBackend(dry_run=True)`). [VERIFIED —
  `aee/installer/__init__.py`].
* `--dry-run` removed (or `--execute` added) to perform side effects.
* Exit codes in §10.4.

[PROPOSAL]

### 10.2 `aee doctor`

```
aee doctor [--json] [--offline] [--reproducibility] [--quiet]
```

Runs the health checks in §11. By default emits human-readable text;
`--json` emits the machine-readable result document (§10.5). Exit code
is non-zero if any check is `FAIL`, zero if all `PASS` or `WARN`.
[PROPOSAL]

### 10.3 `aee update`

```
aee update [--channel {stable,rc,dev}] [--ref <git-ref>]
           [--yes] [--offline-bundle <path>]
           [--log-format {text,json}]
```

* Without arguments: advances to the latest of the current channel.
* With `--channel`: switches channel (refuses if profile would change).
* Exit codes in §10.4.

[PROPOSAL]

### 10.4 Exit Codes

The exit-code table is split into **[VERIFIED]** constants that already exist
on disk in `aee/installer/backend.py` and `aee/cli.py` (and are re-exported
from `aee/installer/__init__.py`), and **[PROPOSAL]** new codes for the
bootstrap v1 surface. The two sets MUST NOT collide. The existing verified
constants occupy `{0, 2, 3, 4, 5, 6}`; bootstrap v1 proposes new codes in the
free range `{7, 8, 9, 10, 11, 12}` and reserves `{64..127}`.

**Verified constants (do NOT renumber — tests pin these values):**

| Code | Constant | Module | Meaning |
| --- | --- | --- | --- |
| 0 | `EXIT_OK` | `aee/installer/backend.py:59`, `aee/cli.py` | Success / dry-run plan + pre-flight passed. |
| 2 | `EXIT_PARSE_ERROR` | `aee/cli.py` | Argument parsing failure (argparse / shell usage). |
| 3 | `EXIT_PROFILE_INVALID` / `EXIT_PROFILE_ERROR` | `aee/installer/backend.py:60`, `aee/cli.py` (as `EXIT_PROFILE_ERROR`) | Unknown profile (defence in depth). |
| 4 | `EXIT_PRE_FLIGHT_FAILED` | `aee/installer/backend.py:61` | Pre-flight checks failed (e.g. repo root missing). |
| 5 | `EXIT_PROFILE_SWITCH_REJECTED` | `aee/installer/backend.py:62`, `aee/cli.py:74` | Existing install with a different profile — operator must uninstall + reinstall. |
| 6 | `EXIT_EXECUTE_NOT_AUTHORIZED` | `aee/installer/backend.py:63` | `execute(dry_run=False)` invoked in a slice that has not authorized side effects (the shell wrapper's `--execute` guard returns this). |

[VERIFIED — `python3 -c "import sys; sys.path.insert(0,'.'); from aee.installer import backend as b; print([(n, getattr(b, n)) for n in dir(b) if n.startswith('EXIT_')])"` returns `EXIT_OK=0, EXIT_PROFILE_INVALID=3, EXIT_PRE_FLIGHT_FAILED=4, EXIT_PROFILE_SWITCH_REJECTED=5, EXIT_EXECUTE_NOT_AUTHORIZED=6`; this matches `aee/cli.py` `__all__` re-export of `EXIT_OK, EXIT_PARSE_ERROR, EXIT_PROFILE_ERROR, EXIT_PRE_FLIGHT_FAILED, EXIT_PROFILE_SWITCH_REJECTED`; `aee/tests/test_aee93_installer_backend.py:546` pins `assertEqual(EXIT_PROFILE_SWITCH_REJECTED, 5)`.]

**Proposed new codes for the bootstrap v1 surface (must not collide with the verified set above):**

| Code | Proposed constant | Used by | Meaning |
| --- | --- | --- | --- |
| 7 | `EXIT_STAGE_FAILED_RETRYABLE` (proposed) | `aee install` | A bootstrap stage failed but is retryable (re-run with `--resume`). |
| 8 | `EXIT_STAGE_FAILED_PERMANENT` (proposed) | `aee install` | A bootstrap stage failed permanently (max retries exceeded); requires `--force-retry` or operator intervention. |
| 9 | `EXIT_DRIFT_DETECTED` (proposed) | `aee doctor` only | On-disk state drifted from the recorded pin (`commit_sha` or `requirements_lock_sha256` mismatch). |
| 10 | `EXIT_NETWORK_ERROR` (proposed) | `aee install`, `aee update` | Network/git error (clone, fetch, package mirror unreachable). |
| 11 | `EXIT_SECRET_MISSING` (proposed) | `aee install`, `aee doctor` | A required secret is missing or invalid. |
| 12 | `EXIT_DEPENDENCY_FLOOR_NOT_MET` (proposed) | `aee install` | A hard dependency floor (git, python, node) is not met and cannot be auto-installed. |
| 64–127 | (reserved) | — | Reserved for future use. |

[PROPOSAL — these codes are net-new and do NOT alter the verified constants
above. Code 5 stays `EXIT_PROFILE_SWITCH_REJECTED` (verified); the prior
draft of this spec that assigned 5 to "stage failed retryable" and 7 to
"profile switch rejected" was a conflict with the verified backend
constants and has been corrected here.]

**Conflict-resolution notes (audit trail):**

* The prior draft assigned 5 to "Stage failed (retryable)" — this collided
  with the verified `EXIT_PROFILE_SWITCH_REJECTED = 5`. Corrected: 5 stays
  the profile-switch-rejected code; stage-failed-retryable moves to 7.
* The prior draft assigned 7 to "Profile switch rejected" and claimed it
  aligned with an existing backend constant — no constant 7 exists in
  `aee/installer/backend.py` or `aee/cli.py`. Corrected: profile switch
  rejected is documented at its verified value 5; 7 is now a proposed
  new code for a different meaning.
* The prior draft assigned 6 to "Stage failed permanently" — 6 is the
  verified `EXIT_EXECUTE_NOT_AUTHORIZED`. Corrected: 6 stays the
  execute-not-authorized code; stage-failed-permanent moves to 8.
* The prior draft assigned 8 to "Drift detected" and 10 to "Secret
  missing or invalid" — these did not collide but are renumbered here
  only to fit the contiguous proposed block `{7..12}`. The shift from
  `8 → 9` (drift) and `10 → 11` (secret) is intentional to make the
  proposed block self-consistent; tests pinning these codes do not yet
  exist (they are [PROPOSAL]).
* `aee/installer/backend.py` does NOT export an `EXIT_GENERIC_FAILURE = 1`
  constant; code 1 (generic failure / uncaught exception) is the Python
  default `sys.exit(1)` and is documented as such, not as a named
  constant. [VERIFIED — `dir(aee.installer.backend)` shows no
  `EXIT_GENERIC_FAILURE`.]

### 10.5 Machine-Readable JSON

`--log-format json` emits one JSON object per line (NDJSON) to stdout.
Each line has:

```json
{
  "ts": "2026-07-25T08:00:00Z",
  "stage": "03_pin",
  "level": "info|warn|error",
  "event": "stage_started|stage_completed|stage_failed|...",
  "data": { ... },
  "run_id": "<uuid>"
}
```

The final line for `aee doctor --json` is a single object with the full
result document (not NDJSON) so it can be parsed in one call. [PROPOSAL]

---

## 11. Health Checks

`aee doctor` runs the following checks in order. Each check returns
`PASS`, `WARN`, `FAIL`, or `SKIP` (with a reason). The aggregate exit
code follows §10.4.

| # | Check | Description | Fail action |
| --- | --- | --- | --- |
| H1 | Runtime | The AEE Runtime process is reachable (IPC or HTTP health endpoint). For Node runtime: `aee-runtime/runtime/aee_runtime.py` responds to `/healthz`. | Report FAIL with last-known status. |
| H2 | Hermes (optional) | If `AEE_AGENT_BACKEND=hermes`, the Hermes gateway is reachable on its port (default 8642). | WARN if not configured, FAIL if configured but unreachable. |
| H3 | Platform Adapter | `resolve_platform_identity()` returns a non-UNKNOWN identity for the host. On Windows pre-adapter, returns UNKNOWN → WARN with pointer to §13.4. | WARN. |
| H4 | Network | Outbound reachability to the git remote and the package-manager mirror. In `--offline`, replaced by a check that the offline bundle is complete. | FAIL on missing network. |
| H5 | Git | `git --version` ≥ floor; `git -C <install> rev-parse HEAD` matches `03_pin` marker. | FAIL on version, WARN on drift (exit 9, `EXIT_DRIFT_DETECTED` proposed). |
| H6 | Python | `python --version` ≥ floor; `uv pip list` matches locked set. | FAIL on version, WARN on drift. |
| H7 | Telegram (optional) | If `AEE_NOTIFY_TELEGRAM=*** ` configured, a no-op message (or `getMe`) succeeds. | WARN if configured and failing, SKIP if not configured. |
| H8 | Disk | At least 1 GB free on the install path's filesystem. | FAIL if below. |
| H9 | Permissions | Install path is writable by the install owner; env file is mode 0600. | FAIL if permissions drift. |
| H10 | Profile consistency | `.aee-profile` marker matches the requested profile and the installed capability set. | FAIL on mismatch (matches existing `ProfileSwitchRejectedError` semantics). |

[VERIFIED basis — `aee/deploy/adapters/base.py` PlatformAdapter has
`health_check(profile) → HealthStatus`; the macOS and Abacus adapters
already implement health checks returning structured status]. [PROPOSAL
for H1–H10 as a unified `aee doctor` surface].

---

## 12. Logging, Diagnostics, Telemetry, Evidence Collection

### 12.1 Log Location

* Per-user install: `~/.cache/aee/logs/bootstrap-<run_id>.log` (POSIX)
  or `%LOCALAPPDATA%\AEE\logs\bootstrap-<run_id>.log` (Windows).
* System install: `/var/log/aee/bootstrap-<run_id>.log` (POSIX).
* CI install: `$AEE_CI_LOG_DIR/bootstrap-<run_id>.log`.

[PROPOSAL]

### 12.2 Telemetry Policy

**Default: no telemetry.** The bootstrap does not phone home, send
usage stats, or report errors to any external service. The only
outbound traffic is to the git remote, the package-manager mirror,
and (optionally, §11 H7) Telegram. [PROPOSAL]

If telemetry is ever added in a future version, it MUST be opt-in via
`AEE_TELEMETRY_CONSENT=*** ` and documented in the release notes; v1
will not implement it. [PROPOSAL]

### 12.3 Diagnostics Bundle

`aee doctor --bundle <path>` produces a tarball containing:

* The bootstrap log for the latest run.
* All stage markers.
* `git status --porcelain`, `git log -1 --format=...` output.
* `aee doctor --json` output.
* Redacted env file (keys present, values redacted).
* Platform adapter `detect()` output.

The bundle is intended for sharing with maintainers during
troubleshooting; all secret material is redacted per §8.2 before
archiving. [PROPOSAL]

### 12.4 Evidence Collection (CI)

In `--ci` mode, the bootstrap writes a final evidence JSON at
`$AEE_CI_LOG_DIR/evidence.json`:

```json
{
  "run_id": "<uuid>",
  "channel": "stable",
  "ref": "v1.0.0",
  "commit_sha": "<40-char>",
  "profile": "mini",
  "platform": "linux",
  "stages": [
    {"name": "00_detect", "state": "completed", "duration_ms": 12},
    ...
  ],
  "doctor": { ... },
  "exit_code": 0
}
```

This is the artifact consumed by the test harness in §14. [PROPOSAL]

---

## 13. Platform-Specific Details and Known Limitations

### 13.1 Ubuntu Linux

* **Supervisor**: `apt install supervisor`; the existing
  `aee-runtime/supervisor/aee-runtime.conf` is the reference unit.
  [VERIFIED].
* **Persistent path**: `/home/ubuntu` is persistent across container
  resets on Abacus.ai. [VERIFIED — memory note + Abacus adapter
  docstring].
* **Limitation**: Ubuntu 22.04 ships Python 3.10; the bootstrap
  installs `python3.11` from deadsnakes PPA. Operators who cannot add
  PPAs must use `--offline` with a pre-staged Python 3.11. [PROPOSAL]

### 13.2 Debian Linux

* **Supervisor**: same as Ubuntu.
* **Limitation**: Debian stable may lag on Python; the bootstrap uses
  the same deadsnakes-equivalent or `pyenv` fallback. [PROPOSAL]
* **Difference from Ubuntu**: No deadsnakes PPA on Debian; the
  bootstrap builds Python 3.11 from source via `pyenv` if the system
  Python is below floor. This is slow (5–10 min) and is called out in
  the install log. [PROPOSAL]

### 13.3 macOS

* **Supervisor**: `launchd` via `~/Library/LaunchAgents/`.
  [VERIFIED — `aee/deploy/adapters/macbook.py` uses `launchd`].
* **Profile restriction**: macOS adapter accepts only `developer`
  profile. [VERIFIED — `aee/deploy/adapters/macbook.py` docstring].
* **Limitation**: No `sudo` required for default install. [PROPOSAL]
* **Limitation**: Homebrew install requires sudo for the first
  `/opt/homebrew` setup; the bootstrap documents this and offers a
  `--no-brew` mode that uses `python.org` installers directly.
  [PROPOSAL]
* **Limitation**: Apple Silicon vs Intel: Homebrew prefix differs
  (`/opt/homebrew` vs `/usr/local`). The bootstrap detects the prefix
  via `brew --prefix` and does not hardcode it. [PROPOSAL]

### 13.4 Windows

* **Shell**: PowerShell 5.1 (built into Windows 11) or PowerShell 7+
  (preferred; install via winget if missing).
* **Supervisor**: Windows Service (system scope) or scheduled task
  (user scope). The bootstrap writes a service manifest via `sc.exe`
  or registers a scheduled task via `schtasks`. [PROPOSAL]
* **Paths**: Install root `%LOCALAPPDATA%\AEE` (user) or
  `C:\Program Files\AEE` (system). Logs in `%LOCALAPPDATA%\AEE\logs`.
  [PROPOSAL]
* **Status**: First-class Windows support is **not yet verified**.
  The existing `PlatformIdentity` enum returns `UNKNOWN` for `win32`.
  [VERIFIED — `aee/tests/test_platform_bootstrap.py:119`]. A
  `WindowsAdapter` (§3, `aee/deploy/adapters/windows.py`) is required
  before Windows can graduate from "bootstrap-only, runs in UNKNOWN
  capability set" to "first-class supported". The spec defines the
  contract; implementation is a separate work order (§16). [PROPOSAL]
* **Limitation**: PowerShell execution policy may block
  `install.ps1`. The bootstrap documents `Set-ExecutionPolicy
  -Scope Process Bypass` as the per-session workaround. [PROPOSAL]
* **Limitation**: Long path support (>260 chars) must be enabled
  system-wide; the bootstrap warns if not. [PROPOSAL]
* **Limitation**: WSL is not a supported target in v1 (WSL installs
  should use the Ubuntu bootstrap path). [PROPOSAL]

---

## 14. Testing Strategy

### 14.1 Unit Tests

* Platform detection: extend `aee/tests/test_platform_bootstrap.py`
  with `WINDOWS` identity cases; cover `win32`, `cygwin`, `msys`.
  [VERIFIED — file exists; PROPOSAL for the new cases].
* Installer backend: the existing `aee.installer.backend` has dry-run
  planning tests; add tests for `--resume`, `--from`, `--rollback-to`,
  marker file read/write, and the 3-retry-max rule. [PROPOSAL]
* CLI: argument parsing, exit-code mapping, NDJSON formatting. [PROPOSAL]

### 14.2 Integration Tests

* Stage transitions: run stages 00–07 in a temp directory with a fake
  remote (local file:// git remote). Assert markers are written in
  order and resume skips completed stages. [PROPOSAL]
* Doctor: run `aee doctor --json` on a known-good install and assert
  the JSON shape; run on a drifted install and assert exit 9
  (`EXIT_DRIFT_DETECTED`, proposed — see §10.4). [PROPOSAL]
* Secret redaction: feed a known secret into a failing stage and
  assert the marker's `stderr_tail` is redacted. [PROPOSAL]

### 14.3 Container / VM Tests

* **Ubuntu container**: Docker image `ubuntu:22.04` and `ubuntu:24.04`
  with `aee install --ci --profile mini --dry-run` (no side effects).
  Full execute run in a disposable container with `--system` declined.
  [PROPOSAL]
* **Debian container**: `debian:12` equivalent.
* **macOS VM**: A GitHub Actions macOS runner (or self-hosted Mac mini)
  runs the bootstrap and `aee doctor`. This is the slowest tier and
  runs on a reduced cadence. [PROPOSAL]
* **Windows VM**: A GitHub Actions `windows-latest` runner. Marked
  experimental until the Windows adapter (§13.4) is implemented.
  [PROPOSAL]

### 14.4 Clean-Machine E2E

The canonical acceptance test: spin up a fresh cloud VM (or Docker
container with no AEE preinstalled), run the one-liner, and assert the
final marker `AGENT_READY` exists and `aee doctor` exits 0. [PROPOSAL]

### 14.5 Upgrade Test

* Install v1.0.0, then `aee update --channel stable` to advance to a
  newer tag. Assert the `03_pin` marker updates and `aee doctor` is
  still green. [PROPOSAL]
* Channel switch (`stable → rc → dev → stable`) without profile change
  must succeed; with profile change must be rejected. [PROPOSAL]

### 14.6 Downgrade Test

* `aee update --ref <older-tag>` downgrades. The bootstrap does NOT
  auto-rollback dependencies (they are owned by the package manager);
  the doctor may WARN on dependency drift after a downgrade. [PROPOSAL]

### 14.7 Failure Injection

* Kill the network mid-`git fetch`: assert exit 10 (`EXIT_NETWORK_ERROR`,
  proposed), marker `state=failed`, `error_class=GitError`.
* Corrupt the venv mid-`runtime_setup`: assert exit 7
  (`EXIT_STAGE_FAILED_RETRYABLE`, proposed), retry succeeds.
* Fill the disk mid-clone: assert exit 7 (`EXIT_STAGE_FAILED_RETRYABLE`,
  proposed), H8 reports FAIL.
* Remove a required secret: assert exit 11 (`EXIT_SECRET_MISSING`,
  proposed).

[PROPOSAL — exit code numbers reference the corrected table in §10.4.]

### 14.8 Regression

* The existing test suites (`aee/tests/`, `tests/`) must remain green
  after each work order. New tests live in `aee/tests/test_installer_*`
  and `aee/tests/test_bootstrap_*` and do not modify existing files.
  [PROPOSAL — matches the byte-identical baseline pattern used in
  prior AEE work, e.g. "12 production files byte-identical"].

---

## 15. Acceptance Criteria

Three named acceptance tracks. Each has explicit pass conditions.

### 15.1 Reproducible Deployment

PASS when, for the same `(channel, ref, commit_sha,
requirements_lock_sha256)` tuple:

1. Two independent clean-machine E2E runs produce byte-identical
   `evidence.json` modulo `run_id` and timestamps.
2. `aee doctor --reproducibility` exits 0 on both.
3. The installed venv's `uv pip list --format json` matches the
   recorded baseline.

[PROPOSAL]

### 15.2 One-click Bootstrap

PASS when:

1. A single command (`curl ... | bash` on POSIX,
   `irm ... | iex` on Windows — exact URL to be defined by release
   engineering) on a clean machine produces `AGENT_READY`.
2. The command requires no interactive prompts in `--yes` mode.
3. Total wall-clock time on a reference host (Ubuntu 22.04 container
   with 2 vCPU / 4 GB RAM) is under 5 minutes excluding network.
4. `aee doctor` exits 0 immediately after.

[PROPOSAL]

### 15.3 Automated Agent Deployment

PASS when:

1. The same bootstrap command works in CI (`--ci` mode) on all four
   target platforms (Windows marked experimental until §13.4 lands).
2. `evidence.json` is produced and consumable by the test harness.
3. A failure in any stage produces a non-zero exit code and a
   diagnostics bundle path.

[PROPOSAL]

---

## 16. Work Breakdown (One Task, One Deliverable)

Each item is an independent implementation / review / commit task
respecting the existing AEE K-shape (single small atomic commit)
pattern. Order is the recommended phased delivery order (§17.4).

| # | Task | Deliverable | Touches |
| --- | --- | --- | --- |
| W1 | Add `WINDOWS` to `PlatformIdentity` + `WindowsAdapter` skeleton | `aee/platform/current.py`, `aee/deploy/adapters/windows.py`, `aee/tests/test_platform_bootstrap.py` | new files + enum extend |
| W2 | Stage marker library (`aee.installer.lifecycle`) | `aee/installer/lifecycle.py`, `aee/tests/test_installer_lifecycle.py` | new files only |
| W3 | `aee doctor` CLI + health checks H1–H10 | `aee/installer/doctor.py`, `aee/tests/test_installer_doctor.py` | new files only |
| W4 | `aee install` CLI surface (wraps existing backend) | `aee/installer/cli.py`, `aee/tests/test_installer_cli.py` | new files only |
| W5 | `aee update` CLI surface | `aee/installer/update.py`, `aee/tests/test_installer_update.py` | new files only |
| W6 | `install.sh` POSIX trampoline — ASSESS/MIGRATE the existing tracked `install.sh` (commit 8a43494, Epic 9.3) rather than creating net-new | `install.sh` (modify/migrate OR formally supersede with a documented rationale), `bootstrap/lib/detect.sh`, `bootstrap/lib/deps.sh`, `bootstrap/lib/resume.sh` | may modify existing tracked `install.sh` + new files; assessment-first, parallel-installer-out-of-scope |
| W7 | `install.ps1` Windows trampoline | `install.ps1`, `bootstrap/lib/detect.ps1`, `bootstrap/lib/deps.ps1` | new files only |
| W8 | Dependency manifests (apt, brew, winget, python lock) | `bootstrap/manifests/*` | new files only |
| W9 | Release channel + ref pinning + drift detection | extend `aee/installer/backend.py` (additive), `aee/tests/test_installer_channels.py` | one modified + new |
| W10 | Integration tests (stage transitions, resume, redaction) + the shared redaction module itself (per §8.2 [PROPOSAL] — no reusable regex exists in `aee/artifacts/policy.py`) | `aee/installer/redaction.py` (new — proposed), `aee/tests/test_bootstrap_integration.py` | new files only |
| W11 | Container E2E harness (Ubuntu, Debian) | `tests/e2e/ubuntu.sh`, `tests/e2e/debian.sh` | new files only |
| W12 | macOS E2E (CI runner) | `tests/e2e/macos.sh` | new files only |
| W13 | Windows E2E (experimental) | `tests/e2e/windows.ps1` | new files only |
| W14 | Docs: operator guide, troubleshooting, offline bundle | `docs/aee/bootstrap/*.md` | new files only |
| W15 | Acceptance gate: Reproducible Deployment + One-click + Automated | `tests/acceptance/bootstrap_v1_acceptance.py` | new files only |

Each task produces its own review + atomic commit; no task modifies
another task's deliverable. [PROPOSAL — matches existing AEE pattern
documented in `~/.hermes/skills/software-development/aee-iteration-pattern/`].

---

## 17. Risks, Open Questions, Phased Delivery Order

### 17.1 Risks

| # | Risk | Mitigation |
| --- | --- | --- |
| R1 | Windows adapter never lands → Windows stays on UNKNOWN capabilities. | Scope W1 first; ship v1 with Windows as experimental, documented in §13.4. |
| R2 | Python 3.11 unavailable on Ubuntu 22.04 without deadsnakes PPA. | Offline bundle path (§7.5) plus pyenv fallback (§13.2). |
| R3 | Homebrew first-install requires sudo and is slow. | `--no-brew` mode uses python.org installers; documented in §13.3. |
| R4 | Git `--filter=blob:none` fails on some proxies. | Detect and fall back to full clone; warn in log. |
| R5 | Secret redaction regex set needs to be defined and kept consistent across shell, PowerShell, and Python layers. | A shared redaction module is [PROPOSAL] (§8.2) — there is NO reusable regex in `aee/artifacts/policy.py` (verified empty). Implement the shared module in a work order; add a regression test (W10). Do NOT cite `aee/artifacts/policy.py` as a verified source of redaction logic. |
| R6 | Long-path issue on Windows truncates install path. | Document §13.4; default to `%LOCALAPPDATA%\AEE` (short). |
| R7 | Container reset on Abacus wipes `/tmp` and non-`/home/ubuntu` paths. | All markers and logs live under the install path (persistent on Abacus). [VERIFIED basis — memory note]. |
| R8 | Profile switch rejection blocks legitimate channel upgrades that imply a profile change. | Document that profile change requires uninstall + reinstall; do not silently switch. |
| R9 | `aee doctor` false-positive drift after a legitimate `git pull` outside the bootstrap. | `aee update` is the only blessed path; doctor drift is real and must be resolved by re-pinning. |
| R10 | macOS launchd user-scope not restartable by `launchctl bootstrap` on older macOS. | Floor macOS 13 (§1.4); document the workaround. |

### 17.2 Open Questions

1. **One-liner distribution URL**: who hosts `install.sh` / `install.ps1`
   for `curl|bash` / `irm|iex`? GitHub release asset, dedicated static
   site, or both? (Tracked; out of scope for this spec. Note: `install.sh`
   already exists in-repo and is tracked at HEAD — the hosting question is
   about the *distribution URL*, not the existence of the file.)
2. **`aee uninstall`**: deferred from v1 (§5.2); is a separate work order
   needed in v1.1?
3. **Telemetry**: v1 has none (§12.2); when is opt-in telemetry needed?
4. **WSL support**: explicitly out of scope for v1 (§13.4); reconsider in
   v1.1.
5. **Multi-instance**: same host, multiple install paths — supported
   via per-path markers (§5.5); needs an E2E test (W10/W11) to confirm.
6. **Signed installers**: should `install.sh` / `install.ps1` be
   cosigned or have a sha256 in the `curl|bash` snippet? (Recommended;
   tracked. `install.sh` already exists and is tracked; this question is
   about signing/distribution, not authorship.)

### 17.3 Phased Delivery Order

The work items in §16 are ordered to land first-class support on the
platforms that already have adapters (Ubuntu, Debian, macOS) before
Windows.

* **Phase A — Core (W1, W2, W3, W4, W5)**: platform identity extension,
  stage lifecycle, doctor, install CLI, update CLI. After Phase A, the
  Python backend is complete and runnable on Linux/macOS via
  `python -m aee.installer.cli`.
* **Phase B — POSIX bootstrap (W6, W8, W10, W11, W12)**: shell
  trampoline, manifests, integration tests, container/macOS E2E. After
  Phase B, One-click Bootstrap (§15.2) passes on Ubuntu, Debian, macOS.
* **Phase C — Windows (W7, W13)**: Windows trampoline + E2E
  (experimental). After Phase C, Windows runs in UNKNOWN capability
  mode; first-class support waits on the Windows adapter (W1 already
  shipped in Phase A as a skeleton).
* **Phase D — Hardening (W9, W14, W15)**: release channels, docs,
  acceptance gate. After Phase D, Reproducible Deployment (§15.1) and
  Automated Agent Deployment (§15.3) pass.

[PROPOSAL]

---

## 18. Production Safety Constraints

1. **No automatic deploy.** The bootstrap installs and brings the Agent
   to "ready"; it does NOT deploy to production. Production deployment
   is a separate operator action with explicit authorization.
2. **No automatic restart.** `aee doctor` reports runtime health but
   does NOT restart the runtime. A restart requires `aee restart`
   (out of scope for v1) or the operator's process supervisor.
3. **Dry-run by default.** `aee install` without `--execute` (or with
   `--dry-run`) only plans and pre-flights; it performs no side
   effects. [VERIFIED basis — `aee/installer/__init__.py` documents
   dry-run default].
4. **No force over an existing install.** Profile switch is rejected
   (§2.4). Hard `git reset` requires `--force-reset` (§9.3).
5. **No secret material in commits.** §8.1; enforced by the redaction
   filter (§8.2) and a pre-commit hook (recommended; tracked).
6. **Read-only doctor.** `aee doctor` performs no mutations; it only
   reads state and reports.
7. **No telemetry without consent.** §12.2.

[PROPOSAL — basis in existing backend's dry-run default and profile
switch rejection is VERIFIED]

---

## 19. Repository Inspection Evidence

All evidence collected read-only at HEAD `31ce4678d8f233e4c1bda9eadd35459d3254b849`.

### 19.1 Branch and HEAD

* Branch: `main`. [VERIFIED — `git rev-parse --abbrev-ref HEAD`]
* HEAD: `31ce4678d8f233e4c1bda9eadd35459d3254b849`. [VERIFIED —
  `git rev-parse HEAD`]
* Recent commits:
  * `31ce467 test: isolate AEE v3 telegram/blocking gate tests from run task mapping`
  * `30ef534 feat(platform): add platform adapter phase 1 — capabilities + bootstrap`
  * `6e5d424 feat(runtime): guarantee terminal outcome notifications`
  * `f0046b5 Brand runtime bridge as AEE Runtime Bridge`
  * `ef36524 fix(dispatcher): preserve case in expected_artifacts persistence`

[VERIFIED — `git log --oneline -5`]

### 19.2 Working Tree Status

* One modified tracked file: `.gitignore` (the known pre-existing dirty
  marker, +13/-2). [VERIFIED — `git diff --stat`]
* Large set of untracked report/manifest markdown files at repo root
  (AEE-7/8/9 reports, K3 reports, etc.) and the untracked `reports/`
  directory tree. [VERIFIED — `git ls-files --others --exclude-standard`]
* No source files under `aee/` or `dispatcher/` are modified. The
  production code matches the committed HEAD. [VERIFIED — the diff
  summary shows only `.gitignore`]

### 19.3 Existing Substrate Reused

| Component | Path | Status |
| --- | --- | --- |
| Platform Adapter Protocol | `aee/deploy/adapters/base.py` | [VERIFIED] present |
| Reference adapters | `aee/deploy/adapters/{abacus,macbook,docker,terraform_aws,zo}.py` | [VERIFIED] present |
| Capability contract | `aee/deploy/contract.py` | [VERIFIED] present |
| YAML loader | `aee/deploy/loader.py` | [VERIFIED] present |
| Adapter registry | `aee/deploy/registry.py` | [VERIFIED] present |
| Bootstrap resolver | `aee/platform/current.py` | [VERIFIED] present |
| Profile descriptor | `aee/profiles/descriptor.py` | [VERIFIED] present; `KNOWN_PROFILES = ("full","mini","edge","developer")` |
| Installer backend | `aee/installer/backend.py` | [VERIFIED] present; dry-run default; `ProfileSwitchRejectedError` |
| AEE Runtime (Node) | `aee-runtime/runtime/` | [VERIFIED] present; Node-based worker runtime |
| Supervisor / systemd units | `aee-runtime/supervisor/`, `aee-runtime/systemd/` | [VERIFIED] present |

### 19.4 Components Not Present (Net-New in this Spec)

| Component | Path | Status |
| --- | --- | --- |
| Windows platform identity | `PlatformIdentity.WINDOWS` | [NOT PRESENT] — `win32` maps to `UNKNOWN` |
| Windows adapter | `aee/deploy/adapters/windows.py` | [NOT PRESENT] |
| Debian adapter | `aee/deploy/adapters/debian.py` | [NOT PRESENT] |
| Ubuntu VM adapter | `aee/deploy/adapters/ubuntu_vm.py` | [NOT PRESENT] |
| Stage lifecycle | `aee/installer/lifecycle.py` | [NOT PRESENT] |
| Doctor CLI | `aee/installer/doctor.py` | [NOT PRESENT] |
| Install CLI | `aee/installer/cli.py` | [NOT PRESENT] |
| Update CLI | `aee/installer/update.py` | [NOT PRESENT] |
| POSIX installer | `install.sh`, `bootstrap/lib/*.sh` | `install.sh` [VERIFIED — PRESENT, tracked at HEAD, added by commit 8a43494]; `bootstrap/lib/*.sh` [NOT PRESENT] |
| Windows installer | `install.ps1`, `bootstrap/lib/*.ps1` | [NOT PRESENT] |
| Manifests | `bootstrap/manifests/*` | [NOT PRESENT] |
| Bootstrap spec | `reports/aee_bootstrap_v1_spec.md` | [PRESENT — this document, created in this task] |

---

## 20. Artifact Verification

### 20.1 Commands

```sh
ls -la /home/ubuntu/hermes-runtime-bridge/reports/aee_bootstrap_v1_spec.md
wc -l /home/ubuntu/hermes-runtime-bridge/reports/aee_bootstrap_v1_spec.md
sha256sum /home/ubuntu/hermes-runtime-bridge/reports/aee_bootstrap_v1_spec.md
```

### 20.2 Required Headings

The artifact must contain all 18 sections requested by the task brief
plus this verification section. The section list:

1. Goals, Non-Goals, Assumptions, Platform/Version Policy (§1)
2. Cross-Platform Architecture (§2)
3. Repository Layout (§3)
4. Bootstrap Lifecycle (§4)
5. Idempotency, Rollback, Partial-Failure Recovery, Retry, Resume (§5)
6. Dependency Strategy (§6)
7. Install Modes (§7)
8. Secure Configuration and Secret Handling (§8)
9. Git Clone/Update/Version Pinning/Release Channels (§9)
10. CLI Contract (§10)
11. Health Checks (§11)
12. Logging, Diagnostics, Telemetry, Evidence Collection (§12)
13. Platform-Specific Details and Known Limitations (§13)
14. Testing Strategy (§14)
15. Acceptance Criteria (§15)
16. Work Breakdown (§16)
17. Risks, Open Questions, Phased Delivery Order (§17)
18. Production Safety Constraints (§18)
19. Repository Inspection Evidence (§19)
20. Artifact Verification (§20)

### 20.3 Verified Repository Facts vs Proposed Design

Throughout this document, assertions are tagged:

* `[VERIFIED]` — backed by read-only inspection of the repository at
  HEAD `31ce467`. Re-checkable via the commands in §19.
* `[PROPOSAL]` — a design decision with no current on-disk
  implementation. The bootstrap v1 work orders in §16 are responsible
  for landing each such proposal.

The document does NOT claim platform support that has not been
verified: Windows first-class support is explicitly marked as
dependent on a not-yet-implemented `WindowsAdapter` (§13.4).
