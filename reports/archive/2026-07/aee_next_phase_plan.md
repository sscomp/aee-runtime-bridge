# AEE Runtime Bridge — Next Phase Planning & Readiness Assessment

> **Status:** PLANNING ONLY (read-only). No source edits, commits, pushes, deploys, restarts, stashes, merges, rebases, moves, or deletes performed to produce this document.
> **Author:** M2 (Hermes Agent, Abacus.ai runtime, glm-5.2 via ollama-cloud)
> **Date:** 2026-07-28 (Asia/Taipei)
> **Repository inspected (read-only):** `/home/ubuntu/hermes-runtime-bridge` @ `0b24ab741f81d43a0ca42f1045f71f9c9e4137d1` on `main`
> **Durable artifact:** `reports/aee_next_phase_plan.md` (this file)

---

## 1. Execution Timing

| Phase | Start (UTC) | End (UTC) | Duration |
|---|---|---|---|
| Repository + roadmap inspection | 2026-07-28 ~T1 | 2026-07-28 ~T2 | ~12 min |
| Phase 4 completion verification | (concurrent) | (concurrent) | (concurrent) |
| Bootstrap v1 spec §16/§17.3 cross-walk | (concurrent) | (concurrent) | (concurrent) |
| Next-phase identification + work-package decomposition | (concurrent) | (concurrent) | (concurrent) |
| Artifact write + verification | (concurrent) | (concurrent) | (concurrent) |

All evidence collected via read-only commands: `git rev-parse`, `/usr/bin/git log`, `git status --short`, `git ls-files`, `git ls-remote`, `ls`, `grep`, `sed -n`, `wc -l`, `head`, `python3 -c "..."` for `--help` smoke, `python3 -m pytest` for the Phase 4D targeted suite (read-only test execution).

---

## 2. Overall Verdict

**Next phase identified: Phase 5 — Bootstrap v1 Phase B (POSIX trampoline completion, integration tests + shared redaction, container/macOS E2E).**

Phase 4 (4A exit-code constants + 4B `aee install` CLI + 4C `aee update` CLI + 4D cross-slice integration tests) is **complete and pushed** to `origin/main` at HEAD `0b24ab7`. Local HEAD and remote `origin/main` are byte-for-byte identical (0/0 ahead-behind). The bootstrap v1 spec (`reports/aee_bootstrap_v1_spec.md`) §17.3 defines the canonical phased delivery order; cross-walking §17.3 against on-disk reality shows:

- **Phase A (W1, W2, W3, W4, W5) — Core:** W2 (lifecycle), W3 (doctor), W4 (install CLI), W5 (update CLI) all SHIPPED across Phase 2/3/4A–4D commits. **W1 (Windows adapter skeleton) NOT SHIPPED** — `win32` still resolves to `PlatformIdentity.UNKNOWN` (verified: `aee/platform/current.py:75,97`; `aee/deploy/adapters/windows.py` absent). Per the Phase 4 plan §2.2, W1 was **explicitly deferred to Phase 5** to keep Phase 4 focused on the POSIX CLI surface. **Phase A is therefore 80% complete (4 of 5 work items).**
- **Phase B (W6, W8, W10, W11, W12) — POSIX bootstrap:** Partial pre-existing substrate exists (Epic 9.3 `install.sh` shell wrapper, `bootstrap/lib/{detect.sh,deps.sh,macos_deps.sh}`, `bootstrap/manifests/{apt,brew}.deps.txt`) but the canonical Phase B deliverables per spec §16 are **NOT fully shipped**: `bootstrap/lib/resume.sh` absent, `bootstrap/manifests/{pwsh.deps.txt,python.requirements.in,python.requirements.lock}` absent, `aee/installer/redaction.py` absent, `aee/tests/test_bootstrap_integration.py` absent, `tests/e2e/{ubuntu.sh,debian.sh,macos.sh}` absent. One-click Bootstrap (§15.2) acceptance criteria NOT satisfied.
- **Phase C (W7, W13) — Windows:** Not started (blocked on W1).
- **Phase D (W9, W14, W15) — Hardening:** Not started.

**Rationale for choosing Phase 5 = Phase B over completing W1 (Windows):**
1. The Phase 4 plan explicitly deferred W1 to "Phase 5 to keep Phase 4 focused on the POSIX CLI surface" — the deferral was a scope decision, not a commitment to make W1 the *next* slice.
2. The bootstrap v1 spec §17.3 orders Phase B **before** Phase C (Windows), and Phase C is the only phase that depends on W1. Phase B unblocks §15.2 (One-click Bootstrap) on the three platforms that already have adapters (Ubuntu, Debian, macOS) — the highest-value next deliverable per spec §17.3's stated rationale ("land first-class support on the platforms that already have adapters ... before Windows").
3. W1 (Windows adapter *skeleton*) is a small, low-risk additive slice that can ship concurrently with or after Phase B without blocking it; Phase C (the actual Windows trampoline + E2E) is what depends on W1, and Phase C is explicitly after Phase B.
4. Phase B closes the largest current acceptance gap: §15.2 One-click Bootstrap is the headline acceptance criterion for the POSIX path and is currently FAILING (no `resume.sh`, no `redaction.py`, no E2E harness).

**Primary next-phase deliverable:** Complete Bootstrap v1 Phase B (W6 assessment/migration + W8 remaining manifests + W10 redaction module + integration tests + W11 container E2E + W12 macOS E2E) so that §15.2 One-click Bootstrap passes on Ubuntu, Debian, and macOS.

---

## 3. Baseline

| Field | Value | Source |
|---|---|---|
| Repository | `/home/ubuntu/hermes-runtime-bridge` | pwd |
| Branch | `main` | `git rev-parse --abbrev-ref HEAD` |
| HEAD (local) | `0b24ab741f81d43a0ca42f1045f71f9c9e4137d1` | `git rev-parse HEAD` |
| HEAD (remote `origin/main`) | `0b24ab741f81d43a0ca42f1045f71f9c9e4137d1` | `git ls-remote origin main` |
| Divergence | 0 ahead / 0 behind (byte-identical) | `git rev-list --left-right --count HEAD...origin/main` |
| Working tree (tracked) | Clean — zero tracked-file modifications | `git status --short --untracked-files=no` (empty) |
| Working tree (untracked) | 50+ pre-existing report/manifest `.md` files at repo root + untracked `reports/` tree (pre-existing from prior sessions; NOT Phase 5 artifacts) | `git status --short` |
| Phase 4 commits (shipped) | 4A=`770598f`, 4B=`87aaaaf`, 4C=`589c299`, 4D=`0b24ab7` | `/usr/bin/git log --oneline -4` |
| Phase 4D targeted suite | 58/58 PASS (0.31s) | `python3 -m pytest aee/tests/test_aee_phase4d_integration.py -q` |

---

## 4. Evidence Inspected

### 4.1 Authoritative roadmap / master plan / spec artifacts (read-only)

| Artifact | Path | Role |
|---|---|---|
| Bootstrap v1 spec (authoritative for work breakdown) | `reports/aee_bootstrap_v1_spec.md` (62.5K, 1293 lines) | §16 Work Breakdown (W1–W15), §17.3 Phased Delivery Order (Phase A/B/C/D), §15.1–15.3 Acceptance, §18 Production Safety |
| Phase 4 plan | `reports/aee_phase4_plan.md` (23.7K) | Phase 4 scope, deferral of W1 to "Phase 5", acceptance criteria |
| Phase 4A–4D reports | `reports/aee_phase4{a,b,c,d}_*.md` (17 files) | Implementation/review/commit/push evidence for each Phase 4 slice |
| Off-repo AEE master plan (§21 Epic 9) | `/home/ubuntu/Abacus/AEE/AEE_MASTER_PLAN.md` (457K) §21.3/§21.4 | Installer profile + runtime profile selection (the policy the bootstrap serves) |
| Phase 2/3 roadmap | `docs/Hermes_M2_Phase2_Phase3_Roadmap.md` | Phase 2/3 design context (not the bootstrap v1 work breakdown) |
| AEE iteration pattern skill | `~/.hermes/skills/software-development/aee-iteration-pattern/` | K-shape atomic commit pattern, acceptance verification recipe |

### 4.2 On-disk reality cross-walk (Phase A items vs. spec §16)

| Spec item | Deliverable | On-disk status @ `0b24ab7` | Shipped by |
|---|---|---|---|
| W1 — `WINDOWS` enum + `WindowsAdapter` skeleton | `aee/platform/current.py`, `aee/deploy/adapters/windows.py` | **NOT SHIPPED** — `PlatformIdentity` has only `LINUX`, `MACOS`, `UNKNOWN`; `win32` maps to `UNKNOWN` | — (deferred per Phase 4 plan §2.2) |
| W2 — Stage marker library | `aee/installer/lifecycle.py`, `aee/tests/test_installer_lifecycle.py` | **SHIPPED** (commit `44223ea`, W1 bootstrap core skeleton) | bootstrap W1/W2 |
| W3 — `aee doctor` CLI + health checks | `aee/doctor.py` (Phase 2), `aee/installer/doctor.py` | **SHIPPED** (commit `6b2609a`, Phase 2 doctor) | Phase 2 |
| W4 — `aee install` CLI surface | `aee/installer/cli_install.py`, `aee/tests/test_aee_phase4b_install_cli.py` | **SHIPPED** (commit `87aaaaf`, Phase 4B) | Phase 4B |
| W5 — `aee update` CLI surface | `aee/installer/update.py`, `aee/tests/test_aee_phase4c_update_cli.py` | **SHIPPED** (commit `589c299`, Phase 4C) | Phase 4C |
| Exit-code constants `{7..12}` | `aee/installer/backend.py`, `aee/installer/__init__.py`, `aee/tests/test_installer_exit_codes.py` | **SHIPPED** (commit `770598f`, Phase 4A) | Phase 4A |
| Cross-slice integration | `aee/tests/test_aee_phase4d_integration.py` (58 tests) | **SHIPPED** (commit `0b24ab7`, Phase 4D) | Phase 4D |

### 4.3 On-disk reality cross-walk (Phase B items vs. spec §16)

| Spec item | Deliverable | On-disk status @ `0b24ab7` |
|---|---|---|
| W6 — `install.sh` POSIX trampoline (ASSESS/MIGRATE existing tracked `install.sh`) | `install.sh` (tracked, Epic 9.3 shell wrapper, 248 lines), `bootstrap/lib/{detect.sh,deps.sh,macos_deps.sh}` (tracked) | **PARTIALLY SHIPPED** — Epic 9.3 `install.sh` is a thin trampoline delegating to `aee.cli` (verified via `head -30 install.sh`); `bootstrap/lib/detect.sh` (181 lines), `deps.sh` (318), `macos_deps.sh` (383) all tracked. **MISSING:** `bootstrap/lib/resume.sh` (spec §16 W6, spec §3 layout line 265) |
| W7 — `install.ps1` Windows trampoline | `install.ps1`, `bootstrap/lib/*.ps1` | **NOT SHIPPED** (Phase C) |
| W8 — Dependency manifests | `bootstrap/manifests/{apt.deps.txt,brew.deps.txt}` tracked; `pwsh.deps.txt`, `python.requirements.in`, `python.requirements.lock` | **PARTIALLY SHIPPED** — apt (50 lines) + brew (53 lines) present; **MISSING:** `pwsh.deps.txt` (Windows, Phase C), `python.requirements.in`, `python.requirements.lock` |
| W9 — Release channel + ref pinning + drift detection backend | extend `aee/installer/backend.py` (additive), `aee/tests/test_installer_channels.py` | **NOT SHIPPED** (Phase D). Phase 4C shipped the *CLI surface* for `--channel`/`--ref` + projected drift detection; the backend release-channel metadata is deferred to W9 (Phase D) per Phase 4 plan §2.2 |
| W10 — Integration tests + shared redaction module | `aee/installer/redaction.py` (new, proposed), `aee/tests/test_bootstrap_integration.py` | **NOT SHIPPED** — `aee/installer/redaction.py` absent (verified `ls aee/installer/redaction.py` → No such file); `aee/tests/test_bootstrap_integration.py` absent |
| W11 — Container E2E (Ubuntu, Debian) | `tests/e2e/{ubuntu.sh,debian.sh}` | **NOT SHIPPED** — `tests/e2e/` directory absent (verified `ls tests/e2e/`) |
| W12 — macOS E2E (CI runner) | `tests/e2e/macos.sh` | **NOT SHIPPED** (same — `tests/e2e/` absent) |
| W13 — Windows E2E (experimental) | `tests/e2e/windows.ps1` | **NOT SHIPPED** (Phase C) |
| W14 — Docs | `docs/aee/bootstrap/*.md` | **NOT SHIPPED** (Phase D) — `docs/aee/bootstrap/` absent |
| W15 — Acceptance gate | `tests/acceptance/bootstrap_v1_acceptance.py` | **NOT SHIPPED** (Phase D) — `tests/acceptance/` absent |

### 4.4 Existing tracked substrate Phase B will reuse

| Component | Path | Verified |
|---|---|---|
| `install.sh` (Epic 9.3 thin trampoline) | `install.sh` (248 lines, tracked at HEAD, commit `8a43494`) | YES — delegates to `aee.cli`, dry-run default, `--execute` guard (exit 6), 4 profiles |
| `tests/test_install_shell_wrapper.sh` (18 TAP integration tests) | tracked | YES — covers help, profiles, invalid profile (exit 2), execute guard, JSON shape |
| `bootstrap/lib/detect.sh`, `deps.sh`, `macos_deps.sh` | tracked (W2/W3 commits) | YES |
| `bootstrap/manifests/apt.deps.txt`, `brew.deps.txt` | tracked (W2/W3 commits) | YES |
| `tests/test_bootstrap_lib_{deps,detect,macos_deps}.sh` | tracked | YES |
| `aee/installer/lifecycle.py` (stage markers, resume support) | `aee/installer/lifecycle.py` (W2) | YES — provides the marker library `resume.sh` will consume |

---

## 5. Next Phase Identification and Rationale

### 5.1 Identification

**Phase 5 = Bootstrap v1 Phase B (POSIX trampoline completion + integration tests + container/macOS E2E)**

Per the bootstrap v1 spec §17.3, Phase B is the next phase after Phase A. Phase A is 80% complete (W1 deferred); the deferral was a deliberate scope decision documented in the Phase 4 plan §2.2, and W1 is not a blocker for Phase B (W1's only consumer is Phase C). Closing Phase B first maximizes value: §15.2 One-click Bootstrap on the three already-adapted platforms (Ubuntu, Debian, macOS) is the headline acceptance criterion the bootstrap v1 spec prioritizes.

### 5.2 Why not W1 (Windows adapter skeleton) first?

- The Phase 4 plan §2.2 explicitly says "recommend deferring to Phase 5 to keep Phase 4 focused on the POSIX CLI surface" — the deferral was scope management, not a sequencing commitment to make W1 the *next* slice.
- W1's only downstream consumer is Phase C (Windows trampoline + E2E). Phase C is explicitly after Phase B in §17.3.
- W1 is a small additive slice (`aee/platform/current.py` enum extend + `aee/deploy/adapters/windows.py` skeleton). It can be inserted as an early Phase 5 slice **concurrently** with Phase B work, or deferred to the start of Phase C — either is safe. This plan treats W1 as an optional early slice inside Phase 5 (Slice 5A) so Phase A is closed before Phase B's E2E lands, but W1 is NOT the primary deliverable.

### 5.3 Why not Phase D (W9 release channels / W14 docs / W15 acceptance gate) next?

- Phase D depends on Phase B (per §17.3, "After Phase D, Reproducible Deployment (§15.1) and Automated Agent Deployment (§15.3) pass" — both §15.1 and §15.3 require the E2E harnesses that Phase B ships).
- Phase 4C already shipped the *CLI surface* for `--channel`/`--ref` + projected drift detection; the W9 backend release-channel metadata is hardening, not a blocker for §15.2.
- The acceptance gate (W15) requires the E2E harnesses (W11/W12) to exist to be meaningful.

### 5.4 Primary deliverable

**Complete Bootstrap v1 Phase B so that §15.2 One-click Bootstrap passes on Ubuntu, Debian, and macOS.**

This requires:
- W6: ASSESS the existing tracked `install.sh` (Epic 9.3) and either REUSE it as the canonical POSIX trampoline (recommended — it is already a thin dry-run-by-default trampoline delegating to `aee.cli`) or formally supersede it with a documented rationale; add the missing `bootstrap/lib/resume.sh` helper.
- W8: Add the missing `bootstrap/manifests/python.requirements.in` + `python.requirements.lock` (the python lock per §6 dependency strategy). (`pwsh.deps.txt` is Windows, Phase C — out of scope for Phase 5.)
- W10: Implement the shared `aee/installer/redaction.py` module (§8.2 PROPOSAL — verified: no reusable redaction regex exists in `aee/artifacts/policy.py`); add `aee/tests/test_bootstrap_integration.py` covering stage transitions, resume, redaction.
- W11: Add `tests/e2e/ubuntu.sh` + `tests/e2e/debian.sh` container E2E harnesses.
- W12: Add `tests/e2e/macos.sh` macOS E2E harness (CI runner).

---

## 6. Ordered Work Packages

Each work package is a single AEE K-shape atomic commit (one task, one deliverable), following the existing pattern in `~/.hermes/skills/software-development/aee-iteration-pattern/`. Implementation / review / commit / push are **separate tickets** for every slice (per AEE v3.1 hard rule #1: commit authority is Hermes-exclusive; per the Phase 4 precedent: each slice had separate implementation, review, atomic-commit, and push reports).

### 6.1 Slice 5A — W1 Windows adapter skeleton (closes Phase A)

| Field | Value |
|---|---|
| Scope | Add `PlatformIdentity.WINDOWS` to `aee/platform/current.py`; add `aee/deploy/adapters/windows.py` skeleton (detect → `HostCapabilities` with `class=container`/`laptop` TBD, `materialize` stub returning NOT_SUPPORTED, `health_check` returning UNKNOWN); update `aee/tests/test_platform_bootstrap.py` so `win32` resolves to `WINDOWS` (not `UNKNOWN`). |
| Files likely affected | `aee/platform/current.py` (additive enum + resolver branch), `aee/deploy/adapters/windows.py` (new), `aee/deploy/adapters/__init__.py` (additive registry), `aee/tests/test_platform_bootstrap.py` (modify — flip `win32` expectation). |
| Acceptance criteria | (1) `PlatformIdentity.WINDOWS` member exists; (2) `resolve_platform_identity("win32") == PlatformIdentity.WINDOWS`; (3) `WindowsAdapter` implements the `PlatformAdapter` Protocol from `aee/deploy/adapters/base.py` with `materialize` returning a NOT_SUPPORTED result (no side effects); (4) `aee/tests/test_platform_bootstrap.py` updated test passes; (5) full `aee/tests` suite: 0 new failures vs. baseline; (6) AST scan: no `subprocess`/`os.system`/`os.popen` in `windows.py`. |
| Tests | Update `test_platform_bootstrap.py::test_win32_resolves_unknown` → `test_win32_resolves_windows`; new `test_windows_adapter_skeleton.py` (~8 tests: enum membership, resolver, adapter protocol conformance, NOT_SUPPORTED materialize, UNKNOWN health_check, no-side-effects AST scan). |
| Dependencies | None (Phase A closure; independent of Phase B). |
| Risks | R-5A-1: Existing tests assert `win32 → UNKNOWN` (verified `aee/tests/test_platform_bootstrap.py:119`, `aee/installer/lifecycle.py:588,597`); flipping the expectation is a test modification, not a pure addition — must update both call sites. R-5A-2: `aee/installer/lifecycle.py:588` comment "add a WINDOWS identity + WindowsAdapter will modify" must be reconciled (the comment anticipated this slice). |
| Separate tickets | Implementation / Review / Atomic Commit / Push (4 tickets, K-shape). |
| Optional | This slice can be deferred to the start of Phase C if the user prefers to keep Phase 5 strictly Phase B. Recommend including it to close Phase A. |

### 6.2 Slice 5B — W6 POSIX trampoline assessment + `resume.sh`

| Field | Value |
|---|---|
| Scope | Per spec §16 W6 and §3 layout: ASSESS the existing tracked `install.sh` (Epic 9.3, commit `8a43494`) against the spec's thin-trampoline contract (§3.1: detect platform → ensure Python/git → `git clone` → hand off to `python -m aee.installer.cli`). Document the assessment in the implementation report. **Reuse** `install.sh` as the canonical POSIX trampoline (recommended — it already delegates to `aee.cli` and is dry-run by default) OR formally supersede with rationale. Add the missing `bootstrap/lib/resume.sh` helper that consumes the W2 lifecycle marker library (`aee/installer/lifecycle.py`) to resume from the last completed stage. |
| Files likely affected | `bootstrap/lib/resume.sh` (new), `install.sh` (modify ONLY if assessment finds gaps — e.g., add `--resume` flag wiring, ensure `resume.sh` is sourced). If `install.sh` is reused as-is, only `bootstrap/lib/resume.sh` is new. |
| Acceptance criteria | (1) Assessment report documents the decision (reuse vs. supersede) with evidence against §3.1's 5-step contract; (2) `bootstrap/lib/resume.sh` exists and reads the last stage marker via the W2 lifecycle library; (3) `install.sh --resume` (or equivalent) invokes `resume.sh`; (4) `tests/test_bootstrap_lib_resume.sh` (new) passes ≥ 10 tests covering: marker read, missing marker (fresh start), corrupted marker, resume to last stage, resume to specific stage (`--from`), no side effects in dry-run; (5) existing `tests/test_install_shell_wrapper.sh` 18/18 still pass byte-for-byte; (6) full tracked test suite: 0 new failures. |
| Tests | `tests/test_bootstrap_lib_resume.sh` (new, ~10 TAP tests). |
| Dependencies | W2 lifecycle library (SHIPPED). |
| Risks | R-5B-1: `install.sh` is tracked and has 18 TAP tests — modifying it risks breaking `test_install_shell_wrapper.sh` byte-for-byte. Mitigation: prefer additive `--resume` flag; if `install.sh` modification is needed, diff the test output before/after. R-5B-2: `resume.sh` parsing markers created by the Python lifecycle library must match the marker format exactly — verify by reading `aee/installer/lifecycle.py` marker write/read functions. |
| Separate tickets | Implementation / Review / Atomic Commit / Push (4 tickets, K-shape). |

### 6.3 Slice 5C — W8 Python dependency manifests

| Field | Value |
|---|---|
| Scope | Add `bootstrap/manifests/python.requirements.in` (top-level Python deps) and `bootstrap/manifests/python.requirements.lock` (uv-pip-compile lock, per §6 dependency strategy and `dependency-locking` skill). These are consumed by the installer's dependency-install stage. (`pwsh.deps.txt` is Windows — Phase C, out of scope.) |
| Files likely affected | `bootstrap/manifests/python.requirements.in` (new), `bootstrap/manifests/python.requirements.lock` (new), `scripts/compile-deps.sh` or `scripts/verify-deps.sh` (modify if they don't already emit the python lock — verify at implementation time). |
| Acceptance criteria | (1) `python.requirements.in` lists the runtime Python deps (matches `requirements.in` at repo root if present — verify); (2) `python.requirements.lock` is a `uv pip compile`-generated lock with hash-pinned entries (per `dependency-locking` skill); (3) `aee doctor` (Phase 2) reads the lock and reports drift if on-disk venv diverges; (4) `tests/test_bootstrap_manifests_python.sh` (new) or `aee/tests/test_installer_manifests.py` (new) passes ≥ 6 tests: in-file parseable, lock hash-pinned, in/lock consistent, doctor drift detection; (5) existing apt/brew manifest tests still pass. |
| Tests | `aee/tests/test_installer_manifests.py` (new, ~6 tests). |
| Dependencies | None (independent of 5A/5B). |
| Risks | R-5C-1: `requirements.in`/`requirements.lock` already exist at repo root (tracked, 46.3K lock) — the `bootstrap/manifests/python.requirements.{in,lock}` may be a *copy* or a *symlink* per spec §3 layout. Assessment needed: if the root lock is the canonical source, `bootstrap/manifests/python.requirements.lock` should be generated from the same `.in` to avoid divergence. R-5C-2: `uv pip compile` reproducibility depends on the index URL — pin the index in the `.in` or via `--index-url` flag. |
| Separate tickets | Implementation / Review / Atomic Commit / Push (4 tickets, K-shape). |

### 6.4 Slice 5D — W10 Shared redaction module + integration tests

| Field | Value |
|---|---|
| Scope | Implement `aee/installer/redaction.py` (spec §8.2 PROPOSAL — verified: no reusable redaction regex exists in `aee/artifacts/policy.py`; the policy module has classification logic, not redaction regexes). The module provides a single `redact(text: str, *, keep_keys: bool = True) -> str` function with a canonical regex set covering: API keys (Bearer tokens, `sk-...`, `AKIA...`), env-file `KEY=value` patterns (keep key, redact value), private keys (`-----BEGIN ... PRIVATE KEY-----`), connection strings with passwords, and the Hermes/Abacus env var vocabulary (`HERMES_API_KEY`, `ABACUSAI_API_KEY`, `OPENAI_API_KEY`, etc.). Add `aee/tests/test_bootstrap_integration.py` covering stage transitions, resume, and redaction end-to-end. |
| Files likely affected | `aee/installer/redaction.py` (new), `aee/installer/__init__.py` (additive re-export), `aee/tests/test_bootstrap_integration.py` (new), `aee/tests/test_redaction.py` (new, unit tests for the regex set). |
| Acceptance criteria | (1) `aee/installer/redaction.py` exposes `redact(text, *, keep_keys=True) -> str`; (2) unit tests (`test_redaction.py`) ≥ 15 tests covering each regex class + edge cases (empty input, no secrets, partial matches, multi-line); (3) integration tests (`test_bootstrap_integration.py`) ≥ 12 tests covering: stage 00→06 transitions, resume from mid-stage, redaction applied to log output, redaction applied to diagnostics bundle, no `subprocess` in dry-run paths (AST scan); (4) `aee doctor --bundle` (if shipped) redacts env values before writing; (5) full `aee/tests` suite: 0 new failures. |
| Tests | `aee/tests/test_redaction.py` (new, ~15), `aee/tests/test_bootstrap_integration.py` (new, ~12). |
| Dependencies | W2 lifecycle (SHIPPED), Slice 5B `resume.sh` (for resume integration tests — can stub if 5B not yet landed). |
| Risks | R-5D-1: Redaction regex set must be kept consistent across shell, PowerShell, and Python layers (spec §17.1 R5). The shared module is the Python layer; shell/PowerShell redaction is out of scope for Phase 5 (shell trampoline is thin, no business logic). Document the seam. R-5D-2: Regex false-positives (e.g., redacting a non-secret value that happens to match `sk-...`). Mitigation: conservative regex + unit test edge cases. R-5D-3: `aee/artifacts/policy.py` has classification logic that looks like redaction — verified NOT a reusable redaction API (per spec §17.1 R5); do not cite it as the source. |
| Separate tickets | Implementation / Review / Atomic Commit / Push (4 tickets, K-shape). |

### 6.5 Slice 5E — W11 Container E2E (Ubuntu + Debian)

| Field | Value |
|---|---|
| Scope | Add `tests/e2e/ubuntu.sh` and `tests/e2e/debian.sh` container E2E harnesses. Each harness: spins up a clean container (Docker image or `unshare --mount` chroot — verify container availability at implementation time; Abacus container has Docker per AEE-9.5 case study), runs `install.sh --profile {full,mini} --dry-run` → `--execute` → `aee doctor`, asserts `AGENT_READY` and `aee doctor` exit 0. Per §15.2: total wall-clock under 5 min excluding network, no interactive prompts in `--yes` mode. |
| Files likely affected | `tests/e2e/ubuntu.sh` (new), `tests/e2e/debian.sh` (new), `tests/e2e/lib/common.sh` (new, shared helpers), `tests/e2e/README.md` (new, how to run). |
| Acceptance criteria | (1) `tests/e2e/ubuntu.sh` runs to completion on an Ubuntu 22.04 container and `aee doctor` exits 0; (2) `tests/e2e/debian.sh` runs to completion on a Debian stable container and `aee doctor` exits 0; (3) both produce `evidence.json` consumable by the test harness (§12.4); (4) both complete in < 5 min excluding network; (5) `--yes` mode requires no interactive prompts; (6) a failure in any stage produces a non-zero exit code + diagnostics bundle path. |
| Tests | The E2E harnesses ARE the tests (no separate unit test file). |
| Dependencies | Slices 5B (resume.sh), 5C (python manifests), 5D (redaction) all landed; `install.sh` reusable as the trampoline. Docker available on the host (Abacus container — verified by AEE-9.5 case study). |
| Risks | R-5E-1: Docker may not be available in the Abacus container for all session types. Mitigation: use `unshare --mount` + `debootstrap` as a fallback, or mark the E2E as `@pytest.mark.e2e` and skip if Docker absent. R-5E-2: Network flakiness during `git clone`/`apt install` makes E2E non-deterministic. Mitigation: `--offline` mode with pre-staged artifacts for the deterministic portion; network-dependent steps marked and allowed to fail gracefully. R-5E-3: Wall-clock > 5 min if Python 3.11 build from source (Debian pyenv fallback, §13.2). Mitigation: use deadsnakes-equivalent or pre-built Python where available; document the slow path. |
| Separate tickets | Implementation / Review / Atomic Commit / Push (4 tickets, K-shape). |

### 6.6 Slice 5F — W12 macOS E2E (CI runner)

| Field | Value |
|---|---|
| Scope | Add `tests/e2e/macos.sh` macOS E2E harness. macOS adapter accepts only `developer` profile (verified `aee/deploy/adapters/macbook.py` docstring). The harness runs on a macOS CI runner (GitHub Actions `macos-latest` or equivalent) — NOT on the Abacus container (which is Linux). |
| Files likely affected | `tests/e2e/macos.sh` (new), `tests/e2e/lib/common.sh` (shared with 5E), `.github/workflows/e2e-macos.yml` (new, if CI runner is GitHub Actions — verify CI provider at implementation time). |
| Acceptance criteria | (1) `tests/e2e/macos.sh` runs to completion on a macOS CI runner with `--profile developer` and `aee doctor` exits 0; (2) produces `evidence.json`; (3) < 5 min excluding network; (4) `--yes` mode no prompts; (5) `--no-brew` mode exercised (§13.3); (6) Homebrew prefix detected via `brew --prefix` (not hardcoded). |
| Tests | The E2E harness IS the test. |
| Dependencies | Slice 5E (shared `common.sh`); macOS CI runner availability. |
| Risks | R-5F-1: No macOS runner on the Abacus container — must use external CI. Mitigation: GitHub Actions `macos-latest` (if repo has GHA configured — verify); otherwise document as "requires manual run on a Mac". R-5F-2: `launchd` user-scope not restartable by `launchctl bootstrap` on older macOS (§17.1 R10). Mitigation: floor macOS 13 (§1.4); document workaround. |
| Separate tickets | Implementation / Review / Atomic Commit / Push (4 tickets, K-shape). |

### 6.7 Slice 5G — Phase 5 integration report + acceptance gate dry-run

| Field | Value |
|---|---|
| Scope | Cross-slice integration report verifying Phase 5 (5A–5F) cohere with Phase 2/3/4; dry-run of the §15.2 One-click Bootstrap acceptance criteria (NOT the W15 acceptance gate itself — that's Phase D). Confirm `aee doctor`, `aee install`, `aee update`, `aee prepare` all still coexist (Phase 4D invariant) + the new E2E harnesses. |
| Files likely affected | `aee/tests/test_phase5_integration.py` (new, ~10 tests), `reports/aee_phase5_implementation.md`, `reports/aee_phase5_atomic_commit.md`, `reports/aee_phase5_push.md`. |
| Acceptance criteria | (1) All Phase 5 slices coexist; (2) full `aee/tests` suite passes with 0 new failures vs. Phase 4D baseline; (3) Phase 4D targeted suite (`test_aee_phase4d_integration.py`) 58/58 still pass; (4) §15.2 One-click Bootstrap dry-run passes on Ubuntu/Debian/macOS E2E harnesses; (5) push to `origin/main` verified 0/0 ahead-behind. |
| Tests | `aee/tests/test_phase5_integration.py` (new, ~10). |
| Dependencies | Slices 5A–5F all landed. |
| Risks | R-5G-1: §15.2 acceptance requires actual E2E on clean machines — the dry-run validates the harness wiring, not the §15.2 PASS. Document that §15.2 final PASS requires an operator-authorized clean-machine run. |
| Separate tickets | Implementation / Review / Atomic Commit / Push (4 tickets, K-shape). |

### 6.8 Ordering

5A (W1, optional) → 5B (W6) → 5C (W8) → 5D (W10) → 5E (W11) → 5F (W12) → 5G (integration).

- 5A is optional and can be deferred to Phase C; recommended to close Phase A.
- 5B must precede 5D (resume.sh is consumed by integration tests) and 5E (E2E exercises resume).
- 5C is independent and can run in parallel with 5B/5D.
- 5D must precede 5E (redaction is exercised by E2E diagnostics bundle).
- 5E must precede 5F (shared `common.sh`) and 5G.
- 5F can run concurrently with 5E if `common.sh` is extracted first.

---

## 7. Immediate Next Ticket

**Slice 5B — W6 POSIX trampoline assessment + `bootstrap/lib/resume.sh`**

Rationale:
1. It is the first Phase B slice that is NOT optional (unlike 5A) and NOT parallelizable-only (unlike 5C, which can wait).
2. It unblocks 5D (integration tests exercise resume) and 5E (E2E exercises resume).
3. It has the smallest scope uncertainty: the assessment is read-only (compare `install.sh` against §3.1's 5-step contract), and the deliverable (`resume.sh`) is a single new file consuming the already-shipped W2 lifecycle library.
4. It has a clean K-shape atomic commit boundary: 1 new file (`resume.sh`) + 1 new test file (`test_bootstrap_lib_resume.sh`) + optionally 1 modified file (`install.sh` additive `--resume` flag). No production source outside `install.sh` is touched.
5. It is the lowest-risk slice in Phase 5: R-5B-1 (breaking `test_install_shell_wrapper.sh`) is mitigated by additive `--resume` flag; R-5B-2 (marker format mismatch) is mitigated by reading the W2 lifecycle library's marker write/read functions directly.

**Recommended first executable ticket:** `Phase 5B — W6 POSIX trampoline assessment + resume.sh implementation` (K-shape: implementation → review → atomic commit → push, 4 separate tickets).

---

## 8. Git Status and Diff Summary

### 8.1 Git status (read-only, pre-Phase-5)

```
$ git status --short --untracked-files=no
[end — empty; zero tracked-file modifications]

$ git status --short | head -5
?? AEE_7_7d_7e_MANIFEST.json
?? AEE_7_7d_7e_STAGING_BOUNDARY.md
?? AEE_7_8_K2_IMPLEMENTATION_REPORT_20260712.md
... (50+ pre-existing untracked report/manifest .md files from prior sessions;
     NONE are Phase 5 artifacts; NONE will be staged)
```

### 8.2 Diff summary

```
$ git diff HEAD --stat
[end — empty; zero tracked-file changes]

$ git diff --cached --stat
[end — empty; zero staged changes]
```

### 8.3 Remote divergence

```
$ git rev-list --left-right --count HEAD...origin/main
0   0
```

Local HEAD == remote `origin/main` == `0b24ab741f81d43a0ca42f1045f71f9c9e4137d1`. Phase 4 is fully pushed; Phase 5 starts from a clean baseline.

---

## 9. Artifact Verification

```
$ ls -la reports/aee_next_phase_plan.md
-rw-rw-r-- 1 ubuntu ubuntu <size> <date> reports/aee_next_phase_plan.md

$ wc -l reports/aee_next_phase_plan.md
<N> reports/aee_next_phase_plan.md

$ sha256sum reports/aee_next_phase_plan.md
<sha256> reports/aee_next_phase_plan.md
```

(Exact `ls -la` size, `wc -l` line count, and `sha256sum` are filled in by the verification commands run immediately after this file is written. The sha is NOT embedded in this file body — the self-referential sha paradox makes an in-band sha unstable, per the Phase 4 plan §8 precedent.)

---

## 10. Production Safety

- **No source code modified** to produce this plan. Only read-only commands (`git rev-parse`, `/usr/bin/git log`, `git status`, `git ls-files`, `git ls-remote`, `ls`, `grep`, `sed -n`, `wc -l`, `head`, `python3 -c "..."` for `--help` smoke, `python3 -m pytest` for the Phase 4D targeted suite) were run.
- **No commits, no pushes, no deploys, no restarts, no merges, no rebases, no stashes, no moves, no deletes.**
- **No `git add` performed.**
- **No `subprocess` / `os.system` / `os.popen` invoked** by the planning process.
- **No secrets read.** No `.env` or config files modified.
- **No tracked files modified.** `git diff HEAD --stat` is empty.
- The `python3 -m pytest aee/tests/test_aee_phase4d_integration.py` run is read-only test execution (no source mutation, no DB writes).
- The 50+ untracked report/manifest `.md` files in the working tree are pre-existing residue from prior sessions; they are NOT Phase 5 artifacts and will NOT be staged by any Phase 5 slice (explicit-path staging only, per the K-shape contract).

---

## 11. Remaining Risks

| # | Risk | Mitigation |
|---|---|---|
| RR1 | **W1 (Windows adapter) deferral ambiguity.** The Phase 4 plan deferred W1 to "Phase 5" but did not commit to making it the *next* slice. This plan treats W1 as optional Slice 5A; the user may prefer to defer W1 to Phase C entirely. | Surface W1 as optional in the authorization request; proceed with 5B as the first non-optional slice regardless. |
| RR2 | **`install.sh` modification risk.** Slice 5B may need to modify `install.sh` (tracked, 18 TAP tests). | Prefer additive `--resume` flag; if modification is needed, diff `test_install_shell_wrapper.sh` output before/after. |
| RR3 | **Python requirements lock divergence.** `requirements.lock` exists at repo root; `bootstrap/manifests/python.requirements.lock` may duplicate or diverge. | Slice 5C assessment: if the root lock is canonical, generate `bootstrap/manifests/python.requirements.lock` from the same `.in` via `uv pip compile` to avoid two sources of truth. |
| RR4 | **Redaction regex consistency across shell/PowerShell/Python.** Spec §17.1 R5 requires consistency; Slice 5D only ships the Python layer. | Document the seam; shell/PowerShell redaction is out of scope for Phase 5 (thin trampoline, no business logic). Phase C (Windows) will need a PowerShell redaction module. |
| RR5 | **E2E non-determinism.** Network flakiness + Python-from-source build (Debian pyenv) makes E2E wall-clock variable. | `--offline` mode with pre-staged artifacts for the deterministic portion; mark network-dependent steps; floor macOS 13. |
| RR6 | **Docker availability on Abacus container.** Slice 5E needs Docker for container E2E. AEE-9.5 case study confirms Docker works, but availability may vary by session. | `@pytest.mark.e2e` skip if Docker absent; `unshare --mount` + `debootstrap` fallback. |
| RR7 | **macOS CI runner availability.** Slice 5F needs a macOS runner; the Abacus container is Linux. | GitHub Actions `macos-latest` if GHA configured; otherwise document as "requires manual run on a Mac". |
| RR8 | **§15.2 acceptance vs. dry-run.** Slice 5G dry-runs §15.2; final §15.2 PASS requires an operator-authorized clean-machine run. | Document explicitly; Phase D (W15) owns the final acceptance gate. |
| RR9 | **Phase B scope boundary vs. W9 (release channels).** Phase 4C shipped the `--channel`/`--ref` CLI surface + projected drift; W9 backend release-channel metadata is Phase D. | Phase 5 does NOT touch W9; the `aee update --channel` CLI surface from Phase 4C is the contract, the backend is deferred. |
| RR10 | **Untracked working-tree residue staging risk.** 50+ untracked `.md` files could be accidentally staged. | Explicit-path staging only (`git add <path1> <path2> ...`); never `git add -A`. Per K-shape contract and Phase 4 precedent. |

---

## 12. Review Ready / Commit Ready

- **Review Ready:** YES — this plan is a complete read-only planning artifact; no source modifications to review.
- **Commit Ready:** NO — this is a planning document, not an implementation. No code is staged, no commit is proposed. The durable artifact (`reports/aee_next_phase_plan.md`) is an untracked report file; it is NOT a source commit candidate. Authorization is required to begin Phase 5 implementation (Slice 5B first).

---

## 13. Telegram Attempt and Result

**Not attempted.** This is a read-only planning work order, not an AEE-MINI task or a direct repository push from the orchestrator. Per the user's notification preference (2026-06-06 confirmed): "健康/正常保持靜默" — a planning artifact with no anomaly/event does not warrant a Telegram notification. The AEE-MINI Telegram rule (2026-07-13: send on every AEE-MINI task) does not apply to read-only planning artifacts.

If a Telegram summary is required, it can be sent separately via:
```
hermes send --to telegram:5132341473 --subject "AEE Phase 5 plan ready" --file reports/aee_next_phase_plan.md --json
```

---

## 14. Cross-References

- Bootstrap v1 spec (authoritative): `reports/aee_bootstrap_v1_spec.md` §16 (W1–W15), §17.3 (Phase A/B/C/D), §15.1–15.3 (Acceptance), §18 (Production Safety), §3.1 (thin trampoline contract), §8.2 (redaction PROPOSAL).
- Phase 4 plan: `reports/aee_phase4_plan.md` (scope, W1 deferral §2.2, acceptance criteria).
- Phase 4A–4D reports: `reports/aee_phase4{a,b,c,d}_*.md` (17 files).
- Phase 2/3 roadmap: `docs/Hermes_M2_Phase2_Phase3_Roadmap.md` (design context).
- Off-repo AEE master plan §21 Epic 9: `/home/ubuntu/Abacus/AEE/AEE_MASTER_PLAN.md` §21.3 (installer profile), §21.4 (runtime profile selection).
- AEE iteration pattern skill (K-shape atomic commit): `~/.hermes/skills/software-development/aee-iteration-pattern/`.
- Dependency locking skill (W8 python lock): `~/.hermes/skills/devops/dependency-locking/`.
- AEE-9.5 Docker case study (E2E Docker availability): `~/.hermes/skills/software-development/aee-iteration-pattern/references/aee95-docker-profiles-atomic-commit-case-study.md`.

---

_End of plan._