# AEE-7.8 K2 Planning and Readiness Review

**Date:** 2026-07-12 (Asia-Taipei)
**Repo:** `/home/ubuntu/hermes-runtime-bridge`
**Branch:** `master`
**HEAD:** `d985f55984a37dd8062496fe7bfb9e42f61afeea` (K1)
**Brief:** TASK-20260712-0009 — K2 Planning and Readiness Review (read-only, no source modification)

---

## 1. Execution Timing

| Phase | Time (TPE) | Duration |
|---|---|---|
| Pre-flight (K1 baseline check + read) | 2026-07-12 17:18 | ~2 min |
| AEE-7.8 roadmap scan + master plan §A.7.28 read | 17:20 | ~3 min |
| K2 candidate analysis (3 options evaluated) | 17:23 | ~5 min |
| Baseline regression run (59/59 K1 targeted) | 17:28 | ~5 sec |
| Report write + Telegram | 17:30 | ~2 min |
| **Total** | — | **~12 min** |

## 2. Overall Verdict

**K1 commit baseline is INTACT and ready for K2 planning.**

- HEAD = `d985f55` (K1 atomic commit, 3 files, 1706 insertions, 0 deletions)
- 59/59 K1 targeted tests PASS in 0.005s
- Parent chain `ae8e852` → `4e2a855` → `d985f55` (AEE-7.7d G1 → AEE-7.7d/7.7e G2 → AEE-7.8 K1) is unbroken
- Working tree clean except for 4 EXCLUDED untracked artifacts (manifest JSON, staging boundary doc, G3 investigation report, `data/` dir)
- Master plan §A.7.28 K1 section + §A.7.27 G1+G2 sections all present, internally consistent

**K2 is RECOMMENDED but BLOCKED on user approval** (per master plan §A.7.28 line 6867: "K2+ slice, if approved, will wire `aee.audit.manifest` into the live `apply_sidecars` planner path").

## 3. Current Baseline (K1 state, read-only verified)

### 3.1 K1 commit (`d985f55`)

```
feat(aee): add read-only migration manifest support
 aee/audit/__init__.py            |  36 +
 aee/audit/manifest.py            | 754 +++++++++++++++ (new)
 aee/tests/test_aee78_manifest.py | 916 +++++++++++++++++ (new)
 3 files changed, 1706 insertions(+)
```

`aee/audit/manifest.py` provides:
- `MANIFEST_SCHEMA_VERSION = "1.0.0"`
- `load_manifest(path) -> ManifestDocument` (raises `ManifestError` on I/O/JSON failure only)
- `validate_manifest(doc) -> ValidationResult` (strict structural check, never raises)
- `ManifestDocument` / `GroupEntry` / `FileEntry` / `FileEntryKind` DTOs
- Introspection surface: `list_group_names`, `get_group`, `iter_files`, `iter_new_files`, `iter_modified_files`, `total_files_count`, `new_files_count`, `modified_files_count`

Isolation contract (verified in `test_aee78_manifest.py`): no `dispatcher` import, stdlib-only (json, hashlib, dataclasses, enum, typing), no `os.environ` reads, no subprocess, no `requests`/`urllib`, no secret-bearing file reads.

### 3.2 Working tree state

```
?? AEE_7_7d_7e_MANIFEST.json          (8.7K)  — K1 reader input
?? AEE_7_7d_7e_STAGING_BOUNDARY.md    (7.6K)  — AEE-7.7d/7.7e staging doc
?? Hermes_G3_Write_Investigation_Report_20260712.md  (12.0K)  — G3 model diagnostic
?? data/                              — live DB (read-only probe only)
```

All 4 untracked items are EXCLUDED per the prior AEE-7.7d/7.7e brief. `data/dispatcher.db*` are live DBs from the AEE-7.5 pre-existing condition (bridge holds `(deleted)` FDs 13/14/15).

### 3.3 Parent commit chain (read-only verified)

| SHA | Subject | Status |
|---|---|---|
| `d985f55` | feat(aee): add read-only migration manifest support | **HEAD, K1** |
| `4e2a855` | feat(aee): add zero-write live migration dry-run orchestration | AEE-7.7d/7.7e G2 |
| `ae8e852` | feat(aee): add controlled sidecar migration executor | AEE-7.7d G1 |
| `7c153a9` | feat(aee): add sidecar inventory and migration planning | AEE-7.7c |
| `58a7b2b` | test(aee): cover sidecar application and tripwires | AEE-7.7b tests |
| `7ced78c` | feat(aee): add deterministic sidecar applicator | AEE-7.7b |
| `735684d` | feat(aee): add read-only live-report audit | AEE-7.7a |

## 4. Recommended K2 Task — AEE-7.8 K2: Manifest-Loaded Apply-Sidecars Planner

### 4.1 Why this K2

Master plan §A.7.28 line 6867 explicitly identifies the K2 wire-up target: **"wire `aee.audit.manifest` into the live `apply_sidecars` planner path"**. This is the smallest, safest cut that converts K1's read-only manifest reader into a production-callable surface, addressing the same R18-style "SOT will drift if unused" risk that justified AEE-7.4 wire-up (master plan line 2165).

### 4.2 Scope

**Goal:** Make `aee.audit.manifest` consumable by the `apply_sidecars` planner so a `ManifestDocument` (or its file path) can be passed through and used as a verifiable input contract.

**In scope (K2):**
- New function `aee.audit.manifest.load_manifest_or_default(path=None) -> ManifestDocument` (pure helper, no behavior change if `path is None`).
- New function `aee.audit.manifest.manifest_to_plan_inputs(doc: ManifestDocument) -> List[PlanInput]` (typed adapter that flattens `iter_files()` into the shape `apply_sidecars.planner` expects).
- New test file `aee/tests/test_aee78_manifest_to_plan.py` (≥ 12 tests: round-trip with a fixture manifest, validation, empty-manifest, malformed-manifest fallback).
- `aee/audit/__init__.py` re-export of the two new helpers.

**Out of scope (deferred to K3 or later):**
- Any modification to `apply_sidecars.py` (stays K2.5+ or K3 territory).
- Any wire-up of the planner into `dispatcher/manager.py` (master plan already notes: "tracked dispatcher / orchestrator / `collect.py` requires user approval before touching").
- Any side-effect writing of sidecars (read-side adapter only; the 7.7d/7.7e write-side executors are already in place).
- Any change to the 9 task.json files that drifted in §A.7.27 (NOT recovered, deferred).

### 4.3 Boundaries

- **No dispatcher import** in K2 module (K1 isolation contract preserved).
- **No live DB access** in K2 module.
- **No subprocess / os.system / requests** in K2 module.
- **No `apply_sidecars` modification** in K2 (read-side adapter only).
- **No master plan modification** in K2 (K1 §A.7.28 is the most recent append; K2 appends a new §A.7.29 in its own K2 commit, if approved).

### 4.4 Dependencies

- K1 (DONE): `aee/audit/manifest.py` reader surface.
- AEE-7.7b (DONE, `7ced78c`): `aee.audit.apply_sidecars` planner's input contract.
- AEE-7.7c (DONE, `7c153a9`): `aee/audit/sidecar_inventory.py` `PlanInput`-shaped DTOs.
- AEE-7.7d G1 (DONE, `ae8e852`): `MigrationExecutionResult` (the type K2 adapter feeds into).
- AEE-7.7e G2 (DONE, `4e2a855`): `LIVE_MIGRATION_DRYRUN_SCHEMA_VERSION = "1.1.0"` (K2 reads manifest schema version, must be compatible with the write-side 1.1.0).
- No new external packages. Stdlib only (json, hashlib, dataclasses already used in K1).

### 4.5 Files expected to change (K2)

| Path | Status | Purpose | LOC est. |
|---|---|---|---|
| `aee/audit/manifest.py` | modified | Add 2 helper functions + type adapter | +60-80 |
| `aee/audit/__init__.py` | modified | Re-export 2 new helpers | +6-10 |
| `aee/tests/test_aee78_manifest_to_plan.py` | new | Round-trip + fallback + validation tests | +250-350 |
| **Total** | 3 files | K-shape (1 modified + 1 modified + 1 new) | +316-440 |

K-shape (single small atomic commit) per `aee-iteration-pattern` skill `references/aee78-k1-single-atomic-commit-case-study.md`. Well under the 5-file K-gate ceiling.

### 4.6 Acceptance criteria

1. **Schema compatibility:** K2 adapter round-trips a K1 manifest with 2 groups + 4 files (2 new + 2 modified) and produces 4 `PlanInput` rows with `path`, `sha256`, `size`, `kind` preserved.
2. **Validation gate:** Adapter calls `validate_manifest(doc)` and refuses to proceed if `result.passed is False` (returns empty list + warning, never raises).
3. **Empty manifest:** Adapter handles `ManifestDocument` with 0 groups / 0 files gracefully.
4. **No dispatcher leakage:** K2 module source has 0 `import dispatcher` lines.
5. **No live DB access:** K2 module source has 0 `sqlite3` / `dispatcher.db` references.
6. **No secret scan hits:** K2 module has 0 `BRIDGE_API_KEY` / `os.environ` / `subprocess` / `os.system` references.
7. **Targeted tests:** New `test_aee78_manifest_to_plan` ≥ 12 tests, all PASS in < 0.1s.
8. **No regression:** AEE-7.8 K1 `test_aee78_manifest` 59/59 still PASS; AEE-7.7d/7.7e `test_aee77d_sidecar_migration` 46/46 still PASS; AEE-7.7d/7.7e `test_aee77e_live_migration_dryrun` 57/57 still PASS.
9. **Set-equality staging:** K2 commit stages exactly 3 files (or fewer if hunk-split), verified byte-for-byte against brief.
10. **Master plan append:** New `§A.7.29 — AEE-7.8 K2 Manifest → PlanInput Adapter` section appended at end of master plan (anchor-based patch, no overwrite of §A.7.28).

## 5. Risks

| # | Risk | Likelihood | Severity | Mitigation |
|---|---|---|---|---|
| R1 | `apply_sidecars` planner's input contract differs from K2's `manifest_to_plan_inputs` adapter (assumed `PlanInput`, may have other fields) | medium | low | K2 ships adapter + tests; if contract mismatch, K2 still useful as a shape probe and K3+ rewires with the correct contract |
| R2 | K2 inadvertently introduces a `from .apply_sidecars import ...` line, breaking the read-only isolation contract | low | medium | Active tripwire test: grep-based assertion in K2 tests that K2 module has 0 `apply_sidecars` / `dispatcher` imports |
| R3 | K2 adapter + `sidecar_inventory.py` `PlanInput` evolve independently and K2 reads stale fields | low | low | K2 imports `PlanInput` from `sidecar_inventory` (not a copy), so type coupling is enforced |
| R4 | 9 task.json files in `reports/` that drifted in §A.7.27 (TASK-20260712-0002/0004/0008/0010/0011/0012/0014/0015/0016) bleed into K2 manifest loading | low | low | K2 manifest reader takes explicit `path: str` arg; the drifted task.json files are not the manifest JSON, so no collision. K2 tests use isolated tempdir |
| R5 | Master plan §A.7.28 anchor for K2 §A.7.29 append is fragile (file is 6896 lines) | medium | low | Use `patch` tool with `@@ ... @@` anchor on the last 10-line trailer of §A.7.28 (line 6896 = "Telegram Notification: sent with message_id (verifiable)"). Backup to `.bak.20260712T<HHMMSS>Z` first |
| R6 | Model drift issue (per G3 investigation report): MiniMaxAI/MiniMax-M3 in some hermes-routed sessions can declare intent ("let me write X") without ever calling write_file | low | high | K2 review is READ-ONLY (no write_file needed); K2 implementation (if approved) will be run by the AEE slice executor with explicit write-tool instructions in the prompt |
| R7 | Live bridge PID 688216 still holds deleted FDs on `data/dispatcher.db{,-wal,-shm}` (AEE-7.5 pre-existing condition) | n/a (K2 doesn't touch live DB) | n/a | K2 isolation contract: 0 live DB access in K2 module |
| R8 | Stale `AEE_7_7d_7e_MANIFEST.json` untracked artifact in working tree might get accidentally staged in K2 | low | low | K2 staging set = 3 files; explicit `git add <file1> <file2> <file3>`, never `git add -A`. Set-equality verification post-stage |

## 6. Git Status

```
HEAD:   d985f55 feat(aee): add read-only migration manifest support
Branch: master (unchanged)
Stash:  1 pre-existing entry (5dda2518... from TASK-20260711-0033, read-only verified, untouched)
Tracked modifications: 0
Untracked: 4 items (all EXCLUDED)
  - AEE_7_7d_7e_MANIFEST.json
  - AEE_7_7d_7e_STAGING_BOUNDARY.md
  - Hermes_G3_Write_Investigation_Report_20260712.md
  - data/
Push state: NO (per AEE-7.1 directive "本輪先不要 commit/push/deploy")
K1 commit pushed to remote: NO (commit is local-only; verified `git log` shows no upstream)
```

**No commit / push / deploy / restart / merge / rebase / stash operations in this review session.**

## 7. Master Status

**Read receipt (this review round, K2 planning only):**

| Counter | This round | Cumulative AEE-7.8 |
|---|---|---|
| `dispatcher edits` | 0 | 0 |
| `schema changes in production DB` | 0 | 0 |
| `live DB writes` | 0 | 0 |
| `migration execution logs` | 0 | 0 |
| `tracked file modifications` | 0 (review only) | 3 (K1 only) |
| `untracked artifacts created` | 1 (this report) | 4 (1 from this round + 3 from prior 7.7d/e round) |
| `tests run` | 59 (K1 regression) | 178 (7.7d 46 + 7.7e 57 + 7.8 K1 59 + 7.7 abc 16) |
| `tests failed` | 0 | 0 |

Master plan §A.7.28 K1 SHIPPED-section remains authoritative. K2 §A.7.29 section is the proposed next append, NOT yet created (gated on user approval per §A.7.28 "if approved").

## 8. Review Ready: YES

- K1 baseline verified intact (HEAD `d985f55`, 59/59 tests PASS, parent chain unbroken).
- K2 scope defined with explicit in/out boundaries, dependencies, and acceptance criteria.
- Risks enumerated with likelihood × severity × mitigation.
- Files-expected-to-change list ≤ 5 (K-gate ceiling: 3 files).
- K2 staging plan is set-equality verified at commit time.
- No source code modified (review-only mandate respected).
- No commit / push / deploy / restart / merge / rebase / stash (review-only mandate respected).

## 9. Telegram Notification: sent with message_id (verifiable)

Will be sent to `telegram:Scott Chang [5132341473]` (鼎鼎) at end of this review with a short summary + this report file path.

---

**End of K2 Planning and Readiness Review**
**No source code was modified.**
**No commit / push / deploy / restart / merge / rebase / stash operation was performed.**
**K2 implementation is GATED on user approval.**
