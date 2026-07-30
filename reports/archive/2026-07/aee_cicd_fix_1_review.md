# WO-CICD-FIX-1 — Independent Read-Only Review

**Work Order:** WO-CICD-FIX-1
**Review Target:** `reports/aee_cicd_fix_1.md` (implementation artifact) + `.github/workflows/ci-matrix.yml` (modified file)
**Reviewer:** Hermes M2 (independent review pass, glm-5.2 via ollama-cloud)
**Date:** 2026-07-30
**Repository:** `/home/ubuntu/hermes-runtime-bridge` (branch: `main`)

---

## 1. Execution Timing

| Field | Value |
|-------|-------|
| Review start (UTC) | 2026-07-30T08:01Z |
| Review end (UTC) | 2026-07-30T08:06Z |
| Duration | ~5 minutes |
| Session type | Read-only independent review |

## 2. Overall Verdict

**PASS**

The change is minimal, correct, and authorized. The branch filter `master` → `main` fix is confirmed against remote branch state (`git ls-remote` shows only `refs/heads/main`). The `permissions: contents: read` addition is valid, least-privilege, and does not break any workflow requirement (only `actions/checkout@v4` is used). YAML validation, structural assertions, 66 targeted CI/CD matrix tests, 517 AEE 9.x regression tests, 142 profile/provider-neutral tests, 18 shell wrapper tests, and docker compose config validation all pass. No unrelated mutation exists. P0-1 shadow run is not disturbed. Artifact verified.

## 3. Baseline

| Field | Value | Verification |
|-------|-------|--------------|
| Branch | `main` | `git branch --show-current` → `main` ✓ |
| HEAD | `b8a6dd2685b143aaef6136240e7a556130f9b77d` | matches expected ✓ |
| origin/main | `b8a6dd2685b143aaef6136240e7a556130f9b77d` | matches HEAD ✓ |
| Remote branches | `refs/heads/main` only | `git ls-remote --heads origin` confirms no `master` ✓ |
| Stash list | No stashes | `git stash list` empty ✓ |
| Tracked modified files | `.github/workflows/ci-matrix.yml` only | `git diff --name-only` confirms single file ✓ |
| Untracked files | 166 (pre-existing reports + scripts) | not relevant to this WO ✓ |
| Pre-change SHA256 (ci-matrix.yml) | `7a521fc66dc88a5ca242e2a41935c9528d8c3f530de1ed904e4a5470529add27` | `git show HEAD:.github/workflows/ci-matrix.yml \| sha256sum` confirms ✓ |

## 4. Review Scope

| Item | Scope |
|------|-------|
| Implementation artifact | `reports/aee_cicd_fix_1.md` (338 lines, 14363 bytes) |
| Modified file | `.github/workflows/ci-matrix.yml` |
| Expected HEAD | `b8a6dd2685b143aaef6136240e7a556130f9b77d` — confirmed |
| Expected tracked change | only `.github/workflows/ci-matrix.yml` — confirmed |
| P0-1 shadow run | active (baseline 2026-07-29T17:38:32Z, expected end 2026-08-05T17:38:32Z) — not disturbed |

## 5. Root-Cause Verification

**Reported root cause:** Workflow branch filters referenced `master` while production branch is `main`.

**Independent verification:**

1. `git ls-remote --heads origin` → only `refs/heads/main` exists. No `master` branch on remote. ✓
2. `git show HEAD:.github/workflows/ci-matrix.yml | grep -n master` → 8 occurrences at lines 14, 19, 49, 50, 56, 58, 219, 242 (pre-change). ✓
3. Post-change `cat .github/workflows/ci-matrix.yml | grep -n master` → 0 occurrences. ✓
4. Pre-change SHA256 `7a521fc...` matches report §3 claim. ✓

**Root cause confirmed.** The workflow would never trigger on push or pull request to the actual production branch (`main`), making the §21.7 merge gate a dead letter.

## 6. Diff Review

**Diff stat:** `1 file changed, 13 insertions(+), 8 deletions(-)` — confirmed via `git diff --numstat`.

**Non-comment functional changes (4 categories):**

| # | Line | Change | Category |
|---|------|--------|----------|
| 1 | pull_request branches | `[master]` → `[main]` | branch filter correction |
| 2 | push branches | `[master]` → `[main]` | branch filter correction |
| 3 | new block after `on:` | `permissions: contents: read` + 2-line comment | least-privilege addition |
| 4 | merge-gate echo | `merge to master` → `merge to main` | error message correction |

**Comment-only changes (6 locations):** Lines 14, 19, 49-50, 219, 242 — all `master` → `main` in comments.

**No unrelated changes:** Filtering diff to non-comment, non-blank lines yields exactly the 4 functional changes above. No action-version bumps, no matrix changes, no step additions/removals, no formatting cleanup, no behavior changes.

**No newline at EOF:** Both pre-change and post-change versions lack a trailing newline (`od -c` confirms). This is a pre-existing condition, not introduced by this WO.

## 7. Workflow Semantics Review

| Check | Result | Evidence |
|-------|--------|----------|
| `pull_request.branches` | `['main']` ✓ | `yaml.safe_load` → `doc[True]['pull_request']['branches'] == ['main']` |
| `push.branches` | `['main']` ✓ | `yaml.safe_load` → `doc[True]['push']['branches'] == ['main']` |
| `on:` triggers | `['pull_request', 'push']` ✓ | PyYAML 1.1 maps `on` → `True` (known YAML 1.1 quirk) |
| Jobs | `['profile-job', 'merge-gate']` ✓ | both present |
| profile-job matrix | 4 profiles: full, mini, edge, developer ✓ | matrix.include confirmed |
| merge-gate needs | `['profile-job']` ✓ | dependency chain correct |
| merge-gate runs-on | `ubuntu-latest` ✓ | |
| No `master` in post-change file | 0 occurrences ✓ | `grep -n master` → empty |
| No `master` in `.github/` | 0 occurrences ✓ | `grep -rn master .github/` → empty |
| No tab characters | 0 ✓ | `grep -n $'\t'` → empty |

## 8. Permissions Review

| Check | Result | Evidence |
|-------|--------|----------|
| `permissions` is top-level | ✓ | `doc['permissions'] == {'contents': 'read'}` (not nested in a job) |
| Scope: `contents: read` | ✓ least-privilege | only `actions/checkout@v4` is used (line 113), which requires `contents: read` |
| No write scope needed | ✓ | no `actions/github-script`, no status checks, no labels, no releases, no deployments |
| Does not break workflow | ✓ | checkout + test execution only needs read access to repo content |
| Explanatory comment present | ✓ | 2-line comment above `permissions:` block explains rationale |

**Note:** Without an explicit `permissions:` block, GitHub Actions defaults to the repo's default `GITHUB_TOKEN` permissions (which may be `contents: write` depending on repo settings). Adding `contents: read` is a security improvement and does not break any functionality used by this workflow.

## 9. Independent Validation

### YAML Syntax Validation
```
.venv/bin/python -c "import yaml; yaml.safe_load(open('.github/workflows/ci-matrix.yml'))"
→ YAML parse: PASS
→ on: ['pull_request', 'push']
→ pull_request branches: ['main']
→ push branches: ['main']
→ permissions: {'contents': 'read'}
→ jobs: ['profile-job', 'merge-gate']
→ All structural assertions: PASS
```

**PyYAML 1.1 note:** `on:` is parsed as boolean `True` by PyYAML (YAML 1.1 spec). This is a known quirk, not a defect. The structural check accesses `doc[True]` to retrieve the trigger block.

### Docker Compose Config Validation
```
docker compose config --services
→ bridge-developer
→ bridge-edge
→ bridge-full
→ bridge-mini
→ EXIT: 0
```

## 10. Targeted Tests

### CI/CD Matrix Tests (`aee.tests.test_aee97_cicd_matrix`)
```
PYTHONPATH=. .venv/bin/python -m unittest aee.tests.test_aee97_cicd_matrix -v
→ Ran 66 tests in 0.038s
→ OK
```
**66/66 PASS** — includes `TestWorkflowYamlAgreement` (workflow structure, dry-run install, no registry push, no secret mutation, no cloud providers, merge gate presence, no services block, Python 3.11-slim container, matrix profile/kind agreement) and `TestValidateMatrixErrorPaths` (error detection for invalid matrix configs).

## 11. Impacted Regression

### Full AEE 9.x Suite
```
PYTHONPATH=. .venv/bin/python -m unittest discover -s aee/tests -p 'test_aee9*.py'
→ Ran 517 tests in 0.245s
→ OK
```
**517/517 PASS**

### Provider-Neutral + Profile Matrix
```
PYTHONPATH=. .venv/bin/python -m unittest aee.tests.test_aee91_canonical_profile_matrix aee.tests.test_aee96_provider_neutral_deployment
→ Ran 142 tests in 0.052s
→ OK
```
**142/142 PASS**

### Shell Wrapper Regression
```
bash tests/test_install_shell_wrapper.sh
→ 1..18
→ pass: 18, fail: 0
→ All tests passed.
```
**18/18 PASS**

### Total
**743/743 PASS** (66 + 517 + 142 + 18), 0 failures, 0 errors, 0 skips.

## 12. Shadow-Run Non-Interference

### P0-1 Shadow Run Status
- Baseline: `logs/shadow_run/baseline.json` (created 2026-07-29T17:38:32Z)
- Duration: 7 days (expected end 2026-08-05T17:38:32Z)
- Status: **ACTIVE** — not disturbed

### Protected Files Verification

| File | Baseline SHA256 | Current SHA256 | Status |
|------|-----------------|----------------|--------|
| `app.py` | `517c8392...` | `517c8392...` | ✓ unchanged |
| `dispatcher/db.py` | `46561e6b...` | `46561e6b...` | ✓ unchanged |
| `dispatcher/manager.py` | `c047c7bc...` | `c047c7bc...` | ✓ unchanged |
| `dispatcher/models.py` | `2413d9f8...` | `2413d9f8...` | ✓ unchanged |
| `dispatcher/notification_state.py` | `d4156829...` | `d4156829...` | ✓ unchanged |
| `dispatcher/notifier.py` | `5a985c2a...` | `cd9ffaac...` | ⚠ pre-existing drift |

**`dispatcher/notifier.py` drift analysis:**
- `git diff -- dispatcher/notifier.py` → empty (no working-tree changes)
- Last commit touching this file: `ac23def` ("fix(aee): suppress ghost task notifications")
- `ac23def` is an ancestor of current HEAD `b8a6dd2` (verified via `git log --oneline`)
- Baseline `bridge_repo.head` is `befe3d6...` (3 commits before current HEAD)
- **Conclusion:** The drift was caused by commit `ac23def` which was committed AFTER the P0-1 baseline was captured. This WO did not touch `dispatcher/notifier.py`. The drift is pre-existing and documented.

**`.github/workflows/ci-matrix.yml` is NOT in the P0-1 protected files list** — the 6 protected files are all dispatcher source files. The workflow fix does not disturb the shadow run.

### Runtime Non-Interference
- No bridge restart, no service changes, no cron changes
- No `docker compose up/down`
- No dependency installation
- Supervisord services all RUNNING (hermes-runtime-bridge pid 1619150, uptime 5 days)
- No `git add`, `git commit`, `git push`, `git stash`, or deploy operations performed

## 13. Git Status and Diff Summary

### Git Status (tracked changes)
```
 M .github/workflows/ci-matrix.yml
```
Only one tracked file modified. No other tracked files changed.

### Diff Summary
```
.github/workflows/ci-matrix.yml | 21 +++++++++++++--------
1 file changed, 13 insertions(+), 8 deletions(-)
```

### Numstat
```
13  8  .github/workflows/ci-matrix.yml
```

### Untracked Files
166 untracked files (pre-existing reports, scripts, manifest files) — none touched by this review.

## 14. Findings

| # | Finding | Severity | Disposition |
|---|---------|----------|-------------|
| 1 | Branch filter `master` → `main` correction | Confirmed correct | Matches remote branch state |
| 2 | `permissions: contents: read` addition | Confirmed valid | Least-privilege, does not break workflow |
| 3 | 6 comment corrections (`master` → `main`) | Confirmed accurate | All directly related to the fix |
| 4 | No unrelated changes | Confirmed | Only master→main + permissions block |
| 5 | `dispatcher/notifier.py` sha256 drift vs P0-1 baseline | Pre-existing | Caused by commit `ac23def` after baseline capture, not by this WO |
| 6 | No newline at EOF | Pre-existing | Both HEAD and working copy lack trailing newline; not introduced by this WO |
| 7 | PyYAML 1.1 `on:` → `True` quirk | Informational | Known YAML 1.1 behavior; structural check handles it correctly |

**No blocking defects found.**

## 15. Artifact Verification

### Implementation Artifact (`reports/aee_cicd_fix_1.md`)
- File exists: ✓
- Lines: 338
- Size: 14363 bytes
- SHA256: `66ca4bd9f40580203096cb1d3ca01e55cbb62bfdaab652bee145a6831b9f487c`

### Review Artifact (`reports/aee_cicd_fix_1_review.md`)
```
ls -la reports/aee_cicd_fix_1_review.md
wc -l reports/aee_cicd_fix_1_review.md
sha256sum reports/aee_cicd_fix_1_review.md
```
(Verified at end of review — see final command output.)

### Implementation Report Accuracy Check
| Report claim | Independent verification | Match |
|--------------|------------------------|-------|
| HEAD `b8a6dd2...` | `git rev-parse HEAD` → `b8a6dd2...` | ✓ |
| origin/main matches HEAD | `git rev-parse origin/main` → `b8a6dd2...` | ✓ |
| Only `ci-matrix.yml` modified | `git diff --name-only` → single file | ✓ |
| 13 insertions, 8 deletions | `git diff --numstat` → `13 8` | ✓ |
| Pre-change SHA256 `7a521fc...` | `git show HEAD:... \| sha256sum` → `7a521fc...` | ✓ |
| 66 CI/CD matrix tests pass | Independent re-run → 66/66 OK | ✓ |
| 517 AEE 9.x tests pass | Independent re-run → 517/517 OK | ✓ |
| 142 profile/provider tests pass | Independent re-run → 142/142 OK | ✓ |
| 18 shell wrapper tests pass | Independent re-run → 18/18 OK | ✓ |
| 6 protected files (notifier.py drift pre-existing) | Independent sha256 check → 5 unchanged, 1 pre-existing drift | ✓ |
| No `master` in post-change file | `grep -n master` → 0 results | ✓ |

**All implementation report claims independently verified. No discrepancies found.**

## 16. Production Safety

| Safety Check | Status |
|--------------|--------|
| No commit or push | ✓ Confirmed |
| No merge, rebase, stash | ✓ Confirmed |
| No `git add` or `git add -A` | ✓ Confirmed |
| No deploy, restart, docker compose up/down | ✓ Confirmed |
| No dependency installation | ✓ Confirmed |
| No cron/service changes | ✓ Confirmed |
| No file delete or move | ✓ Confirmed |
| No secrets or credentials exposed | ✓ Confirmed |
| No edits outside `.github/workflows/ci-matrix.yml` (review artifact excepted) | ✓ Confirmed |
| No remote workflow trigger | ✓ Confirmed — changes are local only, not pushed |
| No edits to implementation report or Master Plan | ✓ Confirmed |
| No edits to tests or source files | ✓ Confirmed |

## 17. Remaining Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| `dispatcher/notifier.py` pre-existing sha256 drift vs P0-1 baseline | Low | Pre-existing from commit `ac23def` (ghost task notification fix) committed after baseline capture. Not caused by this WO. To be addressed in P0-1 final review. |
| Workflow not yet tested on actual GitHub Actions runner | Low | Local YAML parse + structural check + 66 targeted tests all pass. Remote trigger test is out of scope (WO prohibits remote triggers). |
| `master` references may exist in other files (Master Plan, other reports) | Informational | Out of scope for this WO — only `ci-matrix.yml` workflow corrections authorized. |
| No newline at EOF | Informational | Pre-existing condition; both HEAD and working copy lack trailing newline. Not introduced by this WO. |

## 18. Review Ready

**YES**

All changes are limited to `.github/workflows/ci-matrix.yml` and are exactly the authorized workflow corrections:
1. Branch filter `master` → `main` (pull_request + push)
2. Least-privilege `permissions: contents: read`
3. Stale comment corrections (6 locations)

Diff is minimal (13 insertions, 8 deletions), self-contained, and independently verified. All 743 tests pass. No unrelated mutation. P0-1 shadow run not disturbed.

## 19. Commit Ready

**YES**

The change is a single-file, minimal, atomic fix ready for commit when authorized:

```bash
# When authorized:
git add .github/workflows/ci-matrix.yml
git commit -m "fix(ci): correct branch filter master→main + add least-privilege permissions"
```

Not committed per WO safety constraints (read-only review).

## 20. Telegram

**Telegram short version (for 鼎鼎):**

```
✅ WO-CICD-FIX-1 — Independent Review PASS
訊息類型: 20-section independent review
單號: WO-CICD-FIX-1 (review)
test count: 743/743 PASS (66 CI/CD + 517 AEE + 142 profile + 18 shell)
verdict: PASS

摘要: ci-matrix.yml master→main branch filter + permissions: contents: read
變更: 1 file, +13/-8, 僅 .github/workflows/ci-matrix.yml
P0-1 shadow run: 未受影響 (5/6 protected unchanged, 1 pre-existing drift)
commit: 未提交 (per WO safety constraints)
完整報告: reports/aee_cicd_fix_1_review.md
```