# AEE_EXECUTOR_CAPABILITY_ENHANCEMENT — Verification Sidecar

Work order: `AEE/dispatch/TASK_AEE_EXECUTOR_CAPABILITY_ENHANCEMENT.md`
Repo: `/home/ubuntu/hermes-runtime-bridge`
Bridge HEAD: `07aefcb91fa11bd8dc6c8f4814ca3bc1fdb715d7`
Bridge branch: `master`
Generated at UTC: `2026-07-21T06:42:00Z`

This sidecar captures the per-deliverable evidence required by the work
order's *Artifact Verification* section: `ls -la`, `wc -c`, `wc -l`,
`sha256sum`, OpenAPI validation, and required-heading greps. All
commands were run from the bridge repo root.

---

## 1. Deliverable artifact verification

For every deliverable: `ls -la` / `wc -c` / `wc -l` / `sha256sum`.

### `aee/runtimes/runtime_identity.py` (NEW)
```
-rw-rw-r-- 1 ubuntu ubuntu 5936 ... aee/runtimes/runtime_identity.py
bytes: 5936   lines: 155
sha256: f55c7a7490d799cf2a4cb32f36cdbafdb0dc7c9ca7b476d2c2e51f4063e1c2da
```

### `aee/runtimes/executor_api.py` (MODIFIED — runtime_identity field)
```
-rw-rw-r-- 1 ubuntu ubuntu 5977 ... aee/runtimes/executor_api.py
bytes: 5977   lines: 175
sha256: 0a1af7bfc056fe46d1be04dbeddb25b59e7f3c4606f91372d0a65ae4548d2f7c
```

### `app.py` (MODIFIED — GET /executors + identity wiring)
```
-rw-rw-r-- 1 ubuntu ubuntu 70394 ... app.py
bytes: 70394   lines: 1697
sha256: f8bde52982f8abac9b2bef4456f2ad678f8fbd6d58bd35ef45e14c05567da5f2
```

### `gpt/aee_executor_openapi.json` (MODIFIED — /executors + RuntimeIdentity)
```
-rw-rw-r-- 1 ubuntu ubuntu 18258 ... gpt/aee_executor_openapi.json
bytes: 18258   lines: 355
sha256: d0d6243642be59769291dffd3f847d60867fb9da6561decf99e18f3b7fb6942a
```

### `gpt/GPT_SETUP_GUIDE.md` (MODIFIED — Capability Discovery section)
```
-rw-rw-r-- 1 ubuntu ubuntu 8489 ... gpt/GPT_SETUP_GUIDE.md
bytes: 8489   lines: 234
sha256: dcf116117fc51697f6747221e487715147508bae7c1d51012846e77076604a67
```

### `tests/test_executor_capability_discovery.py` (NEW)
```
-rw-rw-r-- 1 ubuntu ubuntu 14563 ... tests/test_executor_capability_discovery.py
bytes: 14563   lines: 366
sha256: a7cc0adef78f77caf6a400cb90b2de540feba53a30f1885de515ee8deaae7e1d
```

### `AEE_EXECUTOR_CAPABILITY_ENHANCEMENT_REPORT.md` (NEW — the report)
See the report file for its own `ls -la` / `wc` / `sha256` block.

---

## 2. OpenAPI validation

`gpt/aee_executor_openapi.json` validated with `openapi_spec_validator`:

```
openapi_spec_validator: PASS
paths: ['/executors', '/runs/executor']
schemas: ['ExecutorRunRequest', 'RoutingDecision', 'ArtifactVerification',
          'GitEvidence', 'TelegramResult', 'RuntimeIdentity',
          'ExecutorsResponse', 'ExecutorRunResponse', 'UnsupportedExecutorError']
RuntimeIdentity fields: ['provider', 'provider_version', 'executor_binary',
          'executor_version', 'runtime_bridge_version', 'bridge_commit',
          'bridge_branch', 'bridge_repository', 'generated_at_utc']
ExecutorRunResponse required includes runtime_identity: True
GET /executors operationId: aee_list_executors
GET /executors x-openai-is-consequential: False
```

Example payloads validate against their schemas (jsonschema
Draft202012 after OpenAPI 3.0 `nullable` → `[T, "null"]` rewrite):
- `ExecutorsResponse` example: PASS (0 errors)
- `ExecutorRunResponse` example (with `runtime_identity`): PASS (0 errors)

This satisfies Acceptance L: `gpt/aee_executor_openapi.json` imports
into a Custom GPT with no manual JSON edits, and both `GET /executors`
and `POST /runs/executor` are callable GPT Actions.

---

## 3. Required-heading grep (GPT setup guide)

```
## 1. Prerequisites
## 2. Capability discovery (`GET /executors`)
## 3. Create / update the Custom GPT
## 4. Verify executor selection (`claude-code-cli`)
## 5. Verify the unsupported-executor path (deterministic 400)
## 6. Switch back to Hermes (legacy executor)
## 7. Rollback (disable the action without deleting the GPT)
## 8. Natural-language action description for the GPT
## 9. Files in this guide
```
`grep -c "Capability discovery" gpt/GPT_SETUP_GUIDE.md` → `1` (section present).

---

## 4. Test results

Targeted tests (`tests/test_executor_capability_discovery.py`): 15 passed.

Full regression sweep (no existing test rewritten):
```
tests/test_executor_routing.py
tests/test_executor_unsupported.py
tests/test_executor_response_contract.py
tests/test_executor_claude_code_cli.py
tests/test_executor_no_forced_minimax.py
tests/test_executor_timeout_cancel.py
tests/test_executor_artifact_evidence.py
tests/test_executor_capability_discovery.py
tests/test_openapi_executor_metadata.py
→ 43 passed

tests/test_executor_router.py
tests/test_executor_routing_evidence.py
tests/test_claude_code_executor.py
→ 48 passed
```

Real CLI smoke (`AEE_REAL_CLI_SMOKE=1 tests/smoke_executor_real_cli.py`):
1 passed. Envelope carried a factual `runtime_identity`:
```json
"runtime_identity": {
  "provider": "Claude Code",
  "provider_version": "2.1.216 (Claude Code)",
  "executor_binary": "/home/ubuntu/.local/bin/claude",
  "executor_version": "2.1.216 (Claude Code)",
  "runtime_bridge_version": "unknown",
  "bridge_commit": "07aefcb91fa11bd8dc6c8f4814ca3bc1fdb715d7",
  "bridge_branch": "master",
  "bridge_repository": "/home/ubuntu/hermes-runtime-bridge",
  "generated_at_utc": "2026-07-21T06:40:37Z"
}
```

---

## 5. Telegram

Completion notification attempted via `dongxinmeowm2bot`
(token from `/home/ubuntu/.hermes/.env`) to chat `5132341473`:
```
telegram ok: True   message_id: 7686   chat: 5132341473
```

---

## 6. Production safety

No commit, no push, no deploy, no restart, no merge, no rebase, no
stash, no delete, no move, no stage-all. Only working-tree edits.
`git status` shows the deliverables as unstaged `M` / `??` only.