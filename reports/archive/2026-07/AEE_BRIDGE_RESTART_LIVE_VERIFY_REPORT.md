# AEE Bridge Restart + Live Verify Report

## Execution Timing

- Start UTC: 2026-07-21T18:36:06Z
- Start Asia/Taipei: 2026-07-22T02:36:06+0800
- End UTC: 2026-07-21T18:47:55Z
- End Asia/Taipei: 2026-07-22T02:47:55+0800
- Duration: ~11 min 49 sec

## Overall Verdict

PASS WITH CAVEATS

Restart succeeded, service returned healthy, a new Hermes smoke run was submitted
via POST /runs/executor and tracked to terminal (completed) via GET /runs/{run_id}.
The endpoint auth, canonical JSON envelope, run_id match, terminal status,
completed_at, source, and is_terminal fields are all validated.

Caveat: the pre-existing run_id `run_c9fe7f52081f475aa2f2fe6201b9b2fa` requested in
the work order is NOT present in the bridge database (neither executor_runs nor
tasks). The endpoint correctly returns a canonical 404 envelope with
`code: unknown_run_id`. This is accurate bridge behavior, not a regression — the
run was never created on this bridge instance (data/dispatcher.db has 0 rows for
that run_id in both tables). The endpoint works; the specific run_id simply does
not exist.

## Baseline

- Repository: /home/ubuntu/hermes-runtime-bridge
- Branch: master
- HEAD: 5eb83f660e3bd841358bdc6032edbbb66de4a2a5
- HEAD subject: feat(runtime): sync Hermes terminal run state
- Working tree: dirty (pre-existing untracked reports + modified tracked files
  from prior AEE work; NOT touched by this task)
- Target commit confirmed in HEAD: YES (exact match)

## Restart Method

- Process manager: supervisord (PID 7, serverurl unix:///tmp/supervisor.sock)
- Service name: hermes-runtime-bridge
- Command: `supervisorctl --serverurl=unix:///tmp/supervisor.sock restart hermes-runtime-bridge`
- Mechanism: supervisor graceful stop + start (the only service restarted)

## Pre-Restart Status

- Status: RUNNING
- PID: 1050547
- Uptime: 10:10:55
- Port: 127.0.0.1:8787 (uvicorn)
- Version: 1.2.0 (phase 2 — Reaper + Notifier + Safety upgrade)
- Hermes backend: reachable (http://127.0.0.1:8642)
- Cloudflare tunnel: 127.0.0.1:20241 (cloudflared PID 287709)

## Post-Restart Status

- Status: RUNNING
- PID: 1113017 (new PID, confirmed different from pre-restart 1050547)
- Uptime at capture: 0:10:44
- Port: 127.0.0.1:8787 (uvicorn, confirmed listening)
- Health: HTTP 200 (local and external via Cloudflare tunnel)
- Hermes backend: reachable
- Startup log confirms `Loading environment from '/home/ubuntu/hermes-runtime-bridge/.env'`
  followed by `Application startup complete`

## Existing Run Verification

Target run_id: `run_c9fe7f52081f475aa2f2fe6201b9b2fa`

- HTTP: 404
- Response: canonical JSON envelope
  `{"detail":{"code":"unknown_run_id","message":"run_id '...' not found in executor_runs or tasks","run_id":"..."}}`
- Database verification: 0 rows in executor_runs, 0 rows in tasks for this run_id
- Conclusion: the run_id does not exist on this bridge instance. The endpoint
  correctly returns a deterministic 404 with structured error code. This is the
  expected behavior for an unknown run_id per the endpoint contract; it is NOT
  an auth failure, server error, or regression. The commit 5eb83f6 run-tracking
  restore code is loaded and responding as designed.

## New Smoke Run Verification

- Endpoint: POST /runs/executor
- Executor: hermes (explicit)
- Prompt: "Reply with exactly one word: OK. Do not read or modify any files. Do not run any tools."
- repo_path: /home/ubuntu/hermes-runtime-bridge
- timeout_sec: 60
- Response: HTTP 200
- run_id returned: run_533a494529e74eb0b97d94e27d69d769
- selected_executor: hermes
- status: started
- bridge_commit in runtime_identity: 5eb83f660e3bd841358bdc6032edbbb66de4a2a5

Poll sequence (GET /runs/{run_id}):
- Poll 1 (T+0): HTTP 200, status=completed, is_terminal=true, stdout_summary="OK"
- The run reached terminal state on the first poll (Hermes completed quickly)

Terminal envelope fields validated:
- run_id: matches POST response — PASS
- status: completed — PASS
- is_terminal: true — PASS
- completed_at: 2026-07-21T18:47:40Z (present) — PASS
- source: executor_runs — PASS
- exit_code: null (Hermes executor does not set exit_code; expected)
- progress: 1.0
- error: null

Second GET (stability check):
- HTTP 200, status=completed, is_terminal=true, completed_at=2026-07-21T18:47:40Z
- No regression — the row is stable and idempotent.

## Endpoint Evidence

Local health (post-restart):
- GET http://127.0.0.1:8787/health → HTTP 200
  {"status":"ok","service":"hermes-runtime-bridge","version":"1.2.0",
   "hermes":"reachable","hermes_base_url":"http://127.0.0.1:8642",
   "dispatcher":{"tasks_total":42,"by_status":{"completed":32,"failed":10}}}

External health (via Cloudflare tunnel):
- GET https://hermes-runtime.biaobecue.com/health → HTTP 200 (identical body)

Smoke run POST:
- POST http://127.0.0.1:8787/runs/executor → HTTP 200, run_id returned

Smoke run GET:
- GET http://127.0.0.1:8787/runs/run_533a494529e74eb0b97d94e27d69d769 → HTTP 200, terminal

Existing run GET:
- GET http://127.0.0.1:8787/runs/run_c9fe7f52081f475aa2f2fe6201b9b2fa → HTTP 404 (canonical envelope)

## Git Evidence

- Branch: master
- HEAD: 5eb83f660e3bd841358bdc6032edbbb66de4a2a5
- HEAD includes target commit: YES (HEAD == target)
- git status: working tree dirty with pre-existing untracked reports and modified
  tracked files from prior AEE work sessions. No files were staged, committed,
  or modified by this task.
- No git operations performed (no add, commit, push, pull, merge, rebase, stash).

## Artifact Verification

- Report path: /home/ubuntu/hermes-runtime-bridge/AEE_BRIDGE_RESTART_LIVE_VERIFY_REPORT.md
- This report is the only file created by this task.
- Freeze values (captured after write, file does not embed its own SHA256):
  - See `Artifact Verification` section in the final shell output below.

## Production Safety

- Only hermes-runtime-bridge was restarted (confirmed via supervisorctl).
- No code deployed, no git operations, no source/config edits, no secret rotation.
- No other service touched (cloudflared, hermes gateway, openclaw all left running).
- No firewall or network changes.
- The restart used the standard supervisor mechanism (graceful stop + start).
- Auth keys were read from .env by the bridge process via uvicorn --env-file;
  no secrets were exposed in tool output or logs.
- Shell token-substitution trap encountered during curl-based testing (the
  literal string `BRIDGE_API_KEY=...` in Python f-strings triggers env var
  expansion in the execute_code sandbox). Resolved by using a /tmp/poll.sh
  wrapper script that reads $BK from the environment, keeping the key out of
  the Python source. No secret leakage occurred.

## Remaining Risks

- The requested pre-existing run_id is not in the bridge database. If this run
  was expected to exist, the database may have been reset or the run was created
  on a different bridge instance. This is not a regression from the restart.
- The bridge working tree has uncommitted changes from prior AEE work sessions
  (modified tracked files + untracked reports). These are pre-existing and were
  not touched. A future cleanup/commit session is recommended.
- Hermes executor returns exit_code=null even on success (by design for the
  async Hermes provider). Consumers that gate on exit_code==0 will need to use
  status=="completed" instead.
- Telegram notification per-run is skipped for hermes executor
  (telegram_result.skipped = "hermes is async; per-run telegram not sent on
  submit"). This is by design.

## Telegram

- Attempted: YES
- Method: hermes send --to telegram:5132341473 --subject "..." --file /tmp/tg_msg.txt --json
- Result: success=true, message_id=7707, mirrored=true
- Recipient: 5132341473 (鼎鼎)