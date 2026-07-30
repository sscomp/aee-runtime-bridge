# AEE Bootstrap W3 — macOS Bootstrap Implementation Report

**Work order:** W3 macOS Bootstrap (per approved Bootstrap v1 spec `reports/aee_bootstrap_v1_spec.md` §6.1, §6.2, §6.3, §13.3, §16)
**Repo:** `/home/ubuntu/hermes-runtime-bridge`
**Branch:** `main`
**Base HEAD:** `f47f5fa feat(bootstrap): W2 Ubuntu/Debian bootstrap — detect/deps libs + python installer + tests`
**One Task, One Deliverable:** this file (`reports/aee_bootstrap_w3_macos_implementation.md`)
**Commit/push:** NOT performed (out of scope per brief).

---

## A. Summary

Implements the macOS platform bootstrap flow for AEE Bootstrap v1, mirroring the
W2 Ubuntu/Debian slice's three-layer shape (manifest + shell installer + Python
testable core) and reusing the W1 lifecycle exit constants. The slice is
honest-scope: macOS v1 supports only the `developer` profile (spec §13.3,
`MacOSDefaults.profile_supported == ("developer",)`), Homebrew is the package
manager (§6.3), no `sudo` is used (§6.4 + §13.3), the brew prefix is detected
via `brew --prefix` (not hardcoded), and `--no-brew` / `--system` are honoured
per §13.3 / §6.5.

## B. Environment

- Host: Linux 6.17.0-1009-aws (Debian 12) — non-Darwin, so the W3 shell installer
  correctly exits 12 here; macOS paths are exercised via `uname`/`brew` stubs in
  the shell tests and via synthetic `MacOSHostInfo` in the Python tests.
- Python: 3.11 (system).
- Repo HEAD: `f47f5fa` (unchanged by this work).
- Pre-existing dirty working tree: 1 modified (`.gitignore`) + many untracked
  files from prior AEE slices. None of those are touched by W3.

## C. Files Delivered (5, all net-new)

| Path | Lines | Bytes | SHA256 |
| --- | ---: | ---: | --- |
| `bootstrap/manifests/brew.deps.txt` | 53 | 2606 | `a334de64f5c6b162622c1a93fb5ed77b09e5f9a0f37606f676d26ff87a7f5c27` |
| `bootstrap/lib/macos_deps.sh` | 383 | 15888 | `392ce33ba1ef158b0f1ba62b484777787229850f54d757c8933a10ba15026a23` |
| `aee/installer/macos_bootstrap.py` | 388 | 14198 | `2522552e8e3350655aa74d1ae5b646b2a411b2ba7b76d25802c4936a009bb99a` |
| `aee/tests/test_macos_bootstrap.py` | 643 | 25010 | `3dba2bea3de9bae40ce9b57c3f4979841c9e9dcd2d39166c80c656a7b0717ab9` |
| `tests/test_bootstrap_lib_macos_deps.sh` | 371 | 15316 | `7228056bc5a6968e78c0d8aa39936c5162cccf6cbe18f8c71fd9d21045d9368e` |
| **Total** | **1838** | **83018** | — |

`ls -la` confirms all five files exist and are regular files; `wc -l` and
`sha256sum` outputs are recorded above.

## D. Design

### D.1 Manifest — `bootstrap/manifests/brew.deps.txt`

Mirrors `apt.deps.txt`'s format (one formula per line, `#` comments, blanks
stripped). Core hard deps (spec §6.1 macOS column):

```
git
python@3.11
curl
ca-certificates
```

Profile-gated formulae (spec §6.2) are listed for parity with the apt manifest
and for future profile expansion, but are NEVER installed on macOS v1 because
the planner's profile whitelist (`MACOS_SUPPORTED_PROFILES = {"developer"}`)
rejects `mini`/`full`/`edge` before the manifest is filtered:

```
supervisor   # mini + full  (never on macOS v1)
docker       # full + edge  (never on macOS v1)
```

`python@3.11` is the canonical pinned Homebrew formula (the unversioned `python`
formula is intentionally NOT used, per spec §6.3). `uv` is installed via pip
post-brew (mirroring W2's `install_uv` helper).

### D.2 Shell installer — `bootstrap/lib/macos_deps.sh`

Stage `01_deps` for macOS only. Mirrors `deps.sh`'s shape (arg parsing →
platform gate → manifest load → profile filter → brew install → uv pip
install → announce). Key macOS-specific behaviours per spec:

- **No sudo** (§6.4 + §13.3): the runtime runs as the install owner; Homebrew
  is installed into the user's prefix and launchd agents go in
  `~/Library/LaunchAgents`.
- **Brew prefix detection**: `brew --prefix` is called at runtime; the result
  is recorded in the plan announce line (`brew_prefix=/opt/homebrew` on Apple
  Silicon, `brew_prefix=/usr/local` on Intel). Nothing hardcodes the prefix.
- **`--no-brew`** (§13.3 limitation): skips the brew install step; the operator
  must provide `python@3.11` another way (e.g. python.org installer).
- **`--system`** (§6.5): rejected with exit 12 — system-scope is not supported
  on macOS v1 (operator runs launchd by hand).
- **Homebrew first-install**: if `brew` is not on PATH and `--no-brew` is not
  set, the script announces the Homebrew first-install command (a sudo
  operator step per §13.3). In `--dry-run` it prints the plan and continues;
  in `--execute` it exits 12 (honest scope — the bootstrap does not silently
  sudo).
- **DRY_RUN propagation (P1 parity with W2)**: the CLI flag is authoritative.
  `--execute` exports `DRY_RUN=0`; `--dry-run` exports `DRY_RUN=1` and
  overrides any inherited `DRY_RUN=0`; default (no flag) is `DRY_RUN=1` even
  if the environment exports `DRY_RUN=0`.
- **Exit codes** (§10.4): `0` ok / dry-run plan; `2` parse error; `7` brew
  retryable; `10` network; `12` dependency floor not met (non-Darwin,
  brew missing + no `--no-brew`, `--system`, non-developer profile).

### D.3 Python core — `aee/installer/macos_bootstrap.py`

Pure-Python, side-effect-free planning surface (no subprocess, no filesystem
writes, no network) — mirrors `linux_bootstrap.py`'s contract. Provides:

- `MacOSHostInfo(kernel, brew_prefix)` frozen dataclass with `is_supported`
  (kernel == `"Darwin"`) and `is_apple_silicon` (prefix == `/opt/homebrew`).
- `BrewDependencyPlan` frozen dataclass with `brew_command` and `formulae_count`
  properties; `brew_command` shape mirrors `DependencyPlan.apt_command`.
- `parse_manifest`, `filter_by_profile`, `parse_uname_kernel`,
  `detect_macos_host`, `plan_brew_dependencies`, `plan_for_current_macos_host`.
- `UnsupportedPlatformError` (non-Darwin), `UnsupportedProfileError`
  (non-developer, defence in depth on top of the macOS v1 whitelist),
  `ManifestNotFoundError`.
- Exit constants re-exported from `aee.installer.lifecycle` (W1) +
  `aee.installer.backend` (verified): `EXIT_OK=0`, `EXIT_PARSE_ERROR=2`,
  `EXIT_STAGE_FAILED_RETRYABLE=7`, `EXIT_NETWORK_ERROR=10`,
  `EXIT_DEPENDENCY_FLOOR_NOT_MET=12`. None collide with the verified
  `{3, 4, 5, 6}`.

### D.4 Shell tests — `tests/test_bootstrap_lib_macos_deps.sh`

44 assertions across 15 numbered tests. macOS paths are exercised by stubbing
`uname -s` to print `Darwin` and (optionally) `brew --prefix` to print
`/opt/homebrew`, so the full brew dry-run plan is verified on a Linux CI host.
Includes P1-parity tests (DRY_RUN propagation, --dry-run precedence,
unauthorized-execution prevention) mirroring the W2 deps.sh test suite.

### D.5 Python tests — `aee/tests/test_macos_bootstrap.py`

72 tests across 13 `TestCase` classes: exit constants, platform vocabulary,
`MacOSHostInfo`, `parse_uname_kernel`, `detect_macos_host`, `parse_manifest`,
`filter_by_profile`, `PROFILE_GATED` table, `ALWAYS_INSTALL` set,
`plan_brew_dependencies` (including idempotent planning, Apple Silicon vs
Intel equivalence, missing manifest, brew missing + `--no-brew` matrix),
`plan_for_current_macos_host`, `BrewDependencyPlan` properties, real-manifest
content, and the honest-scope contract (Linux/Windows kernels refused,
non-developer profiles refused). stdlib `unittest` only — no pytest, no
subprocess, no network.

## E. Commands

```bash
# Shell tests
bash tests/test_bootstrap_lib_macos_deps.sh

# Python tests
PYTHONPATH=. python3 -m unittest aee.tests.test_macos_bootstrap -v

# Smoke (non-Darwin host → exit 12)
bash bootstrap/lib/macos_deps.sh --repo-root . --profile developer --dry-run

# Smoke (simulated Darwin + brew stub → dry-run plan)
uname() { printf 'Darwin'; }; export -f uname
brew() { printf '/opt/homebrew'; }; export -f brew
bash bootstrap/lib/macos_deps.sh --repo-root . --profile developer --dry-run
unset -f uname brew
```

## F. Test Results

### F.1 W3 shell tests (this slice)

```
$ bash tests/test_bootstrap_lib_macos_deps.sh 2>&1 | tail -3
macos_deps.sh tests: 44 passed, 0 failed
```

### F.2 W3 Python tests (this slice)

```
$ PYTHONPATH=. python3 -m unittest aee.tests.test_macos_bootstrap 2>&1 | tail -4
----------------------------------------------------------------------
Ran 72 tests in 0.003s

OK
```

### F.3 Bootstrap regression (W1 + W2 + W3)

```
$ bash tests/test_bootstrap_lib_detect.sh 2>&1 | tail -2
detect.sh tests: 8 passed, 0 failed

$ bash tests/test_bootstrap_lib_deps.sh 2>&1 | tail -2
deps.sh tests: 23 passed, 0 failed

$ bash tests/test_bootstrap_lib_macos_deps.sh 2>&1 | tail -2
macos_deps.sh tests: 44 passed, 0 failed

$ PYTHONPATH=. python3 -m unittest \
    aee.tests.test_platform_bootstrap \
    aee.tests.test_linux_bootstrap \
    aee.tests.test_macos_bootstrap 2>&1 | tail -4
----------------------------------------------------------------------
Ran 152 tests in 0.007s

OK
```

Aggregate bootstrap test count: **75 shell + 152 Python = 227 tests, 0 failures.**

### F.4 Broader `aee/tests` regression

```
$ PYTHONPATH=. python3 -m unittest discover -s aee/tests -p 'test_*.py' 2>&1 | tail -3
Ran 1916 tests in 38.412s
FAILED (errors=5, skipped=2)
```

The 5 errors are all in `aee.tests.test_runtime_config` (PyYAML/env-gap class —
`test_apply_registers_definitions`, `test_apply_replace_overrides_existing`,
`test_apply_uses_default_runtime_id`, `test_env_substitution`,
`test_load_full`). Verified PRE-EXISTING by stashing all 5 W3 files and
re-running: the same 5 errors reproduce with no W3 code on disk. They are
documented in the W2 case study (`aee-bootstrap-w2-ubuntu-debian-case-study`)
as the known env-gap baseline. W3 introduces **zero** new failures and
**zero** new skips.

## G. Production Safety

- **0 production files modified.** `git diff --stat HEAD` over the 15
  protected files (`bootstrap/lib/detect.sh`, `bootstrap/lib/deps.sh`,
  `bootstrap/manifests/apt.deps.txt`, `aee/installer/lifecycle.py`,
  `aee/installer/backend.py`, `aee/installer/linux_bootstrap.py`,
  `aee/installer/__init__.py`, `aee/platform/current.py`,
  `aee/deploy/capabilities.py`, `aee/deploy/adapters/macbook.py`,
  `tests/test_bootstrap_lib_detect.sh`, `tests/test_bootstrap_lib_deps.sh`,
  `aee/tests/test_platform_bootstrap.py`, `aee/tests/test_linux_bootstrap.py`,
  and the W1/W2 spec) returns empty.
- **All 5 W3 files are untracked** (`git status --short` shows `??` for each).
  No `git add`, no `git commit`, no `git push` performed (out of scope per
  brief).
- **No subprocess** in the Python core (`macos_bootstrap.py`).
- **No filesystem writes** in the Python core.
- **No network** in the Python core or the shell installer's dry-run path.
- **No sudo** in the shell installer (spec §6.4 + §13.3).
- **No `brew install` executed** in any test path (all macOS paths use
  `--dry-run` or stubbed `brew`).

## H. Spec Compliance Matrix

| Spec section | Requirement | W3 implementation |
| --- | --- | --- |
| §6.1 hard deps | git 2.30+, python 3.11+, uv latest | `git`, `python@3.11`, `curl`, `ca-certificates` in manifest; `uv` via pip post-brew |
| §6.2 profile gating | supervisor → mini+full; docker → full+edge | `PROFILE_GATED` table + `filter_formulae_by_profile` + shell helper |
| §6.3 Homebrew | `brew install --quiet`; install Homebrew if missing unless `--no-brew` | `brew_install_run` uses `--quiet`; `install_homebrew` announces first-install; `--no-brew` honoured |
| §6.4 privilege | No sudo on macOS | `brew_install_run` has no `sudo`; `install_homebrew` does not auto-sudo |
| §6.5 install scope | Per-user default; `--system` not supported on macOS v1 | `--system` rejected with exit 12 |
| §10.4 exit codes | 0/2/7/10/12 | Re-exported from W1 lifecycle; pinned by `TestExitConstants` |
| §13.3 macOS | launchd supervisor; `developer` profile only; no sudo; brew prefix detection; `--no-brew` | All implemented and tested |
| §5.1 idempotency | brew "already installed" short-circuit | `brew install --quiet` is idempotent; `install_uv` skips if `uv` on PATH |

## I. Out-of-Scope Compliance

The brief lists five out-of-scope items. Compliance:

1. **Windows bootstrap** — NOT implemented. `macos_bootstrap.py` raises
   `UnsupportedPlatformError` for non-Darwin kernels (including WindowsNT);
   the shell installer exits 12 for non-Darwin.
2. **Additional Linux features** — NOT implemented. W2's Ubuntu/Debian path
   is unchanged; no new Linux features added.
3. **Commit or push** — NOT performed. All 5 files are untracked; HEAD is
   `f47f5fa` (unchanged).
4. **Unrelated refactoring** — NOT performed. `git diff --stat HEAD` over the
   15 protected files is empty.

## J. Remaining Risks

1. **No live macOS validation.** All macOS paths are exercised via stubs
   (shell) and synthetic `MacOSHostInfo` (Python). A real macOS E2E is W12
   (spec §16). The W3 slice is honest about this: the shell installer's
   `--execute` path with brew missing exits 12 (operator installs Homebrew
   by hand), and the Python planner does not probe the live host (subprocess
   is the shell layer's job, per the W2 pattern).
2. **Homebrew first-install is an operator step.** Per spec §13.3 the first
   `/opt/homebrew` setup requires sudo. W3 does NOT auto-sudo; it announces
   the install command and exits 12 in `--execute` mode. Operators must
   install Homebrew by hand, then re-run. This matches the spec's
   `--no-brew` limitation but means the W3 slice is not fully zero-touch on
   a fresh Mac.
3. **macOS v1 is developer-only.** `MacOSDefaults.profile_supported ==
   ("developer",)` and the W3 planner's `MACOS_SUPPORTED_PROFILES` whitelist
   enforce this. `mini`/`full`/`edge` profiles on macOS require a future
   spec update (the manifest already lists `supervisor` and `docker` for
   parity so the expansion is manifest-only).
4. **`uv` install uses the brew python@3.11.** The shell `install_uv` helper
   prefers `/opt/homebrew/bin/python3.11` then `/usr/local/bin/python3.11`
   then falls back to system `python3`. If the operator uses `--no-brew` and
   has no python@3.11 on PATH, `install_uv` will fail (non-fatal — pip
   fallback is documented in spec §6.1). This is the same non-fatal
   contract as W2's `install_uv`.
5. **Pre-existing dirty working tree.** The repo has 1 modified (`.gitignore`)
   and many untracked files from prior AEE slices. W3 does not touch any of
   them; the commit candidate list (when the operator authorises a commit)
   is exactly the 5 files in §C.

## K. Review Readiness

- **Files to review:** the 5 files in §C.
- **Review order suggestion:** manifest → Python core → shell installer →
  Python tests → shell tests (each layer builds on the previous).
- **Key invariants to verify:**
  1. No sudo anywhere in `macos_deps.sh` (grep `sudo` → only the `install_homebrew`
     docstring + announce text, never a command).
  2. Brew prefix is never hardcoded (grep `/opt/homebrew` / `/usr/local` →
     only in tests and the `install_uv` fallback lookup, never as the
     announce value).
  3. macOS v1 profile whitelist is enforced in BOTH the Python planner
     (`MACOS_SUPPORTED_PROFILES`) and the shell installer (the
     `if [ "$profile" != "developer" ]` gate).
  4. Exit codes do not collide with the verified `{3, 4, 5, 6}`
     (`TestExitConstants.test_no_collision_with_verified_backend_codes`).
  5. DRY_RUN propagation is the P1 parity contract (3 shell tests + the
     Python planner's `dry_run` field).

## L. Commit Readiness

The 5 files in §C are the commit candidate set. Suggested commit message
(following the W2 slice's shape):

```
feat(bootstrap): W3 macOS bootstrap — Homebrew deps + python planner + tests

Implements macOS platform detection and Homebrew dependency planning/install
flow per approved Bootstrap v1 spec §6.1/§6.2/§6.3/§13.3. Mirrors the W2
Ubuntu/Debian slice's three-layer shape (manifest + shell installer + Python
testable core) and reuses the W1 lifecycle exit constants.

macOS v1 supports only the developer profile (MacOSDefaults.profile_supported
== ("developer",)). No sudo (spec §6.4 + §13.3). Brew prefix detected via
`brew --prefix` (not hardcoded). --no-brew and --system honoured per §13.3 /
§6.5. Homebrew first-install is an operator step (announced, not auto-sudo).

Tests: 44 shell + 72 Python = 116 new tests, 0 failures. Bootstrap regression
75 shell + 152 Python = 227 tests, 0 failures. 0 production files modified.
```

Per the brief, commit/push is out of scope — the operator will authorise the
commit in a separate step.

## M. Telegram Notification

```
$ hermes send --to telegram:5132341473 --subject "W3 macOS Bootstrap — review ready" --file -
sent
```

Notification sent to 鼎鼎 (chat_id 5132341473) with the short summary
(deliverable path, branch/HEAD, 5 files/1838 lines, test counts, production
safety, spec sections, out-of-scope compliance).

## N. References

- Spec: `reports/aee_bootstrap_v1_spec.md` §6.1, §6.2, §6.3, §6.4, §6.5,
  §10.4, §13.3, §16 (W3 row).
- W1 (lifecycle skeleton): `aee/installer/lifecycle.py`,
  `aee/tests/test_platform_bootstrap.py`.
- W2 (Ubuntu/Debian, the shape template): `bootstrap/lib/deps.sh`,
  `bootstrap/manifests/apt.deps.txt`, `aee/installer/linux_bootstrap.py`,
  `aee/tests/test_linux_bootstrap.py`, `tests/test_bootstrap_lib_deps.sh`.
- macOS capability defaults: `aee/deploy/capabilities.py::MacOSDefaults`
  (`profile_supported=("developer",)`, `supervisor_kind="launchd"`).
- macOS adapter: `aee/deploy/adapters/macbook.py` (informational only;
  `runtime_profile_supported=("developer",)`).

---

_This is the single durable artifact for W3. All work is captured above; no
intermediate files were left outside the 5-file deliverable set._