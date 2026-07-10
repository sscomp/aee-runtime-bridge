# Hermes Runtime Bridge — Phase 2 / Phase 3 Roadmap

Author: M2 (Hermes assistant) · Date: 2026-07-07
For: 鼎鼎 (Scott Chang)
Status: Phase 1 ships 12 enhancements. This document is the design
hand-off for what's next.

## Recap: what Phase 1 delivered

- Persistent `TASK-YYYYMMDD-NNNN` for every dispatched task
- 7 new endpoints: `/tasks`, `/tasks/{id}`, `/tasks/{id}/{progress,logs,result,rerun,cancel}`
- 8-state machine with audit log
- Per-task log file under `logs/TASK-*.log`
- Per-task result file under `reports/TASK-*/task.json`
- 9-section report template under `research/report.py`
- Prompts/Config/Repo scanning modules
- CLI: `hermes-task list/show/logs/rerun`
- 21/21 unit tests pass; live end-to-end test against Hermes 8642 passed
- Bridge v1.2.0 live on `https://hermes-runtime.biaobecue.com` via supervisord

The two pain points SOP §1 listed are now resolved:

1. ✅ **"GPT session 結束後任務變孤兒"** — every dispatched task has a
   stable `TASK-YYYYMMDD-NNNN` id; the id is queryable from any chat,
   the CLI, or the bridge URL indefinitely.
2. ✅ **"結果無法回去 Hermes 端"** — `reports/TASK-*/task.json` is
   written on every completion; the bridge's `/tasks/{id}/result`
   endpoint surfaces the full output to the next ChatGPT session.

---

## Phase 2 — make tasks composable and observable

**Goal**: One task should be able to depend on another; the system
should tell us *why* a task is slow, and ChatGPT should be able to ask
"what's running right now" without polling.

### 2.1 Task DAG (parent/child + dependency graph)

Today `parent_task_id` is a column; nothing reads it. Wire it up.

- New column: `depends_on TEXT` (JSON array of `TASK-...` ids)
- New endpoint: `POST /tasks/{id}/wait?timeout=600` (poll until dep done)
- New endpoint: `POST /tasks` (create a child task that auto-runs after a parent)
- New column: `pipeline_id TEXT` for grouping related tasks

Why: the macro morning brief today runs as 3 separate cron jobs that
each call GPT independently. They should be a single `PIPELINE-2026-07-07-001`
with 3 child tasks (macro / industry / company), each can be queried,
retried, cancelled independently, but the pipeline as a whole has a
"current step" view.

### 2.2 Push notifications (Telegram / LINE)

Today the user discovers a task finished by polling. With a Telegram
bot token + chat_id env var, the bridge can push:
- "TASK-20260707-0016 completed: 4 (5.8s)"
- "TASK-... failed: upstream 504 after 60s"

Why: matches the 通知偏好 already confirmed on 2026-06-06 — silent
when healthy, notify only on event. Bridges to N2 so a 鼎鼎
notification can come through the same channel as the cron jobs.

### 2.3 Cost / token tracking

The bridge already grabs `usage` from Hermes; we just don't aggregate.
Add:
- `tasks.tokens_in`, `tokens_out`, `cost_usd` columns (auto-computed via
  a tiny `config/pricing.json`)
- New endpoint: `GET /stats?period=7d&group_by=type`
- CLI: `hermes-task stats --period=7d`

Why: 鼎鼎 cares about the 0050 / 2330 deep research job's cost. The
bridge has the raw signal; we just need the rollup view.

### 2.4 Stale task reaper

If Hermes 8642 crashes mid-run, the dispatcher is stuck in
`running` forever. Add:
- Background task: if `running > 30 min` and no `progress` event in
  the last 5 min, mark `timeout` + write a "watcher gave up" event.
- CLI: `hermes-task list --stale` shows them.

### 2.5 Subagent delegation: research agent

Add a new `mode: research-deep` that:
1. Reads the input → decides whether it's macro/industry/company
2. Picks the matching `prompts/<name>_v<n>.md`
3. Calls scheduler.snapshot() to gather host context
4. Calls repo.scan() if a repo path is given
5. Renders 9-section report to `reports/TASK-.../report.md`
6. Sends back to ChatGPT as a single chat response

This converts the bridge from "thin proxy" into "M2-style analyst"
that can produce a real deliverable, not just a stdout echo.

---

## Phase 3 — multi-host & production-grade

**Goal**: scale beyond a single container, support multiple
orchestrators, and survive the Abacus container reset.

### 3.1 SQLite → Postgres (or stay on SQLite?)

SQLite is fine for ~1M tasks/year on a single host. Stay on SQLite for
Phase 3, **but**:
- Add a `dispatcher/db_migrate.py` script that exports to JSON-lines
- Document the WAL backup procedure
- Add `hermes-task import` / `export` for migration

If we ever need multi-host: switch to Postgres, keep the same
manager.py surface, add `db_dialect = "postgres"` flag.

### 3.2 Multi-bridge federation

The bridge currently assumes 1:1 with Hermes 8642. Phase 3 supports
many bridges (dingde, beibei, work) sharing a Hermes cluster.

- `BRIDGE_ROLE` env var (default "dingde")
- A `bridges` table in the DB; each task records which bridge created it
- `GET /tasks?bridge=dingde` for per-orchestrator views

### 3.3 Container reset survival

The Abacus container resets roughly monthly. The bridge has to come
back fully working.

- Move the `data/`, `logs/`, `reports/`, `config/` directories to a
  persistent volume mount (if available) **or** rsync to S3 on every
  `complete`.
- Add `bin/restore-bridge.sh` script: re-creates venv, re-runs
  `python -m dispatcher.db --init`, re-attaches to supervisord.

### 3.4 WebSocket subscription for real-time progress

For long-running tasks the polling loop wastes ChatGPT context. Add:
- `WS /tasks/{id}/stream` (server-sent events actually) emits
  `progress` / `event` as they happen
- ChatGPT Custom GPT Actions don't support WS, but a future
  ChatGPT-native client (or N2 on a phone) can.

### 3.5 Audit + RBAC

For 鼎鼎's other family members (彪彪, 帽喜) using the same bridge:
- `BRIDGE_API_KEY_<user>` env vars with per-user rate limits
- `/tasks` filter by `owner`
- All actions append to `dispatcher/audit.log` with user, action,
  task_id, ts, ip, user_agent

---

## Risks & open questions

1. **Watcher is single-instance.** If supervisord restarts bridge, the
   in-memory map of "in-flight tasks being watched" is lost. A
   recovery scan on startup would fix this: `Watcher.start()` should
   query `status IN ('queued','running')` and re-arm.
2. **The blocklist is regex, not a parse tree.** A motivated attacker
   can phrase `rm -rf /` as `$(echo 'rm' '-' 'rf' '/')` and pass. We
   should accept this as a known limit; the real defence is Hermes
   itself, not the bridge.
3. **Hermes run_id format is undocumented.** Today we treat it as
   opaque. If the format changes (e.g. `hr_*` instead of `run_*`)
   some queries break. We have no upstream contract to point at.

---

## Recommended next actions (this week)

- [ ] 2.4 Stale task reaper — small, high value, ~1 day
- [ ] 2.2 Telegram push for `failed` / `timeout` — small, high value
- [ ] 2.5 Subagent delegation: research agent — medium, biggest UX win
- [ ] 3.3 Container reset survival — medium, needed before next reset

Skip for now: 2.1 DAG (defer until we have 1 real multi-step pipeline
to validate against), 3.1 Postgres (premature).
