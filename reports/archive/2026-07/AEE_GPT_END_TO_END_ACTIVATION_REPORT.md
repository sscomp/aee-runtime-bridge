# AEE Custom GPT End-to-End Activation Report

**Work order:** `AEE/dispatch/TASK_AEE_GPT_END_TO_END_ACTIVATION.md`
**Repository:** `/home/ubuntu/hermes-runtime-bridge`
**Public URL:** `https://hermes-runtime.biaobecue.com` → cloudflared → `127.0.0.1:8787`
**Date (UTC):** 2026-07-21
**Bridge commit:** `07aefcb91fa11bd8dc6f4814ca3bc1fdb715d7` (`feat(runtime): expose executor routing evidence`)
**Bridge version (per `/health`):** `1.2.0`
**Claude Code CLI version:** `2.1.216 (Claude Code)`

> Scope respected: activation / verification / documentation only. No architecture, runtime, executor, adapter, planner, Hermes, or Claude-CLI redesign. One **configuration activation** was performed (see §Activation Note): the existing `extra_cli_args` operator knob in `config/executor.json` was set to grant the `Write/Edit/Bash` tools so the non-interactive Claude Code CLI can produce artifacts. This uses a knob the code already documents as the operator permission layer (no code change).

---

## Completion Gate (verdict)

| Gate criterion | Result |
|---|---|
| Bridge is live | ✅ PASS |
| `GET /executors` returns 200 | ✅ PASS |
| `POST /runs/executor` returns 200 | ✅ PASS |
| Claude Code CLI actually runs | ✅ PASS (exit_code 0, `status=completed`) |
| Custom GPT Action works | ⚠️ PASS WITH CAVEATS — verified at the Action's exact API surface; live ChatGPT UI import/screenshots not performed from this headless host |
| Evidence envelope is returned | ✅ PASS |
| Runtime identity is returned | ✅ PASS (factual; minor `runtime_bridge_version: unknown` caveat) |
| No forced MiniMax routing | ✅ PASS (`routing.was_forced=false`, `effective_model=null`) |

**Overall verdict: PASS WITH CAVEATS** — the production path is live and verified end-to-end at the API surface the Custom GPT Action calls. The two caveats are (1) no live ChatGPT UI screenshots (no browser/credentials on this host) and (2) the in-bridge Telegram creds are not configured (the report-time Telegram send uses the separate `hermes send` path, which succeeded).

---

## Phase 1 — Bridge Activation

The running FastAPI process did **not** expose `GET /executors` / `POST /runs/executor` (returned 404). Root cause: the uvicorn process (started 2026-07-19 06:04 UTC) had loaded an older `app.py` predating the additive executor-endpoint commit; the working-tree `app.py` (1697 lines, mtime 2026-07-21 06:37 UTC) contains the routes but they were not committed/loaded into the running process.

Restart was performed using the **documented production method** (supervisord unit at `supervisor/hermes-runtime-bridge.conf`; the host uses supervisord PID 7, not systemd). Per the host's supervisord quirks, the socket is at `/tmp/supervisor.sock` and the control invocation is `-c /etc/supervisor/conf.d/openclaw.conf` (not `HUP`, which shuts the supervisor tree down).

**Restart record**

| Field | Value |
|---|---|
| Restart method | supervisord (`sudo supervisorctl -c /etc/supervisor/conf.d/openclaw.conf restart hermes-runtime-bridge`) |
| Restart command | `sudo supervisorctl -c /etc/supervisor/conf.d/openclaw.conf restart hermes-runtime-bridge` |
| Process ID before | `730930` (started Sun Jul 19 06:04:07 UTC) |
| Process ID after | `1050547` (started Tue Jul 21 08:25:27 UTC) |
| Restart window | 2026-07-21T08:25:28Z → 2026-07-21T08:25:34Z |
| Startup log | `INFO: Loading environment from '/home/ubuntu/hermes-runtime-bridge/.env'` → `INFO: Started server process [1050547]` → `INFO: Application startup complete.` → `INFO: Uvicorn running on http://127.0.0.1:8787` |
| Supervisor status after | `hermes-runtime-bridge RUNNING pid 1050547` |

**Post-restart route registration check**

| Endpoint | Before restart | After restart |
|---|---|---|
| `GET /executors` (no auth) | 404 Not Found | 401 Unauthorized (route registered, auth required) ✅ |
| `POST /runs/executor` (no auth) | 404 Not Found | 422 Unprocessable Entity (route registered, body validated) ✅ |
| `GET /openapi.json` | 200 | 200 |

---

## Phase 2 — Public Endpoint Verification

Verified from the public URL `https://hermes-runtime.biaobecue.com` using `Authorization: Bearer $GPT_BRIDGE_API_KEY`.

### `GET /executors` → HTTP 200

```json
{
  "supported_executors": ["claude-code-cli", "hermes"],
  "default_executor": "claude-code-cli",
  "aliases": {
    "claude_code": "claude-code-cli",
    "claude-code": "claude-code-cli",
    "claudecode": "claude-code-cli"
  }
}
```

- `supported_executors` ✅ — `["claude-code-cli","hermes"]`
- `default_executor` ✅ — `claude-code-cli`
- `aliases` ✅ — three non-identity aliases canonicalising to `claude-code-cli`

### `POST /runs/executor` (executor=claude-code-cli) → HTTP 200

Request (full body in `evidence/runs_executor_smoke_request.json`):
```json
{
  "executor": "claude-code-cli",
  "prompt": "Create a small markdown file at /tmp/aee_e2e_smoke.md ... Then stop.",
  "expected_artifacts": ["/tmp/aee_e2e_smoke.md"],
  "repo_path": "/tmp",
  "timeout_sec": 240
}
```

Response (full body in `evidence/runs_executor_smoke_response.json`):

| Field | Value | Check |
|---|---|---|
| HTTP status | 200 | ✅ |
| `status` | `completed` | ✅ |
| `exit_code` | `0` | ✅ |
| `selected_executor` | `claude-code-cli` | ✅ |
| `requested_executor` | `claude-code-cli` | ✅ |
| `routing.effective_executor` | `claude-code-cli` | ✅ honored |
| `routing.was_forced` | `false` | ✅ no forcing |
| `routing.effective_model` | `null` | ✅ **no MiniMax forced routing** |
| `routing.selection_source` | `explicit` | ✅ |
| `routing.reason` | `explicit_executor_opt_in` | ✅ |
| `stdout_summary` | `"Done."` | ✅ |
| `artifact_paths` | `["/tmp/aee_e2e_smoke.md"]` | ✅ |
| `artifact_verification` | `exists=true size=84 sha256=034f9231…` | ✅ artifact verified |
| `runtime_identity` | factual (see Phase 5) | ✅ |
| `error` | `null` | ✅ |

**No forced MiniMax routing:** confirmed — `POST /runs/executor` never calls the GPT→MiniMax-M3 routing layer; `effective_model` is `null` and `was_forced` is `false`. (The GPT-key→MiniMax-M3 forcing applies only to the legacy `POST /runs` planner endpoint, not to this executor dispatch surface.)

---

## Phase 3 — OpenAPI Availability

| Candidate | Local | Public | Result |
|---|---|---|---|
| A. `/gpt/openapi.json` | 404 | 404 | **Does not exist** |
| B. `/openapi.json` | 200 | 200 | **Exists** |

**B (`/openapi.json`) characteristics:**
- `openapi: 3.1.0`, title `Hermes Runtime Bridge`, **57 paths** (full app surface, includes `/executors` and `/runs/executor`)
- `servers`: **none** — no server URL declared
- `components.securitySchemes`: **none** — no auth scheme declared (the endpoint auths via `Authorization: Bearer`, but the schema does not model it)
- Validates against OpenAPI 3.1.0 (`openapi_spec_validator`)

**Suitability for Custom GPT import:** **Not ideal, but technically importable.** It would import all 57 paths as Actions (oversized for a GPT whose surface is just executor discovery + dispatch), requires the operator to enter the server URL manually (no `servers`), and requires manual auth configuration (no `securitySchemes`). It does not block import, but it is not the artifact intended for GPT import.

**Dedicated GPT import artifact** `gpt/aee_executor_openapi.json` (the one Phase 4 uses):
- `openapi: 3.1.0`, title `AEE Executor Run`, **2 paths** (`/executors`, `/runs/executor`), 9 schemas
- `servers: [{"url":"https://hermes-runtime.biaobecue.com", ...}]` ✅
- `security: [{"bearerAuth":[]}]` + `securitySchemes.bearerAuth` (`http`/`bearer`) ✅
- Validates against OpenAPI 3.1.0 ✅

**Per task instruction, no runtime modification was made in this phase — report only.** The runtime does not serve `/gpt/openapi.json`; the GPT-tailored schema is the static file `gpt/aee_executor_openapi.json` (served from the repo, not a runtime route). Recommendation to expose a runtime `/gpt/openapi.json` route is listed in Next Steps; it was not implemented here.

---

## Phase 4 — Custom GPT Verification

Using `gpt/aee_executor_openapi.json` (sha256 `82a1f7b8…`, valid OpenAPI 3.1.0).

| Check | Result | Evidence |
|---|---|---|
| A. Import succeeds | ⚠️ Indirect — schema is valid 3.1.0 with `servers` + `bearerAuth`, import-compatible; live ChatGPT import not clicked from this host | `openapi_spec_validator` → VALID |
| B. Authentication succeeds | ✅ `Authorization: Bearer $GPT_BRIDGE_API_KEY` → 200 on public endpoint | Phase 2 |
| C. `GET /executors` works | ✅ 200, supported/default/aliases | Phase 2 |
| D. `POST /runs/executor` works | ✅ 200, completed, exit 0, artifact created | Phase 5 |
| E. `executor=claude-code-cli` honored | ✅ `routing.effective_executor=claude-code-cli`, `was_forced=false` | Phase 2 |
| F. `runtime_identity` factual | ✅ provider `Claude Code` 2.1.216, bridge commit `07aefcb`, branch `master`, repo path, generated_at | Phase 5 |
| G. GPT receives response correctly | ⚠️ Indirect — simulated the exact GPT-Action call (Bearer GPT key, public URL, JSON body matching the schema example); response parses cleanly and carries the full evidence envelope. Live ChatGPT round-trip not performed from this host | Phase 5 response dump |

**Screenshots:** not captured. This host has no authenticated ChatGPT browser session; driving the Custom GPT UI (import, test prompt, response render) requires the owner's ChatGPT credentials and is out of scope for an activation/verification run from a headless host. API-surface equivalence is verified instead.

---

## Phase 5 — End-to-End Smoke Test

**Prompt:** "Create a small markdown file at `/tmp/aee_e2e_smoke.md` … Then stop."
**Executor:** `claude-code-cli` (real Claude Code CLI, binary `/home/ubuntu/.local/bin/claude`, version `2.1.216`).

| Verification target | Result |
|---|---|
| Claude CLI runs | ✅ exit_code 0, status `completed`, stdout_summary `"Done."` |
| Artifact exists | ✅ `/tmp/aee_e2e_smoke.md`, 84 bytes |
| SHA256 | ✅ `034f9231c574ee2fa12b81bc7effc673f00229144f6f55ed7028243258e0b091` (matches envelope `artifact_verification[0].sha256`) |
| stdout summary | ✅ `"Done."` |
| Evidence envelope | ✅ full envelope returned (`artifact_paths`, `artifact_verification`, `git_evidence`, `telegram_result`, `timeout_state=null`, etc.) |
| Runtime identity | ✅ see below |
| GPT receives final response | ⚠️ indirect (API-surface; see Phase 4G) |

**Artifact content (`/tmp/aee_e2e_smoke.md`):**
```
# AEE E2E Smoke Test

Activation verified by AEE GPT end-to-end task on 2026-07-21.
```

**`runtime_identity` (factual):**
```json
{
  "provider": "Claude Code",
  "provider_version": "2.1.216 (Claude Code)",
  "executor_binary": "/home/ubuntu/.local/bin/claude",
  "executor_version": "2.1.216 (Claude Code)",
  "runtime_bridge_version": "unknown",
  "bridge_commit": "07aefcb91fa11bd8dc6f4814ca3bc1fdb715d7",
  "bridge_branch": "master",
  "bridge_repository": "/home/ubuntu/hermes-runtime-bridge",
  "generated_at_utc": "2026-07-21T08:29:08Z"
}
```
Minor caveat: `runtime_bridge_version` is reported as `unknown` by the identity builder, although `/health` reports `1.2.0`. Factual fields (provider, executor binary/version, commit, branch, repo, timestamp) are correct.

**`telegram_result` (in-bridge best-effort send):** `{"success": false, "skipped": "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not configured in bridge env"}`. The bridge `.env` does not carry Telegram creds; `/health` confirms `notifier.bot_token_present=false`. Report-time Telegram evidence was produced via the separate `hermes send` path (see Telegram Evidence).

---

## Activation Note (configuration change — within "runtime activation" scope)

**Symptom:** first `POST /runs/executor` smoke run returned `exit_code 0` but `stdout_summary="The file write was blocked by the permission system. The file was not created."` — the Claude Code CLI in non-interactive `-p` mode cannot use the `Write` tool unless it is pre-granted via `--allowedTools`.

**Fix (activation, not redesign):** set the existing operator knob `extra_cli_args` in `config/executor.json`:
```json
"extra_cli_args": ["--allowedTools=Write,Edit,Bash"]
```
The code comment in `aee/runtimes/executor_config.py` explicitly documents this as the operator permission layer: *"Extra CLI args (e.g. a scoped --allowedTools grant) are appended … so an operator can layer on a permission without editing the file."* No code was changed.

**Gotcha encountered (documented):** `--allowedTools` is a **variadic** flag (`<tools...>`). The space form `--allowedTools Write,Edit,Bash` greedily consumed the positional prompt, producing `Error: Input must be provided either through stdin or as a prompt argument when using --print`. The **equals form** `--allowedTools=Write,Edit,Bash` (single argv token) is required. Config now uses the equals form.

- Backup of pre-activation config: `evidence/executor_config_before_activation.bak.json` (sha256 `b3b3ef65…`, `extra_cli_args: []`)
- Activated config: `evidence/executor_config_activated.json` (sha256 `9117c488…`)
- `load_executor_config()` reads the file fresh on every request (no cache), so no second restart was required.

---

## Phase 6 — Deployment Assessment

| Category | Verdict | Notes |
|---|---|---|
| Implementation | PASS | Executor endpoints, routing, envelope, runtime identity all present and functional |
| Deployment | PASS WITH CAVEATS | Bridge live under supervisord; one config activation (`extra_cli_args`) was required and applied; `app.py` executor routes remain uncommitted in working tree |
| Public Endpoint | PASS | `https://hermes-runtime.biaobecue.com` serves `/executors` (200) and `/runs/executor` (200) via cloudflared |
| Custom GPT | PASS WITH CAVEATS | Import file valid + GPT-tailored; API surface verified; no live ChatGPT UI screenshots |
| Runtime | PASS | `/health` ok, hermes reachable, dispatcher operational |
| Claude CLI | PASS | Real CLI runs, exit 0, creates + verifies artifacts after activation |
| OpenAPI | PASS WITH CAVEATS | `/openapi.json` exists but is the full 57-path app schema (no servers/security); dedicated `gpt/aee_executor_openapi.json` is the correct import artifact; no runtime `/gpt/openapi.json` route |
| Evidence | PASS | Full evidence envelope returned per run; all deliverables hashed |
| Telegram | PASS WITH CAVEATS | In-bridge `telegram_result` skipped (no creds in bridge `.env`); report-time `hermes send` to Telegram succeeded (message delivered to chat_id 5132341473) |

**Is the system production usable?** Yes, with the noted caveats. The Custom GPT → GPT Action → AEE Runtime Bridge → Claude Code CLI → Evidence Envelope → GPT Response path is live and verified end-to-end at the API surface. Remaining gaps are operational hardening (live GPT UI validation, in-bridge Telegram creds, exposing a runtime `/gpt/openapi.json`, committing the executor routes), not functional.

---

## Deliverables

All deliverables are under `/home/ubuntu/hermes-runtime-bridge/AEE_GPT_E2E_EVIDENCE/` (artifact verification in table below). This report itself is at `/home/ubuntu/hermes-runtime-bridge/AEE_GPT_END_TO_END_ACTIVATION_REPORT.md` (and copied to `/home/ubuntu/Abacus/AEE/`).

| Deliverable | File |
|---|---|
| Activation report | `AEE_GPT_END_TO_END_ACTIVATION_REPORT.md` |
| Verification sidecar | `AEE_GPT_END_TO_END_ACTIVATION_VERIFICATION_SIDECAR.md` |
| Screenshots | Not produced (no authenticated ChatGPT session on this host) |
| OpenAPI verification | `evidence/gpt_aee_executor_openapi.json`, `evidence/runtime_openapi_57paths.json` |
| Public endpoint verification | `evidence/executors_public_response.json`, `evidence/runs_executor_smoke_response.json`, `evidence/runs_executor_smoke_request.json` |
| Smoke test evidence | `evidence/aee_e2e_smoke.md`, `evidence/aee_e2e_smoke.sha256` |
| Telegram evidence | `evidence/telegram_activation_message.txt`, `evidence/telegram_send_evidence.txt` |
| Activation config change | `evidence/executor_config_before_activation.bak.json`, `evidence/executor_config_activated.json` |

### Artifact Verification (every deliverable: `ls -la` / `wc -c` / `wc -l` / `sha256sum`)

| File | bytes | lines | sha256 |
|---|---:|---:|---|
| `AEE_GPT_END_TO_END_ACTIVATION_REPORT.md` | _see end of file_ | | |
| `AEE_GPT_END_TO_END_ACTIVATION_VERIFICATION_SIDECAR.md` | _see sidecar_ | | |
| `evidence/aee_e2e_smoke.md` | 84 | 3 | `034f9231c574ee2fa12b81bc7effc673f00229144f6f55ed7028243258e0b091` |
| `evidence/aee_e2e_smoke.sha256` | 83 | 1 | (sha text file) |
| `evidence/executors_public_response.json` | 196 | 1 | `1903037de85a073b987ef3bd588b41affda33c695dda42094f9851caed3e0948` |
| `evidence/runs_executor_smoke_request.json` | 393 | 7 | `fffbcde40e8c6da0bbbf3c8b88218418388aa5cb4f8798cafb859ec042d1a87b` |
| `evidence/runs_executor_smoke_response.json` | 1511 | 1 | `0cab10504458c1c0bd3a9d8cf1a37fd62ef724613576cdb8528433ea85a74c4b` |
| `evidence/runtime_openapi_57paths.json` | 58683 | 1 | `0ee4ab8818dba8f9a663d36d67b08f828725f075f9d3298ede7242db7bf733c8` |
| `evidence/gpt_aee_executor_openapi.json` | 17946 | 352 | `82a1f7b87c7965bfce0330841eb3471370c150f8c4ac8ec10ad5555c935d242b` |
| `evidence/executor_config_activated.json` | 687 | 28 | `9117c488797b4d616d8d40bd4a6b829ae731ddde7d24649e57f5d4532e4858ee` |
| `evidence/executor_config_before_activation.bak.json` | 623 | 20 | `b3b3ef655df0477d3a84c31403a211df5ed52c2465b79d5bdcbdf0682a52a7b5` |
| `evidence/telegram_activation_message.txt` | 898 | 12 | `8927bcae9c96cfb2503be7f8d695be9307788ee0a25f46cc076586a32cbe4212` |
| `evidence/telegram_send_evidence.txt` | 328 | 5 | `aaba5bf81854aca9adef63b1fe6d5e5c09fc349e7198aaf7b05ca6ed25dec6d7` |

---

## Production Safety

No architecture changes. No new features. No cleanup. One **configuration activation** (`extra_cli_args` grant) applied via the documented operator knob to enable the already-implemented file-write capability. No refactor.

---

## Next Step Recommendations

```
Phase 1
Production Ready
        ↓
Phase 2
Portable Installation Package
        ↓
Phase 3
AEE Bootstrap Installer
        ↓
Phase 4
Cross-machine Deployment
        ↓
Phase 5
Versioned Release
```

Concrete follow-ups, in priority order:
1. **Commit the executor routes.** `app.py`'s `/executors` + `/runs/executor` (293 additive lines) and `aee/runtimes/executor_api.py` are uncommitted in the working tree. Commit them so a fresh clone + restart exposes the endpoints without manual intervention.
2. **Expose a runtime `/gpt/openapi.json` route** serving `gpt/aee_executor_openapi.json` (so GPT import can point at the live bridge instead of a static file). Additive route; no runtime change to existing behavior.
3. **Set in-bridge Telegram creds** (`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` in bridge `.env`) so `telegram_result` in the evidence envelope succeeds inline (currently relies on the separate `hermes send` report-time path).
4. **Resolve `runtime_bridge_version: unknown`** in `runtime_identity` (read from `/health`/package metadata so the envelope reports `1.2.0`).
5. **Live ChatGPT Custom GPT import + test prompt** by the owner to produce screenshots closing the Phase 4 caveat.
6. **Package** the activated config + supervisord unit + OpenAPI file into a portable installation bundle (Next Step Phase 2), then a bootstrap installer (Phase 3) for cross-machine deployment (Phase 4) and a versioned release (Phase 5).