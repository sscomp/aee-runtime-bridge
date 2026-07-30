# AEE Executor OpenAPI — 3.1 Repair Report (for direct Custom GPT Action import)

Target file: `gpt/aee_executor_openapi.json`
Repository: `/home/ubuntu/hermes-runtime-bridge`
Bridge public origin: `https://hermes-runtime.biaobecue.com` (cloudflared → 127.0.0.1:8787)
Generated at UTC: 2026-07-21

---

## 1. Background

The prior `gpt/aee_executor_openapi.json` was OpenAPI **3.0.3**, used the
placeholder server URL `https://AEE_RUNTIME_BRIDGE_BASE_URL_PLACEHOLDER`,
and relied on the OpenAPI 3.0 `nullable: true` keyword. Custom GPT
Actions now expect OpenAPI **3.1** (JSON Schema 2020-12), where
`nullable: true` is invalid. This repair makes the file directly
importable into a Custom GPT Action with no manual JSON edits, and
points it at the real public HTTPS origin.

This is a **schema-only repair**. No runtime implementation
(`app.py`, `aee/`, adapters) and no tests were modified.

---

## 2. Changes made (the 10 required items)

| # | Requirement | Before | After | Status |
|---|---|---|---|---|
| 1 | OpenAPI version | `3.0.3` | `3.1.0` | ✅ |
| 2 | Server URL | placeholder | `https://hermes-runtime.biaobecue.com` (root origin, no path, no underscores) | ✅ |
| 3 | `GET /executors` description ≤ 300 chars | ~300 chars | 164 chars | ✅ |
| 4 | nullable → 3.1 JSON Schema | `nullable: true` scalars/`$ref` | scalars→type arrays; `$ref`→`anyOf`+`{"type":"null"}` | ✅ |
| 5 | Remove all `nullable: true` | many | 0 occurrences | ✅ |
| 6 | operationIds preserved | — | `aee_list_executors`, `aee_executor_run` | ✅ |
| 7 | `bearerAuth` + `x-openai-is-consequential` preserved | — | global + per-op; GET=false, POST=true | ✅ |
| 8 | Claude Code version examples factual | `1.0.0` | `2.1.216 (Claude Code)` (from real `claude --version`) | ✅ |
| 9 | Validation (JSON / 3.1 / no-nullable / desc≤300 / HTTPS root) | — | all PASS | ✅ |
| 10 | Do not modify runtime / tests | — | untouched; 15/15 existing tests still pass | ✅ |

Also bumped `info.version` `1.0.0 → 1.1.0` to reflect the spec change.

---

## 3. Nullable conversion detail

**Scalars → type arrays** (`["string","null"]` / `["integer","null"]` / `["boolean","null"]`):

- `RoutingDecision`: `requested_executor`, `effective_model`
- `ArtifactVerification`: `size`, `mtime`, `sha256`
- `GitEvidence`: `head_sha`, `branch`, `dirty`, `repo_path`
- `TelegramResult`: `message_id`, `recipient`, `skipped`
- `RuntimeIdentity`: `provider`, `provider_version`, `executor_binary`, `executor_version`, `bridge_commit`, `bridge_branch`, `bridge_repository`
- `ExecutorsResponse`: `default_executor`
- `ExecutorRunResponse`: `requested_executor`, `task_id`, `exit_code`, `timeout_state`, `cancel_state`, `error`

**Nullable `$ref` → `anyOf` + `{"type":"null"}`**:

- `ExecutorRunResponse.git_evidence` → `anyOf: [{$ref GitEvidence}, {type:null}]`
- `ExecutorRunResponse.runtime_identity` → `anyOf: [{$ref RuntimeIdentity}, {type:null}]`

`GitEvidence` and `RuntimeIdentity` definitions themselves had
`nullable: true` removed and are now plain `object` schemas; nullability
is expressed at the `$ref` usage site via `anyOf`.

After repair: `grep -c '"nullable"'` on the file = **0**.

---

## 4. Validation results

Run against the repaired file:

```
1. JSON parsing: PASS
2. OpenAPI 3.1 validation (openapi_spec_validator): PASS
3. no nullable:true remains: PASS (0 occurrences)
4. openapi version: 3.1.0 -> PASS
5. server URL: https://hermes-runtime.biaobecue.com
   scheme=https host=hermes-runtime.biaobecue.com path='' (no underscores) -> PASS
6. operationIds: [('get','/executors','aee_list_executors'),('post','/runs/executor','aee_executor_run')] -> PASS
7. security + consequential:
   global security=[{'bearerAuth':[]}] -> PASS
   /executors get:  bearerAuth, x-openai-is-consequential=False -> PASS
   /runs/executor post: bearerAuth, x-openai-is-consequential=True  -> PASS
8. operation description lengths:
   /executors get: 164 chars -> PASS
   /runs/executor post: (no description) -> PASS
9. example runtime_identity provider_version='2.1.216 (Claude Code)' executor_version='2.1.216 (Claude Code)' -> PASS
```

Example payloads still validate against their 3.1 schemas
(jsonschema Draft202012Validator over the 3.1 doc — `nullable` is now
gone so no rewrite is needed):
- `ExecutorsResponse` example: PASS (0 errors)
- `ExecutorRunResponse` example (with `runtime_identity`): PASS (0 errors)

Existing targeted tests (`tests/test_executor_capability_discovery.py`)
re-run unchanged: **15 passed**. No runtime / test file was edited.

---

## 5. Artifact verification

```
path:   /home/ubuntu/hermes-runtime-bridge/gpt/aee_executor_openapi.json
sha256: 82a1f7b87c7965bfce0330841eb3471370c150f8c4ac8ec10ad5555c935d242b
bytes:  17946
lines:  352
```

---

## 6. Live status note

The repaired schema is correct and importable as a file. A live
`GET /executors` against the public origin currently returns **404**
because the long-running bridge process (pid 730930) was started before
the `/executors` / `/runs/executor` routes were added; Production Safety
forbids restarting it in this task. The routes are proven via the
in-process FastAPI TestClient (15/15 tests). After the operator
restarts the bridge, `https://hermes-runtime.biaobecue.com/executors`
and `.../runs/executor` will serve live.

---

## 7. Fixed import URL — not yet available

There is currently **no** public URL that returns this trimmed GPT
schema. The bridge's auto-generated `/openapi.json`
(`https://hermes-runtime.biaobecue.com/openapi.json`) exists but is the
**full 44-route bridge API**, not the GPT-tuned two-route schema, so it
is unsuitable for direct Custom GPT import.

To get a one-click import URL, a dedicated read-only route (e.g.
`GET /gpt/openapi.json`) that serves `gpt/aee_executor_openapi.json`
would need to be added to the bridge, then the fixed URL becomes:

```
https://hermes-runtime.biaobecue.com/gpt/openapi.json
```

This is a separate, optional follow-up (touches `app.py` + requires a
bridge restart). See the question at the end of the accompanying chat
message.

---

## 8. Production safety

No commit, push, deploy, **restart**, merge, rebase, stash, delete,
move, or stage-all. Only the working-tree file
`gpt/aee_executor_openapi.json` was modified (the `gpt/` directory is
untracked in git).

---

## 9. Final verdict

**PASS.** The repaired `gpt/aee_executor_openapi.json` is valid
OpenAPI 3.1, points at the real public HTTPS root origin, contains no
`nullable: true`, keeps both operationIds + bearerAuth + consequential
flags, uses factual Claude Code version examples, and validates
(spec + examples). Directly importable into a Custom GPT Action as a
file upload. Runtime and tests untouched.