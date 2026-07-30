# AEE GPT End-to-End Activation — Verification Sidecar

Structured verification companion to `AEE_GPT_END_TO_END_ACTIVATION_REPORT.md`.
Generated 2026-07-21 (UTC). All values captured live from the running system.

## Environment

| Item | Value |
|---|---|
| Bridge repo | `/home/ubuntu/hermes-runtime-bridge` |
| Bridge commit | `07aefcb91fa11bd8dc6f4814ca3bc1fdb715d7` |
| Bridge version (`/health`) | `1.2.0` |
| Public URL | `https://hermes-runtime.biaobecue.com` |
| Local origin | `127.0.0.1:8787` |
| Supervisor | supervisord PID 7, conf `/etc/supervisor/conf.d/openclaw.conf`, socket `/tmp/supervisor.sock` |
| Claude CLI | `/home/ubuntu/.local/bin/claude` v`2.1.216` |
| Auth key used | `GPT_BRIDGE_API_KEY` (len 47) |

## Phase 1 — Bridge Activation

| Check | Expected | Actual | Pass |
|---|---|---|---|
| Route `/executors` registered before restart | absent | 404 | n/a (baseline) |
| Route `/executors` registered after restart | registered | 401 (auth required) | ✅ |
| Route `/runs/executor` registered after restart | registered | 422 (body validated) | ✅ |
| Restart method = documented production method | supervisord | supervisord | ✅ |
| PID before | — | `730930` | ✅ recorded |
| PID after | — | `1050547` | ✅ recorded |
| Startup log captured | yes | `Started server process [1050547]` / `Application startup complete.` | ✅ |
| Startup timestamp | ISO UTC | 2026-07-21T08:25:27Z | ✅ |

## Phase 2 — Public Endpoint Verification

### `GET /executors` (public, GPT key)
| Field | Expected | Actual | Pass |
|---|---|---|---|
| HTTP | 200 | 200 | ✅ |
| `supported_executors` | list incl. claude-code-cli | `["claude-code-cli","hermes"]` | ✅ |
| `default_executor` | claude-code-cli | `claude-code-cli` | ✅ |
| `aliases` | non-identity alias map | `{claude_code,claude-code,claudecode → claude-code-cli}` | ✅ |

### `POST /runs/executor` executor=claude-code-cli (public, GPT key)
| Field | Expected | Actual | Pass |
|---|---|---|---|
| HTTP | 200 | 200 | ✅ |
| `status` | completed | `completed` | ✅ |
| `exit_code` | 0 | 0 | ✅ |
| `selected_executor` | claude-code-cli | `claude-code-cli` | ✅ |
| `routing.effective_executor` | claude-code-cli | `claude-code-cli` | ✅ |
| `routing.was_forced` | false | `false` | ✅ |
| `routing.effective_model` | null (no MiniMax) | `null` | ✅ |
| `stdout_summary` | non-empty | `"Done."` | ✅ |
| `artifact_verification[0].exists` | true | `true` | ✅ |
| `artifact_verification[0].sha256` | present | `034f9231…58e0b091` | ✅ |
| No forced MiniMax routing | yes | confirmed | ✅ |

## Phase 3 — OpenAPI Availability

| Candidate | Status | Notes |
|---|---|---|
| A `/gpt/openapi.json` | 404 | does not exist |
| B `/openapi.json` | 200 | 3.1.0, 57 paths, no `servers`, no `securitySchemes`; importable but not GPT-tailored |
| `gpt/aee_executor_openapi.json` | valid 3.1.0 | 2 paths, `servers` set, `bearerAuth` scheme — import-correct |

## Phase 4 — Custom GPT Verification

| Check | Pass | Basis |
|---|---|---|
| A Import succeeds | ⚠️ indirect | valid 3.1.0 + servers + bearerAuth; no live ChatGPT import |
| B Auth succeeds | ✅ | Bearer GPT key → 200 |
| C GET /executors | ✅ | Phase 2 |
| D POST /runs/executor | ✅ | Phase 2/5 |
| E executor honored | ✅ | routing |
| F runtime_identity factual | ✅ | provider/commit/branch/repo/ts |
| G GPT receives response | ⚠️ indirect | simulated Action call; no live GPT round-trip |

## Phase 5 — End-to-End Smoke Test

| Target | Actual | Pass |
|---|---|---|
| Claude CLI runs | exit 0, completed | ✅ |
| Artifact exists | `/tmp/aee_e2e_smoke.md`, 84 B | ✅ |
| SHA256 | `034f9231c574ee2fa12b81bc7effc673f00229144f6f55ed7028243258e0b091` | ✅ |
| stdout summary | `"Done."` | ✅ |
| Evidence envelope | returned (full) | ✅ |
| Runtime identity | returned, factual | ✅ |
| GPT receives response | indirect | ⚠️ |

## Phase 6 — Deployment Assessment

| Category | Verdict |
|---|---|
| Implementation | PASS |
| Deployment | PASS WITH CAVEATS |
| Public Endpoint | PASS |
| Custom GPT | PASS WITH CAVEATS |
| Runtime | PASS |
| Claude CLI | PASS |
| OpenAPI | PASS WITH CAVEATS |
| Evidence | PASS |
| Telegram | PASS WITH CAVEATS |

**Overall: PASS WITH CAVEATS.**

## Activation change applied

- File: `config/executor.json`
- Knob: `extra_cli_args` — `[]` → `["--allowedTools=Write,Edit,Bash"]`
- Rationale: enable Write/Edit/Bash in non-interactive `-p` mode (blocked otherwise). Existing operator knob; no code change.
- Equals form required (`--allowedTools=Write,Edit,Bash`) — variadic flag consumes following positionals in space form.
- Backup: `evidence/executor_config_before_activation.bak.json` (sha256 `b3b3ef65…`).

## Telegram evidence

- Path used: `hermes send --to telegram --subject "[AEE] GPT E2E Activation" --file /tmp/telegram_activation_msg.txt`
- Result: `Sent to telegram home channel (chat_id: 5132341473)`, exit 0
- In-bridge `telegram_result`: skipped (bridge `.env` has no `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`)

## Sidecar self-verification

| File | bytes | lines | sha256 |
|---|---:|---:|---|
| `AEE_GPT_END_TO_END_ACTIVATION_VERIFICATION_SIDECAR.md` | _appended below_ | | |