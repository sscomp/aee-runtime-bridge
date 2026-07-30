# AEE-7.7d + AEE-7.7e — Staging Boundary Report

**Repo:** `/home/ubuntu/hermes-runtime-bridge`
**Branch:** `master`
**HEAD:** `7c153a9734822db7c61762faa1ac4951551164bf`
**Generated:** 2026-07-12 (Asia-Taipei)

## 1. The 3-Group Layout

### G1 — AEE-7.7d controlled migration executor
- **New files (2):**
  - `aee/audit/sidecar_migration.py` — 27,341 B, 693 L, SHA `b5255a58…`
  - `aee/tests/test_aee77d_sidecar_migration.py` — 88,672 B, 2,352 L, SHA `bdce4684…`
- **Modified files (0):**
- **Staging command:** `git add aee/audit/sidecar_migration.py aee/tests/test_aee77d_sidecar_migration.py`
- **Test prerequisite for commit:** 46/46 AEE-7.7d targeted PASS
- **Hunk split required:** No (both files are untracked new)
- **Boundary ready:** YES
- **Commit ready:** NO (per user brief — this round does not commit)

### G2 — AEE-7.7e dry-run projection/apply + shared `__init__.py` re-export
- **New files (2):**
  - `aee/audit/live_migration_dryrun.py` — 51,373 B, 1,318 L, SHA `aea50f10…`
  - `aee/tests/test_aee77e_live_migration_dryrun.py` — 65,106 B, 1,663 L, SHA `58a3b32e…`
- **Modified files (1):**
  - `aee/audit/__init__.py` — `+80 / -0` lines, pre 120 L → post 200 L
  - pre-SHA `0be13f3c…` (HEAD) → post-SHA `139ada9f…` (working tree)
  - Hunks: ONE single hunk `@@ -100,21 +100,101 @@`
  - Internal structure:
    - Hunk 1 (lines 100-126): 7.7d re-exports (6 symbols)
    - Hunk 2 (lines 127-181): 7.7e re-exports (9 symbols)
    - `__all__` block (lines 163-200): 6 d-entries + 9 e-entries, **interleaved alphabetically**
- **Staging command:** `git add aee/audit/live_migration_dryrun.py aee/tests/test_aee77e_live_migration_dryrun.py aee/audit/__init__.py`
- **Test prerequisite for commit:** 57/57 AEE-7.7e targeted PASS + 1/1 gated live corpus PASS
- **Hunk split required:** YES (shared file with interleaved ownership)
- **Boundary ready:** YES (single-commit, full 80-line delta in G2)
- **Commit ready:** NO (per user brief — this round does not commit)

## 2. The Hunk-Split Question

**Question:** Can `aee/audit/__init__.py`'s 80-line delta be cleanly split so that G1's commit carries only 7.7d re-exports and G2's commit carries only 7.7e re-exports?

**Answer: Technically YES, operationally NO.**

The hunk is a single block (`@@ -100,21 +100,101 @@`) covering 80 added lines. Inside, the two logical groups (7.7d re-exports at lines 100-126, 7.7e re-exports at lines 127-181) are well-separated and could be hunk-split via `git add -p` line-by-line. The `__all__` block at lines 163-200 is **alphabetically interleaved** — 7.7d entries (`DEFAULT_STATUS_FILTER`, `MIGRATION_EXEC_SCHEMA_VERSION`, `MigrationExecutionResult`, `MigrationStatus`, `PerTaskMigrationOutcome`, `execute_sidecar_migration`) and 7.7e entries (`DEFAULT_TARGET_POLICY_VERSION`, `LIVE_MIGRATION_DRYRUN_SCHEMA_VERSION`, `LiveMigrationDryrunResult`, `PerTaskProjection`, `ProjectedMigrationResult`, `ProjectedOutcome`, `project_migration_execution`, `run_live_migration_apply`, `run_live_migration_dryrun`) sit side by side. A line-by-line hunk split IS feasible (s = 7.7d line, e = 7.7e line) but requires:

1. **Two intermediate states of `__init__.py`** — first with d-entries only, then d+e. This means G1 ships a "partial `__init__.py`" and G2 ships "complete `__init__.py`" — the only sensible approach.
2. **G1's commit must use a TEMPORARY `__init__.py` that will be overwritten in G2** — this is two commits to the same file in 5 minutes, with the first commit being "wrong" until G2 lands. Rollback of G1 alone leaves a half-finished re-export.

**Chosen strategy (safer alternative):** G1 ships WITHOUT the `__init__.py` re-export. The 7.7d production module is fully self-sufficient — callers do `from aee.audit.sidecar_migration import execute_sidecar_migration` directly. G2 ships the FULL 80-line `__init__.py` delta in one piece. This is:

- **1 commit, 1 file, 1 delta** for G1 (2 new files only)
- **1 commit, 1 new + 1 new + 1 modified** for G2
- Total: 2 commits, 4 new files, 1 modified file

**Commit order (proposed, NOT executed this round):**

```
G1: feat(aee): add AEE-7.7d controlled sidecar migration executor
    aee/audit/sidecar_migration.py                              (new, 27341 B)
    aee/tests/test_aee77d_sidecar_migration.py                  (new, 88672 B)
G2: feat(aee): add AEE-7.7e live migration dry-run + projection/apply
    aee/audit/live_migration_dryrun.py                           (new, 51373 B)
    aee/tests/test_aee77e_live_migration_dryrun.py               (new, 65106 B)
    aee/audit/__init__.py                                       (modified, +80 / -0)
```

## 3. Set-Equality Pre-Commit Check

Per the AEE-7.7b pattern, the pre-commit check is:

```
staged_set == group_set_1 ∪ group_set_2
            == {aee/audit/sidecar_migration.py,
                aee/tests/test_aee77d_sidecar_migration.py,
                aee/audit/live_migration_dryrun.py,
                aee/tests/test_aee77e_live_migration_dryrun.py,
                aee/audit/__init__.py}
            == 4 new + 1 modified
```

Any 6th file appearing in the staged diff is a smell (an unannounced file).

## 4. Secret / Debug / Raw-Prompt Scan

```
$ grep -E "BRIDGE_API_KEY|API_SERVER_KEY|os\.environ\[|subprocess\.call|os\.system|shell=True|prompt" \
       aee/audit/sidecar_migration.py \
       aee/audit/live_migration_dryrun.py \
       aee/tests/test_aee77d_sidecar_migration.py \
       aee/tests/test_aee77e_live_migration_dryrun.py \
       aee/audit/__init__.py

(no matches)
```

## 5. Diff / Stat / Numstat

```
$ git diff --numstat aee/audit/__init__.py
80      0       aee/audit/__init__.py
```

```
$ git status --short --untracked-files=all
 M aee/audit/__init__.py
?? aee/audit/live_migration_dryrun.py
?? aee/audit/sidecar_migration.py
?? aee/tests/test_aee77d_sidecar_migration.py
?? aee/tests/test_aee77e_live_migration_dryrun.py
```

5 entries in `git status` ↔ 5 in `G1 ∪ G2` ↔ set-equality OK.

## 6. Unstage Plan (post-commit)

If G1 lands and G2 must be reverted (e.g. 7.7e fails a future regression):
```
git checkout HEAD -- aee/audit/__init__.py
git rm aee/audit/live_migration_dryrun.py
git rm aee/tests/test_aee77e_live_migration_dryrun.py
```

If G2 lands and G1 must be reverted (only — keep G2):
```
git rm aee/audit/sidecar_migration.py
git rm aee/tests/test_aee77d_sidecar_migration.py
# __init__.py must KEEP its 7.7d re-exports (G2 re-exports both d and e)
```

## 7. Targeted Tests Per Group

### G1 (pre-commit, must pass)
```
python3 -m unittest aee.tests.test_aee77d_sidecar_migration -v
# Expected: 46/46 PASS in ~0.23s
```

### G2 (pre-commit, must pass — includes gated live corpus smoke)
```
python3 -m unittest aee.tests.test_aee77e_live_migration_dryrun -v
# Expected: 57/57 PASS + 1 env-gated skip (default env)
AEE77E_LIVE_CORPUS=1 python3 -m unittest aee.tests.test_aee77e_live_migration_dryrun.TestLiveCorpusGatedDryRun
# Expected: 1/1 PASS + 0 skip — proves zero-write on real reports/ corpus
```

### Cross-group (G1+G2 atomic, must pass)
```
python3 -m unittest aee.tests.test_aee77_live_audit \
                  aee.tests.test_aee77_apply_sidecars \
                  aee.tests.test_aee77c_sidecar_inventory \
                  aee.tests.test_aee77d_sidecar_migration \
                  aee.tests.test_aee77e_live_migration_dryrun
# Expected: 178/178 PASS + 2 env-gated skips
```

## 8. Boundary Verdicts

| Group | Files | Hunk Split | Boundary Ready | Commit Ready |
|---|---|---|---|---|
| G1 | 2 new | N/A | YES | NO (this round) |
| G2 | 2 new + 1 modified | feasible but deferred | YES | NO (this round) |
| shared `__init__.py` | in G2 only | n/a (single-commit) | YES (safe additive) | NO (this round) |
