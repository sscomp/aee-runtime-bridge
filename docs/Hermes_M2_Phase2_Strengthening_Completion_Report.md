# Hermes M2 Phase 2 Strengthening — Completion Report

**Date:** 2026-07-07
**Owner:** M2 (Hermes assistant)
**For:** 鼎鼎 (Scott Chang)
**SOP source:** `~/hermes-runtime-bridge/docs/Hermes_M2_Phase1_Strengthening_Design.md` (Phase 2 spec section)

---

## TL;DR

Phase 2 ships all 4 priority items: **reaper, notifier, safety upgrade, usage/cost summary**. The bridge now actively defends itself and tells you what it did.

| Priority | Module | Status | End-to-end test |
|---|---|---|---|
| **P1** | `dispatcher/reaper.py` (stale-task detection) | ✅ shipped | Test 3 — aged task → `timeout` |
| **P2** | `dispatcher/notifier.py` (Telegram push) | ✅ shipped | Local log wired; Telegram needs env vars |
| **P3** | `dispatcher/safety.py` (allowlist + blocklist + approval) | ✅ shipped | Tests 1 + 2 — block + approval |
| **P4** | `dispatcher/usage.py` (token + cost rollup) | ✅ shipped | Test 4 — $0.1075 across 12 tasks |

**Test suite:** 40/40 PASS (Phase 1: 21, Phase 2: 19) · **End-to-end:** 5/5 PASS.

---

## What changed since Phase 1

### New modules

| File | LOC | Purpose |
|---|---|---|
| `dispatcher/reaper.py` | 200 | Pure-function stale detection + `manager.timeout()` driver |
| `dispatcher/notifier.py` | 200 | Telegram alert + rate limit + local fallback log |
| `dispatcher/safety.py` | 145 | Allowlist + blocklist + approval gate + path safety |
| `dispatcher/usage.py` | 215 | Token + cost aggregation (today/7d/30d/all/by-task) |
| `tests/test_phase2.py` | 295 | 19 unit tests for the new modules |
| `tests/phase1_acceptance.py` | 150 | Runs `/health` `/tasks` `/result` + supervisor + CLI → MD |
| `tests/phase2_acceptance.py` | 130 | Runs 5 e2e tests (P1/P2/P3/P4) |

### Modified

- `dispatcher/models.py` — `timeout` is now a legal transition from pending/queued/running/waiting
- `dispatcher/manager.py` — `timeout()` method + auto-derive `model_name` from `raw.model` on `complete()`
- `dispatcher/watcher.py` — reaper runs every 5th tick; config hot-reload; `/logs/reaper.log` audit
- `app.py` — `/health` shows reaper/safety/notifier; `POST /runs` returns `safety` decision; new `GET /stats/usage`
- `cli/hermes_task.py` — `usage` subcommand with `--today` / `--7d` / `--30d` / `--all` / `--task TASK-xxx` / `--json`
- `openapi.yaml` — 2 new schemas, 1 new path (`/stats/usage`)
- `config/__init__.py` — 4 new config blocks: `reaper.json`, `notify.json`, `safety.json`, `pricing.json`

---

## P1 — Stale Task Reaper

**Heuristics** (configurable in `config/reaper.json`):

| Threshold | Default | Targets |
|---|---|---|
| `stale_queued_sec` | 300 (5 min) | Queued task with no progress |
| `stale_running_sec` | 1800 (30 min) | Running/waiting task with no progress events |
| `max_total_age_sec` | 7200 (2 h) | Total age cap regardless of activity |
| `grace_period_sec` | 30 | Newly-created tasks are protected |

**Side effects of a reap:**
1. `tasks.status` → `timeout` (visible in `/tasks` list, `hermes-task list`)
2. `task_events` row appended (`kind=timeout`, `reason=<why>`)
3. `logs/TASK-xxx.log` gets a WARN line
4. `logs/reaper.log` gets a JSON audit line
5. `dispatcher.notifier.notify_timeout()` fires (Telegram if configured, else local log)

**Live dashboard:** `GET /health` → `reaper: { running, queued, waiting, would_reap }` tells you the next sweep's casualty count.

**E2E test 3** confirmed: TASK-20260707-0022, created at `now - 10 min`, hermes_run_id cleared, status reverted to `queued` → after one watcher tick (12s) → `status: timeout`.

**Config hot-reload:** edit `config/reaper.json` and the next 5th-tick reap picks up the new thresholds — no bridge restart needed.

---

## P2 — Failure Notification

**Module:** `dispatcher/notifier.py`

**Trigger points:**
- `dispatcher/reaper.py` → `notify_timeout(task_id)` after every reap
- `dispatcher/manager.fail()` → can be wired into a notifier hook (Phase 3 work)

**Wire format** (HTML, Telegram-native):
```
❌ Task timeout
<code>TASK-20260707-0022</code>
Title: test reaper
Type: <code>normal</code> · Status: <code>timeout</code>
Duration: 0.0s
Hermes run: —

Error: reaper: queued 600s exceeds stale_queued_sec=300

/logs: <code>/tasks/TASK-20260707-0022/logs</code>
/result: <code>/tasks/TASK-20260707-0022/result</code>
```

**To enable Telegram:**
```bash
# In .env:
TELEGRAM_BOT_TOKEN=...      # 從 @BotFather 拿
TELEGRAM_CHAT_ID=...        # 你的 chat id
# In config/notify.json: "telegram.enabled": true
```

**Local fallback:** if Telegram is disabled, every alert goes to `logs/notifier.log` (JSONL, one event per line). `/health.notifier.enabled` will report `false` until you wire env vars.

**Rate limit:** default 20 messages per hour (sliding window). Configurable in `config/notify.json`. The notifier records every attempt (sent, rate-limited, or no-credentials) to the local log so you can audit what *would* have been sent.

---

## P3 — Safety Policy Upgrade

**3-layer defense:**

1. **Hard blocklist** (substring, always reject): `rm -rf /`, `rm -rf ~`, `mkfs`, `dd if=`, `shutdown`, `reboot`, fork bomb
2. **Allowlist** (only in `mode=ops` or `mode=coding`): 42 known-safe commands (ls, cat, python3, git, docker, supervisorctl, …)
3. **Approval gate** (cross-mode): `sudo `, `apt install`, `apt remove`, `pip install`, `curl ... | sh` → not rejected, but flagged `requires_human: true` so the orchestrator (ChatGPT) asks the user before proceeding.

**Path safety** (mode=ops only): any file op must target `/home/ubuntu/`, `/tmp/`, or `/opt/`. `/etc/passwd` etc. get blocked.

**Wire format** (response includes `safety` field):
```json
{
  "action": "block" | "require_approval" | "allow",
  "reason": "...",
  "matched": "rm -rf /",
  "needs_human": true | false,
  "meta": { ... }
}
```

**E2E proof:**
- `rm -rf /` → HTTP 400, decision.action=block, decision.matched="rm -rf /" ✅
- `sudo apt install nginx` → HTTP 200, response.requires_review=True, decision.action=require_approval, decision.matched="sudo " ✅
- `ls -la /home/ubuntu` (mode=ops) → HTTP 200, decision.action=allow ✅
- `cat /etc/passwd` (mode=ops) → HTTP 400, decision.action=block, decision.matched="/etc/passwd" ✅

**Audit trail:** every blocked command creates a task with status=failed, title=`[REJECTED] ...`, error=`safety reject: ...`. So `/tasks` shows you what was rejected.

---

## P4 — Usage / Cost Summary

**`GET /stats/usage?period=today|7d|30d|all&task=TASK-xxx`:**

```json
{
  "period": "today",
  "task_id": null,
  "totals": {
    "task_count": 12, "input_tokens": 35358, "output_tokens": 96,
    "estimated_cost_usd": 0.107514
  },
  "by_type": [
    {"type": "normal", "task_count": 8, "input_tokens": 35350, "output_tokens": 80, "estimated_cost_usd": 0.1067},
    {"type": "research", "task_count": 2, "input_tokens": 1, "output_tokens": 0, "estimated_cost_usd": 0.0},
    {"type": "review", "task_count": 2, ...}
  ],
  "by_model": [
    {"model": "hermes-agent", "task_count": 1, "input_tokens": 35350, "output_tokens": 80, "estimated_cost_usd": 0.106},
    {"model": "unknown", "task_count": 11, "input_tokens": 8, "output_tokens": 16, "estimated_cost_usd": 0.0015}
  ],
  "by_day": [...]   // only for 7d/30d/all
}
```

**CLI equivalent:**
```bash
$ hermes-task usage --today
=== usage (today) ===
  tasks:        12
  input_tokens: 35,358
  output_tokens:96
  est. cost:    $0.1075

  by type:
    normal: 8 tasks, in=35,350 out=80 $0.1067
    research: 2 tasks, in=1 out=0 $0.0000
    review: 2 tasks, in=7 out=16 $0.0008

  by model:
    hermes-agent: 1 tasks, in=35,350 out=80 $0.1061
    unknown: 11 tasks, in=8 out=16 $0.0015
```

**Pricing model:** `config/pricing.json` lists per-1M-token rates for known models (claude-sonnet-4-6, kimi-k2.6:cloud, gpt-4o, gpt-4o-mini). `hermes-agent` is unknown so the system uses `default` ($3 input / $15 output per 1M) which is conservative. Update pricing.json to reflect your negotiated rates.

**Normalize both schemas:** accepts OpenAI-style `{input_tokens, output_tokens, total_tokens}` and Hermes M2 short-form `{p, c, t}`. Bridge's existing tasks were populated by the watcher with the short-form (e.g. `{"p": 1}`) so the normalizer is essential.

---

## Verification

### Unit tests
```
$ .venv/bin/python tests/test_dispatcher.py    # Phase 1
Ran 21 tests in 0.047s — OK

$ .venv/bin/python tests/test_phase2.py        # Phase 2
Ran 19 tests in 6.148s — OK

$ .venv/bin/python tests/test_phase1_acceptance.py  # /health /tasks /result supervisor list
wrote docs/phase1_acceptance_20260707_092245.md (10491 bytes)

$ PHASE2_TEST_KEY=… .venv/bin/python tests/phase2_acceptance.py
=== ALL PHASE 2 ACCEPTANCE TESTS PASSED ===
```

### End-to-end output (5/5 PASS)

```
## Test 1: P3 blocklist - 'rm -rf /' should be rejected (400)
  HTTP 400 body: {"detail": {"code": "dangerous_input", ...}}
  PASS

## Test 2: P3 approval gate - 'sudo apt install' should pass with requires_review=True
  HTTP 200 status: started requires_review: True
  safety: {'action': 'require_approval', 'matched': 'sudo ', 'needs_human': True}
  PASS

## Test 3: P1 reaper - aged queued task should be reaped
  created: TASK-20260707-0022
  rewound created_at by 10 min and cleared hermes_run_id, waiting 12s for reaper tick...
  task status: timeout
  PASS

## Test 4: P4 usage aggregation
  totals: {'task_count': 12, 'input_tokens': 35358, 'output_tokens': 96, 'estimated_cost_usd': 0.107514}
  by_type: ['normal', 'research', 'review']
  by_model: ['hermes-agent', 'unknown']
  PASS

## Test 5: /health shows Phase 2 summary
  version: 1.2.0 phase: 2 — Reaper + Notifier + Safety upgrade
  reaper: {'running': 2, 'queued': 8, 'waiting': 0, 'would_reap': 0}
  safety: {'mode': 'blocklist_plus_allowlist', 'allowlist_size': 42, 'blocklist_size': 8, 'approval_size': 6, 'log_rejected': True}
  notifier: {'enabled': False, 'notify_on': ['failed', 'timeout'], 'rate_limit_per_hour': 20, 'bot_token_present': False, 'chat_id_present': False}
  PASS
```

### Live
- Bridge `v1.2.0` running (supervisord pid 2259, restarted 2026-07-07 09:21 UTC)
- `/health` reachable at `https://hermes-runtime.biaobecue.com/health`
- 16+ tasks now persisted in `data/dispatcher.db`

---

## Files touched

```
Modified:
  app.py                                       # 4 new imports, /health + /stats/usage, safety decision
  dispatcher/manager.py                        # +model_name in complete(); +timeout() method
  dispatcher/models.py                         # +timeout in LEGAL_TRANSITIONS
  dispatcher/watcher.py                        # +reaper tick, +notify hook, +_append_reaper_log
  cli/hermes_task.py                           # +usage subcommand
  openapi.yaml                                 # +/stats/usage path, 2 schemas
  config/__init__.py                           # +reaper.json, +notify.json, +safety.json, +pricing.json

New:
  dispatcher/reaper.py                         # 200 lines, pure-function stale detection
  dispatcher/notifier.py                       # 200 lines, Telegram + local log
  dispatcher/safety.py                         # 145 lines, allowlist + blocklist + approval
  dispatcher/usage.py                          # 215 lines, token + cost rollup
  tests/test_phase2.py                         # 295 lines, 19 tests
  tests/phase1_acceptance.py                   # 150 lines, runs the 5 SOP commands
  tests/phase2_acceptance.py                   # 130 lines, 5 e2e tests
  docs/Hermes_M2_Phase2_Strengthening_Completion_Report.md  # this file
  config/reaper.json                           # generated by ensure_defaults()
  config/notify.json                           # generated by ensure_defaults()
  config/safety.json                           # generated by ensure_defaults()
  config/pricing.json                          # generated by ensure_defaults()
```

---

## Known limitations / Phase 3 candidates

1. **Notifier needs env vars** — `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` not yet set. The notifier logs to `logs/notifier.log` so you can see what it would have sent.
2. **`failed` events not yet wired to notifier** — only `timeout` is auto-notified. Add `notify_failed()` call in `manager.fail()` for Phase 3.
3. **Model name backfill** — older tasks have `model_name=NULL` because the watcher didn't auto-extract `raw.model` until this phase. New tasks will populate correctly.
4. **Reaper sweep is best-effort** — runs every 10s (5 ticks × 2s). A task that was created 5min 5s ago won't be reaped for up to 10s. Acceptable for our use cases.
5. **Safety policy is local** — allowlist is per-bridge, not synced across the org. Phase 3 candidate: read allowlist from a central config endpoint.

---

## Re-running this report's commands

```bash
cd ~/hermes-runtime-bridge
.venv/bin/python tests/test_dispatcher.py        # 21 tests
.venv/bin/python tests/test_phase2.py            # 19 tests
.venv/bin/python tests/phase1_acceptance.py      # writes docs/phase1_acceptance_<TS>.md
export PHASE2_TEST_KEY=$(grep '^GPT_BRIDGE_API_KEY=' .env | cut -d= -f2-)
.venv/bin/python tests/phase2_acceptance.py      # 5 e2e tests
```

---

*Last verified: 2026-07-07 09:25 UTC*  
*Owner: M2 (Hermes assistant)*  
*Signed-off: pending 鼎鼎 review*
