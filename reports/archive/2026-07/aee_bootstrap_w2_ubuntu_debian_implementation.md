# AEE Bootstrap v1 — W2 Ubuntu/Debian Bootstrap Implementation Report

**Repository:** `/home/ubuntu/hermes-runtime-bridge`
**Branch:** `main`
**HEAD:** `44223ea82680bb8089941815dc866738504fb7fa` (W1 commit; unchanged)
**Specification:** `reports/aee_bootstrap_v1_spec.md` §6, §13.1, §13.2, §4 stages 00–01, §3 layout
**Date:** 2026-07-25
**Author:** M2 (Hermes Agent, Abacus.ai runtime)

---

## 1. Execution Timing

* Start (UTC): 2026-07-25 ~12:40
* End (UTC):   2026-07-25 ~12:55
* Duration:   ~15 minutes
* Timezone: Asia/Taipei (UTC+8) — 20:40 to 20:55 CST

---

## 2. Overall Verdict

**PASS** — W2 Ubuntu/Debian Bootstrap delivered as a minimal, tested,
side-effect-free Linux bootstrap detection + dependency installation flow
for Ubuntu/Debian only. macOS and Windows are explicitly out of scope and
refused by `UnsupportedDistroError`.

Targeted Python tests: **59/59 PASS**. Targeted shell tests: **26/26 PASS**
(8 detect.sh + 18 deps.sh). Impacted regression: **317/317 PASS** (263 W1
regression set + 54 W1 lifecycle + 59 W2 new — counted as 376 union, 317
when excluding the 59 new W2 tests from the regression bucket). Broad
regression: 1844 tests, 5 pre-existing errors (PyYAML missing in
`test_runtime_config`, identical to W1 baseline, unrelated to W2), 2
skipped. No commit, push, deploy, restart, stash, merge, rebase. No
production files modified. Pre-existing unrelated working-tree changes
(`.gitignore` + untracked reports/scripts) preserved untouched.

---

## 3. Baseline

* Branch: `main`
* HEAD: `44223ea82680bb8089941815dc866738504fb7fa` (W1 commit; unchanged)
* Pre-existing working-tree state:
  * ` M .gitignore` (pre-existing — NOT touched by W2)
  * 40+ untracked files (reports, scripts, requirements lock files) — all
    pre-existing, none touched by W2
* W1 deliverables (committed at `44223ea`):
  * `aee/installer/lifecycle.py` (W1 shared core skeleton)
  * `aee/tests/test_installer_lifecycle.py` (W1 tests)
  * `aee/installer/__init__.py` (W1 re-exports)
* Verified exit constants (unchanged): `EXIT_OK=0`, `EXIT_PARSE_ERROR=2`,
  `EXIT_PROFILE_INVALID=3`, `EXIT_PRE_FLIGHT_FAILED=4`,
  `EXIT_PROFILE_SWITCH_REJECTED=5`, `EXIT_EXECUTE_NOT_AUTHORIZED=6`
* W1 proposed exit constants (consumed by W2): `EXIT_STAGE_FAILED_RETRYABLE=7`,
  `EXIT_NETWORK_ERROR=10`, `EXIT_DEPENDENCY_FLOOR_NOT_MET=12`
* Host environment: Debian 12 (bookworm), Python 3.11.2, shellcheck 0.9+
- Verified constants in `aee/installer/backend.py`: `EXIT_OK=0`, `EXIT_PROFILE_INVALID=3`,
  `EXIT_PRE_FLIGHT_FAILED=4`, `EXIT_PROFILE_SWITCH_REJECTED=5`, `EXIT_EXECUTE_NOT_AUTHORIZED=6`
- W1 `PlatformIdentity` enum: LINUX, MACOS, UNKNOWN (W2 does NOT extend)

---

## 4. Change Summary

Seven files added (all new, 0 deletions, 0 production files modified).
No commits, no staging.

### 4.1 `bootstrap/manifests/apt.deps.txt` (NEW, 50 lines)

The apt dependency manifest for Ubuntu/Debian. Lists:
- Core hard deps (always installed): `git`, `python3`, `python3-pip`,
  `python3-venv`, `curl`, `ca-certificates`, `gnupg`, `python3.11`,
  `python3.11-venv`
- Profile-gated: `supervisor` (mini + full), `docker.io` (full + edge)

Format: one package per line, `#` comments and blank lines ignored by
`read_manifest`. Comments document the deadsnakes PPA requirement for
Ubuntu 22.04 and the no-op on Debian 12 (native python3.11).

### 4.2 `bootstrap/lib/detect.sh` (NEW, 181 lines, executable)

Thin POSIX shell trampoline for stage 00_detect (spec §2.3, §4).
- `detect_platform <repo_root>`: delegates to
  `aee.platform.current.resolve_platform_identity` via `python3 -c` when
  Python is available; falls back to a conservative `/etc/os-release` +
  `uname -s` heuristic when Python is absent (stage 00 runs before deps).
- `detect_linux_distro`: prints `ubuntu`, `debian`, or `unknown` from
  `/etc/os-release` ID field.
- `detect_linux_version_id`: prints VERSION_ID (e.g. `22.04`, `12`).
- CLI mode: `bash bootstrap/lib/detect.sh [--repo-root <path>]` prints one
  identity value. `--help` exits 0. Unknown args exit 2.
- Sourcing the file does NOT execute main (guard:
  `[ "${BASH_SOURCE[0]:-$0}" = "$0" ]`).
- W2 scope: no macOS/Windows branches. The heuristic reports `darwin`
  honestly (matches Python resolver) but macOS dep installation is a
  separate work order.

### 4.3 `bootstrap/lib/deps.sh` (NEW, 305 lines, executable)

Ubuntu/Debian apt dependency installer for stage 01_deps (spec §6, §13.1,
§13.2).
- `main`: parses `--repo-root`, `--profile`, `--dry-run|--execute`;
  detects distro via `detect.sh`; reads + filters manifest; runs
  `apt-get update` + `apt-get install --no-install-recommends`; installs
  `uv` via pip post-apt.
- `read_manifest`: strips comments/blanks, prints package list.
- `filter_packages_by_profile`: gates `supervisor` (mini+full) and
  `docker.io` (full+edge); core deps always pass.
- `maybe_add_deadsnakes`: adds deadsnakes PPA on Ubuntu 22.04 only
  (no-op on Debian 12 and Ubuntu 24.04).
- `install_uv`: idempotent — skips if `uv` already on PATH.
- Dry-run by default: `--dry-run` prints planned apt commands without
  executing. `--execute` performs real apt installs (gated by W6
  trampoline in production).
- Exit codes: 0 (success/dry-run), 2 (parse error), 7 (retryable apt
  failure), 10 (network error), 12 (unsupported distro / apt missing).
- W2 scope gate: exits 12 with a clear message if distro is not
  `ubuntu` or `debian`. macOS (brew) and Windows (winget) are separate
  work orders.

### 4.4 `aee/installer/linux_bootstrap.py` (NEW, 356 lines)

Python-side testable core for W2 (spec §6, §13.1, §13.2). Side-effect
free: no subprocess, no filesystem writes, no network.
- `DistroInfo` (frozen dataclass): `distro`, `version_id`,
  `is_supported`, `needs_deadsnakes` properties.
- `DependencyPlan` (frozen dataclass): `distro_info`, `profile`,
  `packages`, `needs_deadsnakes`, `needs_uv_pip_install`, `dry_run`,
  `apt_command`, `package_count`.
- `parse_os_release(content)`: parses /etc/os-release content string,
  returns `DistroInfo`. Maps non-ubuntu/debian IDs to `unknown`.
- `detect_distro(path)`: reads /etc/os-release from filesystem.
- `parse_manifest(path)`: reads apt.deps.txt, strips comments/blanks.
- `filter_by_profile(packages, profile)`: applies §6.2 gating rules.
- `plan_apt_dependencies(distro_info, profile, manifest_path, dry_run)`:
  produces a `DependencyPlan`. Raises `UnsupportedDistroError` for
  non-ubuntu/debian, `UnsupportedProfileError` for invalid profiles,
  `ManifestNotFoundError` for missing manifest.
- `plan_for_current_host(profile, repo_root, dry_run)`: convenience
  wrapper that detects the current host's distro.
- Exit constants re-exported from W1 lifecycle (`EXIT_OK=0` from
  `aee.installer.backend`, `EXIT_PARSE_ERROR=2` defined locally,
  `EXIT_STAGE_FAILED_RETRYABLE=7`, `EXIT_NETWORK_ERROR=10`,
  `EXIT_DEPENDENCY_FLOOR_NOT_MET=12` from `aee.installer.lifecycle`).
- Vocabulary: `SUPPORTED_DISTROS = frozenset({"ubuntu", "debian"})`,
  `DEADSNAKES_UBUNTU_VERSIONS = frozenset({"22.04"})`,
  `SUPPORTED_DEBIAN_VERSIONS = frozenset({"12"})`.
- Profile gating: `ALWAYS_INSTALL` (9 core deps),
  `PROFILE_GATED = {"supervisor": {"mini","full"}, "docker.io": {"full","edge"}}`.

### 4.5 `aee/tests/test_linux_bootstrap.py` (NEW, 464 lines)

Targeted Python unit tests, stdlib `unittest` only. 59 tests across 11
test classes:

| Class | Tests | Coverage |
| --- | --- | --- |
| `TestExitConstants` | 6 | Exit code values; no collision with verified `{0,2,3,4,5,6}`; proposed `{7,10,12}` disjoint. |
| `TestDistroVocabulary` | 5 | `SUPPORTED_DISTROS` ubuntu/debian only; macOS/Windows excluded; deadsnakes versions; Debian versions. |
| `TestDistroInfo` | 5 | `is_supported`, `needs_deadsnakes` for ubuntu 22.04/24.04, debian 12, unknown; frozen. |
| `TestParseOsRelease` | 7 | ubuntu/debian/fedora/empty/comments/quotes/missing-ID. |
| `TestParseManifest` | 4 | real manifest, comments/blanks, order preservation, missing file. |
| `TestFilterByProfile` | 6 | mini/full/edge/developer gating; always-install for all profiles; invalid profile. |
| `TestProfileGatedTable` | 2 | supervisor→{mini,full}, docker.io→{full,edge}. |
| `TestPlanAptDependencies` | 13 | ubuntu 22.04 mini (deadsnakes), ubuntu 24.04 full (no deadsnakes), debian 12 edge, developer excludes gated, unknown/fedora raises, invalid profile, missing manifest, dry_run flag, apt_command format, package count, idempotent, frozen. |
| `TestPlanForCurrentHost` | 2 | returns plan or raises on non-ubuntu/debian; invalid profile. |
| `TestDetectDistro` | 3 | returns DistroInfo; custom path; missing file → unknown. |
| `TestAlwaysInstallSet` | 2 | core deps present; gated deps excluded. |
| `TestW2ScopeContract` | 3 | macOS/windows/arch → unknown (honest scope). |

### 4.6 `tests/test_bootstrap_lib_detect.sh` (NEW, 151 lines, executable)

Shell integration tests for `bootstrap/lib/detect.sh`. 8 tests:
- detect_platform prints valid identity (linux/darwin/unknown)
- detect_linux_distro prints known value
- detect_linux_version_id prints non-empty
- --help exits 0 with usage
- --bogus exits 2
- sourcing produces no output (main not auto-run)
- resolve_via_heuristic valid
- resolve_via_python valid (or fails when python3 missing)

### 4.7 `tests/test_bootstrap_lib_deps.sh` (NEW, 250 lines, executable)

Shell integration tests for `bootstrap/lib/deps.sh`. 18 tests:
- --help exits 0 with W2 scope text
- missing --profile exits 2
- invalid profile exits 2
- --dry-run on Debian exits 0, prints packages, supervisor for mini
- developer profile excludes supervisor + docker.io
- edge profile includes docker.io, excludes supervisor
- sourcing produces no output
- read_manifest strips comments/blanks, includes git
- filter_packages_by_profile: mini/full/developer gating
- missing manifest exits non-zero (12)

---

## 5. Evidence

### 5.1 File verification (ls, wc, sha256)
```
$ ls -la bootstrap/lib/detect.sh bootstrap/lib/deps.sh bootstrap/manifests/apt.deps.txt \
    aee/installer/linux_bootstrap.py aee/tests/test_linux_bootstrap.py \
    tests/test_bootstrap_lib_detect.sh tests/test_bootstrap_lib_deps.sh
-rw------- 1 ubuntu ubuntu 12548 Jul 25 12:45 aee/installer/linux_bootstrap.py
-rw------- 1 ubuntu ubuntu 17622 Jul 25 12:46 aee/tests/test_linux_bootstrap.py
-rwx--x--x 1 ubuntu ubuntu 11261 Jul 25 12:45 bootstrap/lib/deps.sh
-rwx--x--x 1 ubuntu ubuntu  7234 Jul 25 12:44 bootstrap/lib/detect.sh
-rw------- 1 ubuntu ubuntu  1946 Jul 25 12:44 bootstrap/manifests/apt.deps.txt
-rwx--x--x 1 ubuntu ubuntu  9022 Jul 25 12:46 tests/test_bootstrap_lib_deps.sh
-rwx--x--x 1 ubuntu ubuntu  5534 Jul 25 12:46 tests/test_bootstrap_lib_detect.sh

$ wc -l bootstrap/lib/detect.sh bootstrap/lib/deps.sh bootstrap/manifests/apt.deps.txt \
    aee/installer/linux_bootstrap.py aee/tests/test_linux_bootstrap.py \
    tests/test_bootstrap_lib_detect.sh tests/test_bootstrap_lib_deps.sh
  181 bootstrap/lib/detect.sh
  305 bootstrap/lib/deps.sh
   50 bootstrap/manifests/apt.deps.txt
  356 aee/installer/linux_bootstrap.py
  464 aee/tests/test_linux_bootstrap.py
  151 tests/test_bootstrap_lib_detect.sh
  250 tests/test_bootstrap_lib_deps.sh
 1757 total

$ sha256sum bootstrap/lib/detect.sh bootstrap/lib/deps.sh bootstrap/manifests/apt.deps.txt \
    aee/installer/linux_bootstrap.py aee/tests/test_linux_bootstrap.py \
    tests/test_bootstrap_lib_detect.sh tests/test_bootstrap_lib_deps.sh
2438671f2ca5fce59a38e232d613c012bd58bba6b9ad3590346dfb8e5c4a4f2a  bootstrap/lib/detect.sh
e03b3d724d06fd08a164b315b4cbc52845f251fad747df00b29efa9a52e2a056  bootstrap/lib/deps.sh
83737a6e51fdd133b6b3c888d175ee9480f76107573791c978ce5a7ff699ce4b  bootstrap/manifests/apt.deps.txt
850b04c8b38a658a638b425f27194f7023c0fb18002d7850214f324a55620524  aee/installer/linux_bootstrap.py
81391373889787f2671df96eb5604fd820e233d87166f938f3fe4746a6003331  aee/tests/test_linux_bootstrap.py
be62a55e615259d85c6b22b8d7ec0e02ba77db2184280ecd58918f1e81238743  tests/test_bootstrap_lib_detect.sh
ce2793ba619f8f821e33e1c9e3844466838ce658931e5a73a7cb00b91d018907  tests/test_bootstrap_lib_deps.sh
```

### 5.2 Python core smoke
```
$ PYTHONPATH=. python3 -c "
from aee.installer.linux_bootstrap import plan_apt_dependencies, parse_os_release, DistroInfo
di = parse_os_release('ID=ubuntu\nVERSION_ID=\"22.04\"\n')
plan = plan_apt_dependencies(di, 'mini', 'bootstrap/manifests/apt.deps.txt')
print('distro:', di)
print('needs_deadsnakes:', di.needs_deadsnakes)
print('packages:', plan.packages)
print('apt_command:', plan.apt_command)
"
distro: DistroInfo(distro='ubuntu', version_id='22.04')
needs_deadsnakes: True
packages: ('git', 'python3', 'python3-pip', 'python3-venv', 'curl', 'ca-certificates', 'gnupg', 'python3.11', 'python3.11-venv', 'supervisor')
apt_command: [dry-run] apt-get install --no-install-recommends -y git python3 ...
```

### 5.3 Shell smoke
```
$ bash bootstrap/lib/detect.sh --repo-root .
linux

$ bash bootstrap/lib/deps.sh --repo-root . --profile mini --dry-run
deps.sh: distro=debian version_id=12 profile=mini dry_run=1
deps.sh: packages: git python3 python3-pip python3-venv curl ca-certificates gnupg python3.11 python3.11-venv supervisor
[dry-run] sudo apt-get update
deps.sh: stage 01_deps planned (dry-run)
```

### 5.4 Shellcheck
```
$ shellcheck bootstrap/lib/detect.sh bootstrap/lib/deps.sh
In bootstrap/lib/deps.sh line 44:
. "$script_dir/detect.sh"
  ^---------------------^ SC1091 (info): Not following: detect.sh was not specified as input
(RC=0 — info only, no errors or warnings)
```

---

## 6. Git Evidence

### 6.1 Branch + HEAD
```
Branch: main
HEAD:   44223ea82680bb8089941815dc866738504fb7fa  (W1 commit; unchanged)
```

### 6.2 Working tree status (W2 changes only)
```
?? aee/installer/linux_bootstrap.py
?? aee/tests/test_linux_bootstrap.py
?? bootstrap/
?? tests/test_bootstrap_lib_deps.sh
?? tests/test_bootstrap_lib_detect.sh
```

### 6.3 Staging
Nothing staged (`git diff --cached` empty). No `git add` performed.

### 6.4 Forbidden-file verification
```
$ /usr/bin/git diff -- install.sh aee/platform/current.py aee/deploy/adapters/windows.py \
    aee/cli.py aee/installer/backend.py aee/installer/lifecycle.py
(empty — no changes to any forbidden file)
```

### 6.5 Pre-existing unrelated changes (preserved, NOT touched by W2)
```
 M .gitignore  (pre-existing)
?? (40+ untracked reports/scripts/requirements files — pre-existing)
```

---

## 7. Tests

### 7.1 Targeted Python tests — new W2 behavior
**Command:**
```
PYTHONPATH=. python3 -m unittest aee.tests.test_linux_bootstrap -v
```
**Result:** **PASS** — 59 tests, 0 failures, 0 errors, 0 skips.
```
Ran 59 tests in 0.003s
OK
```

### 7.2 Targeted shell tests — detect.sh
**Command:**
```
bash tests/test_bootstrap_lib_detect.sh
```
**Result:** **PASS** — 8 tests, 0 failures.
```
detect.sh tests: 8 passed, 0 failed
```

### 7.3 Targeted shell tests — deps.sh
**Command:**
```
bash tests/test_bootstrap_lib_deps.sh
```
**Result:** **PASS** — 18 tests, 0 failures.
```
deps.sh tests: 18 passed, 0 failed
```

### 7.4 Impacted regression — W1 + installer/platform/CLI surfaces
**Command:**
```
PYTHONPATH=. python3 -m unittest \
  aee.tests.test_installer_lifecycle \
  aee.tests.test_platform_bootstrap \
  aee.tests.test_platform_capabilities \
  aee.tests.test_aee93_installer_backend \
  aee.tests.test_aee92_unified_cli_ux \
  aee.tests.test_registry \
  aee.tests.test_aee96_provider_neutral_deployment \
  aee.tests.test_linux_bootstrap
```
**Result:** **PASS** — 376 tests, 0 failures, 0 errors, 0 skips.
```
Ran 376 tests in 0.070s
OK
```
Rationale: W2 re-uses W1 lifecycle exit constants and the existing
platform/adapter substrate. The W1 regression set (263 tests) + W1
lifecycle (54) + W2 new (59) = 376 union.

### 7.5 Broad regression — full `aee/tests` suite
**Command:**
```
PYTHONPATH=. python3 -m unittest discover -s aee/tests -t .
```
**Result:** 1844 tests, **5 errors, 2 skipped** (no failures).
```
Ran 1844 tests in 38.392s
FAILED (errors=5, skipped=2)
```
**Error attribution (all pre-existing, none caused by W2):** all 5
errors are in `aee/tests/test_runtime_config.py`:
```
ERROR: test_apply_registers_definitions
ERROR: test_apply_replace_overrides_existing
ERROR: test_apply_uses_default_runtime_id
ERROR: test_env_substitution
ERROR: test_load_full
```
Root cause: `ModuleNotFoundError: No module named 'yaml'` (PyYAML not
installed in this environment). These errors are present at baseline
HEAD `44223ea` independent of W2 work (identical to the W1 baseline
report). The W2 module does not import `yaml` or any file that depends
on it.

---

## 8. Production Safety

Confirmations:
- **No Commit** — `git diff --cached` empty; no `git add` performed.
- **No Push** — no push attempted.
- **No Deploy** — no deploy attempted.
- **No Restart** — no service restart attempted.
- **No Stash** — no stash attempted.
- **No Merge** — no merge attempted.
- **No Rebase** — no rebase attempted.

Pre-existing unrelated working-tree changes preserved:
- `.gitignore` modification (pre-existing) — untouched by W2.
- 40+ untracked reports/scripts/requirements files (pre-existing) — untouched.

Forbidden-file verification: no changes to `install.sh`,
`aee/platform/current.py`, `aee/deploy/adapters/windows.py` (does not
exist), `aee/cli.py`, `aee/installer/backend.py`,
`aee/installer/lifecycle.py`, or `.gitignore`.

W2 scope rule: macOS and Windows are explicitly out of scope. The Python
core raises `UnsupportedDistroError` for non-ubuntu/debian distros; the
shell `deps.sh` exits 12 with a clear message. No silent fallback to a
"generic Linux" path.

---

## 9. Remaining Risks

1. **No real apt execution tested.** All shell tests run in `--dry-run`
   mode. The `--execute` path (real `sudo apt-get install`) is not
   exercised in CI — it requires a disposable container with sudo. The
   W11 container E2E harness (spec §16) will cover this.
2. **Deadsnakes PPA not exercised.** The `maybe_add_deadsnakes` function
   is only reached on Ubuntu 22.04 with `--execute`. This host is Debian
   12, so the deadsnakes branch is tested only via the Python
   `needs_deadsnakes` property and dry-run plan output, not via a real
   `add-apt-repository` call.
3. **uv install via pip not tested with --execute.** The `install_uv`
   function is exercised in dry-run mode only. Real pip install is
   non-fatal (uv is preferred; pip is the fallback per spec §6.1).
4. **W6 trampoline not yet shipped.** `bootstrap/lib/detect.sh` and
   `bootstrap/lib/deps.sh` are library files; the W6 POSIX trampoline
   (spec §16) will wire them into the stage machine with marker
   persistence. W2 does not modify the existing tracked `install.sh`
   (that is W6's assessment-first scope per spec §3.1).
5. **Marker persistence not yet wired.** W1's `InMemoryMarkerStore` is
   still the only marker backend. Stage 00/01 markers are not written to
   disk by W2 — that is the W2 "marker persistence" follow-up listed in
   the W1 report's remaining risks (the spec §16 W2 was originally
   scoped as "Stage marker library" but the user's task brief reframed
   W2 as "Ubuntu/Debian Bootstrap" — this implementation follows the
   user's brief, not the spec's original W2 label).
6. **Broad regression has 5 pre-existing errors.** `test_runtime_config`
   fails on missing PyYAML at baseline HEAD `44223ea` independent of W2.
   Not a W2 regression.
7. **Profile validation is local to linux_bootstrap.** The
   `filter_by_profile` function validates against the hardcoded set
   `{"full","mini","edge","developer"}` rather than importing
   `aee.profiles.descriptor.KNOWN_PROFILES`. This avoids a cross-module
   dependency in the testable core; the W4 install CLI will validate
   via the canonical `parse_profile`. The values are identical (verified
   in spec §1.3 assumption 6).

---

## 10. Review Ready

Yes. The implementation is minimal, tested, and side-effect free. W2
delivers the Ubuntu/Debian bootstrap detection + dependency installation
flow as specified (§6, §13.1, §13.2, §4 stages 00–01). The Python core
(`aee.installer.linux_bootstrap`) provides a testable planning surface;
the shell libraries (`bootstrap/lib/{detect,deps}.sh`) provide the
stage 00/01 execution layer. Both are gated to Ubuntu/Debian only.
Targeted tests pass (59 Python + 26 shell). Impacted regression passes
(376). Broad regression has only pre-existing PyYAML errors. All
forbidden files untouched. Pre-existing unrelated working-tree changes
preserved. The artifact is durable at the mandated path with all
required sections.

---

## 11. Commit Ready

No — by directive. The task forbids commit/push/deploy/restart/stash/
merge/rebase. The working tree contains the seven W2 additions plus
pre-existing unrelated changes; staging was not performed. The work is
review-ready and commit-ready from a content perspective, but the
commit gate is held by the operator.

Suggested commit (when authorized by the operator):
```
git add bootstrap/manifests/apt.deps.txt \
        bootstrap/lib/detect.sh \
        bootstrap/lib/deps.sh \
        aee/installer/linux_bootstrap.py \
        aee/tests/test_linux_bootstrap.py \
        tests/test_bootstrap_lib_detect.sh \
        tests/test_bootstrap_lib_deps.sh
git commit -m "feat(bootstrap): add AEE Bootstrap v1 W2 Ubuntu/Debian bootstrap

W2 of the AEE Bootstrap v1 spec (reports/aee_bootstrap_v1_spec.md §6,
§13.1, §13.2, §4 stages 00-01): Linux bootstrap detection and dependency
installation flow for Ubuntu/Debian only. macOS and Windows are out of
scope (UnsupportedDistroError / exit 12).

- bootstrap/manifests/apt.deps.txt: apt dependency manifest (core +
  profile-gated supervisor/docker.io).
- bootstrap/lib/detect.sh: thin POSIX detect shim — delegates to
  aee.platform.current.resolve_platform_identity; heuristic fallback
  when Python absent.
- bootstrap/lib/deps.sh: Ubuntu/Debian apt installer —
  apt-get install --no-install-recommends, deadsnakes PPA for Ubuntu
  22.04, dry-run default, profile-gated, idempotent.
- aee/installer/linux_bootstrap.py: Python-side testable core —
  DistroInfo, DependencyPlan, parse_os_release, plan_apt_dependencies.
- aee/tests/test_linux_bootstrap.py: 59 targeted tests.
- tests/test_bootstrap_lib_detect.sh: 8 shell integration tests.
- tests/test_bootstrap_lib_deps.sh: 18 shell integration tests.

No production files modified. No commit/push/deploy.
"
```

---

## 12. Telegram

Telegram notification sent per task contract.

**Attempt:** YES
**Method:** `hermes send --to telegram:5132341473 --file /tmp/w2_telegram_short.txt --json`
**Recipient:** 鼎鼎 (chat_id 5132341473)
**Sent:** YES
**Message ID:** 8396
**UTC:** 2026-07-25 ~12:55
**Asia/Taipei:** 2026-07-25 ~20:55
**mirrored:** true
**success:** true

The Telegram簡版 (per user profile rule 2026-07-13) carries the verdict,
test counts, change summary, artifact path, and commit-ready status. The
full report is the durable artifact at
`/home/ubuntu/hermes-runtime-bridge/reports/aee_bootstrap_w2_ubuntu_debian_implementation.md`.