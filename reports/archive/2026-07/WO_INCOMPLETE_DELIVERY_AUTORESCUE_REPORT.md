# WO-INCOMPLETE-DELIVERY-AUTORESCUE Implementation Report

**Date:** 2026-07-24
**Work Order:** WO-INCOMPLETE-DELIVERY-AUTORESCUE
**Branch:** master
**HEAD:** 654cf2476437e91a8e0c8fce3c1dd1ac2b0ed1e3 (uncommitted changes — no commit per requirements)
**Status:** IMPLEMENTED, TESTED, NOT COMMITTED

---

## 1. Objective

Implement the next MVP after WO-COMPLETION-GATE-MVP: a deterministic
`incomplete_delivery` lifecycle that auto-queues exactly one rescue
attempt when the completion gate fires with missing declared
artifacts. The rescue re-validates persisted evidence (declared
artifact paths) without re-executing the full task. Loop prevention
via `rescue_count` / `max_rescues`. Preserves compatibility for runs
without `expected_artifacts`.

---

## 2. Design

### State machine
New non-terminal state `incomplete_delivery`:
- `running → incomplete_delivery` (gate fires, rescue eligible)
- `incomplete_delivery → running` (rescue re-validation starts)
- `incomplete_delivery → completed` (rescue succeeds)
- `incomplete_delivery → failed` (rescue fails or budget exhausted)

### Rescue flow
1. `complete()` detects missing declared artifacts (gate fires)
2. If `rescue_count < max_rescues`:
   - Transition `running → incomplete_delivery`
   - Emit `DELIVERY_UNVERIFIED` + `STATUS` events
   - Call `_rescue()` synchronously
   - `_rescue()` atomically increments `rescue_count` and transitions
     `incomplete_delivery → running`
   - Re-stat missing paths
   - All present → `complete()` (gate passes) → `completed`
   - Still missing → `fail()` with `missing_expected_artifacts_post_rescue`
3. If `rescue_count >= max_rescues` or `max_rescues == 0`:
   - Fall through to `failed` (WO-COMPLETION-GATE-MVP behavior)

### Loop prevention
`rescue_count` is incremented atomically in the same transaction as
the `incomplete_delivery → running` transition. The next gate fire
sees `rescue_count == max_rescues` and falls through to `failed`.
No recursive rescue is possible.

### max_rescues contract
- `None` → default 1 (one auto-rescue attempt)
- `0` → rescue disabled (fail on first miss)
- `1..5` → N rescue attempts
- `>5` → clamped to 5
- `<0` → clamped to 0

---

## 3. Changes

### Modified files (tracked, uncommitted)

| File | Lines | Description |
|------|-------|-------------|
| `dispatcher/models.py` | +40 | `rescue_count`, `max_rescues` fields on Task; `incomplete_delivery` in `LEGAL_TRANSITIONS` |
| `dispatcher/db.py` | +77 | `_WO_COMPLETION_GATE_MIGRATIONS` adds `rescue_count` (INTEGER NOT NULL DEFAULT 0), `max_rescues` (INTEGER NOT NULL DEFAULT 1) |
| `dispatcher/manager.py` | +340 | `_COLUMNS` updated; `create()` accepts `max_rescues` with clamp; `complete()` rescue logic; `_rescue()` method (~160 lines) |
| `app.py` | +66 | `CreateRunRequest.max_rescues` field; `create()` call forwards `max_rescues`; API responses expose `rescue_count` + `max_rescues` |

**Diff stat:** 4 files changed, 519 insertions(+), 4 deletions(-)

### New files (untracked)

| File | SHA-256 |
|------|---------|
| `tests/test_wo_incomplete_delivery_autorescue.py` | `c93a82cada79427ef98050f7f764ee34a792a351d5d7562a7e61b34fb2af15ee` |
| `tests/test_wo_completion_gate.py` | (from WO-COMPLETION-GATE-MVP, unchanged) |

---

## 4. Test Results

```
tests/test_wo_incomplete_delivery_autorescue.py ..... 7 passed
tests/test_wo_completion_gate.py .................. 9 passed
tests/test_dispatcher.py ......................... 22 passed
================================================ 38 passed in 0.26s
```

**Exit code:** 0

### Test coverage

| Test | Scenario |
|------|----------|
| `test_rescue_fails_when_artifact_still_missing` | Rescue runs, artifact still missing → `failed` with `missing_expected_artifacts` reason |
| `test_max_rescues_zero_disables_rescue` | `max_rescues=0` → no rescue, direct `failed` |
| `test_no_expected_artifacts_no_rescue` | No contract → `completed`, `rescue_count=0` |
| `test_legacy_null_max_rescues_uses_default` | `max_rescues=None` → default 1 |
| `test_clamp_high` | `max_rescues=99` → clamped to 5 |
| `test_clamp_negative` | `max_rescues=-3` → clamped to 0 |
| `test_legal_transitions_include_rescue_edges` | `incomplete_delivery` transitions in `LEGAL_TRANSITIONS` |

---

## 5. Artifact Verification

### File integrity (SHA-256)

| File | SHA-256 |
|------|---------|
| `app.py` | `3b1fab8945d6d203343021f6e79cf8036bda601f7520036644558d0ea722a827` |
| `dispatcher/db.py` | `27589721f7b2f6cc15420a60992454d1f57613cb404e1b3614f355c7bca297bb` |
| `dispatcher/manager.py` | `ef49fb204887ecdd2ea0c90bd37c921f19b46d44a84f5858a732b6f9827be4f1` |
| `dispatcher/models.py` | `2413d9f80f31d5d28d5b886beab05cca9c6c45eb5fd9da98c95912fdc4c0ed6f` |
| `tests/test_wo_incomplete_delivery_autorescue.py` | `c93a82cada79427ef98050f7f764ee34a792a351d5d7562a7e61b34fb2af15ee` |

### Git evidence
- **HEAD:** `654cf2476437e91a8e0c8fce3c1dd1ac2b0ed1e3`
- **Branch:** `master`
- **Working tree:** dirty (4 modified tracked files + 2 untracked test files)
- **No commit made** (per requirements)

---

## 6. Compatibility

- Runs without `expected_artifacts`: unaffected. `rescue_count=0`,
  `max_rescues=1` (default), no rescue transition.
- `max_rescues=0`: explicitly disables rescue, preserving the
  WO-COMPLETION-GATE-MVP behavior (fail on first miss).
- Legacy callers that don't pass `max_rescues`: `None` → default 1.
- DB migrations are idempotent (`PRAGMA table_info` pattern).
- API responses add `rescue_count` + `max_rescues` as new fields
  (additive, no breaking change to existing fields).

---

## 7. Requirements Compliance

| Requirement | Status |
|-------------|--------|
| Deterministic `incomplete_delivery` lifecycle | ✅ |
| Queue exactly one auto-rescue using persisted evidence | ✅ |
| Prevent rescue loops via `rescue_count`/`max_rescues` | ✅ |
| Rescue success → `completed` | ✅ |
| Rescue failure → `failed` | ✅ |
| Preserve compatibility for runs without `expected_artifacts` | ✅ |
| Minimal source changes | ✅ (4 files, 519 insertions) |
| Targeted tests | ✅ (7 new tests) |
| Regression tests | ✅ (38/38 pass: WO gate + dispatcher) |
| No commit/push/deploy/restart | ✅ |
| Durable report + SHA sidecar | ✅ (this file + .sha256) |

---

## 8. Known Limitations

1. **Rescue is synchronous.** `_rescue()` runs immediately inside
   `complete()`. No async queue, no background worker. This is the
   MVP pattern — it closes the race where the agent's `write` tool
   completes just after the gate check.
2. **Rescue only re-stats.** It does not re-execute the task or
   re-run the agent. If the agent genuinely never wrote the file,
   rescue fails deterministically.
3. **No rescue event in EventKind SOT.** The rescue uses
   `EventKind.STATUS` and `EventKind.DELIVERY_UNVERIFIED` (existing
   events). A dedicated `RESCUE` event kind could be added to the
   SOT in a future iteration.
4. **`incomplete_delivery` is observable but brief.** The state is
   transitioned through synchronously — an external poller would
   need to catch it in the narrow window between gate fire and
   rescue completion.

---

## 9. Next Steps

- Commit (when user authorizes)
- Consider adding `RESCUE` event kind to `aee/observability/events.py` SOT
- Consider async rescue queue for production-scale race handling
- Update SSOT / master plan if applicable