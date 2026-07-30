# AEE Runtime + Platform Adapter — Architecture and Migration Plan

> Rescue artifact for TASK-20260724-0202 (prior run `run_039916d6f0bf4b2ab1b8321f1c1b76a4` declared but never delivered the file).
> Author: M2 (Hermes Agent, Abacus.ai runtime). Date: 2026-07-24.
> Repository inspected: `/home/ubuntu/hermes-runtime-bridge` @ `f0046b51a80f05929182de453b8cc1de3be1725a` on `main`.
> This document is **planning only**. No source edits, commits, pushes, or deployments were performed in this task.

---

## 1. Executive Summary

The repository already contains a working **Platform Adapter** substrate introduced
by AEE Epic 9.6 (Master Plan §21.6): a `PlatformAdapter` Protocol, a
`HostCapabilities` contract, a `MaterializationResult` declarative output, four
reference adapters (`abacus`, `macbook`, `docker`, `terraform_aws`, `zo`), a
`HostCapabilities` YAML loader, and a class-based `AdapterRegistry`. This plan
proposes the **minimal** next step: lift the remaining platform-specific
assumptions (hardcoded `/home/ubuntu` paths, `subprocess` shelling out to
`supervisorctl`, git invocations) out of dispatcher hot paths and behind the
existing adapter interface, so that the same codebase runs unchanged on Ubuntu
(M2/Abacus) and macOS (B2/MacBook) with no source forks.

The plan is deliberately incremental. The first work order proposes **one**
primary deliverable — a `PlatformCapabilities` facade backed by the existing
`HostCapabilities` document — and the small set of dispatcher touch points that
consume it. Every later phase is gated on the previous phase's acceptance
criteria, and the existing adapters/tests are preserved byte-for-byte.

**Hard constraint**: Hermes remains an executor. The product identity is AEE
Runtime; `hermes-runtime-bridge` is the current host of that runtime, not the
identity. No changes in this plan alter that boundary.

---

## 2. Current-State Evidence (from the actual repository)

Captured at finalization time via `git`/`ls`/`grep` against the working tree
(see §14 Artifact Verification for the receipt commands).

### 2.1 Repository identity

| Field | Value |
| --- | --- |
| Top-level path | `/home/ubuntu/hermes-runtime-bridge` |
| Branch | `main` |
| HEAD | `f0046b51a80f05929182de453b8cc1de3be1725a` |
| `git remote -v` | `origin  git@github.com:sscomp/aee-runtime-bridge.git (fetch/push)` |
| Tracked files | 332 |
| Python modules (`*.py`, excluding `.venv`/`__pycache__`/`.git`) | 245 |

### 2.2 Working tree status (summary)

`git status --short` shows **one** modified tracked file (`.gitignore`, the
known pre-existing dirty marker) plus a large set of untracked report/manifest
markdown files at the repo root (AEE-7/8/9 reports, K3 reports, etc.) and the
untracked `reports/` directory tree. No source files under `aee/` or
`dispatcher/` are reported as modified — the production code matches the
committed HEAD `f0046b51`.

### 2.3 Existing Platform Adapter substrate (already shipped)

The repo already implements the §21.6 contract. The following files are
tracked and form the foundation this plan builds upon (not re-implements):

| Path | Role | Evidence |
| --- | --- | --- |
| `aee/deploy/contract.py` | `HostCapabilities`, `MaterializationResult`, `HealthStatus`, `ResourceFloor`, validators. §21.6.A–C. | `class HostCapabilities`, `validate_capabilities`, `RESOURCE_FLOOR_BY_PROFILE` |
| `aee/deploy/adapters/base.py` | `PlatformAdapter` Protocol (`@runtime_checkable`): `name`, `detect()`, `materialize(profile, cap)`, `health_check(profile)`. §21.6.D. | 4 methods, no inheritance |
| `aee/deploy/adapters/abacus.py` | Reference adapter for `class: container` (M2/Abacus). `supervisord`, cloudflared egress, `/home/ubuntu` persistent. | `class AbacusAdapter` |
| `aee/deploy/adapters/macbook.py` | Reference adapter for `class: laptop` (B2/MacBook). `launchd`, no tunnel, `profile=developer` only. Declines non-developer profiles. | `class MacBookAdapter` |
| `aee/deploy/adapters/docker.py` | Reference adapter for `class: docker-host`. `docker-compose`, all profiles, used for CI/sandbox. | `class DockerAdapter` |
| `aee/deploy/adapters/terraform_aws.py` | Placeholder for `class: cloud-vm` / `cloud-container`. | `class TerraformAwsAdapter` |
| `aee/deploy/adapters/zo.py` | Reference adapter for the N2/Zo host (profile=mini). | `class ZoAdapter` |
| `aee/deploy/registry.py` | `AdapterRegistry`, `select_adapter(host_class)`, `register_adapter`, default registry populated at import. §21.6.D/E. | `_build_default_registry()` |
| `aee/deploy/loader.py` | `load_host_capabilities(path)` YAML loader (stdlib mini-parser + PyYAML fallback). §21.6.B. | `_parse_block_lines` |
| `aee/deploy/samples/host.capabilities.*.yaml` | Reference Host Capability Documents for `m2-abacus`, `b2-macbook`, `docker-host`, `n2-zo`. | §2.4 below |

### 2.4 Host Capability Documents (reference samples)

- `host.capabilities.m2-abacus.yaml` — `class: container`, `os: linux`,
  `supervisor: supervisord`, `network_egress: tunnel`, `tunnel_kind: cloudflared`,
  `inbound_allowed: false`, persistent `/home/ubuntu`, supports
  `[full, mini, edge, developer]`, default `full`.
- `host.capabilities.b2-macbook.yaml` — `class: laptop`, `os: darwin`,
  `arch: arm64`, `supervisor: launchd`, `network_egress: direct`,
  `inbound_allowed: false`, persistent `/Users`, supports `[developer]` only.
- `host.capabilities.docker-host.yaml`, `host.capabilities.n2-zo.yaml` —
  additional reference documents.

### 2.5 Profile descriptor (canonical SOT)

`aee/profiles/descriptor.py` is the single source of truth for the four
profiles: `full`, `mini`, `edge`, `developer`. It is explicitly **read-only**:
no mutation, no `subprocess`/`os.environ` imports, default is `full`. Every
adapter and the Docker entrypoint import `parse_profile`/`get_descriptor`
from here — there is no parallel hard-coded profile matrix (per §21.3/§21.2).

### 2.6 Process supervision today

- **Abacus container (M2)**: `supervisord` (PID 7). The bridge runs as
  `[program:hermes-runtime-bridge]` in `supervisor/hermes-runtime-bridge.conf`,
  `command=.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8787`, `user=ubuntu`,
  `autorestart=true`. A separate `cloudflared-tunnel` program provides egress.
  `systemctl enable` does **not** work on this host (no user-systemd;
  `Failed to connect to bus: Host is down`).
- **Docker image**: `Dockerfile` (single image, `python:3.11-slim` base),
  `docker-entrypoint.sh` parses `--profile`, sets `AEE_PROFILE` /
  `AEE_DB_READ_ONLY` (edge) / `AEE_DB_PATH` (developer), and execs the
  command. No systemd inside the image.
- **macOS (B2)**: `launchd` (or `tmux` fallback) is expected. The
  `MacBookAdapter` declares `supervisor: launchd`. No launchd plist is
  shipped in the repo today (operator installs it).

### 2.7 Platform-specific assumptions still embedded in dispatcher/aee

Targeted `grep` of `dispatcher/` and `aee/` for hardcoded paths and
subprocess shelling surfaced the following hot spots (each is a candidate
for the adapter facade introduced in §6):

| File | Line(s) | Assumption |
| --- | --- | --- |
| `dispatcher/manager.py` | 104–109 | `subprocess.check_output(["git","rev-parse","HEAD"], cwd=cwd)` — assumes `git` on PATH and `cwd` is the repo |
| `dispatcher/safety.py` | 15, 233, 253 | Path-prefix allowlist defaults to `/home/ubuntu/`, `/tmp/`, `/opt/` |
| `dispatcher/notifier.py` | 390 | `subprocess.run([...])` for the notifier command (Telegram send) |
| `dispatcher/routing.py` | 32–33 | Doc references to `/home/ubuntu/Abacus/...md` (docstring only, not branching) |
| `dispatcher/db.py` | 405–406 | Docstring warning about case-sensitive `/home/ubuntu` paths |
| `aee/installer/backend.py` | (module) | `dry_run=True` default; `execute(dry_run=False)` not yet authorized. No `subprocess` import. |
| `aee/cli.py` | (module) | Unified CLI; profile flag plumbing |
| `Dockerfile` / `docker-entrypoint.sh` | — | Already profile-aware; no Linux/macOS branching needed |
| `supervisor/hermes-runtime-bridge.conf` | — | Hardcoded `/home/ubuntu/hermes-runtime-bridge/.venv/bin/uvicorn` and `--env-file` path |

These are the **only** platform-coupled sites worth touching in this plan.
The §21.6 adapter layer already abstracts process supervision, tunnels, and
resource floors — those are not re-implemented here.

---

## 3. Target Architecture: AEE Runtime Core + Platform Adapter

```
+---------------------------------------------------------------+
|                      AEE Runtime Core                          |
|  (dispatcher, aee/orchestrator, aee/artifacts, aee/audit,      |
|   aee/api, aee/profiles/descriptor, aee/ci, aee/reporting)     |
|                                                               |
|  Depends only on PlatformCapabilities facade + contract data. |
|  NO imports of subprocess/supervisorctl/launchctl from core.   |
+--------------------------+------------------------------------+
                           | PlatformCapabilities (facade)
                           |  - host_root / persistent_paths
                           |  - supervisor_kind
                           |  - shell_runner
                           |  - package_runner
                           |  - git_runner
                           |  - service_manager
                           |  - health_probe
                           |  - network_egress_kind
+--------------------------v------------------------------------+
|                Platform Adapter Layer (§21.6)                  |
|  PlatformAdapter Protocol + AdapterRegistry + contract data   |
|  (aee/deploy/adapters/*, aee/deploy/contract.py, registry.py)  |
+----------+-------------+-------------+-------------+----------+
| Abacus   | MacBook      | Docker       | Terraform   | Zo      |
| (Linux)  | (macOS)      | (CI/sandbox) | (cloud-vm)  | (N2)    |
+----------+-------------+-------------+-------------+----------+
```

Principles (carry forward from §21.6):

1. **AEE Core never imports cloud SDKs or `subprocess` directly.** All
   host-native tooling goes through the adapter.
2. **Selection uses `class`, not `provider_hint`.** `provider_hint` is
   informational only.
3. **Adapters are declarative.** `materialize()` returns what the host
   *should* keep alive, not what the adapter started. Side effects stay
   with the host's process supervisor (supervisord/launchd/docker).
4. **Default is read-only / dry-run.** Adapter `detect()` never mutates;
   `materialize()` only describes; `health_check()` only probes.
5. **Hermes is an executor.** The product identity is AEE Runtime; the
   bridge is the current host process, not the contract.

---

## 4. Responsibilities and Boundaries

### 4.1 AEE Runtime Core owns

- Task lifecycle (dispatcher `manager.py`, `db.py`, `executor_runs.py`).
- Orchestrator + provider protocols (`aee/orchestrator/`).
- Artifacts, audit, observability (`aee/artifacts/`, `aee/audit/`,
  `aee/observability*`).
- Profile descriptor (read-only) (`aee/profiles/descriptor.py`).
- CLI surface (`aee/cli.py`), API surface (`aee/api/`).
- CI matrix and reporting (`aee/ci/`, `aee/reporting/`).

### 4.2 Platform Adapter Layer owns

- `detect()` — probe host: os, arch, supervisor kind, persistent paths,
  egress kind, resource floor.
- `materialize(profile, cap)` — produce declarative supervisor units,
  ports, persistent paths, tunnel config (no side effects).
- `health_check(profile)` — liveness of the running deployment.
- `PlatformCapabilities` facade (new in this plan) — runtime read API
  over a loaded `HostCapabilities` document, consumed by Core.

### 4.3 Explicit non-responsibilities (the adapter MUST NOT)

- Branch on `provider_hint`.
- Mutate AEE Core, the Docker image, or the profile descriptor.
- Assume inbound connectivity is available (default egress-only).
- Fork the source per profile — profiles are runtime selections.
- Start processes directly (the host supervisor does that).

---

## 5. Proposed Repository / Module Layout (grounded in current files)

New modules are **additive** under `aee/deploy/`. Existing files are not
moved or renamed.

```
aee/
  deploy/                              # existing — §21.6 contract
    contract.py                        # existing — HostCapabilities, etc.
    loader.py                          # existing — YAML loader
    registry.py                        # existing — AdapterRegistry
    adapters/
      base.py                          # existing — PlatformAdapter Protocol
      abacus.py                        # existing — Linux/Abacus reference
      macbook.py                       # existing — macOS reference
      docker.py                        # existing — Docker host
      terraform_aws.py                  # existing — cloud-vm placeholder
      zo.py                            # existing — N2/Zo
    samples/
      host.capabilities.*.yaml         # existing — reference documents
    capabilities.py                    # NEW (Phase 1) — PlatformCapabilities facade
    runners.py                         # NEW (Phase 2) — ShellRunner/GitRunner/ServiceManager Protocol
  platform/                            # NEW (Phase 1) — thin bootstrap
    __init__.py                        #   resolve_adapter() → AdapterRegistry + loader
    current.py                         #   cached PlatformCapabilities for this process
  installer/
    backend.py                         # existing — dry-run installer (no subprocess)
  profiles/
    descriptor.py                      # existing — read-only SOT
dispatcher/                            # existing — hot path
  manager.py                           # TOUCH (Phase 2) — replace subprocess git with GitRunner
  safety.py                            # TOUCH (Phase 2) — read path prefixes from capabilities
  notifier.py                          # TOUCH (Phase 3) — route subprocess via ShellRunner (optional)
supervisor/
  hermes-runtime-bridge.conf           # existing — Linux host (supervisord)
Dockerfile                             # existing — profile-aware image
docker-entrypoint.sh                   # existing — profile parser
```

The new `aee/platform/` package is a **bootstrap** (resolve adapter once at
process start, cache the facade). It does not duplicate the registry; it
imports `aee.deploy.registry.get_registry()` and
`aee.deploy.loader.load_host_capabilities()`. This keeps §21.6 as the only
adapter registry and avoids a second source of truth.

---

## 6. Adapter Contracts / APIs (limited to justified areas)

### 6.1 `PlatformCapabilities` facade (Phase 1 — primary deliverable)

```python
# aee/deploy/capabilities.py (proposed, additive)
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Tuple
from aee.deploy.contract import HostCapabilities
from aee.deploy.adapters.base import PlatformAdapter


@dataclass(frozen=True)
class PlatformCapabilities:
    """Read-only runtime view over a HostCapabilities document + adapter.

    Consumed by dispatcher/aee core in place of hardcoded paths and
    platform checks. All fields are derived from the loaded
    HostCapabilities (declared) or the adapter's detect() (probed).
    """
    host_root: str                  # e.g. "/home/ubuntu" or "/Users/<user>"
    persistent_paths: Tuple[str, ...]
    supervisor_kind: str            # "supervisord" | "launchd" | "docker"
    network_egress_kind: str        # "tunnel" | "direct" | "none"
    os_name: str                    # "linux" | "darwin" | ...
    arch: str
    profile_supported: Tuple[str, ...]
    profile_default: str
    inbound_allowed: bool
    db_path_writable: bool
    tempdir_writable: bool

    @property
    def is_linux(self) -> bool: return self.os_name == "linux"
    @property
    def is_macos(self) -> bool: return self.os_name == "darwin"

    def resolve_path(self, p: str) -> str:
        """Resolve a ~/path or {host_root}/path to an absolute path."""
        ...
```

The facade is constructed from a `HostCapabilities` document (loaded from
`host.capabilities.<name>.yaml` or returned by `adapter.detect()`). It is
frozen and cached for the process lifetime. Core code reads it; it never
mutates.

### 6.2 Runner Protocols (Phase 2 — justified subset)

```python
# aee/deploy/runners.py (proposed, additive)
from typing import Protocol, runtime_checkable, Tuple


@runtime_checkable
class GitRunner(Protocol):
    """Read-only git access. Implementations may shell out or use a VCS API."""
    def rev_parse_head(self, cwd: str) -> str: ...
    def branch_name(self, cwd: str) -> str: ...
    def status_short(self, cwd: str) -> str: ...


@runtime_checkable
class ShellRunner(Protocol):
    """Host-native shell execution. Used by notifier/health_check only."""
    def run(self, cmd: Tuple[str, ...], *, cwd: Optional[str] = None,
            timeout: Optional[int] = None) -> "ShellResult": ...


@runtime_checkable
class ServiceManager(Protocol):
    """Read-only service liveness. Maps to supervisorctl / launchctl / docker."""
    def is_running(self, unit: str) -> bool: ...
    def list_units(self) -> Tuple[str, ...]: ...
```

Each Protocol has a default stdlib implementation (using `subprocess`)
registered for the Linux/Abacus adapter. The macOS adapter provides a
`launchctl`-based `ServiceManager` and a `ShellRunner` that uses
`/bin/zsh -c`. Docker adapter provides a `docker compose ps`-backed
`ServiceManager`. Adapters that do not support a capability raise
`NotImplementedError` rather than faking it.

### 6.3 Justified adapter areas (and what is NOT in scope)

| Area | In scope? | Justification |
| --- | --- | --- |
| Platform/capability detection | Yes | Already shipped (`detect()`); facade makes it consumable |
| Paths / filesystem | Yes | `host_root`, `persistent_paths` replace hardcoded `/home/ubuntu` |
| Process / shell | Yes (Phase 2) | `ShellRunner` abstracts `subprocess.run` in notifier/health |
| Service manager | Yes (Phase 2) | `ServiceManager.is_running` replaces direct `supervisorctl` |
| Package manager | **No** (deferred) | Installer backend is already dry-run; no `apt`/`brew` calls in core today |
| Networking / health | Yes (read-only) | `health_check()` already in Protocol; facade exposes `network_egress_kind` |

Package-manager abstraction is explicitly **out of scope** for this plan:
no core module shells out to `apt`/`brew`/`pip` today, and the installer
backend is dry-run by design. Adding it would be speculative.

---

## 7. Ubuntu / Linux Implementation Approach

The reference Linux adapter (`AbacusAdapter`, `aee/deploy/adapters/abacus.py`)
already matches the M2 host. Phase 1 wires it through the facade:

1. **Bootstrap** (`aee/platform/current.py`): on process start, load
   `aee/deploy/samples/host.capabilities.m2-abacus.yaml` (or probe via
   `AbacusAdapter().detect()` if no YAML is supplied). Build a
   `PlatformCapabilities` and cache it as a module-level singleton.
2. **Dispatcher** (`dispatcher/manager.py`): replace
   `subprocess.check_output(["git","rev-parse","HEAD"])` with
   `GitRunner.rev_parse_head(cwd)`. The default Linux `GitRunner`
   implementation still uses `subprocess` — the win is the seam, not the
   mechanism.
3. **Safety** (`dispatcher/safety.py`): read the path-prefix allowlist from
   `PlatformCapabilities.persistent_paths` plus the existing `/tmp` /
   `/opt` defaults. No behavioral change on Linux (the paths are the same).
4. **Supervisor** (`supervisor/hermes-runtime-bridge.conf`): unchanged.
   The adapter's `materialize(profile="full", cap)` already declares this
   unit; the file itself is host-specific and stays.

No new Linux-only code is added. The Linux path is the **default**; every
other adapter is a variation of it.

---

## 8. macOS Implementation Approach

The reference macOS adapter (`MacBookAdapter`,
`aee/deploy/adapters/macbook.py`) already exists and declines non-`developer`
profiles. Phase 1 wires it through the same facade:

1. **Bootstrap**: when `sys.platform == "darwin"`, the bootstrap probes
   via `MacBookAdapter().detect()` (or loads
   `host.capabilities.b2-macbook.yaml` if present). The cached
   `PlatformCapabilities` reports `host_root` = the operator home
   (e.g. `/Users/<user>`), `supervisor_kind="launchd"`,
   `network_egress_kind="direct"`, `profile_supported=("developer",)`.
2. **Dispatcher**: the same `GitRunner` seam works — the macOS
   implementation also shells out to `git` (same binary, same flags).
   No branching.
3. **Safety**: `persistent_paths` becomes `("/Users",)`; the allowlist
   adapts automatically.
4. **Service manager**: a `LaunchdServiceManager` implements
   `ServiceManager.is_running(unit)` via `launchctl list <label>`. A
   `tmux` fallback is provided for hosts without launchd (matching the
   adapter's documented fallback).
5. **No launchd plist shipped**: the adapter's `materialize()` returns the
   *declarative* label `com.biaobecue.aee.bridge`; the operator installs it.
   This matches the §21.6.E "informational only" contract.

The macOS path requires **no new source files** beyond what Phase 1/2
already add (the facade and the runner implementations). The
`MacBookAdapter` is unchanged.

---

## 9. Backward Compatibility Strategy

1. **No source removals.** All existing `subprocess` call sites remain
   until Phase 2 fully migrates them; migration is a swap, not a deletion.
2. **Facade default = current behavior.** When no `HostCapabilities`
   document is loaded, `PlatformCapabilities` falls back to a
   `LinuxDefaults` snapshot (`host_root="/home/ubuntu"`,
   `supervisor_kind="supervisord"`, ...) so M2 behavior is byte-identical
   before and after Phase 1.
3. **Adapter registry unchanged.** `_build_default_registry()` keeps the
   same five entries; new adapters are added via `register_adapter()`,
   not by editing the default registry.
4. **Profile descriptor untouched.** `aee/profiles/descriptor.py` is
   read-only and remains the SOT.
5. **Docker image untouched.** `Dockerfile` and `docker-entrypoint.sh`
   are already profile-aware; Phase 1/2 do not touch them.
6. **Supervisor conf untouched.** `supervisor/hermes-runtime-bridge.conf`
   stays as the Linux host unit file. macOS uses a launchd plist the
   operator installs (not in this plan's first work order).
7. **Tests pass byte-for-byte.** Every phase's acceptance gate includes
   "no existing test regresses" — see §11.

---

## 10. Phased Migration Plan (small reviewable phases)

Each phase is independently reviewable, committable, and revertable.

### Phase 1 — `PlatformCapabilities` facade + bootstrap (PRIMARY)

- New: `aee/deploy/capabilities.py`, `aee/platform/__init__.py`,
  `aee/platform/current.py`.
- New tests: `aee/tests/test_platform_capabilities.py`,
  `aee/tests/test_platform_bootstrap.py`.
- Touch points: **none** in production code (facade is additive; nothing
  consumes it yet). This is the safe first step — a new module that
  compiles, imports, and is verified, but no caller depends on it.
- Acceptance: see §11.1.

### Phase 2 — Migrate dispatcher git + path-prefix sites

- New: `aee/deploy/runners.py` (`GitRunner`, `ShellRunner`,
  `ServiceManager` Protocols + Linux/Abacus default implementations).
- Touch: `dispatcher/manager.py` (replace 2 `subprocess.check_output` calls
  with `GitRunner`), `dispatcher/safety.py` (read prefixes from
  `PlatformCapabilities`).
- New tests: `aee/tests/test_runners.py`,
  `aee/tests/test_dispatcher_uses_capabilities.py`.
- Acceptance: see §11.2.

### Phase 3 — Migrate notifier + health probe (optional)

- Touch: `dispatcher/notifier.py` (route `subprocess.run` through
  `ShellRunner`).
- New: optional `health_probe` adapter extension consumed by the keepalive
  cron pattern.
- Acceptance: see §11.3.

### Phase 4 — macOS runner implementations

- New: `aee/deploy/runners_launchd.py` (`LaunchdServiceManager`,
  macOS `ShellRunner`).
- New tests: macOS contract tests using mocks (no Mac host required).
- Acceptance: see §11.4.

### Phase 5 — (future, out of scope this plan) Installer shell layer

- `install.sh` shell wrapper (§21.3) consuming
  `InstallerBackend.execute(dry_run=False)` — separately authorizable.

---

## 11. Acceptance Criteria Per Phase

### 11.1 Phase 1

- `aee/deploy/capabilities.py` and `aee/platform/current.py` exist and
  import cleanly with stdlib only.
- `PlatformCapabilities.from_capabilities(HostCapabilities(...))` builds
  a frozen facade; `resolve_path("~/x")` returns `"<host_root>/x"`.
- Default fallback (`LinuxDefaults`) yields `host_root="/home/ubuntu"`,
  `supervisor_kind="supervisord"`.
- `aee/tests/test_platform_capabilities.py` PASS (≥ 10 cases).
- `aee/tests/test_platform_bootstrap.py` PASS (caching, idempotent
  resolution, fallback path).
- No existing test regresses (full `aee/tests` + `tests/` suites green).
- No production file modified (Phase 1 is purely additive).

### 11.2 Phase 2

- `dispatcher/manager.py` no longer calls `subprocess.check_output`
  directly for git; it goes through `GitRunner`.
- `dispatcher/safety.py` path-prefix allowlist sourced from
  `PlatformCapabilities.persistent_paths` (with the existing `/tmp` /
  `/opt` defaults preserved when capabilities don't list them).
- `aee/tests/test_runners.py` PASS (Linux `GitRunner`,
  `ShellRunner`, `ServiceManager`).
- `aee/tests/test_dispatcher_uses_capabilities.py` PASS (mock
  `PlatformCapabilities`, assert dispatcher uses facade values, not
  hardcoded `/home/ubuntu`).
- No existing test regresses.
- M2 behavior byte-identical: `git rev-parse HEAD` returns the same SHA
  before and after the swap.

### 11.3 Phase 3

- `dispatcher/notifier.py` routes through `ShellRunner`; the Linux
  implementation is unchanged behaviorally.
- Optional `health_probe` consumed by keepalive cron.
- No existing test regresses.

### 11.4 Phase 4

- `aee/deploy/runners_launchd.py` implements `ServiceManager` via
  `launchctl list` (mocked) and a macOS `ShellRunner` (`/bin/zsh -c`).
- macOS contract tests PASS using mocks (no Mac host required).
- `MacBookAdapter` byte-identical (not modified).
- Linux tests still PASS (no behavioral change on Linux).

---

## 12. Test Strategy

### 12.1 Linux tests (run on this host)

- **Unit**: `aee/tests/test_platform_capabilities.py`,
  `test_platform_bootstrap.py`, `test_runners.py`,
  `test_dispatcher_uses_capabilities.py` (all stdlib, no Mac required).
- **Contract**: assert `PlatformCapabilities.from_capabilities(
  AbacusAdapter().detect())` yields the expected M2 fields.
- **Regression**: full `aee/tests/` + `tests/` suites must remain green
  at every phase. Run command (existing convention):
  `PYTHONPATH=/home/ubuntu/hermes-runtime-bridge /usr/bin/python3 -m unittest discover -s aee/tests -v`
  plus the top-level `tests/` suite.

### 12.2 macOS contract tests (no Mac host available)

Per the brief, no Mac host is available. macOS correctness is enforced
through **mock-based contract tests**, not live execution:

- `aee/tests/test_macos_contract.py` — construct a
  `HostCapabilities` from `host.capabilities.b2-macbook.yaml`, build a
  `PlatformCapabilities`, assert `is_macos == True`, `host_root` starts
  with `/Users`, `supervisor_kind == "launchd"`,
  `profile_supported == ("developer",)`.
- `aee/tests/test_launchd_service_manager_mock.py` — mock
  `subprocess.run` returning `launchctl list` output, assert
  `LaunchdServiceManager.is_running("com.biaobecue.aee.bridge")` parses
  it correctly. No live `launchctl` invocation.
- `aee/tests/test_macbook_adapter_unchanged.py` — load
  `aee/deploy/adapters/macbook.py` source, assert the file's sha256
  matches the committed baseline (guards against silent macOS edits).
- The CI matrix (`.github/workflows/ci-matrix.yml`) already runs on
  Linux; macOS contract tests run there as mocked unit tests, not as
  live Mac jobs.

### 12.3 Adapter Protocol conformance

- `isinstance(adapter, PlatformAdapter)` (runtime_checkable) for every
  registered adapter (already enforced by the registry).
- New: `test_capabilities_facade_conformance.py` — every adapter's
  `detect()` result builds a valid `PlatformCapabilities` without
  raising.

---

## 13. Risks and Rollback Strategy

| Risk | Likelihood | Mitigation | Rollback |
| --- | --- | --- | --- |
| Facade fallback doesn't match M2 exactly | Low | Phase 1 has no callers; mismatch surfaces in Phase 2's `test_dispatcher_uses_capabilities.py` | Revert Phase 2 commit; Phase 1 module is unused |
| `GitRunner` introduces a behavior change on M2 | Low | Linux impl uses the same `subprocess.check_output` flags; acceptance gate requires identical SHA before/after | Revert the `dispatcher/manager.py` swap; the seam is the only change |
| Path-prefix allowlist narrows on M2 | Medium | `LinuxDefaults` preserves `/tmp` and `/opt` even when capabilities don't list them | Revert `dispatcher/safety.py` change; allowlist returns to hardcoded defaults |
| macOS contract tests pass but live Mac breaks | Medium | No Mac host = no live verification; contract tests assert structure, not liveness | Document as a known limitation; defer Mac live test to when a host is available |
| Adapter registry grows unbounded | Low | `register_adapter` is the extension point; default registry stays at 5 | N/A (additive) |
| Profile descriptor treated as mutable | Very low | It is read-only by module invariant; tests enforce no `subprocess`/`os.environ` imports | N/A |

Rollback is **per phase**: each phase is a single small commit (per the
K-shape pattern). Reverting one commit restores the previous phase's
state. No phase depends on a later phase.

---

## 14. Recommended First Implementation Work Order

### Primary deliverable (one)

`aee/deploy/capabilities.py` — the `PlatformCapabilities` frozen facade +
`from_capabilities()` constructor + `LinuxDefaults` fallback.

### Work order (sequential)

1. **Add** `aee/deploy/capabilities.py` with `PlatformCapabilities`
   dataclass, `from_capabilities(HostCapabilities) -> PlatformCapabilities`,
   and `LinuxDefaults` singleton.
2. **Add** `aee/platform/__init__.py` exporting `get_capabilities()`.
3. **Add** `aee/platform/current.py` with the cached singleton
   (`_cached: Optional[PlatformCapabilities]`) and `get_capabilities()`
   that resolves via the registry + loader on first call and caches.
4. **Add** `aee/tests/test_platform_capabilities.py` covering: construction
   from a `HostCapabilities`, `resolve_path` expansion, `is_linux` /
   `is_macos` predicates, `LinuxDefaults` fields.
5. **Add** `aee/tests/test_platform_bootstrap.py` covering: caching,
   idempotent re-resolution, fallback when no YAML is supplied.
6. **Run** `PYTHONPATH=. /usr/bin/python3 -m unittest discover -s aee/tests -v`
   and the top-level `tests/` suite; assert no regressions.
7. **Verify** no production file was modified (`git diff --stat` shows only
   the new additive files).
8. **Stop.** Do not migrate dispatcher in this work order. Phase 2 is a
   separate work order gated on Phase 1 acceptance.

### Likely files to change in this first work order

- **New**: `aee/deploy/capabilities.py`
- **New**: `aee/platform/__init__.py`
- **New**: `aee/platform/current.py`
- **New**: `aee/tests/test_platform_capabilities.py`
- **New**: `aee/tests/test_platform_bootstrap.py`
- **Modified**: none (this is the safe additive first step)

The next work order (Phase 2) will touch:
- `dispatcher/manager.py` (2 `subprocess.check_output` calls)
- `dispatcher/safety.py` (path-prefix allowlist source)
- New: `aee/deploy/runners.py`, `aee/tests/test_runners.py`,
  `aee/tests/test_dispatcher_uses_capabilities.py`

---

## 15. End of plan

This plan is implementation-ready. It does not modify any source file. All
evidence is drawn from the actual repository at the cited HEAD. The first
work order produces exactly one primary deliverable
(`aee/deploy/capabilities.py`) plus its bootstrap and tests, with zero
production modifications — the safest possible first step.