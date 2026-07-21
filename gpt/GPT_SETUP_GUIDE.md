# GPT Setup Guide — AEE Executor Run Action

This guide explains how to wire a Custom GPT to the AEE Runtime Bridge
`POST /runs/executor` endpoint so the GPT can dispatch work to an
explicit executor (`claude-code-cli` or `hermes`) and receive the full
evidence envelope.

A reviewer can follow this cold and import the action without any
external dependency — all artifacts live in this repo under `gpt/`.

---

## 1. Prerequisites

- The AEE Runtime Bridge is running and reachable at a public base URL
  (typically a cloudflared tunnel host). Call it `$BRIDGE_URL`.
- A bridge bearer token exists on the bridge host. It is read by the
  bridge from one of these env vars (any one is sufficient):
  `BRIDGE_API_KEY`, `GPT_BRIDGE_API_KEY`, `CLAUDE_BRIDGE_API_KEY`,
  `CURSOR_BRIDGE_API_KEY`, `MCP_BRIDGE_API_KEY`.
  For the GPT channel the convention is to set `GPT_BRIDGE_API_KEY`.
- The OpenAPI schema: `gpt/aee_executor_openapi.json`.
- `curl` and `jq` for verification.

> **Never print or embed the token value in this guide, in the GPT
> instructions, or in screenshots.** The token is set only in the
> Custom GPT Action's Authorization header field.

---

## 2. Capability discovery (`GET /executors`)

Before dispatching a run, discover what this bridge supports. The
`GET /executors` endpoint is **read-only** — no dispatch, no task
creation, no executor launch, no side effects. It exists only for
capability discovery, so a GPT can pick an executor programmatically
instead of hard-coding one.

```bash
curl -sS -X GET -H "Authorization: Bearer ${AEE_BRIDGE_TOKEN}" \
  "${AEE_RUNTIME_BRIDGE_BASE_URL}/executors" | jq
```

Expected 200 response:

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

The recommended GPT workflow is the three-step discovery → select →
dispatch loop:

```
GET /executors            → read supported_executors + default_executor + aliases
        ↓
select executor           → pick one of supported_executors (or use default_executor);
                             accept any key in `aliases` and canonicalise it
        ↓
POST /runs/executor       → dispatch with executor=<selected>, prompt=<task>,
                             expected_artifacts=[<outputs>], timeout_sec=<N>
```

In the GPT's natural-language instructions, tell it to: call
`aee_list_executors` first, choose an executor from
`supported_executors` (canonicalising any alias via `aliases`), then
call `aee_executor_run` with the chosen executor. If the caller's
requested executor is not in `supported_executors` **and** not in
`aliases`, do not retry — escalate. Never echo the bearer token.

The dispatch response now also carries a `runtime_identity` block
(provider, provider_version, executor_binary, executor_version,
runtime_bridge_version, bridge_commit, bridge_branch,
bridge_repository, generated_at_utc). Every field is factual —
`null` / `"unknown"` when its source is unavailable — so a reviewer can
confirm exactly which provider / executor / bridge produced a run.

---

## 3. Create / update the Custom GPT

1. In ChatGPT: **Settings → Beta features → Custom GPTs → Create** (or
   edit your existing AEE GPT).
2. Under **Configure**, scroll to **Actions → Create new action** (or
   edit the existing AEE action).
3. **Import schema**: choose *Import from file* and upload
   `gpt/aee_executor_openapi.json`. (Or paste the JSON into the editor.)
   - The schema's `servers[0].url` is the placeholder
     `https://AEE_RUNTIME_BRIDGE_BASE_URL_PLACEHOLDER`. After import,
     set the action's base URL to your real `$BRIDGE_URL` (the GPT
     builder lets you override the server URL, or you can edit the
     imported JSON to replace the placeholder before upload).
4. **Authentication**: set the action's auth to *API Key* / *Bearer*,
   header name `Authorization`, value `Bearer <your token>`. Paste the
   token value from `GPT_BRIDGE_API_KEY` on the bridge host — the GPT
   stores it server-side; it is not shown to end users.
5. **Consequential**: the schema marks the POST with
   `x-openai-is-consequential: true`, so the GPT will not auto-fire it;
   it always asks before calling. Keep that.
6. Save the action and the GPT.

---

## 4. Verify executor selection (`claude-code-cli`)

Run this from any host with network access to the bridge:

```bash
export AEE_BRIDGE_TOKEN="<token from GPT_BRIDGE_API_KEY on the bridge host>"
export AEE_RUNTIME_BRIDGE_BASE_URL="https://<your-bridge-public-host>"
bash gpt/examples/curl_executor_claude_code_cli.sh
```

Expected 200 response (abridged):

```json
{
  "selected_executor": "claude-code-cli",
  "requested_executor": "claude-code-cli",
  "status": "completed",
  "exit_code": 0,
  "routing": {
    "effective_executor": "claude-code-cli",
    "was_forced": false,
    "reason": "explicit_executor_opt_in"
  },
  "artifact_paths": ["/tmp/aee_executor_smoke.md"],
  "artifact_verification": [
    { "path": "/tmp/aee_executor_smoke.md", "exists": true, "size": 26, "sha256": "..." }
  ]
}
```

Confirm:
- `selected_executor == "claude-code-cli"` (the request was honored).
- `routing.effective_executor == "claude-code-cli"` and `was_forced == false`.
- The string `MiniMax` does **not** appear anywhere in `routing`.
- `artifact_verification[].exists == true` for the declared file.

The alias `claude_code` (underscore) is also accepted and canonicalises
to `claude-code-cli` in the response.

---

## 5. Verify the unsupported-executor path (deterministic 400)

```bash
curl -sS -X POST -H "Authorization: Bearer ${AEE_BRIDGE_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"executor":"gemini","prompt":"anything","timeout_sec":30}' \
  "${AEE_RUNTIME_BRIDGE_BASE_URL}/runs/executor" | jq
```

Expected HTTP **400** with:

```json
{
  "detail": {
    "code": "unsupported_executor",
    "message": "executor 'gemini' is not supported; ...",
    "supported_executors": ["claude-code-cli", "hermes"]
  }
}
```

There is **no silent fallback** — an unsupported executor never produces
a 200 run.

---

## 6. Switch back to Hermes (legacy executor)

Set `executor` to `hermes` (or omit it and set the bridge's configured
default to `hermes` via `config/executor.json` / `AEE_EXECUTOR_DEFAULT`):

```bash
curl -sS -X POST -H "Authorization: Bearer ${AEE_BRIDGE_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"executor":"hermes","prompt":"summarize AEE runtime status","timeout_sec":120}' \
  "${AEE_RUNTIME_BRIDGE_BASE_URL}/runs/executor" | jq
```

Expected 200 with `selected_executor == "hermes"`,
`routing.effective_executor == "hermes"`, and `status` reflecting the
queued/running Hermes submission (Hermes is async; the per-run artifact
/git/telegram evidence fields are null or skipped on submit).

---

## 7. Rollback (disable the action without deleting the GPT)

1. In the GPT builder, open the action.
2. Toggle the action **off** (or delete just the action). The GPT itself
   and its other actions are untouched.
3. To re-enable, toggle it back on / re-import the schema.

No bridge-side change is required to disable the GPT action — the
endpoint remains available for CLI / CI use; only the GPT surface is
turned off.

---

## 8. Natural-language action description for the GPT

Paste this into the action's *Instructions* (or the GPT's main
instructions):

> First call `aee_list_executors` to read `supported_executors`,
> `default_executor`, and `aliases`. Then call `aee_executor_run` with
> `executor=<one of supported_executors, or an alias canonicalised via
> aliases>`, `prompt=<the user's task>`,
> `expected_artifacts=[<declared outputs>]`, and a `timeout_sec`
> appropriate to the task. Report `selected_executor`, `status`,
> `exit_code`, `artifact_paths`, `runtime_identity`, and any
> `artifact_verification` entries where `exists` is false. Do NOT retry
> on `unsupported_executor`; escalate instead. Do NOT pass
> `executor=minimax` or any value outside
> `["claude-code-cli", "hermes"]`. Never echo the bearer token.

---

## 9. Files in this guide

| File | Purpose |
|------|---------|
| `gpt/aee_executor_openapi.json` | Importable OpenAPI 3.0 schema for the action. |
| `gpt/examples/curl_executor_claude_code_cli.sh` | Verifiable curl smoke for `executor=claude-code-cli`. |
| `gpt/GPT_SETUP_GUIDE.md` | This guide. |