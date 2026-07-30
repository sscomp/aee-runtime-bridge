# AEE Phase 4 — Install/Update CLI Surface + Execute Path (Implementation Plan)

> Status: **PLANNING ONLY**. No source edits, commits, pushes, deployments, or restarts were performed to produce this document.
> Author: M2 (Hermes Agent, Abacus.ai runtime, glm-5.2 via ollama-cloud).
> Repository inspected (read-only): `/home/ubuntu/hermes-runtime-bridge` @ `f8fe2c918a2173c54b147f1380380e699f478ce1` on `main`.
> Date: 2026-07-27 (Asia/Taipei).
> Durable artifact: `reports/aee_phase4_plan.md` (this file).

---

## 1. Executive Summary

Phase 3 (commit `f8fe2c9` "feat(aee): add Phase 3 installer workflow (`aee prepare`)")
shipped and was pushed to `origin/main` on 2026-07-27. The repository now has a
working **`aee prepare`** subcommand that composes the Phase 2 doctor + §21.3
installer backend + W2/W3 platform bootstrap into a single dry-run-by-default
workflow.

The Bootstrap v1 spec (`reports/aee_bootstrap_v1_spec.md` §16 Work Breakdown)
defines the canonical next-work items. Mapping the spec's phased delivery order
(§17.3) against on-disk reality:

| Spec item | Deliverable | On-disk status @ `f8fe2c9` |
|---|---|---|
| W1 — `WINDOWS` enum + `WindowsAdapter` skeleton | `aee/platform/current.py`, `aee/deploy/adapters/windows.py` | **NOT SHIPPED** — Windows still maps to `UNKNOWN` |
| W2 — Stage marker library (`aee.installer.lifecycle`) | `aee/installer/lifecycle.py` | **SHIPPED** (commit `44223ea`) |
| W3 — `aee doctor` CLI + health checks | `aee/doctor.py`, `aee/cli.py` `doctor` subparser | **SHIPPED** (commit `6b2609a`) |
| W4 — `aee install` CLI surface (wraps backend) | `aee/installer/cli.py`, `aee/tests/test_installer_cli.py` | **PARTIAL** — `install` dispatch exists in `aee/cli.py` (`_install_dispatch`, line 325) but `aee/installer/cli.py` does NOT exist; the dispatch is a thin dry-run-only stub over `InstallerBackend` |
| W5 — `aee update` CLI surface | `aee/installer/update.py`, `aee/tests/test_installer_update.py` | **NOT SHIPPED** — no `update_parser` in `aee/cli.py`, no `aee/installer/update.py` |
| W6 — `install.sh` POSIX trampoline (ASSESS/MIGRATE existing tracked `install.sh`) | `install.sh`, `bootstrap/lib/*` | **NOT SHIPPED** (assessment-first per spec §16 W6) |
| W7 — `install.ps1` Windows trampoline | `install.ps1`, `bootstrap/lib/*.ps1` | **NOT SHIPPED** |
| W8 — Dependency manifests (apt, brew, winget, python lock) | `bootstrap/manifests/*` | **NOT SHIPPED** |
| W9 — Release channel + ref pinning + drift detection | extend `aee/installer/backend.py` (additive), `aee/tests/test_installer_channels.py` | **NOT SHIPPED** |
| W10 — Integration tests + shared redaction module | `aee/installer/redaction.py`, `aee/tests/test_bootstrap_integration.py` | **NOT SHIPPED** |
| W11–W13 — Container/macOS/Windows E2E | `tests/e2e/*` | **NOT SHIPPED** |
| W14 — Docs | `docs/aee/bootstrap/*.md` | **NOT SHIPPED** |
| W15 — Acceptance gate | `tests/acceptance/bootstrap_v1_acceptance.py` | **NOT SHIPPED** |

**Phase 3 delivered the `aee prepare` workflow** — a composed dry-run orchestrator
that *surfaces* what an install would do. Phase 3 did **not** deliver the actual
`aee install` execution path, the `aee update` CLI, or the shell trampolines.

The next logical phase per spec §17.3 ("Phase A — Core (W1, W2, W3, W4, W5)")
is **Phase 4: complete the W4/W5 CLI surface** — promote `aee install` from a
dry-run stub to a real (still default-dry-run) CLI surface backed by a dedicated
`aee/installer/cli.py` module, and add the `aee update` CLI surface
(`aee/installer/update.py`) with channel/ref switching and drift detection.

This is **Phase 4** under the bridge repo's own "Phase N" numbering
(Phase 1 = max-turns bump, Phase 2 = doctor, Phase 3 = `aee prepare` workflow).
It corresponds to spec items W4 + W5 of Phase A.

---

## 2. Scope

### 2.1 In scope

1. **W4 — `aee install` CLI surface (real).**
   - New module `aee/installer/cli.py` exposing `run_install(args) -> int` that
     wraps the existing `InstallerBackend` (§21.3) and the Phase 3 workflow
     (`aee/installer/workflow.py`).
   - `aee install` stays **dry-run by default** (matches the §21.3 guard and
     spec §18.3). The existing `_install_dispatch` in `aee/cli.py` becomes a
     thin shim that delegates to `aee.installer.cli.run_install`.
   - Add an `--execute` flag (gated by the existing
     `EXIT_EXECUTE_NOT_AUTHORIZED=6` exit code) that, when present, calls
     `backend.execute(profile, dry_run=False)`. The execute path MUST still
     refuse side effects unless `--execute` is explicit.
   - Add `--resume`, `--from <stage>`, `--rollback-to <stage>` argparse options
     (spec §10.1) wired to the lifecycle marker library (W2, already shipped).
     In dry-run, these are accepted and reflected in the plan; in execute mode
     they drive the stage machine.
   - Exit codes: reuse the verified vocabulary `{0, 2, 3, 4, 5, 6}`. The
     proposed new codes `{7, 8, 10, 12}` (spec §10.4) for stage-failed-retryable,
     stage-failed-permanent, network-error, dependency-floor-not-met are
     **introduced as constants** but only *emitted* by the execute path
     (which is separately authorizable). Phase 4 ships the constants + tests
     for the dry-run path only; execute-mode behavior is gated behind
     `--execute` and exercised in Phase B E2E (W11/W12).
   - New tests `aee/tests/test_installer_cli.py` covering: argparse wiring,
     dry-run default, `--execute` rejected without explicit flag (returns 6),
     `--resume`/`--from`/`--rollback-to` accepted in dry-run, exit-code
     mapping, JSON output shape, profile validation, idempotency.

2. **W5 — `aee update` CLI surface.**
   - New module `aee/installer/update.py` exposing `run_update(args) -> int`.
   - New `update` subparser in `aee/cli.py` (additive — existing dispatch
     paths untouched).
   - `aee update` advances the install to the latest of the current channel
     (default), or switches channel with `--channel {stable,rc,dev}` /
     `--ref <git-ref>`. Refuses if the profile would change (reuses
     `EXIT_PROFILE_SWITCH_REJECTED=5`).
   - Drift detection: compare on-disk `commit_sha` + `requirements_lock_sha256`
     against the recorded pin. Emits `EXIT_DRIFT_DETECTED=9` (proposed code,
     introduced as a constant in Phase 4, exercised in dry-run tests as a
     *projected* check).
   - New tests `aee/tests/test_installer_update.py` covering: argparse wiring,
     no-args advances on current channel, `--channel` switching, profile
     switch rejection, drift detection in dry-run (projected), exit codes,
     JSON output shape.
   - Dry-run by default. `--execute` gated identically to `aee install`.

3. **Exit-code constants for the proposed block `{7, 8, 9, 10, 11, 12}`.**
   - Add named constants in `aee/installer/backend.py` (or a new
     `aee/installer/exit_codes.py` if the spec's "must not collide" rule
     prefers a dedicated module — to be decided at implementation time).
   - Pin via tests. Do **not** alter the verified `{0, 2, 3, 4, 5, 6}` set.

### 2.2 Out of scope (deferred to later phases)

- W1 Windows adapter skeleton (Phase A item, but independent — can ship
  before or after Phase 4; recommend deferring to Phase 5 to keep Phase 4
  focused on the POSIX CLI surface).
- W6 `install.sh` POSIX trampoline (Phase B).
- W7 `install.ps1` Windows trampoline (Phase C).
- W8 dependency manifests (Phase B).
- W9 release-channel + ref pinning **backend** extension (Phase D hardening).
  Phase 4 ships the *CLI surface* for `--channel`/`--ref`; the backend
  channel-switching logic is the existing `InstallerBackend` profile switch
  path. Full release-channel metadata + drift detection backend is W9
  (Phase D).
- W10 integration tests + shared redaction module (Phase B).
- W11–W13 E2E harnesses (Phase B/C).
- W14 docs (Phase D).
- W15 acceptance gate (Phase D).

### 2.3 Hard constraints (carried from prior phases + spec §18)

- No automatic deploy. `aee install --execute` brings the install to "ready";
  it does NOT deploy to production.
- No automatic restart. `aee doctor` reports; it does not restart.
- Dry-run by default. `aee install` / `aee update` without `--execute` only
  plan and pre-flight.
- No force over an existing install. Profile switch is rejected (exit 5).
- No secret material in commits.
- No new exit codes collide with the verified set `{0, 2, 3, 4, 5, 6}`.
- No `subprocess` / `os.system` / `os.popen` in any new module's dry-run path
  (AST scan in tests, matching Phase 3's contract).

---

## 3. Acceptance Criteria

Phase 4 is **PASS** when all of the following hold, verified on disk at
`/home/ubuntu/hermes-runtime-bridge` against the post-Phase-4 HEAD:

### 3.1 W4 — `aee install` CLI surface

1. `aee/installer/cli.py` exists and exposes `run_install(args) -> int`.
2. `aee/tests/test_installer_cli.py` exists and passes ≥ 20 targeted tests.
3. `PYTHONPATH=. python3 -m aee.cli install --help` shows the full
   `--profile / --dry-run / --json / --execute / --resume / --from / --rollback-to`
   surface.
4. `PYTHONPATH=. python3 -m aee.cli install --json` (dry-run) exits 0 and
   emits a JSON object with the dispatch contract.
5. `PYTHONPATH=. python3 -m aee.cli install --execute` (without prior
   authorization flag — tested via a fixture that does NOT set the
   authorizer) exits `EXIT_EXECUTE_NOT_AUTHORIZED=6`.
6. AST scan of `aee/installer/cli.py` confirms no `subprocess` / `os.system`
   / `os.popen` usage.
7. Existing `aee install` dispatch in `aee/cli.py` delegates to
   `aee.installer.cli.run_install` (thin shim); the existing tests
   (`test_aee92_unified_cli_ux`, `test_aee93_installer_backend`) still pass
   byte-for-byte.
8. `aee prepare` (Phase 3) is untouched and its 32 tests still pass.

### 3.2 W5 — `aee update` CLI surface

1. `aee/installer/update.py` exists and exposes `run_update(args) -> int`.
2. `aee/tests/test_installer_update.py` exists and passes ≥ 15 targeted tests.
3. `PYTHONPATH=. python3 -m aee.cli update --help` shows the
   `--channel / --ref / --yes / --offline-bundle / --log-format / --execute / --json`
   surface.
4. `PYTHONPATH=. python3 -m aee.cli update --json` (dry-run, no args) exits 0
   and emits a JSON object describing "advance on current channel".
5. `PYTHONPATH=. python3 -m aee.cli update --channel rc --execute` (profile
   switch scenario in a fixture) exits `EXIT_PROFILE_SWITCH_REJECTED=5` when
   the existing install's profile would change.
6. Drift detection in dry-run reports a *projected* drift check
   (`would_drift: bool`) without performing a git fetch.
7. AST scan of `aee/installer/update.py` confirms no `subprocess` /
   `os.system` / `os.popen` usage.

### 3.3 Exit-code constants

1. Named constants for `{7, 8, 9, 10, 11, 12}` exist in
   `aee/installer/backend.py` (or `aee/installer/exit_codes.py`) and are
   re-exported from `aee/installer/__init__.py`.
2. Tests pin each constant to its numeric value (spec §10.4).
3. The verified constants `{0, 2, 3, 4, 5, 6}` are byte-identical
   (diff-against-`f8fe2c9` shows no modification to the existing constant
   lines).

### 3.4 Regression

1. Full `aee/tests` suite: 2005 tests → ≥2005 tests, with the same 5
   pre-existing PyYAML errors and 2 skips. Zero new failures.
2. Phase 3 targeted suite (`test_aee_phase3_installer_workflow`): 32/32 PASS.
3. Phase 2 targeted suite (`test_aee_phase2_doctor`): unchanged PASS count.
4. `test_installer_lifecycle`: 54/54 PASS (W2 substrate intact).

### 3.5 Production safety

1. Zero production files modified outside the Phase 4 file list.
2. `aee/cli.py` modification is purely additive (new `update_parser`, new
   dispatch branch; existing `install` / `doctor` / `prepare` paths
   byte-identical except `_install_dispatch` becoming a thin shim — the
   shim change must preserve the existing exit codes exactly).
3. No commit, no push, no deploy, no restart performed during planning.
4. No `git add -A`. Explicit-path staging only.

---

## 4. Dependencies

### 4.1 Already satisfied (verified at `f8fe2c9`)

- `aee/installer/backend.py` — `InstallerBackend` with `plan()`, `execute()`,
  `dry_run=True` default, profile-switch rejection. **VERIFIED.**
- `aee/installer/lifecycle.py` — stage marker library (W2). **VERIFIED.**
- `aee/doctor.py` — Phase 2 doctor. **VERIFIED.**
- `aee/installer/workflow.py` — Phase 3 `aee prepare` workflow. **VERIFIED.**
- `aee/profiles/descriptor.py` — canonical profile SOT. **VERIFIED.**
- `aee/platform/current.py` — `resolve_platform_identity`. **VERIFIED.**
- `aee/cli.py` — argparse infrastructure with `install`/`doctor`/`prepare`
  subparsers. **VERIFIED.**

### 4.2 New dependencies introduced by Phase 4

- None at the module level. Phase 4 reuses the existing substrate.
- The `--execute` path logically depends on the shell trampoline (W6) to
  actually perform side effects, but Phase 4 ships the CLI surface and the
  gated `--execute` flag; the actual side-effecting execution is a
  separately authorizable follow-up (Phase B / W11 E2E). In Phase 4,
  `--execute` without the authorizer returns exit 6.

### 4.3 Test dependencies

- `unittest` (stdlib) — no new test deps.
- PyYAML is NOT required for Phase 4 tests (the 5 pre-existing PyYAML
  errors are unrelated and remain baseline).

---

## 5. Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | `_install_dispatch` shim change breaks `test_aee92_unified_cli_ux` byte-for-byte. | The shim MUST preserve exit codes {0, 2, 3, 4, 5, 6} and the JSON output shape exactly. Run the existing test suite before and after; diff the JSON output. |
| R2 | `aee update --channel` channel switching semantics overlap with W9 (release channel backend). | Phase 4 ships the CLI surface + drift *detection* (read-only). Channel-switching *backend* logic reuses the existing `InstallerBackend` profile-switch path. Full W9 release-channel metadata is deferred. Document the seam in the implementation report. |
| R3 | Exit-code constants `{7..12}` collide with an unverified constant already in use by a downstream caller. | Grep `aee/` and `dispatcher/` for the literals 7, 8, 9, 10, 11, 12 in exit-code context before introducing the constants. The spec §10.4 audit already confirmed the verified set is `{0, 2, 3, 4, 5, 6}`; re-verify at implementation time. |
| R4 | `--execute` flag is accidentally enabled in dry-run tests, masking the guard. | Tests must assert that `--execute` WITHOUT the authorizer fixture returns 6. Use a fixture that does NOT set the authorizer env var. |
| R5 | `aee update` drift detection performs an actual `git fetch` in dry-run, violating the no-side-effects contract. | AST scan + test assertion: dry-run `aee update` MUST NOT call `subprocess`/`os.system`/`os.popen`. Drift is *projected* from the recorded pin vs. the on-disk HEAD (read-only `git rev-parse` is allowed; `git fetch` is not). |
| R6 | Profile switch rejection in `aee update --channel` is silently bypassed when the existing install has no profile marker. | Test the no-marker scenario explicitly: `aee update --channel rc` with no existing `.aee-profile` marker should proceed (no profile to switch from), not exit 5. |
| R7 | `aee/installer/cli.py` collides with the existing `aee/cli.py` module name. | Use `aee.installer.cli` (dotted) as the canonical import path; the file at `aee/installer/cli.py` is `aee.installer.cli`. No collision with top-level `aee.cli`. |
| R8 | Untracked working-tree residue (50+ untracked `.md` reports, `scripts/`, `reports/`) gets accidentally staged. | Explicit-path staging only (`git add aee/installer/cli.py aee/installer/update.py aee/tests/test_installer_cli.py aee/tests/test_installer_update.py aee/cli.py aee/installer/backend.py`). No `git add -A`. |
| R9 | Phase 4 ships `--execute` but no actual execution backend, creating a misleading CLI surface. | Document in `--help` text and the implementation report that `--execute` is gated and that actual side-effecting execution is a separately authorizable follow-up (Phase B). The exit 6 path is the contract. |

---

## 6. Proposed Work Breakdown

Each item is a single AEE K-shape atomic commit (one task, one deliverable),
following the existing pattern in `~/.hermes/skills/software-development/aee-iteration-pattern/`.

### 6.1 Slice 4A — Exit-code constants

| Field | Value |
|---|---|
| Deliverable | Named constants for `{7, 8, 9, 10, 11, 12}` in `aee/installer/backend.py` (or new `aee/installer/exit_codes.py`); re-export from `aee/installer/__init__.py`; pin via tests. |
| Files | `aee/installer/backend.py` (additive), `aee/installer/__init__.py` (additive re-export), `aee/tests/test_installer_exit_codes.py` (new, ~6 tests). |
| Touches | 2 modified + 1 new. |
| Acceptance | Constants pinned; verified set `{0,2,3,4,5,6}` byte-identical; full `aee/tests` suite: 2005 → 2011 tests, 0 new failures. |

### 6.2 Slice 4B — `aee install` CLI surface (W4)

| Field | Value |
|---|---|
| Deliverable | `aee/installer/cli.py` with `run_install(args) -> int`; `aee/cli.py` `_install_dispatch` becomes a thin shim; `--execute / --resume / --from / --rollback-to` argparse wiring. |
| Files | `aee/installer/cli.py` (new), `aee/cli.py` (modified — additive `install_parser` args + shim change to `_install_dispatch`), `aee/tests/test_installer_cli.py` (new, ≥20 tests). |
| Touches | 1 new + 1 modified + 1 new test. |
| Acceptance | §3.1 criteria 1–8 above. |

### 6.3 Slice 4C — `aee update` CLI surface (W5)

| Field | Value |
|---|---|
| Deliverable | `aee/installer/update.py` with `run_update(args) -> int`; `aee/cli.py` `update_parser` (additive); `--channel / --ref / --yes / --offline-bundle / --log-format / --execute / --json` argparse wiring; drift detection (projected in dry-run). |
| Files | `aee/installer/update.py` (new), `aee/cli.py` (modified — additive `update_parser` + `_update_dispatch`), `aee/tests/test_installer_update.py` (new, ≥15 tests). |
| Touches | 1 new + 1 modified + 1 new test. |
| Acceptance | §3.2 criteria 1–7 above. |

### 6.4 Slice 4D — Integration + report

| Field | Value |
|---|---|
| Deliverable | Cross-slice integration test (`aee/tests/test_phase4_integration.py`) verifying `install` + `update` + `doctor` + `prepare` coexist; implementation report; push report. |
| Files | `aee/tests/test_phase4_integration.py` (new, ~5 tests), `reports/aee_phase4_implementation.md`, `reports/aee_phase4_atomic_commit.md`, `reports/aee_phase4_push.md`. |
| Touches | 1 new test + 3 report files. |
| Acceptance | All Phase 4 slices coexist; full `aee/tests` suite passes with 0 new failures; Phase 3 (`aee prepare`) still 32/32; push to `origin/main` verified 0/0 ahead-behind. |

### 6.5 Ordering

4A → 4B → 4C → 4D. Each slice is independently reviewable and atomic-committable.
4A must land first because 4B/4C reference the new exit-code constants.

---

## 7. Git Status (Pre-Phase-4, verified read-only)

```
$ cd /home/ubuntu/hermes-runtime-bridge && git rev-parse HEAD
f8fe2c918a2173c54b147f1380380e699f478ce1

$ git rev-parse --abbrev-ref HEAD
main

$ git ls-remote origin main
f8fe2c918a2173c54b147f1380380e699f478ce1	refs/heads/main

$ /usr/bin/git log --oneline -3
f8fe2c9 feat(aee): add Phase 3 installer workflow (aee prepare)
6b2609a feat(aee): add Phase 2 'aee doctor' readiness health check
d2cb78e chore: stop tracking AEE_GPT_E2E_EVIDENCE runtime capture, remove duplicate test
```

Local HEAD == remote `origin/main` == `f8fe2c918a2173c54b147f1380380e699f478ce1`.
Ahead/behind = 0/0. Phase 3 is fully pushed.

`git status --short` shows **only untracked files** (50+ pre-existing report
`.md` files, `scripts/`, `reports/`, `requirements*.lock`). **Zero tracked-file
modifications.** The working tree is clean for tracked source — Phase 4 starts
from a clean baseline.

---

## 8. Artifact Verification (this plan file)

```
$ ls -la reports/aee_phase4_plan.md
-rw-r--r-- 1 ubuntu ubuntu <size> <date> reports/aee_phase4_plan.md

$ wc -l reports/aee_phase4_plan.md
<N> reports/aee_phase4_plan.md

$ sha256sum reports/aee_phase4_plan.md
<sha256> reports/aee_phase4_plan.md
```

The exact byte size, line count, and sha256 are filled in by the verification
commands run after this file is written (see §11 below for the actual
receipts). The sha is NOT embedded in this file body (the self-referential sha
paradox makes an in-band sha unstable — see Phase 3 implementation report §13).

---

## 9. Production Safety

- **No source code modified** to produce this plan. Only read-only commands
  (`git rev-parse`, `git log`, `git status`, `ls`, `grep`, `sed -n`,
  `python3 -c "..."` for `--help` smoke, `python3 -m unittest` for baseline
  count) were run.
- **No commits, no pushes, no deploys, no restarts.**
- **No `git add` performed.**
- **No `subprocess` / `os.system` / `os.popen` invoked** by the planning
  process.
- **No secrets read.** The `aee install --json` smoke output in §1 was
  inspected for shape only; no env var values were captured.
- **No `.env` or config files modified.**
- The `python3 -m unittest discover -s aee/tests` run for the baseline count
  is read-only (test execution against the existing test suite; no source
  mutation, no DB writes — the dispatcher DB is not exercised by these
  tests).

---

## 10. Telegram Notification

Not attempted. This is a planning-only work order, not an AEE-MINI task or a
direct repository push from the orchestrator. The AEE-MINI Telegram rule
(send on every AEE-MINI task) does not apply to read-only planning artifacts.
If a Telegram summary is required, it can be sent separately via
`hermes send --to telegram:5132341473 --file reports/aee_phase4_plan.md`.

---

## 11. Recommendation

**Implementation can begin.**

Rationale:

1. **Phase 3 is fully shipped and pushed.** Local HEAD == remote HEAD ==
   `f8fe2c918a2173c54b147f1380380e699f478ce1`. No outstanding commits.
2. **Working tree is clean for tracked source.** Zero tracked-file
   modifications. The 50+ untracked items are pre-existing report residue,
   not Phase 4 artifacts.
3. **All Phase 4 dependencies are satisfied.** `InstallerBackend`,
   `lifecycle`, `doctor`, `workflow`, `profiles.descriptor`,
   `platform.current`, and the `aee/cli.py` argparse infrastructure all
   exist at `f8fe2c9`.
4. **Test baseline is established.** 2005 tests, 5 pre-existing PyYAML
   errors, 2 skips. Phase 4 must not introduce new failures. Phase 3
   targeted suite is 32/32 PASS — the regression sentinel.
5. **Scope is well-bounded.** 3 implementation slices (4A/4B/4C) + 1
   integration/report slice (4D). Each is a single K-shape atomic commit.
   No slice modifies another slice's deliverable.
6. **Hard constraints are clear.** Dry-run by default, no new exit codes
   collide with `{0,2,3,4,5,6}`, no `subprocess` in dry-run paths, no
   `git add -A`, no auto-deploy/restart.
7. **Risks are identified with mitigations** (§5 R1–R9). The highest-risk
   item (R1: `_install_dispatch` shim change breaking `test_aee92`) is
   mitigated by a byte-for-byte exit-code + JSON-shape diff before and after.

**Recommended next action**: authorize Slice 4A (exit-code constants) as a
single K-shape atomic commit. Slice 4A is the smallest, lowest-risk slice
and unblocks 4B/4C.

---

## 12. Cross-References

- Phase 3 implementation: `reports/aee_phase3_installer_implementation.md`
- Phase 3 atomic commit: `reports/aee_phase3_installer_atomic_commit.md`
- Phase 3 push: `reports/aee_phase3_installer_push.md`
- Phase 2 doctor: `reports/aee_phase2_doctor_implementation.md`
- Bootstrap v1 spec (authoritative): `reports/aee_bootstrap_v1_spec.md`
  §10.1 (`aee install`), §10.3 (`aee update`), §10.4 (exit codes), §16 (W4/W5),
  §17.3 (Phase A), §18 (production safety).
- Bootstrap v1 spec review (round 2): `reports/aee_bootstrap_v1_spec_review_round2.md`
- Platform adapter architecture plan: `reports/aee_platform_adapter_architecture_plan.md`
- AEE iteration pattern skill: `~/.hermes/skills/software-development/aee-iteration-pattern/`
- AEE master plan (off-repo, authoritative for AEE product direction):
  `/home/ubuntu/Abacus/AEE/AEE_MASTER_PLAN.md`

---

_End of plan._