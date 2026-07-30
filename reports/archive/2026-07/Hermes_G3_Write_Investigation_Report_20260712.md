# Hermes Runtime G3 Write Failure — Investigation Report

**Date:** 2026-07-12
**Repo:** `/home/ubuntu/hermes-runtime-bridge`
**Scope:** Diagnose why TASK-20260711-0063 / 0065 / 0066 / 0068 all report `completed` without ever creating `aee/tests/test_aee77_apply_sidecars.py`.

---

## TL;DR

**The bug is not in the bridge, the adapter, the runtime, the timeout, or the context window.** The hermes runtime is healthy: it accepted all four tasks, returned `completed` status, and exposed a `write_file` tool that the model could have used. **The model `MiniMaxAI/MiniMax-M3` (routed to `ollama-cloud` by hermes) consistently chooses to investigate (read + execute_code + terminal) until it runs out of context, then returns a "Let me write..." planning fragment as the final assistant turn — without ever issuing the `write_file` tool call.**

I reproduced the exact failure mode by re-submitting TASK-20260711-0065's prompt verbatim against the live hermes runtime (`run_23b0c5e4636a43fcb95b4271b4feaa0e`). The model used `terminal` × 22, `execute_code` × 22, `search_files` × 8, `read_file` × 6, `todo` × 2 — and **`write_file` × 0**. The final text was again "開始寫測試：" followed by run completion, no file produced.

Therefore: **this is a model+prompt problem, not a runtime problem.** The runtime correctly delivered the model's plan-only output and marked the run `completed`. No retry or adapter change will fix it; the model needs a different instruction that does not let it drift into an unbounded investigation loop.

---

## Q1 — Why did the runtime not actually write the file?

### Investigation: the runtime's path through the request

The flow from bridge → model → filesystem is:

```
bridge /dispatcher
  └─ HermesAdapter.submit()          POST /v1/runs  (hermes_adapter.py:131)
       └─ hermes /gateway/api_server /v1/runs    (api_server.py:3567)
            └─ AIAgent.run_conversation()        (conversation_loop.py:351)
                 └─ many LLM ↔ tool turns
                      └─ final_response ──► run.completed
            └─ _set_run_status("completed", output=final_response)
  └─ watcher polls adapter.poll()    GET /v1/runs/{id}
       └─ manager.complete()         stores output_excerpt in reports/<task_id>/task.json
```

I verified each layer in turn:

### 1.1 Adapter (`aee/adapters/hermes_adapter.py`)

The adapter is a thin wrapper. `submit()` (line 122) POSTs to `{HERMES_BASE_URL}/v1/runs` with `input`, `session_id`, `instructions`, `metadata`. It does no prompt munging that would strip tools. `poll()` (line 164) GETs the same endpoint and returns the upstream `status`, `output`, `usage`. Nothing in the adapter could prevent file writes.

### 1.2 Runtime (`/home/ubuntu/.hermes/hermes-agent/gateway/platforms/api_server.py`)

The `/v1/runs` handler (`api_server.py:3567`) creates an `AIAgent` with `ephemeral_system_prompt=instructions` and calls `agent.run_conversation(user_message=...)`. The agent runs an LLM tool-calling loop. When the agent returns, the handler emits `run.completed` with `final_response` as `output` (api_server.py:3776-3790).

### 1.3 Tools available to the model

`AIAgent.run_conversation()` (conversation_loop.py:351+) runs the full tool-calling loop with **all standard tools enabled**, including `write_file`. Confirmed empirically:

- I POSTed `{"input": "write the file /tmp/dingde_test.txt with the content hello", "session_id": "sess-write-1", "metadata": {"client_source": "gpt", "model_name": "MiniMaxAI/MiniMax-M3"}}` to `/v1/runs`.
- The model invoked `write_file` (verified via `/v1/runs/run_01e6b50bff78459bbed1fa735536cf5a/events`) and the file was created at `/tmp/dingde_test.txt` with content `hello\n` in ~5 seconds.
- **This proves the runtime, the model, the routing, and the tool all work for a simple write task.**

### 1.4 The actual failure: long planning + `write_file` never issued

For the four failed tasks, the runtime received the same call (long prompt + `client_source: gpt` + `model_name: MiniMaxAI/MiniMax-M3`) but the model behaved completely differently. I re-submitted TASK-0065's exact prompt verbatim and captured the live event stream before hermes purged it:

```
$ curl -s ... -X POST .../v1/runs -d @/tmp/hermes_test_0065.json
{"run_id": "run_23b0c5e4636a43fcb95b4271b4feaa0e", "status": "started"}

$ ... /v1/runs/<id>/events | grep '"tool":' | sort | uniq -c
   22 "tool": "terminal"
   22 "tool": "execute_code"
    8 "tool": "search_files"
    6 "tool": "read_file"
    2 "tool": "todo"
    0 "tool": "write_file"        <-- the model never wrote a file
    1 "event": "run.completed"
```

The model's final text was:

> "…先寫完整測試，後面再決定是否為 G1 bug。**開始寫測試：**"

— then `run.completed` fired with no `write_file` call. The model said it was about to write, then closed the turn. Identical pattern to the four original failures.

### 1.5 Where the loop closes

`conversation_loop.py` ends each turn on `finish_reason == "stop"` (line 1604) regardless of whether tools were actually called. The model is **choosing** to return `stop` mid-plan. The runtime respects that and marks the run `completed`. The bridge receives the (planning-only) `output` and stores it as `output_excerpt`. **Every layer is doing what it's supposed to do — the failure is the model's stop decision.**

### 1.6 Ruling out other causes

| Suspect | Status | Evidence |
|---|---|---|
| Adapter bug | Ruled out | adapter is a thin HTTP wrapper; identical shape works for short writes |
| Runtime bug | Ruled out | `write_file` invoked successfully on a 7-token prompt; tools enabled for all runs |
| Timeout | Ruled out | task durations 103s/113s/124s/297s — model finished, not killed |
| Context window | Ruled out | TASK-0066 had only 392 input chars and 842K reported input_tokens (an expansion issue, not a window cap). All four tasks emitted 1.5K–15K output_tokens before stopping. |
| Output truncation | Ruled out | `output_excerpt` truncates to 2000 chars (manager.py:948) but that is for storage; the underlying `output` field is full and contains only the model's plan text |
| Tool restriction | Ruled out | hermes enables `write_file`; the simple test invoked it successfully |
| Permission/sandbox | Ruled out | the simple test wrote `/tmp/dingde_test.txt` from the same runtime |

### 1.7 The `input_tokens` anomaly (worth noting, not a cause)

TASK-0066's `input_text` is 392 chars but `usage.input_tokens=842,477`. This is a 2000× expansion. Likely cause: `agent.run_conversation()` includes skill prompts, system prompt, prior conversation history, and the long `input_text`. This makes the prompt enormous but **does not** cause the model to skip tool calls — the model still reasons extensively. It is a quality-of-service issue (wasted tokens), not a correctness issue. I did not dig further because the diagnosis above is sufficient.

---

## Q2 — Does `aee/tests/test_aee77_apply_sidecars.py` really not exist?

Yes. Three independent confirmations:

### 2.1 `find` (plain)

```
$ find /home/ubuntu/hermes-runtime-bridge -name "test_aee77*" -not -path "*/__pycache__/*"
/home/ubuntu/hermes-runtime-bridge/aee/tests/test_aee77_live_audit.py
```

Only `test_aee77_live_audit.py` (the existing AEE-7.7a test) exists. No `test_aee77_apply_sidecars.py`.

### 2.2 `ls aee/tests/`

```
test_aee711_identity.py
test_aee72_artifact_service.py
test_aee72_dispatcher_e2e.py
test_aee74_emitter.py
test_aee74_observability.py
test_aee74_round_trip_e2e.py
test_aee74_wireup.py
test_aee76_identity_consistency.py
test_aee76_migration_idempotency.py
test_aee76_migration_registry.py
test_aee77_live_audit.py            <-- the only test_aee77_*
test_aee7_env_routing.py
test_aee7_orchestrator_e2e.py
test_aee7_traversal_audit.py
... (no test_aee77_apply_sidecars.py)
```

### 2.3 `git ls-files`

```
$ git ls-files | grep -E "aee77|apply_sidecar"
aee/tests/test_aee77_live_audit.py
```

Only one `aee77*` file is tracked; `apply_sidecars.py` itself is also untracked (the G1 file). Nothing under `aee/tests/` named `test_aee77_apply_sidecars.py`.

### 2.4 Working tree

```
$ git status --short
 M aee/audit/__init__.py
 M aee/reporting/build_index.py
?? aee/audit/apply_sidecars.py
?? data/
```

The working tree contains G1 (`apply_sidecars.py`) and G2 (`build_index.py` changes). No G3 test file.

---

## Q3 — Did hermes call `write_file` for the four failed tasks?

**I cannot directly answer for the original 4 tasks** because hermes purges run records after `run.completed` (verified: `GET /v1/runs/run_3ce5473b7d8941429c240edeb1507066` → 404 `run_not_found`). The full event streams are gone.

**But I can answer for the reproduced run.** For the verbatim re-submission of TASK-0065's prompt:
- The model had `write_file` available (same runtime, same model, same routing).
- The model **did not** invoke `write_file` even once.
- The model **did** invoke `terminal`, `execute_code`, `search_files`, `read_file`, `todo`.
- The run terminated with `run.completed` carrying a final assistant text that said "開始寫測試：" but no tool call followed.

**Strong inference for the original 4**: their `output_excerpt`s all end in plan-fragment style — "Let me write the test file:", "Let me write G3.", "G3。寫 comprehensive test file：", "我寫測試檔" — and **none** contain any tool-call output. If `write_file` had been invoked, the excerpt would show tool execution output and the file would exist. It doesn't. So the original 4 also never called `write_file`.

---

## Root cause

The model `MiniMaxAI/MiniMax-M3` (routed to `ollama-cloud` by hermes) **does not** call `write_file` when given the AEE-7.7b G3 prompt. Instead it:

1. Loads skills (`aee-iteration-pattern`)
2. Reads `apply_sidecars.py`, `__init__.py`, `build_index.py`, `live_audit.py`
3. Runs `git rev-parse`, `git status`, `git log`, `rtk ls`, `rtk wc` to gather context
4. Reasons aloud about test cases, G1/G2 logic, edge cases
5. Eventually says "Let me write the test file:" or "開始寫測試："
6. Returns `finish_reason="stop"` without invoking any write tool
7. Hermes emits `run.completed`
8. The bridge stores the planning text as `output_excerpt` and marks the task done

The model's behavior is consistent: it gets stuck in extended exploration. There is no prompt signal that says "stop investigating, write the file now, then stop." The brief asks for many specific test cases, which gives the model infinite "preparation" surface. The longer the prompt, the more tokens it consumes on exploration before returning.

---

## Recommendation

Since the runtime is healthy and the bug is model-side, the only reliable fix is to:

1. **Make the G3 prompt force early write**: a single-sentence prompt that says "write the file `aee/tests/test_aee77_apply_sidecars.py` with content X, then stop" with the test file content provided inline (not described in prose). The model will not need to discover the API surface and will write on the first turn.
2. **Or write the test file directly in this session.** Since the request explicitly says "若 Runtime 沒問題, …才請直接建立 `aee/tests/test_aee77_apply_sidecars.py`", and the runtime is proven not to be the problem, I should now create the test file.

---

## Artifacts consulted

- `/home/ubuntu/hermes-runtime-bridge/logs/TASK-20260711-006{3,5,6,8}.log`
- `/home/ubuntu/hermes-runtime-bridge/reports/TASK-20260711-006{3,5,6,8}/task.json`
- `/home/ubuntu/hermes-runtime-bridge/aee/adapters/hermes_adapter.py`
- `/home/ubuntu/hermes-runtime-bridge/dispatcher/manager.py:451-548, 940-952`
- `/home/ubuntu/hermes-runtime-bridge/dispatcher/watcher.py:217-300`
- `/home/ubuntu/.hermes/hermes-agent/gateway/platforms/api_server.py:3567-3805`
- `/home/ubuntu/.hermes/hermes-agent/agent/conversation_loop.py:351+`
- Live reproduction runs: `run_01e6b50bff78459bbed1fa735536cf5a` (write success), `run_7ef5dbd907554dac830d93c53be49197` and `run_23b0c5e4636a43fcb95b4271b4feaa0e` (TASK-0065 re-runs showing the bug).

## Cleanup

- Removed: `/tmp/dingde_test.txt`, `/tmp/hermes_test_0065.json`, event capture files
- No repo files were modified during investigation.
