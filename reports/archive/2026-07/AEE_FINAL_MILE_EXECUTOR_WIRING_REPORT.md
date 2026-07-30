# AEE Final-Mile Claude Code CLI Executor Wiring — Implementation Report

**Work order:** `/home/ubuntu/Abacus/AEE/TASK_AEE_CLAUDE_CODE_EXECUTOR_WIRING.md`
**Repo:** `/home/ubuntu/hermes-runtime-bridge` (branch `master`, HEAD `07aefcb`)
**UTC:** 2026-07-21
**Production safety:** Review-only. No commit, push, deploy, restart, merge, rebase, stash, delete, move, or stage-all performed.

---

## 1. Execution Timing

- **Start (impl file phase, UTC):** 2026-07-21 06:04:23Z (first deliverable `config/executor.json` mtime)
- **End (UTC):** 2026-07-21 06:10Z (report finalisation)
- **Start (CST):** 2026-07-21 14:04
- **End (CST):** 2026-07-21 14:10
- **Duration:** ~6 minutes for the file-writing phase (preceded by read-only gap analysis + feasibility probes).

## 2. Overall Verdict

**PASS**

The real path `GPT → AEE Runtime Bridge → ExecutorAdapter → claude-code-cli` is callable end-to-end. Acceptance D is satisfied with the **real** `/home/ubuntu/.local/bin/claude` binary (v2.1.216): real `exit_code=0`, real `stdout_summary`, real artifact (`/tmp/aee_executor_smoke.md`, size 26, sha256 `9b3a23cb…`). Executor selection is honored — `routing.effective_executor == "claude-code-cli"` and `"MiniMax"` never appears in the routing block. All of A–J pass. See §19 for the Telegram send result and §16 for the one non-blocking caveat (envelope `telegram_result` skipped because the bridge env has no Telegram creds; the §9 report Telegram was sent via `hermes send`).

## 3. Baseline

- **Repository:** `/home/ubuntu/hermes-runtime-bridge`
- **Branch:** `master`
- **HEAD before:** `07aefcb91fa11bd8dc6c8f4814ca3bc1fdb715d7` (`feat(runtime): expose executor routing evidence`)
- **Working tree before impl:** 9 unrelated tracked-modified files (`dispatcher/*`, `aee/observability/*`, `config/notify.json`) + many untracked report files — all pre-existing and **out of scope**; untouched.
- **Claude CLI on host:** `/home/ubuntu/.local/bin/claude` → `2.1.216` (verified: `claude --version`).
- **OpenAPI validator:** `openapi-spec-validator 0.9.0` installed into the venv (tooling only; not added to `requirements.txt`).

## 4. Architecture Before

- The only GPT-callable dispatch surface was `POST /runs` (`app.py:592`, `create_run`). Executor selection lived **inside** `metadata.executor` ∈ {`hermes`, `claude_code`} (validated by `aee/runtimes/executor_router.py`).
- **MiniMax-M3 forcing** ran unconditionally on every `/runs` at `app.py:684–709` (`resolve_model_for_source`) *before* the executor branch. It overrode `effective_model_name` but **not** the executor, so a GPT caller dispatching to Claude Code still reported `routing.model_name=MiniMaxAI/MiniMax-M3, was_forced=True`.
- The `claude_code` adapter (`ClaudeCodeExecutorAdapter`) spawned the **Runner subprocess** `python3 -m scripts.claude_code_runner`, not the bare `claude` CLI.
- The direct-CLI path (`ClaudeCodeProvider`, an `ExecProvider`) existed but was wired only into the orchestrator factory — **not** to any `/runs` endpoint.
- **Missing:** top-level `executor` request field; `claude-code-cli` kebab value; `POST /runs/executor`; `config/executor.json`; the full evidence envelope (`artifact_paths`, `stdout_summary`, `stderr_summary`, `exit_code`, `timeout_state`, `cancel_state`, `git_evidence`, `artifact_verification`, `telegram_result`); all `gpt/` artifacts; the 7 new test files.

## 5. Architecture After

A **new, dedicated, synchronous** `POST /runs/executor` endpoint is added **additively** (`app.py`, +246/0). It does **not** touch `create_run` or the GPT→MiniMax routing layer — it never calls `resolve_model_for_source`, so MiniMax can never be forced here.

Flow: **validate config → select executor → launch → track → verify artifacts/evidence → report.** No second planner/orchestrator.

- `executor=claude-code-cli` (aliases `claude_code`, `claude-code` canonicalise to `claude-code-cli`) → `ClaudeCodeCliRunner` runs the real `claude` CLI to completion (bounded by `timeout_sec`) via the existing `ClaudeCodeProvider`, captures real stdout/stderr/exit_code, verifies `expected_artifacts`, collects read-only `git_evidence`, attempts Telegram, and returns the full envelope with `status` ∈ {completed, failed, timeout, cancelled}.
- `executor=hermes` → delegates to the existing Hermes adapter (`adapter_registry`) and returns a queued envelope (Hermes is async; evidence fields null/skipped). Legacy `metadata.executor` on `/runs` is unchanged.
- Unsupported/empty `executor` → deterministic 400 `unsupported_executor` + `supported_executors` list. No silent fallback.

`--bare` is **OFF by default** (per host constraint: it can break OAuth-hosted sessions); configurable via `config/executor.json` `bare` or `AEE_EXECUTOR_BARE`. The runner is reusable for both modes (tested).

## 6. Files Changed

**New implementation:**
- `config/executor.json` — supported executors, aliases, CLI binary path, default timeout/max_turns/bare/output_format, summary caps, repo allow-list, `extra_cli_args`.
- `aee/runtimes/executor_config.py` — loader (file > defaults > env), `canonical_executor` alias normalisation, `supported_executors`.
- `aee/runtimes/executor_envelope.py` — `truncate_summary`, `verify_artifacts` (exists/size/mtime/sha256), `collect_git_evidence` (read-only).
- `aee/runtimes/executor_cli.py` — `ClaudeCodeCliRunner` wrapping `ClaudeCodeProvider`; `CliRunResult`; submit→poll-to-terminal with timeout/cancel translation.
- `aee/runtimes/executor_api.py` — `ExecutorRunRequest` / `ExecutorRunResponse` pydantic models; `build_routing` / `build_executor_response`.

**Modified (additive only):**
- `app.py` — `from aee.runtimes.executor_api import ExecutorRunRequest`; `_attempt_telegram` helper; `POST /runs/executor` handler. **+246 / -0.**

**New tests (stdlib/pytest, no network; fake-binary fixtures):**
- `tests/_executor_test_helpers.py` — shared TestClient + fake-claude-binary helpers.
- `tests/test_executor_routing.py`, `tests/test_executor_claude_code_cli.py`, `tests/test_executor_no_forced_minimax.py`, `tests/test_executor_unsupported.py`, `tests/test_executor_timeout_cancel.py`, `tests/test_executor_artifact_evidence.py`, `tests/test_executor_response_contract.py`.
- `tests/smoke_executor_real_cli.py` — gated real-CLI smoke (`AEE_REAL_CLI_SMOKE=1`).

**New GPT artifacts:**
- `gpt/aee_executor_openapi.json`, `gpt/GPT_SETUP_GUIDE.md`, `gpt/examples/curl_executor_claude_code_cli.sh`.

**New reporting:**
- `reports/AEE_FINAL_MILE_EXECUTOR_WIRING_REPORT.md` (this file), `reports/aee_final_mile_executor_verification_sidecar.json`, `reports/aee_final_mile_real_cli_smoke_envelope.json`.

## 7. API/Tool Contract

`POST /runs/executor` (Bearer auth via bridge client keys).

**Request (`ExecutorRunRequest`):** `executor` (opt; aliases canonicalise to `claude-code-cli`), `prompt` (required, verbatim), `expected_artifacts` (abs paths), `timeout_sec`, `repo_path` (allow-listed), `max_turns`.

**Response (`ExecutorRunResponse`, §2.4):** `selected_executor`, `requested_executor`, `run_id`, `task_id`, `status` (queued|running|completed|failed|timeout|cancelled), `progress` (0.0–1.0), `routing` (`requested_executor`, `selected_executor`, `effective_executor`, `selection_source`, `was_forced`, `reason`, `effective_model`), `artifact_paths`, `stdout_summary`, `stderr_summary`, `exit_code`, `timeout_state`, `cancel_state`, `git_evidence` (`head_sha`, `branch`, `dirty`, `staged_file_count`, `repo_path`), `artifact_verification` (per-path `exists`/`size`/`mtime`/`sha256`), `telegram_result` (`success`/`message_id`/`recipient` or `skipped`), `error`.

Errors: 400 `unsupported_executor` (or `repo_path_not_allowed`); 401 auth; 422 malformed body.

## 8. Executor Routing Evidence

Acceptance A/B/C (unit, fake binary):
- `executor=claude-code-cli` → `selected_executor == "claude-code-cli"`, `routing.effective_executor == "claude-code-cli"`, `selection_source == "explicit"`.
- alias `claude_code` → response canonicalises to `selected_executor == "claude-code-cli"` (`requested_executor` preserves the raw `claude_code`).
- omitted `executor` → `selected_executor == "claude-code-cli"` (configured default), `selection_source == "default"`, `was_forced == false`.
- `routing.was_forced` is **always false** on this endpoint; the string `MiniMax` does **not** appear anywhere in the routing block (asserted by `test_executor_no_forced_minimax.py`).

## 9. Real Claude Code CLI Smoke Test

Acceptance D — **real binary, not mocked.** Gated test `tests/smoke_executor_real_cli.py` run with `AEE_REAL_CLI_SMOKE=1`, invoking the real `/home/ubuntu/.local/bin/claude` (v2.1.216) through `POST /runs/executor`. The worker prompt created `/tmp/aee_executor_smoke.md`.

Result envelope (`reports/aee_final_mile_real_cli_smoke_envelope.json`):
- `selected_executor`: `claude-code-cli`
- `status`: `completed`; `exit_code`: `0`
- `stdout_summary`: `` Created `/tmp/aee_executor_smoke.md` with the content `hello from claude-code-cli`. `` (real)
- `artifact_paths`: `["/tmp/aee_executor_smoke.md"]`
- `artifact_verification`: `{exists: true, size: 26, mtime: 1784613935, sha256: 9b3a23cbaa104b4f1dc3ccf35d714bface1d32cdc110de7162a939e08c91651f}`
- `routing.effective_executor`: `claude-code-cli`; `was_forced`: false; no `MiniMax`.
- On-disk file content: `hello from claude-code-cli` (verified).

**Scoped permission note:** the worker was granted `--allowedTools=Write` (via `AEE_CLAUDE_EXTRA_ARGS`) so the non-interactive CLI can create the declared `/tmp` artifact. This is a **single-tool, explicit, scoped grant** — the standard non-interactive Claude Code pattern — and is **not** `--dangerously-skip-permissions`. No shell escape; `create_subprocess_exec` with an arg list.

## 10. Tests

New suite (`.venv/bin/python -m pytest tests/test_executor_*.py tests/test_executor_timeout_cancel.py -q`): **17 passed, 0 failed.**

| File | Tests | Focus |
|------|-------|-------|
| `test_executor_routing.py` | 3 | A/B/C: honored request, alias canonicalisation, default |
| `test_executor_claude_code_cli.py` | 2 | B/D(unit): end-to-end selected_executor + non-zero-exit failed |
| `test_executor_no_forced_minimax.py` | 2 | C/I: no MiniMax in routing, effective_executor != MiniMax |
| `test_executor_unsupported.py` | 2 | E: 400 `unsupported_executor`, no silent fallback |
| `test_executor_timeout_cancel.py` | 3 | timeout=exceeded (endpoint + runner), cancel=requested (runner) |
| `test_executor_artifact_evidence.py` | 3 | F: artifact_verification, git_evidence, telegram_result shapes |
| `test_executor_response_contract.py` | 2 | F: full §2.4 envelope (claude-code-cli + hermes) |

Real-CLI smoke (`smoke_executor_real_cli.py`, gated): **1 passed** with `AEE_REAL_CLI_SMOKE=1` (acceptance D).

## 11. Regression

Existing executor suite re-run, **no regressions**:
- `test_executor_router.py` + `test_executor_routing_evidence.py` + `test_openapi_executor_metadata.py`: **30 passed.**
- `test_claude_code_executor.py` + `test_claude_executor_integration.py`: **35 passed.**

`app.py` diff is **+246 / -0** (purely additive); `create_run` and the MiniMax routing block are byte-for-byte unchanged. No existing caller or `metadata.executor` path is affected.

## 12. GPT Integration Artifacts

- `gpt/aee_executor_openapi.json` — OpenAPI **3.0.3**, validates with `openapi-spec-validator 0.9.0` (acceptance G). `servers` uses the placeholder `https://AEE_RUNTIME_BRIDGE_BASE_URL_PLACEHOLDER`; `bearerAuth` security scheme; `x-openai-is-consequential: true`; `ExecutorRunRequest`/`ExecutorRunResponse` schemas match §2.4; two request examples (claude-code-cli, hermes) + a 200 example + a 400 `unsupported_executor` example.
- `gpt/GPT_SETUP_GUIDE.md` — create/import GPT action, bearer token handling (never printed), curl verification for `claude-code-cli` (expect `selected_executor` echo), unsupported-executor 400 check, switch to `hermes`, rollback (disable action without deleting the GPT), natural-language action description.
- `gpt/examples/curl_executor_claude_code_cli.sh` — the §3.3 curl example, executable.

## 13. Artifact Verification

Per-deliverable `ls -la` / `wc -c` / `wc -l` / `sha256sum`, plus OpenAPI validator output and report-heading grep, are recorded in the machine-readable sidecar `reports/aee_final_mile_executor_verification_sidecar.json` (one entry per deliverable). The sidecar is valid JSON; the report's required headings are confirmed via `grep -E '^(#|##) '`.

## 14. Git Evidence

From the real-CLI smoke run (`repo_path=/home/ubuntu/Abacus`, read-only):
- `head_sha`: `869a77f1b019dd23b6732dffdbd11676d2bc6583`
- `branch`: `master`
- `dirty`: `true`
- `staged_file_count`: `1`
- `repo_path`: `/home/ubuntu/Abacus`

(This reflects the Abacus repo's pre-existing working state at smoke time; the `/tmp` artifact write does not touch that repo. Read-only: only `git rev-parse` / `git status --porcelain` were run — no mutating git.)

## 15. Production Safety

- No `git commit/push/merge/rebase/stash/reset/add -A`. No file delete/move. No service restart/deploy. No supervisord edit. No secret rotation.
- `app.py` change is additive (+246/-0); existing `/runs`, MiniMax routing, and all existing tests untouched.
- The CLI worker runs with safety defaults: no `--dangerously-skip-permissions`; the only grant is a scoped `--allowedTools=Write` for the smoke's declared `/tmp` artifact; prompt forwarded verbatim (never rewritten); no shell escape (`asyncio.create_subprocess_exec`, arg list).
- Review-only by default; the atomic commit is a separate downstream step authorized by 鼎鼎.

## 16. Remaining Risks

- **Claude Code CLI version drift:** known-good v2.1.216 at `/home/ubuntu/.local/bin/claude`. The binary path is config-driven (`config/executor.json` `claude_cli_binary` / `AEE_CLAUDE_CLI_BINARY`).
- **`--allowedTools` for non-interactive writes:** the real CLI requires an explicit, scoped tool grant to create files in `-p` mode. This is bounded (single tool, declared artifact under `/tmp`) and is not a dangerous-skip; operators must scope it appropriately for production prompts.
- **`--bare` default off:** kept configurable; enabling it on an OAuth-hosted session can produce "Not logged in" unless `ANTHROPIC_API_KEY` is also supplied.
- **Envelope `telegram_result` skipped:** the bridge env has no `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`; the per-run envelope honestly reports `skipped`. The §9 report Telegram is sent via `hermes send` (creds in `/home/ubuntu/.hermes/.env`).
- **Real CLI smoke cost:** bounded by a tight single-file `/tmp` prompt and `max_turns=10`.

## 17. Review Ready

Yes. A reviewer can: (a) re-run the 17 unit tests + 65 existing tests; (b) run `AEE_REAL_CLI_SMOKE=1 .venv/bin/python -m pytest tests/smoke_executor_real_cli.py -s` to reproduce acceptance D; (c) validate `gpt/aee_executor_openapi.json` with `openapi-spec-validator`; (d) follow `gpt/GPT_SETUP_GUIDE.md` cold to import the GPT action. The verification sidecar provides per-file hashes for tamper-checking.

## 18. Commit Ready

**No — by design.** Per the work-order production-safety red lines and 鼎鼎's directive, this is review-only. The atomic commit is a separate downstream step. Suggested commit scope (for the downstream turn): the new `aee/runtimes/executor_*.py`, `config/executor.json`, `app.py` (additive), `tests/test_executor_*.py` + `tests/_executor_test_helpers.py` + `tests/smoke_executor_real_cli.py`, `gpt/`, and `reports/AEE_FINAL_MILE_*` + sidecar. The 9 pre-existing unrelated tracked-modified files remain out of scope.

## 19. Telegram

A real external Telegram send was attempted via `hermes send` (creds in `/home/ubuntu/.hermes/.env`; recipient chat `5132341473`). The short version (~9 fields, ≤15 lines) was sent with this report attached. Result recorded here post-send:

- **Sent:** yes
- **Method:** `hermes send --to telegram:5132341473 --json --subject "…" "MEDIA:<report>"`
- **Platform:** telegram
- **Recipient (chat_id):** `5132341473` (鼎鼎)
- **Result JSON:** `{"success": true, "platform": "telegram", "chat_id": "5132341473", "message_id": "7684", "mirrored": true}`
- **`success`:** `true`
- **`message_id`:** `7684`
- **Exit code:** `0`
- **Attachment:** this report, sent as a Telegram document via `MEDIA:`.

The short version (~9 fields, ≤15 lines) was delivered with the report attached. The per-run envelope `telegram_result` is `skipped` (bridge env has no Telegram creds); the §9 report-level send above is the real external notification.

---

## Final Verdict

**PASS**

Acceptance D is proven with the real Claude Code CLI binary (real stdout, real exit_code 0, real non-zero artifact). A–C, E–J pass; OpenAPI validates (G); no second orchestrator (I); Hermes legacy path intact (J); no regressions. The single non-blocking caveat (envelope `telegram_result` skipped for lack of bridge-env creds) does not affect the verdict; the §9 report Telegram was sent via `hermes send`.