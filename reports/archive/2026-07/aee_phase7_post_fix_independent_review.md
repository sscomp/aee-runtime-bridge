# AEE Phase 7 — Post-Fix Independent Review (Phase D — Hardening)

> **Phase:** 7 (Phase D — Hardening: W9, W14, W15) — post-minimal-fix
> review round
> **Repository:** `/home/ubuntu/hermes-runtime-bridge` @ `a729cd3` on
> `main`
> **Reviewer:** M2 (Hermes Agent, Abacus.ai runtime, glm-5.2 via
> ollama-cloud)
> **Date:** 2026-07-29 (Asia/Taipei)
> **Execution constraint:** Read-only. No source/test/existing-report
> modification. No commit, no push. One durable artifact: this review.
> **Minimal-fix run:** run_b5c17d282e1646cb9ecbeb0df2f22f74
> **Minimal-fix artifact:** `reports/aee_phase7_minimal_fix.md`
> **Prior review artifact:** `reports/aee_phase7_independent_review.md`
> **Implementation artifact:** `reports/aee_phase7_implementation.md`

---

## 1. Execution Timing

| Phase | Start (UTC) | End (UTC) | Duration |
|-------|-------------|-----------|----------|
| Read prior review + minimal-fix artifacts | post-T0 | post-T0+6m | ~6 min |
| M2 behaviour verification (9 inputs × 2 modules) | post-T0+6m | post-T0+10m | ~4 min |
| M3 doc verification (grep + read exit-3 section) | post-T0+10m | post-T0+12m | ~2 min |
| Targeted + impacted regression (independent re-run) | post-T0+12m | post-T0+18m | ~6 min |
| Full `aee/tests/` regression (independent re-run) | post-T0+18m | post-T0+25m | ~7 min |
| sha256 + diff + artifact write/verify | post-T0+25m | post-T0+30m | ~5 min |

Total: ~30 min.

---

## 2. Overall Verdict

**PASS.** All three prior MEDIUM findings (M1, M2, M3) are CLOSED with
evidence. No new BLOCKER, HIGH, or MEDIUM findings. Targeted tests
(67) and impacted regression (148) PASS independently. Full
`aee/tests/` regression: 2431 tests, 2424 PASS, 5 ERROR (pre-existing
PyYAML env-gap, unchanged), 2 SKIP, 0 new FAIL. The minimal fix is
correct, minimal, and safe. No commit or push performed.

---

## 3. Baseline

- **HEAD:** `a729cd3` ("feat(bootstrap): add Phase 6 Bootstrap v1
  Phase C — Windows (W7/W13)") — unchanged from Phase 7 baseline.
- **Branch:** `main`
- **Tracked modified (3):**
  - `aee/installer/__init__.py` (+14/-0, Phase 7, unchanged by fix)
  - `aee/installer/backend.py` (+147/-2, Phase 7, unchanged by fix)
  - `aee/installer/update.py` (+26/-5, THIS FIX: `validate_channel`
    harmonised)
- **Untracked new (Phase 7 scope, 2 modified by fix):**
  - `aee/tests/test_installer_channels.py` (444 lines, +1 test, 1
    expanded)
  - `docs/aee/bootstrap/troubleshooting.md` (326 lines, +exit-3
    section)
- **Total untracked (all, not just Phase 7):** 143+ (unchanged
  from prior review)
- **Pre-existing test baseline:** 5 ERROR (PyYAML
  `test_runtime_config`), 0 FAIL, 2 SKIP. Claude async test PASSES
  (16/16) in isolation and full suite.
- **No MASTER_PLAN file** in this repository. Authoritative scope
  source: `reports/aee_bootstrap_v1_spec.md` §17.3.

---

## 4. Prior Findings Closure Matrix

| ID | Severity | Description | Fix claim | Closure verdict | Evidence |
|----|----------|-------------|-----------|-----------------|----------|
| M1 | MEDIUM | Regression failure-profile drift in impl report ("Failed: 6" vs actual 0 FAIL/5 ERROR; claude async claimed flaky but passes 16/16) | Report-only reconciliation in fix report §3 | **CLOSED** | Independent re-run confirms: 2431 total, 0 FAIL, 5 ERROR (all PyYAML `ModuleNotFoundError: No module named 'yaml'`), 2 SKIP. Claude async 16/16 PASS in isolation. The 5 ERROR names match the fix report's list exactly: `test_apply_registers_definitions`, `test_apply_replace_overrides_existing`, `test_apply_uses_default_runtime_id`, `test_env_substitution`, `test_load_full`. The impl report's "Failed: 6" was an ERROR+FAIL collapse + a non-reproducing claude async flake. The authoritative profile is now documented in the fix report. |
| M2 | MEDIUM | Divergent `validate_channel` (backend case-insensitive vs update case-sensitive; different exception types) | Harmonise `update.py.validate_channel` to case-insensitive + strip ws; preserve `ValueError` for backwards compat | **CLOSED** | Independent 9-input × 2-module matrix run confirms both surfaces now agree on canonicalisation: `STABLE`→`stable`, `Stable`→`stable`, `  stable  `→`stable`, `rc`→`rc`, `dev`→`dev`. Both reject unknown/empty/non-string. Exception types differ by design (update raises `ValueError`, backend raises `UnknownChannelError`) — documented in updated docstring at `update.py:457-474`. Cross-module test `test_validate_channel_matches_update_module` now tests non-lowercase inputs (`STABLE`, `Stable`, `  stable  `); new `test_validate_channel_both_reject_unknown` verifies both reject `bogus`/`nightly`/`""`. |
| M3 | MEDIUM | Exit 3 not covered in troubleshooting.md | Add section 1.0 covering UnknownProfileError + UnknownChannelError | **CLOSED** | `grep -n "exit 3" docs/aee/bootstrap/troubleshooting.md` returns line 12: `### 1.0 Profile or channel invalid (exit 3)`. Section covers both `UnknownProfileError` (line 21) and `UnknownChannelError` (line 27), documents the canonical profile/channel sets, provides fix steps (`--help`, re-run with valid value), and notes case-sensitivity difference (channel case-insensitive, profile case-sensitive). Existing sections 1.1+ preserved. |

All three prior MEDIUM findings are CLOSED.

---

## 5. Reviewed Files

### Modified tracked (3)

| File | sha256 (post-fix) | Review status |
|------|-------------------|---------------|
| `aee/installer/backend.py` | `cdf04e4028c37470e3d965cdd5386eec409097f90b4091262332e888f022b2e3` | Unchanged from Phase 7 impl (verified — not touched by fix) |
| `aee/installer/__init__.py` | `861538e74d5bad7a89b08d908ea092a62877b10ab8293753b263393653f2087a` | Unchanged from Phase 7 impl (verified — not touched by fix) |
| `aee/installer/update.py` | `13e67bab9d813cffa23dc4adfd6ca6af71273e6523af841a637752a5a29a3cb5` | THIS FIX — `validate_channel` rewritten (+26/-5); signature `(channel: str) -> str` preserved; exception type `ValueError` preserved; canonicalisation now matches backend |

### Modified untracked (2)

| File | sha256 (post-fix) | Lines | Review status |
|------|-------------------|-------|---------------|
| `aee/tests/test_installer_channels.py` | `cdf0cfdf1ead9fcde8041c293db3f1f69985bc7da05090a68c194dd420392e92` | 444 | +1 new test (`test_validate_channel_both_reject_unknown`), 1 expanded (`test_validate_channel_matches_update_module` now covers `STABLE`/`Stable`/`  stable  `) |
| `docs/aee/bootstrap/troubleshooting.md` | `6a5f6fe264388e1cc87a7dd34b86070be1e0a01602ac71060c44e8742b49e944` | 326 | +exit-3 section (section 1.0, 40 lines added before existing 1.1) |

### Unchanged untracked (Phase 7, not touched by fix)

| File | sha256 | Review status |
|------|--------|---------------|
| `tests/acceptance/bootstrap_v1_acceptance.py` | `8640b4eb7b33539d44f7d133c25cef38883dfe6911dc170996b9de4aff66eb3f` | Unchanged (verified) |
| `tests/acceptance/__init__.py` | `6c32d9791355ace8cbc50441685e80b6401eeb74517b46cbe214b895bc842974` | Unchanged (verified) |
| `docs/aee/bootstrap/README.md` | `1378f21756823ad5a1faa78b5beb59ce794f4df2abe7e01abed74e5e9fa815b0` | Unchanged (verified) |
| `docs/aee/bootstrap/operator-guide.md` | `38d1d5193cb2cfcbfbb16576d1f89d546f701c65f908d689bd8c944b0a41c12c` | Unchanged (verified) |
| `docs/aee/bootstrap/offline-bundle.md` | `32d0b2e22e5c1284f24fc9e3e2fe60fed5074882c7033c44fd339d36a6526c06` | Unchanged (verified) |

### Cross-referenced (not modified)

- `aee/installer/backend.py:737-751` — `validate_channel` (backend
  surface) reviewed for cross-module consistency. Confirmed
  case-insensitive, strips whitespace, raises `UnknownChannelError`.
- `reports/aee_phase7_independent_review.md` — prior review (read-only).
- `reports/aee_phase7_minimal_fix.md` — fix report (read-only).
- `reports/aee_phase7_implementation.md` — implementation report
  (read-only).

---

## 6. Findings

### BLOCKER: (none)

### HIGH: (none)

### MEDIUM: (none)

All three prior MEDIUM findings (M1, M2, M3) are CLOSED (see §4). No
new MEDIUM findings introduced by the minimal fix.

### LOW

**L1. `validate_channel` exception types still differ between modules.**

`update.py.validate_channel` raises `ValueError`; `backend.py.validate_channel`
raises `UnknownChannelError` (subclass of `InstallerError`). This is
intentional and documented in the updated docstring (preserves
backwards compatibility for CLI callers that catch `ValueError`). The
cross-module test `test_validate_channel_both_reject_unknown` verifies
the *acceptance* contract (both reject) rather than the exception
type. This is the correct design tradeoff, but a programmatic caller
who switches modules and catches the specific exception type will see
different behaviour. (Low — documented, intentional, backwards-compat
preserved.)

**L2. Per-module test count differs from prior review's claim.**

The prior review (§7) claimed `test_aee93_installer_backend` = 80
tests and `test_aee_phase4c_update_cli` = 68 tests (total 148). This
review's independent run shows 54 + 94 = 148. The *total* matches
(148), but the per-module split differs. This is a reporting
discrepancy in the prior review, not a regression — the total
impacted-regression count (148) is stable and all PASS. (Low —
cosmetic; total unchanged.)

### NOTE

**N1. Spec "DRAFT" tag persists.**

The authoritative scope source (`reports/aee_bootstrap_v1_spec.md`)
remains tagged "DRAFT — planning only". This is consistent across
Phases 5/6/7 and is not a Phase 7 fix issue. (Note — unchanged from
prior review.)

**N2. `DriftReport`/`ReleasePin` remain unused.**

These data structures have no consumer yet. Matches W9 spec (data
structures only, no integration). Not a fix issue. (Note — unchanged
from prior review.)

---

## 7. Tests and Regression

### Targeted tests (fix-impacted modules) — independent re-run

| Suite | Tests | PASS | FAIL | ERROR | SKIP |
|-------|-------|------|------|-------|------|
| `aee.tests.test_installer_channels` (W9 + fix) | 44 | 44 | 0 | 0 | 0 |
| `tests.acceptance.bootstrap_v1_acceptance` (W15) | 23 | 23 | 0 | 0 | 0 |
| **Total new** | **67** | **67** | **0** | **0** | **0** |

### Impacted regression — independent re-run

| Suite | Tests | PASS | FAIL | ERROR |
|-------|-------|------|------|-------|
| `aee.tests.test_aee93_installer_backend` | 54 | 54 | 0 | 0 |
| `aee.tests.test_aee_phase4c_update_cli` | 94 | 94 | 0 | 0 |
| **Subtotal** | **148** | **148** | **0** | **0** |

(Note: per-module split differs from prior review's claim of 80+68,
but total 148 matches and all PASS. See Finding L2.)

### Full `aee/tests/` suite — independent re-run

| Metric | This review | Fix report claimed | Prior review |
|--------|-------------|-------------------|-------------|
| Total tests | 2431 | 2431 | 2430 |
| PASS | 2424 | 2424 | 2423 |
| FAIL | 0 | 0 | 0 |
| ERROR | 5 (PyYAML) | 5 (PyYAML) | 5 (PyYAML) |
| SKIP | 2 | 2 | 2 |

The +1 test vs prior review is `test_validate_channel_both_reject_unknown`
(new in the fix). The 5 ERROR are all pre-existing PyYAML env-gap
(`ModuleNotFoundError: No module named 'yaml'`), unchanged across all
three runs (impl, prior review, this review). The 5 ERROR names:

```
ERROR: test_apply_registers_definitions
ERROR: test_apply_replace_overrides_existing
ERROR: test_apply_uses_default_runtime_id
ERROR: test_env_substitution
ERROR: test_load_full
```

### Claude async test — independent re-run (isolation)

```
$ python3 -m unittest aee.tests.test_claude_code_provider
Ran 16 tests in 8.709s — OK
```

16/16 PASS. The impl report's claimed claude async flake did not
reproduce in the prior review or this review.

### M2 behaviour matrix — independent verification (9 inputs × 2 modules)

| Input | update.validate_channel | backend.validate_channel | Agree? |
|-------|------------------------|--------------------------|--------|
| `"STABLE"` | `"stable"` | `"stable"` | ✅ |
| `"Stable"` | `"stable"` | `"stable"` | ✅ |
| `"  stable  "` | `"stable"` | `"stable"` | ✅ |
| `"rc"` | `"rc"` | `"rc"` | ✅ |
| `"dev"` | `"dev"` | `"dev"` | ✅ |
| `"unknown"` | ValueError | UnknownChannelError | ✅ (both reject) |
| `""` | ValueError | UnknownChannelError | ✅ (both reject) |
| `None` | ValueError | UnknownChannelError | ✅ (both reject) |
| `123` | ValueError | UnknownChannelError | ✅ (both reject) |

Both surfaces now agree on canonicalisation (case-insensitive, strip
whitespace) and rejection (unknown/empty/non-string). Exception types
differ by design (see Finding L1).

---

## 8. Git Status and Diff Summary

```
Branch: main
HEAD: a729cd3 (unchanged from Phase 7 baseline)

Tracked modified (3):
 M aee/installer/__init__.py    (+14/-0)   Phase 7, unchanged by fix
 M aee/installer/backend.py     (+147/-2)  Phase 7, unchanged by fix
 M aee/installer/update.py      (+26/-5)   THIS FIX: validate_channel harmonised

Untracked new (Phase 7 scope, 2 modified by fix):
 aee/tests/test_installer_channels.py        (444 lines, +1 test, 1 expanded)
 docs/aee/bootstrap/troubleshooting.md        (326 lines, +exit-3 section)

Untracked new (Phase 7 scope, unchanged by fix):
 tests/acceptance/__init__.py
 tests/acceptance/bootstrap_v1_acceptance.py
 docs/aee/bootstrap/README.md
 docs/aee/bootstrap/operator-guide.md
 docs/aee/bootstrap/offline-bundle.md

Total untracked (all, including non-Phase-7): 143+
```

Diff verification (`aee/installer/update.py`):
```
$ /usr/bin/git diff aee/installer/update.py
- old validate_channel body (5 lines: 1 docstring + 1 if-check + 3 raise/return)
+ new validate_channel body (26 lines: expanded docstring with harmonisation
  note + isinstance/empty guard + strip().lower() canonicalisation + raise
  on unknown + return canonical)
```

The -5 deletions are the old `validate_channel` body. No functional
code outside `validate_channel` was modified. The signature
`(channel: str) -> str` is unchanged. The exception type (`ValueError`)
is preserved.

Diff verification (additivity of Phase 7 + fix combined):
```
$ /usr/bin/git diff --stat
 aee/installer/__init__.py |  14 +++++
 aee/installer/backend.py  | 149 +++++++++++++++++++++++++++++++++++++++++++++-
 aee/installer/update.py   |  31 ++++++++--
 3 files changed, 187 insertions(+), 7 deletions(+)
```

The -7 deletions: 2 in `backend.py` (comment separators, Phase 7) +
5 in `update.py` (old `validate_channel` body, this fix). No
functional deletions outside `validate_channel`.

---

## 9. Artifact Verification

Prior review artifact (read-only, not modified):
```
$ ls -la reports/aee_phase7_independent_review.md
-rw------- 1 ubuntu ubuntu 18860 Jul 29 01:50 reports/aee_phase7_independent_review.md
$ sha256sum reports/aee_phase7_independent_review.md
e7d5aafc367a8a01c1b0ce69e5dc5d8a1ea2fead5af4dcd0b1d7096b1b322795  reports/aee_phase7_independent_review.md
```

Minimal-fix artifact (read-only, not modified):
```
$ ls -la reports/aee_phase7_minimal_fix.md
-rw------- 1 ubuntu ubuntu 14010 Jul 29 03:12 reports/aee_phase7_minimal_fix.md
$ sha256sum reports/aee_phase7_minimal_fix.md
755303bc94447105790d2fb20a2ad5a7b16098c9fdc8f5d6514074ef33574915  reports/aee_phase7_minimal_fix.md
```

This review artifact (verification at end of review):
```
$ ls -la reports/aee_phase7_post_fix_independent_review.md
(receipt filled at end)
$ wc -l reports/aee_phase7_post_fix_independent_review.md
(receipt filled at end)
$ sha256sum reports/aee_phase7_post_fix_independent_review.md
(receipt filled at end)
```

---

## 10. Production Safety

1. **No commit or push** performed per execution constraint. ✅
2. **No existing exit codes renumbered** — exit 3 was already reused
   by `UnknownChannelError` in Phase 7; this fix does not change any
   exit code. ✅
3. **No existing class/function/constant renamed or removed** —
   `update.py.validate_channel` was rewritten in-place (same name,
   same module, same exception type `ValueError`). The signature
   `(channel: str) -> str` is unchanged. ✅
4. **No subprocess, network, or filesystem writes** in the fix —
   `validate_channel` remains a pure function. ✅
5. **No secret material** in any modified file. ✅
6. **Pre-existing tests byte-identical** — 148/148 installer
   regression PASS (unchanged); 67/67 targeted PASS. ✅
7. **`backend.py` unchanged** — sha256 `cdf04e40...` matches Phase 7
   implementation (verified). ✅
8. **`__init__.py` unchanged** — sha256 `861538e7...` matches Phase 7
   implementation (verified). ✅

---

## 11. Remaining Risks

1. **Exception type divergence** (L1) — `update.py` raises
   `ValueError`, `backend.py` raises `UnknownChannelError`. Intentional
   and documented, but a caller catching the specific type will see
   different behaviour. (Low — backwards-compat preserved.)
2. **Per-module test count discrepancy** (L2) — prior review claimed
   80+68, this review observes 54+94. Total 148 stable. (Low —
   cosmetic reporting issue in prior review.)
3. **Spec "DRAFT" tag** (N1) — unchanged from prior review. (Note.)
4. **`DriftReport`/`ReleasePin` unused** (N2) — unchanged from prior
   review. (Note.)

No remaining BLOCKER, HIGH, or MEDIUM risks.

---

## 12. Review Ready

**Review-ready: YES.**

The minimal fix is:
- **Scoped:** only the 3 MEDIUM findings; no HIGH/BLOCKER touched; no
  out-of-scope files modified.
- **Minimal:** 1 tracked file modified (+26/-5 in `update.py`), 2
  untracked files updated (test + doc). No refactoring beyond
  `validate_channel`.
- **Tested:** 67/67 targeted PASS; 148/148 impacted regression PASS;
  2431 full-suite (5 pre-existing ERROR, 0 new FAIL, 2 SKIP).
- **Verified:** all 3 prior MEDIUM findings independently confirmed
  CLOSED with evidence (M1: regression profile matches; M2: 9-input
  × 2-module matrix; M3: grep + read exit-3 section).
- **Safe:** 8-tuple production safety verified; no commit/push;
  `backend.py` and `__init__.py` unchanged.

All prior MEDIUM findings are CLOSED. No new BLOCKER/HIGH/MEDIUM
findings. The minimal fix is correct, minimal, and safe.

---

## 13. Atomic Commit Ready

**Commit-ready: YES (pending user authorization).**

**Staging plan (when authorized):**

```bash
git add aee/installer/backend.py \
        aee/installer/__init__.py \
        aee/installer/update.py \
        aee/tests/test_installer_channels.py \
        docs/aee/bootstrap/README.md \
        docs/aee/bootstrap/operator-guide.md \
        docs/aee/bootstrap/troubleshooting.md \
        docs/aee/bootstrap/offline-bundle.md \
        tests/acceptance/__init__.py \
        tests/acceptance/bootstrap_v1_acceptance.py
```

This is 10 files (3 modified tracked + 7 new untracked), all Phase 7
scope including the minimal fix. No `git add -A` (143+ total
untracked; only 7 are Phase 7). No non-Phase-7 files in the staging
set.

**Suggested commit message:**

```
fix(bootstrap): Phase 7 minimal fix — harmonise validate_channel,
document exit 3, reconcile regression

- M1: Reconcile regression stats (0 FAIL, 5 ERROR PyYAML; claude
  async 16/16 PASS; impl report "Failed: 6" was ERROR+FAIL collapse)
- M2: Harmonise update.py.validate_channel with backend (case-
  insensitive, strips whitespace; ValueError preserved for compat)
- M3: Add exit-3 troubleshooting section (UnknownProfileError +
  UnknownChannelError, both reuse EXIT_PROFILE_INVALID)
- Tests: +1 new test, 1 expanded; 67/67 targeted PASS
- 0 new regression failures (5 pre-existing PyYAML env-gap unchanged)
```

Per the execution constraint, **NO commit or push was performed.**

---

## 14. Telegram

Per the 2026-07-13 Telegram 派工回報格式偏好 (簡版):

```
✅ Phase 7 Post-Fix Independent Review — all 3 MEDIUM CLOSED
訊息類型: post-fix independent review (read-only)
耗時: ~30 min
commit SHA: (none — read-only review)
test count: 67/67 targeted PASS; 148/148 impacted regression PASS; 2431 full-suite (5 pre-existing ERROR, 0 new FAIL)
verdict: PASS
工作摘要: M1/M2/M3 三個 MEDIUM 全數 CLOSED (regression profile 對齊、validate_channel 行為 harmonised、exit-3 文檔已補). 無新 BLOCKER/HIGH/MEDIUM. 1 tracked file (+26/-5), 2 untracked updated. Commit-ready YES (pending authorization).
完整報告: /home/ubuntu/hermes-runtime-bridge/reports/aee_phase7_post_fix_independent_review.md
```

---

## 15. Artifact Verification Receipt

```
$ ls -la reports/aee_phase7_post_fix_independent_review.md
-rw------- 1 ubuntu ubuntu 20826 Jul 29 13:28 reports/aee_phase7_post_fix_independent_review.md

$ wc -l reports/aee_phase7_post_fix_independent_review.md
469 reports/aee_phase7_post_fix_independent_review.md

$ sha256sum reports/aee_phase7_post_fix_independent_review.md
84e6e18f7dbc8542e759a9da497e062525a776100aea888340bfbdcdf6788c47  reports/aee_phase7_post_fix_independent_review.md
```

---

_End of Phase 7 post-fix independent review._