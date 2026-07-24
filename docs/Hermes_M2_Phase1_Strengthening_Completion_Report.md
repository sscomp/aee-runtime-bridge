# Hermes M2 Phase 1 Strengthening — Completion Report

**Date:** 2026-07-07
**Owner:** M2 (Hermes assistant)
**For:** 鼎鼎 (Scott Chang)
**SOP source:** `~/macro-report/Hermes_M2_Phase1_Strengthening_SOP_20260707.md`
**Status:** ✅ Phase 1 complete. 12/12 enhancement areas shipped.

---

## TL;DR

The bridge is now a real **Task Dispatcher**, not a thin proxy.

- Every dispatch gets a stable `TASK-YYYYMMDD-NNNN` id.
- The id survives ChatGPT session resets — query it from any chat, the CLI, or the URL.
- All state, progress, log, and output are in SQLite + per-task files.
- 21/21 unit tests pass; live end-to-end test against real Hermes 8642 passed.
- Bridge v1.2.0 is live on `https://hermes-runtime.biaobecue.com` via supervisord.

The two pain points from the SOP are gone:

1. **"GPT session 結束後任務變孤兒"** — resolved by the persistent task_id
   + SQLite store + re-arm-on-startup logic in the watcher.
2. **"結果無法回去 Hermes 端"** — resolved by per-task `reports/TASK-*/task.json`
   + `/tasks/{id}/result` endpoint that any future ChatGPT session can read.

---

## What was delivered (12/12 SOP areas)

| # | SOP area | Module | Endpoints added | Status |
|---|----------|--------|-----------------|--------|
| 1 | Architecture analysis | `docs/Hermes_M2_Phase1_Strengthening_Design.md` | — | ✅ |
| 2 | SQLite persistence | `dispatcher/db.py` (4 tables, WAL mode) | — | ✅ |
| 3 | Task ID generator | `dispatcher/ids.py` (`TASK-YYYYMMDD-NNNN`) | — | ✅ |
| 4 | State machine | `dispatcher/manager.py` (8 states, audit log) | — | ✅ |
| 5 | Progress reporting | `dispatcher/progress.py` (monotonic 0/5/10/25/40/60/80/95/100) | — | ✅ |
| 6 | Per-task log + retries | `logs/TASK-*.log` + `manager.retry()` | — | ✅ |
| 7 | Bridge integration | `app.py` (rewritten) | 6 new | ✅ |
| 8 | 9-section report | `research/report.py` + 4 prompt templates | — | ✅ |
| 9 | Repo scanning | `research/repo.py` | — | ✅ |
| 10 | Scheduler recon | `research/scheduler.py` (cron + supervisord + systemd) | — | ✅ |
| 11 | Config management | `config/` (4 JSON files) | — | ✅ |
| 12 | Observability CLI | `cli/hermes-task` (list/show/logs/rerun) | — | ✅ |
| +  | OpenAPI schema | `openapi.yaml` (v1.0.0 → v1.2.0, 6 new paths) | — | ✅ |
| +  | Tests | `tests/test_dispatcher.py` (21 cases) | — | ✅ 21/21 |
| +  | Live e2e | supervisord restart + real Hermes dispatch | — | ✅ |
| +  | Phase 2/3 plan | `docs/Hermes_M2_Phase2_Phase3_Roadmap.md` | — | ✅ |

### New endpoints (7)

```
GET  /tasks                              # list (filter: status, type)
GET  /tasks/{task_id}                    # get one
GET  /tasks/{task_id}/progress           # lightweight poll (5/10/25/40/60/80/95/100)
GET  /tasks/{task_id}/logs               # tail per-task log file
GET  /tasks/{task_id}/result             # final output + report path
POST /tasks/{task_id}/cancel             # best-effort cancel
POST /tasks/{task_id}/rerun              # clone + dispatch
```

### 8 task statuses

`pending → queued → running → (completed | failed | cancelled | timeout)`
`failed → queued` (retry path)

### 9 progress values (monotonic)

`5, 10, 25, 40, 60, 80, 95, 100` — used by `/progress` so ChatGPT can show
"thinking… 40% Scanning repo" without model hallucinating a number.

---

## Architecture at a glance

```
ChatGPT Custom GPT Action
  ↓ (POST /runs → task_id + run_id)
AEE Runtime Bridge (port 8787)
  ├─ app.py — FastAPI, multi-key auth, danger check
  ├─ dispatcher/manager.py — state machine, 8 states, audit events
  ├─ dispatcher/db.py — SQLite (WAL) — 4 tables, single writer
  ├─ dispatcher/watcher.py — polls Hermes 8642 every 2s, re-arms on startup
  └─ research/ — scheduler / repo scan / prompts / report
       ↓ (POST /v1/runs)
Hermes M2 (port 8642, Abacus)
       ↓ (state events)
Watcher writes back → manager updates → DB + log
       ↓
ChatGPT polls /tasks/{id}/progress → 80% Generating report
       ↓
Task complete → reports/TASK-20260707-0016/task.json
       ↓
Next ChatGPT session can GET /tasks/{id}/result → "4"
```

---

## Live evidence

### E2E test (2026-07-07 08:57 UTC, real Hermes dispatch)

```
POST /runs  input="What is 2+2?..."
  → task_id = TASK-20260707-0016
  → run_id  = run_fa6ece02fb2247c9a7be889ad5e6e014
poll 1: status=running pct=5
poll 2: status=running pct=5
poll 3: status=running pct=5
poll 4: status=completed pct=100

GET /tasks/.../result
  output_text = "4"
  duration_sec = 5.827
  result_path  = reports/TASK-20260707-0016/task.json
```

### Cross-reference working

`GET /runs/run_fa6ece02fb2247c9a7be889ad5e6e014` returns:
```json
{ "run_id": "run_...", "task_id": "TASK-20260707-0016", ... }
```

ChatGPT can find the task by either id.

### Supervisord restart-safe

Bridge restart re-arms 8 in-flight tasks; none become orphans.

### Unit tests

21/21 OK in 0.04s.

---

## Files added/changed

### New code (~1,950 lines)

```
dispatcher/__init__.py           10
dispatcher/db.py                150
dispatcher/ids.py                48
dispatcher/models.py            115
dispatcher/progress.py           53
dispatcher/manager.py           492
dispatcher/watcher.py           197
research/__init__.py             10
research/scheduler.py            88
research/repo.py                161
research/prompts.py              73
research/report.py               80
config/__init__.py              105
prompts/macro_v1.md              13
prompts/company_v2.md            15
prompts/industry_v1.md           13
prompts/review_v3.md             14
cli/__init__.py                   0
cli/hermes_task.py              201
cli/hermes-task                  13
tests/test_dispatcher.py        252
```

### Modified

```
app.py           17,726 → 30,254 bytes  (+12,528) — 6 new endpoints + watcher
openapi.yaml      9,560 → 16,000 bytes  (+6,440)  — 6 new paths + 6 new schemas
README.md         5,891 bytes  (kept)
```

### New docs

```
docs/Hermes_M2_Phase1_Strengthening_Design.md       365
docs/Hermes_M2_Phase2_Phase3_Roadmap.md             222
docs/Hermes_M2_Phase1_Strengthening_Completion_Report.md  (this file)
```

### Auto-generated by runtime

```
config/scheduler.json
config/report.json
config/research.json
config/model.json
prompts/<auto-discovered>.md
logs/TASK-YYYYMMDD-NNNN.log     (per task, JSON-line audit)
reports/TASK-YYYYMMDD-NNNN/task.json  (per task, full snapshot)
```

---

## How to use it

### From ChatGPT Custom GPT Action

The new `openapi.yaml` exposes all 7 new task operations. After
updating the GPT's Action schema, ChatGPT can:

1. `createRun(...)` → capture `task_id` from response
2. `getTaskProgress(task_id)` every few seconds
3. Summarize the `output_text` to the user

If the GPT session dies, the *next* session can simply re-poll
`getTask(task_id)` — the id is stable forever.

### From the CLI

```bash
hermes-task list --status=running --limit=10
hermes-task show TASK-20260707-0016
hermes-task logs TASK-20260707-0016 --tail 50
hermes-task rerun TASK-20260707-0016 --yes   # via bridge API
```

### From the bridge URL (authenticated)

```bash
curl -H "Authorization: Bearer $BRIDGE_API_KEY" \
  https://hermes-runtime.biaobecue.com/tasks/TASK-20260707-0016/result
```

---

## Known limitations (Phase 1 honest list)

1. **Blocklist is regex, not a parse tree.** A motivated attacker can
   phrase `rm -rf /` as `$(echo 'rm' '-' 'rf' '/')` and pass. Real
   defence is Hermes' own approval flow, not the bridge.
2. **Single bridge instance.** Bridge 2 cannot share a DB with
   bridge 1 (single-writer design). Phase 3 federation will fix.
3. **Watcher is in-process.** A SIGKILL of the bridge drops the
   in-memory `task → run_id` map; the next tick re-fetches it. We
   re-arm at startup, so it's not lossy, but a 2s gap exists.
4. **No token / cost tracking yet.** `usage` is captured; aggregation
   is Phase 2.3.
5. **No push notifications.** User has to poll. Phase 2.2.
6. **Hermes run_id format is opaque.** If Abacus changes the
   prefix, cross-references break. No upstream contract.

---

## What I'd do next (Phase 2 shortlist)

- 2.4 Stale task reaper (1 day, high value)
- 2.2 Telegram push for failed/timeout (½ day, matches 通知偏好)
- 2.5 Subagent research delegation (the biggest UX win)
- 3.3 Container reset survival (needed before next Abacus reset)

Details in `docs/Hermes_M2_Phase2_Phase3_Roadmap.md`.

---

## Sign-off

- ✅ 12/12 SOP areas delivered
- ✅ 21/21 unit tests pass
- ✅ Live e2e against real Hermes 8642 passes
- ✅ Bridge v1.2.0 on supervisord, v1.2.0 OpenAPI in OpenAPI schema
- ✅ Existing 4 endpoints unchanged (backward compatible)
- ⚠️ 6 known limitations documented, all addressed in Phase 2/3 plan

Ready for ChatGPT Custom GPT Action schema refresh + production use.
